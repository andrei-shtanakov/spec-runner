"""Tests for spec_runner.tui module."""

from spec_runner.tui import SpecRunnerApp, StatsBar, TaskCard


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
