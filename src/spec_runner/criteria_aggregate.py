"""Design §3.7 as pure functions: run outcome, selector status, BEH status (#603)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

_PRECEDENCE = ("not-passed", "nondeterministic", "no-product-execution", "subprocess-only")
_STATUSES = frozenset({"traced", "unconfirmed", "error"})


def run_outcome(phases: Mapping[str, str]) -> str:
    """passed only if setup, call and teardown all passed; any failure → failed; else skipped."""
    values = [phases.get(k) for k in ("setup", "call", "teardown")]
    if all(v == "passed" for v in values):
        return "passed"
    return "failed" if "failed" in values else "skipped"


def selector_status(runs: Sequence[Mapping[str, object]]) -> tuple[str, str | None]:
    """The first rule of §3.7 that applies; both runs must satisfy every check.

    Exactly two runs (ValueError otherwise). Each complete run's outcome is computed
    from its phases; a precomputed `outcome` that disagrees is a ValueError.
    """
    if len(runs) != 2:
        raise ValueError(f"exactly two runs expected, got {len(runs)}")
    for run in runs:
        if run["result"] == "error":
            return "error", str(run["reason"])
    phases = [_phases(run) for run in runs]
    outcomes = [run_outcome(p) for p in phases]
    for run, outcome in zip(runs, outcomes, strict=True):
        if run.get("outcome", outcome) != outcome:
            raise ValueError(f"outcome {run['outcome']!r} contradicts its phases ({outcome})")
    if any(o != "passed" for o in outcomes):
        triples = {tuple(sorted(p.items())) for p in phases}
        return "unconfirmed", "nondeterministic" if len(triples) > 1 else "not-passed"
    for run in runs:
        if run["product_line_count"] == 0:
            return "unconfirmed", "subprocess-only" if run[
                "process_operations"
            ] else "no-product-execution"
    return "traced", None


def _phases(run: Mapping[str, object]) -> dict[str, str]:
    phases = run["phases"]
    if not isinstance(phases, Mapping):
        raise ValueError(f"phases must be a mapping, got {type(phases).__name__}")
    return {str(k): str(v) for k, v in phases.items()}


def beh_status(selectors: Sequence[tuple[str, str | None]]) -> tuple[str, str | None]:
    """no selectors → no-test; any error → error; any unconfirmed → by precedence;
    traced only when every selector is traced.

    Fails closed (R-B18): a status other than traced/unconfirmed/error, or an
    unconfirmed reason outside the precedence, is a ValueError — never `traced`.
    """
    for status, reason in selectors:
        if status not in _STATUSES:
            raise ValueError(f"unknown selector status {status!r}")
        if status == "unconfirmed" and reason not in _PRECEDENCE:
            raise ValueError(f"unknown unconfirmed reason {reason!r}")
    if not selectors:
        return "unconfirmed", "no-test"
    errors = [reason for status, reason in selectors if status == "error"]
    if errors:
        return "error", errors[0]
    reasons = {reason for status, reason in selectors if status == "unconfirmed"}
    for reason in _PRECEDENCE:
        if reason in reasons:
            return "unconfirmed", reason
    if all(status == "traced" for status, _ in selectors):
        return "traced", None
    raise ValueError(f"no rule ranks {list(selectors)!r}")  # unreachable after validation
