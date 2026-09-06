"""BEH-07/BEH-08/BEH-09 (#367 milestone 1, TASK-005): the live verify-first
run, its scope, and what commit it judges.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-07 (-BEH-09)
Traces: FR-05, FR-06, FR-07

kind: integration — every scenario here runs real `git`/`pytest` subprocesses
against a fixture repository; nothing about the group's outcome is mocked.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.live_verify import run_live_verify
from spec_runner.runner import CliInvocation
from spec_runner.state import ExecutorState
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
    }
    defaults.update(overrides)
    return Task(**defaults)


class TestLiveRunIsTheFirstAction:
    """kind: integration — BEH-07: the live run happens for real, before the
    first paid call, and no agent call is ever recorded ahead of it."""

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
    def test_no_paid_call_is_recorded_before_the_live_run_completes(
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
            "def test_marks_that_it_ran():\n    assert True\n"
        )
        _commit(root, "base")

        events: list[str] = []

        def _paid_call(*args, **kwargs):
            events.append("paid_call")
            return MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)

        mock_run.side_effect = _paid_call

        real_run_live_verify = run_live_verify

        def _tracked(*args, **kwargs):
            result = real_run_live_verify(*args, **kwargs)
            events.append("verify_evidence")
            return result

        monkeypatch.setattr("spec_runner.execution.run_live_verify", _tracked)

        task = _task(verifies=["tests/test_group.py::test_marks_that_it_ran"])
        config = _cfg(root)
        state = ExecutorState(config)

        execute_task(task, config, state)

        assert events, "the live verify run never happened"
        assert events[0] == "verify_evidence", (
            "an agent call was recorded before the verify-evidence: the ledger "
            f"order was {events!r}, and no call may precede the live run"
        )
        assert "paid_call" in events, "the paid call never ran either"


class TestLiveRunIsScopedToTheDeclaredGroup:
    """kind: integration — BEH-08: only the declared selectors execute, even
    on a default `test_command` that names the whole `tests/` directory."""

    def test_a_check_outside_the_group_never_runs(self, tmp_path):
        root = _init_repo(tmp_path)
        group_marker = tmp_path / "group_ran.txt"
        other_marker = tmp_path / "other_ran.txt"
        (root / "tests" / "test_group.py").write_text(
            "from pathlib import Path\n"
            "\n"
            "\n"
            "def test_in_group():\n"
            f"    Path({str(group_marker)!r}).write_text('ran')\n"
            "    assert True\n"
        )
        (root / "tests" / "test_other.py").write_text(
            "from pathlib import Path\n"
            "\n"
            "\n"
            "def test_outside_group():\n"
            f"    Path({str(other_marker)!r}).write_text('ran')\n"
            "    assert True\n"
        )
        _commit(root, "base")

        task = _task(verifies=["tests/test_group.py::test_in_group"])
        # The default shape FR-06 calls out explicitly: `test_command` already
        # names the directory, so a naive append would run the whole suite.
        config = _cfg(root, test_command="python -m pytest tests/")

        result = run_live_verify(task, config)

        assert result.passed, result.detail
        assert group_marker.exists(), "the declared selector itself never ran"
        assert not other_marker.exists(), (
            "a check outside the declared group executed: the live run must be "
            "restricted to the declared group, not the whole test_command scope"
        )


class TestLiveRunJudgesTheNamedCommit:
    """kind: integration — BEH-09: the verdict is about the commit named by
    HEAD when the live run starts, not about the working tree at that moment
    — a dirty tree that would flip the outcome must not change the verdict,
    and the same commit replayed again gives the same answer."""

    def test_a_dirty_working_tree_does_not_change_the_verdict(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        sha = _commit(root, "base")

        # Dirty the working tree with an uncommitted edit that would flip the
        # outcome if the live run judged the tree instead of the commit.
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert False\n")

        task = _task(verifies=["tests/test_group.py::test_it"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.sha == sha
        assert result.passed, (
            f"the verdict followed the dirty working tree instead of the named "
            f"commit {sha[:12]}: {result.detail}"
        )

        # The uncommitted edit is still sitting in the working tree, unrelated
        # to the replay just performed in its own worktree.
        assert (root / "tests" / "test_group.py").read_text() == (
            "def test_it():\n    assert False\n"
        )

        # Reproducible from the SHA: replaying the same commit again — even
        # with the tree still dirty — gives the same outcome.
        result_again = run_live_verify(task, config)
        assert result_again.sha == sha
        assert result_again.passed == result.passed
