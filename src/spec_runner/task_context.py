"""Specification context for task-execution prompts (`task_context_files`).

A workstream's specification lives in its bundle (`workstreams/<ws>/spec/`),
not in `spec/<prefix>requirements.md`/`design.md` — the only places the task
prompts used to look. The prompt then said "See <path>" for files that did not
exist, and the agent built TASK-002 of #480 without ever seeing the design it
was asked to follow (three attempts, the last one `TASK_BLOCKED` on exactly
that).

`task_context_files` names those files explicitly. The prompt lists them, and
quotes every section whose heading carries an id the task references
(`#### BEH-05: …`, `#### DT-02: …`), so the essentials arrive inline and the
rest is one read away. A declared file that does not exist is refused at
startup: a run without the context it was declared to need is the defect this
key exists to prevent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import ExecutorConfig
    from .task import Task

#: Upper bound on the quoted sections, in characters. A bundle is hundreds of
#: kilobytes; what does not fit is named, not dropped silently.
MAX_QUOTED_CHARS = 60_000

_ID_SHAPE = r"[A-Z][A-Z0-9]*-\d+[a-z]?(?![\w-])"
_ID = re.compile(rf"\b({_ID_SHAPE})")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HEADING_ID = re.compile(rf"({_ID_SHAPE})")


@dataclass(frozen=True)
class ContextFiles:
    """The declared files for one namespace: those present, and the optional
    ones this workstream does not have (named in the prompt, never hidden)."""

    present: list[Path]
    absent: list[str]


def _entry(raw: object) -> tuple[str, bool]:
    """`path` or `{path: …, optional: true}` → (path, optional)."""
    from .config import ConfigError

    if isinstance(raw, str):
        return raw, False
    if (
        isinstance(raw, dict)
        and isinstance(raw.get("path"), str)
        and set(raw)
        <= {
            "path",
            "optional",
        }
    ):
        optional = raw.get("optional", False)
        if isinstance(optional, bool):
            return raw["path"], optional
    raise ConfigError(
        f"task_context_files: {raw!r} — expected a path or {{path: <path>, optional: <bool>}}"
    )


def resolve_context_files(config: ExecutorConfig) -> ContextFiles:
    """The declared context files for this namespace, in declared order.

    `{prefix}`/`{ws}` are substituted as in an external stage path (#338). An
    entry with a placeholder belongs to a namespaced workstream and is skipped
    when no `--spec-prefix` is given. An `optional` entry may be absent — a
    bundle of an older shape has no design node — and is then named as such.

    Raises:
        ConfigError: on a malformed entry, a path outside the project, or a
            required file that does not exist.
    """
    from .config import ConfigError
    from .spec import _PLACEHOLDER, ProfileError, _check_path_template

    root = Path(config.project_root).resolve()
    prefix = config.spec_prefix or ""
    ws = prefix[:-1] if prefix.endswith("-") else prefix
    present: list[Path] = []
    absent: list[str] = []
    for raw in config.task_context_files:
        entry, optional = _entry(raw)
        try:
            _check_path_template(entry, "task_context_files")
        except ProfileError as exc:
            raise ConfigError(str(exc)) from None
        if _PLACEHOLDER.search(entry) and not prefix:
            continue
        path = (root / entry.format(prefix=prefix, ws=ws)).resolve()
        if not path.is_relative_to(root):
            raise ConfigError(
                f"task_context_files: {entry!r} resolves outside the project ({path})"
            )
        if path.is_file():
            present.append(path)
        elif optional:
            absent.append(path.relative_to(root).as_posix())
        else:
            raise ConfigError(
                f"task_context_files: {entry!r} resolves to {path}, which is not a file — "
                "the tasks would run without the specification they were declared to need "
                "(mark the entry `optional: true` if a workstream may lack it)"
            )
    return ContextFiles(present, absent)


def referenced_ids(task: Task) -> list[str]:
    """Ids the task mentions (BEH-05, FR-01, DT-02 …), first occurrence first."""
    text = "\n".join([task.description, *(item for item, _ in task.checklist), *task.traces_to])
    seen: dict[str, None] = {}
    for found in _ID.findall(text):
        if not found.startswith("TASK-"):
            seen.setdefault(found, None)
    return list(seen)


def sections_by_id(text: str) -> dict[str, str]:
    """Each heading that starts with an id → that heading's section.

    A section runs to the next heading of the same or a higher level.
    """
    lines = text.splitlines()
    headings: list[tuple[int, int, str | None]] = []
    in_fence = False
    for index, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        match = None if in_fence else _HEADING.match(line)
        if match:
            id_match = _HEADING_ID.match(match.group(2))
            headings.append((index, len(match.group(1)), id_match.group(1) if id_match else None))
    out: dict[str, str] = {}
    for position, (start, level, section_id) in enumerate(headings):
        if section_id is None or section_id in out:
            continue
        end = next((i for i, lvl, _ in headings[position + 1 :] if lvl <= level), len(lines))
        out[section_id] = "\n".join(lines[start:end]).strip()
    return out


def render_task_context(task: Task, config: ExecutorConfig) -> str:
    """The prompt section for `task`, or "" when nothing is declared.

    Sections are quoted in the order the task references their ids, so the
    task's own scenarios and decomposition entry come before its traces when
    the cap is reached.
    """
    files = config.resolve_task_context_files()
    if not files.present and not files.absent:
        return ""
    root = Path(config.project_root).resolve()
    by_file = [
        (path.relative_to(root).as_posix(), sections_by_id(path.read_text(encoding="utf-8")))
        for path in files.present
    ]
    quoted: list[str] = []
    omitted: list[str] = []
    used = 0
    for section_id in referenced_ids(task):
        for rel, sections in by_file:
            body = sections.get(section_id)
            if body is None:
                continue
            if used + len(body) > MAX_QUOTED_CHARS:
                omitted.append(f"{section_id} ({rel})")
                continue
            quoted.append(f"<!-- {rel} -->\n{body}")
            used += len(body)

    parts = [
        "## Specification context",
        "",
        "This task is specified in the files below, not under `spec/`. Read them for "
        "anything this prompt does not quote — including sections the checklist "
        "cites by number (e.g. §2.2).",
        "",
        "\n".join(f"- `{rel}`" for rel, _ in by_file),
    ]
    if files.absent:
        parts += ["", f"Declared but absent in this workstream: {', '.join(files.absent)}."]
    if quoted:
        parts += ["", "### Sections this task references", "", "\n\n".join(quoted)]
    if omitted:
        parts += ["", f"Not quoted for length — read them in the files: {', '.join(omitted)}."]
    return "\n".join(parts) + "\n"
