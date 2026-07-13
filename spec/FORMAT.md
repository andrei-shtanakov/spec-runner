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
| Status | `TODO`, `IN_PROGRESS`, `DONE`, `BLOCKED` | `todo` |
| Estimate | `Est: Nd`, `Est: N-Md`, `Est: Nh` | empty |

Priority emoji mapping: 🔴=P0, 🟠=P1, 🟡=P2, 🟢=P3
Status emoji mapping: ⬜=TODO, 🔄=IN_PROGRESS, ✅=DONE, ⏸️=BLOCKED

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

Pattern for references: `[A-Z]+-\d+` within the bold field value.

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
