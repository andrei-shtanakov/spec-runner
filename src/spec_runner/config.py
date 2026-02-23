"""Configuration module for spec-runner.

Contains ExecutorConfig dataclass, file-based locking, config loading
from YAML, and config building from CLI arguments.
"""

import argparse
import contextlib
import fcntl
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

import yaml

# === File Lock ===


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


# === Constants ===

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


# === ExecutorConfig ===


@dataclass
class ExecutorConfig:
    """Executor configuration"""

    max_retries: int = 3  # Max attempts per task
    retry_delay_seconds: int = 5  # Pause between attempts
    task_timeout_minutes: int = 30  # Task timeout
    max_consecutive_failures: int = 2  # Stop after N consecutive failures
    on_task_failure: str = "skip"  # What to do when task fails: skip | stop | ask
    max_concurrent: int = 3  # Max parallel tasks
    budget_usd: float | None = None  # Global budget limit (None = unlimited)
    task_budget_usd: float | None = None  # Per-task budget limit (None = unlimited)

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
    state_file: Path = Path("spec/.executor-state.db")

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
            default_state = Path("spec/.executor-state.db")
            default_logs = Path("spec/.executor-logs")
            if self.state_file == default_state:
                self.state_file = Path(f"spec/.executor-{self.spec_prefix}state.db")
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


# === Config Loading ===


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
            "max_concurrent": executor_config.get("max_concurrent"),
            "budget_usd": executor_config.get("budget_usd"),
            "task_budget_usd": executor_config.get("task_budget_usd"),
        }
    except Exception as e:
        print(f"\u26a0\ufe0f  Warning: Failed to load config from {config_path}: {e}")
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
    if hasattr(args, "max_concurrent") and getattr(args, "max_concurrent", 0) > 0:
        config_kwargs["max_concurrent"] = args.max_concurrent
    if hasattr(args, "budget") and getattr(args, "budget", None) is not None:
        config_kwargs["budget_usd"] = args.budget
    if hasattr(args, "task_budget") and getattr(args, "task_budget", None) is not None:
        config_kwargs["task_budget_usd"] = args.task_budget

    return ExecutorConfig(**config_kwargs)
