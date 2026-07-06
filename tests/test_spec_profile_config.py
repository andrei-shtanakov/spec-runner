"""Tests for TASK-306: `spec_profile` config field + `--profile` CLI flag."""

from argparse import Namespace

import pytest

from spec_runner.cli import _build_parser, main
from spec_runner.config import ConfigError, ExecutorConfig, build_config
from spec_runner.spec import LITE, StageProfile, available_profiles


class TestSpecProfileDefault:
    def test_default_is_lite(self):
        assert ExecutorConfig().spec_profile == "lite"

    def test_resolve_default_returns_lite_profile(self):
        prof = ExecutorConfig().resolve_spec_profile()
        assert isinstance(prof, StageProfile)
        assert prof.name == "lite"
        assert prof.names() == LITE.names()


class TestSpecProfileResolveError:
    def test_unknown_profile_raises_config_error(self):
        cfg = ExecutorConfig(spec_profile="does-not-exist")
        with pytest.raises(ConfigError):
            cfg.resolve_spec_profile()

    def test_error_lists_available_profiles(self):
        cfg = ExecutorConfig(spec_profile="nope")
        with pytest.raises(ConfigError) as exc:
            cfg.resolve_spec_profile()
        msg = str(exc.value)
        assert "nope" in msg
        for name in available_profiles():
            assert name in msg

    def test_config_error_is_value_error(self):
        # ConfigError subclasses ValueError so existing handlers still catch it.
        assert issubclass(ConfigError, ValueError)


class TestAvailableProfiles:
    def test_includes_lite(self):
        assert "lite" in available_profiles()

    def test_sorted(self):
        names = available_profiles()
        assert names == sorted(names)


class TestProfileCliFlag:
    def test_plan_accepts_profile(self):
        ns = _build_parser().parse_args(["plan", "--gated", "--profile", "lite", "desc"])
        assert ns.profile == "lite"

    def test_plan_profile_defaults_none(self):
        ns = _build_parser().parse_args(["plan", "desc"])
        assert ns.profile is None

    def test_spec_approve_accepts_profile(self):
        ns = _build_parser().parse_args(["spec", "approve", "tasks", "--profile", "lite"])
        assert ns.profile == "lite"

    def test_spec_status_accepts_profile(self):
        ns = _build_parser().parse_args(["spec", "status", "--profile", "lite"])
        assert ns.profile == "lite"


class TestProfileThreadedIntoConfig:
    def _args(self, **overrides) -> Namespace:
        base = {"profile": None}
        base.update(overrides)
        return Namespace(**base)

    def test_cli_profile_overrides_config(self):
        cfg = build_config({}, self._args(profile="lite"))
        assert cfg.spec_profile == "lite"

    def test_yaml_profile_applied(self):
        cfg = build_config({"spec_profile": "lite"}, self._args())
        assert cfg.spec_profile == "lite"

    def test_cli_beats_yaml(self):
        cfg = build_config({"spec_profile": "yaml-one"}, self._args(profile="lite"))
        assert cfg.spec_profile == "lite"

    def test_missing_profile_attr_ok(self):
        # build_config must tolerate a Namespace without a `profile` attribute.
        cfg = build_config({}, Namespace())
        assert cfg.spec_profile == "lite"


class TestUnknownProfileCleanExit:
    def test_main_exits_cleanly_on_unknown_profile(self, monkeypatch):
        monkeypatch.setattr("spec_runner.cli.load_config_from_yaml", lambda *a, **k: {})
        monkeypatch.setattr(
            "sys.argv", ["spec-runner", "spec", "status", "--profile", "does-not-exist"]
        )
        with pytest.raises(SystemExit) as exc:
            main()
        # A string exit message (clean error), not a traceback / exit code int.
        assert isinstance(exc.value.code, str)
        assert "does-not-exist" in exc.value.code
