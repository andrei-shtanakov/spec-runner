"""Pre-terminal policy gates — the mechanism (#164).

A gate does **not** withhold the checkpoint commit. That commit always happens:
a stable SHA is exactly what a gate evaluates *against*, and replay without a
commit to replay against is trust in whatever is in the working tree. What a
gate withholds is progress past the checkpoint — the next phase and, ultimately,
merge and terminal completion.

```
work → deterministic checks → checkpoint commit
→ policy gate, evaluated against the checkpoint SHA
   ├─ satisfied        → merge → DONE
   ├─ unsatisfied      → resumable / non-terminal
   └─ instrument error → bounded recovery → infrastructure error
```

**Dormant until something registers.** With no gate for a phase, `evaluate_gates`
returns SATISFIED and touches nothing, which is criterion 8: a project that
enables no consumer cannot tell this shipped. The first consumer is the review
policy (#157), the second TDD's confirmed red (#141); neither owns this module.

Design: ``docs/superpowers/specs/2026-08-11-checkpoint-and-pre-terminal-gates-design.md``
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from .logging import get_logger
from .phases import check_outcome, review_verdict_to_phase
from .state import PhaseOutcome, ReviewVerdict

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ExecutorConfig
    from .state import ExecutorState

logger = get_logger("gates")


class GateStatus(str, Enum):
    """What a gate concluded about progressing past the checkpoint.

    Three, not two: "the gate says no" and "the gate could not answer" are
    different facts with different owners — the same distinction `PhaseOutcome`
    draws between `UNEXPECTED_FAIL` and `ERROR`, and the one #138 had to
    introduce after a review timeout was being recorded as a pass.
    """

    SATISFIED = "satisfied"
    UNSATISFIED = "unsatisfied"
    #: The instrument broke. Not a defect in the work, and not NEEDS_HUMAN on
    #: the first stumble: bounded recovery first, infrastructure error after.
    INSTRUMENT_ERROR = "instrument_error"


@dataclass(frozen=True)
class GateResult:
    """One gate's answer, in the shared phase vocabulary."""

    status: GateStatus
    outcome: PhaseOutcome
    detail: str | None = None
    gate_id: str = ""


@dataclass
class GateContext:
    """What a gate is evaluated against.

    ``checkpoint_sha`` and ``config_hash`` together identify *which tree under
    which policy* — a verdict is a statement about that pair and stops being
    one when either moves.
    """

    task_id: str
    checkpoint_sha: str
    config: ExecutorConfig
    state: ExecutorState | None = None
    #: Observations the call site passes in for this evaluation.
    #:
    #: Deliberately not "read it back from `phase_results`". That write is
    #: best-effort — a storage failure is swallowed so bookkeeping can never
    #: fail a task — so reading a *blocking* decision out of it would make
    #: "the instrument produced no verdict" indistinguishable from "we could
    #: not read our own note". The first is a fact about the code; the second
    #: is our bug. A missing key here is an instrument error, never a verdict.
    facts: dict[str, object] = field(default_factory=dict)

    @property
    def config_hash(self) -> str:
        """Short hash of the policy this evaluation runs under.

        Only policy-bearing keys go in: a verdict must not be invalidated by an
        unrelated config edit, and must be invalidated by a relevant one.
        """
        parts = [f"{key}={getattr(self.config, key, None)!r}" for key in sorted(POLICY_KEYS)]
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


#: Config keys whose change makes an earlier verdict stale. Kept explicit
#: rather than hashing the whole config: hashing everything would invalidate
#: verdicts on an unrelated edit and train people to ignore staleness.
POLICY_KEYS: set[str] = {
    "review_policy",
    "gate_recovery_attempts",
}


GateEvaluator = Callable[[GateContext], GateResult]


@dataclass
class GateRegistry:
    """Declarative registration, so a second consumer is not a special case
    inside the first one's code (criterion 7)."""

    _gates: dict[str, list[tuple[str, GateEvaluator]]] = field(default_factory=dict)

    def register(self, gate_id: str, phase: str, evaluate: GateEvaluator) -> None:
        """Register (or replace) the gate ``gate_id`` for ``phase``.

        Re-registering the same id replaces it rather than stacking a
        duplicate: `watch` puts many tasks through one process, and a gate
        that ran twice per phase would double-count its own verdict. Distinct
        consumers must therefore use distinct ids.
        """
        gates = self._gates.setdefault(phase, [])
        for i, (existing, _) in enumerate(gates):
            if existing == gate_id:
                gates[i] = (gate_id, evaluate)
                return
        gates.append((gate_id, evaluate))

    def unregister(self, gate_id: str, phase: str) -> None:
        """Remove a gate if present. Used when a policy is turned back off."""
        gates = self._gates.get(phase)
        if gates:
            self._gates[phase] = [g for g in gates if g[0] != gate_id]

    def for_phase(self, phase: str) -> list[tuple[str, GateEvaluator]]:
        return list(self._gates.get(phase, []))

    def phases(self) -> list[str]:
        """Phases that have at least one gate, in registration order."""
        return [phase for phase, gates in self._gates.items() if gates]


#: The process-wide registry consumers attach to. A registry can also be passed
#: explicitly, which is what the tests do — a global that tests must mutate is
#: how order-dependent suites are born.
REGISTRY = GateRegistry()


@dataclass(frozen=True)
class GateOutcome:
    """The aggregate for one phase."""

    status: GateStatus
    results: list[GateResult]


def _aggregate(results: list[GateResult]) -> GateStatus:
    """Concrete "no" outranks "could not tell": there is something to act on."""
    statuses = {r.status for r in results}
    if GateStatus.UNSATISFIED in statuses:
        return GateStatus.UNSATISFIED
    if GateStatus.INSTRUMENT_ERROR in statuses:
        return GateStatus.INSTRUMENT_ERROR
    return GateStatus.SATISFIED


def evaluate_gates(
    phase: str,
    ctx: GateContext,
    registry: GateRegistry | None = None,
) -> GateOutcome:
    """Run every gate registered for ``phase`` and aggregate.

    Returns SATISFIED with no results when nothing is registered — the dormant
    case, and the one that keeps behaviour unchanged for projects that enable
    no consumer.

    An ``INSTRUMENT_ERROR`` is retried up to ``config.gate_recovery_attempts``
    times. An ``UNSATISFIED`` is not: that is an answer, and repeating the
    question does not make it a different one.
    """
    reg = registry if registry is not None else REGISTRY
    gates = reg.for_phase(phase)
    if not gates:
        return GateOutcome(GateStatus.SATISFIED, [])

    budget = max(0, int(getattr(ctx.config, "gate_recovery_attempts", 1)))
    results: list[GateResult] = []
    for gate_id, evaluate in gates:
        result = _evaluate_one(gate_id, evaluate, ctx, budget, phase)
        results.append(result)
        if ctx.state is not None:
            try:
                ctx.state.record_phase(ctx.task_id, phase, result.outcome, result.detail)
            except ValueError as exc:
                # A gate registered for a phase the vocabulary does not know.
                # Its verdict still gets recorded below — losing the answer
                # because the label was wrong would be the worse failure.
                logger.warning(
                    "Gate phase is outside the outcome vocabulary",
                    gate=gate_id,
                    phase=phase,
                    error=str(exc),
                )
            ctx.state.record_gate_verdict(
                ctx.task_id,
                gate_id,
                ctx.checkpoint_sha,
                ctx.config_hash,
                result.status,
                result.detail,
            )
    return GateOutcome(_aggregate(results), results)


def _evaluate_one(
    gate_id: str,
    evaluate: GateEvaluator,
    ctx: GateContext,
    budget: int,
    phase: str,
) -> GateResult:
    result = GateResult(GateStatus.INSTRUMENT_ERROR, PhaseOutcome.ERROR, "gate never ran", gate_id)
    for attempt in range(budget + 1):
        try:
            result = evaluate(ctx)
            # Type first. Without it a gate returning something GateResult-ish
            # gets re-wrapped below, *outside* this try — and a `status` that
            # is a bare string ("satisfied") is not a GateStatus member, so
            # `_aggregate` would not recognise it and the phase would pass.
            # A malformed answer must fail closed, like every other gate bug.
            if not isinstance(result, GateResult):
                raise TypeError(f"gate returned {type(result).__name__}, expected GateResult")
            # An outcome the phase cannot produce is a bug in the gate, and a
            # buggy gate is a broken instrument — not a licence to crash the
            # run it was supposed to judge. It is also emphatically not a pass.
            check_outcome(phase, result.outcome)
        except Exception as exc:  # a broken gate is an instrument error, not a verdict
            result = GateResult(GateStatus.INSTRUMENT_ERROR, PhaseOutcome.ERROR, str(exc), gate_id)
        result = GateResult(result.status, result.outcome, result.detail, gate_id)
        if result.status is not GateStatus.INSTRUMENT_ERROR:
            return result
        if attempt < budget:
            logger.warning(
                "Gate could not answer; recovering",
                gate=gate_id,
                attempt=attempt + 1,
                remaining=budget - attempt,
                detail=result.detail,
            )
    logger.error("Gate exhausted recovery — infrastructure error", gate=gate_id)
    return result


#: How each review verdict maps to a gate answer under `review_policy: required`.
#: This table is the owner's decision, verbatim — see §1 of the design doc.
_REVIEW_GATE: dict[ReviewVerdict, GateStatus] = {
    ReviewVerdict.PASSED: GateStatus.SATISFIED,
    # `fixed` is a kind of pass, not a peer of it (slice 0's reading). The
    # fixes are commits between the review checkpoint and the merge candidate,
    # and the deterministic gates already re-ran over them (#65).
    ReviewVerdict.FIXED: GateStatus.SATISFIED,
    ReviewVerdict.FAILED: GateStatus.UNSATISFIED,
    ReviewVerdict.REJECTED: GateStatus.UNSATISFIED,
    # The review did not happen. "I don't know" is not "fine" — the #138
    # defect one level up.
    ReviewVerdict.NOT_RUN: GateStatus.UNSATISFIED,
    # The instrument broke, which is not a defect in the work. The mechanism,
    # not this gate, decides what an exhausted error becomes.
    ReviewVerdict.ERROR: GateStatus.INSTRUMENT_ERROR,
    ReviewVerdict.SKIPPED: GateStatus.UNSATISFIED,
}


def _review_gate(ctx: GateContext) -> GateResult:
    """Does the review policy permit this task to complete?

    Reads the verdict from ``ctx.facts`` — never from `phase_results`, for the
    reason spelled out on `GateContext.facts`.
    """
    raw = ctx.facts.get("review_verdict")
    if raw is None:
        return GateResult(
            GateStatus.INSTRUMENT_ERROR,
            PhaseOutcome.ERROR,
            "the run reported no review verdict to the gate",
        )
    # Parse before reading. `facts` is `dict[str, object]`, so a call site can
    # put anything in it; deciding what the verdict *means* before knowing it
    # is a verdict is how a malformed fact becomes an exception instead of a
    # clean instrument error.
    if not isinstance(raw, ReviewVerdict | str):
        return GateResult(
            GateStatus.INSTRUMENT_ERROR,
            PhaseOutcome.ERROR,
            f"review verdict is a {type(raw).__name__}, expected a ReviewVerdict",
        )
    try:
        verdict = ReviewVerdict(raw)
    except ValueError:
        return GateResult(
            GateStatus.INSTRUMENT_ERROR, PhaseOutcome.ERROR, f"unrecognised review verdict {raw!r}"
        )
    outcome, verdict_detail = review_verdict_to_phase(verdict)

    status = _REVIEW_GATE.get(verdict, GateStatus.INSTRUMENT_ERROR)
    parts = [f"review {verdict_detail or verdict.value}"]
    # §2.1: name both trees. The gate judges the merge candidate; review judged
    # the review checkpoint, and a `fixed` verdict means they differ. Claiming
    # they are one tree would be the dishonest option.
    reviewed = ctx.facts.get("review_checkpoint_sha")
    if reviewed:
        parts.append(f"of review checkpoint {reviewed}")
    parts.append(f"at merge candidate {ctx.checkpoint_sha}")
    if verdict is ReviewVerdict.SKIPPED:
        parts.append("— review_policy is 'required' but run_review is off")
    return GateResult(status, outcome, " ".join(parts))


def register_builtin_gates(
    config: ExecutorConfig,
    registry: GateRegistry | None = None,
) -> None:
    """Attach the gates this config asks for. Call once per run.

    Under `advisory` the review gate is **not registered at all**, rather than
    registered as something that always passes. The difference is the whole of
    #164 criterion 8: an always-passing gate would still resolve a checkpoint
    SHA and open the state DB on every task, so "nothing enabled changes
    nothing" would stop being a mechanical property and become a claim.
    """
    reg = registry if registry is not None else REGISTRY
    if getattr(config, "review_policy", "advisory") == "required":
        reg.register("review", "review", _review_gate)
    else:
        reg.unregister("review", "review")


def has_gates(registry: GateRegistry | None = None) -> bool:
    """Whether anything is registered at all.

    Call sites check this *before* building a `GateContext`: the dormant path
    must not resolve a checkpoint SHA, open the state DB or shell out to git,
    or criterion 8 becomes a claim rather than a property.
    """
    reg = registry if registry is not None else REGISTRY
    return bool(reg.phases())


def evaluate_pre_terminal(
    ctx: GateContext,
    registry: GateRegistry | None = None,
) -> GateOutcome:
    """Evaluate every registered gate at the pre-terminal point.

    This is the site the design describes: after the checkpoint commit, before
    merge and terminal completion. Each gate still carries the phase whose
    evidence it judges — ``review`` for #157, ``tests`` for #141's confirmed red
    — so its row lands in the same append-only history as everything else.

    A consumer with its own transition to guard (TDD gating GREEN, not merge)
    calls `evaluate_gates` for its phase at that transition instead.
    """
    reg = registry if registry is not None else REGISTRY
    results: list[GateResult] = []
    for phase in reg.phases():
        results.extend(evaluate_gates(phase, ctx, registry=reg).results)
    return GateOutcome(_aggregate(results), results)


__all__ = [
    "POLICY_KEYS",
    "REGISTRY",
    "GateContext",
    "GateOutcome",
    "GateRegistry",
    "GateResult",
    "GateStatus",
    "evaluate_gates",
    "evaluate_pre_terminal",
    "has_gates",
    "register_builtin_gates",
]
