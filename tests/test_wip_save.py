"""save_wip: composition, ownership, index safety (spec §1)."""

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.tdd import resolve_namespace
from spec_runner.wip import save_wip, wip_commits

BRANCH = "task/task-060-work"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@e.c")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "tasks.md").write_text("# tasks\n")
    (tmp_path / "app.py").write_text("x = 1\n")
    (tmp_path / ".gitignore").write_text("state.db*\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-qb", BRANCH)
    return tmp_path


def _cfg(root: Path) -> ExecutorConfig:
    return ExecutorConfig(project_root=root, state_file=root / "state.db", create_git_branch=True)


def _own(cfg, task="TASK-060", branch=BRANCH, ns=None):
    with ExecutorState(cfg) as st:
        st.record_workspace(ns or resolve_namespace(cfg), task, branch=branch, run_id=None)


def _save(cfg):
    with ExecutorState(cfg) as st:
        return save_wip(cfg, st)


def test_owned_dirt_becomes_a_wip_commit(repo):
    cfg = _cfg(repo)
    _own(cfg)
    (repo / "app.py").write_text("x = 2\n")
    (repo / "new.py").write_text("y = 1\n")
    result = _save(cfg)
    assert result.refusal is None and result.saved_sha
    body = _git(repo, "log", "-1", "--format=%B")
    assert body.startswith("wip(TASK-060): unfinished work of attempt 0 — not a candidate")
    assert "Spec-Runner-WIP: TASK-060" in body and "Spec-Runner-WIP-Attempt: 0" in body
    assert _git(repo, "status", "--porcelain") == ""
    assert wip_commits(cfg, "TASK-060", "main")[0][1] == 0


def test_spec_and_staged_runtime_do_not_ride_along(repo):
    cfg = _cfg(repo)
    _own(cfg)
    (repo / "app.py").write_text("x = 2\n")
    (repo / "spec" / "tasks.md").write_text("# tasks\nflip\n")
    _git(repo, "add", "spec/tasks.md")
    result = _save(cfg)
    assert result.saved_sha
    files = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert files == ["app.py"]
    assert "spec/tasks.md" in _git(repo, "status", "--porcelain")


def test_nothing_eligible_makes_no_commit(repo):
    cfg = _cfg(repo)
    _own(cfg)
    head = _git(repo, "rev-parse", "HEAD")
    (repo / "spec" / "tasks.md").write_text("# tasks\nflip\n")
    assert _save(cfg).saved_sha is None
    assert _git(repo, "rev-parse", "HEAD") == head


def test_partially_staged_path_is_refused_untouched(repo):
    cfg = _cfg(repo)
    _own(cfg)
    (repo / "app.py").write_text("x = 2\n")
    _git(repo, "add", "app.py")
    (repo / "app.py").write_text("x = 3\n")
    before_index = _git(repo, "show", ":app.py")
    result = _save(cfg)
    assert result.refusal is not None and "app.py" in str(result.refusal)
    assert _git(repo, "show", ":app.py") == before_index
    assert (repo / "app.py").read_text() == "x = 3\n"


def test_wip_records_a_deletion(repo):
    cfg = _cfg(repo)
    _own(cfg)
    (repo / "app.py").unlink()
    assert _save(cfg).saved_sha
    assert "app.py" not in _git(repo, "ls-files")


def test_wip_path_with_space_and_dash(repo):
    cfg = _cfg(repo)
    _own(cfg)
    (repo / "a file.py").write_text("1\n")
    (repo / "-dash.py").write_text("2\n")
    assert _save(cfg).saved_sha
    tracked = _git(repo, "ls-files").splitlines()
    assert "a file.py" in tracked and "-dash.py" in tracked


def test_no_row_for_exact_branch_is_not_owned(repo):
    cfg = _cfg(repo)
    _own(cfg, branch="task/task-060")
    (repo / "app.py").write_text("x = 2\n")
    assert _save(cfg).saved_sha is None


def test_other_namespace_row_is_not_ownership(repo):
    cfg = _cfg(repo)
    _own(cfg, ns="someone-else")
    (repo / "app.py").write_text("x = 2\n")
    assert _save(cfg).saved_sha is None


def test_detached_head_falls_back_to_stash(repo):
    cfg = _cfg(repo)
    _own(cfg)
    _git(repo, "checkout", "-q", "--detach")
    (repo / "app.py").write_text("x = 2\n")
    assert _save(cfg).saved_sha is None


def test_unreadable_index_is_an_instrument_refusal(repo, monkeypatch):
    from spec_runner import wip
    from spec_runner.phases import RefusalKind

    cfg = _cfg(repo)
    _own(cfg)
    (repo / "app.py").write_text("x = 2\n")
    real = wip._git

    def _broken(config, *args):
        if args[:1] == ("diff",):
            return subprocess.CompletedProcess(args, 128, "", "fatal: index file corrupt")
        return real(config, *args)

    monkeypatch.setattr(wip, "_git", _broken)
    result = _save(cfg)
    assert result.refusal is not None and result.refusal.kind is RefusalKind.INSTRUMENT
    assert (repo / "app.py").read_text() == "x = 2\n"


def test_commit_failure_is_refused_with_work_in_tree(repo):
    cfg = _cfg(repo)
    _own(cfg)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (repo / "app.py").write_text("x = 2\n")
    result = _save(cfg)
    assert result.refusal is not None and result.saved_sha is None
    assert (repo / "app.py").read_text() == "x = 2\n"
