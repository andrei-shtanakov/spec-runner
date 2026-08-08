"""Core task model, parsing, and dependency resolution."""

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .logging import get_logger
from .spec import split_frontmatter_raw, strip_frontmatter

# Configuration
TASKS_FILE = Path("spec/tasks.md")
HISTORY_FILE = Path("spec/.task-history.log")

# Patterns.
# Task ids are `<PREFIX>-<number>` where PREFIX is any uppercase alnum run
# (#72: external projects use native numbering — KAP-002, ABC-17 — not just
# TASK-001). Headers are unambiguous (`### <id>: <name>` only introduces
# tasks); cross-refs in Depends/Blocks are captured generically and filtered
# after parsing against the prefixes actually used by task headers, so
# [REQ-001]/[DESIGN-001] mentioned in those lines never become dependencies.
ID_PATTERN = r"[A-Z][A-Z0-9]*-\d+"
TASK_HEADER = re.compile(rf"^### ({ID_PATTERN}): (.+)$")
TASK_REF = re.compile(rf"\[({ID_PATTERN})\]")
# Supports both emoji format "🔴 P0 | ⬜ TODO" and plain "P0 | TODO", each
# optionally preceded by a markdown bullet prefix ("- "/"* ", optionally
# indented — #123: agents editing tasks.md mid-run introduce the bullet
# prefix (forensic: git-status correlation on the incident snapshot; the
# generator templates themselves emit the bare form), and the old pattern
# didn't recognize it. The parser must accept both formats regardless of
# source. The bullet prefix requires the `P\d |` form right after it, so
# plain description bullets ("- some prose") and checklist items
# ("- [ ] ...") never match.
TASK_META = re.compile(
    r"^(?:[ \t]*[-*]\s+)?"
    r"(?:(?:🔴|🟠|🟡|🟢)\s+)?(P\d)\s*\|\s*(?:(?:⬜|🔄|🔍|✅|⏸️)\s+)?(\w+)"
)
CHECKLIST_ITEM = re.compile(r"^- \[([ x])\] (.+)$")
TRACES_TO = re.compile(r"\*\*Traces to:\*\* (.+)")
DEPENDS_ON = re.compile(r"\*\*Depends on:\*\* (.+)")
BLOCKS = re.compile(r"\*\*Blocks:\*\* (.+)")
ESTIMATE = re.compile(r"Est: (\d+(?:\.\d+)?(?:[-–]\d+(?:\.\d+)?)?[dh])")

# "review" (🔍, #66): gates passed, code review/commit still running — an
# interrupted run leaves this honest intermediate instead of a bare
# in_progress or a premature DONE. Scheduled like in_progress (resumable).
STATUS_EMOJI = {"todo": "⬜", "in_progress": "🔄", "review": "🔍", "done": "✅", "blocked": "⏸️"}

STATUS_FROM_EMOJI = {v: k for k, v in STATUS_EMOJI.items()}

PRIORITY_EMOJI = {"p0": "🔴", "p1": "🟠", "p2": "🟡", "p3": "🟢"}

PRIORITY_FROM_EMOJI = {v: k for k, v in PRIORITY_EMOJI.items()}


@dataclass
class Task:
    id: str
    name: str
    priority: str  # p0, p1, p2, p3
    status: str  # todo, in_progress, review, done, blocked
    estimate: str
    description: str = ""
    checklist: list = field(default_factory=list)
    traces_to: list = field(default_factory=list)
    depends_on: list = field(default_factory=list)
    blocks: list = field(default_factory=list)
    milestone: str = ""
    line_number: int = 0

    @property
    def checklist_progress(self) -> tuple[int, int]:
        done = sum(1 for item, checked in self.checklist if checked)
        return done, len(self.checklist)

    @property
    def is_ready(self) -> bool:
        """Task is ready to work if all dependencies are completed"""
        return self.status == "todo" and not self.depends_on


def parse_tasks(filepath: Path) -> list[Task]:
    """Parse tasks.md and return list of tasks"""
    if not filepath.exists():
        print(f"❌ File {filepath} not found")
        sys.exit(1)

    content = strip_frontmatter(filepath.read_text())
    lines = content.split("\n")

    tasks = []
    current_task = None
    current_milestone = ""
    in_checklist = False
    in_tests = False

    for i, line in enumerate(lines):
        # Determine milestone
        if line.startswith("## Milestone"):
            current_milestone = line.replace("## ", "").strip()
            continue

        # Start of new task
        header_match = TASK_HEADER.match(line)
        if header_match:
            if current_task:
                tasks.append(current_task)

            task_id, task_name = header_match.groups()
            current_task = Task(
                id=task_id,
                name=task_name,
                priority="p0",
                status="todo",
                estimate="",
                milestone=current_milestone,
                line_number=i + 1,
            )
            in_checklist = False
            in_tests = False
            continue

        if not current_task:
            continue

        # Metadata (priority, status)
        meta_match = TASK_META.match(line)
        if meta_match:
            priority, status_text = meta_match.groups()
            current_task.priority = priority.lower()
            current_task.status = status_text.lower()

            est_match = ESTIMATE.search(line)
            if est_match:
                current_task.estimate = est_match.group(1)
            continue

        # Description header (skip the label itself)
        if line.startswith("**Description:**"):
            continue

        # Capture description: plain text before any bold field or checklist
        if (
            line.strip()
            and not line.startswith("**")
            and not line.startswith("- [")
            and not in_checklist
            and not TASK_META.match(line)
        ):
            if current_task.description:
                current_task.description += "\n" + line.strip()
            else:
                current_task.description = line.strip()
            continue

        # Checklist section
        if line.startswith("**Checklist:**") or line.startswith("**Tests"):
            in_checklist = True
            in_tests = "Tests" in line
            continue

        # Checklist item
        if in_checklist:
            check_match = CHECKLIST_ITEM.match(line)
            if check_match:
                checked = check_match.group(1) == "x"
                text = check_match.group(2)
                prefix = "[TEST] " if in_tests else ""
                current_task.checklist.append((prefix + text, checked))
                continue
            elif line.strip() and not line.startswith("**"):
                continue
            elif line.startswith("**"):
                in_checklist = False
                in_tests = False

        # Traces, Dependencies
        traces_match = TRACES_TO.search(line)
        if traces_match:
            refs = re.findall(r"\[([A-Z]+-\d+)\]", traces_match.group(1))
            current_task.traces_to = refs
            continue

        depends_match = DEPENDS_ON.search(line)
        if depends_match:
            text = depends_match.group(1)
            if text.strip() != "—":
                current_task.depends_on = TASK_REF.findall(text)
            continue

        blocks_match = BLOCKS.search(line)
        if blocks_match:
            text = blocks_match.group(1)
            if text.strip() != "—":
                current_task.blocks = TASK_REF.findall(text)

    if current_task:
        tasks.append(current_task)

    # Keep only cross-refs that use a prefix some task header actually uses —
    # a [REQ-001] mentioned in a Depends line is documentation, not a task.
    prefixes = {t.id.rsplit("-", 1)[0] for t in tasks}
    for t in tasks:
        t.depends_on = [r for r in t.depends_on if r.rsplit("-", 1)[0] in prefixes]
        t.blocks = [r for r in t.blocks if r.rsplit("-", 1)[0] in prefixes]

    return tasks


def history_file_for(tasks_file: Path) -> Path:
    """Derive history log path from tasks file path.

    E.g. spec/phase5-tasks.md -> spec/.phase5-task-history.log
         spec/tasks.md        -> spec/.task-history.log
    """
    stem = tasks_file.stem  # e.g. "phase5-tasks" or "tasks"
    prefix = stem[: -len("tasks")] if stem.endswith("-tasks") else ""
    return tasks_file.parent / f".{prefix}task-history.log"


def log_change(task_id: str, change: str, history_file: Path = HISTORY_FILE):
    """Log change to history"""
    history_file.parent.mkdir(exist_ok=True)
    with open(history_file, "a") as f:
        timestamp = datetime.now().isoformat()
        f.write(f"{timestamp} | {task_id} | {change}\n")


def update_task_status(filepath: Path, task_id: str, new_status: str) -> bool:
    """Update task status in file.

    Fail-closed and task-bounded (#123): the target header must match
    `task_id` exactly (not as a substring — `TASK-001` must not match
    `TASK-0011`), and the meta line to rewrite is only searched for
    strictly between that header and the next `### <id>: ...` header (or
    EOF). If no meta line is found in that window, nothing is written and
    no history entry is logged — a neighboring task's meta is never
    mistaken for the target's.

    A half-state is possible: the write itself can land while the
    post-write confirm still fails (e.g. a concurrent edit shifts lines
    between the write and the re-read), returning False with no rollback
    of the write already made. Callers must treat False as a failure
    regardless of whether the write landed — never infer success from a
    False return just because the file was touched.
    """
    fm, content = split_frontmatter_raw(filepath.read_text())
    lines = content.split("\n")

    header_index = None
    for i, line in enumerate(lines):
        header_match = TASK_HEADER.match(line)
        if header_match and header_match.group(1) == task_id:
            header_index = i
            break

    if header_index is None:
        return False

    meta_index = None
    for j in range(header_index + 1, len(lines)):
        if TASK_HEADER.match(lines[j]):
            break
        if TASK_META.match(lines[j]):
            meta_index = j
            break

    if meta_index is None:
        return False

    line = lines[meta_index]

    # Replace status — supports both emoji and plain format
    new_emoji = STATUS_EMOJI[new_status]
    old_emoji = None
    for emoji in STATUS_EMOJI.values():
        if emoji in line:
            old_emoji = emoji
            break

    if old_emoji:
        # Emoji format: replace emoji and status text
        new_line = line.replace(old_emoji, new_emoji)
        new_line = re.sub(
            r"\| (⬜|🔄|🔍|✅|⏸️) \w+",
            f"| {new_emoji} {new_status.upper()}",
            new_line,
        )
    else:
        # Plain format (no emoji): inject emoji and update status text
        new_line = re.sub(
            r"\|\s*(TODO|IN_PROGRESS|REVIEW|DONE|BLOCKED)",
            f"| {new_emoji} {new_status.upper()}",
            line,
            count=1,
            flags=re.IGNORECASE,
        )
    lines[meta_index] = new_line

    filepath.write_text(fm + "\n".join(lines))
    log_change(
        task_id,
        f"status -> {new_status}",
        history_file_for(filepath),
    )

    # Re-read and confirm the write actually landed on the target task —
    # a write that silently missed its mark must not be reported as success.
    verify_tasks = parse_tasks(filepath)
    updated_task = get_task_by_id(verify_tasks, task_id)
    if updated_task is None or updated_task.status != new_status:
        get_logger("task").warning(
            "update_task_status: post-write verification failed",
            task_id=task_id,
            expected_status=new_status,
            actual_status=updated_task.status if updated_task else None,
        )
        return False

    return True


def update_checklist_item(filepath: Path, task_id: str, item_index: int, checked: bool) -> bool:
    """Update checklist item"""
    fm, content = split_frontmatter_raw(filepath.read_text())
    lines = content.split("\n")

    in_task = False
    checklist_count = 0

    for i, line in enumerate(lines):
        m = TASK_HEADER.match(line)
        if m:
            in_task = m.group(1) == task_id
            checklist_count = 0
            continue

        if in_task and CHECKLIST_ITEM.match(line):
            if checklist_count == item_index:
                mark = "x" if checked else " "
                new_line = re.sub(r"- \[[ x]\]", f"- [{mark}]", line)
                lines[i] = new_line
                filepath.write_text(fm + "\n".join(lines))
                log_change(
                    task_id,
                    f"checklist[{item_index}] -> {'done' if checked else 'undone'}",
                    history_file_for(filepath),
                )
                return True
            checklist_count += 1

    return False


def mark_all_checklist_done(filepath: Path, task_id: str) -> int:
    """Mark all checklist items as done for a task.

    Returns number of items marked.
    """
    fm, content = split_frontmatter_raw(filepath.read_text())
    lines = content.split("\n")

    in_task = False
    marked_count = 0

    for i, line in enumerate(lines):
        m = TASK_HEADER.match(line)
        if m:
            in_task = m.group(1) == task_id
            continue

        if in_task and CHECKLIST_ITEM.match(line) and "[ ]" in line:
            lines[i] = line.replace("[ ]", "[x]")
            marked_count += 1

    if marked_count > 0:
        filepath.write_text(fm + "\n".join(lines))
        log_change(
            task_id,
            f"checklist: marked {marked_count} items done",
            history_file_for(filepath),
        )

    return marked_count


def get_task_by_id(tasks: list[Task], task_id: str) -> Task | None:
    """Find task by ID"""
    for task in tasks:
        if task.id == task_id:
            return task
    return None


@dataclass
class TaskStatusDiff:
    """What changed between two task-status snapshots.

    Used by the pause/resume handlers (CLI and TUI) to tell the operator
    which parents finished while they were paused and which downstream
    tasks became runnable as a result. Empty diff = no visible change.
    """

    completed_parents: list[str] = field(default_factory=list)
    newly_ready: list[str] = field(default_factory=list)
    other_transitions: list[tuple[str, str, str]] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.completed_parents
            or self.newly_ready
            or self.other_transitions
            or self.added
            or self.removed
        )


def snapshot_task_statuses(tasks: list[Task]) -> dict[str, str]:
    """Return `{task_id: status}` — the minimal shape diffing needs."""
    return {task.id: task.status for task in tasks}


def diff_task_statuses(
    before: dict[str, str],
    after_tasks: list[Task],
) -> TaskStatusDiff:
    """Compare a pre-pause snapshot to the current task list.

    A "completed parent" is any id that went `* → done` and is listed in
    another current task's `depends_on`. A "newly ready" task is one that
    previously had unfinished deps (was `blocked` or had non-empty
    `depends_on`) but now has all deps satisfied.

    The caller should pass `after_tasks` **before** `resolve_dependencies`
    has mutated statuses, so the diff reflects the literal on-disk state.
    """
    diff = TaskStatusDiff()
    after_map = {t.id: t for t in after_tasks}

    # Index: task id → set of ids that depend on it (reverse edges).
    reverse_deps: dict[str, set[str]] = {}
    for task in after_tasks:
        for dep in task.depends_on:
            reverse_deps.setdefault(dep, set()).add(task.id)

    for task_id, before_status in before.items():
        if task_id not in after_map:
            diff.removed.append(task_id)
            continue
        after_status = after_map[task_id].status
        if before_status == after_status:
            continue
        if after_status == "done" and task_id in reverse_deps:
            diff.completed_parents.append(task_id)
        else:
            diff.other_transitions.append((task_id, before_status, after_status))

    for task_id in after_map:
        if task_id not in before:
            diff.added.append(task_id)

    # Newly-ready: task whose deps are now all satisfied (either because a
    # parent completed or a dep was removed from tasks.md).
    for task in after_tasks:
        if task.id not in before:
            continue
        was_blocked = before[task.id] in {"blocked", "todo"}
        unfinished_deps = [
            d for d in task.depends_on if d in after_map and after_map[d].status != "done"
        ]
        if was_blocked and not unfinished_deps and task.depends_on:
            # Had deps, now all satisfied (but `status` may still say blocked —
            # `resolve_dependencies` hasn't run yet).
            diff.newly_ready.append(task.id)

    diff.completed_parents.sort()
    diff.newly_ready.sort()
    diff.other_transitions.sort()
    diff.added.sort()
    diff.removed.sort()
    return diff


def format_task_status_diff(diff: TaskStatusDiff) -> str:
    """One-line (or short multi-line) human summary for CLI/TUI logs."""
    if diff.is_empty:
        return "no task changes while paused"

    parts: list[str] = []
    if diff.completed_parents:
        parts.append(f"completed: {', '.join(diff.completed_parents)}")
    if diff.newly_ready:
        parts.append(f"unblocked: {', '.join(diff.newly_ready)}")
    if diff.other_transitions:
        moves = ", ".join(f"{tid}({a}→{b})" for tid, a, b in diff.other_transitions)
        parts.append(f"moved: {moves}")
    if diff.added:
        parts.append(f"added: {', '.join(diff.added)}")
    if diff.removed:
        parts.append(f"removed: {', '.join(diff.removed)}")
    return " | ".join(parts)


def resolve_dependencies(tasks: list[Task]) -> list[Task]:
    """Update depends_on based on dependency status.

    Removes completed dependencies and promotes blocked tasks
    to todo when all their dependencies are done.
    """
    task_map = {t.id: t for t in tasks}

    for task in tasks:
        # Remove completed dependencies
        task.depends_on = [
            dep for dep in task.depends_on if dep in task_map and task_map[dep].status != "done"
        ]
        # Auto-promote: blocked → todo when all deps satisfied
        if task.status == "blocked" and not task.depends_on:
            task.status = "todo"

    return tasks


def get_in_progress_tasks(tasks: list[Task]) -> list[Task]:
    """Return tasks that are currently in progress (interrupted/incomplete).

    These should be resumed before starting new tasks.
    """
    in_progress = [t for t in tasks if t.status in ("in_progress", "review")]
    priority_order = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}
    in_progress.sort(key=lambda t: priority_order.get(t.priority, 99))
    return in_progress


def get_next_tasks(tasks: list[Task], include_in_progress: bool = True) -> list[Task]:
    """Return tasks ready to execute.

    Args:
        tasks: List of all tasks
        include_in_progress: If True, in_progress tasks are returned first (default).
                            Set to False to only get TODO tasks.

    Returns:
        List of tasks ready to execute, with in_progress tasks first (if enabled),
        then TODO tasks with resolved dependencies, sorted by priority.
    """
    result = []

    # First, add in_progress tasks (interrupted tasks should be resumed first)
    if include_in_progress:
        result.extend(get_in_progress_tasks(tasks))

    # Then add TODO tasks with resolved dependencies
    tasks = resolve_dependencies(tasks)
    ready = [t for t in tasks if t.status == "todo" and not t.depends_on]
    priority_order = {"p0": 0, "p1": 1, "p2": 2, "p3": 3}
    ready.sort(key=lambda t: priority_order.get(t.priority, 99))
    result.extend(ready)

    return result
