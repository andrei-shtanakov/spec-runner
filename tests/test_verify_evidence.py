"""BEH-16, BEH-17, BEH-18, BEH-18a, BEH-19 (#367 milestone 1, TASK-007).

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-16 (-BEH-19)
Traces: FR-10, FR-11, FR-12, FR-21

BEH-15 itself (durability + full composition) is pinned by the frozen
`tests/test_task_007_55403b38f9226be0_14df0641_red.py`; this file covers the
rest of TASK-007's group: a third party reproducing a run from the recorded
evidence alone (BEH-16), the evidence going stale when the question it
answered changes — a `POLICY_KEYS` value (BEH-17), the declared group
(BEH-18), or the tree the candidate descends from (BEH-18a) — and green-only
evidence staying invisible to every reader that only knows about confirmed
reds (BEH-19).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.gates import AncestryUnknown, GateContext
from spec_runner.lifecycle import has_confirmed_red
from spec_runner.live_verify import VerifyRunResult, reusable_verify_evidence, run_live_verify
from spec_runner.runner import CliInvocation
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import environment_id, resolve_namespace


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _head(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_group.py").write_text("def test_it():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


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


class TestByEvidenceARunIsReproducedAndComparedByAThirdParty:
    """kind: integration — BEH-16: SHA, group, adapter and environment
    identity recorded in evidence are enough to reproduce the run without
    the original log, and config hash / environment identity are
    deterministic functions of the same input."""

    def test_third_party_reruns_the_group_from_the_recorded_fields_alone(self, tmp_path):
        root = _repo(tmp_path)
        task = _task()
        config = _cfg(root)

        first = run_live_verify(task, config)
        assert first.passed

        state = ExecutorState(config)
        state.record_verify_evidence(task=task, config=config, result=first)
        namespace = resolve_namespace(config)
        evidence = state.verify_evidence(namespace, task.id)
        state.close()
        assert evidence is not None

        # A "third party" holds nothing but the evidence row — not `first`,
        # not the original config object, not a log file — and reconstructs
        # a task + rerun purely from what BEH-15 says must be recorded.
        reproduced_task = _task(verifies=list(evidence.group_declared))
        reproduced_config = _cfg(root)
        rerun = run_live_verify(reproduced_task, reproduced_config)

        assert rerun.sha == evidence.commit_sha
        assert rerun.outcome.value == evidence.outcome
        assert rerun.passed == (evidence.outcome == "green")

    def test_environment_id_and_config_hash_are_deterministic(self, tmp_path):
        root = _repo(tmp_path)
        task = _task()
        config = _cfg(root)

        first_env = environment_id(Path(config.project_root))
        second_env = environment_id(Path(config.project_root))
        assert first_env == second_env

        first_hash = GateContext(task_id=task.id, checkpoint_sha=_head(root), config=config)
        second_hash = GateContext(task_id=task.id, checkpoint_sha=_head(root), config=config)
        assert first_hash.config_hash == second_hash.config_hash


class TestAPolicyKeyChangeDevaluesThePriorEvidence:
    """kind: integration — BEH-17: a `POLICY_KEYS` value changing (here
    `gate_recovery_attempts`, one of the declared keys) makes the recorded
    evidence unreusable — the same rule `_reusable_checkpoint` already
    applies to a red."""

    def test_a_policy_key_change_makes_the_evidence_unreusable(self, tmp_path):
        root = _repo(tmp_path)
        task = _task()
        config = _cfg(root)

        result = run_live_verify(task, config)
        assert result.passed

        state = ExecutorState(config)
        state.record_verify_evidence(task=task, config=config, result=result)

        # Unchanged policy: still reusable.
        assert reusable_verify_evidence(config, state, task) is not None

        changed = _cfg(root, gate_recovery_attempts=config.gate_recovery_attempts + 1)
        assert reusable_verify_evidence(changed, state, task) is None
        state.close()


class TestAChangedDeclaredGroupDevaluesThePriorEvidence:
    """kind: integration — BEH-18: the declared `**Verifies:**` group
    changing — composition or order — under an unchanged policy config
    makes the recorded evidence unreusable; a verdict about a different
    group answers a different question."""

    def test_a_different_group_composition_makes_the_evidence_unreusable(self, tmp_path):
        root = _repo(tmp_path)
        (root / "tests" / "test_other.py").write_text("def test_other():\n    assert True\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "add second test")
        task = _task(verifies=["tests/test_group.py::test_it", "tests/test_other.py::test_other"])
        config = _cfg(root)

        result = run_live_verify(task, config)
        assert result.passed

        state = ExecutorState(config)
        state.record_verify_evidence(task=task, config=config, result=result)

        assert reusable_verify_evidence(config, state, task) is not None

        smaller_group = _task(
            verifies=["tests/test_group.py::test_it"],
            execution_mode=task.execution_mode,
        )
        assert reusable_verify_evidence(config, state, smaller_group) is None
        state.close()

    def test_a_reordered_group_makes_the_evidence_unreusable(self, tmp_path):
        root = _repo(tmp_path)
        (root / "tests" / "test_other.py").write_text("def test_other():\n    assert True\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "add second test")
        declared = ["tests/test_group.py::test_it", "tests/test_other.py::test_other"]
        task = _task(verifies=declared)
        config = _cfg(root)

        result = run_live_verify(task, config)
        assert result.passed

        state = ExecutorState(config)
        state.record_verify_evidence(task=task, config=config, result=result)

        reordered = _task(verifies=list(reversed(declared)))
        assert reusable_verify_evidence(config, state, reordered) is None
        state.close()


class TestEvidenceOfAForeignTreeIsNotInherited:
    """kind: integration — BEH-18a: a candidate that does not descend from
    the evidence's commit gets a fresh live run rather than inheriting a
    green verdict about a different tree; unprovable ancestry is an
    `instrument-error` of class `AncestryUnknown`, the same rule the red
    gate applies to a red checkpoint."""

    def test_a_rewritten_history_does_not_inherit_the_evidence(self, tmp_path):
        root = _repo(tmp_path)
        task = _task()
        config = _cfg(root)

        result = run_live_verify(task, config)
        assert result.passed
        state = ExecutorState(config)
        state.record_verify_evidence(task=task, config=config, result=result)

        # Rewrite history: an orphan branch shares no ancestry with the
        # commit the evidence was recorded against.
        _git(root, "checkout", "--orphan", "rewritten")
        _git(root, "commit", "--allow-empty", "-qm", "rewritten history")

        assert reusable_verify_evidence(config, state, task) is None
        state.close()

    def test_unprovable_ancestry_raises_ancestry_unknown(self, tmp_path):
        root = _repo(tmp_path)
        task = _task()
        config = _cfg(root)

        bogus_sha = "0" * 40
        fake_result = VerifyRunResult(bogus_sha, True, True, "declared group passed: fake")
        state = ExecutorState(config)
        state.record_verify_evidence(task=task, config=config, result=fake_result)

        with pytest.raises(AncestryUnknown):
            reusable_verify_evidence(config, state, task)
        state.close()


class TestGreenOnlyDoesNotMasqueradeAsRed:
    """kind: contract — BEH-19: verify-evidence is a record distinct from a
    confirmed red checkpoint everywhere a red is presented; a reader of red
    checkpoints that does not know about verify-evidence answers "no red",
    not "confirmed red"."""

    def test_readers_of_confirmed_red_see_no_red_for_a_green_only_task(self, tmp_path):
        root = _repo(tmp_path)
        task = _task()
        config = _cfg(root)

        result = run_live_verify(task, config)
        assert result.passed

        state = ExecutorState(config)
        state.record_verify_evidence(task=task, config=config, result=result)
        namespace = resolve_namespace(config)

        # The evidence itself is there...
        assert state.verify_evidence(namespace, task.id) is not None
        # ...but nothing that reads the red-checkpoints table sees it.
        assert state.red_checkpoint(task.id, namespace) is None
        assert has_confirmed_red(state, namespace, task.id) is False
        state.close()


class TestEvidenceIsWrittenByRealExecutionNotOnlyByTests:
    """kind: integration — BEH-15: the evidence row must be a consequence of
    running a verify-first task through `execute_task`, not merely
    something a test can produce by calling `record_verify_evidence`
    directly. Covers all three outcomes (#375 review round N, finding 1)."""

    @patch("spec_runner.execution.update_task_status")
    @patch("spec_runner.execution.log_progress")
    @patch(
        "spec_runner.execution.build_cli_invocation",
        return_value=CliInvocation(["echo", "hi"], "text"),
    )
    @patch("spec_runner.execution.build_task_prompt", return_value="test prompt")
    @patch(
        "spec_runner.execution.post_done_hook",
        return_value=(True, None, "skipped", "", False),
    )
    @patch("spec_runner.execution.pre_start_hook", return_value=True)
    @patch("spec_runner.execution._run_agent_process")
    def test_a_green_execute_task_run_leaves_durable_evidence(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        tmp_path,
    ):
        root = _repo(tmp_path)
        task = _task()
        config = _cfg(root)
        state = ExecutorState(config)
        mock_run.return_value = MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)

        execute_task(task, config, state)

        namespace = resolve_namespace(config)
        evidence = state.verify_evidence(namespace, task.id)
        assert evidence is not None, (
            "execute_task's real verify-first path wrote no evidence row (BEH-15): "
            "record_verify_evidence is reachable only from tests today"
        )
        assert evidence.outcome == "green"
        state.close()

    @patch("spec_runner.execution.update_task_status")
    @patch("spec_runner.execution.log_progress")
    @patch(
        "spec_runner.execution.build_cli_invocation",
        return_value=CliInvocation(["echo", "hi"], "text"),
    )
    @patch("spec_runner.execution.build_task_prompt", return_value="test prompt")
    @patch(
        "spec_runner.execution.post_done_hook",
        return_value=(True, None, "skipped", "", False),
    )
    @patch("spec_runner.execution.pre_start_hook", return_value=True)
    @patch("spec_runner.execution._run_agent_process")
    def test_a_genuine_test_failure_run_leaves_durable_evidence(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        tmp_path,
    ):
        root = _repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_it():\n    assert False, 'deliberate failure'\n"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "make it fail")
        task = _task()
        config = _cfg(root)
        state = ExecutorState(config)
        mock_run.return_value = MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)

        execute_task(task, config, state)

        namespace = resolve_namespace(config)
        evidence = state.verify_evidence(namespace, task.id)
        assert evidence is not None, (
            "a genuine test-failure verify-first run left no evidence (BEH-15)"
        )
        assert evidence.outcome == "test_failure"
        state.close()

    @patch("spec_runner.execution.update_task_status")
    @patch("spec_runner.execution.log_progress")
    @patch("spec_runner.execution._run_agent_process")
    def test_an_instrument_error_run_leaves_durable_evidence(
        self, mock_run, mock_log, mock_status, tmp_path
    ):
        root = _repo(tmp_path)
        task = _task()
        config = _cfg(root, test_command="python -m pytest tests/ && echo done")
        state = ExecutorState(config)

        outcome = execute_task(task, config, state)

        assert outcome is False
        mock_run.assert_not_called()
        namespace = resolve_namespace(config)
        evidence = state.verify_evidence(namespace, task.id)
        assert evidence is not None, (
            "an instrument-error verify-first run left no evidence (BEH-15)"
        )
        assert evidence.outcome == "instrument_error"
        state.close()
