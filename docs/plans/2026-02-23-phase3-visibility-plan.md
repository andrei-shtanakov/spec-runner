# Phase 3 — Visibility Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace print-based output with structlog structured logging, add Textual-based TUI Kanban dashboard for real-time task monitoring.

**Architecture:** New `logging.py` module configures structlog with context processors and output formatters. New `tui.py` module provides a Textual app with Kanban columns reading from SQLite state. All existing `print()` calls are replaced with structlog bound loggers. TUI mode routes logs to file; CLI mode outputs pretty-printed logs to stderr.

**Tech Stack:** Python 3.10+ stdlib, structlog, textual

---

### Task 1: Add structlog and textual dependencies

**Files:**
- Modify: `pyproject.toml:29-32`

**Step 1: Add dependencies**

In `pyproject.toml`, change the `dependencies` section from:

```toml
dependencies = [
    "PyYAML>=6.0",
    "twine>=6.2.0",
]
```

to:

```toml
dependencies = [
    "PyYAML>=6.0",
    "structlog>=24.0",
    "textual>=1.0",
    "twine>=6.2.0",
]
```

**Step 2: Install dependencies**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv sync`
Expected: Resolves and installs structlog + textual

**Step 3: Verify imports work**

Run: `cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run python -c "import structlog; import textual; print('OK')"`
Expected: `OK`

**Step 4: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All 204 tests pass (no code changes, just new deps)

**Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "build: add structlog and textual dependencies"
```

---

### Task 2: Create logging.py module with structlog setup

**Files:**
- Create: `src/spec_runner/logging.py`
- Test: `tests/test_logging.py`

**Step 1: Write the failing tests**

Create `tests/test_logging.py`:

```python
"""Tests for spec_runner.logging module."""

import logging
from io import StringIO
from pathlib import Path

import structlog

from spec_runner.logging import setup_logging, get_logger, redact_sensitive


class TestSetupLogging:
    """Tests for setup_logging."""

    def test_setup_returns_none(self):
        setup_logging()

    def test_setup_with_json_mode(self):
        setup_logging(json_output=True)

    def test_setup_with_log_file(self, tmp_path):
        log_file = tmp_path / "test.log"
        setup_logging(log_file=log_file)

    def test_setup_with_tui_mode(self, tmp_path):
        log_file = tmp_path / "test.log"
        setup_logging(tui_mode=True, log_file=log_file)


class TestGetLogger:
    """Tests for get_logger."""

    def test_returns_bound_logger(self):
        logger = get_logger("test_module")
        assert logger is not None

    def test_logger_has_module_context(self):
        logger = get_logger("my_module")
        # Structlog bound loggers can be called
        assert callable(getattr(logger, "info", None))

    def test_logger_can_bind_task_id(self):
        logger = get_logger("executor")
        task_logger = logger.bind(task_id="TASK-001")
        assert task_logger is not None


class TestRedactSensitive:
    """Tests for redact_sensitive processor."""

    def test_redacts_sk_keys(self):
        _, _, event_dict = redact_sensitive(None, None, {"api_key": "sk-abc123def456"})
        assert "sk-" not in event_dict["api_key"]
        assert "***" in event_dict["api_key"]

    def test_preserves_normal_values(self):
        _, _, event_dict = redact_sensitive(None, None, {"message": "hello world"})
        assert event_dict["message"] == "hello world"

    def test_redacts_in_event_string(self):
        _, _, event_dict = redact_sensitive(
            None, None, {"event": "Using key sk-abc123def456ghi"}
        )
        assert "sk-abc123" not in event_dict["event"]
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_logging.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'spec_runner.logging'`

**Step 3: Implement logging.py**

Create `src/spec_runner/logging.py`:

```python
"""Structured logging for spec-runner.

Configures structlog with context processors, output formatters,
and sensitive data redaction.
"""

import logging
import re
import sys
from pathlib import Path

import structlog


# Regex for sensitive patterns
_SENSITIVE_RE = re.compile(r"(sk-|key-|token-)[a-zA-Z0-9]{6,}", re.IGNORECASE)


def redact_sensitive(
    logger: object, method_name: str, event_dict: dict
) -> tuple[object, str, dict]:
    """Structlog processor that redacts sensitive data."""
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = _SENSITIVE_RE.sub(
                lambda m: m.group(1) + "***", value
            )
    return logger, method_name, event_dict


def setup_logging(
    level: str = "info",
    json_output: bool = False,
    log_file: Path | None = None,
    tui_mode: bool = False,
) -> None:
    """Configure structlog for the entire application.

    Args:
        level: Log level (debug, info, warning, error).
        json_output: If True, output JSON lines.
        log_file: Path to log file (used in TUI mode or for file logging).
        tui_mode: If True, suppress console output (TUI owns the screen).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Shared processors
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        redact_sensitive,
    ]

    # Configure stdlib logging for structlog integration
    logging.basicConfig(
        format="%(message)s",
        level=log_level,
        stream=sys.stderr,
        force=True,
    )

    if tui_mode and log_file:
        # TUI mode: log to file only
        handler = logging.FileHandler(str(log_file))
        handler.setLevel(log_level)
        root = logging.getLogger()
        root.handlers = [handler]

    # Choose renderer
    if json_output:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=not tui_mode)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure formatter for stdlib handler
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


def get_logger(module: str) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger bound to a module name.

    Args:
        module: Module name (e.g., "executor", "hooks").

    Returns:
        Bound structlog logger.
    """
    return structlog.get_logger(module=module)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_logging.py -v`
Expected: PASS (9 tests)

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/spec_runner/logging.py tests/test_logging.py
git commit -m "feat: add structlog-based logging module with redaction"
```

---

### Task 3: Add log_level to config and CLI flags

**Files:**
- Modify: `src/spec_runner/config.py:72-83` (ExecutorConfig)
- Modify: `src/spec_runner/config.py:191-222` (load_config_from_yaml)
- Modify: `src/spec_runner/config.py:249-273` (build_config)
- Modify: `src/spec_runner/executor.py` (main argparse + setup_logging call)
- Test: `tests/test_config.py`

**Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
class TestLoggingConfig:
    def test_log_level_default(self):
        config = ExecutorConfig()
        assert config.log_level == "info"

    def test_log_level_from_kwargs(self):
        config = ExecutorConfig(log_level="debug")
        assert config.log_level == "debug"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py::TestLoggingConfig -v`
Expected: FAIL

**Step 3: Implement config changes**

3a. Add `log_level` field to `ExecutorConfig` (after line 83):

```python
    log_level: str = "info"  # Log level: debug, info, warning, error
```

3b. In `load_config_from_yaml()`, add to the return dict (after line 222):

```python
            "log_level": executor_config.get("log_level"),
```

3c. In `build_config()`, add CLI override handling (after line 273):

```python
    if hasattr(args, "log_level") and getattr(args, "log_level", None):
        config_kwargs["log_level"] = args.log_level
```

3d. In `main()` in executor.py, add CLI flags to `common` parser (after the `--project-root` argument):

```python
    common.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["debug", "info", "warning", "error"],
        help="Log level (default: info)",
    )
    common.add_argument(
        "--log-json",
        action="store_true",
        help="Output logs as JSON lines",
    )
```

3e. After `config = build_config(yaml_config, args)` in `main()`, add:

```python
    # Initialize structured logging
    from .logging import setup_logging
    setup_logging(
        level=config.log_level,
        json_output=getattr(args, "log_json", False),
    )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py::TestLoggingConfig -v`
Expected: PASS (2 tests)

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/spec_runner/config.py src/spec_runner/executor.py tests/test_config.py
git commit -m "feat: add log_level config, --log-level and --log-json CLI flags"
```

---

### Task 4: Replace print() calls in runner.py

**Files:**
- Modify: `src/spec_runner/runner.py:17-28` (log_progress function)

**Step 1: Update log_progress to use structlog**

Replace the `log_progress` function (lines 17-28):

```python
def log_progress(message: str, task_id: str | None = None):
    """Log progress message with timestamp to progress file and structlog."""
    from .logging import get_logger

    timestamp = datetime.now().strftime("%H:%M:%S")
    prefix = f"[{task_id}] " if task_id else ""
    line = f"[{timestamp}] {prefix}{message}\n"

    PROGRESS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PROGRESS_FILE, "a") as f:
        f.write(line)

    # Structured log (replaces print)
    logger = get_logger("runner")
    if task_id:
        logger.info(message, task_id=task_id)
    else:
        logger.info(message)
```

**Step 2: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 3: Commit**

```bash
git add src/spec_runner/runner.py
git commit -m "refactor: replace print with structlog in runner.py"
```

---

### Task 5: Replace print() calls in hooks.py

**Files:**
- Modify: `src/spec_runner/hooks.py`

**Step 1: Replace prints with structlog**

Add at the top of `hooks.py` (after existing imports):

```python
from .logging import get_logger

logger = get_logger("hooks")
```

Then replace all `print()` calls in hooks.py with `logger.info()`, `logger.warning()`, or `logger.error()`. Pattern:
- `print(f"🔧 Pre-start hook for {task.id}")` → `logger.info("Pre-start hook", task_id=task.id)`
- `print("   ✅ Dependencies synced")` → `logger.info("Dependencies synced")`
- `print(f"   ⚠️  uv sync warning: {result.stderr[:200]}")` → `logger.warning("uv sync warning", stderr=result.stderr[:200])`
- `print(f"   Created branch: {branch_name}")` → `logger.info("Created branch", branch=branch_name)`
- `print(f"   ❌ Tests failed!")` → `logger.error("Tests failed")`

Preserve the exact message content (just convert to structured key-value pairs where appropriate).

**Step 2: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 3: Commit**

```bash
git add src/spec_runner/hooks.py
git commit -m "refactor: replace print with structlog in hooks.py"
```

---

### Task 6: Replace print() calls in executor.py

**Files:**
- Modify: `src/spec_runner/executor.py`

**Step 1: Replace prints with structlog**

Add near the top of `executor.py` (after existing imports):

```python
from .logging import get_logger

logger = get_logger("executor")
```

Replace all `print()` calls (109 occurrences) with structured logger calls. Key patterns:

- Status messages: `print(f"📋 Tasks to execute: {n}")` → `logger.info("Tasks to execute", count=n)`
- Errors: `print(f"❌ Task {id} not found")` → `logger.error("Task not found", task_id=id)`
- Warnings: `print(f"⚠️  API error: {p}")` → `logger.warning("API error", pattern=p)`
- Summaries: `print(f"   Tasks completed: {n}")` → `logger.info("Tasks completed", count=n)`
- Section separators: `print(f"{'=' * 60}")` → remove (structlog handles formatting)

For the status command (`cmd_status`), keep `print()` calls since status is meant to be human-readable CLI output. Only replace `print()` in execution paths (execute_task, run_with_retries, _run_tasks, _run_tasks_parallel, cmd_run).

**Step 2: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 3: Commit**

```bash
git add src/spec_runner/executor.py
git commit -m "refactor: replace print with structlog in executor.py"
```

---

### Task 7: Replace print() calls in task.py and remaining modules

**Files:**
- Modify: `src/spec_runner/task.py`
- Modify: `src/spec_runner/config.py:225` (the one print in load_config)
- Modify: `src/spec_runner/init_cmd.py`

**Step 1: Replace prints**

For `task.py` — it has 68 print calls, mostly in CLI commands (list, next, graph, stats). These are CLI display output, so keep them as `print()`. Only replace `print()` calls that are warnings or errors.

For `config.py` line 225 — replace:
```python
print(f"⚠️  Warning: Failed to load config from {config_path}: {e}")
```
with:
```python
from .logging import get_logger
get_logger("config").warning("Failed to load config", path=str(config_path), error=str(e))
```

For `init_cmd.py` — keep print() calls (it's a simple CLI tool with 5 prints).

**Step 2: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 3: Commit**

```bash
git add src/spec_runner/task.py src/spec_runner/config.py src/spec_runner/init_cmd.py
git commit -m "refactor: replace print with structlog in config.py"
```

---

### Task 8: Create TUI app with Textual

**Files:**
- Create: `src/spec_runner/tui.py`
- Test: `tests/test_tui.py`

**Step 1: Write the failing tests**

Create `tests/test_tui.py`:

```python
"""Tests for spec_runner.tui module."""

from spec_runner.tui import SpecRunnerApp, TaskCard, StatsBar


class TestSpecRunnerApp:
    """Tests for the TUI app."""

    def test_app_exists(self):
        """SpecRunnerApp class exists and is a Textual App."""
        from textual.app import App
        assert issubclass(SpecRunnerApp, App)

    def test_app_has_title(self):
        app = SpecRunnerApp.__new__(SpecRunnerApp)
        assert hasattr(SpecRunnerApp, "TITLE")


class TestTaskCard:
    """Tests for TaskCard widget."""

    def test_task_card_exists(self):
        assert TaskCard is not None

    def test_task_card_format_done(self):
        """Done tasks show cost."""
        text = TaskCard.format_card(
            task_id="TASK-001",
            name="Setup project",
            priority="p0",
            status="done",
            cost=0.12,
            duration=45.0,
        )
        assert "TASK-001" in text
        assert "$0.12" in text

    def test_task_card_format_running(self):
        """Running tasks show elapsed time."""
        text = TaskCard.format_card(
            task_id="TASK-002",
            name="Add feature",
            priority="p1",
            status="running",
            elapsed=123.0,
        )
        assert "TASK-002" in text

    def test_task_card_format_blocked(self):
        """Blocked tasks show dependency."""
        text = TaskCard.format_card(
            task_id="TASK-003",
            name="Deploy",
            priority="p1",
            status="blocked",
            blocked_by="TASK-001",
        )
        assert "TASK-003" in text


class TestStatsBar:
    """Tests for StatsBar widget."""

    def test_stats_bar_exists(self):
        assert StatsBar is not None

    def test_stats_bar_format(self):
        text = StatsBar.format_stats(
            total=10,
            completed=5,
            failed=1,
            input_tokens=45200,
            output_tokens=12800,
            cost=0.84,
        )
        assert "10" in text
        assert "$0.84" in text
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tui.py -v`
Expected: FAIL with `ModuleNotFoundError`

**Step 3: Implement TUI app**

Create `src/spec_runner/tui.py`:

```python
"""TUI dashboard for spec-runner.

Textual-based Kanban board showing real-time task execution status.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Footer, Header, Static

from .config import ExecutorConfig
from .state import ExecutorState
from .task import Task, parse_tasks


class TaskCard(Static):
    """Widget displaying a single task as a card."""

    @staticmethod
    def format_card(
        task_id: str,
        name: str,
        priority: str = "p1",
        status: str = "todo",
        cost: float | None = None,
        duration: float | None = None,
        elapsed: float | None = None,
        blocked_by: str | None = None,
        error: str | None = None,
    ) -> str:
        """Format task info as a card string."""
        priority_badges = {"p0": "[bold red]P0[/]", "p1": "[yellow]P1[/]", "p2": "[blue]P2[/]", "p3": "[dim]P3[/]"}
        badge = priority_badges.get(priority, priority)

        lines = [f"[bold]{task_id}[/bold]", f"{badge} {name[:25]}"]

        if status == "done" and cost is not None:
            dur_str = f"{duration:.0f}s" if duration else ""
            lines.append(f"{dur_str}, ${cost:.2f}")
        elif status == "running" and elapsed is not None:
            mins, secs = divmod(int(elapsed), 60)
            lines.append(f"{mins:02d}:{secs:02d}")
        elif status == "blocked" and blocked_by:
            lines.append(f"<- {blocked_by}")
        elif status == "failed" and error:
            lines.append(f"[red]{error[:30]}[/red]")

        return "\n".join(lines)


class StatsBar(Static):
    """Widget displaying aggregated stats."""

    @staticmethod
    def format_stats(
        total: int = 0,
        completed: int = 0,
        failed: int = 0,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost: float = 0.0,
    ) -> str:
        """Format stats as a summary string."""
        def fmt_tok(n: int) -> str:
            return f"{n / 1000:.1f}K" if n >= 1000 else str(n)

        pct = int(completed / total * 100) if total > 0 else 0
        return (
            f"{total} tasks | "
            f"{fmt_tok(input_tokens)} in / {fmt_tok(output_tokens)} out | "
            f"${cost:.2f} | "
            f"{pct}% done"
        )


class KanbanColumn(Vertical):
    """A column in the Kanban board."""

    def __init__(self, title: str, **kwargs):
        super().__init__(**kwargs)
        self.border_title = title


class SpecRunnerApp(App):
    """Textual TUI for spec-runner."""

    TITLE = "spec-runner"
    CSS = """
    KanbanColumn {
        width: 1fr;
        border: solid $primary;
        height: 100%;
        overflow-y: auto;
    }

    TaskCard {
        margin: 0 1;
        padding: 0 1;
        height: auto;
    }

    StatsBar {
        height: 1;
        dock: bottom;
        background: $surface;
        padding: 0 1;
    }

    #board {
        height: 1fr;
    }

    .col-blocked TaskCard { color: $text-muted; }
    .col-todo TaskCard { color: $text; }
    .col-running TaskCard { color: $warning; }
    .col-done TaskCard { color: $success; }
    .col-failed TaskCard { color: $error; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("s", "stop", "Stop execution"),
    ]

    config: ExecutorConfig | None = None
    refresh_interval: float = 2.0

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="board"):
            yield KanbanColumn("BLOCKED", classes="col-blocked", id="col-blocked")
            yield KanbanColumn("TODO", classes="col-todo", id="col-todo")
            yield KanbanColumn("RUNNING", classes="col-running", id="col-running")
            yield KanbanColumn("DONE", classes="col-done", id="col-done")
        yield StatsBar(id="stats")
        yield Footer()

    def on_mount(self) -> None:
        """Start periodic refresh."""
        self.set_interval(self.refresh_interval, self.refresh_board)
        self.refresh_board()

    def refresh_board(self) -> None:
        """Read state from SQLite and update all columns."""
        if not self.config:
            return

        try:
            tasks = parse_tasks(self.config.tasks_file) if self.config.tasks_file.exists() else []
            state = ExecutorState(self.config)

            # Categorize tasks
            columns: dict[str, list[str]] = {
                "blocked": [],
                "todo": [],
                "running": [],
                "done": [],
            }

            for task in tasks:
                ts = state.tasks.get(task.id)
                status = task.status
                if ts:
                    status = ts.status if ts.status != "pending" else task.status

                if status in ("blocked",):
                    card_text = TaskCard.format_card(
                        task.id, task.name, task.priority, "blocked",
                        blocked_by=task.depends_on[0] if task.depends_on else None,
                    )
                    columns["blocked"].append(card_text)
                elif status in ("todo", "pending"):
                    card_text = TaskCard.format_card(task.id, task.name, task.priority, "todo")
                    columns["todo"].append(card_text)
                elif status == "running":
                    elapsed = None
                    if ts and ts.started_at:
                        try:
                            started = datetime.fromisoformat(ts.started_at)
                            elapsed = (datetime.now() - started).total_seconds()
                        except ValueError:
                            pass
                    card_text = TaskCard.format_card(
                        task.id, task.name, task.priority, "running", elapsed=elapsed,
                    )
                    columns["running"].append(card_text)
                elif status in ("success", "done"):
                    cost = state.task_cost(task.id) if ts else None
                    duration = None
                    if ts and ts.attempts:
                        duration = sum(a.duration_seconds for a in ts.attempts)
                    card_text = TaskCard.format_card(
                        task.id, task.name, task.priority, "done",
                        cost=cost, duration=duration,
                    )
                    columns["done"].append(card_text)
                elif status == "failed":
                    error = ts.last_error[:30] if ts and ts.last_error else None
                    card_text = TaskCard.format_card(
                        task.id, task.name, task.priority, "failed", error=error,
                    )
                    columns["done"].append(card_text)

            # Update column widgets
            for col_id, cards in columns.items():
                col = self.query_one(f"#col-{col_id}", KanbanColumn)
                col.remove_children()
                for card_text in cards:
                    col.mount(TaskCard(card_text))

            # Update stats
            total_cost = state.total_cost()
            total_inp, total_out = state.total_tokens()
            stats_text = StatsBar.format_stats(
                total=len(tasks),
                completed=sum(1 for t in tasks if t.status == "done"),
                failed=state.total_failed,
                input_tokens=total_inp,
                output_tokens=total_out,
                cost=total_cost,
            )
            self.query_one("#stats", StatsBar).update(stats_text)

            state.close()
        except Exception:
            pass  # Silently handle errors during refresh

    def action_stop(self) -> None:
        """Request graceful stop."""
        if self.config:
            stop_file = self.config.stop_file
            stop_file.parent.mkdir(parents=True, exist_ok=True)
            stop_file.write_text(f"Stop requested at {datetime.now().isoformat()}\n")
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tui.py -v`
Expected: PASS (7 tests)

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/spec_runner/tui.py tests/test_tui.py
git commit -m "feat: add Textual TUI Kanban dashboard"
```

---

### Task 9: Wire TUI into CLI — cmd_tui and --tui flag

**Files:**
- Modify: `src/spec_runner/executor.py` (main argparse, cmd_tui, --tui handling in cmd_run)

**Step 1: Add `cmd_tui` function**

Add to executor.py (before `main()`):

```python
def cmd_tui(args, config: ExecutorConfig):
    """Launch read-only TUI dashboard."""
    from .tui import SpecRunnerApp

    app = SpecRunnerApp()
    app.config = config
    app.run()
```

**Step 2: Add --tui flag to run_parser and tui subcommand**

In `main()`, add `--tui` to `run_parser` (after `--max-concurrent`):

```python
    run_parser.add_argument(
        "--tui",
        action="store_true",
        help="Show TUI dashboard during execution",
    )
```

Add `tui` subcommand (after the `plan` parser):

```python
    # tui
    subparsers.add_parser("tui", parents=[common], help="Launch read-only TUI dashboard")
```

Add `"tui": cmd_tui` to the `commands` dict.

**Step 3: Handle --tui in cmd_run**

In `cmd_run()`, when `--tui` and `--all` are both set, launch TUI with execution in a worker:

```python
def cmd_run(args, config: ExecutorConfig):
    """Execute tasks"""
    if getattr(args, "tui", False):
        from .logging import setup_logging
        from .tui import SpecRunnerApp

        # TUI mode: log to file, TUI owns screen
        log_file = config.logs_dir / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        setup_logging(level=config.log_level, tui_mode=True, log_file=log_file)

        app = SpecRunnerApp()
        app.config = config

        # Run execution in background worker
        @app.call_later
        def start_execution():
            import threading

            def run():
                if getattr(args, "parallel", False):
                    import asyncio
                    config.create_git_branch = False
                    asyncio.run(_run_tasks_parallel(args, config))
                else:
                    _run_tasks(args, config)

            t = threading.Thread(target=run, daemon=True)
            t.start()

        app.run()
        return

    if getattr(args, "parallel", False):
        # ... existing parallel path
```

**Step 4: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 5: Commit**

```bash
git add src/spec_runner/executor.py
git commit -m "feat: wire TUI into CLI with cmd_tui and --tui flag"
```

---

### Task 10: Update __init__.py exports and CLAUDE.md

**Files:**
- Modify: `src/spec_runner/__init__.py`
- Modify: `CLAUDE.md`

**Step 1: Update __init__.py**

Add imports:
```python
from .logging import get_logger, setup_logging
```

Add to `__all__`:
```python
    "get_logger",
    "setup_logging",
```

**Step 2: Update CLAUDE.md**

- Add `logging.py` and `tui.py` to the Architecture table
- Add `--tui`, `--log-level`, `--log-json` to CLI section
- Add `structlog`, `textual` to dependencies note
- Update test count

**Step 3: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 4: Commit**

```bash
git add src/spec_runner/__init__.py CLAUDE.md
git commit -m "docs: update exports and CLAUDE.md for Phase 3"
```
