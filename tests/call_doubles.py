"""Doubles for observing the paid-call protocol without a real agent (#480).

One shared journal. A store double writes ``call_start(call_id)`` into it at
the moment it acknowledges a call-start record, and a ``_spawn`` double writes
``spawn(argv0)`` into the same journal -- so "the call-start was acknowledged
before the process started" is a statement about the order of two entries, not
about the implementation of either.

The doubles replace ``paid_call._spawn`` and the publisher's store; the guard
that refuses a real agent is replaced by the former (a documented property of
the guard) and the belt underneath is left in place.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

from spec_runner import paid_call, run_context
from spec_runner.artifact_store import Ack, AlreadyExists, StoreCapabilities
from spec_runner.evidence import Publisher


@dataclass
class Journal:
    """Ordered events shared by every double in one test."""

    events: list[tuple[str, str]] = field(default_factory=list)

    def add(self, kind: str, detail: str = "") -> None:
        self.events.append((kind, detail))

    def of(self, kind: str) -> list[str]:
        return [detail for k, detail in self.events if k == kind]

    def call_ids_in_order(self) -> list[str]:
        return self.of("call_start")


class RecordingStore:
    """An ``ArtifactStore`` that journals call-start acknowledgements.

    ``refuse_starts`` makes it refuse (or stall past the publisher's timeout)
    every call-start, the way an unreachable store would.
    """

    def __init__(self, journal: Journal, *, refuse_starts: bool = False, stall: float = 0.0):
        self.journal = journal
        self.objects: dict[str, bytes] = {}
        self.refuse_starts = refuse_starts
        self.stall = stall

    def capabilities(self) -> StoreCapabilities:
        return StoreCapabilities(
            tls=None, encryption_at_rest=True, immutable_put=True, lifecycle=""
        )

    def put(self, key: str, data: bytes, *, metadata: dict[str, str]) -> Ack:
        is_start = key.endswith("/start.json")
        if is_start and self.refuse_starts:
            if self.stall:
                import time

                time.sleep(self.stall)
            raise OSError("store unreachable")
        if key in self.objects:
            raise AlreadyExists(key)
        self.objects[key] = data
        if is_start:
            self.journal.add("call_start", json.loads(data)["call_id"])
        elif key.endswith("/result.json"):
            self.journal.add("call_result", json.loads(data)["call_id"])
        return Ack(key=key, size=len(data))

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def list(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def records(self, kind: str) -> list[dict[str, Any]]:
        out = []
        for key in self.list(""):
            if "/checkpoints/" in key:  # checkpoint files are bytes, not records
                continue
            record = json.loads(self.objects[key])
            if record.get("kind") == kind:
                out.append(record)
        return out


def make_run(
    journal: Journal, store: RecordingStore | None = None, *, ack_timeout: float = 1.0
) -> run_context.RunContext:
    """Install a started ``RunContext`` publishing into ``store``."""
    store = store or RecordingStore(journal)
    ctx = run_context.new_context("run", pipeline_id=None)
    ctx.paying = True
    ctx.publisher = Publisher(store, ack_timeout=ack_timeout)
    ctx.started = True
    run_context.install(ctx)
    ctx.test_store = store  # type: ignore[attr-defined]
    return ctx


def completed(argv: list[str], stdout: str = "ok\n", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)


@contextmanager
def spawn_double(
    journal: Journal, answer: Callable[[list[str]], subprocess.CompletedProcess] | str = "ok\n"
):
    """Replace ``paid_call._spawn``; every spawn is journaled with its provenance."""
    seen_argv: list[list[str]] = []

    def _spawn(invocation, *, timeout, cwd, env):
        argv = list(invocation.argv)
        seen_argv.append(argv)
        journal.add("spawn", paid_call.provenance_in_flight() or "")
        if callable(answer):
            return answer(argv)
        return completed(argv, stdout=answer)

    with patch.object(paid_call, "_spawn", _spawn):
        yield seen_argv


def adjacent_pairs(journal: Journal) -> list[tuple[str, str]]:
    """``(call_id, provenance)`` for every spawn, requiring that the event just
    before it is a ``call_start`` -- raises AssertionError otherwise."""
    pairs = []
    for i, (kind, detail) in enumerate(journal.events):
        if kind != "spawn":
            continue
        assert i > 0 and journal.events[i - 1][0] == "call_start", (
            f"a spawn at position {i} was not immediately preceded by an acknowledged "
            f"call-start: {journal.events}"
        )
        pairs.append((journal.events[i - 1][1], detail))
    return pairs


def project(tmp_path: Path, **overrides):
    """A real ``ExecutorConfig`` rooted at ``tmp_path`` with the real agent name."""
    from spec_runner.config import ExecutorConfig

    root = tmp_path
    (root / "spec").mkdir(parents=True, exist_ok=True)
    defaults = {
        "project_root": root,
        "state_file": root / "spec" / ".executor-state.db",
        "logs_dir": root / "spec" / ".executor-logs",
        "claude_command": "claude",
        "create_git_branch": False,
        "run_tests_on_done": False,
        "auto_commit": False,
        "run_review": False,
        "callback_url": "",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)
