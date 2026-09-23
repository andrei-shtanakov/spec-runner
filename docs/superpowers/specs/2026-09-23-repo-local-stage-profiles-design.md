# Repo-local stage profiles and external stages — design (#338)

Status: **agreed in chat 2026-09-23, awaiting review of this document.**
Issue: spec-runner#338 (`slug: repo-local-stage-profiles`, from devtools).

## 1. Problem

`spec approve tasks` derives `traces_to` and `upstream_hashes` from the stage
profile. The only profile is the bundled `lite` (`requirements → design →
tasks`), so in a devtools workstream — where `tasks.md` is generated from a
bundle node — approval appends a non-existent `design` upstream and pins
nothing for the real upstream. devtools works around it with a bridge that
pre-writes the frontmatter and a `--conform-approve` pass that rewrites it
after the owner's approval.

Two things are missing, not one:

- **a profile the repository declares** — `load_profile` reads only
  `spec_runner/profiles/*.yaml`;
- **a stage whose file lives elsewhere and is produced by someone else** —
  `stage_path` is always `spec_dir/<prefix><stage>.md`, and every stage is
  assumed to be generated, validated and approved by spec-runner. The devtools
  anchor is `workstreams/<ws>/spec/30-decomposition.md` (default mode;
  `15-behaviour-spec.md` only in legacy mode 3), outside `spec/` and not named
  by the stage.

Measured facts this design rests on (read-only survey, 2026-09-23):

- `derive_traces_to` adds every direct upstream **name** even when its file is
  missing; `upstream_pins` pins only upstreams whose file exists.
- `cmd_spec_approve` does **not** check that upstream stages are approved —
  only `plan --gated` (`run_gated_stage`) gates on it.
- `split_frontmatter` swallows YAML errors and reports "no frontmatter".
- CLI stage arguments are hardcoded `choices=["requirements","design","tasks"]`
  (`plan --stage`, `spec approve|reject|check|adopt`).
- `validate_profile_graph` raises plain `ValueError`, so a real cycle is
  reported as "unknown spec_profile" (`resolve_spec_profile`).
- A bundle node carries its own frontmatter, e.g. `spec_stage:
  decomposition`, `status: draft`.

## 2. Decisions (owner, 2026-09-23)

1. Profiles may live in the repository; a name that collides with a bundled
   profile is **refused**, never silently shadowed.
2. A stage may be `external`, and an external stage declares its `path`.
   A **managed** stage keeps its default path in this release (§4.1).
3. An external upstream **admits** approval of a downstream stage when its file
   exists and
   - its frontmatter has `status` → it must equal `approved`;
   - its frontmatter has no `status` (or there is no frontmatter) → existence
     suffices.
4. **Malformed frontmatter** in an external file is a clear error, never read
   as "no status".
5. An external stage stays external: spec-runner checks the admission
   condition and **never writes** approval (or anything else) into it.
6. `lite` and every default behaviour are unchanged.

## 3. Where profiles come from

- Repo-local: `<project_root>/spec/profiles/<name>.yaml`.
- Bundled: `spec_runner/profiles/<name>.yaml` (today's `lite`).
- Selection is unchanged: `spec_profile` in config, `--profile` on the CLI.
- `load_profile(name, project_root)` looks in both. The name exists in exactly
  one place, or it is an error:
  - neither → `ConfigError` listing the available names (both sources);
  - both → `ConfigError`: "profile `<name>` exists in spec/profiles/ and is
    bundled; rename the local one". No precedence rule to remember.
- `available_profiles(project_root)` lists both, marked by source.
- A change folder (`--change <id>`) reads the same `spec/profiles/`: profiles
  belong to the repository, not to a change.

## 4. Profile format — two new, optional stage fields

```yaml
name: workstream
stages:
  - name: decomposition
    external: true
    path: "workstreams/{ws}/spec/30-decomposition.md"
    upstream: []
  - name: tasks
    template: tasks.template.md
    marker_prefix: SPEC_TASKS
    validator: tasks
    upstream: [decomposition]
```

- **`path`** — allowed **only** on an external stage (§4.1). Relative to
  `project_root`. Two placeholders:
  - `{prefix}` — `spec_prefix` as given;
  - `{ws}` — `spec_prefix` without one trailing `-` (the devtools workstream
    id: prefix `durable-…-20260915-` → `durable-…-20260915`).

  Any other `{…}` is a load-time error. An absolute path, or one that resolves
  outside `project_root`, is refused. A managed stage keeps
  `spec_dir/<prefix><stage>.md`.
- **`external: true`** — produced outside spec-runner. `template`,
  `marker_prefix` and `validator` become **optional**; if given they are
  refused (an external stage is never generated or validated here, and a
  field that is never read is a lie about the stage).
- An external stage must declare `path`: its location is by definition not
  spec-runner's convention.
- Every existing field and rule stays: `upstream`/`requires`, graph
  validation, the four required keys for non-external stages.

`StageDef` gains `path: str | None = None` and `external: bool = False`.
`stage_path(config, stage)` consults the profile's `StageDef.path` first —
which, by §4.1, only an external stage has.

### 4.1 Managed stages keep their default path

`path` on a **managed** (non-external) stage is refused at load in this
release. The execution side does not read `stage_path`: `run`, `watch`,
`retry` and every `task` operation use `config.tasks_file`
(`spec_dir/<prefix>tasks.md`), and so do the `requirements_file` /
`design_file` readers. Letting the profile move `tasks` would split one
stage across two files — generated and approved at one path, executed at
another. The issue does not need it; if it is ever wanted, it comes with
`config.tasks_file` reading the profile, as its own change.

### 4.2 No two stages share a file

After the placeholders are substituted and symlinks resolved
(`Path.resolve()` under `project_root`), every stage's path must be distinct —
external against external, and external against every managed stage's
default path. A collision is refused when the profile is resolved for a
config (the prefix is needed to substitute), before any command reads or
writes a stage: an external path that lands on `spec/<prefix>tasks.md` would
otherwise let `plan --gated` or `approve` write into the external file,
which §2.5 forbids. The refusal names both stages and the resolved path.

## 5. What each command does with an external stage

| Command | External stage as **target** | External stage as **upstream** |
|---|---|---|
| `plan --gated` | refused: "`<stage>` is external — produced outside spec-runner" | gate = admission condition (§6); its body is read as upstream context |
| `plan --gated` auto-resolve | never proposes it; if it is the next missing stage, says "waiting for external `<stage>` at `<path>`" | — |
| `spec approve` / `reject` / `adopt` | refused (same message) | `approve` checks admission (§6) |
| `spec check` | refused | — |
| `spec status` | listed as `external`, with `present` / `missing` / `status: <value>` / `malformed frontmatter` | — |
| derive `traces_to` / pin `upstream_hashes` | — | as for any upstream, at its declared path |
| `mark_downstream_stale` | never writes into it | — |

Refusals on an external target exit 1 without a traceback.

## 6. Admission of an external upstream

Evaluated by `spec approve <stage>` for each **external** direct upstream of
`<stage>`, by `spec adopt <stage>` (the other door into `approved`: a stage
whose external upstream is not admitted is adopted as `draft`, and `--force`
does not lift that — it waives validation, not admission), and by
`plan --gated` where it gates on upstreams today:

1. file missing → refused: "external upstream `<name>` not found at `<path>`";
2. file present, frontmatter malformed (a `---` block that is not valid YAML,
   or not a mapping) → refused, naming the file and the YAML error;
3. frontmatter has `status` and it is not `approved` → refused, quoting the
   value;
4. otherwise admitted.

A strict frontmatter reader is added for this (`split_frontmatter` keeps its
lenient behaviour for every other caller). Admission reads; it never writes.

`approve` still does not check **non-external** upstreams — that is today's
behaviour under `lite`, and this design does not change it. Only an external
upstream, which spec-runner cannot otherwise account for, is gated.

## 7. CLI

- Stage arguments of `plan --stage` and `spec approve|reject|check|adopt`
  lose their hardcoded `choices`; the name is checked against the resolved
  profile after config is built, with the profile's names in the error.
  Under `lite` the accepted set is unchanged.
- `ProfileGraphError` is raised for graph errors (cycle, unknown upstream),
  so `resolve_spec_profile` reports them as such instead of "unknown
  spec_profile". Becomes user-facing with repo-local profiles.

## 8. Unchanged (and pinned)

- `lite` — stage names, fields, templates, prompts, goldens (`test_c1_zero_behaviour.py`,
  `test_stage_profile.py`) byte-identical.
- `derive_traces_to` / `upstream_pins` / `_merge_traces` semantics — only the
  path they read comes from the profile.
- `--full` ignores the profile, as today.
- No state-DB, `--json-result` or schema change. Additive → minor.

## 9. Acceptance

In a fixture repo with `spec/profiles/workstream.yaml` as in §4,
`spec_prefix: ws-`, `workstreams/ws/spec/30-decomposition.md` present with
`status: approved`:

- `spec approve tasks --profile workstream` writes
  `traces_to: [decomposition, …]` (no `design`) and
  `upstream_hashes: {decomposition: <git blob of that file>}` — the issue's
  observable, with the devtools default anchor;
- the same with `status: draft` → refused, quoting `draft`; with no `status`
  → approved; with a broken YAML block → refused naming the error; file
  missing → refused naming the path;
- the external file's bytes are unchanged after every command;
- `spec approve decomposition` → refused as external;
- an external stage whose `path` resolves — directly, or through a symlink —
  to `spec/<prefix>tasks.md` → refused when the profile is resolved, naming
  both stages; `spec approve tasks` and `plan --gated` then write nothing, and
  both files' bytes are unchanged;
- `path` on a managed stage → refused at load;
- a local profile named `lite` → refused at load;
- a cyclic local profile → reported as a graph error.

## 10. Out of scope / follow-ups

- devtools adopting it (declaring the profile, dropping `--conform-approve`) is
  their change: an issue to devtools after merge, no edit to their repo.
- Generating external stages, or validating them, is not spec-runner's job.
- `path` for managed stages (§4.1) — not in this release; would require
  `config.tasks_file` and the other stage-file readers to follow the profile.
