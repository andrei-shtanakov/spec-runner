"""#603 B2b: design §3.7 — outcome over three phases, selector and BEH status."""

import pytest

from spec_runner.criteria_aggregate import beh_status, run_outcome, selector_status

P = {"setup": "passed", "call": "passed", "teardown": "passed"}


def complete(phases=P, lines=1, ops=()):
    return {
        "result": "complete",
        "phases": phases,
        "outcome": run_outcome(phases),
        "product_line_count": lines,
        "process_operations": list(ops),
    }


ERR = {"result": "error", "reason": "runner", "detail": "x"}


@pytest.mark.parametrize(
    ("phases", "outcome"),
    [
        (P, "passed"),
        ({**P, "teardown": "failed"}, "failed"),
        ({"setup": "failed", "call": "not-reached", "teardown": "passed"}, "failed"),
        ({"setup": "skipped", "call": "not-reached", "teardown": "passed"}, "skipped"),
        ({**P, "call": "skipped"}, "skipped"),
        ({**P, "teardown": "skipped"}, "skipped"),
    ],
)
def test_run_outcome(phases, outcome):
    assert run_outcome(phases) == outcome


@pytest.mark.parametrize(
    ("runs", "expected"),
    [
        ([complete(), complete()], ("traced", None)),
        ([ERR, complete()], ("error", "runner")),
        ([complete(), {**ERR, "reason": "io"}], ("error", "io")),
        (
            [complete({**P, "teardown": "failed"}), complete()],
            ("unconfirmed", "nondeterministic"),
        ),
        ([complete({**P, "teardown": "failed"})] * 2, ("unconfirmed", "not-passed")),
        (
            [complete(lines=0, ops=["subprocess.Popen"]), complete()],
            ("unconfirmed", "subprocess-only"),
        ),
        ([complete(), complete(lines=0)], ("unconfirmed", "no-product-execution")),
        # skipped: identical triples -> not-passed; different triples -> nondeterministic
        (
            [complete({**P, "teardown": "skipped"})] * 2,
            ("unconfirmed", "not-passed"),
        ),
        (
            [complete({"setup": "skipped", "call": "not-reached", "teardown": "passed"})] * 2,
            ("unconfirmed", "not-passed"),
        ),
        (
            [
                complete({**P, "teardown": "skipped"}),
                complete({**P, "call": "skipped"}),
            ],
            ("unconfirmed", "nondeterministic"),
        ),
        (
            [complete({**P, "teardown": "skipped"}), complete()],
            ("unconfirmed", "nondeterministic"),
        ),
    ],
)
def test_selector_status(runs, expected):
    assert selector_status(runs) == expected


@pytest.mark.parametrize(
    ("selectors", "expected"),
    [
        ([], ("unconfirmed", "no-test")),
        ([("traced", None)] * 3, ("traced", None)),
        (
            [("traced", None), ("error", "io"), ("unconfirmed", "not-passed")],
            ("error", "io"),
        ),
        (
            [("unconfirmed", "subprocess-only"), ("unconfirmed", "no-product-execution")],
            ("unconfirmed", "no-product-execution"),
        ),
        (
            [("unconfirmed", "nondeterministic"), ("unconfirmed", "not-passed")],
            ("unconfirmed", "not-passed"),
        ),
    ],
)
def test_beh_status(selectors, expected):
    assert beh_status(selectors) == expected


@pytest.mark.parametrize(
    "selectors",
    [
        [("traced", None), ("passed", None)],  # unknown status
        [("traced", None), ("Traced", None)],
        [("unconfirmed", "no-test")],  # a BEH-level reason, never a selector's
        [("traced", None), ("unconfirmed", "flaky")],
        [("unconfirmed", None)],
        [("error", "runner"), ("bogus", None)],  # validated before the error short-cut
    ],
)
def test_beh_status_refuses_what_it_cannot_rank(selectors):
    """R-B18: fail closed — never `traced` for a selector it does not understand."""
    with pytest.raises(ValueError):
        beh_status(selectors)
