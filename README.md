# spec-runner

Task automation from markdown specs via Claude CLI. Execute tasks from a structured `tasks.md` file with automatic retries, code review, Git integration, parallel execution, and live TUI dashboard.

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
- `gh` CLI (optional, for GitHub Issues sync)

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

# Execute in parallel with live TUI
spec-runner run --all --parallel --tui

# Create tasks interactively
spec-runner plan "add user authentication"

# Watch mode — continuously execute ready tasks
spec-runner watch
```

## Features

- **Task-based execution** — reads tasks from `spec/tasks.md` with priorities, checklists, and dependencies
- **Specification traceability** — links tasks to requirements (REQ-XXX) and design (DESIGN-XXX)
- **Automatic retries** — configurable retry policy with exponential backoff and error context forwarding
- **Code review** — multi-agent review after task completion with enriched diff context
- **Git integration** — automatic branch creation, commits, and merges
- **Parallel execution** — run multiple independent tasks concurrently with semaphore-based limiting
- **TUI dashboard** — live Textual-based terminal UI with progress bars and log panel
- **Cost tracking** — per-task token usage and cost breakdown
- **Watch mode** — continuously poll and execute ready tasks
- **Plugin system** — extend with custom hooks via `spec/plugins/*/plugin.yaml`
- **MCP server** — read-only Model Context Protocol server for Claude Code integration
- **GitHub Issues sync** — bidirectional sync between tasks.md and GitHub Issues
- **Interactive planning** — generate specs (requirements + design + tasks) through dialogue with Claude
- **Structured logging** — JSON/console output via structlog
- **SQLite state** — persistent execution state with WAL mode, auto-migration from legacy JSON
- **HITL review** — optional human-in-the-loop approval gate after code review
- **Parallel review** — 5 specialized review agents (quality, implementation, testing, simplification, docs) running concurrently
- **Agent personas** — role-specific prompt templates and model selection (architect, implementer, reviewer)
- **Constitution guardrails** — inviolable project rules from `spec/constitution.md` injected into every prompt
- **Telegram notifications** — alerts on task failure and run completion via Telegram Bot API
- **Pause/resume** — pause mid-run with Ctrl+\, edit tasks, resume; TUI keybinding `p`
- **Streaming events** — live stdout streaming from Claude CLI to TUI via EventBus
- **Session/idle timeouts** — automatic stop after configurable session or idle duration

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

**Traces to:** [REQ-001], [DESIGN-001]
**Depends on:** —
**Blocks:** [TASK-002], [TASK-003]
```

## CLI Commands

### spec-runner

```bash
# Execution
spec-runner run                            # Execute next ready task
spec-runner run --task=TASK-001            # Execute specific task
spec-runner run --all                      # Execute all ready tasks
spec-runner run --all --parallel           # Execute ready tasks in parallel
spec-runner run --all --parallel --max-concurrent=5  # With concurrency limit
spec-runner run --all --hitl-review        # Interactive HITL approval gate
spec-runner run --force                    # Skip lock check (stale lock)
spec-runner run --tui                      # Execute with live TUI dashboard
spec-runner run --log-level=DEBUG          # Set log verbosity
spec-runner run --log-json                 # Output logs as JSON

# Monitoring
spec-runner status                         # Show execution status
spec-runner costs                          # Cost breakdown per task
spec-runner costs --json                   # JSON output for automation
spec-runner costs --sort=cost              # Sort by cost descending
spec-runner logs TASK-001                  # View task logs

# Operations
spec-runner retry TASK-001                 # Retry failed task
spec-runner reset                          # Reset state
spec-runner watch                          # Continuously execute ready tasks
spec-runner watch --tui                    # Watch with live TUI dashboard
spec-runner tui                            # Launch TUI status dashboard
spec-runner validate                       # Validate config and tasks

# Planning
spec-runner plan "description"             # Interactive task planning
spec-runner plan --full "description"      # Generate full spec (requirements + design + tasks)

# Integration
spec-runner mcp                            # Launch read-only MCP server (stdio)
```

### spec-task

```bash
# Task management
spec-task list                             # List all tasks
spec-task list --status=todo               # Filter by status
spec-task list --priority=p0               # Filter by priority
spec-task list --milestone=mvp             # Filter by milestone
spec-task show TASK-001                    # Task details
spec-task start TASK-001                   # Mark as in_progress
spec-task done TASK-001                    # Mark as done
spec-task block TASK-001                   # Mark as blocked
spec-task check TASK-001 2                 # Mark checklist item
spec-task stats                            # Statistics
spec-task next                             # Show next ready tasks
spec-task graph                            # ASCII dependency graph

# GitHub Issues
spec-task export-gh                        # Export to GitHub Issues format
spec-task sync-to-gh                       # Sync tasks -> GitHub Issues
spec-task sync-to-gh --dry-run             # Preview without making changes
spec-task sync-from-gh                     # Sync GitHub Issues -> tasks.md
```

### spec-runner-init

```bash
spec-runner-init                           # Install skills to ./.claude/skills
spec-runner-init --force                   # Overwrite existing skills
spec-runner-init /path/to/project          # Install to specific project
```

### Multi-phase Options

Both `spec-runner` and `spec-task` support `--spec-prefix` for phase-based workflows:

```bash
spec-runner run --spec-prefix=phase5-          # Uses spec/phase5-tasks.md
spec-task list --spec-prefix=phase5-           # List phase 5 tasks
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

## MCP Server (Claude Code Integration)

spec-runner includes a read-only MCP server for querying project status from Claude Code.

Add to `.mcp.json`:

```json
{
  "mcpServers": {
    "spec-runner": {
      "command": "spec-runner",
      "args": ["mcp"]
    }
  }
}
```

Available tools: `spec_runner_status`, `spec_runner_tasks`, `spec_runner_costs`, `spec_runner_logs`.

## Configuration

Configuration file: `executor.config.yaml`

```yaml
executor:
  max_retries: 3
  task_timeout_minutes: 30
  claude_command: "claude"
  claude_model: "sonnet"
  spec_prefix: ""              # e.g. "phase5-" for phase5-tasks.md
  max_concurrent: 3            # Parallel task limit
  budget_usd: 50.0             # Total budget cap
  task_budget_usd: 10.0        # Per-task budget cap
  session_timeout_minutes: 0   # Global session timeout (0 = disabled)
  idle_timeout_minutes: 0      # Idle timeout between tasks (0 = disabled)

  # Telegram notifications (optional)
  telegram_bot_token: ""       # Bot token from @BotFather
  telegram_chat_id: ""         # Chat ID to send notifications to
  notify_on: [run_complete, task_failed]

  # Agent personas (optional)
  personas:
    implementer:
      system_prompt: "You are a focused Python developer"
      model: "sonnet"
    reviewer:
      system_prompt: "You are a senior code reviewer"
      model: "haiku"

  hooks:
    pre_start:
      create_git_branch: true
    post_done:
      run_tests: true
      run_lint: true
      auto_commit: true
      run_review: true
      review_parallel: false   # Run 5 review agents in parallel
      review_roles: [quality, implementation, testing]

  commands:
    test: "uv run pytest tests/ -v"
    lint: "uv run ruff check ."

  paths:
    root: "."
    logs: "spec/.executor-logs"
```

### Git Branch Workflow

1. **Branch detection**: Auto-detects `main` or `master`, or use `main_branch` config
2. **Task branches**: Creates `task/TASK-001-short-name` branches for each task
3. **Auto-merge**: Merges task branch to main after completion

### Supported CLIs

| CLI | Auto-detected | Example template |
|-----|--------------|------------------|
| Claude | Yes | `{cmd} -p {prompt} --model {model}` |
| Codex | Yes | `{cmd} -p {prompt} --model {model}` |
| Ollama | Yes | `{cmd} run {model} {prompt}` |
| llama-cli | Yes | `{cmd} -m {model} -p {prompt} --no-display-prompt` |
| Custom | Use template | `{cmd} --prompt {prompt}` |

## Project Structure

```
project/
├── pyproject.toml
├── executor.config.yaml
├── src/
│   └── spec_runner/
│       ├── __init__.py
│       ├── executor.py          # Re-exports (backward compat)
│       ├── cli.py               # CLI commands + argparse
│       ├── execution.py         # Task execution + retry logic
│       ├── parallel.py          # Async parallel execution
│       ├── config.py            # ExecutorConfig + YAML loading
│       ├── state.py             # SQLite state persistence
│       ├── prompt.py            # Prompt building + templates
│       ├── hooks.py             # Git ops, code review (parallel 5-role), plugins
│       ├── runner.py            # Subprocess execution + event streaming
│       ├── task.py              # Task parsing + management
│       ├── validate.py          # Config + task validation
│       ├── plugins.py           # Plugin discovery + hooks
│       ├── logging.py           # Structured logging (structlog)
│       ├── events.py            # EventBus for streaming to TUI
│       ├── notifications.py     # Telegram notifications
│       ├── tui.py               # Textual TUI dashboard + live streaming
│       ├── mcp_server.py        # MCP server (FastMCP, stdio)
│       ├── init_cmd.py          # Skill installer
│       └── skills/
│           └── spec-generator-skill/
└── spec/
    ├── tasks.md
    ├── requirements.md
    ├── design.md
    └── plugins/
```

## License

MIT
