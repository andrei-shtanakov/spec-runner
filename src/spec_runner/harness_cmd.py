"""`spec-runner harness trust` — an operator confirms a task's harness (spec §3).

Not a way around a refusal: the operator restores and checks the harness
first, then states that it is trusted, with a reason that is kept.
"""

from __future__ import annotations

import argparse
import sqlite3

from .config import ExecutorConfig
from .git_ops import current_branch
from .harness import HarnessStateError, surface_snapshot
from .remedy import RemedyError, _guard, resolve_actor
from .state import ExecutorState
from .task import parse_tasks_text


class TrustError(RuntimeError):
    """`harness trust` refused."""


def _check_binding(
    config: ExecutorConfig,
    workspace: dict | None,
    task_id: str,
    branch: str | None,
    bind_branch: str | None,
) -> None:
    """Refuse unless the recorded (or named) branch is the one checked out.

    A named ``--bind-branch`` must be the current branch whether it binds a
    new row or an existing row whose branch is NULL.
    """
    if workspace is not None:
        recorded = workspace["branch"]
        if recorded is not None and recorded != branch:
            raise TrustError(
                f"{task_id}'s recorded branch is {recorded}, the current branch is {branch}"
            )
        if recorded is None and bind_branch is not None and bind_branch != branch:
            raise TrustError(f"--bind-branch {bind_branch} is not the current branch ({branch})")
        return
    if not config.create_git_branch:
        return
    if bind_branch is None:
        raise TrustError(
            f"{task_id} has no workspace record; name its branch with "
            "--bind-branch <current branch>"
        )
    if bind_branch != branch:
        raise TrustError(f"--bind-branch {bind_branch} is not the current branch ({branch})")


def _require_task(config: ExecutorConfig, task_id: str) -> None:
    """Refuse an unknown task; an unreadable or unparseable tasks.md is an instrument error."""
    path = config.tasks_file
    if not path.exists():
        raise TrustError(f"no task {task_id}: {path} does not exist")
    try:
        tasks = parse_tasks_text(path.read_text())
    except (OSError, ValueError) as exc:
        raise HarnessStateError(f"{path} could not be read or parsed: {exc}") from exc
    if not any(t.id == task_id for t in tasks):
        raise TrustError(f"no task {task_id} in {path}")


def _require_readable(files: dict[str, bytes | None]) -> None:
    """Strict refuses a baseline with an unreadable file, so trusting one does nothing."""
    unreadable = sorted(k for k, v in files.items() if v is None)
    if unreadable:
        raise TrustError(
            "harness files cannot be read: "
            + ", ".join(unreadable)
            + "; fix them, then re-run `harness trust`"
        )


def trust(
    config: ExecutorConfig,
    state: ExecutorState,
    task_id: str,
    *,
    reason: str,
    bind_branch: str | None = None,
    actor: str | None = None,
) -> str:
    """Record an operator-trusted baseline for `task_id`; return a summary.

    Confirms a harness the operator already restored and checked; it does not
    restore or check anything itself.
    """
    try:
        namespace = _guard(config, reason)
    except RemedyError as exc:
        raise TrustError(str(exc)) from exc
    _require_task(config, task_id)
    branch = current_branch(config) if config.create_git_branch else None
    workspace = state.get_workspace(namespace, task_id)
    _check_binding(config, workspace, task_id, branch, bind_branch)
    bind = workspace is None
    # An existing row with no branch (the start never ended on the task
    # branch) owns nothing under `strict`; naming the current branch binds it.
    fill = (
        workspace is not None
        and workspace["branch"] is None
        and config.create_git_branch
        and bind_branch is not None
    )
    surface, files = surface_snapshot(config)
    _require_readable(files)
    replaced = state.trust_harness(
        namespace,
        task_id,
        bind=bind,
        fill_branch=fill,
        bind_branch=bind_branch if ((bind or fill) and config.create_git_branch) else None,
        branch=branch,
        surface=surface,
        files=files,
        guard_mode=config.harness_guard,
        actor=resolve_actor(config, actor),
        reason=reason.strip(),
        run_id=None,
    )
    note = f" (replaced the earlier {replaced} baseline)" if replaced else ""
    return f"✅ {task_id}: harness baseline trusted{note}"


def cmd_harness(args: argparse.Namespace, config: ExecutorConfig) -> int:
    """Dispatch `spec-runner harness <command>`: 0 done, 1 refused, 2 instrument."""
    if args.harness_command != "trust":
        print("usage: spec-runner harness trust TASK --reason …")
        return 1
    try:
        with ExecutorState(config) as state:
            print(
                trust(
                    config,
                    state,
                    args.task_id,
                    reason=args.reason,
                    bind_branch=args.bind_branch,
                    actor=args.actor,
                )
            )
    except TrustError as exc:
        print(f"⛔ {exc}")
        return 1
    except sqlite3.IntegrityError as exc:
        # A workspace row appeared between the read and the insert.
        print(f"⛔ the task's workspace record changed while trusting ({exc}); re-run the command")
        return 1
    except (sqlite3.Error, HarnessStateError, OSError) as exc:
        print(f"⛔ the state DB or a harness file could not be read or written: {exc}")
        return 2
    return 0
