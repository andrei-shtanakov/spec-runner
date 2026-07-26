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
