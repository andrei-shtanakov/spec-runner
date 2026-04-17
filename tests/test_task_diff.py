"""Unit tests for snapshot / diff helpers used by the pause/resume flow (LABS-38)."""

from __future__ import annotations

from spec_runner.task import (
    Task,
    diff_task_statuses,
    format_task_status_diff,
    snapshot_task_statuses,
)


def _t(task_id: str, status: str = "todo", depends_on: list[str] | None = None) -> Task:
    return Task(
        id=task_id,
        name=task_id,
        priority="p1",
        status=status,
        estimate="1d",
        depends_on=list(depends_on or []),
    )


class TestSnapshot:
    def test_empty_list(self) -> None:
        assert snapshot_task_statuses([]) == {}

    def test_preserves_status_per_id(self) -> None:
        tasks = [_t("TASK-001", "done"), _t("TASK-002", "in_progress")]
        snapshot = snapshot_task_statuses(tasks)
        assert snapshot == {"TASK-001": "done", "TASK-002": "in_progress"}


class TestDiff:
    def test_no_changes(self) -> None:
        before = {"TASK-001": "done"}
        after = [_t("TASK-001", "done")]
        diff = diff_task_statuses(before, after)
        assert diff.is_empty

    def test_parent_completion_is_classified_as_completed_parent(self) -> None:
        before = {"TASK-001": "in_progress", "TASK-002": "blocked"}
        after = [
            _t("TASK-001", "done"),
            _t("TASK-002", "blocked", depends_on=["TASK-001"]),
        ]
        diff = diff_task_statuses(before, after)
        assert diff.completed_parents == ["TASK-001"]
        # TASK-002 deps are all done → newly ready
        assert diff.newly_ready == ["TASK-002"]

    def test_parent_completion_without_dependents_is_plain_transition(self) -> None:
        before = {"TASK-001": "in_progress"}
        after = [_t("TASK-001", "done")]  # no one depends on it
        diff = diff_task_statuses(before, after)
        assert diff.completed_parents == []
        assert diff.other_transitions == [("TASK-001", "in_progress", "done")]

    def test_added_task_is_tracked(self) -> None:
        before = {"TASK-001": "done"}
        after = [_t("TASK-001", "done"), _t("TASK-002", "todo")]
        diff = diff_task_statuses(before, after)
        assert diff.added == ["TASK-002"]

    def test_removed_task_is_tracked(self) -> None:
        before = {"TASK-001": "done", "TASK-002": "todo"}
        after = [_t("TASK-001", "done")]
        diff = diff_task_statuses(before, after)
        assert diff.removed == ["TASK-002"]

    def test_non_terminal_transition_recorded_in_other(self) -> None:
        before = {"TASK-001": "todo"}
        after = [_t("TASK-001", "in_progress")]
        diff = diff_task_statuses(before, after)
        assert diff.other_transitions == [("TASK-001", "todo", "in_progress")]

    def test_multiple_categories_sorted(self) -> None:
        before = {
            "TASK-001": "in_progress",
            "TASK-002": "blocked",
            "TASK-003": "in_progress",
        }
        after = [
            _t("TASK-003", "done"),  # parent finished
            _t("TASK-001", "done"),  # another parent finished
            _t("TASK-002", "blocked", depends_on=["TASK-001", "TASK-003"]),
            _t("TASK-004", "todo"),  # added during pause
        ]
        diff = diff_task_statuses(before, after)
        assert diff.completed_parents == ["TASK-001", "TASK-003"]
        assert diff.newly_ready == ["TASK-002"]
        assert diff.added == ["TASK-004"]


class TestFormat:
    def test_empty_diff(self) -> None:
        from spec_runner.task import TaskStatusDiff

        assert "no task changes" in format_task_status_diff(TaskStatusDiff())

    def test_completed_and_unblocked_rendered(self) -> None:
        before = {"TASK-001": "in_progress", "TASK-002": "blocked"}
        after = [
            _t("TASK-001", "done"),
            _t("TASK-002", "blocked", depends_on=["TASK-001"]),
        ]
        out = format_task_status_diff(diff_task_statuses(before, after))
        assert "completed: TASK-001" in out
        assert "unblocked: TASK-002" in out
