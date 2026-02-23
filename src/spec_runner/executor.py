#!/usr/bin/env python3
"""
spec-runner — task automation from markdown specs via Claude CLI

Usage:
    spec-runner run                    # Execute next task
    spec-runner run --task=TASK-001    # Execute specific task
    spec-runner run --all              # Execute all ready tasks
    spec-runner run --milestone=mvp    # Execute milestone tasks
    spec-runner status                 # Execution status
    spec-runner retry TASK-001         # Retry failed task
    spec-runner logs TASK-001          # Task logs
"""

import argparse
import asyncio
import re
import shutil
import signal
import subprocess
import sys
from datetime import datetime
from uuid import uuid4

from .config import (
    CONFIG_FILE,
    ExecutorConfig,
    ExecutorLock,
    build_config,
    load_config_from_yaml,
)
from .hooks import (
    ensure_on_main_branch,
    post_done_hook,
    pre_start_hook,
)
from .logging import get_logger
from .prompt import (
    build_task_prompt,
    extract_test_failures,
    load_prompt_template,
    render_template,
)
from .runner import (
    build_cli_command,
    check_error_patterns,
    log_progress,
    parse_token_usage,
    run_claude_async,
    send_callback,
)
from .state import (
    ErrorCode,
    ExecutorState,
    RetryContext,
    check_stop_requested,
    clear_stop_file,
    recover_stale_tasks,
)
from .task import (
    Task,
    get_next_tasks,
    get_task_by_id,
    mark_all_checklist_done,
    parse_tasks,
    update_task_status,
)

logger = get_logger("executor")

_shutdown_requested = False


def _signal_handler(signum, frame):
    """Handle SIGINT/SIGTERM by setting shutdown flag."""
    global _shutdown_requested
    _shutdown_requested = True


# === Task Executor ===


def execute_task(task: Task, config: ExecutorConfig, state: ExecutorState) -> bool | str:
    """Execute a single task via Claude CLI.

    Returns:
        True if successful, False if failed, "API_ERROR" if rate limited,
        or "HOOK_ERROR" if pre-start hook failed (fail fast, no retries).
    """

    task_id = task.id
    log_progress(f"🚀 Starting: {task.name}", task_id)
    logger.info("Executing task", task_id=task_id, name=task.name)

    # Pre-start hook
    if not pre_start_hook(task, config):
        logger.error("Pre-start hook failed", task_id=task_id)
        state.record_attempt(
            task_id,
            False,
            0.0,
            error="Pre-start hook failed",
            error_code=ErrorCode.HOOK_FAILURE,
        )
        return "HOOK_ERROR"

    # Update status
    state.mark_running(task_id)
    update_task_status(config.tasks_file, task_id, "in_progress")
    send_callback(config.callback_url, task_id, "started")

    # Get previous attempts for context (to inform Claude about past failures)
    task_state = state.get_task_state(task_id)
    previous_attempts = task_state.attempts if task_state.attempts else None

    # Build RetryContext from previous failed attempts
    retry_context: RetryContext | None = None
    if previous_attempts:
        failed = [a for a in previous_attempts if not a.success]
        if failed:
            last = failed[-1]
            retry_context = RetryContext(
                attempt_number=task_state.attempt_count + 1,
                max_attempts=config.max_retries,
                previous_error_code=last.error_code or ErrorCode.UNKNOWN,
                previous_error=last.error or "Unknown error",
                what_was_tried=f"Previous attempt for {task.name}",
                test_failures=(
                    extract_test_failures(last.claude_output)
                    if last.claude_output
                    and last.error_code in (ErrorCode.TEST_FAILURE, ErrorCode.LINT_FAILURE)
                    else None
                ),
            )

    # Build prompt with RetryContext
    prompt = build_task_prompt(task, config, previous_attempts, retry_context=retry_context)

    # Save prompt to log
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = config.logs_dir / f"{task_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"

    with open(log_file, "w") as f:
        f.write(f"=== PROMPT ===\n{prompt}\n\n")

    # Run Claude
    start_time = datetime.now()

    try:
        # Build command using template or auto-detect
        cmd = build_cli_command(
            cmd=config.claude_command,
            prompt=prompt,
            model=config.claude_model,
            template=config.command_template,
            skip_permissions=config.skip_permissions,
        )

        logger.info(
            "Running CLI command",
            command=config.claude_command,
            skip_permissions=config.skip_permissions,
        )

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.task_timeout_minutes * 60,
            cwd=config.project_root,
        )

        duration = (datetime.now() - start_time).total_seconds()
        output = result.stdout
        combined_output = output + "\n" + result.stderr

        # Parse token usage from stderr
        input_tokens, output_tokens, cost_usd = parse_token_usage(result.stderr)

        # Save output
        with open(log_file, "a") as f:
            f.write(f"=== OUTPUT ===\n{output}\n\n")
            f.write(f"=== STDERR ===\n{result.stderr}\n\n")
            f.write(f"=== RETURN CODE: {result.returncode} ===\n")

        # Check for API errors (rate limits, etc.)
        error_pattern = check_error_patterns(combined_output)
        if error_pattern:
            log_progress(f"⚠️ API error detected: {error_pattern}", task_id)
            logger.warning(
                "API error detected",
                task_id=task_id,
                error_pattern=error_pattern,
            )
            state.record_attempt(
                task_id,
                False,
                duration,
                error=f"API error: {error_pattern}",
                error_code=ErrorCode.RATE_LIMIT,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
            )
            send_callback(
                config.callback_url,
                task_id,
                "failed",
                duration,
                f"API error: {error_pattern}",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
            )
            return "API_ERROR"

        # Check result
        # Success if:
        # 1. Explicitly says TASK_COMPLETE, or
        # 2. Return code 0 and no TASK_FAILED (Claude forgot the marker)
        has_complete_marker = "TASK_COMPLETE" in output
        has_failed_marker = "TASK_FAILED" in output
        implicit_success = result.returncode == 0 and not has_failed_marker

        success = (has_complete_marker and not has_failed_marker) or implicit_success

        if success:
            if has_complete_marker:
                logger.info("Task completed by Claude", task_id=task_id)
            else:
                logger.info("Implicit success (return code 0)", task_id=task_id)

            # Post-done hook (tests, lint, review)
            hook_success, hook_error, review_status, review_findings = post_done_hook(
                task, config, True
            )

            if hook_success:
                state.record_attempt(
                    task_id,
                    True,
                    duration,
                    output=output,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    review_status=review_status,
                    review_findings=(review_findings[:2048] if review_findings else None),
                )
                update_task_status(config.tasks_file, task_id, "done")
                mark_all_checklist_done(config.tasks_file, task_id)
                log_progress(f"✅ Completed in {duration:.1f}s", task_id)
                send_callback(
                    config.callback_url,
                    task_id,
                    "success",
                    duration,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                )
                return True
            else:
                # Hook failed (tests didn't pass)
                # Include detailed error info for next attempt
                error = hook_error or "Post-done hook failed (tests/lint)"
                # Classify the hook failure
                error_code = ErrorCode.UNKNOWN
                if hook_error:
                    if "Tests failed" in hook_error:
                        error_code = ErrorCode.TEST_FAILURE
                    elif "Lint errors" in hook_error:
                        error_code = ErrorCode.LINT_FAILURE
                    elif "Review rejected" in hook_error or "Fix requested" in hook_error:
                        error_code = ErrorCode.REVIEW_REJECTED
                    else:
                        error_code = ErrorCode.HOOK_FAILURE
                # Combine Claude output with test failures for context
                full_output = output
                if hook_error:
                    full_output = f"{output}\n\n=== TEST FAILURES ===\n{hook_error}"
                state.record_attempt(
                    task_id,
                    False,
                    duration,
                    error=error,
                    output=full_output,
                    error_code=error_code,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                    review_status=review_status,
                    review_findings=(review_findings[:2048] if review_findings else None),
                )
                log_progress("❌ Failed: tests/lint check", task_id)
                send_callback(
                    config.callback_url,
                    task_id,
                    "failed",
                    duration,
                    error,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                )
                return False
        else:
            # Claude reported failure
            error_match = re.search(r"TASK_FAILED:\s*(.+)", output)
            error = error_match.group(1) if error_match else "Unknown error"
            state.record_attempt(
                task_id,
                False,
                duration,
                error=error,
                output=output,
                error_code=ErrorCode.TASK_FAILED,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
            )
            log_progress(f"❌ Failed: {error[:50]}", task_id)
            send_callback(
                config.callback_url,
                task_id,
                "failed",
                duration,
                error,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
            )
            return False

    except subprocess.TimeoutExpired:
        duration = config.task_timeout_minutes * 60
        error = f"Timeout after {config.task_timeout_minutes} minutes"
        state.record_attempt(
            task_id,
            False,
            duration,
            error=error,
            error_code=ErrorCode.TIMEOUT,
        )
        log_progress(f"⏰ Timeout after {config.task_timeout_minutes}m", task_id)
        send_callback(config.callback_url, task_id, "failed", duration, error)
        return False

    except KeyboardInterrupt:
        duration = (datetime.now() - start_time).total_seconds()
        state.record_attempt(
            task_id,
            False,
            duration,
            error="Interrupted by signal",
            error_code=ErrorCode.INTERRUPTED,
        )
        log_progress("Interrupted by signal", task_id)
        return False

    except Exception as e:
        duration = (datetime.now() - start_time).total_seconds()
        error = str(e)
        state.record_attempt(
            task_id,
            False,
            duration,
            error=error,
            error_code=ErrorCode.UNKNOWN,
        )
        log_progress(f"💥 Error: {error[:50]}", task_id)
        send_callback(config.callback_url, task_id, "failed", duration, error)
        return False


def run_with_retries(task: Task, config: ExecutorConfig, state: ExecutorState) -> bool | str:
    """Execute task with retries.

    Returns:
        True if successful, False if failed, "API_ERROR" if rate limited,
        or "SKIP" if task was skipped.
    """

    task_state = state.get_task_state(task.id)

    for attempt in range(task_state.attempt_count, config.max_retries):
        log_progress(f"📍 Attempt {attempt + 1}/{config.max_retries}", task.id)

        result = execute_task(task, config, state)

        # API error - stop immediately, don't retry
        if result == "API_ERROR":
            return "API_ERROR"

        # Hook error - stop immediately, don't retry
        if result == "HOOK_ERROR":
            return False

        # Check per-task budget
        if config.task_budget_usd is not None and state.task_cost(task.id) > config.task_budget_usd:
            log_progress(
                f"Task budget exceeded "
                f"(${state.task_cost(task.id):.2f} > "
                f"${config.task_budget_usd:.2f})",
                task.id,
            )
            update_task_status(config.tasks_file, task.id, "blocked")
            return False

        if result is True:
            return True

        # Review rejection is permanent — no automatic retry
        ts = state.get_task_state(task.id)
        if ts and ts.attempts:
            last = ts.attempts[-1]
            if last.error_code == ErrorCode.REVIEW_REJECTED:
                log_progress("Review rejected — no automatic retry", task.id)
                return False

        if attempt < config.max_retries - 1:
            logger.info(
                "Waiting before retry",
                task_id=task.id,
                delay_seconds=config.retry_delay_seconds,
            )
            import time

            time.sleep(config.retry_delay_seconds)

    # Task failed after all retries
    log_progress(f"❌ Failed after {config.max_retries} attempts", task.id)

    # Log concise error summary
    if task_state.last_error:
        last_attempt = task_state.attempts[-1] if task_state.attempts else None
        error_code = last_attempt.error_code if last_attempt else None
        logger.error(
            "Task failed",
            task_id=task.id,
            error=task_state.last_error,
            error_code=error_code,
            attempts=config.max_retries,
        )

    # Handle based on on_task_failure setting
    if config.on_task_failure == "stop":
        update_task_status(config.tasks_file, task.id, "blocked")
        return False

    elif config.on_task_failure == "ask":
        # Interactive prompt — keep print() for user-facing menu
        print(f"\nTask {task.id} failed. What to do?")
        print("   [s] Skip and continue to next task")
        print("   [r] Retry this task")
        print("   [q] Quit executor")
        choice = input("\nYour choice [s/r/q]: ").strip().lower()

        if choice == "r":
            # Reset attempts and retry
            task_state.attempts = []
            state._save()
            return run_with_retries(task, config, state)
        elif choice == "q":
            update_task_status(config.tasks_file, task.id, "blocked")
            return False
        else:
            # Skip (default)
            update_task_status(config.tasks_file, task.id, "blocked")
            log_progress("⏭️ Skipped, continuing to next task", task.id)
            return "SKIP"

    else:  # "skip" (default)
        update_task_status(config.tasks_file, task.id, "blocked")
        log_progress("⏭️ Skipped, continuing to next task", task.id)
        return "SKIP"


# === Parallel Execution ===


async def _execute_task_async(
    task: Task,
    config: ExecutorConfig,
    state: ExecutorState,
    state_lock: asyncio.Lock,
) -> bool | str:
    """Async wrapper for task execution with state locking.

    Uses run_claude_async for non-blocking subprocess execution.
    Protects ExecutorState writes with asyncio.Lock.
    """
    task_id = task.id
    log_progress(f"Starting: {task.name}", task_id)

    # Pre-start hook (sync, but quick)
    if not pre_start_hook(task, config):
        async with state_lock:
            state.record_attempt(
                task_id,
                False,
                0.0,
                error="Pre-start hook failed",
                error_code=ErrorCode.HOOK_FAILURE,
            )
        return "HOOK_ERROR"

    async with state_lock:
        state.mark_running(task_id)
    update_task_status(config.tasks_file, task_id, "in_progress")

    # Build prompt
    task_state = state.get_task_state(task_id)
    previous_attempts = task_state.attempts if task_state.attempts else None
    retry_context = None
    if previous_attempts:
        failed = [a for a in previous_attempts if not a.success]
        if failed:
            last = failed[-1]
            retry_context = RetryContext(
                attempt_number=task_state.attempt_count + 1,
                max_attempts=config.max_retries,
                previous_error_code=last.error_code or ErrorCode.UNKNOWN,
                previous_error=last.error or "Unknown error",
                what_was_tried=f"Previous attempt for {task.name}",
                test_failures=(
                    extract_test_failures(last.claude_output)
                    if last.claude_output
                    and last.error_code in (ErrorCode.TEST_FAILURE, ErrorCode.LINT_FAILURE)
                    else None
                ),
            )

    prompt = build_task_prompt(task, config, previous_attempts, retry_context=retry_context)

    # Log
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = config.logs_dir / f"{task_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    with open(log_file, "w") as f:
        f.write(f"=== PROMPT ===\n{prompt}\n\n")

    # Build command
    cmd = build_cli_command(
        cmd=config.claude_command,
        prompt=prompt,
        model=config.claude_model,
        template=config.command_template,
        skip_permissions=config.skip_permissions,
    )

    start_time = datetime.now()

    try:
        stdout, stderr, returncode = await run_claude_async(
            cmd,
            timeout=config.task_timeout_minutes * 60,
            cwd=str(config.project_root),
        )

        duration = (datetime.now() - start_time).total_seconds()
        output = stdout
        combined_output = output + "\n" + stderr
        input_tokens, output_tokens, cost_usd = parse_token_usage(stderr)

        # Save output
        with open(log_file, "a") as f:
            f.write(f"=== OUTPUT ===\n{output}\n\n")
            f.write(f"=== STDERR ===\n{stderr}\n\n")
            f.write(f"=== RETURN CODE: {returncode} ===\n")

        # Check for API errors
        error_pattern = check_error_patterns(combined_output)
        if error_pattern:
            async with state_lock:
                state.record_attempt(
                    task_id,
                    False,
                    duration,
                    error=f"API error: {error_pattern}",
                    error_code=ErrorCode.RATE_LIMIT,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                )
            return "API_ERROR"

        # Check result markers
        has_complete = "TASK_COMPLETE" in output
        has_failed = "TASK_FAILED" in output
        implicit_success = returncode == 0 and not has_failed
        success = (has_complete and not has_failed) or implicit_success

        if success:
            hook_success, hook_error, review_status, review_findings = post_done_hook(
                task, config, True
            )
            if hook_success:
                async with state_lock:
                    state.record_attempt(
                        task_id,
                        True,
                        duration,
                        output=output,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_usd=cost_usd,
                        review_status=review_status,
                        review_findings=(review_findings[:2048] if review_findings else None),
                    )
                update_task_status(config.tasks_file, task_id, "done")
                mark_all_checklist_done(config.tasks_file, task_id)
                return True
            else:
                error = hook_error or "Post-done hook failed"
                error_code = ErrorCode.UNKNOWN
                if hook_error:
                    if "Tests failed" in hook_error:
                        error_code = ErrorCode.TEST_FAILURE
                    elif "Lint errors" in hook_error:
                        error_code = ErrorCode.LINT_FAILURE
                    elif "Review rejected" in hook_error or "Fix requested" in hook_error:
                        error_code = ErrorCode.REVIEW_REJECTED
                    else:
                        error_code = ErrorCode.HOOK_FAILURE
                full_output = output
                if hook_error:
                    full_output = f"{output}\n\n=== TEST FAILURES ===\n{hook_error}"
                async with state_lock:
                    state.record_attempt(
                        task_id,
                        False,
                        duration,
                        error=error,
                        output=full_output,
                        error_code=error_code,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cost_usd=cost_usd,
                        review_status=review_status,
                        review_findings=(review_findings[:2048] if review_findings else None),
                    )
                return False
        else:
            error_match = re.search(r"TASK_FAILED:\s*(.+)", output)
            error = error_match.group(1) if error_match else "Unknown error"
            async with state_lock:
                state.record_attempt(
                    task_id,
                    False,
                    duration,
                    error=error,
                    output=output,
                    error_code=ErrorCode.TASK_FAILED,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                )
            return False

    except TimeoutError:
        duration = config.task_timeout_minutes * 60
        async with state_lock:
            state.record_attempt(
                task_id,
                False,
                duration,
                error=f"Timeout after {config.task_timeout_minutes} minutes",
                error_code=ErrorCode.TIMEOUT,
            )
        return False

    except Exception as e:
        duration = (datetime.now() - start_time).total_seconds()
        async with state_lock:
            state.record_attempt(
                task_id,
                False,
                duration,
                error=str(e),
                error_code=ErrorCode.UNKNOWN,
            )
        return False


async def _run_tasks_parallel(args, config: ExecutorConfig):
    """Execute tasks in parallel using asyncio."""
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
            config_file=config.project_root / CONFIG_FILE,
        )
        if not pre_result.ok:
            logger.error("Validation failed before execution")
            print(format_results(pre_result))
            return

        state_lock = asyncio.Lock()
        sem = asyncio.Semaphore(config.max_concurrent)
        executed_ids: set[str] = set()

        async def run_one(task: Task) -> tuple[str, bool | str]:
            async with sem:
                result = await _execute_task_async(task, config, state, state_lock)
                return task.id, result

        include_in_progress = not getattr(args, "restart", False)
        while True:
            if check_stop_requested(config):
                clear_stop_file(config)
                logger.info("Graceful shutdown requested")
                break

            tasks = parse_tasks(config.tasks_file)
            ready = get_next_tasks(tasks, include_in_progress=include_in_progress)
            if hasattr(args, "milestone") and args.milestone:
                ready = [t for t in ready if args.milestone.lower() in t.milestone.lower()]
            ready = [t for t in ready if t.id not in executed_ids]

            if not ready or state.should_stop():
                break

            logger.info("Dispatching tasks in parallel", count=len(ready))
            for t in ready:
                logger.info("Dispatching task", task_id=t.id, name=t.name)
                executed_ids.add(t.id)

            results = await asyncio.gather(
                *[run_one(t) for t in ready],
                return_exceptions=True,
            )

            # Check for API errors
            api_error = False
            for r in results:
                if isinstance(r, tuple) and r[1] == "API_ERROR":
                    api_error = True
                    break
            if api_error:
                logger.warning("Stopping: API rate limit reached")
                break

            if state.should_stop():
                logger.warning("Stopping: failure/budget limit reached")
                break

        # Summary
        tasks = parse_tasks(config.tasks_file)
        remaining = len([t for t in tasks if t.status == "todo"])
        total_cost_val = state.total_cost()

        logger.info(
            "Execution summary (parallel)",
            completed=state.total_completed,
            failed=state.total_failed,
            remaining=remaining,
            total_cost_usd=total_cost_val if total_cost_val > 0 else None,
        )


# === CLI Commands ===


def cmd_run(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Execute tasks."""
    # HITL review incompatible with parallel/TUI modes
    if config.hitl_review and getattr(args, "parallel", False):
        logger.warning("--hitl-review ignored in parallel mode (interactive prompts not supported)")
        config.hitl_review = False
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
            def run() -> None:
                if getattr(args, "parallel", False):
                    config.create_git_branch = False
                    if getattr(args, "max_concurrent", 0) > 0:
                        config.max_concurrent = args.max_concurrent
                    asyncio.run(_run_tasks_parallel(args, config))
                else:
                    _run_tasks(args, config)

            t = threading.Thread(target=run, daemon=True)
            t.start()

        app.call_later(_start_execution)
        app.run()
        return

    if getattr(args, "parallel", False):
        # Parallel mode implies no branch
        config.create_git_branch = False
        if getattr(args, "max_concurrent", 0) > 0:
            config.max_concurrent = args.max_concurrent
        asyncio.run(_run_tasks_parallel(args, config))
    else:
        if getattr(args, "force", False):
            logger.warning("Skipping lock check (--force)")
            _run_tasks(args, config)
        else:
            # Acquire lock to prevent parallel runs
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
            config_file=config.project_root / CONFIG_FILE,
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
            return

        logger.info("Tasks to execute", count=len(tasks_to_run))
        for t in tasks_to_run:
            logger.info("Queued task", task_id=t.id, name=t.name)

        # Execute
        if args.all:
            # For --all mode, continuously re-evaluate ready tasks after each completion
            executed_ids: set[str] = set()
            include_in_progress = not getattr(args, "restart", False)
            while True:
                # Check for graceful shutdown request
                if check_stop_requested(config):
                    clear_stop_file(config)
                    logger.info("Graceful shutdown requested")
                    log_progress("🛑 Graceful shutdown requested")
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

                if result == "API_ERROR":
                    logger.warning("Stopping: API rate limit reached")
                    log_progress("⛔ Stopped: API rate limit")
                    break

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

                if result == "API_ERROR":
                    logger.warning("Stopping: API rate limit reached")
                    log_progress("⛔ Stopped: API rate limit")
                    break

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


def cmd_status(args, config: ExecutorConfig):
    """Execution status"""

    with ExecutorState(config) as state:
        # Parse tasks from tasks.md to cross-reference
        all_tasks: list[Task] = []
        if config.tasks_file.exists():
            all_tasks = parse_tasks(config.tasks_file)
        total_in_spec = len(all_tasks)

        # Calculate statistics from actual task state
        completed_tasks = sum(1 for ts in state.tasks.values() if ts.status == "success")
        failed_tasks = sum(1 for ts in state.tasks.values() if ts.status == "failed")
        running_tasks = [ts for ts in state.tasks.values() if ts.status == "running"]
        failed_attempts = sum(
            1 for ts in state.tasks.values() for a in ts.attempts if not a.success
        )

        # Find tasks in spec but not in state (pending / never started)
        state_ids = set(state.tasks.keys())
        not_started = [t for t in all_tasks if t.id not in state_ids]

        print("\n📊 Executor Status")
        print(f"{'=' * 50}")
        print(f"Tasks in spec:         {total_in_spec}")
        print(f"Tasks completed:       {completed_tasks}")
        print(f"Tasks failed:          {failed_tasks}")
        if running_tasks:
            print(f"Tasks in progress:     {len(running_tasks)}")
        if not_started:
            print(f"Tasks not started:     {len(not_started)}")
        if failed_attempts > 0:
            print(f"Failed attempts:       {failed_attempts} (retried)")
        print(
            f"Consecutive failures:  {state.consecutive_failures}/{config.max_consecutive_failures}"
        )

        # Token/cost summary
        total_cost_val = state.total_cost()
        if total_cost_val > 0:
            total_inp, total_out = state.total_tokens()

            def _fmt_tokens(n: int) -> str:
                if n >= 1000:
                    return f"{n / 1000:.1f}K"
                return str(n)

            print(
                f"Tokens:                {_fmt_tokens(total_inp)} in / {_fmt_tokens(total_out)} out"
            )
            print(f"Total cost:            ${total_cost_val:.2f}")

        # Tasks with attempts
        attempted = [ts for ts in state.tasks.values() if ts.attempts]
        if attempted:
            print("\n📝 Task History:")
            for ts in attempted:
                icon = "✅" if ts.status == "success" else "❌" if ts.status == "failed" else "🔄"
                attempts_info = f"{ts.attempt_count} attempt"
                if ts.attempt_count > 1:
                    attempts_info += "s"
                task_cost = state.task_cost(ts.task_id)
                if task_cost > 0:
                    attempts_info += f", ${task_cost:.2f}"
                print(f"   {icon} {ts.task_id}: {ts.status} ({attempts_info})")
                # Show review verdict from last attempt
                if ts.attempts:
                    last_attempt = ts.attempts[-1]
                    if last_attempt.review_status and last_attempt.review_status != "skipped":
                        print(f"      Review: {last_attempt.review_status}")
                if ts.status == "failed" and ts.last_error:
                    print(f"      Last error: {ts.last_error[:50]}...")
                elif ts.status == "running" and ts.last_error:
                    print(f"      ⚠️  Last attempt failed: {ts.last_error[:50]}...")

        # Show tasks not yet in executor state
        if not_started:
            print(f"\n⏳ Not started ({len(not_started)}):")
            for t in not_started:
                print(f"   ⬜ {t.id}: {t.name}")


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


def cmd_logs(args, config: ExecutorConfig):
    """Show task logs"""

    task_id = args.task_id.upper()
    log_files = sorted(config.logs_dir.glob(f"{task_id}-*.log"))

    if not log_files:
        logger.info("No logs found", task_id=task_id)
        return

    latest = log_files[-1]
    logger.info("Showing latest log", task_id=task_id, log_file=str(latest))
    print(latest.read_text()[:5000])  # Limit output — raw log content to stdout


def cmd_stop(args, config: ExecutorConfig):
    """Request graceful shutdown of the running executor."""
    stop_file = config.stop_file
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text(f"Stop requested at {datetime.now().isoformat()}\n")
    logger.info("Stop requested", stop_file=str(stop_file))


def cmd_reset(args, config: ExecutorConfig):
    """Reset executor state"""

    if config.state_file.exists():
        config.state_file.unlink()
        logger.info("State reset", state_file=str(config.state_file))

    clear_stop_file(config)

    if args.logs and config.logs_dir.exists():
        shutil.rmtree(config.logs_dir)
        logger.info("Logs cleared", logs_dir=str(config.logs_dir))


def cmd_plan(args, config: ExecutorConfig):
    """Interactive task planning via Claude.

    With --full flag, runs a three-stage pipeline to generate
    requirements, design, and tasks files from a description.
    """

    description = args.description

    if getattr(args, "full", False):
        from .prompt import build_generation_prompt, parse_spec_marker

        stages = ["requirements", "design", "tasks"]
        stage_files = {
            "requirements": config.requirements_file,
            "design": config.design_file,
            "tasks": config.tasks_file,
        }
        marker_names = {
            "requirements": "REQUIREMENTS",
            "design": "DESIGN",
            "tasks": "TASKS",
        }
        context: dict[str, str] = {}

        for stage in stages:
            logger.info("Generating spec", stage=stage)
            prompt = build_generation_prompt(stage, description, context)

            cmd = build_cli_command(
                cmd=config.claude_command,
                prompt=prompt,
                model=config.claude_model,
                template=config.command_template,
                skip_permissions=config.skip_permissions,
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.task_timeout_minutes * 60,
                cwd=config.project_root,
            )

            if result.returncode != 0:
                logger.error(
                    "Generation failed",
                    stage=stage,
                    stderr=result.stderr[:500],
                )
                print(f"Failed at stage: {stage}")
                sys.exit(1)

            content = parse_spec_marker(result.stdout, marker_names[stage])
            if not content:
                logger.error("No spec marker found in output", stage=stage)
                print(f"Claude did not produce {stage} content.")
                sys.exit(1)

            output_file = stage_files[stage]
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text(content + "\n")
            logger.info("Spec written", stage=stage, file=str(output_file))
            print(f"Written: {output_file}")

            context[stage] = content

        print("\nSpec generation complete!")
        print(f"  Requirements: {config.requirements_file}")
        print(f"  Design:       {config.design_file}")
        print(f"  Tasks:        {config.tasks_file}")
        return

    print(f"\n📝 Planning: {description}")
    print("=" * 60)

    # Load context
    requirements_summary = "No requirements.md found"
    if config.requirements_file.exists():
        content = config.requirements_file.read_text()
        # Extract just headers and first lines for summary
        lines = content.split("\n")[:100]
        requirements_summary = "\n".join(lines) + "\n...(truncated)"

    design_summary = "No design.md found"
    if config.design_file.exists():
        content = config.design_file.read_text()
        lines = content.split("\n")[:100]
        design_summary = "\n".join(lines) + "\n...(truncated)"

    # Get existing tasks
    existing_tasks = "No existing tasks"
    if config.tasks_file.exists():
        tasks = parse_tasks(config.tasks_file)
        task_lines = [f"- {t.id}: {t.name} ({t.status})" for t in tasks[-20:]]
        existing_tasks = "\n".join(task_lines) if task_lines else "No tasks yet"

    # Load template
    template = load_prompt_template("plan")

    if template:
        prompt = render_template(
            template,
            {
                "DESCRIPTION": description,
                "REQUIREMENTS_SUMMARY": requirements_summary,
                "DESIGN_SUMMARY": design_summary,
                "EXISTING_TASKS": existing_tasks,
            },
        )
    else:
        prompt = f"""# Task Planning Request

## Feature Description:
{description}

## Project Context:

### Requirements (excerpt):
{requirements_summary}

### Existing Tasks:
{existing_tasks}

## Instructions:

Create structured tasks for this feature. For each task use format:

### TASK-XXX: <title>
🔴 P0 | ⬜ TODO | Est: Xd

**Checklist:**
- [ ] Implementation items
- [ ] Tests

When done, respond with: PLAN_READY
"""

    log_progress(f"📝 Planning: {description}")

    # Save prompt
    log_file = config.logs_dir / f"plan-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w") as f:
        f.write(f"=== PLAN PROMPT ===\n{prompt}\n\n")

    # Interactive loop
    conversation_history = []

    while True:
        # Run Claude
        try:
            cmd = [config.claude_command, "-p", prompt]
            if config.skip_permissions:
                cmd.append("--dangerously-skip-permissions")

            print("\n🤖 Claude is analyzing...")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.task_timeout_minutes * 60,
                cwd=config.project_root,
            )

            output = result.stdout

            # Save output
            with open(log_file, "a") as f:
                f.write(f"=== OUTPUT ===\n{output}\n\n")

            # Check for API errors
            error_pattern = check_error_patterns(output + result.stderr)
            if error_pattern:
                print(f"\n⚠️  API error: {error_pattern}")
                return

            # Check for QUESTION
            question_match = re.search(r"QUESTION:\s*(.+?)(?:OPTIONS:|$)", output, re.DOTALL)
            if question_match:
                question = question_match.group(1).strip()
                print(f"\n❓ {question}")

                # Extract options
                options_match = re.search(r"OPTIONS:\s*(.+?)(?:$)", output, re.DOTALL)
                if options_match:
                    options_text = options_match.group(1)
                    options = re.findall(r"[-*]\s*(.+)", options_text)
                    if options:
                        print("\nOptions:")
                        for i, opt in enumerate(options, 1):
                            print(f"  {i}. {opt.strip()}")
                        print(f"  {len(options) + 1}. Other (type custom answer)")

                        choice = input("\nYour choice (number or text): ").strip()

                        # Determine answer
                        try:
                            idx = int(choice)
                            if 1 <= idx <= len(options):
                                answer = options[idx - 1].strip()
                            else:
                                answer = input("Enter your answer: ").strip()
                        except ValueError:
                            answer = choice

                        # Add to conversation
                        conversation_history.append(f"Q: {question}\nA: {answer}")
                        prompt = f"{prompt}\n\nPrevious Q&A:\n" + "\n".join(conversation_history)
                        prompt += f"\n\nContinue planning with the answer: {answer}"
                        continue

                # No parseable options, ask for freeform input
                answer = input("\nYour answer: ").strip()
                conversation_history.append(f"Q: {question}\nA: {answer}")
                prompt += f"\n\nAnswer: {answer}\n\nContinue planning."
                continue

            # Check for TASK_PROPOSAL or PLAN_READY
            if "PLAN_READY" in output or "TASK_PROPOSAL" in output:
                print("\n" + "=" * 60)
                print("📋 Proposed Tasks:")
                print("=" * 60)

                # Extract task proposals
                task_blocks = re.findall(
                    r"### (TASK-\d+:.+?)(?=### TASK-|\Z|PLAN_READY)",
                    output,
                    re.DOTALL,
                )

                for block in task_blocks:
                    print(f"\n### {block.strip()[:500]}")

                print("\n" + "=" * 60)

                # Ask for confirmation
                confirm = input("\nAdd these tasks to tasks.md? [y/N/edit]: ").strip().lower()

                if confirm == "y":
                    # Append tasks to tasks.md
                    tasks_file = config.tasks_file
                    content = tasks_file.read_text() if tasks_file.exists() else "# Tasks\n\n"

                    for block in task_blocks:
                        content += f"\n### {block.strip()}\n"

                    tasks_file.write_text(content)
                    print(f"\n✅ Added {len(task_blocks)} task(s) to {tasks_file}")
                    log_progress(f"✅ Created {len(task_blocks)} tasks")

                elif confirm == "edit":
                    print(f"\nEdit {config.tasks_file} manually, then run 'spec-runner run'")

                else:
                    print("\n❌ Cancelled")

                return

            # No recognizable signal, show output and exit
            print("\n📄 Claude response:")
            print(output[:2000])
            return

        except subprocess.TimeoutExpired:
            print(f"\n⏰ Planning timeout after {config.task_timeout_minutes}m")
            return
        except KeyboardInterrupt:
            print("\n\n❌ Cancelled by user")
            return
        except Exception as e:
            print(f"\n💥 Error: {e}")
            return


def cmd_validate(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Validate tasks file and config, print results."""
    from .validate import format_results, validate_all

    result = validate_all(
        tasks_file=config.tasks_file,
        config_file=config.project_root / "executor.config.yaml",
    )
    output = format_results(result)
    print(output)
    if not result.ok:
        sys.exit(1)


def cmd_tui(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Launch read-only TUI dashboard."""
    from .logging import setup_logging
    from .tui import SpecRunnerApp

    # TUI mode: log to file, TUI owns screen
    log_file = config.logs_dir / f"tui-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(level=config.log_level, tui_mode=True, log_file=log_file)

    app = SpecRunnerApp(config=config)
    app.run()


# === Main ===


def main():
    # Shared options available to every subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--max-retries", type=int, default=3, help="Max retries per task (default: 3)"
    )
    common.add_argument(
        "--timeout", type=int, default=30, help="Task timeout in minutes (default: 30)"
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
        "--parallel",
        action="store_true",
        help="Execute ready tasks in parallel (implies --no-branch)",
    )
    run_parser.add_argument(
        "--max-concurrent",
        type=int,
        default=0,
        help="Max parallel tasks (default: from config, typically 3)",
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

    # status
    subparsers.add_parser("status", parents=[common], help="Show execution status")

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

    # tui
    subparsers.add_parser("tui", parents=[common], help="Launch read-only TUI dashboard")

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

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Dispatch
    commands = {
        "run": cmd_run,
        "status": cmd_status,
        "retry": cmd_retry,
        "logs": cmd_logs,
        "stop": cmd_stop,
        "reset": cmd_reset,
        "plan": cmd_plan,
        "validate": cmd_validate,
        "tui": cmd_tui,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args, config)


if __name__ == "__main__":
    main()
