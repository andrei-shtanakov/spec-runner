"""The one seam every paid agent call goes through (FR-02, design § 2).

``execute`` is the whole protocol in one place, in this order:

1. the call's identity is checked (``call_id`` is minted by the *site*, before
   its prompt artefact is written, and only verified here);
2. the ledger row of the call's family is written, ``open``;
3. ``CallStart`` is published and **acknowledged** -- no ack, no process;
4. ``_spawn`` -- the only function in the repository that hands a provider's
   argv to ``subprocess``;
5. the answer is read once (``parse_cli_result``/``classify_agent_answer``);
6. ``CallResult`` is published (queued for redelivery if the store is down:
   the money is already spent, so the record is owed, not optional);
7. the ledger row is closed.

The budget guard (#213) and the prompt artefact (#282) stay at the site,
*before* ``execute``: a refused call is not a call and leaves no row.

Tests replace ``_spawn``; the autouse guard in ``conftest`` is keyed on the
same name, so one patch point covers every site.
"""

from __future__ import annotations

import contextlib
import subprocess
import threading
from collections.abc import Callable, Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from . import run_context
from .evidence import call_result_for, call_start_for, policy_identity
from .phases import Refusal, RefusalKind
from .prompts_log import append_not_started
from .runner import (
    AgentAnswer,
    CliInvocation,
    CliResult,
    classify_agent_answer,
    parse_cli_result,
    terminal_markers,
)
from .sandbox import sandboxed

if TYPE_CHECKING:
    from .config import ExecutorConfig
    from .state import ExecutorState

#: Call-result vocabulary (design § 2.1).
OUTCOMES = ("success", "failed", "blocked", "timeout", "infrastructure_error")

#: A doctor-style probe renames the two provenances it can reach (§ 2.7).
#: Closed on purpose: a third value would be a call the cost gate never
#: announced.
PROBE_PROVENANCE_MAP: dict[str, str] = {"green": "{probe}:execute", "review": "{probe}:review"}

#: Serialises ledger writes made through short-lived states (the parallel
#: review pool opens one per role; see `review._LEDGER_LOCK`).
_LEDGER_LOCK = threading.Lock()


class CallRefused(Exception):
    """The seam refused to start the call: the instrument could not prove intent.

    Carries the typed ``Refusal`` (``kind=instrument``) so the site records
    ``INFRASTRUCTURE``/exit 2 (#230) instead of a generic failure.
    """

    def __init__(self, refusal: Refusal) -> None:
        super().__init__(str(refusal))
        self.refusal = refusal


def new_call_id() -> str:
    """A fresh full UUIDv4 -- minted by the site, verified by ``execute``."""
    return str(uuid4())


@dataclass(frozen=True)
class Ledger:
    """The ledger row of one call, in the family the call belongs to.

    ``open`` writes the row before the store ack; ``close`` finishes it with
    ``closed`` or, when the process never launched, ``not_started``.
    """

    open: Callable[[str, str | None, str], None]
    close: Callable[[str, str, CallOutcome | None], None]


@dataclass(frozen=True)
class PaidCall:
    """Everything the seam needs to run one call and describe it."""

    invocation: CliInvocation
    provenance: str
    prompt: str
    timeout_seconds: float
    call_id: str
    task_id: str | None = None
    attempt: int | None = None
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    prompt_log: Path | None = None
    ledger: Ledger | None = None


@dataclass(frozen=True)
class CallOutcome:
    """What the call produced, in the terms the sites already use."""

    call_id: str
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool
    parsed: CliResult | None
    outcome: str
    cost_usd: float | None


# --- scope: the site's context for seams whose signature cannot grow --------


@dataclass(frozen=True)
class CallScope:
    """Who is calling, for sites reached through a stubbed two-argument seam.

    `tdd._run_agent(config, prompt)` and `execution._run_agent_process(config,
    invocation)` are patched by dozens of tests with exactly those signatures,
    so the task, attempt, provenance and state travel beside the call.
    """

    task_id: str | None
    attempt: int | None
    provenance: str
    ledger_provenance: str | None = None
    state: ExecutorState | None = None
    #: GREEN's cost lives on the attempt row; its ledger row joins ids only.
    price_on_attempt: bool = False
    prompt_log: Path | None = None
    prompt: str = ""


_SCOPE: ContextVar[CallScope | None] = ContextVar("paid_call_scope", default=None)


@contextlib.contextmanager
def scope(
    *,
    task_id: str | None,
    provenance: str,
    attempt: int | None = None,
    ledger_provenance: str | None = None,
    state: ExecutorState | None = None,
    price_on_attempt: bool = False,
    prompt_log: Path | None = None,
    prompt: str = "",
) -> Iterator[CallScope]:
    """Declare the caller for the calls made inside the block."""
    value = CallScope(
        task_id,
        attempt,
        provenance,
        ledger_provenance,
        state,
        price_on_attempt,
        prompt_log,
        prompt,
    )
    token = _SCOPE.set(value)
    try:
        yield value
    finally:
        _SCOPE.reset(token)


def current_scope() -> CallScope | None:
    return _SCOPE.get()


# --- ledgers ----------------------------------------------------------------


def _with_state(config: ExecutorConfig, state: ExecutorState | None, write: Callable) -> None:
    """Run ``write(state)`` on the caller's state, or on a short-lived one."""
    if state is not None:
        write(state)
        return
    from .state import ExecutorState

    with _LEDGER_LOCK, ExecutorState(config) as own:
        write(own)


def task_ledger(
    config: ExecutorConfig,
    state: ExecutorState | None,
    task_id: str,
    provenance: str,
    *,
    price_on_attempt: bool = False,
) -> Ledger:
    """`agent_calls` -- the ledger of calls that belong to a task."""

    def open_row(call_id: str, run_id: str | None, started_at: str) -> None:
        _with_state(
            config,
            state,
            lambda s: s.open_agent_call(
                task_id, provenance, run_id=run_id, call_id=call_id, started_at=started_at
            ),
        )

    def close_row(call_id: str, status: str, outcome: CallOutcome | None) -> None:
        parsed = outcome.parsed if outcome is not None else None
        # A call priced on its attempt row (GREEN) keeps its tokens there too:
        # the totals sum attempts and ledger, so writing them here as well
        # would count them twice.
        usage = None if price_on_attempt else parsed
        _with_state(
            config,
            state,
            lambda s: s.close_call_row(
                "agent_calls",
                call_id,
                status=status,
                input_tokens=getattr(usage, "input_tokens", None),
                output_tokens=getattr(usage, "output_tokens", None),
                cost_usd=getattr(usage, "cost_usd", None),
            ),
        )

    return Ledger(open_row, close_row)


def plan_ledger(config: ExecutorConfig, provenance: str) -> Ledger:
    """`plan_agent_calls` -- the ledger of calls that belong to no task."""

    def open_row(call_id: str, run_id: str | None, started_at: str) -> None:
        _with_state(
            config,
            None,
            lambda s: s.open_plan_call(
                provenance, run_id=run_id, call_id=call_id, started_at=started_at
            ),
        )

    def close_row(call_id: str, status: str, outcome: CallOutcome | None) -> None:
        parsed = outcome.parsed if outcome is not None else None
        _with_state(
            config,
            None,
            lambda s: s.close_call_row(
                "plan_agent_calls",
                call_id,
                status=status,
                input_tokens=getattr(parsed, "input_tokens", None),
                output_tokens=getattr(parsed, "output_tokens", None),
                cost_usd=getattr(parsed, "cost_usd", None),
            ),
        )

    return Ledger(open_row, close_row)


# --- the seam ---------------------------------------------------------------


_IN_FLIGHT: ContextVar[str | None] = ContextVar("paid_call_in_flight", default=None)


def provenance_in_flight() -> str | None:
    """The provenance of the call being spawned, for a guard's message."""
    return _IN_FLIGHT.get()


def _spawn(
    invocation: CliInvocation,
    *,
    timeout: float,
    cwd: Path | str | None,
    env: Mapping[str, str] | None,
) -> subprocess.CompletedProcess[str]:
    """Start the provider's process. The only place argv reaches `subprocess`.

    Raises ``subprocess.TimeoutExpired`` and ``OSError`` as `subprocess.run`
    does; ``execute`` turns both into recorded results.
    """
    return subprocess.run(
        invocation.argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        env=dict(env) if env is not None else None,
    )


def _evidence_provenance(config: ExecutorConfig, provenance: str) -> str:
    """The provenance a record carries; a probe renames the two it can reach."""
    probe = getattr(config, "probe_provenance", None)
    if not probe:
        return provenance
    template = PROBE_PROVENANCE_MAP.get(provenance)
    if template is None:
        raise CallRefused(
            Refusal(
                f"provenance {provenance!r} is outside the probe map "
                f"({', '.join(sorted(PROBE_PROVENANCE_MAP))}); the call was not started",
                RefusalKind.INSTRUMENT,
            )
        )
    return template.format(probe=probe)


def _check_call_id(call_id: str) -> None:
    try:
        ok = UUID(call_id).version == 4 and str(UUID(call_id)) == call_id
    except (ValueError, AttributeError, TypeError):
        ok = False
    if not ok:
        raise CallRefused(
            Refusal(
                f"call_id {call_id!r} is not a UUIDv4 minted by the site; the call was not started",
                RefusalKind.INSTRUMENT,
            )
        )


def _outcome_of(parsed: CliResult | None, returncode: int, timed_out: bool) -> str:
    if timed_out or parsed is None:
        return "timeout"
    answer = classify_agent_answer(parsed, returncode)
    if answer is AgentAnswer.ANSWERED:
        if any(m.kind == "TASK_BLOCKED" for m in terminal_markers(parsed.text)):
            return "blocked"
        return "success"
    return "failed"


def _refuse(message: str) -> CallRefused:
    return CallRefused(Refusal(message, RefusalKind.INSTRUMENT))


def execute(config: ExecutorConfig, state: ExecutorState | None, call: PaidCall) -> CallOutcome:
    """Run one paid call under the call-start-before-spawn protocol (FR-02).

    Raises ``CallRefused`` (typed instrument refusal) when intent could not be
    made durable -- before any process exists -- and lets ``OSError`` through
    after recording that the process never launched.
    """
    _check_call_id(call.call_id)
    provenance = _evidence_provenance(config, call.provenance)
    ctx = run_context.current()
    publisher = ctx.publisher if ctx is not None and ctx.started else None
    started_at = datetime.now().isoformat()
    run_id = ctx.run_id if ctx is not None else None

    if publisher is not None and not publisher.drain(
        config.durability_ack_timeout_seconds,
        checkpoint_timeout=config.durability_checkpoint_ack_timeout_seconds,
    ):
        raise _refuse("records owed to the store are undelivered; the call was not started")

    ledger = call.ledger
    if ledger is not None:
        try:
            ledger.open(call.call_id, run_id, started_at)
        except Exception as exc:  # noqa: BLE001 - the index is not the truth (Q-12)
            # The row is the local *index* of the call; the acknowledged
            # call-start below is the durable intent. A broken ledger must not
            # decide whether a call may run -- the task ledger has always
            # followed that rule ("an accounting row must not fail a task").
            # TODO(spool, FR-08): when the DB refuses, write the open row to the
            # emergency spool, and refuse only if both refuse.
            from .logging import get_logger

            get_logger("paid_call").warning(
                "Ledger row not written; the call goes on",
                call_id=call.call_id,
                error=str(exc),
            )
            ledger = None

    # A probe publishes no task: its canonical task exists only in the scratch.
    published_task = None if getattr(config, "probe_provenance", None) else call.task_id
    if publisher is not None and ctx is not None:
        start = call_start_for(
            run_id=ctx.run_id,
            pipeline_id=ctx.pipeline_id,
            call_id=call.call_id,
            provenance=provenance,
            policy=policy_identity(config),
            task_id=published_task,
            attempt=call.attempt,
            prompt=call.prompt,
            timestamp=started_at,
        )
        try:
            publisher.publish(start)
        except Exception as exc:
            reason = f"call_start_not_acknowledged: {exc}"
            if ledger is not None:
                with contextlib.suppress(Exception):
                    ledger.close(call.call_id, "not_started", None)
            append_not_started(call.prompt_log, reason)
            raise _refuse(reason) from exc
        ctx.note_call_started(call.call_id)

    sandbox = sandboxed(config, call.invocation, dict(call.env) if call.env is not None else None)
    spawned = replace(call.invocation, argv=sandbox.argv)
    proc: subprocess.CompletedProcess[str] | None = None
    timed_out = False
    in_flight = _IN_FLIGHT.set(provenance)
    try:
        proc = _spawn(
            spawned,
            timeout=call.timeout_seconds,
            cwd=call.cwd or config.project_root,
            env=sandbox.env,
        )
    except subprocess.TimeoutExpired:
        timed_out = True
    except OSError as exc:
        _finish(
            config,
            ctx,
            publisher,
            call,
            provenance,
            outcome=None,
            result_text=f"did not launch: {exc}",
            result_outcome="infrastructure_error",
            status="not_started",
        )
        raise
    finally:
        _IN_FLIGHT.reset(in_flight)
        sandbox.cleanup()

    parsed: CliResult | None = None
    if proc is not None:
        parsed = parse_cli_result(
            call.invocation.result_format, proc.stdout, proc.stderr, proc.returncode
        )
    returncode = proc.returncode if proc is not None else -1
    outcome = CallOutcome(
        call_id=call.call_id,
        stdout=proc.stdout if proc is not None else "",
        stderr=proc.stderr if proc is not None else "",
        returncode=returncode,
        timed_out=timed_out,
        parsed=parsed,
        outcome=_outcome_of(parsed, returncode, timed_out),
        cost_usd=parsed.cost_usd if parsed is not None else None,
    )
    _finish(
        config,
        ctx,
        publisher,
        call,
        provenance,
        outcome=outcome,
        result_text=(parsed.text if parsed is not None else ""),
        result_outcome=outcome.outcome,
        status="closed",
    )
    return outcome


def _finish(
    config: ExecutorConfig,
    ctx: Any,
    publisher: Any,
    call: PaidCall,
    provenance: str,
    *,
    outcome: CallOutcome | None,
    result_text: str,
    result_outcome: str,
    status: str,
) -> None:
    """Steps 6-7: publish the result (queued if undeliverable), close the row."""
    if publisher is not None and ctx is not None:
        record = call_result_for(
            run_id=ctx.run_id,
            pipeline_id=ctx.pipeline_id,
            call_id=call.call_id,
            provenance=provenance,
            outcome=result_outcome,
            cost_usd=outcome.cost_usd if outcome is not None else None,
            returncode=outcome.returncode if outcome is not None else None,
            result=result_text,
            timestamp=datetime.now().isoformat(),
        )
        # AlreadyExists: the first record stands, and that is the right answer.
        with contextlib.suppress(Exception):
            publisher.publish_or_queue(record)
        ctx.note_call_finished(call.call_id)
    if call.ledger is not None:
        try:
            call.ledger.close(call.call_id, status, outcome)
        except Exception as exc:  # noqa: BLE001 - accounting must not fail a finished call
            from .logging import get_logger

            get_logger("paid_call").warning(
                "Could not close the ledger row", call_id=call.call_id, error=str(exc)
            )


__all__ = [
    "OUTCOMES",
    "PROBE_PROVENANCE_MAP",
    "CallOutcome",
    "CallRefused",
    "CallScope",
    "Ledger",
    "PaidCall",
    "current_scope",
    "provenance_in_flight",
    "execute",
    "new_call_id",
    "plan_ledger",
    "scope",
    "task_ledger",
]
