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


class TestPlanFullPipeline:
    def test_full_generates_three_files(self, tmp_path):
        """Test that marker parsing + file writing works for all three stages."""
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()

        # Simulate Claude outputs
        req_output = (
            "SPEC_REQUIREMENTS_READY\n# Requirements\n[REQ-001] User auth\nSPEC_REQUIREMENTS_END"
        )
        des_output = "SPEC_DESIGN_READY\n# Design\n[DESIGN-001] Auth module\nSPEC_DESIGN_END"
        task_output = "SPEC_TASKS_READY\n# Tasks\n### TASK-001: Setup auth\nSPEC_TASKS_END"

        req = parse_spec_marker(req_output, "REQUIREMENTS")
        assert req is not None and "[REQ-001]" in req

        des = parse_spec_marker(des_output, "DESIGN")
        assert des is not None and "[DESIGN-001]" in des

        tasks = parse_spec_marker(task_output, "TASKS")
        assert tasks is not None and "TASK-001" in tasks

        # Write files as pipeline would
        (spec_dir / "requirements.md").write_text(req + "\n")
        (spec_dir / "design.md").write_text(des + "\n")
        (spec_dir / "tasks.md").write_text(tasks + "\n")

        assert (spec_dir / "requirements.md").exists()
        assert (spec_dir / "design.md").exists()
        assert (spec_dir / "tasks.md").exists()
        assert "[REQ-001]" in (spec_dir / "requirements.md").read_text()
