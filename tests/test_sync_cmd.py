"""`spec-runner sync` — post-merge closer for the integration-PR loop (#73)."""

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.sync_cmd import PR_URL_META_KEY, run_sync


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=root, check=False)


def _setup_remote_pair(tmp_path: Path) -> tuple[Path, Path]:
    """A bare 'origin' and a working clone with an initial commit on main."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    work = tmp_path / "work"
    subprocess.run(["git", "clone", "-q", str(origin), str(work)], capture_output=True, check=False)
    _git(work, "config", "user.email", "t@e.c")
    _git(work, "config", "user.name", "T")
    (work / "README.md").write_text("hi\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    _git(work, "push", "-q", "-u", "origin", "main")
    return origin, work


def _make_branch(work: Path, name: str, filename: str, *, merge: bool, push: bool) -> None:
    _git(work, "checkout", "-q", "-b", name)
    (work / filename).write_text("x\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", f"work on {name}")
    if push:
        _git(work, "push", "-q", "-u", "origin", name)
    _git(work, "checkout", "-q", "main")
    if merge:
        _git(work, "merge", "-q", "--no-ff", "-m", f"merge {name}", name)
        _git(work, "push", "-q", "origin", "main")


def _cfg(root: Path) -> ExecutorConfig:
    return ExecutorConfig(project_root=root, create_git_branch=False)


def _step(steps, name):
    matches = [s for s in steps if s.name == name]
    assert matches, f"step {name!r} missing in {[s.name for s in steps]}"
    return matches[0]


class TestRunSync:
    def test_happy_path_deletes_merged_branches_everywhere(self, tmp_path):
        _, work = _setup_remote_pair(tmp_path)
        _make_branch(work, "task/task-001-login", "a.txt", merge=True, push=True)
        _make_branch(work, "spec-runner/run-20260805-1", "b.txt", merge=True, push=True)

        steps = run_sync(_cfg(work))
        assert all(s.ok for s in steps), [(s.name, s.detail) for s in steps]
        branches = _git(work, "branch", "--format=%(refname:short)").stdout.split()
        assert branches == ["main"]
        remote_branches = _git(work, "ls-remote", "--heads", "origin").stdout
        assert "task/task-001-login" not in remote_branches
        assert "spec-runner/run-20260805-1" not in remote_branches

    def test_unmerged_branches_are_kept(self, tmp_path):
        _, work = _setup_remote_pair(tmp_path)
        _make_branch(work, "task/task-002-wip", "c.txt", merge=False, push=True)

        steps = run_sync(_cfg(work))
        assert all(s.ok for s in steps)
        local = _step(steps, "local managed branches")
        assert "kept (unmerged): task/task-002-wip" in local.detail
        assert "task/task-002-wip" in _git(work, "branch", "--format=%(refname:short)").stdout
        assert "task/task-002-wip" in _git(work, "ls-remote", "--heads", "origin").stdout

    def test_foreign_branches_never_touched(self, tmp_path):
        _, work = _setup_remote_pair(tmp_path)
        _make_branch(work, "feature/manual-work", "d.txt", merge=True, push=True)

        steps = run_sync(_cfg(work))
        assert all(s.ok for s in steps)
        assert "feature/manual-work" in _git(work, "branch", "--format=%(refname:short)").stdout

    def test_dry_run_changes_nothing(self, tmp_path):
        _, work = _setup_remote_pair(tmp_path)
        _make_branch(work, "task/task-003-done", "e.txt", merge=True, push=True)

        steps = run_sync(_cfg(work), dry_run=True)
        assert all(s.ok for s in steps)
        assert "would delete: task/task-003-done" in _step(steps, "local managed branches").detail
        assert "task/task-003-done" in _git(work, "branch", "--format=%(refname:short)").stdout

    def test_dirty_worktree_fails(self, tmp_path):
        _, work = _setup_remote_pair(tmp_path)
        (work / "uncommitted.txt").write_text("dirt\n")
        steps = run_sync(_cfg(work))
        clean = _step(steps, "clean worktree")
        assert clean.ok is False
        assert "uncommitted" in clean.detail

    def test_pull_fast_forwards_base(self, tmp_path):
        origin, work = _setup_remote_pair(tmp_path)
        # A second clone pushes a commit the first clone doesn't have.
        other = tmp_path / "other"
        subprocess.run(
            ["git", "clone", "-q", str(origin), str(other)], capture_output=True, check=False
        )
        _git(other, "config", "user.email", "o@e.c")
        _git(other, "config", "user.name", "O")
        (other / "new.txt").write_text("n\n")
        _git(other, "add", "-A")
        _git(other, "commit", "-q", "-m", "remote work")
        _git(other, "push", "-q", "origin", "main")

        steps = run_sync(_cfg(work))
        assert all(s.ok for s in steps)
        assert (work / "new.txt").exists()

    def test_no_git_repo_fails(self, tmp_path):
        steps = run_sync(_cfg(tmp_path))
        assert steps[-1].ok is False

    def test_clears_pr_marker(self, tmp_path):
        _, work = _setup_remote_pair(tmp_path)
        cfg = _cfg(work)
        with ExecutorState(cfg) as state:
            state.set_meta(PR_URL_META_KEY, "https://example.com/pr/1")
        # The state DB lives under spec/ — gitignore it so the worktree
        # stays clean for the sync (as spec-runner itself now ensures, #62).
        (work / ".gitignore").write_text("spec/\n")
        _git(work, "add", ".gitignore")
        _git(work, "commit", "-q", "-m", "ignore spec runtime")
        _git(work, "push", "-q", "origin", "main")

        steps = run_sync(cfg)
        assert all(s.ok for s in steps), [(s.name, s.detail) for s in steps]
        assert any(s.name == "pr loop" for s in steps)
        with ExecutorState(cfg) as state:
            assert not state.get_meta(PR_URL_META_KEY)

    def test_stale_running_task_fails_state_sanity(self, tmp_path):
        _, work = _setup_remote_pair(tmp_path)
        cfg = _cfg(work)
        with ExecutorState(cfg) as state:
            state.mark_running("TASK-001")
        (work / ".gitignore").write_text("spec/\n")
        _git(work, "add", ".gitignore")
        _git(work, "commit", "-q", "-m", "ignore spec runtime")
        _git(work, "push", "-q", "origin", "main")

        steps = run_sync(cfg)
        sanity = _step(steps, "state sanity")
        assert sanity.ok is False
        assert "TASK-001" in sanity.detail


class TestSyncStepHardening:
    """Copilot review findings on #88."""

    def test_lock_precondition_reported_as_step(self, tmp_path):
        _, work = _setup_remote_pair(tmp_path)
        steps = run_sync(_cfg(work))
        assert steps[0].name == "no active run"
        assert steps[0].ok is True

    def test_failed_local_deletion_fails_the_step(self, tmp_path, monkeypatch):
        _, work = _setup_remote_pair(tmp_path)
        _make_branch(work, "task/task-009-x", "i.txt", merge=True, push=False)

        import subprocess as sp

        from spec_runner import sync_cmd

        real_run = sp.run

        def flaky(argv, **kwargs):
            if argv[:3] == ["git", "branch", "-d"]:
                return sp.CompletedProcess(argv, 1, stdout="", stderr="cannot delete")
            return real_run(argv, **kwargs)

        monkeypatch.setattr(sync_cmd.subprocess, "run", flaky)
        steps = run_sync(_cfg(work))
        local = _step(steps, "local managed branches")
        assert local.ok is False
        assert "FAILED to delete: task/task-009-x" in local.detail
        assert not all(s.ok for s in steps)
