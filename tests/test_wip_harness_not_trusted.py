"""A harness edit carried in WIP or a red commit is never the next oracle (spec §4).

Each follow-up "invocation" opens a new `ExecutorState` on the same DB file and
runs ONE attempt the way `cli.cmd_retry` does.
"""

import subprocess

from spec_runner import execution, paid_call
from spec_runner.state import ExecutorState
from spec_runner.tdd import resolve_namespace
from tests.test_retry_continues_from_wip import _cfg, _git, _task
from tests.test_retry_continues_from_wip import repo as repo  # noqa: F401

EDITED = "[project]\nname='d'\n# addopts\n"
ORIGINAL = "[project]\nname='d'\n"


def _baseline(cfg):
    with ExecutorState(cfg) as st:
        return st.get_harness_baseline(resolve_namespace(cfg), "TASK-070")


def _attempts(cfg):
    with ExecutorState(cfg) as st:
        return list(st.get_task_state("TASK-070").attempts)


def _retry(cfg):
    """A separate `spec-runner retry` invocation: one attempt, attempts kept.

    Returns (result, attempts_before, attempts_after).
    """
    before = len(_attempts(cfg))
    with ExecutorState(cfg) as state:
        task_state = state.get_task_state("TASK-070")
        task_state.status = "pending"
        state._save()
        result = execution.execute_task(_task(), cfg, state)
    return result, before, _attempts(cfg)


def _assert_never_trusted(cfg, root, first, *, row_dropped=False, head_original=True):
    again = _baseline(cfg)
    if row_dropped:  # a legitimate DONE drops the baseline with the workspace
        assert again is None
    else:
        assert again.captured_at == first.captured_at
        assert again.provenance == "initial"
        assert again.files["pyproject.toml"] == ORIGINAL.encode()
        assert again.surface == first.surface
    if head_original:
        assert _git(root, "show", "HEAD:pyproject.toml") == ORIGINAL


def _idle(monkeypatch):
    calls: list[int] = []

    def _spawn(invocation, *, timeout, cwd, env):
        calls.append(1)
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    monkeypatch.setattr(paid_call, "_spawn", _spawn)
    return calls


def test_harness_edit_in_wip_is_refused_by_the_next_invocation(repo, monkeypatch):  # noqa: F811
    def _edit_and_time_out(invocation, *, timeout, cwd, env):
        (repo / "pyproject.toml").write_text(EDITED)
        raise subprocess.TimeoutExpired(invocation.argv, timeout)

    monkeypatch.setattr(paid_call, "_spawn", _edit_and_time_out)
    cfg = _cfg(repo, max_retries=1)
    with ExecutorState(cfg) as st:
        execution.run_with_retries(_task(), cfg, st)
    first = _baseline(cfg)
    assert first.provenance == "initial"
    first_attempts = _attempts(cfg)
    assert len(first_attempts) == 1
    assert first_attempts[0].error_kind == "timeout"

    calls = _idle(monkeypatch)
    result, before, after = _retry(cfg)

    assert result is False
    assert len(after) == before + 1
    assert after[-1].error_kind == "harness_guard"
    assert calls == [1]  # the guard fires after the agent call, on the carried edit
    # HEAD is the WIP commit and legitimately carries the edit; the baseline does not.
    _assert_never_trusted(cfg, repo, first, head_original=False)


def test_harness_edit_in_red_commit_never_becomes_trusted(repo, monkeypatch):  # noqa: F811
    """The red commit carries the edit; the retry's stub does NOT re-inject it.

    Attempt 1: the stub commits the edit; the pre-GREEN refuse-and-restore
    (#658) refuses as `harness_guard` and puts the original bytes back in the
    tree, but the red commit stays in history.

    Retry (separate invocation), observed: the tree already holds the original
    bytes (the restore in attempt 1 is the working-tree state; the red commit is
    reachable only in history), so the tree matches the stored baseline, the
    guard finds no violation and the retry succeeds legitimately. The edit is
    not trusted: it was never captured as a baseline (attempt 1's row holds
    the original bytes, asserted above via `first`), and HEAD carries the
    original bytes. After the legitimate DONE the baseline row is dropped with the workspace (Task 9), so it cannot be re-captured.
    """
    red_calls: list[int] = []

    def _red(task, config, state, reporter):
        red_calls.append(1)
        if len(red_calls) > 1:
            return None
        (repo / "pyproject.toml").write_text(EDITED)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "TASK-070: red for x")
        return None

    monkeypatch.setattr(execution, "_run_red_phase_gate", _red)
    calls = _idle(monkeypatch)
    cfg = _cfg(repo, max_retries=1, execution_mode="tdd", run_lint_on_done=False)
    with ExecutorState(cfg) as st:
        execution.run_with_retries(_task(), cfg, st)
    first = _baseline(cfg)
    assert first.provenance == "initial"
    first_attempts = _attempts(cfg)
    assert [a.error_kind for a in first_attempts] == ["harness_guard"]
    assert (repo / "pyproject.toml").read_text() == ORIGINAL  # restored in the tree
    assert calls == []

    result, before, after = _retry(cfg)

    assert len(red_calls) == 2
    assert len(after) == before + 1
    assert result is True
    assert after[-1].error_kind is None
    assert calls == [1]  # the retry ran its GREEN agent call
    assert (repo / "pyproject.toml").read_text() == ORIGINAL
    _assert_never_trusted(cfg, repo, first, row_dropped=True)
