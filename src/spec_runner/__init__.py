"""
spec-runner — task automation from markdown specs via Claude CLI.

Usage as library:
    from spec_runner import ExecutorConfig, Task
    from spec_runner import parse_tasks, get_next_tasks

Usage as CLI:
    spec-runner run            # Execute next task
    spec-runner run --all      # Execute all ready tasks
    spec-runner status         # Execution status

    spec-task list             # List all tasks
    spec-task next             # Show next ready tasks
    spec-task stats            # Statistics
"""

from importlib.metadata import PackageNotFoundError, version

from .config import ExecutorConfig, build_config, load_config_from_yaml
from .executor import (
    classify_retry_strategy,
    cmd_costs,
    cmd_watch,
    compute_retry_delay,
    execute_task,
    run_with_retries,
)
from .executor import (
    main as executor_main,
)
from .logging import get_logger, setup_logging
from .mcp_server import run_server as mcp_run_server
from .plugins import (
    PluginHook,
    PluginInfo,
    build_task_env,
    discover_plugins,
    run_plugin_hooks,
)
from .prompt import (
    SPEC_STAGES,
    build_generation_prompt,
    build_task_prompt,
    parse_spec_marker,
)
from .runner import parse_token_usage, run_claude_async
from .state import (
    ErrorCode,
    ExecutorState,
    RetryContext,
    ReviewVerdict,
    TaskAttempt,
    TaskState,
    recover_stale_tasks,
)
from .task import (
    TASKS_FILE,
    Task,
    get_in_progress_tasks,
    get_next_tasks,
    get_task_by_id,
    mark_all_checklist_done,
    parse_tasks,
    resolve_dependencies,
    update_checklist_item,
    update_task_status,
)
from .tui import LogPanel
from .validate import (
    ValidationResult,
    format_results,
    validate_all,
    validate_config,
    validate_tasks,
)

try:
    __version__ = version("spec-runner")
except PackageNotFoundError:
    __version__ = "0.0.0.dev"  # Fallback for development without install
__all__ = [
    # Task management
    "Task",
    "TASKS_FILE",
    "parse_tasks",
    "get_next_tasks",
    "get_in_progress_tasks",
    "get_task_by_id",
    "resolve_dependencies",
    "update_task_status",
    "update_checklist_item",
    "mark_all_checklist_done",
    # Executor
    "classify_retry_strategy",
    "cmd_costs",
    "cmd_watch",
    "compute_retry_delay",
    "ErrorCode",
    "ExecutorConfig",
    "ExecutorState",
    "RetryContext",
    "ReviewVerdict",
    "TaskAttempt",
    "TaskState",
    "build_config",
    "build_generation_prompt",
    "build_task_prompt",
    "parse_spec_marker",
    "SPEC_STAGES",
    "execute_task",
    "load_config_from_yaml",
    "parse_token_usage",
    "run_claude_async",
    "recover_stale_tasks",
    "run_with_retries",
    "executor_main",
    # Plugins
    "PluginHook",
    "PluginInfo",
    "build_task_env",
    "discover_plugins",
    "run_plugin_hooks",
    # Validation
    "ValidationResult",
    "format_results",
    "validate_all",
    "validate_config",
    "validate_tasks",
    # TUI
    "LogPanel",
    # MCP
    "mcp_run_server",
    # Logging
    "get_logger",
    "setup_logging",
]
