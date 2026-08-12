"""Code review module for spec-runner.

Contains review role definitions, prompt building, single and parallel
code review execution, and HITL approval gate functions.
"""

import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from .config import ExecutorConfig
from .git_ops import stage_all_except_runtime
from .logging import get_logger
from .prompt import load_prompt_template, render_template
from .runner import agent_env, check_error_patterns, log_progress
from .state import ReviewVerdict
from .task import Task

logger = get_logger("review")


def _resolve_review_template(config: ExecutorConfig, review_cmd: str) -> str:
    """Return the command template to use for the review stage.

    Resolution order:
    1. Explicit ``review_command_template`` — always wins.
    2. ``command_template`` — only when ``review_cmd`` is the *same binary* as
       ``config.claude_command`` (i.e. the exec CLI).  Prevents an exec-CLI
       template (e.g. a templated codex/qwen/copilot preset) from bleeding into
       a different review CLI.
    3. Empty string — let the runner auto-detect argv for the review CLI.
    """
    if config.review_command_template:
        return config.review_command_template
    if review_cmd == config.claude_command:
        return config.command_template
    return ""


# Review role definitions for parallel review agents
REVIEW_ROLES: dict[str, str] = {
    "quality": (
        "You are a Quality Review Agent. Focus exclusively on:\n"
        "- Bugs and logic errors\n"
        "- Security vulnerabilities (injection, auth bypass, data leaks)\n"
        "- Error handling gaps and uncaught exceptions"
    ),
    "implementation": (
        "You are an Implementation Review Agent. Focus exclusively on:\n"
        "- Whether the code achieves the stated task goals\n"
        "- Whether all checklist items are properly implemented\n"
        "- Edge cases and boundary conditions"
    ),
    "testing": (
        "You are a Testing Review Agent. Focus exclusively on:\n"
        "- Whether new code has adequate test coverage\n"
        "- Whether tests are meaningful (not trivial pass-through)\n"
        "- Missing test scenarios and edge case tests"
    ),
    "simplification": (
        "You are a Simplification Review Agent. Focus exclusively on:\n"
        "- Unnecessary complexity that can be simplified\n"
        "- Dead code or unused imports\n"
        "- Opportunities for clearer, more concise implementations"
    ),
    "docs": (
        "You are a Documentation Review Agent. Focus exclusively on:\n"
        "- Missing or outdated docstrings on public APIs\n"
        "- Misleading comments or variable names\n"
        "- README or changelog updates needed"
    ),
}


def build_review_prompt(
    task: Task,
    config: ExecutorConfig,
    cli_name: str = "",
    test_output: str | None = None,
    lint_output: str | None = None,
    previous_error: str | None = None,
) -> str:
    """The review prompt, with the frozen-files block appended (#214).

    Review is not exempt. It fixes findings by editing files — that is what
    `REVIEW_FIXED` means — so it is the second pass that can violate a claim it
    was never told about, and in the pilot run the reviewer had started editing
    the claimed file when it died. The block is appended to the base prompt, so
    every parallel role carries it too: a role's prompt is this one with a
    focus prepended.
    """
    from .claims import ESCAPE_REVIEW, append_frozen_files

    body = _render_review_prompt(task, config, cli_name, test_output, lint_output, previous_error)
    return append_frozen_files(body, config, task, escape=ESCAPE_REVIEW)


def _render_review_prompt(
    task: Task,
    config: ExecutorConfig,
    cli_name: str = "",
    test_output: str | None = None,
    lint_output: str | None = None,
    previous_error: str | None = None,
) -> str:
    """Build code review prompt for the specified CLI.

    Args:
        task: Task that was completed
        config: Executor configuration
        cli_name: CLI name for CLI-specific prompt template (e.g., 'codex', 'claude')
        test_output: Test run output to include in review context
        lint_output: Lint check output to include in review context
        previous_error: Error from previous attempt (retry context)
    """
    # Gather the task diff via `git diff HEAD~1` ONLY when this project does
    # git-based task isolation (a branch and/or commit per task). When git
    # automation is off — a subdir of a larger repo, or `--no-branch --no-commit`
    # — `git diff HEAD~1` runs against the PARENT repo and yields a huge, unrelated
    # diff that makes the reviewer slow or hang. In that case skip it.
    if config.create_git_branch or config.auto_commit:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD~1"],
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )
        changed_files = (
            result.stdout.strip() if result.returncode == 0 else "Unable to get changed files"
        )

        result = subprocess.run(
            ["git", "diff", "HEAD~1", "--stat"],
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )
        git_diff_stat = result.stdout.strip() if result.returncode == 0 else ""

        # Full diff for review context (truncated to 30KB)
        diff_p_result = subprocess.run(
            ["git", "diff", "-p", "HEAD~1"],
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )
        full_diff = diff_p_result.stdout[:30_000]
        if len(diff_p_result.stdout) > 30_000:
            full_diff += "\n... (diff truncated)"
    else:
        changed_files = "(git diff unavailable: git automation disabled for this project)"
        git_diff_stat = ""
        full_diff = ""

    # Try to load CLI-specific or custom template
    template = load_prompt_template("review", cli_name=cli_name, prompts_dir=config.prompts_dir)

    if template:
        variables = {
            "TASK_ID": task.id,
            "TASK_NAME": task.name,
            "CHANGED_FILES": changed_files,
            "GIT_DIFF": git_diff_stat,
        }
        return render_template(template, variables)

    # Build additional context sections for fallback prompt
    # Task checklist
    checklist_section = ""
    if task.checklist:
        items = "\n".join(f"- {item}" for item, _checked in task.checklist)
        checklist_section = f"\n## Task Checklist\n{items}\n"

    # Test results
    test_section = ""
    if test_output:
        test_section = f"\n## Test Results\n{test_output[:2048]}\n"

    # Lint status
    lint_section = ""
    if lint_output:
        lint_section = f"\n## Lint Status\n{lint_output[:200]}\n"

    # Previous errors
    error_section = ""
    if previous_error:
        error_section = f"\n## Previous Errors (from retry)\n{previous_error[:1024]}\n"

    # Reviewer persona system prompt
    persona_section = ""
    reviewer_persona = config.get_persona("reviewer")
    if reviewer_persona and reviewer_persona.system_prompt:
        persona_section = f"\n## Reviewer Role\n{reviewer_persona.system_prompt.strip()}\n"

    # Constitution guardrails
    constitution_section = ""
    if config.constitution_file.exists():
        constitution_text = config.constitution_file.read_text().strip()
        if constitution_text:
            constitution_section = f"\n## Constitution (Inviolable Rules)\n{constitution_text}\n"

    # Fallback to built-in prompt
    return f"""{persona_section}# Code Review Request

## Task Completed: {task.id} — {task.name}

## Changed Files:
{changed_files}

## Full Diff:
{full_diff}

## Diff Summary:
{git_diff_stat}
{checklist_section}{test_section}{lint_section}{error_section}{constitution_section}
## Review Instructions:

Launch the following review agents in parallel using the Task tool:

### 1. Quality Agent
Review the code changes for:
- Bugs and logic errors
- Security vulnerabilities
- Error handling gaps

### 2. Implementation Agent
Verify the implementation:
- Code achieves the stated task goals
- All checklist items are properly implemented
- Edge cases are handled

### 3. Testing Agent
Review test coverage:
- New code has adequate test coverage
- Tests are meaningful and not trivial

## Output:

For each issue found, describe it briefly.
If issues are found, fix them and respond with: "REVIEW_FIXED"
If no issues found, respond with: "REVIEW_PASSED"
"""


#: Ledger provenance for the single-pass reviewer.
REVIEW_PROVENANCE = "review"


def role_provenance(role: str) -> str:
    """Ledger provenance for one parallel review role.

    Per role, never one aggregate row: five roles are five paid calls, and a
    single approximate total cannot say which one was expensive or which one
    was never measured.
    """
    return f"review:{role}"


@dataclass(frozen=True)
class ReviewCall:
    """One reviewer subprocess: what it said, and what it cost.

    `text` is the CLI's *result*, extracted by `parse_cli_result` — the same
    authority the RED pass uses — rather than raw stdout, so an explicit
    claude reviewer can be asked for JSON (and thus for its cost) without the
    markers disappearing into a JSON envelope.
    """

    text: str
    stderr: str
    returncode: int
    cost_usd: float | None
    timed_out: bool = False


def _run_reviewer(
    config: ExecutorConfig,
    task_id: str,
    provenance: str,
    prompt: str,
    review_cmd: str,
    review_model: str,
    review_template: str,
) -> ReviewCall:
    """Run one reviewer subprocess and **record what it cost**.

    Every call that happened gets a ledger row: passed, failed, timed out, or
    killed by an account limit. Money spent on a call that produced nothing
    usable is still spent — the rule the RED pass already follows.

    A call that never launched (missing binary, no permission) gets no row and
    the error is re-raised for the caller's existing handler: no subprocess, no
    spend, and a row would assert one. Re-raised rather than reported as a
    return value so the operator still reads *which* binary was missing.

    Until this existed, review cost was recorded **nowhere** — not on the
    attempt row, not in the ledger — so `spec-runner costs`, `task_budget_usd`
    and `budget_usd` were all blind to a third of a TDD attempt's calls, and to
    one call per role of every parallel review (#213).
    """
    from .runner import build_cli_invocation, parse_cli_result

    invocation = build_cli_invocation(
        cmd=review_cmd,
        prompt=prompt,
        model=review_model,
        template=review_template,
        skip_permissions=config.skip_permissions,
        json_output=True,
    )
    try:
        result = subprocess.run(
            invocation.argv,
            capture_output=True,
            text=True,
            timeout=config.review_timeout_minutes * 60,
            cwd=config.project_root,
            env=agent_env(),
        )
    except subprocess.TimeoutExpired:
        # It ran, and it was billed for as long as it ran. The cost is
        # unknown — recorded as unknown, never as zero, because a zero would
        # be indistinguishable from a cheap call in every later sum.
        _record_call(config, task_id, provenance, None)
        return ReviewCall(text="", stderr="", returncode=-1, cost_usd=None, timed_out=True)
    except OSError as exc:
        logger.warning(
            "Reviewer did not launch — no ledger row",
            task_id=task_id,
            provenance=provenance,
            error=str(exc),
        )
        raise

    parsed = parse_cli_result(
        invocation.result_format, result.stdout, result.stderr, result.returncode
    )
    _record_call(config, task_id, provenance, parsed)
    return ReviewCall(
        text=parsed.text,
        stderr=result.stderr,
        returncode=result.returncode,
        cost_usd=parsed.cost_usd,
    )


#: Serialises ledger writes from the parallel review pool. Each role opens its
#: own `ExecutorState`, and five of those arriving together made SQLite return
#: "database is locked" *immediately* — `busy_timeout` does not cover a write
#: lock taken during the schema setup every connection runs. The first run of
#: `test_each_parallel_role_gets_its_own_row` lost one role's row to exactly
#: that. A review call takes minutes; serialising a millisecond write is free,
#: and losing an accounting row is the thing this change exists to stop.
_LEDGER_LOCK = threading.Lock()


def _record_call(config: ExecutorConfig, task_id: str, provenance: str, parsed: object) -> None:
    """Append the ledger row. Never allowed to affect the verdict.

    Loud on failure and swallowed all the same: an accounting problem must not
    turn "the reviewer found issues" into "the reviewer passed it", and must
    not turn a finished review into a failed task.
    """
    from .state import ExecutorState

    try:
        with _LEDGER_LOCK, ExecutorState(config) as state:
            state.record_agent_call(
                task_id,
                provenance,
                input_tokens=getattr(parsed, "input_tokens", None),
                output_tokens=getattr(parsed, "output_tokens", None),
                cost_usd=getattr(parsed, "cost_usd", None),
            )
    except Exception as exc:
        logger.warning(
            "Review cost was not recorded",
            task_id=task_id,
            provenance=provenance,
            error=str(exc),
        )


def run_code_review(
    task: Task,
    config: ExecutorConfig,
    test_output: str | None = None,
    lint_output: str | None = None,
    previous_error: str | None = None,
) -> tuple[ReviewVerdict, str | None, str | None]:
    """Run code review on completed task.

    Args:
        task: Task that was completed
        config: Executor configuration
        test_output: Test run output to include in review context
        lint_output: Lint check output to include in review context
        previous_error: Error from previous attempt (retry context)

    Returns:
        Tuple of (verdict, error_message, review_output).
    """
    log_progress("🔍 Starting code review", task.id)

    # Use review-specific command/model if configured, then persona, then main settings
    review_cmd = config.review_command or config.claude_command
    review_model = config.review_model or config.get_model_for_role("reviewer")
    review_template = _resolve_review_template(config, review_cmd)

    # Build prompt with CLI-specific template
    prompt = build_review_prompt(
        task,
        config,
        cli_name=review_cmd,
        test_output=test_output,
        lint_output=lint_output,
        previous_error=previous_error,
    )

    # Save review prompt to log
    log_file = config.logs_dir / f"{task.id}-review-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    with open(log_file, "w") as f:
        f.write(f"=== REVIEW PROMPT ===\n{prompt}\n\n")

    try:
        log_progress(
            f"🔍 Review using: {review_cmd}" + (f" ({review_model})" if review_model else ""),
            task.id,
        )

        call = _run_reviewer(
            config,
            task.id,
            REVIEW_PROVENANCE,
            prompt,
            review_cmd,
            review_model,
            review_template,
        )
        if call.timed_out:
            log_progress(
                "⏰ Code review produced no verdict: timed out after "
                f"{config.review_timeout_minutes}m",
                task.id,
            )
            return ReviewVerdict.NOT_RUN, "Review timed out", None

        output = call.text
        stderr = call.stderr
        combined_output = output + "\n" + stderr

        # Save output
        with open(log_file, "a") as f:
            f.write(f"=== OUTPUT ===\n{output}\n\n")
            f.write(f"=== STDERR ===\n{stderr}\n\n")
            f.write(f"=== RETURN CODE: {call.returncode} ===\n")
            f.write(f"=== COST: {'unknown' if call.cost_usd is None else call.cost_usd} ===\n")

        # Check for API errors
        error_pattern = check_error_patterns(combined_output)
        if error_pattern:
            log_progress(f"💥 Code review error: {error_pattern}", task.id)
            return ReviewVerdict.ERROR, f"API error: {error_pattern}", output

        # A non-zero exit means the reviewer did not finish, so whatever it
        # managed to print is not a considered verdict — including a marker
        # (Copilot, PR #156). The guard used to require empty output too, so a
        # process that crashed after printing REVIEW_PASSED was believed. The
        # output is still returned: discarding the verdict must not discard
        # what was said.
        if call.returncode != 0:
            log_progress(
                f"💥 Code review error: process exited with code {call.returncode}",
                task.id,
            )
            if stderr.strip():
                log_progress(f"   stderr: {stderr.strip()[:200]}", task.id)
            error_msg = f"Review process exited with code {call.returncode}"
            return ReviewVerdict.ERROR, error_msg, output or None

        if not output.strip():
            log_progress("⚠️ Code review produced no verdict: empty response", task.id)
            return ReviewVerdict.NOT_RUN, "Review returned empty response", None

        # Check review result (case-insensitive, check both stdout and stderr)
        output_upper = combined_output.upper()
        if "REVIEW_PASSED" in output_upper:
            log_progress("✅ Code review passed", task.id)
            return ReviewVerdict.PASSED, None, output
        elif "REVIEW_FIXED" in output_upper:
            log_progress("✅ Code review: issues fixed", task.id)
            # Commit the fixes — runtime state stays out of the commit (#62)
            if stage_all_except_runtime(config):
                commit_result = subprocess.run(
                    ["git", "commit", "-m", f"{task.id}: code review fixes"],
                    capture_output=True,
                    text=True,
                    cwd=config.project_root,
                )
                if commit_result.returncode != 0:
                    logger.warning(
                        "Review fix commit failed",
                        stderr=commit_result.stderr.strip()[:200],
                    )
            return ReviewVerdict.FIXED, None, output
        elif "REVIEW_FAILED" in output_upper:
            log_progress("❌ Code review found unresolved issues", task.id)
            preview = output.strip()[-300:]
            log_progress(f"   Review output (last 300 chars): {preview}", task.id)
            return ReviewVerdict.FAILED, "Review found issues", output
        else:
            # #138: this used to return PASSED — "the agent said nothing I
            # recognize" was recorded, and displayed with a tick, as a clean
            # review. Silence is not approval: the reviewer may have produced
            # prose, hit its context limit, or misunderstood the protocol, and
            # none of those is evidence about the code.
            preview = output.strip()[-200:] if output.strip() else "(empty)"
            log_progress("⚠️ Code review produced no verdict: no status marker", task.id)
            log_progress(f"   Review output (last 200 chars): {preview}", task.id)
            return ReviewVerdict.NOT_RUN, "Review produced no verdict marker", output

    # No `except subprocess.TimeoutExpired` here: the subprocess is owned by
    # `_run_reviewer`, which turns a timeout into a recorded call and a
    # `timed_out` result. Catching it here again would mean a path that spent
    # money and wrote no ledger row.
    except Exception as e:
        log_progress(f"💥 Code review error: {e}", task.id)
        return ReviewVerdict.ERROR, str(e), None


def _run_single_role_review(
    role: str,
    role_prompt: str,
    base_prompt: str,
    review_cmd: str,
    review_model: str,
    review_template: str,
    config: ExecutorConfig,
    task_id: str,
) -> tuple[str, ReviewVerdict, str]:
    """Run a single role-specific review. Returns (role, verdict, output).

    Its cost is recorded under its own provenance, `review:<role>`. One
    aggregate row for the whole parallel pass would hide which role was
    expensive and which one was never measured at all.
    """
    full_prompt = f"{role_prompt}\n\n{base_prompt}"
    try:
        call = _run_reviewer(
            config,
            task_id,
            role_provenance(role),
            full_prompt,
            review_cmd,
            review_model,
            review_template,
        )
        if call.timed_out:
            return role, ReviewVerdict.NOT_RUN, f"Review timeout ({role})"
        output = call.text + "\n" + call.stderr
        if call.returncode != 0:
            # Same rule as the single path: a crashed role has no verdict.
            return role, ReviewVerdict.ERROR, output
        output_upper = output.upper()
        if "REVIEW_FAILED" in output_upper:
            return role, ReviewVerdict.FAILED, output
        elif "REVIEW_FIXED" in output_upper:
            return role, ReviewVerdict.FIXED, output
        elif "REVIEW_PASSED" in output_upper:
            return role, ReviewVerdict.PASSED, output
        # Same rule as the single-review path (#138): no marker means this role
        # produced no verdict, not that it approved.
        return role, ReviewVerdict.NOT_RUN, output
    except Exception as e:
        return role, ReviewVerdict.ERROR, str(e)


def run_parallel_review(
    task: Task,
    config: ExecutorConfig,
    test_output: str | None = None,
    lint_output: str | None = None,
    previous_error: str | None = None,
) -> tuple[ReviewVerdict, str | None, str | None]:
    """Run multiple review agents in parallel, one per role.

    Each role gets a specialized focus prompt prepended to the base review prompt.
    Verdicts are aggregated: any FAILED → overall FAILED.
    """
    log_progress(f"🔍 Starting parallel review ({len(config.review_roles)} roles)", task.id)

    review_cmd = config.review_command or config.claude_command
    review_model = config.review_model or config.get_model_for_role("reviewer")
    review_template = _resolve_review_template(config, review_cmd)

    base_prompt = build_review_prompt(
        task,
        config,
        cli_name=review_cmd,
        test_output=test_output,
        lint_output=lint_output,
        previous_error=previous_error,
    )

    # Get role prompts for configured roles
    roles_to_run = [
        (role, REVIEW_ROLES[role]) for role in config.review_roles if role in REVIEW_ROLES
    ]

    if not roles_to_run:
        log_progress("⚠️ No valid review roles configured, falling back to single review", task.id)
        return run_code_review(task, config, test_output, lint_output, previous_error)

    # Run reviews in parallel using threads (each is a subprocess call)
    results: list[tuple[str, ReviewVerdict, str]] = []
    with ThreadPoolExecutor(max_workers=len(roles_to_run)) as pool:
        futures = [
            pool.submit(
                _run_single_role_review,
                role,
                role_prompt,
                base_prompt,
                review_cmd,
                review_model,
                review_template,
                config,
                task.id,
            )
            for role, role_prompt in roles_to_run
        ]
        for future in futures:
            results.append(future.result())

    # Aggregate verdicts. Precedence (#138): concrete findings first, then a
    # broken reviewer, then one that produced nothing — a role that never
    # answered must never be averaged away into an overall "passed".
    all_outputs: list[str] = []
    has_failed = has_error = has_not_run = has_fixed = False
    for role, verdict, output in results:
        log_progress(f"  📋 {role}: {verdict.value}", task.id)
        all_outputs.append(f"=== {role.upper()} REVIEW ===\n{output[:2000]}")
        if verdict == ReviewVerdict.FAILED:
            has_failed = True
        elif verdict == ReviewVerdict.ERROR:
            has_error = True
        elif verdict == ReviewVerdict.NOT_RUN:
            has_not_run = True
        elif verdict == ReviewVerdict.FIXED:
            has_fixed = True

    # Precedence: findings first, then fixes, then a reviewer that broke or
    # never answered. FIXED outranks ERROR/NOT_RUN deliberately (Copilot,
    # PR #156): it is the verdict the pipeline acts on — `post_done_hook`
    # re-runs tests and lint on FIXED — and a role that edited the tree must
    # get those gates regardless of what the *other* roles managed to return.
    # Ranking a silent role above it left the fixes to be swept up by the
    # general auto-commit later, ungated. The silent roles are not lost: each
    # is logged above and named in the returned reason.
    silent = [
        f"{role}: {verdict.value}"
        for role, verdict, _ in results
        if verdict in (ReviewVerdict.NOT_RUN, ReviewVerdict.ERROR)
    ]
    if has_failed:
        overall_verdict = ReviewVerdict.FAILED
    elif has_fixed:
        overall_verdict = ReviewVerdict.FIXED
    elif has_error:
        overall_verdict = ReviewVerdict.ERROR
    elif has_not_run:
        overall_verdict = ReviewVerdict.NOT_RUN
    else:
        overall_verdict = ReviewVerdict.PASSED

    if has_fixed:
        # Committing is driven by "a role changed the tree", not by the overall
        # verdict: leaving the edits uncommitted here hands them to the general
        # auto-commit, which runs no gates.
        # Commit fixes from any review agent — minus runtime state (#62)
        try:
            staged = stage_all_except_runtime(config)
        except RuntimeError as exc:
            logger.warning("Staging failed after parallel review fixes", error=str(exc))
            staged = False
        if staged:
            subprocess.run(
                ["git", "commit", "-m", f"{task.id}: parallel review fixes"],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )

    combined_output = "\n\n".join(all_outputs)
    log_progress(f"🔍 Parallel review result: {overall_verdict.value}", task.id)

    # The reason keeps every role that produced nothing, whatever the overall
    # verdict turned out to be — otherwise a silent reviewer disappears from
    # the record the moment another role has something to say.
    reasons: list[str] = []
    if overall_verdict == ReviewVerdict.FAILED:
        reasons.append("Review found issues")
    if silent:
        reasons.append("no verdict from " + ", ".join(silent))
    error = "; ".join(reasons) or None
    return overall_verdict, error, combined_output


def format_review_findings(task_id: str, task_name: str, review_output: str) -> str:
    """Format review findings for HITL display."""
    separator = "=" * 50
    return (
        f"\n{separator}\nReview: {task_id} — {task_name}\n{separator}\n\n{review_output[:3000]}\n"
    )


def prompt_hitl_verdict() -> str:
    """Prompt user for HITL review verdict.

    Returns:
        One of: 'approve', 'reject', 'fix', 'skip'.
    """
    print("\n  [a]pprove  [r]eject  [f]ix-and-retry  [s]kip")
    while True:
        choice = input("> ").strip().lower()
        if choice in ("a", "approve"):
            return "approve"
        elif choice in ("r", "reject"):
            return "reject"
        elif choice in ("f", "fix"):
            return "fix"
        elif choice in ("s", "skip"):
            return "skip"
        print("  Invalid choice. Use: a, r, f, or s")
