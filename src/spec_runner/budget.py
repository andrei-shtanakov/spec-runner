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
from pathlib import Path
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


def domain_label(config: ExecutorConfig) -> str:
    """The budget domain, spelled the way an operator can act on it (#330).

    The domain **is** the state DB — `state.budget_domain_id` mints one id per
    file, so a new file inherits no authorization and no spend — and
    `--spec-prefix` selects a different file. Two commands run with different
    flags therefore speak about different ceilings, each of them truthfully:
    the live incident was `authorize` answering "already $35.95" from the
    default DB while a prefixed run refused at $1.82, with nothing in either
    sentence to say they were about different files.

    The path rather than the minted id: the id is what rows join on, the path
    is what an operator can pass a flag about.
    """
    return _relative(Path(config.state_file), config)


def sibling_domains(config: ExecutorConfig) -> list[str]:
    """Other state DBs sitting beside this one, as labels.

    Anchored at this domain's own directory, which for the caller that matters
    — an authorization typed without a prefix — is the spec dir itself.

    Found by *what produced the path* rather than by a flat pattern. A prefix
    is interpolated straight into the state path, so one carrying a separator
    (`--spec-prefix foo/`) produces `spec/.executor-foo/state.db`: a working
    domain, measured, that a `.executor-*state.db` glob does not see (codex
    review, PR #333). A `--change` domain lives under `changes/` and is
    excluded by the same test — naming a change is already an explicit choice
    of domain, and listing those would be noise.
    """
    path = Path(config.state_file)
    try:
        active = path.resolve()
        found = [
            p
            for p in path.parent.rglob("*state.db")
            if p.resolve() != active and _is_executor_domain(p, path.parent)
        ]
    except OSError:  # pragma: no cover - unreadable spec dir
        return []
    return sorted(_relative(p, config) for p in found)


def _is_executor_domain(candidate: Path, root: Path) -> bool:
    """Whether ``candidate`` is a state DB the `.executor-` naming produced."""
    parts = candidate.relative_to(root).parts
    return bool(parts) and parts[0].startswith(".executor-")


def _relative(path: Path, config: ExecutorConfig) -> str:
    """A path an operator recognises: project-relative where that is possible."""
    try:
        return str(path.resolve().relative_to(Path(config.project_root).resolve()))
    except (ValueError, OSError):
        return str(path)


def budget_is_active(config: ExecutorConfig) -> bool:
    """Whether any cap is configured.

    With no cap there is nothing to guard, and the guard must then cost
    nothing: no state reads, no serialised review, no behaviour change at all
    for the runs that never set a budget.
    """
    return config.task_budget_usd is not None or config.budget_usd is not None


#: The provenance prefix of the calls a `review` reserve is *for*. Review runs
#: as `review` and, per role, `review:<role>` — one prefix covers both, and the
#: reserve must cover both or a five-role review would spend past it.
REVIEW_PROVENANCE = "review"


def is_reserved_for(stage: str, provenance: str) -> bool:
    """Whether ``provenance`` is a call the reserve on ``stage`` is meant for.

    Prefix-matched on purpose (#267): `review` and `review:security` are the
    same stage's calls, and a reserve that covered only the bare name would be
    spent by the first role.
    """
    return provenance == stage or provenance.startswith(f"{stage}:")


def _reserve_for(row: dict | None, provenance: str) -> float:
    """How much of an authorization's ceiling is withheld from this call.

    Zero for the stage the reserve is for — that is what it was set aside for —
    and zero when there is no reserve at all, which is every authorization
    written before #267.
    """
    if not row or not row.get("reserve_usd") or not row.get("reserve_stage"):
        return 0.0
    if is_reserved_for(str(row["reserve_stage"]), provenance):
        return 0.0
    return float(row["reserve_usd"])


def effective_limits(
    config: ExecutorConfig,
    state: ExecutorState,
    task_id: str | None = None,
    provenance: str | None = None,
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

    "No namespace" is a statement *within* a domain, and the domain is the
    state file (`state.budget_domain_id`). `--spec-prefix` selects a different
    file, so it does partition the run ceiling — one level above the namespace,
    by choosing which DB the question is asked of. That is the intended
    semantics and not an accident of pathing: a `--budget` bounds a state
    file's lifetime spend, and phases run under separate prefixes precisely to
    account separately. What was wrong was this sentence, which read as though
    one ceiling spanned every prefix (#330). Every message that quotes a
    ceiling now names the domain it came from.

    ``provenance`` names the call the limits are being resolved *for*, and only
    a reserve makes it matter (#267). Without it the answer is the ceiling as
    authorised — which is what every reader that asks a question about the run
    as a whole (the preflight, `costs`) wants, and what `None` means here.
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
            if provenance is not None:
                task_limit -= _reserve_for(row, provenance)
    run_row = state.latest_budget_authorization("run")
    if run_row is not None:
        run_limit = float(run_row["new_limit_usd"])
        if provenance is not None:
            run_limit -= _reserve_for(run_row, provenance)
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

    task_limit, run_limit = effective_limits(config, state, task_id, provenance)
    task_spent = state.task_cost(task_id) + pending_cost
    if task_limit is not None and task_spent >= task_limit:
        return BudgetRefusal(
            TASK_BUDGET,
            f"Task budget reached before the {provenance} call "
            f"(${task_spent:.2f} >= ${task_limit:.2f}) — not starting it"
            + authorization_note(state, "task", task_id, config, provenance),
        )

    if run_limit is not None:
        run_spent = state.total_cost() + pending_cost
        if run_spent >= run_limit:
            return BudgetRefusal(
                RUN_BUDGET,
                f"Run budget reached before the {provenance} call "
                f"(${run_spent:.2f} >= ${run_limit:.2f}) — not starting it"
                + authorization_note(state, "run", None, config, provenance),
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


def authorization_note(
    config_scope_state: ExecutorState,
    scope: str,
    task_id: str | None,
    config: ExecutorConfig,
    provenance: str = "",
) -> str:
    """What an operator needs in order to raise this ceiling (#230 §7.3).

    Public because the *report* of an overshoot needs the same sentence as the
    *refusal* of a call (#255): which authorization is in force, who set it,
    when, and what part of it is reserved. Two compositions of that sentence
    would be two places to drift.

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
    # Every ceiling sentence names its domain (#330): an operator reading a
    # refusal must not have to know that `--spec-prefix` moved the file the
    # number lives in.
    domain = domain_label(config)
    if row is None:
        return (
            f". The limit is the configured one for {domain}; `spec-runner budget authorize` "
            "can raise it"
        )
    note = (
        f". The limit is authorization #{row['id']} in {domain} "
        f"(${float(row['new_limit_usd']):.2f}, {row['actor']}, {row['timestamp']})"
    )
    withheld = _reserve_for(row, provenance)
    if withheld:
        # Without this the arithmetic looks wrong: the operator sees a ceiling
        # they raised and a refusal below it, and nothing says why (#267).
        note += (
            f", of which ${withheld:.2f} is reserved for {row['reserve_stage']} and is not "
            "available to this call"
        )
    return note + f"; raise it with `spec-runner budget authorize --after {row['id']}`"


__all__ = [
    "RUN_BUDGET",
    "TASK_BUDGET",
    "UNPRICED",
    "UNREADABLE",
    "BudgetRefusal",
    "BudgetRefused",
    "budget_is_active",
    "check_before_call",
    "domain_label",
    "authorization_note",
    "effective_limits",
    "sibling_domains",
]
