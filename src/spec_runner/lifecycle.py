"""The TDD lifecycle as a recorded state machine — #141 slice 4a.

Slices 1–3 built the parts: a verified red, a byte-lock on what it depends on,
typed operator remedies. Where a task *was* still lived in inference — read the
checkpoints, read the claims, guess. This makes it a fact.

```
READY → RED_AUTHORING → RED_VERIFYING → GREEN_IMPLEMENTING
      → GREEN_VERIFYING → REFACTORING (skipped) → DONE
```

Two decisions are load-bearing and easy to get wrong:

**`REFACTORING` exists and does nothing.** The phase is materialised so the
machine is complete and a later decision has somewhere to land; its outcome is
always `skipped`, which the vocabulary already has a word for and which is
honest — the stage was deliberately not executed. Running an agent here was
**not approved** (3b): under that one word a new expensive and ill-defined
stage could otherwise arrive without anyone choosing it.

**Backwards is legal.** A remedy sends a task back to authoring and a retry
re-enters implementation; a machine that only moved forward would make both of
those errors. What is *not* legal is reaching GREEN without a red — the one
transition that carries the contract, and the only thing this module refuses.

Contract: ``docs/superpowers/specs/2026-08-11-claim-and-remedy-contracts.md`` §3a
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

from .logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .state import ExecutorState

logger = get_logger("lifecycle")


class TddPhase(str, Enum):
    READY = "ready"
    RED_AUTHORING = "red_authoring"
    RED_VERIFYING = "red_verifying"
    GREEN_IMPLEMENTING = "green_implementing"
    GREEN_VERIFYING = "green_verifying"
    #: Materialised, never executed. See the module docstring.
    REFACTORING = "refactoring"
    DONE = "done"


class IllegalTransition(RuntimeError):
    """A transition the contract forbids."""


_ORDER: tuple[TddPhase, ...] = (
    TddPhase.READY,
    TddPhase.RED_AUTHORING,
    TddPhase.RED_VERIFYING,
    TddPhase.GREEN_IMPLEMENTING,
    TddPhase.GREEN_VERIFYING,
    TddPhase.REFACTORING,
    TddPhase.DONE,
)

#: Transitions that are refused. Deliberately a short, explicit list rather
#: than "anything not in `_ORDER`": most movement between phases is legitimate
#: (retries, remedies), and only reaching green without a demonstrated red is
#: the thing this lifecycle exists to prevent.
ILLEGAL: frozenset[tuple[TddPhase, TddPhase]] = frozenset(
    {
        (TddPhase.READY, TddPhase.GREEN_IMPLEMENTING),
        (TddPhase.READY, TddPhase.GREEN_VERIFYING),
        (TddPhase.RED_AUTHORING, TddPhase.GREEN_IMPLEMENTING),
        (TddPhase.RED_AUTHORING, TddPhase.GREEN_VERIFYING),
    }
)


def next_phase(phase: TddPhase) -> TddPhase | None:
    """The phase that follows in the ordinary path, or None at the end."""
    index = _ORDER.index(phase)
    return _ORDER[index + 1] if index + 1 < len(_ORDER) else None


def is_terminal(phase: TddPhase) -> bool:
    return phase is TddPhase.DONE


def has_reached(state: ExecutorState, namespace: str, task_id: str, phase: TddPhase) -> bool:
    """Did this task **ever** get to ``phase`` or beyond? (#249)

    The high-water mark, not the current position — and the distinction is not
    academic here. **Backwards is legal in this machine** (see the module
    docstring): a retry re-enters `red_authoring`, and a task that reached
    green and was then retried twice reads as `red_authoring` today.

    `tdd resume` asked `current_phase` whether a green existed and refused the
    pilot it was built from, because the wedge it cures *is* a task retrying
    into `red_authoring`. Any question of the form "has this happened" must be
    asked of the history; only "what is happening now" belongs to
    `current_phase`.
    """
    threshold = _ORDER.index(phase)
    for entry in state.tdd_phase_history(task_id, namespace):
        raw = entry["phase"]
        if raw.startswith("refused:"):
            # A refusal records what was attempted, not where the task went.
            continue
        try:
            recorded = TddPhase(raw)
        except ValueError:  # pragma: no cover - a phase from a future version
            continue
        if _ORDER.index(recorded) >= threshold:
            return True
    return False


def current_phase(state: ExecutorState, namespace: str, task_id: str) -> TddPhase:
    """Where the task stands. A task with no history is `READY`."""
    history = state.tdd_phase_history(task_id, namespace)
    for entry in reversed(history):
        raw = entry["phase"]
        if raw.startswith("refused:"):
            # A refusal records what was attempted, not where the task went.
            continue
        try:
            return TddPhase(raw)
        except ValueError:  # pragma: no cover - a phase from a future version
            continue
    return TddPhase.READY


def has_confirmed_red(state: ExecutorState, namespace: str, task_id: str) -> bool:
    """Does this task have a standing confirmed red? (#253)

    The contract this machine enforces is *"GREEN may not be reached without a
    confirmed red"* — a statement about **evidence**, which the code read off
    the previous row instead. Those differ constantly: a reused red records
    `red_authoring` and no verification (there is nothing to replay), and a
    resumed one reinstates the checkpoint without replaying either. Both then
    moved to GREEN legitimately and were refused with the words "GREEN requires
    a confirmed red" while one sat in the database.

    The consequence was not cosmetic: the refusal is non-fatal, so the run
    continued and simply stopped recording green phases — and phase history is
    what `has_reached` reads, which is the admissibility check #249 had just
    fixed.

    Existence only. Whether that red covers *this tree* is the gate's question,
    and it has the candidate SHA to answer it with; a recorder that duplicated
    the test would be a second, weaker gate.
    """
    from .tdd import RedOutcome

    checkpoint = state.red_checkpoint(task_id, namespace)
    return checkpoint is not None and checkpoint.outcome is RedOutcome.EXPECTED_FAIL


def advance(
    state: ExecutorState,
    namespace: str,
    task_id: str,
    target: TddPhase,
    detail: str | None = None,
) -> TddPhase:
    """Move a task to ``target``, recording the transition.

    Raises `IllegalTransition` when the contract forbids it — and records the
    attempt first, because a refused transition is a thing that happened and a
    history of successes only is a poor record of a lifecycle.
    """
    now = current_phase(state, namespace, task_id)
    if (now, target) in ILLEGAL and not has_confirmed_red(state, namespace, task_id):
        state.record_tdd_phase(task_id, namespace, f"refused:{target.value}", f"from {now.value}")
        # Logged **here and only here** (Copilot, PR #259). The call sites used
        # to log it too, so one event produced two lines at two severities —
        # and this is the only place that knows what the event means.
        #
        # An error, not a warning: after #253 this fires only when the record
        # and the gate disagree — the gate refuses to implement without a
        # confirmed red, so reaching here means one of them is wrong about the
        # same task. It stays non-fatal because bookkeeping must never fail a
        # task (`_record_phase`: the gates decide, this remembers), but it must
        # not read as routine either.
        logger.error(
            "Lifecycle transition refused",
            task_id=task_id,
            namespace=namespace,
            frm=now.value,
            to=target.value,
        )
        raise IllegalTransition(
            f"{task_id} cannot move from {now.value} to {target.value}: "
            "GREEN requires a confirmed red"
        )
    if target is TddPhase.REFACTORING and detail is None:
        # Never executed, and the record says so rather than leaving a reader
        # to wonder whether something ran.
        detail = "skipped"
    state.record_tdd_phase(task_id, namespace, target.value, detail)
    logger.info("TDD phase", task_id=task_id, phase=target.value, detail=detail)
    return target


__all__ = [
    "ILLEGAL",
    "IllegalTransition",
    "TddPhase",
    "advance",
    "current_phase",
    "has_confirmed_red",
    "is_terminal",
    "next_phase",
]
