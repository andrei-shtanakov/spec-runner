"""RED for BEH-07: the live verify run is the task's first action, before
any paid agent call.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-07
Traces: FR-05

Given a `verify_first` task in a fixture repository whose tree is already at
a known committed state (the `branch` stage and `rescue_uncommitted` are
mocked away here, standing in for "already ran"), the first thing
`execute_task` does must be a *real* live run of the declared group
(`tests/test_group.py::test_marks_that_it_ran`) — not a claim, an actual
subprocess proof, recorded before the first paid call reaches
`_run_agent_process`.

Today `execute_task` only branches on `execution_mode == "tdd"`; a
`verify_first` task falls straight through to the paid implementation call
with no live run at all. So the declared test never executes, its marker
file is never written, and this fails on a plain missing-file assertion —
not an import error, not a crash.
"""

import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.runner import CliInvocation
from spec_runner.state import ExecutorState
from spec_runner.task import Task

TEST_GROUP_BODY = (
    "import os\n"
    "import time\n"
    "from pathlib import Path\n"
    "\n"
    "\n"
    "def test_marks_that_it_ran():\n"
    "    marker = os.environ.get('VERIFY_RUN_MARKER')\n"
    "    if marker:\n"
    "        Path(marker).write_text(str(time.time()))\n"
    "    assert True\n"
)


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
    (tests_dir / "test_group.py").write_text(TEST_GROUP_BODY)
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


class TestLiveVerifyRunIsTheFirstAction:
    """kind: integration — BEH-07: under `verify_first`, the declared group's
    live run happens for real, and completes before the first paid call."""

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
    def test_the_declared_group_runs_for_real_before_the_first_paid_call(
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
        marker = tmp_path / "verify_ran_at.txt"
        monkeypatch.setenv("VERIFY_RUN_MARKER", str(marker))

        paid_call_started_at: list[float] = []

        def _paid_call(*args, **kwargs):
            paid_call_started_at.append(time.time())
            return MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)

        mock_run.side_effect = _paid_call

        task = Task(
            id="TASK-101",
            name="verify-first task",
            priority="p1",
            status="todo",
            estimate="1h",
            execution_mode="verify_first",
            verifies=["tests/test_group.py::test_marks_that_it_ran"],
        )
        config = _cfg(root, auto_commit=True)
        state = ExecutorState(config)

        execute_task(task, config, state)

        assert marker.exists(), (
            "the declared group never ran for real: under verify_first the "
            "live run of tests/test_group.py::test_marks_that_it_ran must be "
            "the task's first action, before the paid implementation call"
        )
        verify_ran_at = float(marker.read_text())
        assert paid_call_started_at, "the paid call never ran either"
        assert verify_ran_at < paid_call_started_at[0], (
            "the live verify run must complete before the first paid agent call, not after it"
        )
