# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**spec-runner** (v0.3.0) — Task automation from markdown specs via Claude CLI. Reads structured tasks from `spec/tasks.md`, executes them as Claude CLI subprocesses with retries, code review, Git automation, and hook-based CI-like workflows.

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
spec-runner validate                       # Validate config and tasks
spec-runner plan --full "description"      # Generate full spec (requirements + design + tasks)
spec-runner run --all --parallel           # Execute ready tasks in parallel
spec-runner run --all --parallel --max-concurrent=5  # With concurrency limit
spec-runner run --tui                      # Execute with live TUI dashboard
spec-runner tui                            # Launch TUI status dashboard
spec-runner run --log-level=DEBUG          # Set log verbosity (DEBUG/INFO/WARNING/ERROR)
spec-runner run --log-json                 # Output logs as JSON (for pipelines)
spec-runner run --all --hitl-review        # Interactive HITL approval gate after code review
spec-runner run --force                    # Skip lock check (use when lock is stale)
spec-runner costs                          # Cost breakdown per task
spec-runner costs --json                   # JSON output for automation
spec-runner costs --sort=cost              # Sort by cost descending
spec-runner watch                          # Continuously execute ready tasks
spec-runner watch --tui                    # Watch with live TUI dashboard
spec-runner-init                           # Install skills to .claude/skills
```

## Architecture

### Source Layout

All code is in `src/spec_runner/`:

| Module | Lines | Purpose |
|---|---|---|
| `executor.py` | ~1850 | CLI entry point, main loop, retry orchestration, `_run_tasks_parallel()`, `_execute_task_async()`, budget checks, signal handling, crash recovery wiring, `classify_retry_strategy()`, `compute_retry_delay()`, `cmd_costs()`, `cmd_watch()` |
| `config.py` | ~320 | ExecutorConfig, YAML loading, build_config; `max_concurrent`, `budget_usd`, `task_budget_usd` fields; `ExecutorLock` with PID diagnostics |
| `state.py` | ~560 | ExecutorState (context manager), TaskState, TaskAttempt, ErrorCode, RetryContext, SQLite persistence; token fields, `total_cost()`, `task_cost()`, `total_tokens()`, `recover_stale_tasks()` |
| `prompt.py` | ~420 | Prompt building, templates, error formatting, `build_generation_prompt()`, `parse_spec_marker()`, `SPEC_STAGES` |
| `hooks.py` | ~790 | Pre/post hooks, git ops, enriched code review (full diff, checklist, test/lint output), HITL approval gate, plugin hook integration |
| `runner.py` | ~240 | CLI command building, subprocess exec, progress logging; `parse_token_usage()`, `run_claude_async()` |
| `task.py` | ~780 | Task parsing, dependency resolution, status management |
| `validate.py` | ~310 | Config + task validation, CLI command, pre-run checks |
| `plugins.py` | ~260 | Plugin discovery, hook execution, env var building |
| `logging.py` | ~100 | Structured logging via structlog: `setup_logging()`, `get_logger()`, JSON/console output |
| `tui.py` | ~490 | Textual-based TUI: live task dashboard, progress bars, log panel |
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
8. `executor.py:_run_tasks_parallel()` — Parallel execution path: runs multiple ready tasks concurrently via `asyncio` with semaphore-based concurrency limiting and budget checks

### Key Classes

- **`ExecutorConfig`** — Dataclass merging YAML config + CLI args. Handles `spec_prefix` path resolution for multi-phase projects.
- **`ExecutorState`** / **`TaskState`** / **`TaskAttempt`** — Execution state persisted to SQLite (`spec/.executor-state.db`) with WAL mode. Auto-migrates from legacy JSON on first run. `ExecutorState` is a context manager (`with ExecutorState(config) as state:`) for guaranteed cleanup. `recover_stale_tasks()` detects and recovers tasks stuck in 'running' from previous crashes.
- **`ErrorCode`** — `str` enum classifying failures: TIMEOUT, RATE_LIMIT, SYNTAX, TEST_FAILURE, LINT_FAILURE, TASK_FAILED, HOOK_FAILURE, BUDGET_EXCEEDED, REVIEW_REJECTED, INTERRUPTED, UNKNOWN. Stored in `attempts.error_code` column.
- **`ReviewVerdict`** — `str` enum for code review outcomes: PASSED, FIXED, FAILED, SKIPPED, REJECTED. Stored in `attempts.review_status` column.
- **`RetryContext`** — Structured retry info (attempt number, error code, previous error, test failures) passed to `build_task_prompt()` for focused retry prompts.
- **`Task`** — Parsed task with id, priority (p0-p3), status (todo/in_progress/done/blocked), checklist, dependency graph, traceability to `[REQ-XXX]`/`[DESIGN-XXX]`.
- **`ValidationResult`** — Validation outcome with errors and warnings lists, `ok` property.
- **`PluginInfo`** / **`PluginHook`** — Plugin metadata and hook configuration from `spec/plugins/*/plugin.yaml`.

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

## Key Dependencies

- **PyYAML** — YAML config loading
- **structlog** — Structured logging (JSON + console renderers)
- **textual** — Terminal UI dashboard for live task monitoring

## File Locations

- **Specs**: `spec/` (requirements.md, design.md, tasks.md, WORKFLOW.md)
- **Config**: `executor.config.yaml` at repo root
- **Runtime state**: `spec/.executor-state.db` (SQLite + WAL), `spec/.executor-logs/`, `spec/.task-history.log`
- **Bundled skills**: `src/spec_runner/skills/spec-generator-skill/` (templates + review prompts for claude/codex/ollama/llama)
- **Plugins**: `spec/plugins/` (each plugin is a directory with `plugin.yaml`)
- **Tests**: `tests/` — group by CLI module, mark slow tests with `@pytest.mark.slow`, mock Claude CLI invocations

## Testing

Tests use pytest (376 tests). Test files: `test_config.py`, `test_state.py`, `test_runner.py`, `test_prompt.py`, `test_hooks.py`, `test_execution.py`, `test_spec_prefix.py`, `test_logging.py`, `test_tui.py`, `test_validate.py`, `test_plugins.py`, `test_plan_full.py`, `test_e2e.py`. E2E tests use `tests/fixtures/fake_claude.sh` as a mock Claude CLI and are marked with `@pytest.mark.slow`. Mock subprocess/CLI calls to keep runs fast. Regression tests required for bug fixes.
