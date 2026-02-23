# Phase 6: Extensibility — Plugin System, Spec Generation, Config Validation

**Goal:** Make spec-runner extensible (custom hooks via plugins), self-bootstrapping (generate full specs interactively), and safe (validate config + tasks before execution).

**Scope:** Three features, ~510 lines of new code, 2 new modules.

---

## 1. Plugin System (hooks only)

### Architecture

Plugins live in `spec/plugins/<name>/`. Each plugin is a directory with a manifest and executable scripts:

```
spec/plugins/
  notify-slack/
    plugin.yaml
    on_done.sh
  deploy-staging/
    plugin.yaml
    deploy.py
```

### Plugin manifest (`plugin.yaml`)

```yaml
name: notify-slack
description: Send Slack notification on task completion
version: "1.0"
hooks:
  post_done:
    command: "./on_done.sh"
    run_on: on_success    # always | on_success | on_failure
    blocking: false       # if true, failure stops execution
  # pre_start:
  #   command: "./setup.sh"
  #   blocking: false
```

### Hook execution protocol

Plugin hooks run as subprocesses. Context is passed via environment variables:

```
SR_TASK_ID=TASK-001
SR_TASK_NAME=API endpoints
SR_TASK_STATUS=success
SR_TASK_PRIORITY=p0
SR_PROJECT_ROOT=/path/to/project
SR_ATTEMPT_NUMBER=2
SR_DURATION_SECONDS=145.3
SR_ERROR=              # empty on success
SR_ERROR_CODE=         # empty on success
```

### Execution order

1. Built-in `pre_start_hook()` runs first
2. Plugin `pre_start` hooks run in alphabetical order by plugin name
3. Task executes
4. Built-in `post_done_hook()` runs (tests, lint, review, commit)
5. Plugin `post_done` hooks run in alphabetical order

Non-blocking plugins: failure = warning in logs, execution continues.
Blocking plugins (`blocking: true`): failure = error, execution stops.

### New code

- `src/spec_runner/plugins.py` (~120 lines) — discover, load, validate, execute plugin hooks
- Modify `hooks.py` — call `run_plugin_hooks(event, task, config, success)` at appropriate points
- Modify `config.py` — add `plugins_dir: Path` field to `ExecutorConfig` (default: `spec/plugins`)

---

## 2. Spec Generation (`spec-runner plan --full`)

### How it works

Extends existing `cmd_plan()` in `executor.py`. Currently `spec-runner plan "description"` generates only task proposals. With `--full`, it generates the complete spec trilogy.

### Three-stage pipeline

```
spec-runner plan --full "Build a REST API for user management"
```

**Stage 1: Requirements**
- Claude receives project description + `requirements.template.md` as format reference
- Interactive Q&A via existing QUESTION/OPTIONS protocol
- Output: `spec/requirements.md` with `[REQ-001]`.`[REQ-N]` tags
- User sees output, confirms or asks for edits

**Stage 2: Design**
- Claude receives generated requirements + `design.template.md`
- Proposes architecture, components, data flow
- Output: `spec/design.md` with `[DESIGN-001]`.`[DESIGN-N]` tags tracing to REQs
- User confirms

**Stage 3: Tasks**
- Claude receives requirements + design + `tasks.template.md`
- Generates task breakdown with priorities, estimates, dependencies, checklists
- Output: `spec/tasks.md` with `TASK-001`.`TASK-N`, traceability to REQ/DESIGN
- User confirms

### Stage markers in Claude output

```
SPEC_REQUIREMENTS_READY
<requirements content>
SPEC_REQUIREMENTS_END

SPEC_DESIGN_READY
<design content>
SPEC_DESIGN_END
```

Parser extracts content between markers, writes to `spec/`.

### Multi-phase support

`spec-runner plan --full --spec-prefix=phase2-` generates `phase2-requirements.md`, `phase2-design.md`, `phase2-tasks.md`.

### New code

- Modify `executor.py:cmd_plan()` — add `--full` flag, three-stage loop (~80 lines)
- Add `prompt.py:build_generation_prompt(stage, context, template)` (~50 lines)
- No new modules

---

## 3. Config Validation (`spec-runner validate`)

### Error checks (exit code 1)

| Check | Description |
|-------|-------------|
| tasks.md exists | `spec/{prefix}tasks.md` must exist and be readable |
| tasks.md parses | At least 1 task extracted |
| Config YAML valid | `executor.config.yaml` — valid YAML syntax |
| Unknown config keys | Keys not in ExecutorConfig fields (catches typos like `max_retry`) |
| Dependency graph acyclic | No circular deps |
| Dependency refs exist | `depends_on: TASK-999` but TASK-999 not in tasks.md |
| Invalid status | Status not in {todo, in_progress, done, blocked} |
| Invalid priority | Priority not in {p0, p1, p2, p3} |

### Warning checks (exit code 0)

| Check | Description |
|-------|-------------|
| Missing estimates | No `Est:` field |
| Missing traceability | No `Traces to:` refs |
| Orphan traceability | `[REQ-005]` referenced but not in requirements.md |
| Blocked without deps | status=blocked but no `depends_on` |
| Empty checklist | Task has description but no checklist items |
| Unused plugins | Plugin in `spec/plugins/` but hooks don't match any event |

### Output format

```
$ spec-runner validate

spec/tasks.md
  x TASK-003: depends on TASK-999 which does not exist
  x TASK-007 -> TASK-012 -> TASK-007: circular dependency

executor.config.yaml
  x unknown key: max_retry (did you mean max_retries?)

spec/tasks.md (warnings)
  ! TASK-004: missing estimate
  ! TASK-006: no traceability refs

2 errors, 2 warnings
```

### "Did you mean?" for typos

For unknown config keys, compute Levenshtein distance to known keys. If distance <= 2, suggest the closest match (~10 lines, no external deps).

### Integration with `spec-runner run`

`spec-runner run` calls `validate()` automatically before execution. Errors = abort. Warnings = log and proceed.

### New code

- Create `src/spec_runner/validate.py` (~200 lines) — all checks, error/warning formatting
- Modify `executor.py` — add `validate` subcommand + pre-run validation (~20 lines)
- Modify `config.py` — expose known field names for unknown-key detection (~5 lines)

---

## Summary

| Feature | New files | Modified files | ~Lines |
|---------|-----------|----------------|--------|
| Plugin system | `plugins.py` | `hooks.py`, `config.py`, `__init__.py` | ~150 |
| Spec generation | — | `executor.py`, `prompt.py` | ~130 |
| Config validation | `validate.py` | `executor.py`, `config.py`, `__init__.py` | ~230 |
| **Total** | **2 new modules** | **5 modified** | **~510** |

### Implementation order

1. **Config validation** — simplest, immediately useful, no external deps
2. **Plugin system** — builds on hooks.py, needed before spec generation can use plugins
3. **Spec generation** — extends existing plan command, benefits from validation

### Testing strategy

- `test_validate.py` — unit tests for each check (errors + warnings), edge cases
- `test_plugins.py` — plugin discovery, execution, env vars, blocking/non-blocking
- `test_plan_full.py` — stage markers, file writing, multi-phase (mock Claude CLI)
