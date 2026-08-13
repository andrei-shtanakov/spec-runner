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
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .claims import Claim as ClaimT
    from .claims import ClaimStatus as ClaimStatusT
    from .gates import GateStatus as GateStatusT
    from .remedy import RemedyRecord as RemedyRecordT
    from .tdd import RedCheckpoint as RedCheckpointT

from .config import ExecutorConfig

# === State Management ===


class ErrorCode(str, Enum):
    """Structured error classification for task failures."""

    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    TEST_FAILURE = "TEST_FAILURE"
    LINT_FAILURE = "LINT_FAILURE"
    TASK_FAILED = "TASK_FAILED"
    # A refusal the agent issued deliberately: the task cannot be done within
    # the rules and needs an operator, as opposed to "I did not manage it"
    # (#140). Terminal — never retried.
    TASK_BLOCKED = "TASK_BLOCKED"
    HOOK_FAILURE = "HOOK_FAILURE"
    # The instrument broke, so the run could not find out whether the work is
    # good (#141 battle test, F-2). Distinct from HOOK_FAILURE because "the
    # gate says no" and "the gate could not answer" are the two facts
    # `GateStatus` separates, and CI deserves the same distinction.
    INFRASTRUCTURE = "INFRASTRUCTURE"
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
    # A review that did not produce a verdict — timed out, returned nothing, or
    # said nothing recognizable (#138). Distinct from SKIPPED, which means the
    # stage was deliberately not run, and emphatically distinct from PASSED,
    # which is what silence used to be recorded as.
    NOT_RUN = "not_run"
    # The review machinery itself failed: the CLI errored, hit a rate limit, or
    # raised. Nothing was learned about the code, and the cause is ours to fix,
    # not the agent's.
    ERROR = "error"


class PhaseOutcome(str, Enum):
    """What a phase actually did (slice 0 of the lifecycle contract).

    Six values, because each implies a different move by whoever reads it:
    proceed; proceed (in TDD); fix the work; investigate the agent; fix the
    environment; nothing to do.

    Lives here beside `ErrorCode` and `ReviewVerdict` because it is state
    vocabulary; the per-stage rules and the review mapping are in `phases.py`.
    """

    #: ran; its expectation held
    PASS = "pass"
    #: ran; failed exactly as it was supposed to (TDD's confirmed red)
    EXPECTED_FAIL = "expected_fail"
    #: ran; failed some other way. A test failing on a typo in an import looks
    #: exactly like an honest red, and without this split it *becomes* one.
    UNEXPECTED_FAIL = "unexpected_fail"
    #: ran, but produced no usable verdict — a timeout, an empty response,
    #: output with no marker. Not a pass and not a failure; recording it as
    #: either is the defect #138 was built to remove.
    NOT_RUN = "not_run"
    #: could not run — the instrument itself broke. A different fix and a
    #: different owner than a failure of the work.
    ERROR = "error"
    #: deliberately not executed (disabled by config, or unreachable because an
    #: earlier phase already failed). A non-event, not a gap in the evidence.
    SKIPPED = "skipped"

    #: There is deliberately no WAIVED: a result is what the instrument
    #: observed, a waiver is an operator overriding it. See
    #: `ExecutorState.record_waiver`.


@dataclass(frozen=True)
class PhaseRecord:
    """One observed outcome of one phase, as stored. Append-only."""

    phase: str
    outcome: PhaseOutcome
    detail: str | None
    timestamp: str


@dataclass(frozen=True)
class GateVerdict:
    """A stored gate answer, bound to the tree and policy it judged."""

    gate_id: str
    checkpoint_sha: str
    config_hash: str
    status: "GateStatusT"
    detail: str | None
    timestamp: str


@dataclass(frozen=True)
class PhaseWaiver:
    """An operator overriding an observed outcome (never the harness).

    The waived outcome is kept: a report that shows green for a waived phase
    must be able to show that it was waived, and by whom.
    """

    phase: str
    waived_outcome: PhaseOutcome
    reason: str
    actor: str
    timestamp: str
    provenance: str | None = None


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
    error_kind: str | None = None  # v2.3.0: classified by errors.classify
    error_stage: str | None = None  # v2.3.0: stage when failure occurred
    no_op: bool = False  # v2.16.0: task completed without any committable changes (#97)


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


_DISK_FULL_MARKERS = (
    "disk i/o error",
    "database or disk is full",
    "disk full",
    "out of memory",  # SQLite raises this when mmap-backed writes can't extend
    "no space left on device",
)


def _is_disk_full_error(exc: sqlite3.OperationalError) -> bool:
    """Classify an OperationalError as disk-full vs another failure.

    SQLite does not expose a stable error code for disk-full via sqlite3, so we
    match on the textual message. Covers the common POSIX and SQLite phrases.
    """
    message = str(exc).lower()
    return any(marker in message for marker in _DISK_FULL_MARKERS)


class ExecutorState:
    """Global executor state backed by SQLite."""

    def __init__(self, config: ExecutorConfig):
        self.config = config
        self.tasks: dict[str, TaskState] = {}
        self.consecutive_failures = 0
        self.total_completed = 0
        self.total_failed = 0
        self._conn: sqlite3.Connection | None = None
        # Degraded mode: SQLite writes are failing (typically disk-full or
        # corruption). In-memory state keeps working so the current run can
        # finish, but on-disk persistence is lost until the operator fixes the
        # underlying issue.
        self._degraded: bool = False
        self._degraded_reason: str | None = None
        self._degraded_notified: bool = False
        # Optional compliance audit trail. Opt-in via `audit_log_path` in the
        # project config; otherwise a no-op logger. Created lazily so tests
        # that construct ExecutorState with tmp state files don't
        # accidentally create audit files in the CWD.
        from .audit_log import build_audit_logger

        self.audit_logger = build_audit_logger(config)

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
        self._conn.execute("PRAGMA busy_timeout=30000")
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
        # v2.3.0: add error_kind and error_stage to attempts (idempotent)
        for col in ("error_kind", "error_stage"):
            with contextlib.suppress(sqlite3.OperationalError):
                self._conn.execute(f"ALTER TABLE attempts ADD COLUMN {col} TEXT")
        # v2.16.0: no-op completion marker (#97; idempotent)
        with contextlib.suppress(sqlite3.OperationalError):
            self._conn.execute("ALTER TABLE attempts ADD COLUMN no_op INTEGER")
        # Slice 0 of the lifecycle contract (#164/#141 Part A): a typed outcome
        # per phase, append-only — a phase runs again on a retry and the earlier
        # verdicts are evidence, not noise. Nothing gates on these yet.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS phase_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                outcome TEXT NOT NULL,
                detail TEXT,
                timestamp TEXT NOT NULL
            )
        """)
        # A waiver is not an outcome: it is an operator overriding one, and it
        # carries who, why and when precisely because that is the information a
        # waiver exists to preserve.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS phase_waivers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                waived_outcome TEXT NOT NULL,
                reason TEXT NOT NULL,
                actor TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                provenance TEXT
            )
        """)
        # #164: a gate verdict is a statement about a specific tree under a
        # specific policy — hence the (checkpoint_sha, config_hash) key. A
        # verdict for another pair is not this one's, which is what stops
        # evidence from before a change legitimising the change.
        # #141: a verified RED. Keyed by (task_id, namespace) on read because
        # identical TASK-NNN ids from different workstreams collide once their
        # branches meet — the pilot nearly restored one task's claim from
        # another's honest red.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS red_checkpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                commit_sha TEXT NOT NULL,
                baseline_sha TEXT NOT NULL,
                selector TEXT NOT NULL,
                environment_id TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                outcome TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active'
            )
        """)
        # A dev DB from before slice 3 has no `status`; add it rather than
        # rebuild, and default it to active so existing checkpoints keep
        # counting.
        if "status" not in {
            row[1] for row in self._conn.execute("PRAGMA table_info(red_checkpoints)")
        }:
            self._conn.execute(
                "ALTER TABLE red_checkpoints ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
            )
        # #141 F-6: every agent invocation, with the phase that made it. The
        # RED pass's cost was simply discarded, so a TDD run's extra call was
        # invisible in `costs` — money nobody could see.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                provenance TEXT NOT NULL,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cost_usd REAL,
                timestamp TEXT NOT NULL
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_agent_calls_task ON agent_calls (task_id)"
        )
        # #141 slice 4a: where a task is in the TDD lifecycle. Append-only —
        # where it has *been* is evidence, including refused transitions.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS tdd_phases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                namespace TEXT NOT NULL,
                phase TEXT NOT NULL,
                detail TEXT,
                timestamp TEXT NOT NULL
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tdd_phases_lookup "
            "ON tdd_phases (namespace, task_id, id)"
        )
        # #141 slice 3: remedies are authority decisions, kept forever.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS tdd_remedies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace TEXT NOT NULL,
                task_id TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                reason TEXT NOT NULL,
                actor TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                new_checkpoint_id TEXT
            )
        """)
        # #230 part 2: an operator raising a ceiling, kept forever. The CHECKs
        # are the sign-off's correction made unrepresentable rather than
        # documented: `budget_usd` bounds the whole DB domain, so a run-scope
        # row carrying a namespace would give each workstream its own "global"
        # cap — three namespaces, three global limits, no global limit.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS budget_authorizations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain_id TEXT NOT NULL,
                scope TEXT NOT NULL,
                task_id TEXT,
                namespace TEXT,
                previous_limit_usd REAL,
                new_limit_usd REAL NOT NULL,
                recorded_spend_usd REAL NOT NULL,
                unmeasured_calls INTEGER NOT NULL,
                actor TEXT NOT NULL,
                reason TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                CHECK (scope IN ('task', 'run')),
                CHECK (scope != 'run' OR (namespace IS NULL AND task_id IS NULL)),
                CHECK (scope != 'task' OR task_id IS NOT NULL)
            )
        """)
        # #141 slice 2: a claim's own table, not a JSON column on the
        # checkpoint — enforcement queries by (namespace, path, status) across
        # tasks, and two tasks claiming one path is the case that has to be
        # queryable rather than parsed out of every row.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS tdd_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                namespace TEXT NOT NULL,
                task_id TEXT NOT NULL,
                checkpoint_id TEXT NOT NULL,
                checkpoint_sha TEXT NOT NULL,
                path TEXT NOT NULL,
                blob_sha TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL
            )
        """)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tdd_claims_lookup "
            "ON tdd_claims (namespace, status, path)"
        )
        # Reads are always (task_id, namespace) → latest, so give that pattern
        # an index rather than let it become a table scan as TDD mode
        # accumulates a row per task per retry.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_red_checkpoints_lookup "
            "ON red_checkpoints (task_id, namespace, id DESC)"
        )
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS gate_verdicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                gate_id TEXT NOT NULL,
                checkpoint_sha TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                status TEXT NOT NULL,
                detail TEXT,
                timestamp TEXT NOT NULL
            )
        """)
        self._conn.commit()

    def _migrate_from_json(self, json_path: Path) -> None:
        """Migrate state from JSON file to SQLite."""
        data = json.loads(json_path.read_text())

        # Init DB first so tables exist
        self._init_db()

        assert self._conn is not None
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
        assert self._conn is not None
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
            "review_status, review_findings, error_kind, error_stage, no_op "
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
                error_kind,
                error_stage,
                no_op,
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
                error_kind=error_kind,
                error_stage=error_stage,
                no_op=bool(no_op),
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
        assert self._conn is not None
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
        assert self._conn is not None
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
                        "review_status, review_findings, "
                        "error_kind, error_stage, no_op) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                            a.error_kind,
                            a.error_stage,
                            int(a.no_op),
                        ),
                    )
            self._save_meta()

    def get_task_state(self, task_id: str) -> TaskState:
        if task_id not in self.tasks:
            self.tasks[task_id] = TaskState(task_id=task_id, status="pending")
        return self.tasks[task_id]

    # === Phase results (slice 0; nothing gates on these yet) ===

    def _insert_phase_row(self, sql: str, params: tuple) -> None:
        """Single write seam, so recording can be faulted in tests."""
        assert self._conn is not None
        with self._conn:
            self._conn.execute(sql, params)

    def record_phase(
        self,
        task_id: str,
        phase: str,
        outcome: "PhaseOutcome",
        detail: str | None = None,
    ) -> None:
        """Append one observed outcome for ``phase``.

        Append-only: a phase runs again on a retry, and the earlier verdicts
        are evidence, not noise.

        Raises ``ValueError`` for an unknown phase or an outcome that phase
        cannot produce — that is a caller bug and should be loud. A *storage*
        failure is not: recording is additive bookkeeping and must never be
        able to fail a task, so it is logged and swallowed, the same posture as
        degraded mode.
        """
        from .phases import check_outcome

        check_outcome(phase, outcome)
        try:
            self._insert_phase_row(
                "INSERT INTO phase_results (task_id, phase, outcome, detail, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, phase, outcome.value, detail, datetime.now().isoformat()),
            )
        except Exception as exc:  # never fail a run over bookkeeping
            from .logging import get_logger

            get_logger("state").warning(
                "Could not record phase outcome",
                task_id=task_id,
                phase=phase,
                error=str(exc),
            )

    def record_waiver(
        self,
        task_id: str,
        phase: str,
        waived_outcome: "PhaseOutcome",
        reason: str,
        actor: str,
        provenance: str | None = None,
    ) -> None:
        """Record an operator overriding an observed outcome.

        Never called by the harness: a waiver is an authority decision, and
        ``actor``/``reason`` are required precisely because "who decided, and
        why" is the information a waiver exists to preserve. The observed
        outcome stays in ``phase_results`` — a waiver annotates history, it
        does not rewrite it.
        """
        if not actor.strip():
            raise ValueError("a waiver needs an actor: an unattributed override is not a decision")
        if not reason.strip():
            raise ValueError("a waiver needs a reason")
        from .phases import check_outcome

        check_outcome(phase, waived_outcome)
        self._insert_phase_row(
            "INSERT INTO phase_waivers "
            "(task_id, phase, waived_outcome, reason, actor, timestamp, provenance) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                phase,
                waived_outcome.value,
                reason,
                actor,
                datetime.now().isoformat(),
                provenance,
            ),
        )

    def phase_history(self, task_id: str) -> list[PhaseRecord]:
        """Every recorded outcome for ``task_id``, oldest first."""
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT phase, outcome, detail, timestamp FROM phase_results "
            "WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [PhaseRecord(r[0], PhaseOutcome(r[1]), r[2], r[3]) for r in rows]

    def phase_waivers(self, task_id: str) -> list[PhaseWaiver]:
        """Operator waivers recorded for ``task_id``, oldest first."""
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT phase, waived_outcome, reason, actor, timestamp, provenance "
            "FROM phase_waivers WHERE task_id = ? ORDER BY id",
            (task_id,),
        ).fetchall()
        return [PhaseWaiver(r[0], PhaseOutcome(r[1]), r[2], r[3], r[4], r[5]) for r in rows]

    def record_gate_verdict(
        self,
        task_id: str,
        gate_id: str,
        checkpoint_sha: str,
        config_hash: str,
        status: "GateStatusT",
        detail: str | None = None,
    ) -> None:
        """Store one gate verdict, bound to the tree and policy it judged."""
        try:
            self._insert_phase_row(
                "INSERT INTO gate_verdicts "
                "(task_id, gate_id, checkpoint_sha, config_hash, status, detail, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    gate_id,
                    checkpoint_sha,
                    config_hash,
                    getattr(status, "value", status),
                    detail,
                    datetime.now().isoformat(),
                ),
            )
        except Exception as exc:  # bookkeeping must not fail a run
            from .logging import get_logger

            get_logger("state").warning(
                "Could not record gate verdict", task_id=task_id, gate=gate_id, error=str(exc)
            )

    def gate_verdict(
        self,
        task_id: str,
        gate_id: str,
        checkpoint_sha: str,
        config_hash: str,
    ) -> "GateVerdict | None":
        """The latest verdict **for this exact tree and policy**, or None.

        Deliberately not "the latest verdict for this task": a stale answer
        about an older SHA, or one taken under a different policy, must not
        clear the current one (#164 criterion 5).
        """
        from .gates import GateStatus

        assert self._conn is not None
        row = self._conn.execute(
            "SELECT gate_id, checkpoint_sha, config_hash, status, detail, timestamp "
            "FROM gate_verdicts WHERE task_id = ? AND gate_id = ? "
            "AND checkpoint_sha = ? AND config_hash = ? ORDER BY id DESC LIMIT 1",
            (task_id, gate_id, checkpoint_sha, config_hash),
        ).fetchone()
        if row is None:
            return None
        return GateVerdict(row[0], row[1], row[2], GateStatus(row[3]), row[4], row[5])

    def record_red_checkpoint(self, checkpoint: "RedCheckpointT") -> None:
        """Persist a verified RED checkpoint (#141). Append-only."""
        try:
            self._insert_phase_row(
                "INSERT INTO red_checkpoints (task_id, namespace, commit_sha, baseline_sha, "
                "selector, environment_id, execution_mode, config_hash, outcome, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    checkpoint.task_id,
                    checkpoint.namespace,
                    checkpoint.commit_sha,
                    checkpoint.baseline_sha,
                    checkpoint.selector,
                    checkpoint.environment_id,
                    checkpoint.execution_mode,
                    checkpoint.config_hash,
                    getattr(checkpoint.outcome, "value", checkpoint.outcome),
                    checkpoint.timestamp or datetime.now().isoformat(),
                ),
            )
        except Exception as exc:  # bookkeeping must not fail a run
            from .logging import get_logger

            get_logger("state").warning(
                "Could not record red checkpoint", task_id=checkpoint.task_id, error=str(exc)
            )

    def red_checkpoint(self, task_id: str, namespace: str) -> "RedCheckpointT | None":
        """The latest **active** checkpoint for this task in this workstream.

        Only active counts: one retired by `tdd abandon` or superseded by
        `tdd repair` is still evidence, but it is no longer this task's
        standing claim (#141 slice 3).

        Deliberately namespaced: a checkpoint from another workstream is not
        this task's evidence, however identical the id.
        """
        from .tdd import RedCheckpoint, RedOutcome

        assert self._conn is not None
        row = self._conn.execute(
            "SELECT task_id, namespace, commit_sha, baseline_sha, selector, environment_id, "
            "execution_mode, config_hash, outcome, timestamp FROM red_checkpoints "
            "WHERE task_id = ? AND namespace = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
            (task_id, namespace),
        ).fetchone()
        if row is None:
            return None
        return RedCheckpoint(
            task_id=row[0],
            namespace=row[1],
            commit_sha=row[2],
            baseline_sha=row[3],
            selector=row[4],
            environment_id=row[5],
            execution_mode=row[6],
            config_hash=row[7],
            outcome=RedOutcome(row[8]),
            timestamp=row[9],
        )

    def record_claim(self, claim: "ClaimT") -> None:
        """Persist one file claim (#141 slice 2). **Raises** on failure.

        Deliberately not the swallow-and-log posture of the other bookkeeping
        writers. Nothing gates on a `phase_results` row, so losing one costs
        visibility; the gate *does* read claims, so a lost claim is not a
        missing note — it is a byte-lock that silently does not exist while the
        run believes it does. Fail closed.
        """
        self._insert_phase_row(
            "INSERT INTO tdd_claims (namespace, task_id, checkpoint_id, checkpoint_sha, "
            "path, blob_sha, created_at, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                claim.namespace,
                claim.task_id,
                claim.checkpoint_id,
                claim.checkpoint_sha,
                claim.path,
                claim.blob_sha,
                claim.created_at,
                getattr(claim.status, "value", claim.status),
            ),
        )

    def active_claims(self, namespace: str) -> list["ClaimT"]:
        """Every claim still in force in ``namespace``, whoever made it.

        Not filtered by task on purpose: checking only the current task's
        claims is exactly the hole the pilot found — neighbouring tests left
        guarded by prompt text rather than by the instrument.
        """
        from .claims import Claim, ClaimStatus

        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT namespace, task_id, checkpoint_id, checkpoint_sha, path, blob_sha, "
            "created_at, status FROM tdd_claims WHERE namespace = ? AND status = ? ORDER BY id",
            (namespace, ClaimStatus.ACTIVE.value),
        ).fetchall()
        return [
            Claim(
                namespace=r[0],
                task_id=r[1],
                checkpoint_id=r[2],
                checkpoint_sha=r[3],
                path=r[4],
                blob_sha=r[5],
                created_at=r[6],
                status=ClaimStatus(r[7]),
            )
            for r in rows
        ]

    def supersede_claims(
        self,
        namespace: str,
        task_id: str,
        status: "ClaimStatusT",
        checkpoint_id: str | None = None,
    ) -> int:
        """Retire claims. Nothing is deleted — the row stays with its new
        status, because a retired claim is still evidence.

        ``checkpoint_id`` scopes it to one **lineage**. A task can hold claims
        from more than one after a repair, and a remedy aimed at a specific
        checkpoint must not sweep claims belonging to another (F-3).
        """
        assert self._conn is not None
        from .claims import ClaimStatus

        sql = "UPDATE tdd_claims SET status = ? WHERE namespace = ? AND task_id = ? AND status = ?"
        params: list[object] = [
            getattr(status, "value", status),
            namespace,
            task_id,
            ClaimStatus.ACTIVE.value,
        ]
        if checkpoint_id is not None:
            sql += " AND checkpoint_id = ?"
            params.append(checkpoint_id)
        cursor = self._conn.execute(sql, params)
        self._conn.commit()
        return cursor.rowcount

    def checkpoint_by_id(self, namespace: str, checkpoint_id: str) -> "RedCheckpointT | None":
        """Any checkpoint by its derived id, whatever its status.

        Unlike `red_checkpoint`, this deliberately ignores status: a caller
        asking for a specific lineage wants that lineage, including a
        superseded one.
        """
        from .tdd import RedCheckpoint, RedOutcome

        assert self._conn is not None
        # Iterate the cursor rather than `fetchall()`: the loop returns on the
        # first match, so materialising every checkpoint in the namespace only
        # to discard it is waste that grows with the workstream's history.
        cursor = self._conn.execute(
            "SELECT task_id, namespace, commit_sha, baseline_sha, selector, environment_id, "
            "execution_mode, config_hash, outcome, timestamp FROM red_checkpoints "
            "WHERE namespace = ? ORDER BY id DESC",
            (namespace,),
        )
        for r in cursor:
            candidate = RedCheckpoint(
                task_id=r[0],
                namespace=r[1],
                commit_sha=r[2],
                baseline_sha=r[3],
                selector=r[4],
                environment_id=r[5],
                execution_mode=r[6],
                config_hash=r[7],
                outcome=RedOutcome(r[8]),
                timestamp=r[9],
            )
            if candidate.checkpoint_id == checkpoint_id:
                return candidate
        return None

    def active_checkpoints(
        self, namespace: str, task_id: str | None = None
    ) -> list["RedCheckpointT"]:
        """Every **active** checkpoint, newest first.

        `red_checkpoint` returns only the latest, which hides the case an
        operator most needs to see: more than one active lineage for a task,
        where a remedy must not guess which is meant (F-5).
        """
        from .tdd import RedCheckpoint, RedOutcome

        assert self._conn is not None
        sql = (
            "SELECT task_id, namespace, commit_sha, baseline_sha, selector, environment_id, "
            "execution_mode, config_hash, outcome, timestamp FROM red_checkpoints "
            "WHERE namespace = ? AND status = 'active'"
        )
        params: list[object] = [namespace]
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        rows = self._conn.execute(sql + " ORDER BY id DESC", params).fetchall()
        return [
            RedCheckpoint(
                task_id=r[0],
                namespace=r[1],
                commit_sha=r[2],
                baseline_sha=r[3],
                selector=r[4],
                environment_id=r[5],
                execution_mode=r[6],
                config_hash=r[7],
                outcome=RedOutcome(r[8]),
                timestamp=r[9],
            )
            for r in rows
        ]

    def retired_checkpoints(self, namespace: str, task_id: str | None = None) -> list[tuple]:
        """``(task_id, checkpoint status, outcome, selector, timestamp)`` for
        checkpoints no longer active — the trail a remedy leaves behind."""
        assert self._conn is not None
        sql = (
            "SELECT task_id, status, outcome, selector, timestamp FROM red_checkpoints "
            "WHERE namespace = ? AND status != 'active'"
        )
        params: list[object] = [namespace]
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        return self._conn.execute(sql + " ORDER BY id", params).fetchall()

    def claims_for(self, namespace: str, task_id: str | None = None) -> list[tuple]:
        """``(task_id, path, blob_sha, status, checkpoint_id)``, all statuses."""
        assert self._conn is not None
        sql = (
            "SELECT task_id, path, blob_sha, status, checkpoint_id FROM tdd_claims "
            "WHERE namespace = ?"
        )
        params: list[object] = [namespace]
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        return self._conn.execute(sql + " ORDER BY id", params).fetchall()

    def set_checkpoint_status(self, namespace: str, checkpoint_id: str, status) -> int:
        """Retire a checkpoint. Nothing is deleted — the row keeps its history
        and gains a new standing."""
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT id, task_id, commit_sha, selector, timestamp FROM red_checkpoints "
            "WHERE namespace = ?",
            (namespace,),
        ).fetchall()
        from .tdd import RedCheckpoint

        changed = 0
        for row in rows:
            probe = RedCheckpoint(
                task_id=row[1],
                namespace=namespace,
                commit_sha=row[2],
                baseline_sha="",
                selector=row[3],
                environment_id="",
                execution_mode="",
                config_hash="",
                timestamp=row[4],
            )
            if probe.checkpoint_id == checkpoint_id:
                self._conn.execute(
                    "UPDATE red_checkpoints SET status = ? WHERE id = ?",
                    (getattr(status, "value", status), row[0]),
                )
                changed += 1
        self._conn.commit()
        return changed

    def reinstate_checkpoint_with_claims(
        self, namespace: str, task_id: str, checkpoint_id: str
    ) -> tuple[int, int]:
        """Make a retired checkpoint standing again, **with its own claims**,
        in one transaction (#232). Returns `(checkpoints, claims)` changed.

        The atomicity is the safety property, not an optimisation. A resume
        that reinstated a red and then failed to reinstate its byte-lock would
        leave exactly the state the whole design forbids — a confirmed red whose
        evidence nothing protects — and it would leave it while reporting
        success. Refusing outright is better than that, so both flips share one
        `with self._conn` block and roll back together.

        Only claims recorded **for this checkpoint** are touched. A claim
        retired for its own unrelated reasons stays retired; resume is not an
        amnesty.
        """
        from .claims import ClaimStatus
        from .tdd import RedCheckpoint

        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT id, task_id, commit_sha, selector, timestamp FROM red_checkpoints "
            "WHERE namespace = ? AND task_id = ?",
            (namespace, task_id),
        ).fetchall()
        target_ids = [
            row[0]
            for row in rows
            if RedCheckpoint(
                task_id=row[1],
                namespace=namespace,
                commit_sha=row[2],
                baseline_sha="",
                selector=row[3],
                environment_id="",
                execution_mode="",
                config_hash="",
                timestamp=row[4],
            ).checkpoint_id
            == checkpoint_id
        ]
        if not target_ids:
            # Nothing to reinstate — and therefore nothing to reinstate the
            # claims *of*. Touching them anyway would activate a byte-lock with
            # no standing red behind it: the inverse of the hazard this method
            # exists to prevent, produced by the method itself (Copilot, #244).
            raise ValueError(
                f"no checkpoint {checkpoint_id} for {task_id} in {namespace}; "
                "refusing to reinstate claims that would stand alone"
            )
        with self._conn:
            checkpoints = 0
            for row_id in target_ids:
                self._conn.execute(
                    "UPDATE red_checkpoints SET status = 'active' WHERE id = ?", (row_id,)
                )
                checkpoints += 1
            cur = self._conn.execute(
                "UPDATE tdd_claims SET status = ? WHERE namespace = ? AND task_id = ? "
                "AND checkpoint_id = ? AND status != ?",
                (
                    ClaimStatus.ACTIVE.value,
                    namespace,
                    task_id,
                    checkpoint_id,
                    ClaimStatus.ACTIVE.value,
                ),
            )
            claims = int(cur.rowcount or 0)
        return checkpoints, claims

    def claims_of_checkpoint(self, namespace: str, checkpoint_id: str) -> list[dict]:
        """Every claim recorded for one lineage, whatever its status."""
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT task_id, path, blob_sha, status FROM tdd_claims "
            "WHERE namespace = ? AND checkpoint_id = ? ORDER BY id",
            (namespace, checkpoint_id),
        ).fetchall()
        return [{"task_id": r[0], "path": r[1], "blob_sha": r[2], "status": r[3]} for r in rows]

    def confirmed_reds(self, namespace: str, task_id: str) -> list["RedCheckpointT"]:
        """Every checkpoint that ever confirmed a red, **any status**, newest first.

        Supersession retires a lineage; it does not unhappen the observation.
        `resume` needs the evidence, and the evidence is what `expected_fail`
        recorded — the pilot's confirmed red is `superseded` and still true.

        A list rather than "the newest", because more than one is a case an
        authority decision must not resolve by guessing (F-5).
        """
        from .tdd import RedCheckpoint, RedOutcome

        assert self._conn is not None
        cursor = self._conn.execute(
            "SELECT task_id, namespace, commit_sha, baseline_sha, selector, environment_id, "
            "execution_mode, config_hash, outcome, timestamp, status FROM red_checkpoints "
            "WHERE namespace = ? AND task_id = ? AND outcome = ? ORDER BY id DESC",
            (namespace, task_id, RedOutcome.EXPECTED_FAIL.value),
        )
        return [
            RedCheckpoint(
                task_id=row[0],
                namespace=row[1],
                commit_sha=row[2],
                baseline_sha=row[3],
                selector=row[4],
                environment_id=row[5],
                execution_mode=row[6],
                config_hash=row[7],
                outcome=RedOutcome(row[8]),
                timestamp=row[9],
            )
            for row in cursor
        ]

    def record_remedy(self, remedy: "RemedyRecordT") -> None:
        """Persist one remedy. **Raises** on failure — like a claim and for the
        same reason: a remedy nobody can find is indistinguishable from one that
        never happened."""
        self._insert_phase_row(
            "INSERT INTO tdd_remedies (namespace, task_id, checkpoint_id, operation, reason, "
            "actor, timestamp, new_checkpoint_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                remedy.namespace,
                remedy.task_id,
                remedy.checkpoint_id,
                getattr(remedy.operation, "value", remedy.operation),
                remedy.reason,
                remedy.actor,
                remedy.timestamp,
                remedy.new_checkpoint_id,
            ),
        )

    def remedies(self, task_id: str, namespace: str) -> list["RemedyRecordT"]:
        """Every remedy taken on this task in this workstream, oldest first."""
        from .remedy import RemedyOperation, RemedyRecord

        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT namespace, task_id, checkpoint_id, operation, reason, actor, timestamp, "
            "new_checkpoint_id FROM tdd_remedies WHERE task_id = ? AND namespace = ? ORDER BY id",
            (task_id, namespace),
        ).fetchall()
        return [
            RemedyRecord(
                namespace=r[0],
                task_id=r[1],
                checkpoint_id=r[2],
                operation=RemedyOperation(r[3]),
                reason=r[4],
                actor=r[5],
                timestamp=r[6],
                new_checkpoint_id=r[7],
            )
            for r in rows
        ]

    def record_agent_call(
        self,
        task_id: str,
        provenance: str,
        *,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        """Record one agent invocation and what it cost (#141 F-6).

        Best-effort like the other bookkeeping: an accounting row must not be
        able to fail a task. Unlike a claim, nothing *gates* on it — losing one
        costs visibility, which is the very thing being fixed, so it is logged
        loudly rather than swallowed silently.
        """
        try:
            self._insert_phase_row(
                "INSERT INTO agent_calls "
                "(task_id, provenance, input_tokens, output_tokens, cost_usd, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    provenance,
                    input_tokens,
                    output_tokens,
                    cost_usd,
                    datetime.now().isoformat(),
                ),
            )
        except Exception as exc:
            from .logging import get_logger

            get_logger("state").warning(
                "Could not record agent call cost",
                task_id=task_id,
                provenance=provenance,
                error=str(exc),
            )

    def agent_calls(self, task_id: str | None = None) -> list[dict]:
        """Recorded agent invocations, oldest first."""
        assert self._conn is not None
        sql = (
            "SELECT task_id, provenance, input_tokens, output_tokens, cost_usd, timestamp "
            "FROM agent_calls"
        )
        params: list[object] = []
        if task_id:
            sql += " WHERE task_id = ?"
            params.append(task_id)
        rows = self._conn.execute(sql + " ORDER BY id", params).fetchall()
        return [
            {
                "task_id": r[0],
                "provenance": r[1],
                "input_tokens": r[2],
                "output_tokens": r[3],
                "cost_usd": r[4],
                "timestamp": r[5],
            }
            for r in rows
        ]

    def _ledger_cost(self, task_id: str | None = None) -> float:
        """Cost from the agent-call ledger — calls whose money is *not* on an
        attempt (the exec pass keeps its own columns, for the state schema)."""
        sql = "SELECT COALESCE(SUM(cost_usd), 0.0) FROM agent_calls"
        params: list[object] = []
        if task_id:
            sql += " WHERE task_id = ?"
            params.append(task_id)
        try:
            assert self._conn is not None
            return float(self._conn.execute(sql, params).fetchone()[0] or 0.0)
        except Exception:
            # Degraded mode: the in-memory state keeps serving the run, and a
            # cost *report* must not be the thing that raises. Under-reporting
            # here is visible in the same place the degradation is.
            return 0.0

    def budget_domain_id(self) -> str:
        """This state file's budget domain, minted on first use (#230 part 2).

        The domain is **the state DB**, and this id is what makes that
        mechanical rather than a rule people remember. A new state file mints a
        new id, so it inherits no authorization and no spend — which is exactly
        what happened by accident in the pilot, where three attempts ran
        against three state files and the cap that refused had never seen the
        earlier $1.19.
        """
        from uuid import uuid4

        existing = self.get_meta("budget_domain_id")
        if existing:
            return existing
        minted = uuid4().hex[:16]
        self.set_meta("budget_domain_id", minted)
        return minted

    def record_budget_authorization(
        self,
        *,
        scope: str,
        new_limit_usd: float,
        recorded_spend_usd: float,
        unmeasured_calls: int,
        actor: str,
        reason: str,
        task_id: str | None = None,
        namespace: str | None = None,
        previous_limit_usd: float | None = None,
    ) -> int:
        """Append one authorization and return its id. Never updates a row."""
        assert self._conn is not None
        with self._conn:
            cur = self._conn.execute(
                "INSERT INTO budget_authorizations (domain_id, scope, task_id, namespace, "
                "previous_limit_usd, new_limit_usd, recorded_spend_usd, unmeasured_calls, "
                "actor, reason, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.budget_domain_id(),
                    scope,
                    task_id,
                    namespace,
                    previous_limit_usd,
                    new_limit_usd,
                    recorded_spend_usd,
                    unmeasured_calls,
                    actor,
                    reason,
                    datetime.now().isoformat(),
                ),
            )
        return int(cur.lastrowid or 0)

    def latest_budget_authorization(
        self, scope: str, task_id: str | None = None, namespace: str | None = None
    ) -> dict | None:
        """The standing authorization for a scope in *this* domain, or None.

        Scoping is the sign-off's: a task ceiling is `(domain, namespace,
        task)`; a run ceiling is the domain's, with no namespace at all.
        """
        assert self._conn is not None
        sql = (
            "SELECT id, scope, task_id, namespace, previous_limit_usd, new_limit_usd, "
            "recorded_spend_usd, unmeasured_calls, actor, reason, timestamp "
            "FROM budget_authorizations WHERE domain_id = ? AND scope = ?"
        )
        params: list[object] = [self.budget_domain_id(), scope]
        if scope == "task":
            sql += " AND task_id = ? AND namespace IS ?"
            params += [task_id, namespace]
        sql += " ORDER BY id DESC LIMIT 1"
        try:
            row = self._conn.execute(sql, params).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        cols = (
            "id",
            "scope",
            "task_id",
            "namespace",
            "previous_limit_usd",
            "new_limit_usd",
            "recorded_spend_usd",
            "unmeasured_calls",
            "actor",
            "reason",
            "timestamp",
        )
        return dict(zip(cols, row, strict=True))

    def unmeasured_calls(self, task_id: str | None = None) -> int:
        """Ledger rows whose cost is unknown — a call that happened and was
        never priced.

        Reported rather than summed. A NULL cost is not zero: a timed-out or
        session-limited reviewer was billed for as long as it ran, and quietly
        adding 0.0 would make an unpriced call indistinguishable from a free
        one in every total the tool prints. The count is what lets a reader
        (and, from #213, a budget guard) know a figure is a floor.
        """
        sql = "SELECT COUNT(*) FROM agent_calls WHERE cost_usd IS NULL"
        params: list[object] = []
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        try:
            assert self._conn is not None
            return int(self._conn.execute(sql, params).fetchone()[0] or 0)
        except Exception:
            return 0

    def record_tdd_phase(
        self, task_id: str, namespace: str, phase: str, detail: str | None = None
    ) -> None:
        """Append one lifecycle transition (#141 slice 4a).

        Best-effort: losing a transition costs legibility, and nothing gates on
        it — the gates read checkpoints and claims, which are written
        fail-closed. Logged loudly so a gap is visible rather than silent.
        """
        try:
            self._insert_phase_row(
                "INSERT INTO tdd_phases (task_id, namespace, phase, detail, timestamp) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, namespace, phase, detail, datetime.now().isoformat()),
            )
        except Exception as exc:
            from .logging import get_logger

            get_logger("state").warning(
                "Could not record TDD phase", task_id=task_id, phase=phase, error=str(exc)
            )

    def tdd_phase_history(self, task_id: str, namespace: str) -> list[dict]:
        """Every transition for this task in this workstream, oldest first."""
        assert self._conn is not None
        rows = self._conn.execute(
            "SELECT phase, detail, timestamp FROM tdd_phases "
            "WHERE task_id = ? AND namespace = ? ORDER BY id",
            (task_id, namespace),
        ).fetchall()
        return [{"phase": r[0], "detail": r[1], "timestamp": r[2]} for r in rows]

    def tdd_phase_histories(self, namespace: str, task_ids: list[str]) -> dict[str, list[dict]]:
        """Phase history for several tasks in one query.

        `tdd status` needs every task's history at once; asking per task turned
        one read into N (Copilot, PR #188).
        """
        assert self._conn is not None
        if not task_ids:
            return {}
        placeholders = ",".join("?" for _ in task_ids)
        rows = self._conn.execute(
            f"SELECT task_id, phase, detail, timestamp FROM tdd_phases "  # noqa: S608
            f"WHERE namespace = ? AND task_id IN ({placeholders}) ORDER BY id",
            [namespace, *task_ids],
        ).fetchall()
        grouped: dict[str, list[dict]] = {tid: [] for tid in task_ids}
        for task_id, phase, detail, timestamp in rows:
            grouped[task_id].append({"phase": phase, "detail": detail, "timestamp": timestamp})
        return grouped

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
        error_kind: str | None = None,
        error_stage: str | None = None,
        no_op: bool = False,
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
            error_kind=error_kind,
            error_stage=error_stage,
            no_op=no_op,
        )
        state.attempts.append(attempt)
        assert self._conn is not None

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
        try:
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
                    "review_status, review_findings, "
                    "error_kind, error_stage, no_op) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                        attempt.error_kind,
                        attempt.error_stage,
                        int(attempt.no_op),
                    ),
                )
                self._save_meta()
        except sqlite3.OperationalError as e:
            self._enter_degraded_mode("record_attempt", e, task_id=task_id)

        self._audit_attempt(task_id, attempt, state)

    def _audit_attempt(
        self,
        task_id: str,
        attempt: TaskAttempt,
        state: TaskState,
    ) -> None:
        """Record one attempt to the compliance audit log (never raises)."""
        from .audit_log import (
            EVENT_TASK_ATTEMPT,
            EVENT_TASK_COMPLETED,
            EVENT_TASK_FAILED,
        )

        details: dict = {
            "attempt_number": state.attempt_count,
            "success": attempt.success,
            "duration_seconds": attempt.duration_seconds,
            "input_tokens": attempt.input_tokens,
            "output_tokens": attempt.output_tokens,
            "cost_usd": attempt.cost_usd,
            "review_status": attempt.review_status,
            "error_code": (attempt.error_code.value if attempt.error_code else None),
            "error": attempt.error,
            "task_total_cost_usd": round(self.task_cost(task_id), 4),
            "run_total_cost_usd": round(self.total_cost(), 4),
        }
        self.audit_logger.record(EVENT_TASK_ATTEMPT, task_id=task_id, **details)

        # Emit terminal transitions separately so compliance readers can
        # filter on "this is where the task finally succeeded/failed".
        if state.status == "success":
            self.audit_logger.record(
                EVENT_TASK_COMPLETED,
                task_id=task_id,
                attempts=state.attempt_count,
                cost_usd=round(self.task_cost(task_id), 4),
            )
        elif state.status == "failed":
            self.audit_logger.record(
                EVENT_TASK_FAILED,
                task_id=task_id,
                attempts=state.attempt_count,
                cost_usd=round(self.task_cost(task_id), 4),
                last_error=attempt.error,
                error_code=attempt.error_code.value if attempt.error_code else None,
            )

    def mark_running(self, task_id: str) -> None:
        """Mark task as running with atomic SQLite persistence."""
        state = self.get_task_state(task_id)
        state.status = "running"
        state.started_at = datetime.now().isoformat()
        assert self._conn is not None

        try:
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
        except sqlite3.OperationalError as e:
            self._enter_degraded_mode("mark_running", e, task_id=task_id)

        from .audit_log import EVENT_TASK_STARTED

        self.audit_logger.record(
            EVENT_TASK_STARTED,
            task_id=task_id,
            started_at=state.started_at,
        )

    @property
    def degraded(self) -> bool:
        """True if SQLite persistence has failed; in-memory state is still live."""
        return self._degraded

    @property
    def degraded_reason(self) -> str | None:
        """Human-readable description of why we're degraded, or None."""
        return self._degraded_reason

    def _enter_degraded_mode(
        self,
        action: str,
        exc: sqlite3.OperationalError,
        *,
        task_id: str | None = None,
    ) -> None:
        """Record that SQLite persistence failed and notify the operator once.

        The in-memory state remains authoritative for the rest of the run so the
        executor can finish gracefully. Once the underlying issue is fixed,
        restarting the process will reload whatever was last persisted and
        replay from there.
        """
        from .logging import get_logger

        disk_full = _is_disk_full_error(exc)
        kind = "disk full" if disk_full else "DB write failed"
        reason = f"{kind} during {action}: {exc}"
        self._degraded = True
        self._degraded_reason = reason

        logger = get_logger("state")
        if not self._degraded_notified:
            logger.critical(
                "Executor state degraded — continuing in memory only",
                action=action,
                task_id=task_id,
                disk_full=disk_full,
                error=str(exc),
                hint=(
                    "Free disk space or repair DB at "
                    f"{self.config.state_file}; restart to resume persistence."
                ),
            )
            self._notify_degraded(reason)
            from .audit_log import EVENT_STATE_DEGRADED

            self.audit_logger.record(
                EVENT_STATE_DEGRADED,
                task_id=task_id,
                action=action,
                disk_full=disk_full,
                reason=reason,
            )
            self._degraded_notified = True
        else:
            logger.warning(
                "Persistence still failing (already degraded)",
                action=action,
                task_id=task_id,
                error=str(exc),
            )

    def _notify_degraded(self, reason: str) -> None:
        """Best-effort notification that the executor is running in degraded mode.

        Sent via whichever notifier the project has opted into (Telegram,
        webhook). Failures to send are logged but never raised — a broken
        notifier must not turn into a crash loop on top of an already-broken
        state file.
        """
        try:
            from .notifications import notify

            notify(self.config, "state_degraded", f"⚠️ spec-runner degraded: {reason}")
        except Exception as exc:  # pragma: no cover - defensive
            from .logging import get_logger

            get_logger("state").debug("Degraded-mode notification failed", error=str(exc))

    def stop_cause(self) -> tuple[str, str] | None:
        """Why execution should stop, or None if it may proceed.

        Returns a ``(reason, detail)`` pair — reason is one of
        ``max_consecutive_failures`` / ``budget_exceeded`` — so callers can
        report the ACTUAL cause instead of a generic message (#67: a budget
        stop used to be logged as "consecutive failures ... 0/2").
        """
        if self.consecutive_failures >= self.config.max_consecutive_failures:
            return (
                "max_consecutive_failures",
                f"{self.consecutive_failures}/{self.config.max_consecutive_failures}",
            )
        # Dormant unless a run ceiling is *configured*, exactly as before, and
        # exactly as the pre-call guard is (`budget.budget_is_active`). Reading
        # an authorization on a project that configured no budget would enforce
        # here while the guard stayed dormant during execution — the same
        # divergence this fix is about, entered from the other side (Copilot,
        # PR #257).
        #
        # Given a ceiling, the **effective** one decides (#256): an operator
        # raised it deliberately and under audit, and a preflight that reads
        # the YAML anyway makes the ceiling mean different things in different
        # places — `retry` honoured the authorization while `run` refused
        # against a number the operator had already superseded.
        if self.config.budget_usd is None:
            return None
        from .budget import effective_limits

        _task_limit, run_limit = effective_limits(self.config, self, None)
        if run_limit is not None:
            cost = self.total_cost()
            if cost > run_limit:
                return ("budget_exceeded", f"total cost ${cost:.2f} > budget ${run_limit:.2f}")
        return None

    def should_stop(self) -> bool:
        """Check if we should stop (consecutive failures or budget exceeded)."""
        return self.stop_cause() is not None

    def total_cost(self) -> float:
        """Every dollar in the **persisted state**, not just this run.

        Both sources are cumulative across runs: attempts are loaded from the
        state file at startup, and the ledger is queried whole. That matters
        because the budget check reads this — a `--budget` is a ceiling on the
        state file's lifetime spend, not on one invocation.

        Attempts carry the exec pass; `agent_calls` carries the ones that never
        had a home — the TDD RED authoring pass, whose cost used to be parsed
        and thrown away (#141 F-6). Summing both is safe precisely because the
        ledger holds only calls that are *not* on an attempt.
        """
        return (
            sum(
                a.cost_usd
                for ts in self.tasks.values()
                for a in ts.attempts
                if a.cost_usd is not None
            )
            + self._ledger_cost()
        )

    def task_cost(self, task_id: str) -> float:
        """A task's lifetime cost — attempts plus ledger, across runs."""
        ts = self.tasks.get(task_id)
        attempts = sum(a.cost_usd for a in ts.attempts if a.cost_usd is not None) if ts else 0.0
        return attempts + self._ledger_cost(task_id)

    def _ledger_tokens(self, task_id: str | None = None) -> tuple[int, int]:
        """Tokens from the agent-call ledger, mirroring `_ledger_cost`.

        Cost and tokens must come from the same places or a report shows
        $0.73 spent on 10,000 tokens when 15,600 were used — which is how the
        re-run of the battle matrix found this half-done.
        """
        sql = (
            "SELECT COALESCE(SUM(input_tokens), 0), COALESCE(SUM(output_tokens), 0) "
            "FROM agent_calls"
        )
        params: list[object] = []
        if task_id:
            sql += " WHERE task_id = ?"
            params.append(task_id)
        try:
            assert self._conn is not None
            row = self._conn.execute(sql, params).fetchone()
            return int(row[0] or 0), int(row[1] or 0)
        except Exception:
            return 0, 0

    def task_tokens(self, task_id: str) -> tuple[int, int]:
        """(input, output) for one task — attempts plus ledger."""
        ts = self.tasks.get(task_id)
        inp = sum(a.input_tokens for a in ts.attempts if a.input_tokens is not None) if ts else 0
        out = sum(a.output_tokens for a in ts.attempts if a.output_tokens is not None) if ts else 0
        ledger_in, ledger_out = self._ledger_tokens(task_id)
        return inp + ledger_in, out + ledger_out

    def total_tokens(self) -> tuple[int, int]:
        """(input, output) across the persisted state — attempts plus ledger."""
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
        ledger_in, ledger_out = self._ledger_tokens()
        return inp + ledger_in, out + ledger_out

    def set_meta(self, key: str, value: str) -> None:
        """Insert or replace a key in executor_meta."""
        assert self._conn is not None
        with self._conn:
            self._conn.execute(
                "INSERT INTO executor_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        """Read a key from executor_meta; return default if missing."""
        assert self._conn is not None
        row = self._conn.execute("SELECT value FROM executor_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    _SECOND_PASS_META_KEY = "second_pass_fail_tasks"

    def get_second_pass_fails(self) -> set[str]:
        """Return the set of task IDs that have failed across runs."""
        raw = self.get_meta(self._SECOND_PASS_META_KEY, "") or ""
        return {t for t in raw.split(",") if t}

    def add_second_pass_fail(self, task_id: str) -> None:
        """Record that this task_id failed a second time across runs."""
        ids = self.get_second_pass_fails()
        ids.add(task_id)
        self.set_meta(self._SECOND_PASS_META_KEY, ",".join(sorted(ids)))

    def clear_second_pass_fails(self) -> None:
        """Clear the second-pass record (called at start of run --all reset)."""
        self.set_meta(self._SECOND_PASS_META_KEY, "")

    def reset_failed_to_pending(self) -> set[str]:
        """Flip every task with status='failed' to 'pending'.

        Updates both the in-memory cache and SQLite atomically so the change
        survives connection close.  Returns the set of task IDs that were
        flipped (used by second-pass detection in cli.py).
        """
        assert self._conn is not None
        flipped = {task_id for task_id, ts in self.tasks.items() if ts.status == "failed"}
        if flipped:
            for task_id in flipped:
                self.tasks[task_id].status = "pending"
                self.tasks[task_id].attempts = []
            with self._conn:
                self._conn.execute("UPDATE tasks SET status = 'pending' WHERE status = 'failed'")
                placeholders = ",".join("?" for _ in flipped)
                self._conn.execute(
                    f"DELETE FROM attempts WHERE task_id IN ({placeholders})",
                    tuple(flipped),
                )
        return flipped

    def most_recent_failed_attempt(self) -> "TaskAttempt | None":
        """Return the most recently recorded failing attempt, or None."""
        assert self._conn is not None
        row = self._conn.execute(
            "SELECT timestamp, success, duration_seconds, error, error_code, "
            "claude_output, input_tokens, output_tokens, cost_usd, "
            "review_status, review_findings, error_kind, error_stage "
            "FROM attempts WHERE success = 0 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        error_code = ErrorCode(row[4]) if row[4] is not None else None
        return TaskAttempt(
            timestamp=row[0],
            success=bool(row[1]),
            duration_seconds=row[2],
            error=row[3],
            error_code=error_code,
            claude_output=row[5],
            input_tokens=row[6],
            output_tokens=row[7],
            cost_usd=row[8],
            review_status=row[9],
            review_findings=row[10],
            error_kind=row[11],
            error_stage=row[12],
        )

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
    """Check if graceful shutdown was requested via stop file or signal."""
    from .executor import _shutdown_requested

    return config.stop_file.exists() or _shutdown_requested


def clear_stop_file(config: ExecutorConfig) -> None:
    """Remove stop file if it exists."""
    with contextlib.suppress(FileNotFoundError):
        config.stop_file.unlink()


def recover_stale_tasks(
    state: ExecutorState,
    timeout_minutes: float,
    tasks_file: Path,
    *,
    recover_all: bool = False,
) -> list[str]:
    """Detect and recover tasks stuck in 'running' status.

    A task is considered stale if it has been 'running' for longer than
    timeout_minutes (typically 2x the task timeout). When ``recover_all`` is True
    (the caller holds the exclusive executor lock, so any 'running' task is
    orphaned from a dead run) every running task is recovered regardless of age.

    Returns list of recovered task IDs.
    """
    recovered: list[str] = []
    now = datetime.now()

    for task_id, ts in state.tasks.items():
        if ts.status != "running":
            continue
        if not ts.started_at:
            continue

        started = datetime.fromisoformat(ts.started_at)
        elapsed_minutes = (now - started).total_seconds() / 60

        if not recover_all and elapsed_minutes <= timeout_minutes:
            continue

        # Stale task — recover it
        ts.status = "failed"
        state.total_failed += 1
        ts.attempts.append(
            TaskAttempt(
                timestamp=now.isoformat(),
                success=False,
                duration_seconds=elapsed_minutes * 60,
                error="Recovered from stale running state",
                error_code=ErrorCode.INTERRUPTED,
            )
        )
        recovered.append(task_id)

    if recovered:
        state._save()
        from .task import update_task_status

        for task_id in recovered:
            update_task_status(tasks_file, task_id, "todo")

    return recovered
