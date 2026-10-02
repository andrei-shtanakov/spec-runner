"""Run identity, run-start and closure (FR-01, FR-07, design § 6).

One ``RunContext`` per process, created by ``cli.main()``: a full UUIDv4
``run_id`` minted exactly once, the ``pipeline_id`` from the logging
contextvars, and -- for the subcommands in ``PAYING_SUBCOMMANDS`` -- a
run-start record written before the handler runs and one closure written by
the dispatcher's ``finally`` however the handler left.

Why the dispatcher and not the executor lock: ``retry`` and ``watch`` never
take the lock and ``run --force`` skips it, so a run-start tied to the lock
would leave three paying paths with call-starts under a ``run_id`` that "never
began". The kind of the closure is *derived* (``closure.derive``) from facts
the dispatcher observes itself; no exit site chooses it.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from . import closure as closure_mod
from .evidence import (
    AckNotReceived,
    Closure,
    Publisher,
    RunStart,
    policy_identity,
)

if TYPE_CHECKING:
    from .config import ExecutorConfig

#: Subcommands that pay, or change continuation-state (FR-01): each gets a
#: run-start and a closure. ``a:b`` is a subcommand of the group ``a``.
#: ``evidence:<run_id>`` (read-only) is deliberately absent.
PAYING_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "run",
        "retry",
        "watch",
        "plan",
        "review-pr",
        "doctor",
        "tdd:abandon",
        "tdd:repair",
        "tdd:resume",
        "tdd:release",
        "budget:authorize",
        "restore",
        "evidence:close-call",
        "evidence:purge",
    }
)

#: Attribute on the parsed args that names the second word, per group.
_SUBCOMMAND_ATTRS = {
    "tdd": "tdd_command",
    "budget": "budget_command",
    "evidence": "evidence_command",
}


def subcommand_key(args: Any) -> str:
    """``run``, ``tdd:abandon``, ... for the parsed ``args``."""
    command = str(getattr(args, "command", "") or "")
    attr = _SUBCOMMAND_ATTRS.get(command)
    sub = getattr(args, attr, None) if attr else None
    return f"{command}:{sub}" if sub else command


def is_paying(args: Any) -> bool:
    """Whether this invocation writes run-start and closure."""
    return subcommand_key(args) in PAYING_SUBCOMMANDS


class RunStartRefused(Exception):
    """The run-start could not be acknowledged; the invocation must not go on."""


@dataclass
class RunContext:
    """Identity of one invocation and the writer of its two bracketing records."""

    run_id: str
    pipeline_id: str | None
    subcommand: str
    started_at: str
    paying: bool = False
    publisher: Publisher | None = None
    started: bool = False
    closed: bool = False
    #: ``call_id`` of every call this invocation started, in order.
    call_ids: list[str] = field(default_factory=list)
    #: Calls with an acknowledged start and no recorded result.
    open_call_ids: set[str] = field(default_factory=set)

    # -- calls (the seam keeps these two honest) ---------------------------

    def note_call_started(self, call_id: str) -> None:
        self.call_ids.append(call_id)
        self.open_call_ids.add(call_id)

    def note_call_finished(self, call_id: str) -> None:
        self.open_call_ids.discard(call_id)

    # -- bracketing records ------------------------------------------------

    def start(self, config: ExecutorConfig) -> None:
        """Write run-start; the one call site is the dispatcher (design § 6.2).

        Raises ``RunStartRefused`` when the store does not acknowledge it:
        there is no honest way to proceed as a run that has no beginning.
        """
        self.paying = True
        self.publisher = Publisher.from_config(config)
        record = RunStart(
            run_id=self.run_id,
            pipeline_id=self.pipeline_id,
            subcommand=self.subcommand,
            started_at=self.started_at,
            policy=policy_identity(config),
            facts=_facts(config),
            repository=_repository(config),
            ack_channel=self.publisher.channel,
        )
        try:
            self.publisher.publish(record)
        except AckNotReceived as exc:
            raise RunStartRefused(f"run-start was not acknowledged: {exc}") from exc
        self.started = True
        _remember_run(config, self)

    def close(
        self,
        config: ExecutorConfig,
        *,
        exit_code: int,
        crashed: BaseException | None = None,
        interrupted: bool = False,
        hint: str = "",
    ) -> int:
        """Write the closure (once) and return the exit code to use.

        The return value differs from ``exit_code`` in exactly one case: the
        publisher still owes records (an unacknowledged result) -- then the
        run ends ``failed`` with exit 2 and a reason naming the debt.
        """
        if not self.started or self.closed:
            return exit_code
        self.closed = True
        publisher = self.publisher
        assert publisher is not None
        facts = _work_facts(config, self.run_id)
        outcome = closure_mod.Outcome(
            exit_code=exit_code,
            crashed=crashed is not None,
            interrupted=interrupted,
            last_error_kind=facts.last_error_kind,
            unfinished_work=facts.unfinished or bool(self.open_call_ids),
        )
        kind = closure_mod.derive(outcome)
        reason = _reason(kind, crashed, facts, hint)
        if not publisher.drain(config.durability_ack_timeout_seconds):
            kind, exit_code = "failed", 2
            reason = f"{publisher.pending} record(s) were not acknowledged by the store"
        record = Closure(
            run_id=self.run_id,
            pipeline_id=self.pipeline_id,
            subcommand=self.subcommand,
            closure_kind=kind,
            reason=reason,
            exit_code=exit_code,
            last_checkpoint_id=None,
            last_call_ids=list(self.call_ids),
            attempt_ids=facts.attempt_ids,
            open_calls=len(self.open_call_ids),
            degraded=False,
            started_at=self.started_at,
            ended_at=datetime.now().isoformat(),
        )
        try:
            publisher.publish(record)
        except Exception as exc:  # noqa: BLE001 - reported, exit code not improved
            print(f"⚠️  run closure was not written: {exc}", file=sys.stderr)
        return exit_code


# --- the process-wide context ----------------------------------------------

_CURRENT: RunContext | None = None


def current() -> RunContext | None:
    """The context of this process, or None before ``cli.main`` made one."""
    return _CURRENT


def current_run_id() -> str | None:
    return _CURRENT.run_id if _CURRENT is not None else None


def install(ctx: RunContext | None) -> None:
    """Set (or clear, with None) the process-wide context. Tests use this."""
    global _CURRENT
    _CURRENT = ctx


def new_context(subcommand: str, pipeline_id: str | None = None) -> RunContext:
    """Mint a context: full UUIDv4, never truncated."""
    return RunContext(
        run_id=str(uuid4()),
        pipeline_id=pipeline_id,
        subcommand=subcommand,
        started_at=datetime.now().isoformat(),
    )


# --- facts the closure is derived from -------------------------------------


@dataclass(frozen=True)
class WorkFacts:
    """What the DB says this invocation did (design § 6.3, second fact)."""

    attempt_ids: list[str]
    unfinished: bool
    last_error_kind: str | None
    last_error: str | None


def _work_facts(config: ExecutorConfig, run_id: str) -> WorkFacts:
    """Attempts recorded under ``run_id`` and whether any task is not done."""
    empty = WorkFacts([], False, None, None)
    path = config.state_file
    if not path.exists():
        return empty
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return empty
    try:
        rows = conn.execute(
            "SELECT a.id, a.task_id, a.success, a.error_kind, a.error, t.status "
            "FROM attempts a LEFT JOIN tasks t ON t.task_id = a.task_id "
            "WHERE a.run_id = ? ORDER BY a.id",
            (run_id,),
        ).fetchall()
    except sqlite3.Error:
        return empty
    finally:
        conn.close()
    seen: dict[str, int] = {}
    ids: list[str] = []
    for _, task_id, *_rest in rows:
        seen[task_id] = seen.get(task_id, 0) + 1
        ids.append(f"{task_id}-{seen[task_id]}")
    unfinished = any(status != "success" for *_, status in rows)
    failed = [r for r in rows if not r[2]]
    last = failed[-1] if failed else None
    return WorkFacts(ids, unfinished, last[3] if last else None, last[4] if last else None)


def _reason(kind: str, crashed: BaseException | None, facts: WorkFacts, hint: str) -> str:
    if kind == "crashed" and crashed is not None:
        return f"{type(crashed).__name__}: {crashed}"
    if kind == "interrupted":
        return (
            "KeyboardInterrupt" if isinstance(crashed, KeyboardInterrupt) else "shutdown_requested"
        )
    if kind in ("refused", "failed"):
        if facts.last_error_kind:
            return f"{facts.last_error_kind}: {facts.last_error or ''}".strip()
        return hint
    return ""


# --- run-start material ----------------------------------------------------


def _facts(config: ExecutorConfig) -> dict[str, Any]:
    """Facts for the auditor: recorded, never compared (Q-07)."""
    from . import __version__

    return {
        "spec_runner_version": __version__,
        "claude_command": config.claude_command,
        "review_command": config.review_command or None,
        "claude_model": config.claude_model or None,
        "execution_mode": getattr(config, "execution_mode", None),
    }


def _git(config: ExecutorConfig, *args: str) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=config.project_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.strip()
    return out if done.returncode == 0 and out else None


def _repository(config: ExecutorConfig) -> dict[str, str | None]:
    """Repository identity without any absolute path (design § 4.2)."""
    roots = _git(config, "rev-list", "--max-parents=0", "HEAD")
    return {
        "remote": _git(config, "config", "--get", "remote.origin.url"),
        "root_commit": roots.splitlines()[0] if roots else None,
    }


def _remember_run(config: ExecutorConfig, ctx: RunContext) -> None:
    """``last_run_id`` / ``last_pipeline_id`` into the state meta -- and nothing
    else: the ``continuation_index`` marker has one writer, which is not this
    (design Q-12)."""
    from .state import ExecutorState

    if not config.state_file.exists() and ctx.subcommand not in ("run", "retry", "watch"):
        return
    try:
        with ExecutorState(config) as state:
            state.set_meta("last_run_id", ctx.run_id)
            if ctx.pipeline_id:
                state.set_meta("last_pipeline_id", ctx.pipeline_id)
    except Exception as exc:  # noqa: BLE001 - bookkeeping beside the run, not part of it
        print(f"⚠️  could not remember run id: {exc}", file=sys.stderr)
