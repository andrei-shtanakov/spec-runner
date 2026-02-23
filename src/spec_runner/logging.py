"""Structured logging for spec-runner.

Configures structlog with context processors, output formatters,
and sensitive data redaction.
"""

import logging
import re
import sys
from pathlib import Path

import structlog

# Regex for sensitive patterns
_SENSITIVE_RE = re.compile(r"(sk-|key-|token-)[a-zA-Z0-9]{6,}", re.IGNORECASE)


def redact_sensitive(
    logger: object, method_name: str, event_dict: dict
) -> tuple[object, str, dict]:
    """Structlog processor that redacts sensitive data."""
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = _SENSITIVE_RE.sub(lambda m: m.group(1) + "***", value)
    return logger, method_name, event_dict


def setup_logging(
    level: str = "info",
    json_output: bool = False,
    log_file: Path | None = None,
    tui_mode: bool = False,
) -> None:
    """Configure structlog for the entire application.

    Args:
        level: Log level (debug, info, warning, error).
        json_output: If True, output JSON lines.
        log_file: Path to log file (used in TUI mode or for file logging).
        tui_mode: If True, suppress console output (TUI owns the screen).
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Shared processors
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        redact_sensitive,
    ]

    # Configure stdlib logging for structlog integration
    logging.basicConfig(
        format="%(message)s",
        level=log_level,
        stream=sys.stderr,
        force=True,
    )

    if tui_mode and log_file:
        # TUI mode: log to file only
        handler = logging.FileHandler(str(log_file))
        handler.setLevel(log_level)
        root = logging.getLogger()
        root.handlers = [handler]

    # Choose renderer
    if json_output:
        renderer: structlog.types.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=not tui_mode)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure formatter for stdlib handler
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)


def get_logger(module: str) -> structlog.stdlib.BoundLogger:
    """Get a structlog logger bound to a module name.

    Args:
        module: Module name (e.g., "executor", "hooks").

    Returns:
        Bound structlog logger.
    """
    return structlog.get_logger(module=module)
