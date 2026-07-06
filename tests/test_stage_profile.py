"""Tests for the stage-profile model (StageDef/StageProfile) + bundled 'lite' profile."""

import pytest

from spec_runner import prompt, spec
from spec_runner.spec import LITE, StageDef, StageProfile, load_profile


def test_lite_names_matches_canonical_chain():
    assert LITE.names() == ("requirements", "design", "tasks")


def test_lite_reproduces_current_chain_1to1():
    by_name = {s.name: s for s in LITE.stages}

    assert by_name["requirements"].marker_prefix == "SPEC_REQUIREMENTS"
    assert by_name["design"].marker_prefix == "SPEC_DESIGN"
    assert by_name["tasks"].marker_prefix == "SPEC_TASKS"

    assert by_name["requirements"].template == "requirements.template.md"
    assert by_name["design"].template == "design.template.md"
    assert by_name["tasks"].template == "tasks.template.md"

    assert by_name["requirements"].validator_key == "requirements"
    assert by_name["design"].validator_key == "design"
    assert by_name["tasks"].validator_key == "tasks"

    assert by_name["requirements"].upstream == ()
    assert by_name["design"].upstream == ("requirements",)
    assert by_name["tasks"].upstream == ("design",)


def test_load_profile_returns_ordered_stage_profile():
    prof = load_profile("lite")
    assert isinstance(prof, StageProfile)
    assert prof.name == "lite"
    assert [s.name for s in prof.stages] == ["requirements", "design", "tasks"]
    assert all(isinstance(s, StageDef) for s in prof.stages)


def test_load_profile_unknown_name_raises():
    with pytest.raises(ValueError):
        load_profile("does-not-exist")


def test_stages_export_derived_from_lite():
    # spec.STAGES stays a backward-compatible export, now derived from lite.
    assert LITE.names() == spec.STAGES


def test_spec_stages_export_derived_from_lite():
    # SPEC_STAGES keys/order and markers come from the lite profile;
    # instruction text is preserved (non-empty) for each stage.
    assert list(prompt.SPEC_STAGES.keys()) == list(LITE.names())
    for s in LITE.stages:
        assert prompt.SPEC_STAGES[s.name]["marker"] == s.marker_prefix
        assert prompt.SPEC_STAGES[s.name]["instruction"]
