"""CLI commands and argument parsing for spec-runner."""

import argparse
import json
import signal
import sys
import time
from collections.abc import Callable
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
from .git_ops import (
    create_integration_branch,
    ensure_on_main_branch,
    finalize_integration_branch,
    make_integration_branch_name,
)
from .logging import get_logger
from .preset_cmd import cmd_config
from .runner import (
    log_progress,
)
from .spec import read_spec_meta
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


def _acquire_run_lock(config: ExecutorConfig) -> ExecutorLock:
    """Acquire the exclusive executor lock, or exit(1) if another run holds it."""
    lock = ExecutorLock(config.state_file.with_suffix(".lock"))
    if not lock.acquire():
        held_by = getattr(lock, "_held_by", {})
        alive = held_by.get("alive", "true")
        logger.error(
            "Another executor is already running",
            lock_file=str(config.state_file.with_suffix(".lock")),
            held_by_pid=held_by.get("pid", "unknown"),
            started=held_by.get("started", "unknown"),
            process_alive=alive,
        )
        if alive == "false":
            logger.error(
                "Lock holder is dead. Use --force to override, or delete the lock file manually."
            )
        sys.exit(1)
    return lock


def cmd_run(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Execute tasks."""
    # HITL review incompatible with TUI mode
    if config.hitl_review and getattr(args, "tui", False):
        logger.warning("--hitl-review ignored in TUI mode (TUI owns the screen)")
        config.hitl_review = False

    # Acquire the exclusive lock unless --force. TUI mode also holds it (one
    # executor per project) — when held, stale-task recovery can safely reset all
    # orphaned 'running' tasks; with --force a concurrent runner may exist, so we
    # fall back to the age-based heuristic.
    if getattr(args, "force", False):
        logger.warning("Skipping lock check (--force)")
        lock = None
    else:
        lock = _acquire_run_lock(config)
    lock_held = lock is not None

    try:
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
                t = threading.Thread(
                    target=lambda: _run_tasks(args, config, lock_held=lock_held), daemon=True
                )
                t.start()

            app.call_later(_start_execution)
            app.run()
        else:
            _run_tasks(args, config, lock_held=lock_held)
    finally:
        if lock is not None:
            lock.release()


def spec_run_gate_ok(config: ExecutorConfig) -> tuple[bool, str]:
    """Return (allowed, reason). Blocks unapproved managed tasks.md in strict mode.

    Governance is off by default: unless ``config.spec_governance == "strict"``,
    or the tasks.md is unmanaged (no frontmatter), the gate always allows the run.
    """
    if getattr(config, "spec_governance", "off") != "strict":
        return True, ""
    meta = read_spec_meta(config.tasks_file)
    if meta is None:
        return True, ""  # unmanaged: backward-compatible
    if meta.status == "approved":
        return True, ""
    return False, (
        f"tasks.md is {meta.status} (v{meta.version}); "
        f"approve with `spec-runner spec approve tasks` or run with --no-strict"
    )


def _maybe_start_integration(args, config: ExecutorConfig):
    """Fork a per-run integration branch when ``integration_pr`` is enabled.

    Returns an ``IntegrationRun`` (and redirects task merges onto it via
    ``config.main_branch``) or None when the mode is off/unavailable — in
    which case the run behaves exactly as before (self-merge into main).
    """
    if not getattr(config, "integration_pr", False):
        return None
    if not config.create_git_branch:
        logger.warning("integration_pr ignored: create_git_branch is off")
        return None
    if getattr(args, "dry_run", False):
        return None
    run = create_integration_branch(config, make_integration_branch_name())
    if run is not None:
        # Redirect every task's merge target to the integration branch; the
        # existing merge stage reads config.main_branch, so main is untouched.
        config.main_branch = run.branch
    return run


def _run_tasks(args, config: ExecutorConfig, *, lock_held: bool = False):
    """Run tasks, optionally collecting them on one integration branch + PR.

    Thin wrapper around :func:`_run_tasks_inner`: sets up the integration
    branch first (when enabled) and always finalizes it (push + open PR, or
    clean up) afterwards, regardless of how the inner run exits.
    """
    integration = _maybe_start_integration(args, config)
    try:
        _run_tasks_inner(args, config, lock_held=lock_held)
    finally:
        if integration is not None:
            finalize_integration_branch(config, integration)


def _run_tasks_inner(args, config: ExecutorConfig, *, lock_held: bool = False):
    """Internal task execution logic.

    lock_held: True when the caller holds the exclusive executor lock, so any
    orphaned 'running' task can be safely reset regardless of age.
    """
    allowed, reason = spec_run_gate_ok(config)
    if not allowed:
        print(f"⛔ spec governance: {reason}")
        return

    # Clear any leftover stop file from previous runs
    clear_stop_file(config)

    tasks = parse_tasks(config.tasks_file)

    with ExecutorState(config) as state:
        from .audit_log import EVENT_RUN_ENDED, EVENT_RUN_STARTED

        state.audit_logger.record(
            EVENT_RUN_STARTED,
            total_tasks=len(tasks),
            mode="all" if getattr(args, "all", False) else "single",
            task_filter=getattr(args, "task", None),
        )

        # Recover tasks stuck in 'running' from a previous crashed/interrupted run.
        # When we hold the exclusive lock (lock_held), no other runner exists — any
        # 'running' task is orphaned and is reset regardless of age (otherwise a
        # session interruption, e.g. a dropped remote shell, leaves a half-done
        # task that the next run re-picks first and hangs re-doing it). Without the
        # lock (--force), a concurrent runner may be active, so fall back to the
        # age-based heuristic (2x the task timeout).
        stale_timeout = config.task_timeout_minutes * 2
        recovered = recover_stale_tasks(
            state, stale_timeout, config.tasks_file, recover_all=lock_held
        )
        if recovered:
            logger.warning("Recovered stale tasks", task_ids=recovered)
            tasks = parse_tasks(config.tasks_file)

        # v2.3.0: reset failed-task state on `run --all` unless opted out.
        reset_enabled = getattr(args, "all", False) and not getattr(args, "no_reset_failed", False)
        previously_failed: set[str] = set()  # used by T17 second-pass detection
        if reset_enabled:
            previously_failed = state.reset_failed_to_pending()
            state.consecutive_failures = 0
            state.clear_second_pass_fails()
            state._save()
        stop_reason: str = "completed"  # used by T18 stop-reason capture
        stop_detail: str = ""  # used by T18 stop-reason capture

        # Pre-run validation
        from .validate import format_results, validate_all

        pre_result = validate_all(
            tasks_file=config.tasks_file,
            config_file=_resolve_config_path(),
        )
        if not pre_result.ok:
            # H-1 (governed-run finding): a silent `return` here exited 0 and
            # orchestrators (Maestro) read that as workstream success — an
            # unparseable spec became a mergeable empty run. Fail loudly.
            logger.error("Validation failed before execution")
            print(format_results(pre_result))
            # Close the audit pair: EVENT_RUN_STARTED was already recorded,
            # and a dangling start would make the trail ambiguous.
            state.audit_logger.record(
                EVENT_RUN_ENDED,
                completed=0,
                failed=0,
                remaining=len(tasks),
                stop_reason="validation_failed",
            )
            sys.exit(1)

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
            state.set_meta("last_run_stop_reason", stop_reason)
            state.set_meta("last_run_stop_detail", stop_detail)
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

                # v2.3.0: detect tasks that fail again on a second pass.
                # Use the persisted task status (set to "failed" when retries
                # are exhausted) rather than `result is False`, because the
                # default on_task_failure="skip" mode returns "SKIP" for a
                # fully-failed task — so a result-based check would miss it.
                # Must run BEFORE the SKIP `continue` below, which short-circuits.
                if (
                    task.id in previously_failed
                    and state.get_task_state(task.id).status == "failed"
                ):
                    log_progress(
                        f"💡 [{task.id}] repeated failure — review logs at "
                        f"{config.logs_dir}/{task.id}-*.log"
                    )
                    state.add_second_pass_fail(task.id)

                # "SKIP" means continue to next task
                if result == "SKIP":
                    continue

                if result is False and state.should_stop():
                    last = state.most_recent_failed_attempt()
                    if last and last.error_kind and last.error_kind != "unknown":
                        stop_reason = f"error_{last.error_kind}"
                        stop_detail = last.error or ""
                    else:
                        stop_reason = "max_consecutive_failures"
                        stop_detail = (
                            f"{state.consecutive_failures}/{config.max_consecutive_failures}"
                        )
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

                # v2.3.0: detect tasks that fail again on a second pass.
                # Use the persisted task status (set to "failed" when retries
                # are exhausted) rather than `result is False`, because the
                # default on_task_failure="skip" mode returns "SKIP" for a
                # fully-failed task — so a result-based check would miss it.
                # Must run BEFORE the SKIP `continue` below, which short-circuits.
                if (
                    task.id in previously_failed
                    and state.get_task_state(task.id).status == "failed"
                ):
                    log_progress(
                        f"💡 [{task.id}] repeated failure — review logs at "
                        f"{config.logs_dir}/{task.id}-*.log"
                    )
                    state.add_second_pass_fail(task.id)

                if result == "SKIP":
                    continue

                if result is False and state.should_stop():
                    last = state.most_recent_failed_attempt()
                    if last and last.error_kind and last.error_kind != "unknown":
                        stop_reason = f"error_{last.error_kind}"
                        stop_detail = last.error or ""
                    else:
                        stop_reason = "max_consecutive_failures"
                        stop_detail = (
                            f"{state.consecutive_failures}/{config.max_consecutive_failures}"
                        )
                    logger.warning("Stopping: too many consecutive failures")
                    break

        # v2.3.0: persist stop-reason for this run
        state.set_meta("last_run_stop_reason", stop_reason)
        state.set_meta("last_run_stop_detail", stop_detail)

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

        state.audit_logger.record(
            EVENT_RUN_ENDED,
            completed=state.total_completed,
            failed=state.total_failed,
            remaining=remaining,
            total_cost_usd=round(total_cost_val, 4),
        )

        # --json-result: structured JSON result per task (for Maestro interop)
        if getattr(args, "json_result", False):
            results = [build_task_json_result(t.id, state) for t in tasks_to_run]
            print(json.dumps(results if len(results) > 1 else results[0], indent=2))


def cmd_retry(args, config: ExecutorConfig):
    """Retry failed task, preserving error context from previous attempts."""
    # Spec governance gate — must run before any task execution/lock so a
    # blocked retry has zero side effects (same bypass class as `watch`).
    allowed, reason = spec_run_gate_ok(config)
    if not allowed:
        print(f"⛔ spec governance: {reason}")
        return

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
    # Spec governance gate — must run before anything else (before the TUI
    # branch, before pre-run validation, before any lock/stop-file handling)
    # so a blocked watch has zero side effects. `run` gates via `_run_tasks`;
    # `watch` has its own loop and previously bypassed the gate entirely.
    allowed, reason = spec_run_gate_ok(config)
    if not allowed:
        print(f"⛔ spec governance: {reason}")
        return

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


def cmd_doctor(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Run the CLI/model compatibility probe and exit with its status code."""
    from .doctor import run_doctor

    code = run_doctor(
        config,
        cli=args.cli,
        model=args.model,
        with_review=args.with_review,
        budget=args.budget,
        timeout_min=getattr(args, "timeout", None),
        assume_yes=args.yes,
        strict=args.strict,
        as_json=args.json,
        keep=args.keep,
    )
    raise SystemExit(code)


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

    write_commands: dict[str, Callable[..., object]] = {
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


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level argument parser.

    Extracted from main() to allow programmatic use and testing.
    """
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
        "--integration-pr",
        action="store_true",
        default=None,
        help="Collect all tasks on one branch and open a single PR (never merge into main)",
    )
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
        "--change",
        type=str,
        default="",
        help="Operate inside spec/changes/<id>/ (change-as-folder; see `change` command)",
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

    # Gated spec-generation profile selector (plan --gated and the spec family).
    profile_parent = argparse.ArgumentParser(add_help=False)
    profile_parent.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Gated spec-generation profile name (default: lite)",
    )

    parser = argparse.ArgumentParser(
        description="spec-runner — task automation from markdown specs via Claude CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    from importlib.metadata import PackageNotFoundError, version

    try:
        _pkg_version = version("spec-runner")
    except PackageNotFoundError:
        _pkg_version = "0.0.0.dev"
    parser.add_argument(
        "--version",
        action="version",
        version=f"spec-runner {_pkg_version}",
        help="Print the spec-runner version and exit",
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
    run_parser.add_argument(
        "--no-reset-failed",
        action="store_true",
        help="Do not reset failed→pending or clear consecutive_failures "
        "at the start of `run --all` (default: reset enabled).",
    )
    run_parser.add_argument(
        "--strict",
        action="store_true",
        help="Enforce spec governance: block unapproved managed tasks.md",
    )
    run_parser.add_argument(
        "--no-strict",
        action="store_true",
        help="Disable spec governance gate (default behavior)",
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
    plan_parser = subparsers.add_parser(
        "plan", parents=[common, profile_parent], help="Interactive task planning"
    )
    plan_parser.add_argument(
        "description", nargs="?", default=None, help="Feature description (or use --from-file)"
    )
    plan_parser.add_argument(
        "--from-file",
        metavar="PATH",
        help="Read the feature description from a file instead of the positional argument",
    )
    plan_parser.add_argument(
        "--full",
        action="store_true",
        help="Generate full spec (requirements + design + tasks)",
    )
    plan_parser.add_argument(
        "--gated",
        action="store_true",
        help="Generate one gated spec stage, validate, write DRAFT, and stop",
    )
    plan_parser.add_argument(
        "--stage",
        choices=["requirements", "design", "tasks"],
        default=None,
        help="Stage to generate with --gated (default: auto-resolved next stage)",
    )
    plan_parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Disable the interactive checkpoint menu in --gated mode",
    )

    # validate
    subparsers.add_parser("validate", parents=[common], help="Validate tasks and config")

    # config (CLI profile presets)
    config_parser = subparsers.add_parser(
        "config", parents=[common], help="Apply a CLI profile preset to config"
    )
    config_parser.add_argument("--preset", help="CLI for both exec and review (mono)")
    config_parser.add_argument("--exec", dest="exec_cli", help="CLI for the exec/implementer stage")
    config_parser.add_argument("--review", dest="review_cli", help="CLI for the review stage")
    config_parser.add_argument("--model", help="Model for both slots")
    config_parser.add_argument(
        "--review-model", dest="review_model", help="Model for the review slot only"
    )
    config_parser.add_argument("--list-presets", action="store_true", help="List available presets")
    config_parser.add_argument(
        "--dry-run", action="store_true", help="Print keys that would change; write nothing"
    )
    config_parser.add_argument(
        "--apply", action="store_true", help="Update the CLI profile in an existing config"
    )

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
    watch_parser.add_argument(
        "--strict",
        action="store_true",
        help="Enforce spec governance: block unapproved managed tasks.md",
    )
    watch_parser.add_argument(
        "--no-strict",
        action="store_true",
        help="Disable spec governance gate (default behavior)",
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

    # doctor
    doctor_parser = subparsers.add_parser(
        "doctor", parents=[common], help="Probe CLI/model compatibility (real mini-task)"
    )
    doctor_parser.add_argument("--cli", help="Override the CLI command (claude/codex/pi/...)")
    doctor_parser.add_argument("--model", help="Override the model (executor + review)")
    doctor_parser.add_argument(
        "--with-review",
        action="store_true",
        help="Also probe the review stage (2nd model call)",
    )
    doctor_parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip the cost-gate confirmation"
    )
    doctor_parser.add_argument(
        "--strict", action="store_true", help="Exit non-zero on DEGRADED too"
    )
    doctor_parser.add_argument("--json", action="store_true", help="Machine-readable output")
    doctor_parser.add_argument("--keep", action="store_true", help="Keep the scratch workspace")
    # --budget is inherited from common (default None); override default to 0.50 for doctor
    doctor_parser.set_defaults(budget=0.5)

    # spec (gated spec lifecycle: status, approve, reject, adopt, check)
    spec_parser = subparsers.add_parser(
        "spec", parents=[common], help="Manage spec lifecycle (gated governance)"
    )
    spec_sub = spec_parser.add_subparsers(dest="spec_command", help="Spec lifecycle commands")

    spec_sub.add_parser("status", parents=[profile_parent], help="Show per-stage status")

    spec_approve = spec_sub.add_parser(
        "approve", parents=[profile_parent], help="Approve a spec stage"
    )
    spec_approve.add_argument("stage", choices=["requirements", "design", "tasks"])

    spec_reject = spec_sub.add_parser(
        "reject", parents=[profile_parent], help="Reopen a spec stage as draft"
    )
    spec_reject.add_argument("stage", choices=["requirements", "design", "tasks"])

    spec_check = spec_sub.add_parser(
        "check", parents=[profile_parent], help="Refresh cached validation for a stage"
    )
    spec_check.add_argument("stage", choices=["requirements", "design", "tasks"])

    spec_adopt = spec_sub.add_parser(
        "adopt", parents=[profile_parent], help="Adopt an unmanaged spec file"
    )
    spec_adopt.add_argument("stage", choices=["requirements", "design", "tasks"])
    spec_adopt.add_argument(
        "--force", action="store_true", help="Adopt as approved even if validation fails"
    )

    # change (change-as-folder lifecycle, M2)
    change_parser = subparsers.add_parser(
        "change", parents=[common], help="Manage change folders (new, list, archive)"
    )
    change_sub = change_parser.add_subparsers(dest="change_command", help="Change commands")

    ch_new = change_sub.add_parser("new", help="Create spec/changes/<id>/ with a tasks.md stub")
    ch_new.add_argument("change_id", help="Change id (kebab-case, e.g. add-dark-mode)")

    ch_list = change_sub.add_parser("list", help="List in-flight changes")
    ch_list.add_argument("--json", action="store_true", help="JSON output")

    ch_archive = change_sub.add_parser(
        "archive", help="Move a completed change to spec/changes/archive/"
    )
    ch_archive.add_argument("change_id", help="Change id to archive")
    ch_archive.add_argument(
        "--force", action="store_true", help="Archive even if tasks are not all done"
    )

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

    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Load config from YAML file, then override with CLI args
    yaml_config = load_config_from_yaml()
    config = build_config(yaml_config, args)

    # Fail fast with a clean message (no traceback) on an unknown spec profile.
    from .config import ConfigError

    try:
        config.resolve_spec_profile()
    except ConfigError as exc:
        raise SystemExit(f"⛔ {exc}") from None

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
        "doctor": cmd_doctor,
        "config": cmd_config,
    }

    # Handle unified task subcommand
    if args.command == "task":
        _dispatch_task_command(args)
        return

    # Handle change-as-folder subcommand (new/list/archive)
    if args.command == "change":
        from . import change_commands

        handler = {
            "new": change_commands.cmd_change_new,
            "list": change_commands.cmd_change_list,
            "archive": change_commands.cmd_change_archive,
        }.get(args.change_command)
        if handler is None:
            # no sub-subcommand given -> default to `change list`
            raise SystemExit(change_commands.cmd_change_list(args, config))
        raise SystemExit(handler(args, config))

    # Handle spec lifecycle subcommand (status/approve/reject/adopt/check)
    if args.command == "spec":
        from . import spec_commands

        handler = {
            "status": spec_commands.cmd_spec_status,
            "approve": spec_commands.cmd_spec_approve,
            "reject": spec_commands.cmd_spec_reject,
            "adopt": spec_commands.cmd_spec_adopt,
            "check": spec_commands.cmd_spec_check,
        }.get(args.spec_command)
        if handler is None:
            # no sub-subcommand given -> default to `spec status`
            raise SystemExit(spec_commands.cmd_spec_status(args, config))
        raise SystemExit(handler(args, config))

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args, config)


if __name__ == "__main__":
    main()
