"""Carry a failed attempt's work forward as a WIP commit (spec 2026-10-04 §1).

Called before every destructive tree switch. Owns nothing else: whether the
work is trusted is the harness baseline's question, never this commit's.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .config import ExecutorConfig
from .git_ops import current_branch, spec_contract_paths, uncommitted_work_paths
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


def _git(config: ExecutorConfig, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=config.project_root, capture_output=True, text=True)


def owner(config: ExecutorConfig, state: ExecutorState) -> str | None:
    """The started task whose recorded branch is exactly the current one."""
    branch = current_branch(config)
    if branch is None:
        return None
    return state.workspace_for_branch(resolve_namespace(config), branch)


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
    added = _git(config, "add", "-A", "--", *paths)
    committed = (
        _git(config, "commit", "--only", "-m", message, "--", *paths)
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


def is_wip_of(config: ExecutorConfig, sha: str, task_id: str) -> bool:
    """Whether `sha` carries this task's WIP trailer."""
    body = _git(config, "log", "-1", "--format=%(trailers:key=" + WIP_TRAILER + ",valueonly)", sha)
    return body.returncode == 0 and task_id in body.stdout.split()


def wip_commits(
    config: ExecutorConfig, task_id: str, base: str
) -> list[tuple[str, int, list[str]]]:
    """This task's WIP commits in `base..HEAD`, oldest first."""
    shas = _git(config, "rev-list", "--reverse", f"{base}..HEAD").stdout.split()
    found: list[tuple[str, int, list[str]]] = []
    for sha in shas:
        if not is_wip_of(config, sha, task_id):
            continue
        attempt = _git(
            config,
            "log",
            "-1",
            "--format=%(trailers:key=" + WIP_ATTEMPT_TRAILER + ",valueonly)",
            sha,
        ).stdout.strip()
        files = _git(config, "show", "--name-only", "--format=", sha).stdout.split("\n")
        found.append((sha, int(attempt or 0), [f for f in files if f]))
    return found
