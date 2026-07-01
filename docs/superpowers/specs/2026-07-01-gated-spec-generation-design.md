# Gated Spec Generation — Design

**Date:** 2026-07-01
**Status:** Design (approved for planning)
**Component:** spec-runner — `plan` / spec lifecycle

## Problem

Spec creation in spec-runner has almost no discipline. `spec-runner plan --full`
is a pipeline of three automatic LLM calls (`requirements → design → tasks`,
`cli_plan.py:62-124`) driven by three-line instructions (`prompt.py:17-48`,
`SPEC_STAGES`). There are:

- **no human checkpoints** between stages — a bad `requirements` silently
  propagates into `design` and `tasks`;
- **no validation** of generated specs — `validate`/`audit` exist but are never
  wired into generation;
- **no approval status or versioning** — nothing records whether a spec is a
  draft or has been reviewed, and nothing blocks `run` from executing an
  unreviewed `tasks.md`;
- **thin instructions** — the rich templates (Out of Scope, GIVEN-WHEN-THEN,
  ADR) in `spec-generator-skill/templates/*.template.md` are only reachable by
  the IDE agent, so the CLI path and the skill path drift.

We want to bring spec creation closer to the gated discipline of `sdd-framework`
**without losing spec-runner's lightness**.

## Goals

- Introduce a **gated mode** for spec generation with human-in-the-loop
  checkpoints between stages.
- Use **rich generation templates** (single source of truth shared with the
  skill).
- Track **approval status + versions** on each spec document.
- Enforce a **hard, blocking validation gate** after each stage.
- Keep the default experience unchanged — governance is **opt-in**, existing
  projects keep working.

## Non-goals (first iteration)

- Language Profiles (Python/Rust) in the gated CLI — keep extensible, don't ship.
- A full sdd-style gate state-machine in SQLite (rejected approach B).
- Multi-phase gated flow (`phase2-*`) — compatible via existing `--spec-prefix`,
  but not separately tested here.
- Syncing spec status into GitHub Issues.
- **Cross-phase gating** (`--spec-prefix`): the gate already reads prefix-aware
  `tasks_file`/`requirements_file` (`config.py:242,245`), so per-phase gating works
  for free — but **in v1 phases are independent**: phase N does not require phase
  N-1 to be approved.

## Future hook (v1.1) — spec lifecycle observability

spec-runner is the reference implementation of the observability contract
(`obs.py:1-6`, OTel JSONL, vendored into Maestro/arbiter/ATP), yet spec authoring
is currently invisible to the trace world — only `approved_by/at` in frontmatter
records it. Emitting lifecycle events (`spec.stage_generated`,
`spec.validation_verdict`, `spec.approved`) through the existing `obs.py` trace
context would let one correlate **spec version → run → outcome** on the same axis
Maestro/arbiter are already instrumented on. The frontmatter fields
(`generated_by/at`, `version`) are ready-made event attributes. Named here as the
natural v1.1 hook; not built in v1.

## Chosen Approach

**Frontmatter + lightweight `spec` subcommands** (approach A). State lives in the
spec files themselves (YAML frontmatter), commands only read/write it. This is
git-diffable, PR-reviewable, works in CI without a TTY, and adds minimal code.

Rejected:
- **B (SQLite state-machine):** heavy, duplicates execution-state, detaches state
  from files (worse for git/PR).
- **C (thin: checkpoints + rich prompts only):** no resumable file-based state,
  no real `run` gate — doesn't meet the requirements.

## Flow

```
plan --gated "description"
  └─ stage requirements → writes requirements.md DRAFT
       → auto-validation ──✗ errors → show, STOP (approve forbidden)
                          └─✓ ok → STOP (approvable)
  └─ spec approve requirements     [gate: requirements validation passed]
  └─ plan --gated --stage design   [gate: requirements == APPROVED]
       → design.md DRAFT → auto-validation → …
  └─ spec approve design
  └─ plan --gated --stage tasks    [gate: design == APPROVED]
       → tasks.md DRAFT → auto-validation → …
  └─ spec approve tasks
  └─ spec-runner run               [gate: tasks == APPROVED, strict mode]
```

### Two gate levels

1. **Validation gate (hard, automatic).** After a DRAFT stage is generated,
   stage-appropriate validation runs immediately. `errors` → `approve` is
   physically forbidden; the stage stays DRAFT until fixed (edit or regenerate).
   `warnings` do not block.
2. **Approval gate (human).** Even with green validation, advancing to the next
   stage requires an explicit `spec approve`.

### Per-stage validation (extends `validate.py` / `audit.py`)

- **requirements:** unique `[REQ-XXX]`; every REQ has acceptance criteria; an
  Out of Scope section is present.
- **design:** unique `[DESIGN-XXX]`; every `traces to [REQ-XXX]` points at an
  existing REQ (no dangling references).
- **tasks:** existing `validate` (duplicate IDs, depends/blocks symmetry, cycles)
  + `audit` (orphan tasks, uncovered REQ, dead designs).

## Command Surface

| Command | Behavior |
|---|---|
| `plan --gated [--stage S]` | Generate one stage (default: next unresolved), write DRAFT, validate, stop. Resolves "next" from prior files' statuses. |
| `spec status` | Show the three documents with status/version and the next action. |
| `spec approve <stage>` | DRAFT/STALE → APPROVED. Re-validates the current body (never trusts the cached field); proceeds only if fresh result != fail; bumps `version`; records `approved_by/at`. |
| `spec reject <stage>` | Re-open a stage → sets `status` back to `draft`. Deliberately **no new `rejected` status**: `rejected` is already taken by `ReviewVerdict.REJECTED` (`state.py:41`, an execution-review outcome); reusing the word would conflate two domains. `reject` just returns the doc to `draft`. |
| `spec adopt` | Add frontmatter to an existing (unmanaged) file. **Runs validation first**: pass → `status: approved`; fail → `status: draft` (adopt-as-draft), unless `--force` adopts as approved with a loud warning. Never stamps APPROVED over an invalid spec (see Adopt gate below). |
| `spec check [stage]` | Re-run validation and refresh the cached `validation` field. |

### TTY layer (overlay, not a mode)

When stdout is a terminal and `--no-interactive` is not set, after
generation+validation of a DRAFT the verdict and a menu are shown:

```
requirements.md — DRAFT, validation: PASS (2 warnings)
[a] approve   [e] edit ($EDITOR)   [r] regenerate   [s] stop   [q] abort
```

- `a` → `spec approve` (greyed out / disabled while `validation == fail`), then
  continues into the next stage in the same process.
- `e` → open `$EDITOR`; on exit re-read the file, **re-run validation**, redisplay.
- `r` → regenerate (overwrite DRAFT; `version` untouched).
- `s` → exit, state saved (resume later via the file path).
- `q` → abort.

The menu holds no state of its own — each item is the same `spec approve` /
`$EDITOR` / regenerate call as the CI path. Closing the terminal at any step is
safe; resume via files.

## Frontmatter Schema

Each document carries YAML frontmatter at the very top — the single source of
truth for spec state.

```yaml
---
spec_stage: requirements        # requirements | design | tasks
status: draft                   # draft | approved | stale
version: 1                      # integer, bumped on approve
generated_by: "claude@claude-opus-4-8"   # <harness>@<model>, ecosystem agent-id convention
generated_at: "2026-07-01T14:32:00Z"
source_prompt_version: "sha256:1a2b3c…"  # content hash of the generation template
validation: pass                # pass | fail | warn — cached, ADVISORY (display only)
approved_by: null               # git user.name on approve; null while DRAFT
approved_at: null
---
# Requirements: <project>
...document body...
```

**Field semantics:**
- `status` — `draft` right after generation; `approved` only via `spec approve`
  (and only if `validation != fail`); `stale` when an upstream stage changed
  (see Downstream Invalidation).
- `version` — integer starting at 1, `+1` on each approve. Regenerating a DRAFT
  does not touch it.
- `validation` — **cached, advisory verdict** of the last auto-validation, for
  `spec status` display only. Gate decisions never trust it (see TOCTOU below);
  refreshed on generation / `spec check` / `spec approve`.
- `source_prompt_version` — **content hash** (e.g. `sha256:…`) of the generation
  template that produced the doc — not a hand-maintained integer, which would
  drift the moment someone edits `*.template.md` without bumping the constant.
  `spec status` compares it to the current template hash to flag "generated by an
  outdated template" (drift signal, non-blocking).
- `generated_by` — `<harness>@<model>` per the ecosystem agent-id convention
  (ADR-ECO-003), so a single cross-tool grep matches. `approved_by` follows the
  same shape (`<user>` for humans; reserved for `agent/policy` if an autonomous
  approver is ever added — see Cross-project Boundary).
- `approved_by`/`approved_at` — audit trail from `git config user.name` (no
  external dependency).

**TOCTOU — why gates re-validate.** The cached `validation` field can lie: if a
human edits `requirements.md` directly (not via the TTY `edit` action) and then
runs `spec approve`, the cache is out of sync with the body — it could block a
now-valid file, or worse, approve a now-invalid one (`pass` written before a
breaking edit). Therefore `spec approve` **re-runs validation on the current
file body** and never trusts the cached field for the gate decision. The cache
is only a display convenience.

**How gates read it:**
- `plan --gated --stage design` → requires `requirements.md: status == approved`.
- `spec approve <stage>` → **re-runs validation on the current body** (does not
  trust the cached field); proceeds only if the fresh result != fail; then sets
  `approved`, bumps `version`, refreshes cached `validation`, sets `approved_by/at`.
- `spec-runner run` (strict) → requires `tasks.md: status == approved`.

## Rich Generation Templates (single source of truth)

Gated generation pulls the same bundled templates the skill uses:

```
src/spec_runner/skills/spec-generator-skill/templates/
  requirements.template.md
  design.template.md
  tasks.template.md
```

A new `build_gated_generation_prompt(stage, description, context, config)`
assembles the prompt from three parts:

1. **Role + hard instructions** — "fill this template from the description; do
   not invent sections; Out of Scope is mandatory; acceptance criteria in
   GIVEN-WHEN-THEN; add `[REQ-XXX]` traceability".
2. **The template body** — the `<stage>.template.md` content, inserted as "the
   structure the output must follow".
3. **Context** — description + approved prior stages (requirements for design;
   requirements+design for tasks) + `SPEC_<STAGE>_READY/END` markers for parsing.

**Why:**
- One source of truth — editing a template changes both the skill path and the
  CLI path; the current drift is removed by design.
- Validation becomes meaningful — the rich template guarantees the sections
  (Out of Scope, acceptance criteria) that the validation gate checks.
- `source_prompt_version` = template version.

Templates are already packaged with the distribution (`init_cmd.py`). Add
`load_bundled_template(stage) -> str` reading via `importlib.resources` (robust
to wheel installs), not a relative path.

## Next-Stage Resolution

`plan --gated` without an explicit `--stage` computes the next action
deterministically from frontmatter:

| File state | Next action |
|---|---|
| requirements missing/unmanaged | generate requirements |
| requirements = DRAFT | stop; prompt "approve or edit requirements" |
| requirements = APPROVED, design missing | generate design |
| design = DRAFT | prompt about design |
| design = APPROVED, tasks missing | generate tasks |
| tasks = DRAFT | prompt about tasks |
| all APPROVED | "spec ready → spec-runner run" |
| any = STALE | prompt to regenerate/re-approve the stale stage |

One call = one stage. Never skips an unresolved DRAFT/STALE or an unapproved
stage.

## Run Gate & Downstream Invalidation

### Run gate

`spec-runner run` (and `run --all`, `watch`) in strict mode reads `tasks.md`
frontmatter before starting:
- `status == approved` → executes as usual.
- `status == draft | stale` → refuses: "tasks.md is DRAFT; approve via
  `spec approve tasks` or run without strict mode".
- unmanaged (no frontmatter) → **does not block** (backward compatibility).

### Enabling strict mode (default unchanged)

1. `spec-runner.config.yaml`: `spec_governance: strict` (default `off`) — the
   primary, team-level switch.
2. CLI override: `run --strict` / `run --no-strict` for one-offs.

In `off` mode the run gate never fires; `plan --gated` remains available as an
explicit opt-in.

**Strict is a guardrail, not an enforcement boundary.** It is trivially bypassed
— delete the frontmatter and the file becomes "unmanaged", which the gate lets
through. That is acceptable for an opt-in v1 whose goal is discipline for
cooperating humans, not adversarial enforcement. We state it explicitly so no
one mistakes the gate for a security control.

### Downstream invalidation

Stages form a chain `requirements → design → tasks`. When an approved upper stage
changes, lower stages become stale.

**Trigger is a version bump of an approved stage — not the `--force` flag.** This
is the key correctness point. A version bump happens on *every* re-approval,
however the change arrived:
- `plan --gated --stage requirements --force` → requirements back to DRAFT, then
  re-approved later (version bump); **or**
- TTY `edit` (or a direct editor edit) of `requirements.md` followed by
  `spec approve requirements` (version 1→2, no `--force` involved).

Both paths bump the version, and **both must cascade `stale` downstream.** Tying
the cascade to `--force` alone would miss the edit-then-approve path — exactly the
silent upstream/downstream drift `stale` exists to catch. So the rule is: *on any
version increment of an approved stage, mark every downstream stage `stale`.*

- `stale` ≠ `draft`: the file is untouched, but `spec status` warns "design is
  based on requirements v1, now v2 — regenerate or re-approve".
- The run gate treats `stale` as unresolved (blocks in strict mode), but
  `spec approve <stage>` can clear `stale` manually (human decided the upstream
  change was immaterial) — re-validating, bumping version, recording `approved_by/at`.

We do not auto-regenerate the cascade: that would silently overwrite manual edits
in design/tasks. `stale` + an explicit human decision is safer and cheaper.

### Adopt gate

`spec adopt` must not become a hole through the validation gate. Legacy files
predate the rich templates, so they likely lack Out of Scope / acceptance
criteria and would fail the new validators. If adopt blindly stamped
`status: approved`, `run` (which only checks `status == approved`) would execute
an approved-but-invalid spec.

Therefore adopt **runs the stage validator first**:
- validation passes → `status: approved`;
- validation fails → `status: draft` (adopt-as-draft) by default, so the human
  must fix + `spec approve`;
- `--force` → `status: approved` with a loud warning, for the escape hatch where
  a maintainer knowingly adopts a legacy spec that won't pass the new rules.

### Document status machine

```
(no file) --generate--> DRAFT --approve[re-validate!=fail]--> APPROVED
   DRAFT --regenerate--> DRAFT
   APPROVED --force-regen--> DRAFT
   APPROVED --version bump (any path)--> downstream stages → STALE
   STALE --approve[re-validate]--> APPROVED
   STALE --regenerate--> DRAFT
```

## Backward Compatibility

- `task.py:parse_tasks()` must **strip a leading `---…---` frontmatter block**
  before regex parsing (small change, separately tested).
- **Legacy files without frontmatter** are treated as "unmanaged": in the default
  (`off`) mode they work exactly as before. The run gate fires only under
  `--strict`/`spec_governance: strict` **and** when frontmatter is present; or
  after an explicit `spec adopt`.
- All existing projects (spec-runner itself, atp-platform, etc.) must keep working
  under the default `spec_governance: off`. This is an invariant verified by e2e.

## Concurrency & Durability

Frontmatter is now state, so writing it must be crash- and race-safe — today all
spec writes are plain `write_text` (`cli_plan.py:114`, `task.py:239,269,306`),
which is neither. The new `spec approve`/`reject`/`adopt` do read-modify-write on
the frontmatter block, and a crash mid-write or a race with `run`/`watch` reading
the same file would leave half-written YAML → `read_spec_meta` throws → the stage
is wedged.

Requirements for all frontmatter mutations (centralized in `spec.py`):
- **Atomic write:** write to a temp file in the same directory, then
  `os.replace()` onto the target (atomic on POSIX). Never partial-write the spec
  file in place.
- **Locking:** reuse the existing `ExecutorLock`/FileLock (`config.py:42,62`) —
  currently not applied to spec files — around the read-modify-write so concurrent
  mutators serialize.
- **approve-vs-run race:** define the ordering explicitly. `run` reads
  `tasks.md` status under the same lock; an `approve` that lands mid-`run` does
  not affect the already-started run (the run captured status at start). Document
  that a run reflects the status as of its start, not a later approve.

This is the one robustness gap that would otherwise remain open.

## Cross-project Boundary — Maestro (must be named)

`tasks.md` is a **shared surface**: Maestro *produces* it and spec-runner
*consumes* it via `parse_tasks`. Maestro does this autonomously — `decomposer.py`
writes `spec/requirements.md`, `design.md`, `tasks.md` directly, and the
orchestrator regenerates them ("always regenerate"). That is **philosophically
opposite** to gated SDD, where a human approves each stage.

The two coexist today only because of two accidents of default:
1. `spec_governance` defaults to `off`, and
2. Maestro writes files **without frontmatter** → they are "unmanaged" → the run
   gate does not fire.

**Explicit consequence:** a project cannot simultaneously have Maestro
orchestration *and* strict governance on the same specs — under Maestro, `strict`
silently becomes a no-op (Maestro's frontmatter-less files are always unmanaged).
The design acknowledges this rather than hiding it.

**Decision for v1:** gated SDD is scoped to **human-driven spec-runner only**.
Maestro-driven specs stay unmanaged and ungated. Whether Maestro should later
emit frontmatter with an autonomous approval path (`approved_by: agent/policy`,
which is why `generated_by`/`approved_by` already use the `<harness>@<model>`
convention) is **out of scope here** but named as the open question.

**Compatibility note:** the `parse_tasks` frontmatter strip (see Backward
Compatibility) is safe for Maestro files — it strips a leading `---…---` block if
present and is a no-op if absent, so it never breaks Maestro-produced `tasks.md`.

## Implementation Footprint

| File | Change |
|---|---|
| `spec.py` *(new)* | `SpecMeta` dataclass, `read_spec_meta`/`write_spec_meta` (**atomic: temp + `os.replace`**, under FileLock), `bump_version` (**cascades `stale` downstream on any approved-stage bump**), `resolve_next_stage`, status machine (draft/approved/stale), `load_bundled_template` (content hash) |
| `spec_commands.py` *(new)* | `spec status/approve/reject/adopt/check` + TTY menu `[a/e/r/s/q]`. `approve` re-validates; `adopt` validates before stamping; `reject` → draft |
| `prompt.py` | `build_gated_generation_prompt` (pulls bundled template), `source_prompt_version`; keep old `build_generation_prompt` for legacy `--full` |
| `cli_plan.py` | `--gated`/`--stage` branch: generate one stage → validate → write DRAFT → stop; TTY overlay |
| `validate.py` | Per-stage validators: `validate_requirements`, `validate_design` (dangling `[REQ]` refs), `validate_tasks` (existing) |
| `task.py` | `parse_tasks` strips leading frontmatter block |
| `cli.py` / `cli_info.py` | Run gate in `_run_tasks` (check `tasks.md` approved when `spec_governance: strict`); `--strict/--no-strict` flags |
| `config.py` | `spec_governance` field (`off`/`strict`), default `off` |
| `cli_info.py` | `cmd_status` surfaces spec governance state (or dedicated `spec status`) |

## Testing

In the repo style — mock CLI, `@pytest.mark.slow` for e2e:

- `test_spec_meta.py` — frontmatter round-trip, `bump_version`, `resolve_next_stage`
  (the next-stage table), status machine incl. stale transitions; **atomic write
  (temp + `os.replace`) leaves no partial file on simulated interruption**;
  **any approved-stage version bump cascades `stale` downstream — including the
  edit-then-`approve` path with no `--force`**.
- `test_spec_lock.py` *(new)* — concurrent `approve` vs `run` serialize under
  FileLock; a run reflects status as of its start.
- `test_adopt_gate.py` *(new)* — `adopt` on an invalid legacy file → `draft` (not
  approved); `--force` → approved with warning; valid file → approved.
- `test_spec_commands.py` — approve blocked when validation fails; **TOCTOU:
  editing a file to break it after a cached `validation: pass`, then `spec approve`,
  must still block (approve re-validates, never trusts the cache)**; adopt on a
  legacy file; reject/force invalidation → downstream stale.
- `test_source_prompt_version.py` — `source_prompt_version` is the template
  content hash; editing `*.template.md` changes it; `spec status` flags drift.
- `test_gated_plan.py` — gate "design requires requirements APPROVED"; generation
  writes DRAFT+validates; regenerate DRAFT vs `--force` on approved.
- `test_validate.py` — extend: per-stage validators (dangling REQ refs, missing
  Out of Scope).
- `test_task_diff.py`/`test_task.py` — `parse_tasks` ignores frontmatter (regression).
- `test_run_gate.py` *(new)* — `run` blocked on DRAFT/stale in strict, passes
  unmanaged and in `off`.
- Update `test_cli_run_reset.py`/e2e so the strict gate doesn't break existing
  scenarios (fixtures without frontmatter = unmanaged).

## Risks

- **Frontmatter vs regex parser** — main technical risk; covered by `parse_tasks`
  regression tests.
- **Backward compatibility** — all existing frontmatter-less projects must keep
  working under default `spec_governance: off`; invariant verified by e2e.
- **importlib.resources** for templates — must work from a wheel, not only a dev
  checkout.
