"""Tests for spec_runner.state module."""

import json
import sqlite3
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.state import (
    ErrorCode,
    ExecutorState,
    RetryContext,
    TaskAttempt,
    TaskState,
    check_stop_requested,
    clear_stop_file,
)


def _make_config(tmp_path: Path, **overrides) -> ExecutorConfig:
    """Create an ExecutorConfig rooted in tmp_path."""
    defaults: dict = {
        "state_file": tmp_path / "state.db",
        "project_root": tmp_path,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


# --- TaskAttempt ---


class TestTaskAttempt:
    def test_creation(self):
        a = TaskAttempt(
            timestamp="2026-01-01T00:00:00",
            success=True,
            duration_seconds=1.5,
        )
        assert a.success is True
        assert a.duration_seconds == 1.5
        assert a.error is None
        assert a.claude_output is None

    def test_with_error(self):
        a = TaskAttempt(
            timestamp="2026-01-01T00:00:00",
            success=False,
            duration_seconds=2.0,
            error="something went wrong",
        )
        assert a.success is False
        assert a.error == "something went wrong"


# --- TaskState ---


class TestTaskState:
    def test_attempt_count_empty(self):
        ts = TaskState(task_id="TASK-001", status="pending")
        assert ts.attempt_count == 0

    def test_attempt_count_with_attempts(self):
        ts = TaskState(
            task_id="TASK-001",
            status="running",
            attempts=[
                TaskAttempt(timestamp="t1", success=False, duration_seconds=1.0, error="e1"),
                TaskAttempt(timestamp="t2", success=True, duration_seconds=2.0),
            ],
        )
        assert ts.attempt_count == 2

    def test_last_error_none(self):
        ts = TaskState(task_id="TASK-001", status="pending")
        assert ts.last_error is None

    def test_last_error_returns_latest(self):
        ts = TaskState(
            task_id="TASK-001",
            status="failed",
            attempts=[
                TaskAttempt(
                    timestamp="t1",
                    success=False,
                    duration_seconds=1.0,
                    error="first error",
                ),
                TaskAttempt(
                    timestamp="t2",
                    success=False,
                    duration_seconds=1.0,
                    error="second error",
                ),
            ],
        )
        assert ts.last_error == "second error"


# --- ExecutorState ---


class TestExecutorState:
    def test_creates_empty_state(self, tmp_path):
        config = _make_config(tmp_path)
        state = ExecutorState(config)
        assert state.tasks == {}
        assert state.consecutive_failures == 0
        assert state.total_completed == 0
        assert state.total_failed == 0

    def test_save_and_load_roundtrip(self, tmp_path):
        config = _make_config(tmp_path)
        state = ExecutorState(config)
        state.record_attempt("TASK-001", success=True, duration=5.0)

        # Load from same file
        state2 = ExecutorState(config)
        assert "TASK-001" in state2.tasks
        assert state2.tasks["TASK-001"].status == "success"
        assert state2.tasks["TASK-001"].attempt_count == 1
        assert state2.total_completed == 1

    def test_record_failure_increments_consecutive_failures(self, tmp_path):
        config = _make_config(tmp_path, max_retries=5)
        state = ExecutorState(config)
        state.record_attempt("TASK-001", success=False, duration=1.0, error="fail")
        assert state.consecutive_failures == 1
        state.record_attempt("TASK-001", success=False, duration=1.0, error="fail again")
        assert state.consecutive_failures == 2

    def test_record_success_resets_consecutive_failures(self, tmp_path):
        config = _make_config(tmp_path, max_retries=5)
        state = ExecutorState(config)
        state.record_attempt("TASK-001", success=False, duration=1.0, error="fail")
        assert state.consecutive_failures == 1
        state.record_attempt("TASK-002", success=True, duration=2.0)
        assert state.consecutive_failures == 0

    def test_should_stop_threshold(self, tmp_path):
        config = _make_config(tmp_path, max_consecutive_failures=2, max_retries=10)
        state = ExecutorState(config)
        assert state.should_stop() is False
        state.record_attempt("TASK-001", success=False, duration=1.0, error="e1")
        assert state.should_stop() is False
        state.record_attempt("TASK-002", success=False, duration=1.0, error="e2")
        assert state.should_stop() is True

    def test_mark_running_sets_status(self, tmp_path):
        config = _make_config(tmp_path)
        state = ExecutorState(config)
        state.mark_running("TASK-001")
        ts = state.get_task_state("TASK-001")
        assert ts.status == "running"
        assert ts.started_at is not None


# --- ExecutorState SQLite ---


class TestExecutorStateSQLite:
    def test_creates_db_file(self, tmp_path):
        config = _make_config(tmp_path)
        ExecutorState(config)
        assert config.state_file.exists()

    def test_db_has_wal_mode(self, tmp_path):
        config = _make_config(tmp_path)
        ExecutorState(config)
        conn = sqlite3.connect(str(config.state_file))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_db_has_tables(self, tmp_path):
        config = _make_config(tmp_path)
        ExecutorState(config)
        conn = sqlite3.connect(str(config.state_file))
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert "tasks" in tables
        assert "attempts" in tables
        assert "executor_meta" in tables

    def test_record_attempt_stores_error_code(self, tmp_path):
        config = _make_config(tmp_path)
        state = ExecutorState(config)
        state.record_attempt(
            "TASK-001",
            success=False,
            duration=1.0,
            error="tests failed",
            error_code=ErrorCode.TEST_FAILURE,
        )
        # Reload from DB
        state2 = ExecutorState(config)
        ts = state2.get_task_state("TASK-001")
        assert ts.attempts[-1].error_code == ErrorCode.TEST_FAILURE

    def test_consecutive_failures_persisted(self, tmp_path):
        config = _make_config(tmp_path, max_retries=5)
        state = ExecutorState(config)
        state.record_attempt("T1", success=False, duration=1.0, error="e1")
        state.record_attempt("T2", success=False, duration=1.0, error="e2")
        state2 = ExecutorState(config)
        assert state2.consecutive_failures == 2

    def test_total_completed_persisted(self, tmp_path):
        config = _make_config(tmp_path)
        state = ExecutorState(config)
        state.record_attempt("T1", success=True, duration=1.0)
        state2 = ExecutorState(config)
        assert state2.total_completed == 1

    def test_total_failed_persisted(self, tmp_path):
        config = _make_config(tmp_path, max_retries=1)
        state = ExecutorState(config)
        state.record_attempt("T1", success=False, duration=1.0, error="e")
        state2 = ExecutorState(config)
        assert state2.total_failed == 1

    def test_mark_running_persisted(self, tmp_path):
        config = _make_config(tmp_path)
        state = ExecutorState(config)
        state.mark_running("TASK-001")
        state2 = ExecutorState(config)
        ts = state2.get_task_state("TASK-001")
        assert ts.status == "running"
        assert ts.started_at is not None

    def test_save_syncs_in_memory_mutations(self, tmp_path):
        """Test that _save() persists direct in-memory changes."""
        config = _make_config(tmp_path)
        state = ExecutorState(config)
        state.record_attempt("T1", success=False, duration=1.0, error="e")
        # Direct in-memory mutation (as executor.py does)
        ts = state.get_task_state("T1")
        ts.attempts = []
        ts.status = "pending"
        state.consecutive_failures = 0
        state._save()
        # Reload and verify
        state2 = ExecutorState(config)
        ts2 = state2.get_task_state("T1")
        assert ts2.status == "pending"
        assert ts2.attempt_count == 0
        assert state2.consecutive_failures == 0


# --- Stop file ---


class TestStopFile:
    def test_check_stop_not_requested(self, tmp_path):
        (tmp_path / "spec").mkdir()
        config = _make_config(tmp_path)
        assert check_stop_requested(config) is False

    def test_check_stop_requested(self, tmp_path):
        (tmp_path / "spec").mkdir()
        config = _make_config(tmp_path)
        config.stop_file.touch()
        assert check_stop_requested(config) is True

    def test_clear_stop_file(self, tmp_path):
        (tmp_path / "spec").mkdir()
        config = _make_config(tmp_path)
        config.stop_file.touch()
        assert config.stop_file.exists()
        clear_stop_file(config)
        assert not config.stop_file.exists()

    def test_clear_stop_file_noop_if_missing(self, tmp_path):
        (tmp_path / "spec").mkdir()
        config = _make_config(tmp_path)
        assert not config.stop_file.exists()
        # Should not raise
        clear_stop_file(config)
        assert not config.stop_file.exists()


# --- ErrorCode ---


class TestErrorCode:
    def test_values_are_strings(self):
        assert ErrorCode.TIMEOUT == "TIMEOUT"
        assert ErrorCode.RATE_LIMIT == "RATE_LIMIT"
        assert ErrorCode.TEST_FAILURE == "TEST_FAILURE"
        assert ErrorCode.LINT_FAILURE == "LINT_FAILURE"
        assert ErrorCode.TASK_FAILED == "TASK_FAILED"
        assert ErrorCode.HOOK_FAILURE == "HOOK_FAILURE"
        assert ErrorCode.UNKNOWN == "UNKNOWN"

    def test_is_string_enum(self):
        assert isinstance(ErrorCode.TIMEOUT, str)


# --- RetryContext ---


class TestRetryContext:
    def test_creation(self):
        ctx = RetryContext(
            attempt_number=2,
            max_attempts=3,
            previous_error_code=ErrorCode.TEST_FAILURE,
            previous_error="tests failed",
            what_was_tried="Implemented login page",
            test_failures="FAILED test_login - AssertionError",
        )
        assert ctx.attempt_number == 2
        assert ctx.previous_error_code == ErrorCode.TEST_FAILURE
        assert ctx.test_failures is not None

    def test_creation_without_test_failures(self):
        ctx = RetryContext(
            attempt_number=1,
            max_attempts=3,
            previous_error_code=ErrorCode.TIMEOUT,
            previous_error="Timeout after 30 minutes",
            what_was_tried="Implementing feature",
            test_failures=None,
        )
        assert ctx.test_failures is None


# --- TaskAttempt.error_code ---


class TestTaskAttemptErrorCode:
    def test_error_code_default_none(self):
        a = TaskAttempt(
            timestamp="2026-01-01T00:00:00",
            success=True,
            duration_seconds=1.5,
        )
        assert a.error_code is None

    def test_error_code_set(self):
        a = TaskAttempt(
            timestamp="2026-01-01T00:00:00",
            success=False,
            duration_seconds=2.0,
            error="tests failed",
            error_code=ErrorCode.TEST_FAILURE,
        )
        assert a.error_code == ErrorCode.TEST_FAILURE


# --- JSON to SQLite Migration ---


class TestJsonToSqliteMigration:
    def test_migrates_json_to_sqlite(self, tmp_path):
        """If .json exists but no .db, migrate and rename .json to .json.bak."""
        json_path = tmp_path / "state.json"
        db_path = tmp_path / "state.db"
        json_data = {
            "tasks": {
                "TASK-001": {
                    "status": "success",
                    "attempts": [
                        {
                            "timestamp": "2026-01-01T00:00:00",
                            "success": True,
                            "duration_seconds": 5.0,
                            "error": None,
                        }
                    ],
                    "started_at": "2026-01-01T00:00:00",
                    "completed_at": "2026-01-01T00:01:00",
                }
            },
            "consecutive_failures": 1,
            "total_completed": 1,
            "total_failed": 0,
        }
        json_path.write_text(json.dumps(json_data))

        config = _make_config(tmp_path, state_file=db_path)
        state = ExecutorState(config)

        assert db_path.exists()
        assert not json_path.exists()
        assert (tmp_path / "state.json.bak").exists()
        assert "TASK-001" in state.tasks
        assert state.tasks["TASK-001"].status == "success"
        assert state.tasks["TASK-001"].attempt_count == 1
        assert state.consecutive_failures == 1
        assert state.total_completed == 1

    def test_no_migration_if_db_exists(self, tmp_path):
        """If .db already exists, don't touch .json even if present."""
        json_path = tmp_path / "state.json"
        db_path = tmp_path / "state.db"

        # Create DB first (no JSON present yet)
        config = _make_config(tmp_path, state_file=db_path)
        ExecutorState(config)
        assert db_path.exists()

        # Now place JSON alongside the existing DB
        json_path.write_text(
            '{"tasks":{}, "consecutive_failures":0, '
            '"total_completed":0, "total_failed":0}'
        )

        # Re-open: DB exists, so JSON should NOT be touched
        ExecutorState(config)
        assert json_path.exists()

    def test_fresh_db_if_nothing_exists(self, tmp_path):
        """If neither .json nor .db exists, create fresh DB."""
        db_path = tmp_path / "state.db"
        config = _make_config(tmp_path, state_file=db_path)
        state = ExecutorState(config)
        assert db_path.exists()
        assert state.tasks == {}

    def test_migrates_multiple_tasks(self, tmp_path):
        """Migration handles multiple tasks with multiple attempts."""
        json_path = tmp_path / "state.json"
        db_path = tmp_path / "state.db"
        json_data = {
            "tasks": {
                "TASK-001": {
                    "status": "success",
                    "attempts": [
                        {
                            "timestamp": "t1",
                            "success": False,
                            "duration_seconds": 1.0,
                            "error": "e1",
                        },
                        {
                            "timestamp": "t2",
                            "success": True,
                            "duration_seconds": 2.0,
                            "error": None,
                        },
                    ],
                    "started_at": "t0",
                    "completed_at": "t2",
                },
                "TASK-002": {
                    "status": "failed",
                    "attempts": [
                        {
                            "timestamp": "t3",
                            "success": False,
                            "duration_seconds": 3.0,
                            "error": "e2",
                        },
                    ],
                    "started_at": "t3",
                    "completed_at": None,
                },
            },
            "consecutive_failures": 1,
            "total_completed": 1,
            "total_failed": 1,
        }
        json_path.write_text(json.dumps(json_data))

        config = _make_config(tmp_path, state_file=db_path)
        state = ExecutorState(config)

        assert len(state.tasks) == 2
        assert state.tasks["TASK-001"].attempt_count == 2
        assert state.tasks["TASK-002"].attempt_count == 1
        assert state.total_completed == 1
        assert state.total_failed == 1
