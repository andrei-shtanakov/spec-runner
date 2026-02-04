# spec-runner

Task automation from markdown specs via Claude CLI. Execute tasks from a structured `tasks.md` file with automatic retries, code review, and Git integration.

## Installation

```bash
pip install spec-runner
```

Or for development:
```bash
pip install -e ".[dev]"
```

Requirements:
- Python 3.10+
- Claude CLI (`claude` command available)
- Git (for branch management)

## Quick Start

```bash
# Install Claude Code skills (creates .claude/skills in current project)
spec-runner-init

# Execute next ready task
spec-runner run

# Execute specific task
spec-runner run --task=TASK-001

# Execute all ready tasks
spec-runner run --all

# Create tasks interactively
spec-runner plan "add user authentication"
```

## Usage as Library

```python
from spec_runner import Task, ExecutorConfig, parse_tasks, get_next_tasks
from pathlib import Path

tasks = parse_tasks(Path("spec/tasks.md"))
ready = get_next_tasks(tasks)

for task in ready:
    print(f"{task.id}: {task.name} ({task.priority})")
```

## Features

- **Task-based execution** — reads tasks from `spec/tasks.md` with priorities, checklists, and dependencies
- **Specification traceability** — links tasks to requirements (REQ-XXX) and design (DESIGN-XXX)
- **Automatic retries** — configurable retry policy with error context passed to next attempt
- **Code review** — multi-agent review after task completion
- **Git integration** — automatic branch creation, commits, and merges
- **Progress logging** — timestamped progress file for monitoring
- **Interactive planning** — create tasks through dialogue with Claude

## Task File Format

Tasks are defined in `spec/tasks.md`:

```markdown
## Milestone 1: MVP

### TASK-001: Implement user login
🔴 P0 | ⬜ TODO | Est: 2d

**Checklist:**
- [ ] Create login endpoint
- [ ] Add JWT token generation
- [ ] Write unit tests

**Depends on:** —
**Blocks:** [TASK-002], [TASK-003]
```

## CLI Commands

### spec-runner

```bash
spec-runner run                     # Execute next ready task
spec-runner run --task=TASK-001     # Execute specific task
spec-runner run --all               # Execute all ready tasks
spec-runner status                  # Show execution status
spec-runner retry TASK-001          # Retry failed task
spec-runner logs TASK-001           # View task logs
spec-runner reset                   # Reset state
spec-runner plan "feature"          # Interactive task creation
```

### spec-runner-init

```bash
spec-runner-init                    # Install skills to ./.claude/skills
spec-runner-init --global           # Install to ~/.claude/skills
spec-runner-init --force            # Overwrite existing skills
```

### spec-task

```bash
spec-task list                      # List all tasks
spec-task list --status=todo        # Filter by status
spec-task show TASK-001             # Task details
spec-task start TASK-001            # Mark as in_progress
spec-task done TASK-001             # Mark as done
spec-task stats                     # Statistics
spec-task next                      # Show next ready tasks
spec-task graph                     # Dependency graph
```

## Configuration

Configuration file: `executor.config.yaml`

```yaml
executor:
  max_retries: 3
  task_timeout_minutes: 30
  claude_command: "claude"

  hooks:
    pre_start:
      create_git_branch: true
    post_done:
      run_tests: true
      run_lint: true
      auto_commit: true
      run_review: true

  commands:
    test: "pytest tests/ -v"
    lint: "ruff check ."
```

## Project Structure

```
project/
├── pyproject.toml
├── executor.config.yaml
├── src/
│   └── spec_runner/
│       ├── __init__.py
│       ├── executor.py
│       └── task.py
└── spec/
    ├── tasks.md
    ├── requirements.md
    ├── design.md
    └── prompts/
```

## License

MIT
