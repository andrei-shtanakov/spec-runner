# SpecMeta contract v2 — lossless frontmatter + `owner_role` (C2)

> Date: 2026-07-26 · Status: approved for planning
> Consumer: steward (pins a vendored copy) · Owner of the format: spec-runner
> Upstream ask: `../prograph-vault/authored/notes/2026-07-15-spec-runner-specmeta-v2-handoff.md`
> Source bundle (draft, superseded in part by this doc): `../_cowork_output/spec-runner-c2-specmeta-contract/`

## 1. Problem

steward governs artifacts whose frontmatter extends spec-runner's `SpecMeta` with
governance fields (`owner_role`, `traces_to`, `upstream_hashes`). It parses them into its
own `ArtifactMeta` layer and keeps a hand-pinned vendored copy of our parser
(`steward/src/steward/_vendor/spec_meta.py`, DEC-003).

Three things are wrong today.

**spec-runner silently destroys those fields.** `_render()` serializes `asdict(meta)` and
`meta_from_dict()` drops unknown keys, so every spec-runner write — `spec approve`,
`write_spec`, the stale cascade — rewrites the file without them. Verified against the
live code: a document carrying `owner_role`, `traces_to` and `upstream_hashes` loses all
three on one parse→render round-trip. This is data loss, not a missing convenience.

**`SPEC_META_CONTRACT` does not exist upstream.** steward invented the constant in its
vendored copy (`SPEC_META_CONTRACT: int = 1`, "re-vendor when it bumps"). "Bump to 2" in
the handoff therefore means "declare it for the first time".

**The governance gate is bypassable under a custom stage profile.** `read_spec_meta(path,
stages)` returns `None` (unmanaged) when `spec_stage` is not in `stages`, and 7 of 11 call
sites pass no `stages`, defaulting to `lite`. One of them is `spec_run_gate_ok`
(`cli.py:203`). With a non-`lite` profile, a never-approved draft `tasks.md` reads as
unmanaged and passes `spec_governance: strict`. Reproduced: stage `acceptance` yields
`None` with default stages and a valid `SpecMeta` with the profile's stages. This is a
shipped bug (profiles landed in v2.9.0), not merely an inconsistency.

## 2. Design principle

spec-runner owns its own fields and must be a **lossless intermediary** for extending
layers. Fixing only `owner_role` would mask the boundary problem while `traces_to`,
`upstream_hashes` and the next governance key keep disappearing.

## 3. Contract

### 3.1 Shape

```python
@dataclass
class SpecMeta:
    ...                                            # the nine v1 fields, unchanged
    owner_role: str | None = None                  # first-class, no steward-specific enum
    extra: dict[str, Any] = field(default_factory=dict)   # opaque foreign keys
```

`owner_role` is a first-class field because it is named in the contract. `traces_to` and
`upstream_hashes` deliberately stay opaque `extra` entries — they are steward's domain
model, not ours.

Canonical (wire) fields are computed by subtraction:

```python
canonical_fields = {f.name for f in fields(SpecMeta)} - {"extra"}
```

Consequences, all intended:

- a frontmatter key literally named `extra` is foreign and lands in `meta.extra["extra"]`;
- `meta_to_dict()` flattens extras rather than nesting the internal field;
- canonical fields are applied last, so extras can never shadow a reserved key;
- `dataclasses.replace()` preserves extras;
- the public signatures of `read_spec_meta` / `write_spec` do not change.

`SpecMeta.__post_init__` copies the incoming extras mapping, so a caller-owned dict cannot
mutate metadata through an alias.

### 3.2 Round-trip guarantee

Semantic, not textual: **keys and YAML values survive; comments, quoting style and original
key order do not.** A document with no extras and no `owner_role` renders byte-identically
to 2.10.0.

`meta_to_dict()` emits extras first, then canonical fields. By construction extras hold
only non-canonical keys, so a collision cannot occur; the ordering is cheap insurance and
makes "canonical wins" true by mechanism rather than by argument. `meta_to_dict()` also
re-validates that every `extra` key is a string, so a hand-constructed invalid `SpecMeta`
fails loud instead of emitting a non-string key.

**Optional-field serialization rule.** Fields added in v2 and later are omitted when
`None`; the v1 fields keep their current behaviour. Concretely `owner_role=None` emits
nothing, while `approved_by`/`approved_at` continue to emit `null` as they always have.
Without this asymmetry every round-trip would append `owner_role: null` to every existing
spec file and break the byte-compatibility claim.

### 3.3 Parse order and the managed boundary

Managed-detection happens **before** full `SpecMeta` validation. Otherwise a malformed
managed document falls back into `None` and the fail-open hole stays open.

1. Split the YAML frontmatter as a mapping, assuming nothing about key types.
2. No frontmatter, or not a mapping → `None`.
3. `spec_stage` present, a string, and a member of the **active profile's** stage names?
   No → `None`. Foreign and partial frontmatter keeps behaving exactly as today.
4. From here the document is managed and can no longer degrade to `None`:
   - any non-string key → `SpecMetaError`;
   - any malformed canonical field (e.g. `version: "three"`) → `SpecMetaError`.
5. Unknown string keys → `extra`.

**Validation matrix.** "Against declared types" is not specific enough — two implementers
could legitimately ship different v2 contracts. The exact rules:

| Field | Type rule | Value rule |
|---|---|---|
| `spec_stage` | `str` | member of the active profile's stage names (step 3) |
| `status` | `str` | must be one of `draft` / `approved` / `stale` |
| `version` | `type(v) is int` | none |
| `generated_by`, `generated_at`, `source_prompt_version` | `str` | none |
| `validation` | `str` | none |
| `approved_by`, `approved_at` | `str \| None` | none |
| `owner_role` | `str \| None` | none — steward owns role semantics |
| `extra` keys | `str` | none |
| `extra` values | any YAML value | none — opaque by definition |

Three deliberate choices:

- `version` uses `type(v) is int`, not `isinstance`, because `isinstance(True, int)` is
  `True` and `version: true` must not slip through as `1`. **No range check** — the value
  only ever increments from the default of 1, and a bound would reject hand-written files
  for no governance benefit. Implementers should not add one.
- `status` is value-checked because it drives the state machine: `stage_readiness`
  (`spec.py:341,359,371,394,398,405`), `mark_downstream_stale` (`spec.py:451`) and the run
  gate all compare it against literals, so an unrecognized value silently matches no branch
  — another fail-open.
- `validation` is type-checked only. It is written (`cli_plan.py:149`, `spec.py:479`,
  `spec_commands.py:156`) and displayed (`spec_commands.py:61`), and drives no decision.
  Constraining an advisory field would add breakage risk with no governance benefit.

**What today actually does — verified, and worse than the handoff assumed.** There is no
validation at all. `meta_from_dict` filters with `{k: v for k, v in d.items() if k in
known}`, so a non-string key is *silently dropped* rather than raising. Dataclasses do not
enforce types, so `version: "three"` is accepted verbatim as the string `'three'` and
`status: [1, 2]` as a list. Consequently the `except TypeError` guard in `read_spec_meta`
(`spec.py:260`) is effectively unreachable: the only way to reach the constructor with an
unexpected keyword has already been filtered out.

So step 4 **adds validation that never existed** rather than redirecting an existing error.
That carries real risk: a spec file in the wild with a quoted `version: "2"` parses today
and would fail loud after this change. The implementation must therefore validate canonical
fields against their declared types explicitly, and the plan must include a survey of the
fixtures and specs in this repo to confirm none of them rely on the accidental tolerance.
The now-dead `except TypeError` is removed as part of the change.

An unknown or invalid `spec_stage` stays unmanaged. That is the existing, deliberate
profile semantics (`read_spec_meta`, DESIGN-303) and this design does not change it.

**"Managed cannot degrade to `None`" is not a guarantee for syntactically invalid YAML.**
`split_frontmatter` returns `(None, text)` on a `yaml.YAMLError`, so a document whose
frontmatter does not parse is unmanaged before `spec_stage` can be examined at all — step 3
is unreachable. That is existing policy and stays out of scope here; the fail-closed
guarantee begins only once the frontmatter has parsed into a mapping and the stage has been
recognized.

`SpecMetaError` is a new exception alongside the existing `SpecLockError`.

### 3.4 Profile-aware call sites

Every config-aware caller must pass `config.resolve_spec_profile().names()`. Without this
the new fail-loud policy is trivially bypassed under a custom profile — the same hole that
lets the run gate be bypassed today.

| Call site | Function |
|---|---|
| `cli.py:203` | `spec_run_gate_ok` — **the governance-gate bypass** |
| `spec_commands.py:77,89` | `cmd_spec_approve` |
| `spec_commands.py:99` | `cmd_spec_reject` |
| `spec_commands.py:151` | `cmd_spec_check` |
| `cli_plan.py:98,135` | `_generate_stage_draft` (gated plan) |

Already correct: `spec_commands.py:48`, `cli_plan.py:252`, `spec.py:450`
(`mark_downstream_stale`), `spec.py:471` (`apply_approval`).

This is a behavioural bugfix independent of the contract work and gets its own regression
test and CHANGELOG entry.

### 3.5 Version

`SPEC_META_CONTRACT: int = 2`, declared upstream for the first time. v1 is documented as
the implicit historical contract that steward pinned from observed behaviour; v2 is the
first version the owner declares.

Bump policy: adding an optional field is non-breaking and does not bump; removing or
renaming a field bumps. The arrival of `extra` never causes a future bump by itself —
extending layers now grow without our involvement, which is the point.

### 3.6 `approver`: closed by documentation, not by a field

The source bundle's REQ-402 assumes `approved_by` carries an agent-id and proposes a
separate `approver` field. That premise is false: `_approver()`
(`spec_commands.py:31`) returns `git config user.name`, and the agent-id already lives in
`generated_by` (`cli_plan.py:141`, `<harness>@<model>`). The code already matches what
steward asked for. No new field; `docs/CONTRACTS.md` states the semantics instead.

### 3.7 Frozen public surface (REQ-404)

Exported from `spec_runner.__init__`: `SpecMeta`, `SpecMetaError`, `split_frontmatter`,
`strip_frontmatter`, `split_frontmatter_raw`, `read_spec_meta`, `read_spec_body`,
`write_spec`, `meta_from_dict`, `meta_to_dict`, `SPEC_STAGES`, `SPEC_META_CONTRACT`.
Everything else in `spec.py` is documented as private and outside the contract.

`docs/CONTRACTS.md` carries the symbol list, the field table with semantics, the bump
policy and the changelog of contract versions.

## 4. Error handling

A single `try/except SpecMetaError` wraps the whole dispatch block in `cli.main()`,
raising `SystemExit(f"⛔ {exc}") from None` — a clean diagnostic, no traceback, non-zero
exit, matching the existing treatment of an unknown spec profile (`cli.py:1354`).
`executor.py` only re-exports `cli.main`, so this is the process boundary; per-command
handling would drift.

Writes cannot be left partial: `write_spec` uses `mkstemp` + `os.replace`, and parse
errors occur before any write. **The stale cascade is sequential, not transactional** —
`mark_downstream_stale` reads and writes one downstream document at a time, so a failure
on a malformed downstream halts the cascade with earlier writes already committed. This is
documented rather than changed; making it transactional is out of scope.

## 5. Testing

Contract and round-trip:

- parse → render → parse preserves several unknown scalar / list / mapping fields;
- `owner_role` round-trips;
- real `spec approve` and `write_spec` do not erase extras;
- a canonical field cannot be overridden through extras;
- a document with no extras stays byte-compatible with 2.10.0;
- extras are copied on construction — mutating the caller's dict does not reach the meta;
- `meta_to_dict()` fails loud on a hand-constructed meta with a non-string extra key;
- the canonical field set equals an explicit expected set, so adding an internal dataclass
  field cannot silently widen the wire contract;
- golden fixture `spec_meta_contract_v2.md` + expected dict in package data (REQ-406).

Managed boundary:

- unmanaged frontmatter with a non-string key → `None`;
- valid managed `spec_stage` + non-string key → `SpecMetaError` (today: silently dropped);
- valid managed stage + malformed canonical field → `SpecMetaError` (today: silently
  accepted with the wrong type) — one case per row of the validation matrix, including
  `version: true` rejected by the `type(v) is int` rule and an unrecognized `status`;
- `validation` with an unrecognized string value is accepted (type-only rule);
- unknown `spec_stage` stays unmanaged;
- syntactically invalid frontmatter YAML stays unmanaged (pins the documented limit of the
  fail-closed guarantee);
- every spec file and fixture in this repo still parses under the new validation.

Bugfix and boundary:

- a custom-profile managed `tasks.md` in draft is **blocked** by `run --strict`
  (regression test for the gate bypass);
- `SpecMetaError` at the CLI boundary exits non-zero with no traceback.

### Regression gate (supersedes REQ-403's absolute form)

- the existing 1129 tests stay green;
- tests are not rewritten to hide a regression;
- where an existing test explicitly asserts the old fail-open or data-loss behaviour, its
  expectation may be replaced together with a new regression test and a written rationale.

An absolute "touch no test" rule would force preserving the very bug this work fixes.

## 6. Follow-up for steward (not our work)

Once shipped: steward re-vendors `split_frontmatter` / `SpecMeta` / `meta_from_dict` as
contract v2 and drops the "read `owner_role` from the raw frontmatter dict" workaround in
`steward/meta.py`. Communicated via a handoff note; spec-runner does not edit steward.

## 7. Out of scope

- `traces_to` / `upstream_hashes` as domain fields — they stay opaque extras.
- A transactional stale cascade.
- Preserving comments, quoting style or key order in frontmatter.
- Any change to the Maestro interop contract (`.executor-state.db`, `--json-result`).
