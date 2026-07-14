"""``change`` subcommands: new, list, archive (M2, change-as-folder).

A change lives at ``spec/changes/<id>/`` and is a self-rooted spec dir — the
rest of the toolchain scopes to it via ``config.change_id`` (CLI ``--change``).
Archiving here only moves the folder to ``spec/changes/archive/`` with a date
prefix; merging delta specs into the main specs is M3.
Design: docs/plans/2026-07-13-m2-change-folder-design.md.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .config import ConfigError, ExecutorConfig, ExecutorLock, _validate_change_id
from .logging import get_logger
from .task import parse_tasks

logger = get_logger("change")

_TASKS_STUB = """# Tasks

### TASK-001: Describe the first task
P0 | TODO

**Checklist:**
- [ ] fill in real tasks (see spec/FORMAT.md), then `spec-runner run --change {change_id}`
"""


@dataclass(frozen=True)
class ChangeInfo:
    """Summary of one in-flight change."""

    change_id: str
    total: int  # parsed tasks
    done: int


def _changes_dir(config: ExecutorConfig) -> Path:
    return config.project_root / "spec" / "changes"


def _change_dir(config: ExecutorConfig, change_id: str) -> Path:
    return _changes_dir(config) / change_id


def _archive_dest(config: ExecutorConfig, change_id: str) -> Path:
    """Dated archive destination for ``change_id`` (no collision handling)."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    return _changes_dir(config) / "archive" / f"{stamp}-{change_id}"


def _count_tasks(change_dir: Path) -> tuple[int, int]:
    """Return ``(total, done)`` parsed from the change's tasks.md (0, 0 if absent)."""
    tasks_file = change_dir / "tasks.md"
    if not tasks_file.exists():
        return 0, 0
    tasks = parse_tasks(tasks_file)
    return len(tasks), sum(1 for t in tasks if t.status == "done")


def delta_spec_path(config: ExecutorConfig, change_id: str | None = None) -> Path:
    """The change's delta spec for the flat requirements (M3).

    Defaults to ``config.change_id``; pass ``change_id`` explicitly when the
    config is not change-scoped (e.g. from ``change archive <id>``).
    """
    cid = change_id or config.change_id
    return _change_dir(config, cid) / "specs" / "requirements.md"


def _flat_requirements(config: ExecutorConfig) -> Path:
    """The merge target: the project's flat source-of-truth requirements."""
    return config.project_root / "spec" / "requirements.md"


def validate_change_delta(config: ExecutorConfig, change_id: str | None = None) -> list[str]:
    """Return merge conflicts of the change's delta against the flat target.

    Empty list = the delta applies cleanly (or structural parse issues are
    returned as a single conflict). Used both by ``validate --change``
    (fail fast) and by ``change archive`` (gate).
    """
    from .requirements import parse_delta
    from .spec_merge import plan_merge

    path = delta_spec_path(config, change_id)
    try:
        delta = parse_delta(path.read_text())
    except ValueError as exc:
        return [str(exc)]
    target = _flat_requirements(config)
    target_text = target.read_text() if target.exists() else ""
    return list(plan_merge(target_text, delta).conflicts)


def list_changes(config: ExecutorConfig) -> list[ChangeInfo]:
    """Return in-flight changes (sorted by id; the archive dir is excluded)."""
    root = _changes_dir(config)
    if not root.is_dir():
        return []
    infos = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name == "archive":
            continue
        total, done = _count_tasks(entry)
        infos.append(ChangeInfo(change_id=entry.name, total=total, done=done))
    return infos


def cmd_change_new(args: argparse.Namespace, config: ExecutorConfig) -> int:
    """Create ``spec/changes/<id>/`` with a tasks.md stub."""
    change_id = args.change_id
    try:
        _validate_change_id(change_id)
    except ConfigError as exc:
        print(f"⛔ {exc}")
        return 2
    dest = _change_dir(config, change_id)
    if dest.exists():
        print(f"⛔ change {change_id!r} already exists: {dest}")
        return 2
    dest.mkdir(parents=True)
    (dest / "tasks.md").write_text(_TASKS_STUB.format(change_id=change_id))
    logger.info("change_created", change_id=change_id, path=str(dest))
    print(f"created {dest}")
    print(f"next: edit its tasks.md, then `spec-runner run --change {change_id}`")
    return 0


def cmd_change_list(args: argparse.Namespace, config: ExecutorConfig) -> int:
    """List in-flight changes with task progress."""
    infos = list_changes(config)
    if getattr(args, "json", False):
        print(json.dumps([info.__dict__ for info in infos], indent=2))
        return 0
    if not infos:
        print("no changes in flight (create one: spec-runner change new <id>)")
        return 0
    for info in infos:
        print(f"{info.change_id:24} {info.done}/{info.total} tasks done")
    return 0


def cmd_change_archive(args: argparse.Namespace, config: ExecutorConfig) -> int:
    """Move a completed change to ``spec/changes/archive/YYYY-MM-DD-<id>/``.

    Refuses when the change is missing (2), when its executor lock is held by
    a live run, or when not every task is done — ``--force`` overrides the
    task gate only, never the live-run gate.
    """
    change_id = args.change_id
    try:
        # Guards path traversal too: ids are single safe path components.
        _validate_change_id(change_id)
    except ConfigError as exc:
        print(f"⛔ {exc}")
        return 2
    src = _change_dir(config, change_id)
    if not src.is_dir():
        print(f"⛔ no such change: {change_id!r} (see `spec-runner change list`)")
        return 2

    # Never archive under a live run. The run lock derives from the state
    # path: normally the change-local default, but an explicit paths.state
    # makes every run (flat or change) share that location instead.
    lock_paths = [src / ".executor-state.lock"]
    flat_default_state = config.project_root / "spec" / ".executor-state.db"
    if config.state_file != flat_default_state:
        lock_paths.append(config.state_file.with_suffix(".lock"))
    run_locks = [ExecutorLock(p) for p in dict.fromkeys(lock_paths)]
    acquired: list[ExecutorLock] = []
    for lock in run_locks:
        if not lock.acquire():
            for held in acquired:
                held.release()
            print(f"⛔ change {change_id!r} has a running executor — stop it first")
            return 1
        acquired.append(lock)
    try:
        tasks_file = src / "tasks.md"
        force = getattr(args, "force", False)
        if not tasks_file.exists() and not force:
            print(
                f"⛔ {change_id!r} has no tasks.md — a broken change? (--force to archive anyway)"
            )
            return 1
        total, done = _count_tasks(src)
        if done < total and not force:
            print(f"⛔ {change_id!r}: {done}/{total} tasks done — finish or --force")
            return 1

        # Delta merge (M3): a delta spec at specs/requirements.md is merged
        # into the flat source-of-truth requirements as part of archiving.
        # Conflicts abort the archive; --force never overrides merge safety.
        merged_text: str | None = None
        delta_file = delta_spec_path(config, change_id)
        dry_run = getattr(args, "dry_run", False)
        if delta_file.exists():
            from .requirements import parse_delta
            from .spec_merge import MergeConflictError, apply_merge, plan_merge

            try:
                delta = parse_delta(delta_file.read_text())
            except ValueError as exc:
                print(f"⛔ delta spec is malformed: {exc}")
                return 1
            target = _flat_requirements(config)
            target_text = target.read_text() if target.exists() else ""
            plan = plan_merge(target_text, delta)
            if not plan.ok:
                print(f"⛔ delta does not apply cleanly to {target}:")
                for conflict in plan.conflicts:
                    print(f"  - {conflict}")
                return 1
            if dry_run:
                print(f"merge plan for {target} (dry run — nothing written):")
                for op in plan.operations:
                    print(f"  - {op}")
                print(f"would archive {change_id} → {_archive_dest(config, change_id)}")
                return 0
            try:
                merged_text = apply_merge(target_text, delta)
            except MergeConflictError as exc:  # pragma: no cover — plan gated
                print(f"⛔ {exc}")
                return 1
            print(f"merging delta into {target}:")
            for op in plan.operations:
                print(f"  - {op}")
        elif dry_run:
            print(f"no delta spec; would archive {change_id} → {_archive_dest(config, change_id)}")
            return 0

        dest = _archive_dest(config, change_id)
        n = 2
        while dest.exists():
            dest = dest.with_name(f"{_archive_dest(config, change_id).name}-{n}")
            n += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
    finally:
        # Release before the move: one lock file lives inside the folder
        # being moved, and holding it during rename is unnecessary — the
        # gate only guards against a run that was live at check time.
        for lock in acquired:
            lock.release()

    # Write the merged requirements before the move: if the write succeeds
    # but the rename fails, re-running archive conflicts loudly (idempotence
    # guard) instead of silently double-applying.
    if merged_text is not None:
        target = _flat_requirements(config)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(merged_text)

    src.rename(dest)
    logger.info("change_archived", change_id=change_id, dest=str(dest))
    print(f"archived {change_id} → {dest}")
    return 0
