"""A failed mandatory commit or merge never completes a task (PR #661 owner items 3, 4).

Item 3: with `auto_commit: true` a failed final commit, or a failed merge
stage, used to be logged while the hook still answered success — the task
was recorded DONE with its last edit uncommitted (measured in acceptance
round 1). Now the attempt is not successful: INSTRUMENT, nothing merged, and
tasks.md is not left DONE.

Item 4: the drift-check refusal left tasks.md DONE. Every refusal after the
DONE write now goes through one helper that puts the flip back.
"""

import pytest

from spec_runner.phases import Refusal, RefusalKind
from spec_runner.task import get_task_by_id, parse_tasks
from tests.test_candidate_commit import _cfg as cc_cfg
from tests.test_candidate_commit import _git as cc_git
from tests.test_candidate_commit import _repo as cc_repo
from tests.test_candidate_commit import _task as cc_task

pytestmark = pytest.mark.slow

BRANCH = "task/task-001-t"


def _no_gates(monkeypatch) -> None:
    from spec_runner import gates as gates_mod
    from spec_runner.gates import GateRegistry

    monkeypatch.setattr(gates_mod, "REGISTRY", GateRegistry())


def _status(root) -> str:
    task = get_task_by_id(parse_tasks(root / "spec" / "tasks.md"), "TASK-001")
    assert task is not None
    return task.status


def _branch_with_own_commit(tmp_path):
    root = cc_repo(tmp_path)
    base = cc_git(root, "branch", "--show-current").stdout.strip()
    cc_git(root, "checkout", "-qb", BRANCH)
    (root / "own.py").write_text("own\n")
    cc_git(root, "add", "-A")
    cc_git(root, "commit", "-qm", "TASK-001: own commit")
    return root, base


def _refs(root) -> str:
    return cc_git(root, "show-ref", "--heads").stdout


def test_failed_final_commit_is_not_done(tmp_path, monkeypatch):
    from spec_runner import hooks

    root, base = _branch_with_own_commit(tmp_path)
    (root / "work.py").write_text("late\n")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'rejected by policy' >&2\nexit 1\n")
    hook.chmod(0o755)
    _no_gates(monkeypatch)
    refs = _refs(root)
    cfg = cc_cfg(root, create_git_branch=True)

    ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)

    assert ok is False
    assert isinstance(err, Refusal) and err.kind == RefusalKind.INSTRUMENT
    assert "final commit" in err
    assert _refs(root) == refs, "something was merged"
    assert cc_git(root, "branch", "--show-current").stdout.strip() == BRANCH
    assert (root / "work.py").read_text() == "late\n"
    assert _status(root) != "done"
    assert "tasks.md" not in cc_git(root, "diff", "--cached", "--name-only").stdout


def test_a_conflicting_merge_is_not_done(tmp_path, monkeypatch):
    from spec_runner import hooks

    root, base = _branch_with_own_commit(tmp_path)
    cc_git(root, "checkout", "-q", base)
    (root / "own.py").write_text("base's own\n")
    cc_git(root, "add", "-A")
    cc_git(root, "commit", "-qm", "base moves on")
    base_before = cc_git(root, "rev-parse", base).stdout
    cc_git(root, "checkout", "-q", BRANCH)
    (root / "work.py").write_text("work\n")
    _no_gates(monkeypatch)
    cfg = cc_cfg(root, create_git_branch=True)

    ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)

    assert ok is False
    assert isinstance(err, Refusal) and err.kind == RefusalKind.INSTRUMENT
    assert "merge" in err.lower()
    assert cc_git(root, "rev-parse", base).stdout == base_before, "the base moved"
    assert cc_git(root, "branch", "--show-current").stdout.strip() == BRANCH
    assert not (root / ".git" / "MERGE_HEAD").exists(), "a half-done merge was left"
    assert _status(root) != "done", "the committed DONE flip was left in place"
    assert cc_git(root, "status", "--porcelain", "--", "spec/tasks.md").stdout == ""


def test_a_failed_switch_to_the_base_is_not_done(tmp_path, monkeypatch):
    import subprocess

    from spec_runner import hooks

    root, base = _branch_with_own_commit(tmp_path)
    (root / "work.py").write_text("work\n")
    _no_gates(monkeypatch)
    real_run = subprocess.run

    def run(cmd, *a, **k):
        if list(cmd) == ["git", "checkout", base]:
            return subprocess.CompletedProcess(cmd, 1, "", "error: simulated checkout failure")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(hooks.subprocess, "run", run)
    base_before = cc_git(root, "rev-parse", base).stdout
    cfg = cc_cfg(root, create_git_branch=True)

    ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)

    assert ok is False
    assert isinstance(err, Refusal) and err.kind == RefusalKind.INSTRUMENT
    assert "simulated checkout failure" in err
    assert cc_git(root, "rev-parse", base).stdout == base_before
    assert _status(root) != "done"


def test_a_clean_merge_still_completes(tmp_path, monkeypatch):
    from spec_runner import hooks

    root, base = _branch_with_own_commit(tmp_path)
    (root / "work.py").write_text("work\n")
    _no_gates(monkeypatch)
    cfg = cc_cfg(root, create_git_branch=True)

    ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)

    assert ok is True, err
    assert cc_git(root, "branch", "--show-current").stdout.strip() == base
    assert "work.py" in cc_git(root, "ls-tree", "--name-only", "HEAD").stdout
    assert _status(root) == "done"
