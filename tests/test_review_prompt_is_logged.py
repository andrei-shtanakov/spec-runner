"""Every paid stage's prompt is recorded, in one format (#282 follow-up).

The owner's requirement before the next paid run: symmetric observability of
all paid stages. Measuring first narrowed what was actually missing — the
**single** review path already wrote its prompt; the **per-role** path wrote
nothing, so a parallel review left five paid calls and no record of what any of
them had been asked. And the two forms that did exist did not share a format,
a bound, or a provenance with the RED prompt.

So this is one writer for all of them (`prompts_log`), and what it guarantees:

- the prompt **as sent** — after template rendering, and after every block
  appended to it. A prompt logged before rendering answers a different question
  than the one an operator asks;
- **its own provenance**, including `review:<role>` per role, because
  `review:security` and `review:performance` are different questions;
- **nothing else**: no argv, no environment, no model or command name. Those
  are the runner's business and the place a secret would be;
- **bounded**, keeping head *and* tail — the frozen-files block is appended
  last, and a head-only truncation would drop exactly what a claims refusal
  sends you to read;
- **never fatal**. A prompt that cannot be written is a warning; it must not
  turn work that already happened into a failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.prompts_log import KEEP_EACH_END, bound, log_prompt, provenance_slug
from spec_runner.review import ReviewCall


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "spec" / ".executor-state.db",
        "logs_dir": tmp_path / "spec" / ".executor-logs",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task():
    from spec_runner.task import Task

    return Task(id="TASK-104", name="t", priority="p1", status="todo", estimate="1h")


def _call(output: str = "REVIEW_PASSED\n") -> ReviewCall:
    return ReviewCall(
        text=output, stderr="", returncode=0, cost_usd=0.5, is_error=False, timed_out=False
    )


def _logs(cfg: ExecutorConfig, pattern: str) -> list[Path]:
    return sorted(cfg.logs_dir.glob(pattern))


class TestEveryReviewCallLeavesItsPrompt:
    def test_the_single_path_writes_one(self, tmp_path, monkeypatch):
        from spec_runner import review

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: "REVIEW THIS")
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: _call())

        review.run_code_review(_task(), cfg)

        logs = _logs(cfg, "*-review-*.log")
        assert len(logs) == 1
        body = logs[0].read_text()
        assert "=== REVIEW PROMPT ===" in body
        assert "REVIEW THIS" in body

    def test_each_role_writes_its_own(self, tmp_path, monkeypatch):
        """The half that wrote nothing at all: five roles, five paid calls, and
        no record of what any of them was asked."""
        from spec_runner import review

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: _call())

        for role in ("security", "performance"):
            review._run_single_role_review(
                role, f"look for {role} problems", "base prompt", "cli", "", "", cfg, "TASK-104"
            )

        names = [p.name for p in _logs(cfg, "*-review-*.log")]
        assert any("review-security" in n for n in names), names
        assert any("review-performance" in n for n in names), names

    def test_a_role_log_holds_the_prompt_that_role_was_sent(self, tmp_path, monkeypatch):
        """Not the base prompt: the role's own instruction is the part that
        differs, and the reason each role has its own ledger row."""
        from spec_runner import review

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: _call())

        review._run_single_role_review(
            "security", "look for injection", "the base prompt", "cli", "", "", cfg, "TASK-104"
        )

        body = _logs(cfg, "*review-security*")[0].read_text()
        assert "look for injection" in body
        assert "the base prompt" in body

    def test_the_answer_lands_beside_the_prompt(self, tmp_path, monkeypatch):
        """A prompt without its answer says what was asked and not what came
        back — the single path has always kept both, and it still does."""
        from spec_runner import review

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: "REVIEW THIS")
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: _call("REVIEW_PASSED\n"))

        review.run_code_review(_task(), cfg)

        body = _logs(cfg, "*-review-*.log")[0].read_text()
        assert "=== OUTPUT ===" in body and "REVIEW_PASSED" in body
        assert "=== RETURN CODE: 0 ===" in body
        assert "=== COST: 0.5 ===" in body

    def test_an_unreported_cost_is_written_as_unknown(self, tmp_path, monkeypatch):
        """`unknown`, never 0.0. An unreported cost is exactly what the budget
        guard refuses to spend against (#213); a zero in the record would put
        the opposite claim on file."""
        from spec_runner import review

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: "REVIEW THIS")
        monkeypatch.setattr(
            review,
            "_run_reviewer",
            lambda *a, **k: ReviewCall(
                text="REVIEW_PASSED\n",
                stderr="",
                returncode=0,
                cost_usd=None,
                is_error=False,
                timed_out=False,
            ),
        )

        review.run_code_review(_task(), cfg)

        assert "=== COST: unknown ===" in _logs(cfg, "*-review-*.log")[0].read_text()


class TestWhatIsNotWritten:
    def test_no_argv_no_environment(self, tmp_path, monkeypatch):
        """The prompt is text this tool composed from the project's own files.
        argv and the environment are the runner's business, and the place a
        secret would live."""
        from spec_runner import review

        cfg = _cfg(tmp_path, review_command="/usr/bin/claude --dangerously-skip-permissions")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-do-not-log-me")
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: "REVIEW THIS")
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: _call())

        review.run_code_review(_task(), cfg)

        body = _logs(cfg, "*-review-*.log")[0].read_text()
        assert "sk-do-not-log-me" not in body
        assert "dangerously-skip-permissions" not in body
        assert "ANTHROPIC_API_KEY" not in body


class TestTheFormatIsShared:
    def test_a_role_provenance_cannot_escape_the_directory(self):
        """`review:security` is a provenance, not a path — and a role name is
        config, so it must not be able to name a file elsewhere."""
        assert provenance_slug("review:security") == "review-security"
        assert provenance_slug("") == "prompt"

        # The property, not just the examples: nothing that traverses, and
        # nothing that hides itself from `ls`.
        for hostile in ("../../etc/passwd", "a/b", "..", ".hidden", "x\0y"):
            slug = provenance_slug(hostile)
            assert "/" not in slug and ".." not in slug
            assert not slug.startswith(".")

    def test_a_long_prompt_keeps_its_head_and_its_tail(self):
        """The frozen-files block is appended last (#214). Truncating to the
        head would drop precisely what a claims refusal tells you to read."""
        text = "HEAD" + ("x" * (KEEP_EACH_END * 3)) + "TAIL"

        result = bound(text)

        assert result.startswith("HEAD")
        assert result.endswith("TAIL\n") or result.endswith("TAIL")
        assert "omitted from the middle" in result
        assert len(result) < len(text)

    def test_a_short_prompt_is_untouched(self):
        assert bound("just this") == "just this"

    def test_the_red_and_review_logs_share_one_shape(self, tmp_path):
        """One writer, so the two cannot drift into two formats — which is what
        they had before this: `=== REVIEW PROMPT ===` written by hand in one
        place and `=== RED PROMPT ===` in another."""
        cfg = _cfg(tmp_path)

        red = log_prompt(cfg, "TASK-104", "red", "a")
        review = log_prompt(cfg, "TASK-104", "review:security", "b")

        assert red is not None and review is not None
        assert red.read_text().startswith("=== RED PROMPT ===")
        assert review.read_text().startswith("=== REVIEW:SECURITY PROMPT ===")


class TestItNeverCostsTheWork:
    def test_a_failed_write_returns_none_and_warns(self, tmp_path, monkeypatch):
        from spec_runner import prompts_log

        said: list[tuple[str, str, dict]] = []

        class _Recorder:
            def __getattr__(self, level):
                def log(event, **kw):
                    said.append((level, event, kw))

                return log

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(prompts_log, "logger", _Recorder())
        cfg.logs_dir.chmod(0o500)
        probe = cfg.logs_dir / ".probe"
        try:
            probe.write_text("x")
        except OSError:
            pass
        else:
            probe.unlink()
            cfg.logs_dir.chmod(0o700)
            pytest.skip("this filesystem does not enforce a read-only directory")

        try:
            path = log_prompt(cfg, "TASK-104", "review", "x")
        finally:
            cfg.logs_dir.chmod(0o700)

        assert path is None
        assert any(level == "warning" for level, _event, _kw in said)

    def test_a_review_still_returns_its_verdict_when_the_log_fails(self, tmp_path, monkeypatch):
        """Bookkeeping that can fail work is a second, weaker gate. Here it
        would discard a verdict that was already paid for."""
        from spec_runner import prompts_log, review
        from spec_runner.state import ReviewVerdict

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: "REVIEW THIS")
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: _call())
        monkeypatch.setattr(
            prompts_log.Path,
            "write_text",
            lambda self, *a, **k: (_ for _ in ()).throw(OSError("read-only")),
        )

        verdict, _detail, _out = review.run_code_review(_task(), cfg)

        assert verdict is ReviewVerdict.PASSED
