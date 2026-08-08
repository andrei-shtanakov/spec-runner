# Changelog

All notable changes to spec-runner are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per CLAUDE.md: any change to `.executor-state.json` / `--json-result` format
is a **breaking change** and requires a major version bump plus an entry here.

## [Unreleased]

### Changed

- **Docs: the `review-pr` caller contract is consumer-agnostic** (PR #119).
  Clarified that Maestro lifecycle mapping is consumer-owned and not part
  of the `review-pr` CLI contract. The design doc's "External caller
  contract" section now states only what spec-runner promises (invocation,
  exit codes 0/1/2, one-JSON-document stdout, stderr diagnostics,
  idempotent resume, mutating-mode preconditions) and points at the
  consumer's own track instead of restating its lifecycle. The v2.20.0
  release notes are left as the historical artifact they are.

## [2.21.1] — 2026-08-08

Three fail-closed fixes for task-status integrity, found by a live
disputatio run (D3, 2026-08-08; maestro#164) whose forensic snapshot
(`tasks-193159.md`) is now this release's golden regression fixture
(`tests/fixtures/maestro-interop/alternating-bullet-tasks.md`).

### Fixed

- **`update_task_status` is task-bounded** (issue #123). The status rewrite
  now matches the target task's header by exact ID — not substring, so
  `TASK-001` no longer matches `TASK-0011` — and only searches for a meta
  line between that header and the next one. A write that can't find its
  own task's meta in that window returns `False` without touching the file
  or the history log, instead of falling through onto a neighboring task's
  meta line (the incident: updating `TASK-001` repainted `TASK-002`).
- **`TASK_META` recognizes bullet-prefixed meta lines** (issue #123).
  Agents editing `tasks.md` mid-run introduce a bullet prefix on meta lines
  (`- 🔴 P0 | ...` / `* P0 | ...`), confirmed forensically via git-status
  correlation on the incident snapshot — not the generator templates,
  which emit the bare form. The parser and `update_task_status` previously
  only matched the bare form, so those tasks' meta was invisible to both;
  the parser must now accept both formats regardless of source. The
  bullet prefix requires the `P\d |` form immediately after it, so plain
  description bullets and checklist items stay unaffected. The bundled
  `spec-generator-skill` template copy of both fixes was kept in sync.
- **`run --all` fails closed on a state-DB/tasks.md mismatch** (issue #124).
  Two gates now stop the run non-zero (`state_spec_mismatch`) instead of
  reporting exit 0: immediately after each task, if the state DB just
  recorded success but tasks.md doesn't show `done`; and as a backstop when
  the loop runs out of ready tasks, if any state-DB success was never
  reflected in tasks.md. A legitimate block (a TODO waiting on a documented
  failed/skipped dependency) leaves both sets in agreement and is
  unaffected.

## [2.21.0] — 2026-08-06

Unblocks Maestro's accepted `post-pr-command` work (design maestro#147):
`review-pr --json` is now a clean machine interface on every exit path.
Minor, not patch — the payload gains an additive `exit_code` key
(same rule as `no_op` in 2.16.0). Maestro pins this version before
shipping its wrapper.

### Fixed

- **`review-pr --json`: stdout is exactly one JSON document** (inbox issue
  #116 from maestro's `post-pr-command`; PR #117). Limit stops and
  fail-closed paths printed diagnostics to stdout before the report, and
  exit 1 emitted no JSON at all — a consumer storing the report verbatim
  (Maestro's `review-pr` wrapper, design maestro#147) could not parse it.
  All diagnostics now go to stderr; every exit path (0/1/2) emits one JSON
  document, and the payload gains `exit_code` so a stored report is
  self-describing. On exit 1 the document is
  `{repo, pr_number, error, exit_code}` (`repo`/`pr_number` `null` when the
  ref could not be resolved). Text mode is unchanged.

## [2.20.0] — 2026-08-06

Phase M3 completes the review-bot loop (issue #102, now closed): the
optional post-PR stage wires `review-pr` into the run itself, and the
external caller contract is documented for orchestrators. All three
phases shipped the same day the design was approved (M1 v2.18.0,
M2 v2.19.0, M3 here). Maestro interop contract unchanged.

### Added

- **`review-pr` phase M3 — optional post-PR stage + external caller
  contract** (issue #102, final phase; PR #114). `review_pr.post_pr:
  off | verify | full` (default `off` — the `integration_pr` flow stays
  byte-identical without configuration) wires the loop into the run
  itself: after the integration PR is announced, the stage waits
  `post_pr_wait_seconds` (default 120) for the review bot, then runs the
  read-only triage (`verify`) or checks the run branch out, runs the full
  fix+reply loop, and always returns to the base branch (`full`). The
  stage never changes the run's exit status. The external caller contract
  (exit codes 0/1/2 + `--json` surface, Maestro hook mapping) is now
  documented in the design doc. This completes #102.

## [2.19.0] — 2026-08-06

Phase M2 of the review-bot loop (issue #102): `review-pr` now closes the
whole cycle — verify, fix, gate, push, reply — under hard limits and
fail-closed rules. Only the optional post-PR stage (M3) remains. The
Maestro interop contract is unchanged; the experimental review-pr state
tables gain resolution/reply bookkeeping.

### Added

- **`review-pr` phase M2 — fix + reply** (issue #102; PR #112). The default
  invocation now runs the full loop: valid comments are fixed by a TDD
  agent (each fix is a separate commit with a `Review-Comment-Id`
  provenance trailer), the project gates (tests + lint) run after every
  mutation and a failing gate reverts the fix, all fix commits are pushed
  once, and only after a successful push does the loop reply in each
  thread — with the actual fix SHA, or with the verification evidence for
  refuted comments. `uncertain` comments are never fixed and never
  auto-answered. Hard limits (`review_pr.max_rounds` / `max_comments` /
  `max_changed_lines` / `max_cost_usd` / `max_wall_minutes`) stop the loop
  with `NEEDS_HUMAN`; dirty tree, head-SHA mismatch, force-push and push
  failures are fail-closed (exit 1, no replies). Reply idempotency is
  persisted (`replied_at`) — re-invocations never answer twice.
  `--verify-only` preserves the read-only M1 behavior and its exit
  semantics; `spec-runner status` now surfaces comments awaiting a human.

## [2.18.0] — 2026-08-06

Phase M1 of the review-bot loop (issue #102): the read-only
`review-pr` command ships; fix/reply (M2) and the post-PR stage (M3)
follow. The Maestro interop contract is unchanged — the new
`pr_review_comments` state table is experimental and additive.

### Added

- **`spec-runner review-pr <url-or-number>` — phase M1 of the review-bot
  loop** (issue #102, battle-testing F-22; design:
  `docs/superpowers/specs/2026-08-06-review-pr-loop-design.md`; PR #110).
  Read-only: collects inline PR comments from allowed bot identities
  (`review_pr.allowed_bots` config, default Copilot), verifies each against
  the codebase with an agent call, persists per-comment verdicts
  (`valid`/`refuted`/`uncertain`) in a new `pr_review_comments` table — the
  durable cursor makes re-invocations resume instead of re-processing — and
  prints a text or `--json` report. Fail-closed throughout: draft/closed
  PRs and API errors exit 1; missing verdict markers and verifiers that
  mutate the working tree become `uncertain`; any uncertain/unverified
  comment exits 2 (`NEEDS_HUMAN`). Stable exit-code contract (0/1/2) for
  external callers (the future Maestro hook). No fixes, no replies, no
  pushes in M1.

## [2.17.0] — 2026-08-06

Battle-testing round 4 (kapelle TASK-007 on v2.16.0, run d4d33ad0):
three of the four findings fixed the day they were filed. The Maestro
interop contract is unchanged; the notification surface gains one event.
The fourth finding (#102, review-bot loop) awaits an ownership decision.

### Added

- **`pr_opened` notification event** (battle-testing F-21, issue #101;
  PR #107). When a run opens an integration PR, the event is now pushed
  through the configured Telegram/webhook channels (and is in the default
  `notify_on`), so the human-merge gate no longer depends on someone
  watching the terminal. External consoles (e.g. a dispatcher inbox) can
  consume the webhook.

### Fixed

- **Exec-stage work is committed under the task label before review runs**
  (battle-testing F-23, issue #103; PR #105). The review stage commits its
  own fixes, and with nothing committed before it, that commit swept the
  entire feature under a "code review fixes" label while the final task
  commit got only the tasks.md leftovers — git history inverted relative
  to content (kapelle PR #6). The pre-review commit also protects the work
  from the next task's pre-start cleanup, and the #97 no-op detection
  accounts for it (a task whose work was captured pre-review is not
  flagged no-op by the bookkeeping-only final commit).
- **Execution summary reports this run's counts** (battle-testing F-24,
  issue #104; PR #106). `completed`/`failed`/`failed_attempts` in the
  end-of-run summary, the `run_complete` notification and the `run_ended`
  audit record were cumulative across runs (monotonic executor_meta
  counters and full attempt history); a single-task run could end with
  `completed=2`. They now report the delta for the run; the cumulative
  counters themselves are unchanged.

## [2.16.0] — 2026-08-05

Battle-testing S2 round 3 (kapelle, Maestro orchestration): both findings
fixed the day they were filed. The Maestro interop contract gains one
additive `--json-result` key (`no_op`, emitted only when true) — non-breaking
per the change policy in `docs/state-schema.md`; all pre-existing golden
fixtures are byte-identical.

### Added

- **Explicit `no-op` completion marker** (battle-testing S2 finding F-20,
  issue #97, maestro side maestro#123; PR #99). A task that completes with
  nothing to commit (its work was already absorbed by earlier tasks) is now
  visibly a no-op instead of looking like a silently-missing task: new
  `no_op` column on `attempts` (idempotent migration), `"no_op": true` in
  `--json-result` (additive, emitted only when true — existing consumers see
  byte-identical output; new golden fixture `json-result-single-noop.json`),
  `[no-op]` tag in `spec-runner status` task history, and a
  `✔️ No-op: completed without changes` progress line. Non-breaking per the
  change policy in `docs/state-schema.md`.

### Fixed

- **Harness-written `spec/.gitignore` no longer lands in auto-commits**
  (battle-testing S2 finding M-03, issue #96, counterpart of maestro#122;
  PR #98). The #62 fix writes `spec/.gitignore` to protect executor runtime
  state, but `git add -A` in `stage_all_except_runtime` swept it into the
  first subtask's auto-commit — a file no agent chose to create in the
  workstream diff, which Maestro's ex-post scope gate rightly flags as a
  scope escape (both parallel kapelle S2 workstreams went NEEDS_REVIEW
  with all subtasks green). The file is now excluded from the commit set
  when it is not tracked in HEAD; a user-tracked `spec/.gitignore` keeps
  the old travels-with-the-spec behavior and is never staged for deletion.

## [2.15.0] — 2026-08-05

Long-tail cleanup after the two battle-testing waves: the oldest open user
breakage (`--spec-prefix` swallowed by the CLI parser) and the DEC-007
documentation drift. The Maestro interop contract is unchanged;
`SPEC_META_CONTRACT` stays 2 (the golden fixture's example value changes,
not the contract shape).

### Fixed

- **Common flags placed before the subcommand are no longer swallowed**
  (TODO slug `spec-prefix-swallow`, found during the C1 dogfood; PR #93).
  Every option of the shared `common` parent was declared on the top-level
  parser AND on each subparser; the subparser re-applied its default after
  the top-level parse, silently clobbering values given before the
  subcommand — `spec-runner --spec-prefix=phase2- run` ran unprefixed, and
  spec-runner-vscode emits exactly that argv order, so its `specPrefix`
  setting never reached the CLI. The same swallow hit every common flag
  (`--budget`, `--no-review`, …). The `spec status/approve/reject/adopt/
  check` family additionally rejected the flag outright (not parented on
  `common`). Fix: `common` uses SUPPRESS defaults and the top-level parser
  restores documented defaults once after the full parse (with a build-time
  drift-guard assertion); the `spec` family gains the common options. When
  a flag is given both before and after the subcommand, the subcommand
  position wins. The VSCode extension needs no changes — its argv order now
  works as-is.

### Changed

- **`owner_role` documentation aligned with DEC-007** (steward role catalog,
  decided 2026-07-26). `docs/CONTRACTS.md`, the spec-frontmatter schema, the
  inline comment and the **golden fixture shipped as package data** now show
  the canonical single role-slug form (`platform` — one accountable role, no
  `@`) instead of the retired `"@role[,@role]"`. No behaviour change:
  spec-runner remains a pure carrier (string-or-None), and legacy values are
  still round-tripped verbatim — pinned by a new regression test — because
  steward's own data has not fully migrated. `SPEC_META_CONTRACT` stays 2.

## [2.14.0] — 2026-08-05

Second battle-testing wave: the five enhancement/hardening issues from the
2.11.0 field trial (#64, #66, #69, #72, #73). The Maestro interop contract
(`.executor-state.db` schema, `--json-result` stdout) is unchanged; the one
contract edit is an additive enum value on the VSCode read surface
(`costs.schema.json`), updated in lockstep with its drift-guard test.

### Added

- **Fail-closed dirty-spec pre-run guard** (#69; PR #87). `run`/`watch`/
  `retry` refuse (exit 1, offending `git status` lines printed) when the
  spec content files or the config have uncommitted changes — an executed
  spec now always has a committed version predating the run. Enforced only
  when spec-runner's own git automation is on (subdir projects keep a
  permanently dirty tasks.md by design); tracked deletions count as dirt; a
  failing `git status` fails closed; `--allow-dirty-spec` overrides.
  Orchestrators that keep generated specs gitignored (Maestro's
  info/exclude pattern) see zero dirt — verified against Maestro's
  workspace lifecycle before choosing the fail-closed default.
- **Integration-PR closed loop** (#73; PR #88). When a run opens an
  integration PR it now prints an explicit stderr block (merge required
  before the next run) and persists `last_run_pr_url` in `executor_meta`;
  `status` repeats the marker until the new **`spec-runner sync`** command
  clears it. `sync` is the post-merge closer: no-active-run lock check,
  clean worktree (executor runtime state never counts), switch to base,
  `pull --ff-only` + `fetch --prune`, deletion of **merged-only** `task/*`
  and `spec-runner/run-*` branches locally and on the remote
  (ancestor-check, never force, foreign branches untouched), state sanity.
  Each step reports ✓/✗ with detail; failed deletions fail the step;
  `--dry-run` previews; non-zero exit when the tree can't host the next run.
- **Task ids accept any uppercase prefix** (#72; PR #89). `### KAP-002:`
  headers parse natively (`ID_PATTERN`); `Depends on:`/`Blocks:` refs are
  filtered against the prefixes task headers actually use, so `[REQ-001]`
  in those lines stays documentation. gh-sync title matching generalized.
  Zero config — the proposed `task_id_prefix` key turned out unnecessary.
- **Harness-mutation tripwire** (#64; PR #90). The verification harness
  (dependency manifests, pytest/tox/setup configs, conftest.py,
  package.json/mix.exs/Cargo.toml/go.mod, Makefile, CI workflows, plus
  `harness_files` extras) is snapshotted before the agent runs;
  created/modified/deleted files are violations checked BEFORE the gates.
  `harness_guard: warn` (default) logs provenance; `strict` fails the
  attempt with a retry-prompt-feeding error (exemptions via
  `harness_allow` globs); `off` disables. Unreadable files get a sentinel
  hash (a chmod-000 file cannot bypass the guard); config values are
  validated at load.
- **Intermediate `🔍 REVIEW` file status** (#66; PR #91). Written to
  tasks.md when the tests/lint gates pass and code review starts — a run
  killed during review leaves an honest intermediate state instead of a
  premature DONE. Scheduled like IN_PROGRESS (resumable, not done for
  dependents, skipped by `--restart`). Maestro is unaffected (it reads
  only the SQLite state, whose vocabulary is untouched); the VSCode read
  surface maps unknown statuses to an explicit `unknown`, and its vendored
  `costs.schema.json` status enum gains `review` here in lockstep
  (re-vendor requested via spec-runner-vscode#16).

## [2.13.0] — 2026-08-05

Battle-testing release: every change comes from a field trial of 2.11.0 on an
external Elixir/Phoenix repo with the claude CLI (issues #62–#74). The Maestro
interop contract (`.executor-state.db` schema, `--json-result` stdout) is
unchanged — the one schema edit is additive on an experimental-tier column.

### Fixed

- **`doctor`'s $0.50 budget default leaked into every subcommand** (#68, #67;
  PR #78). Subparsers are built with `parents=[common]`, so argparse shares
  the `--budget` Action object across all of them —
  `doctor_parser.set_defaults(budget=0.5)` mutated that shared action and
  every command (`run`, `status`, `costs`, …) silently ran with a $0.50
  budget, overriding the YAML `budget_usd`. Once a previous run's recorded
  cost crossed $0.50, the next `run` refused to start. The doctor default now
  lives in `cmd_doctor` (`DOCTOR_DEFAULT_BUDGET_USD`); the parser default is
  `None` everywhere.
- **A refused run now names its actual cause and exits non-zero** (#67;
  PR #78). The pre-run `should_stop()` refusal always logged "Stopped due to
  consecutive failures" — with a contradictory counter of 0 when the real
  cause was the budget — and exited 0, so orchestrators read it as success.
  New `ExecutorState.stop_cause()` distinguishes `max_consecutive_failures`
  from `budget_exceeded`; the refusal prints the cause, persists it as
  `last_run_stop_reason` (new meta value `budget_exceeded`, experimental
  tier), records the audit event, and exits 1. Mid-run budget stops are
  persisted as `budget_exceeded` too instead of masquerading as
  `max_consecutive_failures`.
- **Executor runtime state can no longer be committed** (#62, root cause of
  #67's state loss; PR #79). `git add -A` in the auto-commit and in both
  review-fix commit sites swept `spec/.executor-state.db` (+`-wal`/`-shm`),
  the executor lock, progress file, task history and executor logs into the
  task branch; the tracked DB was then reverted by the next branch switch
  *under the open SQLite connection*, losing the run's success status and
  costs (`status` showed `running: 1`, `costs` showed $0.00) and blocking
  return-to-base in `integration_pr` mode. All commit sites now stage via
  `git_ops.stage_all_except_runtime()`, which also untracks state files
  committed by the old behavior; `pre_start_hook` maintains a
  `spec/.gitignore` covering runtime files (spec-prefix and change-dir
  aware); runtime-only churn no longer produces commits. The
  return-to-base failure is a loud stderr error with recovery instructions
  instead of a scrolled-away warning.
- **Review fixes are gated again before commit** (#65; PR #80). A
  `REVIEW_FIXED` verdict mutates the code after the tests/lint gates ran, so
  a broken review fix could be committed and merged as a "successful" run.
  `post_done_hook` now re-runs the full test suite and a strict lint check
  (deliberately without auto-fix) after a FIXED verdict; red gates fail the
  attempt with the usual `TEST_FAILURE`/`LINT_FAILURE` classification.
- **`run --dry-run --json-result` reported `checklist_done == total` for
  untouched tasks** (#71; PR #82). Checklist tuples are `(item, checked)` but
  were unpacked as `(done, _)`, counting truthy item *strings*. Now derived
  from `Task.checklist_progress`.
- **`status` no longer shows file-DONE tasks as "Not started"** (#68;
  PR #85). Tasks ticked ✅ DONE in `tasks.md` that the executor never ran
  (manual bootstrap, another tool) appear under a new "Done outside
  executor" bucket in the text display. `status --json` is a stable contract
  surface and is untouched.

### Added

- **Loud warning when no config file backs an execution command** (#63;
  PR #81). A missing `spec-runner.config.yaml` used to silently flip a run
  to all defaults — including `integration_pr=false` (self-merge into the
  main branch) and a Python test command on non-Python repos. `run`/`watch`/
  `retry` now print an operator-facing stderr warning naming the *effective*
  merge mode, test command and model; when prior run state exists (evidence
  the config vanished rather than never existed) the hint is sharper.
- **`commands.sync` config key + stack-aware dependency sync** (#70; PR #83).
  `pre_start_hook` hardcoded `uv sync`, producing per-run stderr noise on
  every non-Python project. A configured `commands.sync` (e.g.
  `mix deps.get`) now runs instead; with no key set, `uv sync` runs only
  when `pyproject.toml` exists, else the stage is skipped quietly.
  `sync_deps: false` still disables the stage entirely.

### Changed

- **Execution stage renamed `codex` → `exec`** (#74; PR #84). Run logs said
  `⏳ stage: codex` even when running the claude CLI. `error_stage` is an
  experimental-tier column: the state schema lists `exec` and keeps `codex`
  valid for rows written by ≤2.12.
- **Branch slugs strip punctuation** (#74; PR #84). Task names with commas
  or `+` produced branches like `task/task-004-fake-executor-+-fake-judge,-sy`
  — valid for git, brittle for tooling/URLs. Slugging now collapses
  non-alphanumeric runs into `-`, trims trailing dashes after truncation,
  and falls back to the bare task id for punctuation-only names.

## [2.12.0] — 2026-08-04

### Changed

- **`mcp` floor raised to `mcp>=2.0.0,<3`** — spec-runner now requires the MCP
  Python SDK v2 line; SDK v1 (`mcp<2`, the `[2.11.1]` ceiling) is no longer
  supported. This is a dependency-major bump for anyone pinning `mcp` directly
  alongside spec-runner.
- **`mcp_server.py` migrated from `FastMCP` to `MCPServer`.**
  `from mcp.server.fastmcp import FastMCP` → `from mcp.server import
  MCPServer`; `mcp_app = FastMCP("spec-runner")` → `mcp_app =
  MCPServer("spec-runner")`. All `@mcp_app.tool()` decorators and
  `mcp_app.run(transport="stdio")` are unchanged — the tool surface (status,
  tasks, costs, logs, run_task, stop, next_tasks, task_detail) is identical.
  Verified with a new in-memory wire test (`tests/test_mcp_v2_wire.py`) that
  drives SDK v2's `Client(mcp_app)` directly against the server object and
  asserts `list_tools()` returns all 8 tools — exercising the actual MCP
  protocol surface rather than only the Python-level handler functions.

### Fixed

- **`import spec_runner` no longer requires (or can be broken by) the `mcp`
  SDK.** `__init__.py` previously imported `mcp_server.run_server` eagerly at
  module load, so any problem with the `mcp` package — including the exact
  v1/v2 breakage this release line exists to fix — took down the whole
  package, not just the MCP server. `mcp_run_server` is now resolved lazily
  through `__getattr__`, imported only when actually accessed; a
  `TYPE_CHECKING`-guarded import keeps the name visible to static analyzers
  (resolving a pyrefly `bad-dunder-all` error) without importing `mcp` at
  runtime.

## [2.11.1] — 2026-08-04

### Added

- **CI guard against an untagged release.** `publish.yml` fires on a pushed tag
  and verifies it matches the pyproject version; nothing verified the reverse —
  a release commit landing on `master` with a bumped version and no tag. That is
  the failure that actually happened, twice: v2.4.0 and v2.10.0 both sat on
  `master` untagged, so PyPI lagged and consumers pinning the published version
  stayed blocked. The new `release-tag-guard` workflow never runs on pull
  requests, so a release PR stays green while its tag legitimately does not exist
  yet; it goes red the moment an untagged release commit merges. It also runs on
  `v*` tag pushes, so tagging the release commit re-runs it on the same SHA and
  the passing run supersedes the failed one — without that trigger the red would
  linger until an unrelated commit landed, since a tag push does not re-run a
  branch-triggered workflow. `workflow_dispatch` is available for the case where
  the tag lands on an older commit than master's HEAD.

### Fixed

- **`mcp` ceiling-pinned to `<2`.** mcp 2.0.0 (released 2026-07-28) removed
  `mcp.server.fastmcp`, which `mcp_server.py`'s `FastMCP` import depends on.
  The previous unbounded floor (`mcp>=1.26.0`) meant a fresh install pulled
  mcp 2.x and broke immediately — `src/spec_runner/__init__.py` imported the
  MCP server eagerly at the time, so even `import spec_runner` crashed, not
  just `spec-runner mcp`. Pinned `mcp>=1.26.0,<2` as an interim hotfix until
  the SDK v2 migration lands (see `[2.12.0]`).

## [2.11.0] — 2026-07-26

The SpecMeta frontmatter contract becomes losslessly extensible, and the spec
surface becomes genuinely profile-aware. Two governance defects that were live
in released versions are fixed: the `run --strict` gate could be bypassed under
a custom stage profile, and `plan --gated` crashed on one. The Maestro interop
contract (`.executor-state.db` schema, `--json-result` stdout) is unchanged.

Additive only — nothing is removed from any contract surface. Consumers pinning
the frontmatter contract should read `docs/CONTRACTS.md` for the field table, the
frozen public surface and the contract changelog, and pin `SPEC_META_CONTRACT = 2`.

### Added

- **`SpecMeta.owner_role: str | None`** — a first-class field carrying
  CODEOWNERS role(s) (`"@role[,@role]"`, e.g. `"@platform,@sre"`). The role
  semantics belong to the consumer (steward); spec-runner is only the
  carrier. `None` is omitted from rendered frontmatter, so existing spec
  files don't gain an `owner_role: null` key on their next write.
- **`SpecMeta.extra: dict[str, Any]`** — foreign frontmatter keys (e.g.
  steward's `traces_to`, `upstream_hashes`) are now preserved verbatim
  through parse and render. The canonical wire fields are
  `fields(SpecMeta) - {"extra"}`, computed by subtraction so an internal
  dataclass field can never silently widen the wire contract — meaning a
  frontmatter key literally named `extra` is itself foreign data and lands
  in `meta.extra["extra"]`.
- **`SPEC_META_CONTRACT = 2`**, declared upstream for the first time. v1 was
  the implicit historical contract a consumer (steward) had pinned by
  inferring it from observed behaviour. Bump policy: adding an optional
  field does not bump the contract; removing or renaming one does; the
  existence of `extra` never bumps it by itself.
- **A frozen public surface** exported from `spec_runner`: `SpecMeta`,
  `SpecMetaError`, `SPEC_META_CONTRACT`, `SPEC_STAGES`, `split_frontmatter`,
  `strip_frontmatter`, `split_frontmatter_raw`, `read_spec_meta`,
  `read_spec_body`, `write_spec`, `meta_from_dict`, `meta_to_dict`.
  Everything else in `spec.py` is private and outside the contract.
- **`docs/CONTRACTS.md`** — the field table, semantics, round-trip
  guarantee, bump policy and contract changelog for the SpecMeta frontmatter
  contract.
- **A golden fixture**
  (`src/spec_runner/contract_fixtures/spec_meta_contract_v2.md`) shipped as
  package data (`importlib.resources`), so a consumer can validate its own
  parser against the exact same bytes spec-runner tests against.

### Changed

- **Gated generation now gates on a stage's *direct* `requires` only.** The
  removed `_UPSTREAM` map demanded that both `requirements` and `design` be
  approved before `tasks` could generate, while `lite.yaml` declares `tasks`
  as requiring only `design`. The stage profile is the single source of
  truth, and `requires` describes direct DAG edges, not the transitive
  closure — which matters for branching profiles, where a hardcoded closure
  is actively wrong. In a normal lifecycle nothing changes: re-approving an
  upstream stales its downstream, so a staled `design` still blocks `tasks`.
  What's newly allowed is reachable through `spec reject` alone, with no file
  editing required: `cmd_spec_reject` writes only the rejected stage's own
  file and does not cascade stale, so `spec reject requirements` after
  `design` is already approved leaves `design` approved while `requirements`
  returns to draft. Even then, `resolve_next_stage` still auto-resolves to
  `requirements`, so only an explicit `plan --gated --stage tasks` reaches
  the `tasks` generator in that state — and it produces a draft that still
  needs `spec approve tasks` before `run --strict` will pass.
- **The generation prompt's context is deliberately *not* narrowed to match.**
  A new `ancestor_stages()` helper in `spec.py` (the mirror of the existing
  `downstream_stages()`) computes the transitive closure of a stage's
  ancestors, so the `tasks` generation prompt still embeds both the approved
  requirements and design, in topological order — gate and prompt context are
  now separate concerns fed by different graph walks. The default `lite`
  pipeline's generated prompts stay byte-identical to before, reproven by the
  unchanged C1 zero-behaviour golden fixtures.
- **spec-runner no longer discards foreign frontmatter keys on write.**
  Previously `meta_to_dict` was `asdict(meta)` and `meta_from_dict` silently
  dropped unknown keys, so every `spec approve` / `write_spec` / stale
  cascade erased them — a real data-loss bug for extending layers (steward),
  not a missing convenience. Frontmatter is now losslessly extensible. The
  round-trip guarantee is *semantic*, not textual: keys and YAML values
  survive; comments, quoting style and original key order do not.
- **Canonical frontmatter fields are now validated.** They were previously
  accepted unchecked — dataclasses don't enforce types, so `version:
  "three"` was silently stored as the string `'three'` and a non-string
  YAML key was silently dropped. A violation now raises `SpecMetaError`.
  `version` uses `type(v) is int` so `version: true` cannot pass as `1`;
  `status` is value-checked against `draft`/`approved`/`stale` because it
  drives the state machine; `validation` is type-checked only, since it
  drives no decision. As a documented compatibility exception,
  `generated_at` and `approved_at` also accept YAML's native date scalars
  and normalize them via `.isoformat()`.
- **A recognized-but-malformed spec now fails loud instead of silently
  reading as unmanaged.** Unmanaged/foreign documents still return `None`
  permissively — that matters, since "unmanaged" passes the governance gate
  — but once `spec_stage` is recognized, the document can no longer
  silently degrade. Syntactically invalid frontmatter YAML remains
  unmanaged, since the stage cannot be read at all in that case.
- **The VS Code frontmatter schema is now open**
  (`additionalProperties: true`) and gained `owner_role`. It previously
  declared `additionalProperties: false` as a drift alarm, which
  contradicts a deliberately extensible frontmatter; that protection moved
  to an exact canonical-field test, which is stricter. This is a cross-repo
  contract consumed by `spec-runner-vscode`.

### Fixed

- **`requires-python` corrected to `>=3.11`.** The package declared `>=3.10`, but
  `from datetime import UTC` (3.11+) has been imported by five shipped modules —
  `obs.py`, `audit_log.py`, `cli_plan.py`, `spec_commands.py`, `change_commands.py` —
  for some time, so `import spec_runner` raised `ImportError` on 3.10 while pip
  happily installed it. The supporting config already assumed 3.11: ruff is set to
  `target-version = "py311"` and CI tests 3.11/3.12/3.13 with no 3.10 job. Metadata,
  the `Programming Language :: Python :: 3.10` classifier and the docs now match the
  code. No runtime behaviour changes; 3.10 was never actually functional.

- **Governance gate could be bypassed under a custom stage profile.**
  `spec_run_gate_ok` read `tasks.md` via `read_spec_meta`'s default `lite`
  stage tuple, so a managed spec whose stage is not part of `lite` resolved
  to `None` (= unmanaged) and passed `run --strict` / `watch --strict` even
  while in `draft`. Live in every released version since stage profiles
  shipped in v2.9.0. The run gate now resolves stages from the configured
  profile.
- **`spec approve` / `reject` / `check` were profile-blind in the same way**
  and wrongly reported a managed custom-profile stage as unmanaged (exit 2),
  refusing to act on it. All four `read_spec_meta` calls in
  `spec_commands.py` now resolve stages from the configured profile.
- **`plan --gated` crashed on any non-`lite` profile.** `_MARKER` and
  `_UPSTREAM` in `cli_plan.py` were module-level dicts hardcoded to the three
  `lite` stages; `_generate_stage_draft` indexed them directly and raised
  `KeyError` for any other profile. Both are gone: markers now come from
  `StageDef.marker_prefix` via a new internal `_parse_stage_marker` (the
  exported `parse_spec_marker` is unchanged, since it prepends `SPEC_` to a
  bare name while `marker_prefix` is already the full prefix).
- **`validate` could not resolve a custom stage's file path.** `validate.py`
  had its own hardcoded three-key stage→file map and raised
  `ValueError: unknown stage: <name>` for anything else; it now resolves the
  path via the shared `spec.stage_path` convention.
- **Custom-profile stages were rejected by the VS Code frontmatter schema.**
  `spec_stage` carried an `enum` of the three `lite` stage names, so a spec
  on any other stage profile failed the contract — a latent bug since stage
  profiles shipped in v2.9.0. Stage membership is a runtime check against
  the configured profile and cannot be expressed in JSON Schema without it,
  so the schema now only requires a non-empty string.

## [2.10.0] — 2026-07-14

OpenSpec-inspired spec lifecycle: this release delivers five milestones drawn
from a study of OpenSpec (2026-07-13) — per-stage prompt context/rules, a
structured requirements parser, DAG stage profiles, change-as-folder, and
delta-spec merge on archive. See `docs/plans/2026-07-13-openspec-inspired-roadmap.md`.
The Maestro interop contract (state-db schema, `--json-result`) is unchanged.

### Added

- **Delta specs + archive merge** (M3, the culmination of the OpenSpec-inspired
  roadmap). A change may carry `spec/changes/<id>/specs/requirements.md` — a
  delta with `## ADDED / MODIFIED / REMOVED / RENAMED Requirements` sections of
  id-keyed blocks (see `spec/FORMAT.md`). `change archive` now validates the
  delta, prints the merge plan, merges it into the flat `spec/requirements.md`
  (all-or-nothing: ADDED appends a new id, MODIFIED replaces the whole block,
  REMOVED deletes it — `**Reason**`/`**Migration**` mandatory, RENAMED rewrites
  the heading name only), then moves the folder to the archive. Conflicts
  (unknown id, duplicate ADDED, several ops on one id, FROM-name mismatch)
  abort the archive and name the offending requirement; `--force` never
  overrides merge safety. New `change archive --dry-run` prints the plan
  without changing anything; `validate --change <id>` reports delta conflicts
  early (fail fast). Re-archiving the same delta conflicts instead of
  double-applying. A missing target is bootstrapped by the first archived
  delta's ADDED blocks. New public API: `parse_delta` (`requirements.py`) and
  `plan_merge`/`apply_merge`/`MergeConflictError` (`spec_merge.py`, new).
  Non-contract, additive change.

- **Change-as-folder lifecycle** (M2 of the OpenSpec-inspired roadmap; design:
  `docs/plans/2026-07-13-m2-change-folder-design.md`). A change is a
  self-rooted spec dir at `spec/changes/<id>/` selected with `--change <id>`:
  every spec path (`tasks.md`, gated pipeline files, locks, state-db, logs)
  scopes to the change through the config path seam, so `run`, `plan --gated`,
  governance, `verify` and reports all work inside a change unchanged. The
  per-change state-db yields a per-change executor lock, so parallel
  `run --change A` and `run --change B` don't contend. New `change` command
  family: `change new <id>` (scaffold with a tasks.md stub), `change list`
  (`--json`), `change archive <id>` (moves to
  `spec/changes/archive/YYYY-MM-DD-<id>/`; refuses while a run is live or
  tasks are unfinished — `--force` overrides the task gate only). Archive here
  only moves the folder; delta-spec merge is M3. `--change` and
  `--spec-prefix` are mutually exclusive. **No contract change**: the state-db
  schema and `--json-result` stdout are untouched (per the M2 design decision,
  the db *location* is configuration — same precedent as `--spec-prefix`);
  the flat `spec/` layout is byte-identical when `--change` is not used.

- **DAG stage profiles** (M4 of the OpenSpec-inspired roadmap). Spec-generation
  profiles are now a true dependency graph rather than a flat ordered list.
  `StageDef.requires` (alias of `upstream`, and the new canonical `requires:`
  key in profile YAML) defines edges; `downstream_stages` follows them
  transitively so a *sibling* stage (sharing an upstream) is no longer
  wrongly stale-cascaded when another stage is approved. New
  `stage_readiness()` reports per-stage `ready`/`blocked`/`done`/`draft`/`stale`
  + `missing_deps`, exposing parallelism (several stages `ready` at once).
  `resolve_next_stage` is dependency-gated, `load_profile` rejects unknown
  `requires` refs and cycles, and `stage_path` / `spec status` / the gated
  planner resolve stages from the configured profile (custom stage names work).
  The built-in linear `lite` profile is byte-for-byte unchanged (proven by an
  exhaustive graph-vs-linear equivalence test). No contract surface touched.

- **Structured requirements parser** (M1 of the OpenSpec-inspired roadmap).
  New `requirements.py` parses `requirements.md` into id-keyed `Requirement`
  blocks — a diffable/mergeable unit that lays the groundwork for delta specs.
  Tolerant of heterogeneous bodies (gherkin, `- [ ]` checklists, prose): it
  anchors only on the `#+ (REQ|NFR)-NNN` heading and block boundaries (next
  same-or-higher-level heading), preserving each block's exact text for
  round-trip. Public API: `parse_requirements`, `serialize_requirement`,
  `find_requirement`, `Requirement`. `spec-runner validate` now also warns per
  functional requirement that has no acceptance-criteria section (NFRs exempt).
  `spec/FORMAT.md` documents the grammar. Non-contract, additive change.

- **Per-stage rules & project context injection for spec generation** (M0 of
  the OpenSpec-inspired roadmap, `docs/plans/2026-07-13-openspec-inspired-roadmap.md`).
  Two new opt-in config keys: `spec_context` (project-wide text prepended to
  every generation stage inside a `<context>` block) and `spec_rules` (per-stage
  rules keyed by stage name, injected only for the matching stage inside a
  `<rules>` block). Both flow through `plan --full` and `plan --gated`.
  `validate` flags an oversized `spec_context` (>50KB) as an error and unknown
  `spec_rules` stage keys as a warning. Non-contract change; default (no config)
  produces byte-identical prompts to 2.9.0.

## [2.9.0] — 2026-07-07

### Added

- **Loadable stage profiles for gated spec generation** — the previously
  hardcoded stage chain `requirements → design → tasks` is now data. A
  `StageProfile` (ordered `StageDef`s carrying name, template, marker prefix,
  validator key, and upstream stages) is loaded from a bundled YAML profile;
  the built-in `lite` profile (`src/spec_runner/profiles/lite.yaml`) reproduces
  the old chain 1:1. `spec.py` (stage ordering / next-stage resolution / stale
  cascade / `spec_stage` validation), `prompt.py` (templates + markers), and
  `validate.py` (per-stage validator dispatch) all read from the profile
  instead of scattered module-level maps.
- **Profile selection** — `spec_profile` config key (default `lite`) and a
  `--profile` flag on `plan --gated` and the `spec` command family. An unknown
  profile raises a clear `ConfigError` listing the available profiles instead
  of a traceback.

  This is additive and behaviour-preserving: the default (`lite`) pipeline is
  identical to 2.8.x, the `SPEC_STAGES` export is unchanged, and existing specs
  with `spec_stage` in `{requirements, design, tasks}` need no migration. Full
  suite stays green with no test edits (976 passed). Unblocks richer
  governance-layer profiles downstream.

## [2.8.1] — 2026-07-05

### Fixed

- **`costs --json` on a project without `tasks.md` emits valid empty JSON** —
  previously `parse_tasks()` hard-exited when `tasks.md` was missing, and with
  zero tasks the command printed the prose fallback `No tasks found`, breaking
  machine consumers (the `spec-runner-vscode` extension polls `costs --json`
  on fresh gated specs that have no tasks stage yet). Now emits a
  schema-conformant `{"tasks": [], "summary": {…}}` payload.
- **Pre-init log lines no longer leak to stdout** — structlog's built-in
  default prints to stdout until `init_logging()` runs, so logs emitted during
  `build_config()` (e.g. the subdir-project warning) corrupted machine output
  (`status --json` failed `JSON.parse` in the VSCode extension). `obs.py` now
  installs a pre-init default that routes logging to the *current*
  `sys.stderr`, keeping stdout reserved for `--json` / `--json-result`.

## [2.8.0] — 2026-07-02

### Added

- **VSCode extension read-surface contracts** — pinned schemas for the surfaces
  the `spec-runner-vscode` extension reads: `schemas/status.schema.json`
  (`status --json` flat aggregate), `schemas/costs.schema.json` (`costs --json`
  per-task list; pins the *mixed* status enum — DB
  `pending/running/success/failed/skipped` + tasks.md
  `todo/in_progress/done/blocked`), and `schemas/spec-frontmatter.schema.json`
  (`SpecMeta` governance frontmatter). Golden fixtures + contract tests
  (`tests/test_vscode_contract.py`) validate both sample fixtures and *live*
  command output, with a drift guard on the union status enum. Adds
  `spec-runner --version` for the extension's activation compatibility check.
  Additive only — no change to existing output shapes.
- **Gated spec generation** (`plan --gated`, `spec status/approve/reject/adopt/check`):
  an opt-in workflow that stamps `requirements.md`/`design.md`/`tasks.md` with
  frontmatter (`spec_stage`, `status: draft|approved|stale`, `version`,
  `generated_by`, `validation`, `approved_by`/`approved_at`) tracked through
  atomic, file-locked writes. `plan --gated [--stage S]` generates one stage at
  a time from rich single-source templates (content-hashed as
  `source_prompt_version`), enforces that upstream stages are already
  `approved`, writes the result as `draft`, validates it, and stops.
  `spec approve <stage>` always re-validates the body before approving (never
  trusts the cached `validation` field) and cascades `stale` to downstream
  stages on any version bump; `spec adopt` validates first and refuses to
  silently stamp an invalid file as `approved` (unless `--force`); `spec
  reject` reopens a stage as `draft`. `run`/`watch` gain a hard gate: with
  `spec_governance: strict` (default `off`) in config, or `--strict`/
  `--no-strict` on the CLI, an unapproved *managed* `tasks.md` blocks
  execution; unmanaged (frontmatter-less) and Maestro-produced specs run
  unchanged for backward compatibility. `task.py` parsing strips leading
  frontmatter transparently and write-back preserves it.

## [2.7.0] — 2026-06-14

### Added

- **`--model` now applies to the `qwen` and `copilot` presets.** Previously these
  template-driven presets ignored `--model` (model was set in the CLI's own
  settings/env). Now `spec-runner config --preset qwen --model qwen-coder-plus`
  (or `copilot --model claude-haiku-4.5`) appends `--model <id>` to the generated
  `command_template`. A blank model still omits the flag (no dangling `--model`).

## [2.6.0] — 2026-06-13

### Added

- **`config` presets for `qwen` and `copilot`.** `spec-runner config --preset
  qwen` (Qwen Code CLI) and `--preset copilot` (GitHub Copilot CLI) now write the
  correct headless `command_template` / `review_command_template` (these CLIs are
  not auto-detected). qwen uses `--approval-mode yolo` for exec and `plan` for the
  read-only review; copilot uses `-s --no-ask-user --allow-all-tools` for exec and
  `--allow-tool='shell'` for review. The model is configured in each CLI's own
  settings/env (see the printed note); `--model` does not apply to these two.
  Preset list is now: claude, codex, opencode, pi, ollama, llama-cli, qwen, copilot.

## [2.5.0] — 2026-06-13

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

[Unreleased]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.21.1...HEAD
[2.21.1]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.21.0...v2.21.1
[2.21.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.20.0...v2.21.0
[2.20.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.19.0...v2.20.0
[2.19.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.18.0...v2.19.0
[2.18.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.17.0...v2.18.0
[2.17.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.16.0...v2.17.0
[2.16.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.15.0...v2.16.0
[2.15.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.14.0...v2.15.0
[2.14.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.13.0...v2.14.0
[2.13.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.12.0...v2.13.0
[2.12.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.11.1...v2.12.0
[2.11.1]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.11.0...v2.11.1
[2.11.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.10.0...v2.11.0
[2.10.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.9.0...v2.10.0
[2.9.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.8.1...v2.9.0
[2.8.1]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.8.0...v2.8.1
[2.8.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.7.0...v2.8.0
[2.7.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.6.0...v2.7.0
[2.6.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.5.0...v2.6.0
[2.5.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.4.1...v2.5.0
[2.4.1]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.4.0...v2.4.1
[2.4.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.3.1...v2.4.0
[2.3.1]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.3.0...v2.3.1
[2.3.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.2.2...v2.3.0
[2.2.2]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.2.1...v2.2.2
[2.2.1]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.2.0...v2.2.1
[2.2.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.0.0...v2.1.0
