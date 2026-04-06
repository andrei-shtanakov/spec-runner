# spec-runner Development Roadmap

**Date**: 2026-04-06
**Status**: Approved
**Strategy**: Variant C (Specialize) — spec-driven development pipeline

## Context

spec-runner v1.1.0 is a CLI tool that reads structured markdown task files and executes them via Claude CLI with retries, code review, git automation, and hook-based workflows. The project has been audited across 6 dimensions (code quality, task format, execution engine, CLI/UX, testing/reliability, positioning). Key findings:

- Sequential execution is production-ready (8/10)
- Parallel mode has 3 critical bugs (race condition, blocking hooks, git clean destruction) and 0 concurrent tests
- 8 critical/medium bugs across modules
- Unique strengths: 5-role review, traceability (REQ->DESIGN->TASK), HITL, GitHub Issues sync
- Strategic recommendation: specialize as spec-driven development tool, not compete with Maestro on orchestration

## Design Principles

1. **Spec-driven identity**: spec-runner's value is the full pipeline `requirements.md -> design.md -> tasks.md -> code -> review -> verify -> report`. Everything serves this pipeline.
2. **Sequential-first**: Sequential execution is the primary mode. Parallel is experimental — Maestro handles production parallel execution.
3. **Markdown-first**: Human-readable specs that render in GitHub without tooling. This is the core differentiator.
4. **Milestone releases**: Each release is a working product with clear value. Tech debt is embedded in milestones, not separated.

## Audit Sources

All findings referenced below come from `_cowork_output/` audit reports:

| Report | Focus |
|--------|-------|
| `01-code-quality.md` | Code quality: 42 legacy traces, 19 high-CC functions, error handling |
| `02-task-format.md` | Task format: regex parsing, missing fields, validation gaps |
| `03-execution-engine.md` | Execution: race conditions, blocking hooks, retry strategy |
| `04-cli-ux.md` | CLI/UX: TUI crash, dead code, MCP, notifications, config |
| `05-testing-reliability.md` | Testing: 488 tests, parallel untested, crash resilience |
| `06-positioning-roadmap.md` | Strategy: spec-runner vs Maestro, variant analysis |

---

## v1.2 "Stable"

**Goal**: Close all known bugs, stabilize CLI, make sequential path fully production-ready. After v1.2 the user can confidently use spec-runner for real projects.

**Estimated effort**: ~18 hours (2-3 working days)

### QW-1: Fix TUI crash — LogPanel.add_line()

- **Problem**: Method called at tui.py:311 and tui.py:493 but never defined. TUI crashes with AttributeError on event streaming or pause.
- **Source**: 04-cli-ux.md section 2.2
- **Fix**: Define `add_line(self, text: str) -> None` in LogPanel class
- **Files**: `src/spec_runner/tui.py`
- **Effort**: 10 min
- **Tests**: Add test for add_line in test_tui.py

### QW-2: SQLite PRAGMA busy_timeout

- **Problem**: No busy_timeout set. Concurrent writes return SQLITE_BUSY immediately without retry. Default is 0ms.
- **Source**: 05-testing-reliability.md section 2.1
- **Fix**: Add `self._conn.execute("PRAGMA busy_timeout=30000")` after connect
- **Files**: `src/spec_runner/state.py:135`
- **Effort**: 1 min
- **Tests**: Existing tests sufficient

### QW-3: Fix config path in cmd_validate

- **Problem**: `cmd_validate` builds path as `config.project_root / "executor.config.yaml"` (without `spec/` prefix), but CONFIG_FILE is `Path("spec/executor.config.yaml")`. Validation cannot find config.
- **Source**: 04-cli-ux.md section 5.2
- **Fix**: Use `CONFIG_FILE` constant instead of hardcoded path
- **Files**: `src/spec_runner/cli.py:949`
- **Effort**: 5 min
- **Tests**: Add test for validate with correct path

### QW-4: Fix logger names

- **Problem**: cli.py and parallel.py use `get_logger("executor")` instead of module-specific names. Log filtering by module is impossible.
- **Source**: 01-code-quality.md section 1.1
- **Fix**: `get_logger("cli")` in cli.py:55, `get_logger("parallel")` in parallel.py:42
- **Files**: `src/spec_runner/cli.py:55`, `src/spec_runner/parallel.py:42`
- **Effort**: 5 min
- **Tests**: Existing logging tests sufficient

### QW-5: Add --budget / --task-budget to argparse

- **Problem**: `build_config` handles `args.budget` (config.py:378), but argument is not defined in argparse. Dead code — budget cannot be set from CLI.
- **Source**: 04-cli-ux.md section 1.2
- **Fix**: Add `--budget` and `--task-budget` arguments to execution_args group
- **Files**: `src/spec_runner/cli.py` (argparse section)
- **Effort**: 15 min
- **Tests**: Add test for budget args in config building

### QW-6: Silent except:pass -> logging

- **Problem**: runner.py:135 and hooks.py:142 silently swallow exceptions. Impossible to diagnose issues.
- **Source**: 01-code-quality.md section 3.1
- **Fix**: Replace `pass` with `logger.debug("...", exc_info=True)`
- **Files**: `src/spec_runner/runner.py:135`, `src/spec_runner/hooks.py:142`
- **Effort**: 10 min

### QW-7: ErrorCode.SYNTAX — remove dead enum value

- **Problem**: Defined in enum but never assigned anywhere. Dead code.
- **Source**: 01-code-quality.md section 3.3
- **Fix**: Remove `SYNTAX` from ErrorCode enum. If syntax error classification is needed later, re-add with actual detection logic.
- **Files**: `src/spec_runner/state.py`
- **Effort**: 5 min
- **Tests**: Update any tests referencing SYNTAX

### QW-8: ErrorCode.BUDGET_EXCEEDED — record attempt

- **Problem**: Budget check in execution.py:401 sets task to "blocked" but does not record an attempt with BUDGET_EXCEEDED. Budget exhaustion events are invisible in history.
- **Source**: 01-code-quality.md section 3.3, 03-execution-engine.md section 1.3
- **Fix**: Call `state.record_attempt()` with `ErrorCode.BUDGET_EXCEEDED` before setting blocked status
- **Files**: `src/spec_runner/execution.py:400-409`
- **Effort**: 15 min
- **Tests**: Add test for budget exceeded attempt recording

### STABLE-1: Parallel mode -> experimental

- **Problem**: Parallel mode has 3 critical bugs: race condition in tasks.md writes (task.py:209-250), blocking hooks in async context (parallel.py:62,164), git clean destroys files of other parallel tasks (hooks.py:186-204). Zero concurrent execution tests.
- **Source**: 03-execution-engine.md sections 5.1-5.3, 05-testing-reliability.md section 1.3
- **Fix**: Add warning on `--parallel` flag: "Warning: parallel mode is experimental. Known limitations: shared worktree conflicts, blocking hooks. For production parallel execution, consider using Maestro." Document in README under Limitations section.
- **Files**: `src/spec_runner/cli.py`, `README.md`
- **Effort**: 2 hours

### STABLE-2: FORMAT.md — formal task format specification

- **Problem**: Task format defined only by 6 regex patterns in task.py:37-44. No formal spec. Invalid markdown silently ignored. Duplicate TASK IDs silently overwritten.
- **Source**: 02-task-format.md sections 1.1-1.4
- **Fix**:
  - Create `spec/FORMAT.md` documenting all fields, regex patterns, optionality, defaults, edge cases
  - Add duplicate TASK ID validation in `validate.py`
  - Add blocks/depends_on symmetry validation (warning level)
- **Files**: `spec/FORMAT.md` (new), `src/spec_runner/validate.py`
- **Effort**: 4 hours
- **Tests**: Add validation tests for duplicate IDs and symmetry

### STABLE-3: CLI stabilization

- **Problem (--dry-run)**: No way to preview which tasks will execute without actually running them. Critical for `--all`.
- **Problem (--json status)**: `costs` has `--json` but `status` does not. Inconsistent.
- **Problem (build_config)**: Argparse default values conflated with explicit user values. `--max-retries=3` (default) does not override YAML `max_retries: 5`.
- **Source**: 04-cli-ux.md sections 1.2, 1.3, 5.4
- **Fix**:
  - Add `--dry-run` flag to `cmd_run`: parse tasks, resolve deps, print plan, exit
  - Add `--json` flag to `cmd_status`: output JSON matching MCP status format
  - Fix `build_config`: use `default=None` for numeric args, check `is not None`
- **Files**: `src/spec_runner/cli.py`
- **Effort**: 4 hours
- **Tests**: Tests for dry-run output, JSON status format, config precedence

### STABLE-4: Crash resilience — disk full / corrupted DB

- **Problem**: `record_attempt()`, `mark_running()`, `_save()` have no try-except for `sqlite3.OperationalError`. Disk full or corrupted DB causes unhandled crash.
- **Source**: 05-testing-reliability.md section 3.3
- **Fix**:
  - Wrap SQLite write operations in try-except for `sqlite3.OperationalError`
  - Log error, set task to failed state in-memory, continue execution
  - Narrow `contextlib.suppress(Exception)` in tui.py to `sqlite3.OperationalError`
- **Files**: `src/spec_runner/state.py:397,432,304`, `src/spec_runner/tui.py:313`
- **Effort**: 3 hours
- **Tests**: Add tests for OperationalError handling

### STABLE-5: CI and developer experience

- **Problem**: mypy strict mode declared in CLAUDE.md but not in CI. No pre-commit config. No Makefile.
- **Source**: 05-testing-reliability.md section 4.4
- **Fix**:
  - Add mypy job to `.github/workflows/ci.yml`
  - Create `.pre-commit-config.yaml` (ruff check + ruff format)
  - Create `Makefile` with targets: `test`, `lint`, `typecheck`, `format`, `e2e`
- **Files**: `.github/workflows/ci.yml`, `.pre-commit-config.yaml` (new), `Makefile` (new)
- **Effort**: 2 hours

### v1.2 Deferred Items

Items identified in audits but intentionally deferred to later releases:

| Item | Why deferred | Target |
|------|-------------|--------|
| Race condition tasks.md parallel writes | Parallel is experimental | v2.0 |
| Blocking hooks in async context | Parallel is experimental | v2.0 |
| Git clean destroys parallel task files | Parallel is experimental | v2.0 |
| Per-task budget check in parallel path | Parallel is experimental | v2.0 |
| Return type annotations (35+ functions) | Volume; needs mypy in CI first | v1.3 |
| Module decomposition (cli.py, hooks.py, task.py) | Refactoring risk, no user value | v2.0 |
| Unified CLI (merge spec-runner + spec-task) | Breaking change | v2.0 |
| Config migration (executor -> spec-runner naming) | Breaking change | v2.0 |
| MCP write operations | Needs stable sequential first | v1.3 |
| Generic webhook notifications | Telegram sufficient for now | v2.0 |
| Description parsing in tasks.md | Depends on FORMAT.md | v1.3 |
| Graceful subprocess termination | Not a blocker | v1.3 |
| spec-runner verify / report | New features, not stabilization | v1.3 |
| Maestro interop contract | Depends on --json | v2.0 |
| Dead config sections (execution_order, skip_tasks, environment) | Decision needed | v1.3 |

---

## v1.3 "Spec Pipeline"

**Goal**: Implement features that differentiate spec-runner from all competitors — the full spec -> code -> verify -> report pipeline. Plus close deferred tech debt from v1.2.

**Estimated effort**: ~2 weeks

### PIPE-1: `spec-runner verify` — post-execution compliance check

- **Motivation**: No tool in the ecosystem checks whether executed code actually satisfies the original spec. This is spec-runner's unique opportunity.
- **Design**:
  - Parse traceability links from done tasks: `[REQ-XXX]` -> search in requirements.md, `[DESIGN-XXX]` -> search in design.md
  - Check execution state from SQLite: tests passed, lint clean, review verdict
  - Cross-reference: for each traced requirement, verify the task that covers it completed successfully
  - Generate compliance report: covered requirements, uncovered requirements, partial coverage
  - Exit code: 0 if all traced requirements covered, 1 if gaps exist
- **CLI**:
  ```
  spec-runner verify                    # Check all done tasks
  spec-runner verify --task=TASK-001    # Check specific task
  spec-runner verify --json             # Machine-readable output
  spec-runner verify --strict           # Fail on warnings too
  ```
- **New module**: `src/spec_runner/verify.py` (~200-300 lines)
- **Files**: `src/spec_runner/verify.py` (new), `src/spec_runner/cli.py`
- **Effort**: 3 days
- **Dependencies**: STABLE-2 (FORMAT.md)
- **Tests**: Verify with full traceability, partial coverage, no traceability, JSON output

### PIPE-2: `spec-runner report` — traceability matrix

- **Motivation**: Enterprise/audit use case. Show the complete chain from requirement to deployed code with review evidence.
- **Design**:
  - Parse all requirements from requirements.md (REQ-XXX identifiers)
  - Parse all design decisions from design.md (DESIGN-XXX identifiers)
  - Map: REQ -> DESIGN -> TASK -> execution state (from SQLite)
  - Output traceability matrix as markdown table:
    ```
    | Requirement | Design | Task | Status | Duration | Cost | Review |
    |-------------|--------|------|--------|----------|------|--------|
    | REQ-001     | DESIGN-003 | TASK-005 | done | 2m31s | $0.12 | PASSED |
    | REQ-002     | DESIGN-007 | TASK-008 | in_progress | — | $0.04 | — |
    | REQ-003     | — | — | not covered | — | — | — |
    ```
  - Coverage metric: `X/Y requirements covered (Z%)`
  - Filters: `--milestone`, `--status`, `--uncovered-only`
  - Formats: markdown (default), `--json`
- **New module**: `src/spec_runner/report.py` (~200-300 lines)
- **Files**: `src/spec_runner/report.py` (new), `src/spec_runner/cli.py`
- **Effort**: 3 days
- **Dependencies**: PIPE-1 (verify)
- **Tests**: Report with full matrix, empty project, uncovered filter, JSON output

### PIPE-3: MCP write operations

- **Motivation**: Read-only MCP is just `spec-runner status --json` with extra steps. Write operations enable IDE workflows: trigger task execution from Claude Code / Cursor.
- **Design**: Add 4 tools to `mcp_server.py`:

  | Tool | Parameters | Behavior |
  |------|-----------|----------|
  | `spec_runner_run_task` | `task_id: str` | Spawn `spec-runner run --task=ID` subprocess, return immediately with status |
  | `spec_runner_stop` | none | Create stop file, return confirmation |
  | `spec_runner_next_tasks` | `spec_prefix?: str` | Return list of ready tasks (parsed + resolved) |
  | `spec_runner_task_detail` | `task_id: str` | Return full task info: checklist, attempts, review verdicts, cost |

- **Security note**: `run_task` executes arbitrary code via Claude CLI. This is acceptable for local stdio MCP (same trust boundary as terminal). Document this. Do not add these tools if/when HTTP transport is added without auth.
- **Files**: `src/spec_runner/mcp_server.py`
- **Effort**: 4 hours
- **Tests**: Handler tests for each new tool, error cases (unknown task, already running)

### PIPE-4: Quality improvements

#### Return type annotations (35+ functions)

- **Source**: 01-code-quality.md section 4.2
- **Scope**: Add return type annotations to all public functions in hooks.py, execution.py, cli.py, parallel.py, state.py, task.py
- **Priority order**: hooks.py (6 missing) -> execution.py (3) -> state.py (2) -> cli.py (9) -> task.py (6+) -> parallel.py (1)
- **Files**: All source modules
- **Effort**: 4 hours

#### Parse description from tasks.md

- **Source**: 02-task-format.md section 1.2
- **Problem**: `description` field exists in Task dataclass but parser never fills it. Text between task header and checklist/metadata is silently ignored.
- **Fix**: Capture lines between TASK_HEADER and first metadata/checklist marker as description. ~10 lines in `parse_tasks()`.
- **Files**: `src/spec_runner/task.py:81-187`
- **Effort**: 1 hour
- **Tests**: Parse task with description, without description, multiline description

#### Graceful subprocess termination

- **Source**: 03-execution-engine.md section 7 (item 6)
- **Problem**: On timeout, `proc.kill()` sends SIGKILL without graceful shutdown. Claude CLI may corrupt files mid-write.
- **Fix**: `proc.terminate()` (SIGTERM) -> wait 5s -> `proc.kill()` (SIGKILL) if still running
- **Files**: `src/spec_runner/runner.py:275-277`
- **Effort**: 1 hour
- **Tests**: Test graceful termination path, test fallback to SIGKILL

#### Dead config sections decision

- **Source**: 04-cli-ux.md section 5.3
- **Problem**: `execution_order`, `skip_tasks`, `environment` defined in YAML but not processed by `load_config_from_yaml()`.
- **Fix**: Remove dead sections from example config. Add validation warning if user has them. If any are useful, implement — but likely dead code.
- **Files**: `executor.config.yaml`, `src/spec_runner/validate.py`
- **Effort**: 1 hour

### v1.3 Deferred Items

| Item | Why deferred | Target |
|------|-------------|--------|
| Module decomposition | Needs unified CLI decision first | v2.0 |
| Unified CLI | Breaking change | v2.0 |
| Config rename | Breaking change | v2.0 |
| Generic webhook | Telegram sufficient | v2.0 |
| Maestro interop | Depends on unified CLI | v2.0 |
| Parallel mode fixes | Experimental, deferred by design | v2.0 |

---

## v2.0 "Polish"

**Goal**: Accumulated breaking changes in one release. Clean API, unified CLI, proper naming. After v2.0 spec-runner is a mature tool with clear identity.

**Estimated effort**: ~3 weeks

### POLISH-1: Unified CLI

- **Problem**: Two binaries (`spec-runner`, `spec-task`) with overlapping semantics. `spec-runner status` vs `spec-task stats`, `spec-runner run --task=X` vs `spec-task start X`.
- **Source**: 04-cli-ux.md section 1.1
- **Design**:
  - Single binary `spec-runner` with subcommand groups
  - `spec-runner run|status|costs|watch|plan|verify|report|validate|retry|logs|stop|reset|tui|mcp`
  - `spec-runner task list|show|start|done|block|check|stats|next|graph|sync-to-gh|sync-from-gh`
  - `spec-task` becomes deprecated alias with warning for 1-2 minor releases
  - Split common args: `global_args` (--spec-prefix, --project-root, --log-level, --log-json) and `execution_args` (--no-tests, --no-commit, --max-retries, --timeout, etc.)
  - Replace "Executor" with "spec-runner" in all help text and output headers
- **Files**: `src/spec_runner/cli.py`, `src/spec_runner/task.py`, `pyproject.toml`
- **Effort**: 3 days
- **Tests**: Update all CLI tests for new command structure. Add deprecation warning test for `spec-task`.

### POLISH-2: Config migration

- **Problem**: Config file still named `executor.config.yaml` in `spec/` directory. Legacy naming from monolith era.
- **Source**: 01-code-quality.md section 1.1, 04-cli-ux.md section 5.1
- **Design**:
  - New location: `spec-runner.config.yaml` in project root
  - New YAML section key: remove `executor:` wrapper, use flat top-level keys (simpler, less nesting)
  - Backward compat: if old file exists and new does not, use old + deprecation warning
  - If both exist: error with migration instruction
  - SQLite: `ALTER TABLE executor_meta RENAME TO runner_meta`
  - Default paths: `.executor-state.db` -> `.spec-runner-state.db`, `.executor-logs/` -> `.spec-runner-logs/`
  - Migration helper: `spec-runner migrate-config` — automatic rename with confirmation
- **Files**: `src/spec_runner/config.py`, `src/spec_runner/state.py`, `executor.config.yaml`
- **Effort**: 2 days
- **Tests**: Migration tests (old -> new, both exist, only new)

### POLISH-3: Module decomposition

- **Problem**: 3 modules exceed 900 lines. cli.py (1280), hooks.py (1040), task.py (970). High cyclomatic complexity: post_done_hook CC=38, _run_tasks CC=29, cmd_plan CC=26.
- **Source**: 01-code-quality.md sections 2.1-2.4
- **Design**: Zero new code — pure relocation.

  **cli.py (1280 lines) ->**
  - `cli.py` — main(), dispatch, global args (~300 lines)
  - `cli_run.py` — cmd_run, _run_tasks, cmd_watch (~400 lines)
  - `cli_info.py` — cmd_status, cmd_costs, cmd_logs, cmd_validate (~300 lines)
  - `cli_plan.py` — cmd_plan (~280 lines)

  **hooks.py (1040 lines) ->**
  - `hooks.py` — pre_start_hook, post_done_hook orchestration (~250 lines)
  - `review.py` — build_review_prompt, run_code_review, run_parallel_review, HITL gate (~400 lines)
  - `git_ops.py` — branch/commit/merge helpers (~200 lines)

  **task.py (970 lines) ->**
  - `task.py` — Task dataclass, parse_tasks, resolve_dependencies, get_next_tasks (~350 lines)
  - `task_commands.py` — cmd_list, cmd_show, cmd_start, cmd_done, cmd_graph, main (~350 lines)
  - `github_sync.py` — sync_to_gh, sync_from_gh, _get_existing_issues (~250 lines)

- **Post-decomposition**: Max module ~400 lines. ~20 test files need import path updates.
- **Files**: All modules >500 lines, all test files
- **Effort**: 5 days
- **Tests**: All existing tests must pass after relocation. No behavioral changes.

### POLISH-4: Generic webhook notifications

- **Problem**: Only Telegram supported. No Slack/Discord/generic webhook.
- **Source**: 04-cli-ux.md section 4.3
- **Design**:
  ```yaml
  spec-runner:
    notifications:
      telegram:
        bot_token: "..."
        chat_id: "..."
      webhook:
        url: "https://hooks.slack.com/services/..."
        method: POST
        headers:
          Content-Type: "application/json"
        template: '{"text": "{{event}}: {{message}}"}'
      events: ["task_failed", "run_complete", "budget_warning"]
  ```
  - Telegram remains first-class citizen
  - Webhook is generic: covers Slack, Discord, ntfy.sh, PagerDuty
  - Template variables: `{{event}}`, `{{task_id}}`, `{{task_name}}`, `{{message}}`, `{{cost}}`, `{{duration}}`
  - New event: `budget_warning` (triggered at 80% of budget threshold)
- **Files**: `src/spec_runner/notifications.py`, `src/spec_runner/config.py`
- **Effort**: 2 days
- **Tests**: Webhook delivery, template rendering, budget_warning event

### POLISH-5: Maestro interop contract

- **Problem**: No formal JSON output for using spec-runner as Maestro spawner. Current return type is `bool | str` — not an API.
- **Design**:
  ```bash
  spec-runner run --task=TASK-001 --json-result
  ```
  Output:
  ```json
  {
    "task_id": "TASK-001",
    "status": "done",
    "duration_seconds": 151,
    "cost_usd": 0.12,
    "tokens": {"input": 12500, "output": 5000},
    "review": "PASSED",
    "attempts": 1,
    "exit_code": 0
  }
  ```
  - Document as stable API contract in README
  - Maestro calls `spec-runner run --task=X --json-result` as subprocess
  - Error cases return same structure with `status: "failed"` and `error` field
- **Files**: `src/spec_runner/cli.py`, `src/spec_runner/execution.py`
- **Effort**: 2 days
- **Tests**: JSON result format for success, failure, timeout, budget exceeded

### POLISH-6: Parallel mode decision

- **Context**: Parallel mode marked experimental in v1.2. By v2.0, usage data determines final decision.
- **Option A (keep)**: Leave as experimental with documented limitations. Useful for small-scale (2-3 tasks without git branches).
- **Option B (remove)**: Delete parallel.py, recommend Maestro. Simplifies codebase by ~450 lines.
- **Recommendation**: Option A. If parallel has not been used by v2.0 release, switch to Option B.
- **Effort**: 1 day (documentation + decision)

### v2.0 Out of Scope

| Item | Rationale |
|------|-----------|
| REST API / HTTP transport for MCP | Not needed for CLI tool, stdio sufficient |
| Docker isolation | Maestro territory |
| Multi-user auth | spec-runner is a single-developer tool |
| TUI redesign (diff-based refresh, detail view) | Nice-to-have, not a blocker |
| Git worktree isolation for parallel | Only if parallel returns from experimental |

---

## Timeline

```
v1.2 "Stable"       v1.3 "Spec Pipeline"      v2.0 "Polish"
  Week 1-2               Week 3-4                Week 5-7

  QW-1..QW-8             PIPE-1: verify          POLISH-1: Unified CLI
  STABLE-1: parallel     PIPE-2: report          POLISH-2: Config migration
  STABLE-2: FORMAT.md    PIPE-3: MCP write       POLISH-3: Decomposition
  STABLE-3: CLI fixes    PIPE-4: Quality         POLISH-4: Webhook
  STABLE-4: Crash res.                           POLISH-5: Interop
  STABLE-5: CI/DX                                POLISH-6: Parallel decision
```

## Dependency Graph

```
QW-1..QW-8 (independent, can be parallelized)
    |
STABLE-1 (parallel experimental) -- no deps
STABLE-2 (FORMAT.md) -- no deps
    |           \
STABLE-3        PIPE-1 (verify) -- depends on STABLE-2
    |               |
STABLE-4        PIPE-2 (report) -- depends on PIPE-1
    |
STABLE-5 (CI)
    |
PIPE-4 (type annotations) -- depends on STABLE-5 (mypy in CI)
    |
POLISH-1 (unified CLI) -- depends on PIPE-4
    |
POLISH-2 (config migration) -- no deps, but after POLISH-1
POLISH-3 (decomposition) -- depends on POLISH-1
POLISH-4 (webhook) -- depends on POLISH-2 (new config format)
POLISH-5 (interop) -- depends on STABLE-3 (--json)
```

## Success Criteria

### v1.2
- All 8 QW bugs fixed, tests pass
- `spec-runner validate` works correctly
- `spec-runner run --dry-run` shows execution plan
- `spec-runner status --json` returns valid JSON
- `--parallel` shows experimental warning
- mypy passes in CI
- 0 known crash scenarios (disk full handled gracefully)

### v1.3
- `spec-runner verify` produces compliance report
- `spec-runner report` produces traceability matrix with coverage %
- MCP server supports run_task and stop from IDE
- All public functions have return type annotations
- Task description parsed from tasks.md

### v2.0
- Single `spec-runner` binary with `task` subcommand group
- Config at `spec-runner.config.yaml` with migration helper
- No module exceeds 400 lines
- Generic webhook notifications work with Slack/Discord
- `--json-result` provides stable API for Maestro integration
