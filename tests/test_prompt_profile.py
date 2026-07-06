"""Golden + source-of-truth tests: prompt.py builds from StageDef (TASK-303).

Locks the byte-for-byte generated prompt for the ``lite`` profile so that
migrating template/marker_prefix/prompt_text into the ``StageDef`` (DESIGN-305)
cannot silently change generated output, and asserts the maps are read from the
profile's ``StageDef`` rather than module-level dicts.
"""

import pytest

import spec_runner.prompt as prompt_mod
from spec_runner.prompt import (
    _stage_def,
    build_gated_generation_prompt,
    build_generation_prompt,
    load_bundled_template,
    template_hash,
)
from spec_runner.spec import LITE, StageDef, StageProfile

# A throwaway non-lite profile whose single stage deliberately borrows the
# ``design`` template (so ``load_bundled_template``/``template_hash`` resolve to
# a bundled file that differs from lite's ``requirements`` template) and carries
# distinct marker/instruction text. Used to prove the ``profile`` argument
# actually flows through each parameterized function rather than defaulting to
# LITE.
_ALT = StageProfile(
    name="alt",
    stages=(
        StageDef(
            name="requirements",
            template="design.template.md",
            marker_prefix="ALT_REQ",
            validator_key="requirements",
            prompt_text="ALT INSTRUCTION",
        ),
    ),
)

# Frozen expected output for the built-in ``lite`` profile, keyed by
# (stage, upstream context) exactly as the gated pipeline feeds each stage.
# Regenerate only on a deliberate, reviewed change to the lite prompt
# text/markers/template.
_GOLDEN_GEN = {
    "requirements": (
        {},
        "Generate a requirements document based on the project description "
        "below. Use [REQ-001], [REQ-002], etc. for each requirement. When done, "
        "output the requirements between markers:\n"
        "SPEC_REQUIREMENTS_READY\n<your requirements>\nSPEC_REQUIREMENTS_END\n"
        "\nProject description: DESC",
    ),
    "design": (
        {"requirements": "RRR"},
        "Generate a design document based on the requirements below. Use "
        "[DESIGN-001], [DESIGN-002], etc. and trace back to requirements with "
        "[REQ-XXX]. When done, output the design between markers:\n"
        "SPEC_DESIGN_READY\n<your design>\nSPEC_DESIGN_END\n"
        "\nProject description: DESC\n"
        "\n## Requirements (already generated)\nRRR",
    ),
    "tasks": (
        {"requirements": "RRR", "design": "DDD"},
        "Generate a tasks document based on the requirements and design below. "
        "Use TASK-001, TASK-002, etc. with priorities (P0-P3), estimates, "
        "checklists, dependencies, and traceability refs to [REQ-XXX] and "
        "[DESIGN-XXX]. When done, output the tasks between markers:\n"
        "SPEC_TASKS_READY\n<your tasks>\nSPEC_TASKS_END\n"
        "\nProject description: DESC\n"
        "\n## Requirements (already generated)\nRRR\n"
        "\n## Design (already generated)\nDDD",
    ),
}


class TestGoldenGenerationPrompt:
    def test_generation_prompt_byte_for_byte_lite(self):
        for stage, (ctx, expected) in _GOLDEN_GEN.items():
            assert build_generation_prompt(stage, "DESC", ctx) == expected

    def test_gated_prompt_markers_come_from_stage_def(self):
        # Markers are generated from StageDef.marker_prefix, not hardcoded.
        for s in LITE.stages:
            p = build_gated_generation_prompt(s.name, "DESC", {})
            assert f"{s.marker_prefix}_READY" in p
            assert f"{s.marker_prefix}_END" in p


class TestProfileParameterFlowsThrough:
    """The ``profile`` argument must actually reach each parameterized function,
    not silently default to LITE (the point of TASK-303/DESIGN-305)."""

    def test_generation_prompt_uses_profile_instruction(self):
        p = build_generation_prompt("requirements", "D", {}, profile=_ALT)
        assert p.startswith("ALT INSTRUCTION")

    def test_gated_prompt_uses_profile_marker_and_template(self):
        p = build_gated_generation_prompt("requirements", "D", {}, profile=_ALT)
        assert "ALT_REQ_READY" in p
        assert "ALT_REQ_END" in p
        # The alt stage borrows the design template, so its body must appear.
        assert load_bundled_template("requirements", profile=_ALT) in p

    def test_load_bundled_template_uses_profile_filename(self):
        # alt's 'requirements' stage points at the design template file.
        assert load_bundled_template("requirements", profile=_ALT) == load_bundled_template(
            "design"
        )

    def test_template_hash_uses_profile_filename(self):
        # Same file → same hash; differs from lite's requirements template.
        assert template_hash("requirements", profile=_ALT) == template_hash("design")
        assert template_hash("requirements", profile=_ALT) != template_hash("requirements")

    def test_stage_def_uses_profile(self):
        assert _stage_def("requirements", _ALT) is _ALT.stages[0]


class TestReadsFromStageDef:
    def test_stage_def_returns_lite_stage(self):
        for s in LITE.stages:
            assert _stage_def(s.name) is s

    def test_stage_def_unknown_stage_raises_keyerror(self):
        # Documented error contract: unknown stage → KeyError (both profiles).
        with pytest.raises(KeyError):
            _stage_def("nonexistent")
        with pytest.raises(KeyError):
            _stage_def("design", _ALT)  # absent from the single-stage alt profile

    def test_no_module_level_template_or_instruction_maps(self):
        # DESIGN-305: the scattered maps are gone; maps come from StageDef.
        assert not hasattr(prompt_mod, "_TEMPLATE_FILES")
        assert not hasattr(prompt_mod, "_LITE_INSTRUCTIONS")

    def test_lite_prompt_text_and_template_populated(self):
        for s in LITE.stages:
            assert s.prompt_text
            assert s.template.endswith(".template.md")
