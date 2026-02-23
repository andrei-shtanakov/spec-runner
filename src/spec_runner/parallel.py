"""Parallel task execution — async wrappers with semaphore control."""

import asyncio
import re
from datetime import datetime

from .config import CONFIG_FILE, ExecutorConfig
from .hooks import post_done_hook, pre_start_hook
from .logging import get_logger
from .prompt import build_task_prompt, extract_test_failures
from .runner import (
    build_cli_command,
    check_error_patterns,
    log_progress,
    parse_token_usage,
    run_claude_async,
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
    mark_all_checklist_done,
    parse_tasks,
    update_task_status,
)

logger = get_logger("executor")


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
            return False

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

            await asyncio.gather(
                *[run_one(t) for t in ready],
                return_exceptions=True,
            )

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
