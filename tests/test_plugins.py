"""Tests for spec_runner.plugins module."""

import stat
from pathlib import Path

import yaml

from spec_runner.plugins import PluginHook, discover_plugins, run_plugin_hooks


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
