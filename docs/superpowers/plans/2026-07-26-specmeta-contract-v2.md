# SpecMeta Contract v2 Implementation Plan (PR 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make spec-runner a lossless intermediary for foreign frontmatter keys, add `owner_role` as a first-class field, and declare `SPEC_META_CONTRACT = 2` so steward can re-vendor and drop its workaround.

**Architecture:** `SpecMeta` gains `owner_role: str | None` and an opaque `extra: dict[str, Any]`. Canonical (wire) fields are `fields(SpecMeta) - {"extra"}`, so a frontmatter key literally named `extra` is foreign. `meta_from_dict` splits known from unknown and validates canonical fields against an explicit matrix; `meta_to_dict` flattens extras first and applies canonical fields last. Managed-detection in `read_spec_meta` happens before validation, so foreign documents stay unmanaged while a recognized-but-malformed spec fails loud instead of silently degrading.

**Tech Stack:** Python 3.10+, PyYAML, pytest, ruff (line length 100), mypy strict, uv.

## Global Constraints

- **Depends on PR 1** (`docs/superpowers/plans/2026-07-26-profile-aware-spec-surface.md`) being merged. Branch from an updated `master`. Without it the fail-loud policy is bypassable under a custom profile.
- Design doc: `docs/superpowers/specs/2026-07-26-specmeta-contract-v2-design.md`. Where this plan and the doc disagree, the doc wins.
- Ruff line length **100**; rules E, F, W, I, UP, B, C4, SIM with E501 ignored.
- Type annotations required everywhere; mypy strict must stay clean.
- Baseline before starting: `uv run pytest tests/ -q -m "not slow"` = **1147 passed** (measured on `master` at `7d336cc`, after PR 1 merged). Per-task counts below are relative to it; report the ACTUAL count each time rather than treating a predicted number as a gate.
- The Maestro interop contract (`.executor-state.db` schema, `--json-result`) must not change.
- A document with no extras and no `owner_role` must render **byte-identically** to before this PR.
- Regression gate: existing tests stay green; a test may only have its expectation changed when it explicitly asserts the old fail-open or data-loss behaviour, and then only together with a new regression test and a written rationale in the commit message.
- Commit style: `<type>(<scope>): <subject>`, ending with `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.
- Branch `feat/specmeta-contract-v2`; direct commits to `master` are forbidden; the user merges the PR.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `src/spec_runner/spec.py` | `SpecMeta`, frontmatter parse/render, `SpecMetaError`, `SPEC_META_CONTRACT` | Main change |
| `src/spec_runner/cli.py` | `main()` dispatch | Wrap dispatch in one `except SpecMetaError` |
| `src/spec_runner/__init__.py` | Public API | Export the frozen contract surface |
| `src/spec_runner/contract_fixtures/spec_meta_contract_v2.md` | Golden fixture for consumers | Create |
| `pyproject.toml` | Packaging | Ship the fixture as package data |
| `docs/CONTRACTS.md` | The contract itself | Create |
| `tests/test_spec_meta.py` | Existing `SpecMeta` tests | Extend |
| `tests/test_spec_meta_contract.py` | Contract, round-trip, validation matrix | Create |
| `CHANGELOG.md` | Release notes | Add `[Unreleased] / Added` + `Changed` |

---

### Task 1: `SpecMetaError` and the canonical validation matrix

**Files:**
- Modify: `src/spec_runner/spec.py` (near `SpecLockError`, and `meta_from_dict` ~line 227, `read_spec_meta` ~line 247-262)
- Test: `tests/test_spec_meta_contract.py` (create)

**Interfaces:**
- Produces: `SpecMetaError(Exception)`; `canonical_fields() -> frozenset[str]`; `_coerce_canonical(key: str, value: object) -> object` (internal, validates and normalizes); `meta_from_dict(d: dict) -> SpecMeta` now raising `SpecMetaError`. Later tasks rely on the first three.

- [ ] **Step 1: Record the baseline test count**

Run: `uv run pytest tests/ -q -m "not slow" | tail -1`
Write the number down; every later step compares against it.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_spec_meta_contract.py`:

```python
"""Contract tests for SpecMeta v2: validation matrix, extras, round-trip."""

import pytest

from spec_runner.spec import SpecMeta, SpecMetaError, canonical_fields, meta_from_dict


def _base(**over):
    d = {"spec_stage": "tasks", "status": "draft", "version": 1}
    d.update(over)
    return d


def test_canonical_fields_excludes_extra():
    assert "extra" not in canonical_fields()
    assert "spec_stage" in canonical_fields()


def test_version_must_be_int():
    with pytest.raises(SpecMetaError, match="version"):
        meta_from_dict(_base(version="three"))


def test_version_rejects_bool():
    """isinstance(True, int) is True — the check must be type(v) is int."""
    with pytest.raises(SpecMetaError, match="version"):
        meta_from_dict(_base(version=True))


def test_status_must_be_a_known_value():
    with pytest.raises(SpecMetaError, match="status"):
        meta_from_dict(_base(status="approvd"))


def test_string_field_rejects_non_string():
    with pytest.raises(SpecMetaError, match="generated_by"):
        meta_from_dict(_base(generated_by=[1, 2]))


def test_nullable_field_accepts_none_and_str():
    assert meta_from_dict(_base(approved_by=None)).approved_by is None
    assert meta_from_dict(_base(approved_by="andrei")).approved_by == "andrei"


def test_nullable_field_rejects_non_string():
    with pytest.raises(SpecMetaError, match="approved_by"):
        meta_from_dict(_base(approved_by=7))


def test_validation_is_type_checked_only():
    """validation drives no decision, so any string value is accepted."""
    assert meta_from_dict(_base(validation="weird")).validation == "weird"
    with pytest.raises(SpecMetaError, match="validation"):
        meta_from_dict(_base(validation=3))


def test_bare_yaml_date_is_normalized_to_string():
    """A hand-written `generated_at: 2026-07-05` must not be a hard error."""
    import datetime

    m = meta_from_dict(_base(generated_at=datetime.date(2026, 7, 5)))
    assert m.generated_at == "2026-07-05"
    assert isinstance(m.generated_at, str)


def test_yaml_datetime_keeps_time_and_offset():
    import datetime

    stamp = datetime.datetime(2026, 7, 5, 13, 45, 1, tzinfo=datetime.timezone.utc)
    m = meta_from_dict(_base(approved_at=stamp))
    assert m.approved_at == stamp.isoformat()
    assert "13:45:01" in m.approved_at


def test_approved_at_accepts_null():
    assert meta_from_dict(_base(approved_at=None)).approved_at is None


@pytest.mark.parametrize("bad", [7, ["2026-07-05"], True])
def test_timestamp_fields_reject_other_types(bad):
    with pytest.raises(SpecMetaError, match="generated_at"):
        meta_from_dict(_base(generated_at=bad))


def test_other_string_fields_still_reject_dates():
    """The exception is narrow: only the two timestamp fields accept dates."""
    import datetime

    with pytest.raises(SpecMetaError, match="generated_by"):
        meta_from_dict(_base(generated_by=datetime.date(2026, 7, 5)))


def test_non_string_key_raises():
    with pytest.raises(SpecMetaError, match="key"):
        meta_from_dict({"spec_stage": "tasks", 1: "foo"})


def test_valid_meta_still_parses():
    m = meta_from_dict(_base(status="approved", version=4, approved_by="andrei"))
    assert (m.spec_stage, m.status, m.version) == ("tasks", "approved", 4)
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_spec_meta_contract.py -v`
Expected: collection error — `SpecMetaError` and `canonical_fields` do not exist yet.

- [ ] **Step 4: Implement**

In `src/spec_runner/spec.py`, add next to `SpecLockError`:

```python
class SpecMetaError(Exception):
    """Raised when a managed spec's frontmatter cannot be parsed faithfully."""
```

Add the matrix constants above `meta_from_dict`:

```python
_STATUS_VALUES = frozenset({"draft", "approved", "stale"})
_STR_FIELDS = frozenset(
    {
        "spec_stage",
        "status",
        "generated_by",
        "source_prompt_version",
        "validation",
    }
)
_NULLABLE_STR_FIELDS = frozenset({"approved_by"})
#: Timestamp wire fields: accept YAML's native date scalars and normalize them
#: to a string, so a hand-written `generated_at: 2026-07-05` is not a hard
#: error. ``approved_at`` is additionally nullable.
_TIMESTAMP_FIELDS = frozenset({"generated_at", "approved_at"})
_NULLABLE_TIMESTAMP_FIELDS = frozenset({"approved_at"})


def canonical_fields() -> frozenset[str]:
    """Frontmatter (wire) field names: every SpecMeta field except ``extra``.

    Derived by subtraction so an internal dataclass field can never silently
    widen the wire contract.
    """
    return frozenset(f.name for f in fields(SpecMeta)) - {"extra"}


def _coerce_canonical(key: str, value: object) -> object:
    """Validate one canonical field against the v2 matrix, returning its value.

    Only the two timestamp fields change their value: YAML parses a bare
    ``2026-07-05`` into a ``datetime.date``, which is normalized here to a
    string so the next write canonicalizes the file (design §3.3).

    Raises:
        SpecMetaError: if the value violates the matrix.
    """
    if key in _TIMESTAMP_FIELDS:
        if value is None:
            if key in _NULLABLE_TIMESTAMP_FIELDS:
                return None
            raise SpecMetaError(f"frontmatter field {key!r} must not be null")
        if isinstance(value, str):
            return value
        # datetime BEFORE date: datetime.datetime subclasses datetime.date.
        if isinstance(value, datetime.datetime | datetime.date):
            return value.isoformat()
        raise SpecMetaError(
            f"frontmatter field {key!r} must be a string or a date, "
            f"got {type(value).__name__}"
        )
    if key in _STR_FIELDS:
        if not isinstance(value, str):
            raise SpecMetaError(
                f"frontmatter field {key!r} must be a string, got {type(value).__name__}"
            )
        if key == "status" and value not in _STATUS_VALUES:
            raise SpecMetaError(
                f"frontmatter field 'status' must be one of "
                f"{sorted(_STATUS_VALUES)}, got {value!r}"
            )
    elif key in _NULLABLE_STR_FIELDS:
        if value is not None and not isinstance(value, str):
            raise SpecMetaError(
                f"frontmatter field {key!r} must be a string or null, "
                f"got {type(value).__name__}"
            )
    elif key == "version":
        # type() not isinstance(): isinstance(True, int) is True.
        if type(value) is not int:
            raise SpecMetaError(
                f"frontmatter field 'version' must be an integer, "
                f"got {type(value).__name__}"
            )
    return value
```

Add `import datetime` to the module imports.

Replace `meta_from_dict` with:

```python
def meta_from_dict(d: dict) -> SpecMeta:
    """Build a SpecMeta from a frontmatter dict.

    Canonical fields are validated against the v2 matrix. Unknown *string*
    keys are preserved verbatim (see ``SpecMeta.extra``). A non-string key
    raises, since it cannot be round-tripped faithfully.

    Raises:
        SpecMetaError: on a non-string key or a malformed canonical field.
    """
    canonical = canonical_fields()
    known: dict[str, object] = {}
    for key, value in d.items():
        if not isinstance(key, str):
            raise SpecMetaError(f"frontmatter key {key!r} is not a string")
        if key in canonical:
            known[key] = _coerce_canonical(key, value)
    return SpecMeta(**known)  # type: ignore[arg-type]
```

In `read_spec_meta`, delete the now-dead guard so errors propagate:

```python
    try:
        return meta_from_dict(meta_dict)
    except TypeError:
        return None
```

becomes:

```python
    return meta_from_dict(meta_dict)
```

- [ ] **Step 5: Run the new tests**

Run: `uv run pytest tests/test_spec_meta_contract.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/ -q -m "not slow"`
Expected: baseline + 10. If an existing test now fails, read it: if it asserts the old silent-acceptance behaviour, update its expectation and say so in the commit message per the regression gate; if it merely uses a malformed fixture, fix the fixture.

- [ ] **Step 7: Commit**

```bash
git add src/spec_runner/spec.py tests/test_spec_meta_contract.py
git commit -m "feat(spec): validate canonical frontmatter fields, add SpecMetaError

There was no validation at all: meta_from_dict filtered unknown keys out
with a dict comprehension, so a non-string key was silently dropped, and
dataclasses do not enforce types, so version: \"three\" was accepted as a
string. The except TypeError guard in read_spec_meta was therefore dead
code and is removed.

Adds the explicit matrix from design §3.3: version is type(v) is int so
version: true cannot pass as 1; status is value-checked because it drives
the state machine; validation is type-checked only because it drives no
decision.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `extra` — preserve foreign keys through parse

**Files:**
- Modify: `src/spec_runner/spec.py` (`SpecMeta`, `meta_from_dict`)
- Test: `tests/test_spec_meta_contract.py`

**Interfaces:**
- Consumes: `canonical_fields()`, `SpecMetaError` from Task 1.
- Produces: `SpecMeta.extra: dict[str, Any]` populated by `meta_from_dict`, copied on construction.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_spec_meta_contract.py`:

```python
def test_unknown_string_keys_land_in_extra():
    m = meta_from_dict(
        _base(owner_role="@platform", traces_to="REQ-001", upstream_hashes={"design": "abc"})
    )
    assert m.extra["traces_to"] == "REQ-001"
    assert m.extra["upstream_hashes"] == {"design": "abc"}


def test_literal_extra_key_is_foreign():
    """A frontmatter key named 'extra' is foreign data, not the extras dict."""
    m = meta_from_dict(_base(extra="hello"))
    assert m.extra["extra"] == "hello"


def test_extra_is_copied_on_construction():
    """A caller-owned mapping must not mutate metadata through an alias."""
    caller = {"traces_to": "REQ-001"}
    m = SpecMeta(spec_stage="tasks", extra=caller)
    caller["traces_to"] = "REQ-999"
    assert m.extra["traces_to"] == "REQ-001"


def test_document_without_extras_has_empty_extra():
    assert meta_from_dict(_base()).extra == {}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_spec_meta_contract.py -k extra -v`
Expected: FAIL — `SpecMeta` has no `extra`.

- [ ] **Step 3: Add the field**

In `src/spec_runner/spec.py`, extend the dataclass (keep `Any` imported from `typing`):

```python
@dataclass
class SpecMeta:
    """Frontmatter state for one spec document.

    ``extra`` holds foreign frontmatter keys verbatim so spec-runner is a
    lossless intermediary for extending layers (steward). It is an internal
    field, not a wire field: see :func:`canonical_fields`.
    """

    spec_stage: str
    status: str = "draft"  # draft | approved | stale
    version: int = 1
    generated_by: str = ""
    generated_at: str = ""
    source_prompt_version: str = ""
    validation: str = ""  # pass | fail | warn | ""
    approved_by: str | None = None
    approved_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Copy: a caller-owned mapping must not mutate metadata via an alias.
        self.extra = dict(self.extra)
```

Add `field` to the existing `dataclasses` import and `Any` to the `typing` import.

- [ ] **Step 4: Capture extras in `meta_from_dict`**

Change the loop body added in Task 1 so foreign keys are collected instead of dropped:

```python
    canonical = canonical_fields()
    known: dict[str, object] = {}
    extra: dict[str, Any] = {}
    for key, value in d.items():
        if not isinstance(key, str):
            raise SpecMetaError(f"frontmatter key {key!r} is not a string")
        if key in canonical:
            known[key] = _coerce_canonical(key, value)
        else:
            extra[key] = value
    return SpecMeta(**known, extra=extra)  # type: ignore[arg-type]
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_spec_meta_contract.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/ -q -m "not slow"`
Expected: baseline + 14. Extras are captured but not yet rendered, so no round-trip test passes yet — that is Task 3.

- [ ] **Step 7: Commit**

```bash
git add src/spec_runner/spec.py tests/test_spec_meta_contract.py
git commit -m "feat(spec): capture foreign frontmatter keys in SpecMeta.extra

Canonical fields are fields(SpecMeta) - {'extra'}, so a frontmatter key
literally named 'extra' is foreign data and lands in meta.extra['extra'].
__post_init__ copies the mapping so a caller-owned dict cannot mutate
metadata through an alias.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Lossless render — flatten extras, canonical last

**Files:**
- Modify: `src/spec_runner/spec.py` (`meta_to_dict`)
- Test: `tests/test_spec_meta_contract.py`

**Interfaces:**
- Consumes: `SpecMeta.extra` from Task 2.
- Produces: `meta_to_dict(m: SpecMeta) -> dict` flattening extras first, canonical last; raising `SpecMetaError` on a non-string or canonical-shadowing extra key. `_render` is unchanged — it already calls `meta_to_dict`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_spec_meta_contract.py`:

```python
from spec_runner.spec import _render, meta_to_dict, split_frontmatter


def _round_trip(meta_dict):
    meta = meta_from_dict(meta_dict)
    text = _render(meta, "# body\n")
    again, body = split_frontmatter(text)
    return again, body


def test_round_trip_preserves_scalar_list_and_mapping_extras():
    src = _base(
        traces_to="REQ-001",
        tags=["a", "b"],
        upstream_hashes={"design": "abc", "requirements": "def"},
    )
    again, body = _round_trip(src)
    assert again["traces_to"] == "REQ-001"
    assert again["tags"] == ["a", "b"]
    assert again["upstream_hashes"] == {"design": "abc", "requirements": "def"}
    assert body == "# body\n"


def test_round_trip_is_stable_across_two_passes():
    src = _base(traces_to="REQ-001", upstream_hashes={"design": "abc"})
    first, _ = _round_trip(src)
    second, _ = _round_trip(first)
    assert first == second


def test_canonical_field_cannot_be_shadowed_by_extra():
    m = meta_from_dict(_base(status="draft"))
    m.extra["status"] = "approved"
    with pytest.raises(SpecMetaError, match="status"):
        meta_to_dict(m)


def test_meta_to_dict_rejects_non_string_extra_key():
    m = meta_from_dict(_base())
    m.extra[1] = "foo"
    with pytest.raises(SpecMetaError, match="key"):
        meta_to_dict(m)


def test_document_without_extras_renders_unchanged():
    """Byte-compatibility: no extras, no owner_role -> same text as before."""
    meta = meta_from_dict(_base(status="approved", version=2, approved_by="andrei"))
    text = _render(meta, "# body\n")
    assert "extra" not in text
    assert text.startswith("---\nspec_stage: tasks\nstatus: approved\nversion: 2\n")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_spec_meta_contract.py -k "round_trip or shadow or non_string_extra or unchanged" -v`
Expected: FAIL — `meta_to_dict` is still `asdict(m)`, so extras render as a nested `extra:` key and are not flattened.

- [ ] **Step 3: Implement**

Replace `meta_to_dict` in `src/spec_runner/spec.py`:

```python
def meta_to_dict(m: SpecMeta) -> dict:
    """Serialize a SpecMeta to a flat frontmatter dict.

    Extras are written first and canonical fields last, so a canonical field
    can never be shadowed by a foreign key. Extras holding a canonical name,
    or a non-string key, are a programming error and raise rather than
    silently corrupting the document.

    Raises:
        SpecMetaError: on a non-string or canonical-shadowing key in ``extra``.
    """
    canonical = canonical_fields()
    for key in m.extra:
        if not isinstance(key, str):
            raise SpecMetaError(f"extra frontmatter key {key!r} is not a string")
        if key in canonical:
            raise SpecMetaError(
                f"extra frontmatter key {key!r} shadows a canonical field"
            )
    out: dict[str, Any] = dict(m.extra)
    for name in _canonical_order():
        out[name] = getattr(m, name)
    return out


def _canonical_order() -> tuple[str, ...]:
    """Canonical field names in declaration order (the frontmatter order)."""
    return tuple(f.name for f in fields(SpecMeta) if f.name != "extra")
```

Define `_canonical_order` **above** `meta_to_dict` so it is defined before use at import time is irrelevant (both are module-level functions), but keep the file readable by placing helpers before their callers as the codebase does.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_spec_meta_contract.py -v`
Expected: PASS.

- [ ] **Step 5: Prove approve and the stale cascade preserve extras**

Append a test that exercises the real write path rather than `_render` directly:

```python
def test_apply_approval_preserves_extras(tmp_path, monkeypatch):
    """The real approve path must not erase steward's governance keys."""
    from spec_runner.spec import LITE, apply_approval, read_spec_meta, stage_path, write_spec

    config = _approval_config(tmp_path)   # see note below
    path = stage_path(config, "tasks")
    meta = meta_from_dict(_base(status="draft", traces_to="REQ-001"))
    write_spec(path, meta, "# body\n")

    apply_approval(config, "tasks", approver="andrei", now="2026-07-26T00:00:00Z",
                   fresh_validation="pass")

    after = read_spec_meta(path, LITE.names())
    assert after is not None
    assert after.status == "approved"
    assert after.extra["traces_to"] == "REQ-001"
```

Read `tests/test_spec_commands.py` and `tests/test_spec_meta.py` for the config helper those files already use for approval flows, and reuse it in place of `_approval_config`; do not build a new one.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/ -q -m "not slow"`
Expected: baseline + 20, all green.

- [ ] **Step 7: Commit**

```bash
git add src/spec_runner/spec.py tests/test_spec_meta_contract.py
git commit -m "feat(spec): render foreign frontmatter keys losslessly

meta_to_dict was asdict(m), so every spec-runner write silently destroyed
foreign keys: a document carrying owner_role/traces_to/upstream_hashes
lost all three on one parse->render round-trip. It now flattens extras
first and applies canonical fields last, so canonical always wins, and
raises on a non-string or canonical-shadowing extra key rather than
corrupting the document.

apply_approval and the stale cascade need no change: both read, mutate and
write through this path, so extras survive automatically. A test pins that.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `owner_role` as a first-class field

**Files:**
- Modify: `src/spec_runner/spec.py` (`SpecMeta`, `_STR_FIELDS`/`_NULLABLE_STR_FIELDS`, `meta_to_dict`)
- Test: `tests/test_spec_meta_contract.py`

**Interfaces:**
- Produces: `SpecMeta.owner_role: str | None = None`, omitted from the rendered frontmatter when `None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_owner_role_round_trips():
    again, _ = _round_trip(_base(owner_role="@platform,@sre"))
    assert again["owner_role"] == "@platform,@sre"


def test_owner_role_is_not_an_extra():
    m = meta_from_dict(_base(owner_role="@platform"))
    assert m.owner_role == "@platform"
    assert "owner_role" not in m.extra


def test_owner_role_none_is_omitted_from_output():
    """v2+ optional fields must not appear as nulls, or every existing spec
    file would gain 'owner_role: null' on its next write."""
    text = _render(meta_from_dict(_base()), "# body\n")
    assert "owner_role" not in text


def test_v1_nullable_fields_still_render_as_null():
    """approved_by/approved_at keep their historical behaviour."""
    text = _render(meta_from_dict(_base()), "# body\n")
    assert "approved_by:" in text
    assert "approved_at:" in text


def test_owner_role_rejects_non_string():
    with pytest.raises(SpecMetaError, match="owner_role"):
        meta_from_dict(_base(owner_role=["@a"]))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_spec_meta_contract.py -k owner_role -v`
Expected: FAIL — `owner_role` is currently captured as an extra, and `SpecMeta` has no such attribute.

- [ ] **Step 3: Add the field**

In `SpecMeta`, insert `owner_role` **after** `approved_at` and **before** `extra`, so the derived canonical order keeps the v1 fields in their historical positions:

```python
    approved_at: str | None = None
    owner_role: str | None = None  # CODEOWNERS role(s), "@role[,@role]"; steward owns the semantics
    extra: dict[str, Any] = field(default_factory=dict)
```

Add it to the nullable-string set:

```python
_NULLABLE_STR_FIELDS = frozenset({"approved_by", "approved_at", "owner_role"})
```

- [ ] **Step 4: Implement the omit-when-None rule**

Above `meta_to_dict`, add the set and apply it in the canonical loop:

```python
# Fields added in contract v2 and later are omitted when None, so existing
# documents do not gain new null keys on their next write. The v1 nullable
# fields (approved_by/approved_at) keep emitting null as they always have.
_OMIT_WHEN_NONE = frozenset({"owner_role"})
```

```python
    for name in _canonical_order():
        value = getattr(m, name)
        if value is None and name in _OMIT_WHEN_NONE:
            continue
        out[name] = value
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_spec_meta_contract.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/ -q -m "not slow"`
Expected: baseline + 25, all green. Pay attention to any test asserting exact frontmatter text — `owner_role` must not appear in it.

- [ ] **Step 7: Commit**

```bash
git add src/spec_runner/spec.py tests/test_spec_meta_contract.py
git commit -m "feat(spec): add owner_role as a first-class SpecMeta field

owner_role is named in the contract, so it is a real field rather than an
opaque extra; traces_to and upstream_hashes deliberately stay extras since
they are steward's domain model, not ours.

Fields added in v2 and later are omitted from the rendered frontmatter when
None — otherwise every existing spec file would gain 'owner_role: null' on
its next write and the byte-compatibility guarantee would break. The v1
nullable fields keep emitting null.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Managed boundary — fail closed after recognition

**Files:**
- Modify: `src/spec_runner/spec.py` (`read_spec_meta`)
- Test: `tests/test_spec_meta_contract.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `read_spec_meta(path, stages) -> SpecMeta | None` — `None` only for unmanaged documents; `SpecMetaError` for a recognized-but-malformed one.

- [ ] **Step 1: Write the failing tests**

```python
def _write(tmp_path, text):
    p = tmp_path / "tasks.md"
    p.write_text(text)
    return p


def test_unmanaged_document_with_non_string_key_is_none(tmp_path):
    """Foreign frontmatter stays permissive: unmanaged, not an error."""
    from spec_runner.spec import LITE, read_spec_meta

    p = _write(tmp_path, "---\ntitle: notes\n1: foo\n---\nbody\n")
    assert read_spec_meta(p, LITE.names()) is None


def test_managed_document_with_non_string_key_raises(tmp_path):
    from spec_runner.spec import LITE, read_spec_meta

    p = _write(tmp_path, "---\nspec_stage: tasks\nstatus: draft\n1: foo\n---\nbody\n")
    with pytest.raises(SpecMetaError):
        read_spec_meta(p, LITE.names())


def test_managed_document_with_malformed_known_field_raises(tmp_path):
    """Must not silently degrade to unmanaged, which would bypass the gate."""
    from spec_runner.spec import LITE, read_spec_meta

    p = _write(tmp_path, "---\nspec_stage: tasks\nstatus: draft\nversion: three\n---\nbody\n")
    with pytest.raises(SpecMetaError):
        read_spec_meta(p, LITE.names())


def test_unknown_spec_stage_stays_unmanaged(tmp_path):
    from spec_runner.spec import LITE, read_spec_meta

    p = _write(tmp_path, "---\nspec_stage: acceptance\nstatus: draft\n---\nbody\n")
    assert read_spec_meta(p, LITE.names()) is None


def test_invalid_yaml_frontmatter_stays_unmanaged(tmp_path):
    """The fail-closed guarantee starts only after the YAML parses."""
    from spec_runner.spec import LITE, read_spec_meta

    p = _write(tmp_path, "---\nspec_stage: [unclosed\n---\nbody\n")
    assert read_spec_meta(p, LITE.names()) is None
```

- [ ] **Step 2: Run them**

Run: `uv run pytest tests/test_spec_meta_contract.py -k "unmanaged or managed or invalid_yaml" -v`
Expected: the two `raises` tests fail if the dead `except TypeError` was not fully removed in Task 1; the rest should already pass. Fix `read_spec_meta` if anything still swallows.

- [ ] **Step 3: Confirm the ordering in `read_spec_meta`**

The body must be exactly this shape — stage recognition strictly before construction:

```python
    if not path.exists():
        return None
    meta_dict, _ = split_frontmatter(path.read_text())
    if meta_dict is None:
        return None
    if meta_dict.get("spec_stage") not in stages:
        return None
    return meta_from_dict(meta_dict)
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest tests/ -q -m "not slow"`
Expected: baseline + 30.

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/spec.py tests/test_spec_meta_contract.py
git commit -m "test(spec): pin the managed/unmanaged boundary

Foreign frontmatter stays permissive (unmanaged), but once a document is
recognized by its spec_stage it can no longer degrade to None: a malformed
key or field raises instead, closing a path that silently dropped a managed
spec out of governance. Also pins the documented limit — syntactically
invalid YAML is unmanaged, because spec_stage is unreachable.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `SPEC_META_CONTRACT`, frozen exports, wire-contract guard

**Files:**
- Modify: `src/spec_runner/spec.py`, `src/spec_runner/__init__.py`
- Test: `tests/test_spec_meta_contract.py`

**Interfaces:**
- Produces: `SPEC_META_CONTRACT: int = 2`; the frozen symbol set importable from `spec_runner`.

- [ ] **Step 1: Write the failing tests**

```python
def test_contract_version_is_two():
    from spec_runner import SPEC_META_CONTRACT

    assert SPEC_META_CONTRACT == 2


def test_frozen_surface_is_importable_from_package_root():
    import spec_runner

    for symbol in (
        "SpecMeta",
        "SpecMetaError",
        "SPEC_META_CONTRACT",
        "SPEC_STAGES",
        "split_frontmatter",
        "strip_frontmatter",
        "split_frontmatter_raw",
        "read_spec_meta",
        "read_spec_body",
        "write_spec",
        "meta_from_dict",
        "meta_to_dict",
    ):
        assert hasattr(spec_runner, symbol), symbol
        assert symbol in spec_runner.__all__, symbol


def test_canonical_wire_fields_are_exactly_the_expected_set():
    """Adding an internal dataclass field must not widen the wire contract."""
    assert canonical_fields() == {
        "spec_stage",
        "status",
        "version",
        "generated_by",
        "generated_at",
        "source_prompt_version",
        "validation",
        "approved_by",
        "approved_at",
        "owner_role",
    }
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_spec_meta_contract.py -k "contract_version or frozen_surface or wire_fields" -v`
Expected: FAIL — `SPEC_META_CONTRACT` does not exist and the symbols are not exported.

- [ ] **Step 3: Declare the constant**

Near the top of `src/spec_runner/spec.py`, below the imports:

```python
# Version of the SpecMeta frontmatter contract that consumers pin against
# (steward vendors a pinned copy — DEC-003). v1 was the implicit historical
# contract, inferred from behaviour before it was ever declared here; v2 is
# the first version this repo declares. Adding an optional field is
# non-breaking and does not bump; removing or renaming one bumps.
SPEC_META_CONTRACT: int = 2
```

- [ ] **Step 4: Export the frozen surface**

In `src/spec_runner/__init__.py`, add the import block (keeping alphabetical order within it):

```python
from .spec import (
    SPEC_META_CONTRACT,
    SpecMeta,
    SpecMetaError,
    meta_from_dict,
    meta_to_dict,
    read_spec_body,
    read_spec_meta,
    split_frontmatter,
    split_frontmatter_raw,
    strip_frontmatter,
    write_spec,
)
```

and to `__all__`, as its own labelled group matching the file's existing style:

```python
    # SpecMeta contract v2 (frozen surface — see docs/CONTRACTS.md)
    "SPEC_META_CONTRACT",
    "SpecMeta",
    "SpecMetaError",
    "meta_from_dict",
    "meta_to_dict",
    "read_spec_body",
    "read_spec_meta",
    "split_frontmatter",
    "split_frontmatter_raw",
    "strip_frontmatter",
    "write_spec",
```

`SPEC_STAGES` is already imported from `.prompt` and already in `__all__` — do not duplicate it.

- [ ] **Step 5: Run the tests and check for import cycles**

```bash
uv run pytest tests/test_spec_meta_contract.py -v
uv run python -c "import spec_runner; print(spec_runner.SPEC_META_CONTRACT)"
```
Expected: PASS, and `2` printed. If importing `.spec` from `__init__` creates a cycle, resolve it by ordering the import after `.config` (which `.spec` depends on) rather than by deferring the import.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest tests/ -q -m "not slow"`
Expected: baseline + 33.

- [ ] **Step 7: Commit**

```bash
git add src/spec_runner/spec.py src/spec_runner/__init__.py tests/test_spec_meta_contract.py
git commit -m "feat(spec): declare SPEC_META_CONTRACT v2 and freeze the public surface

The constant never existed upstream — steward invented it in its vendored
copy and pins against it. We declare it at 2, documenting v1 as the
implicit historical contract.

The wire-field test asserts canonical_fields() against an explicit set so
a future internal dataclass field cannot silently widen the contract.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: CLI error boundary

**Files:**
- Modify: `src/spec_runner/cli.py` (`main()`, the dispatch block from ~line 1372)
- Test: `tests/test_spec_meta_contract.py`

**Interfaces:**
- Consumes: `SpecMetaError`.
- Produces: a non-zero exit with a clean one-line diagnostic and no traceback.

- [ ] **Step 1: Write the failing test**

```python
def test_spec_meta_error_exits_cleanly(tmp_path, monkeypatch, capsys):
    """A malformed managed spec must exit non-zero without a traceback."""
    from spec_runner.cli import main

    project = tmp_path / "proj"
    (project / "spec").mkdir(parents=True)
    (project / "spec" / "tasks.md").write_text(
        "---\nspec_stage: tasks\nstatus: draft\nversion: three\n---\n### TASK-001: x\n"
    )
    monkeypatch.chdir(project)
    monkeypatch.setattr(
        "sys.argv", ["spec-runner", "status", "--project-root", str(project)]
    )

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code not in (0, None)
    assert "⛔" in str(exc.value.code)
```

If `status` does not read the frontmatter, use `spec status` instead — check which command reaches `read_spec_meta` on this fixture and target that one. The point of the test is the boundary, not the command.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_spec_meta_contract.py -k exits_cleanly -v`
Expected: FAIL — `SpecMetaError` propagates as an uncaught exception rather than a `SystemExit`.

- [ ] **Step 3: Wrap the dispatch block**

In `src/spec_runner/cli.py`, import `SpecMetaError` alongside the existing `read_spec_meta` import, then wrap the whole dispatch region of `main()` — from the `commands = {` mapping through the final handler call — in:

```python
    try:
        ...  # the existing dispatch block, unchanged, indented one level
    except SpecMetaError as exc:
        raise SystemExit(f"⛔ {exc}") from None
```

Match the existing style at `cli.py:1354` (`raise SystemExit(f"⛔ {exc}") from None`). Do not add per-command handling: this is the process boundary, and `executor.py` only re-exports `cli.main`.

- [ ] **Step 4: Run the test**

Run: `uv run pytest tests/test_spec_meta_contract.py -k exits_cleanly -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite plus lint and types**

```bash
uv run pytest tests/ -q -m "not slow"
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```
Expected: all green. The re-indentation of the dispatch block is the most likely source of a lint or type error — read the diff before committing.

- [ ] **Step 6: Commit**

```bash
git add src/spec_runner/cli.py tests/test_spec_meta_contract.py
git commit -m "feat(cli): exit cleanly on SpecMetaError

One try/except around the whole dispatch block in main(), matching the
existing treatment of an unknown spec profile: a one-line diagnostic, no
traceback, non-zero exit. executor.py only re-exports cli.main, so this is
the process boundary; per-command handling would drift.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Golden fixture, CONTRACTS.md, survey, CHANGELOG, PR

**Files:**
- Create: `src/spec_runner/contract_fixtures/spec_meta_contract_v2.md`, `docs/CONTRACTS.md`
- Modify: `pyproject.toml`, `CHANGELOG.md`
- Test: `tests/test_spec_meta_contract.py`

**Interfaces:**
- Consumes: everything above.
- Produces: a fixture consumers can load via `importlib.resources`, and the written contract.

- [ ] **Step 1: Create the golden fixture**

`src/spec_runner/contract_fixtures/spec_meta_contract_v2.md`:

```markdown
---
traces_to: REQ-001
upstream_hashes:
  requirements: 5f2a9c1
  design: 8b3e7d0
spec_stage: tasks
status: approved
version: 3
generated_by: claude@claude-opus-5
generated_at: '2026-07-26T00:00:00Z'
source_prompt_version: sha256:0000000000000000
validation: pass
approved_by: andrei
approved_at: '2026-07-26T00:00:00Z'
owner_role: '@platform,@sre'
---
# Golden fixture for SpecMeta contract v2

Consumers (steward) parse this file and compare against the documented field
table in docs/CONTRACTS.md. Extras appear first, canonical fields last —
that ordering is part of the render contract.
```

- [ ] **Step 2: Ship it as package data**

In `pyproject.toml`:

```toml
[tool.setuptools.package-data]
spec_runner = ["skills/**/*", "presets/*.yaml", "profiles/*.yaml", "contract_fixtures/*.md"]
```

Create `src/spec_runner/contract_fixtures/__init__.py` (empty) if `importlib.resources` needs the directory to be a package under this setuptools layout; verify with Step 4.

- [ ] **Step 3: Write the round-trip test against the fixture**

```python
def test_golden_fixture_round_trips():
    from importlib.resources import files

    from spec_runner.spec import split_frontmatter

    text = (files("spec_runner.contract_fixtures") / "spec_meta_contract_v2.md").read_text()
    parsed, body = split_frontmatter(text)
    meta = meta_from_dict(parsed)

    assert meta.owner_role == "@platform,@sre"
    assert meta.approved_by == "andrei"
    assert meta.version == 3
    assert meta.extra["traces_to"] == "REQ-001"
    assert meta.extra["upstream_hashes"] == {
        "requirements": "5f2a9c1",
        "design": "8b3e7d0",
    }
    assert meta_to_dict(meta_from_dict(meta_to_dict(meta))) == meta_to_dict(meta)
```

- [ ] **Step 4: Run it**

Run: `uv run pytest tests/test_spec_meta_contract.py -k golden -v`
Expected: PASS. If `files()` cannot find the directory, add the `__init__.py` from Step 2 and re-run.

- [ ] **Step 5: Survey the repo's own specs under the new validation**

The new validation is stricter than anything before it, so confirm nothing in-tree relied on the old tolerance:

```bash
uv run python - <<'PY'
from pathlib import Path
from spec_runner.spec import LITE, SpecMetaError, read_spec_meta

bad = []
for p in Path(".").rglob("*.md"):
    if ".git" in p.parts:
        continue
    try:
        read_spec_meta(p, LITE.names())
    except SpecMetaError as exc:
        bad.append((p, exc))
for p, exc in bad:
    print(f"{p}: {exc}")
print(f"{len(bad)} file(s) rejected")
PY
```

Expected: `0 file(s) rejected`. Any hit is either a real malformed spec to fix or evidence that the matrix is too strict — in the latter case stop and raise it rather than loosening the matrix unilaterally.

- [ ] **Step 6: Write `docs/CONTRACTS.md`**

Contents: the frozen symbol list from Task 6; the field table with the type and value rules from design §3.3 verbatim; the semantics line — `approved_by` is the git-handle of the human who approved, `generated_by` is `<harness>@<model>`; the round-trip guarantee (semantic, not textual — comments, quoting style and key order are not preserved); the note that the stale cascade is sequential, not transactional; the bump policy (optional field added = no bump, removed or renamed = bump; the existence of `extra` never bumps by itself); a contract changelog with v1 as the implicit historical contract and v2 as this release; and a pointer that everything else in `spec.py` is private.

- [ ] **Step 7: CHANGELOG**

Under `## [Unreleased]`, add an `### Added` entry for `owner_role`, `SpecMeta.extra`, `SPEC_META_CONTRACT = 2`, the frozen export surface, `docs/CONTRACTS.md` and the golden fixture; and a `### Changed` entry stating that spec-runner no longer discards foreign frontmatter keys on write (previously every write dropped them), that canonical fields are now validated with `SpecMetaError` where they were previously accepted unchecked, and that a recognized-but-malformed spec now fails loud instead of silently reading as unmanaged. Mark it a **minor** release: additive, no contract surface removed.

- [ ] **Step 8: Full gate, push, PR**

```bash
uv run pytest tests/ -q -m "not slow"
uv run ruff check . && uv run ruff format --check . && uv run mypy src
git add -A
git commit -m "docs(contract): document SpecMeta contract v2 and ship the golden fixture

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
git push -u origin feat/specmeta-contract-v2
gh pr create --title "feat(spec): SpecMeta contract v2 — lossless frontmatter + owner_role"
```

PR body must state: the data-loss finding with its reproduction; that `SPEC_META_CONTRACT` is newly declared rather than bumped; that the bundle's REQ-402 closed as documentation because `approved_by` already carries the human handle and `generated_by` the agent-id; the validation matrix and that it adds validation that never existed, with the survey result from Step 5 as evidence nothing in-tree breaks; and that steward can now re-vendor and drop its workaround.

- [ ] **Step 9: Address review, then hand off to steward**

Read GitHub Copilot's review; fix valid findings on the same branch, answer invalid ones with reasoning. Do not merge — the user merges. After the merge, write the steward handoff to `../prograph-vault/authored/notes/` saying contract v2 shipped and `steward/meta.py`'s raw-dict workaround can go. Do not edit steward.

---

---

### Task 9: VS Code frontmatter schema — open the wire contract

> **Execution order:** run this immediately after Task 4, before Tasks 5-8. It is caused by
> `owner_role` becoming canonical and by extras now surviving into real files, and Task 8's
> CHANGELOG depends on it having landed.

**Files:**
- Modify: `schemas/spec-frontmatter.schema.json`
- Test: `tests/test_vscode_contract.py`

**Interfaces:**
- Consumes: `canonical_fields()` from Task 1, `SpecMeta.owner_role` from Task 4.
- Produces: a schema that validates real, extended frontmatter. No Python API change.

**Why.** `schemas/spec-frontmatter.schema.json` is the contract the sibling `spec-runner-vscode`
extension pins against, and `tests/test_vscode_contract.py` validates **live** frontmatter
against it (`test_live_frontmatter_matches_schema`), not a curated subset. Two conflicts:

1. It sets `additionalProperties: false` — deliberately, as a drift alarm. But the entire
   point of this PR is that foreign keys (steward's `traces_to`, `upstream_hashes`) now
   survive into the file. Any real governed spec would fail validation. Two contracts in one
   repo demanding opposite things.
2. `spec_stage` carries `enum: ["requirements", "design", "tasks"]`, so a spec on a
   custom-profile stage fails the contract. That is a latent bug dating from stage profiles
   in v2.9.0 — unrelated to this PR, fixed here because it is the same schema.

**Ruling (owner, 2026-07-26):** open the wire contract. Membership of a stage in the active
profile is a runtime, config-aware check that JSON Schema cannot express without the profile.
The drift protection does not disappear — it moves to the exact-canonical-field test from
Task 6, which is stricter and more precise than the schema ever was.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_vscode_contract.py`, reusing the file's existing `_validator` helper and
`SpecMeta`/`write_spec`/`split_frontmatter` imports:

```python
def test_schema_lists_every_canonical_field() -> None:
    """The schema's properties must cover every field spec-runner owns.

    This is the schema half of the drift guard: canonical_fields() is the SSOT,
    and a new SpecMeta field must be described here too.
    """
    import json

    from spec_runner.spec import canonical_fields

    schema = json.loads((SCHEMAS_DIR / "spec-frontmatter.schema.json").read_text())
    missing = canonical_fields() - set(schema["properties"])
    assert not missing, f"schema does not describe canonical field(s): {sorted(missing)}"


def test_schema_accepts_foreign_extension_keys() -> None:
    """Extending layers (steward) add their own keys; they must validate."""
    sample = {
        "spec_stage": "tasks",
        "status": "approved",
        "version": 2,
        "traces_to": "REQ-001",
        "upstream_hashes": {"design": "deadbeef"},
        "owner_role": "@platform,@sre",
    }
    _validator("spec-frontmatter.schema.json").validate(sample)


def test_schema_accepts_a_custom_profile_stage() -> None:
    """Stage membership belongs to the active profile, not to JSON Schema."""
    sample = {"spec_stage": "acceptance", "status": "draft", "version": 1}
    _validator("spec-frontmatter.schema.json").validate(sample)


def test_schema_rejects_empty_spec_stage() -> None:
    import pytest
    from jsonschema.exceptions import ValidationError

    with pytest.raises(ValidationError):
        _validator("spec-frontmatter.schema.json").validate(
            {"spec_stage": "", "status": "draft", "version": 1}
        )


def test_schema_still_type_checks_canonical_properties() -> None:
    """Opening the object must not weaken the fields the schema does describe."""
    import pytest
    from jsonschema.exceptions import ValidationError

    validator = _validator("spec-frontmatter.schema.json")
    for bad in (
        {"spec_stage": "tasks", "status": "approvd", "version": 1},
        {"spec_stage": "tasks", "status": "draft", "version": "two"},
        {"spec_stage": "tasks", "status": "draft", "version": 1, "owner_role": 7},
        {"spec_stage": "tasks", "status": "draft", "version": 1, "validation": "weird"},
    ):
        with pytest.raises(ValidationError):
            validator.validate(bad)


def test_live_frontmatter_with_extras_matches_schema(tmp_path: Path) -> None:
    """End-to-end: a written file carrying extras and owner_role validates."""
    from spec_runner.spec import meta_from_dict

    meta = meta_from_dict(
        {
            "spec_stage": "tasks",
            "status": "approved",
            "version": 2,
            "owner_role": "@platform",
            "traces_to": "REQ-001",
        }
    )
    path = tmp_path / "tasks.md"
    write_spec(path, meta, "# body\n")
    fm, _ = split_frontmatter(path.read_text())
    assert fm is not None
    _validator("spec-frontmatter.schema.json").validate(fm)
```

Check the file's real constant for the schemas directory (it may not be called `SCHEMAS_DIR`)
and the real `_validator` signature before writing; reuse them rather than inventing new ones.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_vscode_contract.py -v`
Expected: the canonical-field, foreign-key, custom-stage and live-extras tests fail —
`owner_role` is not in `properties`, `additionalProperties: false` rejects foreign keys, and
the `spec_stage` enum rejects `acceptance`.

- [ ] **Step 3: Open the schema**

In `schemas/spec-frontmatter.schema.json`:

- set `"additionalProperties": true`;
- replace `spec_stage`'s `enum` with `"minLength": 1` (keep `"type": "string"`);
- add an `owner_role` property of type `["string", "null"]`;
- rewrite `description` so it no longer claims `additionalProperties` is the drift alarm, and
  add a `$comment` recording that `properties` describes the canonical fields spec-runner
  owns, that additional keys are deliberately permitted and preserved losslessly under
  `SPEC_META_CONTRACT` v2, and that stage membership is validated at runtime against the
  configured profile.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_vscode_contract.py -v`
Expected: PASS, including every pre-existing test in the file unchanged.

- [ ] **Step 5: Full suite**

Run: `uv run pytest tests/ -q -m "not slow"`, plus `uv run ruff check .`,
`uv run ruff format --check .`, `uv run mypy src`. All clean.

- [ ] **Step 6: Commit**

```bash
git add schemas/spec-frontmatter.schema.json tests/test_vscode_contract.py
git commit -m "fix(schema): open the spec-frontmatter wire contract

The VS Code frontmatter schema set additionalProperties: false as a drift
alarm, which contradicts the point of contract v2: foreign keys written by
extending layers now survive into the file, so any governed spec carrying
them failed validation. Opened the object, added owner_role, and moved the
drift guard to the exact-canonical-field test, which is stricter.

Separately fixes a latent bug from stage profiles (v2.9.0): spec_stage
carried an enum of the three lite stage names, so a spec on a custom-profile
stage failed the contract. Stage membership is a runtime check against the
configured profile and cannot be expressed in JSON Schema without it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

## Self-Review

**Spec coverage.** Design §3.1 → Tasks 2 and 4; §3.2 round-trip and omit-when-None → Tasks 3 and 4; §3.3 parse order and validation matrix → Tasks 1 and 5; §3.4 → PR 1, deliberately absent here and named as a dependency; §3.5 → Task 6; §3.6 `approver` closed by documentation → Task 8 Step 6; §3.7 frozen surface → Task 6, `docs/CONTRACTS.md` → Task 8; §4 CLI boundary → Task 7; §5 testing → distributed across all tasks, with the golden fixture in Task 8 and the in-tree survey added as Step 5 because the design flags that risk without assigning it a step. §6 steward handoff → Task 8 Step 9. §7 out-of-scope items appear nowhere, as intended.

**Placeholders.** None. Four steps direct the implementer to reuse an existing test helper or pick the right command rather than hardcoding a name this plan cannot verify (Task 3 Step 5, Task 7 Step 1, Task 8 Steps 2 and 4); each names exactly what to look for and what to substitute.

**Type consistency.** `canonical_fields() -> frozenset[str]` is used as a set in `meta_from_dict` and `meta_to_dict`. `_canonical_order() -> tuple[str, ...]` derives from `fields(SpecMeta)`, so adding `owner_role` before `extra` in Task 4 automatically places it last in the wire order without touching the renderer. `_coerce_canonical(key: str, value: object) -> object` raises or returns the value, normalized for the two timestamp fields. `SpecMeta.extra: dict[str, Any]` matches `meta.extra[...]` in every test. `meta_from_dict(d: dict) -> SpecMeta` and `meta_to_dict(m: SpecMeta) -> dict` keep their published signatures, so the frozen surface in Task 6 is honest. `read_spec_meta(path, stages) -> SpecMeta | None` is unchanged. `SPEC_META_CONTRACT: int` matches steward's vendored `SPEC_META_CONTRACT: int = 1`.
