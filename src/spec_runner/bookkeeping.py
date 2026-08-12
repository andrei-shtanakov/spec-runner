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
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .logging import get_logger
from .task import TASK_HEADER, TASK_META, TASK_STATUS_WORDS

logger = get_logger("bookkeeping")

#: The transitions this module will commit — the three the *harness* writes
#: about its own process: `in_progress` when a task starts, `review` when
#: review starts (#66), and `blocked` when a task stops without finishing. Each
#: can be left uncommitted at a point where the run ends, and each then
#: deadlocks the next one.
#:
#: `done` is deliberately absent. It is a claim about the work, it is written
#: with the checklist and carried by the task's own commit, and letting it
#: through here would turn a bookkeeping path into a way to complete a task.
#: `todo` is absent too: it comes from operator commands (`reset`) rather than
#: from a run, and an operator who edits the spec can commit the edit.
BOOKKEEPING_STATUSES = frozenset({"in_progress", "review", "blocked"})


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


#: `TASK_META` with the status emoji and word captured *positionally*, so the
#: status can be neutralised where it actually is rather than by replacing the
#: word wherever it appears. Kept in step with `task.TASK_META` by refusing to
#: answer when the two disagree — see `_split_meta`.
_META_PARTS = re.compile(
    r"^((?:[ \t]*[-*]\s+)?(?:(?:🔴|🟠|🟡|🟢)\s+)?P\d\s*\|\s*)"
    r"((?:⬜|🔄|🔍|✅|⏸️)\s+)?"
    rf"((?i:{'|'.join(TASK_STATUS_WORDS)}))\b"
    r"(.*)$"
)


def _split_meta(line: str) -> tuple[str, str] | None:
    """``(status, the line with its status token blanked)``, or None.

    The blanked form is what proves "only the status changed": everything
    outside the matched status span — priority, separators, any trailing note —
    must compare identical. A global string replace could not do this. A note
    reading ``see TODO below`` would be stripped from one version and not the
    other, which at best refuses a legitimate flip and at worst lets two
    genuinely different lines look alike.
    """
    match = _META_PARTS.match(line)
    if not match:
        return None
    prefix, _emoji, word, rest = match.groups()
    return word.lower(), f"{prefix}\0{rest}"


def _status_of(line: str) -> str | None:
    """The status a meta line declares, or None if it declares none."""
    parts = _split_meta(line)
    return parts[0] if parts else None


def _task_owning(lines: list[str], index: int) -> str | None:
    """The task whose block contains ``index``, by the nearest header above it."""
    for i in range(index, -1, -1):
        match = TASK_HEADER.match(lines[i])
        if match:
            return match.group(1)
    return None


def status_only_transition(
    before: str, after: str, task_id: str | None = None
) -> StatusFlip | None:
    """The status change from ``before`` to ``after``, if that is *all* it is.

    ``task_id`` names the task the caller is acting for; passing None asks
    which task changed, which is what recovery needs — an interrupted run left
    a flip behind and nobody is holding the task object any more. Either way
    the proof is the same, because the answer is only accepted when the changed
    line *is* that task's meta line.

    Returns None when the texts are identical, when anything outside the task's
    status changed, or when the change cannot be proven to be a status flip.
    Fail-closed on purpose: the caller commits on a yes, and committing
    somebody's spec edit as bookkeeping is the failure worth avoiding.
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

    if task_id is None:
        # Inferred from the changed line, then held to the same standard: the
        # checks below still require it to be *that* task's meta line in both
        # versions, so inference cannot widen what gets accepted.
        task_id = _task_owning(new_lines, j1)
        if task_id is None or task_id != _task_owning(old_lines, i1):
            return None

    if _meta_index(old_lines, task_id) != i1 or _meta_index(new_lines, task_id) != j1:
        return None

    old_meta = _split_meta(old_lines[i1])
    new_meta = _split_meta(new_lines[j1])
    if old_meta is None or new_meta is None:
        # `TASK_META` recognised the line and this did not: the two patterns
        # have drifted apart. Refusing is the safe direction — an unproven
        # status-only claim must never become a commit.
        return None
    previous, old_rest = old_meta
    new, new_rest = new_meta
    if previous == new:
        return None
    # Everything outside the status span, compared where it stands.
    if old_rest != new_rest:
        return None

    return StatusFlip(task_id=task_id, previous=previous, new=new)


def _git(config, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=config.project_root, capture_output=True, text=True)


def commit_status_flip(
    config,
    task_id: str,
    *,
    reason: str,
    candidate_sha: str = "",
) -> str | None:
    """Commit a harness-authored status flip. Returns a problem, or None.

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

    # Read once. The file is decided about, staged and then verified against
    # *this* text: a second read could be a different file, and the whole point
    # of the proof is that what gets committed is what was proven.
    working = tasks_file.read_text()
    flip = status_only_transition(committed.stdout, working, task_id)
    if flip is None:
        if committed.stdout == working:
            return None
        return (
            f"{rel} differs from the last commit by more than {task_id}'s status, "
            "so the status flip was not committed on its own; the next run will "
            "refuse until the spec changes are committed"
        )
    if flip.new not in BOOKKEEPING_STATUSES:
        allowed = ", ".join(sorted(BOOKKEEPING_STATUSES))
        return (
            f"refusing to commit a {flip.previous} → {flip.new} transition as "
            f"bookkeeping: only {allowed} are process records"
        )

    message = (
        f"{task_id}: status {flip.previous} → {flip.new} (spec-runner bookkeeping)\n"
        "\n"
        "Bookkeeping only — written by spec-runner, no task work in this commit.\n"
        "The task stays resumable; nothing was merged and nothing is DONE.\n"
        "\n"
        f"Task-Status: {task_id} {flip.previous} -> {flip.new}\n"
        f"Status-Reason: {reason}\n"
    )
    if candidate_sha:
        # Only the gate path has one. Recorded so the commit says which tree
        # was judged — the candidate, never this commit.
        message += f"Gate-Candidate: {candidate_sha}\n"
    add = _git(config, "add", "--", rel)
    if add.returncode != 0:
        return f"could not stage {rel}: {add.stderr.strip()[:200]}"
    if tasks_file.read_text() != working:
        # Somebody edited the file between the proof and the staging. Undo the
        # staging and refuse: what is in the tree is no longer what was proven.
        _git(config, "reset", "-q", "--", rel)
        return f"{rel} changed while the status flip was being committed; nothing was committed"
    commit = _git(config, "commit", "-m", message, "--", rel)
    if commit.returncode != 0:
        _git(config, "reset", "-q", "--", rel)
        return f"could not commit {rel}: {commit.stderr.strip()[:200]}"
    landed = _git(config, "show", f"HEAD:{rel}")
    if landed.returncode != 0 or landed.stdout != working:
        # `git commit -- <path>` takes the working-tree content, so a write
        # landing in that last instant would be committed unproven. It cannot
        # be undone from here, but it must not be reported as a clean stop.
        return (
            f"{rel} was committed with content that differs from what was "
            "proven status-only; inspect the last commit before continuing"
        )
    logger.info(
        "Committed the blocked task's status flip",
        task_id=task_id,
        previous=flip.previous,
        new=flip.new,
        candidate=candidate_sha[:12],
    )
    return None


def recover_interrupted_flip(config) -> StatusFlip | None:
    """Commit a status flip an interrupted run left behind. Returns it, or None.

    A run killed between writing a status and committing it leaves `tasks.md`
    dirty, and the next run refuses at the dirty-spec guard — the same deadlock
    as the blocked stop, reached by crashing instead of by stopping. Measured
    on a build from master: `SIGKILL` during review, then
    `⛔ Refusing to run: spec/config files have uncommitted changes`.

    The same proof decides: exactly one changed line, that task's meta line,
    status only, and a status the harness itself writes about its own process.
    Anything else — including a `done` left behind, which is a claim about work
    rather than about process — is left dirty for the guard to refuse, which is
    the guard doing its job.
    """
    if not getattr(config, "auto_commit", False):
        return None
    tasks_file: Path = config.tasks_file
    if not tasks_file.exists():
        return None
    try:
        rel = str(tasks_file.relative_to(config.project_root))
    except ValueError:
        return None
    committed = _git(config, "show", f"HEAD:{rel}")
    if committed.returncode != 0:
        return None
    flip = status_only_transition(committed.stdout, tasks_file.read_text())
    if flip is None or flip.new not in BOOKKEEPING_STATUSES:
        return None
    problem = commit_status_flip(config, flip.task_id, reason="recovered after an interrupted run")
    if problem:
        logger.warning("Could not recover an interrupted status flip", detail=problem)
        return None
    logger.info(
        "Recovered an interrupted status flip",
        task_id=flip.task_id,
        previous=flip.previous,
        new=flip.new,
    )
    return flip


def commit_status_flip_quietly(config, task_id: str, *, reason: str) -> None:
    """`commit_status_flip` for the terminal-failure path, which has no return
    channel to carry a problem.

    A task is already failing when this runs; the flip to `blocked` is the
    harness recording that, and it deadlocks the next run just as the `review`
    flip did (found by the battle test of the first fix, which cleaned up the
    review flip and then met the same wall one status later). Nothing here may
    raise: the failure being recorded is the important event, and a bookkeeping
    problem is logged loudly rather than taking the run tail with it — the same
    rule `_fail_for_budget` learned in #127.
    """
    if not getattr(config, "auto_commit", False):
        return
    try:
        problem = commit_status_flip(config, task_id, reason=reason)
    except Exception as exc:  # pragma: no cover - defensive
        problem = str(exc)
    if problem:
        logger.warning("Failed task left a dirty spec", task_id=task_id, detail=problem)


__all__ = [
    "BOOKKEEPING_STATUSES",
    "StatusFlip",
    "commit_status_flip",
    "commit_status_flip_quietly",
    "recover_interrupted_flip",
    "status_only_transition",
]
