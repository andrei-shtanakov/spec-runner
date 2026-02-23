"""Tests for spec_runner.logging module."""

from spec_runner.logging import get_logger, redact_sensitive, setup_logging


class TestSetupLogging:
    """Tests for setup_logging."""

    def test_setup_returns_none(self):
        setup_logging()

    def test_setup_with_json_mode(self):
        setup_logging(json_output=True)

    def test_setup_with_log_file(self, tmp_path):
        log_file = tmp_path / "test.log"
        setup_logging(log_file=log_file)

    def test_setup_with_tui_mode(self, tmp_path):
        log_file = tmp_path / "test.log"
        setup_logging(tui_mode=True, log_file=log_file)


class TestGetLogger:
    """Tests for get_logger."""

    def test_returns_bound_logger(self):
        logger = get_logger("test_module")
        assert logger is not None

    def test_logger_has_module_context(self):
        logger = get_logger("my_module")
        assert callable(getattr(logger, "info", None))

    def test_logger_can_bind_task_id(self):
        logger = get_logger("executor")
        task_logger = logger.bind(task_id="TASK-001")
        assert task_logger is not None


class TestRedactSensitive:
    """Tests for redact_sensitive processor."""

    def test_redacts_sk_keys(self):
        event_dict = redact_sensitive(None, None, {"api_key": "sk-abc123def456"})
        assert event_dict["api_key"] == "sk-***"
        assert "abc123" not in event_dict["api_key"]

    def test_preserves_normal_values(self):
        event_dict = redact_sensitive(None, None, {"message": "hello world"})
        assert event_dict["message"] == "hello world"

    def test_redacts_in_event_string(self):
        event_dict = redact_sensitive(None, None, {"event": "Using key sk-abc123def456ghi"})
        assert "sk-abc123" not in event_dict["event"]
