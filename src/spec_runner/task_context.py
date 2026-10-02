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
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import ExecutorConfig
    from .task import Task

#: Upper bound on the quoted sections, in characters. A bundle is hundreds of
#: kilobytes; what does not fit is named, not dropped silently.
MAX_QUOTED_CHARS = 60_000

_ID = re.compile(r"\b([A-Z][A-Z0-9]*-\d+)(?![\w-])")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_HEADING_ID = re.compile(r"([A-Z][A-Z0-9]*-\d+)(?![\w-])")


def resolve_context_files(config: ExecutorConfig) -> list[Path]:
    """The declared context files for this namespace, in declared order.

    `{prefix}`/`{ws}` are substituted as in an external stage path (#338). An
    entry with a placeholder belongs to a namespaced workstream and is skipped
    when no `--spec-prefix` is given.

    Raises:
        ConfigError: on a malformed entry, a path outside the project, or a
            file that does not exist.
    """
    from .config import ConfigError
    from .spec import _PLACEHOLDER, ProfileError, _check_path_template

    root = Path(config.project_root).resolve()
    prefix = config.spec_prefix or ""
    ws = prefix[:-1] if prefix.endswith("-") else prefix
    out: list[Path] = []
    for entry in config.task_context_files:
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
        if not path.is_file():
            raise ConfigError(
                f"task_context_files: {entry!r} resolves to {path}, which is not a file — "
                "the tasks would run without the specification they were declared to need"
            )
        out.append(path)
    return out


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
    """The prompt section for `task`, or "" when nothing is declared."""
    files = config.resolve_task_context_files()
    if not files:
        return ""
    root = Path(config.project_root).resolve()
    ids = referenced_ids(task)
    quoted: list[str] = []
    omitted: list[str] = []
    used = 0
    for path in files:
        rel = path.relative_to(root).as_posix()
        sections = sections_by_id(path.read_text(encoding="utf-8"))
        for section_id in ids:
            body = sections.get(section_id)
            if body is None:
                continue
            if used + len(body) > MAX_QUOTED_CHARS:
                omitted.append(f"{section_id} ({rel})")
                continue
            quoted.append(f"<!-- {rel} -->\n{body}")
            used += len(body)

    listing = "\n".join(f"- `{p.relative_to(root).as_posix()}`" for p in files)
    parts = [
        "## Specification context",
        "",
        "This task is specified in the files below, not under `spec/`. Read them for "
        "anything this prompt does not quote — including sections the checklist "
        "cites by number (e.g. §2.2).",
        "",
        listing,
    ]
    if quoted:
        parts += ["", "### Sections this task references", "", "\n\n".join(quoted)]
    if omitted:
        parts += ["", f"Not quoted for length — read them in the files: {', '.join(omitted)}."]
    return "\n".join(parts) + "\n"
