"""Tests for spec_runner.notifications module."""

from unittest.mock import MagicMock, patch

from spec_runner.config import ExecutorConfig
from spec_runner.notifications import (
    notify,
    notify_run_complete,
    notify_task_failed,
    send_telegram,
)


class TestSendTelegram:
    @patch("spec_runner.notifications.urllib.request.urlopen")
    def test_sends_message_successfully(self, mock_urlopen):
        mock_urlopen.return_value = MagicMock()
        result = send_telegram("token123", "chat456", "Hello")
        assert result is True
        mock_urlopen.assert_called_once()

    @patch("spec_runner.notifications.urllib.request.urlopen")
    def test_returns_false_on_error(self, mock_urlopen):
        from urllib.error import URLError

        mock_urlopen.side_effect = URLError("Connection refused")
        result = send_telegram("token123", "chat456", "Hello")
        assert result is False

    @patch("spec_runner.notifications.urllib.request.urlopen")
    def test_sends_correct_payload(self, mock_urlopen):
        import json

        mock_urlopen.return_value = MagicMock()
        send_telegram("mytoken", "mychat", "Test message")

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        assert "mytoken" in req.full_url
        payload = json.loads(req.data)
        assert payload["chat_id"] == "mychat"
        assert payload["text"] == "Test message"
        assert payload["parse_mode"] == "Markdown"


class TestNotify:
    def test_skips_when_no_token(self):
        config = ExecutorConfig(telegram_chat_id="123")
        assert notify(config, "run_complete", "test") is False

    def test_skips_when_no_chat_id(self):
        config = ExecutorConfig(telegram_bot_token="tok")
        assert notify(config, "run_complete", "test") is False

    def test_skips_when_event_not_in_notify_on(self):
        config = ExecutorConfig(
            telegram_bot_token="tok",
            telegram_chat_id="123",
            notify_on=["run_complete"],
        )
        assert notify(config, "task_failed", "test") is False

    @patch("spec_runner.notifications.send_telegram", return_value=True)
    def test_sends_when_configured(self, mock_send):
        config = ExecutorConfig(
            telegram_bot_token="tok",
            telegram_chat_id="123",
            notify_on=["task_failed"],
        )
        result = notify(config, "task_failed", "error msg")
        assert result is True
        mock_send.assert_called_once_with("tok", "123", "error msg")


class TestNotifyTaskFailed:
    @patch("spec_runner.notifications.send_telegram", return_value=True)
    def test_formats_task_failure_message(self, mock_send):
        config = ExecutorConfig(
            telegram_bot_token="tok",
            telegram_chat_id="123",
        )
        notify_task_failed(config, "TASK-007", "Tests failed: assert False")
        msg = mock_send.call_args[0][2]
        assert "TASK-007" in msg
        assert "failed" in msg
        assert "Tests failed" in msg

    def test_noop_when_not_configured(self):
        config = ExecutorConfig()
        result = notify_task_failed(config, "TASK-001", "error")
        assert result is False


class TestNotifyRunComplete:
    @patch("spec_runner.notifications.send_telegram", return_value=True)
    def test_formats_run_complete_message(self, mock_send):
        config = ExecutorConfig(
            telegram_bot_token="tok",
            telegram_chat_id="123",
        )
        notify_run_complete(config, completed=5, failed=1, total_cost=0.84)
        msg = mock_send.call_args[0][2]
        assert "5 done" in msg
        assert "1 failed" in msg
        assert "$0.84" in msg

    @patch("spec_runner.notifications.send_telegram", return_value=True)
    def test_no_cost_when_zero(self, mock_send):
        config = ExecutorConfig(
            telegram_bot_token="tok",
            telegram_chat_id="123",
        )
        notify_run_complete(config, completed=3, failed=0)
        msg = mock_send.call_args[0][2]
        assert "$" not in msg

    def test_noop_when_not_configured(self):
        config = ExecutorConfig()
        result = notify_run_complete(config, completed=1, failed=0)
        assert result is False


class TestWebhookNotifications:
    @patch("spec_runner.notifications.send_webhook", return_value=True)
    def test_webhook_sends_with_template(self, mock_send):
        config = ExecutorConfig(
            webhook_url="https://hooks.example.com/test",
            webhook_template='{"text": "{{event}}: {{message}}"}',
        )
        result = notify(config, "task_failed", "test error", task_id="TASK-001")
        assert result is True
        mock_send.assert_called_once()
        body = mock_send.call_args[0][3]
        assert "task_failed" in body
        assert "test error" in body

    @patch("spec_runner.notifications.send_webhook", return_value=True)
    def test_webhook_default_json_without_template(self, mock_send):
        config = ExecutorConfig(
            webhook_url="https://hooks.example.com/test",
        )
        result = notify(config, "run_complete", "done")
        assert result is True
        mock_send.assert_called_once()

    def test_render_template_substitutes_variables(self):
        from spec_runner.notifications import _render_webhook_template

        result = _render_webhook_template(
            '{"event": "{{event}}", "task": "{{task_id}}"}',
            event="task_failed",
            message="err",
            task_id="TASK-001",
        )
        assert '"task_failed"' in result
        assert '"TASK-001"' in result
