"""Validation for tasks.md — field checks, dependency refs, and cycle detection."""

from dataclasses import dataclass, field
from pathlib import Path

from spec_runner.logging import get_logger
from spec_runner.task import Task, parse_tasks

log = get_logger("validate")

VALID_STATUSES = {"todo", "in_progress", "done", "blocked"}
VALID_PRIORITIES = {"p0", "p1", "p2", "p3"}


@dataclass
class ValidationResult:
    """Collects errors and warnings from validation checks."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when no errors were found."""
        return len(self.errors) == 0

    def merge(self, other: "ValidationResult") -> None:
        """Merge another result into this one."""
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)


def validate_task_fields(tasks: list[Task]) -> ValidationResult:
    """Check that every task has a valid status, priority, and dependency refs.

    Args:
        tasks: Parsed task list.

    Returns:
        ValidationResult with any errors found.
    """
    result = ValidationResult()
    task_ids = {t.id for t in tasks}

    for task in tasks:
        if task.status not in VALID_STATUSES:
            result.errors.append(
                f"{task.id}: invalid status '{task.status}' "
                f"(expected one of {sorted(VALID_STATUSES)})"
            )

        if task.priority not in VALID_PRIORITIES:
            result.errors.append(
                f"{task.id}: invalid priority '{task.priority}' "
                f"(expected one of {sorted(VALID_PRIORITIES)})"
            )

        for dep in task.depends_on:
            if dep not in task_ids:
                result.errors.append(f"{task.id}: dependency '{dep}' not found in task list")

    return result


def _detect_cycle(tasks: list[Task]) -> ValidationResult:
    """DFS cycle detection on the dependency graph.

    Args:
        tasks: Parsed task list.

    Returns:
        ValidationResult with an error per cycle found.
    """
    result = ValidationResult()

    # Build adjacency: task -> list of tasks it depends on
    adj: dict[str, list[str]] = {t.id: list(t.depends_on) for t in tasks}
    all_ids = set(adj.keys())

    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = dict.fromkeys(all_ids, WHITE)

    def dfs(node: str, path: list[str]) -> None:
        color[node] = GRAY
        path.append(node)
        for neighbour in adj.get(node, []):
            if neighbour not in all_ids:
                continue  # dangling ref handled by validate_task_fields
            if color[neighbour] == GRAY:
                # Found a cycle — extract the cycle portion of the path
                cycle_start = path.index(neighbour)
                cycle = path[cycle_start:] + [neighbour]
                result.errors.append(f"Dependency cycle detected: {' -> '.join(cycle)}")
                # Do NOT return early — continue so path.pop()/BLACK always execute
                continue
            elif color[neighbour] == WHITE:
                dfs(neighbour, path)
        path.pop()
        color[node] = BLACK

    for tid in all_ids:
        if color[tid] == WHITE:
            dfs(tid, [])

    return result


def validate_tasks(tasks_file: Path) -> ValidationResult:
    """Orchestrate all validation checks on a tasks file.

    Checks performed (in order):
    1. File exists
    2. File parses to at least one task
    3. Task fields are valid (status, priority, dep refs)
    4. No dependency cycles

    Args:
        tasks_file: Path to the tasks.md file.

    Returns:
        ValidationResult aggregating all checks.
    """
    result = ValidationResult()

    # 1. File exists
    if not tasks_file.exists():
        result.errors.append(f"Tasks file does not exist: {tasks_file}")
        return result

    # 2. Parse tasks (parse_tasks calls sys.exit on missing file,
    #    but we already checked existence above)
    tasks = parse_tasks(tasks_file)

    if not tasks:
        result.errors.append(f"No tasks found in {tasks_file}")
        return result

    # 3. Field validation (status, priority, dep refs)
    result.merge(validate_task_fields(tasks))

    # 4. Cycle detection
    result.merge(_detect_cycle(tasks))

    log.info(
        "validation_complete",
        file=str(tasks_file),
        errors=len(result.errors),
        warnings=len(result.warnings),
    )

    return result
