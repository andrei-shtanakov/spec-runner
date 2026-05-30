"""Tests for status output formatting (v2.3.0)."""

from pathlib import Path

from spec_runner import __version__
from spec_runner.cli_info import print_status
from spec_runner.config import ExecutorConfig
from spec_runner.state import ErrorCode, ExecutorState


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


class TestErrorDisplay:
    def _seed_failed(self, cfg, *, kind, stage, msg):
        with ExecutorState(cfg) as state:
            state.record_attempt(
                "TASK-001",
                success=False,
                duration=1.0,
                error=msg,
                error_code=ErrorCode.TASK_FAILED,
                error_kind=kind,
                error_stage=stage,
            )

    def test_new_format_with_kind_and_stage(self, tmp_path, capsys):
        cfg = _cfg(tmp_path, max_retries=1)
        cfg.logs_dir.mkdir()
        self._seed_failed(
            cfg,
            kind="rate_limit",
            stage="codex",
            msg="OpenAI usage limit — try again at 9:54 AM",
        )
        print_status(cfg)
        out = capsys.readouterr().out
        assert "[at: codex]" in out
        assert "[rate_limit]" in out
        assert "9:54 AM" in out

    def test_legacy_row_without_kind_falls_back(self, tmp_path, capsys):
        cfg = _cfg(tmp_path, max_retries=1)
        cfg.logs_dir.mkdir()
        self._seed_failed(cfg, kind=None, stage=None, msg="old-style error")
        print_status(cfg)
        out = capsys.readouterr().out
        assert "[at:" not in out
        assert "[rate_limit]" not in out
        assert "old-style error" in out


class TestStopReasonLine:
    def test_max_failures_renders_warning(self, tmp_path, capsys):
        cfg = _cfg(tmp_path)
        cfg.logs_dir.mkdir()
        with ExecutorState(cfg) as state:
            state.set_meta("last_run_stop_reason", "max_consecutive_failures")
            state.set_meta("last_run_stop_detail", "12/2")
        print_status(cfg)
        out = capsys.readouterr().out
        assert "⚠️ Last run stopped: max_consecutive_failures reached (12/2)" in out

    def test_rate_limit_renders_warning(self, tmp_path, capsys):
        cfg = _cfg(tmp_path)
        cfg.logs_dir.mkdir()
        with ExecutorState(cfg) as state:
            state.set_meta("last_run_stop_reason", "error_rate_limit")
            state.set_meta("last_run_stop_detail", "OpenAI usage limit — try again at 9:54 AM")
        print_status(cfg)
        out = capsys.readouterr().out
        assert "⚠️ Last run stopped: rate_limit" in out
        assert "9:54 AM" in out

    def test_completed_omits_warning_line(self, tmp_path, capsys):
        cfg = _cfg(tmp_path)
        cfg.logs_dir.mkdir()
        with ExecutorState(cfg) as state:
            state.set_meta("last_run_stop_reason", "completed")
        print_status(cfg)
        out = capsys.readouterr().out
        assert "⚠️ Last run stopped" not in out
