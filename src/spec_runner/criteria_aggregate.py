"""Design §3.7 as pure functions: run outcome, selector status, BEH status (#603)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

_PRECEDENCE = ("not-passed", "nondeterministic", "no-product-execution", "subprocess-only")


def run_outcome(phases: Mapping[str, str]) -> str:
    """passed only if setup, call and teardown all passed; any failure → failed; else skipped."""
    values = [phases.get(k) for k in ("setup", "call", "teardown")]
    if all(v == "passed" for v in values):
        return "passed"
    return "failed" if "failed" in values else "skipped"


def selector_status(runs: Sequence[Mapping[str, object]]) -> tuple[str, str | None]:
    """The first rule of §3.7 that applies; both runs must satisfy every check."""
    for run in runs:
        if run["result"] == "error":
            return "error", str(run["reason"])
    outcomes = [run["outcome"] for run in runs]
    if any(o != "passed" for o in outcomes):
        triples = [tuple(sorted(dict(run["phases"]).items())) for run in runs]  # type: ignore[call-overload]
        return "unconfirmed", "nondeterministic" if len(set(triples)) > 1 else "not-passed"
    for run in runs:
        if run["product_line_count"] == 0:
            return "unconfirmed", "subprocess-only" if run[
                "process_operations"
            ] else "no-product-execution"
    return "traced", None


def beh_status(selectors: Sequence[tuple[str, str | None]]) -> tuple[str, str | None]:
    """no selectors → no-test; any error → error; any unconfirmed → by precedence; else traced."""
    if not selectors:
        return "unconfirmed", "no-test"
    errors = [reason for status, reason in selectors if status == "error"]
    if errors:
        return "error", errors[0]
    reasons = {reason for status, reason in selectors if status == "unconfirmed"}
    for reason in _PRECEDENCE:
        if reason in reasons:
            return "unconfirmed", reason
    return "traced", None
