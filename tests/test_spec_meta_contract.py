"""Contract tests for SpecMeta v2: validation matrix, extras, round-trip."""

import pytest

from spec_runner.spec import (
    LITE,
    SpecMeta,
    SpecMetaError,
    _render,
    canonical_fields,
    meta_from_dict,
    meta_to_dict,
    read_spec_meta,
    split_frontmatter,
)


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

    stamp = datetime.datetime(2026, 7, 5, 13, 45, 1, tzinfo=datetime.UTC)
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


def test_apply_approval_preserves_extras(tmp_path, monkeypatch):
    """The real approve path must not erase steward's governance keys."""
    from spec_runner.spec import LITE, apply_approval, read_spec_meta, stage_path, write_spec
    from tests.test_spec_commands import _cfg

    config = _cfg(tmp_path)
    path = stage_path(config, "tasks")
    meta = meta_from_dict(_base(status="draft", traces_to="REQ-001"))
    write_spec(path, meta, "# body\n")

    apply_approval(
        config, "tasks", approver="andrei", now="2026-07-26T00:00:00Z", fresh_validation="pass"
    )

    after = read_spec_meta(path, LITE.names())
    assert after is not None
    assert after.status == "approved"
    assert after.extra["traces_to"] == "REQ-001"


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


def _write(tmp_path, text):
    """Write text to tasks.md and return the path."""
    p = tmp_path / "tasks.md"
    p.write_text(text)
    return p


def test_unmanaged_document_with_non_string_key_is_none(tmp_path):
    """Foreign frontmatter stays permissive: unmanaged, not an error."""
    p = _write(tmp_path, "---\ntitle: notes\n1: foo\n---\nbody\n")
    assert read_spec_meta(p, LITE.names()) is None


def test_managed_document_with_non_string_key_raises(tmp_path):
    """Managed document with non-string key must raise, not silently degrade."""
    p = _write(tmp_path, "---\nspec_stage: tasks\nstatus: draft\n1: foo\n---\nbody\n")
    with pytest.raises(SpecMetaError):
        read_spec_meta(p, LITE.names())


def test_managed_document_with_malformed_known_field_raises(tmp_path):
    """Must not silently degrade to unmanaged, which would bypass the gate."""
    p = _write(tmp_path, "---\nspec_stage: tasks\nstatus: draft\nversion: three\n---\nbody\n")
    with pytest.raises(SpecMetaError):
        read_spec_meta(p, LITE.names())


def test_unknown_spec_stage_stays_unmanaged(tmp_path):
    """A spec_stage not in the recognized set keeps the doc unmanaged."""
    p = _write(tmp_path, "---\nspec_stage: acceptance\nstatus: draft\n---\nbody\n")
    assert read_spec_meta(p, LITE.names()) is None


def test_invalid_yaml_frontmatter_stays_unmanaged(tmp_path):
    """The fail-closed guarantee starts only after the YAML parses."""
    p = _write(tmp_path, "---\nspec_stage: [unclosed\n---\nbody\n")
    assert read_spec_meta(p, LITE.names()) is None


# --- SPEC_META_CONTRACT v2: frozen surface + wire-contract guard ---
#
# SPEC_META_CONTRACT never existed upstream before this: steward invented it
# in its vendored copy (pinned at 1), inferring the contract from observed
# behaviour. This declares it here for the first time, at 2, and freezes the
# set of symbols steward may import from spec_runner.


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
