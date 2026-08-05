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

    def test_no_interactive_flag_present(self):
        parser = _build_parser()
        ns = parser.parse_args(["plan", "--gated", "--no-interactive", "desc"])
        assert ns.no_interactive is True

    def test_no_interactive_default_false(self):
        parser = _build_parser()
        ns = parser.parse_args(["plan", "--gated", "desc"])
        assert ns.no_interactive is False


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


class TestBudgetDefaultIsolation:
    """Regression for #68/#67: doctor's 0.50 budget default must not leak.

    argparse shares Action objects across subparsers built with
    ``parents=[common]``, so ``doctor_parser.set_defaults(budget=...)``
    mutated the shared ``--budget`` action and every subcommand inherited
    a $0.50 default — overriding the YAML budget and blocking follow-up
    runs once total cost crossed $0.50.
    """

    def test_run_budget_default_none(self):
        parser = _build_parser()
        ns = parser.parse_args(["run", "--all"])
        assert ns.budget is None

    def test_status_budget_default_none(self):
        parser = _build_parser()
        ns = parser.parse_args(["status"])
        assert ns.budget is None

    def test_costs_budget_default_none(self):
        parser = _build_parser()
        ns = parser.parse_args(["costs"])
        assert ns.budget is None

    def test_doctor_budget_default_none(self):
        # cmd_doctor resolves None → DOCTOR_DEFAULT_BUDGET_USD itself.
        parser = _build_parser()
        ns = parser.parse_args(["doctor"])
        assert ns.budget is None

    def test_run_budget_explicit_still_works(self):
        parser = _build_parser()
        ns = parser.parse_args(["run", "--budget", "2.5"])
        assert ns.budget == 2.5
