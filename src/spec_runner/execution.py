"""Task execution core: execute_task, retry strategy, run_with_retries."""

import re
import subprocess
import time
from datetime import datetime

from .config import ExecutorConfig
from .hooks import post_done_hook, pre_start_hook
from .logging import get_logger
from .prompt import build_task_prompt, extract_test_failures
from .runner import (
    build_cli_command,
    check_error_patterns,
    log_progress,
    parse_token_usage,
    send_callback,
)
from .state import (
    ErrorCode,
    ExecutorState,
    RetryContext,
)
from .task import (
    Task,
    mark_all_checklist_done,
    update_task_status,
)

logger = get_logger("execution")


# === Task Executor ===


def execute_task(task: Task, config: ExecutorConfig, state: ExecutorState) -> bool | str:
    """Execute a single task via Claude CLI.

    Returns:
        True if successful, False if failed (including rate limits),
        or "HOOK_ERROR" if pre-start hook failed (fail fast, no retries).
    """

    task_id = task.id
    log_progress(f"\U0001f680 Starting: {task.name}", task_id)
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
            log_progress(f"\u26a0\ufe0f API error detected: {error_pattern}", task_id)
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
                log_progress(f"\u2705 Completed in {duration:.1f}s", task_id)
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
                log_progress("\u274c Failed: tests/lint check", task_id)
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
            log_progress(f"\u274c Failed: {error[:50]}", task_id)
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
        log_progress(f"\u23f0 Timeout after {config.task_timeout_minutes}m", task_id)
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
        log_progress(f"\U0001f4a5 Error: {error[:50]}", task_id)
        send_callback(config.callback_url, task_id, "failed", duration, error)
        return False


# === Retry Strategy ===

_FATAL_ERRORS = frozenset(
    {
        ErrorCode.HOOK_FAILURE,
        ErrorCode.REVIEW_REJECTED,
        ErrorCode.BUDGET_EXCEEDED,
        ErrorCode.INTERRUPTED,
    }
)

_EXPONENTIAL_ERRORS = frozenset(
    {
        ErrorCode.RATE_LIMIT,
    }
)


def classify_retry_strategy(error_code: ErrorCode | str) -> str:
    """Classify error into retry strategy.

    Returns:
        "fatal" -- no retry, "backoff_exponential" -- long increasing delays,
        "backoff_linear" -- short increasing delays.
    """
    code = ErrorCode(error_code) if isinstance(error_code, str) else error_code
    if code in _FATAL_ERRORS:
        return "fatal"
    if code in _EXPONENTIAL_ERRORS:
        return "backoff_exponential"
    return "backoff_linear"


def compute_retry_delay(error_code: ErrorCode | str, attempt: int, base_delay: int = 5) -> float:
    """Compute delay before next retry based on error type and attempt number.

    Args:
        error_code: The error that caused the failure.
        attempt: Zero-based attempt index.
        base_delay: Base delay in seconds for linear backoff (not used for exponential).
            Exponential backoff uses a fixed 30s base since rate limits need longer waits.
    """
    strategy = classify_retry_strategy(error_code)
    if strategy == "fatal":
        return 0.0
    if strategy == "backoff_exponential":
        return min(30.0 * (2**attempt), 300.0)
    return float(base_delay * (attempt + 1))


def run_with_retries(task: Task, config: ExecutorConfig, state: ExecutorState) -> bool | str:
    """Execute task with retries.

    Returns:
        True if successful, False if failed, or "SKIP" if task was skipped.
    """

    task_state = state.get_task_state(task.id)

    for attempt in range(task_state.attempt_count, config.max_retries):
        log_progress(f"\U0001f4cd Attempt {attempt + 1}/{config.max_retries}", task.id)

        result = execute_task(task, config, state)

        # Hook error -- always fatal, stop immediately (no error_code recorded)
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

        # Get last error code from state
        ts = state.get_task_state(task.id)
        last_error_code = ErrorCode.UNKNOWN
        if ts and ts.attempts:
            last = ts.attempts[-1]
            if last.error_code:
                last_error_code = last.error_code

        # Fatal errors -- no retry
        if classify_retry_strategy(last_error_code) == "fatal":
            log_progress(f"Fatal error ({last_error_code.value}) -- no retry", task.id)
            return False

        if attempt < config.max_retries - 1:
            delay = compute_retry_delay(last_error_code, attempt, config.retry_delay_seconds)
            logger.info(
                "Waiting before retry",
                task_id=task.id,
                delay_seconds=delay,
                error_code=last_error_code.value,
                strategy=classify_retry_strategy(last_error_code),
            )
            time.sleep(delay)

    # Task failed after all retries
    log_progress(f"\u274c Failed after {config.max_retries} attempts", task.id)

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
        # Interactive prompt -- keep print() for user-facing menu
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
            log_progress("\u23ed\ufe0f Skipped, continuing to next task", task.id)
            return "SKIP"

    else:  # "skip" (default)
        update_task_status(config.tasks_file, task.id, "blocked")
        log_progress("\u23ed\ufe0f Skipped, continuing to next task", task.id)
        return "SKIP"
