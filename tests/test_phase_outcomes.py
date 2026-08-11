"""Slice 0: a typed outcome per phase, and an append-only history of them.

Today a stage says where it is (`StageReporter`) and, if it dies, where it died
(`error_stage`). That is the whole vocabulary: a stage either fell over or it
did not. One phase already grew a real one under pressure — `review`, in #138,
because "no verdict" was being recorded as `passed`.

This slice generalizes that, and nothing gates on it yet. The guarantee for a
project that does not opt into anything is the one from the design: **execution,
terminal state and external contracts do not change** — deliberately not "byte
identical", since the new rows make byte identity impossible by construction.

Design: `docs/superpowers/specs/2026-08-11-tdd-lifecycle-design.md` (Part A).
"""

from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.phases import ALLOWED_OUTCOMES, PhaseOutcome, review_verdict_to_phase
from spec_runner.state import ExecutorState, ReviewVerdict


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "state.db",
        "logs_dir": tmp_path / "logs",
        "create_git_branch": False,
        "auto_commit": False,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


class TestVocabulary:
    def test_the_six_values(self):
        assert {o.value for o in PhaseOutcome} == {
            "pass",
            "expected_fail",
            "unexpected_fail",
            "not_run",
            "error",
            "skipped",
        }

    def test_waived_is_not_an_outcome(self):
        """A result is what the instrument observed; a waiver is an operator
        overriding it. Collapsing them destroys the very information a waiver
        exists to preserve."""
        assert not any(o.value == "waived" for o in PhaseOutcome)


class TestPerStageAllowedOutcomes:
    """The vocabulary is the base set, not a set every stage must implement."""

    def test_every_stage_declares_its_set(self):
        from spec_runner.stages import STAGES

        assert set(ALLOWED_OUTCOMES) == set(STAGES)

    def test_expected_fail_is_meaningless_for_commit(self):
        assert PhaseOutcome.EXPECTED_FAIL not in ALLOWED_OUTCOMES["commit"]

    def test_every_stage_can_at_least_pass_and_error(self):
        for stage, allowed in ALLOWED_OUTCOMES.items():
            assert PhaseOutcome.PASS in allowed, stage
            assert PhaseOutcome.ERROR in allowed, stage

    def test_recording_a_disallowed_outcome_is_a_bug_not_a_surprise(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(ValueError, match="commit"):
            state.record_phase("TASK-001", "commit", PhaseOutcome.EXPECTED_FAIL)

    def test_unknown_stage_is_rejected(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(ValueError):
            state.record_phase("TASK-001", "not-a-stage", PhaseOutcome.PASS)


class TestAppendOnlyHistory:
    def test_a_phase_can_be_recorded_more_than_once(self, tmp_path):
        """A phase runs again on a retry, and the earlier verdicts are
        evidence, not noise."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_phase("TASK-001", "tests", PhaseOutcome.UNEXPECTED_FAIL, "2 failed")
            state.record_phase("TASK-001", "tests", PhaseOutcome.PASS, "12 passed")
            rows = state.phase_history("TASK-001")

        assert [r.outcome for r in rows] == [PhaseOutcome.UNEXPECTED_FAIL, PhaseOutcome.PASS]
        assert rows[0].detail == "2 failed"

    def test_history_is_ordered_and_timestamped(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            for outcome in (PhaseOutcome.ERROR, PhaseOutcome.NOT_RUN, PhaseOutcome.PASS):
                state.record_phase("TASK-001", "review", outcome)
            rows = state.phase_history("TASK-001")
        assert [r.outcome for r in rows] == [
            PhaseOutcome.ERROR,
            PhaseOutcome.NOT_RUN,
            PhaseOutcome.PASS,
        ]
        assert all(r.timestamp for r in rows)

    def test_history_survives_a_reopen(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_phase("TASK-001", "lint", PhaseOutcome.PASS)
        with ExecutorState(cfg) as state:
            assert len(state.phase_history("TASK-001")) == 1

    def test_history_is_per_task(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_phase("TASK-001", "lint", PhaseOutcome.PASS)
            state.record_phase("TASK-002", "lint", PhaseOutcome.ERROR)
            assert len(state.phase_history("TASK-001")) == 1
            assert state.phase_history("TASK-002")[0].outcome is PhaseOutcome.ERROR


class TestRecordingNeverBreaksARun:
    """Recording is additive bookkeeping. It must not be able to fail a task."""

    def test_a_write_failure_is_swallowed(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:

            def _boom(*a, **k):
                raise RuntimeError("disk full")

            monkeypatch.setattr(state, "_insert_phase_row", _boom)
            state.record_phase("TASK-001", "tests", PhaseOutcome.PASS)  # must not raise


class TestWaiverIsAnOperatorRecord:
    def test_a_waiver_needs_an_actor_and_a_reason(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            with pytest.raises(ValueError, match="actor"):
                state.record_waiver("TASK-001", "tests", PhaseOutcome.UNEXPECTED_FAIL, "why", "")
            with pytest.raises(ValueError, match="reason"):
                state.record_waiver("TASK-001", "tests", PhaseOutcome.UNEXPECTED_FAIL, "", "ops")

    def test_a_waiver_keeps_the_observed_outcome(self, tmp_path):
        """The waived result is preserved: a report that shows green for a
        waived phase must be able to show that it was waived."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_phase("TASK-001", "tests", PhaseOutcome.UNEXPECTED_FAIL, "2 failed")
            state.record_waiver(
                "TASK-001", "tests", PhaseOutcome.UNEXPECTED_FAIL, "flaky in CI", "ops@example"
            )
            waivers = state.phase_waivers("TASK-001")

            assert len(waivers) == 1
            assert waivers[0].waived_outcome is PhaseOutcome.UNEXPECTED_FAIL
            assert waivers[0].actor == "ops@example"
            # the observed outcome stays: a waiver annotates history, not rewrites it
            assert state.phase_history("TASK-001")[0].outcome is PhaseOutcome.UNEXPECTED_FAIL

    def test_a_normal_run_never_writes_one(self, tmp_path, monkeypatch):
        """Behavioural, not a source grep: drive a task and assert the harness
        produced no waiver. Only an operator can."""
        from spec_runner import execution
        from spec_runner.task import Task

        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / "tasks.md").write_text(
            "# S\n\n## M0\n\n### TASK-001: x\n🔴 P0 | ⬜ TODO | Est: 1d\n\n"
            "**Description:** x\n\n**Checklist:**\n- [ ] a\n\n"
            "**Traces to:** [REQ-1]\n**Depends on:** —\n"
        )
        cfg = _cfg(tmp_path, run_tests_on_done=False, run_review=False, harness_guard="off")
        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
        monkeypatch.setattr(
            execution, "post_done_hook", lambda *a, **k: (True, None, "skipped", "", False)
        )
        import subprocess

        monkeypatch.setattr(
            execution.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(["x"], 0, "TASK_COMPLETE\n", ""),
        )
        task = Task(id="TASK-001", name="x", priority="p0", status="todo", estimate="1d")
        with ExecutorState(cfg) as state:
            execution.run_with_retries(task, cfg, state)
            assert state.phase_waivers("TASK-001") == []


class TestReviewConverges:
    """`ReviewVerdict` stops being a parallel enum: outcome + detail."""

    @pytest.mark.parametrize(
        "verdict,outcome,detail",
        [
            (ReviewVerdict.PASSED, PhaseOutcome.PASS, "passed"),
            (ReviewVerdict.FIXED, PhaseOutcome.PASS, "fixed"),
            (ReviewVerdict.FAILED, PhaseOutcome.UNEXPECTED_FAIL, None),
            (ReviewVerdict.NOT_RUN, PhaseOutcome.NOT_RUN, None),
            (ReviewVerdict.ERROR, PhaseOutcome.ERROR, None),
            (ReviewVerdict.SKIPPED, PhaseOutcome.SKIPPED, None),
        ],
    )
    def test_mapping(self, verdict, outcome, detail):
        assert review_verdict_to_phase(verdict) == (outcome, detail)

    def test_fixed_is_a_kind_of_pass_not_a_peer_of_it(self):
        """A consumer that only cares whether the phase held reads `outcome`
        and stops."""
        assert review_verdict_to_phase(ReviewVerdict.FIXED)[0] is PhaseOutcome.PASS
        assert review_verdict_to_phase(ReviewVerdict.PASSED)[0] is PhaseOutcome.PASS

    def test_the_stored_review_status_is_unchanged(self, tmp_path):
        """Additive migration: `attempts.review_status` keeps its wire values,
        so no consumer of the frozen contract breaks."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_attempt("TASK-001", True, 1.0, review_status=ReviewVerdict.FIXED.value)
            assert state.get_task_state("TASK-001").attempts[-1].review_status == "fixed"


class TestNoObservableChange:
    def test_a_run_reaches_the_same_terminal_state(self, tmp_path, monkeypatch):
        """The slice-0 guarantee, checked rather than asserted in prose."""
        from spec_runner import execution
        from spec_runner.task import Task

        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / "tasks.md").write_text(
            "# S\n\n## M0\n\n### TASK-001: x\n🔴 P0 | ⬜ TODO | Est: 1d\n\n"
            "**Description:** x\n\n**Checklist:**\n- [ ] a\n\n"
            "**Traces to:** [REQ-1]\n**Depends on:** —\n"
        )
        cfg = _cfg(tmp_path, run_tests_on_done=False, run_review=False, harness_guard="off")
        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
        monkeypatch.setattr(
            execution, "post_done_hook", lambda *a, **k: (True, None, "skipped", "", False)
        )
        import subprocess

        monkeypatch.setattr(
            execution.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(["x"], 0, "TASK_COMPLETE\n", ""),
        )
        task = Task(id="TASK-001", name="x", priority="p0", status="todo", estimate="1d")
        with ExecutorState(cfg) as state:
            result = execution.run_with_retries(task, cfg, state)
            assert result is True
            assert state.get_task_state("TASK-001").status == "success"
            # ...and the new records exist alongside, gating nothing.
            assert state.phase_history("TASK-001")


class TestRecordingNeverMovesTheCurrentStage:
    """`reporter.current` feeds `attempts.error_stage`, which consumers read.

    Recording an outcome must not move it. The first version of this slice
    entered `parse` in order to record its result, and a subprocess failure
    started reporting `error_stage="parse"` instead of `"exec"` — a silent
    change to a documented field, in the very slice whose guarantee is that
    external contracts do not change.
    """

    def test_record_for_leaves_current_alone(self):
        from spec_runner.stages import StageReporter

        seen: list[tuple] = []
        reporter = StageReporter("T", lambda _: None, sink=lambda *a: seen.append(a))
        reporter.enter("exec")
        reporter.record_for("parse", PhaseOutcome.NOT_RUN, "no marker")

        assert reporter.current == "exec", "recording moved the stage that error_stage reads"
        assert seen == [("parse", PhaseOutcome.NOT_RUN, "no marker")]

    def test_a_failed_exec_still_reports_error_stage_exec(self, tmp_path, monkeypatch):
        import subprocess

        from spec_runner import execution
        from spec_runner.task import Task

        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / "tasks.md").write_text(
            "# S\n\n## M0\n\n### TASK-001: x\n🔴 P0 | ⬜ TODO | Est: 1d\n\n"
            "**Description:** x\n\n**Checklist:**\n- [ ] a\n\n"
            "**Traces to:** [REQ-1]\n**Depends on:** —\n"
        )
        cfg = _cfg(tmp_path, harness_guard="off")
        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
        monkeypatch.setattr(
            execution.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(["x"], 1, "", "boom\n"),
        )
        task = Task(id="TASK-001", name="x", priority="p0", status="todo", estimate="1d")
        with ExecutorState(cfg) as state:
            execution.execute_task(task, cfg, state)
            assert state.get_task_state("TASK-001").attempts[-1].error_stage == "exec"


class TestRecordedEvidenceMatchesTheVerdict:
    """The point of the slice is an accurate record. Two ways it was not
    (Copilot, PR #167): an `exec` outcome read from the return code alone, and
    a `parse` detail that called a deliberate escalation "no completion
    marker".
    """

    @staticmethod
    def _run(tmp_path, monkeypatch, *, stdout="", returncode=0):
        import subprocess

        from spec_runner import execution
        from spec_runner.task import Task

        (tmp_path / "spec").mkdir(exist_ok=True)
        (tmp_path / "spec" / "tasks.md").write_text(
            "# S\n\n## M0\n\n### TASK-001: x\n🔴 P0 | ⬜ TODO | Est: 1d\n\n"
            "**Description:** x\n\n**Checklist:**\n- [ ] a\n\n"
            "**Traces to:** [REQ-1]\n**Depends on:** —\n"
        )
        cfg = _cfg(tmp_path, harness_guard="off", run_tests_on_done=False, run_review=False)
        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
        monkeypatch.setattr(
            execution, "post_done_hook", lambda *a, **k: (True, None, "skipped", "", False)
        )
        monkeypatch.setattr(
            execution.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(["x"], returncode, stdout, ""),
        )
        task = Task(id="TASK-001", name="x", priority="p0", status="todo", estimate="1d")
        with ExecutorState(cfg) as state:
            execution.execute_task(task, cfg, state)
            return {r.phase: r for r in state.phase_history("TASK-001")}

    def test_a_rate_limit_is_not_recorded_as_a_passing_exec(self, tmp_path, monkeypatch):
        """The API-error path aborts the attempt but arrives with exit 0."""
        rows = self._run(tmp_path, monkeypatch, stdout="rate limit exceeded\n", returncode=0)
        assert rows["exec"].outcome is PhaseOutcome.ERROR
        assert "rate limit" in rows["exec"].detail.lower()

    def test_a_clean_exec_is_still_a_pass(self, tmp_path, monkeypatch):
        rows = self._run(tmp_path, monkeypatch, stdout="TASK_COMPLETE\n")
        assert rows["exec"].outcome is PhaseOutcome.PASS

    def test_a_nonzero_exit_is_an_error_not_a_failure(self, tmp_path, monkeypatch):
        """`exec` reports on the process, not on the work: a CLI that did not
        finish is a broken instrument, the same call #138 made for review.
        Whether the *work* failed is `parse`'s to say."""
        rows = self._run(tmp_path, monkeypatch, stdout="nope\n", returncode=1)
        assert rows["exec"].outcome is PhaseOutcome.ERROR
        assert "exit 1" in rows["exec"].detail

    def test_exec_cannot_report_unexpected_fail_at_all(self):
        assert PhaseOutcome.UNEXPECTED_FAIL not in ALLOWED_OUTCOMES["exec"]

    @pytest.mark.parametrize(
        "stdout,detail",
        [
            ("TASK_BLOCKED: operator must release the claim\n", "blocked marker"),
            ("TASK_FAILED: nope\n", "failure marker"),
            ("just some prose\n", "no completion marker"),
        ],
    )
    def test_parse_detail_distinguishes_blocked_failed_and_silent(
        self, tmp_path, monkeypatch, stdout, detail
    ):
        rows = self._run(tmp_path, monkeypatch, stdout=stdout, returncode=1)
        assert rows["parse"].detail == detail
