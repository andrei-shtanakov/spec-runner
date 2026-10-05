"""BEH-12 (#480 DT-03): the seam after a mutation, its manifest and its queue."""

import json
import sqlite3
from pathlib import Path

import jsonschema
import pytest

from spec_runner import checkpoint
from spec_runner.config import ExecutorConfig
from spec_runner.evidence import MANIFEST_FILE, PendingCheckpoint, Publisher
from spec_runner.state import ExecutorState

SCHEMA = Path(__file__).parent.parent / "schemas" / "checkpoint-manifest.schema.json"


def _config(tmp_path: Path) -> ExecutorConfig:
    root = tmp_path / "proj"
    (root / "spec").mkdir(parents=True)
    return ExecutorConfig(
        project_root=root,
        state_file=root / "spec" / ".executor-state.db",
        logs_dir=root / "spec" / ".executor-logs",
    )


def _dirs(config: ExecutorConfig) -> list[Path]:
    root = config.checkpoints_dir
    return sorted(p for p in root.iterdir() if p.is_dir()) if root.exists() else []


def test_manifest_is_valid_and_sequence_grows(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with ExecutorState(config) as state:
        state.record_attempt("T-1", True, 1.0)
        state.record_attempt("T-2", True, 1.0)
    first, second = _dirs(config)
    m1 = json.loads((first / MANIFEST_FILE).read_text())
    m2 = json.loads((second / MANIFEST_FILE).read_text())
    schema = json.loads(SCHEMA.read_text())
    jsonschema.validate(m1, schema)
    jsonschema.validate(m2, schema)
    assert (m1["sequence"], m2["sequence"]) == (1, 2)
    assert m1["supersedes"] is None and m2["supersedes"] == m1["checkpoint_id"]
    assert str(config.project_root) not in (second / MANIFEST_FILE).read_text()


def test_only_latest_and_previous_copy_are_kept(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with ExecutorState(config) as state:
        for i in range(4):
            state.record_attempt(f"T-{i}", True, 1.0)
    assert [p.name[:6] for p in _dirs(config)] == ["000003", "000004"]


def test_probe_publishes_nothing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.probe_provenance = "doctor"
    with ExecutorState(config) as state:
        state.record_attempt("T-1", True, 1.0)
    assert _dirs(config) == []


def test_snapshot_failure_does_not_fail_the_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.checkpoints_dir.write_text("in the way")
    with ExecutorState(config) as state:
        state.record_attempt("T-1", True, 1.0)
        assert state.get_task_state("T-1").status == "success"


class _Store:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.keys: list[str] = []

    def put(self, key: str, data: bytes, *, metadata: dict[str, str]):  # type: ignore[no-untyped-def]
        from spec_runner.artifact_store import Ack

        if self.fail:
            raise OSError("down")
        self.keys.append(key)
        return Ack(key, len(data))


def _pending(tmp_path: Path, seq: int) -> PendingCheckpoint:
    d = tmp_path / f"{seq:06d}-id{seq}"
    d.mkdir()
    (d / "state.db").write_bytes(b"db")
    (d / MANIFEST_FILE).write_bytes(b"{}")
    return PendingCheckpoint("r", seq, f"id{seq}", d, ("state.db", MANIFEST_FILE))


def test_publisher_delivers_in_sequence_manifest_last(tmp_path: Path) -> None:
    store = _Store()
    pub = Publisher(store, ack_timeout=1.0)  # type: ignore[arg-type]
    pub.enqueue_checkpoint(_pending(tmp_path, 2))
    pub.enqueue_checkpoint(_pending(tmp_path, 1))
    assert pub.last_acknowledged() is None
    assert pub.drain(1.0)
    assert [k.rsplit("/", 1)[1] for k in store.keys] == [
        "state.db",
        MANIFEST_FILE,
        "state.db",
        MANIFEST_FILE,
    ]
    assert store.keys[0].startswith("runs/r/checkpoints/000001-")
    assert pub.last_acknowledged() == (2, "id2")


def test_undelivered_checkpoint_is_owed_and_keeps_previous_ack(tmp_path: Path) -> None:
    store = _Store()
    pub = Publisher(store, ack_timeout=1.0)  # type: ignore[arg-type]
    pub.enqueue_checkpoint(_pending(tmp_path, 1))
    assert pub.drain(1.0)
    store.fail = True
    pub.enqueue_checkpoint(_pending(tmp_path, 2))
    assert not pub.drain(1.0)
    assert pub.pending == 1
    assert pub.last_acknowledged() == (1, "id1")


def test_rotation_spares_a_checkpoint_still_owed(tmp_path: Path) -> None:
    root = tmp_path
    dirs = [root / f"00000{i}-x" for i in range(1, 5)]
    for d in dirs:
        d.mkdir()
    checkpoint._rotate(root, {dirs[0]})
    assert sorted(p.name for p in root.iterdir()) == ["000001-x", "000003-x", "000004-x"]


def test_rotation_orders_copies_across_runs_by_age(tmp_path: Path) -> None:
    import os

    dirs = [tmp_path / "000007-a", tmp_path / "000008-a", tmp_path / "000001-b"]
    for i, d in enumerate(dirs):
        d.mkdir()
        os.utime(d, ns=(10**18 + i, 10**18 + i))
    checkpoint._rotate(tmp_path, set())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["000001-b", "000008-a"]


def test_backup_is_taken_without_a_connection(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with ExecutorState(config):
        pass
    cid = checkpoint.after_mutation(config, table="attempts")
    assert cid is not None
    (snap,) = _dirs(config)
    assert sqlite3.connect(str(snap / "state.db")).execute("SELECT 1").fetchone() == (1,)


def test_jsonschema_available() -> None:
    assert pytest.importorskip("jsonschema")


# -- the directory is the config's, namespaced and never committed (PR #663) --


def _prefixed(root: Path, prefix: str) -> ExecutorConfig:
    (root / "spec").mkdir(parents=True, exist_ok=True)
    return ExecutorConfig(project_root=root, spec_prefix=prefix)


def test_two_spec_prefixes_write_to_their_own_directories(tmp_path: Path) -> None:
    one, two = _prefixed(tmp_path, "ws1-"), _prefixed(tmp_path, "ws2-")
    for config in (one, two):
        with ExecutorState(config) as state:
            state.record_attempt("T-1", True, 1.0)
    assert one.checkpoints_dir != two.checkpoints_dir
    assert len(_dirs(one)) == 1 and len(_dirs(two)) == 1
    assert not (tmp_path / "spec" / ".executor-checkpoints").exists()


def _git(root: Path, *args: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def test_checkpoints_are_never_staged_or_reported_as_work(tmp_path: Path) -> None:
    from spec_runner import git_ops

    root = tmp_path / "proj"
    _git(tmp_path, "init", "-q", str(root))
    config = _prefixed(root, "ws-")
    with ExecutorState(config) as state:
        state.record_attempt("T-1", True, 1.0)
    assert _dirs(config), "nothing was written: the test would prove nothing"
    rel = str(config.checkpoints_dir.relative_to(root))
    assert not [p for p in git_ops.uncommitted_work_paths(config) if p.startswith(rel)]
    git_ops.stage_all_except_runtime(config)
    staged = _git(root, "diff", "--cached", "--name-only").splitlines()
    assert not [p for p in staged if p.startswith(rel)], staged


def test_runtime_gitignore_covers_the_checkpoint_directory(tmp_path: Path) -> None:
    from spec_runner import git_ops

    root = tmp_path / "proj"
    _git(tmp_path, "init", "-q", str(root))
    config = _prefixed(root, "ws-")
    git_ops.ensure_runtime_gitignore(config)
    with ExecutorState(config) as state:
        state.record_attempt("T-1", True, 1.0)
    (snap,) = _dirs(config)
    rel = str((snap / checkpoint.DB_FILE).relative_to(root))
    assert _git(root, "check-ignore", rel).strip() == rel
