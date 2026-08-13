"""#241: one agent result, one reading of it.

`review.run_code_review` and `review_pr.verify_comment` each run one agent and
get back the same four signals — text, return code, the CLI's own error flag, a
timeout — and they disagreed about what those mean:

| | `run_code_review` | `verify_comment` (before) |
|---|---|---|
| exit ≠ 0, text present | verdict **discarded** (#156) | verdict **parsed and used** |
| exit ≠ 0, no text | error | `uncertain` |
| exit 0, nothing printed | `not_run` | `uncertain` |

The permissive one was the one that answers a human on a public PR: a verifier
that died after printing `VERDICT: REFUTED` had its refutation posted as
evidence.

The discriminator behind the disagreement — "did it print anything" — cannot
work. A CLI killed by a provider limit mid-answer prints a partial answer, and
so does one that crashed after its conclusion; the text does not distinguish
them, and only one reached a verdict.

`classify_agent_answer` is now the single place that question is answered.
Consumers still *act* differently on a verdict — `review` returns a
`ReviewVerdict`, `review_pr` a comment verdict, and policy differs between them
— but they no longer differ on whether there is one to act on.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runner import review_pr as rp
from spec_runner.config import ExecutorConfig
from spec_runner.review import run_code_review
from spec_runner.review_pr import BotComment, verify_comment
from spec_runner.runner import AgentAnswer, CliResult, classify_agent_answer
from spec_runner.state import ReviewVerdict
from spec_runner.task import Task

REPO = "owner/repo"


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "state.db",
        "logs_dir": tmp_path / "logs",
        "claude_command": "claude",
        "create_git_branch": False,
        "auto_commit": False,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _claude(text: str, *, returncode: int = 0, is_error: bool = False) -> MagicMock:
    payload: dict = {
        "result": text,
        "usage": {"input_tokens": 10, "output_tokens": 2},
        "total_cost_usd": 0.1,
    }
    if is_error:
        payload["is_error"] = True
    return MagicMock(returncode=returncode, stdout=json.dumps(payload), stderr="stderr line")


def _comment() -> BotComment:
    return BotComment(
        comment_id=1,
        author="Copilot",
        path="src/x.py",
        line=10,
        body="Bug here",
        diff_hunk="@@ -1 +1 @@",
        url="https://example.invalid/1",
    )


def _result(text: str, is_error: bool = False) -> CliResult:
    return CliResult(
        text=text, input_tokens=None, output_tokens=None, cost_usd=None, is_error=is_error
    )


class TestTheClassifier:
    def test_a_clean_run_with_output_answered(self):
        assert classify_agent_answer(_result("VERDICT: VALID"), 0) is AgentAnswer.ANSWERED

    def test_a_clean_run_with_nothing_is_empty(self):
        assert classify_agent_answer(_result("   "), 0) is AgentAnswer.EMPTY

    def test_a_non_zero_exit_crashed(self):
        assert classify_agent_answer(_result("VERDICT: VALID"), 1) is AgentAnswer.CRASHED

    def test_the_clis_own_error_flag_crashed_even_at_exit_zero(self):
        """claude's JSON reports `is_error` with exit 0 — the flag is the
        authority there, not the shell's idea of success."""
        assert classify_agent_answer(_result("boom", is_error=True), 0) is AgentAnswer.CRASHED

    def test_a_timeout_is_its_own_answer(self):
        assert classify_agent_answer(_result(""), 0, timed_out=True) is AgentAnswer.TIMED_OUT

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            (AgentAnswer.ANSWERED, True),
            (AgentAnswer.EMPTY, False),
            (AgentAnswer.CRASHED, False),
            (AgentAnswer.TIMED_OUT, False),
        ],
    )
    def test_only_a_finished_run_carries_a_verdict(self, answer, expected):
        assert answer.carries_a_verdict is expected


class TestTheContradiction:
    """`exit != 0` **plus** a verdict marker — the case the two sites resolved
    in opposite directions."""

    def test_the_reviewer_does_not_believe_it(self, tmp_path):
        cfg = _cfg(tmp_path)
        with patch(
            "spec_runner.review.subprocess.run",
            return_value=_claude("Looks fine. REVIEW_PASSED", returncode=1),
        ):
            verdict, error, _out = run_code_review(
                Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1d"), cfg
            )

        assert verdict is ReviewVerdict.ERROR
        assert "exited with code 1" in (error or "")

    def test_the_verifier_does_not_believe_it_either(self, tmp_path):
        """The change. This used to return `refuted` and post that refutation
        to the PR as evidence."""
        cfg = _cfg(tmp_path)
        with patch.object(
            rp.subprocess,
            "run",
            return_value=_claude("VERDICT: REFUTED\nEVIDENCE: the test passes", returncode=1),
        ):
            verdict, evidence, _cost = verify_comment(_comment(), REPO, 6, cfg)

        assert verdict == "uncertain"
        assert "exited 1" in evidence

    def test_and_says_what_it_threw_away(self, tmp_path):
        """Discarding a verdict silently would leave an operator wondering why
        a comment came back uncertain when the log shows a clear answer."""
        cfg = _cfg(tmp_path)
        with patch.object(
            rp.subprocess,
            "run",
            return_value=_claude("VERDICT: REFUTED\nEVIDENCE: the test passes", returncode=1),
        ):
            _verdict, evidence, _cost = verify_comment(_comment(), REPO, 6, cfg)

        assert "VERDICT: REFUTED" in evidence
        assert "discarded" in evidence

    def test_an_is_error_payload_at_exit_zero_is_also_discarded(self, tmp_path):
        """The signal that arrives with a successful exit code — and the one a
        return-code-only check misses."""
        cfg = _cfg(tmp_path)
        with patch.object(
            rp.subprocess,
            "run",
            return_value=_claude("VERDICT: VALID\nEVIDENCE: e", is_error=True),
        ):
            verdict, _evidence, _cost = verify_comment(_comment(), REPO, 6, cfg)

        assert verdict == "uncertain"


class TestTheThreeCasesStayApart:
    """Transport failure, a valid negative verdict, and the contradiction are
    three different things and must not collapse into one another."""

    def test_a_transport_failure_is_not_a_verdict(self, tmp_path):
        cfg = _cfg(tmp_path)
        with patch.object(
            rp.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)
        ):
            verdict, evidence, cost = verify_comment(_comment(), REPO, 6, cfg)

        assert verdict == "uncertain"
        assert "timed out" in evidence
        assert cost is None, "billed for the time it ran; unknown is not zero"

    def test_a_valid_negative_verdict_is_kept(self, tmp_path):
        """The other half of the fix: a reviewer that finishes and says "no" is
        answering, and must not be swept up with the crashes."""
        cfg = _cfg(tmp_path)
        with patch.object(
            rp.subprocess,
            "run",
            return_value=_claude("VERDICT: REFUTED\nEVIDENCE: ran it, the test passes"),
        ):
            verdict, evidence, _cost = verify_comment(_comment(), REPO, 6, cfg)

        assert verdict == "refuted"
        assert "ran it" in evidence

    def test_the_reviewers_negative_verdict_is_kept_too(self, tmp_path):
        cfg = _cfg(tmp_path)
        with patch(
            "spec_runner.review.subprocess.run",
            return_value=_claude("REVIEW_FAILED: three findings"),
        ):
            verdict, _error, _out = run_code_review(
                Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1d"), cfg
            )

        assert verdict is ReviewVerdict.FAILED

    def test_silence_is_not_approval_on_either_side(self, tmp_path):
        """#138's rule, now reached through the shared classifier."""
        cfg = _cfg(tmp_path)
        with patch("spec_runner.review.subprocess.run", return_value=_claude("")):
            verdict, _error, _out = run_code_review(
                Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1d"), cfg
            )
        assert verdict is ReviewVerdict.NOT_RUN

        with patch.object(rp.subprocess, "run", return_value=_claude("")):
            comment_verdict, evidence, _cost = verify_comment(_comment(), REPO, 6, cfg)
        assert comment_verdict == "uncertain"
        assert "no output" in evidence.lower()
