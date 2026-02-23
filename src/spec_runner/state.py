"""State management for spec-runner executor.

Tracks task execution state: attempts, results, and persistence via SQLite.
"""

import contextlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

from .config import ExecutorConfig

# === State Management ===


class ErrorCode(str, Enum):
    """Structured error classification for task failures."""

    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    SYNTAX = "SYNTAX"
    TEST_FAILURE = "TEST_FAILURE"
    LINT_FAILURE = "LINT_FAILURE"
    TASK_FAILED = "TASK_FAILED"
    HOOK_FAILURE = "HOOK_FAILURE"
    UNKNOWN = "UNKNOWN"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    REVIEW_REJECTED = "REVIEW_REJECTED"
    INTERRUPTED = "INTERRUPTED"


class ReviewVerdict(str, Enum):
    """Verdict from code review step."""

    PASSED = "passed"
    FIXED = "fixed"
    FAILED = "failed"
    SKIPPED = "skipped"
    REJECTED = "rejected"


@dataclass
class TaskAttempt:
    """Task execution attempt"""

    timestamp: str
    success: bool
    duration_seconds: float
    error: str | None = None
    claude_output: str | None = None
    error_code: ErrorCode | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    review_status: str | None = None
    review_findings: str | None = None


@dataclass
class RetryContext:
    """Structured context for retry attempts."""

    attempt_number: int
    max_attempts: int
    previous_error_code: ErrorCode
    previous_error: str
    what_was_tried: str
    test_failures: str | None


@dataclass
class TaskState:
    """Task state in executor"""

    task_id: str
    status: str  # pending, running, success, failed, skipped
    attempts: list[TaskAttempt] = field(default_factory=list)
    started_at: str | None = None
    completed_at: str | None = None

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def last_error(self) -> str | None:
        if self.attempts:
            return self.attempts[-1].error
        return None


class ExecutorState:
    """Global executor state backed by SQLite."""

    def __init__(self, config: ExecutorConfig):
        self.config = config
        self.tasks: dict[str, TaskState] = {}
        self.consecutive_failures = 0
        self.total_completed = 0
        self.total_failed = 0
        self._conn: sqlite3.Connection | None = None

        # Migration: JSON -> SQLite (only for .db state files)
        json_path = (
            self.config.state_file.with_suffix(".json")
            if self.config.state_file.suffix == ".db"
            else None
        )

        if json_path and not self.config.state_file.exists() and json_path.exists():
            # Normal migration path
            self._migrate_from_json(json_path)
        elif json_path and self.config.state_file.exists() and json_path.exists():
            # Partial migration recovery: DB was created but JSON wasn't renamed
            self._init_db()
            assert self._conn is not None
            row = self._conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
            if row[0] == 0:
                # DB is empty, re-populate from JSON
                self._conn.close()
                self._conn = None
                self._migrate_from_json(json_path)
        else:
            self._init_db()

        self._load()

    def _init_db(self) -> None:
        """Initialize SQLite database with WAL mode."""
        self.config.state_file.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.config.state_file))
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT,
                completed_at TEXT
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL REFERENCES tasks(task_id),
                timestamp TEXT NOT NULL,
                success INTEGER NOT NULL,
                duration_seconds REAL NOT NULL,
                error TEXT,
                error_code TEXT,
                claude_output TEXT
            )
        """)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS executor_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Migrate: add token columns if missing (for DBs created before Phase 2)
        cursor = self._conn.execute("PRAGMA table_info(attempts)")
        columns = {row[1] for row in cursor.fetchall()}
        for col, col_type in [
            ("input_tokens", "INTEGER"),
            ("output_tokens", "INTEGER"),
            ("cost_usd", "REAL"),
            ("review_status", "TEXT"),
            ("review_findings", "TEXT"),
        ]:
            if col not in columns:
                self._conn.execute(f"ALTER TABLE attempts ADD COLUMN {col} {col_type}")
        self._conn.commit()

    def _migrate_from_json(self, json_path: Path) -> None:
        """Migrate state from JSON file to SQLite."""
        data = json.loads(json_path.read_text())

        # Init DB first so tables exist
        self._init_db()

        with self._conn:
            # Migrate tasks and attempts
            for task_id, task_data in data.get("tasks", {}).items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO tasks "
                    "(task_id, status, started_at, completed_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        task_id,
                        task_data.get("status", "pending"),
                        task_data.get("started_at"),
                        task_data.get("completed_at"),
                    ),
                )
                for attempt in task_data.get("attempts", []):
                    self._conn.execute(
                        "INSERT INTO attempts "
                        "(task_id, timestamp, success, duration_seconds, "
                        "error, error_code, claude_output) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            task_id,
                            attempt["timestamp"],
                            int(attempt["success"]),
                            attempt["duration_seconds"],
                            attempt.get("error"),
                            attempt.get("error_code"),
                            attempt.get("claude_output"),
                        ),
                    )

            # Migrate meta counters
            for key in (
                "consecutive_failures",
                "total_completed",
                "total_failed",
            ):
                value = data.get(key, 0)
                self._conn.execute(
                    "INSERT OR REPLACE INTO executor_meta (key, value) VALUES (?, ?)",
                    (key, str(value)),
                )

        # Rename JSON to .bak
        bak_path = json_path.with_suffix(".json.bak")
        json_path.rename(bak_path)

    def _load(self) -> None:
        """Load state from SQLite into in-memory dicts."""
        # Load tasks
        cursor = self._conn.execute("SELECT task_id, status, started_at, completed_at FROM tasks")
        for row in cursor.fetchall():
            task_id, status, started_at, completed_at = row
            self.tasks[task_id] = TaskState(
                task_id=task_id,
                status=status,
                started_at=started_at,
                completed_at=completed_at,
            )

        # Load attempts for each task
        cursor = self._conn.execute(
            "SELECT task_id, timestamp, success, duration_seconds, "
            "error, error_code, claude_output, input_tokens, output_tokens, cost_usd, "
            "review_status, review_findings "
            "FROM attempts ORDER BY id"
        )
        for row in cursor.fetchall():
            (
                task_id,
                timestamp,
                success,
                duration_seconds,
                error,
                error_code_str,
                claude_output,
                input_tokens,
                output_tokens,
                cost_usd,
                review_status,
                review_findings,
            ) = row
            error_code: ErrorCode | None = None
            if error_code_str is not None:
                error_code = ErrorCode(error_code_str)
            attempt = TaskAttempt(
                timestamp=timestamp,
                success=bool(success),
                duration_seconds=duration_seconds,
                error=error,
                claude_output=claude_output,
                error_code=error_code,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                review_status=review_status,
                review_findings=review_findings,
            )
            if task_id in self.tasks:
                self.tasks[task_id].attempts.append(attempt)

        # Load meta counters
        cursor = self._conn.execute("SELECT key, value FROM executor_meta")
        meta = {row[0]: row[1] for row in cursor.fetchall()}
        self.consecutive_failures = int(meta.get("consecutive_failures", "0"))
        self.total_completed = int(meta.get("total_completed", "0"))
        self.total_failed = int(meta.get("total_failed", "0"))

    def _save_meta(self) -> None:
        """Persist meta counters to SQLite."""
        for key, value in [
            ("consecutive_failures", str(self.consecutive_failures)),
            ("total_completed", str(self.total_completed)),
            ("total_failed", str(self.total_failed)),
        ]:
            self._conn.execute(
                "INSERT INTO executor_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def _save(self) -> None:
        """Persist current in-memory state to SQLite.

        Called by external code (e.g. executor.py) when direct
        mutations are made to in-memory state outside record_attempt/mark_running.
        """
        with self._conn:
            # Upsert all tasks
            for task_id, ts in self.tasks.items():
                self._conn.execute(
                    "INSERT INTO tasks (task_id, status, started_at, completed_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(task_id) DO UPDATE SET "
                    "status = excluded.status, "
                    "started_at = excluded.started_at, "
                    "completed_at = excluded.completed_at",
                    (task_id, ts.status, ts.started_at, ts.completed_at),
                )
                # Re-sync attempts: delete and re-insert
                self._conn.execute("DELETE FROM attempts WHERE task_id = ?", (task_id,))
                for a in ts.attempts:
                    self._conn.execute(
                        "INSERT INTO attempts "
                        "(task_id, timestamp, success, duration_seconds, "
                        "error, error_code, claude_output, "
                        "input_tokens, output_tokens, cost_usd, "
                        "review_status, review_findings) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            task_id,
                            a.timestamp,
                            int(a.success),
                            a.duration_seconds,
                            a.error,
                            a.error_code.value if a.error_code else None,
                            a.claude_output,
                            a.input_tokens,
                            a.output_tokens,
                            a.cost_usd,
                            a.review_status,
                            a.review_findings,
                        ),
                    )
            self._save_meta()

    def get_task_state(self, task_id: str) -> TaskState:
        if task_id not in self.tasks:
            self.tasks[task_id] = TaskState(task_id=task_id, status="pending")
        return self.tasks[task_id]

    def record_attempt(
        self,
        task_id: str,
        success: bool,
        duration: float,
        error: str | None = None,
        output: str | None = None,
        error_code: ErrorCode | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
        review_status: str | None = None,
        review_findings: str | None = None,
    ) -> None:
        """Record execution attempt with atomic SQLite persistence."""
        state = self.get_task_state(task_id)
        now = datetime.now().isoformat()
        attempt = TaskAttempt(
            timestamp=now,
            success=success,
            duration_seconds=duration,
            error=error,
            claude_output=output,
            error_code=error_code,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            review_status=review_status,
            review_findings=review_findings,
        )
        state.attempts.append(attempt)

        if success:
            state.status = "success"
            state.completed_at = now
            self.consecutive_failures = 0
            self.total_completed += 1
        else:
            if state.attempt_count >= self.config.max_retries:
                state.status = "failed"
                self.total_failed += 1
            self.consecutive_failures += 1

        # Atomic SQL transaction
        with self._conn:
            self._conn.execute(
                "INSERT INTO tasks (task_id, status, started_at, completed_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "status = excluded.status, "
                "started_at = excluded.started_at, "
                "completed_at = excluded.completed_at",
                (task_id, state.status, state.started_at, state.completed_at),
            )
            self._conn.execute(
                "INSERT INTO attempts "
                "(task_id, timestamp, success, duration_seconds, "
                "error, error_code, claude_output, "
                "input_tokens, output_tokens, cost_usd, "
                "review_status, review_findings) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    attempt.timestamp,
                    int(attempt.success),
                    attempt.duration_seconds,
                    attempt.error,
                    attempt.error_code.value if attempt.error_code else None,
                    attempt.claude_output,
                    attempt.input_tokens,
                    attempt.output_tokens,
                    attempt.cost_usd,
                    attempt.review_status,
                    attempt.review_findings,
                ),
            )
            self._save_meta()

    def mark_running(self, task_id: str) -> None:
        """Mark task as running with atomic SQLite persistence."""
        state = self.get_task_state(task_id)
        state.status = "running"
        state.started_at = datetime.now().isoformat()

        with self._conn:
            self._conn.execute(
                "INSERT INTO tasks (task_id, status, started_at, completed_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(task_id) DO UPDATE SET "
                "status = excluded.status, "
                "started_at = excluded.started_at, "
                "completed_at = excluded.completed_at",
                (task_id, state.status, state.started_at, state.completed_at),
            )
            self._save_meta()

    def should_stop(self) -> bool:
        """Check if we should stop (consecutive failures or budget exceeded)."""
        if self.consecutive_failures >= self.config.max_consecutive_failures:
            return True
        return self.config.budget_usd is not None and self.total_cost() > self.config.budget_usd

    def total_cost(self) -> float:
        """Sum of cost_usd across all attempts."""
        return sum(
            a.cost_usd for ts in self.tasks.values() for a in ts.attempts if a.cost_usd is not None
        )

    def task_cost(self, task_id: str) -> float:
        """Sum of cost_usd for a specific task."""
        ts = self.tasks.get(task_id)
        if not ts:
            return 0.0
        return sum(a.cost_usd for a in ts.attempts if a.cost_usd is not None)

    def total_tokens(self) -> tuple[int, int]:
        """(total_input_tokens, total_output_tokens) across all attempts."""
        inp = sum(
            a.input_tokens
            for ts in self.tasks.values()
            for a in ts.attempts
            if a.input_tokens is not None
        )
        out = sum(
            a.output_tokens
            for ts in self.tasks.values()
            for a in ts.attempts
            if a.output_tokens is not None
        )
        return inp, out

    def close(self) -> None:
        """Close the SQLite connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False  # Don't suppress exceptions


def check_stop_requested(config: ExecutorConfig) -> bool:
    """Check if graceful shutdown was requested via stop file."""
    return config.stop_file.exists()


def clear_stop_file(config: ExecutorConfig) -> None:
    """Remove stop file if it exists."""
    with contextlib.suppress(FileNotFoundError):
        config.stop_file.unlink()
