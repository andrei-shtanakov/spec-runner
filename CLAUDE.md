# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Active Work & Roadmap

- **Current task list:** `./TODO.md` — read it at the start of every session
- **Ecosystem roadmap (strategic):** `../_cowork_output/roadmap/ecosystem-roadmap.md` — R-01…R-16 across Maestro / arbiter / ATP / spec-runner
- **Latest weekly status:** `../_cowork_output/status/2026-04-10-status.md`
- **Sibling projects** (reference only): `../Maestro/`, `../arbiter/`, `../atp-platform/`, `../proctor-a/`

spec-runner's role in the ecosystem: the only **working** cross-project link (Maestro→spec-runner). Contract stability (`.executor-state.db` SQLite schema, `--json-result` stdout) is the main ecosystem responsibility — see `docs/state-schema.md`, `schemas/*.json`, and `tests/test_json_result_contract.py`. Any breaking change needs a major version bump.

## Project Overview

**spec-runner** (v2.0.0) — Task automation from markdown specs via Claude CLI. Reads structured tasks from `spec/tasks.md`, executes them as Claude CLI subprocesses with retries, code review, Git automation, and hook-based CI-like workflows. Includes post-execution compliance verification and traceability matrix reporting.

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
make test                                  # Run tests (non-slow)
make lint                                  # Lint + format check
make typecheck                             # mypy
make format                                # Auto-format + fix
```

### CLI entry points (defined in pyproject.toml)

```bash
spec-runner run                            # Execute next ready task
spec-runner run --task=TASK-001            # Execute specific task
spec-runner run --all                      # Execute all ready tasks
spec-runner run --dry-run                  # Show what would execute (JSON)
spec-runner run --json-result              # Output structured JSON per task (Maestro interop)
spec-runner status                         # Show execution status
spec-runner status --json                  # JSON status output
spec-runner plan "description"             # Interactive task planning
spec-runner plan --full "description"      # Generate full spec (requirements + design + tasks)
spec-runner validate                       # Validate config and tasks
spec-runner verify                         # Verify post-execution compliance
spec-runner verify --task=TASK-001         # Verify specific task
spec-runner verify --json                  # JSON compliance output
spec-runner verify --strict                # Fail on warnings too
spec-runner report                         # Generate traceability matrix
spec-runner report --milestone=mvp         # Filter by milestone
spec-runner report --uncovered-only        # Show only uncovered requirements
spec-runner report --json                  # JSON matrix output
spec-runner run --tui                      # Execute with live TUI dashboard
spec-runner tui                            # Launch TUI status dashboard
spec-runner run --log-level=DEBUG          # Set log verbosity (DEBUG/INFO/WARNING/ERROR)
spec-runner run --log-json                 # Output logs as JSON (for pipelines)
spec-runner run --all --hitl-review        # Interactive HITL approval gate after code review
spec-runner run --force                    # Skip lock check (use when lock is stale)
spec-runner run --budget=10.0              # Set global budget in USD
spec-runner run --task-budget=2.0          # Set per-task budget in USD
spec-runner costs                          # Cost breakdown per task
spec-runner costs --json                   # JSON output for automation
spec-runner costs --sort=cost              # Sort by cost descending
spec-runner watch                          # Continuously execute ready tasks
spec-runner watch --tui                    # Watch with live TUI dashboard
spec-runner mcp                            # Launch MCP server (stdio)
spec-runner task list --status=todo        # List tasks by status (unified CLI)
spec-runner task next                      # Show next ready tasks
spec-runner task graph                     # ASCII dependency graph
spec-runner task sync-to-gh                # Sync tasks → GitHub Issues
spec-runner task sync-to-gh --dry-run      # Preview without making changes
spec-runner task sync-from-gh              # Sync GitHub Issues → tasks.md
spec-runner-init                           # Install skills to .claude/skills
```

Note: `spec-task` is deprecated. Use `spec-runner task <command>` instead.

## Architecture

### Source Layout

All code is in `src/spec_runner/`:

| Module | Lines | Purpose |
|---|---|---|
| `executor.py` | ~60 | Backward-compatible re-exports, `_shutdown_requested`/`_pause_requested` flags, signal handlers |
| `cli.py` | ~910 | CLI dispatcher, `cmd_run`, `_run_tasks`, `cmd_watch`, `cmd_retry`, `build_task_json_result` (stable Maestro-interop helper), `main()` with argparse |
| `cli_info.py` | ~370 | Info/query commands: `cmd_status`, `cmd_costs`, `cmd_logs`, `cmd_stop`, `cmd_reset`, `cmd_validate`, `cmd_verify`, `cmd_report`, `cmd_tui`, `cmd_mcp` |
| `cli_plan.py` | ~300 | Interactive planning: `cmd_plan` with both interactive and `--full` pipeline modes |
| `execution.py` | ~495 | `execute_task()`, retry strategy (`classify_retry_strategy`, `compute_retry_delay`, `run_with_retries`), Telegram notification on failure |
| `mcp_server.py` | ~270 | MCP server (FastMCP, stdio): status, tasks, costs, logs, run_task, stop, next_tasks, task_detail tools; module-level security note |
| `config.py` | ~440 | ExecutorConfig, Persona, YAML loading, build_config; supports both `spec-runner.config.yaml` (v2.0) and `spec/executor.config.yaml` (legacy); `ExecutorLock` with PID diagnostics |
| `state.py` | ~665 | ExecutorState (context manager), TaskState, TaskAttempt, ErrorCode, ReviewVerdict, RetryContext, SQLite persistence with crash resilience; `_is_disk_full_error()` / `_enter_degraded_mode()` fallback; token fields, `total_cost()`, `task_cost()`, `total_tokens()`, `recover_stale_tasks()` |
| `prompt.py` | ~435 | Prompt building, templates, error formatting, constitution guardrails, persona injection, `build_generation_prompt()`, `parse_spec_marker()`, `SPEC_STAGES` |
| `hooks.py` | ~470 | Pre/post hook orchestration, plugin hook integration |
| `git_ops.py` | ~150 | Git operations: branch creation, main branch detection, `ensure_on_main_branch`, test file mapping |
| `review.py` | ~480 | Code review: `REVIEW_ROLES` (5 roles), `build_review_prompt`, `run_code_review`, `run_parallel_review`, HITL approval gate |
| `runner.py` | ~290 | CLI command building, subprocess exec with graceful termination (SIGTERM→SIGKILL), progress logging; `parse_token_usage()`, `run_claude_async()` |
| `task.py` | ~380 | Task dataclass, regex parsing, dependency resolution, status management |
| `task_commands.py` | ~440 | Task CLI commands: list, show, start, done, block, check, stats, next, graph |
| `github_sync.py` | ~200 | GitHub Issues sync: `cmd_sync_to_gh` (local wins), `cmd_sync_from_gh` (remote wins), `export_gh` |
| `verify.py` | ~230 | Post-execution compliance verification: traceability check, coverage, review verdicts |
| `report.py` | ~220 | Traceability matrix: REQ → DESIGN → TASK → execution state mapping |
| `validate.py` | ~335 | Config + task validation: duplicate IDs, symmetry checks, cycle detection, dead config warnings |
| `plugins.py` | ~270 | Plugin discovery, hook execution, env var building |
| `logging.py` | ~100 | Structured logging via structlog: `setup_logging()`, `get_logger()`, JSON/console output |
| `events.py` | ~70 | `EventBus` with asyncio.Queue subscribers + thread-safe recent buffer for TUI streaming; `TaskEvent` dataclass |
| `notifications.py` | ~195 | Telegram + generic webhook notifications: `send_telegram()`, `send_webhook()`, `notify()`, template rendering; emits `task_failed`, `run_complete`, `state_degraded` events |
| `tui.py` | ~505 | Textual-based TUI: live task dashboard, Kanban columns, log panel with streaming events, pause keybinding |
| `init_cmd.py` | ~100 | Install bundled Claude Code skills |

Entry points (pyproject.toml): `spec-runner` → `executor:main`, `spec-task` → `task_commands:main` (deprecated), `spec-runner-init` → `init_cmd:main`

### Key Data Flow

1. `task.py:parse_tasks()` — Regex-parses `spec/tasks.md` into `Task` dataclass objects
2. `task.py:resolve_dependencies()` — Resolves dependency graph, auto-promotes blocked→todo
3. `task.py:get_next_tasks()` — Returns ready tasks (in_progress first, then todo by priority)
4. `prompt.py:build_task_prompt()` — Generates prompt with task context, requirements, design refs, previous errors
5. `execution.py:execute_task()` — Runs Claude CLI as subprocess, detects `TASK_COMPLETE`/`TASK_FAILED` markers
6. `execution.py:run_with_retries()` — Retry loop with error context forwarding between attempts
7. `hooks.py`: `pre_start_hook()` (git branch, uv sync) → execution → `post_done_hook()` (tests, lint, review, commit, merge)
8. `events.py:EventBus` — Optional streaming: `run_claude_async()` publishes stdout lines as `TaskEvent`s; TUI drains them on refresh
9. `notifications.py:notify()` — Sends Telegram/webhook notifications on `task_failed` and `run_complete` events (if configured)

### Key Classes

- **`ExecutorConfig`** — Dataclass merging YAML config + CLI args. Handles `spec_prefix` path resolution for multi-phase projects. Includes `personas` (dict of `Persona` for role-specific prompts/models), `review_parallel`, `review_roles`, `webhook_url/method/headers/template`, `notify_on` (defaults to `[run_complete, task_failed, state_degraded]`).
- **`Persona`** — Agent persona with `system_prompt`, `model`, `focus` fields for phase-specific customization (architect, implementer, reviewer, qa).
- **`ExecutorState`** / **`TaskState`** / **`TaskAttempt`** — Execution state persisted to SQLite (`spec/.executor-state.db`) with WAL mode + busy_timeout. Auto-migrates from legacy JSON on first run. `ExecutorState` is a context manager. Degraded-mode fallback: when SQLite writes fail (disk-full, corruption), `state.degraded` / `state.degraded_reason` flip true, the in-memory state keeps serving the run, and operators are notified once via `state_degraded`.
- **`ErrorCode`** — `str` enum classifying failures: TIMEOUT, RATE_LIMIT, TEST_FAILURE, LINT_FAILURE, TASK_FAILED, HOOK_FAILURE, BUDGET_EXCEEDED, REVIEW_REJECTED, INTERRUPTED, UNKNOWN. Stored in `attempts.error_code` column.
- **`ReviewVerdict`** — `str` enum for code review outcomes: PASSED, FIXED, FAILED, SKIPPED, REJECTED. Stored in `attempts.review_status` column.
- **`RetryContext`** — Structured retry info (attempt number, error code, previous error, test failures) passed to `build_task_prompt()` for focused retry prompts.
- **`Task`** — Parsed task with id, priority (p0-p3), status (todo/in_progress/done/blocked), description, checklist, dependency graph, traceability to `[REQ-XXX]`/`[DESIGN-XXX]`.
- **`ValidationResult`** — Validation outcome with errors and warnings lists, `ok` property. Checks duplicate IDs, blocks/depends_on symmetry.
- **`PluginInfo`** / **`PluginHook`** — Plugin metadata and hook configuration from `spec/plugins/*/plugin.yaml`.
- **`EventBus`** / **`TaskEvent`** — Pub/sub event streaming for TUI. Thread-safe `drain_recent()` for cross-thread consumption.
- **`VerifyResult`** / **`VerificationReport`** — Compliance check results per task and overall coverage.
- **`TraceRow`** / **`TraceabilityReport`** — Traceability matrix mapping REQ → DESIGN → TASK → execution state.

### Configuration Precedence

`ExecutorConfig` defaults → `spec-runner.config.yaml` (v2.0, project root) or `spec/executor.config.yaml` (legacy v1.x) → CLI arguments (highest priority)

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
- **mcp** — Model Context Protocol server (FastMCP, stdio transport)

## File Locations

- **Specs**: `spec/` (requirements.md, design.md, tasks.md, FORMAT.md, WORKFLOW.md, prompts/)
- **Config**: `spec-runner.config.yaml` at project root (v2.0) or `spec/executor.config.yaml` (legacy v1.x, deprecated)
- **Runtime state**: `spec/.executor-state.db` (SQLite + WAL), `spec/.executor-logs/`, `spec/.task-history.log`
- **Bundled skills**: `src/spec_runner/skills/spec-generator-skill/` (templates + review prompts for claude/codex/ollama/llama)
- **Plugins**: `spec/plugins/` (optional; each plugin is a directory with `plugin.yaml`)
- **Interop contract**: `docs/state-schema.md` + `schemas/executor-state.schema.json` + `schemas/json-result.schema.json` + `tests/fixtures/maestro-interop/` (golden fixtures copied by Maestro's contract tests)
- **Tests**: `tests/` — group by CLI module, mark slow tests with `@pytest.mark.slow`, mock Claude CLI invocations

## Testing

Tests use pytest (552 collected, ~546 non-slow). Test files: `test_config.py`, `test_costs.py`, `test_e2e.py`, `test_events.py`, `test_execution.py`, `test_gh_sync.py` (includes gh-sync conflict/idempotency tests), `test_hooks.py`, `test_json_result_contract.py` (pins the Maestro `--json-result` contract with golden fixtures), `test_logging.py`, `test_mcp.py`, `test_notifications.py`, `test_plan_full.py`, `test_plugins.py`, `test_prompt.py`, `test_report.py`, `test_runner.py`, `test_spec_prefix.py` (includes multi-phase E2E coverage), `test_state.py` (includes `TestDegradedMode` for disk-full fallback), `test_tui.py`, `test_validate.py`, `test_verify.py`, `test_watch.py`. Shared pytest config in `tests/conftest.py` (adds `--update-golden` for fixture regeneration). E2E tests use `tests/fixtures/fake_claude.sh` as a mock Claude CLI and are marked with `@pytest.mark.slow`. Mock subprocess/CLI calls to keep runs fast. Regression tests required for bug fixes.
