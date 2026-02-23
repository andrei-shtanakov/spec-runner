"""Tests for spec_runner.tui module."""

from pathlib import Path

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
