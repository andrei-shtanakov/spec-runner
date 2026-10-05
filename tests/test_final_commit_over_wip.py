"""A failed final commit over a WIP HEAD never merges or reaches DONE.

PR #661 acceptance round 1. `create_git_branch: true`, `auto_commit: true`,
no review and no gates: `wants_candidate` is False, so the final
`commit_task_work` is the only commit of the attempt. When it fails (here a
rejecting pre-commit hook) HEAD stays on the task's WIP commit; the final
stage handled only `"empty"`, so the branch whose tip is WIP was merged and
the task recorded DONE. The candidate-stage site already refused on
`("empty", "failed")`; both sites now agree.
"""

import pytest

from spec_runner.phases import Refusal, RefusalKind
from spec_runner.task import get_task_by_id, parse_tasks
from tests.test_candidate_commit import _cfg as cc_cfg
from tests.test_candidate_commit import _git as cc_git
from tests.test_candidate_commit import _repo as cc_repo
from tests.test_candidate_commit import _task as cc_task
from tests.test_wip_and_gates import _cc_branch_with_wip

pytestmark = pytest.mark.slow

BRANCH = "task/task-001-t"


def _reject_commits(root) -> None:
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'rejected by policy' >&2\nexit 1\n")
    hook.chmod(0o755)


def _no_gates(monkeypatch) -> None:
    from spec_runner import gates as gates_mod
    from spec_runner.gates import GateRegistry

    monkeypatch.setattr(gates_mod, "REGISTRY", GateRegistry())


def _status(root) -> str:
    task = get_task_by_id(parse_tasks(root / "spec" / "tasks.md"), "TASK-001")
    assert task is not None
    return task.status


def test_failed_final_commit_over_wip_is_refused(tmp_path, monkeypatch):
    from spec_runner import hooks

    root = _cc_branch_with_wip(tmp_path)
    wip_sha = cc_git(root, "rev-parse", "HEAD").stdout.strip()
    (root / "work.py").write_text("def f():\n    return 1\n")
    _reject_commits(root)
    _no_gates(monkeypatch)
    refs_before = cc_git(root, "show-ref", "--heads").stdout
    cfg = cc_cfg(root, create_git_branch=True)

    ok, err, _v, _f, _no_op = hooks.post_done_hook(cc_task(), cfg, True)

    assert ok is False
    assert isinstance(err, Refusal) and err.kind == RefusalKind.INSTRUMENT and err.terminal
    assert cc_git(root, "show-ref", "--heads").stdout == refs_before, "something was merged"
    assert cc_git(root, "branch", "--show-current").stdout.strip() == BRANCH
    assert cc_git(root, "rev-parse", "HEAD").stdout.strip() == wip_sha
    assert (root / "work.py").read_text() == "def f():\n    return 1\n"
    assert _status(root) != "done", "tasks.md says DONE for a refused task"


def test_failed_final_commit_over_wip_through_execute_task(tmp_path, monkeypatch):
    """End to end: the attempt is not a success and nothing reaches main."""
    import subprocess

    from spec_runner import paid_call
    from spec_runner.execution import execute_task
    from spec_runner.state import ExecutorState
    from spec_runner.tdd import resolve_namespace

    root = _cc_branch_with_wip(tmp_path)
    cfg = cc_cfg(root, create_git_branch=True, sync_deps=False)
    with ExecutorState(cfg) as st:
        st.record_workspace(resolve_namespace(cfg), "TASK-001", branch=BRANCH, run_id=None)
    cc_git(root, "checkout", "-q", "-")  # back to the base, as a fresh start finds it
    cc_git(root, "checkout", "-q", BRANCH)
    _reject_commits(root)
    _no_gates(monkeypatch)
    base_branch = [
        b
        for b in cc_git(root, "branch", "--format=%(refname:short)").stdout.split()
        if not b.startswith("task/")
    ][0]
    base_before = cc_git(root, "rev-parse", base_branch).stdout

    def _spawn(invocation, *, timeout, cwd, env):
        (root / "work.py").write_text("x = 1\n")
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    monkeypatch.setattr(paid_call, "_spawn", _spawn)
    with ExecutorState(cfg) as st:
        result = execute_task(cc_task(), cfg, st)
        ts = st.get_task_state("TASK-001")
        assert ts.status != "success"
        assert ts.attempts[-1].error_kind == "instrument"
    assert result == "TERMINAL_REFUSAL"
    assert cc_git(root, "rev-parse", base_branch).stdout == base_before
    assert (root / "work.py").exists()
    assert _status(root) != "done"


def test_failed_final_commit_without_wip_is_refused_too(tmp_path, monkeypatch):
    """No WIP. Acceptance round 1 pinned the old answer here: the failed commit
    was logged, the merge then failed on the uncommitted work, and the hook
    still answered success (DONE with the last edit uncommitted). Owner item 3
    retired it: the required commit failed, so the attempt is not successful
    (INSTRUMENT), nothing is merged and tasks.md is not left DONE."""
    from spec_runner import hooks

    root = cc_repo(tmp_path)
    base = cc_git(root, "branch", "--show-current").stdout.strip()
    cc_git(root, "checkout", "-qb", BRANCH)
    (root / "own.py").write_text("own\n")
    cc_git(root, "add", "-A")
    cc_git(root, "commit", "-qm", "TASK-001: own commit")
    own_sha = cc_git(root, "rev-parse", "HEAD").stdout.strip()
    base_before = cc_git(root, "rev-parse", base).stdout
    (root / "work.py").write_text("late\n")
    _reject_commits(root)
    _no_gates(monkeypatch)
    cfg = cc_cfg(root, create_git_branch=True)

    ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)

    assert ok is False
    assert isinstance(err, Refusal) and err.kind == RefusalKind.INSTRUMENT
    assert cc_git(root, "branch", "--show-current").stdout.strip() == BRANCH
    assert cc_git(root, "rev-parse", "HEAD").stdout.strip() == own_sha
    assert cc_git(root, "rev-parse", base).stdout == base_before
    assert (root / "work.py").read_text() == "late\n"
    assert _status(root) != "done"
