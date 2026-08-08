"""Hooks module for spec-runner.

Contains pre/post execution hooks that orchestrate git operations,
code review, testing, linting, and plugin execution around task runs.
"""

import subprocess

from .config import ExecutorConfig
from .git_ops import (
    build_scoped_test_command,
    ensure_runtime_gitignore,
    find_changed_source_files,
    get_main_branch,
    get_task_branch_name,
    map_source_to_test_files,
    stage_all_except_runtime,
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
from .stages import StageReporter
from .state import ReviewVerdict
from .task import Task, mark_all_checklist_done, update_task_status

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


def pre_start_hook(
    task: Task, config: ExecutorConfig, *, reporter: StageReporter | None = None
) -> bool:
    """Hook before starting task"""
    logger.info("Pre-start hook", task_id=task.id)

    # Sync dependencies (skippable — doctor and other lightweight runs disable
    # this). A custom `commands.sync` always runs; the built-in `uv sync`
    # default only runs when pyproject.toml exists — a hardcoded `uv sync`
    # was per-run stderr noise on every non-Python project (#70).
    if config.sync_deps:
        if config.sync_command:
            if reporter:
                reporter.enter("sync_deps")
            logger.info("Syncing dependencies", command=config.sync_command)
            result = subprocess.run(
                config.sync_command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if result.returncode == 0:
                logger.info("Dependencies synced")
            else:
                logger.warning("Dependency sync warning", stderr=result.stderr[:200])
        elif (config.project_root / "pyproject.toml").exists():
            if reporter:
                reporter.enter("sync_deps")
            logger.info("Syncing dependencies")
            result = subprocess.run(
                ["uv", "sync"], capture_output=True, text=True, cwd=config.project_root
            )
            if result.returncode == 0:
                logger.info("Dependencies synced")
            else:
                logger.warning("uv sync warning", stderr=result.stderr[:200])
        else:
            logger.debug("No pyproject.toml and no sync command — skipping dependency sync")

    # Keep executor runtime state out of git before any commit can happen (#62)
    if config.auto_commit or config.create_git_branch:
        ensure_runtime_gitignore(config)

    # Create git branch
    if config.create_git_branch:
        if reporter:
            reporter.enter("branch")
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


def commit_task_work(task: Task, config: ExecutorConfig) -> str:
    """Stage and commit the working tree under the task's label.

    Stages everything except executor runtime state (#62) and commits with
    a "TASK-XXX: <name>" message. Returns "committed", "empty" (nothing to
    commit), or "failed" — callers must not treat a failed commit as an
    empty one (#97 no-op detection keys on "empty" only). A staging
    failure (git add error) counts as "failed", not "empty".
    """
    try:
        if not stage_all_except_runtime(config):
            return "empty"
    except RuntimeError as exc:
        logger.warning("Staging failed", error=str(exc))
        return "failed"
    commit_title = f"{task.id}: {task.name}"
    done_items = [item for item, checked in task.checklist if checked]
    sections = []
    if done_items:
        sections.append("Completed:\n" + "\n".join(f"  - {item}" for item in done_items))
    if task.milestone:
        sections.append(f"Milestone: {task.milestone}")

    commit_msg = commit_title
    if sections:
        commit_msg += "\n\n" + "\n\n".join(sections)

    commit_result = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=config.project_root,
        capture_output=True,
        text=True,
    )
    if commit_result.returncode == 0:
        logger.info("Committed changes")
        return "committed"
    logger.warning("Commit failed", stderr=commit_result.stderr.strip()[:200])
    return "failed"


def post_done_hook(
    task: Task,
    config: ExecutorConfig,
    success: bool,
    changed_since: float | None = None,
    *,
    reporter: StageReporter | None = None,
) -> tuple[bool, str | None, str, str, bool]:
    """Hook after task completion.

    Returns:
        Tuple of (success, error_details, review_status, review_findings, no_op).
        error_details contains test/lint output on failure.
        review_status is the ReviewVerdict value string (e.g. "passed", "skipped").
        review_findings is the truncated review output (up to 2048 chars).
        no_op is True when auto-commit found nothing to commit — the task
        completed without changing anything committable (#97).
    """
    logger.info("Post-done hook", task_id=task.id, success=success)

    if not success:
        return False, None, ReviewVerdict.SKIPPED.value, "", False

    # Run tests — capture output for review context
    test_output_str: str | None = None
    if config.run_tests_on_done:
        if reporter:
            reporter.enter("tests")
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
                False,
            )
        logger.info("Tests passed")

    # Run lint — capture output for review context
    lint_output_str: str | None = None
    if config.run_lint_on_done and config.lint_command:
        if reporter:
            reporter.enter("lint")
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
                        False,
                    )
                else:
                    logger.warning("Lint warnings (non-blocking)")
            else:
                lint_output_str = "auto-fixed"
                logger.info("Lint auto-fixed")
        else:
            lint_output_str = "clean"
            logger.info("Lint passed")

    # Commit the exec-stage work under the task label BEFORE review runs
    # (#103): the review stage commits its own fixes, and with nothing
    # committed yet that commit swept the ENTIRE feature under a
    # "code review fixes" label while the final task commit got only the
    # tasks.md leftovers — history inverted relative to content. An early
    # commit also protects the work from the next task's pre-start cleanup.
    committed_pre_review = False
    if config.auto_commit and config.run_review:
        if reporter:
            reporter.enter("commit")
        committed_pre_review = commit_task_work(task, config) == "committed"

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
        if reporter:
            reporter.enter("review")
        # #66: gates passed, review starts — record the honest intermediate
        # status. A run killed during review leaves 🔍 REVIEW in tasks.md
        # (not a premature DONE); the task stays resumable like in_progress.
        if config.tasks_file.exists() and not update_task_status(
            config.tasks_file, task.id, "review"
        ):
            logger.warning(
                "Could not record REVIEW status in tasks.md",
                task_id=task.id,
                file=str(config.tasks_file),
            )
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
                False,
            )
        elif choice == "fix":
            logger.info("HITL requested fix-and-retry", task_id=task.id)
            return (
                False,
                f"Fix requested. Review findings:\n{(review_output or '')[:1024]}",
                ReviewVerdict.REJECTED.value,
                (review_output or "")[:2048],
                False,
            )
        elif choice == "skip":
            review_verdict = ReviewVerdict.SKIPPED
            logger.info("HITL skipped review", task_id=task.id)
        # "approve" falls through to normal commit flow

    # REVIEW_FIXED mutates the code AFTER the tests/lint gates ran (#65):
    # re-run both gates so a broken review fix cannot be committed and
    # merged as a "successful" run. Full suite (not scoped) — a fix may
    # touch anything; strict lint check without auto-fix — another mutation
    # here would reopen the same hole.
    if review_verdict == ReviewVerdict.FIXED:
        if config.run_tests_on_done:
            if reporter:
                reporter.enter("tests")
            logger.info("Re-running tests after review fixes", command=config.test_command)
            result = subprocess.run(
                config.test_command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if result.returncode != 0:
                logger.error("Tests failed after review fixes")
                return (
                    False,
                    f"Tests failed after review fixes:\n{result.stdout + result.stderr}",
                    review_verdict.value,
                    (review_output or "")[:2048],
                    False,
                )
            logger.info("Tests passed after review fixes")
        if config.run_lint_on_done and config.lint_command:
            if reporter:
                reporter.enter("lint")
            result = subprocess.run(
                config.lint_command,
                shell=True,
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if result.returncode != 0:
                if config.lint_blocking:
                    logger.error("Lint errors after review fixes")
                    return (
                        False,
                        f"Lint errors after review fixes:\n{result.stdout + result.stderr}",
                        review_verdict.value,
                        (review_output or "")[:2048],
                        False,
                    )
                logger.warning("Lint warnings after review fixes (non-blocking)")

    # Persist the task's DONE status + checklist to tasks.md BEFORE committing,
    # so it is included in the commit/merge. Writing it after the commit (as the
    # old code did in execution.py) left the update in the working tree post-merge
    # where it was never committed and got clobbered by the next task's branch.
    if config.tasks_file.exists():
        if not update_task_status(config.tasks_file, task.id, "done"):
            logger.error(
                "Could not record DONE status in tasks.md",
                task_id=task.id,
                file=str(config.tasks_file),
            )
        mark_all_checklist_done(config.tasks_file, task.id)

    # Auto-commit. no_op flips True when the task completed without any
    # committable changes (#97): work already absorbed by earlier tasks. The
    # marker is persisted so downstream displays (Maestro workstream
    # progress) can tell 5/5-with-one-noop from 4/5-with-one-skipped. The
    # pre-review commit counts as work (#103) — a final commit stage that
    # finds only bookkeeping must not flag a task that produced code.
    no_op = False
    if config.auto_commit:
        if reporter:
            reporter.enter("commit")
        try:
            if commit_task_work(task, config) == "empty" and not committed_pre_review:
                logger.info("No changes to commit — marking task as no-op")
                no_op = True
        except Exception as e:
            logger.error("Commit failed", error=str(e))

    # Merge branch to main
    if config.create_git_branch:
        if reporter:
            reporter.enter("merge")
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
                    no_op,
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
                        no_op,
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
                    False,
                )

    return True, None, review_verdict.value, (review_output or "")[:2048], no_op
