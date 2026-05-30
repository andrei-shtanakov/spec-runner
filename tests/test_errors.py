"""Tests for spec_runner.errors module (v2.3.0)."""

import pytest

from spec_runner.errors import ErrorPattern, classify


class TestClassifyCodexUsageLimit:
    def test_codex_quota_message_yields_rate_limit_with_eta(self):
        stderr = (
            "ERROR: You've hit your usage limit. "
            "Upgrade to Pro or try again at 9:54 AM.\n"
        )
        kind, msg = classify(stderr, returncode=1)
        assert kind == "rate_limit"
        assert "9:54 AM" in msg
        assert "OpenAI usage limit" in msg

    def test_empty_stderr_returns_unknown_with_returncode_message(self):
        kind, msg = classify("", returncode=7)
        assert kind == "unknown"
        assert "code 7" in msg


class TestErrorPatternImmutability:
    def test_pattern_is_frozen(self):
        import re

        p = ErrorPattern(kind="x", regex=re.compile("y"), template="z")
        with pytest.raises(AttributeError):
            p.kind = "different"  # type: ignore[misc]


class TestClassifyMoreProviders:
    def test_generic_rate_limit_word(self):
        kind, msg = classify("Hit a rate-limit, retrying...", 1)
        assert kind == "rate_limit"
        assert msg == "Rate limit hit"

    def test_auth_unauthorized(self):
        kind, msg = classify("error: 401 unauthorized\n", 1)
        assert kind == "auth"
        assert msg == "Authentication failed"

    def test_auth_invalid_api_key(self):
        kind, msg = classify("OpenAI Error: Invalid API key provided.\n", 1)
        assert kind == "auth"

    def test_network_econnrefused(self):
        kind, msg = classify("connect ECONNREFUSED 127.0.0.1:443\n", 1)
        assert kind == "network"
        assert msg == "Network error"

    def test_network_timeout(self):
        kind, _ = classify("request timed out after 30s\n", 1)
        assert kind == "network"

    def test_cli_error_line_extracts_message(self):
        kind, msg = classify("error: invalid value 'foo' for '--bar'\n", 2)
        assert kind == "cli_error"
        assert msg == "invalid value 'foo' for '--bar'"

    def test_fallback_tail_5_lines(self):
        stderr = "\n".join(f"line {i}" for i in range(10)) + "\n"
        kind, msg = classify(stderr, 1)
        assert kind == "unknown"
        assert msg == "line 5\nline 6\nline 7\nline 8\nline 9"

    def test_first_match_wins_when_multiple_patterns_could_apply(self):
        stderr = "Hit a rate-limit error: try again later\n"
        kind, _ = classify(stderr, 1)
        assert kind == "rate_limit"
