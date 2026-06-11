from pathlib import Path
from unittest.mock import patch

from spec_runner.config import ExecutorConfig, build_config
from spec_runner.hooks import pre_start_hook
from spec_runner.task import Task


def test_sync_deps_defaults_true():
    cfg = ExecutorConfig(project_root=Path("."))
    assert cfg.sync_deps is True


def test_build_config_reads_sync_deps_false():
    cfg = build_config({"sync_deps": False}, args=None)
    assert cfg.sync_deps is False


def _smoke_task() -> Task:
    # Task dataclass requires `estimate` (no default), positioned before description.
    return Task(
        id="TASK-001",
        name="probe",
        priority="p0",
        status="todo",
        estimate="",
        description="",
        checklist=[],
    )


def test_pre_start_skips_uv_sync_when_disabled(tmp_path):
    cfg = ExecutorConfig(
        project_root=tmp_path,
        sync_deps=False,
        create_git_branch=False,
    )
    with patch("spec_runner.hooks.subprocess.run") as mock_run:
        pre_start_hook(_smoke_task(), cfg)
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert ["uv", "sync"] not in calls


def test_pre_start_runs_uv_sync_when_enabled(tmp_path):
    cfg = ExecutorConfig(
        project_root=tmp_path,
        sync_deps=True,
        create_git_branch=False,
    )
    with patch("spec_runner.hooks.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        pre_start_hook(_smoke_task(), cfg)
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert ["uv", "sync"] in calls
