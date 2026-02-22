# Phase 1: Reliability — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace fragile JSON state with SQLite, add structured error codes, improve retry context.

**Architecture:** SQLite + WAL mode for crash-safe state persistence (stdlib sqlite3, zero new dependencies). ErrorCode enum classifies failures. RetryContext dataclass replaces raw attempt lists in prompts.

**Tech Stack:** Python 3.10+, sqlite3 stdlib, pytest, ruff (line-length 100)

**Design doc:** `docs/plans/2026-02-22-phase1-reliability-design.md`

---

### Task 1: Add ErrorCode enum and RetryContext dataclass to state.py

**Files:**
- Modify: `src/spec_runner/state.py:1-25` (add imports and new types at top)
- Test: `tests/test_state.py`

**Step 1: Write failing tests for ErrorCode and RetryContext**

Add to `tests/test_state.py`:

```python
from spec_runner.state import ErrorCode, RetryContext

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
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py::TestErrorCode tests/test_state.py::TestRetryContext -v`
Expected: ImportError — `ErrorCode` and `RetryContext` don't exist yet

**Step 3: Implement ErrorCode and RetryContext in state.py**

Add to `src/spec_runner/state.py` after the existing imports (before `TaskAttempt`):

```python
from enum import Enum

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

@dataclass
class RetryContext:
    """Structured context for retry attempts."""
    attempt_number: int
    max_attempts: int
    previous_error_code: ErrorCode
    previous_error: str
    what_was_tried: str
    test_failures: str | None
```

Also add `error_code: ErrorCode | None = None` field to `TaskAttempt` dataclass.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_state.py -v`
Expected: All pass (new + existing)

**Step 5: Commit**

```bash
git add src/spec_runner/state.py tests/test_state.py
git commit -m "feat: add ErrorCode enum and RetryContext dataclass"
```

---

### Task 2: SQLite backend for ExecutorState

**Files:**
- Modify: `src/spec_runner/state.py:48-162` (replace JSON _load/_save with SQLite)
- Test: `tests/test_state.py`

**Step 1: Write failing tests for SQLite persistence**

Add to `tests/test_state.py`:

```python
import sqlite3

class TestExecutorStateSQLite:
    def test_creates_db_file(self, tmp_path):
        config = _make_config(tmp_path, state_file=tmp_path / "state.db")
        state = ExecutorState(config)
        assert config.state_file.exists()

    def test_db_has_wal_mode(self, tmp_path):
        config = _make_config(tmp_path, state_file=tmp_path / "state.db")
        ExecutorState(config)
        conn = sqlite3.connect(config.state_file)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_db_has_tables(self, tmp_path):
        config = _make_config(tmp_path, state_file=tmp_path / "state.db")
        ExecutorState(config)
        conn = sqlite3.connect(config.state_file)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "tasks" in tables
        assert "attempts" in tables
        assert "executor_meta" in tables

    def test_record_attempt_stores_error_code(self, tmp_path):
        config = _make_config(tmp_path, state_file=tmp_path / "state.db")
        state = ExecutorState(config)
        state.record_attempt(
            "TASK-001", success=False, duration=1.0,
            error="tests failed", error_code=ErrorCode.TEST_FAILURE,
        )
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].error_code == ErrorCode.TEST_FAILURE

    def test_save_and_load_roundtrip_sqlite(self, tmp_path):
        config = _make_config(tmp_path, state_file=tmp_path / "state.db")
        state = ExecutorState(config)
        state.record_attempt("TASK-001", success=True, duration=5.0)

        state2 = ExecutorState(config)
        assert "TASK-001" in state2.tasks
        assert state2.tasks["TASK-001"].status == "success"
        assert state2.total_completed == 1

    def test_consecutive_failures_persisted(self, tmp_path):
        config = _make_config(tmp_path, state_file=tmp_path / "state.db", max_retries=5)
        state = ExecutorState(config)
        state.record_attempt("T1", success=False, duration=1.0, error="e1")
        state.record_attempt("T2", success=False, duration=1.0, error="e2")

        state2 = ExecutorState(config)
        assert state2.consecutive_failures == 2
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py::TestExecutorStateSQLite -v`
Expected: FAIL (current code uses JSON, not SQLite)

**Step 3: Implement SQLite backend**

Rewrite `ExecutorState.__init__`, `_load`, `_save`, `record_attempt`, `mark_running`, and `get_task_state` to use sqlite3. Key design:

- `__init__` opens/creates DB, creates tables if not exist, sets WAL mode
- `_init_db()` — CREATE TABLE IF NOT EXISTS for tasks, attempts, executor_meta
- `_load()` — SELECT from all tables, populate self.tasks dict and counters
- `record_attempt()` — INSERT INTO attempts, UPDATE tasks, UPDATE executor_meta (all in one transaction)
- `mark_running()` — UPDATE tasks SET status='running', started_at=...
- `get_task_state()` — return from cache or create new
- `_save()` — removed (no longer needed, each mutation is atomic)

Schema exactly as in design doc:
```sql
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    timestamp TEXT NOT NULL,
    success INTEGER NOT NULL,
    duration_seconds REAL NOT NULL,
    error TEXT,
    error_code TEXT,
    claude_output TEXT
);
CREATE TABLE IF NOT EXISTS executor_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

Keep `_save()` as a no-op or remove references. The existing `_save()` calls in `record_attempt` and `mark_running` should be replaced with direct SQL.

**Step 4: Run all state tests to verify pass**

Run: `uv run pytest tests/test_state.py -v`
Expected: All pass (both new SQLite tests AND existing tests adapted)

**NOTE:** The existing `TestExecutorState` tests (like `test_save_and_load_roundtrip`) use `.json` state files via `_make_config`. Update `_make_config` to use `.db` extension by default. Existing tests should still pass with SQLite backend since the public interface is unchanged.

**Step 5: Commit**

```bash
git add src/spec_runner/state.py tests/test_state.py
git commit -m "feat: replace JSON state with SQLite + WAL mode"
```

---

### Task 3: JSON-to-SQLite migration

**Files:**
- Modify: `src/spec_runner/state.py` (add migration logic in `__init__`)
- Test: `tests/test_state.py`

**Step 1: Write failing tests for migration**

Add to `tests/test_state.py`:

```python
import json

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
        json_path.write_text('{"tasks":{}, "consecutive_failures":0, "total_completed":0, "total_failed":0}')

        # Create DB first
        config = _make_config(tmp_path, state_file=db_path)
        ExecutorState(config)

        # JSON should still exist (not renamed)
        assert json_path.exists()

    def test_fresh_db_if_nothing_exists(self, tmp_path):
        """If neither .json nor .db exists, create fresh DB."""
        db_path = tmp_path / "state.db"
        config = _make_config(tmp_path, state_file=db_path)
        state = ExecutorState(config)
        assert db_path.exists()
        assert state.tasks == {}
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py::TestJsonToSqliteMigration -v`
Expected: FAIL (no migration logic yet)

**Step 3: Implement migration**

In `ExecutorState.__init__`, before opening the DB:

```python
def __init__(self, config: ExecutorConfig):
    self.config = config
    self.tasks: dict[str, TaskState] = {}
    self.consecutive_failures = 0
    self.total_completed = 0
    self.total_failed = 0

    # Migration: JSON -> SQLite
    if not self.config.state_file.exists():
        json_path = self.config.state_file.with_suffix(".json")
        if json_path.exists():
            self._migrate_from_json(json_path)

    self._init_db()
    self._load()
```

Add `_migrate_from_json(json_path)` method:
1. Read JSON data
2. Open/create SQLite DB at `self.config.state_file`
3. Insert tasks, attempts, executor_meta
4. Rename `.json` → `.json.bak`

**Step 4: Run tests**

Run: `uv run pytest tests/test_state.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add src/spec_runner/state.py tests/test_state.py
git commit -m "feat: add JSON-to-SQLite migration on startup"
```

---

### Task 4: Update config.py state_file default to .db

**Files:**
- Modify: `src/spec_runner/config.py:113` (change `.json` to `.db`)
- Modify: `src/spec_runner/config.py:133-136` (`__post_init__` spec_prefix)
- Test: `tests/test_config.py`

**Step 1: Write failing tests**

Add/update in `tests/test_config.py`:

```python
class TestConfigStateFileDefault:
    def test_default_state_file_is_db(self):
        c = ExecutorConfig()
        assert str(c.state_file).endswith(".executor-state.db")

    def test_spec_prefix_state_file_is_db(self):
        c = ExecutorConfig(spec_prefix="phase2-")
        assert str(c.state_file).endswith(".executor-phase2-state.db")
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py::TestConfigStateFileDefault -v`
Expected: FAIL (still `.json`)

**Step 3: Implement**

In `src/spec_runner/config.py`:
- Line 113: `state_file: Path = Path("spec/.executor-state.db")`
- Line 133: `default_state = Path("spec/.executor-state.db")`
- Line 136: `self.state_file = Path(f"spec/.executor-{self.spec_prefix}state.db")`

**Step 4: Run tests**

Run: `uv run pytest tests/test_config.py -v`
Expected: All pass. Some existing tests may need updating if they assert `.json` extension.

Also run: `uv run pytest tests/test_state.py -v`
Expected: All pass. Update `_make_config` helper if needed to use `.db`.

**Step 5: Commit**

```bash
git add src/spec_runner/config.py tests/test_config.py tests/test_state.py
git commit -m "feat: change state_file default from .json to .db"
```

---

### Task 5: Error classification in execute_task

**Files:**
- Modify: `src/spec_runner/executor.py:62-207` (add error_code to each failure path)
- Modify: `src/spec_runner/state.py` (record_attempt signature already has error_code)
- Test: `tests/test_execution.py`

**Step 1: Write failing tests**

Add to `tests/test_execution.py`:

```python
from spec_runner.state import ErrorCode

class TestErrorClassification:
    """Tests for error_code classification in execute_task."""

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_timeout_gets_timeout_code(
        self, mock_run, mock_pre, mock_prompt, mock_cmd, mock_log, mock_status, tmp_path
    ):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="echo", timeout=1800)
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)
        execute_task(task, config, state)
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].error_code == ErrorCode.TIMEOUT

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook")
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_rate_limit_gets_rate_limit_code(
        self, mock_run, mock_pre, mock_post, mock_prompt, mock_cmd, mock_log, mock_status, tmp_path
    ):
        mock_run.return_value = MagicMock(
            stdout="you've hit your limit", stderr="", returncode=1,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)
        execute_task(task, config, state)
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].error_code == ErrorCode.RATE_LIMIT

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook")
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_task_failed_gets_task_failed_code(
        self, mock_run, mock_pre, mock_post, mock_prompt, mock_cmd, mock_log, mock_status, tmp_path
    ):
        mock_run.return_value = MagicMock(
            stdout="TASK_FAILED: could not compile", stderr="", returncode=1,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)
        execute_task(task, config, state)
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].error_code == ErrorCode.TASK_FAILED

    @patch("spec_runner.executor.mark_all_checklist_done")
    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook", return_value=(False, "Tests failed:\nFAILED test_x"))
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_test_failure_hook_gets_test_failure_code(
        self, mock_run, mock_pre, mock_post, mock_prompt, mock_cmd, mock_log, mock_status, mock_cl, tmp_path
    ):
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE", stderr="", returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)
        execute_task(task, config, state)
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].error_code == ErrorCode.TEST_FAILURE

    @patch("spec_runner.executor.mark_all_checklist_done")
    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook", return_value=(False, "Lint errors (not auto-fixable):\nerror"))
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_lint_failure_hook_gets_lint_failure_code(
        self, mock_run, mock_pre, mock_post, mock_prompt, mock_cmd, mock_log, mock_status, mock_cl, tmp_path
    ):
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE", stderr="", returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)
        execute_task(task, config, state)
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].error_code == ErrorCode.LINT_FAILURE
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_execution.py::TestErrorClassification -v`
Expected: FAIL (error_code not set in execute_task)

**Step 3: Implement error classification in execute_task**

In `src/spec_runner/executor.py`, modify `execute_task()` to classify and pass `error_code` to `state.record_attempt()`:

- Line 140 (API error): add `error_code=ErrorCode.RATE_LIMIT`
- Line 180 (hook failure): classify based on error text:
  ```python
  error_code = ErrorCode.UNKNOWN
  if hook_error:
      if "Tests failed" in hook_error:
          error_code = ErrorCode.TEST_FAILURE
      elif "Lint errors" in hook_error:
          error_code = ErrorCode.LINT_FAILURE
      else:
          error_code = ErrorCode.HOOK_FAILURE
  ```
- Line 188 (TASK_FAILED): add `error_code=ErrorCode.TASK_FAILED`
- Line 196 (timeout): add `error_code=ErrorCode.TIMEOUT`
- Line 204 (generic exception): add `error_code=ErrorCode.UNKNOWN`

Add import: `from .state import ErrorCode`

**Step 4: Run all execution tests**

Run: `uv run pytest tests/test_execution.py -v`
Expected: All pass (new + existing)

**Step 5: Commit**

```bash
git add src/spec_runner/executor.py tests/test_execution.py
git commit -m "feat: classify errors with ErrorCode in execute_task"
```

---

### Task 6: RetryContext in run_with_retries and prompt rendering

**Files:**
- Modify: `src/spec_runner/executor.py:210-279` (build RetryContext, pass to build_task_prompt)
- Modify: `src/spec_runner/prompt.py:173-323` (accept RetryContext, render structured section)
- Test: `tests/test_prompt.py`

**Step 1: Write failing tests for RetryContext rendering**

Add to `tests/test_prompt.py`:

```python
from spec_runner.state import ErrorCode, RetryContext

class TestRetryContextRendering:
    def test_retry_context_in_prompt(self, tmp_path):
        """build_task_prompt with RetryContext shows structured error info."""
        config = _make_config(tmp_path)
        task = _make_task()
        ctx = RetryContext(
            attempt_number=2,
            max_attempts=3,
            previous_error_code=ErrorCode.TEST_FAILURE,
            previous_error="Tests failed",
            what_was_tried="Implemented login page",
            test_failures="FAILED test_login - AssertionError",
        )
        prompt = build_task_prompt(task, config, retry_context=ctx)
        assert "Attempt 2 of 3" in prompt
        assert "TEST_FAILURE" in prompt
        assert "FAILED test_login" in prompt

    def test_no_retry_context_no_section(self, tmp_path):
        """Without RetryContext, no retry section in prompt."""
        config = _make_config(tmp_path)
        task = _make_task()
        prompt = build_task_prompt(task, config)
        assert "PREVIOUS ATTEMPTS" not in prompt
        assert "Attempt" not in prompt or "Attempt" in prompt  # no structured retry block

    def test_retry_context_timeout(self, tmp_path):
        """TIMEOUT error code renders correctly."""
        config = _make_config(tmp_path)
        task = _make_task()
        ctx = RetryContext(
            attempt_number=1,
            max_attempts=3,
            previous_error_code=ErrorCode.TIMEOUT,
            previous_error="Timeout after 30 minutes",
            what_was_tried="Implementing feature",
            test_failures=None,
        )
        prompt = build_task_prompt(task, config, retry_context=ctx)
        assert "TIMEOUT" in prompt
        assert "Timeout after 30 minutes" in prompt
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_prompt.py::TestRetryContextRendering -v`
Expected: FAIL (build_task_prompt doesn't accept retry_context)

**Step 3: Implement**

**3a. Update `build_task_prompt()` signature in `src/spec_runner/prompt.py`:**

Change:
```python
def build_task_prompt(
    task: Task,
    config: ExecutorConfig,
    previous_attempts: list[TaskAttempt] | None = None,
) -> str:
```
To:
```python
def build_task_prompt(
    task: Task,
    config: ExecutorConfig,
    previous_attempts: list[TaskAttempt] | None = None,
    retry_context: RetryContext | None = None,
) -> str:
```

Add import: `from .state import RetryContext, ErrorCode`

**3b. Replace the `attempts_section` building logic:**

If `retry_context` is provided, use it instead of raw `previous_attempts`:

```python
if retry_context:
    attempts_section = f"""
## ⚠️ RETRY — Attempt {retry_context.attempt_number} of {retry_context.max_attempts}

**Error type:** {retry_context.previous_error_code.value}
**What was tried:** {retry_context.what_was_tried}
**Error:** {retry_context.previous_error}
"""
    if retry_context.test_failures:
        attempts_section += f"""
**Test failures:**
```
{retry_context.test_failures}
```
"""
    attempts_section += """
**IMPORTANT:** Review the error above and fix the issue. Do not repeat the same mistake.
"""
elif previous_attempts:
    # ... keep existing logic as fallback ...
```

**3c. Update `execute_task()` in `src/spec_runner/executor.py`:**

In the section where `build_task_prompt` is called (line 90), build a `RetryContext` from previous attempts if available:

```python
retry_context = None
if previous_attempts:
    failed = [a for a in previous_attempts if not a.success]
    if failed:
        last = failed[-1]
        retry_context = RetryContext(
            attempt_number=task_state.attempt_count + 1,
            max_attempts=config.max_retries,
            previous_error_code=last.error_code or ErrorCode.UNKNOWN,
            previous_error=last.error or "Unknown error",
            what_was_tried=f"Previous attempt for {task.name}",
            test_failures=extract_test_failures(last.claude_output) if last.claude_output else None,
        )

prompt = build_task_prompt(task, config, previous_attempts, retry_context=retry_context)
```

Add import: `from .state import ErrorCode, RetryContext`
Add import: `from .prompt import extract_test_failures` (already imported via prompt module)

**Step 4: Run tests**

Run: `uv run pytest tests/test_prompt.py tests/test_execution.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add src/spec_runner/executor.py src/spec_runner/prompt.py tests/test_prompt.py
git commit -m "feat: structured RetryContext in prompts instead of raw attempts"
```

---

### Task 7: Update __init__.py exports and run full test suite

**Files:**
- Modify: `src/spec_runner/__init__.py` (export ErrorCode, RetryContext)
- Test: full suite

**Step 1: Update exports**

In `src/spec_runner/__init__.py`, add to the state imports:

```python
from .state import (
    ErrorCode,
    ExecutorState,
    RetryContext,
    TaskAttempt,
    TaskState,
)
```

And add `"ErrorCode"` and `"RetryContext"` to `__all__`.

**Step 2: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 3: Run lint and format**

Run: `uv run ruff check . && uv run ruff format .`
Expected: No errors

**Step 4: Commit**

```bash
git add src/spec_runner/__init__.py
git commit -m "feat: export ErrorCode and RetryContext from package"
```

---

### Task 8: Update CLAUDE.md and cleanup

**Files:**
- Modify: `CLAUDE.md` (document new SQLite state, ErrorCode)

**Step 1: Update CLAUDE.md**

Update the "Key Classes" section to mention:
- `ErrorCode` enum with 7 error types
- `RetryContext` dataclass for structured retry info
- SQLite backend with WAL mode for state persistence
- JSON-to-SQLite auto-migration

Update the `state_file` reference from `.json` to `.db`.

**Step 2: Run full test suite one final time**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: update CLAUDE.md for Phase 1 changes"
```
