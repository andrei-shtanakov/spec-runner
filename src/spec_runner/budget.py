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


def effective_limits(
    config: ExecutorConfig, state: ExecutorState, task_id: str | None = None
) -> tuple[float | None, float | None]:
    """`(task_limit, run_limit)` after operator authorizations (#230 part 2).

    The newest authorization for a scope **wins over the config value** — not
    `max()` of the two. An operator who edits the YAML after authorising
    deserves an answer that does not depend on which number happens to be
    larger. What keeps that honest is that an authorised limit is always
    *displayed as one*, with its id, actor and timestamp, so a config file that
    disagrees with the live ceiling can never be read as the truth.

    Scoping is the sign-off's: a task ceiling is `(domain, namespace, task)`, a
    run ceiling belongs to the whole budget domain and carries no namespace —
    `budget_usd` bounds the DB, and a per-namespace "global" cap is not global.
    """
    task_limit = config.task_budget_usd
    run_limit = config.budget_usd
    if task_id:
        from .tdd import resolve_namespace

        row = state.latest_budget_authorization(
            "task", task_id=task_id, namespace=resolve_namespace(config)
        )
        if row is not None:
            task_limit = float(row["new_limit_usd"])
    run_row = state.latest_budget_authorization("run")
    if run_row is not None:
        run_limit = float(run_row["new_limit_usd"])
    return task_limit, run_limit


def check_before_call(
    config: ExecutorConfig,
    state: ExecutorState,
    task_id: str,
    provenance: str,
    pending_cost: float | None = 0.0,
) -> BudgetRefusal | None:
    """Refuse the next paid call, or None to proceed.

    ``provenance`` names the call about to be made (`red_authoring`, `green`,
    `review`, `review:<role>`) and appears in the refusal, because "which call
    did not happen" is the first thing an operator needs in order to resume.

    ``pending_cost`` is spend that has **happened but is not yet recorded** —
    the implementation call's cost, which `record_attempt` writes only after
    `post_done_hook` returns. Reading the database alone would let review start
    on a budget this very attempt has already spent, and the free rehearsal
    demonstrated exactly that: $0.60 recorded against a $1.00 cap, review
    allowed, $1.80 spent. `None` means the amount is unknown, which is an
    unprovable remainder, not a free call.
    """
    if not budget_is_active(config):
        return None

    if pending_cost is None:
        return BudgetRefusal(
            UNPRICED,
            "the call just made reported no cost, so the remaining budget cannot be proven "
            f"— not starting the {provenance} call",
        )

    task_limit, run_limit = effective_limits(config, state, task_id)
    task_spent = state.task_cost(task_id) + pending_cost
    if task_limit is not None and task_spent >= task_limit:
        return BudgetRefusal(
            TASK_BUDGET,
            f"Task budget reached before the {provenance} call "
            f"(${task_spent:.2f} >= ${task_limit:.2f}) — not starting it"
            + _authorization_note(state, "task", task_id, config),
        )

    if run_limit is not None:
        run_spent = state.total_cost() + pending_cost
        if run_spent >= run_limit:
            return BudgetRefusal(
                RUN_BUDGET,
                f"Run budget reached before the {provenance} call "
                f"(${run_spent:.2f} >= ${run_limit:.2f}) — not starting it"
                + _authorization_note(state, "run", None, config),
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
            floor = f"${state.total_cost() + pending_cost:.2f} for this run"
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


def _authorization_note(
    config_scope_state: ExecutorState, scope: str, task_id: str | None, config: ExecutorConfig
) -> str:
    """What an operator needs in order to raise this ceiling (#230 §7.3).

    A refusal that names only the number sends someone to `git log` to find the
    authorization id before they can pass `--after`, and an operator who cannot
    find the id skips the CAS. So the refusal carries the id, the effective
    limit, who set it and when — or says plainly that the limit is the config's.
    """
    row = None
    if scope == "task" and task_id:
        from .tdd import resolve_namespace

        row = config_scope_state.latest_budget_authorization(
            "task", task_id=task_id, namespace=resolve_namespace(config)
        )
    elif scope == "run":
        row = config_scope_state.latest_budget_authorization("run")
    if row is None:
        return ". The limit is the configured one; `spec-runner budget authorize` can raise it"
    return (
        f". The limit is authorization #{row['id']} (${float(row['new_limit_usd']):.2f}, "
        f"{row['actor']}, {row['timestamp']}); raise it with "
        f"`spec-runner budget authorize --after {row['id']}`"
    )


__all__ = [
    "RUN_BUDGET",
    "TASK_BUDGET",
    "UNPRICED",
    "UNREADABLE",
    "BudgetRefusal",
    "BudgetRefused",
    "budget_is_active",
    "check_before_call",
    "effective_limits",
]
