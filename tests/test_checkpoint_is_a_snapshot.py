"""BEH-12 (#480 DT-03): the seam after a mutation, its manifest and its queue."""

import json
import sqlite3
from pathlib import Path

import jsonschema
import pytest

from spec_runner import checkpoint, run_context
from spec_runner.config import ExecutorConfig
from spec_runner.evidence import MANIFEST_FILE, RELEASED_MARKER, PendingCheckpoint, Publisher
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


def _copy(root: Path, run_id: str, seq: int, age: int, *, released: bool = False) -> Path:
    """A local copy as `_publish` leaves it: a manifest naming its run."""
    import os

    d = root / f"{seq:06d}-{run_id}{seq}"
    d.mkdir(parents=True)
    (d / MANIFEST_FILE).write_text(json.dumps({"run_id": run_id, "sequence": seq}))
    if released:
        (d / RELEASED_MARKER).write_text("")
    os.utime(d / MANIFEST_FILE, ns=(10**18 + age, 10**18 + age))
    return d


def test_rotation_spares_a_checkpoint_still_owed(tmp_path: Path) -> None:
    dirs = [_copy(tmp_path, "r", i, i) for i in range(1, 5)]
    checkpoint._rotate(tmp_path, "r", {dirs[0]})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["000001-r1", "000003-r3", "000004-r4"]


def test_rotation_orders_copies_across_runs_by_age(tmp_path: Path) -> None:
    _copy(tmp_path, "a", 7, 0, released=True)
    _copy(tmp_path, "a", 8, 1, released=True)
    _copy(tmp_path, "b", 1, 2)
    checkpoint._rotate(tmp_path, "b", set())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["000001-b1", "000008-a8"]


def test_rotation_never_deletes_another_runs_unreleased_copy(tmp_path: Path) -> None:
    """Another process's queue is invisible here: without its release marker
    the copy may still be owed, so it is not this process's to delete."""
    theirs = _copy(tmp_path, "a", 1, 0)
    for seq in range(1, 4):
        _copy(tmp_path, "b", seq, seq)
    checkpoint._rotate(tmp_path, "b", set())
    assert theirs.is_dir()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["000001-a1", "000002-b2", "000003-b3"]


def test_rotation_leaves_a_copy_without_a_manifest(tmp_path: Path) -> None:
    """A snapshot whose manifest is not on disk yet is being written by
    someone: its run cannot be told, so it is left alone."""
    partial = tmp_path / "000001-x"
    partial.mkdir()
    for seq in range(1, 4):
        _copy(tmp_path, "b", seq, seq)
    checkpoint._rotate(tmp_path, "b", set())
    assert partial.is_dir()


def _context(store: "_Store") -> run_context.RunContext:
    ctx = run_context.new_context("run")
    ctx.paying = True
    ctx.publisher = Publisher(store, ack_timeout=1.0)  # type: ignore[arg-type]
    ctx.started = True
    return ctx


def test_another_runs_rotation_leaves_an_owed_directory(tmp_path: Path) -> None:
    """Two invocations on one state dir (`retry`/`watch`/`run --force`): B's
    rotation must not delete what A still owes, or A's queue is poisoned."""
    config = _config(tmp_path)
    a_store, b_store = _Store(fail=True), _Store()
    a, b = _context(a_store), _context(b_store)
    assert a.publisher is not None and b.publisher is not None
    with ExecutorState(config) as state:
        run_context.install(a)
        state.record_attempt("T-A", True, 1.0)
        assert not a.publisher.drain(1.0)
        (owed,) = a.publisher.pending_directories()
        run_context.install(b)
        for i in range(3):
            state.record_attempt(f"T-B{i}", True, 1.0)
            assert b.publisher.drain(1.0)
    assert owed.is_dir(), "B deleted a checkpoint A still owes"
    assert len(_dirs(config)) == 3  # B's own two plus A's owed one
    a_store.fail = False
    assert a.publisher.drain(1.0)
    assert a.publisher.last_acknowledged() is not None


def test_a_delivered_copy_of_another_run_is_rotated(tmp_path: Path) -> None:
    """Disk stays bounded: a copy whose owner was acknowledged is released."""
    config = _config(tmp_path)
    a, b = _context(_Store()), _context(_Store())
    assert a.publisher is not None and b.publisher is not None
    with ExecutorState(config) as state:
        run_context.install(a)
        state.record_attempt("T-A", True, 1.0)
        assert a.publisher.drain(1.0)
        run_context.install(b)
        for i in range(3):
            state.record_attempt(f"T-B{i}", True, 1.0)
            assert b.publisher.drain(1.0)
    assert len(_dirs(config)) == checkpoint.KEEP_LOCAL


def test_a_vanished_checkpoint_is_dropped_not_retried_forever(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing local file is not a store outage: no retry can ever deliver
    it. It leaves the queue with a visible error, and the debt it stands for
    -- the latest state is not in the store -- is settled by a successor."""
    import shutil

    store = _Store()
    pub = Publisher(store, ack_timeout=1.0)  # type: ignore[arg-type]
    lost = _pending(tmp_path, 1)
    shutil.rmtree(lost.directory)
    pub.enqueue_checkpoint(lost)
    assert not pub.drain(1.0), "the latest state is not in the store"
    assert pub.pending_directories() == set(), "the lost entry still heads the queue"
    assert "000001" in capsys.readouterr().err
    assert not pub.drain(1.0)
    assert store.keys == [], "a missing file was read again"
    pub.enqueue_checkpoint(_pending(tmp_path, 2))
    assert pub.drain(1.0)
    assert pub.pending == 0
    assert pub.last_acknowledged() == (2, "id2")


def test_a_store_outage_is_still_retried(tmp_path: Path) -> None:
    store = _Store(fail=True)
    pub = Publisher(store, ack_timeout=1.0)  # type: ignore[arg-type]
    pub.enqueue_checkpoint(_pending(tmp_path, 1))
    assert not pub.drain(1.0)
    assert pub.pending_directories(), "a transient outage dropped the checkpoint"
    store.fail = False
    assert pub.drain(1.0)


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


def test_closure_names_a_checkpoint_lost_before_delivery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Q-05 site (b): the newest state never reached the store, so the run
    does not end as a success, and the reason says which checkpoint it was."""
    import shutil

    config = _config(tmp_path)
    ctx = _context(_Store())
    assert ctx.publisher is not None
    run_context.install(ctx)
    with ExecutorState(config) as state:
        state.record_attempt("T-1", True, 1.0)
    (gone,) = ctx.publisher.pending_directories()
    shutil.rmtree(gone)
    assert ctx.close(config, exit_code=0) == 2
    err = capsys.readouterr().err
    assert gone.name in err and "lost locally" in err
