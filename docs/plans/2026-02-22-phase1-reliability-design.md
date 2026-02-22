# Design: Phase 1 — Reliability

**Date:** 2026-02-22
**Status:** Approved
**Goal:** Replace fragile JSON state with SQLite, add structured error codes, improve retry context.
**Constraint:** Same CLI interface, backward-compatible migration from JSON state.

## 1. SQLite State (state.py)

Replace JSON file persistence with SQLite + WAL mode. Zero new dependencies (stdlib sqlite3).

### Schema

```sql
CREATE TABLE tasks (
    task_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT,
    completed_at TEXT
);

CREATE TABLE attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    timestamp TEXT NOT NULL,
    success INTEGER NOT NULL,
    duration_seconds REAL NOT NULL,
    error TEXT,
    error_code TEXT,
    claude_output TEXT
);

CREATE TABLE executor_meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

### Interface

`ExecutorState` public interface unchanged:
- `__init__(config)` — opens/creates DB, runs migration if needed
- `get_task_state(task_id)` → `TaskState`
- `record_attempt(task_id, success, duration, error, output, error_code)` — atomic INSERT
- `mark_running(task_id)` — atomic UPDATE
- `should_stop()` → bool
- Properties: `consecutive_failures`, `total_completed`, `total_failed`

### Migration

On `__init__`:
1. If `.executor-state.db` exists → open it
2. If `.executor-state.json` exists but no `.db` → convert JSON→SQLite, rename JSON to `.json.bak`
3. If neither exists → create fresh DB

### Config change

`state_file` default: `spec/.executor-state.json` → `spec/.executor-state.db`
`spec_prefix` namespacing: `spec/.executor-{prefix}state.db`

## 2. Structured Error Codes

```python
class ErrorCode(str, Enum):
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    SYNTAX = "SYNTAX"
    TEST_FAILURE = "TEST_FAILURE"
    LINT_FAILURE = "LINT_FAILURE"
    TASK_FAILED = "TASK_FAILED"
    HOOK_FAILURE = "HOOK_FAILURE"
    UNKNOWN = "UNKNOWN"
```

### Classification (in execute_task)

| Condition | ErrorCode |
|-----------|-----------|
| `subprocess.TimeoutExpired` | TIMEOUT |
| `check_error_patterns()` matches | RATE_LIMIT |
| `TASK_FAILED` in output | TASK_FAILED |
| `post_done_hook` fail + test errors | TEST_FAILURE |
| `post_done_hook` fail + lint errors | LINT_FAILURE |
| `pre_start_hook` fail | HOOK_FAILURE |
| Everything else | UNKNOWN |

### Storage

`TaskAttempt.error_code: ErrorCode | None` — stored in `attempts.error_code` column.

## 3. Improved Retry Context

### Retry behavior by error code

| ErrorCode | Retry? | Behavior |
|-----------|--------|----------|
| RATE_LIMIT | No | Return "API_ERROR" immediately |
| HOOK_FAILURE | No | Infrastructure problem, fail fast |
| TIMEOUT | Yes | Same timeout (Claude may need full time) |
| TEST_FAILURE | Yes | Include test failure details in prompt |
| LINT_FAILURE | Yes | Include lint errors in prompt |
| TASK_FAILED | Yes | Include error message in prompt |
| UNKNOWN | Yes | Include error in prompt |

### Structured retry context

```python
@dataclass
class RetryContext:
    attempt_number: int
    max_attempts: int
    previous_error_code: ErrorCode
    previous_error: str
    what_was_tried: str
    test_failures: str | None
```

Passed to `build_task_prompt()` instead of raw `previous_attempts` list.
Prompt shows structured analysis: error code → what happened → what to fix.

## 4. Files Changed

| File | Change |
|------|--------|
| `state.py` | SQLite backend, ErrorCode enum, RetryContext dataclass, migration |
| `config.py` | state_file default `.json` → `.db` |
| `executor.py` | Error classification in execute_task, RetryContext in run_with_retries |
| `prompt.py` | Accept RetryContext, format structured retry section |
| `hooks.py` | Return error_code from post_done_hook |
| `tests/test_state.py` | Rewrite for SQLite, add migration tests |
| `tests/test_config.py` | Update state_file extension |
| `tests/test_execution.py` | Add error_code assertions |
| `tests/test_prompt.py` | Add RetryContext rendering tests |

## 5. What Does NOT Change

- CLI interface and arguments
- task.py — untouched
- runner.py — untouched
- spec/tasks.md format
- Entry points in pyproject.toml
