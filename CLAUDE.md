# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**spec-runner** (v0.1.6) — Task automation from markdown specs via Claude CLI. Reads structured tasks from `spec/tasks.md`, executes them as Claude CLI subprocesses with retries, code review, Git automation, and hook-based CI-like workflows.

## Build & Development Commands

```bash
uv sync                                    # Install all dependencies
uv run pytest tests/ -v                    # Run all tests
uv run pytest tests/ -v -m "not slow"      # Skip slow tests
uv run pytest tests/test_spec_prefix.py::TestExecutorConfigDefaults  # Single test class
uv run ruff check .                        # Lint
uv run ruff check . --fix                  # Lint auto-fix
uv run ruff format .                       # Format
uv run mypy src                            # Type check (mypy)
pyrefly check                              # Type check (pyrefly)
```

### CLI entry points (defined in pyproject.toml)

```bash
spec-runner run                            # Execute next ready task
spec-runner run --task=TASK-001            # Execute specific task
spec-runner run --all                      # Execute all ready tasks
spec-runner status                         # Show execution status
spec-runner plan "description"             # Interactive task planning
spec-task list --status=todo               # List tasks by status
spec-task next                             # Show next ready tasks
spec-task graph                            # ASCII dependency graph
spec-runner-init                           # Install skills to .claude/skills
```

## Architecture

### Source Layout

All code is in `src/spec_runner/`:

| Module | Lines | Purpose |
|---|---|---|
| `executor.py` | ~900 | CLI entry point, main loop, retry orchestration |
| `config.py` | ~260 | ExecutorConfig, YAML loading, build_config |
| `state.py` | ~320 | ExecutorState, TaskState, TaskAttempt, ErrorCode, RetryContext, SQLite persistence |
| `prompt.py` | ~320 | Prompt building, templates, error formatting |
| `hooks.py` | ~580 | Pre/post hooks, git ops, code review |
| `runner.py` | ~160 | CLI command building, subprocess exec, progress logging |
| `task.py` | ~780 | Task parsing, dependency resolution, status management |
| `init_cmd.py` | ~100 | Install bundled Claude Code skills |

Entry points (pyproject.toml): `spec-runner` → `executor:main`, `spec-task` → `task:main`, `spec-runner-init` → `init_cmd:main`

### Key Data Flow

1. `task.py:parse_tasks()` — Regex-parses `spec/tasks.md` into `Task` dataclass objects
2. `task.py:resolve_dependencies()` — Resolves dependency graph, auto-promotes blocked→todo
3. `task.py:get_next_tasks()` — Returns ready tasks (in_progress first, then todo by priority)
4. `prompt.py:build_task_prompt()` — Generates prompt with task context, requirements, design refs, previous errors
5. `executor.py:execute_task()` — Runs Claude CLI as subprocess, detects `TASK_COMPLETE`/`TASK_FAILED` markers
6. `executor.py:run_with_retries()` — Retry loop with error context forwarding between attempts
7. `hooks.py`: `pre_start_hook()` (git branch, uv sync) → execution → `post_done_hook()` (tests, lint, review, commit, merge)

### Key Classes

- **`ExecutorConfig`** — Dataclass merging YAML config + CLI args. Handles `spec_prefix` path resolution for multi-phase projects.
- **`ExecutorState`** / **`TaskState`** / **`TaskAttempt`** — Execution state persisted to SQLite (`spec/.executor-state.db`) with WAL mode. Auto-migrates from legacy JSON on first run.
- **`ErrorCode`** — `str` enum classifying failures: TIMEOUT, RATE_LIMIT, SYNTAX, TEST_FAILURE, LINT_FAILURE, TASK_FAILED, HOOK_FAILURE, UNKNOWN. Stored in `attempts.error_code` column.
- **`RetryContext`** — Structured retry info (attempt number, error code, previous error, test failures) passed to `build_task_prompt()` for focused retry prompts.
- **`Task`** — Parsed task with id, priority (p0-p3), status (todo/in_progress/done/blocked), checklist, dependency graph, traceability to `[REQ-XXX]`/`[DESIGN-XXX]`.

### Configuration Precedence

`ExecutorConfig` defaults → `executor.config.yaml` → CLI arguments (highest priority)

### Multi-phase Support

`--spec-prefix=phase2-` namespaces all paths: `phase2-tasks.md`, `phase2-requirements.md`, `.executor-phase2-state.db`, etc.

## Code Style

- Python 3.10+, Ruff line length **100** (not 88 — configured in pyproject.toml)
- Ruff rules: E, F, W, I, UP, B, C4, SIM (E501 ignored)
- Type annotations required everywhere; mypy strict mode
- Git branches follow `task/TASK-###-short-name` pattern
- Config keys: `lowercase_with_underscores` matching YAML convention

## File Locations

- **Specs**: `spec/` (requirements.md, design.md, tasks.md, WORKFLOW.md)
- **Config**: `executor.config.yaml` at repo root
- **Runtime state**: `spec/.executor-state.db` (SQLite + WAL), `spec/.executor-logs/`, `spec/.task-history.log`
- **Bundled skills**: `src/spec_runner/skills/spec-generator-skill/` (templates + review prompts for claude/codex/ollama/llama)
- **Tests**: `tests/` — group by CLI module, mark slow tests with `@pytest.mark.slow`, mock Claude CLI invocations

## Testing

Tests use pytest (167 tests). Test files: `test_config.py`, `test_state.py`, `test_runner.py`, `test_prompt.py`, `test_hooks.py`, `test_execution.py`, `test_spec_prefix.py`. Mock subprocess/CLI calls to keep runs fast. Regression tests required for bug fixes.
