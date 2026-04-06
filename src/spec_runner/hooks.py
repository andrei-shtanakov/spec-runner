"""Hooks module for spec-runner.

Contains git operations, pre/post execution hooks, and code review
functions used before and after task execution.
"""

import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .config import ExecutorConfig
from .logging import get_logger
from .prompt import load_prompt_template, render_template
from .runner import build_cli_command, check_error_patterns, log_progress
from .state import ReviewVerdict
from .task import Task

logger = get_logger("hooks")

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


def get_task_branch_name(task: Task) -> str:
    """Generate branch name for task"""
    safe_name = task.name.lower().replace(" ", "-").replace("/", "-")[:30]
    return f"task/{task.id.lower()}-{safe_name}"


def get_main_branch(config: ExecutorConfig) -> str:
    """Determine main branch name (main or master).

    Detection order:
    1. Config setting (main_branch)
    2. Remote HEAD (origin/HEAD)
    3. Existing main or master branch
    4. Current branch (if no main/master exists yet)
    5. Default to "main"
    """
    # 0. Use config if explicitly set
    if config.main_branch:
        return config.main_branch

    # 1. Try remote HEAD
    result = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )
    if result.returncode == 0:
        # refs/remotes/origin/main -> main
        return result.stdout.strip().split("/")[-1]

    # 2. Check if main or master branch exists
    for branch in ["main", "master"]:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", branch],
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )
        if result.returncode == 0:
            return branch

    # 3. If no main/master, use current branch as "main"
    # (handles fresh repos where first branch might be named differently)
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()

    return "main"  # default for brand new repos


def ensure_on_main_branch(config: ExecutorConfig) -> None:
    """Ensure we're on main branch after all tasks complete."""
    try:
        main_branch = get_main_branch(config)

        # Check current branch
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )
        current_branch = result.stdout.strip()

        if current_branch != main_branch:
            logger.info("Switching to main branch", branch=main_branch)
            result = subprocess.run(
                ["git", "checkout", main_branch],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if result.returncode == 0:
                logger.info("On main branch", branch=main_branch)
            else:
                logger.warning(
                    "Could not switch to main branch",
                    branch=main_branch,
                    stderr=result.stderr.strip(),
                )
    except Exception:
        logger.debug("git_switch_failed", exc_info=True)


def pre_start_hook(task: Task, config: ExecutorConfig) -> bool:
    """Hook before starting task"""
    logger.info("Pre-start hook", task_id=task.id)

    # Sync dependencies
    logger.info("Syncing dependencies")
    result = subprocess.run(["uv", "sync"], capture_output=True, text=True, cwd=config.project_root)
    if result.returncode == 0:
        logger.info("Dependencies synced")
    else:
        logger.warning("uv sync warning", stderr=result.stderr[:200])

    # Create git branch
    if config.create_git_branch:
        branch_name = get_task_branch_name(task)
        try:
            # Check if git exists
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if result.returncode != 0:
                return True  # No git repository

            # Check if repo has any commits
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if result.returncode != 0:
                # Fresh repo without commits — skip branching for now
                # TASK-000 typically does git init, first commit will be on main
                logger.warning("No commits yet, skipping branch creation")
                return True

            # Switch to main
            main_branch = get_main_branch(config)
            subprocess.run(
                ["git", "checkout", main_branch],
                capture_output=True,
                cwd=config.project_root,
            )

            # Clean up leftover files from previous task
            subprocess.run(
                ["git", "checkout", "--", "."],
                capture_output=True,
                cwd=config.project_root,
            )
            # Remove untracked files that could contaminate tests
            subprocess.run(
                ["git", "clean", "-fd", "--exclude=spec/"],
                capture_output=True,
                cwd=config.project_root,
            )

            # Check if branch exists
            result = subprocess.run(
                ["git", "rev-parse", "--verify", branch_name],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )

            if result.returncode == 0:
                # Branch exists — switch to it
                subprocess.run(
                    ["git", "checkout", branch_name],
                    capture_output=True,
                    cwd=config.project_root,
                )
                logger.info("Switched to existing branch", branch=branch_name)
            else:
                # Create new branch
                result = subprocess.run(
                    ["git", "checkout", "-b", branch_name],
                    capture_output=True,
                    text=True,
                    cwd=config.project_root,
                )
                if result.returncode == 0:
                    logger.info("Created branch", branch=branch_name)
                else:
                    logger.warning("Failed to create branch", stderr=result.stderr)

        except FileNotFoundError:
            pass  # git not installed

    # Run plugin pre_start hooks
    from .plugins import build_task_env, discover_plugins, run_plugin_hooks

    plugins = discover_plugins(config.plugins_dir)
    if plugins:
        task_env = build_task_env(task, config, success=None)
        results = run_plugin_hooks("pre_start", plugins, task_env=task_env)
        for name, ok, blocking in results:
            if not ok and blocking:
                logger.error("Blocking plugin failed in pre_start", plugin=name)
                return False

    return True


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


def find_changed_source_files(project_root: Path, changed_since: float) -> list[Path]:
    """Find .py files in src/ with mtime > changed_since."""
    src_dir = project_root / "src"
    if not src_dir.exists():
        return []
    changed: list[Path] = []
    for p in src_dir.rglob("*.py"):
        if p.stat().st_mtime > changed_since:
            changed.append(p)
    return changed


def map_source_to_test_files(source_files: list[Path], project_root: Path) -> list[Path]:
    """Map src/pkg/module/file.py -> tests/test_file.py by convention."""
    tests_dir = project_root / "tests"
    if not tests_dir.exists():
        return []
    mapped: list[Path] = []
    for src in source_files:
        test_name = f"test_{src.name}"
        # Search tests/ for matching test file
        for candidate in tests_dir.rglob(test_name):
            if candidate not in mapped:
                mapped.append(candidate)
    return mapped


def build_scoped_test_command(
    base_command: str,
    test_files: list[Path],
    project_root: Path,
) -> str:
    """Replace generic test path with specific file paths."""
    if not test_files:
        return base_command
    rel_paths = " ".join(str(f.relative_to(project_root)) for f in test_files)
    # Replace common patterns: "tests/" or "tests" at end of command
    for pattern in ["tests/ ", "tests/", "tests "]:
        if pattern in base_command:
            return base_command.replace(pattern, rel_paths + " ", 1)
    # Append test files if no pattern matched
    return f"{base_command} {rel_paths}"


def post_done_hook(
    task: Task,
    config: ExecutorConfig,
    success: bool,
    changed_since: float | None = None,
) -> tuple[bool, str | None, str, str]:
    """Hook after task completion.

    Returns:
        Tuple of (success, error_details, review_status, review_findings).
        error_details contains test/lint output on failure.
        review_status is the ReviewVerdict value string (e.g. "passed", "skipped").
        review_findings is the truncated review output (up to 2048 chars).
    """
    logger.info("Post-done hook", task_id=task.id, success=success)

    if not success:
        return False, None, ReviewVerdict.SKIPPED.value, ""

    # Run tests — capture output for review context
    test_output_str: str | None = None
    if config.run_tests_on_done:
        test_cmd = config.test_command

        # Scope tests to changed files when running in parallel mode
        if changed_since is not None:
            changed_files = find_changed_source_files(config.project_root, changed_since)
            if changed_files:
                test_files = map_source_to_test_files(changed_files, config.project_root)
                if test_files:
                    test_cmd = build_scoped_test_command(
                        config.test_command,
                        test_files,
                        config.project_root,
                    )
                    logger.info(
                        "Running scoped tests",
                        test_files=[str(f) for f in test_files],
                    )
                else:
                    logger.info("No matching test files, running full suite")
            else:
                logger.info("No changed source files, running full suite")

        logger.info("Running tests", command=test_cmd)
        result = subprocess.run(
            test_cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )
        test_output_str = (result.stdout + result.stderr)[:2048]
        if result.returncode != 0:
            logger.error("Tests failed")
            logger.error("Test stderr", stderr=result.stderr[:500])
            return (
                False,
                f"Tests failed:\n{result.stdout + result.stderr}",
                ReviewVerdict.SKIPPED.value,
                "",
            )
        logger.info("Tests passed")

    # Run lint — capture output for review context
    lint_output_str: str | None = None
    if config.run_lint_on_done and config.lint_command:
        logger.info("Running lint")
        result = subprocess.run(
            config.lint_command,
            shell=True,
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )

        if result.returncode != 0:
            # Step 1: Attempt auto-fix
            logger.info("Attempting lint auto-fix")
            subprocess.run(
                config.lint_fix_command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )

            # Step 2: Re-check lint
            recheck = subprocess.run(
                config.lint_command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )

            if recheck.returncode != 0:
                # Step 3: Still failing — block or warn
                if config.lint_blocking:
                    lint_output = recheck.stdout + "\n" + recheck.stderr
                    logger.error("Lint errors remain after auto-fix")
                    return (
                        False,
                        f"Lint errors (not auto-fixable):\n{lint_output}",
                        ReviewVerdict.SKIPPED.value,
                        "",
                    )
                else:
                    logger.warning("Lint warnings (non-blocking)")
            else:
                lint_output_str = "auto-fixed"
                logger.info("Lint auto-fixed")
        else:
            lint_output_str = "clean"
            logger.info("Lint passed")

    # Get previous error for review context (local import to avoid circular dependency)
    from .state import ExecutorState

    previous_error: str | None = None
    state = ExecutorState(config)
    ts = state.tasks.get(task.id)
    if ts and ts.attempts:
        last = ts.attempts[-1]
        if not last.success and last.error:
            previous_error = last.error[:1024]
    state.close()

    # Run code review (before commit, so fixes can be included)
    review_verdict = ReviewVerdict.SKIPPED
    review_output: str | None = None
    if config.hitl_review and not config.run_review:
        logger.warning("hitl_review enabled but run_review is False; HITL gate skipped")
    if config.run_review:
        review_fn = run_parallel_review if config.review_parallel else run_code_review
        logger.info(
            "Running code review",
            parallel=config.review_parallel,
            roles=config.review_roles if config.review_parallel else None,
        )
        review_verdict, review_error, review_output = review_fn(
            task,
            config,
            test_output=test_output_str,
            lint_output=lint_output_str,
            previous_error=previous_error,
        )
        if review_verdict == ReviewVerdict.FAILED:
            logger.warning("Review found issues", error=review_error)
            # Non-HITL mode: review failures are advisory only (warn but don't block).
            # HITL mode handles this below via the interactive prompt.

    # HITL approval gate
    if config.hitl_review and review_output:
        print(format_review_findings(task.id, task.name, review_output))
        choice = prompt_hitl_verdict()
        if choice == "reject":
            logger.info("HITL rejected task", task_id=task.id)
            return (
                False,
                "Review rejected by human",
                ReviewVerdict.REJECTED.value,
                (review_output or "")[:2048],
            )
        elif choice == "fix":
            logger.info("HITL requested fix-and-retry", task_id=task.id)
            return (
                False,
                f"Fix requested. Review findings:\n{(review_output or '')[:1024]}",
                ReviewVerdict.REJECTED.value,
                (review_output or "")[:2048],
            )
        elif choice == "skip":
            review_verdict = ReviewVerdict.SKIPPED
            logger.info("HITL skipped review", task_id=task.id)
        # "approve" falls through to normal commit flow

    # Auto-commit
    if config.auto_commit:
        try:
            # Check if there are changes to commit
            status_result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if not status_result.stdout.strip():
                logger.info("No changes to commit")
            else:
                subprocess.run(["git", "add", "-A"], cwd=config.project_root)
                # Build commit message with task details
                commit_title = f"{task.id}: {task.name}"
                commit_body_lines = []
                if task.checklist:
                    commit_body_lines.append("Completed:")
                    for item, checked in task.checklist:
                        if checked:
                            commit_body_lines.append(f"  - {item}")
                if task.milestone:
                    commit_body_lines.append(f"\nMilestone: {task.milestone}")

                commit_msg = commit_title
                if commit_body_lines:
                    commit_msg += "\n\n" + "\n".join(commit_body_lines)

                subprocess.run(["git", "commit", "-m", commit_msg], cwd=config.project_root)
                logger.info("Committed changes")
        except Exception as e:
            logger.error("Commit failed", error=str(e))

    # Merge branch to main
    if config.create_git_branch:
        try:
            branch_name = get_task_branch_name(task)
            main_branch = get_main_branch(config)

            # Check current branch — if we're already on main, skip merge
            # (happens for TASK-000 or fresh repos)
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            current_branch = result.stdout.strip()
            if current_branch == main_branch:
                # Already on main, no merge needed
                return (
                    True,
                    None,
                    review_verdict.value,
                    (review_output or "")[:2048],
                )

            # Switch to main
            result = subprocess.run(
                ["git", "checkout", main_branch],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if result.returncode != 0:
                # Try with -f flag if there are uncommitted changes
                error_msg = result.stderr.strip()
                if "uncommitted" in error_msg.lower() or "changes" in error_msg.lower():
                    # Stash changes first
                    subprocess.run(
                        ["git", "stash"],
                        capture_output=True,
                        cwd=config.project_root,
                    )
                    result = subprocess.run(
                        ["git", "checkout", main_branch],
                        capture_output=True,
                        text=True,
                        cwd=config.project_root,
                    )

                if result.returncode != 0:
                    logger.warning(
                        "Failed to switch to main branch",
                        branch=main_branch,
                        stderr=error_msg,
                    )
                    return (
                        True,
                        None,
                        review_verdict.value,
                        (review_output or "")[:2048],
                    )

            # Merge task branch
            result = subprocess.run(
                ["git", "merge", branch_name, "--no-ff", "-m", f"Merge {branch_name}"],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if result.returncode == 0:
                logger.info("Merged branch", source=branch_name, target=main_branch)

                # Delete task branch
                subprocess.run(
                    ["git", "branch", "-d", branch_name],
                    capture_output=True,
                    cwd=config.project_root,
                )
                logger.info("Deleted branch", branch=branch_name)
            else:
                logger.warning("Merge failed", stderr=result.stderr)
                # Return to task branch on failure
                subprocess.run(
                    ["git", "checkout", branch_name],
                    capture_output=True,
                    cwd=config.project_root,
                )
        except Exception as e:
            logger.error("Merge failed", error=str(e))

    # Run plugin post_done hooks
    from .plugins import build_task_env, discover_plugins, run_plugin_hooks

    plugins = discover_plugins(config.plugins_dir)
    if plugins:
        task_env = build_task_env(task, config, success=success)
        results = run_plugin_hooks("post_done", plugins, task_env=task_env)
        for name, ok, blocking in results:
            if not ok and blocking:
                logger.error("Blocking plugin failed in post_done", plugin=name)
                return (
                    False,
                    f"Blocking plugin '{name}' failed",
                    review_verdict.value,
                    (review_output or "")[:2048],
                )

    return True, None, review_verdict.value, (review_output or "")[:2048]
