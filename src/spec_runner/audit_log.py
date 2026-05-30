"""Compliance audit-trail writer (LABS-40).

Structured JSON-Lines appender for regulated projects that need a durable
record of every state transition the executor made, independent of the
ordinary structlog output (which is console-oriented and rotates).

Opt-in: disabled by default. Enable by setting `audit_log_path` in
`spec-runner.config.yaml`. Disabled runs instantiate a `NoOpAuditLogger`
whose `.record(...)` is a free no-op, so call sites do not need to
branch on config.

Schema (one JSON object per line):

```
{
  "timestamp": "2026-04-17T15:30:00+00:00",
  "run_id": "<uuid>",
  "operator": "<user@host>",
  "event": "<event name>",
  "task_id": "<TASK-001> | null",
  "spec_prefix": "<phase2-> | ''",
  "details": { ... event-specific }
}
```

Event names are stable public API and match the constants exposed below.
Call sites should use those constants rather than raw strings.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .logging import get_logger

# --- Stable event names (public API; breaking change requires a major bump).
EVENT_RUN_STARTED = "run_started"
EVENT_RUN_ENDED = "run_ended"
EVENT_TASK_STARTED = "task_started"
EVENT_TASK_ATTEMPT = "task_attempt"
EVENT_TASK_COMPLETED = "task_completed"
EVENT_TASK_FAILED = "task_failed"
EVENT_STATE_DEGRADED = "state_degraded"
EVENT_HOOK_STARTED = "hook_started"
EVENT_HOOK_COMPLETED = "hook_completed"

_AUDIT_EVENTS: frozenset[str] = frozenset(
    {
        EVENT_RUN_STARTED,
        EVENT_RUN_ENDED,
        EVENT_TASK_STARTED,
        EVENT_TASK_ATTEMPT,
        EVENT_TASK_COMPLETED,
        EVENT_TASK_FAILED,
        EVENT_STATE_DEGRADED,
        EVENT_HOOK_STARTED,
        EVENT_HOOK_COMPLETED,
    }
)


def _default_operator() -> str:
    """Best-effort `user@host` identifier for unattended / local runs.

    Falls back to `unknown` on either side when the environment doesn't
    expose it (containers, CI). Never raises.
    """
    user = os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"
    try:
        host = socket.gethostname() or "unknown"
    except OSError:
        host = "unknown"
    return f"{user}@{host}"


class NoOpAuditLogger:
    """Returned when auditing is disabled. Every operation is a no-op."""

    enabled: bool = False
    run_id: str = ""
    operator: str = ""

    def record(self, event: str, *, task_id: str | None = None, **details: Any) -> None:
        return None

    def close(self) -> None:
        return None


class AuditLogger:
    """Thread-safe JSON-Lines appender.

    Instances are cheap; create one per run. Writes are serialised with a
    per-instance lock and flushed on every record so a crash never loses
    a committed line. Errors while writing are logged via structlog but
    never propagate — a broken audit log must not turn into a crash loop
    on top of an already-broken workflow.
    """

    enabled: bool = True

    def __init__(
        self,
        path: Path,
        *,
        operator: str | None = None,
        spec_prefix: str = "",
        run_id: str | None = None,
    ) -> None:
        self._path = Path(path).expanduser()
        self._spec_prefix = spec_prefix
        self.operator = operator or _default_operator()
        self.run_id = run_id or str(uuid.uuid4())
        self._lock = threading.Lock()
        self._logger = get_logger("audit_log")
        self._ensure_parent_dir()

    @property
    def path(self) -> Path:
        return self._path

    def _ensure_parent_dir(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._logger.warning(
                "Cannot create audit log directory",
                path=str(self._path),
                error=str(exc),
            )

    def record(
        self,
        event: str,
        *,
        task_id: str | None = None,
        **details: Any,
    ) -> None:
        """Append a single audit entry. Never raises.

        Unknown event names are still written but also trigger a
        structlog warning so drift is noticed in ordinary logs.
        """
        if event not in _AUDIT_EVENTS:
            self._logger.warning(
                "Unknown audit event — still recorded",
                audit_event=event,
                task_id=task_id,
            )

        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
            "run_id": self.run_id,
            "operator": self.operator,
            "event": event,
            "task_id": task_id,
            "spec_prefix": self._spec_prefix,
            "details": details,
        }

        try:
            line = json.dumps(entry, default=str, sort_keys=True)
        except (TypeError, ValueError) as exc:
            self._logger.error(
                "Failed to serialise audit entry",
                event=event,
                task_id=task_id,
                error=str(exc),
            )
            return

        with self._lock:
            try:
                with self._path.open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError as exc:
                self._logger.error(
                    "Failed to write audit entry",
                    path=str(self._path),
                    error=str(exc),
                )

    def close(self) -> None:
        """Compatibility shim — we reopen on every write, nothing to close."""
        return None


def build_audit_logger(config: Any) -> AuditLogger | NoOpAuditLogger:
    """Construct an audit logger from an ExecutorConfig.

    Returns a `NoOpAuditLogger` when auditing is disabled so callers can
    always call `.record(...)` without guarding on config.
    """
    path_value = getattr(config, "audit_log_path", "") or ""
    if not path_value:
        return NoOpAuditLogger()

    path = Path(path_value)
    if not path.is_absolute():
        project_root = getattr(config, "project_root", Path.cwd())
        path = Path(project_root) / path

    operator = getattr(config, "audit_log_operator", "") or None
    spec_prefix = getattr(config, "spec_prefix", "") or ""
    return AuditLogger(path, operator=operator, spec_prefix=spec_prefix)
