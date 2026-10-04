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


def _retry(cfg):
    """A separate `spec-runner retry` invocation: one attempt, attempts kept."""
    with ExecutorState(cfg) as state:
        task_state = state.get_task_state("TASK-070")
        task_state.status = "pending"
        state._save()
        result = execution.execute_task(_task(), cfg, state)
        last = state.get_task_state("TASK-070").attempts[-1]
    return result, last


def _idle(monkeypatch):
    def _spawn(invocation, *, timeout, cwd, env):
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    monkeypatch.setattr(paid_call, "_spawn", _spawn)


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

    _idle(monkeypatch)
    result, last = _retry(cfg)

    again = _baseline(cfg)
    assert result is not True
    assert last.error_kind == "harness_guard"
    assert again.captured_at == first.captured_at
    assert again.provenance == "initial"
    # The edit was never accepted: no baseline row carries it.
    assert "# addopts" not in str(again)


def test_harness_edit_in_red_commit_never_becomes_trusted(repo, monkeypatch):  # noqa: F811
    """Observed contract: the pre-GREEN refuse-and-restore (#658) puts the original
    bytes back in the tree, so the next invocation either refuses as
    `harness_guard` or runs against the ORIGINAL bytes. It never trusts the edit,
    and the baseline row (captured_at, provenance=initial) is never re-captured.
    Observed today: the red commit still differs from the baseline, so the next
    invocation is refused as `harness_guard`.
    """

    def _red(task, config, state, reporter):
        (repo / "pyproject.toml").write_text(EDITED)
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "TASK-070: red for x")
        return None

    monkeypatch.setattr(execution, "_run_red_phase_gate", _red)
    _idle(monkeypatch)
    cfg = _cfg(repo, max_retries=1, execution_mode="tdd")
    with ExecutorState(cfg) as st:
        execution.run_with_retries(_task(), cfg, st)
    first = _baseline(cfg)
    assert first.provenance == "initial"
    with ExecutorState(cfg) as st:
        first_attempt = st.get_task_state("TASK-070").attempts[-1]
    assert first_attempt.error_kind == "harness_guard"
    assert (repo / "pyproject.toml").read_text() == ORIGINAL  # restored in the tree

    result, last = _retry(cfg)

    again = _baseline(cfg)
    assert again.captured_at == first.captured_at
    assert again.provenance == "initial"
    tree = (repo / "pyproject.toml").read_text()
    refused = last.error_kind == "harness_guard"
    assert refused or tree == ORIGINAL, "the edit became the trusted oracle"
