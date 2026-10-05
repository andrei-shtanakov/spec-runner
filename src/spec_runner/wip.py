"""Carry a failed attempt's work forward as a WIP commit (spec 2026-10-04 §1).

Called before every destructive tree switch. Owns nothing else: whether the
work is trusted is the harness baseline's question, never this commit's.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import ExecutorConfig
from .git_ops import (
    WorktreeStatusError,
    current_branch,
    git_with_paths,
    indexed_paths,
    spec_contract_paths,
    uncommitted_work_paths,
)
from .logging import get_logger
from .phases import Refusal, RefusalKind
from .state import ExecutorState
from .tdd import resolve_namespace

logger = get_logger("wip")

WIP_TRAILER = "Spec-Runner-WIP"
WIP_ATTEMPT_TRAILER = "Spec-Runner-WIP-Attempt"


@dataclass(frozen=True)
class WipResult:
    """What `save_wip` did: a commit, a refusal, or neither (not ours)."""

    saved_sha: str | None
    refusal: Refusal | None


#: git's own words for "this is not a repository", matched in the C locale.
NOT_A_REPOSITORY = "not a git repository"


def _git(config: ExecutorConfig, *args: str) -> subprocess.CompletedProcess[str]:
    """git in the C locale: a refusal is told from "no repository" by its words."""
    return subprocess.run(
        ["git", "--literal-pathspecs", *args],
        cwd=config.project_root,
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C", "LANGUAGE": ""},
    )


def owner(config: ExecutorConfig, state: ExecutorState) -> str | None:
    """The started task whose recorded branch is exactly the current one."""
    branch = current_branch(config)
    if branch is None:
        return None
    return state.workspace_for_branch(resolve_namespace(config), branch)


def unowned_dirt_refusal(config: ExecutorConfig, state: ExecutorState) -> Refusal | None:
    """Under `strict`: uncommitted work on a ``task/*`` branch no recorded task owns.

    Spec §1, second bullet: whose work it is cannot be told, so it is neither
    committed nor stashed. Checked before either destructive point. Raises
    what `owner` raises (a state DB error); an unreadable tree is `instrument`.
    """
    from .harness import TRUST_REMEDY

    if config.harness_guard != "strict":
        return None
    branch = current_branch(config) or ""
    if not branch.startswith("task/") or owner(config, state) is not None:
        return None
    try:
        dirt = uncommitted_work_paths(config, spec_contract_paths(config), strict=True)
    except WorktreeStatusError as exc:
        return Refusal(f"could not read the working tree: {exc}", RefusalKind.INSTRUMENT)
    if not dirt:
        return None
    return Refusal(
        f"uncommitted work on {branch} belongs to no recorded task; {TRUST_REMEDY}",
        RefusalKind.POLICY,
    )


class WipReadError(RuntimeError):
    """The WIP history could not be read; callers turn this into a refusal."""


class IndexUnreadable(RuntimeError):
    """`git diff` could not say what is staged."""


def _partially_staged(config: ExecutorConfig, paths: list[str]) -> list[str]:
    cached = _git(config, "diff", "--cached", "--name-only", "-z")
    worktree = _git(config, "diff", "--name-only", "-z")
    if cached.returncode != 0 or worktree.returncode != 0:
        raise IndexUnreadable((cached.stderr or worktree.stderr).strip()[:200] or "git diff failed")
    staged = set(cached.stdout.split("\0"))
    unstaged = set(worktree.stdout.split("\0"))
    return sorted(p for p in paths if p in staged and p in unstaged)


def _stage(config: ExecutorConfig, paths: list[str]) -> subprocess.CompletedProcess[str]:
    """`git add -A` for the paths git still knows about or that exist on disk.

    A staged rename's source is gone from the tree *and* the index; `git add`
    refuses such a path, though `commit --only` still records its removal.
    """
    indexed = indexed_paths(config)
    known = [p for p in paths if p in indexed or os.path.lexists(config.project_root / p)]
    return git_with_paths(config, ["add", "-A"], known) if known else _git(config, "status")


def save_wip(config: ExecutorConfig, state: ExecutorState) -> WipResult:
    """Commit the owned task's eligible changes as WIP, or refuse."""
    task_id = owner(config, state)
    if task_id is None:
        return WipResult(None, None)
    paths = uncommitted_work_paths(config, spec_contract_paths(config), strict=True)
    if not paths:
        return WipResult(None, None)
    try:
        split = _partially_staged(config, paths)
    except IndexUnreadable as exc:
        return WipResult(
            None,
            Refusal(
                f"cannot tell what is staged before saving {task_id}'s work as WIP ({exc}); "
                "the work is in the tree and nothing destructive ran",
                RefusalKind.INSTRUMENT,
            ),
        )
    if split:
        return WipResult(
            None,
            Refusal(
                f"cannot save {task_id}'s work as WIP: {', '.join(split)} "
                "differ between HEAD, the index and the working tree; commit or unstage "
                "them, then retry — the work is in the tree and nothing destructive ran",
                RefusalKind.POLICY,
            ),
        )
    attempt = state.get_task_state(task_id).attempt_count
    message = (
        f"wip({task_id}): unfinished work of attempt {attempt} — not a candidate\n\n"
        f"{WIP_TRAILER}: {task_id}\n{WIP_ATTEMPT_TRAILER}: {attempt}\n"
    )
    added = _stage(config, paths)
    committed = (
        git_with_paths(config, ["commit", "--only", "-m", message], paths)
        if added.returncode == 0
        else added
    )
    if committed.returncode != 0:
        return WipResult(
            None,
            Refusal(
                f"could not save {task_id}'s work as WIP "
                f"({committed.stderr.strip()[:200] or 'git failed'}); the work is in the "
                "tree and nothing destructive ran",
                RefusalKind.INSTRUMENT,
            ),
        )
    sha = _git(config, "rev-parse", "HEAD").stdout.strip()
    logger.info("Saved WIP", task_id=task_id, sha=sha, paths=len(paths))
    return WipResult(sha, None)


def save_wip_before_forgetting_attempts(
    config: ExecutorConfig, state: ExecutorState
) -> Refusal | None:
    """Save the owned task's work as WIP before its attempt records are erased.

    PR #661 blocker 2: the trailer's ``N`` is the last recorded attempt
    (spec §1). `retry --fresh`, `run --all`'s failed → pending reset and
    `reset` erase those records, after which the next start would write
    "attempt 0" about work a real attempt produced. Called first, it keeps
    the real number. No eligible dirt, or a branch nobody owns: nothing to do.
    A state DB or tree that cannot be read is an ``instrument`` refusal.
    """
    try:
        return save_wip(config, state).refusal
    except (sqlite3.Error, OSError, WorktreeStatusError) as exc:
        return Refusal(
            f"could not tell whether the tree holds a task's unfinished work ({exc})",
            RefusalKind.INSTRUMENT,
        )


def is_wip_of(config: ExecutorConfig, sha: str, task_id: str) -> bool:
    """Whether `sha` carries this task's WIP trailer; a git error reads as False.

    Right for a walk that stops at the first other commit; a caller that
    must not mistake "could not look" for "not WIP" uses `_trailer_values`.
    """
    body = _git(config, "log", "-1", "--format=%(trailers:key=" + WIP_TRAILER + ",valueonly)", sha)
    return body.returncode == 0 and task_id in body.stdout.split()


def _trailer_values(config: ExecutorConfig, sha: str, key: str) -> list[str]:
    """The values of trailer `key` on `sha`; a git error raises `WipReadError`."""
    body = _git(config, "log", "-1", "--format=%(trailers:key=" + key + ",valueonly)", sha)
    if body.returncode != 0:
        raise WipReadError(body.stderr.strip()[:200] or f"cannot read the trailers of {sha}")
    return body.stdout.split()


def wip_status(config: ExecutorConfig, sha: str, task_id: str) -> bool | None:
    """Whether `sha` is this task's WIP commit: True, False, or None (git cannot tell).

    The question every evidence writer asks before naming a SHA (PR #661
    blocker 1). Without a per-task branch no WIP exists, so the answer is False
    without a git call; an empty SHA names nothing and is False too.
    """
    if not sha or not config.create_git_branch:
        return False
    try:
        return task_id in _trailer_values(config, sha, WIP_TRAILER)
    except WipReadError:
        return None


def _is_repository(config: ExecutorConfig) -> bool:
    """Whether the project is in a repository; False only when provably not.

    PR #661 blocker 4: "provably not" is git's own "not a git repository"
    **and** no `.git` entry at the project root or above it — git reads a
    `.git` with a corrupt `HEAD` as no repository at all and keeps walking
    up. Any other failure (dubious ownership, a permission error) raises.
    """
    found = _git(config, "rev-parse", "--git-dir")
    if found.returncode == 0:
        return True
    detail = found.stderr.strip()[:300] or "git rev-parse --git-dir failed"
    if NOT_A_REPOSITORY not in found.stderr:
        raise WipReadError(detail)
    try:
        root = Path(config.project_root).resolve()
        damaged = next((p for p in (root, *root.parents) if (p / ".git").exists()), None)
    except OSError as exc:
        # An unreadable parent cannot prove there is no `.git` above.
        raise WipReadError(f"cannot look for a .git above the project: {exc}") from exc
    if damaged is not None:
        raise WipReadError(f"{damaged / '.git'} exists but git cannot read it: {detail}")
    return False


def _require_unborn(config: ExecutorConfig, head: subprocess.CompletedProcess[str]) -> None:
    """Return only when HEAD is provably unborn (no commit anywhere); else raise.

    Unborn: HEAD is a symbolic ref and `rev-list -n1 --all` succeeds with
    nothing. A detached HEAD, a branch naming a missing object or an
    unreadable object store is a damaged repository, not an empty one.
    """
    reason = head.stderr.strip()[:200] or "HEAD does not name a commit"
    if _git(config, "symbolic-ref", "-q", "HEAD").returncode != 0:
        raise WipReadError(f"cannot read HEAD: {reason}")
    listed = _git(config, "rev-list", "-n1", "--all")
    if listed.returncode != 0:
        raise WipReadError(
            f"cannot read HEAD ({reason}): {listed.stderr.strip()[:200] or 'rev-list failed'}"
        )
    if listed.stdout.strip():
        raise WipReadError(f"HEAD names no commit although the repository has some: {reason}")


def wip_base(config: ExecutorConfig) -> str | None:
    """Where the task's branch forked, or None when there provably is no WIP.

    No WIP can exist without a per-task branch, outside a repository or before
    its first commit (bootstrap tasks run `git init`) — each *proven*, see
    `_is_repository` and `_require_unborn` — on the main branch itself, or on
    a task branch with no commit of its own (merge-base == HEAD). Anything
    else that cannot be computed raises `WipReadError` instead of reading as
    "no WIP" (review.task_base falls back to ``HEAD~1`` there).
    """
    from .git_ops import get_main_branch

    if not config.create_git_branch:
        return None
    if not _is_repository(config):
        return None
    head = _git(config, "rev-parse", "--verify", "HEAD^{commit}")
    tip = head.stdout.strip()
    if head.returncode != 0 or not tip:
        _require_unborn(config, head)
        return None
    main = get_main_branch(config)
    if current_branch(config) == main:
        return None
    merge_base = _git(config, "merge-base", "HEAD", main)
    base = merge_base.stdout.strip()
    if merge_base.returncode != 0 or not base:
        raise WipReadError(merge_base.stderr.strip()[:200] or "cannot compute the task's base")
    return None if base == tip else base


def wip_commits(
    config: ExecutorConfig, task_id: str, base: str
) -> list[tuple[str, int, list[str]]]:
    """This task's WIP commits in `base..HEAD`, oldest first.

    Strict: any git error while reading a commit raises `WipReadError` rather
    than dropping that commit from the continuation (final review #7).
    """
    listed = _git(config, "rev-list", "--reverse", f"{base}..HEAD")
    if listed.returncode != 0:
        raise WipReadError(listed.stderr.strip()[:200] or "git rev-list failed")
    shas = listed.stdout.split()
    found: list[tuple[str, int, list[str]]] = []
    for sha in shas:
        if task_id not in _trailer_values(config, sha, WIP_TRAILER):
            continue
        attempt = _trailer_values(config, sha, WIP_ATTEMPT_TRAILER)
        try:
            number = int(attempt[0]) if attempt else 0
        except ValueError as exc:
            raise WipReadError(f"malformed {WIP_ATTEMPT_TRAILER} on {sha}") from exc
        shown = _git(config, "show", "--name-only", "-z", "--format=", sha)
        if shown.returncode != 0:
            raise WipReadError(shown.stderr.strip()[:200] or f"cannot list the files of {sha}")
        found.append((sha, number, [f for f in shown.stdout.split("\0") if f]))
    return found


def head_is_wip_of(config: ExecutorConfig, task_id: str) -> bool:
    """Whether HEAD is this task's WIP commit; an unreadable HEAD raises.

    `is_wip_of` answers False on a git error, which is right for a walk that
    stops and wrong for a gate: "could not look" must not read as "not WIP".
    """
    head = _git(config, "rev-parse", "--verify", "HEAD")
    sha = head.stdout.strip()
    if head.returncode != 0 or not sha:
        raise WipReadError(head.stderr.strip()[:200] or "cannot read HEAD")
    return task_id in _trailer_values(config, sha, WIP_TRAILER)
