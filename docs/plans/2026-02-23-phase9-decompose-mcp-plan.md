# Phase 9: Decompose executor.py & MCP Server — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Split executor.py (~1970 lines) into 3 focused modules and add a read-only MCP server for Claude Code integration.

**Architecture:** Mechanical file split with re-exports for backward compatibility. MCP server uses FastMCP SDK with stdio transport, wrapping existing query functions.

**Tech Stack:** Python 3.10+, `mcp` PyPI package (FastMCP SDK)

---

### Task 1: Create execution.py — extract task execution core

**Files:**
- Create: `src/spec_runner/execution.py`
- Modify: `src/spec_runner/executor.py`

**Context:**
- Lines 84–531 of `executor.py` contain: `execute_task()`, retry strategy constants/functions, `run_with_retries()`
- These functions import from: `config`, `hooks`, `logging`, `prompt`, `runner`, `state`, `task`
- The `_shutdown_requested` global is referenced by `check_stop_requested()` in `state.py`, not directly by execution functions

**Step 1: Create `execution.py` with execution + retry logic**

Cut lines 84–531 from `executor.py` into new file `src/spec_runner/execution.py`. Add proper imports:

```python
"""Task execution core — execute_task(), retry strategy, run_with_retries()."""

import re
import subprocess
import time
from datetime import datetime

from .config import ExecutorConfig
from .hooks import post_done_hook, pre_start_hook
from .logging import get_logger
from .prompt import build_task_prompt, extract_test_failures
from .runner import (
    build_cli_command,
    check_error_patterns,
    log_progress,
    parse_token_usage,
    send_callback,
)
from .state import (
    ErrorCode,
    ExecutorState,
    RetryContext,
    check_stop_requested,
)
from .task import (
    Task,
    mark_all_checklist_done,
    update_task_status,
)

logger = get_logger("executor")

# === Task Executor ===

# ... (paste execute_task, lines 87-377)

# === Retry Strategy ===

# ... (paste _FATAL_ERRORS, _EXPONENTIAL_ERRORS, classify_retry_strategy,
#      compute_retry_delay, run_with_retries, lines 380-531)
```

**Step 2: Update `executor.py` — replace cut section with imports**

Remove lines 84–531 from `executor.py`. Add import at the top (after existing imports):

```python
from .execution import (
    _FATAL_ERRORS,
    _EXPONENTIAL_ERRORS,
    classify_retry_strategy,
    compute_retry_delay,
    execute_task,
    run_with_retries,
)
```

Also remove now-unused imports from `executor.py` that were only used by the moved code (check each: `re`, `subprocess`, `extract_test_failures`, `build_task_prompt`, `check_error_patterns`, `build_cli_command`, `parse_token_usage`, `log_progress`, `send_callback`, `RetryContext`, `check_stop_requested`, `mark_all_checklist_done`, `pre_start_hook`, `post_done_hook`). Keep imports that are still used by remaining functions (parallel.py code and CLI commands still reference many of these).

**Step 3: Run full test suite**

Run: `uv run pytest tests/ -v -m "not slow" --tb=short`
Expected: All 393+ tests PASS (nothing changed functionally)

**Step 4: Run linter**

Run: `uv run ruff check src/spec_runner/execution.py src/spec_runner/executor.py --fix && uv run ruff format src/spec_runner/execution.py src/spec_runner/executor.py`

**Step 5: Commit**

```bash
git add src/spec_runner/execution.py src/spec_runner/executor.py
git commit -m "refactor: extract execution.py from executor.py (execute_task, retry)"
```

---

### Task 2: Create parallel.py — extract async execution

**Files:**
- Create: `src/spec_runner/parallel.py`
- Modify: `src/spec_runner/executor.py`

**Context:**
- Lines 534–816 (after Task 1 renumbering) contain: `_execute_task_async()`, `_run_tasks_parallel()`
- These import from: `config`, `execution` (now), `hooks`, `prompt`, `runner`, `state`, `task`, `validate`
- `_execute_task_async` uses `asyncio.Lock` for state protection

**Step 1: Create `parallel.py`**

Cut the parallel execution section from `executor.py` into `src/spec_runner/parallel.py`:

```python
"""Parallel task execution — async wrappers with semaphore control."""

import asyncio
import time
from datetime import datetime

from .config import ExecutorConfig
from .execution import execute_task, run_with_retries
from .hooks import post_done_hook, pre_start_hook
from .logging import get_logger
from .prompt import build_task_prompt, extract_test_failures
from .runner import (
    build_cli_command,
    check_error_patterns,
    log_progress,
    parse_token_usage,
    run_claude_async,
)
from .state import (
    ErrorCode,
    ExecutorState,
    RetryContext,
    check_stop_requested,
    clear_stop_file,
    recover_stale_tasks,
)
from .task import (
    Task,
    get_next_tasks,
    mark_all_checklist_done,
    parse_tasks,
    resolve_dependencies,
    update_task_status,
)
from .validate import format_results, validate_all

logger = get_logger("executor")

# ... (paste _execute_task_async and _run_tasks_parallel)
```

**Step 2: Update `executor.py` — replace parallel section with import**

Remove the parallel section. Add:

```python
from .parallel import _execute_task_async, _run_tasks_parallel
```

Clean up now-unused imports from `executor.py`.

**Step 3: Run full test suite**

Run: `uv run pytest tests/ -v -m "not slow" --tb=short`
Expected: All tests PASS

**Step 4: Lint and commit**

```bash
uv run ruff check src/spec_runner/parallel.py src/spec_runner/executor.py --fix
uv run ruff format src/spec_runner/parallel.py src/spec_runner/executor.py
git add src/spec_runner/parallel.py src/spec_runner/executor.py
git commit -m "refactor: extract parallel.py from executor.py (async execution)"
```

---

### Task 3: Create cli.py — extract CLI commands and main()

**Files:**
- Create: `src/spec_runner/cli.py`
- Modify: `src/spec_runner/executor.py`

**Context:**
- Remaining code in `executor.py` after Tasks 1-2: imports, `_signal_handler`, `_run_tasks()`, 11 `cmd_*()` functions, `main()`
- This is ~1200 lines — the bulk of executor.py
- `_signal_handler` sets `_shutdown_requested` global — this needs to stay accessible to `state.py:check_stop_requested()`

**Important: `_shutdown_requested` global**

`state.py:check_stop_requested()` imports `_shutdown_requested` from `.executor`:
```python
from .executor import _shutdown_requested
```

After the move, `cli.py` will define `_shutdown_requested` and `_signal_handler`. We need `state.py` to import from `.cli` instead, OR keep the global in `executor.py` and import it into `cli.py`. The safest approach: keep `_shutdown_requested` and `_signal_handler` in `executor.py` (the re-export module), and have `cli.py` import them from `executor.py`. This avoids changing `state.py`.

**Step 1: Create `cli.py`**

Move all `cmd_*()` functions, `_run_tasks()`, and `main()` into `src/spec_runner/cli.py`:

```python
"""CLI commands and argument parsing for spec-runner."""

import argparse
import asyncio
import json
import shutil
import signal
import sys
import time
from datetime import datetime
from uuid import uuid4

from .config import (
    CONFIG_FILE,
    ExecutorConfig,
    ExecutorLock,
    build_config,
    load_config_from_yaml,
)
from .execution import (
    execute_task,
    run_with_retries,
)
from .logging import get_logger
from .parallel import _run_tasks_parallel
from .prompt import (
    build_task_prompt,
    load_prompt_template,
    render_template,
)
from .runner import log_progress
from .state import (
    ExecutorState,
    check_stop_requested,
    clear_stop_file,
    recover_stale_tasks,
)
from .task import (
    Task,
    get_next_tasks,
    get_task_by_id,
    mark_all_checklist_done,
    parse_tasks,
    resolve_dependencies,
    update_task_status,
)
from .validate import format_results, validate_all

logger = get_logger("executor")

# Import shutdown flag from executor (where state.py expects it)
from .executor import _shutdown_requested, _signal_handler

# ... (paste _run_tasks, all cmd_* functions, main)
```

**Step 2: Slim down `executor.py` to re-exports**

```python
"""Backward-compatible re-exports.

All public API is available from this module for existing imports.
Implementation moved to execution.py, parallel.py, cli.py.
"""

from .logging import get_logger

logger = get_logger("executor")

# Global shutdown flag — kept here because state.py imports it from .executor
_shutdown_requested = False


def _signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM by setting shutdown flag."""
    global _shutdown_requested
    _shutdown_requested = True


# Re-exports from execution.py
from .execution import (  # noqa: E402
    _FATAL_ERRORS,
    _EXPONENTIAL_ERRORS,
    classify_retry_strategy,
    compute_retry_delay,
    execute_task,
    run_with_retries,
)

# Re-exports from cli.py
from .cli import (  # noqa: E402
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
```

**Step 3: Run full test suite**

Run: `uv run pytest tests/ -v --tb=short`
Expected: All 399 tests PASS

**Step 4: Lint and commit**

```bash
uv run ruff check src/spec_runner/ --fix
uv run ruff format src/spec_runner/
git add src/spec_runner/cli.py src/spec_runner/executor.py
git commit -m "refactor: extract cli.py from executor.py (all commands + main)"
```

---

### Task 4: Update __init__.py exports

**Files:**
- Modify: `src/spec_runner/__init__.py`

**Step 1: Verify all existing exports still work**

Run: `uv run python -c "from spec_runner import execute_task, run_with_retries, classify_retry_strategy, compute_retry_delay, cmd_costs, cmd_watch, executor_main; print('OK')"`

This should work because `__init__.py` imports from `executor` which re-exports.

**Step 2: Run full test suite**

Run: `uv run pytest tests/ -v --tb=short`
Expected: All 399 tests PASS

**Step 3: Commit if any changes needed**

```bash
git add src/spec_runner/__init__.py
git commit -m "chore: verify __init__.py exports after decomposition"
```

---

### Task 5: MCP Server — tests

**Files:**
- Create: `tests/test_mcp.py`

**Context:**
- `mcp_server.py` will define 4 tool handler functions
- Tests call handlers directly (no MCP transport needed)
- Each handler takes simple params and returns JSON string
- Handlers internally build `ExecutorConfig` and call existing functions

**Step 1: Write failing tests**

```python
"""Tests for MCP server tool handlers."""

import json
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState


def _make_config(tmp_path: Path) -> ExecutorConfig:
    """Create minimal config for MCP tests."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    return ExecutorConfig(
        project_root=tmp_path,
        state_file=spec_dir / ".executor-state.db",
        budget_usd=5.0,
    )


def _write_tasks(tasks_file: Path, tasks: list[tuple[str, str, str, str]]) -> None:
    """Write tasks.md from (id, name, priority, status) tuples."""
    priority_emoji = {"p0": "\U0001f534", "p1": "\U0001f7e0", "p2": "\U0001f7e1", "p3": "\U0001f7e2"}
    status_emoji = {"todo": "\u2b1c", "in_progress": "\U0001f504", "done": "\u2705", "blocked": "\u23f8\ufe0f"}
    lines = ["# Tasks\n"]
    for tid, name, prio, status in tasks:
        p = priority_emoji.get(prio, "\U0001f534")
        s = status_emoji.get(status, "\u2b1c")
        lines.append(f"### {tid}: {name}")
        lines.append(f"{p} {prio.upper()} | {s} {status.upper()} | Est: 1d")
        lines.append("")
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text("\n".join(lines))


def _seed_state(config: ExecutorConfig, task_data: dict) -> None:
    """Populate state.db with attempts."""
    with ExecutorState(config) as state:
        for task_id, attempts in task_data.items():
            for success, cost, inp, out in attempts:
                state.record_attempt(
                    task_id,
                    success=success,
                    duration=10.0,
                    error=None if success else "test error",
                    input_tokens=inp,
                    output_tokens=out,
                    cost_usd=cost,
                )


class TestMCPStatus:
    def test_status_returns_json(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_status
        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [
            ("TASK-001", "Login", "p0", "done"),
            ("TASK-002", "Signup", "p1", "todo"),
        ])
        _seed_state(config, {"TASK-001": [(True, 0.50, 1000, 500)]})
        result = json.loads(_handle_status(config))
        assert result["total_tasks"] == 2
        assert result["completed"] == 1
        assert result["total_cost"] == 0.50


class TestMCPTasks:
    def test_tasks_returns_list(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_tasks
        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [
            ("TASK-001", "Login", "p0", "done"),
            ("TASK-002", "Signup", "p1", "todo"),
        ])
        result = json.loads(_handle_tasks(config))
        assert len(result) == 2
        assert result[0]["id"] == "TASK-001"

    def test_tasks_filter_by_status(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_tasks
        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [
            ("TASK-001", "Login", "p0", "done"),
            ("TASK-002", "Signup", "p1", "todo"),
        ])
        result = json.loads(_handle_tasks(config, status="todo"))
        assert len(result) == 1
        assert result[0]["id"] == "TASK-002"


class TestMCPCosts:
    def test_costs_returns_breakdown(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_costs
        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [
            ("TASK-001", "Login", "p0", "done"),
        ])
        _seed_state(config, {"TASK-001": [(True, 0.45, 12500, 3200)]})
        result = json.loads(_handle_costs(config))
        assert "tasks" in result
        assert "summary" in result
        assert result["summary"]["total_cost"] == 0.45


class TestMCPLogs:
    def test_logs_returns_text(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_logs
        config = _make_config(tmp_path)
        log_dir = config.logs_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "TASK-001-20260101-120000.log"
        log_file.write_text("line 1\nline 2\nline 3\n")
        result = _handle_logs(config, task_id="TASK-001", lines=2)
        assert "line 2" in result
        assert "line 3" in result
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp.py -v`
Expected: FAIL — `spec_runner.mcp_server` not found

**Step 3: Commit**

```bash
git add tests/test_mcp.py
git commit -m "test: add MCP server handler tests (red phase)"
```

---

### Task 6: MCP Server — implementation

**Files:**
- Create: `src/spec_runner/mcp_server.py`
- Modify: `src/spec_runner/cli.py` (add `cmd_mcp` + subparser)

**Step 1: Add `mcp` dependency**

Run: `uv add mcp`

**Step 2: Create `mcp_server.py`**

```python
"""Read-only MCP server for spec-runner — exposes status, tasks, costs, logs as tools."""

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import ExecutorConfig, build_config, load_config_from_yaml
from .state import ExecutorState
from .task import parse_tasks, resolve_dependencies

mcp = FastMCP("spec-runner")


def _build_config(spec_prefix: str = "") -> ExecutorConfig:
    """Build ExecutorConfig from YAML + optional spec_prefix."""
    import argparse

    yaml_config = load_config_from_yaml()
    args = argparse.Namespace(
        spec_prefix=spec_prefix,
        project_root="",
        max_retries=3,
        timeout=30,
        no_tests=False,
        no_branch=False,
        no_commit=False,
        no_review=False,
        hitl_review=False,
        callback_url="",
        log_level=None,
    )
    return build_config(yaml_config, args)


def _handle_status(config: ExecutorConfig) -> str:
    """Get execution status summary."""
    tasks = parse_tasks(config.tasks_file) if config.tasks_file.exists() else []
    with ExecutorState(config) as state:
        completed = sum(1 for ts in state.tasks.values() if ts.status == "success")
        failed = sum(1 for ts in state.tasks.values() if ts.status == "failed")
        running = sum(1 for ts in state.tasks.values() if ts.status == "running")
        cost = state.total_cost()
        inp, out = state.total_tokens()

    return json.dumps({
        "total_tasks": len(tasks),
        "completed": completed,
        "failed": failed,
        "running": running,
        "not_started": len(tasks) - completed - failed - running,
        "total_cost": round(cost, 2),
        "input_tokens": inp,
        "output_tokens": out,
        "budget_usd": config.budget_usd,
    })


def _handle_tasks(config: ExecutorConfig, status: str | None = None) -> str:
    """List tasks from tasks.md."""
    tasks = parse_tasks(config.tasks_file) if config.tasks_file.exists() else []
    tasks = resolve_dependencies(tasks)
    result = []
    for t in tasks:
        if status and t.status != status:
            continue
        result.append({
            "id": t.id,
            "name": t.name,
            "priority": t.priority,
            "status": t.status,
            "depends_on": t.depends_on,
        })
    return json.dumps(result)


def _handle_costs(config: ExecutorConfig, sort: str = "id") -> str:
    """Per-task cost breakdown."""
    tasks = parse_tasks(config.tasks_file) if config.tasks_file.exists() else []
    rows = []
    with ExecutorState(config) as state:
        for t in tasks:
            ts = state.tasks.get(t.id)
            cost = state.task_cost(t.id)
            inp = sum(a.input_tokens for a in ts.attempts if a.input_tokens) if ts else 0
            out = sum(a.output_tokens for a in ts.attempts if a.output_tokens) if ts else 0
            rows.append({
                "task_id": t.id,
                "name": t.name,
                "status": ts.status if ts else t.status,
                "cost": round(cost, 4),
                "attempts": ts.attempt_count if ts else 0,
                "input_tokens": inp,
                "output_tokens": out,
            })
        total_cost = state.total_cost()
        total_inp, total_out = state.total_tokens()

    if sort == "cost":
        rows.sort(key=lambda r: r["cost"], reverse=True)
    elif sort == "tokens":
        rows.sort(key=lambda r: r["input_tokens"] + r["output_tokens"], reverse=True)

    return json.dumps({
        "tasks": rows,
        "summary": {
            "total_cost": round(total_cost, 2),
            "total_input_tokens": total_inp,
            "total_output_tokens": total_out,
            "budget_usd": config.budget_usd,
        },
    })


def _handle_logs(config: ExecutorConfig, task_id: str, lines: int = 50) -> str:
    """Get last N lines of task log."""
    log_dir = config.logs_dir
    if not log_dir.exists():
        return f"No logs directory at {log_dir}"
    # Find log files matching task_id
    matching = sorted(log_dir.glob(f"{task_id}*"), reverse=True)
    if not matching:
        return f"No logs found for {task_id}"
    log_file = matching[0]
    all_lines = log_file.read_text().splitlines()
    return "\n".join(all_lines[-lines:])


# === MCP Tool Definitions ===


@mcp.tool()
def spec_runner_status(spec_prefix: str = "") -> str:
    """Get spec-runner execution status: tasks completed/failed/running, cost, tokens."""
    config = _build_config(spec_prefix)
    return _handle_status(config)


@mcp.tool()
def spec_runner_tasks(status: str = "", spec_prefix: str = "") -> str:
    """List tasks from tasks.md with id, name, priority, status, dependencies."""
    config = _build_config(spec_prefix)
    return _handle_tasks(config, status=status or None)


@mcp.tool()
def spec_runner_costs(sort: str = "id", spec_prefix: str = "") -> str:
    """Per-task cost breakdown with summary totals."""
    config = _build_config(spec_prefix)
    return _handle_costs(config, sort=sort)


@mcp.tool()
def spec_runner_logs(task_id: str, lines: int = 50, spec_prefix: str = "") -> str:
    """Get last N lines of a task's execution log."""
    config = _build_config(spec_prefix)
    return _handle_logs(config, task_id=task_id, lines=lines)


def run_server() -> None:
    """Run the MCP server (stdio transport)."""
    mcp.run(transport="stdio")
```

**Step 3: Add `cmd_mcp` to `cli.py`**

Add function:
```python
def cmd_mcp(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Launch MCP server (stdio transport)."""
    from .mcp_server import run_server
    run_server()
```

Add subparser in `main()`:
```python
    # mcp
    subparsers.add_parser("mcp", parents=[common], help="Launch read-only MCP server")
```

Add to commands dict:
```python
    "mcp": cmd_mcp,
```

**Step 4: Run MCP tests**

Run: `uv run pytest tests/test_mcp.py -v`
Expected: All PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v -m "not slow" --tb=short`
Expected: All PASS

**Step 6: Lint and commit**

```bash
uv run ruff check . --fix && uv run ruff format .
git add src/spec_runner/mcp_server.py src/spec_runner/cli.py pyproject.toml uv.lock tests/test_mcp.py
git commit -m "feat: add read-only MCP server (status, tasks, costs, logs)"
```

---

### Task 7: Exports, docs, final verification

**Files:**
- Modify: `src/spec_runner/__init__.py` (add MCP exports)
- Modify: `CLAUDE.md` (update module table, add mcp command)

**Step 1: Update `__init__.py`**

Add:
```python
from .mcp_server import run_server as mcp_run_server
```

Add to `__all__`: `"mcp_run_server"`

**Step 2: Update CLAUDE.md**

Add to CLI entry points:
```
spec-runner mcp                            # Launch read-only MCP server (stdio)
```

Update module table to reflect new modules: `execution.py`, `parallel.py`, `cli.py`, `mcp_server.py`.

**Step 3: Run full test suite including slow**

Run: `uv run pytest tests/ -v --tb=short`
Expected: All tests PASS

**Step 4: Commit**

```bash
git add src/spec_runner/__init__.py CLAUDE.md
git commit -m "docs: update exports and CLAUDE.md for Phase 9 modules"
```

---

## Summary

| Task | Feature | New files | Modified files | Risk |
|------|---------|-----------|----------------|------|
| 1 | Extract execution.py | `execution.py` | `executor.py` | Low — pure move |
| 2 | Extract parallel.py | `parallel.py` | `executor.py` | Low — pure move |
| 3 | Extract cli.py | `cli.py` | `executor.py` | Medium — `_shutdown_requested` wiring |
| 4 | Verify exports | — | `__init__.py` (maybe) | Low |
| 5 | MCP tests | `test_mcp.py` | — | Low |
| 6 | MCP implementation | `mcp_server.py` | `cli.py` | Low — thin wrappers |
| 7 | Exports + docs | — | `__init__.py`, `CLAUDE.md` | Low |

### Critical path

Task 1 → Task 2 → Task 3 → Task 4 (decomposition must be sequential)
Task 5 → Task 6 (TDD for MCP)
Task 7 (after all)
