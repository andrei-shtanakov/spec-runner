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
| YAML frontmatter at start of file | Ignored (no frontmatter support) |

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
