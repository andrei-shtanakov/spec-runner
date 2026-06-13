# Changelog

All notable changes to spec-runner are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per CLAUDE.md: any change to `.executor-state.json` / `--json-result` format
is a **breaking change** and requires a major version bump plus an entry here.

## [Unreleased]

### Added

- **`spec-runner config`** — apply a CLI profile preset to
  `spec-runner.config.yaml`. `--preset X` sets both the exec and review CLI
  (mono); `--exec X --review Y` mixes them (multi). Presets: claude, codex,
  opencode, pi, ollama, llama-cli. `--model` / `--review-model` override the
  model; `--list-presets` lists them; `--dry-run` previews; `--apply` updates an
  existing config (surgical merge of the 7 CLI-profile keys, other settings
  preserved, backed up to `.bak`). Note: on `--apply`, PyYAML normalises
  comments and key ordering.

### Fixed

- **`validate` now checks flat v2.0 configs**, not only `executor:`-wrapped
  ones, so unknown top-level keys in `spec-runner.config.yaml` are caught.

## [2.4.1] — 2026-06-12

### Fixed

- **`doctor` mislabelled auth/API errors as "command not in PATH".** The
  invocation check matched a bare "not found" substring, so an error whose text
  contained it (e.g. a CLI returning Google's "API Key not found") was reported
  as a missing executable. It now matches the actual `No such file or directory`
  FileNotFoundError, so auth/network failures surface with their real cause.

- **Crash/interruption recovery — orphaned `in_progress` tasks.** When the run
  holds the exclusive executor lock, any task still marked `running` is orphaned
  from a dead run, so recovery now resets **all** such tasks regardless of age
  (previously only those running longer than 2× the task timeout, ~60 min).
  Otherwise an interrupted session (e.g. a dropped remote shell) left a half-done
  task that the next run re-picked first (`in_progress` goes first) and hung
  re-doing it. **TUI runs now also take the lock** (one executor per project);
  `--force` runs hold no lock and keep the conservative age-based heuristic.
- **Review diff against a parent repo.** Code review skips `git diff HEAD~1` when
  git automation is off (a subdir of a larger repo, or `--no-branch --no-commit`).
  There the diff was taken against the **parent** repository — a huge, unrelated
  diff that made the reviewer slow or hang. (The review subprocess timeout
  `review_timeout_minutes` was already enforced.)

## [2.4.0] — 2026-06-12

### Added

- **`spec-runner doctor`** — empirical CLI/model compatibility probe. Runs a
  real one-task run through `execute_task()` against the configured (or
  `--cli`/`--model`) backend and reports per-capability status (invocation,
  completion marker, task action, cost tracking, error classification, optional
  `--with-review`) with a READY/DEGRADED/BROKEN verdict. `--json` output is
  pinned by `schemas/doctor-result.schema.json`. Budget-capped (default $0.50)
  with a confirmation gate (`--yes` to skip); `--strict` fails CI on DEGRADED.
- **`sync_deps` config flag** (under `hooks.pre_start`) — gates the `uv sync`
  step in `pre_start_hook` (doctor disables it for the scratch workspace).
- **`spec-runner plan --from-file PATH`** — read the feature description from a
  file instead of the positional argument (the positional is now optional). Handy
  for long descriptions; `--from-file` takes priority and errors on a
  missing/empty file or when neither source is given.

### Fixed

- **Cost tracking for the claude CLI.** `execute_task` now invokes claude with
  `--output-format json` and parses `total_cost_usd` / `usage` from the result —
  the old stderr regex (`parse_token_usage`) no longer matches modern claude
  (2.x), so cost/tokens were silently `None` and `costs` / `--budget` /
  `--task-budget` did nothing for claude. Implemented behind a per-CLI result
  seam: `build_cli_invocation() -> CliInvocation{argv, result_format}` and
  `parse_cli_result(result_format, …) -> CliResult`. JSON mode is gated to an
  **explicit** `claude` / `claude-code` binary (no template), so other CLIs,
  templated claude, and custom wrappers are unaffected (`build_cli_command` stays
  a thin argv wrapper, so the review path and other callers are unchanged). A
  claude `is_error` JSON payload now forces a task failure. Verified end to end:
  `spec-runner doctor --cli=claude` → READY with a real measured cost. Claude's
  native `--max-budget-usd` cap is supported by the builder but intentionally not
  wired into runs yet (it would hard-fail on slight overage) — deferred. Review-
  stage cost is still not tracked (follow-up).
- **Task `DONE` status is now committed to git.** The `tasks.md` done-status +
  checklist update happened in `execution.py` *after* `post_done_hook`'s
  commit/merge, so it landed in the working tree post-merge, was never committed,
  and got clobbered by the next task's branch — leaving completed tasks stuck at
  `IN_PROGRESS` and desyncing `get_next_tasks` from the executor DB. The update
  now runs inside `post_done_hook` before the auto-commit.

## [2.3.1] — 2026-06-10

### Added

- **Pi-driven dev→review→test loop.** Bundled `pi/` skill templates
  (`pi-implementer`, `pi-reviewer`, `pi-tester`) plus `spec-runner.pi.config.yaml`
  with per-stage command templates — full tools for develop, a read-only review
  gate — letting `pi` run the entire cycle with no core code. Documented in
  `docs/pi-workflow.md` with a runnable `examples/pi-loop/` example.

### Changed

- **`review.pi.md` is now a strict read-only gate.** The reviewer inspects and
  reports findings (no self-fixes); the implementer fixes on retry. Dropped the
  `REVIEW_FIXED` outcome from the pi review prompt.
- Dependency bumps via Dependabot: `urllib3` 2.6.3→2.7.0,
  `python-multipart` 0.0.26→0.0.27, `python-dotenv` 1.2.1→1.2.2,
  `pyjwt` 2.11.0→2.12.0.
- CI: minimal `GITHUB_TOKEN` permissions.

## [2.3.0] — 2026-05-30

### Added

- **Version in `status` header.** First line of `spec-runner status` now
  reads `📊 spec-runner v<version>`.
- **Human-readable error reasons.** Failed-task lines in `status` now show
  `[error_kind] message` instead of "Unknown error", with the failing
  sub-stage tagged as `[at: <stage>]`. Driven by a small pattern library in
  `src/spec_runner/errors.py` (codex usage-limit, generic rate-limit, auth,
  network, generic CLI error) with a last-5-lines-of-stderr fallback.
- **Run stop-reason summary.** When a run halts abnormally (e.g.,
  `max_consecutive_failures`, codex rate limit), `status` prints a
  `⚠️ Last run stopped: …` line above the totals.
- **Repeated-failure log hint (`💡`).** When a task that was already failed
  before the current run fails again, spec-runner emits a `💡` warning to
  stderr immediately and shows a persistent hint under the task in `status`
  with the path to its log file.
- **Per-stage progress mirror.** Extends 2.2.2's stderr progress with one
  `⏳ stage: <name>` line per sub-stage (`sync_deps`, `branch`, `codex`,
  `parse`, `tests`, `lint`, `commit`, `merge`, `review`). Stages are emitted
  only when the corresponding step actually runs.

### Changed

- **`run --all` now resets failed→pending and consecutive_failures→0 by
  default.** Use the new `--no-reset-failed` flag to preserve the old sticky-
  failed behavior. Single-task runs (`run TASK-X`) and `retry` are unaffected.
- **Subdir-project safety: git automation defaults OFF when `project_root`
  is a strict subdirectory of a larger git repo.** Prior behavior could
  commit unrelated files across the whole repo and merge them to `main`.
  Explicit `create_git_branch=true` / `auto_commit=true` in YAML or via CLI
  are respected; a warning log is emitted when the auto-default triggers.

### Fixed

- **codex CLI adapter.** `build_cli_command` now builds `codex exec [-m
  MODEL] <PROMPT>` instead of `codex -p <PROMPT>`. `-p` in the codex CLI is
  `--profile`, not the prompt, so the previous form crashed every codex run
  with an `invalid --profile value` error that spec-runner surfaced as the
  generic "Unknown error". Existing `command_template` overrides are
  preserved (template path is checked before auto-detect).

### Schema

- `attempts` gains two TEXT columns: `error_kind`, `error_stage`. Idempotent
  on-startup migration; legacy rows with NULL values render in the old
  format. Three new keys appear in `executor_meta`: `last_run_stop_reason`,
  `last_run_stop_detail`, `second_pass_fail_tasks`. Forward-compatible:
  downgrading to 2.2.2 simply ignores the extras.

## [2.2.2] — 2026-05-29

### Added

- **Console progress for non-TUI runs.** Plain `spec-runner run` / `watch`
  were silent because `obs` routed all structlog output to the per-PID JSONL
  file only. A compact, human-readable progress line is now mirrored to
  **stderr** (opt-in `obs.init_logging(..., console=True)`, wired through
  `setup_logging`'s existing `tui_mode` flag — on for normal runs, off in
  TUI mode so the dashboard isn't corrupted). Trace/transport fields
  (`pipeline_id`, span/trace ids) are stripped from the console line and
  secrets are redacted upstream. The JSON file sink is byte-identical, so the
  vendored OTel observability contract is unchanged.

### Fixed

- **Task estimate parsing for decimals and en-dash ranges.** The `ESTIMATE`
  regex only accepted integer day/hour values with ASCII-hyphen ranges, so
  estimates like `1.5d` or `1–1.5d` (en-dash, U+2013) were silently dropped
  and surfaced as spurious "missing estimate" validation warnings. The pattern
  now accepts decimals and en-dash ranges (backward-compatible superset).

## [2.2.1] — 2026-05-28

### Changed

- **CI: bump GitHub Actions off the deprecated Node 20 runtime** (forced to
  Node 24 on 2026-06-02): `actions/checkout` v4→v6, `actions/setup-python`
  v5→v6, `astral-sh/setup-uv` v4→v8.1.0 (pinned exactly — setup-uv has no
  floating `v8` major tag). All three now run on `node24`.

### Fixed

- `tests/test_obs_contract.py` no longer crashes pytest collection in
  standalone CI checkouts: it read the shared `log-schema.json` from the
  external cowork workspace at module load. Guarded with a module-level
  `pytest.skip` when the contract file is absent; full coverage still runs
  locally where the workspace is present.

## [2.2.0] — 2026-05-28

### Added

- **CLI auto-detection for OpenCode and Pi Agent.** `runner.build_cli_command()`
  now recognizes two more coding agents alongside Claude / Codex / Ollama /
  llama-cli:
  - **[OpenCode](https://opencode.ai)** (sst/opencode) — `opencode run [--model provider/id] <prompt>`
  - **[Pi Agent](https://pi.dev)** (earendil-works/pi) — `pi -p [--model X] <prompt>` (non-interactive mode)
  Pi uses basename matching (not substring) to avoid false positives on
  command names containing the literal "pi" (e.g. `pipe-cli`). Bundled review
  prompts added under `skills/spec-generator-skill/templates/prompts/`.
  Either CLI can be wired to either role (executor / reviewer / persona) via
  `claude_command` / `review_command` / `personas` in the config — same as
  any other supported CLI.

### Docs

- Architecture diagrams (4 Mermaid views: system context, module map,
  task-execution sequence, storage) under `docs/architecture.{md,html}`.

### Fixed

- Green CI: resolved `ruff format --check` drift and all `mypy` errors
  (red since v2.1.0). No behavior change — Optional narrowing, type casts,
  and supertype-compatible TUI signatures.

### Notes

- No changes to the Maestro interop contract (`.executor-state.db`,
  `--json-result`) — additive feature + docs + type fixes only.

## [2.1.0] — 2026-05-23

### Added — observability module (`spec_runner.obs`)

New canonical observability emitter shared across the ecosystem. Reference
implementation of the cross-project contract at
`_cowork_output/observability-contract/log-schema.json` (OpenTelemetry Logs
Data Model JSONL, one file per PID).

Public API:

- `obs.init_logging(project, level=..., log_dir=...)` — canonical entrypoint
- `obs.get_logger(module=...)` — bound structlog logger
- `obs.span(event, **attrs)` — context manager for spans with error chains
- `obs.child_env()` — emits `TRACEPARENT` env vars for subprocess trace propagation
- `obs.current_trace_id()` / `current_span_id()` / `current_pipeline_id()` — accessors

Features:

- `TRACEPARENT` ingress: parses W3C trace context, uses parent span_id as initial
  `_span_id`; malformed values fall back to root span (warned, not fatal)
- Redaction processor with default blocklist (`api_key`, `token`, `password`,
  `secret`, `authorization`, `cookie`, `private_key`, …) extensible via env
- Timestamps emitted as both ns-string and ISO micros (UTC)
- Contract validation against shared schema/fixtures (`tests/test_obs_contract.py`)

### Changed

- `spec_runner.logging` reduced to a 45-line back-compat shim that delegates
  to `obs.init_logging` / `obs.get_logger`. Existing imports of
  `setup_logging`, `get_logger`, `redact_sensitive` continue to work unchanged.

### Notes

- No changes to the Maestro interop contract (`.executor-state.db`,
  `--json-result`) — observability is additive and does not affect R-04.
- Minor bump (additive feature, fully back-compatible). Already vendored
  into Maestro (M1+M2), arbiter (Rust `arbiter-core::obs`), and ATP.

### Also

- Dependabot: patched 5 alerts (urllib3 2.6.3→2.7.0, python-multipart
  0.0.26→0.0.29, idna 3.11→3.16, python-dotenv 1.2.1→1.2.2). Transitive
  bumps only — no direct dependency changes.
- `.gitignore`: ignore `COWORK_CONTEXT.md`, `_cowork_output/`, and obs
  runtime output under `logs/`.

## [2.0.0] — 2026-04-17

Baseline release. See `TODO.md` and `docs/state-schema.md` for the frozen
R-04 Maestro interop contract (SQLite state schema, `--json-result` stdout,
golden fixtures under `tests/fixtures/maestro-interop/`).

[Unreleased]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.2.2...HEAD
[2.2.2]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.2.1...v2.2.2
[2.2.1]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.2.0...v2.2.1
[2.2.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.0.0...v2.1.0
