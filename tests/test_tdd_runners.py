"""#198 step 2, build order §1: the adapter types, with pytest as the only one.

Behaviour-preserving by construction — pytest is the runner step 1 already
allowed, and the existing RED tests are the proof that nothing moved. What is
new is the *shape*: observation (`RunOutcome`) and proof (`SelectionProof`) are
separate answers, so an adapter cannot assert which test ran as a side effect of
reading an exit code.

That separation is not ceremony. On ExUnit — the adapter this shape exists for —
`mix test path:line` selects the nearest test at or before the line, so a line
past the end of a file runs the *last* test and reports an ordinary
"1 test, 1 failure". `TESTS_FAILED` is true there and the red is still a lie.

Design: `docs/superpowers/specs/2026-08-12-tdd-runner-adapter-design.md`
"""

import subprocess
from pathlib import PurePosixPath

import pytest

from spec_runner.tdd_runners import (
    ADAPTERS,
    PytestAdapter,
    PytestNodeId,
    RunOutcome,
    SelectionProof,
    Selector,
    SelectorRefusal,
    adapter_for,
    executable_of,
    infer_adapter,
    normalise_path,
)

ADAPTER = PytestAdapter()


def _result(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestParsingASelector:
    def test_a_node_id_parses(self):
        selector = ADAPTER.parse_selector("tests/test_x.py::test_y")
        assert isinstance(selector, Selector)
        assert selector.runner == "pytest"
        assert selector.path == PurePosixPath("tests/test_x.py")
        assert selector.locator == PytestNodeId("tests/test_x.py::test_y")

    def test_a_class_qualified_node_id_parses(self):
        selector = ADAPTER.parse_selector("tests/test_x.py::TestY::test_z")
        assert isinstance(selector, Selector)
        assert selector.path == PurePosixPath("tests/test_x.py")

    def test_a_bare_path_is_refused_with_a_stable_code(self):
        """The code is what tests and operators match on; the message is for
        humans and may be reworded."""
        refusal = ADAPTER.parse_selector("tests/test_x.py")
        assert isinstance(refusal, SelectorRefusal)
        assert refusal.code == "not_a_node_id"

    def test_a_selector_naming_no_file_is_refused(self):
        assert isinstance(ADAPTER.parse_selector("::test_y"), SelectorRefusal)

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("./tests/test_x.py", "tests/test_x.py"),
            ("tests//test_x.py", "tests/test_x.py"),
            ("  tests/test_x.py  ", "tests/test_x.py"),
        ],
    )
    def test_paths_are_normalised(self, raw, expected):
        """So a comparison against runner output never turns on a leading `./`."""
        assert str(normalise_path(raw)) == expected


class TestTheCommandContract:
    def test_a_pytest_command_is_accepted(self):
        assert ADAPTER.validate_command("uv run pytest -q") is None

    def test_another_runner_is_refused_by_name(self):
        refusal = ADAPTER.validate_command("mix test")
        assert refusal and "tdd_runner" in refusal

    def test_build_command_is_argv_with_the_selector_as_one_argument(self):
        """The selector is agent output. As argv it is an argument; as a shell
        string it is one quoting slip from being a command."""
        selector = ADAPTER.parse_selector("tests/t.py::test_x; rm -rf ~")
        assert isinstance(selector, Selector)
        argv = ADAPTER.build_command("uv run pytest -q", selector)
        assert argv[:-1] == ["uv", "run", "pytest", "-q"]
        assert argv[-1] == "tests/t.py::test_x; rm -rf ~"

    def test_pytest_needs_no_preflight(self, tmp_path):
        """And the absence is the point: a node id that names nothing exits 4,
        never 1, so it cannot be mistaken for a red. ExUnit lacks that."""
        selector = ADAPTER.parse_selector("tests/t.py::test_x")
        assert isinstance(selector, Selector)
        assert ADAPTER.preflight(tmp_path, selector) is None


class TestClassifyDescribesTheRunOnly:
    @pytest.mark.parametrize(
        "code,expected",
        [
            (0, RunOutcome.TESTS_PASSED),
            (1, RunOutcome.TESTS_FAILED),
            (4, RunOutcome.COLLECTION_OR_COMPILE_ERROR),
            (5, RunOutcome.SELECTION_FAILED),
            (2, RunOutcome.RUNNER_ERROR),
            (137, RunOutcome.RUNNER_ERROR),
        ],
    )
    def test_the_measured_table(self, code, expected):
        assert ADAPTER.classify(_result(code)) is expected

    def test_it_never_answers_which_test_ran(self):
        """`TESTS_FAILED` for a run of a hundred tests is still `TESTS_FAILED`.
        Identity is `prove_selected`'s answer, and keeping them apart is what
        stops an adapter claiming identity in its easy path."""
        result = _result(1, "97 passed, 3 failed in 4.0s")
        assert ADAPTER.classify(result) is RunOutcome.TESTS_FAILED
        selector = ADAPTER.parse_selector("tests/t.py::test_x")
        assert isinstance(selector, Selector)
        assert ADAPTER.prove_selected(selector, result) is SelectionProof.UNKNOWN


class TestProvingTheSelection:
    def _selector(self) -> Selector:
        selector = ADAPTER.parse_selector("tests/test_p.py::test_bad")
        assert isinstance(selector, Selector)
        return selector

    def test_the_node_id_in_the_failure_header_proves_it(self):
        """Measured: pytest prints `FAILED tests/test_p.py::test_bad - assert False`."""
        result = _result(1, "FAILED tests/test_p.py::test_bad - assert False\n1 failed in 0.01s")
        assert ADAPTER.prove_selected(self._selector(), result) is SelectionProof.PROVEN

    def test_a_single_passing_test_proves_it_too(self):
        """Measured: a passing run prints `1 passed in 0.00s` and no node id.
        One test ran, and pytest cannot silently substitute a different one."""
        result = _result(0, "============ 1 passed in 0.00s ============")
        assert ADAPTER.prove_selected(self._selector(), result) is SelectionProof.PROVEN

    def test_several_tests_passing_proves_nothing(self):
        result = _result(0, "============ 12 passed in 0.40s ============")
        assert ADAPTER.prove_selected(self._selector(), result) is SelectionProof.UNKNOWN

    def test_silence_proves_nothing(self):
        assert ADAPTER.prove_selected(self._selector(), _result(0)) is SelectionProof.UNKNOWN


class TestTheRegistry:
    def test_pytest_is_the_only_adapter(self):
        """A list of what was *measured*. Adding a name here without measuring
        the runner is the whole defect (#198) in one line of diff."""
        assert set(ADAPTERS) == {"pytest"}

    def test_lookup_by_name(self):
        assert adapter_for("pytest") is not None
        assert adapter_for("exunit") is None
        assert adapter_for("") is None

    @pytest.mark.parametrize(
        "command,expected",
        [
            ("pytest -x", "pytest"),
            ("uv run pytest", "pytest"),
            ("python -m pytest", "pytest"),
            ("./venv/bin/pytest", "pytest"),
            ("mix test", None),
            ("mix test --formatter PytestFormatter", None),
            ("./scripts/run-pytest-in-docker.sh", None),
            ("", None),
        ],
    )
    def test_inference_only_where_it_cannot_be_wrong(self, command, expected):
        adapter = infer_adapter(command)
        assert (adapter.name if adapter else None) == expected

    def test_executable_looks_past_wrappers(self):
        assert executable_of("uv run poetry run pytest -q") == "pytest"
        assert executable_of("mix test") == "mix"
