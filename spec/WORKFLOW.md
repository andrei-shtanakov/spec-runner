# Task Management Workflow

## Overview

The task management system works directly with the `spec/tasks.md` file:
- Statuses and checklists are updated in markdown
- Change history is logged in `.task-history.log`
- Dependencies are tracked automatically
- **Automatic execution via Claude CLI**

> This workflow assumes `spec/tasks.md` already exists. To *generate* specs
> stage-by-stage with an approval gate between each, see `spec-runner plan
> --gated` and `spec-runner spec approve <stage>` — details in the README's
> "Spec Governance (gated generation)" section.

## Quick Start

```bash
# === Manual Mode ===
spec-runner task stats           # Statistics
spec-runner task next            # What to do next
spec-runner task start TASK-001
spec-runner task done TASK-001

# === Automatic Mode (Claude CLI) ===
spec-runner run                  # Execute next task
spec-runner run --all            # Execute all ready tasks
spec-runner run --all --milestone=mvp  # Execute MVP tasks
spec-runner status               # Execution status
```

<!-- TODO: the Makefile no longer defines task-*/exec* targets (only
     test/lint/typecheck/format/e2e); the CLI invocations above are the
     current equivalents. Re-add make targets here if they come back. -->

---

## Automatic Execution (Claude CLI)

### Concept

Executor runs Claude CLI for each task:
1. Reads specification (requirements.md, design.md)
2. Forms prompt with task context
3. Claude implements code and tests
4. Checks result (tests, lint)
5. On success — moves to next task
6. On failure — retry with limit

### Commands

```bash
# Execute next ready task
spec-runner run

# Execute specific task
spec-runner run --task=TASK-001

# Execute all ready tasks
spec-runner run --all

# Only MVP tasks
spec-runner run --all --milestone=mvp

# Execution status
spec-runner status

# Retry failed task
spec-runner retry TASK-001

# View logs
spec-runner logs TASK-001

# Reset state
spec-runner reset
```

### Options

```bash
# Number of attempts (default: 3)
spec-runner run --max-retries=5

# Timeout in minutes (default: 30)
spec-runner run --timeout=60

# Without tests after execution
spec-runner run --no-tests

# Without creating git branch
spec-runner run --no-branch

# Without auto-commit (auto-commit is ON by default)
spec-runner run --no-commit
```

### Automatic Execution Workflow

```
┌─────────────────────────────────────────────────────────────┐
│                     spec-runner run                           │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│  1. Find next task (by priority + dependencies)             │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│  2. Pre-start hook                                          │
│     - Create git branch: task/TASK-XXX-name                │
│     - Update status: TODO → IN_PROGRESS                     │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│  3. Form prompt                                             │
│     - Context from requirements.md, design.md               │
│     - Task checklist                                        │
│     - Related REQ-XXX, DESIGN-XXX                           │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│  4. Run Claude CLI                                          │
│     claude -p "<prompt>"                                    │
└─────────────────────┬───────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────┐
│  5. Check result                                            │
│     - Claude returned "TASK_COMPLETE"?                      │
│     - Tests pass? (make test)                               │
│     - Lint clean? (make lint)                               │
└─────────────────────┬───────────────────────────────────────┘
                      │
            ┌─────────┴─────────┐
            │                   │
            ▼                   ▼
     ┌──────────┐        ┌──────────┐
     │ SUCCESS  │        │  FAILED  │
     └────┬─────┘        └────┬─────┘
          │                   │
          ▼                   ▼
┌─────────────────┐   ┌─────────────────┐
│ Post-done hook  │   │ Retry?          │
│ - Auto-commit   │   │ attempts < max  │
│ - Mark DONE     │   └────────┬────────┘
│ - Next task     │            │
└─────────────────┘   ┌────────┴────────┐
                      │                 │
                      ▼                 ▼
               ┌──────────┐      ┌──────────┐
               │  RETRY   │      │   STOP   │
               │ (loop)   │      │ BLOCKED  │
               └──────────┘      └──────────┘
```

### Protection Mechanisms

| Mechanism | Default | Description |
|-----------|---------|-------------|
| max_retries | 3 | Max attempts per task |
| max_consecutive_failures | 2 | Stop after N consecutive failures |
| task_timeout | 30 min | Task timeout |
| post_done tests | ON | Test verification |

### Logs

Logs are saved in `spec/.executor-logs/`:

```
spec/.executor-logs/
├── TASK-001-20250122-103000.log
├── TASK-001-20250122-103500.log  # retry
└── TASK-003-20250122-110000.log
```

Log content:
```
=== PROMPT ===
<full prompt for Claude>

=== OUTPUT ===
<Claude response>

=== STDERR ===
<errors if any>

=== RETURN CODE: 0 ===
```

### Configuration

Configuration is loaded from `spec-runner.config.yaml` at the project root (if it
exists). CLI arguments override YAML config. The legacy path
`spec/executor.config.yaml` (with an `executor:` wrapper) is still read as a
fallback but is deprecated — move it to the project root and drop the wrapper.

File `spec-runner.config.yaml`:

```yaml
max_retries: 3
task_timeout_minutes: 30

hooks:
  pre_start:
    create_git_branch: true
  post_done:
    run_tests: true
    run_lint: true
    auto_commit: true  # Use --no-commit to disable

commands:
  test: "uv run pytest tests/ -v -m 'not slow'"
  lint: "uv run ruff check ."
```

---

## CLI Commands

### Viewing

```bash
# All tasks
spec-runner task list

# Filtering
spec-runner task list --status=todo
spec-runner task list --priority=p0
spec-runner task list --milestone=mvp

# Task details
spec-runner task show TASK-001

# Statistics
spec-runner task stats

# Dependency graph
spec-runner task graph
```

### Status Management

```bash
# Start work
spec-runner task start TASK-001

# Start, ignoring dependencies
spec-runner task start TASK-001 --force

# Complete
spec-runner task done TASK-001

# Block
spec-runner task block TASK-001
```

### Checklist

```bash
# Show task with checklist
spec-runner task show TASK-001

# Mark item (toggle)
spec-runner task check TASK-001 0   # first item
spec-runner task check TASK-001 2   # third item
```

## Workflow

### 1. Task Selection

```bash
# See what's ready to work on
spec-runner task next

# Output:
# 🚀 Next tasks (ready to work):
#
# 1. 🔴 TASK-100: Test Infrastructure Setup
#    Est: 2d | Milestone 1: MVP ✓ deps OK
```

### 2. Starting Work

```bash
# Start task
spec-runner task start TASK-100

# ✓ TASK-100 started!
```

Status in `tasks.md` updates: `⬜ TODO` → `🔄 IN PROGRESS`

### 3. Working with Checklist

```bash
# View checklist
spec-runner task show TASK-100

# Mark completed items
spec-runner task check TASK-100 0
spec-runner task check TASK-100 1
```

### 4. Completion

```bash
# Complete
spec-runner task done TASK-100

# ✅ TASK-100 completed!
#
# 🔓 Unblocked tasks:
#    TASK-001: ATP Protocol Models
#    TASK-004: Test Loader
```

### 5. Checking Progress

```bash
spec-runner task stats

# 📊 Task Statistics
# ==================
#
# By status:
#   ✅ done          3 ████░░░░░░░░░░░░░░░░ 12%
#   🔄 in_progress   1 █░░░░░░░░░░░░░░░░░░░  4%
#   ⬜ todo         21 ████████████████████ 84%
```

## Dependencies

The system automatically tracks dependencies:

- With `task next` — shows only tasks with completed dependencies
- With `task start` — warns about incomplete dependencies
- With `task done` — shows unblocked tasks

```bash
# Attempting to start task with incomplete dependencies
spec-runner task start TASK-003

# ⚠️  Task depends on incomplete: TASK-001
#    Use --force to start anyway
```

## Export to GitHub Issues

```bash
# Sync tasks to GitHub Issues (creates/updates issues; local wins on conflict)
spec-runner task sync-to-gh

# Preview without making changes
spec-runner task sync-to-gh --dry-run

# Sync GitHub Issues state back into tasks.md (remote wins on conflict)
spec-runner task sync-from-gh
```

<!-- `task export-gh` (dump gh-issue-create commands to copy/paste) is still
     available but superseded by the sync-to-gh/sync-from-gh pair above. -->

## Git Integration

Recommended workflow with branches:

```bash
# 1. Start task
spec-runner task start TASK-001
git checkout -b task/TASK-001-protocol-models

# 2. Work...
git commit -m "TASK-001: Add ATPRequest model"

# 3. Complete
spec-runner task done TASK-001
git checkout main
git merge task/TASK-001-protocol-models
```

## CLI Equivalents

<!-- TODO: the Makefile (see repo root) currently only defines
     test/lint/typecheck/format/e2e — the task-*/exec-* convenience targets
     that used to wrap these commands are gone. Use `spec-runner task` /
     `spec-runner run` directly until (if) they're reintroduced. -->

| Command | Description |
|---------|-------------|
| `spec-runner task list` | List all tasks |
| `spec-runner task list --status=todo` | TODO tasks |
| `spec-runner task list --status=in_progress` | Tasks in progress |
| `spec-runner task stats` | Statistics |
| `spec-runner task next` | Next tasks |
| `spec-runner task graph` | Dependency graph |
| `spec-runner task list --priority=p0` | Only P0 |
| `spec-runner task list --milestone=mvp` | MVP tasks |
| `spec-runner task start TASK-XXX` | Start task |
| `spec-runner task done TASK-XXX` | Complete task |
| `spec-runner task show TASK-XXX` | Show details |

## Change History

All changes are logged in `spec/.task-history.log`:

```
2025-01-22T10:30:00 | TASK-100 | status -> in_progress
2025-01-22T10:35:00 | TASK-100 | checklist[0] -> done
2025-01-22T11:00:00 | TASK-100 | status -> done
```

## Tips

1. **Start your day with `task next`** — see priority ready tasks
2. **Mark checklist regularly** — progress is immediately visible
3. **Don't force dependencies** — they're there for a reason
4. **Commit tasks.md** — history in Git
5. **Use `--force` consciously** — only when really needed
