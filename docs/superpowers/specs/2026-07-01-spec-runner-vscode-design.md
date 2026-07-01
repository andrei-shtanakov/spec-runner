# spec-runner VSCode Extension — Design

**Date:** 2026-07-01
**Status:** Design (approved for planning)
**Target repo:** new sibling `spec-runner-vscode` (this doc lives in spec-runner for now; relocate on scaffold)

## Problem

spec-runner is CLI/TUI-only. Driving the full lifecycle — authoring/approving gated specs
and running/monitoring tasks — means switching to a terminal. A VSCode extension can surface
that lifecycle natively inside the IDE, next to the code and the spec files, without
reimplementing spec-runner's logic.

## Goals

- Cover the **full lifecycle in the IDE, both surfaces equally**: gated spec governance
  (generate → approve/reject stages, validation gates) **and** task execution
  (run/monitor tasks, live status, cost).
- Be a **thin extension over spec-runner's existing contracts** — no duplicated logic.
- Feel **native** (VSCode TreeViews, not a custom app).

## Non-goals (MVP)

- Cost dashboard / traceability matrix panel (`report --json`).
- Kanban webview, drag-drop status changes.
- Editor CodeLens / decorations surface.
- watch mode, sync-to-gh, adopt UI, doctor.
- Multi-project concurrency, multiple simultaneous runs.
- MCP backend for in-IDE agents.
- Marketplace publishing polish (walkthrough, icon set) — v1 ships as `.vsix`.

## Key Decisions (from brainstorming)

- **User / job:** full lifecycle, author-side and exec-side equally weighted.
- **Backend:** CLI/JSON + file-watch (not MCP).
- **UI:** native TreeView, three sections (SPEC / TASKS / RUN).
- **Scope:** Tier B — includes gated generation (`plan --gated`) with streaming + confirm.

## Core Principle

**The extension never writes spec files or the state DB.** Every mutation goes through the
spec-runner CLI, so atomic-locked frontmatter writes, re-validation, and governance stay
authoritative in spec-runner. The extension is a **read-model + action-dispatcher**.

## Prerequisites — spec-runner contract additions (land BEFORE the extension)

Code review found that the machine read paths (per-task list, run-level aggregate, governance
frontmatter) were unprotected against drift, and that live "current stage" was aimed at data
that isn't on stdout. The extension cannot close these itself — they are small spec-runner-side
changes that must land first, as a separate spec-runner PR, and become part of the pinned
`extension ↔ spec-runner` contract:

1. **Publish `schemas/status.schema.json` and `schemas/costs.schema.json`** for the two machine
   read surfaces, whose shapes differ from each other and from `executor-state.schema.json`
   (`{tasks, consecutive_failures, total_completed, total_failed}`, which the design originally
   mis-targeted):
   - `status --json` (`cmd_status`) is a **flat aggregate** — `{total_tasks, completed,
     failed, running, not_started, total_cost, input_tokens, output_tokens, budget_usd}`, with
     **no per-task array**. It feeds the RUN summary/budget only.
   - `costs --json` (`cmd_costs`) is the **per-task** surface — `{tasks: [{task_id, name,
     status, cost, attempts, input_tokens, output_tokens}], summary}`. It is what actually
     backs the TASKS tree.
   Add fixture/contract tests in spec-runner so both shapes are pinned.
   **Pin the `status` enum in `costs.schema.json`.** `cmd_costs` emits a **mixed vocabulary**:
   `ts.status` from the DB (`pending/running/success/failed/skipped`) when the task has a state
   row, else `t.status` from `tasks.md` (`todo/in_progress/done/blocked`) — neither matches the
   `todo/in_progress/done/blocked` the TASKS tree declares. Pinning the real (union) enum forces
   an explicit decision: either normalize on the spec-runner side, or accept the union and map
   it in the extension (`model.ts`). Without this the TASKS icons silently break on an
   "unknown" status.
2. **Publish `schemas/spec-frontmatter.schema.json`** for the `SpecMeta` frontmatter
   (`spec_stage`/`status`/`version`/`validation`/…). The extension is a second reader of this
   format; without a published+pinned schema its TS reader can silently drift. (A future
   `spec status --json` would also solve this, but publishing the schema is the minimal change
   and keeps the extension's direct-frontmatter-read valid.)
3. **Progress signal decision (RUN "current stage").** `⏳ stage: <name>` is emitted to
   **stderr** via the structlog console sink (`stages.py` → `log_progress` → `logging.py`), as
   *human* progress, not a stable machine contract. Decision for MVP: treat stderr stage lines
   as **advisory** best-effort; the **source of truth** for the RUN view is JSON polling
   (`costs --json` for per-task states, `status --json` for the run-level aggregate). A
   dedicated machine progress line in `run` is deferred (a separate contract line
   if we later want a reliable live stage).

**Version pin:** the extension pins `spec-runner >=` the release that contains gated generation
(#28, currently on `master`, unreleased — will be the next tagged version) **and** the three new
schemas above. Half-working against an older spec-runner is thereby prevented.

## Architecture

### Read vs Action split

- **Read (state), refreshed by file-watchers:**
  - Per-task execution + cost (id/name/status/cost/attempts/tokens) → `spec-runner costs
    --json` → `{tasks, summary}` (validated against the **new** `schemas/costs.schema.json`).
    This — NOT `status --json` — is the per-task surface that backs the TASKS tree.
  - Run-level aggregate + budget → `spec-runner status --json` → the **flat** `{total_tasks,
    completed, failed, running, not_started, total_cost, …, budget_usd}` (validated against the
    **new** `schemas/status.schema.json`; it has **no** per-task array). Feeds the RUN
    summary/budget. Neither is `executor-state.schema.json`, whose shape differs; see
    Prerequisites.
  - Governance state per stage (status/version/validation) → read the YAML **frontmatter**
    of `spec/${prefix}{requirements,design,tasks}.md` directly in TS, validated against the
    **new** `schemas/spec-frontmatter.schema.json` (see Prerequisites). Reads are race-safe:
    `write_spec` uses temp-file + atomic `os.replace`, so the TS reader always sees the whole
    old or whole new file, never a torn one — a watcher debounce is sufficient.
  - Static task structure (name/priority/deps) → from `costs --json` where present; fall
    back to reading `tasks.md`.
- **Action (write/execute) — CLI only:** `spec approve/reject`, `plan --gated --no-interactive`,
  `run`, `stop`.

### Components (TS modules, one responsibility each)

| Module | Responsibility |
|---|---|
| `cli.ts` | `SpecRunnerCli` adapter: locate binary; `run(args)`, `runStreaming(args, onLine)`; typed wrappers `statusJson()` (run-level aggregate), `costsJson()` (per-task list), `approve(stage)`, `reject(stage)`, `generate(stage, desc)`, `runTask(id)`, `runAll()`, `stop()`. Parses JSON against vendored `schemas/*.json`. |
| `specState.ts` | Frontmatter reader (YAML) → `SpecStage[] {stage, status, version, validation}`. |
| `model.ts` | Combined `WorkspaceState {stages, tasks, summary, running}` — single source the trees render. Owns the **status-normalization layer**: maps both `costs --json` vocabularies (DB `pending/running/success/failed/skipped` and tasks.md `todo/in_progress/done/blocked`) → the tree's canonical `todo/in_progress/done/blocked` (+ an explicit `unknown` fallback icon, never a silent drop). |
| `watchers.ts` | `FileSystemWatcher` on `spec/${prefix}*.md` + the **actual** state-DB path resolved from config (default `spec/.executor-${prefix}state.db*`, but honor a custom `state_file`/`paths.state`) → debounced refresh. |
| `trees/specTree.ts` | SPEC governance `TreeDataProvider`. |
| `trees/tasksTree.ts` | TASKS `TreeDataProvider`. |
| `trees/runTree.ts` | RUN status `TreeDataProvider`. |
| `commands.ts` | Command handlers → CLI adapter + confirm dialogs. |
| `output.ts` | `OutputChannel` for streamed logs; status-bar item for the active run. |
| `config.ts` | Settings (binary as `{command, args[]}`, confirmBeforeRun/Generate, specPrefix); resolves the state-DB path and the pinned min-version. |
| `extension.ts` | `activate()` — wire everything. |

### Data flow

`action → confirm → spawn CLI (stream to OutputChannel) → on completion, file-watcher
reloads model → trees update`. No direct file writes from the extension.

## The Three TreeViews

### SPEC (governance)

Per stage requirements/design/tasks: status icon (draft/approved/stale — the only three
states; `reject` returns a stage to `draft`, so icon logic needs just those three), version,
validation. Root shows `governance: strict|off` — read from the **`spec_governance`** key in
`spec-runner.config.yaml` (exact key name). Context-sensitive inline buttons:
- file exists → **Edit** (open in editor).
- `draft`/`stale` and validation ≠ fail → **Approve**; **Reject** (if managed); **Regenerate**.
- stage missing and upstream approved → **Generate**.

**Validation is a cache, not live truth.** The frontmatter `validation` field is only refreshed
by `spec check`/`spec approve` — a manual **Edit → save** of the body does **not** re-run the
validator, so a re-read shows a *stale* verdict. The button's enabled/disabled state must not be
trusted as ground truth: (a) run `spec check <stage>` after an Edit-save (and/or on stage-node
focus) to refresh the cached verdict before deciding button state; (b) `spec approve` always
re-validates from scratch, so treat **its** rc/stderr as the primary source of truth — an
Approve that looks available on a really-invalid file is safe (approve → rc≠0 → error
notification), but the `spec check` refresh avoids the confusing state.

### TASKS (from `costs --json`)

id + name + status. **Note the status vocabulary is not uniform** — `costs --json` reports DB
states (`pending/running/success/failed/skipped`) for tasks that have run and tasks.md states
(`todo/in_progress/done/blocked`) for those that haven't; `model.ts` normalizes both to the
tree's canonical `todo/in_progress/done/blocked` with an explicit `unknown` fallback. Inline:
**Run** (ready tasks), **Logs**. Section header: **Run all**, **Stop**.

### RUN

Active task, current stage (**advisory**, best-effort from stderr `⏳ stage:`; may be absent),
summary (done/failed), budget — the reliable fields come from JSON polling: run-level summary
and budget from `status --json`, per-task states from `costs --json`; the stage line is a nicety.

## GUI Action → CLI Mapping

All via `SpecRunnerCli`, with a confirm dialog where code executes / budget is spent:

| Action | CLI |
|---|---|
| Approve `<stage>` | `spec approve <stage>` (re-validates; rc≠0 → show errors as notification) |
| Reject `<stage>` | `spec reject <stage>` |
| Generate/Regenerate `<stage>` | `plan --gated --stage <stage> --no-interactive` (**`--no-interactive` required** — the GUI *is* the menu; first generation prompts for a description via `InputBox`) — **confirm** (LLM, cost) |
| Edit `<stage>` | `vscode.window.showTextDocument(file)`; on save → `spec check <stage>` to refresh the cached validation verdict |
| Run task | `run --task=<id> --json-result` (stream + final JSON) — **confirm** |
| Run all | `run --all --json-result` — **confirm** |
| Stop | **SIGTERM the child process** (the extension owns its PID; spec-runner's signal handler shuts down gracefully), SIGKILL after 5 s. Do **not** also call `spec-runner stop` — that targets a *different/detached* run via a stop-file and would be redundant/confusing here. |

## Gated-Checkpoint UX in the GUI

The CLI menu `[a/e/r/s/q]` is **not** emulated as an interactive session. Its roles map to
native elements:
- `a` (approve) → **Approve** button on the stage node.
- `e` (edit) → **Edit** opens the file; on save, the file-watcher re-reads frontmatter **and
  runs `spec check <stage>`** to refresh the cached `validation` verdict (a body edit alone does
  not re-validate — see "Validation is a cache" under SPEC).
- `r` (regenerate) → **Regenerate**.
- `s`/`q` (stop/abort) → do nothing / dismiss.
- Auto-continue to the next stage → a palette command **"Generate next stage"** that reads
  `resolve_next_stage` from frontmatter and generates the next unresolved stage (or the
  **Generate** button simply appears on the right node).

File-based frontmatter + `--no-interactive` deliver the same gated flow, button-driven rather
than stdin-driven. No interactive CLI session is held.

## Live Updates & Streaming

Reality of spec-runner's streams (verified against code) drives this section:
- **stdout** carries only the final `--json-result` — a **single multi-line
  `json.dumps(indent=2)` block emitted once, after all tasks finish** (`cli.py:555`). It is
  NOT a per-line stream. For `run --all` it is a JSON **array** at the end; for `run --task`
  a single object. There is no intermediate per-task JSON on stdout.
- **stderr** carries human progress, including `⏳ stage: <name>` lines (structlog console
  sink) — advisory, not a pinned contract.

Mechanism:
- `FileSystemWatcher` on `spec/${prefix}*.md` → re-read frontmatter (governance); on
  `tasks.md` change also refresh `costs --json` (task list) + `status --json` (aggregate).
- `FileSystemWatcher` on the **config-resolved** state-DB path (+ `-wal`) — default
  `spec/.executor-${prefix}state.db`, but resolve a custom `state_file`/`paths.state` from
  config rather than assuming default naming → debounce 300–500 ms → `costs --json` +
  `status --json`. The DB is read **only** via these CLI commands (no sqlite driver in TS, no
  coupling to the DB schema).
- `runStreaming` spawns `spec-runner … --json-result` and:
  - pipes **stderr** lines to the `OutputChannel`; best-effort matches `⏳ stage: <name>` to
    update the RUN node + status bar (**advisory** — if the format changes it degrades to "no
    live stage", not breakage).
  - buffers **stdout** and, on process exit (code 0), parses the whole buffer as JSON and
    validates it against `schemas/json-result.schema.json` (the pinned Maestro-interop
    contract) → updates task state.
- **Source of truth for per-task liveness = `costs --json` polling** (run-level summary/budget
  from `status --json` alongside it). Poll on a **fixed cadence** (every **3–5 s**) while a run
  is active — do **not** gate the authoritative poll on stderr silence: stderr `⏳ stage:` lines
  carry no task-state, so a chatty stderr must not suppress the poll (that would let per-task
  state go stale). stderr drives only the decorative stage label. Each poll spawns a Python
  process reading the WAL DB — safe but not free, so keep the cadence sparse. Polling stops when
  the child exits.
- One active run process at a time (MVP).

## Safety

- `run` / `run all` / `generate` execute code / spend budget → **modal confirm** with explicit
  text ("executes code, git operations, spends API budget"). Settings
  `spec-runner.confirmBeforeRun` (default `true`) and `spec-runner.confirmBeforeGenerate`.
- Show `governance` (strict/off) in the SPEC header so strict mode is visible.
- The extension does **not** bypass the governance gate: `run` under strict with an unapproved
  `tasks.md` returns `⛔`; surface it as an actionable notification with an "Approve tasks" link.
- No auto-run, no network I/O from the extension (all I/O is the local spec-runner).

## Discovery & Config

- Binary: setting `spec-runner.path` (default `spec-runner` on PATH); auto-detect
  `.venv/bin/spec-runner` / `uv run spec-runner` at the workspace root. **Model the invocation
  as `{command, args[]}`, not a single path string** — `uv run spec-runner` is a command with a
  leading arg, so a string-path model breaks `spawn`. Missing binary → a one-time actionable
  notification ("install: `uv tool install spec-runner` / set path"); the extension degrades to
  read-only (frontmatter + tasks.md are still readable).
- **Version check on activation:** run `spec-runner --version`, parse semver, compare to the
  pinned minimum (the release carrying #28 + the new schemas). Below the pin → a hard warning
  and degrade to **read-only** (don't dispatch actions against an incompatible contract).
- Activation: any of `spec/tasks.md`, `spec/${prefix}tasks.md`, `spec/*.md`, or
  `spec-runner.config.yaml` present (`activationEvents: workspaceContains`). The bare
  `spec/tasks.md` glob misses phase-only projects whose file is `spec/phase2-tasks.md`, so
  match `spec/*.md` too.
- `spec_prefix`: setting `spec-runner.specPrefix` (phase2-* etc.) threaded into all CLI calls
  and watcher paths.
- Multi-root workspace: MVP handles one active project (first found), with a header switcher;
  full multi-project is deferred.

## Testing

- **Unit:** `cli.ts` — parse real `status --json` / `costs --json` / `--json-result` samples
  validated against vendored `schemas/*.json`; `specState.ts` frontmatter reader; command→argv
  mapping (mock `child_process.spawn`). No real spec-runner / LLM needed.
- **Integration:** `@vscode/test-electron` + a **fake `spec-runner` script** on PATH (mirrors
  spec-runner's `tests/fixtures/fake_claude.sh` pattern) emitting canned JSON → drives
  `activate → tree render → command → refresh` without Python or an LLM.
- **Contract:** the pinned surface is **four** vendored schemas — `json-result.schema.json`
  (existing) + the three new `status.schema.json`, `costs.schema.json`, and
  `spec-frontmatter.schema.json` (see Prerequisites). `executor-state.schema.json` is **not**
  vendored unless a path actually reads raw state (none does today). A test asserts the sample
  fixtures validate against the vendored copies, so drift in any of the extension's real read
  paths is caught — not just json-result.

## Packaging & Repo

- New sibling repo **`spec-runner-vscode`** in the monorepo (TS/npm toolchain, own git — like
  the other ecosystem projects). Not a subdir of spec-runner (different language/CI).
- Dependency on spec-runner is a **contract**: vendored schemas (`json-result` + `status` +
  `costs` + `spec-frontmatter`) + a concrete "min spec-runner version" pin — the release
  carrying #28 and the three new schemas (see Prerequisites). Mirrors the Maestro-interop
  golden-fixtures pattern; keeps the two decoupled but versioned.
- Distribution v1: `.vsix` (manual install) → Marketplace later. Requires `spec-runner`
  installed separately (the extension does not bundle it).
- **First TypeScript/npm toolchain in the ecosystem.** At scaffold time, register
  `spec-runner-vscode` in `COWORK_CONTEXT.md` and note it introduces a JS toolchain/CI to the
  monorepo (deferred to scaffold, not done here).

## Risks / Notes

- **Machine read paths needed contracts** (fixed in this design) — per-task data (`costs
  --json` → `{tasks, summary}`), run-level aggregate (`status --json` → flat counts/budget),
  and frontmatter were the most-used yet least-protected paths; the design originally
  mis-targeted per-task reads at `status --json` (whose real shape is the flat aggregate, not
  `{tasks, summary}`) and, before that, at `executor-state.schema.json`. Closed by the
  Prerequisites (publish + pin `status.schema.json`, `costs.schema.json`, and
  `spec-frontmatter.schema.json`).
- **Live stage is advisory** — `⏳ stage:` is stderr human-progress, not a machine contract;
  the RUN view degrades gracefully to `costs --json` / `status --json` polling if it's absent.
- **SQLite WAL churn** — the state-DB watcher and the run-time poll must debounce/space out
  (3–5 s) or they thrash: each `costs --json` / `status --json` spawns a Python process.
- **Mixed task-status vocabulary** — `costs --json`'s `status` is DB vocab
  (`pending/running/success/failed/skipped`) for run tasks and tasks.md vocab
  (`todo/in_progress/done/blocked`) otherwise; `model.ts` must normalize both (+ an `unknown`
  fallback) and `costs.schema.json` should pin the union enum so drift is caught.
- **Validation cache vs manual edits** — the frontmatter `validation` field is refreshed only
  by `spec check`/`spec approve`; a body Edit-save leaves a stale verdict. The extension runs
  `spec check <stage>` after edits and treats `spec approve`'s rc as the source of truth.
- **Config-driven paths** — a custom `state_file`/`spec_prefix` moves the state DB and spec
  files; watchers/CLI must use config-resolved paths, not default naming. Activation must match
  `spec/*.md` (not just `spec/tasks.md`) or phase-only projects never activate.
- **Version skew** — an older `spec-runner` may emit different JSON/CLI; a `--version` check on
  activation gates against the pin and drops to read-only below it.
- **Cross-repo sequencing** — the three spec-runner schema additions (+ pinned `--version`) must
  land (a small spec-runner PR) before the extension can honor its pin; the plan sequences
  spec-runner-side first, extension second.
- **Contract drift** — vendored schemas (now four-surface) + a concrete version pin + a
  contract test are the guard.
- **Interactive parity** — the `plan --gated` TTY menu is intentionally not reproduced; the
  button-driven flow is the GUI equivalent.
