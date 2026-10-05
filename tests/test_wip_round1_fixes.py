"""PR #661 blocker fixes, review round 1.

1. `reset` on an unreadable state DB: the exit-2 message, never a traceback.
2. `reset` takes the executor lock before saving WIP: a live run's tree is
   never committed under it.
3. `auto_commit: false` + a gate + HEAD on WIP is refused right after the
   start, before the pre-implementation gates and the paid call.
5. `task done` / `task sync-from-gh` refuse while a run holds the lock.
"""

import argparse
import subprocess
from pathlib import Path

import pytest

from spec_runner import paid_call
from spec_runner.config import ExecutorConfig, ExecutorLock
from spec_runner.phases import Refusal, RefusalKind
from spec_runner.state import ExecutorState
from tests.test_retry_continues_from_wip import BRANCH, _cfg, _fail_once, _git, _task, repo

__all__ = ["repo"]


def _lock(cfg: ExecutorConfig) -> ExecutorLock:
    lock = ExecutorLock(cfg.state_file.with_suffix(".lock"))
    assert lock.acquire()
    return lock


class TestResetUnreadableDb:
    def test_garbage_db_is_exit_two_and_untouched(self, tmp_path, monkeypatch, capsys):
        from spec_runner.cli import main

        (tmp_path / "spec").mkdir()
        db = tmp_path / "spec" / ".executor-state.db"
        db.write_bytes(b"this is not a sqlite database\x00" * 64)
        before = db.read_bytes()
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main(["reset"])
        assert exc.value.code == 2
        assert db.read_bytes() == before
        out = capsys.readouterr()
        assert "reset failed, the state DB is unchanged" in out.out
        assert "Traceback" not in out.out + out.err


@pytest.mark.slow
class TestResetUnderALiveRun:
    def test_reset_refuses_before_saving_wip(self, repo, monkeypatch, capsys):
        from spec_runner.cli_info import cmd_reset

        _fail_once(repo, monkeypatch)
        cfg = _cfg(repo, max_retries=1)
        head = _git(repo, "rev-parse", "HEAD")
        lock = _lock(cfg)
        try:
            with pytest.raises(SystemExit) as exc:
                cmd_reset(argparse.Namespace(logs=False), cfg)
        finally:
            lock.release()
        assert exc.value.code == 2
        assert _git(repo, "rev-parse", "HEAD") == head, "a WIP commit was made under the run"
        assert (repo / "feature.py").exists()
        assert "feature.py" in _git(repo, "status", "--porcelain")
        with ExecutorState(cfg) as st:
            assert len(st.get_task_state("TASK-070").attempts) == 1
        assert "lock" in capsys.readouterr().out

    def test_reset_without_a_run_still_saves(self, repo, monkeypatch):
        from spec_runner.cli_info import cmd_reset

        _fail_once(repo, monkeypatch)
        cfg = _cfg(repo, max_retries=1)
        cmd_reset(argparse.Namespace(logs=False), cfg)
        log = _git(
            repo, "log", BRANCH, "--format=%(trailers:key=Spec-Runner-WIP-Attempt,valueonly)"
        )
        assert [x for x in log.split() if x] == ["1"]
        # The lock was released: a run can start afterwards.
        _lock(cfg).release()


def _wip_head_start(repo: Path, monkeypatch, **cfg_kw):
    """A failed attempt left work; HEAD becomes WIP at the next start."""
    _fail_once(repo, monkeypatch)
    spawned: list[str] = []

    def _spawn(invocation, *, timeout, cwd, env):
        spawned.append(" ".join(invocation.argv))
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    monkeypatch.setattr(paid_call, "_spawn", _spawn)
    return _cfg(repo, max_retries=1, auto_commit=False, **cfg_kw), spawned


@pytest.mark.slow
class TestRefusedBeforeThePaidCall:
    def test_standard_task_with_a_gate(self, repo, monkeypatch):
        from spec_runner.execution import execute_task
        from tests.test_candidate_commit import _recording_gate

        cfg, spawned = _wip_head_start(repo, monkeypatch)
        seen = _recording_gate(monkeypatch)
        with ExecutorState(cfg) as st:
            result = execute_task(_task(), cfg, st)
            last = st.get_task_state("TASK-070").attempts[-1]
        assert result == "TERMINAL_REFUSAL"
        assert spawned == [], "the paid call ran over a WIP HEAD"
        assert seen == []
        assert last.error_kind == "policy"
        assert "auto_commit" in (last.error or "")
        body = _git(repo, "log", "-1", "--format=%B")
        assert "Spec-Runner-WIP: TASK-070" in body, "HEAD is not the WIP (setup)"

    def test_tdd_task_never_reaches_the_red_gate(self, repo, monkeypatch):
        from spec_runner import execution

        cfg, spawned = _wip_head_start(repo, monkeypatch, execution_mode="tdd")
        red: list[int] = []
        monkeypatch.setattr(execution, "_run_red_phase_gate", lambda *a, **k: red.append(1) or None)
        with ExecutorState(cfg) as st:
            result = execution.execute_task(_task(), cfg, st)
        assert result == "TERMINAL_REFUSAL"
        assert red == [] and spawned == []

    @pytest.mark.parametrize(
        "extra",
        [{"run_review": False}, {"run_review": True, "review_policy": "advisory"}],
        ids=["no-review", "advisory-review"],
    )
    def test_refused_without_any_gate(self, repo, monkeypatch, extra):
        """Round 2: no gate, no or advisory review — WIP still is no candidate."""
        from spec_runner import gates as gates_mod
        from spec_runner import hooks
        from spec_runner.execution import execute_task
        from spec_runner.gates import GateRegistry
        from spec_runner.task import get_task_by_id, parse_tasks

        cfg, spawned = _wip_head_start(repo, monkeypatch, run_lint_on_done=False, **extra)
        monkeypatch.setattr(gates_mod, "REGISTRY", GateRegistry())
        reviewed: list[int] = []
        monkeypatch.setattr(hooks, "run_code_review", lambda *a, **k: reviewed.append(1))
        main_before = _git(repo, "rev-parse", "main")
        with ExecutorState(cfg) as st:
            result = execute_task(_task(), cfg, st)
            ts = st.get_task_state("TASK-070")
            last = ts.attempts[-1]
            assert ts.status != "success"
        assert result == "TERMINAL_REFUSAL"
        assert spawned == [] and reviewed == []
        assert last.error_kind == "policy"
        assert "auto_commit: true" in (last.error or "")
        assert "squash" in (last.error or "")
        assert _git(repo, "rev-parse", "main") == main_before, "something was merged"
        assert _git(repo, "branch", "--show-current").strip() == BRANCH
        task = get_task_by_id(parse_tasks(repo / "spec" / "tasks.md"), "TASK-070")
        assert task is not None and task.status != "done"

    def test_head_not_on_wip_runs_as_before(self, repo, monkeypatch):
        """A first start under `auto_commit: false`: no WIP, nothing refused."""
        from spec_runner import gates as gates_mod
        from spec_runner.execution import execute_task
        from spec_runner.gates import GateRegistry

        spawned: list[int] = []

        def _spawn(invocation, *, timeout, cwd, env):
            spawned.append(1)
            (repo / "feature.py").write_text("x\n")
            return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

        monkeypatch.setattr(paid_call, "_spawn", _spawn)
        monkeypatch.setattr(gates_mod, "REGISTRY", GateRegistry())
        cfg = _cfg(repo, max_retries=1, auto_commit=False, run_lint_on_done=False)
        with ExecutorState(cfg) as st:
            result = execute_task(_task(), cfg, st)
            assert st.get_task_state("TASK-070").status == "success"
        assert result is True
        assert spawned == [1]
        assert "Spec-Runner-WIP" not in _git(repo, "log", "--all", "--format=%B")


TASKS_MD = "# Tasks\n\n### TASK-001: one\n🔴 P0 | 🔄 IN_PROGRESS | Est: 1d\n\n- [x] done\n"


class TestTaskDoneUnderALiveRun:
    @pytest.mark.parametrize("command", ["done", "sync-from-gh"])
    def test_refused_and_nothing_written(self, tmp_path, monkeypatch, command, capsys):
        from spec_runner import github_sync
        from spec_runner.cli import _dispatch_task_command
        from spec_runner.task import update_task_status
        from tests.test_done_paths_forget_workspace import _rows, _seed

        (tmp_path / "spec").mkdir()
        tasks = tmp_path / "spec" / "tasks.md"
        tasks.write_text(TASKS_MD)
        monkeypatch.chdir(tmp_path)
        cfg = ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "spec" / ".state.db")
        _seed(cfg, "TASK-001")
        monkeypatch.setattr(
            github_sync,
            "cmd_sync_from_gh",
            lambda a, t, f: update_task_status(f, "TASK-001", "done"),
        )
        args = argparse.Namespace(
            command="task",
            task_command=command,
            task_id="TASK-001",
            force=False,
            spec_prefix="",
            change="",
        )
        lock = _lock(cfg)
        try:
            with pytest.raises(SystemExit) as exc:
                _dispatch_task_command(args, cfg)
        finally:
            lock.release()
        assert exc.value.code == 2
        assert tasks.read_text() == TASKS_MD
        assert _rows(cfg) == (1, 1, 1, 1)
        assert "run" in capsys.readouterr().err


def test_refusal_kind_is_policy_and_terminal_at_the_start(tmp_path):
    """The start-site refusal is the same object as post_done's."""
    from spec_runner.hooks import _no_candidate_over_wip_refusal

    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@e.c")
    _git(tmp_path, "config", "user.name", "T")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "base")
    _git(tmp_path, "switch", "-q", "-c", BRANCH)
    _git(
        tmp_path,
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "wip(TASK-070): x\n\nSpec-Runner-WIP: TASK-070\nSpec-Runner-WIP-Attempt: 1",
    )
    refusal = _no_candidate_over_wip_refusal(_task(), _cfg(tmp_path, auto_commit=False))
    assert isinstance(refusal, Refusal)
    assert refusal.kind is RefusalKind.POLICY and refusal.terminal
