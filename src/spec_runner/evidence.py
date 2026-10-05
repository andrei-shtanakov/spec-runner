"""Evidence records and the one writer that publishes them (design § 1.4, § 2.2).

``Publisher`` is the only owner of an ``ArtifactStore`` instance and the only
importer of the store opener. Every text field of every record goes through
``redaction.redact`` *before* it is serialised, so the bytes that leave the
machine never held a recognised secret, and each key is written once
(``AlreadyExists`` is a refusal, never an overwrite).

Records: ``RunStart``, ``CallStart``, ``CallResult``, ``Closure``. Bounded
copies of a prompt or result carry the digest and size of the *full* text
(``bound_evidence``), so a truncated record can still be matched against a
full artefact reproduced later.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sys
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple

from .artifact_store import (
    Ack,
    AlreadyExists,
    ArtifactStore,
    _open_store,
    call_result_key,
    call_start_key,
    checkpoint_key,
    closure_key,
    run_start_key,
)
from .redaction import redact

if TYPE_CHECKING:
    from .config import ExecutorConfig

#: Version of the evidence contract. Restore/evidence refuse a run whose
#: run-start carries a lower number (legacy: no evidence contract).
CONTRACT_VERSION = 1

#: NFR-06 bounds on what one record carries inline.
PROMPT_LIMIT = 1024 * 1024
RESULT_LIMIT = 4 * 1024 * 1024

#: Record fields that are identities or digests: never redacted, because a
#: redaction there would break the join keys the records exist to provide.
_VERBATIM_SUFFIXES = ("_id", "_ids", "_sha256", "_at", "sha256")


class AckNotReceived(Exception):
    """The store did not acknowledge a write in time, or refused it.

    The caller's reason is in ``str(self)``. Not an ``AlreadyExists``: a
    duplicate key is the store *answering*, and callers treat it differently.
    """


# --- bounded evidence ------------------------------------------------------


@dataclass(frozen=True)
class BoundedText:
    """A text cut to a limit, with proof of what the whole text was."""

    text: str
    truncated: bool
    full_sha256: str
    full_size: int


def bound_evidence(text: str, limit: int, *, original: str | None = None) -> BoundedText:
    """``text`` unchanged if it fits ``limit`` bytes, else its head and tail.

    Pattern of ``prompts_log.bound``: head *and* tail survive (the frozen-files
    block is appended last, #214), a marker says that a cut happened, and the
    SHA-256 and size describe the *whole* text. ``limit`` is in UTF-8 bytes and
    the result, marker included, never exceeds it.

    ``original``: the text the digest and size must describe when ``text`` is
    a redacted copy of it — BEH-27: they prove the *unredacted* content, so an
    auditor can join a published record to the local artefact.
    """
    raw = text.encode("utf-8", "surrogatepass")
    whole = raw if original is None else original.encode("utf-8", "surrogatepass")
    digest = hashlib.sha256(whole).hexdigest()
    if len(raw) <= limit:
        return BoundedText(text, False, digest, len(whole))
    marker = (
        f"\n\n=== TRUNCATED: {len(whole)} bytes in total; sha256 of the full text {digest} ===\n\n"
    )
    keep = max((limit - len(marker.encode("utf-8"))) // 2, 0)
    head = raw[:keep].decode("utf-8", "ignore")
    tail = raw[len(raw) - keep :].decode("utf-8", "ignore") if keep else ""
    return BoundedText(f"{head}{marker}{tail}", True, digest, len(whole))


# --- policy identity -------------------------------------------------------


@dataclass(frozen=True)
class PolicyIdentity:
    """What makes two runs comparable (design § 3.4, Q-07).

    ``config_hash`` is over ``gates.POLICY_KEYS`` only, so an unrelated config
    edit does not change the identity.
    """

    contract_version: int
    config_hash: str
    namespace: str
    namespace_source: str
    spec_prefix: str
    change_id: str


def policy_identity(config: ExecutorConfig) -> PolicyIdentity:
    """The identity of ``config`` as run-start and call-start record it."""
    from .gates import POLICY_KEYS
    from .tdd import resolve_namespace

    parts = [f"{key}={getattr(config, key, None)!r}" for key in sorted(POLICY_KEYS)]
    declared = (getattr(config, "tdd_namespace", "") or "").strip()
    return PolicyIdentity(
        contract_version=CONTRACT_VERSION,
        config_hash=hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16],
        namespace=resolve_namespace(config),
        namespace_source="declared" if declared else "derived",
        spec_prefix=getattr(config, "spec_prefix", "") or "",
        change_id=getattr(config, "change_id", "") or "",
    )


# --- records ---------------------------------------------------------------


@dataclass(frozen=True)
class RunStart:
    """Written before a paying subcommand does any work."""

    run_id: str
    pipeline_id: str | None
    subcommand: str
    started_at: str
    policy: PolicyIdentity
    facts: dict[str, Any]
    repository: dict[str, str | None]
    ack_channel: str
    contract_version: int = CONTRACT_VERSION
    kind: str = "run-start"

    @property
    def key(self) -> str:
        return run_start_key(self.run_id)


@dataclass(frozen=True)
class CallStart:
    """Durable intent: written and acknowledged *before* the process starts."""

    run_id: str
    pipeline_id: str | None
    call_id: str
    provenance: str
    policy: PolicyIdentity
    task_id: str | None
    attempt: int | None
    prompt_sha256: str
    prompt: str
    prompt_truncated: bool
    prompt_full_sha256: str
    prompt_full_size: int
    timestamp: str
    kind: str = "call-start"

    @property
    def key(self) -> str:
        return call_start_key(self.run_id, self.call_id)


@dataclass(frozen=True)
class CallResult:
    """What a started call produced. At most one per ``call_id``."""

    run_id: str
    pipeline_id: str | None
    call_id: str
    provenance: str
    outcome: str
    cost_usd: float | None
    returncode: int | None
    result: str
    result_truncated: bool
    result_full_sha256: str
    result_full_size: int
    timestamp: str
    supersedes: str | None = None
    kind: str = "call-result"

    @property
    def key(self) -> str:
        return call_result_key(self.run_id, self.call_id)


@dataclass(frozen=True)
class Closure:
    """The last record of a run: how it ended (design § 6.3)."""

    run_id: str
    pipeline_id: str | None
    subcommand: str
    closure_kind: str
    reason: str
    exit_code: int
    last_checkpoint_id: str | None
    last_call_ids: list[str]
    attempt_ids: list[str]
    open_calls: int
    degraded: bool
    started_at: str
    ended_at: str
    kind: str = "closure"

    @property
    def key(self) -> str:
        return closure_key(self.run_id)


Record = RunStart | CallStart | CallResult | Closure


def call_start_for(
    *,
    run_id: str,
    pipeline_id: str | None,
    call_id: str,
    provenance: str,
    policy: PolicyIdentity,
    task_id: str | None,
    attempt: int | None,
    prompt: str,
    timestamp: str,
) -> CallStart:
    """Assemble a ``CallStart``: a bounded *redacted* prompt, its digest, and
    the full digest and size of the *original* (design: taken before
    redaction, BEH-26/27)."""
    redacted = redact(prompt)
    bounded = bound_evidence(redacted, PROMPT_LIMIT, original=prompt)
    return CallStart(
        run_id=run_id,
        pipeline_id=pipeline_id,
        call_id=call_id,
        provenance=provenance,
        policy=policy,
        task_id=task_id,
        attempt=attempt,
        prompt_sha256=hashlib.sha256(redacted.encode("utf-8", "surrogatepass")).hexdigest(),
        prompt=bounded.text,
        prompt_truncated=bounded.truncated,
        prompt_full_sha256=bounded.full_sha256,
        prompt_full_size=bounded.full_size,
        timestamp=timestamp,
    )


def call_result_for(
    *,
    run_id: str,
    pipeline_id: str | None,
    call_id: str,
    provenance: str,
    outcome: str,
    cost_usd: float | None,
    returncode: int | None,
    result: str,
    timestamp: str,
    supersedes: str | None = None,
) -> CallResult:
    """Assemble a ``CallResult`` with a bounded, redacted copy of ``result``."""
    bounded = bound_evidence(redact(result), RESULT_LIMIT, original=result)
    return CallResult(
        run_id=run_id,
        pipeline_id=pipeline_id,
        call_id=call_id,
        provenance=provenance,
        outcome=outcome,
        cost_usd=cost_usd,
        returncode=returncode,
        result=bounded.text,
        result_truncated=bounded.truncated,
        result_full_sha256=bounded.full_sha256,
        result_full_size=bounded.full_size,
        timestamp=timestamp,
        supersedes=supersedes,
    )


# --- publisher -------------------------------------------------------------


def _redact_value(name: str, value: Any) -> Any:
    if isinstance(value, str):
        return value if name.endswith(_VERBATIM_SUFFIXES) else redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(name, v) for v in value]
    return value


def serialise(record: Record) -> bytes:
    """Canonical JSON of ``record`` with every text field redacted."""
    body = {k: _redact_value(k, v) for k, v in asdict(record).items()}
    return json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")


#: The file of a checkpoint that is put last: its ack is the checkpoint's ack.
MANIFEST_FILE = "manifest.json"
#: Local-only marker in a checkpoint directory: no process owes it to the store
#: any more (acknowledged, or never queued). Another process's rotation may
#: delete only a directory that carries it -- its queue is invisible (#663).
RELEASED_MARKER = ".released"


class AckedCheckpoint(NamedTuple):
    """The newest checkpoint whose manifest the store acknowledged."""

    sequence: int
    checkpoint_id: str


@dataclass(frozen=True)
class PendingCheckpoint:
    """A checkpoint written locally and owed to the store (design Q-05).

    ``files`` are names inside ``directory``; ``MANIFEST_FILE`` is delivered
    last whatever the order given, so a reader never sees a manifest whose
    files are missing.
    """

    run_id: str
    sequence: int
    checkpoint_id: str
    directory: Path
    files: tuple[str, ...]


class CheckpointLost(Exception):
    """A queued checkpoint's local file is gone: no retry can deliver it."""


def release_directory(directory: Path) -> None:
    """Mark ``directory`` as owed by nobody; best effort (a stale copy stays)."""
    with contextlib.suppress(OSError):
        (directory / RELEASED_MARKER).touch()


@dataclass
class Publisher:
    """The single writer into an ``ArtifactStore``.

    ``publish`` is synchronous and bounded by ``ack_timeout``: an ack that does
    not arrive is a refusal (``AckNotReceived``), never a wait without limit.
    A result whose delivery failed after the process had already run is queued
    (``publish_or_queue``) and ``drain`` waits for it before the next spend and
    before closure.
    """

    store: ArtifactStore
    ack_timeout: float = 3.0
    channel: str = "store"
    _queue: deque[Record] = field(default_factory=deque)
    _drain_lock: threading.Lock = field(default_factory=threading.Lock)
    _checkpoints: list[PendingCheckpoint] = field(default_factory=list)
    _acked: AckedCheckpoint | None = None
    #: Checkpoints dropped because their local files vanished, in order.
    lost: list[PendingCheckpoint] = field(default_factory=list)

    @classmethod
    def from_config(cls, config: ExecutorConfig) -> Publisher:
        """The publisher the config asks for.

        No adapter configured is the default (continuation is experimental):
        the ack channel is then ``local`` -- an fsynced volume under the logs
        directory -- and run-start/closure say so, because a local ack does not
        survive the loss of the machine and restore must not pretend it does.
        """
        adapter = config.durability_store_adapter
        if adapter and config.durability_ack != "local":
            store = _open_store(
                adapter,
                dict(config.durability_store_options),
                encryption_at_rest=config.durability_store_encryption_at_rest,
            )
            channel = "store"
        else:
            store = _open_store("local_volume", {"root": str(_local_root(config))})
            channel = "local"
        return cls(store, ack_timeout=config.durability_ack_timeout_seconds, channel=channel)

    def publish(self, record: Record, *, timeout: float | None = None) -> Ack:
        """Write ``record`` once; return the store's ack or raise.

        Raises ``AlreadyExists`` when the key is taken (first write unchanged)
        and ``AckNotReceived`` on a store error or an ack past the timeout.
        """
        payload = serialise(record)
        metadata = {
            "run_id": record.run_id,
            "kind": record.kind,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        return self._put(record.key, payload, metadata, timeout=timeout)

    def _put(
        self, key: str, payload: bytes, metadata: dict[str, str], *, timeout: float | None = None
    ) -> Ack:
        wait = self.ack_timeout if timeout is None else timeout
        outcome: dict[str, Any] = {}

        def deliver() -> None:
            try:
                outcome["ack"] = self.store.put(key, payload, metadata=metadata)
            except BaseException as exc:  # noqa: BLE001 - handed to the caller below
                outcome["error"] = exc

        worker = threading.Thread(target=deliver, name="spec-runner-publish", daemon=True)
        worker.start()
        worker.join(wait)
        if worker.is_alive():
            raise AckNotReceived(f"no acknowledgement for {key} within {wait}s")
        error = outcome.get("error")
        if isinstance(error, AlreadyExists):
            raise error
        if error is not None:
            raise AckNotReceived(f"store refused {key}: {error}") from error
        ack: Ack = outcome["ack"]
        return ack

    def publish_or_queue(self, record: Record) -> bool:
        """``publish``, but a failure queues the record for ``drain``.

        For the result of a call that has already run: the spend happened, so
        the record is owed to the store whether or not it is reachable now.
        ``AlreadyExists`` is not queued -- the first record stands.
        """
        try:
            self.publish(record)
        except AlreadyExists:
            raise
        except AckNotReceived:
            self._queue.append(record)
            return False
        return True

    def enqueue_checkpoint(self, checkpoint: PendingCheckpoint) -> None:
        """Owe ``checkpoint`` to the store; delivery happens in ``drain``.

        Never waits: the seam after a mutation must not block (Q-05). The
        queue is ordered by ``sequence``.
        """
        with self._drain_lock:
            self._checkpoints.append(checkpoint)
            self._checkpoints.sort(key=lambda c: (c.run_id, c.sequence))

    def last_acknowledged(self) -> AckedCheckpoint | None:
        """The newest checkpoint whose manifest was acknowledged, if any."""
        return self._acked

    def pending_directories(self) -> set[Path]:
        """Local checkpoint directories still owed to the store."""
        return {c.directory for c in self._checkpoints}

    def unrecovered_losses(self) -> list[PendingCheckpoint]:
        """Lost checkpoints no later acknowledged checkpoint supersedes.

        A snapshot is the whole DB, so an acknowledged successor carries
        everything the lost one did; until then the newest state is not in
        the store, which the Q-05 sites treat like any undelivered checkpoint.
        """
        acked = self._acked.sequence if self._acked is not None else 0
        return [c for c in self.lost if c.sequence > acked]

    def _deliver_checkpoint(self, checkpoint: PendingCheckpoint, timeout: float) -> None:
        names = [n for n in checkpoint.files if n != MANIFEST_FILE] + [MANIFEST_FILE]
        for name in names:
            try:
                data = (checkpoint.directory / name).read_bytes()
            except (FileNotFoundError, NotADirectoryError) as exc:
                raise CheckpointLost(f"checkpoint file {name} is gone: {exc}") from exc
            except OSError as exc:
                raise AckNotReceived(f"checkpoint file {name} is unreadable: {exc}") from exc
            key = checkpoint_key(
                checkpoint.run_id, checkpoint.sequence, checkpoint.checkpoint_id, name
            )
            metadata = {
                "run_id": checkpoint.run_id,
                "kind": "checkpoint",
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            try:
                self._put(key, data, metadata, timeout=timeout)
            except AlreadyExists:
                continue

    def _drain_checkpoints(self, timeout: float) -> None:
        while self._checkpoints:
            head = self._checkpoints[0]
            try:
                self._deliver_checkpoint(head, timeout)
            except AckNotReceived:
                return
            except CheckpointLost as exc:
                # Terminal, unlike an outage: drop it so it cannot head the
                # queue forever, and say so -- the debt stays visible through
                # `unrecovered_losses` until a successor is acknowledged.
                self._checkpoints.pop(0)
                self.lost.append(head)
                print(
                    f"⚠️  checkpoint {head.sequence:06d}-{head.checkpoint_id} of run "
                    f"{head.run_id} cannot be delivered and was dropped: {exc}",
                    file=sys.stderr,
                )
                continue
            self._checkpoints.pop(0)
            self._acked = AckedCheckpoint(head.sequence, head.checkpoint_id)
            release_directory(head.directory)

    def _drain_records(self, timeout: float) -> None:
        for _ in range(len(self._queue)):
            try:
                record = self._queue.popleft()
            except IndexError:
                break
            try:
                self.publish(record, timeout=timeout)
            except AlreadyExists:
                continue
            except AckNotReceived:
                self._queue.append(record)
                break

    def drain(self, timeout: float, *, checkpoint_timeout: float | None = None) -> bool:
        """Deliver every queued record and checkpoint; ``True`` when none is owed.

        ``timeout`` bounds each acknowledgement it waits for
        (``checkpoint_timeout`` the checkpoint files', by default the same).
        Serialised: the parallel review pool drains from several threads, and
        two drains popping one queue raced (review of #653).
        """
        with self._drain_lock:
            self._drain_records(timeout)
            self._drain_checkpoints(timeout if checkpoint_timeout is None else checkpoint_timeout)
            return not self._queue and not self._checkpoints and not self.unrecovered_losses()

    @property
    def pending(self) -> int:
        """Records and checkpoints still owed to the store, lost ones included."""
        return len(self._queue) + len(self._checkpoints) + len(self.unrecovered_losses())


def _local_root(config: ExecutorConfig) -> Path:
    return Path(config.logs_dir) / "evidence-local"
