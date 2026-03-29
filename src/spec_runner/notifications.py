"""Telegram notifications for spec-runner.

Sends notifications on run_complete and task_failed events
via Telegram Bot API. Best-effort — errors are logged, never raised.
"""

import json
import urllib.request
from urllib.error import URLError

from .config import ExecutorConfig
from .logging import get_logger

logger = get_logger("notifications")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def send_telegram(token: str, chat_id: str, message: str) -> bool:
    """Send a message via Telegram Bot API.

    Returns True on success, False on failure. Never raises.
    """
    url = TELEGRAM_API.format(token=token)
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown",
        }
    ).encode("utf-8")

    try:
        req = urllib.request.Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except (URLError, OSError, ValueError) as e:
        logger.warning("Telegram send failed", error=str(e))
        return False


def notify(config: ExecutorConfig, event: str, message: str) -> bool:
    """Send notification if event is in notify_on list and Telegram is configured.

    Args:
        config: Executor configuration with Telegram settings.
        event: Event type (e.g., "run_complete", "task_failed").
        message: Notification message text.

    Returns:
        True if sent, False if skipped or failed.
    """
    import os

    token = config.telegram_bot_token or os.environ.get("SPEC_RUNNER_TELEGRAM_TOKEN", "")
    chat_id = config.telegram_chat_id or os.environ.get("SPEC_RUNNER_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False

    if event not in config.notify_on:
        return False

    return send_telegram(token, chat_id, message)


def notify_task_failed(config: ExecutorConfig, task_id: str, error: str) -> bool:
    """Notify about a task failure."""
    message = f"*spec-runner*: task `{task_id}` failed\n_{error[:200]}_"
    return notify(config, "task_failed", message)


def notify_run_complete(
    config: ExecutorConfig,
    completed: int,
    failed: int,
    total_cost: float | None = None,
) -> bool:
    """Notify about run completion."""
    parts = [f"*spec-runner*: run complete — {completed} done, {failed} failed"]
    if total_cost is not None and total_cost > 0:
        parts.append(f"Cost: ${total_cost:.2f}")
    return notify(config, "run_complete", "\n".join(parts))
