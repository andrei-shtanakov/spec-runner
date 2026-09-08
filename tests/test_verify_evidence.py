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

import json
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runner import tdd
from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.gates import AncestryUnknown, GateContext
from spec_runner.lifecycle import has_confirmed_red
from spec_runner.live_verify import VerifyRunResult, reusable_verify_evidence, run_live_verify
from spec_runner.runner import CliInvocation
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import AgentCall, environment_id, resolve_namespace


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
        config = _cfg(root, auto_commit=True)
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
        monkeypatch,
    ):
        root = _repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_it():\n    assert False, 'deliberate failure'\n"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "make it fail")
        task = _task()
        config = _cfg(root, auto_commit=True)
        state = ExecutorState(config)
        mock_run.return_value = MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)

        def _fake_red_agent(config, prompt, **kwargs):
            red_test = Path(config.project_root) / "tests" / "test_red_task101.py"
            red_test.write_text("def test_red_task101():\n    assert False, 'red'\n")
            return AgentCall(
                text="TDD_SELECTOR: tests/test_red_task101.py::test_red_task101\nTASK_COMPLETE"
            )

        monkeypatch.setattr(tdd, "_run_agent", _fake_red_agent)

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
        config = _cfg(root, test_command="python -m pytest tests/ && echo done", auto_commit=True)
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


class TestOnlyGreenEvidenceIsReusable:
    """kind: contract — a recorded verify-evidence row only answers a later
    task's question when its own outcome is green; a `test_failure` or
    `instrument_error` row must never be handed back by
    `reusable_verify_evidence` as "already answered, skip the live run"
    (#375 review, finding 2) — mirroring `_reusable_checkpoint`'s
    `RedOutcome.EXPECTED_FAIL`-only rule."""

    def test_a_test_failure_record_is_not_reusable(self, tmp_path):
        root = _repo(tmp_path)
        task = _task()
        config = _cfg(root)
        sha = _head(root)

        failed = VerifyRunResult(sha, True, False, "tests/test_group.py::test_it failed")
        state = ExecutorState(config)
        state.record_verify_evidence(task=task, config=config, result=failed)

        assert reusable_verify_evidence(config, state, task) is None, (
            "a test-failure evidence row was handed back as reusable"
        )
        state.close()

    def test_an_instrument_error_record_is_not_reusable(self, tmp_path):
        root = _repo(tmp_path)
        task = _task()
        config = _cfg(root)
        sha = _head(root)

        errored = VerifyRunResult(sha, False, False, "composite test_command")
        state = ExecutorState(config)
        state.record_verify_evidence(task=task, config=config, result=errored)

        assert reusable_verify_evidence(config, state, task) is None, (
            "an instrument-error evidence row was handed back as reusable"
        )
        state.close()


class TestDeclaredGroupIsStoredVerbatim:
    """kind: contract — BEH-21 (TASK-006/DT-06): the recorded
    `group_declared` is the declaration itself — a file target stays a file
    target, never replaced by its resolved composition — and a mixed group
    keeps the file target and node ids in their declared order."""

    def test_a_file_target_is_recorded_as_the_path_not_its_resolved_members(self, tmp_path):
        root = _repo(tmp_path)
        task = _task(verifies=["tests/test_group.py"])
        config = _cfg(root)

        result = run_live_verify(task, config)
        assert result.passed, result.detail

        state = ExecutorState(config)
        state.record_verify_evidence(task=task, config=config, result=result)
        namespace = resolve_namespace(config)
        evidence = state.verify_evidence(namespace, task.id)
        state.close()
        assert evidence is not None

        assert evidence.group_declared == ("tests/test_group.py",), (
            "BEH-21: group_declared must hold the file target as written, "
            f"not its resolved members, got {evidence.group_declared!r}"
        )

    def test_a_mixed_group_keeps_its_declared_order(self, tmp_path):
        root = _repo(tmp_path)
        (root / "tests" / "test_other.py").write_text("def test_other():\n    assert True\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "add second file")
        declared = ["tests/test_other.py::test_other", "tests/test_group.py"]
        task = _task(verifies=declared)
        config = _cfg(root)

        result = run_live_verify(task, config)
        assert result.passed, result.detail

        state = ExecutorState(config)
        state.record_verify_evidence(task=task, config=config, result=result)
        namespace = resolve_namespace(config)
        evidence = state.verify_evidence(namespace, task.id)
        state.close()
        assert evidence is not None

        assert evidence.group_declared == tuple(declared), (
            "BEH-21: a mixed group's declared order must survive round-trip, "
            f"got {evidence.group_declared!r}"
        )


class TestExistingEvidenceStaysReadable:
    """kind: contract — BEH-23 (TASK-006/DT-06): a verify-evidence row
    written by the delivered #367 version — before `composition` existed —
    is still read by the new version, and its old fields keep their old
    meaning; the migration that adds `composition` is additive only."""

    def test_a_pre_composition_row_reads_with_an_empty_composition(self, tmp_path):
        root = _repo(tmp_path)
        task = _task(id="TASK-901", verifies=["tests/test_group.py::test_it"])
        config = _cfg(root)
        namespace = resolve_namespace(config)
        sha = _head(root)
        config_hash = GateContext(task_id=task.id, checkpoint_sha=sha, config=config).config_hash
        env_id = environment_id(Path(config.project_root))
        declared = tuple(task.verifies or ())

        # The exact pre-#TASK-006 shape: `verify_evidence` with no
        # `composition` column at all, carrying a genuinely reusable row
        # (real HEAD, real config hash, real environment id) so the gate
        # check below exercises the real reuse path, not a stand-in.
        with sqlite3.connect(config.state_file) as conn:
            conn.execute(
                """
                CREATE TABLE verify_evidence (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    group_declared TEXT NOT NULL,
                    group_executed TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    environment_id TEXT NOT NULL,
                    adapter TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    detail TEXT,
                    actor TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO verify_evidence (task_id, namespace, commit_sha, "
                "group_declared, group_executed, config_hash, environment_id, "
                "adapter, outcome, detail, actor, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task.id,
                    namespace,
                    sha,
                    json.dumps(list(declared)),
                    json.dumps(list(declared)),
                    config_hash,
                    env_id,
                    "pytest",
                    "green",
                    "declared group passed: tests/test_group.py::test_it",
                    "harness",
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()

        # Opening with the new version must migrate additively — a table
        # that predates `composition` is not an error — and the old row
        # must still read back with its old fields intact.
        state = ExecutorState(config)
        evidence = state.verify_evidence(namespace, task.id)

        assert evidence is not None
        assert evidence.outcome == "green"
        assert evidence.group_declared == declared
        assert evidence.group_executed == declared
        assert getattr(evidence, "composition", None) == (), (
            "BEH-23: a pre-migration row's missing composition must read as empty, not as a defect"
        )

        # The pre-terminal gate answers the old row the same way it did
        # before composition existed — reuse is unaffected by the migration.
        reusable = reusable_verify_evidence(config, state, task)
        assert reusable is not None, (
            "BEH-23: the pre-terminal gate must still accept a pre-migration "
            "green row exactly as it did before composition existed"
        )
        state.close()
