# Phase 9: Decompose executor.py & MCP Server

**Goal:** Split the 1970-line executor.py into focused modules, and add a read-only MCP server for Claude Code integration.

**Scope:** Two features, ~300 lines of new code, 3 new modules, 1 modified module.

---

## 1. Decompose executor.py

### Problem

`executor.py` is ~1970 lines containing task execution, retry logic, parallel execution, 11 CLI commands, and argparse setup. This makes navigation difficult and conflates execution logic with CLI concerns.

### Approach: Mechanical split

Move functions into new modules by logical group. `executor.py` becomes a thin re-export layer for backward compatibility. No logic changes, no DRY refactoring — pure file reorganization.

### New modules

| Module | Contents | ~Lines |
|--------|----------|--------|
| `execution.py` | `execute_task()`, `_FATAL_ERRORS`, `_EXPONENTIAL_ERRORS`, `classify_retry_strategy()`, `compute_retry_delay()`, `run_with_retries()` | ~440 |
| `parallel.py` | `_execute_task_async()`, `_run_tasks_parallel()` | ~280 |
| `cli.py` | `_signal_handler`, `_run_tasks()`, all `cmd_*()` functions (11 total), `main()` with argparse | ~1150 |

### executor.py after split (~50 lines)

Backward-compatible re-exports:

```python
"""Backward-compatible re-exports.

All public API is available from this module for existing imports.
Implementation moved to execution.py, parallel.py, cli.py.
"""
from .cli import (
    cmd_costs,
    cmd_logs,
    cmd_plan,
    cmd_reset,
    cmd_retry,
    cmd_run,
    cmd_status,
    cmd_stop,
    cmd_tui,
    cmd_validate,
    cmd_watch,
    main,
)
from .execution import (
    classify_retry_strategy,
    compute_retry_delay,
    execute_task,
    run_with_retries,
)
```

### What doesn't change

- **`pyproject.toml`** entry point: `spec-runner = "spec_runner.executor:main"` — works via re-export
- **`__init__.py`** — imports from `spec_runner.executor` which re-exports
- **All tests** — import from `spec_runner.executor` or `spec_runner` which both re-export
- **No logic changes** — pure mechanical move

### Import resolution

Each new module imports what it needs from sibling modules:

- `execution.py` imports from: `config`, `hooks`, `prompt`, `runner`, `state`, `task`
- `parallel.py` imports from: `config`, `execution`, `hooks`, `prompt`, `runner`, `state`, `task`, `validate`
- `cli.py` imports from: `config`, `execution`, `parallel`, `state`, `task`, `validate`, `logging`

No circular dependencies: `execution` ← `parallel` ← `cli`.

---

## 2. MCP Server (read-only)

### Architecture

New module `src/spec_runner/mcp_server.py` (~150 lines) using the `mcp` Python SDK (FastMCP).

Stdio-based MCP server exposing 4 read-only tools. Each tool is a thin wrapper calling existing functions.

### Tools

| Tool name | Description | Input | Output |
|-----------|-------------|-------|--------|
| `spec_runner_status` | Execution status summary | `spec_prefix` (optional) | JSON: tasks total/completed/failed, cost, tokens |
| `spec_runner_costs` | Per-task cost breakdown | `sort` (optional: id/cost/tokens), `spec_prefix` (optional) | JSON: tasks array with cost/tokens, summary |
| `spec_runner_tasks` | List tasks from tasks.md | `status` (optional filter), `spec_prefix` (optional) | JSON: array of {id, name, priority, status, depends_on} |
| `spec_runner_logs` | Task execution logs | `task_id` (required), `lines` (optional, default 50) | Text: last N lines of task log |

### CLI integration

```
spec-runner mcp        # Launch stdio MCP server
```

New `cmd_mcp()` in `cli.py`, new `mcp` subparser in `main()`.

### Claude Code integration

Users add to `.mcp.json`:

```json
{
  "mcpServers": {
    "spec-runner": {
      "command": "spec-runner",
      "args": ["mcp"]
    }
  }
}
```

### Implementation

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("spec-runner")

@mcp.tool()
def spec_runner_status(spec_prefix: str = "") -> str:
    """Get execution status summary."""
    config = _build_config(spec_prefix)
    tasks = parse_tasks(config.tasks_file)
    with ExecutorState(config) as state:
        # ... build status dict ...
    return json.dumps(result)
```

Each tool:
1. Builds `ExecutorConfig` from defaults + optional `spec_prefix`
2. Calls existing functions (`parse_tasks`, `ExecutorState`, etc.)
3. Returns JSON string

### Dependency

Add `mcp` package: `uv add mcp`

### What we don't do

- No write tools (run, retry, stop) — read-only for safety
- No SSE/WebSocket transport — stdio sufficient for Claude Code
- No authentication — local tool, trusted environment

---

## Summary

| Feature | New files | Modified files | ~Lines |
|---------|-----------|----------------|--------|
| Decompose executor.py | `execution.py`, `parallel.py`, `cli.py` | `executor.py` (gutted to re-exports) | ~50 new (rest is moved) |
| MCP Server | `mcp_server.py` | `cli.py` (add cmd_mcp + subparser) | ~150 |
| **Total** | **4 new modules** | **1 modified** | **~200 new** |

### Implementation order

1. **Decompose executor.py** — must be first, MCP server's `cmd_mcp` goes into `cli.py`
2. **MCP Server** — depends on clean module structure

### Testing strategy

- **Decompose**: Run full test suite after each module extraction — zero test changes expected
- **MCP Server**: `test_mcp.py` — unit tests calling tool handlers directly with tmp_path configs
