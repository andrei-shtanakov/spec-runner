"""Hooks module for spec-runner.

Contains pre/post execution hooks that orchestrate git operations,
code review, testing, linting, and plugin execution around task runs.
"""

import subprocess

from .config import ExecutorConfig
from .git_ops import (
    build_scoped_test_command,
    find_changed_source_files,
    get_main_branch,
    get_task_branch_name,
    map_source_to_test_files,
)
from .logging import get_logger
from .review import (
    REVIEW_ROLES,
    build_review_prompt,
    format_review_findings,
    prompt_hitl_verdict,
    run_code_review,
    run_parallel_review,
)
from .state import ReviewVerdict
from .task import Task

logger = get_logger("hooks")

# Re-export for backward compatibility
__all__ = [
    "REVIEW_ROLES",
    "build_review_prompt",
    "build_scoped_test_command",
    "find_changed_source_files",
    "format_review_findings",
    "get_main_branch",
    "get_task_branch_name",
    "map_source_to_test_files",
    "post_done_hook",
    "pre_start_hook",
    "prompt_hitl_verdict",
    "run_code_review",
    "run_parallel_review",
]


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
