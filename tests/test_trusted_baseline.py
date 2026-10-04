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
        # Under spec/: pre_start's `git clean -fd --exclude=spec/` keeps it.
        "state_file": project / "spec" / "state.db",
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

    def test_a_tag_of_the_branch_name_is_not_a_branch(self, project):
        """Review #5: only refs/heads counts."""
        from spec_runner.git_ops import get_task_branch_name
        from spec_runner.harness import task_started

        _git_repo(project)
        subprocess.run(["git", "tag", get_task_branch_name(_task())], cwd=project, check=True)
        cfg = _cfg(project, create_git_branch=True)
        with ExecutorState(cfg) as st:
            assert task_started(cfg, st, _task()) is False

    def test_missing_git_means_no_branch(self, project, monkeypatch):
        """Review #4."""
        from spec_runner import git_ops
        from spec_runner.harness import task_started

        def no_git(*a, **k):
            raise FileNotFoundError("git")

        monkeypatch.setattr(git_ops.subprocess, "run", no_git)
        cfg = _cfg(project, create_git_branch=True)
        with ExecutorState(cfg) as st:
            assert task_started(cfg, st, _task()) is False

    def test_pre_capture_failures_do_not_count(self, project):
        from spec_runner.harness import task_started

        cfg = _cfg(project)
        with ExecutorState(cfg) as st:
            for stage in ("setup", "sync_deps", "branch"):
                st.record_attempt("TASK-050", False, 0.0, error="x", error_stage=stage)
        with ExecutorState(cfg) as st:
            assert task_started(cfg, st, _task()) is False
            st.record_attempt("TASK-050", False, 0.0, error="x", error_stage="codex")
            assert task_started(cfg, st, _task()) is True


def _git_repo(project: Path) -> None:
    def git(*args):
        subprocess.run(["git", *args], cwd=project, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")


class TestWorkspaceRecord:
    """Written after the capture, before the agent, in every guard mode (R3)."""

    def test_branching_records_the_task_branch(self, project, monkeypatch):
        from spec_runner.git_ops import get_task_branch_name

        _git_repo(project)
        _agent(monkeypatch)
        cfg = _cfg(project, create_git_branch=True)
        _run(cfg)
        with ExecutorState(cfg) as st:
            ws = st.get_workspace(resolve_namespace(cfg), "TASK-050")
        assert ws is not None and ws["branch"] == get_task_branch_name(_task())

    def test_a_failed_checkout_records_a_null_branch(self, project, monkeypatch):
        from spec_runner import git_ops

        _git_repo(project)
        _agent(monkeypatch)
        monkeypatch.setattr(git_ops, "current_branch", lambda config: "main")
        cfg = _cfg(project, create_git_branch=True)
        _run(cfg)
        with ExecutorState(cfg) as st:
            ws = st.get_workspace(resolve_namespace(cfg), "TASK-050")
        assert ws is not None and ws["branch"] is None

    def test_no_repository_still_records_the_start(self, project, monkeypatch):
        """Review #3: pre_start's no-git early return used to leave no row."""
        _agent(monkeypatch)
        _run(_cfg(project, create_git_branch=True, harness_guard="off"))
        called: list[int] = []
        _agent(monkeypatch, write=lambda: called.append(1))
        strict = _cfg(project, create_git_branch=True)
        assert _retry(strict, fresh=True) is not True and called == []
        assert _baseline(strict) is None, "captured as initial instead of refused"
        with ExecutorState(strict) as st:
            assert st.get_task_state("TASK-050").attempts[-1].error_kind == "policy"


class TestAFailedStartDoesNotLockTheTask:
    """R3: an attempt that stopped before the capture ran no agent."""

    def test_a_pre_start_failure_then_a_strict_retry_proceeds(self, project, monkeypatch):
        from spec_runner import execution

        cfg = _cfg(project)
        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: False)
        called: list[int] = []
        _agent(monkeypatch, write=lambda: called.append(1))
        assert _run(cfg) is not True and called == []
        with ExecutorState(cfg) as st:
            first = st.get_task_state("TASK-050").attempts[-1]
            assert st.get_workspace(resolve_namespace(cfg), "TASK-050") is None
        assert first.error_kind == "hook_failure" and first.error_stage == "setup"
        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
        _retry(cfg)
        stored = _baseline(cfg)
        assert called == [1], "the retry was refused"
        assert stored is not None and stored.provenance == "initial"

    def test_a_capture_write_failure_then_a_strict_retry_proceeds(self, project, monkeypatch):
        original = ExecutorState.store_harness_baseline

        def boom(self, *a, **k):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(ExecutorState, "store_harness_baseline", boom)
        called: list[int] = []
        _agent(monkeypatch, write=lambda: called.append(1))
        cfg = _cfg(project)
        assert _run(cfg) is not True and called == []
        monkeypatch.setattr(ExecutorState, "store_harness_baseline", original)
        with ExecutorState(cfg) as st:
            assert st.get_task_state("TASK-050").attempts[-1].error_stage == "setup"
        _retry(cfg)
        stored = _baseline(cfg)
        assert called == [1], "the retry was refused"
        assert stored is not None and stored.provenance == "initial"


def test_a_second_attempt_in_one_run_reuses_the_in_run_baseline(project, monkeypatch):
    """Review #6: attempt 2 sees attempt 1 (started) and the `initial` row it wrote."""
    from spec_runner import execution

    cfg = _cfg(project, max_retries=2)
    stores: list[int] = []
    original = ExecutorState.store_harness_baseline

    def counting(self, *a, **k):
        stores.append(1)
        return original(self, *a, **k)

    monkeypatch.setattr(ExecutorState, "store_harness_baseline", counting)
    seen: list[str] = []

    def post_done(*a, **k):
        stored = _baseline(cfg)
        assert stored is not None
        seen.append(stored.captured_at)
        return (False, "Tests failed", "skipped", "", False)

    monkeypatch.setattr(execution, "post_done_hook", post_done)
    called: list[int] = []
    _agent(monkeypatch, write=lambda: called.append(1))
    assert _run(cfg) is not True
    with ExecutorState(cfg) as st:
        attempts = st.get_task_state("TASK-050").attempts
    assert called == [1, 1] and len(attempts) == 2
    assert all(a.error_kind not in ("policy", "instrument") for a in attempts)
    assert stores == [1] and len(seen) == 2 and seen[0] == seen[1] != ""


def test_the_in_memory_baseline_carries_the_stored_timestamp(project):
    """Review #7: `captured_at` is the persisted one, not a placeholder."""
    from spec_runner.harness import HarnessBaseline

    cfg = _cfg(project)
    with ExecutorState(cfg) as st:
        baseline = HarnessBaseline()
        baseline.prepare(cfg, st, _task(), started=False)
        baseline.capture(cfg, st, _task())
        stored = st.get_harness_baseline(resolve_namespace(cfg), "TASK-050")
    assert stored is not None
    assert baseline._stored is not None
    assert baseline._stored.captured_at == stored.captured_at != ""
