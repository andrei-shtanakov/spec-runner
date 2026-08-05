"""A run without a config file must warn loudly, not default silently (#63)."""

from pathlib import Path

from spec_runner.config import ExecutorConfig, missing_config_warning


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {"project_root": root, "config_found": False}
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


class TestMissingConfigWarning:
    def test_none_when_config_found(self, tmp_path):
        assert missing_config_warning(_cfg(tmp_path, config_found=True)) is None

    def test_warns_and_names_safety_defaults(self, tmp_path):
        msg = missing_config_warning(_cfg(tmp_path))
        assert msg is not None
        assert "spec-runner.config.yaml" in msg
        assert "self-merge" in msg
        assert "uv run pytest" in msg  # default test oracle named explicitly

    def test_sharper_hint_when_prior_state_exists(self, tmp_path):
        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / ".executor-state.db").write_bytes(b"x")
        msg = missing_config_warning(_cfg(tmp_path))
        assert msg is not None
        assert "previous run's state" in msg

    def test_no_state_hint_on_fresh_project(self, tmp_path):
        msg = missing_config_warning(_cfg(tmp_path))
        assert msg is not None
        assert "previous run's state" not in msg
