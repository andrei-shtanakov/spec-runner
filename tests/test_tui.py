"""Tests for spec_runner.tui module."""

from pathlib import Path
from unittest.mock import MagicMock

from spec_runner.config import ExecutorConfig
from spec_runner.tui import LogPanel, SpecRunnerApp, StatsBar, TaskCard


class TestSpecRunnerApp:
    """Tests for the TUI app."""

    def test_app_exists(self):
        """SpecRunnerApp class exists and is a Textual App."""
        from textual.app import App

        assert issubclass(SpecRunnerApp, App)

    def test_app_has_title(self):
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


class TestLogPanel:
    """Tests for the LogPanel widget."""

    def test_format_line_strips_whitespace(self) -> None:
        """format_line strips trailing whitespace."""
        assert LogPanel.format_line("hello world  \n") == "hello world"

    def test_format_line_preserves_content(self) -> None:
        """format_line preserves message content."""
        line = "[14:23:01] TASK-003 Attempt 1/3 started"
        result = LogPanel.format_line(line)
        assert "TASK-003" in result
        assert "Attempt 1/3" in result

    def test_read_new_lines_empty_file(self, tmp_path: Path) -> None:
        """Empty file returns no lines."""
        progress = tmp_path / "progress.txt"
        progress.write_text("")
        panel = LogPanel()
        lines = panel.read_new_lines(progress)
        assert lines == []

    def test_read_new_lines_reads_content(self, tmp_path: Path) -> None:
        """Reads lines from progress file."""
        progress = tmp_path / "progress.txt"
        progress.write_text("[14:23] Line 1\n[14:24] Line 2\n")
        panel = LogPanel()
        lines = panel.read_new_lines(progress)
        assert len(lines) == 2
        assert "Line 1" in lines[0]
        assert "Line 2" in lines[1]

    def test_read_new_lines_incremental(self, tmp_path: Path) -> None:
        """Only reads new lines on subsequent calls."""
        progress = tmp_path / "progress.txt"
        progress.write_text("[14:23] Line 1\n")
        panel = LogPanel()

        lines1 = panel.read_new_lines(progress)
        assert len(lines1) == 1

        # Append more content
        with open(progress, "a") as f:
            f.write("[14:24] Line 2\n[14:25] Line 3\n")

        lines2 = panel.read_new_lines(progress)
        assert len(lines2) == 2
        assert "Line 2" in lines2[0]

    def test_read_new_lines_nonexistent_file(self, tmp_path: Path) -> None:
        """Nonexistent file returns empty list."""
        panel = LogPanel()
        lines = panel.read_new_lines(tmp_path / "nonexistent.txt")
        assert lines == []

    def test_read_new_lines_caps_at_100(self, tmp_path: Path) -> None:
        """Internal buffer caps at 100 lines."""
        progress = tmp_path / "progress.txt"
        content = "\n".join(f"[14:00] Line {i}" for i in range(150)) + "\n"
        progress.write_text(content)
        panel = LogPanel()
        panel.read_new_lines(progress)
        assert len(panel._lines) == 100

    def test_add_line_appends(self) -> None:
        """add_line appends a formatted line to the buffer."""
        panel = LogPanel()
        panel.add_line("Hello world  ")
        assert len(panel._lines) == 1
        assert panel._lines[0] == "Hello world"

    def test_add_line_caps_at_100(self) -> None:
        """add_line caps internal buffer at 100 lines."""
        panel = LogPanel()
        for i in range(105):
            panel.add_line(f"Line {i}")
        assert len(panel._lines) == 100
        assert "Line 104" in panel._lines[-1]
        assert "Line 4" not in panel._lines[0]  # first 5 trimmed

    def test_add_line_updates_render(self) -> None:
        """add_line includes new line in render output."""
        panel = LogPanel()
        panel.add_line("Test message")
        text = panel.render_log()
        assert "Test message" in text

    def test_render_log_empty(self) -> None:
        """Render with no lines shows placeholder."""
        panel = LogPanel()
        text = panel.render_log()
        assert "No log entries" in text

    def test_render_log_shows_lines(self, tmp_path: Path) -> None:
        """Render shows the last lines."""
        progress = tmp_path / "progress.txt"
        progress.write_text("[14:23] Line 1\n[14:24] Line 2\n")
        panel = LogPanel()
        panel.read_new_lines(progress)
        text = panel.render_log()
        assert "Line 1" in text
        assert "Line 2" in text


# --- LABS-38: pause/resume dependency diff ---------------------------


TASKS_BEFORE = """\
### TASK-001: Parent
🔴 P0 | 🔄 IN_PROGRESS

### TASK-002: Dependent
🟠 P1 | ⏸️ BLOCKED
**Depends on:** [TASK-001]
"""

TASKS_AFTER_PARENT_DONE = """\
### TASK-001: Parent
🔴 P0 | ✅ DONE

### TASK-002: Dependent
🟠 P1 | ⏸️ BLOCKED
**Depends on:** [TASK-001]
"""


def _make_config(tmp_path: Path, tasks_md: str) -> ExecutorConfig:
    spec = tmp_path / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    (spec / "tasks.md").write_text(tasks_md)
    return ExecutorConfig(project_root=tmp_path)


class TestTuiPauseResumeDiff:
    """Pause/resume must surface parent completion + newly-ready children."""

    def _build_app(self, config: ExecutorConfig) -> tuple[SpecRunnerApp, LogPanel]:
        app = SpecRunnerApp(config=config)
        # SpecRunnerApp.query_one would normally require a running app. Swap
        # it for a stub that returns a LogPanel we can inspect, and swap
        # refresh_board so the action does not try to hit the real widgets.
        log_panel = LogPanel()
        app.query_one = MagicMock(return_value=log_panel)  # type: ignore[method-assign]
        app.refresh_board = MagicMock()  # type: ignore[method-assign]
        return app, log_panel

    @staticmethod
    def _reset_pause_flag(monkeypatch) -> None:
        import spec_runner.executor as real_executor

        monkeypatch.setattr(real_executor, "_pause_requested", False)

    def test_pause_snapshot_is_captured_and_cleared(self, tmp_path, monkeypatch):
        self._reset_pause_flag(monkeypatch)
        config = _make_config(tmp_path, TASKS_BEFORE)
        app, _ = self._build_app(config)

        import spec_runner.executor as real_executor

        # First press → pause, snapshot must be taken
        app.action_pause()
        assert real_executor._pause_requested is True
        assert app._pause_snapshot == {"TASK-001": "in_progress", "TASK-002": "blocked"}

        # Second press → resume, snapshot cleared, refresh_board called
        app.action_pause()
        assert real_executor._pause_requested is False
        assert app._pause_snapshot is None
        assert app.refresh_board.called

    def test_resume_reports_completed_parent(self, tmp_path, monkeypatch):
        self._reset_pause_flag(monkeypatch)
        config = _make_config(tmp_path, TASKS_BEFORE)
        app, log_panel = self._build_app(config)

        app.action_pause()  # pause

        # Simulate another session completing TASK-001
        (tmp_path / "spec" / "tasks.md").write_text(TASKS_AFTER_PARENT_DONE)

        app.action_pause()  # resume

        combined = "\n".join(log_panel._lines)
        assert "While paused" in combined
        assert "TASK-001" in combined  # completed parent
        assert "TASK-002" in combined  # newly-ready child

    def test_resume_with_no_changes_logs_empty_diff(self, tmp_path, monkeypatch):
        self._reset_pause_flag(monkeypatch)
        config = _make_config(tmp_path, TASKS_BEFORE)
        app, log_panel = self._build_app(config)

        app.action_pause()  # pause
        app.action_pause()  # resume (tasks.md untouched)

        combined = "\n".join(log_panel._lines)
        assert "no task changes while paused" in combined
