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
