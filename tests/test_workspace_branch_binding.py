"""A workspace row with a NULL branch must not wedge `strict` (final review #2).

The row is written NULL when the start did not end on the task branch: a
bootstrap repo without commits, a failed `checkout -b`, or — the parser
differential — `rev-parse --abbrev-ref HEAD` answering ``heads/task/…`` because
a tag carries the branch's name. Under `strict` a NULL row owns nothing, so the
task's own dirt was refused as "belongs to no recorded task", and
`harness trust` could not bind the row. Now `current_branch` is exact, a later
start that checked the task branch out fills the row, and `harness trust
--bind-branch` binds it.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner import paid_call
from spec_runner.git_ops import current_branch
from spec_runner.harness_cmd import TrustError, trust
from spec_runner.state import ExecutorState
from spec_runner.tdd import resolve_namespace
from tests.test_retry_continues_from_wip import BRANCH, _cfg, _git, _task
from tests.test_retry_continues_from_wip import repo as repo  # noqa: F401


def _ns(repo: Path) -> str:  # noqa: F811
    return resolve_namespace(_cfg(repo))


def _row(repo: Path) -> dict | None:  # noqa: F811
    with ExecutorState(_cfg(repo)) as st:
        return st.get_workspace(_ns(repo), "TASK-070")


class TestCurrentBranchIsExact:
    def test_a_tag_of_the_same_name_does_not_change_the_answer(self, repo):  # noqa: F811
        _git(repo, "tag", BRANCH)
        _git(repo, "checkout", "-q", "-b", BRANCH)
        assert current_branch(_cfg(repo)) == BRANCH

    def test_detached_head_is_none(self, repo):  # noqa: F811
        _git(repo, "checkout", "-q", "--detach")
        assert current_branch(_cfg(repo)) is None

    def test_unborn_branch_is_named(self, tmp_path):
        _git(tmp_path, "init", "-q", "-b", "trunk")
        assert current_branch(_cfg(tmp_path)) == "trunk"

    def test_not_a_repository_is_none(self, tmp_path):
        assert current_branch(_cfg(tmp_path)) is None


class TestRecordWorkspaceFillsNull:
    def test_null_branch_is_filled_once(self, repo):  # noqa: F811
        with ExecutorState(_cfg(repo)) as st:
            st.record_workspace(_ns(repo), "TASK-070", branch=None, run_id=None)
            st.record_workspace(_ns(repo), "TASK-070", branch=BRANCH, run_id=None)
            assert st.get_workspace(_ns(repo), "TASK-070")["branch"] == BRANCH
            st.record_workspace(_ns(repo), "TASK-070", branch="task/other", run_id=None)
            assert st.get_workspace(_ns(repo), "TASK-070")["branch"] == BRANCH

    def test_a_start_with_a_same_name_tag_records_the_branch(self, repo, monkeypatch):  # noqa: F811
        """The parser differential end to end: the row is bound, not NULL."""
        _git(repo, "tag", BRANCH)

        def _fail(invocation, *, timeout, cwd, env):
            (repo / "feature.py").write_text("x\n")
            raise subprocess.TimeoutExpired(invocation.argv, timeout)

        monkeypatch.setattr(paid_call, "_spawn", _fail)
        from spec_runner.execution import run_with_retries

        cfg = _cfg(repo, max_retries=1)
        with ExecutorState(cfg) as st:
            run_with_retries(_task(), cfg, st)
        assert _row(repo)["branch"] == BRANCH

    def test_a_later_start_on_the_task_branch_fills_the_row(self, repo, monkeypatch):  # noqa: F811
        with ExecutorState(_cfg(repo)) as st:
            st.record_workspace(_ns(repo), "TASK-070", branch=None, run_id=None)
        monkeypatch.setattr(
            paid_call,
            "_spawn",
            lambda inv, **kw: subprocess.CompletedProcess(inv.argv, 1, "", "no"),
        )
        from spec_runner.execution import run_with_retries

        cfg = _cfg(repo, max_retries=1, harness_guard="warn")
        with ExecutorState(cfg) as st:
            run_with_retries(_task(), cfg, st)
        assert _row(repo)["branch"] == BRANCH


class TestTrustBindsANullRow:
    def _null_row_with_dirt(self, repo: Path) -> None:  # noqa: F811
        _git(repo, "checkout", "-q", "-b", BRANCH)
        (repo / "feature.py").write_text("work\n")
        with ExecutorState(_cfg(repo)) as st:
            st.record_workspace(_ns(repo), "TASK-070", branch=None, run_id=None)

    def _start(self, repo: Path, monkeypatch) -> tuple[list[int], object]:  # noqa: F811
        calls: list[int] = []

        def _spawn(invocation, *, timeout, cwd, env):
            calls.append(1)
            return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

        monkeypatch.setattr(paid_call, "_spawn", _spawn)
        from spec_runner.execution import run_with_retries

        cfg = _cfg(repo, max_retries=1)
        with ExecutorState(cfg) as st:
            # A separate `retry`, as `cmd_retry` runs it: earlier attempts stay
            # in the DB but do not use up this invocation's one attempt (R1).
            st.get_task_state("TASK-070").attempts = []
            run_with_retries(_task(), cfg, st)
            return calls, st.get_task_state("TASK-070").attempts[-1]

    def test_bind_branch_must_be_the_current_branch(self, repo):  # noqa: F811
        self._null_row_with_dirt(repo)
        with ExecutorState(_cfg(repo)) as st, pytest.raises(TrustError, match="current branch"):
            trust(_cfg(repo), st, "TASK-070", reason="checked", bind_branch="task/other")
        assert _row(repo)["branch"] is None

    def test_the_wedge_is_cleared_by_trust_bind_branch(self, repo, monkeypatch):  # noqa: F811
        self._null_row_with_dirt(repo)

        calls, attempt = self._start(repo, monkeypatch)
        assert calls == [], "started, no trusted baseline: refused"

        with ExecutorState(_cfg(repo)) as st:
            trust(_cfg(repo), st, "TASK-070", reason="checked", bind_branch=BRANCH)
            audit = st.harness_trust_audit(_ns(repo), "TASK-070")
        row = _row(repo)
        assert row["branch"] == BRANCH
        assert row["bound_by"] == "operator"
        assert audit[-1]["bound_branch"] == 1

        calls, attempt = self._start(repo, monkeypatch)
        assert calls == [1], "the agent ran: the start was not refused"
        assert "wip(TASK-070)" in _git(repo, "log", BRANCH, "--format=%s")

    def test_trust_without_bind_branch_leaves_a_null_row_unbound(self, repo, monkeypatch):  # noqa: F811
        """The row stays NULL, and the start's refusal names the flag that binds it."""
        self._null_row_with_dirt(repo)
        with ExecutorState(_cfg(repo)) as st:
            trust(_cfg(repo), st, "TASK-070", reason="checked")
            assert st.harness_trust_audit(_ns(repo), "TASK-070")[-1]["bound_branch"] == 0
        assert _row(repo)["branch"] is None

        calls, attempt = self._start(repo, monkeypatch)
        assert calls == []
        assert "belongs to no recorded task" in (attempt.error or "")
        assert "--bind-branch" in (attempt.error or "")
        assert (repo / "feature.py").read_text() == "work\n"
