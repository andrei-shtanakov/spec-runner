"""Workspace/baseline lifecycle is atomic with the records that end a task (spec §2)."""

import sqlite3
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState, reset_state_preserving_workspaces
from tests.test_remedy import _establish

NS = "ns-a"


def _cfg(tmp_path: Path) -> ExecutorConfig:
    return ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "state.db")


def _seed(st: ExecutorState, ns: str = NS) -> None:
    st.record_workspace(ns, "TASK-1", branch="task/task-1", run_id=None)
    st.store_harness_baseline(
        ns,
        "TASK-1",
        provenance="initial",
        guard_mode="strict",
        surface={"pyproject.toml": "file"},
        files={"pyproject.toml": b"x"},
        run_id=None,
    )


def test_done_forgets_in_the_same_transaction(tmp_path):
    with ExecutorState(_cfg(tmp_path)) as st:
        _seed(st)
        st.record_attempt("TASK-1", True, 1.0, forget_workspace=NS)
        assert st.get_workspace(NS, "TASK-1") is None
        assert st.get_harness_baseline(NS, "TASK-1") is None


def test_done_without_a_namespace_keeps_them(tmp_path):
    with ExecutorState(_cfg(tmp_path)) as st:
        _seed(st)
        st.record_attempt("TASK-1", True, 1.0)
        assert st.get_workspace(NS, "TASK-1") is not None


def test_failed_attempt_keeps_them(tmp_path):
    with ExecutorState(_cfg(tmp_path)) as st:
        _seed(st)
        st.record_attempt("TASK-1", False, 1.0, error="x", forget_workspace=NS)
        assert st.get_workspace(NS, "TASK-1") is not None


def test_a_failing_forget_rolls_back_the_done_row(tmp_path, monkeypatch):
    with ExecutorState(_cfg(tmp_path)) as st:
        _seed(st)

        def boom(namespace, task_id):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(st, "_forget_workspace_sql", boom)
        st.record_attempt("TASK-1", True, 1.0, forget_workspace=NS)  # degraded, no raise
        assert st.degraded
    conn = sqlite3.connect(tmp_path / "state.db")
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM task_workspaces").fetchone()[0] == 1


def test_the_done_site_forgets_in_every_mode():
    """Every success `record_attempt` in execution.py passes the namespace, unconditionally."""
    import ast

    tree = ast.parse((Path(__file__).parents[1] / "src/spec_runner/execution.py").read_text())
    successes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "record_attempt"
        and len(node.args) > 1
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value is True
    ]
    assert successes, "the DONE site is gone"
    for call in successes:
        kw = {k.arg: k.value for k in call.keywords}
        value = kw.get("forget_workspace")
        assert isinstance(value, ast.Call), "DONE must forget the workspace"
        assert isinstance(value.func, ast.Name) and value.func.id == "resolve_namespace"


def test_reset_keeps_the_four_tables_and_drops_the_rest(tmp_path):
    cfg = _cfg(tmp_path)
    with ExecutorState(cfg) as st:
        _seed(st)
        st.trust_harness(
            NS,
            "TASK-1",
            bind=False,
            bind_branch=None,
            branch=None,
            surface={},
            files={},
            guard_mode="strict",
            actor="op",
            reason="r",
            run_id=None,
        )
        st.record_attempt("TASK-1", False, 1.0, error="x")
    reset_state_preserving_workspaces(cfg)
    with ExecutorState(cfg) as st:
        assert st.get_workspace(NS, "TASK-1") is not None
        baseline = st.get_harness_baseline(NS, "TASK-1")
        assert baseline is not None and baseline.provenance == "operator"
        assert len(st.harness_trust_audit(NS, "TASK-1")) == 1
        assert st.get_task_state("TASK-1").attempt_count == 0
    assert not Path(f"{cfg.state_file}.reset-tmp").exists()


def test_reset_of_an_absent_db_creates_an_empty_one(tmp_path):
    cfg = _cfg(tmp_path)
    reset_state_preserving_workspaces(cfg)
    with ExecutorState(cfg) as st:
        assert st.get_workspace(NS, "TASK-1") is None


def test_a_failing_reset_leaves_the_original_untouched(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    with ExecutorState(cfg) as st:
        _seed(st)
        st.record_attempt("TASK-1", False, 1.0, error="x")
    before = (tmp_path / "state.db").read_bytes()
    import spec_runner.state as state_mod

    def full(*a):
        raise OSError("full")

    monkeypatch.setattr(state_mod.os, "replace", full)
    with pytest.raises(OSError):
        reset_state_preserving_workspaces(cfg)
    monkeypatch.undo()
    assert (tmp_path / "state.db").read_bytes() == before
    assert not Path(f"{cfg.state_file}.reset-tmp").exists()
    with ExecutorState(cfg) as st:
        assert st.get_task_state("TASK-1").attempt_count == 1


def test_no_old_wal_is_beside_the_new_file_when_it_lands(tmp_path, monkeypatch):
    """A connection kept open (WAL file alive) must not leave its WAL to be replayed
    onto the rebuilt file: the old sidecars are gone before the replace."""
    cfg = _cfg(tmp_path)
    with ExecutorState(cfg) as st:
        _seed(st)
        st.record_attempt("TASK-1", False, 1.0, error="x")
    idle = sqlite3.connect(cfg.state_file)
    idle.execute("PRAGMA wal_autocheckpoint=0")
    idle.execute("INSERT INTO executor_meta (key, value) VALUES ('stale', 'yes')")
    idle.commit()
    assert Path(f"{cfg.state_file}-wal").stat().st_size > 0
    import spec_runner.state as state_mod

    real_replace = state_mod.os.replace
    seen: list[bool] = []

    def watching(src, dst):
        seen.append(Path(f"{dst}-wal").exists())
        real_replace(src, dst)

    monkeypatch.setattr(state_mod.os, "replace", watching)
    try:
        reset_state_preserving_workspaces(cfg)
    finally:
        idle.close()
    monkeypatch.undo()
    assert seen == [False]
    conn = sqlite3.connect(cfg.state_file)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM task_workspaces").fetchone()[0] == 1
    assert conn.execute("SELECT value FROM executor_meta WHERE key='stale'").fetchone() is None


def test_reset_refuses_a_db_in_use(tmp_path):
    """An open read transaction blocks the TRUNCATE checkpoint: refuse, change nothing."""
    cfg = _cfg(tmp_path)
    with ExecutorState(cfg) as st:
        _seed(st)
        st.record_attempt("TASK-1", False, 1.0, error="x")
    writer = sqlite3.connect(cfg.state_file)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO executor_meta (key, value) VALUES ('w', '1')")
    writer.commit()
    reader = sqlite3.connect(cfg.state_file)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM attempts").fetchone()
    try:
        with pytest.raises(RuntimeError, match="in use"):
            reset_state_preserving_workspaces(cfg)
    finally:
        reader.rollback()
        reader.close()
        writer.close()
    with ExecutorState(cfg) as st:
        assert st.get_task_state("TASK-1").attempt_count == 1


def _status(cfg, sql: str, *params) -> list:
    conn = sqlite3.connect(cfg.state_file)
    try:
        return [r[0] for r in conn.execute(sql, params)]
    finally:
        conn.close()


def test_abandon_is_one_transaction(tmp_path, monkeypatch):
    from spec_runner.remedy import abandon
    from spec_runner.tdd import resolve_namespace

    root, cfg, cp = _establish(tmp_path, task="TASK-1")
    ns = resolve_namespace(cfg)
    with ExecutorState(cfg) as st:
        _seed(st, ns)

    def boom(self, namespace, task_id):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ExecutorState, "_forget_workspace_sql", boom)
    with ExecutorState(cfg) as st, pytest.raises(sqlite3.OperationalError):
        abandon(cfg, st, "TASK-1", cp.checkpoint_id, reason="bad red")
    monkeypatch.undo()

    assert _status(cfg, "SELECT status FROM red_checkpoints") == ["active"]
    assert set(_status(cfg, "SELECT status FROM tdd_claims")) == {"active"}
    assert _status(cfg, "SELECT COUNT(*) FROM tdd_remedies") == [0]
    assert _status(cfg, "SELECT COUNT(*) FROM task_workspaces") == [1]

    with ExecutorState(cfg) as st:
        result = abandon(cfg, st, "TASK-1", cp.checkpoint_id, reason="bad red")
        assert not result.already_applied
    assert _status(cfg, "SELECT status FROM red_checkpoints") == ["abandoned"]
    assert set(_status(cfg, "SELECT status FROM tdd_claims")) == {"abandoned"}
    assert _status(cfg, "SELECT COUNT(*) FROM tdd_remedies") == [1]
    assert _status(cfg, "SELECT COUNT(*) FROM task_workspaces") == [0]
    assert _status(cfg, "SELECT COUNT(*) FROM harness_baselines") == [0]
    assert _status(cfg, "SELECT COUNT(*) FROM harness_baseline_files") == [0]

    with ExecutorState(cfg) as st:
        again = abandon(cfg, st, "TASK-1", cp.checkpoint_id, reason="bad red")
    assert again.already_applied
    assert _status(cfg, "SELECT COUNT(*) FROM tdd_remedies") == [1]
    assert _status(cfg, "SELECT COUNT(*) FROM task_workspaces") == [0]


def test_reset_refuses_while_the_executor_lock_is_held(tmp_path):
    """A live run holds the lock: reset touches nothing, DB and sidecars alike."""
    from spec_runner.config import ExecutorLock
    from spec_runner.state import ResetRefused

    cfg = _cfg(tmp_path)
    with ExecutorState(cfg) as st:
        _seed(st)
        st.record_attempt("TASK-1", False, 1.0, error="x")
    live = sqlite3.connect(cfg.state_file)
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("INSERT INTO executor_meta (key, value) VALUES ('live', '1')")
    live.commit()
    wal, shm = (Path(f"{cfg.state_file}{s}") for s in ("-wal", "-shm"))
    before = [p.read_bytes() for p in (cfg.state_file, wal, shm)]
    lock = ExecutorLock(cfg.state_file.with_suffix(".lock"))
    assert lock.acquire()
    try:
        with pytest.raises(ResetRefused, match="lock"):
            reset_state_preserving_workspaces(cfg)
        assert [p.read_bytes() for p in (cfg.state_file, wal, shm)] == before
        assert not Path(f"{cfg.state_file}.reset-tmp").exists()
    finally:
        lock.release()
        live.close()


def test_reset_releases_the_lock(tmp_path):
    from spec_runner.config import ExecutorLock

    cfg = _cfg(tmp_path)
    with ExecutorState(cfg) as st:
        _seed(st)
    reset_state_preserving_workspaces(cfg)
    lock = ExecutorLock(cfg.state_file.with_suffix(".lock"))
    assert lock.acquire()
    lock.release()


def test_the_in_use_refusal_is_fast(tmp_path):
    import time

    cfg = _cfg(tmp_path)
    with ExecutorState(cfg) as st:
        _seed(st)
    holder = sqlite3.connect(cfg.state_file)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO executor_meta (key, value) VALUES ('h', '1')")
    started = time.monotonic()
    try:
        with pytest.raises((RuntimeError, sqlite3.OperationalError)):
            reset_state_preserving_workspaces(cfg)
    finally:
        holder.rollback()
        holder.close()
    assert time.monotonic() - started < 2.0
