# Design: Phase 3 — Visibility

**Date:** 2026-02-23
**Status:** Approved
**Goal:** Structured logging with correlation IDs, Textual-based TUI Kanban dashboard.
**Constraint:** Same CLI interface (only new flags added). Execution logic untouched.

## 1. Structured Logging

### Library

`structlog` — stdlib-compatible structured logging with context binding.

### New module: `src/spec_runner/logging.py` (~80 lines)

```python
def setup_logging(
    level: str = "info",
    json_output: bool = False,
    log_file: Path | None = None,
    tui_mode: bool = False,
) -> None:
    """Configure structlog for the entire application."""
```

### Output modes

| Mode | Trigger | Output |
|------|---------|--------|
| **CLI (default)** | Normal run | Pretty-printed colored logs to stderr |
| **JSON** | `--log-json` | JSON lines to stderr |
| **TUI** | `--tui` | Logs to file only (`spec/.executor-logs/run-YYYYMMDD-HHMMSS.log`), TUI owns the screen |

### Context processors

Every log event automatically includes:
- `timestamp` (ISO 8601)
- `level` (info/warning/error/debug)
- `module` (which source module)
- `task_id` (when in task context, via bound logger)
- `attempt` (retry attempt number, when applicable)
- `run_id` (UUID4 prefix, set once at executor start)

### Correlation IDs

```python
# At executor start
logger = logger.bind(run_id=uuid4().hex[:8])

# At task execution start
task_logger = logger.bind(task_id="TASK-001", attempt=1)
```

In parallel mode, each concurrent task has its own bound logger with `task_id`.

### Replacement strategy

Replace all `print()` calls with structlog calls:
- `print("message")` → `logger.info("message")`
- `print(f"warning: {x}")` → `logger.warning("warning", detail=x)`
- `print(f"error: {e}")` → `logger.error("error", error=str(e))`

The `log_progress()` function in `runner.py` continues to write to the progress file AND delegates to structlog.

### Config

```yaml
# executor.config.yaml
log_level: info       # debug, info, warning, error
```

CLI flags:
- `--log-level=debug` — override log level
- `--log-json` — JSON output mode

### Redaction

Structlog processor masks sensitive patterns:
- API keys matching `sk-...` or `key-...`
- Environment variable values containing `TOKEN`, `SECRET`, `KEY`

## 2. TUI Dashboard

### Library

`textual` — modern Python TUI framework, async-native.

### New module: `src/spec_runner/tui.py` (~300-400 lines)

### Layout

```
┌─────────────────────────────────────────────────┐
│  spec-runner — Phase 2 Tasks          00:12:34  │
├──────────┬──────────┬───────────┬───────────────┤
│ BLOCKED  │   TODO   │ RUNNING   │     DONE      │
├──────────┼──────────┼───────────┼───────────────┤
│ TASK-005 │ TASK-003 │ TASK-001  │ TASK-000      │
│ p1 Auth  │ p1 API   │ p0 Setup  │ 45s, $0.12   │
│ <- 001   │          │ 00:01:23  │               │
│          │          │           │ TASK-002      │
│          │          │ TASK-004  │ 30s, $0.08   │
│          │          │ p1 Tests  │               │
│          │          │ 00:00:45  │               │
├──────────┴──────────┴───────────┴───────────────┤
│ 6 tasks | 45.2K in / 12.8K out | $0.84 | prog  │
└─────────────────────────────────────────────────┘
```

### Components

**`SpecRunnerApp(App)`** — Main Textual app:
- Header: project name, elapsed wall-clock time
- Body: 4-column horizontal layout (BLOCKED, TODO, RUNNING, DONE)
- Footer: aggregated stats (tasks, tokens, cost, progress bar)

**`TaskCard(Static)`** — Individual task widget:
- Task ID, priority badge, truncated name
- Status-specific info: elapsed time (running), cost (done), error type (failed), dependency (blocked)

**`StatsBar(Static)`** — Footer widget:
- Total tasks, token usage, cost, completion percentage

### Data source

Polls SQLite state database every 2 seconds:
```python
async def poll_state(self) -> None:
    """Read state from SQLite and update widgets."""
    state = ExecutorState(self.config)
    tasks = parse_tasks(self.config.tasks_file)
    # Update widgets...
    state.close()
```

### Integration with execution

Two modes:

1. **`spec-runner tui`** — Read-only dashboard. Shows current state, auto-refreshes. No execution.

2. **`spec-runner run --all --tui`** — Dashboard + execution. Runs `_run_tasks` / `_run_tasks_parallel` in a Textual worker thread. TUI owns the main thread.

```python
@work(thread=True)
def run_executor(self) -> None:
    """Run task execution in background thread."""
    _run_tasks(self.args, self.config)
```

### Keyboard shortcuts

| Key | Action |
|-----|--------|
| `q` | Quit (stops execution gracefully via stop file) |
| `r` | Retry selected failed task |
| `s` | Request graceful stop |

### Color scheme

- BLOCKED: dim/gray
- TODO: white/default
- RUNNING: yellow/amber
- DONE: green
- FAILED: red

## 3. New CLI Flags

```bash
spec-runner run --all --tui              # Execute with TUI dashboard
spec-runner run --all --parallel --tui   # Parallel + TUI
spec-runner tui                          # Read-only dashboard (no execution)
spec-runner run --log-level=debug        # Debug logging
spec-runner run --log-json               # JSON log output
```

## 4. Config Additions

```yaml
# executor.config.yaml
log_level: info        # Log level (default: info)
```

## 5. Files Changed

| File | Change |
|------|--------|
| `logging.py` | **NEW** — structlog setup, processors, redaction |
| `tui.py` | **NEW** — Textual app, TaskCard, StatsBar widgets |
| `executor.py` | Replace print() with logger, add `--tui`/`--log-level`/`--log-json` flags, `cmd_tui()` |
| `hooks.py` | Replace print() with logger |
| `runner.py` | Update `log_progress()` to use structlog, replace remaining prints |
| `config.py` | Add `log_level` field |
| `state.py` | No changes (no print calls) |
| `prompt.py` | No changes (no print calls) |
| `task.py` | Replace print() with logger in CLI commands |

## 6. New Dependencies

```toml
[project]
dependencies = [
    "structlog",
    "textual",
]
```

## 7. What Does NOT Change

- Sequential execution path logic
- Parallel execution path logic
- Task parsing, dependency resolution
- Prompt building
- State persistence, budget enforcement
- Hook execution flow (only logging changes)
- spec/tasks.md format
