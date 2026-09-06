"""RED for BEH-15: verify-first live-run evidence must be durable in `state`
and carry the full declared composition — not merely observed in-process by
`run_live_verify`'s caller.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-15
Traces: FR-10

`run_live_verify` already produces a real `VerifyRunResult` for a green
group (#367 TASK-005/006) — but nothing today writes that outcome into
`ExecutorState`, so it never survives the process. `ExecutorState` has no
method to record or read a verify-evidence row at all: this fails on a plain
missing-attribute assertion (`getattr(..., None)`), not an import error, not
a crash — the same shape TASK-006's red used for a field that did not exist
yet on `VerifyRunResult`.
"""

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.gates import GateContext
from spec_runner.live_verify import run_live_verify
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import environment_id, resolve_namespace


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


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


class TestVerifyEvidenceIsDurableAndCarriesTheFullDeclaredComposition:
    """kind: contract — BEH-15: after a live verify-first run, the state
    (reopened fresh, never the in-process object) must carry a record naming
    the task/workstream identity, the judged commit, the group as declared
    and as executed, the policy config hash, the environment identity, the
    adapter name, the outcome, and the harness as the record's author."""

    def test_a_green_run_leaves_a_durable_evidence_row_state_can_read_back(self, tmp_path):
        root = _repo(tmp_path)
        task = _task()
        config = _cfg(root)

        result = run_live_verify(task, config)
        assert result.passed, f"fixture group must be green to test BEH-15: {result!r}"

        expected_namespace = resolve_namespace(config)
        expected_environment_id = environment_id(config.project_root)
        expected_config_hash = GateContext(
            task_id=task.id, checkpoint_sha=result.sha, config=config
        ).config_hash

        state = ExecutorState(config)
        record = getattr(state, "record_verify_evidence", None)
        assert record is not None, (
            "ExecutorState has no record_verify_evidence method yet (BEH-15/FR-10): "
            "a live verify-first outcome must be written into a durable state row, "
            "the same way a RED replay durably writes a red_checkpoints row"
        )
        record(task=task, config=config, result=result)
        state.close()

        # Durability (BEH-15's own clause): read back from a freshly-opened
        # state, never the in-process object that ran the verify.
        reopened = ExecutorState(config)
        read = getattr(reopened, "verify_evidence", None)
        assert read is not None, (
            "ExecutorState has no verify_evidence reader yet (BEH-15/FR-10): "
            "the record must be readable after the process restarts, not merely "
            "held in memory by whoever recorded it"
        )
        evidence = read(expected_namespace, task.id)
        assert evidence is not None, (
            f"no durable verify-evidence row for namespace={expected_namespace!r} "
            f"task_id={task.id!r} — the record either was not written or was not "
            "keyed by task+workstream identity as BEH-15 requires"
        )
        assert evidence.task_id == task.id
        assert evidence.namespace == expected_namespace
        assert evidence.commit_sha == result.sha
        assert list(evidence.group_declared) == list(task.verifies)
        assert list(evidence.group_executed) == list(task.verifies)
        assert evidence.config_hash == expected_config_hash
        assert evidence.environment_id == expected_environment_id
        assert evidence.adapter == "pytest"
        assert evidence.outcome == "green"
        assert evidence.actor == "harness"
        assert evidence.timestamp, "evidence must carry when the run happened"
        reopened.close()
