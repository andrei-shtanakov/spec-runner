"""Regression tests for task parsing (spec/tasks.md)."""

from pathlib import Path

from spec_runner.task import (
    mark_all_checklist_done,
    parse_tasks,
    update_checklist_item,
    update_task_status,
)


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


TASKS_WITH_FM = """---
spec_stage: tasks
status: approved
version: 2
---
## Milestone M1

### TASK-001: First
🔴 P0 | ⬜ TODO | Est: 1d
"""


def test_parse_tasks_ignores_frontmatter(tmp_path: Path) -> None:
    p = tmp_path / "tasks.md"
    p.write_text(TASKS_WITH_FM)
    tasks = parse_tasks(p)
    assert [t.id for t in tasks] == ["TASK-001"]
    assert tasks[0].name == "First"


def test_parse_tasks_without_frontmatter_unchanged(tmp_path: Path) -> None:
    p = tmp_path / "tasks.md"
    p.write_text("### TASK-009: Solo\n🔴 P0 | ⬜ TODO | Est: 1d\n")
    tasks = parse_tasks(p)
    assert [t.id for t in tasks] == ["TASK-009"]


def test_update_task_status_preserves_frontmatter(tmp_path: Path) -> None:
    """A routine status update must not silently drop leading frontmatter."""
    p = tmp_path / "tasks.md"
    p.write_text(TASKS_WITH_FM)

    assert update_task_status(p, "TASK-001", "in_progress") is True

    text = p.read_text()
    assert text.startswith("---\n")
    assert "spec_stage: tasks" in text
    assert "version: 2" in text
    assert parse_tasks(p)[0].status == "in_progress"


def test_update_checklist_item_preserves_frontmatter(tmp_path: Path) -> None:
    p = tmp_path / "tasks.md"
    p.write_text(TASKS_WITH_FM.rstrip("\n") + "\n- [ ] do the thing\n")

    assert update_checklist_item(p, "TASK-001", 0, True) is True

    text = p.read_text()
    assert text.startswith("---\n")
    assert "version: 2" in text
    assert "- [x] do the thing" in text


def test_mark_all_checklist_done_preserves_frontmatter(tmp_path: Path) -> None:
    p = tmp_path / "tasks.md"
    p.write_text(TASKS_WITH_FM.rstrip("\n") + "\n- [ ] one\n- [ ] two\n### TASK-002: Next\n")

    assert mark_all_checklist_done(p, "TASK-001") == 2

    text = p.read_text()
    assert text.startswith("---\n")
    assert "version: 2" in text
    assert "- [x] one" in text
    assert "- [x] two" in text


class TestCustomIdPrefix:
    """#72: external projects use native numbering (KAP-002), not just TASK-."""

    def _write(self, tmp_path, body):
        p = tmp_path / "tasks.md"
        p.write_text(body)
        return p

    def test_custom_prefix_parses(self, tmp_path):
        p = self._write(
            tmp_path,
            "## Milestone 1: MVP\n\n"
            "### KAP-001: Bootstrap\n"
            "🔴 P0 | ✅ DONE | Est: 1d\n\n"
            "**Checklist:**\n- [x] a\n\n"
            "**Traces to:** [REQ-001]\n"
            "**Depends on:** —\n"
            "**Blocks:** [KAP-002]\n\n"
            "### KAP-002: Feature\n"
            "🟠 P1 | ⬜ TODO | Est: 2d\n\n"
            "**Checklist:**\n- [ ] b\n\n"
            "**Traces to:** [REQ-002]\n"
            "**Depends on:** [KAP-001]\n",
        )
        tasks = parse_tasks(p)
        assert [t.id for t in tasks] == ["KAP-001", "KAP-002"]
        assert tasks[0].blocks == ["KAP-002"]
        assert tasks[1].depends_on == ["KAP-001"]
        assert tasks[0].traces_to == ["REQ-001"]

    def test_foreign_prefix_refs_not_dependencies(self, tmp_path):
        """[REQ-…]/[DESIGN-…] in a Depends line are docs, not task deps."""
        p = self._write(
            tmp_path,
            "### KAP-001: Solo\n"
            "🔴 P0 | ⬜ TODO | Est: 1d\n\n"
            "**Checklist:**\n- [ ] a\n\n"
            "**Depends on:** [REQ-001], [DESIGN-002]\n",
        )
        tasks = parse_tasks(p)
        assert tasks[0].depends_on == []

    def test_mixed_prefixes_both_recognized(self, tmp_path):
        p = self._write(
            tmp_path,
            "### TASK-001: Old style\n"
            "🔴 P0 | ✅ DONE | Est: 1d\n\n"
            "**Checklist:**\n- [x] a\n\n"
            "### KAP-002: New style\n"
            "🟠 P1 | ⬜ TODO | Est: 1d\n\n"
            "**Checklist:**\n- [ ] b\n\n"
            "**Depends on:** [TASK-001]\n",
        )
        tasks = parse_tasks(p)
        assert [t.id for t in tasks] == ["TASK-001", "KAP-002"]
        assert tasks[1].depends_on == ["TASK-001"]

    def test_default_task_prefix_unchanged(self, tmp_path):
        p = self._write(
            tmp_path,
            "### TASK-001: Classic\n"
            "🔴 P0 | ⬜ TODO | Est: 1d\n\n"
            "**Checklist:**\n- [ ] a\n\n"
            "**Depends on:** —\n",
        )
        tasks = parse_tasks(p)
        assert tasks[0].id == "TASK-001"
        assert tasks[0].depends_on == []
