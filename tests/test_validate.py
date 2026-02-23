"""Tests for spec_runner.validate module."""

from pathlib import Path

from spec_runner.task import Task
from spec_runner.validate import (
    _detect_cycle,
    validate_task_fields,
    validate_tasks,
)


def _make_task(
    task_id: str = "TASK-001",
    name: str = "Test task",
    priority: str = "p0",
    status: str = "todo",
    depends_on: list[str] | None = None,
) -> Task:
    """Helper to create a Task with sensible defaults."""
    return Task(
        id=task_id,
        name=name,
        priority=priority,
        status=status,
        estimate="1d",
        depends_on=depends_on or [],
    )


# --- Minimal tasks.md content for testing ---

VALID_TASKS_MD = """\
## Milestone 1: Core

### TASK-001: First task
🔴 P0 | ⬜ todo | Est: 1d

**Checklist:**
- [ ] Step one

**Depends on:** —
**Blocks:** [TASK-002]

### TASK-002: Second task
🟠 P1 | ⬜ todo | Est: 2d

**Depends on:** [TASK-001]
**Blocks:** —
"""

CIRCULAR_TASKS_MD = """\
### TASK-001: First task
🔴 P0 | ⬜ todo | Est: 1d

**Depends on:** [TASK-002]

### TASK-002: Second task
🟠 P1 | ⬜ todo | Est: 2d

**Depends on:** [TASK-001]
"""


class TestValidateTasksExist:
    """File existence and basic parsing checks."""

    def test_missing_file(self, tmp_path: Path) -> None:
        result = validate_tasks(tmp_path / "nonexistent.md")
        assert not result.ok
        assert any("not found" in e.lower() or "does not exist" in e.lower() for e in result.errors)

    def test_empty_file(self, tmp_path: Path) -> None:
        empty = tmp_path / "tasks.md"
        empty.write_text("")
        result = validate_tasks(empty)
        assert not result.ok
        assert any("no tasks" in e.lower() for e in result.errors)

    def test_valid_file(self, tmp_path: Path) -> None:
        tasks_file = tmp_path / "tasks.md"
        tasks_file.write_text(VALID_TASKS_MD)
        result = validate_tasks(tasks_file)
        assert result.ok
        assert result.errors == []


class TestValidateDependencies:
    """Dependency reference and cycle checks."""

    def test_missing_dep_ref(self) -> None:
        tasks = [
            _make_task("TASK-001", depends_on=["TASK-999"]),
        ]
        result = validate_task_fields(tasks)
        assert not result.ok
        assert any("TASK-999" in e for e in result.errors)

    def test_circular_dep(self) -> None:
        tasks = [
            _make_task("TASK-001", depends_on=["TASK-002"]),
            _make_task("TASK-002", depends_on=["TASK-001"]),
        ]
        result = _detect_cycle(tasks)
        assert not result.ok
        assert any("cycle" in e.lower() for e in result.errors)

    def test_valid_chain(self) -> None:
        tasks = [
            _make_task("TASK-001"),
            _make_task("TASK-002", depends_on=["TASK-001"]),
            _make_task("TASK-003", depends_on=["TASK-002"]),
        ]
        result = _detect_cycle(tasks)
        assert result.ok


class TestValidateStatusAndPriority:
    """Status and priority field validation."""

    def test_invalid_status(self) -> None:
        tasks = [_make_task("TASK-001", status="running")]
        result = validate_task_fields(tasks)
        assert not result.ok
        assert any("status" in e.lower() for e in result.errors)

    def test_invalid_priority(self) -> None:
        tasks = [_make_task("TASK-001", priority="critical")]
        result = validate_task_fields(tasks)
        assert not result.ok
        assert any("priority" in e.lower() for e in result.errors)

    def test_multiple_errors_accumulated(self) -> None:
        """A task with both invalid status AND priority produces 2 errors."""
        tasks = [_make_task("TASK-001", priority="p9", status="invalid")]
        result = validate_task_fields(tasks)
        assert len(result.errors) >= 2


class TestCircularDepFile:
    """Cycle detection via file-based validation."""

    def test_circular_dep_via_file(self, tmp_path: Path) -> None:
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text(CIRCULAR_TASKS_MD)
        result = validate_tasks(tasks_file)
        assert not result.ok
        assert any("cycle" in e.lower() for e in result.errors)


class TestDfsCycleRegression:
    """Regression tests for DFS cycle detection on complex graphs."""

    def test_complex_cycle_no_crash(self, tmp_path: Path) -> None:
        """Regression: DFS must not crash on complex graphs with cycles."""
        spec_dir = tmp_path / "spec"
        spec_dir.mkdir()
        tasks_file = spec_dir / "tasks.md"
        tasks_file.write_text(
            "# Tasks\n\n"
            "### TASK-A: A\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
            "**Depends on:** TASK-B, TASK-C\n\n"
            "### TASK-B: B\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
            "**Depends on:** TASK-D\n\n"
            "### TASK-C: C\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
            "**Depends on:** TASK-D\n\n"
            "### TASK-D: D\n"
            "🔴 P0 | ⬜ todo | Est: 1d\n\n"
            "**Depends on:** TASK-B\n"
        )
        result = validate_tasks(tasks_file)
        assert not result.ok
        assert any("cycle" in e.lower() for e in result.errors)
