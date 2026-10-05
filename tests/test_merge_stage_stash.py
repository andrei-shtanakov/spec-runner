"""The merge stage never strands work in an unnamed stash (pre-acceptance I1).

Reproduced: `auto_commit: false`, an uncommitted tracked edit, a base that
moved. `git checkout <base>` refused, a bare `git stash` took the edit, the
merge conflicted and the stage refused — the edit sat in an unlabelled
`stash@{0}: WIP on task/…`, the tree was clean, and the refusal said "the
work is committed on its branch". Now the stage's own stash is labelled,
popped back by its exact ref on every refusal after it, and the refusal
says what state the work is in. Stash entries the stage did not create are
never touched.
"""

import pytest

from spec_runner.phases import Refusal, RefusalKind
from tests.test_candidate_commit import _cfg as cc_cfg
from tests.test_candidate_commit import _git as cc_git
from tests.test_candidate_commit import _repo as cc_repo
from tests.test_candidate_commit import _task as cc_task
from tests.test_no_done_on_failed_delivery import BRANCH, _no_gates, _status

pytestmark = pytest.mark.slow


def _stash_shas(root) -> list[str]:
    return cc_git(root, "stash", "list", "--format=%H").stdout.split()


def _conflicting(tmp_path):
    root = cc_repo(tmp_path)
    base = cc_git(root, "branch", "--show-current").stdout.strip()
    (root / "own.py").write_text("base\n")
    (root / "other.py").write_text("other\n")
    cc_git(root, "add", "-A")
    cc_git(root, "commit", "-qm", "own.py on the base")
    # Someone else's stash entry, already there: never touched.
    (root / "other.py").write_text("someone's stash\n")
    cc_git(root, "stash", "push", "-q", "-m", "operator: keep me")
    cc_git(root, "checkout", "-qb", BRANCH)
    (root / "own.py").write_text("branch\n")
    cc_git(root, "add", "-A")
    cc_git(root, "commit", "-qm", "TASK-001: own commit")
    cc_git(root, "checkout", "-q", base)
    (root / "own.py").write_text("base moved\n")
    cc_git(root, "add", "-A")
    cc_git(root, "commit", "-qm", "base moves on")
    cc_git(root, "checkout", "-q", BRANCH)
    (root / "own.py").write_text("uncommitted edit\n")
    return root, base


def test_conflicting_merge_restores_the_stashed_edit(tmp_path, monkeypatch):
    from spec_runner import hooks

    root, base = _conflicting(tmp_path)
    stashes_before = _stash_shas(root)
    base_before = cc_git(root, "rev-parse", base).stdout
    _no_gates(monkeypatch)
    cfg = cc_cfg(root, create_git_branch=True, auto_commit=False)

    ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)

    assert ok is False
    assert isinstance(err, Refusal) and err.kind == RefusalKind.INSTRUMENT
    assert cc_git(root, "branch", "--show-current").stdout.strip() == BRANCH
    assert (root / "own.py").read_text() == "uncommitted edit\n", "the edit was stranded"
    assert _stash_shas(root) == stashes_before
    assert cc_git(root, "rev-parse", base).stdout == base_before
    assert "committed on its branch" not in err
    assert "uncommitted" in err and "working tree" in err
    assert _status(root) != "done"


def test_a_stash_that_cannot_be_popped_is_named(tmp_path, monkeypatch):
    import subprocess

    from spec_runner import hooks

    root, base = _conflicting(tmp_path)
    stashes_before = _stash_shas(root)
    _no_gates(monkeypatch)
    real_run = subprocess.run

    def run(cmd, *a, **k):
        if list(cmd[:3]) == ["git", "stash", "pop"]:
            return subprocess.CompletedProcess(cmd, 1, "", "error: simulated pop failure")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(hooks.subprocess, "run", run)
    cfg = cc_cfg(root, create_git_branch=True, auto_commit=False)

    ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)

    assert ok is False
    after = _stash_shas(root)
    assert after[1:] == stashes_before, "a stash the stage did not create was touched"
    listed = cc_git(root, "stash", "list").stdout
    [ours] = [line for line in listed.splitlines() if "spec-runner merge" in line]
    assert "TASK-001" in ours
    assert "spec-runner merge" in err and "git stash apply" in err


def test_double_fault_never_pops_onto_the_base(tmp_path, monkeypatch):
    """Conflicting merge, then the return checkout fails too: the stash is not
    popped onto the base's tree; it is named, with what to do once resolved."""
    import subprocess

    from spec_runner import hooks

    root, base = _conflicting(tmp_path)
    stashes_before = _stash_shas(root)
    _no_gates(monkeypatch)
    real_run = subprocess.run

    def run(cmd, *a, **k):
        if list(cmd) == ["git", "checkout", BRANCH]:
            return subprocess.CompletedProcess(cmd, 1, "", "error: simulated")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(hooks.subprocess, "run", run)
    cfg = cc_cfg(root, create_git_branch=True, auto_commit=False)

    ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)

    assert ok is False
    assert cc_git(root, "branch", "--show-current").stdout.strip() == base
    status = cc_git(root, "status", "--porcelain").stdout
    assert "UU" not in status and "AA" not in status, status
    assert (root / "own.py").read_text() == "base moved\n", "the stash was applied to the base"
    after = _stash_shas(root)
    assert after[1:] == stashes_before, "a stash the stage did not create was touched"
    listed = cc_git(root, "stash", "list").stdout
    assert any("spec-runner merge: TASK-001" in line for line in listed.splitlines())
    assert "spec-runner merge" in err and "git stash apply" in err
    assert f"git checkout {BRANCH}" in err
    assert "working tree of" not in err
