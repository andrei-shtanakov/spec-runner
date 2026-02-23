"""Tests for spec_runner.prompt module."""

from pathlib import Path

import spec_runner.prompt as prompt_mod
from spec_runner.config import ExecutorConfig
from spec_runner.prompt import (
    build_task_prompt,
    extract_test_failures,
    format_error_summary,
    load_prompt_template,
    parse_spec_marker,
    render_template,
)
from spec_runner.state import ErrorCode, RetryContext, TaskAttempt
from spec_runner.task import Task

# === render_template ===


class TestRenderTemplate:
    def test_double_brace_substitution(self):
        result = render_template("Hello {{NAME}}", {"NAME": "world"})
        assert result == "Hello world"

    def test_dollar_brace_substitution(self):
        result = render_template("Hello ${NAME}", {"NAME": "world"})
        assert result == "Hello world"

    def test_both_syntaxes_in_same_template(self):
        tpl = "{{GREETING}} ${NAME}!"
        result = render_template(tpl, {"GREETING": "Hi", "NAME": "Alice"})
        assert result == "Hi Alice!"

    def test_missing_variable_left_as_is(self):
        result = render_template("Hello {{MISSING}}", {})
        assert result == "Hello {{MISSING}}"

    def test_empty_value_substitution(self):
        result = render_template("Hello {{NAME}}!", {"NAME": ""})
        assert result == "Hello !"


# === format_error_summary ===


class TestFormatErrorSummary:
    def test_error_only(self):
        result = format_error_summary("timeout")
        assert "timeout" in result

    def test_includes_error_text(self):
        result = format_error_summary("ValueError")
        assert "ValueError" in result

    def test_truncates_long_output(self):
        lines = [f"error line {i}" for i in range(50)]
        output = "\n".join(lines)
        result = format_error_summary("crash", output=output, max_lines=3)
        # Should show at most 3 key-issue lines
        bullet_lines = [ln for ln in result.split("\n") if ln.strip().startswith("•")]
        assert len(bullet_lines) <= 3

    def test_shows_last_output_when_no_keywords(self):
        output = "line1\nline2\nline3\nline4\nline5\nline6"
        result = format_error_summary("unknown", output=output)
        assert "Last output" in result


# === extract_test_failures ===


class TestExtractTestFailures:
    def test_extracts_failed_lines(self):
        output = "PASSED test_a\nFAILED test_b\nPASSED test_c\n"
        result = extract_test_failures(output)
        assert "FAILED test_b" in result

    def test_returns_tail_for_no_failures(self):
        output = "all good\nnothing wrong\n"
        result = extract_test_failures(output)
        # With no FAILED/ERROR/assert lines, falls back to output[-500:]
        assert "all good" in result or "nothing wrong" in result

    def test_captures_short_summary_section(self):
        output = "test_a PASSED\n= short test summary info =\nFAILED test_b - assert 1 == 2\n"
        result = extract_test_failures(output)
        assert "FAILED test_b" in result

    def test_limits_to_max_failures(self):
        lines = [f"FAILED test_{i}" for i in range(10)]
        output = "\n".join(lines)
        result = extract_test_failures(output)
        assert "showing first 5" in result


# === load_prompt_template ===


class TestLoadPromptTemplate:
    def test_returns_none_for_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", tmp_path)
        result = load_prompt_template("nonexistent")
        assert result is None

    def test_loads_md_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", tmp_path)
        (tmp_path / "task.md").write_text("# Task Template\n")
        result = load_prompt_template("task")
        assert result == "# Task Template"

    def test_cli_specific_template_has_priority(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", tmp_path)
        (tmp_path / "review.md").write_text("generic review")
        (tmp_path / "review.codex.md").write_text("codex review")
        result = load_prompt_template("review", cli_name="codex")
        assert result == "codex review"

    def test_falls_back_to_generic_when_no_cli_template(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", tmp_path)
        (tmp_path / "review.md").write_text("generic review")
        result = load_prompt_template("review", cli_name="codex")
        assert result == "generic review"

    def test_strips_comments_from_txt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", tmp_path)
        (tmp_path / "task.txt").write_text("# comment\nactual content\n")
        result = load_prompt_template("task")
        assert result == "actual content"
        assert "# comment" not in result

    def test_cli_name_path_extraction(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", tmp_path)
        (tmp_path / "task.claude.md").write_text("claude template")
        result = load_prompt_template("task", cli_name="/usr/bin/claude")
        assert result == "claude template"


# === build_task_prompt ===


class TestBuildTaskPrompt:
    def _make_task(self, **overrides) -> Task:
        defaults = {
            "id": "TASK-042",
            "name": "Implement feature X",
            "priority": "p1",
            "status": "todo",
            "estimate": "2d",
            "milestone": "mvp",
            "checklist": [("Write tests", False), ("Implement code", True)],
            "traces_to": [],
            "depends_on": [],
        }
        defaults.update(overrides)
        return Task(**defaults)

    def _make_config(self, tmp_path: Path) -> ExecutorConfig:
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir(exist_ok=True)
        return ExecutorConfig(project_root=tmp_path)

    def test_includes_task_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", tmp_path / "no-prompts")
        task = self._make_task()
        config = self._make_config(tmp_path)
        result = build_task_prompt(task, config)
        assert "TASK-042" in result

    def test_includes_task_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", tmp_path / "no-prompts")
        task = self._make_task()
        config = self._make_config(tmp_path)
        result = build_task_prompt(task, config)
        assert "Implement feature X" in result

    def test_includes_checklist_items(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", tmp_path / "no-prompts")
        task = self._make_task()
        config = self._make_config(tmp_path)
        result = build_task_prompt(task, config)
        assert "Write tests" in result
        assert "Implement code" in result

    def test_uses_custom_template_when_available(self, tmp_path, monkeypatch):
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()
        (prompts_dir / "task.md").write_text("Custom: {{TASK_ID}} - {{TASK_NAME}} (${PRIORITY})")
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", prompts_dir)

        task = self._make_task()
        config = self._make_config(tmp_path)
        result = build_task_prompt(task, config)
        assert result == "Custom: TASK-042 - Implement feature X (P1)"

    def test_includes_previous_attempt_errors(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", tmp_path / "no-prompts")
        task = self._make_task()
        config = self._make_config(tmp_path)
        attempts = [
            TaskAttempt(
                timestamp="2025-01-01T00:00:00",
                success=False,
                duration_seconds=10.0,
                error="AssertionError in test_foo",
            ),
        ]
        result = build_task_prompt(task, config, previous_attempts=attempts)
        assert "PREVIOUS ATTEMPTS FAILED" in result
        assert "AssertionError in test_foo" in result

    def test_extracts_related_requirements(self, tmp_path, monkeypatch):
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", tmp_path / "no-prompts")
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir(exist_ok=True)
        (spec_dir / "requirements.md").write_text(
            "#### REQ-001: Must handle errors\nDetails here\n"
        )
        task = self._make_task(traces_to=["REQ-001"])
        config = self._make_config(tmp_path)
        result = build_task_prompt(task, config)
        assert "Must handle errors" in result


# === RetryContext rendering ===


class TestRetryContextRendering:
    """Tests for structured RetryContext rendering in build_task_prompt."""

    def _make_task(self, **overrides) -> Task:
        defaults = {
            "id": "TASK-042",
            "name": "Implement feature X",
            "priority": "p1",
            "status": "todo",
            "estimate": "2d",
            "milestone": "mvp",
            "checklist": [("Write tests", False), ("Implement code", True)],
            "traces_to": [],
            "depends_on": [],
        }
        defaults.update(overrides)
        return Task(**defaults)

    def _make_config(self, tmp_path: Path) -> ExecutorConfig:
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir(exist_ok=True)
        return ExecutorConfig(project_root=tmp_path)

    def test_retry_context_in_prompt(self, tmp_path, monkeypatch):
        """build_task_prompt with RetryContext shows structured error info."""
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", tmp_path / "no-prompts")
        config = self._make_config(tmp_path)
        task = self._make_task()
        ctx = RetryContext(
            attempt_number=2,
            max_attempts=3,
            previous_error_code=ErrorCode.TEST_FAILURE,
            previous_error="Tests failed",
            what_was_tried="Implemented login page",
            test_failures="FAILED test_login - AssertionError",
        )
        prompt = build_task_prompt(task, config, retry_context=ctx)
        assert "Attempt 2 of 3" in prompt
        assert "TEST_FAILURE" in prompt
        assert "FAILED test_login" in prompt

    def test_no_retry_context_no_retry_section(self, tmp_path, monkeypatch):
        """Without RetryContext, no structured retry section."""
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", tmp_path / "no-prompts")
        config = self._make_config(tmp_path)
        task = self._make_task()
        prompt = build_task_prompt(task, config)
        assert "RETRY" not in prompt

    def test_retry_context_timeout(self, tmp_path, monkeypatch):
        """TIMEOUT error code renders correctly."""
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", tmp_path / "no-prompts")
        config = self._make_config(tmp_path)
        task = self._make_task()
        ctx = RetryContext(
            attempt_number=1,
            max_attempts=3,
            previous_error_code=ErrorCode.TIMEOUT,
            previous_error="Timeout after 30 minutes",
            what_was_tried="Implementing feature",
            test_failures=None,
        )
        prompt = build_task_prompt(task, config, retry_context=ctx)
        assert "TIMEOUT" in prompt
        assert "Timeout after 30 minutes" in prompt
        # No test failures section
        assert "Test failures" not in prompt

    def test_retry_context_without_test_failures(self, tmp_path, monkeypatch):
        """RetryContext without test_failures doesn't show that section."""
        monkeypatch.setattr(prompt_mod, "PROMPTS_DIR", tmp_path / "no-prompts")
        config = self._make_config(tmp_path)
        task = self._make_task()
        ctx = RetryContext(
            attempt_number=2,
            max_attempts=3,
            previous_error_code=ErrorCode.TASK_FAILED,
            previous_error="Could not compile",
            what_was_tried="Implementing feature",
            test_failures=None,
        )
        prompt = build_task_prompt(task, config, retry_context=ctx)
        assert "TASK_FAILED" in prompt
        assert "Could not compile" in prompt


# === parse_spec_marker ===


class TestParseSpecMarker:
    def test_extracts_content_between_markers(self):
        output = "prefix SPEC_REQUIREMENTS_READY\nHello World\nSPEC_REQUIREMENTS_END suffix"
        result = parse_spec_marker(output, "REQUIREMENTS")
        assert result == "Hello World"

    def test_returns_none_when_start_missing(self):
        output = "no markers here"
        result = parse_spec_marker(output, "REQUIREMENTS")
        assert result is None

    def test_returns_none_when_end_missing(self):
        """When end marker is missing, return None instead of unbounded content."""
        output = "SPEC_DESIGN_READY\nSome very long content that goes on and on"
        result = parse_spec_marker(output, "DESIGN")
        assert result is None

    def test_strips_whitespace(self):
        output = "SPEC_TASKS_READY\n  content with spaces  \nSPEC_TASKS_END"
        result = parse_spec_marker(output, "TASKS")
        assert result == "content with spaces"

    def test_multiple_markers_takes_first(self):
        output = (
            "SPEC_REQUIREMENTS_READY\nFirst\nSPEC_REQUIREMENTS_END\n"
            "SPEC_REQUIREMENTS_READY\nSecond\nSPEC_REQUIREMENTS_END"
        )
        result = parse_spec_marker(output, "REQUIREMENTS")
        assert result == "First"
