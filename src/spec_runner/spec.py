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
    from .config import ExecutorLock

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
