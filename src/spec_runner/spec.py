"""Spec frontmatter: SpecMeta dataclass and parse/split/strip/read/write helpers."""

from __future__ import annotations

import contextlib
import os
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from .config import ExecutorConfig, ExecutorLock

STAGES: tuple[str, str, str] = ("requirements", "design", "tasks")

_FM_DELIM = "---"


class SpecLockError(RuntimeError):
    """Raised when a spec-file lock cannot be acquired (another mutation in progress)."""


@dataclass
class SpecMeta:
    """Frontmatter state for one spec document."""

    spec_stage: str
    status: str = "draft"  # draft | approved | stale
    version: int = 1
    generated_by: str = ""
    generated_at: str = ""
    source_prompt_version: str = ""
    validation: str = ""  # pass | fail | warn | ""
    approved_by: str | None = None
    approved_at: str | None = None


def split_frontmatter(text: str) -> tuple[dict | None, str]:
    """Split a leading ``---\\n...\\n---`` YAML block from the body.

    Returns ``(meta_dict, body)`` or ``(None, text)`` when no frontmatter.
    """
    if not text.startswith(_FM_DELIM + "\n"):
        return None, text
    end = text.find("\n" + _FM_DELIM, len(_FM_DELIM) + 1)
    if end == -1:
        return None, text
    raw = text[len(_FM_DELIM) + 1 : end]
    # Body starts after the closing delimiter's line.
    after = text.find("\n", end + 1)
    body = text[after + 1 :] if after != -1 else ""
    try:
        loaded = yaml.safe_load(raw)
    except yaml.YAMLError:
        return None, text
    if not isinstance(loaded, dict):
        return None, text
    return loaded, body


def strip_frontmatter(text: str) -> str:
    """Return the document body with any leading frontmatter removed."""
    _, body = split_frontmatter(text)
    return body


def split_frontmatter_raw(text: str) -> tuple[str, str]:
    """Split the verbatim leading frontmatter block from the body.

    Returns ``("", text)`` when no frontmatter is present, else
    ``(raw_prefix, body)`` such that ``raw_prefix + body == text`` exactly,
    where ``raw_prefix`` is the leading ``---\\n...\\n---\\n`` block verbatim
    (including delimiters).
    """
    meta, body = split_frontmatter(text)
    if meta is None:
        return "", text
    return text[: len(text) - len(body)], body


def meta_from_dict(d: dict) -> SpecMeta:
    """Build a SpecMeta from a dict, ignoring unknown keys."""
    known = {f.name for f in fields(SpecMeta)}
    return SpecMeta(**{k: v for k, v in d.items() if k in known})


def meta_to_dict(m: SpecMeta) -> dict:
    """Serialize a SpecMeta to a plain dict (frontmatter order)."""
    return asdict(m)


def _render(meta: SpecMeta, body: str) -> str:
    """Render frontmatter + body back into document text."""
    fm = yaml.safe_dump(meta_to_dict(meta), sort_keys=False).rstrip("\n")
    return f"{_FM_DELIM}\n{fm}\n{_FM_DELIM}\n{body}"


def read_spec_meta(path: Path) -> SpecMeta | None:
    """Return the SpecMeta for ``path``, or None if missing/unmanaged."""
    if not path.exists():
        return None
    meta_dict, _ = split_frontmatter(path.read_text())
    if meta_dict is None:
        return None
    return meta_from_dict(meta_dict)


def read_spec_body(path: Path) -> str:
    """Return the document body (frontmatter stripped); '' if missing."""
    if not path.exists():
        return ""
    return strip_frontmatter(path.read_text())


def write_spec(
    path: Path,
    meta: SpecMeta,
    body: str,
    lock: ExecutorLock | None = None,
) -> None:
    """Atomically write frontmatter + body, optionally under a file lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    if lock is not None:
        acquired = lock.acquire()
        if not acquired:
            raise SpecLockError(
                f"could not acquire spec lock {lock.lock_path}; another spec mutation in progress"
            )
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(_render(meta, body))
            os.replace(tmp, str(path))
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
    finally:
        if lock is not None and acquired:
            lock.release()


def downstream_stages(stage: str) -> list[str]:
    """Stages strictly after ``stage`` in canonical order."""
    i = STAGES.index(stage)
    return list(STAGES[i + 1 :])


def resolve_next_stage(metas: dict[str, SpecMeta | None]) -> tuple[str, str]:
    """Compute ``(action, stage)`` from current per-stage metas.

    A stale stage anywhere takes priority; else the first missing stage is
    generated, then the first draft stage awaits approval; if all stages are
    approved, the pipeline is done.
    """
    for stage in STAGES:
        m = metas.get(stage)
        if m is not None and m.status == "stale":
            return ("stale", stage)
    for stage in STAGES:
        m = metas.get(stage)
        if m is None:
            return ("generate", stage)
        if m.status == "draft":
            return ("await_approval", stage)
    return ("done", STAGES[-1])


def stage_path(config: ExecutorConfig, stage: str) -> Path:
    """Map a stage name to its spec file path on ``config``."""
    paths: dict[str, Path] = {
        "requirements": config.requirements_file,
        "design": config.design_file,
        "tasks": config.tasks_file,
    }
    return paths[stage]


def _spec_lock(config: ExecutorConfig) -> ExecutorLock:
    """Build an ``ExecutorLock`` bound to ``config``'s spec lock file."""
    from .config import ExecutorLock

    return ExecutorLock(config.spec_lock_file)  # type: ignore[attr-defined]  # spec_lock_file added in a later task


def apply_approval(
    config: ExecutorConfig,
    stage: str,
    approver: str,
    now: str,
    fresh_validation: str,
) -> None:
    """Approve a stage: bump version, record approver, cascade stale downstream.

    Always cascades a ``stale`` status to every downstream stage that isn't
    already stale, since approval bumps the version and any generated
    downstream content may now be out of sync with the newly approved stage.
    """
    path = stage_path(config, stage)
    meta = read_spec_meta(path)
    if meta is None:
        raise ValueError(f"{stage} is unmanaged (no frontmatter)")
    lock = _spec_lock(config)
    meta.status = "approved"
    meta.version += 1
    meta.approved_by = approver
    meta.approved_at = now
    meta.validation = fresh_validation
    write_spec(path, meta, read_spec_body(path), lock=lock)
    for ds in downstream_stages(stage):
        ds_path = stage_path(config, ds)
        ds_meta = read_spec_meta(ds_path)
        if ds_meta is not None and ds_meta.status != "stale":
            ds_meta.status = "stale"
            write_spec(ds_path, ds_meta, read_spec_body(ds_path), lock=lock)
