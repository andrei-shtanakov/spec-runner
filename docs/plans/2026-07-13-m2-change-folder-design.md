# Design: Change-as-Folder Lifecycle (M2)

**Date:** 2026-07-13
**Status:** Approved — Fork A decided 2026-07-13: **Option 1, no contract
change** (owner). M2 ships additive in v2.x; `change_id` in `--json-result`
deferred to a possible future, well-telegraphed v3.0.
**Roadmap:** `2026-07-13-openspec-inspired-roadmap.md`, milestone M2.
**Pattern borrowed:** OpenSpec `changes/<name>/` — each change is a
self-contained folder; parallel changes coexist; completed changes archive to
`changes/archive/<date>-<name>/` with full context preserved.

## Problem

One flat `spec/` per project. `--spec-prefix` gives flat namespacing with no
lifecycle: no parallel in-flight changes with isolated state, no archive, no
"why" preserved after completion.

## Key discovery that reshapes the milestone

The roadmap assumed M2 must touch the Maestro contract (state-db location +
`change_id` in `--json-result`) and therefore forces v3.0. Reading the actual
code shows the first half of that is wrong:

1. **State-db location is already configuration, not contract.** The contract
   is the SQLite *schema* + the `--json-result` *stdout shape*
   (`docs/state-schema.md`, `schemas/*.json`). The db *path* is already mobile:
   `paths.state` config moves it, and `--spec-prefix` already relocates it to
   `spec/.executor-{prefix}state.db`. A change-scoped location
   (`spec/changes/<id>/.executor-state.db`) is another config-driven path —
   same precedent, schema untouched, **not a breaking change**.
2. **The run lock derives from the state path**
   (`config.state_file.with_suffix(".lock")`, `cli.py:129`), so a per-change
   state-db automatically yields a per-change executor lock — parallel
   `run --change A` ∥ `run --change B` do not contend, with zero new lock code.
3. **`--json-result` `TaskResult` has `additionalProperties: false`.** Adding
   `change_id` would break any strict validator pinned to the current schema —
   that is the *only* genuinely contract-breaking part of the original M2
   scope, and it is severable.

## Fork A — contract: add `change_id` to `--json-result`?

| | Option 1: no contract change (recommended) | Option 2: add `change_id` |
|---|---|---|
| `--json-result` | unchanged, byte-stable | +`change_id` when running with `--change` |
| Schema/golden | untouched | schema + 5 golden fixtures + state-schema.md updated |
| Version | v2.x (minor) | v3.0 (major, per repo policy) |
| Maestro correlation | by invocation — the orchestrator passes `--change <id>` itself, so it already knows which change a result belongs to | in-band in every result |
| Risk | none | strict validators on the old schema reject new output |

Recommendation: **Option 1**. The orchestrator is the one choosing the change
id per invocation; echoing it back adds no information Maestro doesn't have.
If in-band correlation is ever needed (e.g. mixed multi-change runs), it can
ship later as the sole, well-telegraphed v3.0 change.

## Design (independent of Fork A)

### Layout

```
spec/
├── tasks.md / requirements.md / design.md     # flat layout — unchanged
├── changes/
│   ├── add-dark-mode/                         # one in-flight change
│   │   ├── tasks.md                           # same formats as flat spec/
│   │   ├── requirements.md / design.md        # optional, gated pipeline works
│   │   ├── .executor-state.db (+ .lock)       # per-change state → per-change run lock
│   │   ├── .executor-logs/
│   │   └── .spec.lock
│   └── archive/
│       └── 2026-07-13-fix-auth/               # completed, dated, verbatim
```

A change folder is a **self-rooted spec dir**: every existing subsystem
(parser, gated pipeline, governance, verify/report, stage profiles) works
inside it unchanged, because all paths flow through the `ExecutorConfig`
path seam.

### Mechanism: `config.change_id`

New optional field `change_id: str = ""` (YAML `change`, CLI `--change <id>`).
A single private helper redirects the spec dir:

```python
@property
def spec_dir(self) -> Path:
    base = self.project_root / "spec"
    return base / "changes" / self.change_id if self.change_id else base
```

- All `*_file` properties (`tasks_file`, `requirements_file`, `design_file`,
  `constitution_file`, `spec_lock_file`, `stop_file`) switch from
  `project_root / "spec"` to `self.spec_dir`. With `change_id == ""` this is
  byte-identical to today (flat layout untouched).
- `__post_init__` state/logs namespacing: when `change_id` is set and
  `state_file`/`logs_dir` are at their defaults, relocate them into the change
  folder. Explicit `paths.state`/`paths.logs` still win (same rule as
  `spec_prefix` today).
- `spec.py:stage_path` already builds from the config properties' convention —
  it gains `spec_dir` awareness for custom profile stages.
- **Mutual exclusion:** `--change` + `--spec-prefix` together → `ConfigError`.
  Prefixes namespace *within* a spec dir; changes *are* dirs. Combining them
  multiplies path variants for no known use case. Can be relaxed later.

### Change identity

`.openspec.yaml`-style metadata file is **descoped** — OpenSpec needs it
because change dirs carry per-change schema config; our per-change config is
the global config + CLI flags. The folder name is the id. Validation:
`^[a-z0-9][a-z0-9._-]*$` (kebab-case recommendation, archive-prefix-safe),
`archive` reserved.

### CLI (new `change` command family, `change_commands.py`)

| Command | Behavior |
|---|---|
| `change new <id>` | create `spec/changes/<id>/` + seed `tasks.md` stub; refuse if exists |
| `change list` | in-flight changes with task counts (from parse) + a `run --change` hint; `--json` |
| `change archive <id>` | refuse unless all tasks done (or `--force`); move folder to `changes/archive/YYYY-MM-DD-<id>/`; date = today, collision → `-2` suffix. M2 archives **without merging** — delta merge is M3. |
| `run/status/verify/... --change <id>` | operate inside the change folder (flows through config) |

`change list`/`archive` never touch state-dbs of other changes; `archive`
refuses while the change's executor lock is held (live run).

### Governance & profiles

Per-change and unchanged: gated pipeline, `spec approve/reject`, stage
profiles, `spec_governance` all read paths from config, so they scope to the
change automatically. This is the payoff of M4's profile-threading.

### Out of scope (M2)

- Delta specs + merge on archive (M3 — archive here only moves the folder).
- `change_id` in `--json-result` (Fork A option 2; only if chosen, else never).
- Cross-change dependencies, change templates, proposal.md scaffolding.

## Testing

- Path seam: `change_id` redirects every `*_file` property; empty `change_id`
  byte-identical (extend the zero-behaviour proof).
- Parallel isolation: two changes, both `run` (fake CLI) concurrently — no
  lock contention, states independent.
- CLI: new/list/archive happy paths; archive refusals (tasks not done, lock
  held, id collision in archive); id validation.
- Contract: `test_json_result_contract.py` untouched and green (Option 1).
- E2E: `change new` → seed tasks → `run --change` → `change archive`.

## Rollout

Single PR, additive, off unless `--change`/`change` used. CHANGELOG under
Unreleased (minor). CLAUDE.md command list + README snippet updated.
