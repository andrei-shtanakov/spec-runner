# Roadmap: OpenSpec-Inspired Spec Evolution (M0–M5)

**Date:** 2026-07-13
**Status:** Draft — awaiting owner approval
**Source:** Study of the OpenSpec repo (Fission-AI, MIT, v1.6.0) conducted
2026-07-13. OpenSpec is a planning-only spec framework; spec-runner is an
execution engine with a weak spec-evolution story. This roadmap ports the
four patterns worth borrowing, in dependency order.

**Non-goals:**
- Adopting OpenSpec as a dependency (TS/Node, no execution engine).
- Abandoning gated spec governance (draft/approved/stale). OpenSpec's
  "fluid, no gates" philosophy fits interactive use; our autonomous
  execution needs gates. New features must compose with the governance
  gate, not replace it.
- Breaking the Maestro interop contract (`.executor-state.db` schema,
  `--json-result` stdout). Any milestone that touches it requires a major
  version bump and golden-fixture updates.

**Compatibility principle (applies to every milestone):** the current flat
`spec/` layout (requirements.md / design.md / tasks.md, `--spec-prefix`
namespacing) keeps working unchanged. All new mechanisms are opt-in.

---

## Milestone overview

| # | Title | Depends on | Size | Target version |
|---|-------|-----------|------|----------------|
| M0 | Per-stage rules & context injection | — | S | v2.10 |
| M1 | Structured requirements format + parser | — | M | v2.11 |
| M2 | Change-as-folder lifecycle | M1 | L | v3.0 |
| M3 | Delta specs + archive merge | M1, M2 | L | v3.1 |
| M4 | Stage profiles → artifact DAG | — (parallel track) | M | v2.x or v3.x |
| M5 | OpenSpec bridge (experimental) | M1 | S | optional |

```
M0 ──────────────────────────────► (quick win, independent)
M1 ──► M2 ──► M3                   (spec-evolution track)
  └──► M5                          (experimental bridge)
M4 ──────────────────────────────► (profile track, independent)
```

---

## M0: Per-stage rules & context injection

**Pattern borrowed:** OpenSpec `config.yaml` — `context:` (project-wide) and
`rules:` (keyed by artifact id) injected into generation instructions inside
`<context>`/`<rules>` tags.

**Problem:** personas and constitution guardrails are global; there is no way
to say "proposals must include a rollback plan" or "specs use Given/When/Then"
for one generation stage only.

**Scope:**
- New config keys under the spec family:
  `spec_context: str` and `spec_rules: dict[stage_name, list[str]]`.
- `prompt.py:build_generation_prompt()` prepends `<context>` (if set) and
  injects `<rules>` for the matching stage of the active `StageProfile`.
- Validation: unknown stage names in `spec_rules` → warning (mirror
  OpenSpec's behavior); context size cap (50 KB) → error.

**Touches:** `config.py`, `prompt.py`, `validate.py`.
**Acceptance:** rules appear only in the matching stage's prompt; golden
prompt tests; unknown-stage warning covered; default (no config) produces
byte-identical prompts to v2.9.

---

## M1: Structured requirements parser — SHIPPED (PR #43)

**Pattern borrowed:** requirement-as-mergeable-unit. OpenSpec specs are
`### Requirement:` blocks with RFC-2119 keywords, rigid enough that later
delta merges (M3) are mechanical.

**Problem:** `requirements.md` has `REQ-XXX` anchors used only for
traceability. There was no parseable *unit* of requirement, so no diffing,
merging, or per-requirement validation was possible.

**Reality-based design decision (settled during implementation):** the strict
OpenSpec grammar (`### Requirement:` + `#### Scenario:` gherkin) was
**rejected** — the repo's own `spec/requirements.md` and the bundled `lite`
template already use `#### REQ-NNN:` headings with *heterogeneous* bodies
(gherkin, `- [ ]` checklists, or prose). Forcing a rigid grammar would break
brownfield compatibility and the byte-identical guarantee. Instead M1 ships a
**tolerant, id-keyed block parser** that anchors only on the `#+ (REQ|NFR)-NNN`
heading and block boundaries (next same-or-higher-level heading), preserving
each block's exact `raw` (the merge/round-trip unit) and extracting optional
fields best-effort. The rigid `Scenario` dataclass was descoped as fiction
against real data.

**Delivered:**
- `requirements.py` (new): frozen `Requirement` dataclass (`id`, `name`,
  `level`, `raw`, `acceptance_criteria`, `priority`, `traces_to`, `kind`,
  `number`); `parse_requirements()`, `serialize_requirement()` (= `raw`),
  `find_requirement()`. Handles REQ + NFR, strips frontmatter.
- `validate.py`: `validate_requirements` enriched additively — per functional
  requirement lacking an acceptance-criteria section → warning (NFRs exempt to
  avoid noise). Existing checks (dup ids, Out of Scope, global AC) untouched.
- `spec/FORMAT.md`: documents the tolerant requirements grammar.
- `__init__.py`: exports the new public API.
- `report.py` / `audit.py`: unchanged (they keep their own REQ regex; M1 is
  purely additive).

**Result:** parser round-trips the repo's own 28KB `spec/requirements.md`
(idempotent per-block reparse); free-form/no-requirement files parse to `[]`
without erroring; 1040 tests pass, lint + mypy clean. No contract surface
touched.

---

## M2: Change-as-folder lifecycle

**Pattern borrowed:** OpenSpec `changes/<name>/` — each change is a
self-contained folder (proposal + design + tasks + delta specs), parallel
changes coexist, completed changes archive to `changes/archive/<date>-<name>/`
with full context preserved.

**Problem:** one flat `spec/` per project; `--spec-prefix` gives flat
namespacing with no lifecycle (no parallel in-flight changes, no archive, no
"why" preserved after completion).

**Scope:**
- New layout (opt-in): `spec/changes/<change-id>/` containing `proposal.md`,
  `design.md`, `tasks.md`, `specs/` (delta specs, consumed in M3), and
  per-change state db (`.executor-state.db` inside the change folder —
  schema unchanged, location parameterized).
- CLI: `spec-runner change new <id>`, `change list`, `change archive <id>`
  (M2 archives without merging; merge lands in M3), `run --change <id>`.
- `--spec-prefix` remains supported; a change folder is effectively a
  self-rooted spec dir, so most path logic reuses the existing
  `spec_prefix`/root resolution seam in `config.py`.
- Gated governance and stage profiles operate per-change unchanged.
- Archive: move to `spec/changes/archive/YYYY-MM-DD-<id>/`, refuse if tasks
  are not all done unless `--force`.

**Version note:** v3.0 — layout addition is backward compatible, but state-db
location and `--json-result` gain a `change_id` field → contract-affecting;
requires schema version bump + golden fixtures + `docs/state-schema.md`
update, per the Maestro interop rule.

**Touches:** `config.py`, `cli.py`, `state.py` (path only), new
`change_commands.py`, `docs/state-schema.md`, `schemas/*.json`.
**Acceptance:** two changes run in parallel without lock/db contention; e2e
test: new → plan → run → archive; legacy flat layout untouched by default;
Maestro contract tests updated and green.

---

## M3: Delta specs + archive merge

**Pattern borrowed:** OpenSpec delta specs — a change carries only what
changes: `## ADDED Requirements`, `## MODIFIED Requirements` (full updated
block), `## REMOVED Requirements` (with Reason/Migration), `## RENAMED
Requirements` (FROM:/TO:). On archive, deltas merge deterministically into
the source-of-truth spec.

**Problem:** requirements never evolve; there is no mechanism connecting a
completed change back into the project's requirements. This is the single
biggest gap OpenSpec exposes in spec-runner.

**Scope:**
- Source of truth moves to `spec/specs/<capability>/spec.md` (created lazily;
  a project's first archived delta bootstraps it). Flat `requirements.md`
  remains valid for projects that never opt in.
- `requirements.py` grows delta parsing: section headers → operations
  (`Added/Modified/Removed/Renamed` dataclasses).
- Merge engine (`spec_merge.py`): apply operations to a parsed spec —
  ADDED appends, MODIFIED replaces by header match (whitespace-insensitive),
  REMOVED deletes (requires Reason + Migration), RENAMED rewrites header.
  Conflicts (target not found, duplicate add) → hard errors listing the
  requirement header.
- `spec-runner change archive` (from M2) now: validate deltas → merge →
  write updated specs → move folder to archive. `--dry-run` prints the merge
  plan.
- Validation: delta files validated at plan time, not only at archive time
  (fail fast, mirrors OpenSpec's "4-hashtag scenarios fail silently" pitfall
  warning — we make it a hard error).

**Touches:** `requirements.py`, `spec_merge.py` (new), `change_commands.py`,
`validate.py`, generation templates for the specs stage.
**Acceptance:** golden merge fixtures (each operation + conflict cases);
property: archive(parse(spec) + delta) == expected spec, byte-stable;
round-trip idempotence (re-archiving same delta → conflict error, not dup).

---

## M4: Stage profiles → artifact DAG

**Pattern borrowed:** OpenSpec `schema.yaml` — artifacts declare
`generates:` (file or glob) and `requires:` (list ⇒ DAG); artifact state is
derived from file existence (`blocked/ready/done` via topological sort);
`status --json` / `instructions --json` feed agents structured next-step data.

**Problem:** `StageProfile` (v2.9) is a linear chain — `upstream` is a single
stage, `generates` is a single file, parallel branches (specs ∥ design after
proposal) are inexpressible.

**Scope:**
- `spec.py`: `StageDef.upstream: str | None` → `requires: tuple[str, ...]`
  (loader accepts both spellings; `upstream: x` ≡ `requires: [x]`).
- `generates:` supports glob patterns; stage status = all deps done ∧ any
  generated file exists (existence-based, no new state storage).
- Status derivation: topological sort + `blocked/ready/done`; cycle
  detection at profile load.
- `spec-runner spec status --json` gains `ready`/`blocked`/`missing_deps`
  per stage (additive fields only).
- `resolve_next_stage` returns the set of ready stages; `plan --gated`
  prompts when >1 is ready.
- Ship one bundled non-linear profile (e.g. `spec-driven`: proposal →
  {specs, design} → tasks) as the reference; `lite` stays default and
  byte-identical in behavior.

**Touches:** `spec.py`, `spec_commands.py`, `cli_plan.py`, `validate.py`,
`profiles/*.yaml`.
**Acceptance:** `lite` profile zero-behaviour-change proof (extend
`test_c1_zero_behaviour.py`); DAG profile e2e: parallel stages both ready
after root approved; cycle → `ConfigError` naming the cycle.

---

## M5 (optional, experimental): OpenSpec bridge

**Idea:** OpenSpec's `tasks.md` (checkbox checklist, hierarchical numbering)
is structurally close to our `TASK-NNN` format. A one-way converter would let
teams plan with `/opsx:*` and execute with spec-runner.

**Scope:** `spec-runner import openspec <path-to-change-dir>` → generates
`tasks.md` in our format (checklist groups → tasks, numbering → ids,
proposal/design copied alongside), plus REQ extraction from their spec files
once M1's parser exists.
**Gate:** build only if a real use case shows up; keep out of core until
then (candidate for a plugin under `spec/plugins/`).

---

## Sequencing & workflow

1. Every milestone = its own branch + PR per repo git-workflow rules
   (Copilot review, human merge). M2 and M3 each get a dated design doc in
   `docs/plans/` before implementation (they carry real design decisions).
2. Recommended order: **M0 → M1 → M4 → M2 → M3** (M4 pulled earlier since it
   is independent and de-risks profile machinery before the v3.0 layout
   work), M5 opportunistic.
3. Each milestone ends with: full test suite, `ruff` + `mypy`/`pyrefly`,
   regression tests for touched behavior, CHANGELOG entry.

## Risks

- **Scope creep into an OpenSpec clone.** We port mechanisms, not the
  workflow philosophy; every feature must serve autonomous execution.
- **Contract drift (Maestro).** M2 is the only contract-touching milestone;
  isolate contract changes there, nowhere else.
- **Format migration fatigue.** M1's structured grammar is opt-in; provide
  `spec-runner spec adopt`-style migration hints rather than forcing
  rewrites.
