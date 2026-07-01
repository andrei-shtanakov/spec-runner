"""CLI flag parsing tests (v2.3.0)."""

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
