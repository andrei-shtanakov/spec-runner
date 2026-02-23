"""Tests for GitHub Issues sync commands."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runner.task import Task, parse_tasks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_TASKS_MD = """\
# Tasks

## Milestone: MVP

### TASK-001: Set up project
🔴 P0 | ✅ DONE
**Traces to:** [REQ-001]
Est: 1d

- [x] Init repo
- [x] Add CI

### TASK-002: Add authentication
🟠 P1 | 🔄 IN_PROGRESS
**Traces to:** [REQ-002]
**Depends on:** TASK-001
Est: 3d

- [x] Design auth flow
- [ ] Implement login

### TASK-003: Write docs
🟡 P2 | ⬜ TODO
**Depends on:** TASK-002
Est: 2d
"""


def _write_tasks(tmp_path: Path) -> Path:
    tasks_file = tmp_path / "spec" / "tasks.md"
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text(SAMPLE_TASKS_MD)
    # History file needed by update_task_status
    history = tmp_path / "spec" / ".task-history.log"
    history.touch()
    return tasks_file


def _make_args(**kwargs):
    args = MagicMock()
    args.spec_prefix = kwargs.get("spec_prefix", "")
    args.dry_run = kwargs.get("dry_run", False)
    return args


# ---------------------------------------------------------------------------
# sync-to-gh
# ---------------------------------------------------------------------------

class TestSyncToGh:
    """Tests for cmd_sync_to_gh."""

    @patch("spec_runner.task.subprocess.run")
    def test_creates_issues_for_open_tasks(self, mock_run, tmp_path):
        """Should create issues for todo/in_progress tasks, skip done."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        # gh issue list returns empty (no existing issues)
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="[]",
        )

        from spec_runner.task import cmd_sync_to_gh
        cmd_sync_to_gh(_make_args(), tasks)

        # Should call gh issue list first, then create for TASK-002 and TASK-003
        calls = mock_run.call_args_list
        assert any("issue" in str(c) and "list" in str(c) for c in calls)
        create_calls = [c for c in calls if "create" in str(c)]
        assert len(create_calls) == 2

    @patch("spec_runner.task.subprocess.run")
    def test_updates_existing_issues(self, mock_run, tmp_path):
        """Should update labels on existing issues instead of creating duplicates."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        # gh issue list returns existing issue for TASK-002
        existing = json.dumps([
            {"number": 5, "title": "[TASK-002] Add authentication", "state": "OPEN",
             "labels": [{"name": "priority:p1"}]},
        ])
        mock_run.return_value = MagicMock(returncode=0, stdout=existing)

        from spec_runner.task import cmd_sync_to_gh
        cmd_sync_to_gh(_make_args(), tasks)

        calls = mock_run.call_args_list
        # Should edit #5, create TASK-003, skip TASK-001 (done)
        edit_calls = [c for c in calls if "edit" in str(c)]
        create_calls = [c for c in calls if "create" in str(c)]
        assert len(edit_calls) >= 1
        assert len(create_calls) == 1

    @patch("spec_runner.task.subprocess.run")
    def test_closes_done_issues(self, mock_run, tmp_path):
        """Should close issues for done tasks."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        # Issue exists for TASK-001 which is done
        existing = json.dumps([
            {"number": 1, "title": "[TASK-001] Set up project", "state": "OPEN",
             "labels": [{"name": "priority:p0"}]},
        ])
        mock_run.return_value = MagicMock(returncode=0, stdout=existing)

        from spec_runner.task import cmd_sync_to_gh
        cmd_sync_to_gh(_make_args(), tasks)

        calls = mock_run.call_args_list
        close_calls = [c for c in calls if "close" in str(c)]
        assert len(close_calls) >= 1

    @patch("spec_runner.task.subprocess.run")
    def test_dry_run_no_mutations(self, mock_run, tmp_path):
        """Dry run should only list issues, never create/edit/close."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        mock_run.return_value = MagicMock(returncode=0, stdout="[]")

        from spec_runner.task import cmd_sync_to_gh
        cmd_sync_to_gh(_make_args(dry_run=True), tasks)

        calls = mock_run.call_args_list
        # Only the initial list call
        mutation_calls = [
            c for c in calls
            if any(word in str(c) for word in ["create", "edit", "close"])
        ]
        assert len(mutation_calls) == 0

    @patch("spec_runner.task.subprocess.run")
    def test_gh_not_found(self, mock_run, tmp_path, capsys):
        """Should print error when gh CLI is not available."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        mock_run.side_effect = FileNotFoundError("gh not found")

        from spec_runner.task import cmd_sync_to_gh
        cmd_sync_to_gh(_make_args(), tasks)

        captured = capsys.readouterr()
        assert "gh" in captured.out.lower() or "gh" in captured.err.lower()


# ---------------------------------------------------------------------------
# sync-from-gh
# ---------------------------------------------------------------------------


class TestSyncFromGh:
    """Tests for cmd_sync_from_gh."""

    @patch("spec_runner.task.subprocess.run")
    def test_updates_status_from_closed_issues(self, mock_run, tmp_path):
        """Closed issues should mark tasks as done."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        issues = json.dumps([
            {"number": 1, "title": "[TASK-003] Write docs", "state": "CLOSED",
             "labels": [{"name": "priority:p2"}, {"name": "status:done"}]},
        ])
        mock_run.return_value = MagicMock(returncode=0, stdout=issues)

        from spec_runner.task import cmd_sync_from_gh
        cmd_sync_from_gh(_make_args(), tasks, tasks_file)

        updated_tasks = parse_tasks(tasks_file)
        task_003 = next(t for t in updated_tasks if t.id == "TASK-003")
        assert task_003.status == "done"

    @patch("spec_runner.task.subprocess.run")
    def test_updates_status_from_labels(self, mock_run, tmp_path):
        """Should use status:X labels to determine status for open issues."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        issues = json.dumps([
            {"number": 2, "title": "[TASK-003] Write docs", "state": "OPEN",
             "labels": [{"name": "status:in_progress"}, {"name": "priority:p2"}]},
        ])
        mock_run.return_value = MagicMock(returncode=0, stdout=issues)

        from spec_runner.task import cmd_sync_from_gh
        cmd_sync_from_gh(_make_args(), tasks, tasks_file)

        updated_tasks = parse_tasks(tasks_file)
        task_003 = next(t for t in updated_tasks if t.id == "TASK-003")
        assert task_003.status == "in_progress"

    @patch("spec_runner.task.subprocess.run")
    def test_no_change_when_status_matches(self, mock_run, tmp_path):
        """Should not write file when nothing changed."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)
        original_content = tasks_file.read_text()

        issues = json.dumps([
            {"number": 2, "title": "[TASK-002] Add authentication", "state": "OPEN",
             "labels": [{"name": "status:in_progress"}]},
        ])
        mock_run.return_value = MagicMock(returncode=0, stdout=issues)

        from spec_runner.task import cmd_sync_from_gh
        cmd_sync_from_gh(_make_args(), tasks, tasks_file)

        assert tasks_file.read_text() == original_content

    @patch("spec_runner.task.subprocess.run")
    def test_gh_not_found(self, mock_run, tmp_path, capsys):
        """Should print error when gh CLI is not available."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        mock_run.side_effect = FileNotFoundError("gh not found")

        from spec_runner.task import cmd_sync_from_gh
        cmd_sync_from_gh(_make_args(), tasks, tasks_file)

        captured = capsys.readouterr()
        assert "gh" in captured.out.lower() or "gh" in captured.err.lower()
