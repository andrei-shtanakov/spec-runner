"""Tests for status output formatting (v2.3.0)."""

from pathlib import Path

from spec_runner import __version__
from spec_runner.cli_info import print_status
from spec_runner.config import ExecutorConfig


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "state_file": tmp_path / "state.db",
        "project_root": tmp_path,
        "logs_dir": tmp_path / "logs",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    return cfg


class TestStatusVersionHeader:
    def test_first_line_includes_version(self, tmp_path, capsys):
        cfg = _cfg(tmp_path)
        cfg.logs_dir.mkdir()
        print_status(cfg)
        out = capsys.readouterr().out
        first = out.strip().splitlines()[0]
        assert __version__ in first
        assert "spec-runner" in first
