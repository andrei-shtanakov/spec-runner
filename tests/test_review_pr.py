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
    base: dict = {"pr_ref": "6", "json_output": False, "no_verify": False, "verify_only": False}
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


def _gh_router(
    pr_state: str = "open",
    draft: bool = False,
    comments: list | None = None,
    head_sha: str = "abc123",
    reply_log: list | None = None,
    reply_rc: int = 0,
):
    """Build a fake `_gh` answering the meta, comments and reply endpoints."""

    def fake_gh(config, *args):
        joined = " ".join(args)
        if "/replies" in joined:
            if reply_log is not None:
                reply_log.append(args)
            return _proc(returncode=reply_rc, stderr="reply denied" if reply_rc else "")
        if joined.startswith("api repos/") and joined.endswith("/comments --paginate"):
            return _proc(stdout=json.dumps(comments or []))
        if joined.startswith("api repos/"):
            return _proc(
                stdout=json.dumps(
                    {
                        "state": pr_state,
                        "draft": draft,
                        "head": {"sha": head_sha, "ref": "feature-branch"},
                    }
                )
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
            code = cmd_review_pr(_args(verify_only=True), cfg)
        assert code == EXIT_OK
        out = capsys.readouterr().out
        assert "2 bot comment(s), 2 new" in out
        assert "valid: 1" in out and "refuted: 1" in out

    def test_uncertain_exits_needs_human(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)]))
        with patch.object(rp, "verify_comment", return_value=("uncertain", "?")):
            assert cmd_review_pr(_args(verify_only=True), _cfg(tmp_path)) == EXIT_NEEDS_HUMAN

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
            cmd_review_pr(_args(verify_only=True), cfg)
        assert m1.call_count == 1

        # Second run: same comment + one new
        monkeypatch.setattr(
            rp, "_gh", _gh_router(comments=[_comment_payload(1), _comment_payload(9)])
        )
        with patch.object(rp, "verify_comment", return_value=("valid", "ev")) as m2:
            code = cmd_review_pr(_args(verify_only=True), cfg)
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
            code = cmd_review_pr(_args(verify_only=True), cfg)
        assert code == EXIT_OK
        # Both the new comment AND the stranded one got verified
        verified_ids = {c.args[0].comment_id for c in mock_verify.call_args_list}
        assert verified_ids == {1, 2}
        with ReviewPrState(cfg) as st:
            assert st.unverified_ids(REPO, 6) == set()

    def test_draft_pr_fails_closed(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(rp, "_gh", _gh_router(draft=True))
        assert cmd_review_pr(_args(), _cfg(tmp_path)) == EXIT_FAIL
        assert "draft" in capsys.readouterr().err

    def test_closed_pr_fails_closed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(rp, "_gh", _gh_router(pr_state="closed"))
        assert cmd_review_pr(_args(), _cfg(tmp_path)) == EXIT_FAIL

    def test_api_failure_fails_closed(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(rp, "_gh", lambda *a: _proc(returncode=1, stderr="403"))
        assert (
            cmd_review_pr(_args(pr_ref="https://github.com/o/r/pull/6"), _cfg(tmp_path))
            == EXIT_FAIL
        )
        assert "⛔" in capsys.readouterr().err

    def test_json_report_shape(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)]))
        with patch.object(rp, "verify_comment", return_value=("valid", "ev")):
            code = cmd_review_pr(_args(json_output=True, verify_only=True), _cfg(tmp_path))
        assert code == EXIT_OK
        payload = json.loads(capsys.readouterr().out)
        assert payload["repo"] == REPO
        assert payload["pr_number"] == 6
        assert payload["head_sha"] == "abc123"
        assert payload["needs_human"] is False
        assert payload["counts"] == {
            "valid": 1,
            "refuted": 0,
            "uncertain": 0,
            "unverified": 0,
            "fixed": 0,
            "replied": 0,
        }
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
            code = cmd_review_pr(_args(verify_only=True), cfg)
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


# --- M2: fix + reply -------------------------------------------------------


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=root, check=False)


def _init_repo_with_remote(tmp_path: Path) -> tuple[Path, Path, str]:
    """Local repo on `feature-branch` + a bare origin. Returns
    (workdir, bare_remote, head_sha)."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q", "-b", "feature-branch")
    _git(work, "config", "user.email", "t@e.c")
    _git(work, "config", "user.name", "T")
    (work / "src.py").write_text("x = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-q", "origin", "feature-branch")
    head = _git(work, "rev-parse", "HEAD").stdout.strip()
    return work, bare, head


def _m2_cfg(work: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": work,
        "state_file": work / "state.db",
        "logs_dir": work / "logs",
        "run_tests_on_done": True,
        "test_command": "true",
        "run_lint_on_done": False,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _fix_agent_factory(work: Path, content: str = "x = 2\n"):
    """A fix agent that edits src.py and reports success."""

    def agent(comment, evidence, repo, pr, config):
        (work / "src.py").write_text(content)
        return True, "changed x to 2", 0.01

    return agent


class TestApplyPhase:
    def test_full_loop_fixes_pushes_and_replies(self, tmp_path, monkeypatch):
        work, bare, head = _init_repo_with_remote(tmp_path)
        reply_log: list = []
        monkeypatch.setattr(
            rp,
            "_gh",
            _gh_router(comments=[_comment_payload(1)], head_sha=head, reply_log=reply_log),
        )
        cfg = _m2_cfg(work)
        with (
            patch.object(rp, "verify_comment", return_value=("valid", "checked")),
            patch.object(rp, "run_fix_agent", side_effect=_fix_agent_factory(work)),
        ):
            code = cmd_review_pr(_args(), cfg)
        assert code == EXIT_OK

        # Fix committed with provenance
        log = _git(work, "log", "-1", "--format=%B").stdout
        assert "Review-Comment-Id: 1" in log
        # Pushed: remote tip == local tip
        local = _git(work, "rev-parse", "HEAD").stdout.strip()
        remote = subprocess.run(
            ["git", "rev-parse", "feature-branch"], capture_output=True, text=True, cwd=bare
        ).stdout.strip()
        assert local == remote != head
        # Replied with the actual SHA
        assert len(reply_log) == 1
        assert local in " ".join(reply_log[0])
        with ReviewPrState(cfg) as st:
            row = st.rows(REPO, 6)[0]
        assert row["resolution"] == "fixed"
        assert row["fix_sha"] == local
        assert row["replied_at"]

    def test_refuted_replies_without_commit(self, tmp_path, monkeypatch):
        work, bare, head = _init_repo_with_remote(tmp_path)
        reply_log: list = []
        monkeypatch.setattr(
            rp,
            "_gh",
            _gh_router(comments=[_comment_payload(1)], head_sha=head, reply_log=reply_log),
        )
        cfg = _m2_cfg(work)
        with patch.object(rp, "verify_comment", return_value=("refuted", "disproven: test passes")):
            code = cmd_review_pr(_args(), cfg)
        assert code == EXIT_OK
        assert _git(work, "rev-parse", "HEAD").stdout.strip() == head  # no commit
        assert len(reply_log) == 1
        assert "disproven: test passes" in " ".join(reply_log[0])

    def test_uncertain_gets_no_reply(self, tmp_path, monkeypatch):
        work, _, head = _init_repo_with_remote(tmp_path)
        reply_log: list = []
        monkeypatch.setattr(
            rp,
            "_gh",
            _gh_router(comments=[_comment_payload(1)], head_sha=head, reply_log=reply_log),
        )
        with patch.object(rp, "verify_comment", return_value=("uncertain", "?")):
            code = cmd_review_pr(_args(), _m2_cfg(work))
        assert code == EXIT_NEEDS_HUMAN
        assert reply_log == []

    def test_gate_failure_reverts_fix(self, tmp_path, monkeypatch):
        work, _, head = _init_repo_with_remote(tmp_path)
        reply_log: list = []
        monkeypatch.setattr(
            rp,
            "_gh",
            _gh_router(comments=[_comment_payload(1)], head_sha=head, reply_log=reply_log),
        )
        cfg = _m2_cfg(work, test_command="false")
        with (
            patch.object(rp, "verify_comment", return_value=("valid", "checked")),
            patch.object(rp, "run_fix_agent", side_effect=_fix_agent_factory(work)),
        ):
            code = cmd_review_pr(_args(), cfg)
        assert code == EXIT_NEEDS_HUMAN
        assert _git(work, "rev-parse", "HEAD").stdout.strip() == head  # reverted
        assert (work / "src.py").read_text() == "x = 1\n"
        assert reply_log == []
        with ReviewPrState(cfg) as st:
            assert st.rows(REPO, 6)[0]["resolution"] == "needs_human"

    def test_diff_size_limit_reverts_fix(self, tmp_path, monkeypatch):
        work, _, head = _init_repo_with_remote(tmp_path)
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)], head_sha=head))
        cfg = _m2_cfg(work, review_pr_max_changed_lines=1)
        big = "".join(f"line{i} = {i}\n" for i in range(50))
        with (
            patch.object(rp, "verify_comment", return_value=("valid", "checked")),
            patch.object(rp, "run_fix_agent", side_effect=_fix_agent_factory(work, big)),
        ):
            code = cmd_review_pr(_args(), cfg)
        assert code == EXIT_NEEDS_HUMAN
        assert _git(work, "rev-parse", "HEAD").stdout.strip() == head

    def test_dirty_tree_fails_closed(self, tmp_path, monkeypatch, capsys):
        work, _, head = _init_repo_with_remote(tmp_path)
        (work / "uncommitted.py").write_text("dirty\n")
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)], head_sha=head))
        with patch.object(rp, "verify_comment", return_value=("valid", "checked")):
            code = cmd_review_pr(_args(), _m2_cfg(work))
        assert code == EXIT_FAIL
        assert "not clean" in capsys.readouterr().err

    def test_head_mismatch_fails_closed(self, tmp_path, monkeypatch, capsys):
        work, _, head = _init_repo_with_remote(tmp_path)
        monkeypatch.setattr(
            rp, "_gh", _gh_router(comments=[_comment_payload(1)], head_sha="f" * 40)
        )
        with patch.object(rp, "verify_comment", return_value=("valid", "checked")):
            code = cmd_review_pr(_args(), _m2_cfg(work))
        assert code == EXIT_FAIL
        assert "check out the PR branch" in capsys.readouterr().err

    def test_push_failure_publishes_no_replies(self, tmp_path, monkeypatch, capsys):
        work, bare, head = _init_repo_with_remote(tmp_path)
        _git(work, "remote", "set-url", "origin", str(tmp_path / "nonexistent.git"))
        reply_log: list = []
        monkeypatch.setattr(
            rp,
            "_gh",
            _gh_router(comments=[_comment_payload(1)], head_sha=head, reply_log=reply_log),
        )
        cfg = _m2_cfg(work)
        with (
            patch.object(rp, "verify_comment", return_value=("valid", "checked")),
            patch.object(rp, "run_fix_agent", side_effect=_fix_agent_factory(work)),
        ):
            code = cmd_review_pr(_args(), cfg)
        assert code == EXIT_FAIL
        assert reply_log == []
        assert "push failed" in capsys.readouterr().err

    def test_no_double_reply_on_rerun(self, tmp_path, monkeypatch):
        work, _, head = _init_repo_with_remote(tmp_path)
        reply_log: list = []
        monkeypatch.setattr(
            rp,
            "_gh",
            _gh_router(comments=[_comment_payload(1)], head_sha=head, reply_log=reply_log),
        )
        cfg = _m2_cfg(work)
        with patch.object(rp, "verify_comment", return_value=("refuted", "no")):
            assert cmd_review_pr(_args(), cfg) == EXIT_OK
            assert cmd_review_pr(_args(), cfg) == EXIT_OK
        assert len(reply_log) == 1  # replied_at guard held

    def test_force_push_detected(self, tmp_path, monkeypatch, capsys):
        work, _, head = _init_repo_with_remote(tmp_path)
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)], head_sha=head))
        cfg = _m2_cfg(work)
        with ReviewPrState(cfg) as st:
            st.start_round(REPO, 6, "e" * 40)  # a SHA that is no ancestor
        with patch.object(rp, "verify_comment", return_value=("valid", "checked")):
            code = cmd_review_pr(_args(), cfg)
        assert code == EXIT_FAIL
        assert "force-push" in capsys.readouterr().err

    def test_round_limit_stops_fixes(self, tmp_path, monkeypatch):
        work, _, head = _init_repo_with_remote(tmp_path)
        # Two more real commits become past-round SHAs (ancestors of HEAD)
        shas = []
        for i in range(2):
            (work / "src.py").write_text(f"x = {i + 10}\n")
            _git(work, "add", "-A")
            _git(work, "commit", "-q", "-m", f"round {i}")
            shas.append(_git(work, "rev-parse", "HEAD").stdout.strip())
        head = shas[-1]
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)], head_sha=head))
        cfg = _m2_cfg(work, review_pr_max_rounds=2)
        with ReviewPrState(cfg) as st:
            st.start_round(REPO, 6, shas[0])
            st.start_round(REPO, 6, shas[1])
        # current head == shas[1] → idempotent round insert keeps count at 2…
        # simulate one MORE new head by using the first sha as "previous":
        (work / "src.py").write_text("x = 99\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "round 3")
        head3 = _git(work, "rev-parse", "HEAD").stdout.strip()
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)], head_sha=head3))
        with (
            patch.object(rp, "verify_comment", return_value=("valid", "checked")),
            patch.object(rp, "run_fix_agent") as mock_fix,
        ):
            code = cmd_review_pr(_args(), cfg)
        assert code == EXIT_NEEDS_HUMAN
        mock_fix.assert_not_called()

    def test_deleted_comment_marked(self, tmp_path, monkeypatch):
        work, _, head = _init_repo_with_remote(tmp_path)
        cfg = _m2_cfg(work)
        # Stored earlier with a verdict, but the PR no longer has it
        with ReviewPrState(cfg) as st:
            st.record(REPO, 6, head, BotComment(77, "Copilot", "src.py", 1, "gone", "@@", "u"))
            st.set_verdict(REPO, 6, 77, "valid", "ev")
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[], head_sha=head))
        code = cmd_review_pr(_args(), cfg)
        assert code == EXIT_NEEDS_HUMAN
        with ReviewPrState(cfg) as st:
            assert st.rows(REPO, 6)[0]["resolution"] == "deleted"

    def test_comment_limit_stops_everything(self, tmp_path, monkeypatch):
        work, _, head = _init_repo_with_remote(tmp_path)
        payload = [_comment_payload(i) for i in range(1, 4)]
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=payload, head_sha=head))
        cfg = _m2_cfg(work, review_pr_max_comments=2)
        with (
            patch.object(rp, "verify_comment", return_value=("valid", "checked")),
            patch.object(rp, "run_fix_agent") as mock_fix,
        ):
            code = cmd_review_pr(_args(), cfg)
        assert code == EXIT_NEEDS_HUMAN
        mock_fix.assert_not_called()


class TestStatusSurfacing:
    def test_needs_human_rows(self, tmp_path):
        from spec_runner.review_pr import needs_human_rows

        cfg = _cfg(tmp_path)
        with ReviewPrState(cfg) as st:
            st.record(REPO, 6, "abc", BotComment(1, "Copilot", "x", 1, "b", "@@", "u"))
            st.set_verdict(REPO, 6, 1, "uncertain", "?")
            st.record(REPO, 6, "abc", BotComment(2, "Copilot", "x", 2, "b", "@@", "u"))
            st.set_verdict(REPO, 6, 2, "valid", "ev")
            st.set_resolution(REPO, 6, 2, "needs_human")
            st.record(REPO, 6, "abc", BotComment(3, "Copilot", "x", 3, "b", "@@", "u"))
            st.set_verdict(REPO, 6, 3, "refuted", "no")
            st.set_resolution(REPO, 6, 3, "refuted")
        assert needs_human_rows(cfg) == [(REPO, 6, 2)]

    def test_no_state_file_is_empty(self, tmp_path):
        from spec_runner.review_pr import needs_human_rows

        assert needs_human_rows(_cfg(tmp_path)) == []

    def test_binary_fix_reverted_by_diff_cap(self, tmp_path, monkeypatch):
        """numstat reports '-' for binaries; that must count as over-cap."""
        work, _, head = _init_repo_with_remote(tmp_path)
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)], head_sha=head))
        cfg = _m2_cfg(work)  # default generous line cap

        def binary_agent(comment, evidence, repo, pr, config):
            (work / "blob.bin").write_bytes(bytes(range(256)) * 4)
            return True, "added binary", 0.01

        with (
            patch.object(rp, "verify_comment", return_value=("valid", "checked")),
            patch.object(rp, "run_fix_agent", side_effect=binary_agent),
        ):
            code = cmd_review_pr(_args(), cfg)
        assert code == EXIT_NEEDS_HUMAN
        assert _git(work, "rev-parse", "HEAD").stdout.strip() == head
        assert not (work / "blob.bin").exists()

    def test_cost_overshoot_reverts_fix_and_stops(self, tmp_path, monkeypatch):
        """A single fix that blows the cost cap is discarded, not pushed."""
        work, _, head = _init_repo_with_remote(tmp_path)
        reply_log: list = []
        monkeypatch.setattr(
            rp,
            "_gh",
            _gh_router(
                comments=[_comment_payload(1), _comment_payload(2)],
                head_sha=head,
                reply_log=reply_log,
            ),
        )
        cfg = _m2_cfg(work, review_pr_max_cost_usd=5.0)

        def pricey_agent(comment, evidence, repo, pr, config):
            (work / "src.py").write_text("x = 3\n")
            return True, "expensive", 10.0  # blows the 5.0 cap

        with (
            patch.object(rp, "verify_comment", return_value=("valid", "checked")),
            patch.object(rp, "run_fix_agent", side_effect=pricey_agent) as mock_fix,
        ):
            code = cmd_review_pr(_args(), cfg)
        assert code == EXIT_NEEDS_HUMAN
        assert mock_fix.call_count == 1  # loop stopped, comment 2 untouched
        assert _git(work, "rev-parse", "HEAD").stdout.strip() == head  # reverted
        assert (work / "src.py").read_text() == "x = 1\n"
        assert reply_log == []


class TestJsonStdoutPurity:
    """#116 (inbox from maestro#post-pr-command): with --json, stdout must
    carry exactly ONE JSON document on every exit path — Maestro's wrapper
    stores the report verbatim in an audit table."""

    @staticmethod
    def _only_json(captured) -> dict:
        """stdout parses as a single JSON document; diagnostics on stderr."""
        return json.loads(captured.out)

    def test_clean_exit_zero(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)]))
        with patch.object(rp, "verify_comment", return_value=("refuted", "ev")):
            code = cmd_review_pr(_args(json_output=True, verify_only=True), _cfg(tmp_path))
        payload = self._only_json(capsys.readouterr())
        assert code == EXIT_OK
        assert payload["exit_code"] == EXIT_OK

    def test_needs_human_exit_two(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)]))
        with patch.object(rp, "verify_comment", return_value=("uncertain", "?")):
            code = cmd_review_pr(_args(json_output=True, verify_only=True), _cfg(tmp_path))
        payload = self._only_json(capsys.readouterr())
        assert code == EXIT_NEEDS_HUMAN
        assert payload["exit_code"] == EXIT_NEEDS_HUMAN
        assert payload["needs_human"] is True

    def test_draft_fail_closed_still_emits_json(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(rp, "_gh", _gh_router(draft=True))
        code = cmd_review_pr(_args(json_output=True), _cfg(tmp_path))
        captured = capsys.readouterr()
        payload = self._only_json(captured)
        assert code == EXIT_FAIL
        assert payload["exit_code"] == EXIT_FAIL
        assert "draft" in payload["error"]
        assert "⛔" in captured.err  # human text went to stderr

    def test_bad_ref_fail_closed_emits_json_without_repo(self, tmp_path, capsys):
        code = cmd_review_pr(_args(pr_ref="not-a-pr", json_output=True), _cfg(tmp_path))
        payload = self._only_json(capsys.readouterr())
        assert code == EXIT_FAIL
        assert payload["repo"] is None and payload["pr_number"] is None
        assert payload["error"]

    def test_comment_limit_diagnostic_not_on_stdout(self, tmp_path, monkeypatch, capsys):
        """The exact path named in #116: a limit stop used to print text
        before the JSON."""
        work, _, head = _init_repo_with_remote(tmp_path)
        payload_comments = [_comment_payload(i) for i in range(1, 4)]
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=payload_comments, head_sha=head))
        cfg = _m2_cfg(work, review_pr_max_comments=2)
        with patch.object(rp, "verify_comment", return_value=("valid", "checked")):
            code = cmd_review_pr(_args(json_output=True), cfg)
        captured = capsys.readouterr()
        payload = self._only_json(captured)
        assert code == EXIT_NEEDS_HUMAN
        assert payload["exit_code"] == EXIT_NEEDS_HUMAN
        assert "comment limit exceeded" in captured.err

    def test_round_limit_diagnostic_not_on_stdout(self, tmp_path, monkeypatch, capsys):
        work, _, head = _init_repo_with_remote(tmp_path)
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)], head_sha=head))
        cfg = _m2_cfg(work, review_pr_max_rounds=1)
        with ReviewPrState(cfg) as st:
            st.start_round(REPO, 6, head)  # round 1 already used
        # A second head SHA opens round 2 > limit
        (work / "src.py").write_text("x = 5\n")
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "next round")
        head2 = _git(work, "rev-parse", "HEAD").stdout.strip()
        monkeypatch.setattr(rp, "_gh", _gh_router(comments=[_comment_payload(1)], head_sha=head2))
        with patch.object(rp, "verify_comment", return_value=("valid", "checked")):
            code = cmd_review_pr(_args(json_output=True), cfg)
        captured = capsys.readouterr()
        self._only_json(captured)
        assert code == EXIT_NEEDS_HUMAN
        assert "round limit exceeded" in captured.err
