"""Back-compat shim over spec_runner.obs.

The canonical entrypoint is now `spec_runner.obs.init_logging`. This module
remains for existing callers that import `setup_logging`, `get_logger`, or
`redact_sensitive`.
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog

from spec_runner import obs

# Regex for sensitive patterns — kept for redact_sensitive back-compat.
_SENSITIVE_RE = re.compile(r"(sk-|key-|token-)[a-zA-Z0-9]{6,}", re.IGNORECASE)


def redact_sensitive(logger: object, method_name: object, event_dict: dict) -> dict:
    """Structlog processor that redacts sensitive data (back-compat export)."""
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = _SENSITIVE_RE.sub(lambda m: m.group(1) + "***", value)
    return event_dict


def setup_logging(
    level: str = "info",
    json_output: bool = True,  # ignored — obs always emits JSON
    log_file: Path | None = None,
    tui_mode: bool = False,  # ignored — obs writes to file; stdout stays free
) -> None:
    """Delegate to obs.init_logging; preserved signature for back-compat."""
    log_dir = log_file.parent if log_file else None
    obs.init_logging("spec-runner", level=level, log_dir=log_dir)


def get_logger(module: str) -> structlog.BoundLogger:
    """Get a structlog logger bound to a module name (back-compat export)."""
    return obs.get_logger(module=module)


__all__ = ["get_logger", "redact_sensitive", "setup_logging"]
