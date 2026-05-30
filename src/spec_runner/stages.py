"""Per-task sub-stage tracking and mirroring (v2.3.0).

One StageReporter per task. Threaded explicitly through execution; safe
with `max_concurrent > 1` because each task gets its own reporter and
there are no thread-locals.
"""

from __future__ import annotations

from collections.abc import Callable

STAGES: tuple[str, ...] = (
    "sync_deps",
    "branch",
    "codex",
    "parse",
    "tests",
    "lint",
    "commit",
    "merge",
    "review",
)


class StageReporter:
    """Track the current sub-stage of a task and mirror transitions.

    Args:
        task_id: ID used in the mirrored line (e.g., "TASK-001").
        mirror: callable invoked with the formatted line for each transition.
    """

    def __init__(self, task_id: str, mirror: Callable[[str], None]) -> None:
        self.task_id = task_id
        self._mirror = mirror
        self.current: str | None = None

    def enter(self, name: str) -> None:
        """Enter a new stage, update `current`, and emit the mirror line.

        Raises AssertionError if `name` is not in STAGES.
        """
        assert name in STAGES, f"unknown stage: {name!r}"
        self.current = name
        self._mirror(f"[{self.task_id}] ⏳ stage: {name}")
