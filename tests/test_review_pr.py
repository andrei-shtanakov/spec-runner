"""`spec-runner review-pr` M1 — read-only collect + verify + report (#102).

Design: docs/superpowers/specs/2026-08-06-review-pr-loop-design.md.
GitHub transport is mocked at the `_gh` seam; the verifier at
`verify_comment`.
"""

import argparse
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from spec_runner import review_pr as rp
from spec_runner.config import ExecutorConfig
from spec_runner.review_pr import (
    EXIT_FAIL,
    EXIT_NEEDS_HUMAN,
    EXIT_OK,
    BotComment,
    ReviewPrError,
    ReviewPrState,
    cmd_review_pr,
    fetch_bot_comments,
    parse_pr_ref,
    parse_verdict,
)

REPO = "owner/repo"


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "state.db",
        "logs_dir": tmp_path / "logs",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _args(**overrides) -> argparse.Namespace:
    base: dict = {"pr_ref": "6", "json_output": False, "no_verify": False}
    base.update(overrides)
    return argparse.Namespace(**base)


def _proc(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _comment_payload(comment_id: int, author: str = "Copilot", body: str = "Bug here") -> dict:
    return {
        "id": comment_id,
        "user": {"login": author},
        "path": "src/x.py",
        "line": 10,
        "body": body,
        "diff_hunk": "@@ -1 +1 @@",
        "html_url": f"https://github.com/{REPO}/pull/6#discussion_r{comment_id}",
    }


def _gh_router(pr_state: str = "open", draft: bool = False, comments: list | None = None):
    """Build a fake `_gh` answering the meta and comments endpoints."""

    def fake_gh(config, *args):
        joined = " ".join(args)
        if joined.startswith("api repos/") and joined.endswith("/comments --paginate"):
            return _proc(stdout=json.dumps(comments or []))
        if joined.startswith("api repos/"):
            return _proc(
                stdout=json.dumps({"state": pr_state, "draft": draft, "head": {"sha": "abc123"}})
            )
        if joined.startswith("repo view"):
            return _proc(stdout=REPO + "\n")
        raise AssertionError(f"unexpected gh call: {joined}")

    return fake_gh


class TestParsePrRef:
    def test_url(self, tmp_path):
        assert parse_pr_ref("https://github.com/o/r/pull/42", _cfg(tmp_path)) == ("o/r", 42)

    def test_bare_number_resolves_repo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rp, "_gh", _gh_router())
        assert parse_pr_ref("6", _cfg(tmp_path)) == (REPO, 6)

    def test_garbage_fails_closed(self, tmp_path):
        with pytest.raises(ReviewPrError):
            parse_pr_ref("not-a-pr", _cfg(tmp_path))

    def test_bare_number_without_repo_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rp, "_gh", lambda *a: _proc(returncode=1, stderr="no repo"))
        with pytest.raises(ReviewPrError):
            parse_pr_ref("6", _cfg(tmp_path))


class TestFetchBotComments:
    def test_filters_to_allowed_bots_only(self, tmp_path, monkeypatch):
        payload = [
            _comment_payload(1, author="Copilot"),
            _comment_payload(2, author="some-human"),
            _comment_payload(3, author="copilot-pull-request-reviewer[bot]"),
        ]
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=payload))
        cfg = _cfg(tmp_path)
        got = fetch_bot_comments(cfg, REPO, 6, cfg.review_pr_allowed_bots)
        assert [c.comment_id for c in got] == [1, 3]

    def test_api_error_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rp, "_gh", lambda *a: _proc(returncode=1, stderr="rate limited"))
        with pytest.raises(ReviewPrError):
            fetch_bot_comments(_cfg(tmp_path), REPO, 6, ["Copilot"])


class TestParseVerdict:
    def test_valid(self):
        v, e = parse_verdict("blah\nVERDICT: VALID\nEVIDENCE: checked src/x.py:10")
        assert v == "valid"
        assert "src/x.py:10" in e

    def test_refuted_case_insensitive(self):
        v, _ = parse_verdict("verdict: refuted\nevidence: ran the test, passes")
        assert v == "refuted"

    def test_last_marker_wins(self):
        out = "VERDICT: VALID\n...reconsidering...\nVERDICT: REFUTED\nEVIDENCE: x"
        assert parse_verdict(out)[0] == "refuted"

    def test_no_marker_is_uncertain(self):
        v, e = parse_verdict("I think it might be right?")
        assert v == "uncertain"
        assert "No VERDICT marker" in e

    def test_evidence_pairs_with_last_verdict(self):
        """When the agent revises mid-answer, verdict AND evidence must
        both come from the final block — no mismatched pairs."""
        out = (
            "VERDICT: VALID\nEVIDENCE: looked plausible at first\n"
            "Wait — re-checking...\n"
            "VERDICT: REFUTED\nEVIDENCE: ran the test, it passes on line 10"
        )
        v, e = parse_verdict(out)
        assert v == "refuted"
        assert "ran the test" in e
        assert "plausible" not in e

    def test_empty_output_is_uncertain(self):
        assert parse_verdict("")[0] == "uncertain"


class TestCmdReviewPr:
    def test_happy_path_all_verified(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(
            rp, "_gh", _gh_router(comments=[_comment_payload(1), _comment_payload(2)])
        )
        cfg = _cfg(tmp_path)
        with patch.object(rp, "verify_comment", side_effect=[("valid", "ev1"), ("refuted", "ev2")]):
            code = cmd_review_pr(_args(), cfg)
        assert code == EXIT_OK
        out = capsys.readouterr().out
        assert "2 bot comment(s), 2 new" in out
        assert "valid: 1" in out and "refuted: 1" in out

    def test_uncertain_exits_needs_human(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)]))
        with patch.object(rp, "verify_comment", return_value=("uncertain", "?")):
            assert cmd_review_pr(_args(), _cfg(tmp_path)) == EXIT_NEEDS_HUMAN

    def test_no_verify_leaves_unverified_and_needs_human(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)]))
        with patch.object(rp, "verify_comment") as mock_verify:
            code = cmd_review_pr(_args(no_verify=True), _cfg(tmp_path))
        assert code == EXIT_NEEDS_HUMAN
        mock_verify.assert_not_called()

    def test_cursor_never_reprocesses_stored_comments(self, tmp_path, monkeypatch):
        """The durable-cursor guarantee: a second invocation verifies only
        comments it has not seen."""
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)]))
        cfg = _cfg(tmp_path)
        with patch.object(rp, "verify_comment", return_value=("valid", "ev")) as m1:
            cmd_review_pr(_args(), cfg)
        assert m1.call_count == 1

        # Second run: same comment + one new
        monkeypatch.setattr(
            rp, "_gh", _gh_router(comments=[_comment_payload(1), _comment_payload(9)])
        )
        with patch.object(rp, "verify_comment", return_value=("valid", "ev")) as m2:
            code = cmd_review_pr(_args(), cfg)
        assert code == EXIT_OK
        assert m2.call_count == 1  # only comment 9
        assert m2.call_args.args[0].comment_id == 9

    def test_no_verify_then_resume_verifies_stranded_comments(self, tmp_path, monkeypatch):
        """A --no-verify collection run must not strand comments: the next
        run without the flag verifies the previously collected ones."""
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)]))
        cfg = _cfg(tmp_path)
        assert cmd_review_pr(_args(no_verify=True), cfg) == EXIT_NEEDS_HUMAN

        monkeypatch.setattr(
            rp, "_gh", _gh_router(comments=[_comment_payload(1), _comment_payload(2)])
        )
        with patch.object(rp, "verify_comment", return_value=("valid", "ev")) as mock_verify:
            code = cmd_review_pr(_args(), cfg)
        assert code == EXIT_OK
        # Both the new comment AND the stranded one got verified
        verified_ids = {c.args[0].comment_id for c in mock_verify.call_args_list}
        assert verified_ids == {1, 2}
        with ReviewPrState(cfg) as st:
            assert st.unverified_ids(REPO, 6) == set()

    def test_draft_pr_fails_closed(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(rp, "_gh", _gh_router(draft=True))
        assert cmd_review_pr(_args(), _cfg(tmp_path)) == EXIT_FAIL
        assert "draft" in capsys.readouterr().out

    def test_closed_pr_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rp, "_gh", _gh_router(pr_state="closed"))
        assert cmd_review_pr(_args(), _cfg(tmp_path)) == EXIT_FAIL

    def test_api_failure_fails_closed(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(rp, "_gh", lambda *a: _proc(returncode=1, stderr="403"))
        assert (
            cmd_review_pr(_args(pr_ref="https://github.com/o/r/pull/6"), _cfg(tmp_path))
            == EXIT_FAIL
        )
        assert "⛔" in capsys.readouterr().out

    def test_json_report_shape(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)]))
        with patch.object(rp, "verify_comment", return_value=("valid", "ev")):
            code = cmd_review_pr(_args(json_output=True), _cfg(tmp_path))
        assert code == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["repo"] == REPO
        assert payload["pr_number"] == 6
        assert payload["head_sha"] == "abc123"
        assert payload["needs_human"] is False
        assert payload["counts"] == {"valid": 1, "refuted": 0, "uncertain": 0, "unverified": 0}
        assert payload["comments"][0]["comment_id"] == 1
        assert payload["comments"][0]["verdict"] == "valid"

    def test_dirty_tree_verifier_forfeits_verdict(self, tmp_path, monkeypatch):
        """Read-only guard: a verifier that mutates the tree gets its
        verdict discarded (uncertain → human)."""
        subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)]))
        cfg = _cfg(tmp_path)

        def rogue_verifier(comment, repo, pr, config):
            (tmp_path / "mutated.py").write_text("oops")
            return "valid", "trust me"

        with patch.object(rp, "verify_comment", side_effect=rogue_verifier):
            code = cmd_review_pr(_args(), cfg)
        assert code == EXIT_NEEDS_HUMAN
        with ReviewPrState(cfg) as st:
            row = st.rows(REPO, 6)[0]
        assert row["verdict"] == "uncertain"
        assert "modified the working tree" in row["evidence"]


class TestReviewPrState:
    def test_round_trip_and_idempotent_record(self, tmp_path):
        cfg = _cfg(tmp_path)
        c = BotComment(1, "Copilot", "src/x.py", 10, "Bug", "@@", "http://u")
        with ReviewPrState(cfg) as st:
            st.record(REPO, 6, "abc", c)
            st.record(REPO, 6, "abc", c)  # duplicate insert ignored
            st.set_verdict(REPO, 6, 1, "refuted", "evidence")
        with ReviewPrState(cfg) as st:
            rows = st.rows(REPO, 6)
        assert len(rows) == 1
        assert rows[0]["verdict"] == "refuted"
        assert rows[0]["evidence"] == "evidence"

    def test_coexists_with_executor_state(self, tmp_path):
        """The new table lives in the executor DB without disturbing it."""
        from spec_runner.state import ExecutorState

        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as es:
            es.record_attempt("TASK-001", True, 1.0)
        with ReviewPrState(cfg) as st:
            st.record(REPO, 6, "abc", BotComment(1, "Copilot", "x", 1, "b", "@@", "u"))
        with ExecutorState(cfg) as es:
            assert es.tasks["TASK-001"].status == "success"
        with ReviewPrState(cfg) as st:
            assert st.known_ids(REPO, 6) == {1}


class TestParserWiring:
    def test_review_pr_subcommand_parses(self):
        from spec_runner.cli import _build_parser

        args = _build_parser().parse_args(["review-pr", "6", "--json", "--no-verify"])
        assert args.command == "review-pr"
        assert args.pr_ref == "6"
        assert args.json_output is True
        assert args.no_verify is True

    def test_allowed_bots_config_default(self):
        cfg = ExecutorConfig()
        assert "Copilot" in cfg.review_pr_allowed_bots
        assert "copilot-pull-request-reviewer[bot]" in cfg.review_pr_allowed_bots

    def test_allowed_bots_from_yaml(self, tmp_path):
        from spec_runner.config import build_config, load_config_from_yaml

        cfg_file = tmp_path / "spec-runner.config.yaml"
        cfg_file.write_text("review_pr:\n  allowed_bots: [mybot]\n")
        cfg = build_config(load_config_from_yaml(cfg_file), argparse.Namespace(command="status"))
        assert cfg.review_pr_allowed_bots == ["mybot"]
