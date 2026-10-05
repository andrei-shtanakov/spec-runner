"""BEH-12: a checkpoint is a snapshot that sees WAL-only pages, not a file copy."""

import shutil
import sqlite3
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState

MARKER = "TASK-WAL-ONLY"


def _attempts(db: Path) -> list[str]:
    conn = sqlite3.connect(str(db))
    try:
        return [r[0] for r in conn.execute("SELECT task_id FROM attempts")]
    except sqlite3.DatabaseError:
        return []
    finally:
        conn.close()


def test_checkpoint_snapshot_contains_wal_only_attempt(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / "spec").mkdir(parents=True)
    state_file = root / "spec" / ".executor-state.db"
    config = ExecutorConfig(
        project_root=root,
        state_file=state_file,
        logs_dir=root / "spec" / ".executor-logs",
    )

    with ExecutorState(config) as state:
        assert state._conn is not None
        state._conn.execute("PRAGMA wal_autocheckpoint=0")
        state.record_attempt(MARKER, True, 1.0)

        # Control: a plain file copy misses the WAL-only attempt.
        copied = tmp_path / "copied.db"
        shutil.copy(state_file, copied)
        assert MARKER not in _attempts(copied)

        # The seam published a snapshot taken through the backup API.
        snapshots = [p for p in state_file.parent.rglob("state.db") if p != state_file]
        assert snapshots, "record_attempt published no checkpoint snapshot"

        restored = tmp_path / "restored" / "state.db"
        restored.parent.mkdir()
        shutil.copy(max(snapshots), restored)
        assert MARKER in _attempts(restored)
