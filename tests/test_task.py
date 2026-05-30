"""Regression tests for task parsing (spec/tasks.md)."""

from pathlib import Path

from spec_runner.task import parse_tasks


def _single_task(tmp_path: Path, estimate_line: str) -> str:
    """Parse a one-task file and return the parsed estimate."""
    f = tmp_path / "tasks.md"
    f.write_text(f"### TASK-001: Bootstrap\n{estimate_line}\n")
    (task,) = parse_tasks(f)
    return task.estimate


class TestEstimateParsing:
    def test_integer_estimate_parsed(self, tmp_path: Path) -> None:
        assert _single_task(tmp_path, "P0 | todo | Est: 2d") == "2d"

    def test_decimal_estimate_parsed(self, tmp_path: Path) -> None:
        """Decimal estimates (e.g. 1.5d) must parse, not read as missing."""
        assert _single_task(tmp_path, "P0 | todo | Est: 1.5d") == "1.5d"

    def test_endash_range_parsed(self, tmp_path: Path) -> None:
        """En-dash ranges (1–1.5d, U+2013) must parse, not read as missing."""
        assert _single_task(tmp_path, "P0 | todo | Est: 1–1.5d") == "1–1.5d"

    def test_ascii_hyphen_range_still_parsed(self, tmp_path: Path) -> None:
        assert _single_task(tmp_path, "P0 | todo | Est: 1-2d") == "1-2d"
