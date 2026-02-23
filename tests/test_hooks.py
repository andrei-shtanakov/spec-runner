"""Tests for spec_runner.hooks module."""

from pathlib import Path
from unittest.mock import MagicMock, call, patch

from spec_runner.config import ExecutorConfig
from spec_runner.hooks import (
    build_review_prompt,
    get_main_branch,
    get_task_branch_name,
    post_done_hook,
    pre_start_hook,
)
from spec_runner.task import Task


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


def _make_config(**overrides) -> ExecutorConfig:
    """Create an ExecutorConfig for testing with sensible defaults."""
    defaults = {
        "project_root": Path("/tmp/test-project"),
        "create_git_branch": False,
        "main_branch": "",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


class TestGetTaskBranchName:
    """Tests for get_task_branch_name."""

    def test_basic_format(self):
        task = _make_task(task_id="TASK-001", name="Add login page")
        result = get_task_branch_name(task)
        assert result == "task/task-001-add-login-page"

    def test_truncates_long_names(self):
        task = _make_task(
            task_id="TASK-002",
            name="Implement the very long feature name that exceeds thirty characters limit",
        )
        result = get_task_branch_name(task)
        # The safe_name part (after lowering, replacing, truncating) is max 30 chars
        # Full result: "task/task-002-" + safe_name[:30]
        prefix = "task/task-002-"
        safe_part = result[len(prefix) :]
        assert len(safe_part) <= 30

    def test_replaces_slashes_in_name(self):
        task = _make_task(task_id="TASK-003", name="Fix auth/login flow")
        result = get_task_branch_name(task)
        # Slashes should be replaced with hyphens
        assert "/" not in result.split("/", 1)[1]  # after the "task/" prefix
        assert result == "task/task-003-fix-auth-login-flow"


class TestGetMainBranch:
    """Tests for get_main_branch."""

    def test_config_override(self):
        config = _make_config(main_branch="develop")
        result = get_main_branch(config)
        assert result == "develop"

    @patch("spec_runner.hooks.subprocess.run")
    def test_detects_from_remote_head(self, mock_run):
        config = _make_config(main_branch="")
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="refs/remotes/origin/main\n",
        )
        result = get_main_branch(config)
        assert result == "main"
        mock_run.assert_called_once_with(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )

    @patch("spec_runner.hooks.subprocess.run")
    def test_detects_remote_head_master(self, mock_run):
        config = _make_config(main_branch="")
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="refs/remotes/origin/master\n",
        )
        result = get_main_branch(config)
        assert result == "master"

    @patch("spec_runner.hooks.subprocess.run")
    def test_fallback_when_no_git(self, mock_run):
        config = _make_config(main_branch="")
        # All subprocess calls fail (no git, no branches, no current branch)
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
        )
        result = get_main_branch(config)
        assert result == "main"


class TestPreStartHook:
    """Tests for pre_start_hook."""

    @patch("spec_runner.hooks.subprocess.run")
    def test_calls_uv_sync(self, mock_run):
        task = _make_task()
        config = _make_config(create_git_branch=False)
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        result = pre_start_hook(task, config)

        assert result is True
        # First call should be uv sync
        first_call = mock_run.call_args_list[0]
        assert first_call == call(
            ["uv", "sync"],
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )

    @patch("spec_runner.hooks.subprocess.run")
    def test_creates_git_branch(self, mock_run):
        task = _make_task(task_id="TASK-005", name="Setup CI")
        config = _make_config(create_git_branch=True, main_branch="main")

        def side_effect(cmd, **kwargs):
            mock_result = MagicMock()
            if cmd == ["uv", "sync"]:
                mock_result.returncode = 0
                mock_result.stdout = ""
                mock_result.stderr = ""
            elif cmd == ["git", "rev-parse", "--git-dir"]:
                mock_result.returncode = 0
                mock_result.stdout = ".git"
            elif cmd == ["git", "rev-parse", "HEAD"]:
                mock_result.returncode = 0
                mock_result.stdout = "abc123"
            elif (
                cmd == ["git", "checkout", "main"]
                or cmd == ["git", "checkout", "--", "."]
                or cmd == ["git", "clean", "-fd", "--exclude=spec/"]
            ):
                mock_result.returncode = 0
            elif cmd == ["git", "rev-parse", "--verify", "task/task-005-setup-ci"]:
                # Branch does not exist yet
                mock_result.returncode = 1
                mock_result.stdout = ""
                mock_result.stderr = ""
            elif cmd == ["git", "checkout", "-b", "task/task-005-setup-ci"]:
                mock_result.returncode = 0
                mock_result.stdout = ""
                mock_result.stderr = ""
            else:
                mock_result.returncode = 0
                mock_result.stdout = ""
                mock_result.stderr = ""
            return mock_result

        mock_run.side_effect = side_effect

        result = pre_start_hook(task, config)

        assert result is True
        # Verify branch creation was attempted
        branch_calls = [
            c
            for c in mock_run.call_args_list
            if isinstance(c[0][0], list) and "checkout" in c[0][0] and "-b" in c[0][0]
        ]
        assert len(branch_calls) == 1
        assert "task/task-005-setup-ci" in branch_calls[0][0][0]

    @patch("spec_runner.hooks.subprocess.run")
    def test_returns_true_when_no_git_repo(self, mock_run):
        """When git rev-parse --git-dir fails, pre_start_hook returns True."""
        task = _make_task()
        config = _make_config(create_git_branch=True, main_branch="main")

        def side_effect(cmd, **kwargs):
            mock_result = MagicMock()
            if cmd == ["uv", "sync"]:
                mock_result.returncode = 0
                mock_result.stdout = ""
                mock_result.stderr = ""
            elif cmd == ["git", "rev-parse", "--git-dir"]:
                mock_result.returncode = 1
                mock_result.stdout = ""
            else:
                mock_result.returncode = 0
                mock_result.stdout = ""
                mock_result.stderr = ""
            return mock_result

        mock_run.side_effect = side_effect

        result = pre_start_hook(task, config)
        assert result is True


class TestNoBranchMode:
    """Verify hooks skip git ops when create_git_branch=False (parallel mode)."""

    @patch("spec_runner.hooks.subprocess.run")
    def test_pre_start_skips_branch_when_no_branch(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        config = ExecutorConfig(
            project_root=tmp_path,
            create_git_branch=False,
        )
        task = Task(id="TASK-001", name="Test", priority="p1", status="todo", estimate="1d")

        result = pre_start_hook(task, config)

        assert result is True
        # Git checkout/branch should not be called
        call_args = [str(c) for c in mock_run.call_args_list]
        assert not any("checkout" in c for c in call_args)

    @patch("spec_runner.hooks.subprocess.run")
    def test_post_done_skips_merge_when_no_branch(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        config = ExecutorConfig(
            project_root=tmp_path,
            create_git_branch=False,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=False,
            auto_commit=False,
        )
        task = Task(id="TASK-001", name="Test", priority="p1", status="todo", estimate="1d")

        success, error = post_done_hook(task, config, True)

        assert success is True
        call_args = [str(c) for c in mock_run.call_args_list]
        assert not any("merge" in c for c in call_args)


class TestBuildReviewPrompt:
    """Tests for build_review_prompt with enriched context."""

    def test_includes_task_checklist(self):
        task = _make_task()
        task.checklist = [
            ("Implement API endpoint", True),
            ("Add error handling", False),
            ("Write tests", False),
        ]
        config = _make_config()
        with patch("spec_runner.hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
            prompt = build_review_prompt(task, config)
        assert "Implement API endpoint" in prompt
        assert "Add error handling" in prompt

    def test_includes_test_output(self):
        task = _make_task()
        config = _make_config()
        with patch("spec_runner.hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
            prompt = build_review_prompt(
                task, config, test_output="15 passed, 0 failed in 2.1s"
            )
        assert "15 passed" in prompt

    def test_includes_previous_error(self):
        task = _make_task()
        config = _make_config()
        with patch("spec_runner.hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
            prompt = build_review_prompt(
                task, config, previous_error="TypeError: expected str"
            )
        assert "TypeError" in prompt

    def test_includes_lint_output(self):
        task = _make_task()
        config = _make_config()
        with patch("spec_runner.hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
            prompt = build_review_prompt(
                task, config, lint_output="All checks passed"
            )
        assert "All checks passed" in prompt

    def test_includes_full_diff(self):
        task = _make_task()
        config = _make_config()
        with patch("spec_runner.hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="diff --git a/foo.py b/foo.py\n+new line",
                stderr="",
                returncode=0,
            )
            prompt = build_review_prompt(task, config)
        assert "Full Diff" in prompt

    def test_no_extra_sections_when_no_context(self):
        task = _make_task()
        config = _make_config()
        with patch("spec_runner.hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
            prompt = build_review_prompt(task, config)
        assert "Task Checklist" not in prompt
        assert "Test Results" not in prompt
        assert "Lint Status" not in prompt
        assert "Previous Errors" not in prompt
