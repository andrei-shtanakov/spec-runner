# Phase 10: CI + GitHub Issues Sync

**Goal:** Add GitHub Actions CI pipeline and bidirectional GitHub Issues synchronization.

**Scope:** One YAML workflow file, two new CLI commands (~140 lines Python), ~10 new tests.

---

## 1. CI (GitHub Actions)

### Approach

Single workflow `.github/workflows/ci.yml` with two parallel jobs:

**Job: lint**
- `ubuntu-latest`
- `astral-sh/setup-uv` for uv installation
- `uv sync` → `ruff check .` → `ruff format --check .`

**Job: test**
- `ubuntu-latest`, matrix: Python 3.11, 3.12, 3.13
- `actions/setup-python` + `astral-sh/setup-uv`
- `uv sync` → `pytest tests/ -v`

**Triggers:** `push` and `pull_request` on all branches.

### What we don't do

- No publishing (PyPI, Docker)
- No type checking in CI (mypy/pyrefly — too slow for now)
- No coverage reporting
- No caching beyond uv's built-in cache

---

## 2. GitHub Issues Sync

### Architecture

Two new CLI commands in `task.py` (where `cmd_export_gh` already lives), registered as `spec-task` subcommands.

### Matching strategy

Issue title prefix `[TASK-XXX]` is the key for linking tasks to issues. Created issues get this prefix automatically. Lookup uses `gh issue list --search "[TASK-XXX] in:title"`.

### Labels

- Priority: `priority:p0`, `priority:p1`, `priority:p2`, `priority:p3`
- Status: `status:todo`, `status:in_progress`, `status:done`, `status:blocked`

Labels are created on first sync if they don't exist (`gh label create`).

### cmd_sync_to_gh (~80 lines)

1. `parse_tasks()` to get all tasks
2. `gh issue list --json number,title,state,labels --limit 200` to find existing `[TASK-XXX]` issues
3. For each task:
   - If issue exists: `gh issue edit <number>` — update labels (priority, status), close if done, reopen if todo/in_progress
   - If no issue: `gh issue create` with title `[TASK-XXX] Task name`, body from description + checklist, labels
4. Print summary: created N, updated M, closed K

### cmd_sync_from_gh (~60 lines)

1. `gh issue list --json number,title,state,labels --limit 200` to get all `[TASK-XXX]` issues
2. Extract task ID from title prefix
3. Map issue state to task status:
   - `closed` → `done`
   - `open` + label `status:in_progress` → `in_progress`
   - `open` + label `status:blocked` → `blocked`
   - `open` (default) → `todo`
4. For each changed task: `update_task_status()` to write back to tasks.md
5. Print summary: updated N tasks

### CLI integration

```
spec-task sync-to-gh      # Push tasks → GitHub Issues
spec-task sync-from-gh    # Pull GitHub Issues → tasks
```

New subparsers in `task.py:main()`.

### Error handling

- Missing `gh` CLI: error message with install instructions, exit 1
- No git remote: error message, exit 1
- Individual task sync failure: warn and continue with next task
- API rate limit: stop and report progress so far

### No new dependencies

Uses `subprocess.run` to call `gh` CLI — same pattern as existing `cmd_export_gh`.

---

## Summary

| Feature | New files | Modified files | ~Lines |
|---------|-----------|----------------|--------|
| CI | `.github/workflows/ci.yml` | — | ~50 YAML |
| sync-to-gh | — | `task.py` | ~80 |
| sync-from-gh | — | `task.py` | ~60 |
| Tests | `tests/test_gh_sync.py` | — | ~150 |
| **Total** | **2 new files** | **1 modified** | **~340** |

### Implementation order

1. **CI workflow** — standalone YAML, no code changes
2. **sync-to-gh** — builds on existing cmd_export_gh patterns
3. **sync-from-gh** — depends on sync-to-gh label conventions
4. **Tests** — mock gh CLI subprocess calls

### What we don't do

- No webhook-based automation
- No GitHub Actions for sync (user runs manually)
- No issue body updates on re-sync (only title, labels, state)
- No conflict resolution beyond "command direction wins"
