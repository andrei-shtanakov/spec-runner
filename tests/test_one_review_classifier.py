"""#270: two review paths read a verdict two ways, by substring.

Sequential review tried `REVIEW_PASSED` → `REVIEW_FIXED` → `REVIEW_FAILED`; the
per-role path tried them in the opposite order. So a reviewer that stated two of
them was recorded **passed** when roles ran one at a time and **failed** when
they ran together — and what makes them run one at a time is an active budget
(#213). The verdict depended on how much money was left, which is not a
property of the code under review.

Both also matched substrings, exactly as the terminal markers did before #266:
"this is not a REVIEW_FAILED situation" was a failed review.

The owner's decision, and what this file pins:

- one classifier for both paths;
- markers only on lines of their own;
- a repeat of the same verdict is fine — a reviewer saying `REVIEW_PASSED` in a
  heading and again in a summary has said one thing twice;
- **different** verdicts are an error, not a ranking problem. Precedence would
  have removed the sequential/parallel divergence while keeping the ambiguity:
  a reviewer that says both has not decided, and choosing for it would be the
  tool inventing a verdict;
- no first/last/priority wins anywhere;
- history quoted into a later prompt is neutralised;
- under `review_policy: required` the result blocks; under `advisory` it is
  recorded as uncertainty and never as passed.
"""

from __future__ import annotations

import pytest

from spec_runner.review import ReviewSignal, read_review
from spec_runner.runner import review_markers
from spec_runner.state import ReviewVerdict


def _cfg(tmp_path):
    """A project whose log directory exists — review writes its prompt there."""
    from spec_runner.config import ExecutorConfig

    cfg = ExecutorConfig(
        project_root=tmp_path,
        state_file=tmp_path / "spec" / ".executor-state.db",
        logs_dir=tmp_path / "spec" / ".executor-logs",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


class TestWhatCountsAsAVerdict:
    @pytest.mark.parametrize(
        "text",
        [
            "REVIEW_PASSED",
            "  REVIEW_PASSED  ",
            "review_passed",
            "Looks good.\n\nREVIEW_PASSED\n",
        ],
    )
    def test_a_line_of_its_own_counts(self, text):
        assert read_review(text).only == "REVIEW_PASSED"

    @pytest.mark.parametrize(
        "text",
        [
            "this is not a REVIEW_FAILED situation",
            "I would normally say REVIEW_PASSED here",
            "- REVIEW_PASSED (a bullet, not a verdict)",
            "REVIEW_PASSED means approved, REVIEW_FAILED means not",
        ],
    )
    def test_a_mention_does_not(self, text):
        assert read_review(text).stated == ()

    def test_a_repeat_is_one_verdict(self):
        """A reviewer that states its verdict in a heading and again in a
        summary has said one thing twice."""
        signal = read_review("REVIEW_PASSED\n\nsummary\n\nREVIEW_PASSED\n")

        assert signal.stated == ("REVIEW_PASSED",)
        assert signal.conflicting is False

    def test_two_different_verdicts_conflict(self):
        signal = read_review("REVIEW_FAILED: the migration is unsafe\n\nREVIEW_PASSED\n")

        assert signal.conflicting is True
        assert signal.only is None

    def test_the_order_is_kept_but_never_ranked(self):
        """The order is reported because an operator reading the message wants
        it. Nothing decides on it — that is the whole point."""
        assert read_review("REVIEW_PASSED\nREVIEW_FAILED\n").stated == (
            "REVIEW_PASSED",
            "REVIEW_FAILED",
        )
        assert read_review("REVIEW_FAILED\nREVIEW_PASSED\n").stated == (
            "REVIEW_FAILED",
            "REVIEW_PASSED",
        )

    def test_the_low_level_reader_is_shared_with_the_terminal_markers(self):
        """One pattern builder, two vocabularies (#266/#270), so "what counts
        as a marker" cannot be answered differently in two places."""
        assert review_markers("REVIEW_FIXED\n") == ["REVIEW_FIXED"]
        assert review_markers("say REVIEW_FIXED to mean fixed") == []


class TestBothPathsAgree:
    """The defect itself: the same output, both readers."""

    CONTRADICTION = "REVIEW_FAILED: unsafe\n\n…on reflection…\n\nREVIEW_PASSED\n"

    def _call(self, output):
        from spec_runner.review import ReviewCall

        return ReviewCall(
            text=output, stderr="", returncode=0, cost_usd=0.0, is_error=False, timed_out=False
        )

    def _single(self, output: str, tmp_path, monkeypatch):
        from spec_runner import review
        from spec_runner.task import Task

        cfg = _cfg(tmp_path)
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: "prompt")
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: self._call(output))
        verdict, _detail, _out = review.run_code_review(task, cfg)
        return verdict

    def _role(self, output: str, tmp_path, monkeypatch):
        from spec_runner import review

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: self._call(output))
        _role_name, verdict, _out = review._run_single_role_review(
            "security", "role prompt", "base", "cli", "", "", cfg, "TASK-001"
        )
        return verdict

    def test_a_contradiction_is_an_error_on_both(self, tmp_path, monkeypatch):
        assert self._single(self.CONTRADICTION, tmp_path, monkeypatch) is ReviewVerdict.ERROR
        assert self._role(self.CONTRADICTION, tmp_path, monkeypatch) is ReviewVerdict.ERROR

    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            ("REVIEW_PASSED\n", ReviewVerdict.PASSED),
            ("REVIEW_FAILED\n", ReviewVerdict.FAILED),
            ("prose only, no marker\n", ReviewVerdict.NOT_RUN),
            ("mentions REVIEW_PASSED inline\n", ReviewVerdict.NOT_RUN),
        ],
    )
    def test_the_ordinary_verdicts_agree_too(self, output, expected, tmp_path, monkeypatch):
        assert self._single(output, tmp_path, monkeypatch) is expected
        assert self._role(output, tmp_path, monkeypatch) is expected

    def test_the_role_path_keeps_the_reviewer_s_words(self, tmp_path, monkeypatch):
        """The branch exists so a person can read what the reviewer said, so it
        must not delete it (Copilot, PR #278). The aggregate report quotes this
        text under the role's heading — the explanation is prefixed, and the
        function keeps its `(role, verdict, output)` contract."""
        from spec_runner import review

        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: self._call(self.CONTRADICTION))
        cfg = _cfg(tmp_path)

        _role, verdict, output = review._run_single_role_review(
            "security", "role prompt", "base", "cli", "", "", cfg, "TASK-001"
        )

        assert verdict is ReviewVerdict.ERROR
        assert "conflicting verdicts" in output
        assert "the migration is unsafe" in output or "unsafe" in output
        assert self.CONTRADICTION in output, "the reviewer's own text, verbatim"

    def test_the_message_names_what_was_stated(self, tmp_path, monkeypatch):
        """An error an operator cannot act on is a shrug: the point of refusing
        to choose is that a person now has to read it."""
        from spec_runner import review
        from spec_runner.task import Task

        cfg = _cfg(tmp_path)
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: "prompt")
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: self._call(self.CONTRADICTION))

        _verdict, detail, _out = review.run_code_review(task, cfg)

        assert detail is not None
        assert "REVIEW_FAILED" in detail and "REVIEW_PASSED" in detail
        assert "person" in detail


class TestThePolicyConsequence:
    """`required` blocks, `advisory` records uncertainty rather than passed."""

    def test_error_blocks_under_required(self, tmp_path):
        from spec_runner.config import ExecutorConfig
        from spec_runner.gates import GateContext, GateStatus, _review_gate
        from spec_runner.state import ExecutorState

        cfg = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "s.db",
            logs_dir=tmp_path / "logs",
            review_policy="required",
        )
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)
        with ExecutorState(cfg) as state:
            ctx = GateContext(
                task_id="TASK-001",
                checkpoint_sha="candidate",
                config=cfg,
                state=state,
                facts={"review_verdict": ReviewVerdict.ERROR.value},
            )
            result = _review_gate(ctx)

        assert result.status is not GateStatus.SATISFIED

    def test_advisory_records_it_and_does_not_read_as_passed(self):
        """Nothing gates under advisory — what matters is that the recorded
        verdict is not `passed`, since that is what a reader trusts later."""
        signal = read_review("REVIEW_FAILED\nREVIEW_PASSED\n")

        assert signal.conflicting
        assert signal.only is None, "no verdict is derivable, so none is recorded"


class TestQuotedHistoryIsNeutralised:
    def test_review_markers_are_broken_in_a_prompt(self):
        from spec_runner.prompt import neutralise_markers

        quoted = neutralise_markers("the previous round said REVIEW_FAILED about the migration")

        assert "REVIEW_FAILED" not in quoted
        assert read_review(quoted).stated == ()

    def test_the_review_prompt_carries_the_broken_form(self, tmp_path):
        from spec_runner.config import ExecutorConfig
        from spec_runner.review import build_review_prompt
        from spec_runner.task import Task

        cfg = ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "s.db")
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")

        prompt = build_review_prompt(
            task, cfg, previous_error="the reviewer said REVIEW_FAILED last time"
        )

        assert "said REVIEW_FAILED last time" not in prompt
        assert "last time" in prompt, "the information survives; only the token is broken"

    def test_the_instruction_that_asks_for_a_marker_is_untouched(self, tmp_path):
        from spec_runner.config import ExecutorConfig
        from spec_runner.review import build_review_prompt
        from spec_runner.task import Task

        cfg = ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "s.db")
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")

        prompt = build_review_prompt(task, cfg)

        assert "REVIEW_PASSED" in prompt


class TestTheSignalItself:
    def test_no_marker_is_not_a_conflict(self):
        assert ReviewSignal(()).conflicting is False
        assert ReviewSignal(()).only is None

    def test_one_marker_is_not_a_conflict(self):
        assert ReviewSignal(("REVIEW_FIXED",)).only == "REVIEW_FIXED"
        assert ReviewSignal(("REVIEW_FIXED",)).conflicting is False


class TestMarkdownTolerantReviewMarkers:
    """#336, four live hits on the devtools conveyor in two days: models
    naturally bold the final verdict, and the built-in prompt itself spelled
    the marker inside double quotes — a model following it literally never
    matched. Symmetric wrapping on an otherwise-empty line is the same
    statement; prose and asymmetric wrappers still are not."""

    def test_symmetric_wrappers_count(self):
        for wrapped in (
            "**REVIEW_FIXED**",
            "*REVIEW_PASSED*",
            "__REVIEW_PASSED__",
            "`REVIEW_FIXED`",
            '"REVIEW_PASSED"',
        ):
            kinds = review_markers(f"summary…\n{wrapped}\n")
            assert kinds == [wrapped.strip('*_`"').upper()], wrapped

    def test_wrapped_marker_with_reason(self):
        assert review_markers("**REVIEW_FAILED**: unsafe eval\n") == [
            "REVIEW_FAILED"
        ]

    def test_asymmetric_wrapper_does_not_count(self):
        assert review_markers("**REVIEW_PASSED*\n") == []
        assert review_markers('"REVIEW_PASSED\n') == []

    def test_prose_mention_still_does_not_count(self):
        assert review_markers("this is not a **REVIEW_FAILED** situation\n") == []
        assert review_markers("we should say REVIEW_PASSED here\n") == []

    def test_terminal_markers_stay_strict(self):
        from spec_runner.runner import terminal_markers

        assert terminal_markers("**TASK_COMPLETE**\n") == []
