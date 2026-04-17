"""CLI commands and argument parsing for spec-runner."""

import argparse
import json
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

# Re-exports from submodules for backward compatibility
from .cli_info import (  # noqa: E402, F401
    cmd_audit,
    cmd_costs,
    cmd_logs,
    cmd_mcp,
    cmd_report,
    cmd_reset,
    cmd_status,
    cmd_stop,
    cmd_tui,
    cmd_validate,
    cmd_verify,
)
from .cli_plan import cmd_plan  # noqa: E402, F401
from .config import (
    ExecutorConfig,
    ExecutorLock,
    _resolve_config_path,
    build_config,
    load_config_from_yaml,
)
from .execution import (
    execute_task,
    run_with_retries,
)
from .git_ops import ensure_on_main_branch
from .logging import get_logger
from .runner import (
    log_progress,
)
from .state import (
    ExecutorState,
    check_stop_requested,
    clear_stop_file,
    recover_stale_tasks,
)
from .task import (
    Task,
    diff_task_statuses,
    format_task_status_diff,
    get_next_tasks,
    get_task_by_id,
    mark_all_checklist_done,
    parse_tasks,
    resolve_dependencies,
    snapshot_task_statuses,
    update_task_status,
)
from .validate import format_results, validate_all

logger = get_logger("cli")


# === CLI Commands ===


def build_task_json_result(task_id: str, state: ExecutorState) -> dict:
    """Build a single task's `--json-result` entry.

    Stable contract: see docs/state-schema.md and schemas/json-result.schema.json.
    Golden-fixed by tests/test_json_result_contract.py. Any change here is a
    breaking change requiring a major version bump.
    """
    ts = state.get_task_state(task_id)
    entry: dict = {"task_id": task_id, "status": "unknown", "attempts": 0}
    if not ts:
        return entry
    entry["status"] = "done" if ts.status == "success" else "failed"
    entry["attempts"] = ts.attempt_count
    entry["cost_usd"] = round(state.task_cost(task_id), 2)
    inp_t = sum(a.input_tokens or 0 for a in ts.attempts)
    out_t = sum(a.output_tokens or 0 for a in ts.attempts)
    entry["tokens"] = {"input": inp_t, "output": out_t}
    total_dur = sum(a.duration_seconds for a in ts.attempts)
    entry["duration_seconds"] = round(total_dur, 1)
    if ts.attempts:
        last = ts.attempts[-1]
        entry["review"] = last.review_status or "skipped"
        if last.error:
            entry["error"] = last.error[:200]
    entry["exit_code"] = 0 if ts.status == "success" else 1
    return entry


def _print_dry_run(tasks_to_run: list[Task], config: ExecutorConfig, state: ExecutorState) -> None:
    """Print what tasks would execute without running them."""
    data = []
    for t in tasks_to_run:
        entry = {
            "task_id": t.id,
            "name": t.name,
            "priority": t.priority,
            "status": t.status,
            "depends_on": t.depends_on,
            "checklist_total": len(t.checklist),
            "checklist_done": sum(1 for done, _ in t.checklist if done),
        }
        ts = state.get_task_state(t.id)
        if ts:
            entry["previous_attempts"] = ts.attempt_count
            entry["previous_cost_usd"] = round(state.task_cost(t.id), 2)
        data.append(entry)

    print(json.dumps({"dry_run": True, "tasks": data}, indent=2))


def cmd_run(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Execute tasks."""
    # HITL review incompatible with TUI mode
    if config.hitl_review and getattr(args, "tui", False):
        logger.warning("--hitl-review ignored in TUI mode (TUI owns the screen)")
        config.hitl_review = False

    if getattr(args, "tui", False):
        import threading

        from .logging import setup_logging
        from .tui import SpecRunnerApp

        # TUI mode: log to file, TUI owns screen
        log_file = config.logs_dir / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        setup_logging(level=config.log_level, tui_mode=True, log_file=log_file)

        app = SpecRunnerApp(config=config)

        def _start_execution() -> None:
            t = threading.Thread(target=lambda: _run_tasks(args, config), daemon=True)
            t.start()

        app.call_later(_start_execution)
        app.run()
        return

    if getattr(args, "force", False):
        logger.warning("Skipping lock check (--force)")
        _run_tasks(args, config)
    else:
        # Acquire lock to prevent concurrent runs
        lock = ExecutorLock(config.state_file.with_suffix(".lock"))
        if not lock.acquire():
            held_by = getattr(lock, "_held_by", {})
            pid = held_by.get("pid", "unknown")
            started = held_by.get("started", "unknown")
            alive = held_by.get("alive", "true")

            logger.error(
                "Another executor is already running",
                lock_file=str(config.state_file.with_suffix(".lock")),
                held_by_pid=pid,
                started=started,
                process_alive=alive,
            )
            if alive == "false":
                logger.error(
                    "Lock holder is dead. Use --force to override, "
                    "or delete the lock file manually."
                )
            sys.exit(1)

        try:
            _run_tasks(args, config)
        finally:
            lock.release()


def _run_tasks(args, config: ExecutorConfig):
    """Internal task execution logic."""
    # Clear any leftover stop file from previous runs
    clear_stop_file(config)

    tasks = parse_tasks(config.tasks_file)

    with ExecutorState(config) as state:
        # Recover tasks stuck in 'running' from previous crash
        stale_timeout = config.task_timeout_minutes * 2
        recovered = recover_stale_tasks(state, stale_timeout, config.tasks_file)
        if recovered:
            logger.warning("Recovered stale tasks", task_ids=recovered)
            tasks = parse_tasks(config.tasks_file)

        # Pre-run validation
        from .validate import format_results, validate_all

        pre_result = validate_all(
            tasks_file=config.tasks_file,
            config_file=_resolve_config_path(),
        )
        if not pre_result.ok:
            logger.error("Validation failed before execution")
            print(format_results(pre_result))
            return

        # Check failure limit
        if state.should_stop():
            logger.error(
                "Stopped due to consecutive failures",
                consecutive_failures=state.consecutive_failures,
            )
            return

        # Determine which tasks to execute
        if args.task:
            # Specific task
            task = get_task_by_id(tasks, args.task.upper())
            if not task:
                logger.error("Task not found", task_id=args.task)
                return
            tasks_to_run = [task]

        elif args.all:
            # All ready tasks (include in_progress unless --restart)
            include_in_progress = not getattr(args, "restart", False)
            tasks_to_run = get_next_tasks(tasks, include_in_progress=include_in_progress)
            if args.milestone:
                tasks_to_run = [
                    t for t in tasks_to_run if args.milestone.lower() in t.milestone.lower()
                ]

        elif args.milestone:
            # Tasks for specific milestone
            include_in_progress = not getattr(args, "restart", False)
            next_tasks = get_next_tasks(tasks, include_in_progress=include_in_progress)
            tasks_to_run = [t for t in next_tasks if args.milestone.lower() in t.milestone.lower()]

        else:
            # Next task (include in_progress unless --restart)
            include_in_progress = not getattr(args, "restart", False)
            next_tasks = get_next_tasks(tasks, include_in_progress=include_in_progress)
            tasks_to_run = next_tasks[:1] if next_tasks else []

        if not tasks_to_run:
            logger.info("No tasks ready to execute")
            if getattr(args, "json_result", False):
                print(json.dumps({"tasks": [], "message": "No tasks ready to execute"}))
            return

        # --dry-run: show what would execute and exit
        if getattr(args, "dry_run", False):
            _print_dry_run(tasks_to_run, config, state)
            return

        logger.info("Tasks to execute", count=len(tasks_to_run))
        for t in tasks_to_run:
            logger.info("Queued task", task_id=t.id, name=t.name)

        # Execute
        if args.all:
            # For --all mode, continuously re-evaluate ready tasks after each completion
            executed_ids: set[str] = set()
            include_in_progress = not getattr(args, "restart", False)
            session_start = time.monotonic()
            last_activity = time.monotonic()
            while True:
                # Check for pause request (SIGQUIT / Ctrl+\)
                from .executor import _pause_requested

                if _pause_requested:
                    import spec_runner.executor as _executor_mod

                    _executor_mod._pause_requested = False
                    pause_snapshot = snapshot_task_statuses(tasks)
                    log_progress(
                        "⏸️ Paused. Edit spec/tasks.md, then press Enter to resume (q to quit)."
                    )
                    choice = input("> ").strip().lower()
                    if choice == "q":
                        break
                    # Re-parse tasks to pick up edits AND external changes made
                    # while we were paused (another session, Maestro, manual
                    # edits). Diff against the pre-pause snapshot so the
                    # operator can see newly-completed parents and downstream
                    # tasks that just became ready — LABS-38.
                    tasks = parse_tasks(config.tasks_file)
                    diff = diff_task_statuses(pause_snapshot, tasks)
                    executed_ids.clear()
                    logger.info(
                        "Resumed after pause, tasks re-read",
                        changes=format_task_status_diff(diff),
                    )
                    if not diff.is_empty:
                        log_progress(f"▶️ {format_task_status_diff(diff)}")

                # Check for graceful shutdown request
                if check_stop_requested(config):
                    clear_stop_file(config)
                    logger.info("Graceful shutdown requested")
                    log_progress("🛑 Graceful shutdown requested")
                    break

                # Session timeout check
                if config.session_timeout_minutes > 0:
                    elapsed = (time.monotonic() - session_start) / 60
                    if elapsed >= config.session_timeout_minutes:
                        logger.warning(
                            "Session timeout reached",
                            elapsed_minutes=round(elapsed, 1),
                            limit_minutes=config.session_timeout_minutes,
                        )
                        break

                # Idle timeout check
                if config.idle_timeout_minutes > 0:
                    idle = (time.monotonic() - last_activity) / 60
                    if idle >= config.idle_timeout_minutes:
                        logger.warning(
                            "Idle timeout reached",
                            idle_minutes=round(idle, 1),
                            limit_minutes=config.idle_timeout_minutes,
                        )
                        break

                # Re-parse tasks to get updated statuses
                tasks = parse_tasks(config.tasks_file)
                ready_tasks = get_next_tasks(tasks, include_in_progress=include_in_progress)

                # Filter by milestone if specified
                if args.milestone:
                    ready_tasks = [
                        t for t in ready_tasks if args.milestone.lower() in t.milestone.lower()
                    ]

                # Filter out already executed tasks
                ready_tasks = [t for t in ready_tasks if t.id not in executed_ids]

                if not ready_tasks:
                    # Show why we're stopping
                    all_tasks = parse_tasks(config.tasks_file)
                    todo_tasks = [t for t in all_tasks if t.status == "todo"]
                    if todo_tasks:
                        blocked_info = {
                            t.id: ", ".join(t.depends_on) if t.depends_on else "none"
                            for t in todo_tasks
                        }
                        logger.info(
                            "No more ready tasks",
                            blocked_count=len(todo_tasks),
                            blocked_tasks=blocked_info,
                        )
                    else:
                        logger.info("All tasks completed")
                        # Ensure we're on main branch at the end
                        ensure_on_main_branch(config)
                    break

                task = ready_tasks[0]
                executed_ids.add(task.id)

                logger.info("Next ready task", task_id=task.id, name=task.name)

                result = run_with_retries(task, config, state)
                last_activity = time.monotonic()

                # "SKIP" means continue to next task
                if result == "SKIP":
                    continue

                if result is False and state.should_stop():
                    logger.warning("Stopping: too many consecutive failures")
                    break
        else:
            # For single task or milestone mode, execute the fixed list
            for task in tasks_to_run:
                # Check for graceful shutdown request
                if check_stop_requested(config):
                    clear_stop_file(config)
                    logger.info("Graceful shutdown requested")
                    log_progress("🛑 Graceful shutdown requested")
                    break

                result = run_with_retries(task, config, state)

                if result == "SKIP":
                    continue

                if result is False and state.should_stop():
                    logger.warning("Stopping: too many consecutive failures")
                    break

        # Summary
        # Re-read tasks to get updated statuses after execution
        tasks = parse_tasks(config.tasks_file)

        # Calculate statistics
        failed_attempts = sum(
            1 for ts in state.tasks.values() for a in ts.attempts if not a.success
        )
        remaining = len([t for t in tasks if t.status == "todo"])

        logger.info(
            "Execution summary",
            completed=state.total_completed,
            failed=state.total_failed,
            remaining=remaining,
            failed_attempts=failed_attempts if failed_attempts > 0 else None,
        )

        # Notify run completion
        from .notifications import notify_run_complete

        total_cost_val = state.total_cost()
        notify_run_complete(
            config,
            completed=state.total_completed,
            failed=state.total_failed,
            total_cost=total_cost_val if total_cost_val > 0 else None,
        )

        # --json-result: structured JSON result per task (for Maestro interop)
        if getattr(args, "json_result", False):
            results = [build_task_json_result(t.id, state) for t in tasks_to_run]
            print(json.dumps(results if len(results) > 1 else results[0], indent=2))


def cmd_retry(args, config: ExecutorConfig):
    """Retry failed task, preserving error context from previous attempts."""

    tasks = parse_tasks(config.tasks_file)

    with ExecutorState(config) as state:
        task = get_task_by_id(tasks, args.task_id.upper())
        if not task:
            logger.error("Task not found", task_id=args.task_id)
            return

        task_state = state.get_task_state(task.id)

        # Handle --fresh flag
        if hasattr(args, "fresh") and args.fresh:
            logger.info("Fresh start: clearing previous attempts", task_id=task.id)
            task_state.attempts = []
        else:
            # Keep previous attempts for context (Claude will see past errors)
            previous_attempts = len(task_state.attempts)
            if previous_attempts > 0:
                logger.info(
                    "Preserving previous attempts for context",
                    task_id=task.id,
                    previous_attempts=previous_attempts,
                    last_error=task_state.last_error[:100] if task_state.last_error else None,
                )

        # Only reset status and failure counter
        task_state.status = "pending"
        state.consecutive_failures = 0
        state._save()

        logger.info("Retrying task", task_id=task.id)

        # Execute single attempt (not run_with_retries which has max_retries limit)
        success = execute_task(task, config, state)

        if success:
            update_task_status(config.tasks_file, task.id, "done")
            mark_all_checklist_done(config.tasks_file, task.id)
        else:
            update_task_status(config.tasks_file, task.id, "blocked")


def cmd_watch(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Continuously watch tasks.md and execute ready tasks."""
    # Pre-run validation
    pre_result = validate_all(
        tasks_file=config.tasks_file,
        config_file=_resolve_config_path(),
    )
    if not pre_result.ok:
        logger.error("Validation failed before watch")
        print(format_results(pre_result))
        return

    # TUI mode
    if getattr(args, "tui", False):
        import threading

        from .logging import setup_logging
        from .tui import SpecRunnerApp

        log_file = config.logs_dir / f"watch-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        setup_logging(level=config.log_level, tui_mode=True, log_file=log_file)

        app = SpecRunnerApp(config=config)

        def _start_watch() -> None:
            def watch_loop() -> None:
                consecutive_failures = 0
                while True:
                    if check_stop_requested(config):
                        break
                    if consecutive_failures >= config.max_consecutive_failures:
                        break
                    tasks = parse_tasks(config.tasks_file)
                    tasks = resolve_dependencies(tasks)
                    ready = get_next_tasks(tasks)
                    if not ready:
                        time.sleep(5)
                        continue
                    task = ready[0]
                    with ExecutorState(config) as state:
                        result = run_with_retries(task, config, state)
                    if result is True:
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                    time.sleep(1)

            t = threading.Thread(target=watch_loop, daemon=True)
            t.start()

        app.call_later(_start_watch)
        app.run()
        return

    print(f"Watching {config.tasks_file} for changes...")
    print(f"Polling every 5s | Stop: Ctrl+C or touch {config.stop_file}")

    consecutive_failures = 0

    while True:
        if check_stop_requested(config):
            logger.info("Stop requested, exiting watch mode")
            break

        if consecutive_failures >= config.max_consecutive_failures:
            logger.error(
                "Watch stopped: too many consecutive failures",
                consecutive_failures=consecutive_failures,
            )
            break

        tasks = parse_tasks(config.tasks_file)
        tasks = resolve_dependencies(tasks)
        ready = get_next_tasks(tasks)

        if not ready:
            time.sleep(5)
            continue

        task = ready[0]
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] Starting {task.id}: {task.name}")

        with ExecutorState(config) as state:
            result = run_with_retries(task, config, state)

        if result is True:
            consecutive_failures = 0
            cost = 0.0
            with ExecutorState(config) as state:
                cost = state.task_cost(task.id)
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] {task.id} completed (${cost:.2f})")
        else:
            consecutive_failures += 1
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(
                f"[{timestamp}] {task.id} failed "
                f"({consecutive_failures}/{config.max_consecutive_failures})"
            )

        time.sleep(1)


# === Main ===


def _dispatch_task_command(args: argparse.Namespace) -> None:
    """Dispatch `spec-runner task <subcommand>` to task_commands functions."""
    from .github_sync import cmd_sync_from_gh, cmd_sync_to_gh, export_gh
    from .task import parse_tasks
    from .task_commands import (
        TASKS_FILE,
        cmd_block,
        cmd_check,
        cmd_done,
        cmd_graph,
        cmd_list,
        cmd_next,
        cmd_show,
        cmd_start,
        cmd_stats,
    )

    task_cmd = getattr(args, "task_command", None)
    if not task_cmd:
        print("Usage: spec-runner task <command>\n")
        print("Commands: list, show, start, done, block, check, stats, next, graph,")
        print("          export-gh, sync-to-gh, sync-from-gh")
        return

    prefix = getattr(args, "spec_prefix", "")
    tasks_file = Path(f"spec/{prefix}tasks.md") if prefix else TASKS_FILE
    tasks = parse_tasks(tasks_file)

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

    if task_cmd in write_commands:
        write_commands[task_cmd](args, tasks, tasks_file)
    elif task_cmd in read_commands:
        read_commands[task_cmd](args, tasks)


def main():
    # Shared options available to every subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--max-retries", type=int, default=None, help="Max retries per task (default: 3)"
    )
    common.add_argument(
        "--timeout", type=int, default=None, help="Task timeout in minutes (default: 30)"
    )
    common.add_argument("--no-tests", action="store_true", help="Skip tests on task completion")
    common.add_argument("--no-branch", action="store_true", help="Skip git branch creation")
    common.add_argument("--no-commit", action="store_true", help="Skip auto-commit on success")
    common.add_argument("--no-review", action="store_true", help="Skip code review after task")
    common.add_argument(
        "--hitl-review",
        action="store_true",
        help="Enable interactive approval gate after code review",
    )
    common.add_argument(
        "--callback-url", type=str, default="", help="URL to POST task status updates to"
    )
    common.add_argument(
        "--spec-prefix",
        type=str,
        default="",
        help='Spec file prefix (e.g. "phase5-" for phase5-tasks.md)',
    )
    common.add_argument(
        "--project-root",
        type=str,
        default="",
        help="Project root directory (default: current directory)",
    )
    common.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["debug", "info", "warning", "error"],
        help="Log level (default: info)",
    )
    common.add_argument(
        "--log-json",
        action="store_true",
        help="Output logs as JSON lines",
    )
    common.add_argument(
        "--budget",
        type=float,
        default=None,
        help="Global budget in USD (stop when exceeded)",
    )
    common.add_argument(
        "--task-budget",
        type=float,
        default=None,
        help="Per-task budget in USD (block task when exceeded)",
    )

    parser = argparse.ArgumentParser(
        description="spec-runner — task automation from markdown specs via Claude CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # run
    run_parser = subparsers.add_parser("run", parents=[common], help="Execute tasks")
    run_parser.add_argument("--task", "-t", help="Specific task ID")
    run_parser.add_argument("--all", "-a", action="store_true", help="Run all ready tasks")
    run_parser.add_argument("--milestone", "-m", help="Filter by milestone")
    run_parser.add_argument(
        "--restart",
        action="store_true",
        help="Ignore in-progress tasks, start fresh with TODO tasks only",
    )
    run_parser.add_argument(
        "--tui",
        action="store_true",
        help="Show TUI dashboard during execution",
    )
    run_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip lock check (use when lock is stale)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which tasks would execute without running them",
    )
    run_parser.add_argument(
        "--json-result",
        action="store_true",
        help="Output structured JSON result per task (for Maestro interop)",
    )

    # status
    status_parser = subparsers.add_parser("status", parents=[common], help="Show execution status")
    status_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Output status as JSON"
    )

    # retry
    retry_parser = subparsers.add_parser("retry", parents=[common], help="Retry failed task")
    retry_parser.add_argument("task_id", help="Task ID to retry")
    retry_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Clear previous attempts (start fresh, no error context)",
    )

    # logs
    logs_parser = subparsers.add_parser("logs", parents=[common], help="Show task logs")
    logs_parser.add_argument("task_id", help="Task ID")

    # stop
    subparsers.add_parser("stop", parents=[common], help="Graceful shutdown of running executor")

    # reset
    reset_parser = subparsers.add_parser("reset", parents=[common], help="Reset executor state")
    reset_parser.add_argument("--logs", action="store_true", help="Also clear logs")

    # plan
    plan_parser = subparsers.add_parser("plan", parents=[common], help="Interactive task planning")
    plan_parser.add_argument("description", help="Feature description")
    plan_parser.add_argument(
        "--full",
        action="store_true",
        help="Generate full spec (requirements + design + tasks)",
    )

    # validate
    subparsers.add_parser("validate", parents=[common], help="Validate tasks and config")

    # verify
    verify_parser = subparsers.add_parser(
        "verify", parents=[common], help="Verify post-execution compliance"
    )
    verify_parser.add_argument("--task", "-t", help="Verify specific task ID")
    verify_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Output as JSON"
    )
    verify_parser.add_argument(
        "--strict", action="store_true", help="Fail on warnings (missing traceability)"
    )

    # audit (pre-execution compliance)
    audit_parser = subparsers.add_parser(
        "audit",
        parents=[common],
        help="Static pre-execution audit of the spec triangle",
    )
    audit_group = audit_parser.add_mutually_exclusive_group()
    audit_group.add_argument(
        "--json",
        action="store_const",
        dest="output_format",
        const="json",
        help="Output as JSON",
    )
    audit_group.add_argument(
        "--csv",
        action="store_const",
        dest="output_format",
        const="csv",
        help="Output as CSV",
    )
    audit_parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings (orphans, uncovered) as failures",
    )

    # report
    report_parser = subparsers.add_parser(
        "report", parents=[common], help="Generate traceability matrix"
    )
    report_parser.add_argument("--milestone", "-m", help="Filter by milestone")
    report_parser.add_argument("--status", help="Filter by status (done/failed/todo/not covered)")
    report_parser.add_argument(
        "--uncovered-only", action="store_true", help="Show only uncovered requirements"
    )
    report_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Output as JSON"
    )

    # tui
    subparsers.add_parser("tui", parents=[common], help="Launch read-only TUI dashboard")

    # watch
    watch_parser = subparsers.add_parser(
        "watch", parents=[common], help="Continuously execute ready tasks"
    )
    watch_parser.add_argument(
        "--tui",
        action="store_true",
        help="Show TUI dashboard during watch",
    )

    # costs
    costs_parser = subparsers.add_parser(
        "costs", parents=[common], help="Show cost breakdown per task"
    )
    costs_parser.add_argument("--json", action="store_true", help="Output as JSON")
    costs_parser.add_argument(
        "--sort",
        choices=["id", "cost", "tokens", "name"],
        default="id",
        help="Sort order (default: task id)",
    )

    # mcp
    subparsers.add_parser("mcp", parents=[common], help="Launch read-only MCP server")

    # task (unified: replaces spec-task binary)
    task_parser = subparsers.add_parser(
        "task", help="Task management (list, show, start, done, graph, sync)"
    )
    task_sub = task_parser.add_subparsers(dest="task_command", help="Task commands")

    task_common = argparse.ArgumentParser(add_help=False)
    task_common.add_argument(
        "--spec-prefix", type=str, default="", help='Spec file prefix (e.g. "phase5-")'
    )

    t_list = task_sub.add_parser("list", aliases=["ls"], parents=[task_common], help="List tasks")
    t_list.add_argument("--status", "-s", choices=["todo", "in_progress", "done", "blocked"])
    t_list.add_argument("--priority", "-p", choices=["p0", "p1", "p2", "p3"])
    t_list.add_argument("--milestone", "-m", help="Filter by milestone")

    t_show = task_sub.add_parser("show", parents=[task_common], help="Task details")
    t_show.add_argument("task_id", help="Task ID (e.g., TASK-001)")

    t_start = task_sub.add_parser("start", parents=[task_common], help="Start task")
    t_start.add_argument("task_id", help="Task ID")
    t_start.add_argument("--force", "-f", action="store_true", help="Ignore dependencies")

    t_done = task_sub.add_parser("done", parents=[task_common], help="Complete task")
    t_done.add_argument("task_id", help="Task ID")
    t_done.add_argument("--force", "-f", action="store_true", help="Ignore incomplete checklist")

    t_block = task_sub.add_parser("block", parents=[task_common], help="Block task")
    t_block.add_argument("task_id", help="Task ID")

    t_check = task_sub.add_parser("check", parents=[task_common], help="Mark checklist item")
    t_check.add_argument("task_id", help="Task ID")
    t_check.add_argument("item_index", help="Item index (0, 1, 2...)")

    task_sub.add_parser("stats", parents=[task_common], help="Statistics")
    task_sub.add_parser("next", parents=[task_common], help="Next ready tasks")
    task_sub.add_parser("graph", parents=[task_common], help="Dependency graph")
    task_sub.add_parser("export-gh", parents=[task_common], help="Export to GitHub Issues")

    t_sync_to = task_sub.add_parser(
        "sync-to-gh", parents=[task_common], help="Sync tasks to GitHub Issues"
    )
    t_sync_to.add_argument("--dry-run", action="store_true", help="Preview without changes")

    task_sub.add_parser(
        "sync-from-gh", parents=[task_common], help="Sync GitHub Issues to tasks.md"
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Load config from YAML file, then override with CLI args
    yaml_config = load_config_from_yaml()
    config = build_config(yaml_config, args)

    from .logging import setup_logging

    setup_logging(level=config.log_level, json_output=getattr(args, "log_json", False))

    import structlog

    structlog.contextvars.bind_contextvars(run_id=uuid4().hex[:8])

    # Register signal handlers for graceful shutdown (late import to avoid circular)
    from .executor import _pause_handler, _signal_handler

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGQUIT, _pause_handler)

    # Dispatch
    commands = {
        "run": cmd_run,
        "status": cmd_status,
        "costs": cmd_costs,
        "retry": cmd_retry,
        "logs": cmd_logs,
        "stop": cmd_stop,
        "reset": cmd_reset,
        "plan": cmd_plan,
        "validate": cmd_validate,
        "verify": cmd_verify,
        "audit": cmd_audit,
        "report": cmd_report,
        "tui": cmd_tui,
        "watch": cmd_watch,
        "mcp": cmd_mcp,
    }

    # Handle unified task subcommand
    if args.command == "task":
        _dispatch_task_command(args)
        return

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args, config)


if __name__ == "__main__":
    main()
