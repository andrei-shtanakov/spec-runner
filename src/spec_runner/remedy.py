"""Operator remedies for a frozen red — `tdd abandon` / `tdd repair` (#141 slice 3).

Without these, the only cure for a mistake in a byte-locked test is rewriting
history. The pilot did that twice in a single phase, each time with a state
freeze and a second signature. That is why slice 2 does not ship without this
one.

A remedy is an **authority decision**, not an observation, and the shape
follows from that:

- it carries an **actor** and a **reason** — an authority decision without an
  author is an anonymous one;
- it **deletes nothing**. Superseded and abandoned records stay, because a
  retired claim is still evidence of what was believed and when;
- `repair` does **not** mean "these bytes are fine". It opens a **new lineage**
  which must prove its own red. Accepting repaired bytes without re-verifying
  would make `repair` a way to launder an unconfirmed claim — the exact hole
  the whole contract closes.

Contract: ``docs/superpowers/specs/2026-08-11-claim-and-remedy-contracts.md`` §2
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from .claims import ClaimRefused, ClaimStatus, ensure_claimable, record_claims
from .logging import get_logger
from .tdd import RedCheckpoint, RedOutcome, resolve_namespace, verify_red

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ExecutorConfig
    from .state import ExecutorState

logger = get_logger("remedy")

#: Set by the runner in agent subprocesses. See `_refuse_if_agent`.
AGENT_MARKER = "SPEC_RUNNER_AGENT"


class RemedyError(RuntimeError):
    """A remedy was refused. Always with a reason a person can act on."""


class RemedyOperation(str, Enum):
    ABANDON = "abandon"
    REPAIR = "repair"


class CheckpointStatus(str, Enum):
    """A checkpoint's standing. Mirrors `ClaimStatus` deliberately: they are
    retired together and for the same reasons."""

    ACTIVE = "active"
    SUPERSEDED = "superseded"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class RemedyRecord:
    """One remedy, as stored."""

    namespace: str
    task_id: str
    checkpoint_id: str
    operation: RemedyOperation
    reason: str
    actor: str
    timestamp: str
    new_checkpoint_id: str | None = None


@dataclass(frozen=True)
class RemedyResult:
    operation: RemedyOperation
    checkpoint_id: str
    new_checkpoint_id: str | None = None
    outcome: RedOutcome | None = None
    already_applied: bool = False


def resolve_actor(config: ExecutorConfig, actor: str | None) -> str:
    """Who is taking responsibility. Explicit, else the git identity, else
    the OS user — and `unknown` rather than a guess if none of them answer."""
    if actor and actor.strip():
        return actor.strip()
    result = subprocess.run(
        ["git", "config", "user.email"],
        cwd=config.project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return os.environ.get("USER") or "unknown"


def abandon(
    config: ExecutorConfig,
    state: ExecutorState,
    task_id: str,
    checkpoint_id: str,
    *,
    reason: str,
    actor: str | None = None,
) -> RemedyResult:
    """This red was no good. Start again honestly; the commit stays in history."""
    namespace = _guard(config, reason)

    # Idempotency is checked *before* CAS, not after: applying the remedy
    # retires the very checkpoint CAS compares against, so a repeat would fail
    # the swap and report a stale id — which is true but useless, and would
    # make "run it twice" an error instead of a no-op.
    prior = _existing(state, namespace, task_id, checkpoint_id, RemedyOperation.ABANDON)
    if prior is not None:
        return RemedyResult(RemedyOperation.ABANDON, checkpoint_id, already_applied=True)

    active = _swap(state, namespace, task_id, checkpoint_id)
    state.set_checkpoint_status(namespace, active.checkpoint_id, CheckpointStatus.ABANDONED)
    state.supersede_claims(namespace, task_id, ClaimStatus.ABANDONED)
    _record(
        state, namespace, task_id, checkpoint_id, RemedyOperation.ABANDON, reason, actor, config
    )
    logger.info("Red abandoned", task_id=task_id, checkpoint=checkpoint_id)
    return RemedyResult(RemedyOperation.ABANDON, checkpoint_id)


def repair(
    config: ExecutorConfig,
    state: ExecutorState,
    task_id: str,
    checkpoint_id: str,
    commit: str,
    *,
    reason: str,
    actor: str | None = None,
) -> RemedyResult:
    """The edit to the locked file is legitimate. Open a new lineage from it.

    Not "accept these bytes": the new lineage is replayed immediately, and what
    it establishes is recorded honestly — including a repaired test that turns
    out to pass, which is not a red and must not be recorded as one.
    """
    namespace = _guard(config, reason)

    prior = _existing(state, namespace, task_id, checkpoint_id, RemedyOperation.REPAIR)
    if prior is not None:
        # Carry the lineage's outcome, not just its id. Without it a repeated
        # `repair` reports a bare success over a lineage that never
        # re-established a red — the first call said so and exited 2, and the
        # second must not quietly disagree.
        lineage = (
            state.checkpoint_by_id(namespace, prior.new_checkpoint_id)
            if prior.new_checkpoint_id
            else None
        )
        return RemedyResult(
            RemedyOperation.REPAIR,
            checkpoint_id,
            new_checkpoint_id=prior.new_checkpoint_id,
            outcome=lineage.outcome if lineage else None,
            already_applied=True,
        )

    active = _swap(state, namespace, task_id, checkpoint_id)
    if not _resolves(config, commit):
        raise RemedyError(f"{commit[:12]} does not resolve to a commit")

    verification = verify_red(
        config, sha=commit, selector=active.selector, baseline_sha=active.commit_sha
    )
    lineage = RedCheckpoint(
        task_id=task_id,
        namespace=namespace,
        commit_sha=commit,
        # The new lineage descends from the one it replaces — that is what
        # makes it a repair rather than an unrelated second claim.
        baseline_sha=active.commit_sha,
        selector=active.selector,
        environment_id=verification.environment_id,
        execution_mode=active.execution_mode,
        config_hash=active.config_hash,
        outcome=verification.outcome,
        timestamp=datetime.now().isoformat(),
    )

    if verification.outcome is RedOutcome.EXPECTED_FAIL:
        # Refuse before touching anything: a lineage recorded as a confirmed
        # red whose file cannot be locked is the same hole in a different
        # doorway, and by then the old lock would already be gone.
        try:
            ensure_claimable(config, lineage.selector)
        except ClaimRefused as exc:
            raise RemedyError(f"the repaired red cannot be locked: {exc}") from exc

    state.set_checkpoint_status(namespace, active.checkpoint_id, CheckpointStatus.SUPERSEDED)
    state.supersede_claims(namespace, task_id, ClaimStatus.SUPERSEDED)
    # Claims before the checkpoint, for the reason spelled out in
    # `run_red_phase`: a crash between the two must not leave a confirmed red
    # with no lock.
    if verification.outcome is RedOutcome.EXPECTED_FAIL:
        record_claims(config, state, lineage)
    state.record_red_checkpoint(lineage)
    _record(
        state,
        namespace,
        task_id,
        checkpoint_id,
        RemedyOperation.REPAIR,
        reason,
        actor,
        config,
        new_checkpoint_id=lineage.checkpoint_id,
    )
    logger.info(
        "Red repaired",
        task_id=task_id,
        superseded=checkpoint_id,
        lineage=lineage.checkpoint_id,
        outcome=verification.outcome.value,
    )
    return RemedyResult(
        RemedyOperation.REPAIR,
        checkpoint_id,
        new_checkpoint_id=lineage.checkpoint_id,
        outcome=verification.outcome,
    )


def _guard(config: ExecutorConfig, reason: str) -> str:
    """The refusals that do not depend on any checkpoint, cheapest first."""
    _refuse_if_agent()
    if not reason or not reason.strip():
        raise RemedyError("a remedy needs a reason; an unexplained one is unreviewable")
    _refuse_if_running(config)
    return resolve_namespace(config)


def _swap(state: ExecutorState, namespace: str, task_id: str, checkpoint_id: str) -> RedCheckpoint:
    """Compare-and-swap against the active checkpoint."""
    active = state.red_checkpoint(task_id, namespace)
    if active is None:
        raise RemedyError(f"{task_id} has no active checkpoint in this workstream")
    if active.checkpoint_id != checkpoint_id:
        # Compare-and-swap. Without it a remedy issued against what the
        # operator last saw silently applies to whatever arrived since.
        raise RemedyError(
            f"{checkpoint_id} is not the active checkpoint for {task_id} "
            f"(active: {active.checkpoint_id})"
        )
    return active


def _refuse_if_agent() -> None:
    """Refuse when running inside an agent subprocess.

    A **guardrail, not a boundary**: the agent runs arbitrary shell and can
    unset the marker. What actually holds is that a remedy carries an
    operator's name; this only stops the ordinary path.
    """
    if os.environ.get(AGENT_MARKER):
        raise RemedyError(
            "a remedy is an operator decision and cannot be taken from inside an agent run"
        )


def _refuse_if_running(config: ExecutorConfig) -> None:
    """Refuse while a live run holds the executor lock.

    The lock, not the state row: `ExecutorLock` checks the recorded PID, so a
    lock left by a crashed process does not count — and a `running` row left by
    the same crash must not lock an operator out of the tool recovery needs.
    """
    from .config import ExecutorLock

    # The same path `cli.py` uses for the run lock — a different one would
    # answer a different question.
    probe = ExecutorLock(config.state_file.with_suffix(".lock"))
    if not probe.acquire():
        raise RemedyError(
            "the task is running (the executor lock is held); stop the run before a remedy"
        )
    probe.release()


def _resolves(config: ExecutorConfig, commit: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"],
            cwd=config.project_root,
            capture_output=True,
            text=True,
        ).returncode
        == 0
    )


def _existing(
    state: ExecutorState,
    namespace: str,
    task_id: str,
    checkpoint_id: str,
    operation: RemedyOperation,
) -> RemedyRecord | None:
    """The same remedy, already applied. Repeating one must not fork a second
    lineage or write a second record."""
    for record in state.remedies(task_id, namespace):
        if record.checkpoint_id == checkpoint_id and record.operation is operation:
            return record
    return None


def _record(
    state: ExecutorState,
    namespace: str,
    task_id: str,
    checkpoint_id: str,
    operation: RemedyOperation,
    reason: str,
    actor: str | None,
    config: ExecutorConfig,
    new_checkpoint_id: str | None = None,
) -> None:
    state.record_remedy(
        RemedyRecord(
            namespace=namespace,
            task_id=task_id,
            checkpoint_id=checkpoint_id,
            operation=operation,
            reason=reason.strip(),
            actor=resolve_actor(config, actor),
            timestamp=datetime.now().isoformat(),
            new_checkpoint_id=new_checkpoint_id,
        )
    )


def cmd_tdd(args, config: ExecutorConfig) -> int:
    """`spec-runner tdd abandon|repair`. Returns a process exit code.

    A refusal is a message, not a traceback: the operator is being told the
    remedy did not apply and why, which is information they can act on.
    """
    from .state import ExecutorState

    try:
        with ExecutorState(config) as state:
            if args.tdd_command == "abandon":
                result = abandon(
                    config,
                    state,
                    args.task_id,
                    args.checkpoint,
                    reason=args.reason,
                    actor=getattr(args, "actor", None),
                )
            else:
                result = repair(
                    config,
                    state,
                    args.task_id,
                    args.checkpoint,
                    args.commit,
                    reason=args.reason,
                    actor=getattr(args, "actor", None),
                )
    except RemedyError as exc:
        print(f"⛔ {exc}")
        return 1

    if result.already_applied:
        print(f"✔️  Already applied — {result.operation.value} on {result.checkpoint_id}")
        # A repeat must reach the same verdict as the first call: an
        # already-applied repair whose lineage is not a confirmed red is still
        # not a success.
        if result.operation is RemedyOperation.REPAIR:
            return _repair_exit(result)
        return 0
    if result.operation is RemedyOperation.ABANDON:
        print(f"✔️  Abandoned {result.checkpoint_id}; {args.task_id} returns to RED authoring")
        return 0

    print(f"✔️  Repaired: new lineage {result.new_checkpoint_id} (was {result.checkpoint_id})")
    return _repair_exit(result)


def _repair_exit(result: RemedyResult) -> int:
    """0 only when the new lineage actually re-established a red.

    A repair is not a blessing, so the exit code must not imply one — and the
    same must hold on a repeat, or running the command twice would launder the
    verdict.
    """
    if result.outcome is RedOutcome.EXPECTED_FAIL:
        print("   Red re-confirmed on the repaired commit; the new bytes are claimed.")
        return 0
    outcome = result.outcome.value if result.outcome else "no verdict"
    print(f"   ⚠️  The repaired commit did not establish a red ({outcome}).")
    print("   The task has no confirmed red and will be gated until it does.")
    return 2


__all__ = [
    "AGENT_MARKER",
    "CheckpointStatus",
    "RemedyError",
    "RemedyOperation",
    "RemedyRecord",
    "RemedyResult",
    "abandon",
    "cmd_tdd",
    "repair",
    "resolve_actor",
]
