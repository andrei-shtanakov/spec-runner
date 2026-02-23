# Phase 8: TUI Dashboard, Cost Reporting, Watch Mode

**Goal:** Add a full kanban TUI dashboard, a cost reporting command, and a continuous watch mode that auto-executes tasks when tasks.md changes.

**Scope:** Three features, ~540 lines of new code, 1 new module.

---

## 1. TUI Dashboard (Kanban)

### Layout

```
+-------------------------------------------------------------+
|  spec-runner dashboard                          [q]uit [r]   |
+------------------+------------------+------------------------+
|  TODO (3)        |  IN PROGRESS (1) |  DONE (2)              |
|                  |                  |                        |
|  TASK-004 [p1]   |  TASK-003 [p0]   |  TASK-001 [p0]         |
|  Add tests       |  API endpoints   |  Login page            |
|                  |  timer 2m 15s    |  $0.45 | 1 attempt     |
|  TASK-005 [p2]   |                  |                        |
|  Docs            |                  |  TASK-002 [p1]         |
|                  |                  |  DB schema             |
|  TASK-006 [p3]   |                  |  $0.23 | 2 attempts    |
|  Refactor        |                  |                        |
+------------------+------------------+------------------------+
|  [14:23] TASK-003 Attempt 1/3                                |
|  [14:24] TASK-003 Completed in 27.3s                         |
+-------------------------------------------------------------+
|  Done: 2/6 | Cost: $0.68/$5.00 | Tokens: 20.7K in, 5.3K out |
+-------------------------------------------------------------+
```

### Architecture

New module: `src/spec_runner/tui.py` (~400 lines)

**Widgets:**
- `DashboardApp(App)` — main Textual application, manages layout and refresh timer
- `TaskColumn(Static)` — renders a list of task cards for one status (todo/in_progress/done)
- `TaskCard(Static)` — single task card with id, name, priority, cost info
- `LogPanel(RichLog)` — tails `spec/.executor-progress.txt` in real-time
- `StatusBar(Static)` — footer with aggregated cost/token/progress stats

**Data flow:**
1. On startup: parse `tasks.md` + read `state.db` for cost/attempt data
2. Every 2 seconds: re-parse tasks, re-read state, update all widgets
3. Log panel: read new lines from progress file on each tick
4. Polling via `set_interval(2.0)` — no inotify, no watchdog

**Keyboard shortcuts:**
- `q` — quit
- `r` — force refresh
- `j/k` — scroll log panel

**No interactivity:** Tasks are read-only in TUI. Task execution is managed via `spec-runner run` or `spec-runner watch`. The TUI is a monitoring tool, not a control panel.

### Integration

- `spec-runner tui` — standalone dashboard (replaces existing stub `cmd_tui`)
- `spec-runner watch --tui` — watch mode with live dashboard
- `spec-runner run --tui` — existing flag, rewired to use new dashboard

### New code

- Create `src/spec_runner/tui.py` (~400 lines)
- Modify `executor.py` — update `cmd_tui()` to launch new dashboard

---

## 2. Cost Reporting

### CLI output

```
$ spec-runner costs

Task Costs:
  TASK-001  Add login page       done    $0.45  3 attempts  12.5K tokens
  TASK-002  DB schema            done    $0.23  1 attempt    8.2K tokens
  TASK-003  API endpoints        failed  $0.31  3 attempts  10.1K tokens
  TASK-004  Add tests            todo    --     --          --
  TASK-005  Docs                 todo    --     --          --

Summary:
  Total cost:     $0.99 / $5.00 budget (19.8%)
  Total tokens:   30.8K input, 8.4K output
  Avg per task:   $0.33 (completed only)
  Most expensive: TASK-001 ($0.45)
```

### Flags

- `--json` — output as JSON for automation
- `--sort=cost|tokens|name` — sort order (default: task id)

### Implementation

New function `cmd_costs()` in `executor.py` (~80 lines):
1. Parse tasks from `tasks.md` for names and statuses
2. Open `state.db`, read cost/tokens per task via `state.task_cost()` and `state.get_task_state()`
3. Format table with aligned columns
4. Summary section: total cost, budget percentage, avg per completed task, most expensive
5. JSON mode: output dict with `tasks` array and `summary` object

### New code

- Modify `executor.py` — add `cmd_costs()`, `costs` subparser (~80 lines)

---

## 3. Watch Mode

### Behavior

```
$ spec-runner watch

Watching spec/tasks.md for changes...
Polling every 5s | Stop: Ctrl+C or touch spec/.executor-stop

[14:23:01] Found 2 ready tasks
[14:23:01] Starting TASK-003: API endpoints
[14:25:17] TASK-003 completed ($0.31)
[14:25:22] Found 1 ready task
[14:25:22] Starting TASK-004: Add tests
[14:29:03] TASK-004 completed ($0.42)
[14:29:08] No ready tasks. Waiting...
[14:40:08] Found 1 ready task (tasks.md changed)
[14:40:08] Starting TASK-005: Docs
```

### Implementation

New function `cmd_watch()` in `executor.py` (~80 lines):

```
loop:
  1. Check stop file → break
  2. Check consecutive failure limit → break
  3. Parse tasks.md, resolve dependencies, get next tasks
  4. If no tasks → sleep 5s, continue
  5. Pre-run validation (first iteration only)
  6. Execute task via run_with_retries
  7. Track consecutive failures
  8. Brief pause (1s) between tasks
```

### Key decisions

- **Polling, not watchdog** — no extra dependencies, 5s interval sufficient
- **One task at a time** — sequential, like `spec-runner run --all` but infinite loop
- **Respects stop file** and Ctrl+C via existing signal handler
- **Pre-run validation** — calls `validate_all()` before first execution
- **Consecutive failure limit** — stops after `max_consecutive_failures` in a row (default 2)

### Watch + TUI combination

`spec-runner watch --tui` launches the TUI dashboard as the main thread, with the watch loop running as an asyncio background task. The TUI polls state.db for updates as usual.

### New code

- Modify `executor.py` — add `cmd_watch()`, `watch` subparser (~80 lines)

---

## Summary

| Feature | New files | Modified files | ~Lines |
|---------|-----------|----------------|--------|
| TUI Dashboard | `tui.py` | `executor.py`, `__init__.py` | ~420 |
| Cost Reporting | — | `executor.py` | ~80 |
| Watch Mode | — | `executor.py` | ~80 |
| **Total** | **1 new module** | **3 modified** | **~580** |

### Implementation order

1. **Cost Reporting** — simplest, standalone, no new dependencies
2. **Watch Mode** — reuses existing run_with_retries, no TUI dependency
3. **TUI Dashboard** — most complex, benefits from cost data and watch mode being ready

### Testing strategy

- `test_costs.py` — unit tests for cost formatting, JSON output, sorting
- `test_watch.py` — watch loop with mocked parse/execute, stop file, consecutive failures
- `test_tui.py` — Textual app tests using `pilot` test framework (headless)
