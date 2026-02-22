"""Tests for spec_runner.runner module."""

from pathlib import Path

from spec_runner.runner import build_cli_command, check_error_patterns, log_progress


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

    def test_prints_to_stdout(self, tmp_path, monkeypatch, capsys):
        progress_file = tmp_path / "progress.txt"
        monkeypatch.setattr("spec_runner.runner.PROGRESS_FILE", progress_file)

        log_progress("hello stdout")

        captured = capsys.readouterr()
        assert "hello stdout" in captured.out

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
