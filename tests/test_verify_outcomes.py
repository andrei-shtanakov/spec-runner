"""BEH-10, BEH-11, BEH-12, BEH-13, BEH-14, BEH-23 (#367 milestone 1, TASK-006).

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-10 (-BEH-23)
Traces: FR-06, FR-07, FR-08, FR-09, FR-15, FR-16

`run_live_verify` (TASK-005) already refused a composite `test_command` and
an unresolvable adapter, and already read a verdict through the adapter's
own classify/prove_selected/execution_proven dictionary instead of a raw
exit code — but only through the `ran`/`passed` pair, the exact shape a
genuine `TESTS_FAILED` replay also produces (`ran=True, passed=False`) or
never distinguishes from a pre-run refusal (`ran=False, passed=False`).
This file pins the named third outcome (`VerifyOutcome.INSTRUMENT_ERROR`)
distinct from `green`/`test_failure`, the exhaustive classifier that
produces it (`classify_verify_outcome`), and the per-selector obligation
that a group's `green` verdict is not an aggregate one.
"""

from __future__ import annotations

import itertools
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from spec_runner import live_verify as live_verify_module
from spec_runner.config import ExecutorConfig
from spec_runner.live_verify import (
    VERIFY_GROUP_TIMEOUT_SECONDS,
    ExecutionProof,
    VerifyOutcome,
    classify_verify_outcome,
    run_live_verify,
)
from spec_runner.task import Task
from spec_runner.tdd_runners import RunOutcome, SelectionProof

ALL_RUN_OUTCOMES = list(RunOutcome)
ALL_SELECTION_PROOFS = list(SelectionProof)
ALL_EXECUTION_PROOFS = list(ExecutionProof)
ALL_COMBINATIONS = list(
    itertools.product(ALL_RUN_OUTCOMES, ALL_SELECTION_PROOFS, ALL_EXECUTION_PROOFS)
)


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


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    )
    return head.stdout.strip()


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
        "auto_commit": False,
        "run_review": False,
        "callback_url": "",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(**overrides) -> Task:
    defaults: dict = {
        "id": "TASK-101",
        "name": "verify-first task",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
        "execution_mode": "verify_first",
        "verifies": ["tests/test_group.py::test_it"],
    }
    defaults.update(overrides)
    return Task(**defaults)


class TestBEH10UnresolvableAdapterAndCompositeCommand:
    """kind: integration — BEH-10: both pre-run refusals report through the
    same named `instrument_error` outcome, and the message tells the two
    cases apart."""

    def test_composite_test_command_is_instrument_error(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")
        task = _task()
        config = _cfg(root, test_command="python -m pytest tests/ && echo done")

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.INSTRUMENT_ERROR
        assert not result.ran
        assert not result.passed
        assert "composite" in result.detail

    def test_unresolvable_adapter_is_instrument_error(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")
        task = _task()
        config = _cfg(root, test_command="some-unrecognizable-runner --frobnicate")

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.INSTRUMENT_ERROR
        assert not result.ran
        assert not result.passed
        assert "adapter" in result.detail

    def test_the_two_cases_are_distinguished_by_message(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")
        task = _task()

        composite = run_live_verify(task, _cfg(root, test_command="a && b"))
        unresolvable = run_live_verify(task, _cfg(root, test_command="some-unrecognizable-runner"))

        assert composite.detail != unresolvable.detail
        assert composite.outcome is unresolvable.outcome is VerifyOutcome.INSTRUMENT_ERROR


class TestBEH11GreenRequiresThreeFactsPerSelector:
    """kind: contract — BEH-11: `green` is produced only by the single
    combination where the run outcome, the selection proof, and the
    execution proof are ALL simultaneously satisfied; any one axis missing
    is not green, whatever the other two say."""

    def test_all_three_facts_together_is_green(self):
        result = classify_verify_outcome(
            RunOutcome.TESTS_PASSED, SelectionProof.PROVEN, ExecutionProof.EXECUTED
        )
        assert result is VerifyOutcome.GREEN

    @pytest.mark.parametrize(
        "run_outcome,proof,execution",
        [
            # Passed and proven, but never actually executed (skip/xfail).
            (RunOutcome.TESTS_PASSED, SelectionProof.PROVEN, ExecutionProof.NOT_EXECUTED),
            (RunOutcome.TESTS_PASSED, SelectionProof.PROVEN, ExecutionProof.UNDETERMINED),
            # Passed and executed, but the wrong test ran.
            (RunOutcome.TESTS_PASSED, SelectionProof.REFUTED, ExecutionProof.EXECUTED),
            (RunOutcome.TESTS_PASSED, SelectionProof.UNKNOWN, ExecutionProof.EXECUTED),
            # Proven and executed, but the run itself did not pass.
            (RunOutcome.TESTS_FAILED, SelectionProof.PROVEN, ExecutionProof.EXECUTED),
            (RunOutcome.SELECTION_FAILED, SelectionProof.PROVEN, ExecutionProof.EXECUTED),
        ],
    )
    def test_missing_any_one_fact_is_not_green(self, run_outcome, proof, execution):
        result = classify_verify_outcome(run_outcome, proof, execution)
        assert result is not VerifyOutcome.GREEN

    def test_a_refuted_selection_on_any_selector_is_not_green(self, tmp_path):
        """A real two-selector group: the first selector genuinely passes,
        the second names a test that does not exist. The aggregate command
        would look green if it were judged as a whole; per-selector
        obligation must still refuse it."""
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n"
        )
        _commit(root, "base")
        task = _task(
            verifies=[
                "tests/test_group.py::test_a",
                "tests/test_group.py::test_does_not_exist",
            ]
        )
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.INSTRUMENT_ERROR
        assert not result.passed


class TestBEH12PassedRunWithUnprovenSelectionIsNotGreen:
    """kind: integration — BEH-12: a live run that returns success is not
    `green` when the adapter could not prove which test executed
    (`SelectionProof.UNKNOWN`) — the run never reaches a paid call."""

    def test_unknown_selection_proof_is_instrument_error(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")
        task = _task()
        config = _cfg(root)

        real_prove_selected = live_verify_module.resolve_adapter(config).__class__.prove_selected

        def _unproven(self, selector, result):
            real = real_prove_selected(self, selector, result)
            if real is SelectionProof.PROVEN:
                return SelectionProof.UNKNOWN
            return real

        monkeypatch.setattr(
            live_verify_module.resolve_adapter(config).__class__,
            "prove_selected",
            _unproven,
        )

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.INSTRUMENT_ERROR
        assert not result.passed
        assert "did not prove which test executed" in result.detail


class TestBEH13EmptyAndSkippedSelectionIsNotHealth:
    """kind: integration — BEH-13: a selector that selects nothing, and a
    selector whose check is skipped, each in their own run, are both
    `instrument_error` — neither opens the green-only path."""

    def test_a_selection_matching_nothing_is_instrument_error(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")
        task = _task(verifies=["tests/test_group.py::test_does_not_exist"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.INSTRUMENT_ERROR
        assert not result.passed
        assert "test_does_not_exist" in result.detail

    def test_a_skipped_selection_is_instrument_error(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "import pytest\n"
            "\n"
            "\n"
            "@pytest.mark.skip(reason='not ready')\n"
            "def test_it():\n"
            "    assert True\n"
        )
        _commit(root, "base")
        task = _task(verifies=["tests/test_group.py::test_it"])
        # `-v` so the SKIPPED line carries the requested node id verbatim —
        # `prove_selected` reads PROVEN even though the test never executed.
        config = _cfg(root, test_command="python -m pytest -v")

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.INSTRUMENT_ERROR
        assert not result.passed
        assert "test_it" in result.detail
        assert "not executed" in result.detail


class TestBEH14GroupTimeoutCeilingIsDeclaredAndEnforced:
    """kind: integration — BEH-14: the group's time ceiling is a declared,
    named constant, and exceeding it refuses fail-closed as
    `instrument_error` rather than hanging the executor."""

    def test_the_ceiling_is_declared_ahead_of_time(self):
        assert isinstance(VERIFY_GROUP_TIMEOUT_SECONDS, int)
        assert VERIFY_GROUP_TIMEOUT_SECONDS > 0

    def test_exceeding_the_ceiling_is_instrument_error_not_a_hang(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")
        task = _task(verifies=["tests/test_group.py::test_it"])
        config = _cfg(root)

        # The group deadline is set, then the very first budget check finds
        # the clock already past it — the selector must never be started.
        past_deadline = VERIFY_GROUP_TIMEOUT_SECONDS + 1.0
        clock = iter([0.0, past_deadline])
        monkeypatch.setattr(
            live_verify_module.time, "monotonic", lambda: next(clock, past_deadline)
        )

        real_run = live_verify_module.subprocess.run
        ran_the_selector = False

        def _tracked_run(argv, **kwargs):
            nonlocal ran_the_selector
            if "timeout" in kwargs:
                ran_the_selector = True
            return real_run(argv, **kwargs)

        monkeypatch.setattr(live_verify_module.subprocess, "run", _tracked_run)

        result = run_live_verify(task, config)

        assert not ran_the_selector, "the selector ran despite an exhausted group budget"
        assert result.outcome is VerifyOutcome.INSTRUMENT_ERROR
        assert str(VERIFY_GROUP_TIMEOUT_SECONDS) in result.detail

    def test_a_real_subprocess_timeout_is_instrument_error(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "import time\n\n\ndef test_it():\n    time.sleep(5)\n"
        )
        _commit(root, "base")
        task = _task(verifies=["tests/test_group.py::test_it"])
        config = _cfg(root)

        # Force a tiny per-selector timeout by shrinking the group budget —
        # the sleeping test cannot finish inside it.
        original_ceiling = live_verify_module.VERIFY_GROUP_TIMEOUT_SECONDS
        try:
            live_verify_module.VERIFY_GROUP_TIMEOUT_SECONDS = 1
            result = run_live_verify(task, config)
        finally:
            live_verify_module.VERIFY_GROUP_TIMEOUT_SECONDS = original_ceiling

        assert result.outcome is VerifyOutcome.INSTRUMENT_ERROR
        assert "timed out" in result.detail or "budget" in result.detail


class TestBEH23ThreeOutcomesNoSilentFourth:
    """kind: contract — BEH-23: every combination of the three axes
    (`RunOutcome` x `SelectionProof` x `ExecutionProof`) lands in exactly
    one of the three named outcomes; nothing produces an unenumerated
    fourth value. Exhaustive by iterating the real Cartesian product, not a
    hand-picked list."""

    @pytest.mark.parametrize("run_outcome,proof,execution", ALL_COMBINATIONS)
    def test_every_combination_yields_one_of_the_three_named_outcomes(
        self, run_outcome, proof, execution
    ):
        result = classify_verify_outcome(run_outcome, proof, execution)
        assert result in (
            VerifyOutcome.GREEN,
            VerifyOutcome.TEST_FAILURE,
            VerifyOutcome.INSTRUMENT_ERROR,
        )

    def test_exactly_one_combination_is_green(self):
        counts = Counter(classify_verify_outcome(*combo) for combo in ALL_COMBINATIONS)
        assert counts[VerifyOutcome.GREEN] == 1

    def test_test_failure_requires_proven_selection_regardless_of_execution_axis(self):
        counts = Counter(classify_verify_outcome(*combo) for combo in ALL_COMBINATIONS)
        # TESTS_FAILED x PROVEN x {each ExecutionProof value}: the execution
        # axis is irrelevant once a genuine, attributable failure is seen.
        assert counts[VerifyOutcome.TEST_FAILURE] == len(ALL_EXECUTION_PROOFS)

    def test_unrecognized_run_outcome_is_never_green_or_test_failure(self):
        for proof, execution in itertools.product(ALL_SELECTION_PROOFS, ALL_EXECUTION_PROOFS):
            result = classify_verify_outcome(RunOutcome.UNRECOGNIZED, proof, execution)
            assert result is VerifyOutcome.INSTRUMENT_ERROR

    def test_passed_and_proven_without_proven_execution_is_instrument_error_not_green(self):
        """The explicit BEH-23 example: TESTS_PASSED x PROVEN without a
        proven execution is instrument_error, never green."""
        for execution in (ExecutionProof.NOT_EXECUTED, ExecutionProof.UNDETERMINED):
            result = classify_verify_outcome(
                RunOutcome.TESTS_PASSED, SelectionProof.PROVEN, execution
            )
            assert result is VerifyOutcome.INSTRUMENT_ERROR

    def test_everything_not_green_or_test_failure_is_instrument_error(self):
        counts = Counter(classify_verify_outcome(*combo) for combo in ALL_COMBINATIONS)
        total = len(ALL_COMBINATIONS)
        accounted_for = counts[VerifyOutcome.GREEN] + counts[VerifyOutcome.TEST_FAILURE]
        assert counts[VerifyOutcome.INSTRUMENT_ERROR] == total - accounted_for
        # And the mapping is total: no combination is left unclassified.
        assert sum(counts.values()) == total
