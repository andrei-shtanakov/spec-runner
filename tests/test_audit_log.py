"""Tests for compliance audit-trail logger (LABS-40)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from spec_runner.audit_log import (
    EVENT_RUN_ENDED,
    EVENT_RUN_STARTED,
    EVENT_STATE_DEGRADED,
    EVENT_TASK_ATTEMPT,
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_STARTED,
    AuditLogger,
    NoOpAuditLogger,
    build_audit_logger,
)
from spec_runner.config import ExecutorConfig
from spec_runner.state import ErrorCode, ExecutorState

# --- Helpers --------------------------------------------------------


def _read_audit(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _make_config(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "state_file": tmp_path / "state.db",
        "project_root": tmp_path,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


# --- AuditLogger unit tests -----------------------------------------


class TestAuditLogger:
    def test_writes_jsonl_entry(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_path, operator="alice@host")
        logger.record(EVENT_TASK_STARTED, task_id="TASK-001", started_at="2026-04-17T10:00:00")

        entries = _read_audit(log_path)
        assert len(entries) == 1
        entry = entries[0]
        assert entry["event"] == EVENT_TASK_STARTED
        assert entry["task_id"] == "TASK-001"
        assert entry["operator"] == "alice@host"
        assert entry["run_id"]
        assert entry["details"]["started_at"] == "2026-04-17T10:00:00"
        assert "timestamp" in entry

    def test_run_id_is_stable_across_calls(self, tmp_path: Path) -> None:
        logger = AuditLogger(tmp_path / "audit.jsonl", run_id="fixed-run-id")
        logger.record(EVENT_RUN_STARTED)
        logger.record(EVENT_RUN_ENDED)

        entries = _read_audit(tmp_path / "audit.jsonl")
        assert [e["run_id"] for e in entries] == ["fixed-run-id", "fixed-run-id"]

    def test_appends_instead_of_overwrite(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.jsonl"
        log_path.write_text('{"pre-existing":true}\n')
        logger = AuditLogger(log_path)
        logger.record(EVENT_TASK_STARTED, task_id="TASK-001")

        lines = log_path.read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"pre-existing": True}

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "audit.jsonl"
        logger = AuditLogger(nested)
        logger.record(EVENT_RUN_STARTED)
        assert nested.exists()

    def test_unknown_event_is_still_recorded(self, tmp_path: Path, caplog) -> None:
        import logging as _logging

        logger = AuditLogger(tmp_path / "audit.jsonl")
        with caplog.at_level(_logging.WARNING):
            logger.record("not_a_real_event", task_id="TASK-001")

        entries = _read_audit(tmp_path / "audit.jsonl")
        assert entries[0]["event"] == "not_a_real_event"

    def test_does_not_raise_on_write_failure(self, tmp_path: Path, monkeypatch) -> None:
        logger = AuditLogger(tmp_path / "audit.jsonl")

        def _raise(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "open", _raise)
        # Must not raise
        logger.record(EVENT_TASK_STARTED, task_id="TASK-001")

    def test_thread_safe(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.jsonl"
        logger = AuditLogger(log_path)

        def _hammer() -> None:
            for i in range(50):
                logger.record(EVENT_TASK_ATTEMPT, task_id=f"TASK-{i:03d}")

        threads = [threading.Thread(target=_hammer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        entries = _read_audit(log_path)
        assert len(entries) == 200
        # Every line must be a complete JSON object — no interleaved writes.
        for entry in entries:
            assert entry["event"] == EVENT_TASK_ATTEMPT


# --- build_audit_logger + NoOp -------------------------------------


class TestBuildAuditLogger:
    def test_disabled_when_no_path(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        logger = build_audit_logger(config)
        assert isinstance(logger, NoOpAuditLogger)
        logger.record(EVENT_RUN_STARTED)  # must be callable, no file side effect

    def test_relative_path_is_anchored_to_project_root(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path, audit_log_path="spec/audit.jsonl")
        logger = build_audit_logger(config)
        assert isinstance(logger, AuditLogger)
        assert logger.path == tmp_path / "spec" / "audit.jsonl"

    def test_absolute_path_preserved(self, tmp_path: Path) -> None:
        abs_path = tmp_path / "abs" / "audit.jsonl"
        config = _make_config(tmp_path, audit_log_path=str(abs_path))
        logger = build_audit_logger(config)
        assert logger.path == abs_path

    def test_operator_override(self, tmp_path: Path) -> None:
        config = _make_config(
            tmp_path,
            audit_log_path="audit.jsonl",
            audit_log_operator="maestro",
        )
        logger = build_audit_logger(config)
        assert logger.operator == "maestro"

    def test_spec_prefix_flows_through(self, tmp_path: Path) -> None:
        config = _make_config(
            tmp_path, audit_log_path="audit.jsonl", spec_prefix="phase5-"
        )
        logger = build_audit_logger(config)
        logger.record(EVENT_RUN_STARTED)
        entry = _read_audit(logger.path)[0]
        assert entry["spec_prefix"] == "phase5-"


# --- ExecutorState integration --------------------------------------


class TestExecutorStateAuditIntegration:
    def test_disabled_by_default(self, tmp_path: Path) -> None:
        config = _make_config(tmp_path)
        with ExecutorState(config) as state:
            assert isinstance(state.audit_logger, NoOpAuditLogger)
            state.mark_running("TASK-001")  # must not crash
            state.record_attempt("TASK-001", success=True, duration=1.0)

    def test_mark_running_emits_task_started(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.jsonl"
        config = _make_config(tmp_path, audit_log_path=str(log_path))
        with ExecutorState(config) as state:
            state.mark_running("TASK-001")

        entries = _read_audit(log_path)
        events = [e["event"] for e in entries]
        assert EVENT_TASK_STARTED in events
        started = next(e for e in entries if e["event"] == EVENT_TASK_STARTED)
        assert started["task_id"] == "TASK-001"

    def test_record_attempt_emits_attempt_and_completion(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.jsonl"
        config = _make_config(tmp_path, audit_log_path=str(log_path))
        with ExecutorState(config) as state:
            state.record_attempt(
                "TASK-001",
                success=True,
                duration=12.3,
                input_tokens=100,
                output_tokens=50,
                cost_usd=0.42,
                review_status="passed",
            )

        entries = _read_audit(log_path)
        events = [e["event"] for e in entries]
        assert events == [EVENT_TASK_ATTEMPT, EVENT_TASK_COMPLETED]

        attempt = entries[0]
        assert attempt["task_id"] == "TASK-001"
        assert attempt["details"]["success"] is True
        assert attempt["details"]["cost_usd"] == pytest.approx(0.42)
        assert attempt["details"]["task_total_cost_usd"] == pytest.approx(0.42)
        assert attempt["details"]["review_status"] == "passed"

        completed = entries[1]
        assert completed["details"]["attempts"] == 1
        assert completed["details"]["cost_usd"] == pytest.approx(0.42)

    def test_permanent_failure_emits_task_failed(self, tmp_path: Path) -> None:
        log_path = tmp_path / "audit.jsonl"
        config = _make_config(tmp_path, audit_log_path=str(log_path), max_retries=1)
        with ExecutorState(config) as state:
            state.record_attempt(
                "TASK-001",
                success=False,
                duration=2.0,
                error="boom",
                error_code=ErrorCode.TASK_FAILED,
            )

        entries = _read_audit(log_path)
        events = [e["event"] for e in entries]
        assert EVENT_TASK_FAILED in events
        failed = next(e for e in entries if e["event"] == EVENT_TASK_FAILED)
        assert failed["task_id"] == "TASK-001"
        assert failed["details"]["last_error"] == "boom"
        assert failed["details"]["error_code"] == "TASK_FAILED"

    def test_degraded_mode_emits_state_degraded_event(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import sqlite3
        from unittest.mock import MagicMock

        log_path = tmp_path / "audit.jsonl"
        config = _make_config(tmp_path, audit_log_path=str(log_path))
        with ExecutorState(config) as state:
            fake = MagicMock()
            fake.execute.side_effect = sqlite3.OperationalError("disk I/O error")
            fake.__enter__.return_value = fake
            fake.__exit__.return_value = False
            state._conn = fake

            state.record_attempt("TASK-001", success=False, duration=0.5)

        entries = _read_audit(log_path)
        events = [e["event"] for e in entries]
        assert EVENT_STATE_DEGRADED in events
        degraded = next(e for e in entries if e["event"] == EVENT_STATE_DEGRADED)
        assert degraded["details"]["disk_full"] is True
        assert degraded["task_id"] == "TASK-001"
