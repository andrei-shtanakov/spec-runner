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
