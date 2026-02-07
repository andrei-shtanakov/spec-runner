#!/usr/bin/env python3
"""
spec-runner — task automation from markdown specs via Claude CLI

Usage:
    spec-runner run                    # Execute next task
    spec-runner run --task=TASK-001    # Execute specific task
    spec-runner run --all              # Execute all ready tasks
    spec-runner run --milestone=mvp    # Execute milestone tasks
    spec-runner status                 # Execution status
    spec-runner retry TASK-001         # Retry failed task
    spec-runner logs TASK-001          # Task logs
"""

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

import yaml

from .task import (
    Task,
    get_next_tasks,
    get_task_by_id,
    mark_all_checklist_done,
    parse_tasks,
    update_task_status,
)

# === Progress Logging ===


def log_progress(message: str, task_id: str | None = None):
    """Log progress message with timestamp to progress file."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{task_id}] " if task_id else ""
    line = f"[{timestamp}] {prefix}{message}\n"

    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "a") as f:
        f.write(line)

    # Also print to stdout
    print(line.rstrip())


def check_error_patterns(output: str) -> str | None:
    """Check output for API error patterns. Returns matched pattern or None."""
    output_lower = output.lower()
    for pattern in ERROR_PATTERNS:
        if pattern.lower() in output_lower:
            return pattern
    return None


def _send_callback(
    callback_url: str,
    task_id: str,
    status: str,
    duration: float | None = None,
    error: str | None = None,
) -> None:
    """Send task status callback to orchestrator.

    Uses urllib to avoid adding dependencies. Errors are silently
    ignored — callback is best-effort, state file is the fallback.

    Args:
        callback_url: URL to POST status to.
        task_id: Task identifier.
        status: Task status (started, success, failed).
        duration: Execution duration in seconds.
        error: Error message if failed.
    """
    if not callback_url:
        return

    import urllib.request

    payload: dict[str, str | float] = {
        "task_id": task_id,
        "status": status,
        "timestamp": datetime.now().isoformat(),
    }
    if duration is not None:
        payload["duration_seconds"] = duration
    if error:
        payload["error"] = error

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            callback_url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass  # Best-effort, state file is the fallback


class ExecutorLock:
    """File lock to prevent parallel executor runs."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.lock_file: TextIO | None = None

    def acquire(self) -> bool:
        """Try to acquire lock. Returns True if successful."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file = open(self.lock_path, "w")  # noqa: SIM115
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.lock_file.write(f"PID: {os.getpid()}\nStarted: {datetime.now().isoformat()}\n")
            self.lock_file.flush()
            return True
        except BlockingIOError:
            self.lock_file.close()
            self.lock_file = None
            return False

    def release(self):
        """Release the lock."""
        if self.lock_file:
            fcntl.flock(self.lock_file, fcntl.LOCK_UN)
            self.lock_file.close()
            self.lock_file = None
            with contextlib.suppress(FileNotFoundError):
                self.lock_path.unlink()


# Configuration file path
CONFIG_FILE = Path("spec/executor.config.yaml")
PROGRESS_FILE = Path("spec/.executor-progress.txt")

# Error patterns for graceful exit (rate limits, context window, etc.)
ERROR_PATTERNS = [
    "you've hit your limit",
    "rate limit exceeded",
    "context window",
    "quota exceeded",
    "too many requests",
    "anthropic.RateLimitError",
]


@dataclass
class ExecutorConfig:
    """Executor configuration"""

    max_retries: int = 3  # Max attempts per task
    retry_delay_seconds: int = 5  # Pause between attempts
    task_timeout_minutes: int = 30  # Task timeout
    max_consecutive_failures: int = 2  # Stop after N consecutive failures
    on_task_failure: str = "skip"  # What to do when task fails: skip | stop | ask

    # Claude CLI
    claude_command: str = "claude"  # Claude CLI command
    claude_model: str = ""  # Model (empty = default)
    skip_permissions: bool = True  # Skip permission prompts
    # Command template for custom CLIs. Placeholders: {cmd}, {model}, {prompt}, {prompt_file}
    # Examples:
    #   claude: "{cmd} -p {prompt}" or "{cmd} -p {prompt} --model {model}"
    #   codex: "{cmd} -p {prompt}"
    #   ollama: "{cmd} run {model} {prompt}"
    #   llama-cli: "{cmd} -m {model} -p {prompt} --no-display-prompt"
    #   llama-server: "curl -s http://localhost:8080/completion -d '{{\"prompt\": {prompt}}}'"
    # If empty, auto-detects based on command name
    command_template: str = ""

    # Hooks
    run_tests_on_done: bool = True  # Run tests on completion
    create_git_branch: bool = True  # Create branch on start
    auto_commit: bool = True  # Auto-commit on success
    main_branch: str = ""  # Main branch name (empty = auto-detect: main/master)

    # Code review
    run_review: bool = True  # Run code review after task completion
    review_timeout_minutes: int = 15  # Review timeout
    review_command: str = ""  # Review CLI command (empty = use claude_command)
    review_model: str = ""  # Review model (empty = use claude_model)
    # Review command template (if empty, uses command_template or auto-detect)
    review_command_template: str = ""

    # Paths
    project_root: Path = Path(".")
    logs_dir: Path = Path("spec/.executor-logs")
    state_file: Path = Path("spec/.executor-state.json")

    # Callback URL for reporting task progress to orchestrator
    callback_url: str = ""

    # Spec file prefix (e.g. "phase5-" for phase5-tasks.md)
    spec_prefix: str = ""

    # Test command (using uv)
    test_command: str = "uv run pytest tests/ -v -m 'not slow'"
    lint_command: str = "uv run ruff check ."
    lint_fix_command: str = "uv run ruff check . --fix"  # Lint auto-fix command
    run_lint_on_done: bool = True  # Run lint on completion
    lint_blocking: bool = True  # Lint errors block task completion

    def __post_init__(self):
        """Resolve project_root and namespace state/log paths by spec_prefix."""
        self.project_root = self.project_root.resolve()

        if self.spec_prefix:
            default_state = Path("spec/.executor-state.json")
            default_logs = Path("spec/.executor-logs")
            if self.state_file == default_state:
                self.state_file = Path(f"spec/.executor-{self.spec_prefix}state.json")
            if self.logs_dir == default_logs:
                self.logs_dir = Path(f"spec/.executor-{self.spec_prefix}logs")

        if not self.state_file.is_absolute():
            self.state_file = self.project_root / self.state_file
        if not self.logs_dir.is_absolute():
            self.logs_dir = self.project_root / self.logs_dir

    @property
    def stop_file(self) -> Path:
        return self.project_root / "spec" / ".executor-stop"

    @property
    def tasks_file(self) -> Path:
        return self.project_root / "spec" / f"{self.spec_prefix}tasks.md"

    @property
    def requirements_file(self) -> Path:
        return self.project_root / "spec" / f"{self.spec_prefix}requirements.md"

    @property
    def design_file(self) -> Path:
        return self.project_root / "spec" / f"{self.spec_prefix}design.md"


def build_cli_command(
    cmd: str,
    prompt: str,
    model: str = "",
    template: str = "",
    skip_permissions: bool = False,
    prompt_file: Path | None = None,
) -> list[str]:
    """Build CLI command from template or auto-detect based on command name.

    Args:
        cmd: CLI command name (e.g., "claude", "codex", "llama-cli")
        prompt: The prompt text
        model: Model name (optional)
        template: Command template with placeholders (optional)
        skip_permissions: Add --dangerously-skip-permissions for Claude (optional)
        prompt_file: Path to file containing prompt (optional, for large prompts)

    Returns:
        List of command arguments ready for subprocess.

    Template placeholders:
        {cmd} - CLI command
        {model} - Model name
        {prompt} - Prompt text (shell-escaped)
        {prompt_file} - Path to prompt file
    """
    import shlex

    # Use template if provided
    if template:
        # Replace placeholders
        prompt_escaped = shlex.quote(prompt)
        prompt_file_str = str(prompt_file) if prompt_file else ""

        formatted = template.format(
            cmd=cmd,
            model=model,
            prompt=prompt_escaped,
            prompt_file=prompt_file_str,
        )
        # Parse the formatted string into arguments
        return shlex.split(formatted)

    # Auto-detect based on command name
    cmd_lower = cmd.lower()

    if "llama-cli" in cmd_lower or "llama.cpp" in cmd_lower:
        # llama.cpp CLI
        result = [cmd, "-p", prompt, "--no-display-prompt"]
        if model:
            result.extend(["-m", model])
        return result

    elif "llama-server" in cmd_lower or "localhost:8080" in cmd_lower:
        # llama.cpp server via curl
        import json

        payload = json.dumps({"prompt": prompt})
        return ["curl", "-s", "http://localhost:8080/completion", "-d", payload]

    elif "ollama" in cmd_lower:
        # Ollama CLI
        return [cmd, "run", model or "llama3", prompt]

    elif "codex" in cmd_lower:
        # Codex CLI
        result = [cmd, "-p", prompt]
        if model:
            result.extend(["--model", model])
        return result

    else:
        # Claude CLI (default)
        result = [cmd, "-p", prompt]
        if skip_permissions:
            result.append("--dangerously-skip-permissions")
        if model:
            result.extend(["--model", model])
        return result


def load_config_from_yaml(config_path: Path = CONFIG_FILE) -> dict:
    """Load configuration from YAML file.

    Args:
        config_path: Path to the configuration file.

    Returns:
        Dictionary with configuration values.
    """
    if not config_path.exists():
        return {}

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

        executor_config = data.get("executor", {})
        hooks = executor_config.get("hooks", {})
        pre_start = hooks.get("pre_start", {})
        post_done = hooks.get("post_done", {})
        commands = executor_config.get("commands", {})
        paths = executor_config.get("paths", {})

        return {
            "max_retries": executor_config.get("max_retries"),
            "retry_delay_seconds": executor_config.get("retry_delay_seconds"),
            "task_timeout_minutes": executor_config.get("task_timeout_minutes"),
            "max_consecutive_failures": executor_config.get("max_consecutive_failures"),
            "on_task_failure": executor_config.get("on_task_failure"),
            "claude_command": executor_config.get("claude_command"),
            "claude_model": executor_config.get("claude_model"),
            "skip_permissions": executor_config.get("skip_permissions"),
            "create_git_branch": pre_start.get("create_git_branch"),
            "main_branch": executor_config.get("main_branch"),
            "run_tests_on_done": post_done.get("run_tests"),
            "run_lint_on_done": post_done.get("run_lint"),
            "lint_blocking": post_done.get("lint_blocking"),
            "auto_commit": post_done.get("auto_commit"),
            "run_review": post_done.get("run_review"),
            "review_timeout_minutes": executor_config.get("review_timeout_minutes"),
            "review_command": executor_config.get("review_command"),
            "review_model": executor_config.get("review_model"),
            "command_template": executor_config.get("command_template"),
            "review_command_template": executor_config.get("review_command_template"),
            "test_command": commands.get("test"),
            "lint_command": commands.get("lint"),
            "lint_fix_command": commands.get("lint_fix"),
            "project_root": Path(paths["root"]) if paths.get("root") else None,
            "logs_dir": Path(paths["logs"]) if paths.get("logs") else None,
            "state_file": Path(paths["state"]) if paths.get("state") else None,
            "callback_url": executor_config.get("callback_url"),
            "spec_prefix": executor_config.get("spec_prefix"),
        }
    except Exception as e:
        print(f"⚠️  Warning: Failed to load config from {config_path}: {e}")
        return {}


def build_config(yaml_config: dict, args: argparse.Namespace) -> ExecutorConfig:
    """Build ExecutorConfig from YAML and CLI arguments.

    CLI arguments override YAML config.

    Args:
        yaml_config: Configuration loaded from YAML file.
        args: Parsed CLI arguments.

    Returns:
        ExecutorConfig instance.
    """
    # Start with defaults
    config_kwargs = {}

    # Apply YAML config (only non-None values)
    for key, value in yaml_config.items():
        if value is not None:
            config_kwargs[key] = value

    # Override with CLI arguments
    if hasattr(args, "max_retries") and args.max_retries != 3:
        config_kwargs["max_retries"] = args.max_retries
    if hasattr(args, "timeout") and args.timeout != 30:
        config_kwargs["task_timeout_minutes"] = args.timeout
    if hasattr(args, "no_tests") and args.no_tests:
        config_kwargs["run_tests_on_done"] = False
    if hasattr(args, "no_branch") and args.no_branch:
        config_kwargs["create_git_branch"] = False
    if hasattr(args, "no_commit") and args.no_commit:
        config_kwargs["auto_commit"] = False
    if hasattr(args, "no_review") and args.no_review:
        config_kwargs["run_review"] = False
    if hasattr(args, "callback_url") and args.callback_url:
        config_kwargs["callback_url"] = args.callback_url
    if hasattr(args, "spec_prefix") and args.spec_prefix:
        config_kwargs["spec_prefix"] = args.spec_prefix
    if hasattr(args, "project_root") and args.project_root:
        config_kwargs["project_root"] = Path(args.project_root)

    return ExecutorConfig(**config_kwargs)


# === State Management ===


@dataclass
class TaskAttempt:
    """Task execution attempt"""

    timestamp: str
    success: bool
    duration_seconds: float
    error: str | None = None
    claude_output: str | None = None


@dataclass
class TaskState:
    """Task state in executor"""

    task_id: str
    status: str  # pending, running, success, failed, skipped
    attempts: list[TaskAttempt] = field(default_factory=list)
    started_at: str | None = None
    completed_at: str | None = None

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def last_error(self) -> str | None:
        if self.attempts:
            return self.attempts[-1].error
        return None


class ExecutorState:
    """Global executor state"""

    def __init__(self, config: ExecutorConfig):
        self.config = config
        self.tasks: dict[str, TaskState] = {}
        self.consecutive_failures = 0
        self.total_completed = 0
        self.total_failed = 0
        self._load()

    def _load(self):
        """Load state from file"""
        if self.config.state_file.exists():
            data = json.loads(self.config.state_file.read_text())
            for task_id, task_data in data.get("tasks", {}).items():
                attempts = [TaskAttempt(**a) for a in task_data.get("attempts", [])]
                self.tasks[task_id] = TaskState(
                    task_id=task_id,
                    status=task_data.get("status", "pending"),
                    attempts=attempts,
                    started_at=task_data.get("started_at"),
                    completed_at=task_data.get("completed_at"),
                )
            self.consecutive_failures = data.get("consecutive_failures", 0)
            self.total_completed = data.get("total_completed", 0)
            self.total_failed = data.get("total_failed", 0)

    def _save(self):
        """Save state to file"""
        self.config.state_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "tasks": {
                task_id: {
                    "status": ts.status,
                    "attempts": [
                        {
                            "timestamp": a.timestamp,
                            "success": a.success,
                            "duration_seconds": a.duration_seconds,
                            "error": a.error,
                        }
                        for a in ts.attempts
                    ],
                    "started_at": ts.started_at,
                    "completed_at": ts.completed_at,
                }
                for task_id, ts in self.tasks.items()
            },
            "consecutive_failures": self.consecutive_failures,
            "total_completed": self.total_completed,
            "total_failed": self.total_failed,
            "last_updated": datetime.now().isoformat(),
        }
        self.config.state_file.write_text(json.dumps(data, indent=2))

    def get_task_state(self, task_id: str) -> TaskState:
        if task_id not in self.tasks:
            self.tasks[task_id] = TaskState(task_id=task_id, status="pending")
        return self.tasks[task_id]

    def record_attempt(
        self,
        task_id: str,
        success: bool,
        duration: float,
        error: str | None = None,
        output: str | None = None,
    ):
        """Record execution attempt"""
        state = self.get_task_state(task_id)
        state.attempts.append(
            TaskAttempt(
                timestamp=datetime.now().isoformat(),
                success=success,
                duration_seconds=duration,
                error=error,
                claude_output=output,
            )
        )

        if success:
            state.status = "success"
            state.completed_at = datetime.now().isoformat()
            self.consecutive_failures = 0
            self.total_completed += 1
        else:
            if state.attempt_count >= self.config.max_retries:
                state.status = "failed"
                self.total_failed += 1
            self.consecutive_failures += 1

        self._save()

    def mark_running(self, task_id: str):
        state = self.get_task_state(task_id)
        state.status = "running"
        state.started_at = datetime.now().isoformat()
        self._save()

    def should_stop(self) -> bool:
        """Check if we should stop"""
        return self.consecutive_failures >= self.config.max_consecutive_failures


def check_stop_requested(config: ExecutorConfig) -> bool:
    """Check if graceful shutdown was requested via stop file."""
    return config.stop_file.exists()


def clear_stop_file(config: ExecutorConfig) -> None:
    """Remove stop file if it exists."""
    with contextlib.suppress(FileNotFoundError):
        config.stop_file.unlink()


# === Prompt Builder ===

PROMPTS_DIR = Path("spec/prompts")


def load_prompt_template(name: str, cli_name: str = "") -> str | None:
    """Load prompt template from spec/prompts/ directory.

    Tries to load CLI-specific template first (e.g., review.codex.md),
    then falls back to generic template (e.g., review.md or review.txt).

    Args:
        name: Template name without extension (e.g., 'task', 'review')
        cli_name: CLI name for CLI-specific templates (e.g., 'codex', 'claude')

    Returns:
        Template content, or None if not found.
    """
    # Try CLI-specific template first
    if cli_name:
        cli_lower = cli_name.lower()
        # Extract base CLI name (e.g., "codex" from "/usr/bin/codex")
        cli_base = cli_lower.split("/")[-1]

        # Try different CLI-specific patterns
        for pattern in [f"{name}.{cli_base}.md", f"{name}.{cli_base}.txt"]:
            template_path = PROMPTS_DIR / pattern
            if template_path.exists():
                return _read_template(template_path)

    # Try generic templates
    for ext in [".md", ".txt"]:
        template_path = PROMPTS_DIR / f"{name}{ext}"
        if template_path.exists():
            return _read_template(template_path)

    return None


def _read_template(path: Path) -> str:
    """Read and process template file."""
    content = path.read_text()

    # Strip comment lines (lines starting with #) only for .txt files
    if path.suffix == ".txt":
        lines = []
        for line in content.split("\n"):
            stripped = line.strip()
            if not stripped.startswith("#"):
                lines.append(line)
        return "\n".join(lines).strip()

    return content.strip()


def render_template(template: str, variables: dict[str, str]) -> str:
    """Render template with variable substitution.

    Supports both {{VARIABLE}} and ${VARIABLE} placeholder syntax.

    Args:
        template: Template string with placeholders
        variables: Dict of variable names to values

    Returns:
        Rendered template string.
    """
    result = template
    for name, value in variables.items():
        # Support both {{VAR}} and ${VAR} syntax
        result = result.replace(f"{{{{{name}}}}}", value)
        result = result.replace(f"${{{name}}}", value)
    return result


def format_error_summary(error: str, output: str | None = None, max_lines: int = 10) -> str:
    """Format a concise error summary for display.

    Args:
        error: Error message/type
        output: Full output (optional)
        max_lines: Max lines to show from output

    Returns:
        Formatted error summary string.
    """
    lines = [f"  ❌ Error: {error}"]

    if output:
        # Try to extract the most relevant part
        relevant_lines = []

        for line in output.split("\n"):
            line_lower = line.lower()
            # Look for error indicators
            if any(
                kw in line_lower
                for kw in [
                    "error",
                    "failed",
                    "exception",
                    "traceback",
                    "assert",
                    "expected",
                    "actual",
                    "typeerror",
                    "nameerror",
                    "valueerror",
                    "keyerror",
                    "attributeerror",
                ]
            ):
                relevant_lines.append(line.strip())

        if relevant_lines:
            lines.append("  📋 Key issues:")
            for line in relevant_lines[:max_lines]:
                if line:
                    lines.append(f"     • {line[:100]}")
            if len(relevant_lines) > max_lines:
                lines.append(f"     ... and {len(relevant_lines) - max_lines} more")
        else:
            # No specific errors found, show last few lines
            output_lines = [ln.strip() for ln in output.split("\n") if ln.strip()]
            if output_lines:
                lines.append("  📋 Last output:")
                for line in output_lines[-5:]:
                    lines.append(f"     {line[:100]}")

    return "\n".join(lines)


def extract_test_failures(output: str) -> str:
    """Extract relevant test failure info from pytest output."""
    lines = output.split("\n")
    result_lines = []
    in_failure = False
    failure_count = 0
    max_failures = 5  # Limit to avoid huge prompts

    for line in lines:
        # Capture FAILED lines
        if "FAILED" in line or "ERROR" in line:
            result_lines.append(line)
            failure_count += 1
            if failure_count >= max_failures:
                result_lines.append(f"... and more (showing first {max_failures})")
                break
        # Capture assertion errors
        elif "AssertionError" in line or "assert" in line.lower():
            result_lines.append(line)
        # Capture short summary
        elif "short test summary" in line.lower():
            in_failure = True
        elif in_failure and line.strip():
            result_lines.append(line)

    return "\n".join(result_lines[-50:]) if result_lines else output[-1500:]


def build_task_prompt(
    task: Task,
    config: ExecutorConfig,
    previous_attempts: list[TaskAttempt] | None = None,
) -> str:
    """Build prompt for Claude with task context and previous attempt info."""

    # Read specifications
    requirements = ""
    if config.requirements_file.exists():
        requirements = config.requirements_file.read_text()

    design = ""
    if config.design_file.exists():
        design = config.design_file.read_text()

    # Find related requirements
    related_reqs = []
    for ref in task.traces_to:
        if ref.startswith("REQ-"):
            # Extract requirement from requirements.md
            pattern = rf"#### {ref}:.*?(?=####|\Z)"
            match = re.search(pattern, requirements, re.DOTALL)
            if match:
                related_reqs.append(match.group(0).strip())

    # Find related design
    related_design = []
    for ref in task.traces_to:
        if ref.startswith("DESIGN-"):
            pattern = rf"### {ref}:.*?(?=###|\Z)"
            match = re.search(pattern, design, re.DOTALL)
            if match:
                related_design.append(match.group(0).strip())

    # Checklist
    checklist_text = "\n".join(
        [f"- {'[x]' if done else '[ ]'} {item}" for item, done in task.checklist]
    )

    # Build previous attempts section
    attempts_section = ""
    if previous_attempts:
        failed_attempts = [a for a in previous_attempts if not a.success]
        if failed_attempts:
            attempts_section = "\n## ⚠️ PREVIOUS ATTEMPTS FAILED - FIX THESE ISSUES:\n\n"
            for i, attempt in enumerate(failed_attempts, 1):
                attempts_section += f"### Attempt {i} (failed):\n"
                if attempt.error:
                    attempts_section += f"**Error:** {attempt.error}\n\n"
                if attempt.claude_output:
                    failures = extract_test_failures(attempt.claude_output)
                    if failures:
                        attempts_section += f"**Test failures:**\n```\n{failures}\n```\n\n"

            attempts_section += (
                "**IMPORTANT:** Review the errors above and fix the issues. "
                "Do not repeat the same mistakes.\n\n"
            )

    # Try to load custom template
    template = load_prompt_template("task")

    if template:
        # Use custom template with variable substitution
        variables = {
            "TASK_ID": task.id,
            "TASK_NAME": task.name,
            "PRIORITY": task.priority.upper(),
            "ESTIMATE": task.estimate or "TBD",
            "MILESTONE": task.milestone or "N/A",
            "CHECKLIST": checklist_text,
            "RELATED_REQS": "\n".join(related_reqs)
            if related_reqs
            else f"See {config.requirements_file}",
            "RELATED_DESIGN": "\n".join(related_design)
            if related_design
            else f"See {config.design_file}",
            "PREVIOUS_ATTEMPTS": attempts_section,
        }
        return render_template(template, variables)

    # Fallback to built-in prompt
    prompt = f"""# Task Execution Request

## Task: {task.id} — {task.name}

**Priority:** {task.priority.upper()}
**Estimate:** {task.estimate}
**Milestone:** {task.milestone}

## Checklist (implement ALL items):

{checklist_text}

## Related Requirements:

{chr(10).join(related_reqs) if related_reqs else f"See {config.requirements_file}"}

## Related Design:

{chr(10).join(related_design) if related_design else f"See {config.design_file}"}

## Instructions:

1. Implement ALL checklist items for this task
2. Write unit tests for new code (coverage ≥80%)
3. Follow the design patterns from {config.design_file}
4. Use existing code style and conventions
5. Create/update files as needed

## Dependencies:

- To add a new dependency: `uv add <package>`
- To add a dev dependency: `uv add --dev <package>`
- NEVER edit pyproject.toml manually for dependencies
- After adding dependencies, they are available immediately

## Success Criteria:

- All checklist items implemented
- All tests pass (`uv run pytest`)
- No lint errors (`uv run ruff check .`)
- Code follows project conventions

## Output:

When complete, respond with:
- Summary of changes made
- Files created/modified
- Any issues or notes
- "TASK_COMPLETE" if successful, or "TASK_FAILED: <reason>" if not

{attempts_section}
Begin implementation:
"""

    return prompt


# === Hooks ===


def get_task_branch_name(task: Task) -> str:
    """Generate branch name for task"""
    safe_name = task.name.lower().replace(" ", "-").replace("/", "-")[:30]
    return f"task/{task.id.lower()}-{safe_name}"


def get_main_branch(config: ExecutorConfig) -> str:
    """Determine main branch name (main or master).

    Detection order:
    1. Config setting (main_branch)
    2. Remote HEAD (origin/HEAD)
    3. Existing main or master branch
    4. Current branch (if no main/master exists yet)
    5. Default to "main"
    """
    # 0. Use config if explicitly set
    if config.main_branch:
        return config.main_branch

    # 1. Try remote HEAD
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )
    if result.returncode == 0:
        # refs/remotes/origin/main -> main
        return result.stdout.strip().split("/")[-1]

    # 2. Check if main or master branch exists
    for branch in ["main", "master"]:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )
        if result.returncode == 0:
            return branch

    # 3. If no main/master, use current branch as "main"
    # (handles fresh repos where first branch might be named differently)
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    return "main"  # default for brand new repos


def _ensure_on_main_branch(config: ExecutorConfig) -> None:
    """Ensure we're on main branch after all tasks complete."""
    try:
        main_branch = get_main_branch(config)

        # Check current branch
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )
        current_branch = result.stdout.strip()

        if current_branch != main_branch:
            print(f"   Switching to {main_branch}...")
            result = subprocess.run(
                ["git", "checkout", main_branch],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if result.returncode == 0:
                print(f"   ✅ On {main_branch}")
            else:
                print(f"   ⚠️  Could not switch to {main_branch}: {result.stderr.strip()}")
    except Exception:
        pass  # Ignore git errors


def pre_start_hook(task: Task, config: ExecutorConfig) -> bool:
    """Hook before starting task"""
    print(f"🔧 Pre-start hook for {task.id}")

    # Sync dependencies
    print("   Syncing dependencies...")
    result = subprocess.run(["uv", "sync"], capture_output=True, text=True, cwd=config.project_root)
    if result.returncode == 0:
        print("   ✅ Dependencies synced")
    else:
        print(f"   ⚠️  uv sync warning: {result.stderr[:200]}")

    # Create git branch
    if config.create_git_branch:
        branch_name = get_task_branch_name(task)
        try:
            # Check if git exists
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if result.returncode != 0:
                return True  # No git repository

            # Check if repo has any commits
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if result.returncode != 0:
                # Fresh repo without commits — skip branching for now
                # TASK-000 typically does git init, first commit will be on main
                print("   ⚠️  No commits yet, skipping branch creation")
                return True

            # Switch to main
            main_branch = get_main_branch(config)
            subprocess.run(
                ["git", "checkout", main_branch],
                capture_output=True,
                cwd=config.project_root,
            )

            # Check if branch exists
            result = subprocess.run(
                ["git", "rev-parse", "--verify", branch_name],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )

            if result.returncode == 0:
                # Branch exists — switch to it
                subprocess.run(
                    ["git", "checkout", branch_name],
                    capture_output=True,
                    cwd=config.project_root,
                )
                print(f"   Switched to existing branch: {branch_name}")
            else:
                # Create new branch
                result = subprocess.run(
                    ["git", "checkout", "-b", branch_name],
                    capture_output=True,
                    text=True,
                    cwd=config.project_root,
                )
                if result.returncode == 0:
                    print(f"   Created branch: {branch_name}")
                else:
                    print(f"   ⚠️  Failed to create branch: {result.stderr}")

        except FileNotFoundError:
            pass  # git not installed

    return True


def build_review_prompt(task: Task, config: ExecutorConfig, cli_name: str = "") -> str:
    """Build code review prompt for the specified CLI.

    Args:
        task: Task that was completed
        config: Executor configuration
        cli_name: CLI name for CLI-specific prompt template (e.g., 'codex', 'claude')
    """
    # Get changed files from git
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD~1"],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )
    changed_files = (
        result.stdout.strip() if result.returncode == 0 else "Unable to get changed files"
    )

    # Get git diff
    result = subprocess.run(
        ["git", "diff", "HEAD~1", "--stat"],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )
    git_diff_stat = result.stdout.strip() if result.returncode == 0 else ""

    # Try to load CLI-specific or custom template
    template = load_prompt_template("review", cli_name=cli_name)

    if template:
        variables = {
            "TASK_ID": task.id,
            "TASK_NAME": task.name,
            "CHANGED_FILES": changed_files,
            "GIT_DIFF": git_diff_stat,
        }
        return render_template(template, variables)

    # Fallback to built-in prompt
    return f"""# Code Review Request

## Task Completed: {task.id} — {task.name}

## Changed Files:
{changed_files}

## Diff Summary:
{git_diff_stat}

## Review Instructions:

Launch the following review agents in parallel using the Task tool:

### 1. Quality Agent
Review the code changes for:
- Bugs and logic errors
- Security vulnerabilities
- Error handling gaps

### 2. Implementation Agent
Verify the implementation:
- Code achieves the stated task goals
- All checklist items are properly implemented
- Edge cases are handled

### 3. Testing Agent
Review test coverage:
- New code has adequate test coverage
- Tests are meaningful and not trivial

## Output:

For each issue found, describe it briefly.
If issues are found, fix them and respond with: "REVIEW_FIXED"
If no issues found, respond with: "REVIEW_PASSED"
"""


def run_code_review(task: Task, config: ExecutorConfig) -> tuple[bool, str | None]:
    """Run code review on completed task.

    Returns:
        Tuple of (success, error_message).
    """
    log_progress("🔍 Starting code review", task.id)

    # Use review-specific command/model if configured, otherwise fall back to main settings
    review_cmd = config.review_command or config.claude_command
    review_model = config.review_model or config.claude_model
    review_template = config.review_command_template or config.command_template

    # Build prompt with CLI-specific template
    prompt = build_review_prompt(task, config, cli_name=review_cmd)

    # Save review prompt to log
    log_file = config.logs_dir / f"{task.id}-review-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    with open(log_file, "w") as f:
        f.write(f"=== REVIEW PROMPT ===\n{prompt}\n\n")

    try:
        # Build command using template or auto-detect
        cmd = build_cli_command(
            cmd=review_cmd,
            prompt=prompt,
            model=review_model,
            template=review_template,
            skip_permissions=config.skip_permissions,
        )

        log_progress(
            f"🔍 Review using: {review_cmd}" + (f" ({review_model})" if review_model else ""),
            task.id,
        )

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.review_timeout_minutes * 60,
            cwd=config.project_root,
        )

        output = result.stdout
        stderr = result.stderr
        combined_output = output + "\n" + stderr

        # Save output
        with open(log_file, "a") as f:
            f.write(f"=== OUTPUT ===\n{output}\n\n")
            f.write(f"=== STDERR ===\n{stderr}\n\n")
            f.write(f"=== RETURN CODE: {result.returncode} ===\n")

        # Check for API errors
        error_pattern = check_error_patterns(combined_output)
        if error_pattern:
            log_progress(f"⚠️ Review API error: {error_pattern}", task.id)
            return False, f"API error: {error_pattern}"

        # Check for empty or failed response
        if result.returncode != 0 and not output.strip():
            log_progress(
                f"⚠️ Review process failed (exit code {result.returncode})",
                task.id,
            )
            if stderr.strip():
                log_progress(f"   stderr: {stderr.strip()[:200]}", task.id)
            return False, f"Review process exited with code {result.returncode}"

        if not output.strip():
            log_progress("⚠️ Review returned empty response", task.id)
            return False, "Review returned empty response"

        # Check review result (case-insensitive, check both stdout and stderr)
        output_upper = combined_output.upper()
        if "REVIEW_PASSED" in output_upper:
            log_progress("✅ Code review passed", task.id)
            return True, None
        elif "REVIEW_FIXED" in output_upper:
            log_progress("✅ Code review: issues fixed", task.id)
            # Commit the fixes
            subprocess.run(["git", "add", "-A"], cwd=config.project_root)
            subprocess.run(
                ["git", "commit", "-m", f"{task.id}: code review fixes"],
                cwd=config.project_root,
            )
            return True, None
        elif "REVIEW_FAILED" in output_upper:
            log_progress("❌ Code review found unresolved issues", task.id)
            preview = output.strip()[-300:]
            log_progress(f"   Review output (last 300 chars): {preview}", task.id)
            return False, "Code review found unresolved issues"
        else:
            # No explicit marker — treat as passed but log for visibility
            preview = output.strip()[-200:] if output.strip() else "(empty)"
            log_progress("✅ Code review completed (no explicit status marker)", task.id)
            log_progress(f"   Review output (last 200 chars): {preview}", task.id)
            return True, None

    except subprocess.TimeoutExpired:
        log_progress(f"⏰ Review timeout after {config.review_timeout_minutes}m", task.id)
        return False, "Review timeout"
    except Exception as e:
        log_progress(f"💥 Review error: {e}", task.id)
        return False, str(e)


def post_done_hook(task: Task, config: ExecutorConfig, success: bool) -> tuple[bool, str | None]:
    """Hook after task completion.

    Returns:
        Tuple of (success, error_details).
        error_details contains test/lint output on failure.
    """
    print(f"🔧 Post-done hook for {task.id} (success={success})")

    if not success:
        return False, None

    # Run tests
    if config.run_tests_on_done:
        print("   Running tests...")
        result = subprocess.run(
            config.test_command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )
        if result.returncode != 0:
            print("   ❌ Tests failed!")
            # Combine stdout and stderr for full picture
            test_output = result.stdout + "\n" + result.stderr
            print(result.stderr[:500])
            return False, f"Tests failed:\n{test_output}"
        print("   ✅ Tests passed")

    # Run lint
    if config.run_lint_on_done and config.lint_command:
        print("   Running lint...")
        result = subprocess.run(
            config.lint_command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )

        if result.returncode != 0:
            # Step 1: Attempt auto-fix
            print("   🔧 Attempting auto-fix...")
            subprocess.run(
                config.lint_fix_command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )

            # Step 2: Re-check lint
            recheck = subprocess.run(
                config.lint_command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )

            if recheck.returncode != 0:
                # Step 3: Still failing — block or warn
                if config.lint_blocking:
                    lint_output = recheck.stdout + "\n" + recheck.stderr
                    print("   ❌ Lint errors remain after auto-fix!")
                    return False, f"Lint errors (not auto-fixable):\n{lint_output}"
                else:
                    print("   ⚠️  Lint warnings (non-blocking)")
            else:
                print("   ✅ Lint auto-fixed")
        else:
            print("   ✅ Lint passed")

    # Run code review (before commit, so fixes can be included)
    if config.run_review:
        print("   Running code review...")
        review_ok, review_error = run_code_review(task, config)
        if not review_ok:
            print(f"   ⚠️  Review issue: {review_error}")
            # Don't block on review failures, just warn

    # Auto-commit
    if config.auto_commit:
        try:
            # Check if there are changes to commit
            status_result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if not status_result.stdout.strip():
                print("   No changes to commit")
            else:
                subprocess.run(["git", "add", "-A"], cwd=config.project_root)
                # Build commit message with task details
                commit_title = f"{task.id}: {task.name}"
                commit_body_lines = []
                if task.checklist:
                    commit_body_lines.append("Completed:")
                    for item, checked in task.checklist:
                        if checked:
                            commit_body_lines.append(f"  - {item}")
                if task.milestone:
                    commit_body_lines.append(f"\nMilestone: {task.milestone}")

                commit_msg = commit_title
                if commit_body_lines:
                    commit_msg += "\n\n" + "\n".join(commit_body_lines)

                subprocess.run(["git", "commit", "-m", commit_msg], cwd=config.project_root)
                print("   Committed changes")
        except Exception as e:
            print(f"   Commit failed: {e}")

    # Merge branch to main
    if config.create_git_branch:
        try:
            branch_name = get_task_branch_name(task)
            main_branch = get_main_branch(config)

            # Check current branch — if we're already on main, skip merge
            # (happens for TASK-000 or fresh repos)
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            current_branch = result.stdout.strip()
            if current_branch == main_branch:
                # Already on main, no merge needed
                return True, None

            # Switch to main
            result = subprocess.run(
                ["git", "checkout", main_branch],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if result.returncode != 0:
                # Try with -f flag if there are uncommitted changes
                error_msg = result.stderr.strip()
                if "uncommitted" in error_msg.lower() or "changes" in error_msg.lower():
                    # Stash changes first
                    subprocess.run(
                        ["git", "stash"],
                        capture_output=True,
                        cwd=config.project_root,
                    )
                    result = subprocess.run(
                        ["git", "checkout", main_branch],
                        capture_output=True,
                        text=True,
                        cwd=config.project_root,
                    )

                if result.returncode != 0:
                    print(f"   ⚠️  Failed to switch to {main_branch}: {error_msg}")
                    return True, None

            # Merge task branch
            result = subprocess.run(
                ["git", "merge", branch_name, "--no-ff", "-m", f"Merge {branch_name}"],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if result.returncode == 0:
                print(f"   Merged {branch_name} → {main_branch}")

                # Delete task branch
                subprocess.run(
                    ["git", "branch", "-d", branch_name],
                    capture_output=True,
                    cwd=config.project_root,
                )
                print(f"   Deleted branch: {branch_name}")
            else:
                print(f"   ⚠️  Merge failed: {result.stderr}")
                # Return to task branch on failure
                subprocess.run(
                    ["git", "checkout", branch_name],
                    capture_output=True,
                    cwd=config.project_root,
                )
        except Exception as e:
            print(f"   Merge failed: {e}")

    return True, None


# === Task Executor ===


def execute_task(task: Task, config: ExecutorConfig, state: ExecutorState) -> bool | str:
    """Execute a single task via Claude CLI.

    Returns:
        True if successful, False if failed, or "API_ERROR" if rate limited.
    """

    task_id = task.id
    log_progress(f"🚀 Starting: {task.name}", task_id)
    print(f"\n{'=' * 60}")
    print(f"🚀 Executing {task_id}: {task.name}")
    print(f"{'=' * 60}")

    # Pre-start hook
    if not pre_start_hook(task, config):
        print("❌ Pre-start hook failed")
        return False

    # Update status
    state.mark_running(task_id)
    update_task_status(config.tasks_file, task_id, "in_progress")
    _send_callback(config.callback_url, task_id, "started")

    # Get previous attempts for context (to inform Claude about past failures)
    task_state = state.get_task_state(task_id)
    previous_attempts = task_state.attempts if task_state.attempts else None

    # Build prompt with previous attempt context
    prompt = build_task_prompt(task, config, previous_attempts)

    # Save prompt to log
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = config.logs_dir / f"{task_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"

    with open(log_file, "w") as f:
        f.write(f"=== PROMPT ===\n{prompt}\n\n")

    # Run Claude
    start_time = datetime.now()

    try:
        # Build command using template or auto-detect
        cmd = build_cli_command(
            cmd=config.claude_command,
            prompt=prompt,
            model=config.claude_model,
            template=config.command_template,
            skip_permissions=config.skip_permissions,
        )

        flags = " --dangerously-skip-permissions" if config.skip_permissions else ""
        print(f"🤖 Running: {config.claude_command} -p ...{flags}")

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.task_timeout_minutes * 60,
            cwd=config.project_root,
        )

        duration = (datetime.now() - start_time).total_seconds()
        output = result.stdout
        combined_output = output + "\n" + result.stderr

        # Save output
        with open(log_file, "a") as f:
            f.write(f"=== OUTPUT ===\n{output}\n\n")
            f.write(f"=== STDERR ===\n{result.stderr}\n\n")
            f.write(f"=== RETURN CODE: {result.returncode} ===\n")

        # Check for API errors (rate limits, etc.)
        error_pattern = check_error_patterns(combined_output)
        if error_pattern:
            log_progress(f"⚠️ API error detected: {error_pattern}", task_id)
            print(f"\n⚠️  API error detected: '{error_pattern}'")
            print("   Check your usage: claude usage")
            print("   Or wait and retry later.")
            state.record_attempt(task_id, False, duration, error=f"API error: {error_pattern}")
            _send_callback(
                config.callback_url, task_id, "failed", duration, f"API error: {error_pattern}"
            )
            return "API_ERROR"

        # Check result
        # Success if:
        # 1. Explicitly says TASK_COMPLETE, or
        # 2. Return code 0 and no TASK_FAILED (Claude forgot the marker)
        has_complete_marker = "TASK_COMPLETE" in output
        has_failed_marker = "TASK_FAILED" in output
        implicit_success = result.returncode == 0 and not has_failed_marker

        success = (has_complete_marker and not has_failed_marker) or implicit_success

        if success:
            if has_complete_marker:
                print("✅ Claude reports: TASK_COMPLETE")
            else:
                print("✅ Implicit success (return code 0, no TASK_FAILED)")

            # Post-done hook (tests, lint)
            hook_success, hook_error = post_done_hook(task, config, True)

            if hook_success:
                state.record_attempt(task_id, True, duration, output=output)
                update_task_status(config.tasks_file, task_id, "done")
                mark_all_checklist_done(config.tasks_file, task_id)
                log_progress(f"✅ Completed in {duration:.1f}s", task_id)
                _send_callback(config.callback_url, task_id, "success", duration)
                return True
            else:
                # Hook failed (tests didn't pass)
                # Include detailed error info for next attempt
                error = hook_error or "Post-done hook failed (tests/lint)"
                # Combine Claude output with test failures for context
                full_output = output
                if hook_error:
                    full_output = f"{output}\n\n=== TEST FAILURES ===\n{hook_error}"
                state.record_attempt(task_id, False, duration, error=error, output=full_output)
                log_progress("❌ Failed: tests/lint check", task_id)
                _send_callback(config.callback_url, task_id, "failed", duration, error)
                return False
        else:
            # Claude reported failure
            error_match = re.search(r"TASK_FAILED:\s*(.+)", output)
            error = error_match.group(1) if error_match else "Unknown error"
            state.record_attempt(task_id, False, duration, error=error, output=output)
            log_progress(f"❌ Failed: {error[:50]}", task_id)
            _send_callback(config.callback_url, task_id, "failed", duration, error)
            return False

    except subprocess.TimeoutExpired:
        duration = config.task_timeout_minutes * 60
        error = f"Timeout after {config.task_timeout_minutes} minutes"
        state.record_attempt(task_id, False, duration, error=error)
        log_progress(f"⏰ Timeout after {config.task_timeout_minutes}m", task_id)
        _send_callback(config.callback_url, task_id, "failed", duration, error)
        return False

    except Exception as e:
        duration = (datetime.now() - start_time).total_seconds()
        error = str(e)
        state.record_attempt(task_id, False, duration, error=error)
        log_progress(f"💥 Error: {error[:50]}", task_id)
        _send_callback(config.callback_url, task_id, "failed", duration, error)
        return False


def run_with_retries(task: Task, config: ExecutorConfig, state: ExecutorState) -> bool | str:
    """Execute task with retries.

    Returns:
        True if successful, False if failed, "API_ERROR" if rate limited,
        or "SKIP" if task was skipped.
    """

    task_state = state.get_task_state(task.id)

    for attempt in range(task_state.attempt_count, config.max_retries):
        log_progress(f"📍 Attempt {attempt + 1}/{config.max_retries}", task.id)

        result = execute_task(task, config, state)

        # API error - stop immediately, don't retry
        if result == "API_ERROR":
            return "API_ERROR"

        if result is True:
            return True

        if attempt < config.max_retries - 1:
            print(f"⏳ Waiting {config.retry_delay_seconds}s before retry...")
            import time

            time.sleep(config.retry_delay_seconds)

    # Task failed after all retries
    log_progress(f"❌ Failed after {config.max_retries} attempts", task.id)

    # Show concise error summary
    if task_state.last_error:
        last_attempt = task_state.attempts[-1] if task_state.attempts else None
        output = last_attempt.claude_output if last_attempt else None
        print(f"\n{'─' * 60}")
        print(f"📛 {task.id} FAILED")
        print(format_error_summary(task_state.last_error, output))
        print(f"{'─' * 60}")

    # Handle based on on_task_failure setting
    if config.on_task_failure == "stop":
        update_task_status(config.tasks_file, task.id, "blocked")
        return False

    elif config.on_task_failure == "ask":
        print(f"\n❓ Task {task.id} failed. What to do?")
        print("   [s] Skip and continue to next task")
        print("   [r] Retry this task")
        print("   [q] Quit executor")
        choice = input("\nYour choice [s/r/q]: ").strip().lower()

        if choice == "r":
            # Reset attempts and retry
            task_state.attempts = []
            state._save()
            return run_with_retries(task, config, state)
        elif choice == "q":
            update_task_status(config.tasks_file, task.id, "blocked")
            return False
        else:
            # Skip (default)
            update_task_status(config.tasks_file, task.id, "blocked")
            log_progress("⏭️ Skipped, continuing to next task", task.id)
            return "SKIP"

    else:  # "skip" (default)
        update_task_status(config.tasks_file, task.id, "blocked")
        log_progress("⏭️ Skipped, continuing to next task", task.id)
        return "SKIP"


# === CLI Commands ===


def cmd_run(args, config: ExecutorConfig):
    """Execute tasks"""

    # Acquire lock to prevent parallel runs
    lock = ExecutorLock(config.state_file.with_suffix(".lock"))
    if not lock.acquire():
        print("❌ Another executor is already running")
        print(f"   Lock file: {config.state_file.with_suffix('.lock')}")
        print("   If this is an error, delete the lock file manually.")
        sys.exit(1)

    try:
        _run_tasks(args, config)
    finally:
        lock.release()


def _run_tasks(args, config: ExecutorConfig):
    """Internal task execution logic."""
    # Clear any leftover stop file from previous runs
    clear_stop_file(config)

    tasks = parse_tasks(config.tasks_file)
    state = ExecutorState(config)

    # Check failure limit
    if state.should_stop():
        print(f"⛔ Stopped: {state.consecutive_failures} consecutive failures")
        print("   Use 'executor.py retry <TASK-ID>' to retry specific task")
        return

    # Determine which tasks to execute
    if args.task:
        # Specific task
        task = get_task_by_id(tasks, args.task.upper())
        if not task:
            print(f"❌ Task {args.task} not found")
            return
        tasks_to_run = [task]

    elif args.all:
        # All ready tasks (include in_progress unless --restart)
        include_in_progress = not getattr(args, "restart", False)
        tasks_to_run = get_next_tasks(tasks, include_in_progress=include_in_progress)
        if args.milestone:
            tasks_to_run = [
                t for t in tasks_to_run if args.milestone.lower() in t.milestone.lower()
            ]

    elif args.milestone:
        # Tasks for specific milestone
        include_in_progress = not getattr(args, "restart", False)
        next_tasks = get_next_tasks(tasks, include_in_progress=include_in_progress)
        tasks_to_run = [t for t in next_tasks if args.milestone.lower() in t.milestone.lower()]

    else:
        # Next task (include in_progress unless --restart)
        include_in_progress = not getattr(args, "restart", False)
        next_tasks = get_next_tasks(tasks, include_in_progress=include_in_progress)
        tasks_to_run = next_tasks[:1] if next_tasks else []

    if not tasks_to_run:
        print("✅ No tasks ready to execute")
        print("   All dependencies might be incomplete, or all tasks done")
        return

    print(f"📋 Tasks to execute: {len(tasks_to_run)}")
    for t in tasks_to_run:
        print(f"   - {t.id}: {t.name}")

    # Execute
    if args.all:
        # For --all mode, continuously re-evaluate ready tasks after each completion
        executed_ids: set[str] = set()
        include_in_progress = not getattr(args, "restart", False)
        while True:
            # Check for graceful shutdown request
            if check_stop_requested(config):
                clear_stop_file(config)
                print("\n🛑 Graceful shutdown requested (spec/.executor-stop)")
                log_progress("🛑 Graceful shutdown requested")
                break

            # Re-parse tasks to get updated statuses
            tasks = parse_tasks(config.tasks_file)
            ready_tasks = get_next_tasks(tasks, include_in_progress=include_in_progress)

            # Filter by milestone if specified
            if args.milestone:
                ready_tasks = [
                    t for t in ready_tasks if args.milestone.lower() in t.milestone.lower()
                ]

            # Filter out already executed tasks
            ready_tasks = [t for t in ready_tasks if t.id not in executed_ids]

            if not ready_tasks:
                # Show why we're stopping
                all_tasks = parse_tasks(config.tasks_file)
                todo_tasks = [t for t in all_tasks if t.status == "todo"]
                if todo_tasks:
                    print(f"\n⏸️  No more ready tasks. {len(todo_tasks)} tasks blocked:")
                    for t in todo_tasks:
                        deps = ", ".join(t.depends_on) if t.depends_on else "none"
                        print(f"   - {t.id}: waiting on [{deps}]")
                else:
                    print("\n✅ All tasks completed!")
                    # Ensure we're on main branch at the end
                    _ensure_on_main_branch(config)
                break

            task = ready_tasks[0]
            executed_ids.add(task.id)

            print(f"\n📋 Next ready task: {task.id}: {task.name}")

            result = run_with_retries(task, config, state)

            if result == "API_ERROR":
                print("\n⛔ Stopping: API rate limit reached")
                log_progress("⛔ Stopped: API rate limit")
                break

            # "SKIP" means continue to next task (don't count as consecutive failure)
            if result == "SKIP":
                continue

            if result is False and state.should_stop():
                print("\n⛔ Stopping: too many consecutive failures")
                break
    else:
        # For single task or milestone mode, execute the fixed list
        for task in tasks_to_run:
            # Check for graceful shutdown request
            if check_stop_requested(config):
                clear_stop_file(config)
                print("\n🛑 Graceful shutdown requested (spec/.executor-stop)")
                log_progress("🛑 Graceful shutdown requested")
                break

            result = run_with_retries(task, config, state)

            if result == "API_ERROR":
                print("\n⛔ Stopping: API rate limit reached")
                log_progress("⛔ Stopped: API rate limit")
                break

            if result == "SKIP":
                continue

            if result is False and state.should_stop():
                print("\n⛔ Stopping: too many consecutive failures")
                break

    # Summary
    # Re-read tasks to get updated statuses after execution
    tasks = parse_tasks(config.tasks_file)

    # Calculate statistics
    failed_attempts = sum(1 for ts in state.tasks.values() for a in ts.attempts if not a.success)
    remaining = len([t for t in tasks if t.status == "todo"])

    print(f"\n{'=' * 60}")
    print("📊 Execution Summary")
    print(f"{'=' * 60}")
    print(f"   Tasks completed:    {state.total_completed}")
    print(f"   Tasks failed:       {state.total_failed}")
    print(f"   Tasks remaining:    {remaining}")
    if failed_attempts > 0:
        print(f"   Failed attempts:    {failed_attempts} (retried successfully)")


def cmd_status(args, config: ExecutorConfig):
    """Execution status"""

    state = ExecutorState(config)

    # Calculate statistics from actual task state
    completed_tasks = sum(1 for ts in state.tasks.values() if ts.status == "success")
    failed_tasks = sum(1 for ts in state.tasks.values() if ts.status == "failed")
    running_tasks = [ts for ts in state.tasks.values() if ts.status == "running"]
    failed_attempts = sum(1 for ts in state.tasks.values() for a in ts.attempts if not a.success)

    print("\n📊 Executor Status")
    print(f"{'=' * 50}")
    print(f"Tasks completed:       {completed_tasks}")
    print(f"Tasks failed:          {failed_tasks}")
    if running_tasks:
        print(f"Tasks in progress:     {len(running_tasks)}")
    if failed_attempts > 0:
        print(f"Failed attempts:       {failed_attempts} (retried)")
    print(f"Consecutive failures:  {state.consecutive_failures}/{config.max_consecutive_failures}")

    # Tasks with attempts
    attempted = [ts for ts in state.tasks.values() if ts.attempts]
    if attempted:
        print("\n📝 Task History:")
        for ts in attempted:
            icon = "✅" if ts.status == "success" else "❌" if ts.status == "failed" else "🔄"
            attempts_info = f"{ts.attempt_count} attempt"
            if ts.attempt_count > 1:
                attempts_info += "s"
            print(f"   {icon} {ts.task_id}: {ts.status} ({attempts_info})")
            if ts.status == "failed" and ts.last_error:
                print(f"      Last error: {ts.last_error[:50]}...")
            elif ts.status == "running" and ts.last_error:
                print(f"      ⚠️  Last attempt failed: {ts.last_error[:50]}...")


def cmd_retry(args, config: ExecutorConfig):
    """Retry failed task, preserving error context from previous attempts."""

    tasks = parse_tasks(config.tasks_file)
    state = ExecutorState(config)

    task = get_task_by_id(tasks, args.task_id.upper())
    if not task:
        print(f"❌ Task {args.task_id} not found")
        return

    task_state = state.get_task_state(task.id)

    # Handle --fresh flag
    if hasattr(args, "fresh") and args.fresh:
        print("🧹 Fresh start: clearing previous attempts")
        task_state.attempts = []
    else:
        # Keep previous attempts for context (Claude will see past errors)
        previous_attempts = len(task_state.attempts)
        if previous_attempts > 0:
            print(f"📋 Preserving {previous_attempts} previous attempt(s) for context")
            # Show last error for reference
            if task_state.last_error:
                error_preview = task_state.last_error[:100]
                print(f"   Last error: {error_preview}...")

    # Only reset status and failure counter
    task_state.status = "pending"
    state.consecutive_failures = 0
    state._save()

    print(f"🔄 Retrying {task.id}...")

    # Execute single attempt (not run_with_retries which has max_retries limit)
    success = execute_task(task, config, state)

    if success:
        update_task_status(config.tasks_file, task.id, "done")
        mark_all_checklist_done(config.tasks_file, task.id)
    else:
        update_task_status(config.tasks_file, task.id, "blocked")


def cmd_logs(args, config: ExecutorConfig):
    """Show task logs"""

    task_id = args.task_id.upper()
    log_files = sorted(config.logs_dir.glob(f"{task_id}-*.log"))

    if not log_files:
        print(f"No logs found for {task_id}")
        return

    latest = log_files[-1]
    print(f"📄 Latest log: {latest}")
    print("=" * 50)
    print(latest.read_text()[:5000])  # Limit output


def cmd_stop(args, config: ExecutorConfig):
    """Request graceful shutdown of the running executor."""
    stop_file = config.stop_file
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text(f"Stop requested at {datetime.now().isoformat()}\n")
    print("🛑 Stop requested — executor will finish current task and exit")
    print(f"   Created {stop_file}")


def cmd_reset(args, config: ExecutorConfig):
    """Reset executor state"""

    if config.state_file.exists():
        config.state_file.unlink()
        print("✅ State reset")

    clear_stop_file(config)

    if args.logs and config.logs_dir.exists():
        shutil.rmtree(config.logs_dir)
        print("✅ Logs cleared")


def cmd_plan(args, config: ExecutorConfig):
    """Interactive task planning via Claude."""

    description = args.description
    print(f"\n📝 Planning: {description}")
    print("=" * 60)

    # Load context
    requirements_summary = "No requirements.md found"
    if config.requirements_file.exists():
        content = config.requirements_file.read_text()
        # Extract just headers and first lines for summary
        lines = content.split("\n")[:100]
        requirements_summary = "\n".join(lines) + "\n...(truncated)"

    design_summary = "No design.md found"
    if config.design_file.exists():
        content = config.design_file.read_text()
        lines = content.split("\n")[:100]
        design_summary = "\n".join(lines) + "\n...(truncated)"

    # Get existing tasks
    existing_tasks = "No existing tasks"
    if config.tasks_file.exists():
        tasks = parse_tasks(config.tasks_file)
        task_lines = [f"- {t.id}: {t.name} ({t.status})" for t in tasks[-20:]]
        existing_tasks = "\n".join(task_lines) if task_lines else "No tasks yet"

    # Load template
    template = load_prompt_template("plan")

    if template:
        prompt = render_template(
            template,
            {
                "DESCRIPTION": description,
                "REQUIREMENTS_SUMMARY": requirements_summary,
                "DESIGN_SUMMARY": design_summary,
                "EXISTING_TASKS": existing_tasks,
            },
        )
    else:
        prompt = f"""# Task Planning Request

## Feature Description:
{description}

## Project Context:

### Requirements (excerpt):
{requirements_summary}

### Existing Tasks:
{existing_tasks}

## Instructions:

Create structured tasks for this feature. For each task use format:

### TASK-XXX: <title>
🔴 P0 | ⬜ TODO | Est: Xd

**Checklist:**
- [ ] Implementation items
- [ ] Tests

When done, respond with: PLAN_READY
"""

    log_progress(f"📝 Planning: {description}")

    # Save prompt
    log_file = config.logs_dir / f"plan-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w") as f:
        f.write(f"=== PLAN PROMPT ===\n{prompt}\n\n")

    # Interactive loop
    conversation_history = []

    while True:
        # Run Claude
        try:
            cmd = [config.claude_command, "-p", prompt]
            if config.skip_permissions:
                cmd.append("--dangerously-skip-permissions")

            print("\n🤖 Claude is analyzing...")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.task_timeout_minutes * 60,
                cwd=config.project_root,
            )

            output = result.stdout

            # Save output
            with open(log_file, "a") as f:
                f.write(f"=== OUTPUT ===\n{output}\n\n")

            # Check for API errors
            error_pattern = check_error_patterns(output + result.stderr)
            if error_pattern:
                print(f"\n⚠️  API error: {error_pattern}")
                return

            # Check for QUESTION
            question_match = re.search(r"QUESTION:\s*(.+?)(?:OPTIONS:|$)", output, re.DOTALL)
            if question_match:
                question = question_match.group(1).strip()
                print(f"\n❓ {question}")

                # Extract options
                options_match = re.search(r"OPTIONS:\s*(.+?)(?:$)", output, re.DOTALL)
                if options_match:
                    options_text = options_match.group(1)
                    options = re.findall(r"[-*]\s*(.+)", options_text)
                    if options:
                        print("\nOptions:")
                        for i, opt in enumerate(options, 1):
                            print(f"  {i}. {opt.strip()}")
                        print(f"  {len(options) + 1}. Other (type custom answer)")

                        choice = input("\nYour choice (number or text): ").strip()

                        # Determine answer
                        try:
                            idx = int(choice)
                            if 1 <= idx <= len(options):
                                answer = options[idx - 1].strip()
                            else:
                                answer = input("Enter your answer: ").strip()
                        except ValueError:
                            answer = choice

                        # Add to conversation
                        conversation_history.append(f"Q: {question}\nA: {answer}")
                        prompt = f"{prompt}\n\nPrevious Q&A:\n" + "\n".join(conversation_history)
                        prompt += f"\n\nContinue planning with the answer: {answer}"
                        continue

                # No parseable options, ask for freeform input
                answer = input("\nYour answer: ").strip()
                conversation_history.append(f"Q: {question}\nA: {answer}")
                prompt += f"\n\nAnswer: {answer}\n\nContinue planning."
                continue

            # Check for TASK_PROPOSAL or PLAN_READY
            if "PLAN_READY" in output or "TASK_PROPOSAL" in output:
                print("\n" + "=" * 60)
                print("📋 Proposed Tasks:")
                print("=" * 60)

                # Extract task proposals
                task_blocks = re.findall(
                    r"### (TASK-\d+:.+?)(?=### TASK-|\Z|PLAN_READY)",
                    output,
                    re.DOTALL,
                )

                for block in task_blocks:
                    print(f"\n### {block.strip()[:500]}")

                print("\n" + "=" * 60)

                # Ask for confirmation
                confirm = input("\nAdd these tasks to tasks.md? [y/N/edit]: ").strip().lower()

                if confirm == "y":
                    # Append tasks to tasks.md
                    tasks_file = config.tasks_file
                    content = tasks_file.read_text() if tasks_file.exists() else "# Tasks\n\n"

                    for block in task_blocks:
                        content += f"\n### {block.strip()}\n"

                    tasks_file.write_text(content)
                    print(f"\n✅ Added {len(task_blocks)} task(s) to {tasks_file}")
                    log_progress(f"✅ Created {len(task_blocks)} tasks")

                elif confirm == "edit":
                    print(f"\nEdit {config.tasks_file} manually, then run 'executor.py run'")

                else:
                    print("\n❌ Cancelled")

                return

            # No recognizable signal, show output and exit
            print("\n📄 Claude response:")
            print(output[:2000])
            return

        except subprocess.TimeoutExpired:
            print(f"\n⏰ Planning timeout after {config.task_timeout_minutes}m")
            return
        except KeyboardInterrupt:
            print("\n\n❌ Cancelled by user")
            return
        except Exception as e:
            print(f"\n💥 Error: {e}")
            return


# === Main ===


def main():
    # Shared options available to every subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--max-retries", type=int, default=3, help="Max retries per task (default: 3)"
    )
    common.add_argument(
        "--timeout", type=int, default=30, help="Task timeout in minutes (default: 30)"
    )
    common.add_argument("--no-tests", action="store_true", help="Skip tests on task completion")
    common.add_argument("--no-branch", action="store_true", help="Skip git branch creation")
    common.add_argument("--no-commit", action="store_true", help="Skip auto-commit on success")
    common.add_argument("--no-review", action="store_true", help="Skip code review after task")
    common.add_argument(
        "--callback-url", type=str, default="", help="URL to POST task status updates to"
    )
    common.add_argument(
        "--spec-prefix",
        type=str,
        default="",
        help='Spec file prefix (e.g. "phase5-" for phase5-tasks.md)',
    )
    common.add_argument(
        "--project-root",
        type=str,
        default="",
        help="Project root directory (default: current directory)",
    )

    parser = argparse.ArgumentParser(
        description="spec-runner — task automation from markdown specs via Claude CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # run
    run_parser = subparsers.add_parser("run", parents=[common], help="Execute tasks")
    run_parser.add_argument("--task", "-t", help="Specific task ID")
    run_parser.add_argument("--all", "-a", action="store_true", help="Run all ready tasks")
    run_parser.add_argument("--milestone", "-m", help="Filter by milestone")
    run_parser.add_argument(
        "--restart",
        action="store_true",
        help="Ignore in-progress tasks, start fresh with TODO tasks only",
    )

    # status
    subparsers.add_parser("status", parents=[common], help="Show execution status")

    # retry
    retry_parser = subparsers.add_parser("retry", parents=[common], help="Retry failed task")
    retry_parser.add_argument("task_id", help="Task ID to retry")
    retry_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Clear previous attempts (start fresh, no error context)",
    )

    # logs
    logs_parser = subparsers.add_parser("logs", parents=[common], help="Show task logs")
    logs_parser.add_argument("task_id", help="Task ID")

    # stop
    subparsers.add_parser("stop", parents=[common], help="Graceful shutdown of running executor")

    # reset
    reset_parser = subparsers.add_parser("reset", parents=[common], help="Reset executor state")
    reset_parser.add_argument("--logs", action="store_true", help="Also clear logs")

    # plan
    plan_parser = subparsers.add_parser("plan", parents=[common], help="Interactive task planning")
    plan_parser.add_argument("description", help="Feature description")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Load config from YAML file, then override with CLI args
    yaml_config = load_config_from_yaml()
    config = build_config(yaml_config, args)

    # Dispatch
    commands = {
        "run": cmd_run,
        "status": cmd_status,
        "retry": cmd_retry,
        "logs": cmd_logs,
        "stop": cmd_stop,
        "reset": cmd_reset,
        "plan": cmd_plan,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args, config)


if __name__ == "__main__":
    main()
