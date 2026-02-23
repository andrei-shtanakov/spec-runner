"""Tests for spec_runner.runner module."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from spec_runner.runner import (
    build_cli_command,
    check_error_patterns,
    log_progress,
    parse_token_usage,
    run_claude_async,
)


class TestBuildCliCommand:
    """Tests for build_cli_command."""

    def test_claude_default(self):
        result = build_cli_command("claude", "hello")
        assert result == ["claude", "-p", "hello"]

    def test_claude_with_skip_permissions(self):
        result = build_cli_command("claude", "hello", skip_permissions=True)
        assert "--dangerously-skip-permissions" in result
        assert result[0] == "claude"
        assert "-p" in result

    def test_claude_with_model(self):
        result = build_cli_command("claude", "hello", model="opus-4")
        assert "--model" in result
        assert "opus-4" in result

    def test_claude_with_model_and_skip_permissions(self):
        result = build_cli_command("claude", "hello", model="opus-4", skip_permissions=True)
        assert "--dangerously-skip-permissions" in result
        assert "--model" in result
        assert "opus-4" in result

    def test_codex_auto_detect(self):
        result = build_cli_command("codex", "hello")
        assert result[0] == "codex"
        assert "-p" in result
        assert "hello" in result

    def test_codex_with_model(self):
        result = build_cli_command("codex", "hello", model="gpt-4")
        assert "--model" in result
        assert "gpt-4" in result

    def test_ollama_auto_detect(self):
        result = build_cli_command("ollama", "hello", model="llama3")
        assert result == ["ollama", "run", "llama3", "hello"]

    def test_ollama_default_model(self):
        result = build_cli_command("ollama", "hello")
        assert result == ["ollama", "run", "llama3", "hello"]

    def test_llama_cli_auto_detect(self):
        result = build_cli_command("llama-cli", "hello")
        assert "--no-display-prompt" in result
        assert "-p" in result
        assert result[0] == "llama-cli"

    def test_llama_cli_with_model(self):
        result = build_cli_command("llama-cli", "hello", model="model.gguf")
        assert "-m" in result
        assert "model.gguf" in result

    def test_custom_template(self):
        template = "{cmd} --custom {prompt}"
        result = build_cli_command("mycli", "hello world", template=template)
        assert result[0] == "mycli"
        assert "--custom" in result
        # shlex.quote wraps 'hello world' in quotes; shlex.split unpacks it
        assert "hello world" in result

    def test_custom_template_with_model(self):
        template = "{cmd} -m {model} -p {prompt}"
        result = build_cli_command("mycli", "hello", model="mymodel", template=template)
        assert result == ["mycli", "-m", "mymodel", "-p", "hello"]

    def test_custom_template_with_prompt_file(self):
        template = "{cmd} --file {prompt_file}"
        result = build_cli_command(
            "mycli", "ignored", template=template, prompt_file=Path("/tmp/p.txt")
        )
        assert "/tmp/p.txt" in result


class TestCheckErrorPatterns:
    """Tests for check_error_patterns."""

    def test_detects_rate_limit(self):
        result = check_error_patterns("Error: rate limit exceeded, try again later")
        assert result is not None
        assert "rate limit" in result.lower()

    def test_detects_context_window(self):
        result = check_error_patterns("context window overflow detected")
        assert result is not None
        assert "context window" in result.lower()

    def test_detects_quota_exceeded(self):
        result = check_error_patterns("quota exceeded for your account")
        assert result is not None

    def test_normal_output_returns_none(self):
        result = check_error_patterns("Task completed successfully")
        assert result is None

    def test_empty_output_returns_none(self):
        result = check_error_patterns("")
        assert result is None

    def test_case_insensitive(self):
        result = check_error_patterns("RATE LIMIT EXCEEDED")
        assert result is not None

    def test_case_insensitive_mixed(self):
        result = check_error_patterns("Context Window is full")
        assert result is not None


class TestLogProgress:
    """Tests for log_progress."""

    def test_writes_to_file(self, tmp_path, monkeypatch):
        progress_file = tmp_path / "progress.txt"
        monkeypatch.setattr("spec_runner.runner.PROGRESS_FILE", progress_file)

        log_progress("test message")

        content = progress_file.read_text()
        assert "test message" in content

    def test_writes_with_task_id(self, tmp_path, monkeypatch):
        progress_file = tmp_path / "progress.txt"
        monkeypatch.setattr("spec_runner.runner.PROGRESS_FILE", progress_file)

        log_progress("doing work", task_id="TASK-001")

        content = progress_file.read_text()
        assert "[TASK-001]" in content
        assert "doing work" in content

    def test_logs_via_structlog(self, tmp_path, monkeypatch):
        progress_file = tmp_path / "progress.txt"
        monkeypatch.setattr("spec_runner.runner.PROGRESS_FILE", progress_file)

        mock_logger = MagicMock()
        with patch("spec_runner.logging.get_logger", return_value=mock_logger):
            log_progress("hello structlog")
        mock_logger.info.assert_called_once_with("hello structlog")

    def test_logs_via_structlog_with_task_id(self, tmp_path, monkeypatch):
        progress_file = tmp_path / "progress.txt"
        monkeypatch.setattr("spec_runner.runner.PROGRESS_FILE", progress_file)

        mock_logger = MagicMock()
        with patch("spec_runner.logging.get_logger", return_value=mock_logger):
            log_progress("doing work", task_id="TASK-001")
        mock_logger.info.assert_called_once_with("doing work", task_id="TASK-001")

    def test_appends_to_file(self, tmp_path, monkeypatch):
        progress_file = tmp_path / "progress.txt"
        monkeypatch.setattr("spec_runner.runner.PROGRESS_FILE", progress_file)

        log_progress("first")
        log_progress("second")

        content = progress_file.read_text()
        assert "first" in content
        assert "second" in content

    def test_creates_parent_directory(self, tmp_path, monkeypatch):
        progress_file = tmp_path / "subdir" / "progress.txt"
        monkeypatch.setattr("spec_runner.runner.PROGRESS_FILE", progress_file)

        log_progress("nested")

        assert progress_file.exists()
        assert "nested" in progress_file.read_text()

    def test_includes_timestamp(self, tmp_path, monkeypatch):
        progress_file = tmp_path / "progress.txt"
        monkeypatch.setattr("spec_runner.runner.PROGRESS_FILE", progress_file)

        log_progress("timestamped")

        content = progress_file.read_text()
        # Timestamp format is [HH:MM:SS]
        import re

        assert re.search(r"\[\d{2}:\d{2}:\d{2}\]", content)


class TestParseTokenUsage:
    """Tests for parse_token_usage."""

    def test_parses_standard_format(self):
        stderr = "input_tokens: 12500\noutput_tokens: 3200\ntotal cost: $0.12"
        inp, out, cost = parse_token_usage(stderr)
        assert inp == 12500
        assert out == 3200
        assert cost == 0.12

    def test_parses_with_commas(self):
        stderr = "input_tokens: 1,250\noutput_tokens: 320\ncost: $1.50"
        inp, out, cost = parse_token_usage(stderr)
        assert inp == 1250
        assert out == 320
        assert cost == 1.50

    def test_parses_underscore_variant(self):
        stderr = "input tokens: 500\noutput tokens: 100\ntotal_cost: $0.01"
        inp, out, cost = parse_token_usage(stderr)
        assert inp == 500
        assert out == 100
        assert cost == 0.01

    def test_returns_none_on_empty(self):
        inp, out, cost = parse_token_usage("")
        assert inp is None
        assert out is None
        assert cost is None

    def test_returns_none_on_garbage(self):
        inp, out, cost = parse_token_usage("some random text\nwith no tokens")
        assert inp is None
        assert out is None
        assert cost is None

    def test_partial_match_returns_available(self):
        stderr = "input_tokens: 500\nno output info"
        inp, out, cost = parse_token_usage(stderr)
        assert inp == 500
        assert out is None
        assert cost is None


class TestRunClaudeAsync:
    """Tests for async subprocess wrapper."""

    def test_returns_stdout_stderr_returncode(self):
        async def _run():
            with patch("spec_runner.runner.asyncio.create_subprocess_exec") as mock_cse:
                mock_proc = AsyncMock()
                mock_proc.communicate.return_value = (b"output text", b"stderr text")
                mock_proc.returncode = 0
                mock_cse.return_value = mock_proc

                stdout, stderr, rc = await run_claude_async(
                    ["echo", "hi"], timeout=60, cwd="/tmp"
                )
                assert stdout == "output text"
                assert stderr == "stderr text"
                assert rc == 0

        asyncio.run(_run())

    def test_timeout_kills_process(self):
        async def _run():
            with patch("spec_runner.runner.asyncio.create_subprocess_exec") as mock_cse:
                mock_proc = AsyncMock()
                mock_proc.communicate.side_effect = TimeoutError()
                mock_proc.kill = MagicMock()
                mock_proc.wait = AsyncMock()
                mock_cse.return_value = mock_proc

                with pytest.raises(TimeoutError):
                    await run_claude_async(["echo", "hi"], timeout=1, cwd="/tmp")
                mock_proc.kill.assert_called_once()

        asyncio.run(_run())
