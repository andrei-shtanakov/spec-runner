"""`spec-runner evidence <run_id> [--json]` -- the read-surface of one run (FR-09).

Reads **only the store**, through `open_store_readonly`: no `project_root`, no
state DB, no Git, and never a `put`. `collect()` is the one reader for the text
a person sees and the payload `--json` prints (the pattern of `tdd_status.py`),
so the two cannot drift apart.

The surface names what it can prove and says "не доказуемо" for the rest. It
never decides for the operator: a run-start without a closure is always
`crash/unknown` (a live process cannot be told from a dead one by a lock PID),
and the next step is a recommendation, not an action. Design § 7.4.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .artifact_store import ArtifactStore, open_store_readonly

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import ExecutorConfig

#: Exit code when the store cannot answer: a reason, never an empty report.
EXIT_STORE_UNAVAILABLE = 2

STATUS_CRASH = "crash/unknown"
STATUS_LEGACY = "legacy"
NOT_PROVABLE_ALIVE = "не доказуемо: процесс мог быть жив"
COST_UNKNOWN = "unknown"
#: The recommendation names no command this version cannot run: `restore`
#: (experimental, behind `--experimental`) is DT-06 and not shipped yet.
RESTORE_NOT_SHIPPED = (
    "восстановление этого прогона — подкомандой restore (экспериментальной), "
    "которой в этой версии ещё нет"
)


class EvidenceError(Exception):
    """The store did not answer, or answered with something unreadable."""


@dataclass
class OpenCall:
    call_id: str
    provenance: str | None
    task_id: str | None
    timestamp: str | None


@dataclass
class AttemptView:
    task_id: str
    attempt: int
    outcome: str
    cost_usd: float | str  # a number, or "unknown" when the record carries none
    calls: list[str] = field(default_factory=list)
    key: str = ""


@dataclass
class CheckpointView:
    checkpoint_id: str
    sequence: int
    namespace: str | None
    #: The manifest carries no clock (its schema is closed), so this is None.
    timestamp: str | None = None


@dataclass
class NextStep:
    recommendation: str
    #: "не доказуемо: <причина>" where the data to decide is missing.
    unprovable: str | None = None


@dataclass
class EvidenceView:
    """One run, as the store knows it. `status` is the headline."""

    run_id: str
    status: str
    status_note: str | None = None
    missing: list[str] = field(default_factory=list)
    run_start: dict[str, Any] | None = None
    closure: dict[str, Any] | None = None
    last_checkpoint: CheckpointView | None = None
    open_calls: list[OpenCall] = field(default_factory=list)
    attempts: list[AttemptView] = field(default_factory=list)
    total_cost_usd: float = 0.0
    unpriced_calls: int = 0
    deletions: list[str] = field(default_factory=list)
    #: Only when the adapter declares it (`StoreCapabilities.storage_cost`).
    storage_cost: float | None = None
    next_step: NextStep | None = None


# --- reading ----------------------------------------------------------------


def _get_json(store: ArtifactStore, key: str) -> dict[str, Any] | None:
    try:
        raw = store.get(key)
    except Exception as exc:  # noqa: BLE001 - any adapter failure is "unavailable"
        raise EvidenceError(f"store cannot read {key}: {exc}") from exc
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise EvidenceError(f"{key} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise EvidenceError(f"{key} is not a JSON object")
    return data


def _list(store: ArtifactStore, prefix: str) -> list[str]:
    try:
        return store.list(prefix)
    except Exception as exc:  # noqa: BLE001
        raise EvidenceError(f"store cannot list {prefix}: {exc}") from exc


def legacy_missing(start: dict[str, Any] | None) -> list[str]:
    """What a run lacks to carry the evidence contract; empty when it has it.

    The one classification `restore` reuses to refuse a legacy run.
    """
    if start is None:
        return ["run-start.json"]
    missing = []
    version = start.get("contract_version")
    if not isinstance(version, int) or version < 1:
        missing.append("contract_version >= 1")
    if start.get("ack_channel") == "local":
        missing.append("ack_channel: store (acknowledged by the store, not a local volume)")
    return missing


def _last_checkpoint(store: ArtifactStore, run_id: str) -> CheckpointView | None:
    prefix = f"runs/{run_id}/checkpoints/"
    # The manifest is put last: its presence is the checkpoint's ack.
    manifests = [k for k in _list(store, prefix) if k.endswith("/manifest.json")]
    if not manifests:
        return None
    key = manifests[-1]  # keys are sorted and the sequence is zero-padded
    manifest = _get_json(store, key) or {}
    identity = manifest.get("identity") or {}
    return CheckpointView(
        checkpoint_id=str(manifest.get("checkpoint_id", "")),
        sequence=int(manifest.get("sequence", 0)),
        namespace=identity.get("namespace"),
        timestamp=manifest.get("timestamp"),
    )


@dataclass
class _Call:
    call_id: str
    start: dict[str, Any]
    result: dict[str, Any] | None


def _calls(store: ArtifactStore, run_id: str) -> list[_Call]:
    prefix = f"runs/{run_id}/calls/"
    keys = _list(store, prefix)
    ids = sorted({k[len(prefix) :].split("/", 1)[0] for k in keys})
    calls = []
    for call_id in ids:
        start = _get_json(store, f"{prefix}{call_id}/start.json")
        if start is None:
            continue  # a result without its start is not a call this surface can name
        calls.append(_Call(call_id, start, _get_json(store, f"{prefix}{call_id}/result.json")))
    return calls


def _attempt_row(store: ArtifactStore, key: str) -> dict[str, Any] | None:
    try:
        raw = store.get(key)
    except Exception as exc:  # noqa: BLE001
        raise EvidenceError(f"store cannot read {key}: {exc}") from exc
    for line in (raw or b"").decode("utf-8", "replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("table") == "attempts":
            row = record.get("row")
            return row if isinstance(row, dict) else None
    return None


def _attempts(store: ArtifactStore, run_id: str, calls: list[_Call]) -> list[AttemptView]:
    prefix = f"runs/{run_id}/attempts/"
    views = []
    for key in _list(store, prefix):
        stem = key[len(prefix) :].removesuffix(".jsonl")
        task_id, _, number = stem.rpartition("-")
        if not task_id or not number.isdigit():
            continue
        row = _attempt_row(store, key) or {}
        cost = row.get("cost_usd")
        outcome = "success" if row.get("success") else (row.get("error_kind") or "failed")
        linked = [
            c.call_id
            for c in calls
            if c.start.get("task_id") == task_id and c.start.get("attempt") == int(number)
        ]
        views.append(
            AttemptView(
                task_id=task_id,
                attempt=int(number),
                outcome=outcome,
                cost_usd=COST_UNKNOWN if cost is None else cost,
                calls=linked,
                key=key,
            )
        )
    return views


def _storage_cost(store: ArtifactStore) -> float | None:
    value = store.capabilities().storage_cost
    return None if value is None else float(value)


def _next_step(view: EvidenceView) -> NextStep:
    if view.status == STATUS_LEGACY:
        return NextStep(
            "restore невозможен: у прогона нет evidence-контракта",
            "не доказуемо: " + ", ".join(view.missing),
        )
    restore = RESTORE_NOT_SHIPPED
    if view.open_calls:
        ids = ", ".join(c.call_id for c in view.open_calls)
        return NextStep(
            f"{restore}; открыты вызовы ({ids}) — решение по ним за оператором",
            "не доказуемо: исход вызова без call-result неизвестен",
        )
    if view.status == STATUS_CRASH:
        return NextStep(restore, NOT_PROVABLE_ALIVE)
    return NextStep(restore)


def collect(store: ArtifactStore, run_id: str) -> EvidenceView:
    """Everything the store knows about `run_id`, as plain data.

    Raises `EvidenceError` when the store cannot answer. Reads only: the store
    handed in is the read-only door, and nothing here calls `put`/`delete`.
    """
    start = _get_json(store, f"runs/{run_id}/run-start.json")
    missing = legacy_missing(start)
    if missing:
        view = EvidenceView(run_id=run_id, status=STATUS_LEGACY, run_start=start, missing=missing)
        view.status_note = "нет evidence-контракта"
        view.next_step = _next_step(view)
        return view

    closure = _get_json(store, f"runs/{run_id}/closure.json")
    calls = _calls(store, run_id)
    open_calls = [
        OpenCall(
            c.call_id, c.start.get("provenance"), c.start.get("task_id"), c.start.get("timestamp")
        )
        for c in calls
        if c.result is None
    ]
    costs = [c.result.get("cost_usd") if c.result else None for c in calls]
    view = EvidenceView(
        run_id=run_id,
        status=f"closed:{closure.get('closure_kind')}" if closure else STATUS_CRASH,
        status_note=None if closure else NOT_PROVABLE_ALIVE,
        run_start=start,
        closure=closure,
        last_checkpoint=_last_checkpoint(store, run_id),
        open_calls=open_calls,
        attempts=_attempts(store, run_id, calls),
        total_cost_usd=sum(c for c in costs if isinstance(c, (int, float))),
        unpriced_calls=sum(1 for c in costs if not isinstance(c, (int, float))),
        deletions=_list(store, f"runs/{run_id}/deletions/"),
        storage_cost=_storage_cost(store),
    )
    view.next_step = _next_step(view)
    return view


def as_dict(view: EvidenceView) -> dict[str, Any]:
    """The `--json` payload: the view, without the fields that do not apply."""
    data = asdict(view)
    if data["storage_cost"] is None:
        del data["storage_cost"]
    return data


# --- output -----------------------------------------------------------------


def _money(value: float | str) -> str:
    return value if isinstance(value, str) else f"${value:.4f}"


def render(view: EvidenceView) -> str:
    """The text a person reads, built from the same `EvidenceView`."""
    lines = [f"Run {view.run_id}: {view.status}"]
    if view.status_note:
        lines.append(f"  ({view.status_note})")
    if view.missing:
        lines.append("  отсутствует: " + "; ".join(view.missing))
    start = view.run_start or {}
    if start and not view.missing:
        policy = start.get("policy") or {}
        repo = start.get("repository") or {}
        lines.append(f"  subcommand: {start.get('subcommand')}  started: {start.get('started_at')}")
        lines.append(
            f"  policy: {policy.get('config_hash')} (namespace {policy.get('namespace')})  "
            f"repository: {repo.get('remote')}"
        )
    if view.closure:
        lines.append(
            f"  closure: {view.closure.get('closure_kind')} exit={view.closure.get('exit_code')} "
            f"reason={view.closure.get('reason') or '-'}"
        )
    if view.status != STATUS_LEGACY:
        cp = view.last_checkpoint
        lines.append(
            f"  last checkpoint: {cp.checkpoint_id} seq={cp.sequence} ns={cp.namespace} "
            f"time={cp.timestamp or 'unknown'}"
            if cp
            else "  last checkpoint: none"
        )
        lines.append(f"  open calls: {len(view.open_calls)}")
        for call in view.open_calls:
            lines.append(
                f"    {call.call_id} {call.provenance} task={call.task_id} at {call.timestamp}"
            )
        lines.append(f"  attempts: {len(view.attempts)}")
        for a in view.attempts:
            lines.append(
                f"    {a.task_id}#{a.attempt} {a.outcome} {_money(a.cost_usd)} "
                f"calls={','.join(a.calls) or '-'}"
            )
        lines.append(
            f"  total cost: {_money(view.total_cost_usd)}"
            + (f" ({view.unpriced_calls} call(s) unpriced)" if view.unpriced_calls else "")
        )
        if view.storage_cost is not None:
            lines.append(f"  storage cost: {_money(view.storage_cost)}")
        for key in view.deletions:
            lines.append(f"  deletion: {key}")
    if view.next_step:
        lines.append(f"  next: {view.next_step.recommendation}")
        if view.next_step.unprovable:
            lines.append(f"    ({view.next_step.unprovable})")
    return "\n".join(lines)


def open_configured_store(config: ExecutorConfig) -> ArtifactStore:
    """The read-only door onto the store the config declares.

    No adapter (or `durability.ack: local`) means the local volume under the
    logs directory, the same place the publisher writes by default.
    """
    adapter = config.durability_store_adapter
    if adapter and config.durability_ack != "local":
        return open_store_readonly(
            adapter,
            dict(config.durability_store_options),
            encryption_at_rest=config.durability_store_encryption_at_rest,
        )
    return open_store_readonly(
        "local_volume", {"root": str(Path(config.logs_dir) / "evidence-local")}
    )


def cmd_evidence(args: Any, config: ExecutorConfig) -> int:
    """`evidence <run_id> [--json]`; exit 2 when the store cannot answer."""
    try:
        view = collect(open_configured_store(config), args.run_id)
    except (EvidenceError, ValueError) as exc:
        print(f"⛔ evidence {args.run_id}: store unavailable: {exc}")
        return EXIT_STORE_UNAVAILABLE
    if getattr(args, "json", False):
        print(json.dumps(as_dict(view), indent=2, default=str))
    else:
        print(render(view))
    return 0


__all__ = [
    "EvidenceError",
    "EvidenceView",
    "as_dict",
    "cmd_evidence",
    "collect",
    "legacy_missing",
    "render",
]
