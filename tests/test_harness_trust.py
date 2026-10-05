"""`harness trust`: audited, guarded, atomic (spec §3); lifecycle (spec §2)."""

import argparse
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from spec_runner import cli
from spec_runner.config import ExecutorConfig, ExecutorLock
from spec_runner.harness_cmd import TrustError, cmd_harness, trust
from spec_runner.state import ExecutorState
from spec_runner.tdd import resolve_namespace
from tests.test_task_workspace_state import _Proxy

BRANCH = "task/task-100-work"


def _git(root: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@e.c")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "tasks.md").write_text(
        "# Spec\n\n## M0\n\n### TASK-100: work\n🔴 P0 | ⬜ TODO | Est: 1d\n\n"
        "**Description:** work\n\n**Checklist:**\n- [ ] do it\n\n"
        "**Traces to:** [REQ-0]\n**Depends on:** —\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-qb", BRANCH)
    return tmp_path


def _cfg(root: Path, **kw: object) -> ExecutorConfig:
    base: dict[str, object] = {
        "project_root": root,
        "state_file": root / "spec" / "state.db",
        "create_git_branch": True,
        "harness_guard": "strict",
    }
    base.update(kw)
    return ExecutorConfig(**base)  # type: ignore[arg-type]


def _trust(cfg: ExecutorConfig, **kw: object) -> str:
    with ExecutorState(cfg) as st:
        return trust(cfg, st, "TASK-100", **kw)  # type: ignore[arg-type]


def test_reason_is_required(repo):
    with pytest.raises(TrustError):
        _trust(_cfg(repo), reason="  ", bind_branch=BRANCH)


def test_refused_under_an_agent(repo, monkeypatch):
    monkeypatch.setenv("SPEC_RUNNER_AGENT", "1")
    with pytest.raises(TrustError):
        _trust(_cfg(repo), reason="checked", bind_branch=BRANCH)


def test_refused_while_the_lock_is_held(repo):
    cfg = _cfg(repo)
    lock = ExecutorLock(cfg.state_file.with_suffix(".lock"))
    assert lock.acquire()
    try:
        with pytest.raises(TrustError):
            _trust(cfg, reason="checked", bind_branch=BRANCH)
    finally:
        lock.release()


def test_without_a_workspace_bind_branch_is_required_and_must_be_current(repo):
    cfg = _cfg(repo)
    with pytest.raises(TrustError, match="--bind-branch"):
        _trust(cfg, reason="checked")
    with pytest.raises(TrustError, match="task's branch"):
        _trust(cfg, reason="checked", bind_branch="task/other")
    _git(repo, "switch", "-q", "main")
    with pytest.raises(TrustError, match="current branch"):
        _trust(cfg, reason="checked", bind_branch=BRANCH)
    _git(repo, "switch", "-q", BRANCH)
    _trust(cfg, reason="checked", bind_branch=BRANCH)
    with ExecutorState(cfg) as st:
        ns = resolve_namespace(cfg)
        assert st.get_workspace(ns, "TASK-100")["bound_by"] == "operator"
        assert st.get_harness_baseline(ns, "TASK-100").provenance == "operator"
        assert st.harness_trust_audit(ns, "TASK-100")[0]["bound_branch"] == 1


def test_with_a_workspace_the_branch_must_match(repo):
    cfg = _cfg(repo)
    with ExecutorState(cfg) as st:
        st.record_workspace(resolve_namespace(cfg), "TASK-100", branch="task/else", run_id=None)
    with pytest.raises(TrustError, match="recorded branch"):
        _trust(cfg, reason="checked")


def test_create_git_branch_false_needs_no_bind(repo):
    cfg = _cfg(repo, create_git_branch=False)
    _trust(cfg, reason="checked")
    with ExecutorState(cfg) as st:
        assert st.get_workspace(resolve_namespace(cfg), "TASK-100")["branch"] is None


def test_unknown_task_is_refused(repo):
    cfg = _cfg(repo)
    with pytest.raises(TrustError, match="no task TASK-404"), ExecutorState(cfg) as st:
        trust(cfg, st, "TASK-404", reason="checked", bind_branch=BRANCH)


def _args(**kw: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "harness_command": "trust",
        "task_id": "TASK-100",
        "reason": "checked",
        "bind_branch": BRANCH,
        "actor": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_db_failure_is_exit_2(repo, monkeypatch, capsys):
    def boom(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ExecutorState, "trust_harness", boom)
    assert cmd_harness(_args(), _cfg(repo)) == 2
    assert "disk I/O error" in capsys.readouterr().out


def test_unreadable_harness_file_is_exit_2(repo, monkeypatch, capsys):
    def boom(config):
        raise OSError("cannot read harness")

    monkeypatch.setattr("spec_runner.harness_cmd.surface_snapshot", boom)
    assert cmd_harness(_args(), _cfg(repo)) == 2
    assert "cannot read harness" in capsys.readouterr().out


def test_existing_row_race_is_a_clean_refusal(repo, monkeypatch, capsys):
    def boom(self, *a, **k):
        raise sqlite3.IntegrityError("UNIQUE constraint failed")

    monkeypatch.setattr(ExecutorState, "trust_harness", boom)
    assert cmd_harness(_args(), _cfg(repo)) == 1
    assert "workspace" in capsys.readouterr().out


def test_cmd_success_is_exit_0(repo, capsys):
    assert cmd_harness(_args(), _cfg(repo)) == 0
    assert "trusted" in capsys.readouterr().out


def test_cli_main_exit_codes(repo, monkeypatch):
    monkeypatch.chdir(repo)
    base = ["--project-root", str(repo), "harness", "trust", "TASK-100"]
    with pytest.raises(SystemExit) as refused:
        cli.main([*base, "--reason", "checked"])  # no --bind-branch
    assert refused.value.code == 1
    with pytest.raises(SystemExit) as ok:
        cli.main([*base, "--reason", "checked", "--bind-branch", BRANCH])
    assert ok.value.code == 0

    def boom(self, *a, **k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ExecutorState, "trust_harness", boom)
    with pytest.raises(SystemExit) as broken:
        cli.main([*base, "--reason", "again"])
    assert broken.value.code == 2


def _rows(cfg: ExecutorConfig) -> tuple[object, object, list]:
    ns = resolve_namespace(cfg)
    with ExecutorState(cfg) as st:
        return (
            st.get_workspace(ns, "TASK-100"),
            st.get_harness_baseline(ns, "TASK-100"),
            st.harness_trust_audit(ns, "TASK-100"),
        )


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable_harness_file_refuses_and_writes_nothing(repo, capsys):
    cfg = _cfg(repo)
    (repo / "pyproject.toml").chmod(0o000)
    try:
        assert cmd_harness(_args(), cfg) == 1
    finally:
        (repo / "pyproject.toml").chmod(0o644)
    assert "pyproject.toml" in capsys.readouterr().out
    assert _rows(cfg) == (None, None, [])


def test_audit_failure_at_command_level_leaves_nothing(repo, monkeypatch, capsys):
    cfg = _cfg(repo)
    real_init = ExecutorState.trust_harness

    def failing_trust(self, *a, **k):
        real = self._conn.execute

        def failing(sql, *args):
            if sql.lstrip().startswith("INSERT INTO harness_trust_audit"):
                raise sqlite3.OperationalError("disk I/O error")
            return real(sql, *args)

        self._conn = _Proxy(self._conn, failing)
        return real_init(self, *a, **k)

    monkeypatch.setattr(ExecutorState, "trust_harness", failing_trust)
    assert cmd_harness(_args(), cfg) == 2
    assert "disk I/O error" in capsys.readouterr().out
    monkeypatch.undo()
    assert _rows(cfg) == (None, None, [])


def test_missing_tasks_file_is_a_refusal(repo, capsys):
    (repo / "spec" / "tasks.md").unlink()
    assert cmd_harness(_args(), _cfg(repo)) == 1
    assert "no task TASK-100" in capsys.readouterr().out


def test_unreadable_tasks_file_is_exit_2(repo, capsys):
    (repo / "spec" / "tasks.md").write_bytes(b"\xff\xfe\x00bad")
    assert cmd_harness(_args(), _cfg(repo)) == 2
    assert "tasks.md" in capsys.readouterr().out


def test_replacement_is_named_in_plain_words(repo):
    cfg = _cfg(repo)
    _trust(cfg, reason="checked", bind_branch=BRANCH)
    assert _trust(cfg, reason="again") == (
        "✅ TASK-100: harness baseline trusted (replaced the earlier operator baseline)"
    )
