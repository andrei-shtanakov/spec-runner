# spec-runner Task Format Specification

This document formally describes the `tasks.md` file format parsed by spec-runner.

## File Structure

Tasks are defined in a markdown file (default: `spec/tasks.md`). The file consists of:

1. Optional milestone headings (`## Milestone ...`)
2. Task entries starting with `### TASK-NNN: Name`
3. Task metadata, checklists, and dependency declarations

## Task Entry

### Header (required)

```markdown
### TASK-001: Short descriptive name
```

Pattern: `^### (TASK-\d+): (.+)$`

- `TASK-\d+` — unique task identifier (e.g., TASK-001, TASK-042)
- Name — free text after the colon

### Metadata Line (optional)

```markdown
🔴 P0 | ⬜ TODO   Est: 2-3d
```

Or plain text (no emoji):

```markdown
P0 | TODO   Est: 2-3d
```

Pattern: `^(?:emoji\s+)?(P\d)\s*\|\s*(?:emoji\s+)?(\w+)`

| Field | Values | Default |
|-------|--------|---------|
| Priority | `P0`, `P1`, `P2`, `P3` | `p0` |
| Status | `TODO`, `IN_PROGRESS`, `REVIEW`, `DONE`, `BLOCKED` | `todo` |
| Estimate | `Est: Nd`, `Est: N-Md`, `Est: Nh` | empty |

Priority emoji mapping: 🔴=P0, 🟠=P1, 🟡=P2, 🟢=P3
Status emoji mapping: ⬜=TODO, 🔄=IN_PROGRESS, 🔍=REVIEW, ✅=DONE, ⏸️=BLOCKED

`REVIEW` is written by the executor itself when a task's tests/lint gates
passed and code review is still running — an interrupted run leaves this
honest intermediate state instead of a premature DONE. Review-status tasks
are resumed like IN_PROGRESS ones.

### Description (optional)

Free text lines between the header/metadata and the first bold metadata field or checklist. Currently captured as the `description` field on the Task dataclass.

### Checklist (optional)

```markdown
**Checklist:**
- [x] Completed item
- [ ] Pending item
```

Pattern: `^- \[([ x])\] (.+)$`

Items must start at column 0 (no indentation). Indented checklist items are silently ignored.

### Dependency Declarations (optional)

```markdown
**Depends on:** [TASK-002], [TASK-003]
**Blocks:** [TASK-005]
**Traces to:** [REQ-001], [DESIGN-003]
```

- `Depends on` — tasks that must complete before this task can start
- `Blocks` — tasks that depend on this task (reverse of depends_on)
- `Traces to` — traceability references to requirements/design documents

Pattern for references: each id in its own square brackets, comma-separated
— `[REQ-001], [REQ-002]`. The parser reads only bracketed ids, so
`[REQ-001, REQ-002]` yields no references at all. The id shape differs per
field: `Traces to` accepts `[A-Z]+-\d+`; `Depends on` and `Blocks` accept
`[A-Z][A-Z0-9]*-\d+` (digits allowed in the prefix, e.g. `[KAP2-001]`) and
then keep only prefixes that task headers actually use.

### Execution Declarations (optional)

```markdown
### TASK-007: Re-verify login after the refactor
**Mode:** verify_first
**Verifies:** tests/test_login.py::test_ok, tests/test_login.py::test_locked
**Scenarios:** BEH-09, BEH-10

### TASK-008: Pin current login behaviour
**TDD-waiver:** characterisation · sanction: spec-runner#428
**Negative-control:** tests/controls/break_login.patch :: tests/test_login.py::test_ok
```

Each is its own line in the task body. The two tasks above show the two
combinations that belong together; a `TDD-waiver` on a `tdd` or
`verify_first` task is an error. Values are stored as written and
judged later, so a typo is refused with the declared text quoted back
instead of being mapped to something plausible.

- `Mode` — per-task override of the project's `execution_mode`:
  `standard`, `tdd` or `verify_first`. It works both ways: opt in while the
  project is `standard`, or opt out while it is `tdd`. Case-insensitive; an
  unknown word is an error that names the task.
- `Verifies` — the group of checks a `verify_first` task runs live before
  any paid agent call. Selectors are pytest node ids (`path::test`). Two
  forms: comma-separated on the same line, or a `- <selector>` bulleted
  block on the following lines when nothing follows the marker. A node id
  whose parametrize suffix contains a comma (`test_y[a,b]`) must use the
  block form; in the comma form it is refused, not split. Required
  (non-empty) under `verify_first`, and never inferred from `Traces to`,
  filenames or the checklist.
- `Scenarios` — optional, `verify_first` only: comma-separated scenario ids
  (`BEH-09`, `BEH-09a`) the `Verifies` group must carry. At the live entry
  run, before any paid call, every id must appear as a whole token
  (`BEH-09` does not match `BEH-091`, `BEH-09a` or `TestBEH09`) in at least
  one of the group's files **as committed**; otherwise the task is refused
  terminally, naming the uncovered ids. Coverage is per file, not per test,
  and ids are unqualified: a foreign file carrying its own `BEH-09`
  satisfies it — the check catches a group that claims nothing about the
  task's scenarios, not one that claims them falsely. `validate` only
  warns (the working tree is not the commit); an empty or malformed line is
  an error. Without the line nothing changes.
- `TDD-waiver` — `<class> · sanction: <id>`. Valid only on a task whose
  resolved mode is `standard`. It removes the baseline-RED requirement and
  nothing else: active claims and the frozen-files block still apply. The
  class is a closed vocabulary (`characterisation` today). The sanction must
  be `batch-approve-<YYYY-MM-DD>` or `<repo>#<number>`; only its form is
  checked.
- `Negative-control` — `<path to patch> :: <selector>`. The separator is
  ` :: ` with spaces (the `::` inside a pytest node id has none). The patch
  is committed with the work and breaks the property the test claims to
  check. The harness requires the selector to pass on the clean commit and
  fail with the patch applied before review. Required on every task that
  is not yet done and carries a `TDD-waiver`; an error on any task that
  does not carry one.

Details: `docs/architecture.md` (execution modes, verify-first) and
`docs/state-schema.md` (tables `waivers_applied`, `negative_controls`).

### Milestone Grouping (optional)

```markdown
## Milestone 1: MVP
```

Tasks under a milestone heading inherit the milestone name. Milestones are used for filtering (`--milestone`).

## Validation Rules

### Errors (block execution)

- Status must be one of: `todo`, `in_progress`, `done`, `blocked`
- Priority must be one of: `p0`, `p1`, `p2`, `p3`
- Dependencies must reference existing task IDs
- No dependency cycles allowed
- No duplicate task IDs allowed

### Warnings (reported but don't block)

- Missing estimate
- `blocked` status without dependencies
- No traceability references
- Asymmetric blocks/depends_on (A blocks B but B doesn't depend on A)

## Edge Cases

| Situation | Behavior |
|-----------|----------|
| `## TASK-001: Foo` (2 `#` instead of 3) | Task not found — silently skipped |
| Missing metadata line | Defaults: priority=p0, status=todo |
| Duplicate TASK ID | Validation error |
| `- [ ] item` with indentation | Not matched — silently skipped |
| Text between header and checklist | Captured as description |
| YAML frontmatter at start of file | Stripped before parsing, and preserved on write-back (see below) |

## Frontmatter (gated spec governance, optional)

Under the opt-in gated-generation workflow (`spec-runner plan --gated`, `spec-runner
spec ...`), `tasks.md` (and `requirements.md`/`design.md`) may carry a leading YAML
frontmatter block tracking `SpecMeta` (`src/spec_runner/spec.py`):

```yaml
---
spec_stage: tasks
status: approved   # draft | approved | stale
version: 2
generated_by: claude
generated_at: 2026-07-01T00:00:00
source_prompt_version: ""
validation: pass   # pass | warn | fail | ""
approved_by: ""
approved_at: 2026-07-01T00:05:00
---
```

`parse_tasks()` strips this block before parsing the task entries below it;
`update_task_status()` / `mark_all_checklist_done()` preserve it on write-back.
Files without frontmatter ("unmanaged") parse exactly as before — this is purely
additive. See `README.md#spec-governance-gated-generation` for the full workflow.

## Example

```markdown
## Milestone 1: Authentication

### TASK-001: Implement login endpoint
🔴 P0 | ⬜ TODO   Est: 2d

Implement the /api/login endpoint with JWT token generation.

**Checklist:**
- [ ] Create route handler
- [ ] Add JWT signing
- [ ] Write integration tests

**Traces to:** [REQ-001], [DESIGN-002]

### TASK-002: Add rate limiting
🟡 P2 | ⬜ TODO   Est: 1d

**Depends on:** [TASK-001]
**Traces to:** [REQ-003]
```

## Requirements Format (`requirements.md`)

`requirements.py:parse_requirements()` reads `requirements.md` into id-keyed
requirement blocks so a requirement is a diffable, mergeable unit (the
foundation for delta specs). Real requirement bodies vary widely — gherkin
acceptance criteria, `- [ ]` checklists, or plain prose — so the parser is
**tolerant** and anchors on only two firm signals:

1. **Requirement heading** — a line matching `#+ (REQ|NFR)-NNN[: Name]` at any
   heading depth. `REQ-` is functional, `NFR-` is non-functional.
2. **Block boundary** — the block runs from its heading until the next heading
   whose level is the same or higher (i.e. fewer-or-equal `#`). Everything in
   between — prose, code fences, `---` rules — belongs to that requirement and
   is preserved verbatim in the block's `raw` text.

Best-effort optional fields extracted from each block (empty when absent):

| Field | Source | Notes |
|-------|--------|-------|
| `priority` | `**Priority:** …` / `**Priority**: …` | colon inside or outside the bold |
| `acceptance_criteria` | text under `**Acceptance Criteria:**` | up to the next bold-field marker or block end |
| `traces_to` | `REQ-`/`DESIGN-`/`TASK-`/`NFR-` refs in the block | excludes the requirement's own id |

### Example

```markdown
## 2. Functional Requirements

### 2.1 Authentication
#### REQ-001: User can log in
**Priority**: P0

**Acceptance Criteria**:
- [ ] POST /api/login returns a JWT on valid credentials
- [ ] Invalid credentials return 401

**Traces to:** [DESIGN-002], [TASK-001]
```

### Validation

`spec-runner validate` (and the gated `requirements` stage) checks: unique
requirement ids, an `Out of Scope` section, and that acceptance criteria are
present. As of M1 it additionally warns per functional requirement that has no
acceptance-criteria section (NFRs are exempt to avoid noise).

## Delta Specs (`spec/changes/<id>/specs/requirements.md`)

A change may carry a **delta spec** describing only what changes relative to
the flat `spec/requirements.md`. On `change archive` the delta is merged into
the target (all-or-nothing; conflicts abort the archive). Identity is the
REQ/NFR **id**, so matching is exact.

Four level-2 sections, each containing the same id-keyed blocks as above:

```markdown
## ADDED Requirements

#### REQ-010: Dark mode
**Acceptance Criteria**:
- [ ] theme toggles

## MODIFIED Requirements

#### REQ-001: Login          <- full updated block; replaces the target block
New behavior text.

## REMOVED Requirements

#### REQ-002: Legacy export
**Reason**: replaced by the new export system
**Migration**: use REQ-010 flow

## RENAMED Requirements

- FROM: `### NFR-001: Performance`
- TO: `### NFR-001: Performance & Latency`
```

| Section | Rule | On archive |
|---------|------|------------|
| `ADDED` | id must **not** exist in target | block appended |
| `MODIFIED` | id must exist; carry the **full** updated block | block replaced |
| `REMOVED` | id must exist; `**Reason**` + `**Migration**` mandatory | block deleted |
| `RENAMED` | id must exist; FROM name must match target; id never changes | heading rewritten |

Conflicts (unknown id, duplicate ADDED, several ops on one id, name mismatch)
are hard errors listing the requirement id. `spec-runner validate --change
<id>` checks the delta against the current target early;
`change archive --dry-run` prints the merge plan without changing anything.
Re-archiving the same delta conflicts instead of double-applying.
