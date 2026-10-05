"""Continuation checkpoint: the seam after a mutation (design §3.1-3.2, Q-05).

``after_mutation`` is the one place a checkpoint is made. The snapshot is taken
synchronously with ``sqlite3.Connection.backup()`` from the live connection --
the backup API reads through the pager, so pages that exist only in the ``-wal``
file are included, which a copy of the ``.db`` file does not do (BEH-12).
Delivery is the publisher's business and never waits here: a wait inside the
seam would raise from the middle of a multi-step handler and cost one wait per
mutation instead of one per invocation.

This task connects one write site, ``ExecutorState.record_attempt``; the other
sites of §3.1 are connected by DT-05.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .evidence import (
    CONTRACT_VERSION,
    MANIFEST_FILE,
    RELEASED_MARKER,
    PendingCheckpoint,
    policy_identity,
    release_directory,
)

if TYPE_CHECKING:
    from .config import ExecutorConfig

DB_FILE = "state.db"
#: Local copies kept: the latest and the one before it.
KEEP_LOCAL = 2
#: ``run_id`` of a mutation made outside any invocation (library use, tests).
LOCAL_RUN = "local"
#: Temporary worktrees (``tempfile.mkdtemp(prefix="spec-runner-…")``): outside
#: the project, so a name pattern is all a manifest can say about them.
WORKTREE_PATTERN = "spec-runner-*"


def after_mutation(
    config: ExecutorConfig,
    *,
    table: str,
    task_id: str | None = None,
    conn: sqlite3.Connection | None = None,
) -> str | None:
    """Snapshot the state DB after a continuation-relevant mutation.

    Returns the ``checkpoint_id``, or None when nothing was published: a probe
    (``config.probe_provenance``) is ephemeral and its DB is deleted with the
    directory, so its mutations are not continuation-relevant. A snapshot that
    cannot be taken warns on stderr and never fails the mutation it follows.
    """
    if getattr(config, "probe_provenance", None):
        return None
    try:
        return _publish(config, table, task_id, conn)
    except (sqlite3.Error, OSError) as exc:
        print(f"⚠️  checkpoint after {table} was not taken: {exc}", file=sys.stderr)
        return None


def _publish(
    config: ExecutorConfig, table: str, task_id: str | None, conn: sqlite3.Connection | None
) -> str:
    from . import run_context

    ctx = run_context.current()
    run_id = ctx.run_id if ctx is not None else LOCAL_RUN
    pipeline_id = ctx.pipeline_id if ctx is not None else None
    owned = conn is None
    live = sqlite3.connect(str(config.state_file)) if conn is None else conn
    # The config's path, not a literal: it is namespaced like the state DB
    # and it is the one `runtime_state_paths` keeps out of commits (#663).
    root = Path(config.checkpoints_dir)
    try:
        sequence, checkpoint_id, supersedes = _next_identity(live, run_id)
        directory = root / f"{sequence:06d}-{checkpoint_id}"
        try:
            _snapshot(live, directory / DB_FILE)
            manifest = _manifest(
                config,
                directory,
                run_id,
                pipeline_id,
                checkpoint_id,
                sequence,
                supersedes,
                table,
                task_id,
            )
            (directory / MANIFEST_FILE).write_bytes(_canonical(manifest, indent=2))
        except BaseException:
            # Half a checkpoint is no checkpoint: no copy without its manifest
            # and no `supersedes` link to it (the sequence stays reserved).
            shutil.rmtree(directory, ignore_errors=True)
            raise
        publisher = ctx.publisher if ctx is not None and ctx.started else None
        if publisher is not None:
            publisher.enqueue_checkpoint(
                PendingCheckpoint(
                    run_id, sequence, checkpoint_id, directory, (DB_FILE, MANIFEST_FILE)
                )
            )
        else:
            release_directory(directory)  # no queue owes it
        # After the enqueue: a failed link write costs one `supersedes` hop
        # to an older real checkpoint, never the checkpoint itself.
        _remember_last(live, run_id, checkpoint_id)
    finally:
        if owned:
            live.close()
    _rotate(root, run_id, publisher.pending_directories() if publisher is not None else set())
    return checkpoint_id


def _next_identity(conn: sqlite3.Connection, run_id: str) -> tuple[int, str, str | None]:
    """Reserve the next ``sequence``; ``supersedes`` is the last written checkpoint.

    Only the sequence is advanced here, before the snapshot: the link to the
    new checkpoint is recorded by ``_remember_last`` once its manifest is on
    disk, so a failed snapshot never becomes a later manifest's ``supersedes``.
    """
    seq_key, last_key = f"checkpoint_seq:{run_id}", f"checkpoint_last:{run_id}"
    with conn:
        meta = dict(
            conn.execute(
                "SELECT key, value FROM executor_meta WHERE key IN (?, ?)", (seq_key, last_key)
            ).fetchall()
        )
        sequence = int(meta.get(seq_key, "0")) + 1
        _set_meta(conn, seq_key, str(sequence))
    return sequence, str(uuid4()), meta.get(last_key)


def _remember_last(conn: sqlite3.Connection, run_id: str, checkpoint_id: str) -> None:
    with conn:
        _set_meta(conn, f"checkpoint_last:{run_id}", checkpoint_id)


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO executor_meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _snapshot(conn: sqlite3.Connection, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    dest = sqlite3.connect(str(target))
    try:
        conn.backup(dest)
        # One self-contained file: no -wal/-shm beside the copy.
        dest.execute("PRAGMA journal_mode=DELETE")
    finally:
        dest.close()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(
    config: ExecutorConfig,
    directory: Path,
    run_id: str,
    pipeline_id: str | None,
    checkpoint_id: str,
    sequence: int,
    supersedes: str | None,
    table: str,
    task_id: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "run_id": run_id,
        "pipeline_id": pipeline_id,
        "checkpoint_id": checkpoint_id,
        "sequence": sequence,
        "supersedes": supersedes,
        "identity": asdict(policy_identity(config)),
        "join_keys": {
            "run_id": run_id,
            "pipeline_id": pipeline_id,
            "table": table,
            "task_id": task_id,
        },
        "digests": {DB_FILE: _sha256(directory / DB_FILE)},
        "excluded": _excluded(config),
        "degraded": False,
        "wip": "none",
        "spool": "none",
    }
    body["manifest_sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    return body


def _excluded(config: ExecutorConfig) -> list[str]:
    """Local facts a checkpoint deliberately leaves out (CON-06), as this
    namespace names them: resolved from config, project-relative (BEH-14)."""
    from .config import PROGRESS_FILE

    root = Path(config.project_root)
    paths = (
        Path(config.state_file).with_suffix(".lock"),  # the run lock
        config.spec_lock_file,
        config.stop_file,
        config.ready_file,
    )
    names = [_relative(path, root) for path in paths]
    return [*names, WORKTREE_PATTERN, _relative(root / PROGRESS_FILE, root)]


def _relative(path: Path, root: Path) -> str:
    """Project-relative, or the bare name for a path outside the project."""
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError:
        return Path(path).name


def _canonical(body: dict[str, Any], indent: int | None = None) -> bytes:
    """Sorted-key JSON; ``manifest_sha256`` is over the compact form without itself."""
    if indent is None:
        body = {k: v for k, v in body.items() if k != "manifest_sha256"}
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return json.dumps(body, sort_keys=True, indent=indent).encode()


def _rotate(root: Path, run_id: str, owed: set[Path]) -> None:
    """Keep the newest ``KEEP_LOCAL`` copies; older ones go once nobody owes them.

    ``owed`` is only this process's queue. Several invocations can share one
    state dir (``retry``/``watch`` take no lock, ``run --force`` skips it), and
    another process's queue is invisible here, so a copy of another run is
    deleted only when it carries ``RELEASED_MARKER``; a copy whose manifest
    is not on disk yet is someone's write in progress and is left alone.
    Deleting an owed copy poisoned its owner's queue for good (#663).
    """
    copies = []
    for path in root.iterdir():
        owner = _owner(path)
        if owner is not None:
            copies.append((owner[1], path.name, path, owner[0]))
    # ``sequence`` restarts per run, so names alone do not order copies of
    # different runs; the manifest's age does (the directory's would move
    # when the release marker is added).
    copies.sort()
    for _, _, old, owner_run in copies[:-KEEP_LOCAL]:
        if old in owed:
            continue
        if owner_run == run_id or (old / RELEASED_MARKER).exists():
            shutil.rmtree(old, ignore_errors=True)


def _owner(path: Path) -> tuple[str, int] | None:
    """``(run_id, manifest mtime)`` of a finished copy, else None."""
    manifest = path / MANIFEST_FILE
    try:
        run_id = json.loads(manifest.read_bytes())["run_id"]
        return (str(run_id), manifest.stat().st_mtime_ns)
    except (OSError, ValueError, KeyError, TypeError):
        return None
