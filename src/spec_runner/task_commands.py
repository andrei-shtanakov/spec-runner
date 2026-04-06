#!/usr/bin/env python3
"""
spec-task — CLI for managing tasks from tasks.md

Usage:
    spec-task list                    # List all tasks
    spec-task list --status=todo      # Filter by status
    spec-task list --priority=p0      # Filter by priority
    spec-task list --milestone=mvp    # Filter by milestone
    spec-task show TASK-001           # Task details
    spec-task start TASK-001          # Start task
    spec-task done TASK-001           # Complete task
    spec-task block TASK-001          # Block task
    spec-task check TASK-001 2        # Mark checklist item
    spec-task stats                   # Statistics
    spec-task next                    # Next task (by dependencies)
    spec-task graph                   # ASCII dependency graph
    spec-task export-gh               # Export to GitHub Issues
    spec-task sync-to-gh              # Sync tasks to GitHub Issues
    spec-task sync-from-gh            # Sync GitHub Issues to tasks.md
"""

import argparse
import re
from pathlib import Path

from .github_sync import cmd_sync_from_gh, cmd_sync_to_gh, export_gh
from .task import (
    PRIORITY_EMOJI,
    STATUS_EMOJI,
    TASKS_FILE,
    Task,
    get_next_tasks,
    get_task_by_id,
    parse_tasks,
    resolve_dependencies,
    update_checklist_item,
    update_task_status,
)

# === CLI Commands ===


def cmd_list(args, tasks: list[Task]):
    """List tasks"""
    filtered = tasks

    if args.status:
        filtered = [t for t in filtered if t.status == args.status]

    if args.priority:
        filtered = [t for t in filtered if t.priority == args.priority.lower()]

    if args.milestone:
        milestone_lower = args.milestone.lower()
        filtered = [t for t in filtered if milestone_lower in t.milestone.lower()]

    if not filtered:
        print("No tasks matching criteria")
        return

    header = f"\n{'ID':<12} {'Status':<4} {'P':<3} {'Name':<40} {'Progress':<10} {'Est':<6}"
    print(header)
    print("-" * 85)

    for task in filtered:
        done, total = task.checklist_progress
        progress = f"{done}/{total}" if total > 0 else "—"
        status_icon = STATUS_EMOJI.get(task.status, "?")
        priority_icon = PRIORITY_EMOJI.get(task.priority, "?")

        name = task.name[:38] + ".." if len(task.name) > 40 else task.name
        line = (
            f"{task.id:<12} {status_icon:<4} {priority_icon:<3} "
            f"{name:<40} {progress:<10} {task.estimate:<6}"
        )
        print(line)

    print(f"\nTotal: {len(filtered)} tasks")


def cmd_show(args, tasks: list[Task]):
    """Task details"""
    task = get_task_by_id(tasks, args.task_id.upper())
    if not task:
        print(f"❌ Task {args.task_id} not found")
        return

    status_icon = STATUS_EMOJI.get(task.status, "?")
    priority_icon = PRIORITY_EMOJI.get(task.priority, "?")
    done, total = task.checklist_progress

    print(f"\n{'=' * 60}")
    print(f"{priority_icon} {task.id}: {task.name}")
    print(f"{'=' * 60}")
    print(f"Status:     {status_icon} {task.status.upper()}")
    print(f"Priority:   {task.priority.upper()}")
    print(f"Milestone:  {task.milestone}")
    print(f"Estimate:   {task.estimate or '—'}")
    print(f"Progress:   {done}/{total} ({done * 100 // total if total else 0}%)")

    if task.depends_on:
        print(f"\n⬅️  Depends on: {', '.join(task.depends_on)}")
    if task.blocks:
        print(f"➡️  Blocks:     {', '.join(task.blocks)}")
    if task.traces_to:
        print(f"📋 Traces to:  {', '.join(task.traces_to)}")

    if task.checklist:
        print("\n📝 Checklist:")
        for i, (item, checked) in enumerate(task.checklist):
            mark = "✅" if checked else "⬜"
            print(f"   {i}. {mark} {item}")


def cmd_start(args, tasks: list[Task], tasks_file: Path = TASKS_FILE):
    """Start task"""
    task = get_task_by_id(tasks, args.task_id.upper())
    if not task:
        print(f"❌ Task {args.task_id} not found")
        return

    # Check dependencies
    tasks = resolve_dependencies(tasks)
    task = get_task_by_id(tasks, args.task_id.upper())
    if not task:
        print(f"❌ Task {args.task_id} not found after resolving")
        return

    if task.depends_on:
        print(f"⚠️  Task depends on incomplete: {', '.join(task.depends_on)}")
        if not args.force:
            print("   Use --force to start anyway")
            return

    if update_task_status(tasks_file, task.id, "in_progress"):
        print(f"🔄 {task.id} started!")
    else:
        print("❌ Failed to update status")


def cmd_done(args, tasks: list[Task], tasks_file: Path = TASKS_FILE):
    """Complete task"""
    task = get_task_by_id(tasks, args.task_id.upper())
    if not task:
        print(f"❌ Task {args.task_id} not found")
        return

    # Check checklist
    done, total = task.checklist_progress
    if total > 0 and done < total:
        print(f"⚠️  Checklist incomplete: {done}/{total}")
        if not args.force:
            print("   Use --force to complete anyway")
            return

    if update_task_status(tasks_file, task.id, "done"):
        print(f"✅ {task.id} completed!")

        # Show unblocked tasks
        tasks = parse_tasks(tasks_file)
        tasks = resolve_dependencies(tasks)
        unblocked = [t for t in tasks if t.status == "todo" and not t.depends_on]
        if unblocked:
            print("\n🔓 Unblocked tasks:")
            for t in unblocked[:5]:
                print(f"   {t.id}: {t.name}")
    else:
        print("❌ Failed to update status")


def cmd_block(args, tasks: list[Task], tasks_file: Path = TASKS_FILE):
    """Block task"""
    task = get_task_by_id(tasks, args.task_id.upper())
    if not task:
        print(f"❌ Task {args.task_id} not found")
        return

    if update_task_status(tasks_file, task.id, "blocked"):
        print(f"⏸️ {task.id} blocked")
    else:
        print("❌ Failed to update status")


def cmd_check(args, tasks: list[Task], tasks_file: Path = TASKS_FILE):
    """Mark checklist item"""
    task = get_task_by_id(tasks, args.task_id.upper())
    if not task:
        print(f"❌ Task {args.task_id} not found")
        return

    item_index = int(args.item_index)
    if item_index < 0 or item_index >= len(task.checklist):
        print(f"❌ Invalid index. Available: 0-{len(task.checklist) - 1}")
        return

    item_text, was_checked = task.checklist[item_index]
    new_checked = not was_checked  # toggle

    if update_checklist_item(tasks_file, task.id, item_index, new_checked):
        mark = "✅" if new_checked else "⬜"
        print(f"{mark} {item_text}")
    else:
        print("❌ Failed to update checklist")


def cmd_stats(args, tasks: list[Task]):
    """Task statistics"""
    tasks = resolve_dependencies(tasks)

    by_status: dict[str, int] = {}
    by_priority: dict[str, int] = {}
    by_milestone: dict[str, int] = {}
    total_estimate = 0

    for task in tasks:
        by_status[task.status] = by_status.get(task.status, 0) + 1
        by_priority[task.priority] = by_priority.get(task.priority, 0) + 1
        by_milestone[task.milestone] = by_milestone.get(task.milestone, 0) + 1

        # Parse estimate
        if task.estimate:
            match = re.match(r"(\d+)", task.estimate)
            if match:
                total_estimate += int(match.group(1))

    print("\n📊 Task Statistics")
    print("=" * 40)

    print("\nBy status:")
    for status, count in sorted(by_status.items()):
        icon = STATUS_EMOJI.get(status, "?")
        pct = count * 100 // len(tasks)
        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
        print(f"  {icon} {status:<12} {count:>3} {bar} {pct}%")

    print("\nBy priority:")
    for priority in ["p0", "p1", "p2", "p3"]:
        count = by_priority.get(priority, 0)
        icon = PRIORITY_EMOJI.get(priority, "?")
        print(f"  {icon} {priority.upper():<3} {count:>3}")

    print("\nBy milestone:")
    for milestone, count in sorted(by_milestone.items()):
        print(f"  {milestone:<25} {count:>3}")

    ready = get_next_tasks(tasks)
    print(f"\n🚀 Ready to work: {len(ready)}")
    for t in ready[:3]:
        print(f"   {PRIORITY_EMOJI[t.priority]} {t.id}: {t.name}")

    done_count = by_status.get("done", 0)
    progress = done_count * 100 // len(tasks) if tasks else 0
    print(f"\n📈 Overall progress: {done_count}/{len(tasks)} ({progress}%)")
    print(f"⏱️  Total estimate: ~{total_estimate}d")


def cmd_next(args, tasks: list[Task]):
    """Next task to work on"""
    ready = get_next_tasks(tasks)

    if not ready:
        in_progress = [t for t in tasks if t.status == "in_progress"]
        if in_progress:
            print("🔄 Currently in progress:")
            for t in in_progress:
                done, total = t.checklist_progress
                print(f"   {t.id}: {t.name} ({done}/{total})")
        else:
            print("🎉 All tasks completed or blocked!")
        return

    print("🚀 Next tasks (ready to work):\n")
    for i, task in enumerate(ready[:5], 1):
        icon = PRIORITY_EMOJI.get(task.priority, "?")
        deps_done = "✓ deps OK" if not task.depends_on else ""
        print(f"{i}. {icon} {task.id}: {task.name}")
        print(f"   Est: {task.estimate or '?'} | {task.milestone} {deps_done}")
        if task.checklist:
            print(f"   Checklist: {len(task.checklist)} items")
        print()


def cmd_graph(args, tasks: list[Task]):
    """ASCII dependency graph"""
    print("\n📊 Dependency Graph\n")

    # Find roots (no dependencies)
    roots = [t for t in tasks if not t.depends_on]

    def print_tree(task_id: str, indent: int = 0, visited: set | None = None):
        if visited is None:
            visited = set()

        if task_id in visited:
            return
        visited.add(task_id)

        task = get_task_by_id(tasks, task_id)
        if not task:
            return

        prefix = "  " * indent + ("├── " if indent > 0 else "")
        status_icon = STATUS_EMOJI.get(task.status, "?")

        print(f"{prefix}{status_icon} {task.id}: {task.name[:30]}")

        # Find tasks that depend on this one
        dependents = [t for t in tasks if task_id in t.depends_on]
        for dep in dependents:
            print_tree(dep.id, indent + 1, visited)

    for root in roots[:10]:  # Limit output
        print_tree(root.id)
        print()


def main():
    # Shared options available to every subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--spec-prefix",
        type=str,
        default="",
        help='Spec file prefix (e.g. "phase5-" for phase5-tasks.md)',
    )

    parser = argparse.ArgumentParser(
        description="spec-task — manage tasks from tasks.md",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
        parents=[common],
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # list
    list_parser = subparsers.add_parser("list", aliases=["ls"], parents=[common], help="List tasks")
    list_parser.add_argument("--status", "-s", choices=["todo", "in_progress", "done", "blocked"])
    list_parser.add_argument("--priority", "-p", choices=["p0", "p1", "p2", "p3"])
    list_parser.add_argument("--milestone", "-m", help="Filter by milestone")

    # show
    show_parser = subparsers.add_parser("show", parents=[common], help="Task details")
    show_parser.add_argument("task_id", help="Task ID (e.g., TASK-001)")

    # start
    start_parser = subparsers.add_parser("start", parents=[common], help="Start task")
    start_parser.add_argument("task_id", help="Task ID")
    start_parser.add_argument("--force", "-f", action="store_true", help="Ignore dependencies")

    # done
    done_parser = subparsers.add_parser("done", parents=[common], help="Complete task")
    done_parser.add_argument("task_id", help="Task ID")
    done_parser.add_argument(
        "--force", "-f", action="store_true", help="Ignore incomplete checklist"
    )

    # block
    block_parser = subparsers.add_parser("block", parents=[common], help="Block task")
    block_parser.add_argument("task_id", help="Task ID")

    # check
    check_parser = subparsers.add_parser("check", parents=[common], help="Mark checklist item")
    check_parser.add_argument("task_id", help="Task ID")
    check_parser.add_argument("item_index", help="Item index (0, 1, 2...)")

    # stats
    subparsers.add_parser("stats", parents=[common], help="Statistics")

    # next
    subparsers.add_parser("next", parents=[common], help="Next tasks")

    # graph
    subparsers.add_parser("graph", parents=[common], help="Dependency graph")

    # export-gh
    subparsers.add_parser("export-gh", parents=[common], help="Export to GitHub Issues")

    # sync-to-gh
    sync_to_parser = subparsers.add_parser(
        "sync-to-gh", parents=[common], help="Sync tasks to GitHub Issues"
    )
    sync_to_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would happen without making changes"
    )

    # sync-from-gh
    subparsers.add_parser(
        "sync-from-gh", parents=[common], help="Sync GitHub Issues state to tasks.md"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    tasks_file = Path(f"spec/{args.spec_prefix}tasks.md") if args.spec_prefix else TASKS_FILE
    tasks = parse_tasks(tasks_file)

    # Commands that modify the tasks file need tasks_file passed
    write_commands = {
        "start": cmd_start,
        "done": cmd_done,
        "block": cmd_block,
        "check": cmd_check,
        "sync-from-gh": cmd_sync_from_gh,
    }
    read_commands = {
        "list": cmd_list,
        "ls": cmd_list,
        "show": cmd_show,
        "stats": cmd_stats,
        "next": cmd_next,
        "graph": cmd_graph,
        "export-gh": export_gh,
        "sync-to-gh": cmd_sync_to_gh,
    }

    if args.command in write_commands:
        write_commands[args.command](args, tasks, tasks_file)
    elif args.command in read_commands:
        read_commands[args.command](args, tasks)


if __name__ == "__main__":
    main()
