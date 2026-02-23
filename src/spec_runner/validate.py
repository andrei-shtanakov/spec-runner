"""Validation for tasks.md — field checks, dependency refs, cycle detection, and config validation."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from spec_runner.config import ExecutorConfig
from spec_runner.logging import get_logger
from spec_runner.task import Task, parse_tasks

log = get_logger("validate")

VALID_STATUSES = {"todo", "in_progress", "done", "blocked"}
VALID_PRIORITIES = {"p0", "p1", "p2", "p3"}

# Known keys allowed under the executor: section in config YAML.
# Built from ExecutorConfig dataclass fields plus nested config sections.
KNOWN_EXECUTOR_KEYS: set[str] = set(ExecutorConfig.__dataclass_fields__.keys()) | {
    "hooks",
    "commands",
    "paths",
}


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

    Also emits warnings for missing estimates, blocked tasks without
    dependencies, and tasks without traceability references.

    Args:
        tasks: Parsed task list.

    Returns:
        ValidationResult with errors and warnings found.
    """
    result = ValidationResult()
    task_ids = {t.id for t in tasks}

    for task in tasks:
        # --- Errors ---
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

        # --- Warnings ---
        if not task.estimate:
            result.warnings.append(f"{task.id}: missing estimate")

        if task.status == "blocked" and not task.depends_on:
            result.warnings.append(f"{task.id}: status is blocked but has no dependencies")

        if not task.traces_to:
            result.warnings.append(f"{task.id}: no traceability references")

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


def _levenshtein(s1: str, s2: str) -> int:
    """Compute the Levenshtein (edit) distance between two strings."""
    if len(s1) < len(s2):
        return _levenshtein(s2, s1)

    if not s2:
        return len(s1)

    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            # Insertion, deletion, substitution
            cost = 0 if c1 == c2 else 1
            curr_row.append(min(curr_row[j] + 1, prev_row[j + 1] + 1, prev_row[j] + cost))
        prev_row = curr_row

    return prev_row[-1]


def _suggest_key(unknown: str, known: set[str]) -> str | None:
    """Suggest the closest known key if Levenshtein distance <= 2.

    Args:
        unknown: The unrecognised key.
        known: Set of valid key names.

    Returns:
        The best suggestion, or None if nothing is close enough.
    """
    best: str | None = None
    best_dist = 3  # threshold: only suggest if distance <= 2
    for k in sorted(known):  # sorted for deterministic results
        d = _levenshtein(unknown, k)
        if d < best_dist:
            best = k
            best_dist = d
    return best


def validate_config(config_path: Path) -> ValidationResult:
    """Validate an executor config YAML file.

    Checks:
    - File exists (missing = ok, use defaults)
    - YAML is parseable
    - Keys under ``executor:`` are recognised

    Args:
        config_path: Path to the YAML config file.

    Returns:
        ValidationResult with any errors found.
    """
    result = ValidationResult()

    if not config_path.exists():
        return result  # missing config is fine — defaults apply

    raw = config_path.read_text()
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        result.errors.append(f"Failed to parse YAML in {config_path}: {exc}")
        return result

    if not isinstance(data, dict):
        return result

    executor_section = data.get("executor")
    if not isinstance(executor_section, dict):
        return result

    for key in executor_section:
        if key not in KNOWN_EXECUTOR_KEYS:
            suggestion = _suggest_key(key, KNOWN_EXECUTOR_KEYS)
            msg = f"Unknown config key 'executor.{key}'"
            if suggestion:
                msg += f" — did you mean '{suggestion}'?"
            result.errors.append(msg)

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


def validate_all(
    tasks_file: Path | None = None,
    config_file: Path | None = None,
) -> ValidationResult:
    """Run all validation checks.

    Args:
        tasks_file: Path to tasks.md (optional).
        config_file: Path to executor config YAML (optional).

    Returns:
        Merged ValidationResult from all checks.
    """
    result = ValidationResult()
    if tasks_file:
        result.merge(validate_tasks(tasks_file))
    if config_file:
        result.merge(validate_config(config_file))
    return result


def format_results(result: ValidationResult) -> str:
    """Format validation results for terminal output.

    Args:
        result: ValidationResult to format.

    Returns:
        Human-readable string with errors, warnings, and summary.
    """
    lines: list[str] = []
    if result.errors:
        for e in result.errors:
            lines.append(f"  x {e}")
    if result.warnings:
        if lines:
            lines.append("")
        for w in result.warnings:
            lines.append(f"  ! {w}")
    n_err = len(result.errors)
    n_warn = len(result.warnings)
    err_word = "error" if n_err == 1 else "errors"
    warn_word = "warning" if n_warn == 1 else "warnings"
    lines.append(f"\n{n_err} {err_word}, {n_warn} {warn_word}")
    return "\n".join(lines)
