"""#402: `**Scenarios:**` — parsing, the coverage core and `validate`.

Spec: docs/superpowers/specs/2026-09-23-verify-scenario-coverage-design.md
"""

from __future__ import annotations

from pathlib import Path

from spec_runner.task import parse_tasks
from spec_runner.validate import validate_task_fields

HEADER = "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\nEst: 1d\n"


def _tasks(tmp_path: Path, body: str):
    path = tmp_path / "tasks.md"
    path.write_text(HEADER + body)
    return parse_tasks(path)


class TestParsing:
    def test_absent_line_is_none(self, tmp_path):
        (task,) = _tasks(tmp_path, "**Mode:** verify_first\n")
        assert task.scenarios is None
        assert task.scenarios_error is None

    def test_ids_kept_in_declared_order(self, tmp_path):
        (task,) = _tasks(tmp_path, "**Scenarios:** BEH-10, BEH-09, BEH-09a\n")
        assert task.scenarios == ["BEH-10", "BEH-09", "BEH-09a"]
        assert task.scenarios_error is None

    def test_line_after_a_verifies_block_is_still_read(self, tmp_path):
        (task,) = _tasks(
            tmp_path,
            "**Verifies:**\n- tests/test_a.py::test_x\n**Scenarios:** BEH-09\n",
        )
        assert task.verifies == ["tests/test_a.py::test_x"]
        assert task.scenarios == ["BEH-09"]


class TestMalformedLineIsAValidateError:
    def _errors(self, tmp_path, line: str) -> str:
        tasks = _tasks(tmp_path, line + "\n")
        assert tasks[0].scenarios is None
        return "\n".join(validate_task_fields(tasks).errors)

    def test_empty_line(self, tmp_path):
        joined = self._errors(tmp_path, "**Scenarios:**")
        assert "TASK-001" in joined and "**Scenarios:**" in joined and "empty" in joined

    def test_empty_item(self, tmp_path):
        joined = self._errors(tmp_path, "**Scenarios:** BEH-09,")
        assert "BEH-09," in joined  # the line is quoted back

    def test_wrong_shape(self, tmp_path):
        for bad in ("beh-09", "BEH09", "BEH-09A", "BEH-09-a", "BEH-09; BEH-10", "—"):
            joined = self._errors(tmp_path, f"**Scenarios:** {bad}")
            assert "TASK-001" in joined, bad
            assert bad in joined, bad
