"""Typed outcome for a task phase — slice 0 of the lifecycle contract.

Until now a stage said only where it was (`StageReporter`) and, if it died,
where it died (`attempts.error_stage`). That is the whole vocabulary: a stage
either fell over or it did not. One phase already grew a real one under
pressure — `review`, in #138, because "no verdict" was being recorded as
`passed`, and a timeout as a failure.

This module generalizes that vocabulary. **Nothing gates on it yet.** The
guarantee for a project that opts into nothing is the one from the design:
execution, terminal state and external contracts do not change. (Deliberately
not "byte identical" — the append-only rows this enables make byte identity
impossible by construction.)

Design: ``docs/superpowers/specs/2026-08-11-tdd-lifecycle-design.md`` Part A.
Consumers of the gating built on top: #157 (review policy), #141 (TDD).
"""

from .stages import STAGES
from .state import PhaseOutcome, ReviewVerdict

#: There is no ``WAIVED``: a result is what the instrument observed, a waiver is
#: an operator overriding it. See ``ExecutorState.record_waiver``.

_ALWAYS = frozenset({PhaseOutcome.PASS, PhaseOutcome.ERROR, PhaseOutcome.SKIPPED})
_CAN_FAIL = _ALWAYS | {PhaseOutcome.UNEXPECTED_FAIL}
#: A phase whose verdict can be absent rather than negative: it talks to an
#: agent or to something that can time out without answering.
_CAN_BE_SILENT = _CAN_FAIL | {PhaseOutcome.NOT_RUN}

#: Admissible outcomes per stage. The vocabulary above is the *base* set, not a
#: set every stage must implement: `EXPECTED_FAIL` is meaningful for a TDD red
#: verification and meaningless for `commit`, and declaring that per stage makes
#: it a checkable property instead of a convention.
ALLOWED_OUTCOMES: dict[str, frozenset[PhaseOutcome]] = {
    "sync_deps": _CAN_FAIL,
    "branch": _CAN_FAIL,
    # The agent can also simply not answer.
    "exec": _CAN_BE_SILENT,
    "parse": _CAN_BE_SILENT,
    # EXPECTED_FAIL lands here when the TDD slices arrive: a confirmed red is a
    # test run that failed on purpose.
    "tests": _CAN_BE_SILENT | {PhaseOutcome.EXPECTED_FAIL},
    "lint": _CAN_FAIL,
    "commit": _CAN_FAIL,
    "merge": _CAN_FAIL,
    "review": _CAN_BE_SILENT,
}

assert set(ALLOWED_OUTCOMES) == set(STAGES), "every stage must declare its outcomes"


def check_outcome(phase: str, outcome: PhaseOutcome) -> None:
    """Raise ``ValueError`` when ``outcome`` is not admissible for ``phase``.

    A stage producing an outcome outside its declared set is a bug in the
    caller, not an interesting runtime condition — so it is loud here, at the
    boundary, rather than stored and puzzled over later.
    """
    allowed = ALLOWED_OUTCOMES.get(phase)
    if allowed is None:
        raise ValueError(f"unknown phase {phase!r}; expected one of {sorted(ALLOWED_OUTCOMES)}")
    if outcome not in allowed:
        raise ValueError(
            f"outcome {outcome.value!r} is not admissible for phase {phase!r}; "
            f"allowed: {sorted(o.value for o in allowed)}"
        )


#: `ReviewVerdict` is not a second, parallel vocabulary. Review reports the
#: shared outcome plus a review-specific detail, so `fixed` stops being a peer
#: of `passed` and becomes what kind of pass it was. The stored wire values in
#: `attempts.review_status` are untouched — this is a reading, not a migration.
_REVIEW_MAP: dict[ReviewVerdict, tuple[PhaseOutcome, str | None]] = {
    ReviewVerdict.PASSED: (PhaseOutcome.PASS, "passed"),
    ReviewVerdict.FIXED: (PhaseOutcome.PASS, "fixed"),
    ReviewVerdict.FAILED: (PhaseOutcome.UNEXPECTED_FAIL, None),
    ReviewVerdict.REJECTED: (PhaseOutcome.UNEXPECTED_FAIL, "rejected"),
    ReviewVerdict.NOT_RUN: (PhaseOutcome.NOT_RUN, None),
    ReviewVerdict.ERROR: (PhaseOutcome.ERROR, None),
    ReviewVerdict.SKIPPED: (PhaseOutcome.SKIPPED, None),
}


def review_verdict_to_phase(verdict: ReviewVerdict | str) -> tuple[PhaseOutcome, str | None]:
    """Read a `ReviewVerdict` as ``(outcome, detail)``.

    Unknown values map to ``ERROR`` rather than raising: the state DB is a
    long-lived artifact and may hold a value written by a future version.
    """
    try:
        key = ReviewVerdict(verdict) if isinstance(verdict, str) else verdict
    except ValueError:
        return PhaseOutcome.ERROR, str(verdict)
    return _REVIEW_MAP.get(key, (PhaseOutcome.ERROR, str(key.value)))


__all__ = [
    "ALLOWED_OUTCOMES",
    "PhaseOutcome",
    "check_outcome",
    "review_verdict_to_phase",
]
