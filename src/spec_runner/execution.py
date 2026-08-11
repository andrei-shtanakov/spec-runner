"""Task execution core: execute_task, retry strategy, run_with_retries."""

import re
import subprocess
import time
from datetime import datetime

from .config import ExecutorConfig
from .errors import classify
from .harness import HarnessBaseline
from .hooks import post_done_hook, pre_start_hook
from .logging import get_logger
from .prompt import build_task_prompt, extract_test_failures
from .runner import (
    agent_env,
    build_cli_invocation,
    check_error_patterns,
    log_progress,
    parse_cli_result,
    send_callback,
)
from .stages import StageReporter
from .state import (
    ErrorCode,
    ExecutorState,
    PhaseOutcome,
    RetryContext,
)
from .task import (
    Task,
    update_task_status,
)

logger = get_logger("execution")


# === Task Executor ===


def _refusal_error_code(refusal: str) -> ErrorCode:
    """Classify a gate refusal: a verdict on the work, or a broken instrument.

    The two need different answers from whoever reads the run — one says fix
    the code, the other says fix the environment — so they must not collapse
    into one `HOOK_FAILURE`.
    """
    from .hooks import GATE_INSTRUMENT_ERROR_PREFIX

    if refusal.startswith(GATE_INSTRUMENT_ERROR_PREFIX):
        return ErrorCode.INFRASTRUCTURE
    return ErrorCode.HOOK_FAILURE


def _run_red_phase_gate(task, config, state, reporter) -> str | None:
    """Author and confirm a red, then ask the gate. Returns a refusal, or None.

    The two halves are deliberately separate. `run_red_phase` *observes* — it
    authors, commits and replays, and records whatever it found, including a
    refuted claim. The gate *decides*. Folding the decision into the observer
    is how the review policy and this one would drift apart, which is the whole
    reason #164 exists as its own mechanism.
    """
    from .gates import GateContext, GateStatus, ensure_red_gate, evaluate_gates
    from .tdd import run_red_phase

    ensure_red_gate()
    reporter.enter("tests")
    red = run_red_phase(task, config, state, log_progress=lambda line: log_progress(line, task.id))

    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=config.project_root,
        capture_output=True,
        text=True,
    )
    outcome = evaluate_gates(
        "tests",
        GateContext(
            task_id=task.id,
            checkpoint_sha=head.stdout.strip() if head.returncode == 0 else "",
            config=config,
            state=state,
            facts={"execution_mode": "tdd"},
        ),
    )
    if outcome.status is GateStatus.SATISFIED:
        return None

    detail = "; ".join(
        r.detail or "" for r in outcome.results if r.status is not GateStatus.SATISFIED
    )
    if outcome.status is GateStatus.INSTRUMENT_ERROR:
        return f"RED could not be verified (infrastructure): {detail or red.detail}"
    return f"RED not confirmed, refusing to implement: {detail or red.detail}"


def execute_task(
    task: Task,
    config: ExecutorConfig,
    state: ExecutorState,
    harness_baseline: HarnessBaseline | None = None,
) -> bool | str:
    """Execute a single task via Claude CLI.

    Args:
        harness_baseline: the task's harness snapshot holder, shared across
            retries (#137). Omit it and this attempt takes its own snapshot —
            fine for a one-shot call, wrong inside a retry loop, where a
            per-attempt snapshot lets a forbidden edit survive one failure and
            become the next attempt's baseline.

    Returns:
        True if successful, False if failed (including rate limits),
        or "HOOK_ERROR" if pre-start hook failed (fail fast, no retries).
    """

    task_id = task.id
    # Slice 0: typed phase outcomes go to the state DB alongside the progress
    # mirror. Recording is best-effort inside `record_phase`; nothing gates on
    # it, so execution and terminal state are unchanged.
    reporter = StageReporter(
        task.id,
        lambda line: log_progress(line),
        sink=lambda phase, outcome, detail: state.record_phase(task.id, phase, outcome, detail),
    )
    log_progress(f"\U0001f680 Starting: {task.name}", task_id)
    logger.info("Executing task", task_id=task_id, name=task.name)

    # Pre-start hook
    if not pre_start_hook(task, config, reporter=reporter):
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

    # RED phase (#141). Under `tdd` the implementation pass does not run until
    # a red has been *demonstrated* — authored, committed, and replayed against
    # that commit. The gate is what refuses, not this code: TDD is a consumer
    # of #164's mechanism, so that the review policy and this one cannot drift.
    if config.resolve_execution_mode(task) == "tdd":
        refusal = _run_red_phase_gate(task, config, state, reporter)
        if refusal is not None:
            log_progress(f"⛔ {refusal}", task_id)
            state.record_attempt(
                task_id,
                False,
                0.0,
                error=refusal,
                error_code=_refusal_error_code(refusal),
            )
            return False

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
        # Use implementer persona model if configured
        task_model = config.get_model_for_role("implementer")
        # NOTE: claude's native --max-budget-usd is intentionally NOT passed here.
        # build_cli_invocation supports it, but enforcing a hard mid-call cap turns
        # a slight per-task overage into a hard failure and made `doctor --cli=claude`
        # fail on its small default budget. Budget stays state-based (post-attempt)
        # as before; wiring the native cap is a separate, considered follow-up.
        invocation = build_cli_invocation(
            cmd=config.claude_command,
            prompt=prompt,
            model=task_model,
            template=config.command_template,
            skip_permissions=config.skip_permissions,
            json_output=True,
        )

        logger.info(
            "Running CLI command",
            command=config.claude_command,
            model=task_model or "(default)",
            skip_permissions=config.skip_permissions,
        )

        # Harness tripwire (#64): snapshot the verification surface before
        # the agent gets write access to it. Taken here — after
        # pre_start_hook — so `uv sync` rewriting uv.lock is not an agent
        # mutation. #137: the snapshot belongs to the task, not the attempt,
        # so a retry cannot re-baseline a forbidden edit into legitimacy.
        from .harness import harness_violations

        harness_before = (harness_baseline or HarnessBaseline()).capture(config)

        reporter.enter("exec")
        result = subprocess.run(
            invocation.argv,
            capture_output=True,
            text=True,
            timeout=config.task_timeout_minutes * 60,
            cwd=config.project_root,
            env=agent_env(),
        )

        duration = (datetime.now() - start_time).total_seconds()
        cli_result = parse_cli_result(
            invocation.result_format, result.stdout, result.stderr, result.returncode
        )
        output = cli_result.text
        combined_output = output + "\n" + result.stderr
        input_tokens = cli_result.input_tokens
        output_tokens = cli_result.output_tokens
        cost_usd = cli_result.cost_usd

        # Save output
        with open(log_file, "a") as f:
            f.write(f"=== OUTPUT ===\n{output}\n\n")
            f.write(f"=== STDERR ===\n{result.stderr}\n\n")
            f.write(f"=== RETURN CODE: {result.returncode} ===\n")

        # Check for API errors (rate limits, etc.)
        error_pattern = check_error_patterns(combined_output)

        # Record the exec outcome from the *same* signals that decide the
        # attempt, not from the return code alone (Copilot, PR #167): a rate
        # limit or an is_error payload arrives with exit 0, and recording that
        # as a pass would make the new evidence disagree with the verdict it
        # sits next to.
        # `exec` is about the *process*: did the agent run to completion. What
        # it then said about the work belongs to `parse`. So a non-zero exit,
        # an explicit `is_error` payload and an API pattern are all ERROR —
        # the same call #138 made for a review whose CLI did not finish.
        # (`is_error` is the return code for text CLIs and the payload flag for
        # claude's JSON, so the three collapse into one honest signal.)
        if error_pattern or cli_result.is_error or result.returncode != 0:
            reporter.record(
                PhaseOutcome.ERROR,
                error_pattern or f"exit {result.returncode}",
            )
        else:
            reporter.record(PhaseOutcome.PASS, f"exit {result.returncode}")

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
        # #140: a deliberate escalation — "this cannot be done within the
        # rules, an operator is needed" — as opposed to "I did not manage it".
        # It outranks both other markers: an agent that says COMPLETE and
        # BLOCKED has not finished, and one that says FAILED and BLOCKED has
        # given a reason no retry can resolve.
        has_blocked_marker = "TASK_BLOCKED" in output
        implicit_success = (
            result.returncode == 0
            and not has_failed_marker
            and not has_blocked_marker
            and not cli_result.is_error
        )
        success = (
            has_complete_marker
            and not has_failed_marker
            and not has_blocked_marker
            and not cli_result.is_error
        ) or implicit_success

        # Harness tripwire (#64): check BEFORE the gates run — a mutated
        # oracle makes their verdict worthless. strict fails the attempt
        # (the message feeds the retry prompt); warn logs provenance.
        violations = harness_violations(config, harness_before)
        if violations:
            summary = ", ".join(violations)
            if config.harness_guard == "strict":
                error = (
                    "Harness guard: the agent modified verification files: "
                    f"{summary}. These files define how the task is verified "
                    "and must not be changed by the task. Revert them or, if "
                    "the change is intentional, exempt it via harness_allow."
                )
                log_progress(f"⛔ Harness guard: {summary}", task_id)
                logger.error("Harness files mutated by agent", violations=violations)
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
                    error_stage=reporter.current,
                )
                send_callback(config.callback_url, task_id, "failed", duration, error)
                return False
            log_progress(f"⚠️ Harness files changed by agent: {summary}", task_id)
            logger.warning("Harness files mutated by agent", violations=violations)

        if success:
            reporter.enter("parse")
            reporter.record(PhaseOutcome.PASS, "completion marker recognized")
            if has_complete_marker:
                logger.info("Task completed by Claude", task_id=task_id)
            else:
                logger.info("Implicit success (return code 0)", task_id=task_id)

            # Post-done hook (tests, lint, review)
            hook_success, hook_error, review_status, review_findings, hook_no_op = post_done_hook(
                task, config, True, reporter=reporter
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
                    no_op=hook_no_op,
                )
                if hook_no_op:
                    log_progress("✔️ No-op: completed without changes", task_id)
                # NOTE: tasks.md "done" status + checklist are now written inside
                # post_done_hook (before the commit) so they get committed/merged.
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
                        # Distinguishes "the gate says no" from "the gate could
                        # not answer" — the second is an instrument failure and
                        # the run exits 2 rather than 1.
                        error_code = _refusal_error_code(hook_error)
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
            # record_for, not enter: `reporter.current` feeds `error_stage`,
            # and a failure here belongs to `exec`, not to `parse`.
            reporter.record_for(
                "parse",
                PhaseOutcome.UNEXPECTED_FAIL
                if (has_failed_marker or has_blocked_marker)
                else PhaseOutcome.NOT_RUN,
                # Three distinguishable cases: a deliberate escalation, a
                # reported failure, and output nobody could read. Collapsing
                # them made the evidence for a blocked task say "no completion
                # marker", which is simply untrue (Copilot, PR #167).
                "blocked marker"
                if has_blocked_marker
                else ("failure marker" if has_failed_marker else "no completion marker"),
            )
            blocked_match = re.search(r"TASK_BLOCKED:\s*(.+)", output)
            error_match = re.search(r"TASK_FAILED:\s*(.+)", output)
            if has_blocked_marker:
                # The agent's own words, verbatim: the operator has to act on
                # this escalation, and a paraphrase is not actionable (#140).
                # A bare marker with no reason is still terminal — refusing to
                # retry a stated refusal is the safe side of that ambiguity.
                error = (
                    blocked_match.group(1)
                    if blocked_match
                    else "agent reported TASK_BLOCKED without a reason"
                )
                error_kind = "blocked"
            elif error_match:
                error = error_match.group(1)
                error_kind = "cli_error"
            else:
                error_kind, error = classify(combined_output, result.returncode)
            state.record_attempt(
                task_id,
                False,
                duration,
                error=error,
                output=output,
                error_code=(
                    ErrorCode.TASK_BLOCKED if has_blocked_marker else ErrorCode.TASK_FAILED
                ),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
                error_kind=error_kind,
                error_stage=reporter.current,
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
        # #140: retrying a deliberate escalation spends time and tokens on the
        # knowingly impossible, and the "do not repeat the same mistake" retry
        # prompt pushes the agent to work around the very rule it just
        # honoured — on TASK-025 attempt 2 crossed a scope boundary that
        # attempt 1 had correctly escalated about.
        ErrorCode.TASK_BLOCKED,
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
        return float(min(30.0 * (2**attempt), 300.0))
    return float(base_delay * (attempt + 1))


def _check_task_budget(
    task_id: str,
    config: ExecutorConfig,
    state: ExecutorState,
    attempt_index: int,
) -> str | None:
    """Return a budget-exceeded error message, or None if OK to proceed.

    Two independent caps (LABS-41):
    - `task_budget_usd` — hard ceiling on total task cost (all attempts).
    - `max_retry_cost_usd` — cap on cumulative cost of retries only,
      i.e. attempts 2..N. The initial attempt (`attempt_index == 0`)
      always runs regardless of this key so flaky tasks can fail fast
      rather than never getting a chance.
    """
    spent = state.task_cost(task_id)
    if config.task_budget_usd is not None and spent >= config.task_budget_usd:
        return f"Task budget exceeded (${spent:.2f} >= ${config.task_budget_usd:.2f})"

    if config.max_retry_cost_usd is not None and attempt_index > 0:
        ts = state.get_task_state(task_id)
        retry_spent = sum(a.cost_usd or 0.0 for a in ts.attempts[1:]) if ts else 0.0
        if retry_spent >= config.max_retry_cost_usd:
            return f"Retry budget exceeded (${retry_spent:.2f} >= ${config.max_retry_cost_usd:.2f})"
    return None


def _fail_for_budget(
    task: Task,
    config: ExecutorConfig,
    state: ExecutorState,
    message: str,
) -> None:
    """Record a BUDGET_EXCEEDED attempt and mark the task failed."""
    log_progress(message, task.id)
    state.record_attempt(
        task.id,
        False,
        0.0,
        error=message,
        error_code=ErrorCode.BUDGET_EXCEEDED,
    )
    # LABS-41: budget exhaustion is terminal, not a dependency wait. That
    # distinction lives in the state DB (status "failed", error_code
    # BUDGET_EXCEEDED) — tasks.md has a five-status vocabulary and no "failed"
    # in it, so writing one raised KeyError and took the whole run tail with
    # it (#127). "blocked" is what every other terminal failure writes here.
    update_task_status(config.tasks_file, task.id, "blocked")


def run_with_retries(
    task: Task,
    config: ExecutorConfig,
    state: ExecutorState,
    harness_baseline: HarnessBaseline | None = None,
) -> bool | str:
    """Execute task with retries.

    Args:
        harness_baseline: normally omitted — one baseline is created here and
            shared by every attempt of this task (#137). It is a parameter
            only so the operator-driven "retry" branch below can carry the
            same baseline into its recursive call instead of re-snapshotting a
            working tree the previous attempt may have left dirty.

    Returns:
        True if successful, False if failed, or "SKIP" if task was skipped.
    """

    task_state = state.get_task_state(task.id)
    # One snapshot per task lifecycle, captured lazily on the first attempt
    # (after its pre_start_hook) and replayed for every retry. A per-attempt
    # snapshot let a forbidden harness edit that survived a failed attempt
    # become the next attempt's baseline — the guard blocked exactly once and
    # was disarmed by persistence (#137).
    if harness_baseline is None:
        harness_baseline = HarnessBaseline()

    for attempt in range(task_state.attempt_count, config.max_retries):
        # Pre-attempt budget check (LABS-41): stop BEFORE burning another
        # attempt if the caps are already exhausted.
        pre_msg = _check_task_budget(task.id, config, state, attempt)
        if pre_msg is not None:
            _fail_for_budget(task, config, state, pre_msg)
            return False

        log_progress(f"\U0001f4cd Attempt {attempt + 1}/{config.max_retries}", task.id)

        result = execute_task(task, config, state, harness_baseline=harness_baseline)

        # Hook error -- always fatal, stop immediately (no error_code recorded)
        if result == "HOOK_ERROR":
            return False

        # Post-attempt budget check: catches cases where a single expensive
        # attempt pushed us over the cap.
        post_msg = _check_task_budget(task.id, config, state, attempt + 1)
        if post_msg is not None:
            _fail_for_budget(task, config, state, post_msg)
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

    # Notify on task failure
    from .notifications import notify_task_failed

    notify_task_failed(config, task.id, task_state.last_error or "Retries exhausted")

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
            return run_with_retries(task, config, state, harness_baseline=harness_baseline)
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
