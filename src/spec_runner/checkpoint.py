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

from .evidence import CONTRACT_VERSION, MANIFEST_FILE, PendingCheckpoint, policy_identity

if TYPE_CHECKING:
    from .config import ExecutorConfig

CHECKPOINT_DIR = ".executor-checkpoints"
DB_FILE = "state.db"
#: Local copies kept: the latest and the one before it.
KEEP_LOCAL = 2
#: ``run_id`` of a mutation made outside any invocation (library use, tests).
LOCAL_RUN = "local"
#: Local facts a checkpoint deliberately leaves out (CON-06).
EXCLUDED = (
    ".executor.lock",
    ".<prefix>spec.lock",
    ".executor-stop",
    ".executor-ready",
    "spec-runner-*",
    ".executor-progress.txt",
)


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
    try:
        sequence, checkpoint_id, supersedes = _next_identity(live, run_id)
        root = Path(config.state_file).parent / CHECKPOINT_DIR
        directory = root / f"{sequence:06d}-{checkpoint_id}"
        _snapshot(live, directory / DB_FILE)
    finally:
        if owned:
            live.close()
    manifest = _manifest(
        config, directory, run_id, pipeline_id, checkpoint_id, sequence, supersedes, table, task_id
    )
    (directory / MANIFEST_FILE).write_bytes(_canonical(manifest, indent=2))
    publisher = ctx.publisher if ctx is not None and ctx.started else None
    if publisher is not None:
        publisher.enqueue_checkpoint(
            PendingCheckpoint(run_id, sequence, checkpoint_id, directory, (DB_FILE, MANIFEST_FILE))
        )
    _rotate(root, publisher.pending_directories() if publisher is not None else set())
    return checkpoint_id


def _next_identity(conn: sqlite3.Connection, run_id: str) -> tuple[int, str, str | None]:
    """Advance ``sequence`` and remember the id, in the DB the snapshot copies."""
    seq_key, last_key = f"checkpoint_seq:{run_id}", f"checkpoint_last:{run_id}"
    with conn:
        meta = dict(
            conn.execute(
                "SELECT key, value FROM executor_meta WHERE key IN (?, ?)", (seq_key, last_key)
            ).fetchall()
        )
        sequence = int(meta.get(seq_key, "0")) + 1
        checkpoint_id = str(uuid4())
        for key, value in ((seq_key, str(sequence)), (last_key, checkpoint_id)):
            conn.execute(
                "INSERT INTO executor_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
    return sequence, checkpoint_id, meta.get(last_key)


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
        "excluded": list(EXCLUDED),
        "degraded": False,
        "wip": "none",
        "spool": "none",
    }
    body["manifest_sha256"] = hashlib.sha256(_canonical(body)).hexdigest()
    return body


def _canonical(body: dict[str, Any], indent: int | None = None) -> bytes:
    """Sorted-key JSON; ``manifest_sha256`` is over the compact form without itself."""
    if indent is None:
        body = {k: v for k, v in body.items() if k != "manifest_sha256"}
        return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return json.dumps(body, sort_keys=True, indent=indent).encode()


def _rotate(root: Path, owed: set[Path]) -> None:
    """Keep the newest ``KEEP_LOCAL`` copies; older ones go once delivered."""
    # ``sequence`` restarts per run, so names alone do not order copies of
    # different runs; the directory's age does.
    copies = sorted(
        (p for p in root.iterdir() if p.is_dir()), key=lambda p: (p.stat().st_mtime_ns, p.name)
    )
    for old in copies[:-KEEP_LOCAL]:
        if old not in owed:
            shutil.rmtree(old, ignore_errors=True)
