"""Per-task sub-stage tracking and mirroring (v2.3.0).

One StageReporter per task. Threaded explicitly through execution; safe
with `max_concurrent > 1` because each task gets its own reporter and
there are no thread-locals.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .state import PhaseOutcome

STAGES: tuple[str, ...] = (
    "sync_deps",
    "branch",
    # CLI-agnostic name for the agent-execution stage. Was "codex" (≤2.11),
    # which read as the codex CLI even on claude runs (#74); historical
    # error_stage rows may still carry the old value.
    "exec",
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

    def __init__(
        self,
        task_id: str,
        mirror: Callable[[str], None],
        sink: Callable[[str, PhaseOutcome, str | None], None] | None = None,
    ) -> None:
        self.task_id = task_id
        self._mirror = mirror
        # Optional: where typed outcomes go (slice 0). None keeps the reporter
        # a pure progress mirror, which is what every existing caller expects.
        self._sink = sink
        self.current: str | None = None

    def enter(self, name: str) -> None:
        """Enter a new stage, update `current`, and emit the mirror line.

        Raises AssertionError if `name` is not in STAGES.
        """
        assert name in STAGES, f"unknown stage: {name!r}"
        self.current = name
        self._mirror(f"[{self.task_id}] ⏳ stage: {name}")

    def record(self, outcome: PhaseOutcome, detail: str | None = None) -> None:
        """Record the outcome of the stage that is currently entered.

        A no-op without a sink and outside a stage, so adding calls to this is
        never able to change control flow — the slice-0 guarantee is that
        execution and terminal state do not move.
        """
        if self.current is None:
            return
        self.record_for(self.current, outcome, detail)

    def record_for(self, phase: str, outcome: PhaseOutcome, detail: str | None = None) -> None:
        """Record an outcome for ``phase`` **without** entering it.

        `current` feeds `attempts.error_stage`, a documented field consumers
        read, so recording an outcome must never move it. Entering a stage just
        to record its result is how slice 0 would have silently changed that
        contract — caught by `TestErrorStageRecorded`.
        """
        if self._sink is None:
            return
        self._sink(phase, outcome, detail)
