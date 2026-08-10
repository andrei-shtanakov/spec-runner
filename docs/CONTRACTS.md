# SpecMeta frontmatter contract

This document describes the YAML frontmatter contract that spec-runner owns and
extending layers (primarily steward) consume. It complements
`docs/state-schema.md` (the Maestro execution-state/`--json-result` contract),
which is unrelated and unaffected by anything here.

**Source of truth:** `src/spec_runner/spec.py` — dataclass `SpecMeta` and the
parse/render functions below. **Everything else in `spec.py`** (`StageDef`,
`StageProfile`, `load_profile`, the stage-graph helpers, `apply_approval`,
`write_spec`'s locking, etc.) **is private** and outside this contract; it may
change without a contract-version bump.

## Contract version

```python
from spec_runner import SPEC_META_CONTRACT
```

`SPEC_META_CONTRACT: int = 2`, declared upstream (spec-runner) for the first
time in this release. See [Contract changelog](#contract-changelog) below for
why v1 predates this constant's existence.

**Bump policy:**

- adding an optional field does **not** bump the contract;
- removing or renaming a field **does** bump it;
- the existence of `SpecMeta.extra` — i.e. a consumer writing new foreign
  frontmatter keys — never bumps the contract by itself. That is the point of
  `extra`: extending layers grow without spec-runner's involvement.

## Frozen public surface

The following symbols are importable from the `spec_runner` package root and
are covered by this contract (pinned by
`tests/test_spec_meta_contract.py::test_frozen_surface_is_importable_from_package_root`):

| Symbol | Kind |
|---|---|
| `SpecMeta` | dataclass |
| `SpecMetaError` | exception |
| `SPEC_META_CONTRACT` | `int` constant |
| `SPEC_STAGES` | `tuple[str, ...]` constant (the `lite` profile's stage names) |
| `split_frontmatter` | function |
| `strip_frontmatter` | function |
| `split_frontmatter_raw` | function |
| `read_spec_meta` | function |
| `read_spec_body` | function |
| `write_spec` | function |
| `meta_from_dict` | function |
| `meta_to_dict` | function |

A consumer should import only from `spec_runner` (the package root), never
from `spec_runner.spec` directly — the module path is not part of the
contract, only the re-export is.

## `SpecMeta` field table

`canonical_fields()` (= `{f.name for f in fields(SpecMeta)} - {"extra"}`) is
the exact set of wire (frontmatter) keys spec-runner owns. Every other
frontmatter key a document carries is foreign and lands in `SpecMeta.extra`
verbatim.

| Field | Type rule | Value rule |
|---|---|---|
| `spec_stage` | `str` | member of the active profile's stage names |
| `status` | `str` | one of `draft` / `approved` / `stale` |
| `version` | `type(v) is int` (not `isinstance` — a bool must not pass as an int) | none |
| `generated_by`, `source_prompt_version` | `str` | none |
| `validation` | `str` | none (advisory field, drives no decision) |
| `approved_by` | `str \| None` | none |
| `generated_at`, `approved_at` | `str`, or `datetime.datetime` / `datetime.date` (normalized via `.isoformat()`); `approved_at` also accepts `None` | none |
| `owner_role` | `str \| None` | none — steward owns role semantics, spec-runner is only the carrier |
| `extra` keys | `str` | none |
| `extra` values | any YAML value | none — opaque by definition |

A violation of any type or value rule above raises `SpecMetaError` once the
document is recognized as managed (see
[Managed boundary](#managed-boundary-and-fail-closed-parsing) below).

`spec_stage` has no default: calling `meta_from_dict()` directly (bypassing
`read_spec_meta`'s stage-recognition gate) with a dict that omits it raises
`SpecMetaError("frontmatter is missing required field 'spec_stage'")` rather
than the constructor's own `TypeError`, so a consumer that catches only
`SpecMetaError` per this contract never sees an unrelated exception type.
This path is unreachable through `read_spec_meta`, since a missing
`spec_stage` there is treated as unmanaged (returns `None`) before
`meta_from_dict` is ever called; it only matters for direct `meta_from_dict`
use.

### `generated_at` / `approved_at`: the date-scalar exception

These two fields alone accept YAML's native `datetime.date` /
`datetime.datetime` scalars (e.g. a hand-written `generated_at: 2026-07-05`
with no quotes) and normalize them to a string via `.isoformat()`. This is a
narrow, documented compatibility exception — not a general loosening of the
string rule — because a bare YAML date is the same information in YAML's own
scalar type, unlike e.g. `version: true`, where a bool masquerades as an int.
`datetime.datetime` is checked before `datetime.date` since `datetime`
subclasses `date`. Every other string-typed field rejects a date value.

### `owner_role`

The accountable governance role for the stage: a **DEC-007 role slug** —
exactly one role, no `@`, no comma-list (e.g. `platform`). The role catalog
(steward `profiles/roles.yaml`) is the SSOT for role identity; multiplicity
is modelled by separate fields on the steward side (`reviewer_roles`,
`allowed_approver_roles`), never inside `owner_role`.

spec-runner validates only that the value is a string or `None`; the role
semantics belong to the consumer (steward), not to spec-runner. In
particular, legacy pre-DEC-007 values (`"@role[,@role]"`, e.g.
`"@platform,@sre"`) are still **carried verbatim** — steward's own data has
not fully migrated, and rejecting them here would break round-trips.

### `approved_by` vs. `generated_by`

- `approved_by` is the **git handle of the human** who ran `spec approve`
  (`git config user.name`, via `_approver()` in `spec_commands.py`).
- `generated_by` is `<harness>@<model>` — the agent identity that produced the
  draft (e.g. `claude@claude-opus-5`).

These are two different actors recorded in two different fields; there is no
separate "approver" field, and none is planned — an earlier proposal to add
one was closed as documentation, because the semantics above already match
what was being asked for.

## Round-trip guarantee

**Semantic, not textual.** Keys and YAML values survive a
parse → mutate → render → parse cycle; comments, quoting style, and the
original key order in the source file do **not** survive.

- `meta_to_dict()` emits extras first, canonical fields last, so a canonical
  field can never be shadowed by a foreign key — this is enforced by
  construction (extras are checked against `canonical_fields()` at
  serialization time), not merely by convention.
- A document with **no** extras and no `owner_role` set renders
  byte-identically to spec-runner 2.10.0 (the pre-contract-v2 behaviour).
- Fields added in contract v2 and later (currently only `owner_role`) are
  omitted from the rendered frontmatter when `None`, so an existing document
  does not gain a new `owner_role: null` key on its next write. The v1
  nullable fields (`approved_by`, `approved_at`) keep emitting `null` as they
  always have — changing that would itself be a round-trip break.

### `validation`: the parser is more permissive than the published JSON schema

`_coerce_canonical` type-checks `validation` only (any `str` passes) because
the field is an advisory cache that drives no decision in spec-runner
itself. `schemas/spec-frontmatter.schema.json`, by contrast, pins it to
`enum: ["pass", "fail", "warn", ""]` for the benefit of downstream consumers
(the VS Code extension) who *do* want to reject an out-of-vocabulary value.
This divergence is deliberate and will not be reconciled: a hand-edited
`validation: weird` round-trips cleanly through `meta_from_dict`/`meta_to_dict`
(e.g. surviving a `spec reject`) but fails validation against the published
schema. Treat the schema, not the parser, as authoritative for what a
well-formed document's `validation` value should be.

## Authored extras: `traces_to` and `upstream_hashes` (DEC-008)

These two keys are **not** canonical fields — they belong to steward's
governance vocabulary and ride through `SpecMeta.extra` like any other foreign
key. What changed with DEC-008 (issue #135) is that spec-runner, as the
authoring side, now **writes** them: steward is the validator of these fields
and never rewrites a generated artifact, so a field nobody wrote stayed empty
and every spec-runner-authored bundle came back `GC-TRACE-EMPTY` +
`GC-STALE-UNPINNED`.

| Key | Shape | Written when |
|---|---|---|
| `traces_to` | list of id strings: the stage's **direct** upstream stage name(s) first, then id tokens (`REQ-001`, `DESIGN-207`) that occur in the downstream body **and** resolve in the upstream text | whenever content is authored — `plan --gated` draft, `spec approve`, `spec adopt` |
| `upstream_hashes` | mapping `{direct upstream stage: git blob hash}`, reproducible as `git hash-object <upstream file>` over the whole file (frontmatter included) | at approval only — `spec approve`, `spec adopt` when it lands on `approved` |

Rules that follow from the consumer's checks, not from taste:

- **Direct upstream only.** A pin for a transitive ancestor is reported as
  `GC-STALE-KEY`; a `traces_to` entry that resolves to nothing is a `GC-TRACE`
  **error**, which is worse than the empty-link warning. Derived id tokens are
  therefore verified against the upstream body and dropped when absent.
- **Absent, never empty.** A first stage (no upstream) gets neither key. An
  upstream file that does not exist is left unpinned rather than pinned to a
  value that was never its content.
- **Additive, not authoritative.** An existing `traces_to` value is kept —
  entries in their original order, derived ones appended, a legacy scalar
  normalized into the list steward's reader requires. spec-runner materializes
  what it can prove from the stage chain and deletes nobody else's claim. Where
  it can prove nothing (a first stage), it leaves the field untouched.
- **Pins are not refreshed behind your back.** Only the approval of *that* stage
  restamps its pins. Re-approving an upstream cascades `stale` downstream and
  deliberately leaves the old pin in place — that mismatch is exactly the signal
  steward's stale-cascade reads.

## Managed boundary and fail-closed parsing

`read_spec_meta(path, stages)` returns `None` for an **unmanaged** document
(no frontmatter, frontmatter that isn't a YAML mapping, or a `spec_stage` not
in `stages`) — this stays permissive, unchanged from prior versions, and is
depended on by the spec-governance gate (an unmanaged `tasks.md` always
passes `run --strict`).

Once a document's `spec_stage` **is** recognized, it is managed and can no
longer silently degrade to `None`: a non-string frontmatter key, or any
canonical field violating the table above, raises `SpecMetaError` rather than
being silently dropped or accepted with the wrong type. This is new in
contract v2 — previously `meta_from_dict` filtered to known keys and
dataclasses did not enforce types, so e.g. `version: "three"` was silently
accepted as the string `'three'`.

The one case that stays outside this guarantee: syntactically invalid YAML
frontmatter is unmanaged, full stop — `split_frontmatter` returns `(None,
text)` before `spec_stage` can even be examined, so step 3 (the managed
check) is unreachable. The fail-closed guarantee begins only once the
frontmatter has parsed into a mapping and the stage has been recognized.

At the CLI process boundary, `main()` wraps the whole dispatch in a single
`try/except SpecMetaError`, exiting with `SystemExit(f"⛔ {exc}")` — a clean
diagnostic, no traceback, non-zero exit.

## The stale cascade is sequential, not transactional

`mark_downstream_stale` reads and writes one downstream document at a time.
If a downstream document is malformed (raises `SpecMetaError`) partway
through, the cascade halts with the earlier writes already committed —
there is no rollback. This is documented behaviour, not a bug to fix here;
making it transactional is out of scope for this contract.

## Golden fixture

`src/spec_runner/contract_fixtures/spec_meta_contract_v2.md` ships as package
data specifically so a consumer can validate its own parser against the exact
same bytes spec-runner tests against:

```python
from importlib.resources import files

text = (files("spec_runner.contract_fixtures") / "spec_meta_contract_v2.md").read_text()
```

See `tests/test_spec_meta_contract.py::test_golden_fixture_round_trips` for
the reference assertions (extras present, `owner_role` present, round-trip
stability).

Its two governance extras were corrected with DEC-008: the fixture used to show
`traces_to` as a scalar and pin a transitive ancestor, neither of which a
consumer's reader accepts. It now shows what spec-runner actually writes.

## Contract changelog

- **v1 (implicit, historical).** Never declared by spec-runner. steward
  inferred this version by observing spec-runner's behaviour and pinned
  `SPEC_META_CONTRACT = 1` in its own vendored copy. Under v1, foreign
  frontmatter keys were silently discarded on every write (`spec approve`,
  `write_spec`, the stale cascade), and canonical fields were accepted
  unchecked.
- **v2 (this release).** Declared upstream for the first time.
  - `SpecMeta.extra` — foreign frontmatter keys are preserved losslessly
    through parse and render.
  - `SpecMeta.owner_role: str | None` — a first-class field.
  - Canonical fields are validated against the table above; a violation
    raises `SpecMetaError` instead of being silently dropped or
    misinterpreted.
  - A recognized-but-malformed managed spec now fails loud instead of
    silently reading as unmanaged (closing a governance-gate bypass).
  - The frozen public surface (above) and this document are new.
