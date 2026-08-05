"""No-op completion marker (#97, battle-testing F-20).

A task that completes with nothing to commit (work already absorbed by
earlier tasks) must be marked done with an explicit no-op marker visible in
state, `--json-result` and `status` output — so 5/5-with-one-noop is
distinguishable from 4/5-with-one-skipped.
"""

import sqlite3
import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.hooks import post_done_hook
from spec_runner.state import ExecutorState
from spec_runner.task import Task


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=root, check=False)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("hello\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")


def _task(task_id: str = "TASK-001") -> Task:
    return Task(
        id=task_id,
        name="demo",
        priority="p0",
        status="in_progress",
        estimate="",
        description="",
        checklist=[],
    )


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "create_git_branch": False,
        "auto_commit": True,
        "run_tests_on_done": False,
        "run_lint_on_done": False,
        "run_review": False,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


class TestPostDoneHookNoOp:
    def test_no_changes_flags_no_op(self, tmp_path):
        """The exact F-20 scenario: nothing staged → success + no_op=True."""
        _init_repo(tmp_path)
        ok, err, _, _, no_op = post_done_hook(_task(), _cfg(tmp_path), True)
        assert ok is True, err
        assert no_op is True

    def test_real_changes_not_flagged(self, tmp_path):
        _init_repo(tmp_path)
        (tmp_path / "feature.py").write_text("x = 1\n")
        ok, err, _, _, no_op = post_done_hook(_task(), _cfg(tmp_path), True)
        assert ok is True, err
        assert no_op is False

    def test_auto_commit_off_never_flags(self, tmp_path):
        """Without auto-commit we cannot tell — no_op must stay False."""
        _init_repo(tmp_path)
        ok, err, _, _, no_op = post_done_hook(_task(), _cfg(tmp_path, auto_commit=False), True)
        assert ok is True, err
        assert no_op is False

    def test_failure_path_not_flagged(self, tmp_path):
        _init_repo(tmp_path)
        ok, _err, _, _, no_op = post_done_hook(_task(), _cfg(tmp_path), False)
        assert ok is False
        assert no_op is False


class TestNoOpPersistence:
    def test_round_trip_through_sqlite(self, tmp_path):
        cfg = ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "state.db")
        with ExecutorState(cfg) as state:
            state.record_attempt("TASK-004", True, 1.0, no_op=True)
            state.record_attempt("TASK-005", True, 1.0)
        with ExecutorState(cfg) as state:
            assert state.tasks["TASK-004"].attempts[-1].no_op is True
            assert state.tasks["TASK-005"].attempts[-1].no_op is False

    def test_migration_adds_column_to_old_db(self, tmp_path):
        """A pre-v2.16.0 DB (no no_op column) opens and upgrades cleanly."""
        db = tmp_path / "state.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, "
            "status TEXT NOT NULL DEFAULT 'pending', started_at TEXT, completed_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE attempts (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "task_id TEXT NOT NULL, timestamp TEXT NOT NULL, success INTEGER NOT NULL, "
            "duration_seconds REAL NOT NULL, error TEXT, error_code TEXT, claude_output TEXT)"
        )
        conn.execute("INSERT INTO tasks (task_id, status) VALUES ('TASK-001', 'success')")
        conn.execute(
            "INSERT INTO attempts (task_id, timestamp, success, duration_seconds) "
            "VALUES ('TASK-001', '2026-08-05T10:00:00', 1, 2.5)"
        )
        conn.commit()
        conn.close()

        cfg = ExecutorConfig(project_root=tmp_path, state_file=db)
        with ExecutorState(cfg) as state:
            # Old attempts default to no_op=False; new writes persist the flag
            assert state.tasks["TASK-001"].attempts[-1].no_op is False
            state.record_attempt("TASK-002", True, 1.0, no_op=True)
        with ExecutorState(cfg) as state:
            assert state.tasks["TASK-002"].attempts[-1].no_op is True


class TestNoOpStatusDisplay:
    def test_status_shows_noop_tag(self, tmp_path, capsys):
        from spec_runner.cli_info import print_status

        cfg = ExecutorConfig(project_root=tmp_path, state_file=tmp_path / "state.db")
        with ExecutorState(cfg) as state:
            state.record_attempt("TASK-004", True, 1.0, no_op=True)
            state.record_attempt("TASK-005", True, 1.0)
        print_status(cfg)
        out = capsys.readouterr().out
        task4_line = next(line for line in out.splitlines() if "TASK-004" in line)
        task5_line = next(line for line in out.splitlines() if "TASK-005" in line)
        assert "[no-op]" in task4_line
        assert "[no-op]" not in task5_line
