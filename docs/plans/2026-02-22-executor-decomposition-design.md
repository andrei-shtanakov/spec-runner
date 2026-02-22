# Design: executor.py Decomposition (Phase 0)

**Date:** 2026-02-22
**Status:** Approved
**Goal:** Split executor.py (2313 lines) into 6 focused modules + add 40-50 tests.
**Constraint:** Pure refactoring — no behavior changes, no new features.

## Module Structure

```
src/spec_runner/
├── executor.py    (~500 lines) — CLI + main loop + retry orchestration
├── config.py      (~300 lines) — ExecutorConfig, YAML loading, build_config
├── state.py       (~200 lines) — ExecutorState, TaskState, TaskAttempt, persistence
├── prompt.py      (~350 lines) — build_task_prompt, templates, error formatting
├── hooks.py       (~600 lines) — pre/post hooks, git ops, code review
├── runner.py      (~200 lines) — build_cli_command, subprocess exec, logging
├── task.py        (unchanged)
└── init_cmd.py    (unchanged)
```

## Dependency Graph

```
executor.py (CLI + orchestration)
  ├── config.py     (no internal deps)
  ├── state.py      (depends on: config)
  ├── runner.py     (depends on: config)
  ├── prompt.py     (depends on: config, state, task)
  ├── hooks.py      (depends on: config, prompt, runner, task)
  └── task.py       (unchanged, no new deps)
```

Rule: dependencies flow top-down only. Leaf modules (config, task) have no
internal deps. executor.py depends on all others.

## Module Contents

### config.py (~300 lines)

Source clusters: 2 (file locking), 3 (config & globals), 5 (config loading)

Contents:
- Constants: `CONFIG_FILE`, `PROGRESS_FILE`, `ERROR_PATTERNS`
- `ExecutorLock` — file-based mutex (fcntl)
- `ExecutorConfig` — dataclass with all fields + `__post_init__` path resolution
- `load_config_from_yaml()` — YAML parsing with nested section extraction
- `build_config(yaml_config, args)` — three-level precedence merge

External deps: `yaml`, `fcntl`, `dataclasses`, `pathlib`

### state.py (~200 lines)

Source cluster: 6

Contents:
- `TaskAttempt` — dataclass (timestamp, success, duration, error, claude_output)
- `TaskState` — dataclass (task_id, status, attempts, started_at, completed_at)
  - Properties: `attempt_count`, `last_error`
- `ExecutorState` — dataclass with JSON persistence
  - Methods: `_load()`, `_save()`, `get_task_state()`, `record_attempt()`,
    `mark_running()`, `should_stop()`
- `check_stop_requested(config)`, `clear_stop_file(config)`

External deps: `json`, `datetime`, `dataclasses`
Internal deps: `config.ExecutorConfig` (for state_file path)

### runner.py (~200 lines)

Source clusters: 1 (logging & callbacks), 4 (CLI command building)

Contents:
- `log_progress(message, task_id=None)` — file + stdout logging
- `check_error_patterns(output)` — API error detection
- `_send_callback(callback_url, task_id, status, ...)` — HTTP POST to orchestrator
- `build_cli_command(cmd, prompt, model, template, ...)` — CLI string building
  with template substitution and auto-detect for claude/codex/ollama/llama

External deps: `subprocess` (for shlex), `json`, `urllib.request`
Internal deps: `config.PROGRESS_FILE`, `config.ERROR_PATTERNS`

### prompt.py (~350 lines)

Source cluster: 7

Contents:
- `PROMPTS_DIR` constant
- `load_prompt_template(name, cli_name="")` — template file lookup
- `_read_template(path)` — file reading with error handling
- `render_template(template, variables)` — {{VAR}} and ${VAR} substitution
- `format_error_summary(error, output, max_lines)` — concise error formatting
- `extract_test_failures(output)` — test output extraction
- `build_task_prompt(task, config, previous_attempts)` — main prompt builder
  with requirement/design extraction, checklist rendering, error context

External deps: `re`, `pathlib`
Internal deps: `config.ExecutorConfig`, `state.TaskAttempt`, `task.Task`

### hooks.py (~600 lines)

Source cluster: 8

Contents:
- `get_task_branch_name(task)` — task/TASK-###-short-name
- `get_main_branch(config)` — auto-detect main branch
- `_ensure_on_main_branch(config)` — switch to main
- `pre_start_hook(task, config)` — uv sync, git branch, cleanup
- `post_done_hook(task, config, success)` — test, lint, review, commit, merge
- `build_review_prompt(task, config, cli_name)` — review prompt construction
- `run_code_review(task, config)` — subprocess review execution

External deps: `subprocess`, `re`
Internal deps: `config.ExecutorConfig`, `prompt.load_prompt_template`,
`prompt.render_template`, `runner.build_cli_command`, `runner.log_progress`,
`runner.check_error_patterns`, `task.Task`

### executor.py (~500 lines, remains as entry point)

Source clusters: 9 (execution), 10 (retry), 11 (CLI commands), 12 (main)

Contents:
- `execute_task(task, config, state)` — orchestrate single task execution
- `run_with_retries(task, config, state)` — retry loop with error forwarding
- `cmd_run()`, `_run_tasks()` — main execution loop
- `cmd_status()`, `cmd_retry()`, `cmd_logs()`, `cmd_stop()`, `cmd_reset()`
- `cmd_plan()` — interactive planning
- `main()` — argparse + dispatch

External deps: `argparse`, `subprocess`, `time`, `sys`, `shutil`
Internal deps: all other modules

## Migration Strategy

Order of creation (leaves to root):

1. **config.py** — extract, update executor imports, run tests
2. **state.py** — extract, update executor imports, run tests
3. **runner.py** — extract, update executor imports, run tests
4. **prompt.py** — extract, update executor imports, run tests
5. **hooks.py** — extract, update executor imports, run tests
6. **executor.py** — verify remaining code, final import cleanup, run tests

Each step: extract code → add imports in executor.py → run existing tests →
verify `spec-runner --help` works.

## Test Plan

| Test file | Module | Count | Coverage |
|---|---|---|---|
| test_config.py | config | ~10 | Path resolution, YAML merge, spec_prefix, CLI override, ExecutorLock |
| test_state.py | state | ~8 | Save/load, recovery from corrupt JSON, attempt recording, consecutive failures, should_stop |
| test_prompt.py | prompt | ~8 | Template rendering, error truncation (30KB), checklist formatting, REQ/DESIGN extraction |
| test_runner.py | runner | ~6 | build_cli_command templates (claude/codex/ollama), error patterns, log_progress |
| test_hooks.py | hooks | ~6 | Branch naming, main branch detection, hook sequence (mock subprocess) |
| test_retry.py | executor | ~6 | Attempt counting, API error passthrough, on_task_failure modes (skip/stop/ask) |
| test_execution.py | executor | ~6 | TASK_COMPLETE/FAILED markers, timeout handling, output parsing |

All tests mock subprocess — no real Claude CLI calls.
Existing test_spec_prefix.py tests must pass throughout.

## Invariants (what does NOT change)

- `spec/.executor-state.json` format
- CLI interface (`spec-runner run/status/plan/retry/logs/stop/reset`)
- Entry points in `pyproject.toml` (`spec_runner.executor:main`)
- `spec/tasks.md` format
- Exit codes and stdout output
- `task.py` — completely untouched

## Preparation for Future Phases

- Phase 1 (SQLite): replace `state.py` persistence layer, tests already cover interface
- Phase 2 (Parallel): make `runner.py` async, add semaphore in executor.py
- Phase 3 (Structured logging): replace `runner.log_progress` with structlog
