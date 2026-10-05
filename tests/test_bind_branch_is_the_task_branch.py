"""`--bind-branch` is the task's own branch, never main (pre-acceptance C1).

The remedy printed `--bind-branch <current branch>`. `prepare` runs before
`pre_start_hook`, and a run returns to main at its end, so the abandon note
and the strict refusal both said `--bind-branch main`; `harness trust`
accepted it, `wip.owner()` then made the task the owner of main, and the next
start committed `wip(TASK…)` straight onto main. Now the remedy names
`get_task_branch_name(task)` (with the checkout it needs) and `harness trust`
refuses any other branch.
"""

import os
import re
import subprocess
import sys

import pytest

from spec_runner import paid_call
from spec_runner.harness_cmd import TrustError, trust
from spec_runner.state import ExecutorState
from spec_runner.tdd import resolve_namespace
from tests.test_abandon_then_trust import _red_for_task_070
from tests.test_retry_continues_from_wip import BRANCH, _cfg, _fail_once, _git, _task, repo

__all__ = ["repo"]

pytestmark = pytest.mark.slow


def _spawn_spy(monkeypatch) -> list[int]:
    calls: list[int] = []

    def _spawn(invocation, *, timeout, cwd, env):
        calls.append(1)
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    monkeypatch.setattr(paid_call, "_spawn", _spawn)
    return calls


def _first_parent_subjects(repo) -> list[str]:
    return _git(repo, "log", "--first-parent", "main", "--format=%s").splitlines()


def _abandoned_on_main(repo, monkeypatch):
    from spec_runner.remedy import abandon

    _fail_once(repo, monkeypatch)
    cfg = _cfg(repo, max_retries=1, run_lint_on_done=False)
    (repo / "feature.py").unlink()
    cp = _red_for_task_070(cfg)
    _git(repo, "switch", "-q", "main")  # where a finished run leaves the operator
    (repo / "operator-notes.txt").write_text("not the task's\n")
    with ExecutorState(cfg) as st:
        abandon(cfg, st, "TASK-070", cp.checkpoint_id, reason="the red was wrong")
    return cfg


def test_follow_the_printed_remedy_literally(repo, monkeypatch):
    from spec_runner.execution import execute_task

    cfg = _abandoned_on_main(repo, monkeypatch)
    calls = _spawn_spy(monkeypatch)
    main_before = _first_parent_subjects(repo)

    with ExecutorState(cfg) as st:
        execute_task(_task(), cfg, st)
        refusal = st.get_task_state("TASK-070").attempts[-1].error or ""
    assert calls == []
    assert "--bind-branch main" not in refusal
    [command] = re.findall(r"`(git checkout [^`]+)`", refusal)
    assert f"--bind-branch {BRANCH}" in command

    with ExecutorState(cfg) as st, pytest.raises(TrustError, match="task's branch"):
        trust(cfg, st, "TASK-070", reason="wrong", bind_branch="main")

    env = {**os.environ, "PATH": f"{os.path.dirname(sys.executable)}:{os.environ['PATH']}"}
    env.pop("SPEC_RUNNER_AGENT", None)
    done = subprocess.run(
        ["bash", "-c", command], cwd=repo, env=env, capture_output=True, text=True
    )
    assert done.returncode == 0, done.stdout + done.stderr
    with ExecutorState(cfg) as st:
        ws = st.get_workspace(resolve_namespace(cfg), "TASK-070")
    assert ws is not None and ws["branch"] == BRANCH

    with ExecutorState(cfg) as st:
        execute_task(_task(), cfg, st)
    assert calls, "the retry did not proceed after the printed remedy"
    after = _first_parent_subjects(repo)
    new_on_main = after[: len(after) - len(main_before)]
    assert not any(s.startswith("wip(") or s.startswith("TASK-070:") for s in new_on_main), after


def test_bind_branch_main_is_refused(repo, monkeypatch):
    _fail_once(repo, monkeypatch)
    cfg = _cfg(repo, max_retries=1)
    ns = resolve_namespace(cfg)
    with ExecutorState(cfg) as st:
        st.forget_task_workspace(ns, "TASK-070")
    _git(repo, "stash", "-q", "-u")
    _git(repo, "switch", "-q", "main")
    with ExecutorState(cfg) as st:
        with pytest.raises(TrustError, match="task's branch"):
            trust(cfg, st, "TASK-070", reason="bind main", bind_branch="main")
        assert st.get_workspace(ns, "TASK-070") is None


def test_filling_a_null_row_with_another_branch_is_refused(repo, monkeypatch):
    _fail_once(repo, monkeypatch)
    cfg = _cfg(repo, max_retries=1)
    ns = resolve_namespace(cfg)
    with ExecutorState(cfg) as st:
        st.forget_task_workspace(ns, "TASK-070")
        st.record_workspace(ns, "TASK-070", branch=None, run_id=None)
    _git(repo, "stash", "-q", "-u")
    _git(repo, "switch", "-q", "-c", "task/other-task")
    with ExecutorState(cfg) as st:
        with pytest.raises(TrustError, match="task's branch"):
            trust(cfg, st, "TASK-070", reason="fill", bind_branch="task/other-task")
        assert st.get_workspace(ns, "TASK-070")["branch"] is None


def test_the_cli_exits_one(repo, monkeypatch, capsys):
    import argparse

    from spec_runner.harness_cmd import cmd_harness

    _fail_once(repo, monkeypatch)
    cfg = _cfg(repo, max_retries=1)
    with ExecutorState(cfg) as st:
        st.forget_task_workspace(resolve_namespace(cfg), "TASK-070")
    _git(repo, "stash", "-q", "-u")
    _git(repo, "switch", "-q", "main")
    code = cmd_harness(
        argparse.Namespace(
            harness_command="trust",
            task_id="TASK-070",
            reason="bind main",
            bind_branch="main",
            actor=None,
        ),
        cfg,
    )
    assert code == 1
    assert BRANCH in capsys.readouterr().out
