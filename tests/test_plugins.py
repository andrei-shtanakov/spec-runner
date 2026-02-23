"""Tests for spec_runner.plugins module."""

import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from spec_runner.plugins import (
    PluginHook,
    build_task_env,
    discover_plugins,
    run_plugin_hooks,
)


def _create_plugin(plugins_dir: Path, name: str, hooks: dict) -> Path:
    """Helper to create a plugin directory with a manifest."""
    plugin_dir = plugins_dir / name
    plugin_dir.mkdir(parents=True)
    manifest = {
        "name": name,
        "description": f"Test plugin {name}",
        "version": "1.0",
        "hooks": hooks,
    }
    (plugin_dir / "plugin.yaml").write_text(yaml.dump(manifest))
    return plugin_dir


class TestDiscoverPlugins:
    """Tests for discover_plugins()."""

    def test_no_plugins_dir(self, tmp_path: Path) -> None:
        """Non-existent directory returns empty list."""
        result = discover_plugins(tmp_path / "nonexistent")
        assert result == []

    def test_empty_plugins_dir(self, tmp_path: Path) -> None:
        """Empty directory returns empty list."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()
        result = discover_plugins(plugins_dir)
        assert result == []

    def test_discover_single_plugin(self, tmp_path: Path) -> None:
        """Single plugin with post_done hook is discovered."""
        plugins_dir = tmp_path / "plugins"
        _create_plugin(
            plugins_dir,
            "notify-slack",
            {"post_done": {"command": "./on_done.sh", "run_on": "on_success"}},
        )

        result = discover_plugins(plugins_dir)

        assert len(result) == 1
        plugin = result[0]
        assert plugin.name == "notify-slack"
        assert plugin.description == "Test plugin notify-slack"
        assert plugin.version == "1.0"
        assert plugin.path == plugins_dir / "notify-slack"
        assert "post_done" in plugin.hooks
        hook = plugin.hooks["post_done"]
        assert isinstance(hook, PluginHook)
        assert hook.command == "./on_done.sh"
        assert hook.run_on == "on_success"

    def test_discover_multiple_sorted(self, tmp_path: Path) -> None:
        """Multiple plugins are returned sorted alphabetically by name."""
        plugins_dir = tmp_path / "plugins"
        _create_plugin(
            plugins_dir,
            "zebra-plugin",
            {"post_done": {"command": "./z.sh"}},
        )
        _create_plugin(
            plugins_dir,
            "alpha-plugin",
            {"pre_start": {"command": "./a.sh"}},
        )

        result = discover_plugins(plugins_dir)

        assert len(result) == 2
        assert result[0].name == "alpha-plugin"
        assert result[1].name == "zebra-plugin"

    def test_skip_dir_without_manifest(self, tmp_path: Path) -> None:
        """Directory without plugin.yaml is skipped."""
        plugins_dir = tmp_path / "plugins"
        plugins_dir.mkdir()

        # Valid plugin
        _create_plugin(
            plugins_dir,
            "valid-plugin",
            {"post_done": {"command": "./run.sh"}},
        )

        # Directory without manifest
        (plugins_dir / "no-manifest").mkdir()

        result = discover_plugins(plugins_dir)

        assert len(result) == 1
        assert result[0].name == "valid-plugin"

    def test_plugin_hook_defaults(self, tmp_path: Path) -> None:
        """PluginHook defaults: run_on='always', blocking=False."""
        plugins_dir = tmp_path / "plugins"
        _create_plugin(
            plugins_dir,
            "minimal-plugin",
            {"post_done": {"command": "./run.sh"}},
        )

        result = discover_plugins(plugins_dir)

        assert len(result) == 1
        hook = result[0].hooks["post_done"]
        assert hook.run_on == "always"
        assert hook.blocking is False


class TestRunPluginHooks:
    """Tests for run_plugin_hooks()."""

    def _make_script(self, plugin_dir: Path, name: str, content: str) -> Path:
        script = plugin_dir / name
        script.write_text(content)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    def test_run_post_done_hook(self, tmp_path: Path) -> None:
        """Hook command is executed and success is reported."""
        plugins_dir = tmp_path / "spec" / "plugins"
        plugin_dir = _create_plugin(
            plugins_dir,
            "test-plugin",
            {"post_done": {"command": "./done.sh"}},
        )
        self._make_script(plugin_dir, "done.sh", "#!/bin/bash\necho OK")
        plugins = discover_plugins(plugins_dir)

        results = run_plugin_hooks("post_done", plugins, task_env={"SR_TASK_ID": "TASK-001"})

        assert len(results) == 1
        assert results[0][0] == "test-plugin"
        assert results[0][1] is True

    def test_skip_on_run_on_filter(self, tmp_path: Path) -> None:
        """Hook with run_on=on_success is skipped when status is failed."""
        plugins_dir = tmp_path / "spec" / "plugins"
        _create_plugin(
            plugins_dir,
            "success-only",
            {"post_done": {"command": "./done.sh", "run_on": "on_success"}},
        )
        plugins = discover_plugins(plugins_dir)

        results = run_plugin_hooks(
            "post_done",
            plugins,
            task_env={"SR_TASK_ID": "TASK-001", "SR_TASK_STATUS": "failed"},
        )

        assert len(results) == 0  # skipped

    def test_env_vars_passed(self, tmp_path: Path) -> None:
        """Environment variables from task_env are passed to hook subprocess."""
        plugins_dir = tmp_path / "spec" / "plugins"
        plugin_dir = _create_plugin(
            plugins_dir,
            "env-check",
            {"post_done": {"command": "./check_env.sh"}},
        )
        marker = tmp_path / "env_marker.txt"
        self._make_script(
            plugin_dir,
            "check_env.sh",
            f"#!/bin/bash\necho $SR_TASK_ID > {marker}",
        )
        plugins = discover_plugins(plugins_dir)

        run_plugin_hooks("post_done", plugins, task_env={"SR_TASK_ID": "TASK-042"})

        assert marker.read_text().strip() == "TASK-042"

    def test_blocking_failure_reported(self, tmp_path: Path) -> None:
        """Blocking hook failure is reported with blocking=True."""
        plugins_dir = tmp_path / "spec" / "plugins"
        plugin_dir = _create_plugin(
            plugins_dir,
            "blocker",
            {"post_done": {"command": "./fail.sh", "blocking": True}},
        )
        self._make_script(plugin_dir, "fail.sh", "#!/bin/bash\nexit 1")
        plugins = discover_plugins(plugins_dir)

        results = run_plugin_hooks("post_done", plugins, task_env={"SR_TASK_ID": "TASK-001"})

        assert len(results) == 1
        assert results[0][1] is False  # failure
        assert results[0][2] is True  # blocking


class TestBuildTaskEnv:
    """Tests for build_task_env()."""

    def test_success_status(self) -> None:
        """success=True produces SR_TASK_STATUS=success."""
        from spec_runner.config import ExecutorConfig
        from spec_runner.task import Task

        task = Task(id="TASK-010", name="My Task", priority="p1", status="todo", estimate="1d")
        config = ExecutorConfig(project_root=Path("/tmp/proj"))
        env = build_task_env(task, config, success=True)
        assert env["SR_TASK_ID"] == "TASK-010"
        assert env["SR_TASK_NAME"] == "My Task"
        assert env["SR_TASK_STATUS"] == "success"
        assert env["SR_TASK_PRIORITY"] == "p1"
        assert env["SR_PROJECT_ROOT"] == str(config.project_root)

    def test_failure_status(self) -> None:
        """success=False produces SR_TASK_STATUS=failed."""
        from spec_runner.config import ExecutorConfig
        from spec_runner.task import Task

        task = Task(id="TASK-011", name="Broken", priority="p0", status="todo", estimate="1d")
        config = ExecutorConfig(project_root=Path("/tmp/proj"))
        env = build_task_env(task, config, success=False)
        assert env["SR_TASK_STATUS"] == "failed"

    def test_pending_status(self) -> None:
        """success=None produces SR_TASK_STATUS=pending."""
        from spec_runner.config import ExecutorConfig
        from spec_runner.task import Task

        task = Task(id="TASK-012", name="Pending", priority="p2", status="todo", estimate="1d")
        config = ExecutorConfig(project_root=Path("/tmp/proj"))
        env = build_task_env(task, config, success=None)
        assert env["SR_TASK_STATUS"] == "pending"

    def test_attempt_number_env_var(self) -> None:
        """SR_ATTEMPT_NUMBER is included in env."""
        from spec_runner.config import ExecutorConfig
        from spec_runner.task import Task

        task = Task(id="TASK-020", name="Retry", priority="p1", status="todo", estimate="1d")
        config = ExecutorConfig(project_root=Path("/tmp/proj"))
        env = build_task_env(task, config, success=True, attempt_number=3)
        assert env["SR_ATTEMPT_NUMBER"] == "3"

    def test_duration_seconds_env_var(self) -> None:
        """SR_DURATION_SECONDS is formatted with one decimal."""
        from spec_runner.config import ExecutorConfig
        from spec_runner.task import Task

        task = Task(id="TASK-021", name="Timed", priority="p1", status="todo", estimate="1d")
        config = ExecutorConfig(project_root=Path("/tmp/proj"))
        env = build_task_env(task, config, success=True, duration_seconds=45.678)
        assert env["SR_DURATION_SECONDS"] == "45.7"

    def test_error_env_vars(self) -> None:
        """SR_ERROR and SR_ERROR_CODE are included in env."""
        from spec_runner.config import ExecutorConfig
        from spec_runner.task import Task

        task = Task(id="TASK-022", name="Failed", priority="p1", status="todo", estimate="1d")
        config = ExecutorConfig(project_root=Path("/tmp/proj"))
        env = build_task_env(
            task,
            config,
            success=False,
            error="Tests failed",
            error_code="TEST_FAILURE",
        )
        assert env["SR_ERROR"] == "Tests failed"
        assert env["SR_ERROR_CODE"] == "TEST_FAILURE"

    def test_default_env_vars(self) -> None:
        """Default values for new env vars are empty/zero."""
        from spec_runner.config import ExecutorConfig
        from spec_runner.task import Task

        task = Task(id="TASK-023", name="Default", priority="p1", status="todo", estimate="1d")
        config = ExecutorConfig(project_root=Path("/tmp/proj"))
        env = build_task_env(task, config, success=True)
        assert env["SR_ATTEMPT_NUMBER"] == "0"
        assert env["SR_DURATION_SECONDS"] == "0.0"
        assert env["SR_ERROR"] == ""
        assert env["SR_ERROR_CODE"] == ""


class TestPluginIntegration:
    """Integration tests: plugins wired through pre_start_hook and post_done_hook."""

    def _make_script(self, plugin_dir: Path, name: str, content: str) -> Path:
        script = plugin_dir / name
        script.write_text(content)
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    def test_pre_start_runs_plugins(self, tmp_path: Path) -> None:
        """pre_start_hook discovers and runs pre_start plugin hooks."""
        import subprocess as real_subprocess

        from spec_runner.config import ExecutorConfig
        from spec_runner.hooks import pre_start_hook
        from spec_runner.task import Task

        plugins_dir = tmp_path / "spec" / "plugins"
        plugin_dir = _create_plugin(
            plugins_dir,
            "pre-test",
            {"pre_start": {"command": "./start.sh"}},
        )
        marker = tmp_path / "pre_marker.txt"
        self._make_script(plugin_dir, "start.sh", f"#!/bin/bash\ntouch {marker}")

        task = Task(id="TASK-001", name="Test", priority="p0", status="todo", estimate="1d")
        config = ExecutorConfig(
            project_root=tmp_path,
            create_git_branch=False,
            plugins_dir=plugins_dir,
        )

        original_run = real_subprocess.run

        def selective_mock(cmd, *args, **kwargs):
            """Mock only uv/git calls, let plugin scripts run for real."""
            if isinstance(cmd, list) and cmd[0] in ("uv", "git"):
                return MagicMock(returncode=0, stdout="", stderr="")
            return original_run(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=selective_mock):
            pre_start_hook(task, config)

        assert marker.exists()

    def test_post_done_runs_plugins(self, tmp_path: Path) -> None:
        """post_done_hook discovers and runs post_done plugin hooks."""
        from spec_runner.config import ExecutorConfig
        from spec_runner.hooks import post_done_hook
        from spec_runner.task import Task

        plugins_dir = tmp_path / "spec" / "plugins"
        plugin_dir = _create_plugin(
            plugins_dir,
            "post-test",
            {"post_done": {"command": "./done.sh"}},
        )
        marker = tmp_path / "post_marker.txt"
        self._make_script(plugin_dir, "done.sh", f"#!/bin/bash\ntouch {marker}")

        task = Task(id="TASK-001", name="Test", priority="p0", status="todo", estimate="1d")
        config = ExecutorConfig(
            project_root=tmp_path,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=False,
            auto_commit=False,
            create_git_branch=False,
            plugins_dir=plugins_dir,
        )

        with patch("spec_runner.state.ExecutorState") as mock_state_cls:
            mock_state = MagicMock()
            mock_state.tasks = {}
            mock_state_cls.return_value = mock_state
            post_done_hook(task, config, success=True)

        assert marker.exists()

    def test_pre_start_blocking_plugin_returns_false(self, tmp_path: Path) -> None:
        """pre_start_hook returns False when a blocking plugin fails."""
        import subprocess as real_subprocess

        from spec_runner.config import ExecutorConfig
        from spec_runner.hooks import pre_start_hook
        from spec_runner.task import Task

        plugins_dir = tmp_path / "spec" / "plugins"
        plugin_dir = _create_plugin(
            plugins_dir,
            "blocker",
            {"pre_start": {"command": "./fail.sh", "blocking": True}},
        )
        self._make_script(plugin_dir, "fail.sh", "#!/bin/bash\nexit 1")

        task = Task(id="TASK-001", name="Test", priority="p0", status="todo", estimate="1d")
        config = ExecutorConfig(
            project_root=tmp_path,
            create_git_branch=False,
            plugins_dir=plugins_dir,
        )

        original_run = real_subprocess.run

        def selective_mock(cmd, *args, **kwargs):
            if isinstance(cmd, list) and cmd[0] in ("uv", "git"):
                return MagicMock(returncode=0, stdout="", stderr="")
            return original_run(cmd, *args, **kwargs)

        with patch("subprocess.run", side_effect=selective_mock):
            result = pre_start_hook(task, config)

        assert result is False

    def test_post_done_blocking_plugin_returns_false(self, tmp_path: Path) -> None:
        """post_done_hook returns failure when a blocking plugin fails."""
        from spec_runner.config import ExecutorConfig
        from spec_runner.hooks import post_done_hook
        from spec_runner.task import Task

        plugins_dir = tmp_path / "spec" / "plugins"
        plugin_dir = _create_plugin(
            plugins_dir,
            "blocker",
            {"post_done": {"command": "./fail.sh", "blocking": True}},
        )
        self._make_script(plugin_dir, "fail.sh", "#!/bin/bash\nexit 1")

        task = Task(id="TASK-001", name="Test", priority="p0", status="todo", estimate="1d")
        config = ExecutorConfig(
            project_root=tmp_path,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=False,
            auto_commit=False,
            create_git_branch=False,
            plugins_dir=plugins_dir,
        )

        with patch("spec_runner.state.ExecutorState") as mock_state_cls:
            mock_state = MagicMock()
            mock_state.tasks = {}
            mock_state_cls.return_value = mock_state
            success, error, review_status, review_findings = post_done_hook(
                task, config, success=True
            )

        assert success is False
        assert error is not None
        assert "blocker" in error.lower()

    def test_post_done_nonblocking_plugin_still_returns_true(self, tmp_path: Path) -> None:
        """post_done_hook returns success when a non-blocking plugin fails."""
        from spec_runner.config import ExecutorConfig
        from spec_runner.hooks import post_done_hook
        from spec_runner.task import Task

        plugins_dir = tmp_path / "spec" / "plugins"
        plugin_dir = _create_plugin(
            plugins_dir,
            "nonblocker",
            {"post_done": {"command": "./fail.sh", "blocking": False}},
        )
        self._make_script(plugin_dir, "fail.sh", "#!/bin/bash\nexit 1")

        task = Task(id="TASK-001", name="Test", priority="p0", status="todo", estimate="1d")
        config = ExecutorConfig(
            project_root=tmp_path,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=False,
            auto_commit=False,
            create_git_branch=False,
            plugins_dir=plugins_dir,
        )

        with patch("spec_runner.state.ExecutorState") as mock_state_cls:
            mock_state = MagicMock()
            mock_state.tasks = {}
            mock_state_cls.return_value = mock_state
            success, error, review_status, review_findings = post_done_hook(
                task, config, success=True
            )

        assert success is True
