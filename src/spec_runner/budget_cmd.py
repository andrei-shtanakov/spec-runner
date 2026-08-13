"""`spec-runner budget authorize` — an operator raising a ceiling (#230 part 2).

Refunds and a separate infrastructure budget were rejected at design time: both
stop the sentence *"the number bounds the money"* from being true. A refund
turns the cap into a progress bound, and a deterministic instrument failure
then loops with every attempt refunded; a second budget means
`task_budget_usd` no longer bounds the spend, because there are two numbers.

What is left is the honest one: a human raises a specific ceiling, deliberately,
with a reason, and the record says who and when. The #213 guarantee is untouched
— this decides *which number* the limit is, never whether it binds.

Design: ``docs/superpowers/specs/2026-08-13-budget-authorization-design.md``
(signed off 2026-08-13).
"""

from __future__ import annotations

import argparse

from .config import ExecutorConfig
from .logging import get_logger
from .remedy import RemedyError, resolve_actor
from .state import ExecutorState

logger = get_logger("budget_cmd")

TASK_SCOPE = "task"
RUN_SCOPE = "run"


class AuthorizationError(RuntimeError):
    """The authorization was refused, always with something to act on."""


def authorize(
    config: ExecutorConfig,
    state: ExecutorState,
    *,
    reason: str,
    task_id: str | None = None,
    task_budget_usd: float | None = None,
    run_budget_usd: float | None = None,
    actor: str | None = None,
    after: int | None = None,
) -> list[dict]:
    """Raise one or both ceilings. Returns the rows written.

    Both axes are accepted because neither implies the other: raising only the
    task ceiling leaves `budget_usd` refusing the very next call, and raising
    only the run ceiling leaves the task's own cap in place.
    """
    from .budget import effective_limits
    from .remedy import _refuse_if_running
    from .tdd import resolve_namespace

    _refuse_if_agent_or_error()
    if not reason or not reason.strip():
        raise AuthorizationError(
            "an authorization needs a reason; an unexplained ceiling raise is unreviewable "
            "six weeks later, which is when it will be read"
        )
    if task_budget_usd is None and run_budget_usd is None:
        raise AuthorizationError("nothing to authorize: name --task-limit, --run-limit, or both")
    if task_budget_usd is not None and not task_id:
        raise AuthorizationError("--task-limit needs the task it applies to")
    try:
        _refuse_if_running(config)
    except RemedyError as exc:
        # The guard reads limits mid-call; changing them under a running loop
        # makes "what was authorised when this call started" unanswerable.
        raise AuthorizationError(str(exc)) from exc

    resolved_actor = resolve_actor(config, actor)
    namespace = resolve_namespace(config)
    written: list[dict] = []

    if task_budget_usd is not None:
        assert task_id is not None
        current = state.latest_budget_authorization(
            TASK_SCOPE, task_id=task_id, namespace=namespace
        )
        _check_cas(current, after, TASK_SCOPE)
        previous, _run = effective_limits(config, state, task_id)
        _check_monotonic(previous, task_budget_usd, TASK_SCOPE)
        written.append(
            _write(
                state,
                scope=TASK_SCOPE,
                new_limit=task_budget_usd,
                previous=previous,
                spend=state.task_cost(task_id),
                unmeasured=state.unmeasured_calls(task_id),
                actor=resolved_actor,
                reason=reason.strip(),
                task_id=task_id,
                namespace=namespace,
            )
        )

    if run_budget_usd is not None:
        current = state.latest_budget_authorization(RUN_SCOPE)
        _check_cas(current, after, RUN_SCOPE)
        _task, previous = effective_limits(config, state, None)
        _check_monotonic(previous, run_budget_usd, RUN_SCOPE)
        written.append(
            _write(
                state,
                scope=RUN_SCOPE,
                new_limit=run_budget_usd,
                previous=previous,
                spend=state.total_cost(),
                unmeasured=state.unmeasured_calls(),
                actor=resolved_actor,
                reason=reason.strip(),
                # NULL for both, enforced by a CHECK: `budget_usd` bounds the
                # whole domain, and a namespaced run ceiling would give each
                # workstream its own "global" cap.
                task_id=None,
                namespace=None,
            )
        )
    return written


def _refuse_if_agent_or_error() -> None:
    from .remedy import _refuse_if_agent

    try:
        _refuse_if_agent()
    except RemedyError as exc:
        raise AuthorizationError(
            "raising a budget is an operator decision and cannot be taken from inside "
            f"an agent run ({exc})"
        ) from exc


def _check_cas(current: dict | None, after: int | None, scope: str) -> None:
    """An authorization is made against a state the operator has seen."""
    if current is None:
        return
    if after is None:
        raise AuthorizationError(
            f"the {scope} ceiling is already authorization #{current['id']} "
            f"(${float(current['new_limit_usd']):.2f}, {current['actor']}, "
            f"{current['timestamp']}); pass --after {current['id']} to raise it from there"
        )
    if int(after) != int(current["id"]):
        raise AuthorizationError(
            f"--after {after} is stale: the standing {scope} authorization is "
            f"#{current['id']} (${float(current['new_limit_usd']):.2f}, {current['actor']}, "
            f"{current['timestamp']}). Read it, then decide again"
        )


def _check_monotonic(previous: float | None, new_limit: float, scope: str) -> None:
    """Only upwards. Lowering is not supported — by this command or any flag on
    it — because lowering a ceiling someone authorised is either a mistake or a
    new decision, and the answer to both is a new budget domain."""
    if new_limit <= 0:
        raise AuthorizationError(f"a {scope} ceiling of ${new_limit:.2f} is not a ceiling")
    if previous is not None and new_limit <= previous:
        raise AuthorizationError(
            f"the {scope} ceiling is already ${previous:.2f}; this command only raises "
            f"(asked for ${new_limit:.2f})"
        )


def _write(
    state: ExecutorState,
    *,
    scope: str,
    new_limit: float,
    previous: float | None,
    spend: float,
    unmeasured: int,
    actor: str,
    reason: str,
    task_id: str | None,
    namespace: str | None,
) -> dict:
    auth_id = state.record_budget_authorization(
        scope=scope,
        new_limit_usd=new_limit,
        recorded_spend_usd=spend,
        unmeasured_calls=unmeasured,
        actor=actor,
        reason=reason,
        task_id=task_id,
        namespace=namespace,
        previous_limit_usd=previous,
    )
    logger.info(
        "Budget authorized",
        id=auth_id,
        scope=scope,
        task_id=task_id,
        previous=previous,
        new_limit=new_limit,
        recorded_spend=round(spend, 4),
        unmeasured_calls=unmeasured,
        actor=actor,
    )
    return {
        "id": auth_id,
        "scope": scope,
        "task_id": task_id,
        "previous_limit_usd": previous,
        "new_limit_usd": new_limit,
        "recorded_spend_usd": spend,
        "unmeasured_calls": unmeasured,
        "actor": actor,
    }


def cmd_budget(args: argparse.Namespace, config: ExecutorConfig) -> int:
    """`spec-runner budget authorize` — print the decision, or the refusal."""
    if getattr(args, "budget_command", None) != "authorize":
        print("Usage: spec-runner budget authorize [TASK-ID] --reason ... [--task-limit N]")
        return 1
    try:
        with ExecutorState(config) as state:
            rows = authorize(
                config,
                state,
                reason=args.reason,
                task_id=getattr(args, "task_id", None),
                task_budget_usd=getattr(args, "task_limit", None),
                run_budget_usd=getattr(args, "run_limit", None),
                actor=getattr(args, "actor", None),
                after=getattr(args, "after", None),
            )
    except AuthorizationError as exc:
        print(f"⛔ {exc}")
        return 1

    for row in rows:
        was = "—" if row["previous_limit_usd"] is None else f"${row['previous_limit_usd']:.2f}"
        floor = "≥" if row["unmeasured_calls"] else ""
        target = row["task_id"] or "this state file"
        print(
            f"✅ authorization #{row['id']}: {row['scope']} ceiling for {target} "
            f"{was} → ${row['new_limit_usd']:.2f}"
        )
        print(
            f"   recorded spend at the decision: {floor}${row['recorded_spend_usd']:.2f}"
            + (
                f" ({row['unmeasured_calls']} unpriced call(s) — this is a floor)"
                if row["unmeasured_calls"]
                else ""
            )
        )
        print(f"   actor: {row['actor']}")
    return 0


__all__ = ["AuthorizationError", "authorize", "cmd_budget"]
