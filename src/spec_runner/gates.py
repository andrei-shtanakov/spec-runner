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
from .phases import check_outcome
from .state import PhaseOutcome

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
    # `review_policy` joins this set when #157 lands; listing it before the key
    # exists would hash a permanent `None` and quietly do nothing.
    "gate_recovery_attempts",
}


GateEvaluator = Callable[[GateContext], GateResult]


@dataclass
class GateRegistry:
    """Declarative registration, so a second consumer is not a special case
    inside the first one's code (criterion 7)."""

    _gates: dict[str, list[tuple[str, GateEvaluator]]] = field(default_factory=dict)

    def register(self, gate_id: str, phase: str, evaluate: GateEvaluator) -> None:
        self._gates.setdefault(phase, []).append((gate_id, evaluate))

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
]
