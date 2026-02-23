# Design: Phase 5 — Core Safety

**Date:** 2026-02-23
**Status:** Approved
**Goal:** Signal handling, crash recovery, stale lock diagnostics, guaranteed SQLite cleanup.
**Constraint:** Same CLI interface (only new flags added). Execution logic untouched.

## 1. Signal Handling (SIGINT/SIGTERM)

### Problem

Ctrl+C during task execution leaves tasks stuck in "running" status, lock files orphaned, and Claude CLI subprocesses potentially still running. SIGTERM (kill -15) has the same effect.

### Design

Module-level shutdown flag in `executor.py`:

```python
_shutdown_requested = False

def _signal_handler(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
```

Registered in `main()`:

```python
signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)
```

### Integration points

**`check_stop_requested()`** in `state.py` — already called between tasks. Extend to also check the shutdown flag:

```python
def check_stop_requested(config: ExecutorConfig) -> bool:
    from .executor import _shutdown_requested
    return config.stop_file.exists() or _shutdown_requested
```

This avoids circular import by using a lazy import. Alternative: move the flag to state.py or a shared module.

**`execute_task()`** — `subprocess.run()` raises `KeyboardInterrupt` on SIGINT. Catch it:

```python
except KeyboardInterrupt:
    duration = (datetime.now() - start_time).total_seconds()
    state.record_attempt(task_id, False, duration, error="Interrupted by signal",
                         error_code=ErrorCode.INTERRUPTED)
    return False
```

**`_run_tasks()` / `_run_tasks_parallel()`** — Wrap in try/finally:

```python
try:
    # ... main loop ...
except KeyboardInterrupt:
    logger.info("Interrupted by signal")
finally:
    state.close()
    # lock released by cmd_run's own finally block
```

### New error code

```python
class ErrorCode(str, Enum):
    # ... existing ...
    INTERRUPTED = "INTERRUPTED"
```

`INTERRUPTED` is permanent — no automatic retries.

## 2. Crash Recovery (Stale "running" Tasks)

### Problem

If the executor crashes (SIGKILL, power loss, OOM) mid-task, the task remains in "running" status in SQLite forever. The next run skips it because it's already "running."

### Design

New function in `state.py`:

```python
def recover_stale_tasks(
    state: ExecutorState,
    timeout_minutes: float,
    tasks_file: Path,
) -> list[str]:
    """Detect and recover tasks stuck in 'running' status.

    A task is considered stale if it has been 'running' for longer
    than timeout_minutes (typically 2x the task timeout).

    Returns list of recovered task IDs.
    """
```

Logic:
1. Query all tasks with `status="running"`
2. For each, check the last attempt's timestamp. If `now - started_at > timeout_minutes`: stale
3. Reset status to `"failed"` in state DB
4. Record a synthetic attempt: `success=False, error="Recovered from stale running state", error_code=ErrorCode.INTERRUPTED`
5. Update `tasks.md`: change status from `in_progress` back to `todo`
6. Log a warning for each recovered task

### Timestamp tracking

`TaskAttempt` already has a `started_at` field? Let me check... No, it has `timestamp` (ISO string, set at record time). We need to also track when the task started running.

Add `started_at: str | None = None` to `TaskState`. Set it in `mark_running()`:

```python
def mark_running(self, task_id: str) -> None:
    state = self.get_task_state(task_id)
    state.status = "running"
    state.started_at = datetime.now().isoformat()
    # ... persist ...
```

Schema migration: `ALTER TABLE tasks ADD COLUMN started_at TEXT;`

### Call site

At the start of `_run_tasks()` and `_run_tasks_parallel()`, before the main loop:

```python
recovered = recover_stale_tasks(state, config.task_timeout_minutes * 2, config.tasks_file)
if recovered:
    logger.warning("Recovered stale tasks", task_ids=recovered)
    tasks = parse_tasks(config.tasks_file)  # Re-parse after status updates
```

### Config

```yaml
executor:
  stale_task_timeout_minutes: 0    # 0 = auto (2x task_timeout_minutes)
```

## 3. Stale Lock Diagnostics

### Current state

`ExecutorLock` uses `fcntl.flock(LOCK_EX | LOCK_NB)`. This is fd-based — the kernel releases the lock when the process dies. So stale locks don't actually block. The lock file stays on disk but is harmless.

### Improvements

**Better error message on lock failure** — Read the existing lock file to show which PID holds it:

```python
def acquire(self) -> bool:
    # ... existing flock logic ...
    except BlockingIOError:
        # Read existing lock info for diagnostic message
        held_by = self._read_lock_info()
        self.lock_file.close()
        self.lock_file = None
        if held_by:
            logger.error("Lock held by another process", **held_by)
        return False

def _read_lock_info(self) -> dict[str, str]:
    """Read PID and start time from existing lock file."""
    try:
        content = self.lock_path.read_text()
        info = {}
        for line in content.splitlines():
            if line.startswith("PID:"):
                info["pid"] = line.split(":", 1)[1].strip()
            elif line.startswith("Started:"):
                info["started"] = line.split(":", 1)[1].strip()
        return info
    except Exception:
        return {}
```

**PID liveness check** — When lock fails, check if the holding PID is alive:

```python
import os

def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)  # Signal 0 = check existence
        return True
    except (ProcessLookupError, PermissionError):
        return False
```

If PID is dead, warn the user:

```
ERROR: Lock held by PID 12345 (started 2026-02-23T10:00:00) — process is dead.
Use `spec-runner reset` to clean up, or run with `--force` to override.
```

**`--force` flag** — Skip lock check:

```python
if getattr(args, "force", False):
    logger.warning("Skipping lock check (--force)")
else:
    lock = ExecutorLock(...)
    if not lock.acquire():
        ...
```

### Files changed

`config.py` (ExecutorLock), `executor.py` (--force flag, error messages)

## 4. SQLite Connection Cleanup (Context Manager)

### Problem

Several code paths create `ExecutorState()` without calling `.close()`:
- `cmd_status()` (executor.py)
- `cmd_retry()` (executor.py)
- `_run_tasks()` exception paths (executor.py)
- `post_done_hook()` creates and closes inline (hooks.py) — OK

### Design

Make `ExecutorState` a context manager:

```python
class ExecutorState:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False  # Don't suppress exceptions
```

Update all usage sites:

```python
# Before
state = ExecutorState(config)
# ... use state ...

# After
with ExecutorState(config) as state:
    # ... use state ...
```

This guarantees `close()` on normal exit, exceptions, and KeyboardInterrupt.

### Call sites to update

| Location | Current | Change |
|----------|---------|--------|
| `cmd_status()` | `state = ExecutorState(config)` | `with ExecutorState(config) as state:` |
| `cmd_retry()` | `state = ExecutorState(config)` | `with ExecutorState(config) as state:` |
| `_run_tasks()` | `state = ExecutorState(config)` | `with ExecutorState(config) as state:` |
| `_run_tasks_parallel()` | `state = ExecutorState(config)` + `finally: state.close()` | `with ExecutorState(config) as state:` (remove explicit finally) |
| `run_with_retries()` | `state = ExecutorState(config)` | `with ExecutorState(config) as state:` |
| `execute_task()` | `state = ExecutorState(config)` | `with ExecutorState(config) as state:` |
| `post_done_hook()` | Local state + close() | `with ExecutorState(config) as state:` |

## 5. New Error Code

```python
class ErrorCode(str, Enum):
    # ... existing codes ...
    INTERRUPTED = "INTERRUPTED"
```

`INTERRUPTED` is a **permanent** error — no automatic retries. Used for:
- Signal interruption (SIGINT/SIGTERM)
- Crash recovery (stale running tasks)

## 6. Files Changed

| File | Change |
|------|--------|
| `executor.py` | Signal handler, KeyboardInterrupt catches, `--force` flag, context manager usage |
| `state.py` | `recover_stale_tasks()`, `started_at` field, `INTERRUPTED` error code, `__enter__`/`__exit__`, schema migration |
| `config.py` | `_read_lock_info()`, `_is_pid_alive()`, improved error messages |

## 7. What Does NOT Change

- Sequential/parallel execution path logic
- Task parsing, dependency resolution
- Prompt building, review system, HITL gate
- Structured logging, TUI dashboard
- CLI interface (only `--force` added)
- tasks.md format
