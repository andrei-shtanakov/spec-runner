"""Every final-DONE path drops the task's workspace and baseline (PR #661 blocker 3).

Spec 2026-10-04 §2 Lifecycle: workspace, snapshot and file rows are deleted
together, and atomically with the record that ends the task. Only the
`record_attempt` DONE site and `tdd abandon` did it; `tdd complete`,
`task done`, `task sync-from-gh` and the stale-run reconciliation closed a
task and left the rows behind. The trust audit is never deleted.
"""

import argparse
import sqlite3
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.remedy import RemedyError, complete
from spec_runner.state import ExecutorState
from spec_runner.tdd import resolve_namespace
from tests.test_tdd_complete import REASON, TASK, _recorded, _wedged


def _seed(cfg: ExecutorConfig, task_id: str) -> str:
    ns = resolve_namespace(cfg)
    with ExecutorState(cfg) as st:
        st.record_workspace(ns, task_id, branch=f"task/{task_id.lower()}", run_id=None)
        st.store_harness_baseline(
            ns,
            task_id,
            provenance="initial",
            guard_mode="strict",
            surface={"pyproject.toml": "file"},
            files={"pyproject.toml": b"x"},
            run_id=None,
        )
        st._conn.execute(
            "INSERT INTO harness_trust_audit (namespace, task_id, at, actor, reason, branch, "
            "bound_branch, replaced_provenance) VALUES (?, ?, 'now', 'op', 'r', NULL, 0, NULL)",
            (ns, task_id),
        )
        st._conn.commit()
    return ns


def _count(cfg: ExecutorConfig, table: str) -> int:
    conn = sqlite3.connect(cfg.state_file)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _rows(cfg: ExecutorConfig) -> tuple[int, int, int, int]:
    return tuple(
        _count(cfg, t)
        for t in (
            "task_workspaces",
            "harness_baselines",
            "harness_baseline_files",
            "harness_trust_audit",
        )
    )  # type: ignore[return-value]


class TestTddComplete:
    def test_complete_forgets_and_keeps_the_audit(self, tmp_path):
        root, cfg, _cp, green_sha = _wedged(tmp_path)
        _seed(cfg, TASK)
        assert _rows(cfg) == (1, 1, 1, 1)
        with ExecutorState(cfg) as state:
            complete(cfg, state, TASK, green_sha, reason=REASON)
        assert _rows(cfg) == (0, 0, 0, 1)

    def test_a_failing_forget_rolls_back_the_completion(self, tmp_path, monkeypatch):
        root, cfg, _cp, green_sha = _wedged(tmp_path)
        _seed(cfg, TASK)
        before = _recorded(cfg)

        def boom(self, namespace, task_id):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(ExecutorState, "_forget_workspace_sql", boom)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="nothing was recorded"):
            complete(cfg, state, TASK, green_sha, reason=REASON)
        monkeypatch.undo()
        assert _recorded(cfg) == before, "DONE, release or remedy survived the rollback"
        assert _rows(cfg) == (1, 1, 1, 1)

        with ExecutorState(cfg) as state:
            result = complete(cfg, state, TASK, green_sha, reason=REASON)
        assert not result.already_applied
        assert _rows(cfg) == (0, 0, 0, 1)


TASKS_MD = (
    "# Tasks\n\n### TASK-001: one\n🔴 P0 | 🔄 IN_PROGRESS | Est: 1d\n\n"
    "**Checklist:**\n- [x] done\n\n"
    "### TASK-002: two\n🔴 P0 | ⬜ TODO | Est: 1d\n\n**Checklist:**\n- [x] done\n"
)


def _project(tmp_path: Path, monkeypatch) -> ExecutorConfig:
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "tasks.md").write_text(TASKS_MD)
    monkeypatch.chdir(tmp_path)
    return ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "spec" / ".state.db")


def _task_args(command: str, task_id: str = "TASK-001") -> argparse.Namespace:
    return argparse.Namespace(
        command="task",
        task_command=command,
        task_id=task_id,
        force=False,
        spec_prefix="",
        change="",
    )


def _status(root: Path, task_id: str) -> str:
    from spec_runner.task import get_task_by_id, parse_tasks

    task = get_task_by_id(parse_tasks(root / "spec" / "tasks.md"), task_id)
    assert task is not None
    return task.status


class TestTaskDone:
    def test_done_forgets_after_the_status_write(self, tmp_path, monkeypatch):
        from spec_runner.cli import _dispatch_task_command

        cfg = _project(tmp_path, monkeypatch)
        _seed(cfg, "TASK-001")
        _seed(cfg, "TASK-002")
        _dispatch_task_command(_task_args("done"), cfg)
        assert _status(tmp_path, "TASK-001") == "done"
        ns = resolve_namespace(cfg)
        with ExecutorState(cfg) as st:
            assert st.get_workspace(ns, "TASK-001") is None
            assert st.get_harness_baseline(ns, "TASK-001") is None
            assert st.get_workspace(ns, "TASK-002") is not None, "another task's rows went too"
        assert _count(cfg, "harness_trust_audit") == 2

    def test_a_failing_forget_is_reported_with_exit_two(self, tmp_path, monkeypatch, capsys):
        from spec_runner.cli import _dispatch_task_command

        cfg = _project(tmp_path, monkeypatch)
        _seed(cfg, "TASK-001")

        def boom(self, namespace, task_id):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(ExecutorState, "_forget_workspace_sql", boom)
        with pytest.raises(SystemExit) as exc:
            _dispatch_task_command(_task_args("done"), cfg)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "TASK-001" in err and "disk I/O error" in err and "task done" in err
        assert _status(tmp_path, "TASK-001") == "done"
        assert _rows(cfg) == (1, 1, 1, 1)

        monkeypatch.undo()
        monkeypatch.chdir(tmp_path)
        _dispatch_task_command(_task_args("done"), cfg)  # the rerun finishes the job
        assert _rows(cfg) == (0, 0, 0, 1)

    def test_a_refused_done_forgets_nothing(self, tmp_path, monkeypatch):
        from spec_runner.cli import _dispatch_task_command

        cfg = _project(tmp_path, monkeypatch)
        _seed(cfg, "TASK-001")
        _dispatch_task_command(_task_args("done", "TASK-404"), cfg)
        assert _rows(cfg) == (1, 1, 1, 1)

    def test_no_state_db_is_not_created(self, tmp_path, monkeypatch):
        from spec_runner.cli import _dispatch_task_command

        cfg = _project(tmp_path, monkeypatch)
        _dispatch_task_command(_task_args("done"), cfg)
        assert _status(tmp_path, "TASK-001") == "done"
        assert not cfg.state_file.exists()

    def test_sync_from_gh_closing_a_task_forgets(self, tmp_path, monkeypatch):
        from spec_runner import github_sync
        from spec_runner.cli import _dispatch_task_command
        from spec_runner.task import update_task_status

        cfg = _project(tmp_path, monkeypatch)
        _seed(cfg, "TASK-002")

        def fake_sync(args, tasks, tasks_file):
            update_task_status(tasks_file, "TASK-002", "done")

        monkeypatch.setattr(github_sync, "cmd_sync_from_gh", fake_sync)
        _dispatch_task_command(_task_args("sync-from-gh"), cfg)
        assert _rows(cfg) == (0, 0, 0, 1)

    def test_a_task_already_done_is_not_touched_by_sync(self, tmp_path, monkeypatch):
        """Only a flip to DONE ends a task; a task that stayed open keeps its rows."""
        from spec_runner import github_sync
        from spec_runner.cli import _dispatch_task_command

        cfg = _project(tmp_path, monkeypatch)
        _seed(cfg, "TASK-002")
        monkeypatch.setattr(github_sync, "cmd_sync_from_gh", lambda *a: None)
        _dispatch_task_command(_task_args("sync-from-gh"), cfg)
        assert _rows(cfg) == (1, 1, 1, 1)


def test_stale_recovery_reconciling_done_forgets(tmp_path, monkeypatch):
    """A stale `running` task the main branch shows DONE is reconciled to success."""
    from spec_runner import state as state_mod
    from spec_runner.state import recover_stale_tasks

    cfg = ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "state.db")
    _seed(cfg, "TASK-001")
    monkeypatch.setattr(state_mod, "_done_on_main_branch", lambda c, f: {"TASK-001"})
    with ExecutorState(cfg) as st:
        st.mark_running("TASK-001")
        recovered = recover_stale_tasks(st, 0, tmp_path / "tasks.md", recover_all=True)
        assert recovered == ["TASK-001"]
        assert st.get_task_state("TASK-001").status == "success"
    assert _rows(cfg) == (0, 0, 0, 1)


def test_stale_recovery_to_failed_keeps_them(tmp_path, monkeypatch):
    from spec_runner import state as state_mod
    from spec_runner.state import recover_stale_tasks

    cfg = ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "state.db")
    (tmp_path / "tasks.md").write_text(TASKS_MD)
    _seed(cfg, "TASK-001")
    monkeypatch.setattr(state_mod, "_done_on_main_branch", lambda c, f: set())
    with ExecutorState(cfg) as st:
        st.mark_running("TASK-001")
        recover_stale_tasks(st, 0, tmp_path / "tasks.md", recover_all=True)
    assert _rows(cfg) == (1, 1, 1, 1)


def test_the_cli_passes_its_config_to_task_done(tmp_path, monkeypatch):
    """`spec-runner task done` end to end: main hands the dispatcher its config."""
    from spec_runner.cli import main

    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "tasks.md").write_text(TASKS_MD)
    monkeypatch.chdir(tmp_path)
    cfg = ExecutorConfig(project_root=Path.cwd())
    _seed(cfg, "TASK-001")
    main(["task", "done", "TASK-001"])
    assert _status(tmp_path, "TASK-001") == "done"
    assert _rows(cfg) == (0, 0, 0, 1)
