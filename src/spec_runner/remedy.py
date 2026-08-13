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

from .claims import ClaimRefused, ClaimStatus, ensure_claimable, record_claims, selector_of
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
    #: #232: the post-green half finally has one. `abandon` and `repair` both
    #: ask questions about a *red*; an operator meeting a crash after green had
    #: no remedy that fitted, reached for `repair`, and superseded the very
    #: evidence that would have let the task finish.
    RESUME = "resume"


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
    state.supersede_claims(
        namespace, task_id, ClaimStatus.ABANDONED, checkpoint_id=active.checkpoint_id
    )
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
    _refuse_repair_after_green(state, namespace, task_id)

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
            parsed = selector_of(config, lineage)
            if parsed is None:
                raise ClaimRefused(
                    f"selector {lineage.selector!r} cannot be parsed by this "
                    "project's runner adapter"
                )
            ensure_claimable(config, parsed)
        except ClaimRefused as exc:
            raise RemedyError(f"the repaired red cannot be locked: {exc}") from exc

    state.set_checkpoint_status(namespace, active.checkpoint_id, CheckpointStatus.SUPERSEDED)
    state.supersede_claims(
        namespace, task_id, ClaimStatus.SUPERSEDED, checkpoint_id=active.checkpoint_id
    )
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


@dataclass(frozen=True)
class ResumeConflict:
    """A claimed file whose bytes have moved since the red was recorded."""

    path: str
    claimed: str
    found: str | None


def resume(
    config: ExecutorConfig,
    state: ExecutorState,
    task_id: str,
    *,
    reason: str,
    actor: str | None = None,
    checkpoint_id: str | None = None,
) -> tuple[RemedyResult, list[ResumeConflict]]:
    """Green is established; make this task's confirmed red standing again.

    The post-green remedy (#232). It **introduces no new way to satisfy the RED
    gate**: the gate is untouched and still demands a confirmed `expected_fail`
    whose commit is an ancestor of the tree in hand. All this changes is *which
    row is standing*, and only when such a row already exists.

    Three conditions, and failing any of them means the task cannot be resumed
    past a green it never had:

    1. a checkpoint with outcome `expected_fail` exists for this task here —
       **any status**, since supersession retires a lineage rather than
       unhappening the observation it recorded;
    2. its commit is an **ancestor of HEAD** — the same descent test the gate
       applies, so a resume can never authorise a tree the gate would refuse
       for a different reason;
    3. the lifecycle has reached `green_implementing` or later.

    The checkpoint and **its own lineage's claims** are reinstated in one
    transaction. Reinstating the red alone would make this chain legal —
    `confirmed red + claim → GREEN edits the frozen test → repair supersedes
    both → resume returns only the red → merge` — which launders exactly the
    violation the byte-lock exists to catch, using the command built to help.
    A claim protects the evidence from the RED until the terminal gate.

    Returns the result and any **conflicts**: claimed paths whose bytes in HEAD
    differ from what was locked. The decision is still recorded — an operator
    may legitimately want the record before restoring the bytes — but the gate
    will refuse until they match, and the caller is expected to say so loudly.
    Nothing here ever accepts new bytes.
    """
    from .lifecycle import TddPhase, current_phase

    namespace = _guard(config, reason)

    candidates = state.confirmed_reds(namespace, task_id)
    if not candidates:
        raise RemedyError(
            f"{task_id} has no confirmed red in this workstream — there is no green to resume "
            "past, and `resume` cannot invent the evidence a red is"
        )
    if checkpoint_id:
        matches = [cp for cp in candidates if cp.checkpoint_id == checkpoint_id]
        if not matches:
            listed = ", ".join(cp.checkpoint_id for cp in candidates)
            raise RemedyError(
                f"{checkpoint_id} is not a confirmed red of {task_id} in this workstream "
                f"(have: {listed})"
            )
        evidence = matches[0]
    elif len(candidates) > 1:
        # The same rule the other remedies follow (F-5): "probably that one" is
        # not a thing to guess about an authority decision, and reinstating the
        # wrong lineage reinstates the wrong byte-lock with it.
        listed = ", ".join(cp.checkpoint_id for cp in candidates)
        raise RemedyError(
            f"{task_id} has {len(candidates)} confirmed reds ({listed}); name one with --checkpoint"
        )
    else:
        evidence = candidates[0]
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=config.project_root,
        capture_output=True,
        text=True,
    )
    if head.returncode != 0:
        raise RemedyError("cannot resolve HEAD; resume needs a tree to check the red against")
    candidate = head.stdout.strip()
    if not _is_ancestor(config, evidence.commit_sha, candidate):
        raise RemedyError(
            f"the confirmed red {evidence.checkpoint_id} ({evidence.commit_sha[:12]}) is not an "
            f"ancestor of HEAD ({candidate[:12]}) — this tree was not built on that red"
        )
    phase = current_phase(state, namespace, task_id)
    if phase in (TddPhase.READY, TddPhase.RED_AUTHORING, TddPhase.RED_VERIFYING):
        raise RemedyError(
            f"{task_id} is at {phase.value}: there is no established green to resume from. "
            "`resume` reinstates evidence for work that reached GREEN, not for work that has "
            "not started"
        )

    prior = _existing(state, namespace, task_id, evidence.checkpoint_id, RemedyOperation.RESUME)
    conflicts = _claim_conflicts(config, state, namespace, evidence.checkpoint_id, candidate)
    if prior is not None:
        return (
            RemedyResult(RemedyOperation.RESUME, evidence.checkpoint_id, already_applied=True),
            conflicts,
        )

    checkpoints, claims = state.reinstate_checkpoint_with_claims(
        namespace, task_id, evidence.checkpoint_id
    )
    _record(
        state,
        namespace,
        task_id,
        evidence.checkpoint_id,
        RemedyOperation.RESUME,
        reason,
        actor,
        config,
    )
    logger.info(
        "Red reinstated",
        task_id=task_id,
        checkpoint=evidence.checkpoint_id,
        checkpoints=checkpoints,
        claims=claims,
        conflicts=len(conflicts),
    )
    return RemedyResult(RemedyOperation.RESUME, evidence.checkpoint_id), conflicts


def _claim_conflicts(
    config: ExecutorConfig,
    state: ExecutorState,
    namespace: str,
    checkpoint_id: str,
    candidate: str,
) -> list[ResumeConflict]:
    """Claimed paths whose bytes in the candidate differ from the lock.

    Surfaced by the command's own preflight rather than discovered at the gate:
    an operator who learns at merge time that their resume cannot merge has
    been told too late to do anything cheap about it.
    """
    out: list[ResumeConflict] = []
    for claim in state.claims_of_checkpoint(namespace, checkpoint_id):
        found = subprocess.run(
            ["git", "rev-parse", f"{candidate}:{claim['path']}"],
            cwd=config.project_root,
            capture_output=True,
            text=True,
        )
        current = found.stdout.strip() if found.returncode == 0 else None
        if current != claim["blob_sha"]:
            out.append(ResumeConflict(claim["path"], claim["blob_sha"], current))
    return out


def _is_ancestor(config: ExecutorConfig, ancestor: str, descendant: str) -> bool:
    return (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant],
            cwd=config.project_root,
            capture_output=True,
        ).returncode
        == 0
    )


def _refuse_repair_after_green(state: ExecutorState, namespace: str, task_id: str) -> None:
    """Repair asks "is this changed test still a red?" — a question with no
    honest answer once the implementation exists (#232 §5).

    Answering it costs a replay and, worse, retires the evidence: that is how
    the pilot ended with a `not_red` lineage, a superseded confirmed red, and a
    task that could not finish. A behaviour change to a shipped command, made
    deliberately — what worked yesterday produced a wedge.
    """
    from .lifecycle import TddPhase, current_phase

    phase = current_phase(state, namespace, task_id)
    if phase in (TddPhase.GREEN_VERIFYING, TddPhase.REFACTORING, TddPhase.DONE) or (
        phase is TddPhase.GREEN_IMPLEMENTING
    ):
        raise RemedyError(
            f"{task_id} is at {phase.value}: a red cannot be repaired once the implementation "
            "exists — the replay would pass, and recording that supersedes the confirmed red "
            "this task still needs. Use `spec-runner tdd resume` to reinstate it"
        )


def _guard(config: ExecutorConfig, reason: str) -> str:
    """The refusals that do not depend on any checkpoint, cheapest first."""
    _refuse_if_agent()
    if not reason or not reason.strip():
        raise RemedyError("a remedy needs a reason; an unexplained one is unreviewable")
    _refuse_if_running(config)
    return resolve_namespace(config)


def resolve_checkpoint(
    state: ExecutorState, namespace: str, task_id: str, given: str | None
) -> tuple[str, str | None]:
    """The checkpoint a remedy applies to, and a line to print about it.

    An explicit id always wins. With none given the id is inferred **only when
    exactly one lineage is active**, and the chosen id is printed — an operator
    must be able to see what their command was aimed at. With several, this
    fails closed rather than picking the newest: "probably that one" is not a
    thing to guess about an authority decision (F-5).
    """
    if given:
        return given, None
    active = state.active_checkpoints(namespace, task_id)
    if not active:
        raise RemedyError(f"{task_id} has no active checkpoint in this workstream")
    if len(active) > 1:
        listed = ", ".join(cp.checkpoint_id for cp in active)
        raise RemedyError(
            f"{task_id} has {len(active)} active checkpoints ({listed}); name one with --checkpoint"
        )
    chosen = active[0].checkpoint_id
    return chosen, f"Using the only active checkpoint for {task_id}: {chosen}"


def _swap(state: ExecutorState, namespace: str, task_id: str, checkpoint_id: str) -> RedCheckpoint:
    """Compare-and-swap against the **set** of active checkpoints.

    Not only against the newest. A task can have more than one active lineage
    today, `tdd checkpoints` lists them all, and the ambiguity error tells the
    operator to "name one with --checkpoint" — so refusing every id but the
    newest would point them at an action the code rejects, leaving no way out
    but editing SQLite (Copilot, PR #185).

    CAS still holds where it matters: a retired or unknown id is refused,
    because it is no longer the thing the operator thinks it is.
    """
    active = state.active_checkpoints(namespace, task_id)
    if not active:
        raise RemedyError(f"{task_id} has no active checkpoint in this workstream")
    for candidate in active:
        if candidate.checkpoint_id == checkpoint_id:
            return candidate
    listed = ", ".join(cp.checkpoint_id for cp in active)
    raise RemedyError(
        f"{checkpoint_id} is not an active checkpoint for {task_id} (active: {listed})"
    )


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

    if args.tdd_command == "resume":
        return _cmd_resume(args, config)

    try:
        with ExecutorState(config) as state:
            checkpoint_id, note = resolve_checkpoint(
                state,
                resolve_namespace(config),
                args.task_id,
                getattr(args, "checkpoint", None),
            )
            if note:
                print(f"ℹ️  {note}")
            if args.tdd_command == "abandon":
                result = abandon(
                    config,
                    state,
                    args.task_id,
                    checkpoint_id,
                    reason=args.reason,
                    actor=getattr(args, "actor", None),
                )
            else:
                result = repair(
                    config,
                    state,
                    args.task_id,
                    checkpoint_id,
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


def _cmd_resume(args, config: ExecutorConfig) -> int:
    """`spec-runner tdd resume`. 0 when the task can proceed, 2 when the
    decision is recorded but the claimed bytes no longer match.

    The non-zero exit on a conflict is the point: the record is allowed — an
    operator may want it before restoring anything — but the gate *will* refuse
    until the bytes match, and a command that returned 0 would be promising a
    merge that cannot happen.
    """
    from .state import ExecutorState

    try:
        with ExecutorState(config) as state:
            result, conflicts = resume(
                config,
                state,
                args.task_id,
                reason=args.reason,
                actor=getattr(args, "actor", None),
                checkpoint_id=getattr(args, "checkpoint", None),
            )
    except RemedyError as exc:
        print(f"⛔ {exc}")
        return 1

    if result.already_applied:
        print(f"✔️  Already applied — resume on {result.checkpoint_id}")
    else:
        print(f"✔️  Reinstated {result.checkpoint_id} and its claims; {args.task_id} can proceed")
    if not conflicts:
        return 0
    print("   ⚠️  Claimed bytes have moved since that red was recorded:")
    for c in conflicts:
        found = c.found[:12] if c.found else "(absent from HEAD)"
        print(f"      {c.path}: claimed {c.claimed[:12]}, HEAD has {found}")
    print("   The gate will refuse until they match. Restore the evidential bytes, or")
    print("   record a deliberate change of evidence — this command never accepts new ones.")
    return 2


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
    "ResumeConflict",
    "RemedyError",
    "RemedyOperation",
    "RemedyRecord",
    "RemedyResult",
    "abandon",
    "cmd_tdd",
    "resolve_checkpoint",
    "repair",
    "resolve_actor",
]
