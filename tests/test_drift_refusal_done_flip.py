"""A drift-check refusal does not leave tasks.md DONE (PR #661 owner item 4).

The drift check runs after the DONE write (and, under `auto_commit`, after
the final commit that carries it). Its refusal now goes through the same
helper as every other refusal after the DONE write, which puts the flip back.
"""

import pytest

from tests.test_candidate_commit import _cfg as cc_cfg
from tests.test_candidate_commit import _git as cc_git
from tests.test_candidate_commit import _task as cc_task
from tests.test_no_done_on_failed_delivery import (
    BRANCH,
    _branch_with_own_commit,
    _no_gates,
    _status,
)

pytestmark = pytest.mark.slow


def test_drift_refusal_puts_the_done_flip_back(tmp_path, monkeypatch):
    from spec_runner import hooks

    root, base = _branch_with_own_commit(tmp_path)
    (root / "work.py").write_text("work\n")
    _no_gates(monkeypatch)
    monkeypatch.setattr(
        hooks, "_detect_candidate_drift", lambda c, sha, t: "the tree moved under the gate"
    )
    base_before = cc_git(root, "rev-parse", base).stdout
    cfg = cc_cfg(root, create_git_branch=True)

    ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)

    assert ok is False and "moved" in err
    assert _status(root) != "done"
    assert cc_git(root, "status", "--porcelain", "--", "spec/tasks.md").stdout == ""
    assert cc_git(root, "rev-parse", base).stdout == base_before, "something was merged"
    assert cc_git(root, "branch", "--show-current").stdout.strip() == BRANCH


def test_drift_refusal_without_auto_commit_restores_the_tree(tmp_path, monkeypatch):
    from spec_runner import hooks

    root, _base = _branch_with_own_commit(tmp_path)
    before = (root / "spec" / "tasks.md").read_text()
    _no_gates(monkeypatch)
    monkeypatch.setattr(hooks, "_detect_candidate_drift", lambda c, sha, t: "moved")
    cfg = cc_cfg(root, create_git_branch=True, auto_commit=False)

    ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)

    assert ok is False
    assert (root / "spec" / "tasks.md").read_text() == before


def test_a_blocking_post_done_plugin_does_not_leave_done(tmp_path, monkeypatch):
    """The last refusal after the DONE write (after the merge): same helper."""
    from spec_runner import hooks

    root, base = _branch_with_own_commit(tmp_path)
    (root / "work.py").write_text("work\n")
    _no_gates(monkeypatch)
    real = hooks.run_plugin_hooks_for

    def plugin(point, task, config, success):
        if point == "post_done":
            return "Blocking plugin 'notify' failed in post_done"
        return real(point, task, config, success=success)

    monkeypatch.setattr(hooks, "run_plugin_hooks_for", plugin)
    cfg = cc_cfg(root, create_git_branch=True)

    ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)

    assert ok is False and "notify" in err
    assert cc_git(root, "branch", "--show-current").stdout.strip() == base
    assert _status(root) != "done"
    assert cc_git(root, "status", "--porcelain", "--", "spec/tasks.md").stdout == ""


def test_every_return_after_the_done_write_goes_through_the_helper():
    """Structural: past the DONE write, a refusal is `_refuse_after_done_write`."""
    import inspect
    import re

    from spec_runner import hooks

    source = inspect.getsource(hooks.post_done_hook)
    after = source[source.index("tasks_before = config.tasks_file.read_text()") :]
    raw_refusals = re.findall(r"return \(\s*False,", after)
    assert raw_refusals == [], "a refusal after the DONE write bypasses the helper"
