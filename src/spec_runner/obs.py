"""Orchestra observability emitter — reference implementation.

Source of truth for `obs.py` (vendored into other Python projects).
Produces OpenTelemetry Logs Data Model JSONL, one file per PID.

Contract: see _cowork_output/observability-contract/log-schema.json
"""
from __future__ import annotations

import json
import logging as _stdlib_logging
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

import structlog
import ulid

_SEVERITY_NUMBER = {
    "debug": 5, "info": 9, "warning": 13, "warn": 13,
    "error": 17, "critical": 21, "fatal": 21,
}
_SEVERITY_TEXT = {
    5: "DEBUG", 9: "INFO", 13: "WARN", 17: "ERROR", 21: "FATAL",
}

_initialized = False


def _now_ns() -> int:
    return time.time_ns()


def _iso_micros(ns: int) -> str:
    dt = datetime.fromtimestamp(ns / 1_000_000_000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


_TRACEPARENT_RE = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")


def _parse_traceparent() -> tuple[str, str | None]:
    """Return (trace_id, parent_span_id). parent_span_id is None at root."""
    raw = os.environ.get("TRACEPARENT", "").strip()
    if not raw:
        return secrets.token_hex(16), None
    m = _TRACEPARENT_RE.match(raw)
    if not m:
        _stdlib_logging.getLogger(__name__).warning(
            "malformed TRACEPARENT=%r, treating as root", raw
        )
        return secrets.token_hex(16), None
    return m.group(1), m.group(2)


def _reshape_to_otel(project: str):
    """Final processor: rearrange structlog dict into OTel Logs DM shape."""
    def processor(logger, method_name, event_dict):
        ns = event_dict.pop("_ts_ns", _now_ns())
        # structlog passes method_name: "info", "error", etc. Use it, not event_dict.
        sev_num = _SEVERITY_NUMBER.get(method_name.lower(), 9)
        event_dict.pop("level", None)   # drop if present; method_name is authoritative
        event_name = event_dict.pop("event")

        attrs = {"event": event_name}
        for key in ("pipeline_id", "parent_span_id", "task_id", "module"):
            if key in event_dict:
                attrs[key] = event_dict.pop(key)
        attrs.update(event_dict)

        return {
            "Timestamp": str(ns),
            "ts_iso": _iso_micros(ns),
            "SeverityText": _SEVERITY_TEXT[sev_num],
            "SeverityNumber": sev_num,
            "TraceId": attrs.pop("_trace_id", "0" * 32),
            "SpanId": attrs.pop("_span_id", "0" * 16),
            "TraceFlags": "01",
            "Body": attrs.pop("_body", event_name),
            "Resource": {"service.name": project},
            "Attributes": attrs,
        }
    return processor


def _default_log_dir() -> Path:
    env_dir = os.environ.get("ORCHESTRA_LOG_DIR")
    if env_dir:
        return Path(env_dir)
    pid = os.environ.get("ORCHESTRA_PIPELINE_ID") or str(ulid.new())
    return Path.cwd() / "logs" / pid


def init_logging(
    project: str,
    *,
    level: str | None = None,
    log_dir: Path | None = None,
) -> None:
    global _initialized
    _initialized = False
    structlog.contextvars.clear_contextvars()

    if _initialized:
        return
    _initialized = True

    log_dir = log_dir or _default_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    output_path = log_dir / f"{project}-{os.getpid()}.jsonl"

    pipeline_id = os.environ.get("ORCHESTRA_PIPELINE_ID") or str(ulid.new())
    trace_id, parent_span_id = _parse_traceparent()
    bind_kwargs: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        "_trace_id": trace_id,
        "_span_id": secrets.token_hex(8),
    }
    if parent_span_id is not None:
        bind_kwargs["parent_span_id"] = parent_span_id
    structlog.contextvars.bind_contextvars(**bind_kwargs)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _reshape_to_otel(project),
            structlog.processors.JSONRenderer(sort_keys=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            {"debug": 10, "info": 20, "warning": 30, "error": 40}.get(
                (level or os.environ.get("ORCHESTRA_LOG_LEVEL") or "info").lower(), 20
            )
        ),
        logger_factory=structlog.WriteLoggerFactory(file=output_path.open("a")),
        cache_logger_on_first_use=True,
    )


def get_logger(module: str | None = None) -> structlog.BoundLogger:
    return structlog.get_logger(module=module) if module else structlog.get_logger()
