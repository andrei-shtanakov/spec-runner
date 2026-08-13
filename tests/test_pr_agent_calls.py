"""#218 stage 2: review-pr's own calls have a ledger of their own.

Stage 1 made the loop's internal limit count both kinds of call. It still
recorded nothing, so `spec-runner costs` showed a review-pr session as free and
the money existed only inside one invocation's memory.

The owner settled the storage question: a **separate `pr_agent_calls` table**,
never a nullable `task_id` on `agent_calls`. A review-pr call belongs to a PR
comment; `costs` groups the task ledger by task, and rows belonging to no task
would have to be special-cased by every reader of that surface — including the
vendored one in spec-runner-vscode.

What this file pins, boundary by boundary:

1. the table exists, is created idempotently, and carries **no `task_id`**;
2. rows are append-only, with PR / comment / round / kind / provenance, and a
   NULL cost when the CLI reported none;
3. a call is recorded when its process **started** — including one that failed
   or timed out; a refusal that spawned nothing is not a call;
4. `costs` aggregates the two ledgers **separately** and sums them only in a
   repo total: `total_cost` remains the task total, untouched;
5. an old state file without the table still reads.
"""

import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runner import review_pr as rp
from spec_runner.review_pr import (
    BotComment,
    ReviewPrState,
    pr_cost_rows,
    run_fix_agent,
    verify_comment,
)
from tests.test_review_pr import REPO, _args, _cfg, _comment_payload, _gh_router


def _comment(cid: int = 1) -> BotComment:
    return BotComment(
        comment_id=cid,
        author="Copilot",
        path="src/x.py",
        line=10,
        body="Bug here",
        diff_hunk="@@ -1 +1 @@",
        url=f"https://example.invalid/{cid}",
    )


def _claude(text: str, cost: float | None = 0.42, returncode: int = 0) -> MagicMock:
    payload: dict = {"result": text, "usage": {"input_tokens": 100, "output_tokens": 20}}
    if cost is not None:
        payload["total_cost_usd"] = cost
    return MagicMock(returncode=returncode, stdout=json.dumps(payload), stderr="")


def _rows(cfg) -> list[dict]:
    with ReviewPrState(cfg) as state:
        return state.agent_calls()


class TestTheTableIsItsOwn:
    def test_it_has_no_task_id_column(self, tmp_path):
        """The whole reason this is a second table (#218, owner's decision)."""
        cfg = _cfg(tmp_path, claude_command="claude")
        with ReviewPrState(cfg):
            pass
        conn = sqlite3.connect(cfg.state_file)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pr_agent_calls)")}
        conn.close()

        assert "task_id" not in cols
        assert {"repo", "pr_number", "comment_id", "round_number", "kind", "provenance"} <= cols

    def test_creation_is_idempotent(self, tmp_path):
        cfg = _cfg(tmp_path, claude_command="claude")
        for _ in range(3):
            with ReviewPrState(cfg) as state:
                state.record_agent_call(REPO, 6, 1, kind="verify", outcome="completed")

        assert len(_rows(cfg)) == 3

    def test_an_old_state_file_gains_the_table(self, tmp_path):
        """A DB written before 2.31.0 has the other two review tables and not
        this one; opening it must migrate, not fail."""
        cfg = _cfg(tmp_path, claude_command="claude")
        cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(cfg.state_file)
        conn.execute("CREATE TABLE pr_review_comments (id INTEGER PRIMARY KEY, repo TEXT)")
        conn.commit()
        conn.close()

        with ReviewPrState(cfg) as state:
            state.record_agent_call(REPO, 6, 1, kind="verify", outcome="completed")

        assert len(_rows(cfg)) == 1

    def test_rows_are_append_only(self, tmp_path):
        """Two calls about the same comment are two rows. A ledger that
        overwrote the first would report the cheaper of two paid calls."""
        cfg = _cfg(tmp_path, claude_command="claude")
        with ReviewPrState(cfg) as state:
            state.record_agent_call(REPO, 6, 1, kind="verify", outcome="completed", cost_usd=0.1)
            state.record_agent_call(REPO, 6, 1, kind="fix", outcome="completed", cost_usd=0.2)

        rows = _rows(cfg)
        assert [(r["kind"], r["cost_usd"]) for r in rows] == [("verify", 0.1), ("fix", 0.2)]


class TestWhatCountsAsACall:
    def test_a_completed_verification_is_recorded_with_its_price(self, tmp_path):
        cfg = _cfg(tmp_path, claude_command="claude")
        with (
            ReviewPrState(cfg) as state,
            patch.object(
                rp.subprocess, "run", return_value=_claude("VERDICT: VALID\nEVIDENCE: e", 0.42)
            ),
        ):
            verify_comment(_comment(), REPO, 6, cfg, ledger=state, head_sha="abc123")

        row = _rows(cfg)[0]
        assert (row["kind"], row["provenance"], row["outcome"]) == (
            "verify",
            "review_pr:verify",
            "completed",
        )
        assert (row["cost_usd"], row["input_tokens"], row["output_tokens"]) == (0.42, 100, 20)
        assert (row["repo"], row["pr_number"], row["comment_id"]) == (REPO, 6, 1)
        assert row["head_sha"] == "abc123"

    def test_a_timed_out_verification_is_a_row_with_an_unknown_price(self, tmp_path):
        """It ran, so it was billed. Recording nothing would make the ledger
        agree with a session that never happened."""
        cfg = _cfg(tmp_path, claude_command="claude")
        with (
            ReviewPrState(cfg) as state,
            patch.object(
                rp.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)
            ),
        ):
            verify_comment(_comment(), REPO, 6, cfg, ledger=state)

        row = _rows(cfg)[0]
        assert (row["outcome"], row["cost_usd"]) == ("timeout", None)

    def test_a_failed_verification_is_recorded_as_error(self, tmp_path):
        cfg = _cfg(tmp_path, claude_command="claude")
        broken = MagicMock(returncode=1, stdout="", stderr="boom")
        with ReviewPrState(cfg) as state, patch.object(rp.subprocess, "run", return_value=broken):
            verify_comment(_comment(), REPO, 6, cfg, ledger=state)

        assert _rows(cfg)[0]["outcome"] == "error"

    def test_a_nonzero_exit_is_an_error_even_when_it_printed_something(self, tmp_path):
        """The ledger reports what the process did. A CLI that died after
        emitting partial output did not complete, and recording it as
        `completed` would under-report failures and disagree with the fix
        agent's own rule (Copilot, PR #240)."""
        cfg = _cfg(tmp_path, claude_command="claude")
        partial = _claude("VERDICT: VALID\nEVIDENCE: half an answer", 0.2, returncode=1)
        with ReviewPrState(cfg) as state, patch.object(rp.subprocess, "run", return_value=partial):
            verify_comment(_comment(), REPO, 6, cfg, ledger=state)

        row = _rows(cfg)[0]
        assert row["outcome"] == "error"
        assert row["cost_usd"] == 0.2, "it still cost what it cost"

    def test_a_fix_call_is_recorded_under_its_own_kind(self, tmp_path):
        cfg = _cfg(tmp_path, claude_command="claude")
        with (
            ReviewPrState(cfg) as state,
            patch.object(rp.subprocess, "run", return_value=_claude("FIX_COMPLETE: done", 0.7)),
        ):
            run_fix_agent(_comment(), "evidence", REPO, 6, cfg, ledger=state)

        row = _rows(cfg)[0]
        assert (row["kind"], row["provenance"], row["cost_usd"]) == ("fix", "review_pr:fix", 0.7)

    def test_an_unreported_cost_is_null_not_zero(self, tmp_path):
        cfg = _cfg(tmp_path, claude_command="claude")
        with (
            ReviewPrState(cfg) as state,
            patch.object(
                rp.subprocess, "run", return_value=_claude("VERDICT: VALID\nEVIDENCE: e", None)
            ),
        ):
            verify_comment(_comment(), REPO, 6, cfg, ledger=state)

        assert _rows(cfg)[0]["cost_usd"] is None

    def test_a_ledger_failure_does_not_break_the_loop(self, tmp_path):
        """A verdict must not be lost because a write failed. Same rule the
        task ledger follows."""
        cfg = _cfg(tmp_path, claude_command="claude")
        with ReviewPrState(cfg) as state:
            state._conn.close()  # the ledger is now unusable
            with patch.object(
                rp.subprocess, "run", return_value=_claude("VERDICT: REFUTED\nEVIDENCE: e", 0.1)
            ):
                verdict, _evidence, cost = verify_comment(_comment(), REPO, 6, cfg, ledger=state)

        assert (verdict, cost) == ("refuted", 0.1)


@pytest.mark.slow
class TestThroughTheLoop:
    def test_a_refused_call_leaves_no_row(self, tmp_path, monkeypatch):
        """The cost guard stops the call *before* the process starts (#218
        stage 1). A row for it would be a charge for work never done."""
        monkeypatch.setattr(
            rp, "_gh", _gh_router(comments=[_comment_payload(1), _comment_payload(2)])
        )
        cfg = _cfg(tmp_path, review_pr_max_cost_usd=0.5, claude_command="claude")

        with patch.object(
            rp.subprocess, "run", return_value=_claude("VERDICT: VALID\nEVIDENCE: e", 0.6)
        ):
            rp.cmd_review_pr(_args(verify_only=True), cfg)

        rows = _rows(cfg)
        assert len(rows) == 1, "the second verification was refused, not made"
        assert rows[0]["comment_id"] == 1

    def test_the_round_is_recorded_when_there_is_one(self, tmp_path):
        """Verification runs before any round is started, so its honest round
        is NULL; a fix always has one."""
        cfg = _cfg(tmp_path, claude_command="claude")
        with ReviewPrState(cfg) as state:
            state.record_agent_call(REPO, 6, 1, kind="verify", outcome="completed")
            state.start_round(REPO, 6, "abc123")
            state.record_agent_call(REPO, 6, 1, kind="fix", outcome="completed")

        rows = _rows(cfg)
        assert rows[0]["round_number"] is None
        assert rows[1]["round_number"] == 1

    def test_a_verification_after_an_earlier_round_still_has_none(self, tmp_path):
        """A later invocation verifies new comments *before* opening its round.
        Reading the current count there would bill the verification to the
        previous invocation's round — one it took no part in (Copilot, #240)."""
        cfg = _cfg(tmp_path, claude_command="claude")
        with ReviewPrState(cfg) as state:
            state.start_round(REPO, 6, "sha-round-1")
            state.record_agent_call(REPO, 6, 2, kind="verify", outcome="completed")

        assert _rows(cfg)[-1]["round_number"] is None


class TestCostsKeepsTheLedgersApart:
    def _costs_json(self, cfg, capsys) -> dict:
        from argparse import Namespace

        from spec_runner.cli_info import cmd_costs

        cmd_costs(Namespace(json=True, sort="id"), cfg)
        return json.loads(capsys.readouterr().out)

    def test_task_cost_is_not_contaminated(self, tmp_path, capsys):
        """The boundary the owner drew: PR spend is reported, never folded
        into a task's number."""
        cfg = _cfg(tmp_path, claude_command="claude")
        cfg.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.tasks_file.write_text("### TASK-001: t\n🟠 P1 | ⬜ TODO | Est: 1d\n")
        with ReviewPrState(cfg) as state:
            state.record_agent_call(REPO, 6, 1, kind="verify", outcome="completed", cost_usd=1.5)

        payload = self._costs_json(cfg, capsys)

        assert payload["summary"]["total_cost"] == 0.0, "no task spent anything"
        assert payload["summary"]["pr_review_cost"] == 1.5
        assert payload["summary"]["repo_total_cost"] == 1.5
        assert payload["tasks"][0]["cost"] == 0.0
        assert payload["pr_reviews"][0]["pr_number"] == 6

    def test_the_repo_total_is_the_sum_of_both(self, tmp_path, capsys):
        cfg = _cfg(tmp_path, claude_command="claude")
        cfg.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.tasks_file.write_text("### TASK-001: t\n🟠 P1 | ⬜ TODO | Est: 1d\n")
        from spec_runner.state import ExecutorState

        with ExecutorState(cfg) as state:
            state.record_agent_call("TASK-001", "green", cost_usd=2.0)
        with ReviewPrState(cfg) as state:
            state.record_agent_call(REPO, 6, 1, kind="fix", outcome="completed", cost_usd=0.5)

        payload = self._costs_json(cfg, capsys)

        assert payload["summary"]["total_cost"] == 2.0
        assert payload["summary"]["pr_review_cost"] == 0.5
        assert payload["summary"]["repo_total_cost"] == 2.5

    def test_nothing_new_appears_without_pr_spend(self, tmp_path, capsys):
        """A project that never runs review-pr sees the surface it always saw."""
        cfg = _cfg(tmp_path, claude_command="claude")
        cfg.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.tasks_file.write_text("### TASK-001: t\n🟠 P1 | ⬜ TODO | Est: 1d\n")

        payload = self._costs_json(cfg, capsys)

        assert "pr_reviews" not in payload
        for key in ("pr_review_cost", "repo_total_cost", "pr_ledger_since"):
            assert key not in payload["summary"]

    def test_an_unpriced_pr_call_makes_the_total_a_floor(self, tmp_path, capsys):
        cfg = _cfg(tmp_path, claude_command="claude")
        cfg.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.tasks_file.write_text("### TASK-001: t\n🟠 P1 | ⬜ TODO | Est: 1d\n")
        with ReviewPrState(cfg) as state:
            state.record_agent_call(REPO, 6, 1, kind="verify", outcome="timeout")

        payload = self._costs_json(cfg, capsys)

        assert payload["summary"]["pr_review_unmeasured_calls"] == 1
        assert payload["pr_reviews"][0]["unmeasured_calls"] == 1

    def test_the_json_matches_the_published_schema(self, tmp_path, capsys):
        import jsonschema

        cfg = _cfg(tmp_path, claude_command="claude")
        cfg.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.tasks_file.write_text("### TASK-001: t\n🟠 P1 | ⬜ TODO | Est: 1d\n")
        with ReviewPrState(cfg) as state:
            state.record_agent_call(REPO, 6, 1, kind="verify", outcome="completed", cost_usd=0.3)

        payload = self._costs_json(cfg, capsys)
        schema = json.loads(Path("schemas/costs.schema.json").read_text())
        jsonschema.validate(payload, schema)

    def test_a_state_file_without_the_table_reads_cleanly(self, tmp_path):
        """`costs` must never break over a ledger it does not own."""
        cfg = _cfg(tmp_path, claude_command="claude")
        cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(cfg.state_file).close()

        assert pr_cost_rows(cfg) == []

    def test_the_text_output_says_the_history_is_incomplete(self, tmp_path, capsys):
        """Review-pr spend was recorded nowhere before this version. A reader
        must not take an old repo's small total for a cheap one."""
        from argparse import Namespace

        from spec_runner.cli_info import cmd_costs

        cfg = _cfg(tmp_path, claude_command="claude")
        cfg.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.tasks_file.write_text("### TASK-001: t\n🟠 P1 | ⬜ TODO | Est: 1d\n")
        with ReviewPrState(cfg) as state:
            state.record_agent_call(REPO, 6, 1, kind="verify", outcome="completed", cost_usd=0.3)

        cmd_costs(Namespace(json=False, sort="id"), cfg)
        out = capsys.readouterr().out

        assert "Review-PR sessions" in out
        assert "Repo total" in out
        assert "were not recorded at all" in out
