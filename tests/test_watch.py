"""Tests for cmd_watch() — watch mode polling loop (red phase).

Tests are written against the expected interface of cmd_watch() which
will be added to spec_runner.executor. Import will fail until the
function is implemented.
"""

from argparse import Namespace
from pathlib import Path
from unittest.mock import MagicMock, patch

from spec_runner.config import ExecutorConfig
from spec_runner.executor import cmd_watch
from spec_runner.state import ExecutorState
from spec_runner.task import Task


# --- Helpers ---


def _make_config(tmp_path: Path, **overrides) -> ExecutorConfig:
    """Create an ExecutorConfig rooted in tmp_path."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": spec_dir / ".executor-state.db",
        "logs_dir": spec_dir / ".executor-logs",
        "max_retries": 3,
        "max_consecutive_failures": 2,
        "retry_delay_seconds": 0,
        "create_git_branch": False,
        "run_tests_on_done": False,
        "auto_commit": False,
        "run_review": False,
        "callback_url": "",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _make_task(
    task_id: str = "TASK-001",
    name: str = "Login page",
    priority: str = "p0",
    status: str = "todo",
    estimate: str = "1d",
) -> Task:
    """Create a Task object for testing."""
    return Task(
        id=task_id,
        name=name,
        priority=priority,
        status=status,
        estimate=estimate,
    )


def _write_tasks(
    tasks_file: Path,
    tasks: list[tuple[str, str, str, str]],
) -> None:
    """Write tasks.md from a list of (id, name, priority, status) tuples."""
    priority_emoji = {
        "p0": "\U0001f534",
        "p1": "\U0001f7e0",
        "p2": "\U0001f7e1",
        "p3": "\U0001f7e2",
    }
    status_emoji = {
        "todo": "\u2b1c",
        "in_progress": "\U0001f504",
        "done": "\u2705",
        "blocked": "\u23f8\ufe0f",
    }
    lines = ["# Tasks\n"]
    for task_id, name, priority, status in tasks:
        p_emoji = priority_emoji.get(priority, "\U0001f534")
        s_emoji = status_emoji.get(status, "\u2b1c")
        lines.append(f"### {task_id}: {name}")
        lines.append(
            f"{p_emoji} {priority.upper()} | {s_emoji} {status.upper()} | Est: 1d"
        )
        lines.append("")
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text("\n".join(lines))


def _make_args(**overrides) -> Namespace:
    """Create an argparse.Namespace for cmd_watch."""
    defaults = {"tui": False}
    defaults.update(overrides)
    return Namespace(**defaults)


# --- Tests ---


class TestCmdWatch:
    """Tests for the cmd_watch polling loop."""

    @patch("spec_runner.executor.run_with_retries")
    @patch("spec_runner.executor.validate_all")
    @patch("spec_runner.executor.time")
    def test_executes_ready_task_then_stops(
        self,
        mock_time,
        mock_validate,
        mock_run,
        tmp_path: Path,
    ) -> None:
        """Execute one ready task, then stop via stop file."""
        config = _make_config(tmp_path)
        _write_tasks(
            config.tasks_file,
            [("TASK-001", "Login page", "p0", "todo")],
        )
        mock_validate.return_value = MagicMock(ok=True)
        mock_run.return_value = True
        mock_time.sleep = MagicMock()

        # Create stop file after run_with_retries completes
        def _create_stop(*args, **kwargs):
            config.stop_file.touch()
            return True

        mock_run.side_effect = _create_stop

        cmd_watch(_make_args(), config)

        assert mock_run.call_count == 1

    @patch("spec_runner.executor.run_with_retries")
    @patch("spec_runner.executor.validate_all")
    @patch("spec_runner.executor.time")
    def test_stops_on_stop_file(
        self,
        mock_time,
        mock_validate,
        mock_run,
        tmp_path: Path,
    ) -> None:
        """Stop file present before loop starts: run_with_retries never called."""
        config = _make_config(tmp_path)
        _write_tasks(
            config.tasks_file,
            [("TASK-001", "Login page", "p0", "todo")],
        )
        mock_validate.return_value = MagicMock(ok=True)
        mock_time.sleep = MagicMock()

        # Create stop file before calling cmd_watch
        config.stop_file.touch()

        cmd_watch(_make_args(), config)

        mock_run.assert_not_called()

    @patch("spec_runner.executor.run_with_retries")
    @patch("spec_runner.executor.validate_all")
    @patch("spec_runner.executor.time")
    def test_stops_on_consecutive_failures(
        self,
        mock_time,
        mock_validate,
        mock_run,
        tmp_path: Path,
    ) -> None:
        """Stop after max_consecutive_failures (2) consecutive failures."""
        config = _make_config(tmp_path, max_consecutive_failures=2)
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Login page", "p0", "todo"),
                ("TASK-002", "Signup page", "p1", "todo"),
                ("TASK-003", "Dashboard", "p2", "todo"),
            ],
        )
        mock_validate.return_value = MagicMock(ok=True)
        mock_time.sleep = MagicMock()
        mock_run.return_value = False

        cmd_watch(_make_args(), config)

        assert mock_run.call_count == 2

    @patch("spec_runner.executor.run_with_retries")
    @patch("spec_runner.executor.validate_all")
    @patch("spec_runner.executor.time")
    def test_resets_failures_on_success(
        self,
        mock_time,
        mock_validate,
        mock_run,
        tmp_path: Path,
    ) -> None:
        """Consecutive failure counter resets on success.

        Sequence: fail, success (reset), fail, fail -> stop.
        Total calls = 4.
        """
        config = _make_config(tmp_path, max_consecutive_failures=2)
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Login page", "p0", "todo"),
                ("TASK-002", "Signup page", "p1", "todo"),
                ("TASK-003", "Dashboard", "p2", "todo"),
                ("TASK-004", "Settings", "p3", "todo"),
            ],
        )
        mock_validate.return_value = MagicMock(ok=True)
        mock_time.sleep = MagicMock()
        mock_run.side_effect = [False, True, False, False]

        cmd_watch(_make_args(), config)

        assert mock_run.call_count == 4

    @patch("spec_runner.executor.run_with_retries")
    @patch("spec_runner.executor.validate_all")
    @patch("spec_runner.executor.time")
    def test_validation_failure_stops(
        self,
        mock_time,
        mock_validate,
        mock_run,
        tmp_path: Path,
    ) -> None:
        """Validation failure prevents entering the watch loop."""
        config = _make_config(tmp_path)
        _write_tasks(
            config.tasks_file,
            [("TASK-001", "Login page", "p0", "todo")],
        )
        mock_validate.return_value = MagicMock(ok=False, errors=["bad task"])
        mock_time.sleep = MagicMock()

        cmd_watch(_make_args(), config)

        mock_run.assert_not_called()

    @patch("spec_runner.executor.run_with_retries")
    @patch("spec_runner.executor.validate_all")
    @patch("spec_runner.executor.time")
    def test_polls_when_no_tasks_ready(
        self,
        mock_time,
        mock_validate,
        mock_run,
        tmp_path: Path,
    ) -> None:
        """No ready tasks: polls with sleep, never calls run_with_retries."""
        config = _make_config(tmp_path)
        # All tasks are done — nothing to execute
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Login page", "p0", "done"),
                ("TASK-002", "Signup page", "p1", "done"),
            ],
        )
        mock_validate.return_value = MagicMock(ok=True)

        # Create stop file after 2 sleep calls to break the loop
        call_count = 0

        def _sleep_side_effect(seconds):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                config.stop_file.touch()

        mock_time.sleep = MagicMock(side_effect=_sleep_side_effect)

        cmd_watch(_make_args(), config)

        mock_run.assert_not_called()
        assert mock_time.sleep.call_count >= 2
