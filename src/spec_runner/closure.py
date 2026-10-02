"""Run closure: the vocabulary of how an invocation ended (FR-07, design § 6.3).

Five kinds, pinned by ``schemas/run-closure.schema.json``. The kind is *derived*
by one function from two facts the dispatcher observes itself -- how the
handler left, and whether work was left undone -- and never chosen by an exit
site. ``completed`` is the single cell "exit 0 and nothing undone"; there is no
"reason unknown, so completed" default.
"""

from __future__ import annotations

from dataclasses import dataclass

CLOSURE_KINDS: tuple[str, ...] = ("completed", "refused", "failed", "interrupted", "crashed")

#: The subset of ``errors.ERROR_KINDS`` that means "a rule said no", as opposed
#: to "the instrument could not answer". Taken from the tree's own vocabulary
#: (a second dictionary for the same states is how #230 happened).
REFUSAL_ERROR_KINDS: frozenset[str] = frozenset(
    {"policy", "budget", "blocked", "hook_failure", "harness_guard"}
)


@dataclass(frozen=True)
class Outcome:
    """What the dispatcher saw when the handler returned or raised."""

    exit_code: int
    #: An unhandled exception that is not ``SystemExit``.
    crashed: bool = False
    #: ``executor._shutdown_requested`` was raised, or ``KeyboardInterrupt``.
    interrupted: bool = False
    #: The ``error_kind`` of this invocation's last unsuccessful attempt.
    last_error_kind: str | None = None
    #: This invocation recorded an attempt on a task that is not ``success``,
    #: or left an open call.
    unfinished_work: bool = False


def derive(outcome: Outcome) -> str:
    """The closure kind, rows read top to bottom, first match wins."""
    if outcome.crashed:
        return "crashed"
    if outcome.interrupted:
        return "interrupted"
    if outcome.exit_code != 0:
        if outcome.last_error_kind in REFUSAL_ERROR_KINDS:
            return "refused"
        return "failed"
    return "failed" if outcome.unfinished_work else "completed"


__all__ = ["CLOSURE_KINDS", "REFUSAL_ERROR_KINDS", "Outcome", "derive"]
