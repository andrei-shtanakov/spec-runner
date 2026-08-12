"""#213 (second half): the pre-call budget guard.

A **guard, not a cap**, and the tests are written against that wording. It
cannot stop the call that crosses the line — a call's cost is known only once
it returns — so what it guarantees is narrower and checkable:

    Once recorded spend has reached the limit, no new paid call is started;
    the maximum consecutive overshoot is bounded by one call.

Everything here defends one of the three consequences: the guard sits at each
of the three paid calls a TDD attempt makes, parallel review is serialised
while a cap is set (five roles past one check would make the sentence false),
and an unpriced call fails the guard closed, because a remainder computed from
a floor is not a remainder.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runner.budget import (
    RUN_BUDGET,
    TASK_BUDGET,
    UNPRICED,
    budget_is_active,
    check_before_call,
)
from spec_runner.config import ExecutorConfig
from spec_runner.review import REVIEW_ROLES, run_code_review, run_parallel_review
from spec_runner.state import ErrorCode, ExecutorState, ReviewVerdict
from spec_runner.task import Task


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / ".state.db",
        "logs_dir": tmp_path / ".logs",
        "claude_command": "claude",
        "create_git_branch": False,
        "auto_commit": False,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task() -> Task:
    return Task(id="TASK-001", name="Thing", priority="p1", status="todo", estimate="1d")


def _spend(cfg: ExecutorConfig, cost: float | None, task_id: str = "TASK-001") -> None:
    with ExecutorState(cfg) as state:
        state.record_agent_call(task_id, "review", cost_usd=cost)


def _priced(text: str = "REVIEW_PASSED", cost: float = 0.10) -> MagicMock:
    import json

    payload = {"result": text, "total_cost_usd": cost, "usage": {}}
    return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")


class TestDormantWithoutACap:
    def test_no_budget_means_no_guard(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert budget_is_active(cfg) is False
        with ExecutorState(cfg) as state:
            assert check_before_call(cfg, state, "TASK-001", "green") is None

    def test_an_unpriced_call_blocks_nothing_when_no_cap_is_set(self, tmp_path):
        """Unprovable remainders only matter when something depends on them."""
        cfg = _cfg(tmp_path)
        _spend(cfg, None)

        with patch("spec_runner.review.subprocess.run", return_value=_priced()) as run:
            verdict, _error, _out = run_code_review(_task(), cfg)

        assert verdict is ReviewVerdict.PASSED
        run.assert_called_once()

    def test_review_stays_parallel_without_a_cap(self, tmp_path):
        cfg = _cfg(tmp_path, review_parallel=True, review_roles=list(REVIEW_ROLES))

        with patch("spec_runner.review.ThreadPoolExecutor") as pool:
            pool.side_effect = RuntimeError("pool was used")
            with pytest.raises(RuntimeError, match="pool was used"):
                run_parallel_review(_task(), cfg)


class TestTheThreeCallSites:
    def test_the_task_cap_refuses_the_named_call(self, tmp_path):
        cfg = _cfg(tmp_path, task_budget_usd=1.0)
        _spend(cfg, 1.0)

        with ExecutorState(cfg) as state:
            refusal = check_before_call(cfg, state, "TASK-001", "red_authoring")

        assert refusal is not None
        assert refusal.kind == TASK_BUDGET
        # Which call did not happen is the first thing a resume needs.
        assert "red_authoring" in refusal.reason

    def test_the_run_cap_refuses_too(self, tmp_path):
        cfg = _cfg(tmp_path, budget_usd=1.0)
        _spend(cfg, 0.60, task_id="TASK-001")
        _spend(cfg, 0.60, task_id="TASK-002")

        with ExecutorState(cfg) as state:
            refusal = check_before_call(cfg, state, "TASK-001", "green")

        assert refusal is not None and refusal.kind == RUN_BUDGET

    def test_spend_below_the_cap_proceeds(self, tmp_path):
        cfg = _cfg(tmp_path, task_budget_usd=1.0)
        _spend(cfg, 0.99)

        with ExecutorState(cfg) as state:
            assert check_before_call(cfg, state, "TASK-001", "green") is None

    def test_the_review_call_is_refused_and_never_launched(self, tmp_path):
        cfg = _cfg(tmp_path, task_budget_usd=1.0)
        _spend(cfg, 1.0)

        with patch("spec_runner.review.subprocess.run") as run:
            verdict, error, _out = run_code_review(_task(), cfg)

        run.assert_not_called()
        # NOT_RUN, never SKIPPED: `skipped` is a policy decision, and under
        # `advisory` the absence of a review must not read as one that passed.
        assert verdict is ReviewVerdict.NOT_RUN
        assert "budget" in (error or "").lower()

    def test_the_refused_review_writes_no_ledger_row(self, tmp_path):
        """No call, no cost, no row — the row would claim a call was made."""
        cfg = _cfg(tmp_path, task_budget_usd=1.0)
        _spend(cfg, 1.0)

        with patch("spec_runner.review.subprocess.run"):
            run_code_review(_task(), cfg)

        with ExecutorState(cfg) as state:
            assert len(state.agent_calls("TASK-001")) == 1  # only the seeded one


class TestUnprovableRemainder:
    def test_an_unpriced_call_blocks_the_next_paid_call(self, tmp_path):
        cfg = _cfg(tmp_path, task_budget_usd=10.0)
        _spend(cfg, None)

        with ExecutorState(cfg) as state:
            refusal = check_before_call(cfg, state, "TASK-001", "green")

        assert refusal is not None
        assert refusal.kind == UNPRICED
        assert "floor" in refusal.reason

    def test_being_out_of_money_is_reported_before_being_unable_to_prove_it(self, tmp_path):
        """Both are true; the operator needs the actionable one.

        "Raise the cap" and "find out what that call cost" send someone to
        different places, and a cap that is genuinely exhausted is the simpler
        fact.
        """
        cfg = _cfg(tmp_path, task_budget_usd=1.0)
        _spend(cfg, 1.0)
        _spend(cfg, None)

        with ExecutorState(cfg) as state:
            refusal = check_before_call(cfg, state, "TASK-001", "green")

        assert refusal is not None and refusal.kind == TASK_BUDGET

    def test_a_task_cap_ignores_another_tasks_missing_price(self, tmp_path):
        """A per-task cap is provable from that task's own calls.

        Freezing every task in the run because one of them has an unpriced
        call would be fail-closed against a question nobody asked.
        """
        cfg = _cfg(tmp_path, task_budget_usd=10.0)
        _spend(cfg, None, task_id="TASK-002")

        with ExecutorState(cfg) as state:
            assert check_before_call(cfg, state, "TASK-001", "green") is None

    def test_a_run_cap_does_not_ignore_it(self, tmp_path):
        cfg = _cfg(tmp_path, budget_usd=10.0)
        _spend(cfg, None, task_id="TASK-002")

        with ExecutorState(cfg) as state:
            refusal = check_before_call(cfg, state, "TASK-001", "green")

        assert refusal is not None and refusal.kind == UNPRICED

    def test_a_cli_that_never_reports_cost_cannot_be_run_under_a_cap(self, tmp_path):
        """The honest consequence, stated as a test rather than discovered.

        A templated CLI reports no usage, so its first call is unpriced and
        every later one is refused. You cannot enforce a budget you cannot
        measure; the tool says so instead of pretending.
        """
        cfg = _cfg(
            tmp_path,
            task_budget_usd=10.0,
            claude_command="qwen",
            command_template="{cmd} -p {prompt}",
        )
        plain = MagicMock(returncode=0, stdout="REVIEW_PASSED", stderr="")

        with patch("spec_runner.review.subprocess.run", return_value=plain) as run:
            first, _e, _o = run_code_review(_task(), cfg)
            second, error, _o = run_code_review(_task(), cfg)

        assert first is ReviewVerdict.PASSED
        assert run.call_count == 1
        assert second is ReviewVerdict.NOT_RUN
        assert "cannot be proven" in (error or "")


class TestParallelReviewIsSerialisedUnderACap:
    def test_five_roles_run_one_at_a_time(self, tmp_path):
        """Five calls in flight past one check would make the guarantee false."""
        cfg = _cfg(
            tmp_path,
            review_parallel=True,
            review_roles=list(REVIEW_ROLES),
            task_budget_usd=10.0,
        )

        with patch("spec_runner.review.ThreadPoolExecutor") as pool:
            pool.side_effect = AssertionError("must not run roles concurrently under a budget")
            with patch("spec_runner.review.subprocess.run", return_value=_priced()) as run:
                verdict, _error, _out = run_parallel_review(_task(), cfg)

        assert verdict is ReviewVerdict.PASSED
        assert run.call_count == len(REVIEW_ROLES)

    def test_the_roles_stop_as_soon_as_the_cap_is_reached(self, tmp_path):
        """The overshoot is one call, not five.

        Each role costs $0.40 against a $1.00 cap: two run, the third finds
        $0.80 recorded, the fourth would have found $1.20. Sequentially the
        run stops at three; in parallel all five would have been in flight.
        """
        cfg = _cfg(
            tmp_path,
            review_parallel=True,
            review_roles=list(REVIEW_ROLES),
            task_budget_usd=1.0,
        )

        with patch("spec_runner.review.subprocess.run", return_value=_priced(cost=0.40)) as run:
            run_parallel_review(_task(), cfg)

        assert run.call_count == 3
        with ExecutorState(cfg) as state:
            assert state.task_cost("TASK-001") == pytest.approx(1.20)

    def test_a_refused_role_does_not_become_a_passing_role(self, tmp_path):
        cfg = _cfg(
            tmp_path,
            review_parallel=True,
            review_roles=list(REVIEW_ROLES),
            task_budget_usd=1.0,
        )
        _spend(cfg, 1.0)

        with patch("spec_runner.review.subprocess.run") as run:
            verdict, _error, _out = run_parallel_review(_task(), cfg)

        run.assert_not_called()
        assert verdict is ReviewVerdict.NOT_RUN


class TestTheExecutionSites:
    """RED and GREEN, through `execute_task` rather than in isolation."""

    def _repo(self, tmp_path: Path) -> Path:
        root = tmp_path / "repo"
        root.mkdir()
        for args in (
            ("init", "-q"),
            ("config", "user.email", "t@example.com"),
            ("config", "user.name", "t"),
        ):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
        (root / "README.md").write_text("x\n")
        spec = root / "spec"
        spec.mkdir()
        (spec / "tasks.md").write_text(
            "# Tasks\n\n### TASK-001: Thing\n🟠 P1 | ⬜ TODO | Est: 1d\n\n"
        )
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True, capture_output=True)
        return root

    def test_the_green_call_is_refused_and_the_attempt_says_why(self, tmp_path):
        from spec_runner.execution import execute_task

        cfg = _cfg(self._repo(tmp_path), task_budget_usd=1.0, run_tests_on_done=False)
        _spend(cfg, 1.0)

        with ExecutorState(cfg) as state:
            with patch("spec_runner.execution.subprocess.run") as run:
                ok = execute_task(_task(), cfg, state)
            attempts = state.tasks["TASK-001"].attempts

        assert ok is False
        run.assert_not_called()
        assert attempts[-1].error_code is ErrorCode.BUDGET_EXCEEDED
        assert "green" in (attempts[-1].error or "")

    def test_the_red_call_is_refused_before_it_is_authored(self, tmp_path):
        from spec_runner.execution import execute_task

        cfg = _cfg(
            self._repo(tmp_path),
            task_budget_usd=1.0,
            execution_mode="tdd",
            tdd_runner="pytest",
            test_command="pytest",
            run_tests_on_done=False,
        )
        _spend(cfg, 1.0)

        with ExecutorState(cfg) as state:
            with patch("spec_runner.tdd._run_agent") as agent:
                ok = execute_task(_task(), cfg, state)
            attempts = state.tasks["TASK-001"].attempts

        assert ok is False
        agent.assert_not_called()
        assert attempts[-1].error_code is ErrorCode.BUDGET_EXCEEDED
        assert "red_authoring" in (attempts[-1].error or "")
        # A budget stop is not an instrument error: recording it as one would
        # send the gate's bounded recovery back to spend the money it does not
        # have.
        assert attempts[-1].error_code is not ErrorCode.INFRASTRUCTURE


class TestTheGuardFailsClosedWhenItCannotRead:
    """ "We do not know" is not a reason to spend (Copilot, PR #217).

    The first version logged and proceeded, reasoning that refusing on a broken
    reader stops work for a reason that may not be true. But an unreadable
    ledger is the extreme case of the unprovable remainder the guard already
    refuses on — and it is the one situation where nothing at all is counting,
    so every remaining call would go through.
    """

    def test_an_unreadable_ledger_refuses_the_review_call(self, tmp_path):
        from spec_runner.budget import UNREADABLE

        cfg = _cfg(tmp_path, task_budget_usd=10.0)

        with (
            patch(
                "spec_runner.state.ExecutorState.__init__", side_effect=RuntimeError("db is gone")
            ),
            patch("spec_runner.review.subprocess.run") as run,
        ):
            verdict, error, _out = run_code_review(_task(), cfg)

        run.assert_not_called()
        assert verdict is ReviewVerdict.NOT_RUN
        assert "cannot be proven" in (error or "")
        assert UNREADABLE  # the kind exists and is distinct from UNPRICED

    def test_it_still_does_nothing_when_no_cap_is_set(self, tmp_path):
        """A broken reader only matters to a question someone is asking."""
        cfg = _cfg(tmp_path)

        with (
            patch(
                "spec_runner.state.ExecutorState.__init__", side_effect=RuntimeError("db is gone")
            ),
            patch("spec_runner.review.subprocess.run", return_value=_priced()) as run,
        ):
            verdict, _error, _out = run_code_review(_task(), cfg)

        run.assert_called_once()
        assert verdict is ReviewVerdict.PASSED


class TestTheFloorIsQuotedInTheBindingScope:
    def test_a_run_cap_quotes_the_run_total(self, tmp_path):
        """The task's own total says nothing about how much of the run is left."""
        cfg = _cfg(tmp_path, budget_usd=10.0)
        _spend(cfg, 3.0, task_id="TASK-002")
        _spend(cfg, None, task_id="TASK-001")

        with ExecutorState(cfg) as state:
            refusal = check_before_call(cfg, state, "TASK-001", "green")

        assert refusal is not None
        assert "$3.00 for this run" in refusal.reason

    def test_a_task_cap_quotes_the_task_total(self, tmp_path):
        cfg = _cfg(tmp_path, task_budget_usd=10.0)
        _spend(cfg, 2.0)
        _spend(cfg, None)

        with ExecutorState(cfg) as state:
            refusal = check_before_call(cfg, state, "TASK-001", "green")

        assert refusal is not None
        assert "$2.00 for this task" in refusal.reason


class TestSpendThatHappenedButIsNotRecordedYet:
    """The free budget rehearsal's finding, at unit scale.

    `record_attempt` writes the implementation call's cost only *after*
    `post_done_hook` returns, so a guard reading the database alone sees the
    task's spend as it was before this attempt's most expensive call. In the
    rehearsal that meant $0.60 recorded against a $1.00 cap, review allowed,
    and $1.80 spent — the guarantee broken by the one call it was written for.
    """

    def test_the_pending_call_counts_against_the_cap(self, tmp_path):
        cfg = _cfg(tmp_path, task_budget_usd=1.0)
        _spend(cfg, 0.60)  # RED, recorded

        with ExecutorState(cfg) as state:
            # Without the pending amount this is $0.60 < $1.00 and proceeds.
            assert check_before_call(cfg, state, "TASK-001", "review") is None
            refusal = check_before_call(cfg, state, "TASK-001", "review", pending_cost=0.60)

        assert refusal is not None and refusal.kind == TASK_BUDGET
        assert "$1.20" in refusal.reason

    def test_it_counts_against_the_run_cap_too(self, tmp_path):
        cfg = _cfg(tmp_path, budget_usd=1.0)
        _spend(cfg, 0.60, task_id="TASK-002")

        with ExecutorState(cfg) as state:
            refusal = check_before_call(cfg, state, "TASK-001", "review", pending_cost=0.60)

        assert refusal is not None and refusal.kind == RUN_BUDGET

    def test_an_unknown_pending_cost_is_unprovable_not_free(self, tmp_path):
        """The call happened; nobody knows what it cost. That is not $0.00."""
        cfg = _cfg(tmp_path, task_budget_usd=10.0)

        with ExecutorState(cfg) as state:
            refusal = check_before_call(cfg, state, "TASK-001", "review", pending_cost=None)

        assert refusal is not None and refusal.kind == UNPRICED

    def test_review_is_refused_through_the_hook(self, tmp_path):
        """End to end through `post_done_hook`, the way the rehearsal ran it."""
        from spec_runner.hooks import post_done_hook

        cfg = _cfg(
            tmp_path,
            task_budget_usd=1.0,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=True,
        )
        _spend(cfg, 0.60)

        with patch("spec_runner.review.subprocess.run") as run:
            _ok, _err, verdict, _findings, _no_op = post_done_hook(
                _task(), cfg, True, pending_cost=0.60
            )

        run.assert_not_called()
        assert verdict == "not_run"
