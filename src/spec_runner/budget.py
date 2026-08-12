"""The pre-call budget guard (#213, second half).

**A guard, not a cap.** It answers one question, immediately before a paid
call: *is there anything left to spend?* It cannot stop the call that crosses
the line, because a call's cost is known only after it returns. The only true
hard cap is a backend-enforced per-call limit, and this project deliberately
does not pass one (`execution.py`: claude's native `--max-budget-usd` turned a
slight overage into a hard failure and broke `doctor`).

What it guarantees, and the wording matters:

    Once recorded spend has reached the limit, no new paid call is started;
    the maximum consecutive overshoot is bounded by one call.

Before this, the bound was checked **between attempts**, and a TDD attempt makes
three paid calls (RED authoring → GREEN implementation → review). The third
pilot run spent at least $2.53 against a $1.82 cap and the cap then correctly
refused a *second* attempt — a real limit doing half its job, after the money
was gone.

Two consequences follow from the guarantee rather than from taste:

- **Parallel review is serialised while a budget is active.** Five roles
  launched together are five calls in flight past one check, so "at most one
  call" would simply be false. `review` runs the roles one at a time when a cap
  is set (see `review.run_parallel_review`).
- **An unpriced call blocks what follows.** A call whose cost the CLI never
  reported (timeout, account limit, or a CLI that reports no cost at all) makes
  the remainder unprovable, so the guard fails closed rather than spending
  against a number it knows to be a floor. A project whose CLI never reports
  cost therefore cannot combine that CLI with a budget — which is the honest
  answer, not a limitation to work around. A guard that cannot read spend *at
  all* is the extreme of the same case and refuses for the same reason: "we do
  not know" is not a reason to spend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ExecutorConfig
    from .state import ExecutorState

logger = get_logger("budget")

#: Refusal kinds. Distinguished because they send an operator to different
#: places: raise a cap, raise the other cap, or find out what a call cost.
TASK_BUDGET = "task_budget"
RUN_BUDGET = "run_budget"
UNPRICED = "unpriced"
#: The guard could not read recorded spend at all. The extreme case of an
#: unprovable remainder, and refused for the same reason (Copilot, PR #217).
UNREADABLE = "unreadable"


@dataclass(frozen=True)
class BudgetRefusal:
    """Why the next paid call must not start."""

    kind: str
    reason: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.reason


class BudgetRefused(Exception):
    """Raised where a paid call was about to be made and must not be.

    An exception rather than a return value at the RED site: `run_red_phase`
    returns a *verdict about a red*, and every one of those values means the
    instrument looked at something. "We did not look, because there is no money
    left" is not a verdict, and an `unverifiable` returned here would be read by
    the gate as an instrument error and retried — spending again to discover the
    same emptiness.
    """

    def __init__(self, refusal: BudgetRefusal) -> None:
        super().__init__(refusal.reason)
        self.refusal = refusal


def budget_is_active(config: ExecutorConfig) -> bool:
    """Whether any cap is configured.

    With no cap there is nothing to guard, and the guard must then cost
    nothing: no state reads, no serialised review, no behaviour change at all
    for the runs that never set a budget.
    """
    return config.task_budget_usd is not None or config.budget_usd is not None


def check_before_call(
    config: ExecutorConfig,
    state: ExecutorState,
    task_id: str,
    provenance: str,
) -> BudgetRefusal | None:
    """Refuse the next paid call, or None to proceed.

    ``provenance`` names the call about to be made (`red_authoring`, `green`,
    `review`, `review:<role>`) and appears in the refusal, because "which call
    did not happen" is the first thing an operator needs in order to resume.
    """
    if not budget_is_active(config):
        return None

    task_spent = state.task_cost(task_id)
    if config.task_budget_usd is not None and task_spent >= config.task_budget_usd:
        return BudgetRefusal(
            TASK_BUDGET,
            f"Task budget reached before the {provenance} call "
            f"(${task_spent:.2f} >= ${config.task_budget_usd:.2f}) — not starting it",
        )

    if config.budget_usd is not None:
        run_spent = state.total_cost()
        if run_spent >= config.budget_usd:
            return BudgetRefusal(
                RUN_BUDGET,
                f"Run budget reached before the {provenance} call "
                f"(${run_spent:.2f} >= ${config.budget_usd:.2f}) — not starting it",
            )

    # Fail closed on an unprovable remainder. Checked *after* the caps so an
    # operator who is simply out of money is told that, rather than being sent
    # to look for a missing price.
    unpriced = _unpriced_in_scope(config, state, task_id)
    if unpriced:
        # The floor quoted is the one the *binding* cap is measured against:
        # under a run-wide budget the task's own total says nothing about how
        # much of the run remains (Copilot, PR #217).
        if config.budget_usd is not None:
            floor = f"${state.total_cost():.2f} for this run"
        else:
            floor = f"${task_spent:.2f} for this task"
        return BudgetRefusal(
            UNPRICED,
            f"{unpriced} earlier call(s) reported no cost, so the remaining budget cannot be "
            f"proven — not starting the {provenance} call. Recorded spend is a floor "
            f"({floor}); see `spec-runner costs`",
        )
    return None


def _unpriced_in_scope(config: ExecutorConfig, state: ExecutorState, task_id: str) -> int:
    """Unpriced calls that make *this* decision unprovable.

    A run-wide cap is unprovable if anything anywhere went unpriced; a per-task
    cap only cares about this task. Asking the narrower question where the
    narrower cap applies keeps one task's missing price from freezing every
    other task in the run.
    """
    if config.budget_usd is not None:
        return state.unmeasured_calls()
    return state.unmeasured_calls(task_id)


__all__ = [
    "RUN_BUDGET",
    "TASK_BUDGET",
    "UNPRICED",
    "UNREADABLE",
    "BudgetRefusal",
    "BudgetRefused",
    "budget_is_active",
    "check_before_call",
]
