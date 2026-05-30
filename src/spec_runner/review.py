"""Code review module for spec-runner.

Contains review role definitions, prompt building, single and parallel
code review execution, and HITL approval gate functions.
"""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from .config import ExecutorConfig
from .logging import get_logger
from .prompt import load_prompt_template, render_template
from .runner import build_cli_command, check_error_patterns, log_progress
from .state import ReviewVerdict
from .task import Task

logger = get_logger("review")

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
    """Build code review prompt for the specified CLI.

    Args:
        task: Task that was completed
        config: Executor configuration
        cli_name: CLI name for CLI-specific prompt template (e.g., 'codex', 'claude')
        test_output: Test run output to include in review context
        lint_output: Lint check output to include in review context
        previous_error: Error from previous attempt (retry context)
    """
    # Get changed files from git
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD~1"],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )
    changed_files = (
        result.stdout.strip() if result.returncode == 0 else "Unable to get changed files"
    )

    # Get git diff stat
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

    # Try to load CLI-specific or custom template
    template = load_prompt_template("review", cli_name=cli_name)

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
    review_template = config.review_command_template or config.command_template

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
        # Build command using template or auto-detect
        cmd = build_cli_command(
            cmd=review_cmd,
            prompt=prompt,
            model=review_model,
            template=review_template,
            skip_permissions=config.skip_permissions,
        )

        log_progress(
            f"🔍 Review using: {review_cmd}" + (f" ({review_model})" if review_model else ""),
            task.id,
        )

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.review_timeout_minutes * 60,
            cwd=config.project_root,
        )

        output = result.stdout
        stderr = result.stderr
        combined_output = output + "\n" + stderr

        # Save output
        with open(log_file, "a") as f:
            f.write(f"=== OUTPUT ===\n{output}\n\n")
            f.write(f"=== STDERR ===\n{stderr}\n\n")
            f.write(f"=== RETURN CODE: {result.returncode} ===\n")

        # Check for API errors
        error_pattern = check_error_patterns(combined_output)
        if error_pattern:
            log_progress(f"⚠️ Review API error: {error_pattern}", task.id)
            return ReviewVerdict.FAILED, f"API error: {error_pattern}", output

        # Check for empty or failed response
        if result.returncode != 0 and not output.strip():
            log_progress(
                f"⚠️ Review process failed (exit code {result.returncode})",
                task.id,
            )
            if stderr.strip():
                log_progress(f"   stderr: {stderr.strip()[:200]}", task.id)
            error_msg = f"Review process exited with code {result.returncode}"
            return ReviewVerdict.FAILED, error_msg, None

        if not output.strip():
            log_progress("⚠️ Review returned empty response", task.id)
            return ReviewVerdict.FAILED, "Review returned empty response", None

        # Check review result (case-insensitive, check both stdout and stderr)
        output_upper = combined_output.upper()
        if "REVIEW_PASSED" in output_upper:
            log_progress("✅ Code review passed", task.id)
            return ReviewVerdict.PASSED, None, output
        elif "REVIEW_FIXED" in output_upper:
            log_progress("✅ Code review: issues fixed", task.id)
            # Commit the fixes
            subprocess.run(["git", "add", "-A"], capture_output=True, cwd=config.project_root)
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
            # No explicit marker — treat as passed but log for visibility
            preview = output.strip()[-200:] if output.strip() else "(empty)"
            log_progress("✅ Code review completed (no explicit status marker)", task.id)
            log_progress(f"   Review output (last 200 chars): {preview}", task.id)
            return ReviewVerdict.PASSED, None, output

    except subprocess.TimeoutExpired:
        log_progress(f"⏰ Review timeout after {config.review_timeout_minutes}m", task.id)
        return ReviewVerdict.FAILED, "Review timed out", None
    except Exception as e:
        log_progress(f"💥 Review error: {e}", task.id)
        return ReviewVerdict.FAILED, str(e), None


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
    """Run a single role-specific review. Returns (role, verdict, output)."""
    full_prompt = f"{role_prompt}\n\n{base_prompt}"
    cmd = build_cli_command(
        cmd=review_cmd,
        prompt=full_prompt,
        model=review_model,
        template=review_template,
        skip_permissions=config.skip_permissions,
    )
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.review_timeout_minutes * 60,
            cwd=config.project_root,
        )
        output = result.stdout + "\n" + result.stderr
        output_upper = output.upper()
        if "REVIEW_FAILED" in output_upper:
            return role, ReviewVerdict.FAILED, output
        elif "REVIEW_FIXED" in output_upper:
            return role, ReviewVerdict.FIXED, output
        return role, ReviewVerdict.PASSED, output
    except subprocess.TimeoutExpired:
        return role, ReviewVerdict.FAILED, f"Review timeout ({role})"
    except Exception as e:
        return role, ReviewVerdict.FAILED, str(e)


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
    review_template = config.review_command_template or config.command_template

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

    # Aggregate verdicts
    all_outputs: list[str] = []
    overall_verdict = ReviewVerdict.PASSED
    has_fixed = False
    for role, verdict, output in results:
        log_progress(f"  📋 {role}: {verdict.value}", task.id)
        all_outputs.append(f"=== {role.upper()} REVIEW ===\n{output[:2000]}")
        if verdict == ReviewVerdict.FAILED:
            overall_verdict = ReviewVerdict.FAILED
        elif verdict == ReviewVerdict.FIXED:
            has_fixed = True

    if overall_verdict != ReviewVerdict.FAILED and has_fixed:
        overall_verdict = ReviewVerdict.FIXED
        # Commit fixes from any review agent
        subprocess.run(["git", "add", "-A"], capture_output=True, cwd=config.project_root)
        subprocess.run(
            ["git", "commit", "-m", f"{task.id}: parallel review fixes"],
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )

    combined_output = "\n\n".join(all_outputs)
    log_progress(f"🔍 Parallel review result: {overall_verdict.value}", task.id)

    error = "Review found issues" if overall_verdict == ReviewVerdict.FAILED else None
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
