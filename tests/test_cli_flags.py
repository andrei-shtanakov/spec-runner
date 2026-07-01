"""CLI flag parsing tests (v2.3.0)."""

import pytest

from spec_runner.cli import _build_parser


class TestRunSubparserFlags:
    def test_no_reset_failed_flag_present(self):
        parser = _build_parser()
        ns = parser.parse_args(["run", "--all", "--no-reset-failed"])
        assert ns.no_reset_failed is True

    def test_no_reset_failed_default_false(self):
        parser = _build_parser()
        ns = parser.parse_args(["run", "--all"])
        assert ns.no_reset_failed is False

    def test_strict_flag_present(self):
        parser = _build_parser()
        ns = parser.parse_args(["run", "--strict"])
        assert ns.strict is True
        assert ns.no_strict is False

    def test_no_strict_flag_present(self):
        parser = _build_parser()
        ns = parser.parse_args(["run", "--no-strict"])
        assert ns.no_strict is True
        assert ns.strict is False


class TestWatchSubparserFlags:
    def test_strict_flag_present(self):
        parser = _build_parser()
        ns = parser.parse_args(["watch", "--strict"])
        assert ns.strict is True
        assert ns.no_strict is False

    def test_no_strict_flag_present(self):
        parser = _build_parser()
        ns = parser.parse_args(["watch", "--no-strict"])
        assert ns.no_strict is True
        assert ns.strict is False


class TestPlanSubparserFlags:
    def test_gated_and_stage_flags(self):
        parser = _build_parser()
        ns = parser.parse_args(["plan", "--gated", "--stage", "design", "desc"])
        assert ns.gated is True
        assert ns.stage == "design"

    def test_gated_default_false_and_stage_default_none(self):
        parser = _build_parser()
        ns = parser.parse_args(["plan", "desc"])
        assert ns.gated is False
        assert ns.stage is None


class TestSpecSubparser:
    def test_spec_subparser_exists(self):
        parser = _build_parser()
        ns = parser.parse_args(["spec", "approve", "requirements"])
        assert ns.command == "spec"
        assert ns.spec_command == "approve"
        assert ns.stage == "requirements"

    def test_spec_status_no_extra_args(self):
        parser = _build_parser()
        ns = parser.parse_args(["spec", "status"])
        assert ns.command == "spec"
        assert ns.spec_command == "status"

    def test_spec_reject_and_check_take_stage(self):
        parser = _build_parser()
        ns = parser.parse_args(["spec", "reject", "design"])
        assert ns.spec_command == "reject"
        assert ns.stage == "design"
        ns2 = parser.parse_args(["spec", "check", "tasks"])
        assert ns2.spec_command == "check"
        assert ns2.stage == "tasks"

    def test_spec_adopt_takes_stage_and_force(self):
        parser = _build_parser()
        ns = parser.parse_args(["spec", "adopt", "requirements", "--force"])
        assert ns.spec_command == "adopt"
        assert ns.stage == "requirements"
        assert ns.force is True

    def test_spec_adopt_force_defaults_false(self):
        parser = _build_parser()
        ns = parser.parse_args(["spec", "adopt", "requirements"])
        assert ns.force is False

    def test_spec_invalid_stage_rejected(self):
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["spec", "approve", "bogus"])

    def test_spec_no_subcommand(self):
        parser = _build_parser()
        ns = parser.parse_args(["spec"])
        assert ns.command == "spec"
        assert ns.spec_command is None
