"""#218 stage 1: review-pr's own cost limit counts every paid call it makes.

`review_pr_max_cost_usd` was summed over the **fix** agents alone. A PR with
twenty bot comments makes twenty verification calls the limit never saw, so an
operator who set $5 bounded roughly half the spend — and which half depended on
how many comments turned out valid. Worse, the check ran *after* each fix, so
inside one round nothing was bounded at all.

What is pinned here is the same guarantee the task loop's guard carries (#213):
once recorded spend has reached the limit no new paid call is started, and the
maximum consecutive overshoot is one call. Its two consequences are pinned too,
because both are the kind of thing a later refactor "simplifies" away: an
unpriced call stops the next one (a floor is not a total), and a non-positive
limit disables the guard so that a CLI which never reports cost can still run.
"""

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from spec_runner import review_pr as rp
from spec_runner.review_pr import (
    EXIT_NEEDS_HUMAN,
    BotComment,
    CostGuard,
    ReviewPrState,
    cmd_review_pr,
    verify_comment,
)
from tests.test_review_pr import (  # shared fakes: gh transport + repo fixtures
    REPO,
    _args,
    _cfg,
    _comment_payload,
    _gh_router,
    _init_repo_with_remote,
    _m2_cfg,
)


def _verifier(*costs: float | None):
    """A verifier whose calls cost the given amounts, in order."""
    seq = list(costs)

    def verify(comment, repo, pr, config, **_kw):
        cost = seq.pop(0) if seq else 0.0
        return "valid", "checked", cost

    return verify


def _verdicts(cfg, pr: int = 6) -> dict[int, str | None]:
    with ReviewPrState(cfg) as state:
        return {r["comment_id"]: r["verdict"] for r in state.rows(REPO, pr)}


class TestVerificationCallsCount:
    def test_the_limit_stops_verification_it_used_not_to_see(self, tmp_path, monkeypatch):
        """Three comments, $0.60 a verification, a $1.00 limit.

        Before this fix the limit was blind to verification entirely: all
        three ran and $1.80 was spent against a $1.00 cap.
        """
        monkeypatch.setattr(
            rp,
            "_gh",
            _gh_router(comments=[_comment_payload(i) for i in (1, 2, 3)]),
        )
        cfg = _cfg(tmp_path, review_pr_max_cost_usd=1.0)

        with patch.object(rp, "verify_comment", side_effect=_verifier(0.6, 0.6, 0.6)) as verify:
            code = cmd_review_pr(_args(verify_only=True), cfg)

        assert verify.call_count == 2  # 1.20 recorded ⇒ the third never starts
        assert code == EXIT_NEEDS_HUMAN
        assert _verdicts(cfg)[3] is None  # unverified, not guessed

    def test_the_overshoot_is_one_call_not_the_whole_queue(self, tmp_path, monkeypatch):
        """Ten comments at $0.60 against $1.00: two calls, not ten.

        The bound is "one call of overshoot" — $1.20 against $1.00 — and that
        is what a guard checked *before* each call can promise.
        """
        monkeypatch.setattr(
            rp, "_gh", _gh_router(comments=[_comment_payload(i) for i in range(1, 11)])
        )
        cfg = _cfg(tmp_path, review_pr_max_cost_usd=1.0)

        with patch.object(rp, "verify_comment", side_effect=_verifier(*([0.6] * 10))) as verify:
            cmd_review_pr(_args(verify_only=True), cfg)

        assert verify.call_count == 2

    def test_verification_spend_is_charged_to_the_fix_phase_too(self, tmp_path, monkeypatch):
        """One shared guard, not one per phase.

        A verification that exhausts the limit must stop the fix agent — two
        separate counters would let each phase spend the whole cap.
        """
        work, _bare, head = _init_repo_with_remote(tmp_path)
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)], head_sha=head))
        cfg = _m2_cfg(work, review_pr_max_cost_usd=1.0)

        with (
            patch.object(rp, "verify_comment", side_effect=_verifier(1.0)),
            patch.object(rp, "run_fix_agent") as fix,
        ):
            code = cmd_review_pr(_args(), cfg)

        assert fix.call_count == 0  # nothing left to spend on the fix
        assert code == EXIT_NEEDS_HUMAN
        assert _git_head(work) == head  # no fix, no commit

    def test_a_fix_is_refused_before_it_runs_not_reverted_after(self, tmp_path, monkeypatch):
        """Two valid comments, the first fix exhausting the limit.

        The second fix must not be *made and rolled back* — it must never be
        paid for. `run_fix_agent` is called once.
        """
        work, _bare, head = _init_repo_with_remote(tmp_path)
        monkeypatch.setattr(
            rp,
            "_gh",
            _gh_router(comments=[_comment_payload(1), _comment_payload(2)], head_sha=head),
        )
        cfg = _m2_cfg(work, review_pr_max_cost_usd=1.0)

        def fix_agent(comment, evidence, repo, pr, config, **_kw):
            (work / "src.py").write_text(f"x = {comment.comment_id + 1}\n")
            return True, "fixed", 1.0

        with (
            patch.object(rp, "verify_comment", side_effect=_verifier(0.0, 0.0)),
            patch.object(rp, "run_fix_agent", side_effect=fix_agent) as fix,
        ):
            code = cmd_review_pr(_args(), cfg)

        assert fix.call_count == 1
        assert code == EXIT_NEEDS_HUMAN


class TestUnknownIsNotZero:
    def test_an_unpriced_call_stops_the_next_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            rp, "_gh", _gh_router(comments=[_comment_payload(1), _comment_payload(2)])
        )
        cfg = _cfg(tmp_path, review_pr_max_cost_usd=5.0)

        with patch.object(rp, "verify_comment", side_effect=_verifier(None, 0.1)) as verify:
            code = cmd_review_pr(_args(verify_only=True), cfg)

        assert verify.call_count == 1
        assert code == EXIT_NEEDS_HUMAN

    def test_the_refusal_says_the_spend_is_a_floor(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            rp, "_gh", _gh_router(comments=[_comment_payload(1), _comment_payload(2)])
        )
        cfg = _cfg(tmp_path, review_pr_max_cost_usd=5.0)

        with patch.object(rp, "verify_comment", side_effect=_verifier(None, 0.1)):
            cmd_review_pr(_args(verify_only=True), cfg)

        err = capsys.readouterr().err
        assert "reported no cost" in err
        assert "max_cost_usd: 0" in err  # the escape hatch is named where it is hit

    def test_a_non_positive_limit_disables_the_guard(self, tmp_path, monkeypatch):
        """The escape hatch for a CLI that never reports cost.

        Without it, one unpriced call would make every later one unprovable
        and `review-pr` would be unusable outside claude.
        """
        monkeypatch.setattr(
            rp, "_gh", _gh_router(comments=[_comment_payload(i) for i in (1, 2, 3)])
        )
        cfg = _cfg(tmp_path, review_pr_max_cost_usd=0.0)

        with patch.object(rp, "verify_comment", side_effect=_verifier(None, None, None)) as verify:
            cmd_review_pr(_args(verify_only=True), cfg)

        assert verify.call_count == 3


class TestGuardUnit:
    """The decision table on its own, away from the gh transport."""

    def test_below_the_limit_proceeds(self):
        guard = CostGuard(limit_usd=1.0, spent_usd=0.99)
        assert guard.refusal("the next call") is None

    def test_reaching_the_limit_refuses(self):
        assert CostGuard(limit_usd=1.0, spent_usd=1.0).refusal("x") is not None

    def test_the_refusal_names_the_call_that_will_not_happen(self):
        reason = CostGuard(limit_usd=1.0, spent_usd=2.0).refusal("the fix for comment 7")
        assert "the fix for comment 7" in reason

    def test_out_of_money_is_reported_before_a_missing_price(self):
        """An operator who is simply out of budget must not be sent looking
        for an unreported cost."""
        guard = CostGuard(limit_usd=1.0, spent_usd=1.0, unpriced_calls=1)
        assert "cost limit reached" in guard.refusal("x")

    def test_unknown_cost_is_counted_not_treated_as_zero(self):
        guard = CostGuard(limit_usd=5.0)
        guard.record(None)
        assert guard.spent_usd == 0.0
        assert guard.unpriced_calls == 1
        assert "cannot be proven" in guard.refusal("x")


class TestVerifierIsPriced:
    """`verify_comment` had no cost to report at all: it used
    `build_cli_command` and never parsed a result."""

    def _claude(self, text: str, cost: float | None) -> MagicMock:
        payload: dict = {"result": text, "usage": {"input_tokens": 10, "output_tokens": 2}}
        if cost is not None:
            payload["total_cost_usd"] = cost
        return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")

    def _comment(self) -> BotComment:
        return BotComment(
            comment_id=1,
            author="Copilot",
            path="src/x.py",
            line=10,
            body="Bug here",
            diff_hunk="@@ -1 +1 @@",
            url="https://example.invalid/1",
        )

    def test_it_reports_what_the_call_cost(self, tmp_path):
        cfg = _cfg(tmp_path, claude_command="claude")
        with patch.object(
            rp.subprocess,
            "run",
            return_value=self._claude("VERDICT: VALID\nEVIDENCE: read src/x.py:10", 0.42),
        ):
            verdict, evidence, cost = verify_comment(self._comment(), REPO, 6, cfg)

        assert (verdict, cost) == ("valid", 0.42)
        assert "src/x.py:10" in evidence

    def test_the_verdict_markers_survive_the_json_envelope(self, tmp_path):
        """Asking for the money must not cost the verdict (#216).

        Cost is only obtainable by asking claude for structured output, which
        moves the marker out of raw stdout into the `result` field.
        """
        cfg = _cfg(tmp_path, claude_command="claude")
        with patch.object(rp.subprocess, "run") as run:
            run.return_value = self._claude("Checked. VERDICT: REFUTED\nEVIDENCE: passes", 0.1)
            verdict, evidence, _cost = verify_comment(self._comment(), REPO, 6, cfg)
            argv = run.call_args.args[0]

        assert verdict == "refuted"
        assert "passes" in evidence
        assert "--output-format" in argv and "json" in argv

    def test_an_unreported_cost_is_none_not_zero(self, tmp_path):
        cfg = _cfg(tmp_path, claude_command="claude")
        with patch.object(
            rp.subprocess, "run", return_value=self._claude("VERDICT: VALID\nEVIDENCE: e", None)
        ):
            _verdict, _evidence, cost = verify_comment(self._comment(), REPO, 6, cfg)

        assert cost is None

    def test_a_timed_out_verifier_reports_unknown_cost(self, tmp_path):
        """It was billed for as long as it ran; 0.0 would be a lie that the
        limit then spends against."""
        cfg = _cfg(tmp_path, claude_command="claude")
        with patch.object(
            rp.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1)
        ):
            verdict, _evidence, cost = verify_comment(self._comment(), REPO, 6, cfg)

        assert verdict == "uncertain"
        assert cost is None


def _git_head(work: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True
    ).stdout.strip()
