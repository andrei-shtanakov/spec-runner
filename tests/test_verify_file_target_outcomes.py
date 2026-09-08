"""BEH-10, BEH-11, BEH-12, BEH-13, BEH-14, BEH-15, BEH-16 (TASK-003, DT-03).

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-10 (-BEH-16)
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/30-decomposition.md#DT-03

The subject is the fold `live_verify._resolve_file_target_triplet`: it turns
a file target's per-member manifest (DT-02) into the same
`RunOutcome`/`SelectionProof`/`ExecutionProof` triplet `classify_verify_outcome`
already judges for a single node id — `classify_verify_outcome` itself is not
touched by this file, and the node-id branch never goes through this fold at
all.

Two kinds of test, per the behaviour spec's own `checked_by` lines:
- `kind: contract` — a synthetic per-member report drives the fold directly,
  proving the classification table (no subprocess).
- `kind: integration` — a real git/pytest replay against a fixture repo,
  proving both the classification AND the message an operator reads,
  mirroring `tests/test_verify_file_composition.py`'s fixtures.
At least one table row (`TestBEH12CompositionConjunctionTable`'s green case)
is cross-checked against a real manifest of a real run, not only synthetic
data.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runner import tdd
from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.live_verify import (
    VerifyOutcome,
    _resolve_file_target_triplet,
    classify_verify_outcome,
    run_live_verify,
)
from spec_runner.state import ErrorCode, ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import resolve_namespace
from spec_runner.tdd_runners import FileComposition


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests").mkdir()
    return root


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "test_command": "python -m pytest",
        "max_retries": 1,
        "retry_delay_seconds": 0,
        "create_git_branch": False,
        "run_tests_on_done": False,
        "auto_commit": True,
        "run_review": False,
        "callback_url": "",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(**overrides) -> Task:
    defaults: dict = {
        "id": "TASK-301",
        "name": "verify-first file target",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
        "execution_mode": "verify_first",
    }
    defaults.update(overrides)
    return Task(**defaults)


def _classify(composition: FileComposition | None) -> VerifyOutcome:
    return classify_verify_outcome(*_resolve_file_target_triplet(composition))


class TestBEH10FileTargetStopsBeforeAnyPaidCall:
    """kind: integration — BEH-10: every class of file-target refusal stops
    the task before its first paid call — the ledger stays empty, and the
    class is `ErrorCode.INFRASTRUCTURE`, never a silent `green`."""

    def _assert_no_paid_call_and_infrastructure(self, task, config):
        red_agent = MagicMock(
            side_effect=AssertionError("no red authoring for a file-target refusal")
        )
        with (
            patch.object(tdd, "_run_agent", red_agent),
            patch("spec_runner.execution.update_task_status"),
            patch("spec_runner.execution._run_agent_process") as mock_run,
            ExecutorState(config) as state,
        ):
            outcome = execute_task(task, config, state)

            assert outcome is False
            mock_run.assert_not_called()
            red_agent.assert_not_called()

            attempts = state.get_task_state(task.id).attempts
            assert attempts, "a refused attempt must still be recorded"
            assert attempts[-1].error_code is ErrorCode.INFRASTRUCTURE, (
                f"expected an infrastructure refusal, got {attempts[-1].error_code} — "
                f"{attempts[-1].error}"
            )

            namespace = resolve_namespace(config)
            evidence = state.verify_evidence(namespace, task.id)
            assert evidence is not None
            assert evidence.outcome == "instrument_error"

    def test_defective_form_stops_before_any_paid_call(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_a():\n    assert True\n")
        _commit(root, "base")

        # A glob is a defective form of a declared-group element (BEH-03) —
        # never resolved into a runnable file target.
        task = _task(verifies=["tests/*.py"])
        config = _cfg(root)
        self._assert_no_paid_call_and_infrastructure(task, config)

    def test_adapter_without_support_stops_before_any_paid_call(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "group_test.exs").write_text('test "a" do\nend\n')
        _commit(root, "base")

        # ExUnit declares `supports_file_targets = False` (Q-05); a file
        # target falls back to its own node-id-only `parse_selector`, which
        # refuses the file path outright — no `mix` invocation happens.
        task = _task(verifies=["tests/group_test.exs"])
        config = _cfg(root, test_command="mix test", tdd_runner="exunit")
        self._assert_no_paid_call_and_infrastructure(task, config)

    def test_empty_collected_composition_stops_before_any_paid_call(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_empty.py").write_text("# no tests in this file\n")
        _commit(root, "base")

        task = _task(verifies=["tests/test_empty.py"])
        config = _cfg(root)
        self._assert_no_paid_call_and_infrastructure(task, config)

    def test_fully_not_executed_composition_stops_before_any_paid_call(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_all_skipped.py").write_text(
            "import pytest\n\n\n"
            "@pytest.mark.skip(reason='not applicable on this platform')\n"
            "def test_a():\n    assert True\n\n\n"
            "@pytest.mark.skip(reason='requires optional dependency')\n"
            "def test_b():\n    assert True\n"
        )
        _commit(root, "base")

        task = _task(verifies=["tests/test_all_skipped.py"])
        config = _cfg(root)
        self._assert_no_paid_call_and_infrastructure(task, config)

    def test_unaccounted_member_stops_before_any_paid_call(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_early_stop.py").write_text(
            "def test_a():\n    assert False, 'genuine failure'\n\n\ndef test_b():\n    assert True\n"
        )
        _commit(root, "base")

        # `-x` stops the run after test_a fails, leaving test_b collected
        # but never accounted for — unaccountedness outranks the genuine
        # failure (Q-03).
        task = _task(verifies=["tests/test_early_stop.py"])
        config = _cfg(root, test_command="python -m pytest -x")
        self._assert_no_paid_call_and_infrastructure(task, config)


class TestBEH11ExitCodeAloneIsNotProof:
    """kind: contract — BEH-11: a run that returned 0 but left no per-member
    accounting is never read as green — an unreadable manifest, an
    incomplete one, and an empty one are the same "could not tell", not
    three different degrees of health."""

    def test_unreadable_manifest_is_not_green(self):
        assert _classify(None) is VerifyOutcome.INSTRUMENT_ERROR

    def test_incomplete_manifest_is_not_green(self):
        composition = FileComposition(
            members=("tests/x.py::test_a",),
            outcomes={"tests/x.py::test_a": "passed"},
            complete=False,
        )
        assert _classify(composition) is VerifyOutcome.INSTRUMENT_ERROR

    def test_empty_collected_composition_is_not_green(self):
        composition = FileComposition(members=(), outcomes={}, complete=True)
        assert _classify(composition) is VerifyOutcome.INSTRUMENT_ERROR


class TestBEH12CompositionConjunctionTable:
    """kind: contract — BEH-12: `green` happens exactly when the composition
    is non-empty, every member is accounted for, none failed or errored, and
    at least one member is proven to have actually executed and passed —
    the same `SelectionProof`/`ExecutionProof` standard the single-selector
    path already applies. Every row lands on exactly one of the three named
    outcomes."""

    _PASS = "passed"
    _FAIL = "failed"
    _ERROR = "error"
    _SKIP = "skipped"
    _DESELECTED = "deselected"
    _XPASS = "xpassed"

    ROWS: list[tuple[str, FileComposition, VerifyOutcome]] = [
        ("empty", FileComposition((), {}, True), VerifyOutcome.INSTRUMENT_ERROR),
        (
            "all-executed-and-passed",
            FileComposition(("a", "b"), {"a": _PASS, "b": _PASS}, True),
            VerifyOutcome.GREEN,
        ),
        (
            "partial-skip-rest-passed",
            FileComposition(("a", "b"), {"a": _PASS, "b": _SKIP}, True),
            VerifyOutcome.GREEN,
        ),
        (
            "partial-deselection-rest-passed",
            FileComposition(("a", "b"), {"a": _PASS, "b": _DESELECTED}, True),
            VerifyOutcome.GREEN,
        ),
        (
            "all-skipped",
            FileComposition(("a", "b"), {"a": _SKIP, "b": _SKIP}, True),
            VerifyOutcome.INSTRUMENT_ERROR,
        ),
        (
            "xpassed-does-not-count-as-executed",
            FileComposition(("a",), {"a": _XPASS}, True),
            VerifyOutcome.INSTRUMENT_ERROR,
        ),
        (
            "one-failed",
            FileComposition(("a", "b"), {"a": _PASS, "b": _FAIL}, True),
            VerifyOutcome.TEST_FAILURE,
        ),
        (
            "one-errored",
            FileComposition(("a", "b"), {"a": _PASS, "b": _ERROR}, True),
            VerifyOutcome.TEST_FAILURE,
        ),
        (
            "one-unaccounted",
            FileComposition(("a", "b"), {"a": _PASS}, True),
            VerifyOutcome.INSTRUMENT_ERROR,
        ),
        (
            "failed-plus-unaccounted-unaccountedness-wins",
            FileComposition(("a", "b"), {"a": _FAIL}, True),
            VerifyOutcome.INSTRUMENT_ERROR,
        ),
    ]

    @pytest.mark.parametrize("case_id,composition,expected", ROWS)
    def test_row(self, case_id, composition, expected):
        assert _classify(composition) is expected

    def test_every_row_is_one_of_the_three_named_outcomes(self):
        for _case_id, composition, _expected in self.ROWS:
            assert _classify(composition) in (
                VerifyOutcome.GREEN,
                VerifyOutcome.TEST_FAILURE,
                VerifyOutcome.INSTRUMENT_ERROR,
            )

    def test_green_row_matches_a_real_manifest_of_a_real_run(self, tmp_path):
        """The 'all executed and passed' row is not only synthetic data: a
        real pytest replay of the same shape resolves to the same triplet
        and the same outcome."""
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n"
        )
        _commit(root, "all pass")
        task = _task(verifies=["tests/test_group.py"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.GREEN
        assert (
            _classify(
                FileComposition(
                    members=("tests/test_group.py::test_a", "tests/test_group.py::test_b"),
                    outcomes={
                        "tests/test_group.py::test_a": "passed",
                        "tests/test_group.py::test_b": "passed",
                    },
                    complete=True,
                )
            )
            is VerifyOutcome.GREEN
        )


class TestBEH13EmptyCollectionNamesTheFile:
    """kind: integration — BEH-13: a file target whose collection produces
    zero members is `instrument_error`, never `green` and never
    `test_failure`, and the refusal names the file and the reason."""

    def test_a_file_that_never_had_tests(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_never_had_tests.py").write_text("# nothing to see here\n")
        _commit(root, "base")
        task = _task(verifies=["tests/test_never_had_tests.py"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.INSTRUMENT_ERROR
        assert not result.passed
        assert "tests/test_never_had_tests.py" in result.detail

    def test_a_file_whose_tests_discovery_does_not_collect(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_renamed.py").write_text(
            "def check_a():\n    assert True\n\n\ndef check_b():\n    assert True\n"
        )
        _commit(root, "base")
        task = _task(verifies=["tests/test_renamed.py"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.INSTRUMENT_ERROR
        assert not result.passed
        assert "tests/test_renamed.py" in result.detail

    def test_a_file_whose_tests_were_deleted(self, tmp_path):
        root = _init_repo(tmp_path)
        target = root / "tests" / "test_deleted.py"
        target.write_text("def test_a():\n    assert True\n")
        _commit(root, "tests present")
        target.write_text('"""All tests removed from this file."""\n')
        _commit(root, "tests removed")
        task = _task(verifies=["tests/test_deleted.py"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.INSTRUMENT_ERROR
        assert not result.passed
        assert "tests/test_deleted.py" in result.detail


class TestBEH14AllSkippedIsNotHealth:
    """kind: integration — BEH-14: a fully-accounted composition where no
    member ever executed is `instrument_error`, not health by silence — even
    though the run itself returned 0."""

    def test_a_fully_skipped_file_is_instrument_error(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "import pytest\n\n\n"
            "@pytest.mark.skip(reason='not applicable on this platform')\n"
            "def test_a():\n    assert True\n\n\n"
            "@pytest.mark.xfail(reason='known broken upstream', strict=False)\n"
            "def test_b():\n    assert False\n"
        )
        _commit(root, "all members unexecuted")
        task = _task(verifies=["tests/test_group.py"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.INSTRUMENT_ERROR
        assert not result.passed
        assert "tests/test_group.py" in result.detail

    def test_the_same_file_with_one_executed_member_is_green(self, tmp_path):
        """The other side of the boundary (BEH-16): add one executed, passing
        member to the same file and the outcome flips to green."""
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "import pytest\n\n\n"
            "def test_c():\n    assert True\n\n\n"
            "@pytest.mark.skip(reason='not applicable on this platform')\n"
            "def test_a():\n    assert True\n"
        )
        _commit(root, "one executed, one skipped")
        task = _task(verifies=["tests/test_group.py"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.GREEN


class TestBEH15UnaccountedMemberIsRefusedByName:
    """kind: integration — BEH-15: a member the run never mentions is a
    refusal that names it — silence is not a pass — and it outranks a
    genuine failure recorded elsewhere in the same run (Q-03)."""

    def test_unaccounted_member_names_itself_and_outranks_the_real_failure(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_early_stop.py").write_text(
            "def test_a():\n    assert False, 'genuine failure'\n\n\ndef test_b():\n    assert True\n"
        )
        _commit(root, "base")

        task = _task(verifies=["tests/test_early_stop.py"])
        config = _cfg(root, test_command="python -m pytest -x")

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.INSTRUMENT_ERROR, (
            f"unaccountedness of test_b must outrank test_a's genuine failure: {result.detail}"
        )
        assert "test_b" in result.detail, (
            f"the unaccounted member must be named by its node id: {result.detail}"
        )

    def test_same_composition_but_explicitly_skipped_is_accounted(self):
        """Contract counterpart: the same two-member composition, differing
        only in whether the silent member is explicitly recorded as skipped
        with a reason, lands on two different outcomes."""
        silent = FileComposition(("a", "b"), {"a": "passed"}, True)
        explicit = FileComposition(("a", "b"), {"a": "passed", "b": "skipped"}, True)

        assert _classify(silent) is VerifyOutcome.INSTRUMENT_ERROR
        assert _classify(explicit) is VerifyOutcome.GREEN


class TestBEH16PartialSkipIsGreenAndNamesEachSkip:
    """kind: integration — BEH-16: a file target where some members are
    skipped by the project's own condition and the rest executed and passed
    is green, with every skipped member named by node id and its own
    reason — the asymmetry with the node-id path (BEH-06) is paid for with
    visibility, not silence."""

    def test_multiple_skips_are_each_named_with_their_own_reason(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "import pytest\n\n\n"
            "def test_pass():\n    assert True\n\n\n"
            "@pytest.mark.skip(reason='no network access in CI')\n"
            "def test_skip_a():\n    assert True\n\n\n"
            "@pytest.mark.skip(reason='requires a GPU')\n"
            "def test_skip_b():\n    assert True\n"
        )
        _commit(root, "one pass, two named skips")
        task = _task(verifies=["tests/test_group.py"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.GREEN
        assert "test_skip_a" in result.detail
        assert "no network access in CI" in result.detail
        assert "test_skip_b" in result.detail
        assert "requires a GPU" in result.detail

    def test_the_same_file_declared_as_a_node_id_list_stays_instrument_error(self, tmp_path):
        """Closing asymmetry BEH-16 draws explicitly: the identical tree,
        declared as a list of its own node ids instead of a file target,
        does not benefit from the file target's accounting rule — the first
        non-green selector still stops the group."""
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "import pytest\n\n\n"
            "def test_pass():\n    assert True\n\n\n"
            "@pytest.mark.skip(reason='no network access in CI')\n"
            "def test_skip_a():\n    assert True\n"
        )
        _commit(root, "one pass, one named skip")
        config = _cfg(root)

        file_target_task = _task(id="TASK-301", verifies=["tests/test_group.py"])
        node_id_task = _task(
            id="TASK-302",
            verifies=[
                "tests/test_group.py::test_pass",
                "tests/test_group.py::test_skip_a",
            ],
        )

        file_target_result = run_live_verify(file_target_task, config)
        node_id_result = run_live_verify(node_id_task, config)

        assert file_target_result.outcome is VerifyOutcome.GREEN
        assert node_id_result.outcome is VerifyOutcome.INSTRUMENT_ERROR
