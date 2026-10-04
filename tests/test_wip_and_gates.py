"""WIP never feeds a gate; red adoption sees through a WIP chain (spec §4)."""

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.task import Task
from spec_runner.wip import WIP_ATTEMPT_TRAILER, WIP_TRAILER

SEL = "tests/test_x.py::test_x"


def _git(root, *a):
    return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True).stdout


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@e.c")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "a.py").write_text("1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-qb", "task/task-080-w")
    return tmp_path


def _commit(root, name, msg):
    (root / name).write_text(name)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)


def _wip(root, name, task="TASK-080", attempt=1):
    _commit(
        root, name, f"wip({task}): x\n\n{WIP_TRAILER}: {task}\n{WIP_ATTEMPT_TRAILER}: {attempt}"
    )


def _task():
    return Task(id="TASK-080", name="w", priority="p0", status="todo", estimate="1d")


class _St:
    def checkpoint_exists_for_commit(self, ns, sha):
        return False


def test_red_found_through_a_wip_chain(tmp_path):
    from spec_runner.tdd import _unregistered_red

    root = _repo(tmp_path)
    _commit(root, "t.py", f"TASK-080: red for {SEL}")
    red = _git(root, "rev-parse", "HEAD").strip()
    _wip(root, "w1.py")
    _wip(root, "w2.py", attempt=2)
    cfg = ExecutorConfig(project_root=root, create_git_branch=True)
    assert _unregistered_red(cfg, _St(), _task(), SEL) == red


def test_chain_stops_at_another_commit(tmp_path):
    from spec_runner.tdd import _unregistered_red

    root = _repo(tmp_path)
    _commit(root, "t.py", f"TASK-080: red for {SEL}")
    _commit(root, "o.py", "someone else")
    _wip(root, "w1.py")
    cfg = ExecutorConfig(project_root=root, create_git_branch=True)
    assert _unregistered_red(cfg, _St(), _task(), SEL) == ""


def test_other_tasks_wip_is_not_skipped(tmp_path):
    from spec_runner.tdd import _unregistered_red

    root = _repo(tmp_path)
    _commit(root, "t.py", f"TASK-080: red for {SEL}")
    _wip(root, "w1.py", task="TASK-999")
    cfg = ExecutorConfig(project_root=root, create_git_branch=True)
    assert _unregistered_red(cfg, _St(), _task(), SEL) == ""


def test_candidate_over_wip_is_explicit(tmp_path):
    from spec_runner.hooks import commit_candidate_over_wip

    root = _repo(tmp_path)
    _wip(root, "w1.py")
    cfg = ExecutorConfig(project_root=root, create_git_branch=True, auto_commit=True)
    commit_candidate_over_wip(_task(), cfg)
    assert _git(root, "log", "-1", "--format=%s").strip() == "TASK-080: candidate"


def test_noop_is_cumulative(tmp_path):
    from spec_runner.hooks import task_changed_since_base

    root = _repo(tmp_path)
    cfg = ExecutorConfig(project_root=root, create_git_branch=True)
    _wip(root, "w1.py")
    assert task_changed_since_base(cfg) is True
    (root / "w1.py").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "undo")
    assert task_changed_since_base(cfg) is False


def test_wip_read_failure_fails_closed(tmp_path, monkeypatch):
    import spec_runner.wip as wipmod
    from spec_runner.hooks import post_done_hook
    from spec_runner.phases import Refusal, RefusalKind

    root = _repo(tmp_path)
    _commit(root, "own.py", "TASK-080: own commit")
    cfg = ExecutorConfig(
        project_root=root,
        create_git_branch=True,
        auto_commit=True,
        run_tests_on_done=False,
        run_lint_on_done=False,
        run_review=False,
    )

    def boom(*a, **k):
        raise wipmod.WipReadError("rev-list failed")

    monkeypatch.setattr(wipmod, "wip_commits", boom)
    ok, err, _v, _f, _n = post_done_hook(_task(), cfg, True)
    assert ok is False
    assert isinstance(err, Refusal)
    assert err.kind == RefusalKind.INSTRUMENT
