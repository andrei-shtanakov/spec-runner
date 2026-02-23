"""Textual TUI Kanban dashboard for spec-runner.

Provides a live terminal UI showing task status across Kanban columns
(blocked, todo, running, done/failed), with token usage and cost stats.
"""

from __future__ import annotations

import contextlib
from datetime import datetime

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, Static

from .config import ExecutorConfig
from .state import ExecutorState
from .task import parse_tasks, resolve_dependencies

# === Priority badges ===

PRIORITY_BADGE: dict[str, str] = {
    "p0": "[bold red]P0[/]",
    "p1": "[bold yellow]P1[/]",
    "p2": "[cyan]P2[/]",
    "p3": "[dim]P3[/]",
}

STATUS_STYLE: dict[str, str] = {
    "done": "green",
    "running": "yellow",
    "failed": "red",
    "blocked": "dim",
    "todo": "white",
}


def _fmt_duration(seconds: float) -> str:
    """Format seconds as human-readable duration (e.g. '2m 15s')."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours = int(minutes // 60)
    mins = minutes % 60
    return f"{hours}h {mins:02d}m"


def _fmt_tokens(count: int) -> str:
    """Format token count as K (e.g. 45200 -> '45.2K')."""
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}K"


# === Widgets ===


class TaskCard(Static):
    """A card representing a single task in the Kanban board."""

    @staticmethod
    def format_card(
        task_id: str,
        name: str,
        priority: str,
        status: str,
        cost: float | None = None,
        duration: float | None = None,
        elapsed: float | None = None,
        blocked_by: str | None = None,
        error: str | None = None,
    ) -> str:
        """Format task info as a Rich-markup string.

        Args:
            task_id: Task identifier (e.g. 'TASK-001').
            name: Short task name.
            priority: Priority level (p0-p3).
            status: Task status (done, running, blocked, failed, todo).
            cost: Total cost in USD (for done tasks).
            duration: Duration in seconds (for done tasks).
            elapsed: Elapsed time in seconds (for running tasks).
            blocked_by: Blocking task ID (for blocked tasks).
            error: Error message (for failed tasks).

        Returns:
            Rich-markup formatted string.
        """
        badge = PRIORITY_BADGE.get(priority, priority.upper())
        style = STATUS_STYLE.get(status, "white")
        truncated = name[:30] + ".." if len(name) > 32 else name

        lines = [f"[bold {style}]{task_id}[/] {badge}", f"  {truncated}"]

        if status == "done" and cost is not None:
            dur_str = _fmt_duration(duration) if duration else ""
            lines.append(f"  [green]${cost:.2f}[/] {dur_str}")
        elif status == "running" and elapsed is not None:
            lines.append(f"  [yellow]{_fmt_duration(elapsed)}[/]")
        elif status == "blocked" and blocked_by:
            lines.append(f"  [dim]blocked by {blocked_by}[/]")
        elif status == "failed" and error:
            short_err = error[:40] + ".." if len(error) > 42 else error
            lines.append(f"  [red]{short_err}[/]")

        return "\n".join(lines)


class StatsBar(Static):
    """Bottom bar showing aggregate statistics."""

    @staticmethod
    def format_stats(
        total: int,
        completed: int,
        failed: int,
        input_tokens: int,
        output_tokens: int,
        cost: float,
    ) -> str:
        """Format aggregate stats as a Rich-markup string.

        Args:
            total: Total number of tasks.
            completed: Number of completed tasks.
            failed: Number of failed tasks.
            input_tokens: Total input tokens used.
            output_tokens: Total output tokens used.
            cost: Total cost in USD.

        Returns:
            Rich-markup formatted string.
        """
        pct = (completed * 100 // total) if total > 0 else 0
        in_tok = _fmt_tokens(input_tokens)
        out_tok = _fmt_tokens(output_tokens)

        return (
            f"[bold]Tasks:[/] {completed}/{total} ({pct}%) "
            f"[red]Failed: {failed}[/]  |  "
            f"[bold]Tokens:[/] {in_tok} in / {out_tok} out  |  "
            f"[bold]Cost:[/] [green]${cost:.2f}[/]"
        )


class KanbanColumn(Vertical):
    """A single column in the Kanban board."""

    def __init__(self, title: str, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.border_title = title


class SpecRunnerApp(App[None]):
    """Textual TUI app showing a live Kanban dashboard for spec-runner."""

    TITLE = "spec-runner"

    CSS = """
    Screen {
        layout: vertical;
    }

    #board {
        height: 1fr;
    }

    KanbanColumn {
        width: 1fr;
        border: solid $secondary;
        padding: 1;
        overflow-y: auto;
    }

    #col-blocked {
        border: solid $error-darken-2;
    }

    #col-todo {
        border: solid $primary-darken-1;
    }

    #col-running {
        border: solid $warning;
    }

    #col-done {
        border: solid $success;
    }

    TaskCard {
        margin-bottom: 1;
        padding: 0 1;
    }

    #stats-bar {
        dock: bottom;
        height: 1;
        background: $surface;
        padding: 0 2;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("s", "stop", "Stop execution"),
    ]

    def __init__(self, config: ExecutorConfig | None = None) -> None:
        super().__init__()
        self._config = config

    def compose(self) -> ComposeResult:
        """Build the widget tree."""
        yield Header()
        with Horizontal(id="board"):
            yield KanbanColumn("Blocked", id="col-blocked")
            yield KanbanColumn("Todo", id="col-todo")
            yield KanbanColumn("Running", id="col-running")
            yield KanbanColumn("Done", id="col-done")
        yield StatsBar(id="stats-bar")
        yield Footer()

    def on_mount(self) -> None:
        """Start periodic refresh after mount."""
        self.refresh_board()
        self.set_interval(2.0, self.refresh_board)

    def refresh_board(self) -> None:
        """Re-read state from SQLite + tasks file and update all columns."""
        if self._config is None:
            return

        with contextlib.suppress(Exception):
            # State DB may be locked by executor — silently skip this tick
            self._do_refresh()

    def _do_refresh(self) -> None:
        """Internal refresh logic (may raise on DB lock)."""
        config = self._config
        assert config is not None

        tasks_file = config.tasks_file
        if not tasks_file.exists():
            return

        tasks = parse_tasks(tasks_file)
        tasks = resolve_dependencies(tasks)

        state: ExecutorState | None = None
        with contextlib.suppress(Exception):
            state = ExecutorState(config)

        # Categorise tasks
        columns: dict[str, list[str]] = {
            "blocked": [],
            "todo": [],
            "running": [],
            "done": [],
        }

        for task in tasks:
            ts = state.tasks.get(task.id) if state else None

            # Determine effective status
            if ts and ts.status == "running":
                elapsed = self._calc_elapsed(ts.started_at)
                card = TaskCard.format_card(
                    task_id=task.id,
                    name=task.name,
                    priority=task.priority,
                    status="running",
                    elapsed=elapsed,
                )
                columns["running"].append(card)
            elif ts and ts.status == "failed":
                card = TaskCard.format_card(
                    task_id=task.id,
                    name=task.name,
                    priority=task.priority,
                    status="failed",
                    error=ts.last_error,
                )
                columns["done"].append(card)
            elif ts and ts.status == "success":
                cost = state.task_cost(task.id) if state else 0.0
                duration = self._calc_duration(ts)
                card = TaskCard.format_card(
                    task_id=task.id,
                    name=task.name,
                    priority=task.priority,
                    status="done",
                    cost=cost,
                    duration=duration,
                )
                columns["done"].append(card)
            elif task.status == "blocked":
                blocked_by = task.depends_on[0] if task.depends_on else None
                card = TaskCard.format_card(
                    task_id=task.id,
                    name=task.name,
                    priority=task.priority,
                    status="blocked",
                    blocked_by=blocked_by,
                )
                columns["blocked"].append(card)
            else:
                card = TaskCard.format_card(
                    task_id=task.id,
                    name=task.name,
                    priority=task.priority,
                    status="todo",
                )
                columns["todo"].append(card)

        # Update column widgets
        col_map = {
            "blocked": self.query_one("#col-blocked", KanbanColumn),
            "todo": self.query_one("#col-todo", KanbanColumn),
            "running": self.query_one("#col-running", KanbanColumn),
            "done": self.query_one("#col-done", KanbanColumn),
        }

        for key, col in col_map.items():
            col.remove_children()
            for card_text in columns[key]:
                col.mount(TaskCard(card_text))

        # Update stats bar
        total = len(tasks)
        completed = sum(
            1
            for t in tasks
            if state and state.tasks.get(t.id) and state.tasks[t.id].status == "success"
        )
        failed = sum(
            1
            for t in tasks
            if state and state.tasks.get(t.id) and state.tasks[t.id].status == "failed"
        )
        input_tokens, output_tokens = state.total_tokens() if state else (0, 0)
        cost = state.total_cost() if state else 0.0

        stats_bar = self.query_one("#stats-bar", StatsBar)
        stats_bar.update(
            StatsBar.format_stats(
                total=total,
                completed=completed,
                failed=failed,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost=cost,
            )
        )

        if state:
            state.close()

    @staticmethod
    def _calc_elapsed(started_at: str | None) -> float:
        """Calculate elapsed seconds from ISO timestamp to now."""
        if not started_at:
            return 0.0
        try:
            start = datetime.fromisoformat(started_at)
            return (datetime.now() - start).total_seconds()
        except (ValueError, TypeError):
            return 0.0

    @staticmethod
    def _calc_duration(ts: object) -> float:
        """Calculate total duration from task state attempts."""
        from .state import TaskState

        if not isinstance(ts, TaskState):
            return 0.0
        return sum(a.duration_seconds for a in ts.attempts)

    def action_stop(self) -> None:
        """Create the stop file to request graceful shutdown."""
        if self._config is None:
            return
        stop_file = self._config.stop_file
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text("stop requested from TUI")

    def action_quit(self) -> None:
        """Quit the TUI."""
        self.exit()
