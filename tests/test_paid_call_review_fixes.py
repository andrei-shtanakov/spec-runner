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


class TestEvidenceProvesTheOriginalBytes:
    """Finding 3: BEH-27 — full digest and size describe the unredacted text."""

    # A synthetic key in the provider shape; the redactor checks shape only.
    SECRET = "sk-ant-" + "Q" * 40

    def _sha(self, text: str) -> str:
        import hashlib

        return hashlib.sha256(text.encode()).hexdigest()

    def test_call_start_digest_is_over_the_original_prompt(self):
        from spec_runner.evidence import PolicyIdentity, call_start_for

        prompt = f"use {self.SECRET} for the call"
        record = call_start_for(
            run_id="r",
            pipeline_id=None,
            call_id="c",
            provenance="review",
            policy=PolicyIdentity(1, "h", "ns", "derived", "", ""),
            task_id="TASK-1",
            attempt=1,
            prompt=prompt,
            timestamp="t",
        )

        assert self.SECRET not in record.prompt
        assert record.prompt_full_sha256 == self._sha(prompt)
        assert record.prompt_full_size == len(prompt.encode())
        assert record.prompt_sha256 == self._sha(record.prompt)

    def test_call_result_digest_is_over_the_original_result(self):
        from spec_runner.evidence import call_result_for

        result = f"leaked {self.SECRET} here"
        record = call_result_for(
            run_id="r",
            pipeline_id=None,
            call_id="c",
            provenance="review",
            outcome="answered",
            cost_usd=None,
            returncode=0,
            result=result,
            timestamp="t",
        )

        assert self.SECRET not in record.result
        assert record.result_full_sha256 == self._sha(result)
        assert record.result_full_size == len(result.encode())

    def test_a_truncated_record_names_the_original_digest(self):
        from spec_runner.evidence import bound_evidence
        from spec_runner.redaction import redact

        original = self.SECRET + " " + "x" * 4096
        bounded = bound_evidence(redact(original), 512, original=original)

        assert bounded.truncated
        assert self._sha(original) in bounded.text
        assert self.SECRET not in bounded.text


class _SlowStore:
    """Acknowledges after `delay` seconds."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.keys: list[str] = []

    def put(self, key, payload, *, metadata):
        import time

        from spec_runner.artifact_store import Ack

        time.sleep(self.delay)
        self.keys.append(key)
        return Ack(key=key, size=len(payload))


def _result(call_id: str):
    from spec_runner.evidence import call_result_for

    return call_result_for(
        run_id="r",
        pipeline_id=None,
        call_id=call_id,
        provenance="review",
        outcome="answered",
        cost_usd=None,
        returncode=0,
        result="ok",
        timestamp="t",
    )


class TestDrain:
    """Minor of #653: `drain` ignored its timeout and raced with itself."""

    def test_drain_honours_its_timeout(self):
        import time

        from spec_runner.evidence import Publisher

        publisher = Publisher(_SlowStore(0.5), ack_timeout=30.0)
        publisher._queue.append(_result("c-1"))

        started = time.monotonic()
        assert publisher.drain(0.05) is False
        assert time.monotonic() - started < 1.0
        assert publisher.pending == 1

    def test_concurrent_drains_deliver_each_record_once(self):
        import threading

        from spec_runner.evidence import Publisher

        store = _SlowStore(0.01)
        publisher = Publisher(store, ack_timeout=5.0)
        for i in range(20):
            publisher._queue.append(_result(f"c-{i}"))
        errors: list[BaseException] = []

        def run() -> None:
            try:
                publisher.drain(5.0)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert publisher.pending == 0
        assert len(store.keys) == len(set(store.keys)) == 20


def test_review_pr_total_is_not_a_floor_because_of_planning(capsys):
    from spec_runner.cli_info import _print_pr_costs

    pr_rows = [{"repo": "o/r", "pr_number": 7, "calls": 1, "cost": 0.2, "unmeasured_calls": 0}]
    _print_pr_costs(
        pr_rows, task_cost=1.0, planning={"calls": 1, "cost": 0.0, "unmeasured_calls": 1}
    )

    out = capsys.readouterr().out
    assert "Review-PR total:      $0.20" in out
    assert "Repo total:           ≥$1.20" in out


class TestALedgerFromBeforeTheStatusColumn:
    """Round 2 of #653: the not_started filter must not hide a legacy ledger."""

    LEGACY_PR = """
        CREATE TABLE pr_agent_calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT NOT NULL,
            pr_number INTEGER NOT NULL, comment_id INTEGER NOT NULL, head_sha TEXT,
            round_number INTEGER, kind TEXT NOT NULL, provenance TEXT NOT NULL,
            outcome TEXT NOT NULL, cost_usd REAL, input_tokens INTEGER,
            output_tokens INTEGER, timestamp TEXT NOT NULL)
    """

    def test_costs_still_reads_review_pr_spend(self, tmp_path):
        import sqlite3

        from spec_runner.review_pr import pr_cost_rows

        config = _cfg(tmp_path)
        conn = sqlite3.connect(config.state_file)
        conn.execute(self.LEGACY_PR)
        conn.execute(
            "INSERT INTO pr_agent_calls (repo, pr_number, comment_id, kind, provenance, "
            "outcome, cost_usd, timestamp) VALUES ('o/r', 7, 1, 'verify', 'v', 'ok', NULL, 't')"
        )
        conn.commit()
        conn.close()

        (row,) = pr_cost_rows(config)
        assert (row["calls"], row["unmeasured_calls"]) == (1, 1)

    def test_the_filter_follows_the_column(self):
        import sqlite3

        from spec_runner.state import started_calls_only

        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE old (cost_usd REAL)")
        conn.execute("CREATE TABLE new (cost_usd REAL, status TEXT)")

        assert started_calls_only(conn, "old") == "1 = 1"
        assert "not_started" in started_calls_only(conn, "new")


def test_url_credentials_are_redacted():
    from spec_runner.redaction import redact

    text = "origin https://ci-bot:s3cretPassw0rd@git.example.com/o/r.git (fetch)"
    out = redact(text)

    assert "s3cretPassw0rd" not in out
    assert "git.example.com/o/r.git" in out
