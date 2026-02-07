# spec-runner

Task automation from markdown specs via Claude CLI. Execute tasks from a structured `tasks.md` file with automatic retries, code review, and Git integration.

## Installation

```bash
uv add spec-runner
```

Or for development:
```bash
uv sync
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
spec-runner-init --force            # Overwrite existing skills
spec-runner-init /path/to/project   # Install to specific project
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

### Multi-phase / Multi-project Options

Both `spec-runner` and `spec-task` support `--spec-prefix` for phase-based workflows:

```bash
spec-runner run --spec-prefix=phase5-          # Uses spec/phase5-tasks.md
spec-runner run --project-root=/path/to/proj   # Run against another project
spec-task list --spec-prefix=phase5-           # List phase 5 tasks
```

## Configuration

Configuration file: `executor.config.yaml`

```yaml
executor:
  max_retries: 3
  task_timeout_minutes: 30
  claude_command: "claude"
  claude_model: "sonnet"
  spec_prefix: ""              # e.g. "phase5-" for phase5-tasks.md

  # Custom CLI template (optional). Placeholders: {cmd}, {model}, {prompt}
  # command_template: "{cmd} -p {prompt} --model {model}"

  # Review can use different CLI
  review_command: "codex"
  review_model: "gpt-4"
  # review_command_template: "{cmd} -p {prompt}"

  # Git settings
  main_branch: ""  # Auto-detect (main/master) or set explicitly: "master"

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

  paths:
    root: "."                        # Project root directory
    logs: "spec/.executor-logs"
    state: "spec/.executor-state.json"
```

### Git Branch Workflow

The executor manages git branches automatically:

1. **Branch detection**: Auto-detects `main` or `master`, or use `main_branch` config
2. **Task branches**: Creates `task/task-001-name` branches for each task
3. **Auto-merge**: Merges task branch to main after completion

**Fresh repository (after `git init`):**
- TASK-000 (scaffolding) runs on the initial branch without creating a separate task branch
- First commit is made on `main`
- Subsequent tasks create their own branches

**Existing repository:**
- Each task creates a new branch from `main`
- After task completion, branch is merged back to `main`
- Task branch is deleted after successful merge

**Interrupted tasks:**
- Tasks marked as `in_progress` are resumed first on next run
- Use `--restart` flag to ignore in-progress tasks and start fresh

### Supported CLIs

| CLI | Auto-detected | Example template |
|-----|--------------|------------------|
| Claude | ✅ | `{cmd} -p {prompt} --model {model}` |
| Codex | ✅ | `{cmd} -p {prompt} --model {model}` |
| Ollama | ✅ | `{cmd} run {model} {prompt}` |
| llama-cli | ✅ | `{cmd} -m {model} -p {prompt} --no-display-prompt` |
| llama-server | ✅ | via curl to localhost:8080 |
| Custom | Use template | `{cmd} --prompt {prompt}` |

### Custom Prompts

You can customize prompts for different LLMs by creating files in `spec/prompts/`:

```
spec/prompts/
├── review.md           # Default review prompt
├── review.codex.md     # Codex-specific review prompt
├── review.claude.md    # Claude-specific review prompt
├── review.ollama.md    # Ollama-specific review prompt
├── review.llama.md     # llama.cpp-specific review prompt
└── task.md             # Task execution prompt
```

The executor automatically selects the prompt based on the CLI being used:
- `review_command: "codex"` → uses `review.codex.md` if exists, otherwise `review.md`
- `review_command: "ollama"` → uses `review.ollama.md` if exists, otherwise `review.md`

#### Prompt Variables

Use `${VARIABLE}` or `{{VARIABLE}}` syntax in templates:

| Variable | Description |
|----------|-------------|
| `${TASK_ID}` | Task ID (e.g., TASK-001) |
| `${TASK_NAME}` | Task name |
| `${CHANGED_FILES}` | List of changed files |
| `${GIT_DIFF}` | Git diff summary |

#### Response Format

All review prompts should instruct the LLM to end responses with one of:
- `REVIEW_PASSED` — code is acceptable
- `REVIEW_FIXED` — issues found and fixed
- `REVIEW_FAILED` — issues remain

**Tip for smaller models (Ollama, llama):** Use shorter, simpler prompts and emphasize the response format requirement.

## Project Structure

```
project/
├── pyproject.toml
├── executor.config.yaml
├── src/
│   └── spec_runner/
│       ├── __init__.py
│       ├── executor.py
│       ├── task.py
│       ├── init_cmd.py
│       └── skills/
│           └── spec-generator-skill/
│               ├── SKILL.md
│               └── templates/
└── spec/
    ├── tasks.md
    ├── requirements.md
    ├── design.md
    └── prompts/
```

## License

MIT
