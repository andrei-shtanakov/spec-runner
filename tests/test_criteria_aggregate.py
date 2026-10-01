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
        # the second run alone executes nothing in the product
        (
            [complete(), complete(lines=0, ops=["os.fork"])],
            ("unconfirmed", "subprocess-only"),
        ),
        ([complete(lines=0), complete(lines=0)], ("unconfirmed", "no-product-execution")),
        # a missing phase is never `passed`: the outcome is computed from the phases
        (
            [complete({"setup": "passed", "call": "passed"})] * 2,
            ("unconfirmed", "not-passed"),
        ),
    ],
)
def test_selector_status(runs, expected):
    assert selector_status(runs) == expected


@pytest.mark.parametrize("count", [0, 1, 3])
def test_selector_status_needs_exactly_two_runs(count):
    with pytest.raises(ValueError, match="two runs"):
        selector_status([complete()] * count)


def test_selector_status_refuses_an_outcome_its_phases_contradict():
    lying = {**complete({**P, "teardown": "failed"}), "outcome": "passed"}
    with pytest.raises(ValueError, match="outcome"):
        selector_status([lying, lying])


def test_selector_status_computes_the_outcome_when_absent():
    bare = {k: v for k, v in complete({**P, "call": "failed"}).items() if k != "outcome"}
    assert selector_status([bare, complete()]) == ("unconfirmed", "nondeterministic")


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
        (  # cross-precedence: not-passed outranks no-product-execution, in either order
            [("unconfirmed", "no-product-execution"), ("unconfirmed", "not-passed")],
            ("unconfirmed", "not-passed"),
        ),
        (
            [("unconfirmed", "not-passed"), ("traced", None), ("unconfirmed", "subprocess-only")],
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
