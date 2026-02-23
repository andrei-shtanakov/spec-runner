"""Tests for spec-runner plan --full (spec generation)."""

from spec_runner.prompt import build_generation_prompt, parse_spec_marker


class TestBuildGenerationPrompt:
    def test_requirements_stage(self):
        prompt = build_generation_prompt(
            stage="requirements",
            description="Build a REST API for user management",
            context={},
        )
        assert "requirements" in prompt.lower()
        assert "REST API" in prompt
        assert "SPEC_REQUIREMENTS_READY" in prompt

    def test_design_stage_includes_requirements(self):
        prompt = build_generation_prompt(
            stage="design",
            description="Build a REST API",
            context={"requirements": "# Requirements\n[REQ-001] User auth"},
        )
        assert "design" in prompt.lower()
        assert "REQ-001" in prompt
        assert "SPEC_DESIGN_READY" in prompt

    def test_tasks_stage_includes_requirements_and_design(self):
        prompt = build_generation_prompt(
            stage="tasks",
            description="Build a REST API",
            context={
                "requirements": "# Requirements\n[REQ-001] Auth",
                "design": "# Design\n[DESIGN-001] REST layer",
            },
        )
        assert "tasks" in prompt.lower()
        assert "REQ-001" in prompt
        assert "DESIGN-001" in prompt
        assert "SPEC_TASKS_READY" in prompt


class TestParseSpecMarkers:
    def test_extract_requirements(self):
        output = (
            "Some preamble\n"
            "SPEC_REQUIREMENTS_READY\n"
            "# Requirements\n[REQ-001] Auth\n"
            "SPEC_REQUIREMENTS_END\n"
            "Trailing"
        )
        content = parse_spec_marker(output, "REQUIREMENTS")
        assert content is not None
        assert "[REQ-001]" in content

    def test_no_marker_returns_none(self):
        content = parse_spec_marker("No markers here", "REQUIREMENTS")
        assert content is None
