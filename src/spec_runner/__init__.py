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
    build_task_prompt,
    execute_task,
    run_with_retries,
)
from .executor import (
    main as executor_main,
)
from .state import (
    ExecutorState,
    TaskAttempt,
    TaskState,
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
    "ExecutorConfig",
    "ExecutorState",
    "TaskAttempt",
    "TaskState",
    "build_config",
    "build_task_prompt",
    "execute_task",
    "load_config_from_yaml",
    "run_with_retries",
    "executor_main",
]
