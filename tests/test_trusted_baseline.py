"""The harness baseline is persisted before the first attempt and reused (spec §2).

Each "invocation" below opens its own `ExecutorState` — a new connection on
the same DB file — the way a separate `spec-runner` process would. A later
invocation is a `spec-runner retry`: `cli.cmd_retry` runs ONE attempt through
`execute_task` (keeping the recorded attempts), or clears them first under
`--fresh`. A second `run_with_retries` with `max_retries=1` would run nothing,
since it loops from the recorded attempt count.
"""

import sqlite3
import subprocess
from pathlib import Path

import pytest

from spec_runner import paid_call
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import resolve_namespace

PYPROJECT = "[project]\nname = 'demo'\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "tasks.md").write_text(
        "# Spec\n\n## M0\n\n### TASK-050: work\n🔴 P0 | ⬜ TODO | Est: 1d\n\n"
        "**Description:** work\n\n**Checklist:**\n- [ ] do it\n\n"
        "**Traces to:** [REQ-0]\n**Depends on:** —\n"
    )
    (tmp_path / "logs").mkdir()
    return tmp_path


def _cfg(project: Path, **kw) -> ExecutorConfig:
    base = {
        "state_file": project / "state.db",
        "project_root": project,
        "logs_dir": project / "logs",
        "create_git_branch": False,
        "auto_commit": False,
        "run_tests_on_done": False,
        "run_review": False,
        "sync_deps": False,
        "harness_guard": "strict",
        "max_retries": 1,
        "retry_delay_seconds": 0,
    }
    base.update(kw)
    return ExecutorConfig(**base)


def _task() -> Task:
    return Task(
        id="TASK-050",
        name="work",
        priority="p0",
        status="todo",
        milestone="M0",
        description="work",
        estimate="1d",
    )


def _agent(monkeypatch, write=None):
    def _spawn(invocation, *, timeout, cwd, env):
        if write:
            write()
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    monkeypatch.setattr(paid_call, "_spawn", _spawn)


@pytest.fixture(autouse=True)
def no_hooks(monkeypatch):
    from spec_runner import execution

    monkeypatch.setattr(
        execution, "post_done_hook", lambda *a, **k: (False, "Tests failed", "skipped", "", False)
    )


def _run(cfg):
    """A first `spec-runner run` invocation."""
    from spec_runner.execution import run_with_retries

    with ExecutorState(cfg) as state:
        return run_with_retries(_task(), cfg, state)


def _retry(cfg, *, fresh: bool = False):
    """A separate `spec-runner retry` invocation, as `cli.cmd_retry` runs it."""
    from spec_runner.execution import execute_task

    with ExecutorState(cfg) as state:
        task_state = state.get_task_state("TASK-050")
        if fresh:
            task_state.attempts = []
        task_state.status = "pending"
        state._save()
        return execute_task(_task(), cfg, state)


def _baseline(cfg):
    with ExecutorState(cfg) as st:
        return st.get_harness_baseline(resolve_namespace(cfg), "TASK-050")


def test_a_separate_retry_reuses_the_original_baseline(project, monkeypatch):
    cfg = _cfg(project)
    _agent(monkeypatch)
    _run(cfg)
    first = _baseline(cfg)
    assert first is not None and first.provenance == "initial"
    (project / "pyproject.toml").write_text(PYPROJECT + "# drift\n")
    _retry(cfg)  # new ExecutorState, new connection, same DB file
    again = _baseline(cfg)
    with ExecutorState(cfg) as st:
        attempts = st.get_task_state("TASK-050").attempts
    assert again is not None
    assert again.captured_at == first.captured_at and again.provenance == "initial"
    assert len(attempts) == 2, "the retry did not run an attempt"
    assert attempts[-1].error_kind == "harness_guard"


def test_a_fresh_retry_also_reuses_the_original_baseline(project, monkeypatch):
    """`retry --fresh` clears the attempts; the workspace row still says started."""
    cfg = _cfg(project)
    _agent(monkeypatch)
    _run(cfg)
    first = _baseline(cfg)
    (project / "pyproject.toml").write_text(PYPROJECT + "# drift\n")
    _retry(cfg, fresh=True)
    again = _baseline(cfg)
    with ExecutorState(cfg) as st:
        attempts = st.get_task_state("TASK-050").attempts
    assert again is not None and first is not None
    assert again.captured_at == first.captured_at
    assert attempts[-1].error_kind == "harness_guard"


def test_strict_refuses_a_started_task_without_baseline(project, monkeypatch):
    cfg = _cfg(project, harness_guard="off")
    _agent(monkeypatch)
    _run(cfg)  # started under off: workspace row, no snapshot
    with ExecutorState(cfg) as st:
        assert st.get_workspace(resolve_namespace(cfg), "TASK-050") is not None
    assert _baseline(cfg) is None
    called: list[int] = []
    _agent(monkeypatch, write=lambda: called.append(1))
    strict = _cfg(project)
    result = _retry(strict, fresh=True)  # only the workspace row says "started"
    assert result is not True and called == []
    assert _baseline(strict) is None, "a refused start must not capture"
    with ExecutorState(strict) as st:
        last = st.get_task_state("TASK-050").attempts[-1]
    assert last.error_kind == "policy" and last.error_stage == "setup"
    assert "harness trust" in (last.error or "")


def test_warn_recaptures_but_strict_does_not_trust_it(project, monkeypatch):
    _agent(monkeypatch)
    _run(_cfg(project, harness_guard="off"))
    _retry(_cfg(project, harness_guard="warn"))
    stored = _baseline(_cfg(project))
    assert stored is not None and stored.provenance == "recaptured"
    called: list[int] = []
    _agent(monkeypatch, write=lambda: called.append(1))
    assert _retry(_cfg(project)) is not True and called == []


def test_a_new_task_captures_initial_under_warn(project, monkeypatch):
    _agent(monkeypatch)
    _run(_cfg(project, harness_guard="warn"))
    stored = _baseline(_cfg(project))
    assert stored is not None and stored.provenance == "initial"
    assert stored.guard_mode == "warn"
    assert stored.files["pyproject.toml"] == PYPROJECT.encode()


def test_off_records_the_workspace_but_no_baseline(project, monkeypatch):
    cfg = _cfg(project, harness_guard="off")
    _agent(monkeypatch)
    _run(cfg)
    with ExecutorState(cfg) as st:
        ws = st.get_workspace(resolve_namespace(cfg), "TASK-050")
    assert ws is not None and ws["branch"] is None
    assert _baseline(cfg) is None


def test_db_read_failure_refuses_before_the_agent(project, monkeypatch):
    def boom(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ExecutorState, "get_harness_baseline", boom)
    called: list[int] = []
    _agent(monkeypatch, write=lambda: called.append(1))
    cfg = _cfg(project)
    assert _run(cfg) is not True and called == []
    monkeypatch.undo()
    with ExecutorState(cfg) as st:
        last = st.get_task_state("TASK-050").attempts[-1]
        ws = st.get_workspace(resolve_namespace(cfg), "TASK-050")
    assert last.error_kind == "instrument" and last.error_stage == "setup"
    assert ws is None, "the refusal must come before pre_start"


def test_db_write_failure_refuses_before_the_agent(project, monkeypatch):
    def boom(self, *a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ExecutorState, "store_harness_baseline", boom)
    called: list[int] = []
    _agent(monkeypatch, write=lambda: called.append(1))
    cfg = _cfg(project)
    assert _run(cfg) is not True and called == []
    monkeypatch.undo()
    with ExecutorState(cfg) as st:
        last = st.get_task_state("TASK-050").attempts[-1]
    assert last.error_kind == "instrument"


def test_workspace_record_failure_is_an_instrument_refusal(project, monkeypatch):
    def boom(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ExecutorState, "record_workspace", boom)
    called: list[int] = []
    _agent(monkeypatch, write=lambda: called.append(1))
    cfg = _cfg(project, harness_guard="off")
    assert _run(cfg) is not True and called == []
    monkeypatch.undo()
    with ExecutorState(cfg) as st:
        last = st.get_task_state("TASK-050").attempts[-1]
    assert last.error_kind == "instrument"


class TestTaskStarted:
    def test_nothing_recorded_is_not_started(self, project):
        from spec_runner.harness import task_started

        cfg = _cfg(project)
        with ExecutorState(cfg) as st:
            assert task_started(cfg, st, _task()) is False

    def test_a_recorded_attempt_is_started(self, project):
        from spec_runner.harness import task_started

        cfg = _cfg(project)
        with ExecutorState(cfg) as st:
            st.record_attempt("TASK-050", False, 0.0, error="x")
        with ExecutorState(cfg) as st:
            assert task_started(cfg, st, _task()) is True

    def test_an_existing_task_branch_is_started(self, project):
        from spec_runner.git_ops import get_task_branch_name
        from spec_runner.harness import task_started

        def git(*args):
            subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)

        git("init", "-q", "-b", "main")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i")
        cfg = _cfg(project, create_git_branch=True)
        with ExecutorState(cfg) as st:
            assert task_started(cfg, st, _task()) is False
            git("branch", get_task_branch_name(_task()))
            assert task_started(cfg, st, _task()) is True
            # Without branching, a branch of that name proves nothing.
            assert task_started(_cfg(project), st, _task()) is False


class TestPreStartRecordsTheWorkspace:
    """`pre_start_hook(state=...)` binds the workspace to the branch it checked out."""

    def _repo(self, project: Path) -> None:
        def git(*args):
            subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)

        git("init", "-q", "-b", "main")
        git("add", "-A")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")

    def test_branching_records_the_task_branch(self, project):
        from spec_runner.git_ops import get_task_branch_name
        from spec_runner.hooks import pre_start_hook

        self._repo(project)
        cfg = _cfg(project, create_git_branch=True)
        with ExecutorState(cfg) as st:
            assert pre_start_hook(_task(), cfg, state=st) is True
            ws = st.get_workspace(resolve_namespace(cfg), "TASK-050")
        assert ws is not None and ws["branch"] == get_task_branch_name(_task())

    def test_a_failed_checkout_records_nothing(self, project, monkeypatch):
        from spec_runner import hooks

        self._repo(project)
        monkeypatch.setattr(hooks, "current_branch", lambda config: "main")
        cfg = _cfg(project, create_git_branch=True)
        with ExecutorState(cfg) as st:
            hooks.pre_start_hook(_task(), cfg, state=st)
            assert st.get_workspace(resolve_namespace(cfg), "TASK-050") is None

    def test_without_state_nothing_is_recorded(self, project):
        from spec_runner.hooks import pre_start_hook

        cfg = _cfg(project)
        assert pre_start_hook(_task(), cfg) is True
        with ExecutorState(cfg) as st:
            assert st.get_workspace(resolve_namespace(cfg), "TASK-050") is None
