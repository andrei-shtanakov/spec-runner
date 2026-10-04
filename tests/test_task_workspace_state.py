"""State surface for retry-from-WIP (spec §2): four tables, atomic writes."""

import sqlite3
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState, StoredBaseline

NS = "ns-a"


def _cfg(tmp_path: Path) -> ExecutorConfig:
    return ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "state.db")


def test_the_four_tables_exist(tmp_path):
    with ExecutorState(_cfg(tmp_path)):
        pass
    conn = sqlite3.connect(tmp_path / "state.db")
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "task_workspaces",
        "harness_baselines",
        "harness_baseline_files",
        "harness_trust_audit",
    } <= names


def test_table_structure_keys_and_checks(tmp_path):
    with ExecutorState(_cfg(tmp_path)):
        pass
    conn = sqlite3.connect(tmp_path / "state.db")

    def cols(t):
        return {r[1]: r[5] for r in conn.execute(f"PRAGMA table_info({t})")}  # name -> pk pos

    assert cols("task_workspaces") == {
        "namespace": 1,
        "task_id": 2,
        "branch": 0,
        "started_at": 0,
        "run_id": 0,
        "bound_by": 0,
    }
    assert cols("harness_baselines") == {
        "namespace": 1,
        "task_id": 2,
        "captured_at": 0,
        "run_id": 0,
        "guard_mode": 0,
        "provenance": 0,
        "surface": 0,
    }
    assert cols("harness_baseline_files") == {
        "namespace": 1,
        "task_id": 2,
        "path": 3,
        "state": 0,
        "digest": 0,
        "content": 0,
    }
    assert cols("harness_trust_audit")["id"] == 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO harness_baselines VALUES ('n','t','now',NULL,'strict','guessed','{}')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO task_workspaces VALUES ('n','t',NULL,'now',NULL,'agent')")


def test_workspace_is_recorded_once_and_found_by_exact_branch(tmp_path):
    with ExecutorState(_cfg(tmp_path)) as st:
        st.record_workspace(NS, "TASK-1", branch="task/task-1-x", run_id="r1")
        st.record_workspace(NS, "TASK-1", branch="task/other", run_id="r2")
        row = st.get_workspace(NS, "TASK-1")
        assert row["branch"] == "task/task-1-x" and row["bound_by"] == "run"
        assert st.workspace_for_branch(NS, "task/task-1-x") == "TASK-1"
        assert st.workspace_for_branch("ns-b", "task/task-1-x") is None
        assert st.workspace_for_branch(NS, "task/task-1") is None


def test_baseline_round_trips_bytes_and_states(tmp_path):
    surface = {"pyproject.toml": "file", ".github/workflows": "dir", "Makefile": "absent"}
    files = {"pyproject.toml": b"[project]\n", ".github/workflows/ci.yml": None}
    with ExecutorState(_cfg(tmp_path)) as st:
        st.store_harness_baseline(
            NS,
            "TASK-1",
            provenance="initial",
            guard_mode="strict",
            surface=surface,
            files=files,
            run_id="r1",
        )
    with ExecutorState(_cfg(tmp_path)) as st:
        got = st.get_harness_baseline(NS, "TASK-1")
    assert isinstance(got, StoredBaseline)
    assert got.provenance == "initial" and got.surface == surface and got.files == files


def test_trust_is_atomic(tmp_path, monkeypatch):
    with ExecutorState(_cfg(tmp_path)) as st:
        real = st._conn.execute

        def failing(sql, *a):
            if sql.lstrip().startswith("INSERT INTO harness_trust_audit"):
                raise sqlite3.OperationalError("disk I/O error")
            return real(sql, *a)

        monkeypatch.setattr(st, "_conn", _Proxy(st._conn, failing))
        with pytest.raises(sqlite3.OperationalError):
            st.trust_harness(
                NS,
                "TASK-1",
                bind=True,
                bind_branch="task/task-1",
                branch="task/task-1",
                surface={},
                files={},
                guard_mode="strict",
                actor="op",
                reason="checked",
                run_id=None,
            )
    with ExecutorState(_cfg(tmp_path)) as st:
        assert st.get_workspace(NS, "TASK-1") is None
        assert st.get_harness_baseline(NS, "TASK-1") is None
        assert st.harness_trust_audit(NS, "TASK-1") == []


def test_trust_replacement_is_audited(tmp_path):
    with ExecutorState(_cfg(tmp_path)) as st:
        st.store_harness_baseline(
            NS,
            "TASK-1",
            provenance="recaptured",
            guard_mode="warn",
            surface={},
            files={},
            run_id=None,
        )
        replaced = st.trust_harness(
            NS,
            "TASK-1",
            bind=False,
            bind_branch=None,
            branch=None,
            surface={},
            files={},
            guard_mode="strict",
            actor="op",
            reason="restored and checked",
            run_id=None,
        )
        assert replaced == "recaptured"
        audit = st.harness_trust_audit(NS, "TASK-1")
        assert audit[-1]["replaced_provenance"] == "recaptured"
        assert st.get_harness_baseline(NS, "TASK-1").provenance == "operator"


def test_forget_keeps_the_audit(tmp_path):
    with ExecutorState(_cfg(tmp_path)) as st:
        st.trust_harness(
            NS,
            "TASK-1",
            bind=True,
            bind_branch="b",
            branch="b",
            surface={"pyproject.toml": "file"},
            files={"pyproject.toml": b"x"},
            guard_mode="strict",
            actor="op",
            reason="r",
            run_id=None,
        )
        st.forget_task_workspace(NS, "TASK-1")
        assert st.get_workspace(NS, "TASK-1") is None
        assert st.get_harness_baseline(NS, "TASK-1") is None
        assert len(st.harness_trust_audit(NS, "TASK-1")) == 1


class _Proxy:
    """Delegates to a sqlite3 connection but routes `execute` through `fn`."""

    def __init__(self, conn, fn):
        self._conn, self._fn = conn, fn

    def execute(self, sql, *a):
        return self._fn(sql, *a)

    def __getattr__(self, name):
        return getattr(self._conn, name)
