"""#266 (F-38): a mention of a marker in prose outranked the real verdict.

Attempt 5 did the whole job — wiring, migrations, tests 306/0, format clean,
frozen files untouched — and ended its output with:

```
TASK_COMPLETE
```

Its summary's first paragraph, describing history, said:

> The prior `TASK_BLOCKED` was resolved upstream: an operator repair (commit
> 7c3b3fe) …

and the harness answered:

```
❌ Failed: agent reported TASK_BLOCKED without a reason
```

A word mid-sentence, in backticks, with no colon and no reason, beat the
terminal marker the same attempt ended with. The finished, green tree survived
only because of #231.

The trap primes itself: the failure text quoting the token goes into the next
attempt's prompt, and an honest agent summarising what came before is likely to
utter it again. So there are two halves here — markers are read as **lines**,
and quoted history reaches the agent with the tokens broken.
"""

from __future__ import annotations

import pytest

from spec_runner.runner import TERMINAL_MARKERS, terminal_markers

THE_RUN = """I implemented the provider fallback chain.

The prior `TASK_BLOCKED` was resolved upstream: an operator repair (commit
7c3b3fe) corrected the asserted shape, so the frozen test now matches REQ-104.

mix test: 306 tests, 0 failures. mix format --check-formatted: clean.

TASK_COMPLETE
"""


class TestTheReportedRun:
    def test_the_prose_mention_is_not_a_verdict(self):
        kinds = [m.kind for m in terminal_markers(THE_RUN)]
        assert kinds == ["TASK_COMPLETE"]

    def test_the_attempt_reads_as_complete(self):
        """The whole point: what the run actually said."""
        kinds = {m.kind for m in terminal_markers(THE_RUN)}
        assert "TASK_BLOCKED" not in kinds
        assert "TASK_COMPLETE" in kinds


class TestWhatCountsAsAMarker:
    @pytest.mark.parametrize(
        "line",
        [
            "TASK_COMPLETE",
            "  TASK_COMPLETE  ",
            "\tTASK_COMPLETE",
        ],
    )
    def test_a_line_of_its_own_counts(self, line):
        assert [m.kind for m in terminal_markers(f"work done\n{line}\n")] == ["TASK_COMPLETE"]

    @pytest.mark.parametrize(
        "text",
        [
            "the prior `TASK_BLOCKED` was resolved upstream",
            "I will not report TASK_FAILED here",
            "see TASK_COMPLETE for the marker to use",
            "- TASK_COMPLETE (a bullet, not a verdict)",
            "TASK_COMPLETE means done, TASK_FAILED means not",
        ],
    )
    def test_a_mention_does_not(self, text):
        assert terminal_markers(text) == []

    def test_a_reason_is_read_from_the_same_line(self):
        markers = terminal_markers("TASK_BLOCKED: the frozen test contradicts REQ-104\n")
        assert markers[0].kind == "TASK_BLOCKED"
        assert markers[0].reason == "the frozen test contradicts REQ-104"

    def test_a_bare_blocked_line_still_counts(self):
        """Deliberately *not* the report's stricter option. Requiring a reason
        would demote a bare escalation to "no marker" — and with exit code 0
        that reads as implicit success, so a blocked task would be recorded
        done. Refusing to guess is the safe side of this ambiguity."""
        markers = terminal_markers("TASK_BLOCKED\n")
        assert [m.kind for m in markers] == ["TASK_BLOCKED"]
        assert markers[0].reason is None

    def test_every_marker_is_recognised(self):
        """The tuple and the pattern are one thing; a marker added to the list
        without the pattern would be silently unreadable."""
        for marker in TERMINAL_MARKERS:
            assert [m.kind for m in terminal_markers(f"{marker}\n")] == [marker]

    def test_the_order_stated_is_preserved(self):
        markers = terminal_markers("TASK_FAILED: first\nTASK_COMPLETE\n")
        assert [m.kind for m in markers] == ["TASK_FAILED", "TASK_COMPLETE"]


@pytest.mark.slow
class TestTheAttemptIsRecordedAsItReads:
    """The call site, not just the parser."""

    def _run(self, tmp_path, monkeypatch, output: str):
        import subprocess
        from pathlib import Path

        from spec_runner import execution, hooks
        from spec_runner.config import ExecutorConfig
        from spec_runner.state import ExecutorState
        from spec_runner.task import Task

        root = tmp_path / "repo"
        root.mkdir(parents=True, exist_ok=True)
        for args in (
            ("init", "-q"),
            ("config", "user.email", "o@e.c"),
            ("config", "user.name", "O"),
        ):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
        (root / "README.md").write_text("x\n")
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True, capture_output=True)

        cfg = ExecutorConfig(
            project_root=root,
            state_file=root / ".state.db",
            logs_dir=root / ".logs",
            create_git_branch=False,
            auto_commit=False,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=False,
        )
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)

        script = root / "fake-cli"
        script.write_text("#!/usr/bin/env bash\ncat <<'MARKER_EOF'\n" + output + "\nMARKER_EOF\n")
        script.chmod(0o755)
        cfg.claude_command = str(script)

        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
        monkeypatch.setattr(execution, "update_task_status", lambda *a, **k: True)
        monkeypatch.setattr(
            hooks, "post_done_hook", lambda *a, **k: (True, None, "skipped", None, False)
        )
        monkeypatch.setattr(execution, "post_done_hook", hooks.post_done_hook)

        task = Task(id="TASK-104", name="t", priority="p1", status="todo", estimate="1h")
        with ExecutorState(cfg) as state:
            ok = execution.execute_task(task, cfg, state)
            attempts = state.tasks["TASK-104"].attempts
        assert Path(script).exists()
        return ok, attempts

    def test_the_finished_run_is_recorded_successful(self, tmp_path, monkeypatch):
        ok, attempts = self._run(tmp_path, monkeypatch, THE_RUN)

        assert ok is True
        assert attempts[-1].success is True

    def test_a_real_block_is_still_a_block(self, tmp_path, monkeypatch):
        """The guarantee that must survive: an agent that actually escalates is
        still terminal, with its own words kept verbatim."""
        blocked = "I cannot do this.\n\nTASK_BLOCKED: the frozen test contradicts REQ-104\n"
        ok, attempts = self._run(tmp_path, monkeypatch, blocked)

        assert ok is False
        assert attempts[-1].error == "the frozen test contradicts REQ-104"
        assert attempts[-1].error_kind == "blocked"


class TestTheLoopDoesNotPrimeItself:
    """The second half. Line-anchoring stops a mention being read as a verdict;
    this stops the harness from teaching the agent to write one."""

    def test_quoted_history_reaches_the_prompt_with_the_token_broken(self):
        from spec_runner.prompt import neutralise_markers

        quoted = neutralise_markers("agent reported TASK_BLOCKED without a reason")

        assert "TASK_BLOCKED" not in quoted
        assert "TASK_BLOCKE" in quoted, "the operator must still see which marker is meant"
        assert terminal_markers(quoted) == []

    def test_the_separator_is_the_named_character(self):
        """Pinned by codepoint (Copilot, PR #269). The separator used to be an
        invisible literal in the source: an editor, a formatter or a paste
        through a terminal could drop it, and nothing here would have noticed —
        the tokens would simply be back in prompts."""
        from spec_runner.prompt import ZERO_WIDTH_SPACE, neutralise_markers

        assert ZERO_WIDTH_SPACE == "\u200b"
        assert neutralise_markers("TASK_COMPLETE") == "TASK_COMPLET\u200bE"

    def test_the_retry_prompt_carries_the_broken_form(self, tmp_path):
        from spec_runner.config import ExecutorConfig
        from spec_runner.prompt import build_task_prompt
        from spec_runner.state import ErrorCode, RetryContext
        from spec_runner.task import Task

        cfg = ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "s.db")
        task = Task(id="TASK-104", name="t", priority="p1", status="todo", estimate="1h")
        context = RetryContext(
            attempt_number=2,
            max_attempts=3,
            previous_error_code=ErrorCode.TASK_BLOCKED,
            previous_error="agent reported TASK_BLOCKED without a reason",
            what_was_tried="reported TASK_BLOCKED",
            test_failures=None,
        )

        prompt = build_task_prompt(task, cfg, [], retry_context=context)

        assert "TASK_BLOCKED without a reason" not in prompt
        assert "reason" in prompt, "the operator's information is still there"

    def test_the_instruction_that_asks_for_a_marker_is_untouched(self, tmp_path):
        """Only *quoted history* is neutralised. The protocol we state must
        stay verbatim, or the agent would be asked for a marker it cannot
        spell."""
        from spec_runner.config import ExecutorConfig
        from spec_runner.prompt import build_task_prompt
        from spec_runner.task import Task

        cfg = ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "s.db")
        task = Task(id="TASK-104", name="t", priority="p1", status="todo", estimate="1h")

        prompt = build_task_prompt(task, cfg, [])

        assert "TASK_COMPLETE" in prompt
