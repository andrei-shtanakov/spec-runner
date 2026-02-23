# Phase 10: CI + GitHub Issues Sync — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add a GitHub Actions CI pipeline (lint + test) and two CLI commands for bidirectional GitHub Issues synchronization.

**Architecture:** CI is a single YAML workflow with two parallel jobs (lint, test matrix). GitHub Issues sync adds two commands to `task.py` — `sync-to-gh` pushes tasks as issues via `gh` CLI, `sync-from-gh` pulls issue state back into tasks.md. Both commands use subprocess calls to `gh`, matching tasks to issues by `[TASK-XXX]` title prefix.

**Tech Stack:** GitHub Actions, `gh` CLI (subprocess), Python 3.10+, pytest

---

### Task 1: CI Workflow

**Files:**
- Create: `.github/workflows/ci.yml`

**Step 1: Create the workflow directory**

```bash
mkdir -p /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor/.github/workflows
```

**Step 2: Write the CI workflow**

Create `.github/workflows/ci.yml`:

```yaml
name: CI

on:
  push:
    branches: [master]
  pull_request:

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
      - run: uv sync
      - run: uv run ruff check .
      - run: uv run ruff format --check .

  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python-version: ["3.11", "3.12", "3.13"]
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python-version }}
      - uses: astral-sh/setup-uv@v4
      - run: uv sync
      - run: uv run pytest tests/ -v -m "not slow"
```

**Step 3: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: add GitHub Actions workflow for lint + test matrix"
```

---

### Task 2: sync-to-gh Tests (Red Phase)

**Files:**
- Create: `tests/test_gh_sync.py`

**Step 1: Write the failing tests**

Create `tests/test_gh_sync.py`:

```python
"""Tests for GitHub Issues sync commands."""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from spec_runner.task import Task, cmd_sync_from_gh, cmd_sync_to_gh, parse_tasks


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

        cmd_sync_to_gh(_make_args(), tasks)

        captured = capsys.readouterr()
        assert "gh" in captured.out.lower() or "gh" in captured.err.lower()
```

**Step 2: Run tests to verify they fail**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_gh_sync.py -v
```

Expected: FAIL with `ImportError: cannot import name 'cmd_sync_to_gh' from 'spec_runner.task'`

**Step 3: Commit**

```bash
git add tests/test_gh_sync.py
git commit -m "test: add sync-to-gh tests (red phase)"
```

---

### Task 3: sync-to-gh Implementation (Green Phase)

**Files:**
- Modify: `src/spec_runner/task.py:1-20` (add `import json, subprocess`)
- Modify: `src/spec_runner/task.py:653` (add `cmd_sync_to_gh` after `cmd_export_gh`)
- Modify: `src/spec_runner/task.py:743-770` (add subparser + dispatch)

**Step 1: Add imports to task.py**

At the top of `src/spec_runner/task.py`, add `import json` and `import subprocess` to the existing imports (after `import argparse`, before `import re`):

```python
import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
```

**Step 2: Add `_gh_run` helper and `cmd_sync_to_gh` function**

Insert after `cmd_export_gh` (after line 681) in `src/spec_runner/task.py`:

```python
def _gh_run(args: list[str], capture: bool = True) -> subprocess.CompletedProcess:
    """Run a gh CLI command. Raises FileNotFoundError if gh is missing."""
    return subprocess.run(
        ["gh"] + args,
        capture_output=capture,
        text=True,
        check=False,
    )


def _get_existing_issues() -> dict[str, dict]:
    """Fetch existing [TASK-XXX] issues from GitHub. Returns {task_id: issue_dict}."""
    result = _gh_run(["issue", "list", "--json", "number,title,state,labels", "--limit", "200"])
    if result.returncode != 0:
        return {}
    issues = json.loads(result.stdout)
    mapping: dict[str, dict] = {}
    for issue in issues:
        # Match [TASK-XXX] prefix in title
        m = re.match(r"\[(TASK-\d+)\]", issue["title"])
        if m:
            mapping[m.group(1)] = issue
    return mapping


def _task_labels(task: Task) -> list[str]:
    """Build label list for a task."""
    labels = [f"priority:{task.priority}", f"status:{task.status}"]
    return labels


def _task_body(task: Task) -> str:
    """Build issue body from task."""
    parts: list[str] = []
    if task.estimate:
        parts.append(f"**Estimate:** {task.estimate}")
    if task.checklist:
        parts.append("**Checklist:**")
        for item, checked in task.checklist:
            mark = "x" if checked else " "
            parts.append(f"- [{mark}] {item}")
    if task.depends_on:
        parts.append(f"\n**Depends on:** {', '.join(task.depends_on)}")
    if task.traces_to:
        parts.append(f"**Traces to:** {', '.join(task.traces_to)}")
    return "\n".join(parts)


def cmd_sync_to_gh(args, tasks: list[Task]):
    """Sync tasks to GitHub Issues. Creates, updates, or closes issues."""
    dry_run = getattr(args, "dry_run", False)

    try:
        existing = _get_existing_issues()
    except FileNotFoundError:
        print("Error: 'gh' CLI not found. Install from https://cli.github.com/")
        return

    created, updated, closed = 0, 0, 0

    for task in tasks:
        issue = existing.get(task.id)
        labels = _task_labels(task)
        label_str = ",".join(labels)

        if task.status == "done":
            # Close issue if it exists and is open
            if issue and issue["state"] == "OPEN":
                if not dry_run:
                    _gh_run(["issue", "close", str(issue["number"])])
                closed += 1
            continue

        if issue:
            # Update existing issue labels
            if not dry_run:
                _gh_run(["issue", "edit", str(issue["number"]), "--add-label", label_str])
                # Reopen if closed
                if issue["state"] == "CLOSED":
                    _gh_run(["issue", "edit", str(issue["number"]), "--state", "open"])
            updated += 1
        else:
            # Create new issue
            title = f"[{task.id}] {task.name}"
            body = _task_body(task)
            if not dry_run:
                _gh_run([
                    "issue", "create",
                    "--title", title,
                    "--body", body,
                    "--label", label_str,
                ])
            created += 1

    action = "Would" if dry_run else "Done"
    print(f"{action}: created={created}, updated={updated}, closed={closed}")
```

**Step 3: Register sync-to-gh subcommand**

In `main()`, add after the `export-gh` subparser (line ~744):

```python
    # sync-to-gh
    sync_to_parser = subparsers.add_parser(
        "sync-to-gh", parents=[common], help="Sync tasks to GitHub Issues"
    )
    sync_to_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would happen without making changes"
    )
```

Add `"sync-to-gh": cmd_sync_to_gh` to the `read_commands` dict (it reads tasks but writes to GitHub, not tasks.md).

**Step 4: Run tests to verify they pass**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_gh_sync.py::TestSyncToGh -v
```

Expected: All 5 tests PASS.

**Step 5: Run full test suite**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/ -v -m "not slow"
```

Expected: All tests pass (410+ existing + 5 new).

**Step 6: Lint**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run ruff check src/spec_runner/task.py && uv run ruff format --check src/spec_runner/task.py
```

**Step 7: Commit**

```bash
git add src/spec_runner/task.py
git commit -m "feat: add sync-to-gh command for GitHub Issues sync"
```

---

### Task 4: sync-from-gh Tests (Red Phase)

**Files:**
- Modify: `tests/test_gh_sync.py` (add `TestSyncFromGh` class)

**Step 1: Add sync-from-gh tests**

Append to `tests/test_gh_sync.py`:

```python
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

        cmd_sync_from_gh(_make_args(), tasks, tasks_file)

        # Re-parse to check updated status
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

        cmd_sync_from_gh(_make_args(), tasks, tasks_file)

        # File should be unchanged since TASK-002 is already in_progress
        assert tasks_file.read_text() == original_content

    @patch("spec_runner.task.subprocess.run")
    def test_gh_not_found(self, mock_run, tmp_path, capsys):
        """Should print error when gh CLI is not available."""
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)

        mock_run.side_effect = FileNotFoundError("gh not found")

        cmd_sync_from_gh(_make_args(), tasks, tasks_file)

        captured = capsys.readouterr()
        assert "gh" in captured.out.lower() or "gh" in captured.err.lower()
```

**Step 2: Run tests to verify they fail**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_gh_sync.py::TestSyncFromGh -v
```

Expected: FAIL with `ImportError: cannot import name 'cmd_sync_from_gh' from 'spec_runner.task'`

**Step 3: Commit**

```bash
git add tests/test_gh_sync.py
git commit -m "test: add sync-from-gh tests (red phase)"
```

---

### Task 5: sync-from-gh Implementation (Green Phase)

**Files:**
- Modify: `src/spec_runner/task.py` (add `cmd_sync_from_gh` after `cmd_sync_to_gh`)
- Modify: `src/spec_runner/task.py` `main()` (add subparser + dispatch)

**Step 1: Add `cmd_sync_from_gh` function**

Insert after `cmd_sync_to_gh` in `src/spec_runner/task.py`:

```python
def _status_from_issue(issue: dict) -> str:
    """Derive task status from GitHub issue state + labels."""
    if issue["state"] == "CLOSED":
        return "done"
    # Check status:X labels
    for label in issue.get("labels", []):
        name = label["name"] if isinstance(label, dict) else label
        if name.startswith("status:"):
            status = name.split(":", 1)[1]
            if status in STATUS_EMOJI:
                return status
    return "todo"


def cmd_sync_from_gh(args, tasks: list[Task], tasks_file: Path):
    """Sync GitHub Issues state back to tasks.md."""
    try:
        result = _gh_run(["issue", "list", "--json", "number,title,state,labels", "--limit", "200"])
    except FileNotFoundError:
        print("Error: 'gh' CLI not found. Install from https://cli.github.com/")
        return

    if result.returncode != 0:
        print(f"Error: gh issue list failed: {result.stderr}")
        return

    issues = json.loads(result.stdout)

    # Build task_id -> desired status mapping
    status_map: dict[str, str] = {}
    for issue in issues:
        m = re.match(r"\[(TASK-\d+)\]", issue["title"])
        if m:
            status_map[m.group(1)] = _status_from_issue(issue)

    updated = 0
    for task in tasks:
        new_status = status_map.get(task.id)
        if new_status and new_status != task.status:
            if update_task_status(tasks_file, task.id, new_status):
                updated += 1
                print(f"  {task.id}: {task.status} -> {new_status}")

    print(f"Updated {updated} task(s) from GitHub Issues.")
```

**Step 2: Register sync-from-gh subcommand**

In `main()`, add after `sync-to-gh` subparser:

```python
    # sync-from-gh
    subparsers.add_parser(
        "sync-from-gh", parents=[common], help="Sync GitHub Issues state to tasks.md"
    )
```

Add `"sync-from-gh": cmd_sync_from_gh` to the `write_commands` dict (it writes to tasks.md).

Note: `cmd_sync_from_gh` takes `(args, tasks, tasks_file)` — same signature as other write commands.

**Step 3: Run tests to verify they pass**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/test_gh_sync.py -v
```

Expected: All 9 tests PASS (5 sync-to-gh + 4 sync-from-gh).

**Step 4: Run full test suite**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/ -v -m "not slow"
```

Expected: All tests pass.

**Step 5: Lint**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run ruff check src/spec_runner/task.py && uv run ruff format --check src/spec_runner/task.py
```

**Step 6: Commit**

```bash
git add src/spec_runner/task.py
git commit -m "feat: add sync-from-gh command for bidirectional GitHub Issues sync"
```

---

### Task 6: Update Docs and Exports

**Files:**
- Modify: `src/spec_runner/__init__.py` (add exports)
- Modify: `CLAUDE.md` (add new CLI commands)

**Step 1: Update `__init__.py`**

In `src/spec_runner/__init__.py`, add `cmd_sync_to_gh` and `cmd_sync_from_gh` to the imports from `.task` and to `__all__`:

Add to the import block from `.task`:
```python
from .task import (
    ...
    cmd_sync_from_gh,
    cmd_sync_to_gh,
    ...
)
```

Add to `__all__`:
```python
    "cmd_sync_to_gh",
    "cmd_sync_from_gh",
```

**Step 2: Update CLAUDE.md CLI section**

Add to the `### CLI entry points` section in `CLAUDE.md`:

```
spec-task sync-to-gh                       # Sync tasks → GitHub Issues
spec-task sync-to-gh --dry-run             # Preview without making changes
spec-task sync-from-gh                     # Sync GitHub Issues → tasks.md
```

**Step 3: Update docstring at top of task.py**

Add to the module docstring:
```
    spec-task sync-to-gh              # Sync tasks to GitHub Issues
    spec-task sync-from-gh            # Sync GitHub Issues to tasks.md
```

**Step 4: Run full test suite**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/executor && uv run pytest tests/ -v -m "not slow"
```

Expected: All tests pass.

**Step 5: Commit**

```bash
git add src/spec_runner/__init__.py src/spec_runner/task.py CLAUDE.md
git commit -m "docs: update exports and docs for GitHub Issues sync commands"
```
