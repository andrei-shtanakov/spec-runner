"""The status flip a blocked task leaves behind, committed — #192 (F-8).

`post_done_hook` writes `🔍 REVIEW` into `tasks.md` when review *starts*, so a
run killed mid-review is resumable rather than prematurely DONE (#66). That
write is uncommitted by design: the commit that would carry it comes later. But
when a pre-terminal gate then blocks, there *is* no later — the run stops, and
the next one meets the dirty-spec guard (#69) and refuses, because `tasks.md`
is dirty. Both behaviours are right; together they are a recovery deadlock,
whose only exits were `--allow-dirty-spec` (which disarms the guard for real
spec edits too) or committing a status flip the operator did not make.

So the blocked path commits it:

```
candidate commit → gate UNSATISFIED → bookkeeping commit (status only) → stop
```

Two things this module is careful about, because they are what make the commit
safe rather than merely convenient:

**Only a proven status-only transition.** The proof is textual and total: the
committed file and the working file may differ in exactly one line, that line
must be the named task's meta line in both, and only its status may have
changed. A checklist tick, a renamed task, an edited dependency, a new task, a
line of prose — any of these and the file is not bookkeeping, so it stays dirty
and the next run's guard is doing its job rather than deadlocking. This is
deliberately stricter than comparing parsed `Task` objects: the parser ignores
prose, and "the parser didn't notice" is not the same as "nothing changed".

**Idempotence comes from the diff, not from a marker.** Resuming re-writes the
same status, which produces no diff, so there is nothing to commit — that is
what keeps a resumed task from growing a chain of identical REVIEW commits.
"""

from __future__ import annotations

import difflib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .logging import get_logger
from .task import STATUS_EMOJI, STATUS_FROM_EMOJI, TASK_HEADER, TASK_META

logger = get_logger("bookkeeping")

#: The only transition this module will commit. A `done` flip is a terminal
#: claim, not bookkeeping, and must never ride in on this path.
BOOKKEEPING_STATUS = "review"


@dataclass(frozen=True)
class StatusFlip:
    """A proven status-only change to one task."""

    task_id: str
    previous: str
    new: str


def _meta_index(lines: list[str], task_id: str) -> int | None:
    """Index of ``task_id``'s meta line, searched only within its own block.

    Same window rule as `update_task_status`: from the exact header to the next
    header. A neighbouring task's meta line is never mistaken for the target's.
    """
    header_index = None
    for i, line in enumerate(lines):
        match = TASK_HEADER.match(line)
        if match and match.group(1) == task_id:
            header_index = i
            break
    if header_index is None:
        return None
    for j in range(header_index + 1, len(lines)):
        if TASK_HEADER.match(lines[j]):
            return None
        if TASK_META.match(lines[j]):
            return j
    return None


def _status_of(line: str) -> str | None:
    """The status a meta line declares, or None if it declares none."""
    for emoji, status in STATUS_FROM_EMOJI.items():
        if emoji in line:
            return status
    match = TASK_META.match(line)
    return match.group(2).lower() if match else None


def status_only_transition(before: str, after: str, task_id: str) -> StatusFlip | None:
    """The status change from ``before`` to ``after``, if that is *all* it is.

    Returns None when the texts are identical, when anything outside
    ``task_id``'s status changed, or when the change cannot be proven to be a
    status flip. Fail-closed on purpose: the caller commits on a yes, and
    committing somebody's spec edit as bookkeeping is the failure worth
    avoiding.
    """
    if before == after:
        return None

    # Whole-text, frontmatter included: a governance stamp changing at the same
    # moment is a spec change like any other, not something to skip past.
    old_lines = before.split("\n")
    new_lines = after.split("\n")

    changes = [
        op
        for op in difflib.SequenceMatcher(None, old_lines, new_lines).get_opcodes()
        if op[0] != "equal"
    ]
    if len(changes) != 1:
        return None
    tag, i1, i2, j1, j2 = changes[0]
    if tag != "replace" or i2 - i1 != 1 or j2 - j1 != 1:
        return None

    if _meta_index(old_lines, task_id) != i1 or _meta_index(new_lines, task_id) != j1:
        return None

    previous = _status_of(old_lines[i1])
    new = _status_of(new_lines[j1])
    if not previous or not new or previous == new:
        return None

    # The line may carry more than a status. Neutralise the statuses and
    # require what is left to be identical, so a priority or a trailing note
    # changed in the same line is caught rather than waved through.
    if _without_status(old_lines[i1], previous) != _without_status(new_lines[j1], new):
        return None

    return StatusFlip(task_id=task_id, previous=previous, new=new)


def _without_status(line: str, status: str) -> str:
    """The meta line with its status token removed, for comparing the rest."""
    stripped = line.replace(STATUS_EMOJI[status], "")
    return stripped.replace(status.upper(), "").replace(status.lower(), "")


def _git(config, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=config.project_root, capture_output=True, text=True)


def commit_status_flip(
    config,
    task_id: str,
    *,
    candidate_sha: str,
    verdict: str,
) -> str | None:
    """Commit the blocked task's status flip. Returns a problem, or None.

    None means "nothing needed doing or it is done": an untracked `tasks.md`
    (orchestrators that keep generated specs out of git never meet the guard),
    an unchanged file (a resumed run — see the module docstring), or a
    successful commit.

    A returned string is a **failure to make the tree clean**, and the caller
    must surface it: the operator is about to meet the dirty-spec guard, and a
    stop that quietly left the deadlock in place would be the original bug with
    an extra commit attempt in front of it.
    """
    tasks_file: Path = config.tasks_file
    if not tasks_file.exists():
        return None
    try:
        rel = str(tasks_file.relative_to(config.project_root))
    except ValueError:
        return None

    committed = _git(config, "show", f"HEAD:{rel}")
    if committed.returncode != 0:
        # Untracked (or no commits yet): `git status --porcelain` does not
        # report it either, so there is no deadlock to prevent.
        return None

    flip = status_only_transition(committed.stdout, tasks_file.read_text(), task_id)
    if flip is None:
        if committed.stdout == tasks_file.read_text():
            return None
        return (
            f"{rel} differs from the last commit by more than {task_id}'s status, "
            "so the status flip was not committed on its own; the next run will "
            "refuse until the spec changes are committed"
        )
    if flip.new != BOOKKEEPING_STATUS:
        return (
            f"refusing to commit a {flip.previous} → {flip.new} transition as "
            f"bookkeeping: only '{BOOKKEEPING_STATUS}' is a process record"
        )

    message = (
        f"{task_id}: status {flip.previous} → {flip.new} (blocked before merge)\n"
        "\n"
        "Bookkeeping only — written by spec-runner, no task work in this commit.\n"
        "The task stays resumable; nothing was merged and nothing is DONE.\n"
        "\n"
        f"Task-Status: {task_id} {flip.previous} -> {flip.new}\n"
        f"Gate-Candidate: {candidate_sha}\n"
        f"Gate-Verdict: {verdict}\n"
    )
    add = _git(config, "add", "--", rel)
    if add.returncode != 0:
        return f"could not stage {rel}: {add.stderr.strip()[:200]}"
    commit = _git(config, "commit", "-m", message, "--", rel)
    if commit.returncode != 0:
        _git(config, "reset", "-q", "--", rel)
        return f"could not commit {rel}: {commit.stderr.strip()[:200]}"
    logger.info(
        "Committed the blocked task's status flip",
        task_id=task_id,
        previous=flip.previous,
        new=flip.new,
        candidate=candidate_sha[:12],
    )
    return None


__all__ = [
    "BOOKKEEPING_STATUS",
    "StatusFlip",
    "commit_status_flip",
    "status_only_transition",
]
