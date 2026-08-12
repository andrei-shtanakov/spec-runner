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

    @pytest.mark.parametrize(
        "summary",
        [
            "===== 1 failed, 2 passed, 1 skipped, 1 xfailed in 0.01s =====",
            "===== 1 failed, 97 passed in 4.00s =====",
            "===== 1 passed, 2 failed in 0.10s =====",
        ],
    )
    def test_a_whole_suite_run_is_not_proof(self, summary):
        """Raised in review of this PR: the summary's *first* count being 1 is
        not "one test ran". Measured — pytest's mixed line really does start
        `1 failed, 2 passed, …`, so a full-suite run would otherwise stand in
        for the named test and confirm a red nobody demonstrated."""
        assert ADAPTER.prove_selected(self._selector(), _result(1, summary)) is (
            SelectionProof.UNKNOWN
        )

    def test_a_single_skipped_test_is_not_proof(self):
        """One skipped test is one test that did **not** run. Counting it would
        retire a claimed red — `not_red` sends an operator to `repair` — on the
        strength of a test nobody executed."""
        result = _result(0, "===== 1 skipped in 0.00s =====")
        assert ADAPTER.prove_selected(self._selector(), result) is SelectionProof.UNKNOWN

    @pytest.mark.parametrize("word", ["passed", "failed", "xfailed", "xpassed"])
    def test_the_measured_single_test_forms_are_proof(self, word):
        result = _result(0, f"============ 1 {word} in 0.01s ============")
        assert ADAPTER.prove_selected(self._selector(), result) is SelectionProof.PROVEN

    def test_the_node_id_still_wins_over_any_summary(self):
        """A named failure is direct evidence and does not need the count."""
        result = _result(
            1,
            "FAILED tests/test_p.py::test_bad - assert False\n1 failed, 97 passed in 4.0s",
        )
        assert ADAPTER.prove_selected(self._selector(), result) is SelectionProof.PROVEN


class TestTheRegistry:
    def test_the_registry_lists_only_measured_runners(self):
        """A list of what was *measured*. Adding a name here without measuring
        the runner is the whole defect (#198) in one line of diff — so this
        asserts the exact set, and grows only when a measurement does."""
        assert set(ADAPTERS) == {"pytest", "exunit"}

    def test_lookup_by_name(self):
        assert adapter_for("pytest") is not None
        assert adapter_for("exunit") is not None
        assert adapter_for("rspec") is None
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


class TestThePromptAsksForTheRightShape:
    """#198, found before the first paid pilot run: the RED prompt hardcoded
    pytest's node id, so an agent on an Elixir project would comply with *that*
    shape — and `path::name` is exactly what the ExUnit adapter refuses. Every
    RED would have died `unverifiable` before a line of implementation, and the
    pilot would have burned its budget discovering it."""

    def _selector_line(self, test_command: str, runner: str) -> str:
        from spec_runner.config import ExecutorConfig
        from spec_runner.prompt import build_red_prompt
        from spec_runner.task import Task

        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1d")
        prompt = build_red_prompt(
            task, ExecutorConfig(test_command=test_command, tdd_runner=runner)
        )
        return next(line for line in prompt.splitlines() if "TDD_SELECTOR" in line).strip()

    def test_pytest_is_asked_for_a_node_id(self):
        assert "::" in self._selector_line("uv run pytest", "pytest")

    def test_exunit_is_asked_for_a_line(self):
        line = self._selector_line("mix test", "exunit")
        assert "::" not in line
        assert ":LINE" in line

    def test_what_the_prompt_asks_for_is_what_the_adapter_parses(self):
        """The property that matters, and it has to be read out of the prompt
        text itself.

        An earlier version compared against a hand-written example per adapter,
        which asserted that *my* example parses — not that the **prompted** one
        does — and would have raised `KeyError` the moment a third adapter
        arrived (Copilot, PR #205). The example now comes from
        `selector_instruction`, so prompt and parser cannot drift apart in
        silence.
        """
        from spec_runner.tdd_runners import ADAPTERS, Selector

        for name, adapter in ADAPTERS.items():
            instruction = adapter.selector_instruction
            marker = "TDD_SELECTOR:"
            assert marker in instruction, name
            example = next(line for line in instruction.splitlines() if marker in line).split(
                marker, 1
            )[1]
            # The one substitution a shape may carry: a placeholder for a
            # number the agent fills in.
            example = example.strip().replace("LINE", "12")
            parsed = adapter.parse_selector(example)
            assert isinstance(parsed, Selector), f"{name}: prompt asks for {example!r}, refused"

    def test_an_unsupported_runner_says_so_rather_than_asking_for_pytest(self):
        line = self._selector_line("go test ./...", "")
        assert "::" not in line


class TestPytestReplayIsUnchanged:
    """#207 gave every adapter a preparation step. pytest's is a passthrough,
    and it must stay one: a Python environment lives outside the checkout,
    which is exactly why pytest never met the defect."""

    def test_it_adds_no_environment_and_nothing_to_clean_up(self, tmp_path):
        from spec_runner.tdd_runners import ReplayEnvironment

        selector = ADAPTER.parse_selector("tests/t.py::test_x")
        assert isinstance(selector, Selector)
        prepared = ADAPTER.prepare_replay(tmp_path, tmp_path, selector)
        assert isinstance(prepared, ReplayEnvironment)
        assert prepared.env == {}
        assert prepared.cleanup_paths == ()

    def test_it_keeps_the_lockfile_identity(self, tmp_path):
        from spec_runner.tdd_runners import ReplayEnvironment

        (tmp_path / "uv.lock").write_text("x = 1\n")
        selector = ADAPTER.parse_selector("tests/t.py::test_x")
        assert isinstance(selector, Selector)
        prepared = ADAPTER.prepare_replay(tmp_path, tmp_path, selector)
        assert isinstance(prepared, ReplayEnvironment)
        assert prepared.environment_id.startswith("uv.lock:")

    def test_it_never_shells_out(self, tmp_path, monkeypatch):
        """Preparation for pytest costs nothing, and must not start costing."""
        import spec_runner.tdd_runners as runners

        monkeypatch.setattr(
            runners.subprocess,
            "run",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("pytest prepare ran a process")),
        )
        selector = ADAPTER.parse_selector("tests/t.py::test_x")
        assert isinstance(selector, Selector)
        ADAPTER.prepare_replay(tmp_path, tmp_path, selector)
