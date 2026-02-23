"""Backward-compatible re-exports.

All public API is available from this module for existing imports.
Implementation moved to execution.py, parallel.py, cli.py.
"""

from .logging import get_logger

logger = get_logger("executor")

# Global shutdown flag — kept here because state.py imports it from .executor
_shutdown_requested = False


def _signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM by setting shutdown flag."""
    global _shutdown_requested
    _shutdown_requested = True


# Re-exports from cli.py
from .cli import (  # noqa: E402, F401
    _run_tasks,
    cmd_costs,
    cmd_logs,
    cmd_mcp,
    cmd_plan,
    cmd_reset,
    cmd_retry,
    cmd_run,
    cmd_status,
    cmd_stop,
    cmd_tui,
    cmd_validate,
    cmd_watch,
    main,
)

# Re-exports from execution.py
from .execution import (  # noqa: E402, F401
    _EXPONENTIAL_ERRORS,
    _FATAL_ERRORS,
    classify_retry_strategy,
    compute_retry_delay,
    execute_task,
    run_with_retries,
)

# Re-exports from parallel.py
from .parallel import _execute_task_async, _run_tasks_parallel  # noqa: E402, F401
