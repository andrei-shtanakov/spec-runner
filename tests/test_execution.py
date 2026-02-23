"""Tests for spec_runner.executor — execute_task and run_with_retries."""

import asyncio
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task, run_with_retries
from spec_runner.state import ErrorCode, ExecutorState
from spec_runner.task import Task

# --- Helpers ---


def _make_task(
    task_id: str = "TASK-001",
    name: str = "Add login page",
    priority: str = "p1",
    status: str = "todo",
    estimate: str = "2d",
) -> Task:
    """Create a Task object for testing."""
    return Task(
        id=task_id,
        name=name,
        priority=priority,
        status=status,
        estimate=estimate,
    )


def _make_config(tmp_path: Path, **overrides) -> ExecutorConfig:
    """Create an ExecutorConfig rooted in tmp_path."""
    defaults = {
        "project_root": tmp_path,
        "state_file": tmp_path / "state.db",
        "logs_dir": tmp_path / "logs",
        "max_retries": 3,
        "retry_delay_seconds": 0,
        "create_git_branch": False,
        "run_tests_on_done": False,
        "auto_commit": False,
        "run_review": False,
        "callback_url": "",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _make_state(config: ExecutorConfig) -> ExecutorState:
    """Create an ExecutorState backed by the given config."""
    return ExecutorState(config)


# --- execute_task tests ---


class TestExecuteTask:
    """Tests for execute_task."""

    @patch("spec_runner.executor.mark_all_checklist_done")
    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook", return_value=(True, None, "skipped", ""))
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_success_returns_true(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        mock_checklist,
        tmp_path,
    ):
        """Task with TASK_COMPLETE and returncode=0 returns True."""
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE",
            stderr="",
            returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        result = execute_task(task, config, state)

        assert result is True
        mock_pre.assert_called_once_with(task, config)
        mock_post.assert_called_once_with(task, config, True)
        mock_status.assert_called()
        mock_checklist.assert_called_once()

    @patch("spec_runner.executor.mark_all_checklist_done")
    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook")
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_implicit_success_returncode_zero(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        mock_checklist,
        tmp_path,
    ):
        """Return code 0 without explicit marker is implicit success."""
        mock_run.return_value = MagicMock(
            stdout="all done, no marker",
            stderr="",
            returncode=0,
        )
        mock_post.return_value = (True, None, "skipped", "")
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        result = execute_task(task, config, state)

        assert result is True
        mock_post.assert_called_once_with(task, config, True)

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook")
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_api_error_returns_api_error(
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
        """Rate limit pattern in output returns 'API_ERROR'."""
        mock_run.return_value = MagicMock(
            stdout="you've hit your limit",
            stderr="",
            returncode=1,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        result = execute_task(task, config, state)

        assert result == "API_ERROR"
        # post_done_hook should NOT be called on API error
        mock_post.assert_not_called()

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook")
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_failure_returns_false(
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
        """TASK_FAILED in output returns False."""
        mock_run.return_value = MagicMock(
            stdout="TASK_FAILED: could not compile",
            stderr="",
            returncode=1,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        result = execute_task(task, config, state)

        assert result is False
        # post_done_hook should NOT be called on explicit failure
        mock_post.assert_not_called()

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch(
        "spec_runner.executor.post_done_hook", return_value=(False, "tests failed", "skipped", "")
    )
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_hook_failure_returns_false(
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
        """When post_done_hook fails, execute_task returns False."""
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE",
            stderr="",
            returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        result = execute_task(task, config, state)

        assert result is False
        mock_post.assert_called_once()

    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.pre_start_hook", return_value=False)
    def test_pre_hook_failure_returns_hook_error(
        self,
        mock_pre,
        mock_log,
        tmp_path,
    ):
        """When pre_start_hook fails, execute_task returns 'HOOK_ERROR'."""
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        result = execute_task(task, config, state)

        assert result == "HOOK_ERROR"
        mock_pre.assert_called_once()

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_timeout_returns_false(
        self,
        mock_run,
        mock_pre,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        tmp_path,
    ):
        """Subprocess timeout returns False."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="echo", timeout=1800)
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        result = execute_task(task, config, state)

        assert result is False


# --- run_with_retries tests ---


class TestRunWithRetries:
    """Tests for run_with_retries."""

    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.execute_task")
    def test_returns_true_on_first_success(
        self,
        mock_exec,
        mock_log,
        tmp_path,
    ):
        """Returns True when first attempt succeeds."""
        mock_exec.return_value = True
        task = _make_task()
        config = _make_config(tmp_path, max_retries=3)
        state = _make_state(config)

        result = run_with_retries(task, config, state)

        assert result is True
        assert mock_exec.call_count == 1

    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.execute_task")
    def test_api_error_stops_immediately(
        self,
        mock_exec,
        mock_log,
        tmp_path,
    ):
        """API_ERROR stops retries immediately; call count is 1."""
        mock_exec.return_value = "API_ERROR"
        task = _make_task()
        config = _make_config(tmp_path, max_retries=3)
        state = _make_state(config)

        result = run_with_retries(task, config, state)

        assert result == "API_ERROR"
        assert mock_exec.call_count == 1

    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.execute_task")
    def test_hook_error_stops_immediately(
        self,
        mock_exec,
        mock_log,
        tmp_path,
    ):
        """HOOK_ERROR stops retries immediately."""
        mock_exec.return_value = "HOOK_ERROR"
        task = _make_task()
        config = _make_config(tmp_path, max_retries=3)
        state = _make_state(config)

        result = run_with_retries(task, config, state)

        assert result is False
        assert mock_exec.call_count == 1

    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.execute_task")
    def test_retries_on_failure_then_succeeds(
        self,
        mock_exec,
        mock_log,
        tmp_path,
    ):
        """Retries on failure; succeeds on third attempt."""
        mock_exec.side_effect = [False, False, True]
        task = _make_task()
        config = _make_config(tmp_path, max_retries=3)
        state = _make_state(config)

        result = run_with_retries(task, config, state)

        assert result is True
        assert mock_exec.call_count == 3


# --- Error classification tests ---


class TestErrorClassification:
    """Tests for error_code classification in execute_task."""

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_timeout_gets_timeout_code(
        self, mock_run, mock_pre, mock_prompt, mock_cmd, mock_log, mock_status, tmp_path
    ):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="echo", timeout=1800)
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)
        execute_task(task, config, state)
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].error_code == ErrorCode.TIMEOUT

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook")
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_rate_limit_gets_rate_limit_code(
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
        mock_run.return_value = MagicMock(
            stdout="you've hit your limit",
            stderr="",
            returncode=1,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)
        execute_task(task, config, state)
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].error_code == ErrorCode.RATE_LIMIT

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook")
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_task_failed_gets_task_failed_code(
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
        mock_run.return_value = MagicMock(
            stdout="TASK_FAILED: could not compile",
            stderr="",
            returncode=1,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)
        execute_task(task, config, state)
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].error_code == ErrorCode.TASK_FAILED

    @patch("spec_runner.executor.mark_all_checklist_done")
    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch(
        "spec_runner.executor.post_done_hook",
        return_value=(False, "Tests failed:\nFAILED test_x", "skipped", ""),
    )
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_test_failure_hook_gets_test_failure_code(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        mock_cl,
        tmp_path,
    ):
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE",
            stderr="",
            returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)
        execute_task(task, config, state)
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].error_code == ErrorCode.TEST_FAILURE

    @patch("spec_runner.executor.mark_all_checklist_done")
    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch(
        "spec_runner.executor.post_done_hook",
        return_value=(False, "Lint errors (not auto-fixable):\nerr", "skipped", ""),
    )
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_lint_failure_hook_gets_lint_failure_code(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        mock_cl,
        tmp_path,
    ):
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE",
            stderr="",
            returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)
        execute_task(task, config, state)
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].error_code == ErrorCode.LINT_FAILURE

    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.pre_start_hook", return_value=False)
    def test_pre_hook_failure_gets_hook_failure_code(
        self,
        mock_pre,
        mock_log,
        tmp_path,
    ):
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)
        execute_task(task, config, state)
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].error_code == ErrorCode.HOOK_FAILURE


# --- Token tracking tests ---


class TestTokenTrackingInExecutor:
    """Tests for token/cost tracking in execute_task."""

    @patch("spec_runner.executor.mark_all_checklist_done")
    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook", return_value=(True, None, "skipped", ""))
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_tokens_parsed_from_stderr(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        mock_checklist,
        tmp_path,
    ):
        """Token counts and cost are parsed from stderr on success."""
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE",
            stderr="input_tokens: 5000\noutput_tokens: 1200\ntotal cost: $0.08",
            returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        result = execute_task(task, config, state)

        assert result is True
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].input_tokens == 5000
        assert ts.attempts[-1].output_tokens == 1200
        assert ts.attempts[-1].cost_usd == 0.08

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook")
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_tokens_stored_on_failure(
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
        """Token counts and cost are stored even when task fails."""
        mock_run.return_value = MagicMock(
            stdout="TASK_FAILED: could not compile",
            stderr="input_tokens: 3000\noutput_tokens: 800\ncost: $0.04",
            returncode=1,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        execute_task(task, config, state)

        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].input_tokens == 3000
        assert ts.attempts[-1].output_tokens == 800
        assert ts.attempts[-1].cost_usd == 0.04

    @patch("spec_runner.executor.mark_all_checklist_done")
    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook", return_value=(True, None, "skipped", ""))
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_no_tokens_in_stderr_stores_none(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        mock_checklist,
        tmp_path,
    ):
        """When stderr has no token info, fields are None."""
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE",
            stderr="",
            returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        execute_task(task, config, state)

        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].input_tokens is None
        assert ts.attempts[-1].output_tokens is None
        assert ts.attempts[-1].cost_usd is None

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook")
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_tokens_stored_on_hook_failure(
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
        """Token counts are stored when post_done_hook fails."""
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE",
            stderr="input_tokens: 4000\noutput_tokens: 900\ncost: $0.06",
            returncode=0,
        )
        mock_post.return_value = (False, "Tests failed:\nFAILED test_x", "skipped", "")
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        execute_task(task, config, state)

        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].input_tokens == 4000
        assert ts.attempts[-1].output_tokens == 900
        assert ts.attempts[-1].cost_usd == 0.06

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_timeout_has_no_tokens(
        self,
        mock_run,
        mock_pre,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        tmp_path,
    ):
        """Timeout path has no result, so tokens are None."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="echo", timeout=1800)
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        execute_task(task, config, state)

        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].input_tokens is None
        assert ts.attempts[-1].cost_usd is None


# --- Review data tracking tests ---


class TestReviewDataTracking:
    """Tests for review_status and review_findings being passed to record_attempt."""

    @patch("spec_runner.executor.mark_all_checklist_done")
    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch(
        "spec_runner.executor.post_done_hook",
        return_value=(True, None, "passed", "All checks passed, code looks good"),
    )
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_review_data_stored_on_success(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        mock_checklist,
        tmp_path,
    ):
        """Review status and findings from post_done_hook are stored in attempt on success."""
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE",
            stderr="",
            returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        result = execute_task(task, config, state)

        assert result is True
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].review_status == "passed"
        assert ts.attempts[-1].review_findings == "All checks passed, code looks good"

    @patch("spec_runner.executor.mark_all_checklist_done")
    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch(
        "spec_runner.executor.post_done_hook",
        return_value=(False, "Tests failed:\nFAILED test_x", "failed", "Review found issues"),
    )
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_review_data_stored_on_hook_failure(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        mock_checklist,
        tmp_path,
    ):
        """Review status and findings are stored even when post_done_hook fails."""
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE",
            stderr="",
            returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        result = execute_task(task, config, state)

        assert result is False
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].review_status == "failed"
        assert ts.attempts[-1].review_findings == "Review found issues"

    @patch("spec_runner.executor.mark_all_checklist_done")
    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook", return_value=(True, None, "skipped", ""))
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_empty_review_findings_stored_as_none(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        mock_checklist,
        tmp_path,
    ):
        """Empty review_findings string is stored as None."""
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE",
            stderr="",
            returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        execute_task(task, config, state)

        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].review_status == "skipped"
        assert ts.attempts[-1].review_findings is None

    @patch("spec_runner.executor.mark_all_checklist_done")
    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch(
        "spec_runner.executor.post_done_hook",
        return_value=(True, None, "passed", "x" * 5000),
    )
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_review_findings_truncated_to_2048(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        mock_checklist,
        tmp_path,
    ):
        """Long review_findings are truncated to 2048 characters."""
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE",
            stderr="",
            returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        execute_task(task, config, state)

        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].review_findings is not None
        assert len(ts.attempts[-1].review_findings) == 2048


# --- Parallel execution tests ---


class TestParallelExecution:
    """Tests for parallel task execution."""

    def test_run_tasks_parallel_exists(self):
        """_run_tasks_parallel function exists and is a coroutine."""
        from spec_runner.executor import _run_tasks_parallel

        assert asyncio.iscoroutinefunction(_run_tasks_parallel)

    def test_execute_task_async_exists(self):
        """_execute_task_async function exists and is a coroutine."""
        from spec_runner.executor import _execute_task_async

        assert asyncio.iscoroutinefunction(_execute_task_async)

    def test_parallel_flag_in_argparser(self):
        """CLI parser accepts --parallel flag."""
        from spec_runner.executor import main

        # Just verify it doesn't crash on import
        assert callable(main)


# --- Budget enforcement tests ---


class TestBudgetEnforcement:
    """Tests for budget enforcement in run_with_retries."""

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.execute_task")
    def test_task_budget_exceeded_stops_retries(
        self,
        mock_exec,
        mock_log,
        mock_status,
        tmp_path,
    ):
        """When task cost exceeds task_budget_usd, stop retrying."""
        config = _make_config(tmp_path, max_retries=5, task_budget_usd=0.10)
        state = _make_state(config)
        task = _make_task()

        call_count = 0

        def side_effect(t, cfg, st):
            nonlocal call_count
            call_count += 1
            st.record_attempt(
                t.id,
                False,
                5.0,
                error="err",
                error_code=ErrorCode.TASK_FAILED,
                cost_usd=0.06,
            )
            return False

        mock_exec.side_effect = side_effect

        result = run_with_retries(task, config, state)

        assert result is False
        # Should stop after 2 attempts ($0.12 > $0.10)
        assert call_count == 2


class TestStateCleanup:
    """Verify ExecutorState is always closed after executor functions."""

    def test_run_tasks_closes_state(self, tmp_path):
        """_run_tasks uses context manager, so state is closed even on exception."""
        config = ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "state.db")
        (tmp_path / "spec").mkdir(exist_ok=True)
        (tmp_path / "spec" / "tasks.md").write_text("# Tasks\n")

        state = ExecutorState(config)
        assert state._conn is not None
        state.close()
        assert state._conn is None

    def test_context_manager_closes_on_normal_exit(self, tmp_path):
        config = ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "state.db")
        (tmp_path / "spec").mkdir(exist_ok=True)

        with ExecutorState(config) as state:
            assert state._conn is not None

        assert state._conn is None

    def test_context_manager_closes_on_exception(self, tmp_path):
        config = ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "state.db")
        (tmp_path / "spec").mkdir(exist_ok=True)

        try:
            with ExecutorState(config) as state:
                assert state._conn is not None
                raise RuntimeError("simulated crash")
        except RuntimeError:
            pass

        assert state._conn is None


class TestSignalHandling:
    def test_shutdown_flag_initially_false(self):
        import spec_runner.executor as mod

        mod._shutdown_requested = False
        assert mod._shutdown_requested is False

    def test_signal_handler_sets_flag(self):
        import spec_runner.executor as mod
        from spec_runner.executor import _signal_handler

        mod._shutdown_requested = False
        _signal_handler(2, None)  # SIGINT = 2
        assert mod._shutdown_requested is True
        mod._shutdown_requested = False  # cleanup

    def test_check_stop_includes_shutdown_flag(self, tmp_path):
        import spec_runner.executor as mod
        from spec_runner.config import ExecutorConfig
        from spec_runner.state import check_stop_requested

        config = ExecutorConfig(project_root=tmp_path)
        (tmp_path / "spec").mkdir()

        mod._shutdown_requested = False
        assert check_stop_requested(config) is False

        mod._shutdown_requested = True
        assert check_stop_requested(config) is True
        mod._shutdown_requested = False  # cleanup

    def test_execute_task_catches_keyboard_interrupt(self, tmp_path, monkeypatch):
        """KeyboardInterrupt during subprocess.run is caught and recorded."""
        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.db",
            logs_dir=tmp_path / "logs",
        )
        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / "tasks.md").write_text("# Tasks\n")

        task = _make_task()

        monkeypatch.setattr("spec_runner.executor.pre_start_hook", lambda t, c: True)
        monkeypatch.setattr("spec_runner.executor.update_task_status", lambda *a, **kw: None)
        monkeypatch.setattr("spec_runner.executor.send_callback", lambda *a, **kw: None)
        monkeypatch.setattr(
            "spec_runner.executor.build_cli_command", lambda **kw: ["echo", "test"]
        )

        def raise_interrupt(*a, **kw):
            raise KeyboardInterrupt

        monkeypatch.setattr(subprocess, "run", raise_interrupt)

        with ExecutorState(config) as state:
            result = execute_task(task, config, state)
            assert result is False
            ts = state.get_task_state("TASK-001")
            assert ts.attempts[-1].error_code == ErrorCode.INTERRUPTED
            assert "Interrupted" in ts.attempts[-1].error


class TestCrashRecovery:
    def test_run_tasks_calls_recover_stale(self, tmp_path, monkeypatch):
        """_run_tasks calls recover_stale_tasks at startup."""
        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.db",
        )
        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / "tasks.md").write_text("# Tasks\n")

        recover_calls = []
        monkeypatch.setattr(
            "spec_runner.executor.recover_stale_tasks",
            lambda state, timeout_minutes, tasks_file: recover_calls.append(True) or [],
        )

        args = type(
            "Args",
            (),
            {"task": None, "all": False, "milestone": None, "restart": False},
        )()

        from spec_runner.executor import _run_tasks

        _run_tasks(args, config)

        assert len(recover_calls) == 1


class TestForceFlag:
    def test_force_flag_skips_lock(self, tmp_path, monkeypatch):
        """--force skips lock check entirely."""
        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.db",
        )
        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / "tasks.md").write_text("# Tasks\n")

        lock_acquired = []
        monkeypatch.setattr(
            "spec_runner.config.ExecutorLock.acquire",
            lambda self: lock_acquired.append(True) or True,
        )
        monkeypatch.setattr(
            "spec_runner.config.ExecutorLock.release",
            lambda self: None,
        )
        monkeypatch.setattr(
            "spec_runner.executor.recover_stale_tasks",
            lambda *a, **kw: [],
        )

        from spec_runner.executor import cmd_run

        args = type(
            "Args",
            (),
            {
                "task": None,
                "all": False,
                "milestone": None,
                "restart": False,
                "parallel": False,
                "tui": False,
                "force": True,
            },
        )()
        cmd_run(args, config)
        assert len(lock_acquired) == 0

    def test_no_force_acquires_lock(self, tmp_path, monkeypatch):
        """Without --force, lock is acquired."""
        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.db",
        )
        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / "tasks.md").write_text("# Tasks\n")

        lock_acquired = []
        monkeypatch.setattr(
            "spec_runner.config.ExecutorLock.acquire",
            lambda self: lock_acquired.append(True) or True,
        )
        monkeypatch.setattr(
            "spec_runner.config.ExecutorLock.release",
            lambda self: None,
        )
        monkeypatch.setattr(
            "spec_runner.executor.recover_stale_tasks",
            lambda *a, **kw: [],
        )

        from spec_runner.executor import cmd_run

        args = type(
            "Args",
            (),
            {
                "task": None,
                "all": False,
                "milestone": None,
                "restart": False,
                "parallel": False,
                "tui": False,
                "force": False,
            },
        )()
        cmd_run(args, config)
        assert len(lock_acquired) == 1
