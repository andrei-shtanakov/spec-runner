# Design: Phase 2 — Performance

**Date:** 2026-02-22
**Status:** Approved
**Goal:** Parallel task execution, token/cost tracking, budget enforcement.
**Constraint:** Same CLI interface (only new flags added). Sequential path untouched.

## 1. Parallel Execution

### New CLI flags and config

```yaml
# executor.config.yaml
max_concurrent: 3          # Max parallel tasks (default 3)
```

```bash
spec-runner run --all --parallel        # Parallel dispatch
spec-runner run --all                   # Sequential (unchanged)
```

`--parallel` implies `--no-branch` — disables git branch-per-task in hooks.

### Async dispatch (executor.py)

New function `_run_tasks_parallel()` alongside existing `_run_tasks()`:

```python
async def _run_tasks_parallel(config, state, tasks_file):
    sem = asyncio.Semaphore(config.max_concurrent)

    async def run_one(task):
        async with sem:
            return await _execute_task_async(config, state, task)

    while True:
        ready = get_next_tasks(tasks_file)
        if not ready or state.should_stop():
            break
        # Launch all ready tasks concurrently
        coros = [run_one(t) for t in ready]
        results = await asyncio.gather(*coros, return_exceptions=True)
        # Re-check for newly unblocked tasks
```

Entry point wraps with `asyncio.run()` only when `--parallel` flag is set.

### Async subprocess (runner.py)

```python
async def run_claude_async(cmd, timeout, cwd):
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=timeout
    )
    return stdout.decode(), stderr.decode(), proc.returncode
```

### State thread-safety

`asyncio.Lock` protects all `ExecutorState` writes:

```python
state_lock = asyncio.Lock()

async with state_lock:
    state.record_attempt(task_id, ...)
    state.mark_running(task_id)
```

Single event loop = no threads, but Lock prevents interleaved coroutine writes to SQLite.

### Hooks in parallel mode

- `pre_start_hook()`: Skip git branch creation when `no_branch=True`. Still run `uv sync`.
- `post_done_hook()`: Skip merge. Still run tests/lint/review/commit on working tree.
- Race condition mitigation: each task runs tests/lint, but commits are serialized via state_lock.

## 2. Token/Cost Tracking

### Parsing stderr

Claude CLI prints usage summary to stderr. Parse with regex:

```python
def parse_token_usage(stderr: str) -> tuple[int | None, int | None, float | None]:
    """Extract (input_tokens, output_tokens, cost_usd) from Claude CLI stderr."""
    input_tokens = _parse_int(r"input[_ ]tokens?[:\s]+(\d[\d,]*)", stderr)
    output_tokens = _parse_int(r"output[_ ]tokens?[:\s]+(\d[\d,]*)", stderr)
    cost = _parse_float(r"(?:total[_ ])?cost[:\s]+\$?([\d.]+)", stderr)
    return input_tokens, output_tokens, cost
```

Graceful fallback: if parsing fails, fields are `None`. Never blocks execution.

### Storage

New fields in `TaskAttempt`:

```python
@dataclass
class TaskAttempt:
    # ... existing fields ...
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
```

New columns in SQLite `attempts` table:

```sql
ALTER TABLE attempts ADD COLUMN input_tokens INTEGER;
ALTER TABLE attempts ADD COLUMN output_tokens INTEGER;
ALTER TABLE attempts ADD COLUMN cost_usd REAL;
```

Schema migration: check if columns exist, add if missing.

### Aggregation

New methods on `ExecutorState`:

```python
def total_cost(self) -> float:
    """Sum of cost_usd across all attempts."""

def task_cost(self, task_id: str) -> float:
    """Sum of cost_usd for a specific task."""

def total_tokens(self) -> tuple[int, int]:
    """(total_input_tokens, total_output_tokens) across all attempts."""
```

### Status output

`spec-runner status` shows token/cost summary:

```
Total: 45.2K input / 12.8K output tokens | Cost: $0.84
```

## 3. Budget Enforcement

### Config

```yaml
budget_usd: 10.0              # Global budget limit (null = unlimited)
task_budget_usd: 2.0           # Per-task budget limit (null = unlimited)
```

### New error code

```python
class ErrorCode(str, Enum):
    # ... existing codes ...
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
```

`BUDGET_EXCEEDED` — no retry (fail fast, like RATE_LIMIT).

### Enforcement points

After each attempt completes:

1. **Per-task check**: `state.task_cost(task_id) > config.task_budget_usd`
   — Mark task failed with BUDGET_EXCEEDED, skip retries
2. **Global check**: `state.total_cost() > config.budget_usd`
   — `should_stop()` returns True, halt all execution

### Callback payload

`send_callback()` updated to include cost info:

```python
{
    "task_id": "TASK-001",
    "status": "done",
    "duration_seconds": 45.2,
    "input_tokens": 12500,
    "output_tokens": 3200,
    "cost_usd": 0.12
}
```

## 4. Files Changed

| File | Change |
|------|--------|
| `executor.py` | `_run_tasks_parallel()`, async entry point, budget checks after attempts |
| `config.py` | `max_concurrent`, `budget_usd`, `task_budget_usd`, `parallel` CLI flag |
| `state.py` | Token fields in TaskAttempt, new columns + migration, aggregation methods, BUDGET_EXCEEDED |
| `runner.py` | `run_claude_async()`, `parse_token_usage()` |
| `hooks.py` | `no_branch` parameter to skip git branch creation/merge |
| `prompt.py` | Unchanged |
| `task.py` | Unchanged |

## 5. What Does NOT Change

- Sequential execution path (`--all` without `--parallel`)
- CLI interface (only new optional flags)
- spec/tasks.md format
- Prompt building logic
- Entry points in pyproject.toml
- Test execution within hooks (sync subprocess)
