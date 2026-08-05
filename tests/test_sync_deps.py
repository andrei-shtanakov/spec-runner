"""Dependency sync must not assume a Python stack (#70)."""

from pathlib import Path
from unittest.mock import patch

from spec_runner.config import ExecutorConfig, build_config
from spec_runner.hooks import pre_start_hook
from spec_runner.task import Task


def _task() -> Task:
    return Task(
        id="TASK-001",
        name="probe",
        priority="p0",
        status="todo",
        estimate="",
        description="",
        checklist=[],
    )


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "sync_deps": True,
        "create_git_branch": False,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _run_hook(cfg: ExecutorConfig) -> list:
    with patch("spec_runner.hooks.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        pre_start_hook(_task(), cfg)
    return mock_run.call_args_list


class TestSyncDepsStackAware:
    def test_no_pyproject_skips_uv_sync(self, tmp_path):
        """An Elixir/Go/JS repo must not see `uv sync` noise every run."""
        calls = [c.args[0] for c in _run_hook(_cfg(tmp_path))]
        assert ["uv", "sync"] not in calls

    def test_pyproject_present_runs_uv_sync(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        calls = [c.args[0] for c in _run_hook(_cfg(tmp_path))]
        assert ["uv", "sync"] in calls

    def test_custom_sync_command_runs_regardless_of_stack(self, tmp_path):
        calls = _run_hook(_cfg(tmp_path, sync_command="mix deps.get"))
        commands = [c.args[0] for c in calls]
        assert "mix deps.get" in commands
        assert ["uv", "sync"] not in commands

    def test_custom_sync_command_wins_over_pyproject_default(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        commands = [c.args[0] for c in _run_hook(_cfg(tmp_path, sync_command="mix deps.get"))]
        assert "mix deps.get" in commands
        assert ["uv", "sync"] not in commands

    def test_sync_deps_off_skips_everything(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        commands = [
            c.args[0]
            for c in _run_hook(_cfg(tmp_path, sync_deps=False, sync_command="mix deps.get"))
        ]
        assert "mix deps.get" not in commands
        assert ["uv", "sync"] not in commands


class TestSyncCommandConfig:
    def test_yaml_commands_sync_flows_into_config(self):
        cfg = build_config({"sync_command": "mix deps.get"}, args=None)
        assert cfg.sync_command == "mix deps.get"

    def test_default_is_empty(self):
        cfg = ExecutorConfig()
        assert cfg.sync_command == ""
