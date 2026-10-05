"""A git read error is a refusal, never "no WIP" (PR #661 blocker 4).

`wip.wip_base` answered None — "provably no WIP" — whenever
`rev-parse --git-dir` or `rev-parse --verify HEAD` failed, so a damaged
repository (a corrupt `.git/HEAD`, HEAD naming a missing object, "dubious
ownership", a permission error) read as "no WIP" and the continuation was
silently dropped: fail-open. None is now kept for exactly two proven shapes:

- not a repository: git says "not a git repository" (C locale) and no `.git`
  entry exists at the project root or above it;
- no commits: HEAD is a symbolic ref and `rev-list -n1 --all` succeeds empty.

Everything else raises `WipReadError`, which the callers refuse on as
INSTRUMENT before the paid call.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner import paid_call, wip
from spec_runner.state import ExecutorState
from spec_runner.wip import WipReadError
from tests.test_retry_continues_from_wip import BRANCH, _cfg, _fail_once, _git, _task, repo

__all__ = ["repo"]

DUBIOUS = (
    "fatal: detected dubious ownership in repository at '{root}'\n"
    "To add an exception for this directory, call:\n\n"
    "\tgit config --global --add safe.directory {root}\n"
)


def _on_task_branch(repo: Path) -> None:
    _git(repo, "switch", "-q", "-c", BRANCH)
    (repo / "w.py").write_text("w\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "own commit")


class TestProvenShapesStayNone:
    def test_a_plain_directory(self, tmp_path):
        assert wip.wip_base(_cfg(tmp_path)) is None

    def test_an_empty_repository(self, tmp_path):
        _git(tmp_path, "init", "-q", "-b", "main")
        assert wip.wip_base(_cfg(tmp_path)) is None

    def test_an_empty_repository_on_an_unborn_task_branch(self, tmp_path):
        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "symbolic-ref", "HEAD", f"refs/heads/{BRANCH}")
        assert wip.wip_base(_cfg(tmp_path)) is None

    def test_a_localised_git_is_still_read_in_c(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
        monkeypatch.setenv("LANG", "de_DE.UTF-8")
        monkeypatch.setenv("LANGUAGE", "de")
        assert wip.wip_base(_cfg(tmp_path)) is None


class TestDamagedRepositoriesRefuse:
    def test_corrupt_dot_git_head(self, repo):
        _on_task_branch(repo)
        (repo / ".git" / "HEAD").write_text("garbage\n")
        with pytest.raises(WipReadError):
            wip.wip_base(_cfg(repo))

    def test_corrupt_dot_git_head_in_a_parent(self, repo):
        """A project in a subdirectory of a damaged repository."""
        (repo / ".git" / "HEAD").write_text("garbage\n")
        sub = repo / "sub"
        sub.mkdir()
        with pytest.raises(WipReadError):
            wip.wip_base(_cfg(sub))

    def test_head_names_a_missing_object_on_a_task_branch(self, repo):
        _on_task_branch(repo)
        ref = repo / ".git" / "refs" / "heads" / "task" / "task-070-work"
        ref.write_text("1234567890123456789012345678901234567890\n")
        with pytest.raises(WipReadError):
            wip.wip_base(_cfg(repo))

    def test_head_names_a_missing_object_on_main(self, repo):
        (repo / ".git" / "refs" / "heads" / "main").write_text(
            "1234567890123456789012345678901234567890\n"
        )
        with pytest.raises(WipReadError):
            wip.wip_base(_cfg(repo))

    def test_dubious_ownership(self, repo, monkeypatch):
        real = wip._git

        def dubious(config, *args):
            if args[:2] == ("rev-parse", "--git-dir"):
                return subprocess.CompletedProcess(
                    args, 128, "", DUBIOUS.format(root=config.project_root)
                )
            return real(config, *args)

        monkeypatch.setattr(wip, "_git", dubious)
        with pytest.raises(WipReadError, match="dubious ownership"):
            wip.wip_base(_cfg(repo))

    def test_an_unreadable_head_on_an_unborn_check(self, repo, monkeypatch):
        """HEAD unresolvable and `rev-list --all` failing is not "no commits"."""
        real = wip._git

        def broken(config, *args):
            if args[:2] == ("rev-parse", "--verify"):
                return subprocess.CompletedProcess(args, 128, "", "fatal: Permission denied")
            if args[:1] == ("rev-list",):
                return subprocess.CompletedProcess(args, 128, "", "fatal: Permission denied")
            return real(config, *args)

        monkeypatch.setattr(wip, "_git", broken)
        with pytest.raises(WipReadError, match="Permission denied"):
            wip.wip_base(_cfg(repo))

    def test_a_detached_head_that_cannot_be_read(self, repo, monkeypatch):
        """No symbolic HEAD: it cannot be unborn, so an unreadable one refuses."""
        real = wip._git

        def broken(config, *args):
            if args[:2] == ("rev-parse", "--verify"):
                return subprocess.CompletedProcess(args, 128, "", "fatal: bad object HEAD")
            if args[:2] == ("symbolic-ref", "-q"):
                return subprocess.CompletedProcess(args, 1, "", "")
            return real(config, *args)

        monkeypatch.setattr(wip, "_git", broken)
        with pytest.raises(WipReadError):
            wip.wip_base(_cfg(repo))


@pytest.mark.slow
def test_the_attempt_refuses_before_the_paid_call(repo, monkeypatch):
    """End to end: dubious ownership at the continuation read is INSTRUMENT."""
    from spec_runner.execution import execute_task

    _fail_once(repo, monkeypatch)
    spawned: list[int] = []

    def _spawn(invocation, *, timeout, cwd, env):
        spawned.append(1)
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    real = wip._git

    def dubious(config, *args):
        if args[:2] == ("rev-parse", "--git-dir"):
            return subprocess.CompletedProcess(
                args, 128, "", DUBIOUS.format(root=config.project_root)
            )
        return real(config, *args)

    monkeypatch.setattr(paid_call, "_spawn", _spawn)
    cfg = _cfg(repo, max_retries=1)
    with ExecutorState(cfg) as st:
        monkeypatch.setattr(wip, "_git", dubious)
        execute_task(_task(), cfg, st)
        last = st.get_task_state("TASK-070").attempts[-1]
    assert spawned == [], "the paid call ran on an unreadable repository"
    assert not last.success
    assert last.error_kind == "instrument"
    assert "dubious ownership" in (last.error or "")
