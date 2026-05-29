# Changelog

All notable changes to spec-runner are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per CLAUDE.md: any change to `.executor-state.json` / `--json-result` format
is a **breaking change** and requires a major version bump plus an entry here.

## [Unreleased]

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
