# Executor state schema

This document describes the on-disk and CLI state surfaces that spec-runner exposes to external consumers (primarily Maestro, but also operator tooling and dashboards). Any breaking change to these surfaces requires a **major version bump** and a `BREAKING` note in `CHANGELOG.md`.

**Source of truth:** `src/spec_runner/state.py` — dataclasses `ExecutorState`, `TaskState`, `TaskAttempt`, and enums `ErrorCode`, `ReviewVerdict`.

**Pinned version (Maestro side):** `maestro.spec_runner.SPEC_RUNNER_REQUIRED_VERSION = "2.0.0"`.

## Contract surfaces

spec-runner exposes three distinct surfaces. Each has a separate stability guarantee:

| Surface | Path / form | Consumer | Stability |
|---|---|---|---|
| SQLite state (canonical) | `spec/.executor-state.db` | Maestro (read-only), TUI | **stable** |
| Legacy JSON state | `spec/.executor-state.json` (pre-2.0, renamed to `.bak` after migration) | Old Maestro builds | **deprecated**, read-only fallback |
| `spec-runner run --json-result` stdout | CLI output | Maestro invocation result | **stable** |
| `spec-runner status --json` stdout | CLI output | Dashboards, monitoring | **stable** |

Consumers **must not** rely on any other path, column, log field, or stdout line not listed below.

---

## 1. SQLite state (`.executor-state.db`)

Canonical format since spec-runner 2.0. Uses SQLite with WAL journaling and `busy_timeout=30000`. Read-only consumers should open via URI mode (`file:path?mode=ro`) to avoid write-lock contention with the executor.

### Schema

```sql
CREATE TABLE tasks (
    task_id      TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'pending',
    started_at   TEXT,
    completed_at TEXT
);

CREATE TABLE attempts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          TEXT NOT NULL REFERENCES tasks(task_id),
    timestamp        TEXT NOT NULL,
    success          INTEGER NOT NULL,   -- 0 or 1
    duration_seconds REAL NOT NULL,
    error            TEXT,
    error_code       TEXT,               -- ErrorCode enum string
    claude_output    TEXT,
    -- Added in later migrations (detect via PRAGMA table_info):
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    cost_usd         REAL,
    review_status    TEXT,               -- ReviewVerdict enum string
    review_findings  TEXT
);

CREATE TABLE executor_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

### `tasks` columns

| Column | Type | Stability | Notes |
|---|---|---|---|
| `task_id` | TEXT | stable | `TASK-###` identifier from `tasks.md` |
| `status` | TEXT | stable | One of: `pending`, `running`, `success`, `failed`, `skipped` |
| `started_at` | TEXT | stable | ISO 8601 timestamp; nullable (task never ran) |
| `completed_at` | TEXT | stable | ISO 8601 timestamp; nullable (not finished) |

### `attempts` columns

| Column | Type | Stability | Notes |
|---|---|---|---|
| `id` | INTEGER | stable | Autoincrement; use `ORDER BY id` for chronological order |
| `task_id` | TEXT | stable | Foreign key to `tasks.task_id` |
| `timestamp` | TEXT | stable | ISO 8601 |
| `success` | INTEGER | stable | 0 = failure, 1 = success |
| `duration_seconds` | REAL | stable | Wall-clock; `>= 0` |
| `error` | TEXT | stable | Human-readable error; nullable on success |
| `error_code` | TEXT | stable | See `ErrorCode` values below; nullable |
| `claude_output` | TEXT | experimental | Captured Claude CLI stdout; may be truncated, format changes allowed |
| `input_tokens` | INTEGER | stable | Prompt tokens; null if unavailable |
| `output_tokens` | INTEGER | stable | Completion tokens; null if unavailable |
| `cost_usd` | REAL | stable | Attempt cost in USD |
| `review_status` | TEXT | stable | See `ReviewVerdict` values below |
| `review_findings` | TEXT | experimental | Free-text review notes |

**Column detection:** older databases may lack `input_tokens`, `output_tokens`, `cost_usd`, `review_status`, `review_findings`. Consumers should probe with `PRAGMA table_info(attempts)` and treat missing columns as `None`.

### `executor_meta` key-value pairs

| Key | Value type | Stability | Notes |
|---|---|---|---|
| `consecutive_failures` | int (stored as TEXT) | stable | Resets to 0 on any task success |
| `total_completed` | int (stored as TEXT) | stable | Monotonic counter |
| `total_failed` | int (stored as TEXT) | stable | Monotonic counter |

### `ErrorCode` enum values

Source: `src/spec_runner/state.py:ErrorCode`.

Stable: `TIMEOUT`, `RATE_LIMIT`, `TEST_FAILURE`, `LINT_FAILURE`, `TASK_FAILED`, `HOOK_FAILURE`, `BUDGET_EXCEEDED`, `REVIEW_REJECTED`, `INTERRUPTED`, `UNKNOWN`.

Consumers should treat unknown values as `UNKNOWN` rather than raising — new codes may be added in minor releases.

### `ReviewVerdict` enum values

Source: `src/spec_runner/state.py:ReviewVerdict`.

Stable: `passed`, `fixed`, `failed`, `skipped`, `rejected`. (Lowercase — stored as-is.)

### Read-only access pattern

```python
import sqlite3

uri = f"file:{spec_dir / '.executor-state.db'}?mode=ro"
with sqlite3.connect(uri, uri=True) as conn:
    conn.row_factory = sqlite3.Row
    for row in conn.execute("SELECT task_id, status FROM tasks"):
        ...
```

### Concurrent-write safety

spec-runner acquires its own connection for writes. WAL journaling means readers never block writers and vice versa. Readers using `mode=ro` **must not** call `PRAGMA journal_mode` or any write operation.

---

## 2. Legacy JSON state (`.executor-state.json`)

**Status: deprecated. Read-only fallback only.**

Pre-2.0 spec-runner wrote state as JSON. On first run of 2.0+, the JSON file is migrated to SQLite and renamed to `.executor-state.json.bak`. New executor runs never write JSON.

Consumers should only read this file when the SQLite file does not exist (pre-migration snapshots, archived workspaces).

### Format

```json
{
  "tasks": {
    "TASK-001": {
      "status": "success",
      "started_at": "2026-04-16T10:00:00",
      "completed_at": "2026-04-16T10:05:00",
      "attempts": [
        {
          "timestamp": "2026-04-16T10:00:00",
          "success": true,
          "duration_seconds": 300.0,
          "error": null,
          "error_code": null,
          "claude_output": "..."
        }
      ]
    }
  },
  "consecutive_failures": 0,
  "total_completed": 1,
  "total_failed": 0
}
```

Field types and semantics match the SQLite columns above. Token/cost/review fields were added after the JSON-era; legacy JSON files will not contain them.

---

## 3. `spec-runner run --json-result` stdout

Emitted after a run completes. Used by Maestro to capture per-task outcomes without reopening the state file.

### Shape

Single task (one element list) → JSON object. Multiple tasks → JSON array.

```json
{
  "task_id": "TASK-001",
  "status": "done",
  "attempts": 1,
  "cost_usd": 0.42,
  "tokens": {"input": 1500, "output": 800},
  "duration_seconds": 120.3,
  "review": "passed",
  "exit_code": 0
}
```

### Fields

| Field | Type | Stability | Notes |
|---|---|---|---|
| `task_id` | string | stable | `TASK-###` |
| `status` | string | stable | `done` (success), `failed`, or `unknown` (task never reached state) |
| `attempts` | int | stable | Total attempt count |
| `cost_usd` | float | stable | Rounded to 2 decimals; sum across attempts |
| `tokens.input` | int | stable | Sum across attempts |
| `tokens.output` | int | stable | Sum across attempts |
| `duration_seconds` | float | stable | Rounded to 1 decimal; sum across attempts |
| `review` | string | stable | Last attempt's review verdict, or `skipped` |
| `error` | string | stable | Present only on failure; truncated to 200 chars |
| `exit_code` | int | stable | 0 on success, 1 on failure |

### Empty-tasks edge case

If no tasks were ready to run:

```json
{"tasks": [], "message": "No tasks ready to execute"}
```

---

## 4. `spec-runner status --json` stdout

Aggregate snapshot for dashboards. Does not include per-task details.

### Shape

```json
{
  "total_tasks": 12,
  "completed": 8,
  "failed": 1,
  "running": 0,
  "not_started": 3,
  "total_cost": 12.34,
  "input_tokens": 45000,
  "output_tokens": 22000,
  "budget_usd": 50.0
}
```

All fields are **stable**. `budget_usd` is `null` when no budget is configured.

---

## Breaking change policy

A change is **breaking** if it:

- removes or renames a column, table, JSON key, or CLI flag listed above
- changes the semantic meaning of a field (e.g. redefining `status` values)
- changes a type (e.g. int → string)
- changes a stored value format (e.g. ISO 8601 → Unix epoch)
- drops a previously-documented `ErrorCode` or `ReviewVerdict` value

A change is **non-breaking** if it:

- adds a new column, table, JSON key, or CLI flag
- adds a new `ErrorCode` or `ReviewVerdict` value (consumers must tolerate unknowns)
- improves internal storage (indexes, triggers) without touching the surface above

Breaking changes require:

1. Major version bump (`2.x.y → 3.0.0`)
2. `CHANGELOG.md` entry prefixed with `BREAKING:`
3. Golden-test update (`tests/test_json_result_contract.py`)
4. Notification to Maestro (bump `SPEC_RUNNER_REQUIRED_VERSION`)

---

## Related files

- `src/spec_runner/state.py` — dataclasses and SQLite schema
- `src/spec_runner/cli.py` — `--json-result` emitter
- `src/spec_runner/cli_info.py` — `status --json` emitter
- `schemas/executor-state.schema.json` — generated JSON Schema
- `tests/fixtures/maestro-interop/` — golden fixtures for contract tests
- Maestro side: `Maestro/maestro/spec_runner.py`, `Maestro/maestro/models.py` (ExecutorState, ExecutorTaskEntry, ExecutorTaskAttempt, ExecutorTaskStatus)
