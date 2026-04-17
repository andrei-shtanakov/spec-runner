"""Tests for GitHub Issues sync commands."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from spec_runner.task import parse_tasks

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

    @patch("spec_runner.github_sync.subprocess.run")
    def test_creates_issues_for_open_tasks(self, mock_run, tmp_path):
        """Should create issues for todo/in_progress tasks, skip done."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        # gh issue list returns empty (no existing issues)
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="[]",
        )

        from spec_runner.github_sync import cmd_sync_to_gh

        cmd_sync_to_gh(_make_args(), tasks)

        # Should call gh issue list first, then create for TASK-002 and TASK-003
        calls = mock_run.call_args_list
        assert any("issue" in str(c) and "list" in str(c) for c in calls)
        create_calls = [c for c in calls if "create" in str(c)]
        assert len(create_calls) == 2

    @patch("spec_runner.github_sync.subprocess.run")
    def test_updates_existing_issues(self, mock_run, tmp_path):
        """Should update labels on existing issues instead of creating duplicates."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        # gh issue list returns existing issue for TASK-002
        existing = json.dumps(
            [
                {
                    "number": 5,
                    "title": "[TASK-002] Add authentication",
                    "state": "OPEN",
                    "labels": [{"name": "priority:p1"}],
                },
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=existing)

        from spec_runner.github_sync import cmd_sync_to_gh

        cmd_sync_to_gh(_make_args(), tasks)

        calls = mock_run.call_args_list
        # Should edit #5, create TASK-003, skip TASK-001 (done)
        edit_calls = [c for c in calls if "edit" in str(c)]
        create_calls = [c for c in calls if "create" in str(c)]
        assert len(edit_calls) >= 1
        assert len(create_calls) == 1

    @patch("spec_runner.github_sync.subprocess.run")
    def test_closes_done_issues(self, mock_run, tmp_path):
        """Should close issues for done tasks."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        # Issue exists for TASK-001 which is done
        existing = json.dumps(
            [
                {
                    "number": 1,
                    "title": "[TASK-001] Set up project",
                    "state": "OPEN",
                    "labels": [{"name": "priority:p0"}],
                },
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=existing)

        from spec_runner.github_sync import cmd_sync_to_gh

        cmd_sync_to_gh(_make_args(), tasks)

        calls = mock_run.call_args_list
        close_calls = [c for c in calls if "close" in str(c)]
        assert len(close_calls) >= 1

    @patch("spec_runner.github_sync.subprocess.run")
    def test_dry_run_no_mutations(self, mock_run, tmp_path):
        """Dry run should only list issues, never create/edit/close."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        mock_run.return_value = MagicMock(returncode=0, stdout="[]")

        from spec_runner.github_sync import cmd_sync_to_gh

        cmd_sync_to_gh(_make_args(dry_run=True), tasks)

        calls = mock_run.call_args_list
        # Only the initial list call
        mutation_calls = [
            c for c in calls if any(word in str(c) for word in ["create", "edit", "close"])
        ]
        assert len(mutation_calls) == 0

    @patch("spec_runner.github_sync.subprocess.run")
    def test_gh_not_found(self, mock_run, tmp_path, capsys):
        """Should print error when gh CLI is not available."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        mock_run.side_effect = FileNotFoundError("gh not found")

        from spec_runner.github_sync import cmd_sync_to_gh

        cmd_sync_to_gh(_make_args(), tasks)

        captured = capsys.readouterr()
        assert "gh" in captured.out.lower() or "gh" in captured.err.lower()


# ---------------------------------------------------------------------------
# sync-from-gh
# ---------------------------------------------------------------------------


class TestSyncFromGh:
    """Tests for cmd_sync_from_gh."""

    @patch("spec_runner.github_sync.subprocess.run")
    def test_updates_status_from_closed_issues(self, mock_run, tmp_path):
        """Closed issues should mark tasks as done."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        issues = json.dumps(
            [
                {
                    "number": 1,
                    "title": "[TASK-003] Write docs",
                    "state": "CLOSED",
                    "labels": [{"name": "priority:p2"}, {"name": "status:done"}],
                },
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=issues)

        from spec_runner.github_sync import cmd_sync_from_gh

        cmd_sync_from_gh(_make_args(), tasks, tasks_file)

        updated_tasks = parse_tasks(tasks_file)
        task_003 = next(t for t in updated_tasks if t.id == "TASK-003")
        assert task_003.status == "done"

    @patch("spec_runner.github_sync.subprocess.run")
    def test_updates_status_from_labels(self, mock_run, tmp_path):
        """Should use status:X labels to determine status for open issues."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        issues = json.dumps(
            [
                {
                    "number": 2,
                    "title": "[TASK-003] Write docs",
                    "state": "OPEN",
                    "labels": [{"name": "status:in_progress"}, {"name": "priority:p2"}],
                },
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=issues)

        from spec_runner.github_sync import cmd_sync_from_gh

        cmd_sync_from_gh(_make_args(), tasks, tasks_file)

        updated_tasks = parse_tasks(tasks_file)
        task_003 = next(t for t in updated_tasks if t.id == "TASK-003")
        assert task_003.status == "in_progress"

    @patch("spec_runner.github_sync.subprocess.run")
    def test_no_change_when_status_matches(self, mock_run, tmp_path):
        """Should not write file when nothing changed."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)
        original_content = tasks_file.read_text()

        issues = json.dumps(
            [
                {
                    "number": 2,
                    "title": "[TASK-002] Add authentication",
                    "state": "OPEN",
                    "labels": [{"name": "status:in_progress"}],
                },
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=issues)

        from spec_runner.github_sync import cmd_sync_from_gh

        cmd_sync_from_gh(_make_args(), tasks, tasks_file)

        assert tasks_file.read_text() == original_content

    @patch("spec_runner.github_sync.subprocess.run")
    def test_gh_not_found(self, mock_run, tmp_path, capsys):
        """Should print error when gh CLI is not available."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        mock_run.side_effect = FileNotFoundError("gh not found")

        from spec_runner.github_sync import cmd_sync_from_gh

        cmd_sync_from_gh(_make_args(), tasks, tasks_file)

        captured = capsys.readouterr()
        assert "gh" in captured.out.lower() or "gh" in captured.err.lower()


# ---------------------------------------------------------------------------
# Conflict handling and idempotency (LABS-35)
#
# These tests pin the *currently-shipping* resolution strategy:
#   - `sync_to_gh` treats local tasks.md as the source of truth (push).
#   - `sync_from_gh` treats GitHub Issues as the source of truth (pull).
# There is no three-way merge. The two directions are explicit; the caller
# picks which side wins. These tests lock that behavior so a silent drift
# (e.g. picking "newer wins" based on a timestamp) becomes a red test, not a
# broken production run.
# ---------------------------------------------------------------------------


class TestSyncToGhConflicts:
    @patch("spec_runner.github_sync.subprocess.run")
    def test_reopens_closed_issue_for_non_done_task(self, mock_run, tmp_path):
        """Conflict: remote issue is CLOSED, local task is in_progress → reopen."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        existing = json.dumps(
            [
                {
                    "number": 5,
                    "title": "[TASK-002] Add authentication",
                    "state": "CLOSED",
                    "labels": [{"name": "priority:p1"}, {"name": "status:done"}],
                }
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=existing)

        from spec_runner.github_sync import cmd_sync_to_gh

        cmd_sync_to_gh(_make_args(), tasks)

        reopens = [c for c in mock_run.call_args_list if "reopen" in str(c)]
        assert len(reopens) == 1, "Closed issue for non-done task must be reopened"

    @patch("spec_runner.github_sync.subprocess.run")
    def test_no_reopen_when_task_is_done(self, mock_run, tmp_path):
        """Already-closed issue for done task stays closed (no churn)."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        existing = json.dumps(
            [
                {
                    "number": 1,
                    "title": "[TASK-001] Set up project",
                    "state": "CLOSED",
                    "labels": [{"name": "priority:p0"}, {"name": "status:done"}],
                }
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=existing)

        from spec_runner.github_sync import cmd_sync_to_gh

        cmd_sync_to_gh(_make_args(), tasks)

        # No reopen, no close (issue already closed and task already done)
        reopens = [c for c in mock_run.call_args_list if "reopen" in str(c)]
        closes = [c for c in mock_run.call_args_list if "close" in str(c)]
        assert reopens == []
        assert closes == []

    @patch("spec_runner.github_sync.subprocess.run")
    def test_does_not_reclose_already_closed_done_task(self, mock_run, tmp_path):
        """Idempotency: done task with already-closed issue → no duplicate close."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        existing = json.dumps(
            [
                {
                    "number": 1,
                    "title": "[TASK-001] Set up project",
                    "state": "CLOSED",
                    "labels": [{"name": "priority:p0"}],
                }
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=existing)

        from spec_runner.github_sync import cmd_sync_to_gh

        cmd_sync_to_gh(_make_args(), tasks)

        closes = [c for c in mock_run.call_args_list if "close" in str(c)]
        assert closes == []

    @patch("spec_runner.github_sync.subprocess.run")
    def test_second_run_is_idempotent_for_unchanged_tasks(self, mock_run, tmp_path):
        """Running sync_to_gh twice with unchanged tasks must not create duplicates.

        The first run creates; the second sees the existing issues and emits
        only edits, never a second create for the same TASK-id.
        """
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        # Simulate the state after an initial successful push: all non-done
        # tasks now have issues on GitHub.
        existing = json.dumps(
            [
                {
                    "number": 5,
                    "title": "[TASK-002] Add authentication",
                    "state": "OPEN",
                    "labels": [{"name": "priority:p1"}, {"name": "status:in_progress"}],
                },
                {
                    "number": 6,
                    "title": "[TASK-003] Write docs",
                    "state": "OPEN",
                    "labels": [{"name": "priority:p2"}, {"name": "status:todo"}],
                },
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=existing)

        from spec_runner.github_sync import cmd_sync_to_gh

        cmd_sync_to_gh(_make_args(), tasks)

        creates = [c for c in mock_run.call_args_list if "create" in str(c)]
        assert creates == [], "Idempotent re-run must not call `gh issue create`"

    @patch("spec_runner.github_sync.subprocess.run")
    def test_push_overwrites_remote_status_label(self, mock_run, tmp_path):
        """Divergent status: local says in_progress, remote label says done.

        sync_to_gh's contract is "local wins" — it adds the local-derived
        label set via `gh issue edit --add-label`. (Stale labels are not
        removed; that's the known limitation this test pins.)
        """
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        existing = json.dumps(
            [
                {
                    "number": 5,
                    "title": "[TASK-002] Add authentication",
                    "state": "OPEN",
                    "labels": [{"name": "priority:p1"}, {"name": "status:done"}],
                }
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=existing)

        from spec_runner.github_sync import cmd_sync_to_gh

        cmd_sync_to_gh(_make_args(), tasks)

        edit_calls = [c for c in mock_run.call_args_list if "edit" in str(c)]
        assert len(edit_calls) == 1
        args = edit_calls[0].args[0]
        assert "--add-label" in args
        label_idx = args.index("--add-label")
        label_arg = args[label_idx + 1]
        assert "status:in_progress" in label_arg
        assert "priority:p1" in label_arg

    @patch("spec_runner.github_sync.subprocess.run")
    def test_ignores_issues_without_task_id_prefix(self, mock_run, tmp_path):
        """Issues whose titles don't start with [TASK-XXX] are irrelevant."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        existing = json.dumps(
            [
                {
                    "number": 42,
                    "title": "Random bug report unrelated to specs",
                    "state": "OPEN",
                    "labels": [],
                },
                {
                    "number": 43,
                    "title": "feat: rename utility",
                    "state": "CLOSED",
                    "labels": [],
                },
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=existing)

        from spec_runner.github_sync import cmd_sync_to_gh

        cmd_sync_to_gh(_make_args(), tasks)

        # All non-done tasks should be freshly created (the unrelated issues
        # must not shadow the lookup).
        creates = [c for c in mock_run.call_args_list if "create" in str(c)]
        assert len(creates) == 2  # TASK-002 and TASK-003


class TestSyncFromGhConflicts:
    @patch("spec_runner.github_sync.subprocess.run")
    def test_pull_overwrites_local_when_remote_closed(self, mock_run, tmp_path):
        """Divergent: local task is in_progress, remote issue is CLOSED → local→done."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        issues = json.dumps(
            [
                {
                    "number": 5,
                    "title": "[TASK-002] Add authentication",
                    "state": "CLOSED",
                    "labels": [{"name": "priority:p1"}],
                }
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=issues)

        from spec_runner.github_sync import cmd_sync_from_gh

        cmd_sync_from_gh(_make_args(), tasks, tasks_file)

        updated = {t.id: t for t in parse_tasks(tasks_file)}
        assert updated["TASK-002"].status == "done"

    @patch("spec_runner.github_sync.subprocess.run")
    def test_pull_is_idempotent_when_states_match(self, mock_run, tmp_path):
        """Running sync_from_gh twice with unchanged remote state is a no-op."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        issues = json.dumps(
            [
                {
                    "number": 5,
                    "title": "[TASK-002] Add authentication",
                    "state": "OPEN",
                    "labels": [{"name": "status:in_progress"}, {"name": "priority:p1"}],
                }
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=issues)

        from spec_runner.github_sync import cmd_sync_from_gh

        cmd_sync_from_gh(_make_args(), tasks, tasks_file)
        content_after_first = tasks_file.read_text()

        # Second pull with identical remote state
        tasks_after_first = parse_tasks(tasks_file)
        cmd_sync_from_gh(_make_args(), tasks_after_first, tasks_file)

        assert tasks_file.read_text() == content_after_first

    @patch("spec_runner.github_sync.subprocess.run")
    def test_open_issue_without_status_label_defaults_to_todo(self, mock_run, tmp_path):
        """Open issue, no status:* label → local goes to 'todo'.

        This pins `_status_from_issue`'s fallback behavior. A task currently
        marked `in_progress` locally gets pulled back to `todo`, which is
        destructive — the caller is responsible for knowing the remote lacks
        the label. Test exists so a silent change to "stay as-is" is caught.
        """
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        issues = json.dumps(
            [
                {
                    "number": 5,
                    "title": "[TASK-002] Add authentication",
                    "state": "OPEN",
                    "labels": [{"name": "priority:p1"}],  # no status:*
                }
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=issues)

        from spec_runner.github_sync import cmd_sync_from_gh

        cmd_sync_from_gh(_make_args(), tasks, tasks_file)

        updated = {t.id: t for t in parse_tasks(tasks_file)}
        assert updated["TASK-002"].status == "todo"

    @patch("spec_runner.github_sync.subprocess.run")
    def test_pull_ignores_issues_without_task_id_prefix(self, mock_run, tmp_path):
        """Issues without [TASK-XXX] must not touch any local task."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)
        before = tasks_file.read_text()

        issues = json.dumps(
            [
                {
                    "number": 1,
                    "title": "Random issue not mapped to a task",
                    "state": "CLOSED",
                    "labels": [{"name": "status:done"}],
                }
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=issues)

        from spec_runner.github_sync import cmd_sync_from_gh

        cmd_sync_from_gh(_make_args(), tasks, tasks_file)

        assert tasks_file.read_text() == before

    @patch("spec_runner.github_sync.subprocess.run")
    def test_gh_list_failure_does_not_modify_tasks_file(self, mock_run, tmp_path, capsys):
        """If `gh issue list` returns non-zero, tasks.md must stay untouched."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)
        before = tasks_file.read_text()

        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="gh: authentication required",
        )

        from spec_runner.github_sync import cmd_sync_from_gh

        cmd_sync_from_gh(_make_args(), tasks, tasks_file)

        assert tasks_file.read_text() == before
        captured = capsys.readouterr()
        assert "gh issue list failed" in captured.out or "gh issue list failed" in captured.err

    @patch("spec_runner.github_sync.subprocess.run")
    def test_roundtrip_push_then_pull_is_stable(self, mock_run, tmp_path):
        """push → pull round-trip must converge: tasks.md unchanged when nothing diverged.

        Pins the contract that `sync_to_gh` followed by `sync_from_gh` with
        the same GitHub state is a fixed point.
        """
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)
        original = tasks_file.read_text()

        # After the push, remote matches local state.
        remote_state = json.dumps(
            [
                {
                    "number": 1,
                    "title": "[TASK-001] Set up project",
                    "state": "CLOSED",
                    "labels": [{"name": "priority:p0"}, {"name": "status:done"}],
                },
                {
                    "number": 2,
                    "title": "[TASK-002] Add authentication",
                    "state": "OPEN",
                    "labels": [{"name": "priority:p1"}, {"name": "status:in_progress"}],
                },
                {
                    "number": 3,
                    "title": "[TASK-003] Write docs",
                    "state": "OPEN",
                    "labels": [{"name": "priority:p2"}, {"name": "status:todo"}],
                },
            ]
        )
        mock_run.return_value = MagicMock(returncode=0, stdout=remote_state)

        from spec_runner.github_sync import cmd_sync_from_gh, cmd_sync_to_gh

        cmd_sync_to_gh(_make_args(), tasks)
        cmd_sync_from_gh(_make_args(), parse_tasks(tasks_file), tasks_file)

        assert tasks_file.read_text() == original
