# Spec Generator Skill

## Overview

A skill for creating project specifications in Kiro-style format: three interlinked documents with requirements-to-tasks traceability.

## When to Use

- Starting a new project -- creating a full specification
- Documenting an existing project
- Requests like: "create a spec", "write a project brief", "document the project"

## Structure

A specification consists of 3 files in the `spec/` directory:

```
project/
└── spec/
    ├── requirements.md   # WHAT we build
    ├── design.md         # HOW we build it
    └── tasks.md          # WHEN we build it
```

## Files

### 1. requirements.md

**Contains:**
- Project context and goals
- Stakeholders
- Out of Scope (explicit!)
- Functional requirements (REQ-XXX) in User Story + GIVEN-WHEN-THEN format
- Non-functional requirements (NFR-XXX)
- Constraints and tech stack
- Acceptance criteria per milestone

**Requirement format:**
```markdown
#### REQ-001: Title
**As a** <role>
**I want** <action>
**So that** <value>

**Acceptance Criteria:**
\```gherkin
GIVEN <precondition>
WHEN <action>
THEN <result>
AND <additional result>
\```

**Priority:** P0 | P1 | P2 | P3
**Traces to:** [TASK-XXX], [DESIGN-XXX]
```

### 2. design.md

**Contains:**
- Architectural principles
- High-level diagram (ASCII)
- System components (DESIGN-XXX)
- APIs and interfaces
- Data schemas
- Key decisions (ADR)
- Directory structure

**Component format:**
```markdown
### DESIGN-001: Component Name

#### Description
...

#### Interface
\```python
class Component(ABC):
    @abstractmethod
    def method(self, param: Type) -> ReturnType:
        pass
\```

#### Configuration
\```yaml
component:
  option: value
\```

**Traces to:** [REQ-XXX]
```

### 3. tasks.md

**Contains:**
- Priority and status legend
- Tasks (TASK-XXX) with checklists
- Dependencies between tasks
- Traceability to requirements
- Dependency graph
- Summary per milestone

**Task format:**
```markdown
### TASK-001: Title
🔴 P0 | ⬜ TODO | Est: 3d

**Description:**
Brief description of the task.

**Checklist:**
- [ ] Subtask 1
- [ ] Subtask 2
- [ ] Subtask 3

**Traces to:** [REQ-XXX], [REQ-YYY]
**Depends on:** [TASK-ZZZ]
**Blocks:** [TASK-AAA]
```

## Traceability

The key feature is linking documents together:

```
REQ-001 ──────► DESIGN-001
    │               │
    │               ▼
    └─────────► TASK-001
```

- Every requirement references its design and tasks
- Every design references its requirements
- Every task references its requirements and design
- Use the format `[REQ-XXX]`, `[DESIGN-XXX]`, `[TASK-XXX]`

## Priorities

| Emoji | Code | Description |
|-------|------|-------------|
| 🔴 | P0 | Critical -- blocks release |
| 🟠 | P1 | High -- required for full usability |
| 🟡 | P2 | Medium -- improves user experience |
| 🟢 | P3 | Low -- nice to have |

## Statuses

| Emoji | Status | Description |
|-------|--------|-------------|
| ⬜ | TODO | Not started |
| 🔄 | IN PROGRESS | In progress |
| ✅ | DONE | Completed |
| ⏸️ | BLOCKED | Blocked |

## Creation Process

1. **Gather context:**
   - What problem are we solving?
   - Who are the users?
   - What are the constraints?

2. **Start with requirements.md:**
   - Goals and success metrics
   - Out of scope (important!)
   - Requirements in user story format
   - Acceptance criteria in GIVEN-WHEN-THEN

3. **Then design.md:**
   - Architecture driven by requirements
   - Components and interfaces
   - ADRs for key decisions
   - References to requirements

4. **Finish with tasks.md:**
   - Break down the design into tasks
   - Dependencies between tasks
   - Estimates and priorities
   - Milestones

## Templates

File templates are located in `templates/`:
- `requirements.template.md` -- requirements template
- `design.template.md` -- design template
- `tasks.template.md` -- tasks template
- `workflow.template.md` -- workflow guide
- `task.py` -- CLI for task management
- `executor.py` -- automated execution via Claude CLI
- `executor.config.yaml` -- executor configuration
- `Makefile.template` -- Make targets for the project

## Examples

See examples in `examples/`:
- `atp-platform/` -- Agent Test Platform

## Task Management

The specification includes a CLI for task management:

```bash
# === Manual mode ===
python task.py list              # List tasks
python task.py next              # Next available tasks
python task.py start TASK-001    # Start a task
python task.py done TASK-001     # Complete a task

# === Automatic mode (Claude CLI) ===
python executor.py run           # Execute the next task
python executor.py run --all     # Execute all ready tasks
python executor.py status        # Show status
python executor.py retry TASK-001
```

**Automated execution:**
- Builds a prompt from spec/* for Claude
- Runs `claude -p "<prompt>"`
- Validates the result (tests, lint)
- On failure -- retries with a limit
- Safeguards: max_retries=3, max_consecutive_failures=2

A `Makefile` is also generated with targets:
- `make exec` -- execute the next task
- `make exec-all` -- execute all ready tasks
- `make exec-mvp` -- MVP milestone only

See `spec/WORKFLOW.md` for more details.

## TASK-000: Project Scaffolding

**IMPORTANT:** When creating a specification for a **new project** (not an existing one), always include TASK-000 as the first task. This task blocks all other tasks.

```markdown
### TASK-000: Project Scaffolding
🔴 P0 | ⬜ TODO | Est: 1h

**Description:**
Initialize project structure: directories, configuration, dependencies.

**Checklist:**
- [ ] Create directories (src/, tests/, examples/)
- [ ] Create pyproject.toml with runtime and dev dependencies
- [ ] Run `uv sync` to create the virtual environment
- [ ] Create .gitignore
- [ ] Initialize the git repository

**Traces to:** —
**Depends on:** —
**Blocks:** [TASK-001], [TASK-002], ...
```

**When TASK-000 is NOT needed:**
- The project already exists (pyproject.toml, src/, etc. are present)
- You are documenting existing code
- You are adding a feature to an existing project

## Best Practices

1. **Out of Scope is mandatory** -- explicitly state what is NOT part of the project
2. **Acceptance criteria must be concrete** -- use GIVEN-WHEN-THEN, not abstractions
3. **Full traceability** -- every requirement must be linked to tasks
4. **Honest priorities** -- not everything is P0; distribute realistically
5. **Approximate estimates** -- a range (3-5d) is better than an exact number
6. **ADRs for important decisions** -- document the "why", not just the "what"
7. **Dependency graph** -- visualize task dependencies
8. **Tests in every task** -- Definition of Done includes unit tests
9. **NFR for testing** -- a coverage requirement is mandatory
10. **Test tasks come first** -- TASK-100 (Test Infrastructure) blocks the rest
