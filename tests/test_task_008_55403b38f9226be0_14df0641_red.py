"""BEH-21 (#367 TASK-008): `test-failure` sends the task into the ordinary,
unchanged TDD cycle.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-21
Traces: FR-14

kind: e2e — a real fixture repository, a real live verify run (real
`git`/`pytest` subprocesses), and a real `execute_task` call; only the two
paid agent calls (the implementation pass and the RED-authoring pass) are
stood in for, exactly as the sibling verify-first e2e tests
(`tests/test_verify_run_order.py`) already do for the implementation pass.

`test_verify_run_order.py::TestLiveRunFailurePath
::test_a_genuine_test_failure_is_observed_not_blocked` pins today's (pre-
TASK-008) behaviour in its own docstring: "until the green/test-failure/
instrument-error branching exists (#367 TASK-006/008), a genuine failure
must not block the task; it proceeds exactly as `standard` would". FR-14
retires that: a red declared group must now walk the task through the same
RED-authoring pass, checkpoint and replay that `tdd` mode already has —
"the ordinary TDD cycle... without a single loosening" — rather than
skipping straight to the paid implementation call with nothing recorded.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from spec_runner import tdd
from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.runner import CliInvocation
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import AgentCall, resolve_namespace


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
        "auto_commit": True,
        "run_review": False,
        "callback_url": "",
        "lint_command": "",
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
    }
    defaults.update(overrides)
    return Task(**defaults)


class TestRedGroupEntersTheOrdinaryRedAuthoringCycle:
    """kind: e2e — BEH-21: a declared group that is red on entry must be
    authored, committed and replayed like any `tdd` red — not silently
    skipped because the task's mode is `verify_first`."""

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
    def test_a_red_declared_group_produces_a_confirmed_red_checkpoint(
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
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_it():\n    assert False, 'not implemented'\n"
        )
        _commit(root, "base")

        # The paid implementation call — never allowed to be the ONLY thing
        # this task pays for when its declared group is red on entry.
        mock_run.return_value = MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)

        # Stand-in for the RED-authoring agent call (`tdd._run_agent`,
        # mirroring `tests/test_tdd_battle.py`'s own `_agent` helper): writes
        # a fresh failing test and reports its selector.
        red_calls: list[str] = []

        def _fake_red_agent(config, prompt, **kwargs):
            red_calls.append("red_authoring")
            red_test = Path(config.project_root) / "tests" / "test_red_task101.py"
            red_test.write_text("def test_red_task101():\n    assert False, 'red'\n")
            return AgentCall(
                text="TDD_SELECTOR: tests/test_red_task101.py::test_red_task101\nTASK_COMPLETE"
            )

        monkeypatch.setattr(tdd, "_run_agent", _fake_red_agent)

        task = _task(verifies=["tests/test_group.py::test_it"])
        config = _cfg(root)
        state = ExecutorState(config)

        execute_task(task, config, state)

        namespace = resolve_namespace(config)
        checkpoint = state.red_checkpoint(task.id, namespace)

        assert red_calls, (
            "BEH-21: the RED-authoring agent was never invoked — a red "
            "declared group must go through the ordinary TDD cycle, not "
            "straight to the implementation call"
        )
        assert checkpoint is not None, (
            "BEH-21: no red checkpoint was recorded for a task whose "
            "declared verify-first group was red on entry — the red-gate "
            "has nothing to confirm the same way it would for `tdd`"
        )
