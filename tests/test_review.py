"""Tests for review.py — focusing on _resolve_review_template helper."""

from __future__ import annotations

from spec_runner.config import ExecutorConfig


class TestResolveReviewTemplate:
    """Tests for the _resolve_review_template helper function."""

    def test_mixed_cli_does_not_bleed_exec_template(self) -> None:
        """When review CLI differs from exec CLI, exec template must NOT be used."""
        from spec_runner.review import _resolve_review_template

        config = ExecutorConfig(
            claude_command="copilot",
            command_template="{cmd} -p {prompt} --allow-all-tools",
            review_command="claude",
            review_command_template="",
        )
        review_cmd = config.review_command or config.claude_command  # "claude"
        result = _resolve_review_template(config, review_cmd)
        assert result == "", (
            "Expected '' when review CLI differs from exec CLI and no explicit "
            f"review_command_template is set, got {result!r}"
        )

    def test_same_cli_inherits_exec_template(self) -> None:
        """When review CLI is the same binary as exec CLI, inherit exec template."""
        from spec_runner.review import _resolve_review_template

        config = ExecutorConfig(
            claude_command="pi",
            command_template="{cmd} -p {prompt} --tools x",
            review_command="",
            review_command_template="",
        )
        review_cmd = config.review_command or config.claude_command  # "pi"
        result = _resolve_review_template(config, review_cmd)
        assert result == "{cmd} -p {prompt} --tools x", (
            "Expected exec command_template to be inherited when review and exec "
            f"use the same CLI, got {result!r}"
        )

    def test_explicit_review_template_always_wins(self) -> None:
        """Explicit review_command_template takes priority over everything."""
        from spec_runner.review import _resolve_review_template

        config = ExecutorConfig(
            claude_command="copilot",
            command_template="{cmd} -p {prompt} --allow-all-tools",
            review_command="claude",
            review_command_template="{cmd} review",
        )
        review_cmd = config.review_command or config.claude_command  # "claude"
        result = _resolve_review_template(config, review_cmd)
        assert result == "{cmd} review", (
            f"Expected explicit review_command_template to win, got {result!r}"
        )

    def test_explicit_review_template_wins_even_for_same_cli(self) -> None:
        """Explicit review_command_template takes priority even when CLIs match."""
        from spec_runner.review import _resolve_review_template

        config = ExecutorConfig(
            claude_command="claude",
            command_template="{cmd} -p {prompt}",
            review_command="claude",
            review_command_template="{cmd} --special-review-flag",
        )
        review_cmd = config.review_command or config.claude_command  # "claude"
        result = _resolve_review_template(config, review_cmd)
        assert result == "{cmd} --special-review-flag", (
            f"Expected explicit review_command_template to win, got {result!r}"
        )

    def test_both_empty_returns_empty(self) -> None:
        """When no templates set and CLIs differ, returns empty string."""
        from spec_runner.review import _resolve_review_template

        config = ExecutorConfig(
            claude_command="qwen",
            command_template="",
            review_command="claude",
            review_command_template="",
        )
        review_cmd = config.review_command or config.claude_command  # "claude"
        result = _resolve_review_template(config, review_cmd)
        assert result == ""

    def test_default_claude_inherits_exec_template(self) -> None:
        """Default claude_command='claude', no review_command → same CLI, inherit."""
        from spec_runner.review import _resolve_review_template

        config = ExecutorConfig(
            command_template="{cmd} --output-format json",
            review_command="",
            review_command_template="",
        )
        # claude_command defaults to "claude"; review_command="" → resolves to "claude"
        review_cmd = config.review_command or config.claude_command
        result = _resolve_review_template(config, review_cmd)
        assert result == "{cmd} --output-format json"
