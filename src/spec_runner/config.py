"""Configuration module for spec-runner.

Contains ExecutorConfig dataclass, file-based locking, config loading
from YAML, and config building from CLI arguments.
"""

import argparse
import contextlib
import fcntl
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, TextIO

import yaml

if TYPE_CHECKING:
    from .spec import StageProfile

# === Errors ===


class ConfigError(ValueError):
    """Raised for invalid configuration values (e.g. an unknown ``spec_profile``)."""


# === Persona ===


@dataclass
class Persona:
    """Agent persona for phase-specific prompt customization."""

    system_prompt: str = ""
    model: str = ""
    focus: list[str] = field(default_factory=list)


# === File Lock ===


class ExecutorLock:
    """File lock to prevent parallel executor runs."""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.lock_file: TextIO | None = None
        self._held_by: dict[str, str] = {}

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
            held_by = self._read_lock_info()
            self.lock_file.close()
            self.lock_file = None
            if held_by:
                pid_str = held_by.get("pid")
                if pid_str and not self._is_pid_alive(int(pid_str)):
                    held_by["alive"] = "false"
            self._held_by = held_by
            return False

    def release(self):
        """Release the lock."""
        if self.lock_file:
            fcntl.flock(self.lock_file, fcntl.LOCK_UN)
            self.lock_file.close()
            self.lock_file = None
            with contextlib.suppress(FileNotFoundError):
                self.lock_path.unlink()

    def _read_lock_info(self) -> dict[str, str]:
        """Read PID and start time from existing lock file."""
        try:
            content = self.lock_path.read_text()
            info: dict[str, str] = {}
            for line in content.splitlines():
                if line.startswith("PID:"):
                    info["pid"] = line.split(":", 1)[1].strip()
                elif line.startswith("Started:"):
                    info["started"] = line.split(":", 1)[1].strip()
            return info
        except Exception:
            return {}

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Check if a process with the given PID is alive."""
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False


# === Constants ===

# Configuration file paths (new and legacy)
CONFIG_FILE = Path("spec-runner.config.yaml")  # v2.0 location (project root)
LEGACY_CONFIG_FILE = Path("spec/executor.config.yaml")  # v1.x location
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


# Change ids become directory names and dated archive prefixes: lowercase
# alnum plus ._- (no leading separator); "archive" is the archive dir itself.
_CHANGE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def _validate_change_id(change_id: str) -> None:
    """Raise ConfigError when ``change_id`` can't be a safe change dir name."""
    if change_id == "archive" or not _CHANGE_ID_RE.match(change_id):
        raise ConfigError(
            f"invalid change id {change_id!r}: use lowercase letters, digits, "
            "'.', '_' or '-' (must not start with a separator; 'archive' is reserved)"
        )


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
    task_budget_usd: float | None = None  # Total per-task budget (includes attempt 1)
    # Cap on cumulative cost of retry attempts only (attempt 2+). None = unlimited.
    # Use when you want the initial attempt to always run but want to stop a
    # flaky task from burning budget on repeated retries. LABS-41.
    max_retry_cost_usd: float | None = None
    log_level: str = "info"  # Logging level (debug, info, warning, error)

    # Claude CLI
    claude_command: str = "claude"  # Claude CLI command
    claude_model: str = ""  # Model (empty = default)
    skip_permissions: bool = True  # Skip permission prompts
    # Command template for custom CLIs. Placeholders: {cmd}, {model}, {prompt}, {prompt_file}
    # Examples:
    #   claude: "{cmd} -p {prompt}" or "{cmd} -p {prompt} --model {model}"
    #   codex: "{cmd} exec {prompt}"   # -p is --profile in codex, not the prompt
    #   opencode: "{cmd} run --model {model} {prompt}"
    #   pi: "{cmd} -p --model {model} {prompt}"
    #   ollama: "{cmd} run {model} {prompt}"
    #   llama-cli: "{cmd} -m {model} -p {prompt} --no-display-prompt"
    #   llama-server: "curl -s http://localhost:8080/completion -d '{{\"prompt\": {prompt}}}'"
    # If empty, auto-detects based on command name (claude, codex, opencode, pi,
    # ollama, llama-cli, llama-server)
    command_template: str = ""

    # Hooks
    run_tests_on_done: bool = True  # Run tests on completion
    create_git_branch: bool = True  # Create branch on start
    auto_commit: bool = True  # Auto-commit on success
    main_branch: str = ""  # Main branch name (empty = auto-detect: main/master)
    # Integration mode: fork one branch per run, merge every task into it, and
    # open a single PR at the end instead of self-merging tasks into main. For
    # repos with a remote where a human reviews/merges (never touches main).
    integration_pr: bool = False
    sync_deps: bool = True  # Run dependency sync in pre_start_hook (doctor disables this)
    # Dependency sync command (commands.sync in YAML). Empty = auto: run
    # `uv sync` only when pyproject.toml exists, else skip quietly — a
    # hardcoded `uv sync` produced per-run stderr noise on every
    # non-Python project (#70).
    sync_command: str = ""

    # Code review
    run_review: bool = True  # Run code review after task completion
    hitl_review: bool = False  # Interactive approval gate after code review
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
    # Bounded regeneration when generated spec content fails validation (#160).
    # Not unlimited: an LLM that cannot satisfy the validator twice will not
    # satisfy it on the tenth try either, and each attempt costs money.
    spec_repair_attempts: int = 2
    # Bounded recovery when a pre-terminal policy gate cannot answer (#164).
    # An instrument error is not a defect in the work, so it is retried — but
    # boundedly, and then reported as an infrastructure error rather than
    # escalated to a human on the first stumble.
    gate_recovery_attempts: int = 1

    # Change-as-folder id (M2): scope every spec path to
    # spec/changes/<change_id>/ (CLI --change). Empty = flat spec/ layout.
    # Mutually exclusive with spec_prefix.
    change_id: str = ""

    # Test command (using uv)
    test_command: str = "uv run pytest tests/ -v -m 'not slow'"
    # Narrow the test command to the files a task touched, in parallel mode
    # only. Set False where the contract is the whole suite — a workstream
    # acceptance or a release gate proves nothing if the gate quietly ran a
    # subset (#139). Composite `test_command`s are never narrowed regardless.
    scoped_tests: bool = True
    lint_command: str = "uv run ruff check ."
    lint_fix_command: str = "uv run ruff check . --fix"  # Lint auto-fix command
    run_lint_on_done: bool = True  # Run lint on completion
    lint_blocking: bool = True  # Lint errors block task completion
    plugins_dir: Path = Path("spec/plugins")  # Plugin hooks directory

    # Timeouts
    session_timeout_minutes: int = 0  # Global session timeout (0 = disabled)
    idle_timeout_minutes: int = 0  # Idle timeout between tasks (0 = disabled)

    # Agent personas (role-specific prompts and models)
    personas: dict[str, Persona] = field(default_factory=dict)

    # Parallel review (multiple specialized review agents)
    review_parallel: bool = False  # Run review agents in parallel
    review_roles: list[str] = field(
        default_factory=lambda: ["quality", "implementation", "testing"]
    )

    # Notifications
    notify_project_name: str = ""  # Project name in notifications (default: directory name)
    telegram_bot_token: str = ""  # Telegram bot token (empty = disabled)
    telegram_chat_id: str = ""  # Telegram chat ID to send notifications to
    notify_on: list[str] = field(
        default_factory=lambda: ["run_complete", "task_failed", "state_degraded", "pr_opened"]
    )

    # review-pr loop (#102, M1): bot identities whose PR comments the loop
    # may process. Comments from anyone else — including humans — are
    # ignored, never verified and (in later phases) never answered.
    review_pr_allowed_bots: list[str] = field(
        default_factory=lambda: ["Copilot", "copilot-pull-request-reviewer[bot]"]
    )
    # review-pr loop (#102, M2): hard limits — exceeding any of them stops
    # the loop with NEEDS_HUMAN instead of grinding on.
    review_pr_max_rounds: int = 3  # bounded rounds per PR (a new head SHA opens one)
    review_pr_max_comments: int = 20  # per-invocation comment cap
    review_pr_max_changed_lines: int = 300  # per-fix diff size cap (insertions+deletions)
    review_pr_max_wall_minutes: int = 30  # per-invocation wall-clock cap
    review_pr_max_cost_usd: float = 5.0  # per-invocation fix-agent cost cap
    # review-pr loop (#102, M3): optional post-PR stage after integration_pr.
    # off (default — integration_pr behavior unchanged) | verify (read-only
    # triage) | full (check out the run branch, run the fix+reply loop).
    review_pr_post_pr: str = "off"
    review_pr_post_pr_wait_seconds: int = 120  # let the review bot comment first

    # Generic webhook notifications
    webhook_url: str = ""  # Webhook URL (empty = disabled)
    webhook_method: str = "POST"  # HTTP method
    webhook_headers: dict[str, str] = field(default_factory=dict)
    webhook_template: str = ""  # Template with {{event}}, {{task_id}}, {{message}}, etc.

    # Compliance audit trail (JSON Lines; LABS-40). Empty path = disabled.
    audit_log_path: str = ""
    audit_log_operator: str = ""  # Override auto-detected "user@host"

    # Spec governance: "off" (default) | "strict" (gate run on approved tasks.md)
    spec_governance: str = "off"

    # Gated spec-generation profile name (resolves to a spec.StageProfile).
    spec_profile: str = "lite"

    # Project-wide context prepended to every spec-generation stage prompt,
    # wrapped in <context>...</context> (OpenSpec-style, M0).
    spec_context: str = ""

    # Per-stage generation rules keyed by stage name (e.g. "requirements"),
    # injected only for the matching stage, wrapped in <rules>...</rules>.
    spec_rules: dict[str, list[str]] = field(default_factory=dict)

    # Harness-mutation tripwire (#64): the verification harness (test/lint
    # config, dependency manifests, CI workflows) is writable by the agent
    # under test — an agent can satisfy the gates by patching the oracle.
    # off | warn (log mutations) | strict (fail the attempt before gates).
    harness_guard: str = "warn"
    # Extra harness paths (project-root-relative files or dirs) to watch.
    harness_files: list[str] = field(default_factory=list)
    # Glob patterns exempt from strict-mode violations (e.g. ["uv.lock"]).
    harness_allow: list[str] = field(default_factory=list)

    # False when no config file backed this run (CLI flags may still have
    # overridden individual defaults). Set by main() after load; execution
    # commands warn on it (#63) — a silently vanished config once flipped a
    # run to self-merge + pytest on an Elixir repo.
    config_found: bool = True

    def __post_init__(self):
        """Resolve project_root and namespace state/log paths by spec_prefix/change_id."""
        self.project_root = self.project_root.resolve()

        # Harness-guard config sanity (#64): a YAML typo must not silently
        # degrade the guard or crash later (a bare string would iterate
        # per-character as paths).
        if self.harness_guard not in ("off", "warn", "strict"):
            raise ConfigError(
                f"invalid harness_guard {self.harness_guard!r}: "
                "expected one of 'off', 'warn', 'strict'"
            )
        for attr in ("harness_files", "harness_allow"):
            value = getattr(self, attr)
            if isinstance(value, str):
                setattr(self, attr, [value])
            elif not isinstance(value, list):
                raise ConfigError(f"{attr} must be a list of paths, got {type(value).__name__}")

        if self.change_id:
            if self.spec_prefix:
                raise ConfigError(
                    "change_id and spec_prefix are mutually exclusive: a change is "
                    "its own spec dir, a prefix namespaces within one"
                )
            _validate_change_id(self.change_id)

        default_state = Path("spec/.executor-state.db")
        default_logs = Path("spec/.executor-logs")
        if self.spec_prefix:
            if self.state_file == default_state:
                self.state_file = Path(f"spec/.executor-{self.spec_prefix}state.db")
            if self.logs_dir == default_logs:
                self.logs_dir = Path(f"spec/.executor-{self.spec_prefix}logs")
        elif self.change_id:
            # Per-change state also yields a per-change run lock (the lock
            # derives from the state path), so changes run in parallel.
            if self.state_file == default_state:
                self.state_file = Path(f"spec/changes/{self.change_id}/.executor-state.db")
            if self.logs_dir == default_logs:
                self.logs_dir = Path(f"spec/changes/{self.change_id}/.executor-logs")

        if not self.state_file.is_absolute():
            self.state_file = self.project_root / self.state_file
        if not self.logs_dir.is_absolute():
            self.logs_dir = self.project_root / self.logs_dir
        if not self.plugins_dir.is_absolute():
            self.plugins_dir = self.project_root / self.plugins_dir

    @property
    def spec_dir(self) -> Path:
        """The active spec dir: ``spec/changes/<id>/`` under a change, else ``spec/``."""
        base = self.project_root / "spec"
        return base / "changes" / self.change_id if self.change_id else base

    @property
    def stop_file(self) -> Path:
        return self.spec_dir / ".executor-stop"

    @property
    def tasks_file(self) -> Path:
        return self.spec_dir / f"{self.spec_prefix}tasks.md"

    @property
    def requirements_file(self) -> Path:
        return self.spec_dir / f"{self.spec_prefix}requirements.md"

    @property
    def design_file(self) -> Path:
        return self.spec_dir / f"{self.spec_prefix}design.md"

    @property
    def constitution_file(self) -> Path:
        return self.spec_dir / f"{self.spec_prefix}constitution.md"

    @property
    def spec_lock_file(self) -> Path:
        return self.spec_dir / f".{self.spec_prefix}spec.lock"

    @property
    def prompts_dir(self) -> Path:
        """Project-owned prompt templates, resolved from ``project_root`` (#153).

        Namespaced by ``spec_prefix`` like the other per-phase paths, and it
        moves into the change dir under ``--change`` because ``spec_dir`` does.
        Previously this was the module-level relative ``Path("spec/prompts")``,
        i.e. resolved against the process CWD: running against another project
        from a directory that happened to have templates silently used those.
        """
        return self.spec_dir / f"{self.spec_prefix}prompts"

    def get_persona(self, role: str) -> Persona | None:
        """Get persona by role name (e.g., 'implementer', 'reviewer', 'architect')."""
        return self.personas.get(role)

    def get_model_for_role(self, role: str) -> str:
        """Get model for a given role, falling back to claude_model."""
        persona = self.get_persona(role)
        if persona and persona.model:
            return persona.model
        return self.claude_model

    def resolve_spec_profile(self) -> "StageProfile":
        """Resolve ``spec_profile`` (a name) to its :class:`~spec_runner.spec.StageProfile`.

        Raises:
            ConfigError: If the name matches no bundled profile; the message
                lists the available profile names (no traceback for the CLI).
        """
        from .spec import ProfileGraphError, available_profiles, load_profile

        try:
            return load_profile(self.spec_profile)
        except ProfileGraphError as exc:
            # The profile exists but its dependency graph is invalid — surface
            # the real cycle/unknown-stage message, not "unknown profile".
            raise ConfigError(str(exc)) from exc
        except ValueError:
            available = ", ".join(available_profiles())
            raise ConfigError(
                f"unknown spec_profile: {self.spec_profile!r}; available: {available}"
            ) from None


# === Config Loading ===


def _parse_personas(raw: dict) -> dict[str, Persona] | None:
    """Parse personas section from YAML config into Persona objects."""
    if not raw:
        return None
    personas: dict[str, Persona] = {}
    for name, data in raw.items():
        if isinstance(data, dict):
            personas[name] = Persona(
                system_prompt=data.get("system_prompt", ""),
                model=data.get("model", ""),
                focus=data.get("focus", []),
            )
    return personas if personas else None


def _detect_subdir_repo(project_root: Path) -> Path | None:
    """Return the git repo toplevel if `project_root` is a strict subdir of
    a git repo. Return None when project_root IS the toplevel, when no git
    repo wraps it, or when git is not installed.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    toplevel = Path(result.stdout.strip()).resolve()
    return toplevel if toplevel != project_root.resolve() else None


def _user_set(yaml_config: dict, args: argparse.Namespace, key: str) -> bool:
    """True if user explicitly set this key in YAML or CLI."""
    if yaml_config.get(key) is not None:
        return True
    val = getattr(args, key, None)
    return val not in (None, False)


def _resolve_config_path() -> Path:
    """Find the config file, preferring new location over legacy.

    Returns the path to use. Emits deprecation warning for legacy path.
    """
    if CONFIG_FILE.exists():
        if LEGACY_CONFIG_FILE.exists():
            from .logging import get_logger

            get_logger("config").error(
                "Both config files exist — remove the legacy one",
                new=str(CONFIG_FILE),
                legacy=str(LEGACY_CONFIG_FILE),
            )
        return CONFIG_FILE

    if LEGACY_CONFIG_FILE.exists():
        import sys

        print(
            f"WARNING: {LEGACY_CONFIG_FILE} is deprecated. "
            f"Move it to {CONFIG_FILE} (project root).",
            file=sys.stderr,
        )
        return LEGACY_CONFIG_FILE

    return CONFIG_FILE  # default (won't exist, returns empty config)


def missing_config_warning(config: "ExecutorConfig") -> str | None:
    """Operator-facing warning when a run proceeds without a config file (#63).

    Returns None when a config file was found. The message names the
    safety-relevant defaults that now apply (merge mode, test oracle, model);
    if prior run state exists, the config likely vanished rather than never
    existed, so the hint is sharper.
    """
    if config.config_found:
        return None
    # Report the EFFECTIVE values (defaults possibly overridden by CLI
    # flags), not a hard-coded default list — a wrong claim here would be
    # as misleading as the silence this warning replaces.
    if config.integration_pr:
        merge_desc = "integration branch + PR"
    elif config.create_git_branch:
        merge_desc = "self-merge into the main branch (no PR)"
    else:
        merge_desc = "no git branching"
    lines = [
        f"⚠️  No {CONFIG_FILE} found — running on defaults (CLI flags still apply):",
        f"    • merge mode: {merge_desc}",
        f"    • test command: {config.test_command!r}",
        f"    • model: {config.claude_model or '(CLI default)'}",
    ]
    if config.state_file.exists():
        lines.append(
            "    A previous run's state exists here — if this project had a "
            "config file, restore it before running."
        )
    return "\n".join(lines)


def load_config_from_yaml(config_path: Path | None = None) -> dict:
    """Load configuration from YAML file.

    Supports both v2.0 flat format (spec-runner.config.yaml at project root)
    and legacy format (spec/executor.config.yaml with executor: wrapper).

    Args:
        config_path: Explicit path, or None to auto-detect.

    Returns:
        Dictionary with configuration values.
    """
    if config_path is None:
        config_path = _resolve_config_path()

    if not config_path.exists():
        return {}

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

        # Support both v2.0 flat format and v1.x legacy (executor: wrapper)
        executor_config = data.get("executor", {}) if "executor" in data else data
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
            "sync_deps": pre_start.get("sync_deps"),
            "main_branch": executor_config.get("main_branch"),
            "integration_pr": executor_config.get("integration_pr"),
            "run_tests_on_done": post_done.get("run_tests"),
            "run_lint_on_done": post_done.get("run_lint"),
            "lint_blocking": post_done.get("lint_blocking"),
            "auto_commit": post_done.get("auto_commit"),
            "run_review": post_done.get("run_review"),
            "hitl_review": executor_config.get("hitl_review"),
            "review_timeout_minutes": executor_config.get("review_timeout_minutes"),
            "review_command": executor_config.get("review_command"),
            "review_model": executor_config.get("review_model"),
            "command_template": executor_config.get("command_template"),
            "review_command_template": executor_config.get("review_command_template"),
            "test_command": commands.get("test"),
            "lint_command": commands.get("lint"),
            "lint_fix_command": commands.get("lint_fix"),
            "sync_command": commands.get("sync"),
            "project_root": Path(paths["root"]) if paths.get("root") else None,
            "logs_dir": Path(paths["logs"]) if paths.get("logs") else None,
            "state_file": Path(paths["state"]) if paths.get("state") else None,
            "plugins_dir": Path(paths["plugins"]) if paths.get("plugins") else None,
            "callback_url": executor_config.get("callback_url"),
            "spec_prefix": executor_config.get("spec_prefix"),
            "max_concurrent": executor_config.get("max_concurrent"),
            "budget_usd": executor_config.get("budget_usd"),
            "task_budget_usd": executor_config.get("task_budget_usd"),
            "max_retry_cost_usd": executor_config.get("max_retry_cost_usd"),
            "log_level": executor_config.get("log_level"),
            "session_timeout_minutes": executor_config.get("session_timeout_minutes"),
            "idle_timeout_minutes": executor_config.get("idle_timeout_minutes"),
            "personas": _parse_personas(executor_config.get("personas", {})),
            "review_parallel": post_done.get("review_parallel"),
            "review_roles": post_done.get("review_roles"),
            "telegram_bot_token": executor_config.get("telegram_bot_token"),
            "telegram_chat_id": executor_config.get("telegram_chat_id"),
            "notify_on": executor_config.get("notify_on"),
            "webhook_url": executor_config.get("webhook_url"),
            "webhook_method": executor_config.get("webhook_method"),
            "webhook_headers": executor_config.get("webhook_headers"),
            "webhook_template": executor_config.get("webhook_template"),
            "audit_log_path": executor_config.get("audit_log_path"),
            "audit_log_operator": executor_config.get("audit_log_operator"),
            "spec_governance": executor_config.get("spec_governance"),
            "harness_guard": executor_config.get("harness_guard"),
            "harness_files": executor_config.get("harness_files"),
            "harness_allow": executor_config.get("harness_allow"),
            "spec_profile": executor_config.get("spec_profile"),
            "spec_context": executor_config.get("spec_context"),
            "spec_rules": executor_config.get("spec_rules"),
            "review_pr_allowed_bots": (executor_config.get("review_pr") or {}).get("allowed_bots"),
            "review_pr_max_rounds": (executor_config.get("review_pr") or {}).get("max_rounds"),
            "review_pr_max_comments": (executor_config.get("review_pr") or {}).get("max_comments"),
            "review_pr_max_changed_lines": (executor_config.get("review_pr") or {}).get(
                "max_changed_lines"
            ),
            "review_pr_max_wall_minutes": (executor_config.get("review_pr") or {}).get(
                "max_wall_minutes"
            ),
            "review_pr_max_cost_usd": (executor_config.get("review_pr") or {}).get("max_cost_usd"),
            "review_pr_post_pr": (executor_config.get("review_pr") or {}).get("post_pr"),
            "review_pr_post_pr_wait_seconds": (executor_config.get("review_pr") or {}).get(
                "post_pr_wait_seconds"
            ),
        }
    except Exception as e:
        from .logging import get_logger

        get_logger("config").warning("Failed to load config", path=str(config_path), error=str(e))
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
    if hasattr(args, "max_retries") and args.max_retries is not None:
        config_kwargs["max_retries"] = args.max_retries
    if hasattr(args, "timeout") and args.timeout is not None:
        config_kwargs["task_timeout_minutes"] = args.timeout
    if hasattr(args, "no_tests") and args.no_tests:
        config_kwargs["run_tests_on_done"] = False
    if hasattr(args, "no_branch") and args.no_branch:
        config_kwargs["create_git_branch"] = False
    if hasattr(args, "no_commit") and args.no_commit:
        config_kwargs["auto_commit"] = False
    if hasattr(args, "no_review") and args.no_review:
        config_kwargs["run_review"] = False
    if getattr(args, "integration_pr", None):
        config_kwargs["integration_pr"] = True
    if hasattr(args, "callback_url") and args.callback_url:
        config_kwargs["callback_url"] = args.callback_url
    if hasattr(args, "spec_prefix") and args.spec_prefix:
        config_kwargs["spec_prefix"] = args.spec_prefix
    if getattr(args, "change", None):
        config_kwargs["change_id"] = args.change
    if hasattr(args, "project_root") and args.project_root:
        config_kwargs["project_root"] = Path(args.project_root)
    if hasattr(args, "max_concurrent") and getattr(args, "max_concurrent", 0) > 0:
        config_kwargs["max_concurrent"] = args.max_concurrent
    if hasattr(args, "budget") and getattr(args, "budget", None) is not None:
        config_kwargs["budget_usd"] = args.budget
    if hasattr(args, "task_budget") and getattr(args, "task_budget", None) is not None:
        config_kwargs["task_budget_usd"] = args.task_budget
    if hasattr(args, "hitl_review") and getattr(args, "hitl_review", False):
        config_kwargs["hitl_review"] = True
    if getattr(args, "strict", False):
        config_kwargs["spec_governance"] = "strict"
    if getattr(args, "no_strict", False):
        config_kwargs["spec_governance"] = "off"
    if hasattr(args, "log_level") and getattr(args, "log_level", None):
        config_kwargs["log_level"] = args.log_level
    if getattr(args, "profile", None):
        config_kwargs["spec_profile"] = args.profile

    config = ExecutorConfig(**config_kwargs)

    git_root = _detect_subdir_repo(config.project_root)
    if git_root is not None:
        flipped = []
        if not _user_set(yaml_config, args, "create_git_branch"):
            config.create_git_branch = False
            flipped.append("create_git_branch")
        if not _user_set(yaml_config, args, "auto_commit"):
            config.auto_commit = False
            flipped.append("auto_commit")
        if flipped:
            from .logging import get_logger

            get_logger("config").warning(
                "subdir_project_detected",
                project_root=str(config.project_root),
                git_root=str(git_root),
                defaulted_off=flipped,
                override_hint="set create_git_branch/auto_commit=true in YAML to opt-in",
            )

    return config
