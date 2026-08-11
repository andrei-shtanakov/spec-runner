# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Active Work & Roadmap

- **Current task list:** `./TODO.md` — read it at the start of every session
- **Ecosystem roadmap (strategic):** `../prograph-vault/authored/notes/ecosystem-roadmap.md` — R-01…R-16 across Maestro / arbiter / ATP / spec-runner
- **Latest weekly status:** `../prograph-vault/authored/notes/status/2026-04-10-status.md`
- **Sibling projects** (reference only): `../maestro/`, `../arbiter/`, `../atp-platform/`, `../proctor/`

spec-runner's role in the ecosystem: the only **working** cross-project link (Maestro→spec-runner). Contract stability (`.executor-state.db` SQLite schema, `--json-result` stdout) is the main ecosystem responsibility — see `docs/state-schema.md`, `schemas/*.json`, and `tests/test_json_result_contract.py`. Any breaking change needs a major version bump.

## `../_cowork_output/` is dev-only — never a code/runtime resource

`../_cowork_output/` (the polyrepo **sibling** workspace — not to be confused with this repo's own local `./_cowork_output/` scratch directory) is the development-time coordination area (cross-team ADRs, status notes, contract drafts, PM/dev tooling). Users and teams installing or cloning this project do NOT have it. Rules:

- Shipped/runtime code must never read, import, or resolve paths under `../_cowork_output/`.
- Canonical shippable facts live inside the owning repo (e.g. the ecosystem agents-catalog SSOT is `atp-platform/method/agents-catalog.toml`). Cross-repo contracts this repo depends on must be **vendored in** as pinned copies (as with the observability-contract pin) — never referenced from `../_cowork_output/` at runtime.
- Only workspace-local dev tooling (e.g. `../_cowork_output/devtools/`) and documentation may reference it.

## Project Overview

**spec-runner** (v2.22.0) — Task automation from markdown specs via a coding-agent CLI. Reads structured tasks from `spec/tasks.md`, executes them as CLI subprocesses (claude/codex/opencode/pi/ollama/llama-cli/qwen/copilot — selectable via `spec-runner config`) with retries, code review, Git automation, and hook-based CI-like workflows. Includes post-execution compliance verification and traceability matrix reporting.

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
spec-runner run --all                      # Execute all ready tasks (resets failed→pending by default)
spec-runner run --all --no-reset-failed    # Keep failed tasks sticky (skip the default reset)
spec-runner run --dry-run                  # Show what would execute (JSON)
spec-runner run --json-result              # Output structured JSON per task (Maestro interop)
spec-runner status                         # Show execution status
spec-runner status --json                  # JSON status output
spec-runner plan "description"             # Interactive task planning
spec-runner plan --full "description"      # Generate full spec (requirements + design + tasks)
spec-runner plan --full --from-file spec.md  # Read description from a file (vs positional arg)
spec-runner plan --gated "description"     # Generate one gated spec stage (auto-resolved), write DRAFT, validate, stop
spec-runner plan --gated --stage design    # Generate a specific stage (upstream must be approved)
spec-runner plan --gated --profile lite    # Select the stage profile (default lite; also on the spec family)
spec-runner spec status                    # Show per-stage draft/approved/stale + next action
spec-runner spec approve tasks             # Re-validate + approve a stage (cascades stale downstream)
spec-runner spec reject tasks              # Reopen an approved/stale stage as draft
spec-runner spec adopt tasks               # Stamp frontmatter onto an unmanaged file (validates first)
spec-runner spec adopt tasks --force       # Adopt as approved even if validation fails
spec-runner spec check tasks               # Refresh the cached validation verdict for a stage
spec-runner run --strict                   # Enforce spec governance gate (block unapproved managed tasks.md)
spec-runner run --no-strict                # Disable the gate for this run (default behavior)
spec-runner watch --strict                 # `watch` is gated too — same enforcement as `run --strict`
spec-runner validate                       # Validate config and tasks
spec-runner verify                         # Verify post-execution compliance
spec-runner verify --task=TASK-001         # Verify specific task
spec-runner verify --json                  # JSON compliance output
spec-runner verify --strict                # Fail on warnings too
spec-runner preflight                      # Read-only: what is missing before tasks can run
spec-runner preflight --json               # Machine-readable readiness report
spec-runner audit                          # Static pre-execution spec audit
spec-runner audit --strict                 # Treat orphans/uncovered as failures
spec-runner audit --json|--csv             # Machine-readable output for CI
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
spec-runner run --allow-dirty-spec         # Skip the dirty-spec guard (default: refuse to run
                                           # with uncommitted spec/config when git automation is on)
spec-runner watch --allow-dirty-spec       # Same override for watch (and `retry` accepts it too)
spec-runner run --budget=10.0              # Set global budget in USD
spec-runner run --task-budget=2.0          # Set per-task budget in USD
spec-runner costs                          # Cost breakdown per task
spec-runner costs --json                   # JSON output for automation
spec-runner costs --sort=cost              # Sort by cost descending
spec-runner watch                          # Continuously execute ready tasks
spec-runner watch --tui                    # Watch with live TUI dashboard
spec-runner mcp                            # Launch MCP server (stdio)
spec-runner tdd abandon TASK-001 --checkpoint <id> --reason "..."   # Give up on a red (#141)
spec-runner tdd repair TASK-001 --checkpoint <id> --commit <sha> --reason "..."  # New lineage
spec-runner sync                           # Post-merge closer: pull base, prune merged task/run branches, state check
spec-runner review-pr <url-or-number>      # Review-bot loop: collect, verify, fix valid, gate, push, reply (exit 0/1/2)
spec-runner review-pr 6 --verify-only      # Stop after per-comment verdicts (read-only)
spec-runner review-pr 6 --json             # Machine-readable verdict/resolution report
spec-runner review-pr 6 --no-verify        # Collect + persist only, skip the verification agent
spec-runner sync --dry-run                 # Show what sync would do without changing anything
spec-runner task list --status=todo        # List tasks by status (unified CLI)
spec-runner task next                      # Show next ready tasks
spec-runner task graph                     # ASCII dependency graph
spec-runner task sync-to-gh                # Sync tasks → GitHub Issues
spec-runner task sync-to-gh --dry-run      # Preview without making changes
spec-runner task sync-from-gh              # Sync GitHub Issues → tasks.md
spec-runner change new add-dark-mode       # Scaffold spec/changes/add-dark-mode/ (change-as-folder)
spec-runner change list                    # List in-flight changes (--json)
spec-runner change archive add-dark-mode   # Merge the change's delta spec into spec/requirements.md + move to archive/ (--force, --dry-run)
spec-runner run --change add-dark-mode     # Any command scoped to a change folder (--change on run/status/verify/...)
spec-runner doctor                         # Probe CLI/model compatibility (real mini-task)
spec-runner doctor --cli=codex --model=X   # Ad-hoc CLI+model probe
spec-runner doctor --with-review --json    # Include review stage, machine output
spec-runner config --preset codex          # Set exec+review CLI (mono)
spec-runner config --exec claude --review codex  # Mixed CLIs (multi)
spec-runner config --list-presets          # List available CLI presets
spec-runner config --preset qwen           # Qwen Code CLI (template-driven)
spec-runner config --preset copilot        # GitHub Copilot CLI (template-driven)
spec-runner-init                           # Install skills to .claude/skills
```

Note: `spec-task` is deprecated. Use `spec-runner task <command>` instead.

## Architecture

### Source Layout

All code is in `src/spec_runner/`:

| Module | Lines | Purpose |
|---|---|---|
| `executor.py` | ~60 | Backward-compatible re-exports, `_shutdown_requested`/`_pause_requested` flags, signal handlers |
| `cli.py` | ~1290 | CLI dispatcher, `cmd_run`, `_run_tasks` (incl. `run --all` failed→pending reset, second-pass-failure detection, stop-reason capture), `cmd_watch`, `cmd_retry`, `build_task_json_result` (stable Maestro-interop helper), `_build_parser()` (incl. the `config` subparser and the shared `--profile` stage-profile selector on `plan --gated`/`spec`) + `main()` with argparse |
| `cli_info.py` | ~430 | Info/query commands: `print_status`/`cmd_status` (version header, `[error_kind]`/`[at: stage]` failed-task display, `⚠️` stop-reason line, `💡` repeated-failure hint), `cmd_costs`, `cmd_logs`, `cmd_stop`, `cmd_reset`, `cmd_validate`, `cmd_verify`, `cmd_report`, `cmd_tui`, `cmd_mcp` |
| `cli_plan.py` | ~325 | Interactive planning: `cmd_plan` with interactive, `--full`, and `--gated` (`run_gated_stage`: one-stage generate, upstream-approved gate, write DRAFT, validate, stop) pipeline modes |
| `preset_cmd.py` | ~245 | `spec-runner config` CLI-profile presets: `Fragment` + 8 bundled `presets/*.yaml` (claude/codex/opencode/pi/ollama/llama-cli/qwen/copilot), `load_fragment`/`list_presets`/`compose` (→ 7 CLI-profile keys), `apply_to_config` (fresh static-template write / `--dry-run` / refuse / shape-preserving `--apply` merge with `.bak`), `cmd_config`. qwen/copilot carry `exec_template`/`review_template`; the rest rely on runner auto-detect |
| `execution.py` | ~560 | `execute_task()`, retry strategy (`classify_retry_strategy`, `compute_retry_delay`, `run_with_retries`), Telegram notification on failure |
| `mcp_server.py` | ~270 | MCP server (`MCPServer`, stdio): status, tasks, costs, logs, run_task, stop, next_tasks, task_detail tools; module-level security note |
| `config.py` | ~545 | ExecutorConfig, Persona, YAML loading, build_config; supports both `spec-runner.config.yaml` (v2.0) and `spec/executor.config.yaml` (legacy); `ExecutorLock` with PID diagnostics; `_detect_subdir_repo()` + subdir-project git-automation auto-default (OFF when project_root is a strict subdir of a larger git repo); `sync_deps` flag (under `hooks.pre_start`, default true); `spec_profile` (default `lite`) + `resolve_spec_profile()` → `StageProfile` (raises `ConfigError` listing available profiles on unknown name) |
| `state.py` | ~855 | ExecutorState (context manager), TaskState, TaskAttempt, ErrorCode, ReviewVerdict, RetryContext, SQLite persistence with crash resilience; `_is_disk_full_error()` / `_enter_degraded_mode()` fallback; token fields, `total_cost()`, `task_cost()`, `total_tokens()`, `recover_stale_tasks()`; v2.3.0: `error_kind`/`error_stage` attempt columns, `set_meta`/`get_meta`, `reset_failed_to_pending()`, second-pass helpers (`add/get/clear_second_pass_fails`), `most_recent_failed_attempt()` |
| `errors.py` | ~80 | Error classification (v2.3.0): `ErrorPattern` + `classify()` turn CLI stderr into human-readable failure reasons (codex usage-limit, generic rate-limit, auth, network, cli_error) with a last-5-lines stderr fallback |
| `stages.py` | ~45 | Per-task sub-stage tracking (v2.3.0): `STAGES` tuple + `StageReporter.enter()` mirrors `⏳ stage: <name>` transitions to stderr progress; one reporter per task (concurrency-safe) |
| `prompt.py` | ~530 | Prompt building, templates, error formatting, constitution guardrails, persona injection, `build_generation_prompt()`, `parse_spec_marker()`, `SPEC_STAGES`; stage template/marker/prompt text resolved from the active `StageProfile` via `_stage_def()`/`load_bundled_template()` (default `lite`), not module-level maps |
| `hooks.py` | ~515 | Pre/post hook orchestration, plugin hook integration; `pre_start_hook` gates `uv sync` on `sync_deps` config flag |
| `git_ops.py` | ~150 | Git operations: branch creation, main branch detection, `ensure_on_main_branch`, test file mapping |
| `review.py` | ~510 | Code review: `REVIEW_ROLES` (5 roles), `build_review_prompt`, `run_code_review`, `run_parallel_review`, HITL approval gate; `_resolve_review_template()` inherits the exec template only when the review CLI is the same binary (no cross-CLI template bleed) |
| `runner.py` | ~450 | CLI command building (`build_cli_invocation` → `CliInvocation{argv, result_format}`; claude/codex/opencode/pi/ollama/llama auto-detect; codex uses `codex exec [-m MODEL] <PROMPT>`, NOT `-p` which is `--profile`; qwen/copilot are NOT auto-detected — driven by `command_template` from their `config` presets); `build_cli_command` is a back-compat argv wrapper. Per-CLI result seam `parse_cli_result(result_format, …) → CliResult`: explicit claude (`claude`/`claude-code`) runs use `--output-format json`, so cost comes from `total_cost_usd`/`usage` (`_parse_claude_json`); other CLIs fall back to `parse_token_usage()`. subprocess exec with graceful termination (SIGTERM→SIGKILL), progress logging; `run_claude_async()` |
| `doctor.py` | ~420 | CLI/model compatibility probe: ephemeral scratch workspace + real `execute_task()`, raw-signal extraction (marker/action/cost/error/review), READY/DEGRADED/BROKEN verdict, `--json` schema (`schemas/doctor-result.schema.json`) |
| `task.py` | ~490 | Task dataclass, regex parsing, dependency resolution, status management; `parse_tasks` strips leading frontmatter, write-back functions preserve it |
| `task_commands.py` | ~440 | Task CLI commands: list, show, start, done, block, check, stats, next, graph |
| `spec.py` | ~790 | Stage profiles (v2.9.0): `StageDef`/`StageProfile` (frozen dataclasses; `name`/`template`/`marker_prefix`/`validator_key`/`upstream`/`prompt_text`), `load_profile()`/`available_profiles()` load bundled `profiles/*.yaml` via `importlib.resources`; `STAGES`/`SPEC_STAGES` derived from the `LITE` profile. Gated spec-generation frontmatter: `SpecMeta` (spec_stage/status/version/generated_by/generated_at/source_prompt_version/validation/approved_by/approved_at), atomic locked `write_spec`/`read_spec_meta`/`read_spec_body` (raises `SpecLockError` on lock contention), profile-parameterized `resolve_next_stage`/`downstream_stages`/`mark_downstream_stale`, `apply_approval` (bumps version, cascades `stale` downstream, stamps the authoring contract). Authoring contract (DEC-008, #135): `git_blob_hash`/`upstream_pins`/`derive_traces_to`/`stamp_authoring_links` materialize the steward-owned extras `traces_to` (direct upstream stage ids + body ids that resolve upstream) and `upstream_hashes` (`{direct upstream: git hash-object value}`, at approval only) |
| `spec_commands.py` | ~195 | `spec status/approve/reject/adopt/check` CLI handlers + `run_checkpoint_menu` TTY overlay; `approve` re-validates from scratch (never trusts cached `validation`), `adopt` validates-first (fail→draft unless `--force`), `reject`→draft |
| `change_commands.py` | ~250 | Change-as-folder (M2+M3): `change new/list/archive` — a change is a self-rooted spec dir at `spec/changes/<id>/` (selected via `config.change_id` / `--change`); archive validates + merges the change's delta spec (`specs/requirements.md`) into flat `spec/requirements.md`, then moves to a dated `archive/` dir; refuses live runs/unfinished tasks/merge conflicts; `--dry-run` prints the merge plan |
| `spec_merge.py` | ~135 | Delta merge engine (M3): `plan_merge` (dry-run ops/conflicts) + `apply_merge` (all-or-nothing ADDED/MODIFIED/REMOVED/RENAMED by requirement id, bootstrap on empty target) + `MergeConflictError` |
| `review_pr.py` | ~840 | Review-bot loop M1+M2 (#102): collect (gh CLI, allowed-bots filter) → `verify_comment` (fail-closed to `uncertain`; verdict discarded if the verifier mutates the tree) → `_apply_phase` (fix valid via `run_fix_agent` TDD agent, per-comment commits with `Review-Comment-Id` trailers, `_run_gates` before push, single push, thread replies with fix SHA / refutation evidence; `uncertain` never auto-answered). `ReviewPrState`: durable `pr_review_comments` (+ `resolution`/`fix_sha`/`replied_at`) and `pr_review_rounds` tables. Limits (rounds/comments/diff-lines/cost/wall) → NEEDS_HUMAN; fail-closed on dirty tree, head mismatch, force-push, push failure. `needs_human_rows` feeds `status`. Exit 0/1/2. Design: `docs/superpowers/specs/2026-08-06-review-pr-loop-design.md` |
| `github_sync.py` | ~200 | GitHub Issues sync: `cmd_sync_to_gh` (local wins), `cmd_sync_from_gh` (remote wins), `export_gh` |
| `preflight.py` | ~250 | Read-only readiness diagnostics (#142a): `run_preflight` returns typed `Check`s (`ok`/`missing`/`empty`/`broken`/`unavailable`/`skipped`) with a separate `blocking` flag; writes nothing, never guesses (composite `test_command` → `unavailable`), and treats an empty suite as a blocker rather than health. `--json` pinned by `schemas/preflight-result.schema.json`. Not `doctor` (which probes a CLI with a real mini-task) and not `validate` (spec contents) |
| `phases.py` | ~105 | Typed phase outcomes (slice 0, #164/#141 Part A): `PhaseOutcome` vocabulary, `ALLOWED_OUTCOMES` declared per stage (`expected_fail` for `tests`, rejected for `commit`), `check_outcome`, `review_verdict_to_phase` (reads a `ReviewVerdict` as outcome + detail without migrating the stored values). Records only — nothing gates on it |
| `tdd.py` | ~250 | RED checkpoint machinery (#141 slice 1b): `verify_red()` replays a claimed red against its **commit** in a disposable `git worktree --detach`, so the working tree can never influence the verdict; `RedOutcome` (expected_fail/not_red/**unverifiable**), `environment_id()` (`<lockfile>:<hash>` or `unpinned`), `resolve_namespace()`, `RedCheckpoint` + `red_checkpoints` table. Refuses without running: a non-node-id selector, a composite `test_command`, an unknown SHA. Exit-code mapping measured, not assumed (1 = failed; 4 covers both a bad node id and a syntax error). Standalone — nothing imports it until the gate (1c) |
| `remedy.py` | ~330 | Operator remedies (#141 slice 3): `abandon` / `repair` with compare-and-swap on `--checkpoint`, mandatory `--reason` + recorded actor, idempotency checked **before** the swap, refusal while the PID-checked executor lock is held, `SPEC_RUNNER_AGENT` guardrail. `repair` opens a new lineage and re-replays — a repaired test that passes is recorded `not_red` and exits 2. Tables `tdd_remedies` + `red_checkpoints.status`. `cmd_tdd` prints a refusal, never a traceback |
| `claims.py` | ~230 | File claims (#141 slice 2): `Claim`/`ClaimStatus`/`ViolationKind`, `claim_paths_for` (one file per node id — a `conftest.py` fixture is a documented unclaimed gap), `validate_claim_path` (refuses symlink / outside-repo / non-regular), `claim_blob_sha` (git blob SHA over raw bytes, no newline normalisation), `record_claims` (idempotent), `check_claims` — every **active** claim in the namespace against the **candidate commit** via `git ls-tree`, never the working tree. Table `tdd_claims` |
| `gates.py` | ~350 | Pre-terminal policy gates (#164): `GateStatus` (satisfied/unsatisfied/instrument_error), `GateResult`/`GateContext` (`config_hash` over `POLICY_KEYS` only; `facts` carries per-evaluation observations from the call site), `GateRegistry` (idempotent `register`/`unregister`) + module-level `REGISTRY`, `evaluate_gates` (per phase) and `evaluate_pre_terminal` (all registered phases). A gate never withholds the checkpoint commit — it is evaluated *against* that SHA and withholds merge/DONE. Only `instrument_error` is retried (`gate_recovery_attempts`); a malformed gate answer fails closed. `register_builtin_gates(config)` attaches the review gate (#157) only under `review_policy: required` — nothing is registered under `advisory`, so the site stays dormant. Second consumer: #141 |
| `audit.py` | ~280 | Pre-execution static audit: orphan tasks, dangling/uncovered refs, dead designs; text/JSON/CSV output |
| `audit_log.py` | ~210 | Opt-in compliance audit-trail writer: JSON-Lines appender, `AuditLogger` + `NoOpAuditLogger`, thread-safe, `run_id` + operator attribution |
| `verify.py` | ~230 | Post-execution compliance verification: traceability check, coverage, review verdicts |
| `report.py` | ~260 | Traceability matrix: REQ → DESIGN → TASK → execution state mapping |
| `validate.py` | ~490 | Config + task validation: duplicate IDs, symmetry checks, cycle detection, dead config warnings; per-stage gated-spec validators `validate_requirements`/`validate_design`; `validate_spec_stage` dispatches via the `VALIDATORS` registry keyed by `StageDef.validator_key` (from the active profile) instead of if/elif; `verdict_from_result` (pass/warn/fail) |
| `plugins.py` | ~270 | Plugin discovery, hook execution, env var building |
| `logging.py` | ~50 | Structured logging via structlog: `setup_logging()`, `get_logger()`, JSON/console output |
| `obs.py` | ~305 | Orchestra observability emitter (reference impl, vendored into Maestro/arbiter/ATP): OpenTelemetry Logs Data Model JSONL, one file per PID; `init_logging()`, spans, `child_env`. Contract: `maestro/contracts/observability/log-schema.json` |
| `events.py` | ~70 | `EventBus` with asyncio.Queue subscribers + thread-safe recent buffer for TUI streaming; `TaskEvent` dataclass |
| `notifications.py` | ~195 | Telegram + generic webhook notifications: `send_telegram()`, `send_webhook()`, `notify()`, template rendering; emits `task_failed`, `run_complete`, `state_degraded` events |
| `tui.py` | ~560 | Textual-based TUI: live task dashboard, Kanban columns, log panel with streaming events, pause keybinding |
| `init_cmd.py` | ~100 | Install bundled Claude Code skills |

Entry points (pyproject.toml): `spec-runner` → `executor:main`, `spec-task` → `task_commands:main` (deprecated), `spec-runner-init` → `init_cmd:main`

### Key Data Flow

1. `task.py:parse_tasks()` — Regex-parses `spec/tasks.md` into `Task` dataclass objects
2. `task.py:resolve_dependencies()` — Resolves dependency graph, auto-promotes blocked→todo
3. `task.py:get_next_tasks()` — Returns ready tasks (in_progress first, then todo by priority)
4. `prompt.py:build_task_prompt()` — Generates prompt with task context, requirements, design refs, previous errors
5. `execution.py:execute_task()` — Runs Claude CLI as subprocess, detects `TASK_COMPLETE`/`TASK_FAILED` markers; drives a per-task `stages.StageReporter` (`codex`/`parse`) and classifies failure stderr via `errors.classify()` into `error_kind`/`error_stage`
6. `execution.py:run_with_retries()` — Retry loop with error context forwarding between attempts
7. `hooks.py`: `pre_start_hook()` (git branch, uv sync — emits `sync_deps`/`branch` stages) → execution → `post_done_hook()` (tests, lint, review, commit, merge — emits the matching stages)
8. `events.py:EventBus` — Optional streaming: `run_claude_async()` publishes stdout lines as `TaskEvent`s; TUI drains them on refresh
9. `notifications.py:notify()` — Sends Telegram/webhook notifications on `task_failed` and `run_complete` events (if configured)

### Key Classes

- **`ExecutorConfig`** — Dataclass merging YAML config + CLI args. Handles `spec_prefix` path resolution for multi-phase projects. Includes `personas` (dict of `Persona` for role-specific prompts/models), `review_parallel`, `review_roles`, `webhook_url/method/headers/template`, `notify_on` (defaults to `[run_complete, task_failed, state_degraded]`).
- **`Persona`** — Agent persona with `system_prompt`, `model`, `focus` fields for phase-specific customization (architect, implementer, reviewer, qa).
- **`ExecutorState`** / **`TaskState`** / **`TaskAttempt`** — Execution state persisted to SQLite (`spec/.executor-state.db`) with WAL mode + busy_timeout. Auto-migrates from legacy JSON on first run. `ExecutorState` is a context manager. Degraded-mode fallback: when SQLite writes fail (disk-full, corruption), `state.degraded` / `state.degraded_reason` flip true, the in-memory state keeps serving the run, and operators are notified once via `state_degraded`.
- **`ErrorCode`** — `str` enum classifying failures: TIMEOUT, RATE_LIMIT, TEST_FAILURE, LINT_FAILURE, TASK_FAILED, HOOK_FAILURE, BUDGET_EXCEEDED, REVIEW_REJECTED, INTERRUPTED, UNKNOWN. Stored in `attempts.error_code` column.
- **`ReviewVerdict`** — `str` enum for code review outcomes: PASSED, FIXED, FAILED, SKIPPED, REJECTED. Stored in `attempts.review_status` column.
- **`RetryContext`** — Structured retry info (attempt number, error code, previous error, test failures) passed to `build_task_prompt()` for focused retry prompts.
- **`ErrorPattern`** (`errors.py`) — Frozen dataclass (kind, regex, template) in the `PATTERNS` library; `classify(stderr, returncode)` returns `(error_kind, human_message)`, first-match-wins, with a last-5-lines stderr fallback.
- **`StageReporter`** (`stages.py`) — One per task; `.enter(name)` validates against `STAGES`, updates `.current`, and mirrors `⏳ stage: <name>` to the progress callback. `.current` is recorded as `error_stage` on failure.
- **`StageProfile`** / **`StageDef`** (`spec.py`, v2.9.0) — Frozen dataclasses describing the gated spec-generation stage chain as data. A `StageDef` carries `name`, `template`, `marker_prefix`, `validator_key`, `upstream`, and optional `prompt_text`; a `StageProfile` is the ordered list (`.names()` yields the stage tuple). Loaded from bundled `src/spec_runner/profiles/*.yaml` via `load_profile()`; the built-in `lite` profile (`LITE`) reproduces the historical `requirements → design → tasks` chain, and `STAGES`/`SPEC_STAGES` derive from it. Selected via `config.spec_profile` / `--profile`; `spec.py`, `prompt.py`, and `validate.py` all read stages from the resolved profile. Behaviour-preserving: the default is `lite`.
- **`Task`** — Parsed task with id, priority (p0-p3), status (todo/in_progress/done/blocked), description, checklist, dependency graph, traceability to `[REQ-XXX]`/`[DESIGN-XXX]`.
- **`ValidationResult`** — Validation outcome with errors and warnings lists, `ok` property. Checks duplicate IDs, blocks/depends_on symmetry.
- **`PluginInfo`** / **`PluginHook`** — Plugin metadata and hook configuration from `spec/plugins/*/plugin.yaml`.
- **`EventBus`** / **`TaskEvent`** — Pub/sub event streaming for TUI. Thread-safe `drain_recent()` for cross-thread consumption.
- **`VerifyResult`** / **`VerificationReport`** — Compliance check results per task and overall coverage.
- **`TraceRow`** / **`TraceabilityReport`** — Traceability matrix mapping REQ → DESIGN → TASK → execution state.

### Configuration Precedence

`ExecutorConfig` defaults → `spec-runner.config.yaml` (v2.0, project root) or `spec/executor.config.yaml` (legacy v1.x) → CLI arguments (highest priority)

### Execution mode (#141, slice 1a)

`execution_mode: standard|tdd` (config key, default `standard`) with a per-task
`**Mode:** tdd` override in tasks.md, resolved by
`ExecutorConfig.resolve_execution_mode(task)` (raises `ConfigError` on an
unknown value, naming the task when the task declared it). **Declared, not
enforced**: no execution-path module reads it yet — pinned by
`tests/test_execution_mode.py::TestNothingBranchesOnItYet`. Enforcement arrives
with the RED checkpoint (slices 1b/1c).

### RED gate (#141, slice 1c)

Under `execution_mode: tdd`, `execute_task` runs a RED authoring pass
(`prompt.build_red_prompt` → `tdd.run_red_phase`: write one failing test, report
`TDD_SELECTOR: path::test`), commits it, replays it against that commit, and
records a `RedCheckpoint` whatever the outcome. The **gate** (`gates._red_gate`,
registered for phase `tests` by `register_builtin_gates` / `ensure_red_gate`)
then decides: a confirmed `expected_fail` whose commit is an **ancestor** of the
tree in hand satisfies it; `not_red` blocks; `unverifiable` is an instrument
error. Evaluated twice — before the implementation pass and again at the
pre-terminal site — since "do not merge a task that never had a confirmed red"
is the same question. The effective mode reaches the gate via
`GateContext.facts["execution_mode"]`, because the mode is per task.

### Review policy (#157)

`review_policy: advisory|required` (config key, default `advisory`) decides
whether the review verdict may withhold terminal completion. Under `required`
`failed`/`rejected`/`not_run`/`skipped` block, `error` is an instrument error
(bounded recovery → infrastructure error), `passed`/`fixed` proceed. The gate is
registered by `gates.register_builtin_gates()` at CLI startup and evaluated at
the pre-terminal site in `post_done_hook`, between the checkpoint commit and the
merge; it reads the verdict from `GateContext.facts`, never from `phase_results`
(that write is best-effort). `required` + `run_review: false` is refused by
`validate` and again at startup (covers `--no-review`).

### Spec Governance

`spec_governance: off|strict` (config key, default `off`) gates `run`/`watch` on an approved `tasks.md` (`spec_run_gate_ok()` in `cli.py`); `--strict`/`--no-strict` override it per invocation. Only a *managed* `tasks.md` (has frontmatter) with `status != approved` is blocked — unmanaged (frontmatter-less) files always pass, so default-off and Maestro-produced specs are unaffected. `config.spec_lock_file` (`spec/.{spec_prefix}spec.lock`) serializes `spec.py`/`spec_commands.py` writes via `ExecutorLock`.

### Multi-phase Support

`--spec-prefix=phase2-` namespaces all paths: `phase2-tasks.md`, `phase2-requirements.md`, `.executor-phase2-state.db`, etc.

## Code Style

- Python 3.11+, Ruff line length **100** (not 88 — configured in pyproject.toml)
- Ruff rules: E, F, W, I, UP, B, C4, SIM (E501 ignored)
- Type annotations required everywhere; mypy strict mode
- Git branches follow `task/TASK-###-short-name` pattern
- Config keys: `lowercase_with_underscores` matching YAML convention

## Key Dependencies

- **PyYAML** — YAML config loading
- **structlog** — Structured logging (JSON + console renderers)
- **textual** — Terminal UI dashboard for live task monitoring
- **mcp** — Model Context Protocol server (`MCPServer`, stdio transport)

## File Locations

- **Specs**: `spec/` (requirements.md, design.md, tasks.md, FORMAT.md, WORKFLOW.md, prompts/)
- **Config**: `spec-runner.config.yaml` at project root (v2.0) or `spec/executor.config.yaml` (legacy v1.x, deprecated)
- **Runtime state**: `spec/.executor-state.db` (SQLite + WAL), `spec/.executor-logs/`, `spec/.task-history.log`
- **Stage profiles**: `src/spec_runner/profiles/*.yaml` (bundled gated-spec stage chains; `lite.yaml` = default `requirements → design → tasks`)
- **Bundled skills**: `src/spec_runner/skills/spec-generator-skill/` (templates + review prompts for claude/codex/opencode/pi/ollama/llama; plus a full pi-driven dev→review→test loop under `templates/pi/`)
- **Plugins**: `spec/plugins/` (optional; each plugin is a directory with `plugin.yaml`)
- **Interop contract**: `docs/state-schema.md` + `schemas/executor-state.schema.json` + `schemas/json-result.schema.json` + `tests/fixtures/maestro-interop/` (golden fixtures copied by Maestro's contract tests)
- **Tests**: `tests/` — group by CLI module, mark slow tests with `@pytest.mark.slow`, mock Claude CLI invocations

## Testing

Tests use pytest. Test files: `test_adopt_gate.py` (`spec adopt` validate-first gate), `test_audit.py`, `test_audit_log.py`, `test_doctor.py` (doctor signal extraction, scratch workspace builder, probe execution via fake CLIs, result parser, `run_doctor` exit codes), `test_tdd_battle.py` (#141 battle test: mutation/delete/rename, shared claims across two workstreams, the abandon+repair loop, crash-resume between writes), `test_remedy.py` (#141 3: CAS, abandon/repair semantics, lineage, idempotency, lock refusal, agent guardrail, CLI), `test_claims.py` (#141 2: claim derivation, path refusal, raw-byte hashing, enforcement kinds, the gate, red-phase freezing + lint), `test_cli_flags.py` (run-subparser flag parsing, incl. `--no-reset-failed`), `test_cli_info.py` (status display: version header, error-kind/stage, stop-reason line, second-pass hint), `test_cli_run_reset.py` (`run --all` reset, second-pass detection, stop-reason capture), `test_config.py` (incl. subdir auto-default), `test_costs.py`, `test_e2e.py`, `test_errors.py` (error classification patterns), `test_events.py`, `test_execution.py` (incl. error classification + stage wiring), `test_execution_mode.py` (#141 1a: mode default/override/refusal, `**Mode:**` parsing, the not-enforced guard), `test_gated_plan.py` (`plan --gated` one-stage generate + upstream gate), `test_gh_sync.py` (includes gh-sync conflict/idempotency tests), `test_hooks.py` (incl. stage emission), `test_json_result_contract.py` (pins the Maestro `--json-result` contract with golden fixtures), `test_logging.py`, `test_mcp.py`, `test_notifications.py`, `test_obs.py`, `test_obs_contract.py`, `test_phase_outcomes.py` (slice-0 vocabulary, per-stage admissible sets, waivers), `test_plan_full.py`, `test_plugins.py`, `test_policy_gates.py` (#164 gate mechanism: dormancy, `(sha, config_hash)` binding, bounded recovery, the pre-terminal call site), `test_presets.py` (`config` preset fragments, `compose`, fresh/refuse/`--dry-run`/`--apply` merge, `cmd_config` dispatch, incl. qwen/copilot templates), `test_prompt.py`, `test_red_gate.py` (#141 1c: the verdict table, tree descent, the execution refusal), `test_red_checkpoint.py` (#141 1b: replay outcomes, worktree disposal, environment identity, namespace collision, persistence), `test_report.py`, `test_review.py` (`_resolve_review_template` cross-CLI template guard), `test_review_policy.py` (#157: the verdict table under `required`, advisory dormancy, both-trees evidence, the validate/startup contradiction check), `test_run_gate.py` (`spec_run_gate_ok`/`run`/`watch` governance gate), `test_runner.py` (incl. codex `exec` adapter), `test_source_prompt_version.py` (`template_hash` content-hash versioning), `test_spec_commands.py` (`spec status/approve/reject/adopt/check`, checkpoint menu), `test_spec_lock.py` (`write_spec` lock contention/`SpecLockError`), `test_spec_meta.py` (`SpecMeta`, frontmatter split/strip, `resolve_next_stage`, `apply_approval` stale cascade), `test_spec_prefix.py` (includes multi-phase E2E coverage), `test_stage_profile.py` (C1 `StageDef`/`StageProfile`, `load_profile`, `lite` names), `test_prompt_profile.py` (prompt building from `StageDef`), `test_spec_profile_config.py` (`spec_profile` config + `resolve_spec_profile` unknown-name error), `test_c1_zero_behaviour.py` (golden zero-behaviour-change proof for the default `lite` pipeline), `test_stages.py` (StageReporter), `test_state.py` (includes `TestDegradedMode` for disk-full fallback, plus v2.3.0 migration/reset/meta tests), `test_subdir_detection.py` (`_detect_subdir_repo`), `test_task.py`, `test_task_diff.py`, `test_tui.py`, `test_validate.py`, `test_verify.py`, `test_watch.py`. Shared pytest config in `tests/conftest.py` (adds `--update-golden` for fixture regeneration). E2E tests use `tests/fixtures/fake_claude.sh` as a mock Claude CLI and are marked with `@pytest.mark.slow`. Mock subprocess/CLI calls to keep runs fast. Regression tests required for bug fixes.

## Repo scope & boundaries

- **Этот репо:** `spec-runner` — git-корень `all_ai_orchestrators/spec-runner/`, remote `git@github.com:andrei-shtanakov/spec-runner.git`.
- **Соседи (READ-ONLY reference):** `../arbiter/`, `../atp-platform/`, `../deployer/`, `../dispatcher/`, `../maestro/`, `../libretto/`, `../proctor/`, `../prograph/`, `../prograph-vault/`, `../robin-runtime/`, `../robin-toolkit/`, `../spec-runner-vscode/`, `../steward/` — их код не редактировать.
- Нужна правка у соседа → **стоп**: запиши handoff в `../prograph-vault/authored/notes/`
  (кросс-проектное) или `../_cowork_output/` (черновик), не трогай его файлы.
- Кросс-репные контракты — **вендорить пиненой копией внутрь**, не ссылаться наружу.
- Полное правило (SSOT): `../prograph-vault/authored/rules/repo-boundaries.md`.

## Git workflow (у репо есть remote)

- Ветка `<type>/<slug>` → push → `gh pr create`. **Прямые коммиты в `master` запрещены.**
- После открытия PR — прочитать ревью **GitHub Copilot**: валидные замечания исправлять
  новыми коммитами в ту же ветку; невалидные — ответить с обоснованием, **не применять
  вслепую**; итерировать, пока не останется открытых замечаний.
- **Не мержить.** Мерж делает пользователь.
- После мержа пользователем: `git switch master && git pull --ff-only`, затем удалить
  влитую ветку (`git branch -d <branch>`) и `git fetch --prune`; убрать прочие влитые ветки.
- Никогда не делать force-push в общие ветки; не трогать другие репо (см. scope выше).
- Полное правило (SSOT): `../prograph-vault/authored/rules/git-workflow.md`.

## Входящие запросы (inbox)

В начале работы проверь входящие: `gh issue list --label inbox --state open`.
Issue с лейблом `inbox` — запрос от соседнего репо, ещё **не** пункт плана.
Принять = завести пункт в `TODO.md` с указанным `slug:`; принял под другим
именем — поправь `slug:` в теле issue.
Отказать = `gh issue close --reason "not planned"`.
Нужна работа в соседнем репо — не редактируй его: заведи там issue
(`slug:` + `from:` + проза). Правило: ADR-ECO-006 — канон в `ecosystem-kb`
(каталог `prograph-vault/` в корне воркспейса),
`authored/decisions/2026-07-28-adr-eco-006-cross-repo-issue-inbox.md`.
