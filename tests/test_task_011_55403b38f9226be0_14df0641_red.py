"""BEH-30 (#367 TASK-011): the live verify-first run gets its own stage.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-30 (-BEH-31)
Traces: FR-22

kind: integration — real `git` subprocesses against a fixture repository, a
real `execute_task` call; only the paid agent call is stood in for (there is
none to make here — a composite `test_command` refuses before any live run
starts, exactly like `tests/test_verify_run_order.py`'s own instrument-error
scenario).

Today `_run_verify_first_phase` (execution.py) reports the live run under the
`"tests"` stage — the same name the post-done test stage uses. FR-22 requires
a stage of its own: an operator reading `error_stage` on a failed attempt must
be able to tell "failed on the verify-first live run" (a fresh commit, judged
before any paid call) apart from "failed on post-done tests" (the candidate
tree, after the implementation pass) — two different questions about two
different trees, per BEH-30's Then clause. This test pins the live-run
instrument-error path recording `error_stage == "verify"`, not `"tests"`.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.state import ErrorCode, ExecutorState
from spec_runner.task import Task


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
        "test_command": "pytest tests/ && echo done",  # composite: refuses before running
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


class TestBEH30LiveVerifyHasItsOwnStage:
    """kind: integration — BEH-30 Given a verify-first task whose live run
    ends in instrument-error (composite `test_command`, refused before any
    selector runs). Then the failure is recorded under a stage of its own,
    not the post-done `tests` stage."""

    @patch("spec_runner.execution.update_task_status")
    @patch("spec_runner.execution.log_progress")
    @patch("spec_runner.execution._run_agent_process")
    def test_an_instrument_error_live_run_is_not_recorded_as_the_tests_stage(
        self, mock_run, mock_log, mock_status, tmp_path
    ):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")

        task = _task()
        config = _cfg(root)
        state = ExecutorState(config)

        outcome = execute_task(task, config, state)

        assert outcome is False
        mock_run.assert_not_called()
        attempts = state.get_task_state(task.id).attempts
        assert attempts[-1].error_code == ErrorCode.INFRASTRUCTURE
        assert attempts[-1].error_stage != "tests", (
            "the verify-first live run recorded its instrument-error under "
            "the post-done 'tests' stage; BEH-30/FR-22 requires a stage of "
            "its own so 'failed on the live verify run' cannot be confused "
            "with 'failed on post-done tests' — two different questions "
            "about two different trees"
        )
        assert attempts[-1].error_stage == "verify"
