from pathlib import Path

from spec_runner.config import ExecutorConfig, build_config


def test_sync_deps_defaults_true():
    cfg = ExecutorConfig(project_root=Path("."))
    assert cfg.sync_deps is True


def test_build_config_reads_sync_deps_false():
    cfg = build_config({"sync_deps": False}, args=None)
    assert cfg.sync_deps is False
