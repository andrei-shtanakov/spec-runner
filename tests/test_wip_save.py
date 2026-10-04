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


def test_staged_rename_keeps_its_source(repo):
    cfg = _cfg(repo)
    _own(cfg)
    _git(repo, "mv", "app.py", "moved.py")
    assert _save(cfg).saved_sha
    assert _git(repo, "status", "--porcelain") == ""
    tracked = _git(repo, "ls-files").splitlines()
    assert "moved.py" in tracked and "app.py" not in tracked


def test_glob_and_magic_named_files_are_literal(repo):
    cfg = _cfg(repo)
    _own(cfg)
    (repo / "*").write_text("star\n")
    (repo / ":(top)x").write_text("magic\n")
    (repo / "other.py").write_text("o\n")
    (repo / "spec" / "tasks.md").write_text("# tasks\nflip\n")
    assert _save(cfg).saved_sha
    files = set(_git(repo, "show", "--name-only", "-z", "--format=", "HEAD").split("\0")) - {""}
    assert files == {"*", ":(top)x", "other.py"}
    assert "spec/tasks.md" in _git(repo, "status", "--porcelain")


def test_untracked_change_folder_tasks_md_stays_out(repo):
    cfg = _cfg(repo)
    cfg.change_id = "c1"
    cfg.__post_init__()
    _own(cfg)
    folder = repo / "spec" / "changes" / "c1"
    folder.mkdir(parents=True)
    (folder / "tasks.md").write_text("# t\n")
    (folder / "notes.py").write_text("n\n")
    (repo / "app.py").write_text("x = 2\n")
    assert _save(cfg).saved_sha
    files = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert "spec/changes/c1/tasks.md" not in files
    assert "app.py" in files


def test_is_wip_of_is_exact(repo):
    from spec_runner.wip import is_wip_of

    cfg = _cfg(repo)
    (repo / "a.py").write_text("1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "wip\n\nSpec-Runner-WIP: TASK-10\nSpec-Runner-WIP-Attempt: 0\n")
    sha = _git(repo, "rev-parse", "HEAD").strip()
    assert is_wip_of(cfg, sha, "TASK-10")
    assert not is_wip_of(cfg, sha, "TASK-1")


def _wip_commit(repo, name, task, attempt):
    (repo / name).write_text("1\n")
    _git(repo, "add", "-A")
    _git(
        repo,
        "commit",
        "-qm",
        f"wip\n\nSpec-Runner-WIP: {task}\nSpec-Runner-WIP-Attempt: {attempt}\n",
    )


def test_wip_commits_filters_and_orders(repo):
    cfg = _cfg(repo)
    _wip_commit(repo, "a.py", "TASK-060", 0)
    _wip_commit(repo, "other.py", "TASK-061", 5)
    (repo / "plain.py").write_text("1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "plain")
    _wip_commit(repo, "b c.py", "TASK-060", 2)
    found = wip_commits(cfg, "TASK-060", "main")
    assert [(a, f) for _, a, f in found] == [(0, ["a.py"]), (2, ["b c.py"])]


def test_wip_commits_raises_on_bad_base(repo):
    from spec_runner.wip import WipReadError

    with pytest.raises(WipReadError):
        wip_commits(_cfg(repo), "TASK-060", "no-such-ref")


def test_wip_commits_repeated_attempt_trailer_takes_first(repo):
    cfg = _cfg(repo)
    (repo / "a.py").write_text("1\n")
    _git(repo, "add", "-A")
    _git(
        repo,
        "commit",
        "-qm",
        "wip\n\nSpec-Runner-WIP: TASK-060\nSpec-Runner-WIP-Attempt: 3\nSpec-Runner-WIP-Attempt: 4\n",
    )
    assert wip_commits(cfg, "TASK-060", "main")[0][1] == 3


@pytest.mark.parametrize("failing", ["--cached", "worktree"])
def test_one_failing_diff_is_an_instrument_refusal(repo, monkeypatch, failing):
    from spec_runner import wip
    from spec_runner.phases import RefusalKind

    cfg = _cfg(repo)
    _own(cfg)
    (repo / "app.py").write_text("x = 2\n")
    real = wip._git

    def _broken(config, *args):
        if args[:1] == ("diff",) and (("--cached" in args) == (failing == "--cached")):
            return subprocess.CompletedProcess(args, 128, "", "fatal: bad")
        return real(config, *args)

    monkeypatch.setattr(wip, "_git", _broken)
    result = _save(cfg)
    assert result.refusal is not None and result.refusal.kind is RefusalKind.INSTRUMENT


def test_dangling_symlink_is_saved_not_gone(repo):
    """`lexists`: an untracked symlink to nowhere is work, not a vanished path."""
    cfg = _cfg(repo)
    _own(cfg)
    (repo / "link").symlink_to("missing-target")
    result = _save(cfg)
    assert result.refusal is None and result.saved_sha
    assert "link" in _git(repo, "show", "--name-only", "--format=", "HEAD").split()


def test_dangling_symlink_is_not_unstaged_as_vanished(repo):
    from spec_runner.git_ops import unstage_vanished_paths

    cfg = _cfg(repo)
    _git(repo, "rm", "-q", "--cached", "app.py")
    (repo / "app.py").unlink()
    (repo / "app.py").symlink_to("missing-target")
    unstage_vanished_paths(cfg, ["app.py"])
    # Not "gone": the staged removal is left alone, not reset back from HEAD.
    assert "app.py" in _git(repo, "diff", "--cached", "--name-only").split()


@pytest.mark.parametrize("failing", ["log", "show"])
def test_wip_commits_raises_when_a_commit_cannot_be_read(repo, monkeypatch, failing):
    """A git error inside the walk is "could not look", never "not WIP" (final review #7)."""
    from spec_runner import wip
    from spec_runner.wip import WipReadError

    cfg = _cfg(repo)
    _wip_commit(repo, "a.py", "TASK-060", 1)
    real = wip._git

    def _broken(config, *args):
        if args[:1] == (failing,):
            return subprocess.CompletedProcess(args, 128, "", "fatal: bad object")
        return real(config, *args)

    monkeypatch.setattr(wip, "_git", _broken)
    with pytest.raises(WipReadError, match="bad object"):
        wip_commits(cfg, "TASK-060", "main")
