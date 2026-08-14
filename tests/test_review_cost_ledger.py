"""#213 (first half): every review call is priced, or recorded as unpriced.

Review spend used to be recorded **nowhere** — not on the attempt row, not in
the `agent_calls` ledger. `review.py` ran a bare `subprocess.run` and threw the
result's usage away, so `spec-runner costs`, `task_budget_usd` and `budget_usd`
were all blind to it. Not a TDD regression: review has never been counted, in
any mode. A TDD attempt makes three paid calls and thereby made the hole
visible; with `review_parallel` it is one invisible call per role.

The distinction this file exists to defend is **unknown versus zero**. A
reviewer killed by an account limit was billed for as long as it ran. Writing
0.0 would make that indistinguishable from a cheap call in every later sum;
writing NULL and counting the NULLs is what lets a total be read as a floor.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.review import REVIEW_ROLES, run_code_review, run_parallel_review
from spec_runner.state import ExecutorState, ReviewVerdict
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


def _claude_json(text: str, cost: float | None = 0.42) -> MagicMock:
    payload: dict = {"result": text, "usage": {"input_tokens": 100, "output_tokens": 20}}
    if cost is not None:
        payload["total_cost_usd"] = cost
    return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")


def _calls(cfg: ExecutorConfig) -> list[dict]:
    with ExecutorState(cfg) as state:
        return state.agent_calls()


class TestOneRowPerCall:
    def test_a_review_records_its_cost_under_its_own_provenance(self, tmp_path):
        cfg = _cfg(tmp_path)

        with patch("spec_runner.review.subprocess.run", return_value=_claude_json("REVIEW_PASSED")):
            verdict, _error, _out = run_code_review(_task(), cfg)

        assert verdict is ReviewVerdict.PASSED
        rows = _calls(cfg)
        assert [r["provenance"] for r in rows] == ["review"]
        assert rows[0]["cost_usd"] == 0.42
        assert (rows[0]["input_tokens"], rows[0]["output_tokens"]) == (100, 20)

    def test_the_markers_survive_the_json_envelope(self, tmp_path):
        """Cost is only obtainable by asking the CLI for structured output.

        That moves the verdict marker out of raw stdout and into the result
        field, so the same `parse_cli_result` the RED pass uses now supplies
        both — asking for the money must not cost the verdict.
        """
        cfg = _cfg(tmp_path)

        with patch("spec_runner.review.subprocess.run") as run:
            run.return_value = _claude_json("Looks fine.\nREVIEW_PASSED")
            verdict, _error, _out = run_code_review(_task(), cfg)
            argv = run.call_args.args[0]

        assert verdict is ReviewVerdict.PASSED
        assert "--output-format" in argv and "json" in argv

    def test_each_parallel_role_gets_its_own_row(self, tmp_path):
        """Five roles are five paid calls.

        One aggregate row cannot say which role was expensive, or which one was
        never measured — and that is exactly what a per-role budget question
        needs to know.
        """
        cfg = _cfg(tmp_path, review_parallel=True, review_roles=list(REVIEW_ROLES))

        with patch("spec_runner.review.subprocess.run", return_value=_claude_json("REVIEW_PASSED")):
            run_parallel_review(_task(), cfg)

        rows = _calls(cfg)
        assert sorted(r["provenance"] for r in rows) == sorted(
            f"review:{role}" for role in REVIEW_ROLES
        )
        assert all(r["cost_usd"] == 0.42 for r in rows)


class TestUnknownIsNotZero:
    def test_a_timeout_is_recorded_with_no_cost(self, tmp_path):
        """It ran, and it was billed for as long as it ran."""
        cfg = _cfg(tmp_path)

        with patch(
            "spec_runner.review.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1),
        ):
            verdict, _error, _out = run_code_review(_task(), cfg)

        assert verdict is ReviewVerdict.NOT_RUN
        rows = _calls(cfg)
        assert len(rows) == 1
        assert rows[0]["cost_usd"] is None

    def test_an_account_limit_is_recorded(self, tmp_path):
        """The pilot's actual ending: exit 1, "You've hit your session limit".

        Money spent on a call that produced nothing usable is still spent.
        """
        cfg = _cfg(tmp_path)
        died = MagicMock(
            returncode=1, stdout="You've hit your session limit · resets 5:30pm", stderr=""
        )

        with patch("spec_runner.review.subprocess.run", return_value=died):
            verdict, _error, _out = run_code_review(_task(), cfg)

        assert verdict is ReviewVerdict.ERROR
        assert len(_calls(cfg)) == 1

    def test_a_reviewer_that_never_launched_gets_no_row(self, tmp_path):
        """No subprocess, no spend — a row would assert one."""
        cfg = _cfg(tmp_path)

        with patch(
            "spec_runner.review.subprocess.run", side_effect=OSError("no such binary: claude")
        ):
            verdict, error, _out = run_code_review(_task(), cfg)

        assert verdict is ReviewVerdict.ERROR
        # The operator still learns *which* binary was missing.
        assert "no such binary" in (error or "")
        assert _calls(cfg) == []

    def test_unpriced_calls_are_counted_not_summed(self, tmp_path):
        cfg = _cfg(tmp_path)

        with patch(
            "spec_runner.review.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1),
        ):
            run_code_review(_task(), cfg)

        with ExecutorState(cfg) as state:
            assert state.unmeasured_calls() == 1
            assert state.unmeasured_calls("TASK-001") == 1
            # Not silently added as 0.0 to a figure people read as a total.
            assert state.task_cost("TASK-001") == 0.0

    def test_a_cli_that_reports_no_cost_is_unpriced_not_free(self, tmp_path):
        """A templated CLI (codex/qwen/copilot) prints no usage at all."""
        cfg = _cfg(tmp_path, claude_command="qwen", command_template="{cmd} -p {prompt}")

        with patch(
            "spec_runner.review.subprocess.run",
            return_value=MagicMock(returncode=0, stdout="REVIEW_PASSED", stderr=""),
        ):
            verdict, _error, _out = run_code_review(_task(), cfg)

        assert verdict is ReviewVerdict.PASSED
        with ExecutorState(cfg) as state:
            assert state.unmeasured_calls() == 1


class TestAnErrorPayloadIsAnError:
    """Exit 0 with `is_error` in the JSON is how claude reports a rate limit.

    The exec path has decided on all three signals together since #167 — return
    code, `is_error`, and the API error patterns. The review path keyed on the
    return code alone, so such a reply became "no marker" → NOT_RUN: recorded
    as *the reviewer said nothing*, when the reviewer said it had failed
    (Copilot, PR #216).
    """

    def _rate_limited(self) -> MagicMock:
        payload = {
            "is_error": True,
            "subtype": "rate_limit",
            "result": "",
            "total_cost_usd": 0.11,
        }
        return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")

    def test_a_zero_exit_error_payload_is_an_error_not_a_missing_verdict(self, tmp_path):
        cfg = _cfg(tmp_path)

        with patch("spec_runner.review.subprocess.run", return_value=self._rate_limited()):
            verdict, error, _out = run_code_review(_task(), cfg)

        assert verdict is ReviewVerdict.ERROR
        assert "error" in (error or "").lower()

    def test_a_parallel_role_reads_it_the_same_way(self, tmp_path):
        cfg = _cfg(tmp_path, review_parallel=True, review_roles=["quality"])

        with patch("spec_runner.review.subprocess.run", return_value=self._rate_limited()):
            verdict, _error, _out = run_parallel_review(_task(), cfg)

        assert verdict is ReviewVerdict.ERROR

    def test_it_is_still_paid_for_and_still_recorded(self, tmp_path):
        cfg = _cfg(tmp_path)

        with patch("spec_runner.review.subprocess.run", return_value=self._rate_limited()):
            run_code_review(_task(), cfg)

        rows = _calls(cfg)
        assert len(rows) == 1
        assert rows[0]["cost_usd"] == 0.11


class TestTheLedgerNeverDecidesAnything:
    def test_a_failed_ledger_write_does_not_change_the_verdict(self, tmp_path):
        """An accounting problem must not turn "found issues" into "passed",
        nor a finished review into a failed task."""
        cfg = _cfg(tmp_path)

        with (
            patch("spec_runner.review.subprocess.run", return_value=_claude_json("REVIEW_FAILED")),
            patch(
                "spec_runner.state.ExecutorState.record_agent_call",
                side_effect=RuntimeError("disk full"),
            ),
        ):
            verdict, error, _out = run_code_review(_task(), cfg)

        assert verdict is ReviewVerdict.FAILED
        assert error == "Review found issues"

    def test_the_review_call_is_not_counted_twice(self, tmp_path):
        """Exec cost lives on the attempt row, review cost in the ledger.

        `total_cost` sums both, so a review row that also landed on an attempt
        would be counted twice — the reason the ledger holds only calls whose
        money has nowhere else to live.
        """
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_attempt("TASK-001", True, 12.0, cost_usd=1.00)

        with patch("spec_runner.review.subprocess.run", return_value=_claude_json("REVIEW_PASSED")):
            run_code_review(_task(), cfg)

        with ExecutorState(cfg) as state:
            assert state.task_cost("TASK-001") == pytest.approx(1.42)
            assert state.total_cost() == pytest.approx(1.42)
            attempts = state.tasks["TASK-001"].attempts
            assert [a.cost_usd for a in attempts] == [1.00]
