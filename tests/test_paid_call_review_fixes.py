"""Regressions for the review of #653 (TASK-002 of #480).

TASK-002 passed its own review on a one-line diff (review read `HEAD~1`
only); these pin what the first real review found.
"""

from __future__ import annotations

from pathlib import Path

from spec_runner import paid_call
from spec_runner.config import ExecutorConfig
from spec_runner.runner import CliResult
from spec_runner.state import ExecutorState


def _cfg(tmp_path: Path) -> ExecutorConfig:
    return ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "state.db")


def _outcome(call_id: str, parsed: CliResult | None) -> paid_call.CallOutcome:
    return paid_call.CallOutcome(
        call_id=call_id,
        stdout="",
        stderr="",
        returncode=0,
        timed_out=False,
        parsed=parsed,
        outcome="answered",
        cost_usd=getattr(parsed, "cost_usd", None),
    )


class TestGreenTokensCountOnce:
    """Finding 1: GREEN's ledger row repeated the tokens its attempt row holds."""

    def test_a_call_priced_on_its_attempt_adds_no_tokens_to_the_ledger(self, tmp_path):
        config = _cfg(tmp_path)
        with ExecutorState(config) as state:
            ledger = paid_call.task_ledger(config, state, "TASK-1", "green", price_on_attempt=True)
            ledger.open("c-1", None, "2026-10-02T00:00:00")
            ledger.close("c-1", "closed", _outcome("c-1", CliResult("ok", 8000, 2000, 0.5)))
            state.record_attempt(
                "TASK-1", success=True, duration=1.0, input_tokens=8000, output_tokens=2000
            )

            assert state.task_tokens("TASK-1") == (8000, 2000)
            assert state.total_tokens() == (8000, 2000)

    def test_any_other_call_still_carries_its_own_usage(self, tmp_path):
        config = _cfg(tmp_path)
        with ExecutorState(config) as state:
            ledger = paid_call.task_ledger(config, state, "TASK-1", "review")
            ledger.open("c-2", None, "2026-10-02T00:00:00")
            ledger.close("c-2", "closed", _outcome("c-2", CliResult("ok", 300, 40, 0.1)))

            assert state.task_tokens("TASK-1") == (300, 40)
            assert state.task_cost("TASK-1") == 0.1


class TestANeverStartedCallIsNotUnpriced:
    """Finding 2 (+ minor): a `not_started` row spent nothing."""

    def test_a_refused_start_does_not_freeze_the_budget(self, tmp_path):
        from spec_runner.budget import check_before_call

        config = _cfg(tmp_path)
        config.task_budget_usd = 5.0
        with ExecutorState(config) as state:
            ledger = paid_call.task_ledger(config, state, "TASK-1", "review")
            ledger.open("c-3", None, "2026-10-02T00:00:00")
            ledger.close("c-3", "not_started", None)

            assert state.unmeasured_calls() == 0
            assert check_before_call(config, state, "TASK-1", "review", 0.0) is None

    def test_an_open_row_still_counts_as_unpriced(self, tmp_path):
        """A call that started and never closed may have spent: fail closed."""
        config = _cfg(tmp_path)
        with ExecutorState(config) as state:
            paid_call.task_ledger(config, state, "TASK-1", "review").open(
                "c-4", None, "2026-10-02T00:00:00"
            )

            assert state.unmeasured_calls() == 1

    def test_review_pr_readers_skip_never_started_calls(self, tmp_path):
        from spec_runner.review_pr import ReviewPrState, pr_cost_rows

        config = _cfg(tmp_path)
        pr = ReviewPrState(config)
        for call_id, status in (("p-1", "not_started"), ("p-2", "closed")):
            pr.open_agent_call(
                "o/r",
                7,
                1,
                kind="verify",
                head_sha=None,
                run_id=None,
                call_id=call_id,
                started_at="2026-10-02T00:00:00",
            )
            pr.close_call_row(call_id, status=status, cost_usd=0.2 if status == "closed" else None)

        assert [r["cost_usd"] for r in pr.agent_calls()] == [0.2]
        (row,) = pr_cost_rows(config)
        assert (row["calls"], row["unmeasured_calls"]) == (1, 0)
