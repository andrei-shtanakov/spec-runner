"""Notifications for spec-runner.

Sends notifications via Telegram Bot API and/or generic webhook
on run_complete, task_failed, and budget_warning events.
Best-effort — errors are logged, never raised.
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


def send_webhook(
    url: str,
    method: str,
    headers: dict[str, str],
    body: str,
) -> bool:
    """Send a generic webhook notification.

    Returns True on success, False on failure. Never raises.
    """
    try:
        req_headers = {"Content-Type": "application/json"}
        req_headers.update(headers)
        data = body.encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
        urllib.request.urlopen(req, timeout=10)
        return True
    except (URLError, OSError, ValueError) as e:
        logger.warning("Webhook send failed", url=url, error=str(e))
        return False


def _render_webhook_template(
    template: str,
    event: str,
    message: str,
    task_id: str = "",
    task_name: str = "",
    cost: str = "",
    duration: str = "",
) -> str:
    """Render webhook template with variable substitution."""
    result = template
    for key, value in [
        ("{{event}}", event),
        ("{{message}}", message),
        ("{{task_id}}", task_id),
        ("{{task_name}}", task_name),
        ("{{cost}}", cost),
        ("{{duration}}", duration),
    ]:
        result = result.replace(key, value)
    return result


def notify(
    config: ExecutorConfig,
    event: str,
    message: str,
    task_id: str = "",
    task_name: str = "",
    cost: str = "",
    duration: str = "",
) -> bool:
    """Send notification if event is in notify_on list.

    Tries both Telegram and webhook if configured. Returns True if any succeeded.
    """
    import os

    if event not in config.notify_on:
        return False

    sent = False

    # Telegram
    token = config.telegram_bot_token or os.environ.get("SPEC_RUNNER_TELEGRAM_TOKEN", "")
    chat_id = config.telegram_chat_id or os.environ.get("SPEC_RUNNER_TELEGRAM_CHAT_ID", "")
    if token and chat_id:
        sent = send_telegram(token, chat_id, message) or sent

    # Generic webhook
    if config.webhook_url:
        if config.webhook_template:
            body = _render_webhook_template(
                config.webhook_template,
                event=event,
                message=message,
                task_id=task_id,
                task_name=task_name,
                cost=cost,
                duration=duration,
            )
        else:
            body = json.dumps({"event": event, "message": message})
        sent = (
            send_webhook(
                config.webhook_url,
                config.webhook_method,
                config.webhook_headers,
                body,
            )
            or sent
        )

    return sent


def notify_task_failed(config: ExecutorConfig, task_id: str, error: str) -> bool:
    """Notify about a task failure."""
    message = f"*spec-runner*: task `{task_id}` failed\n_{error[:200]}_"
    return notify(config, "task_failed", message, task_id=task_id)


def notify_run_complete(
    config: ExecutorConfig,
    completed: int,
    failed: int,
    total_cost: float | None = None,
) -> bool:
    """Notify about run completion."""
    parts = [f"*spec-runner*: run complete — {completed} done, {failed} failed"]
    cost_str = ""
    if total_cost is not None and total_cost > 0:
        cost_str = f"${total_cost:.2f}"
        parts.append(f"Cost: {cost_str}")
    return notify(config, "run_complete", "\n".join(parts), cost=cost_str)
