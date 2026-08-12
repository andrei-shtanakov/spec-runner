"""Hooks module for spec-runner.

Contains pre/post execution hooks that orchestrate git operations,
code review, testing, linting, and plugin execution around task runs.
"""

import subprocess

from .config import ExecutorConfig
from .gates import GateContext, GateStatus, evaluate_pre_terminal, has_gates
from .git_ops import (
    build_scoped_test_command,
    ensure_runtime_gitignore,
    find_changed_source_files,
    get_main_branch,
    get_task_branch_name,
    map_source_to_test_files,
    stage_all_except_runtime,
)
from .lifecycle import TddPhase
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
from .state import PhaseOutcome, ReviewVerdict
from .task import Task, mark_all_checklist_done, update_task_status

logger = get_logger("hooks")

#: Marks a pre-terminal refusal that is an instrument failure rather than a
#: verdict on the work. Read by `execution` to classify the attempt.
GATE_INSTRUMENT_ERROR_PREFIX = "Pre-terminal gate infrastructure error"

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
            if reporter:
                reporter.record(
                    PhaseOutcome.PASS if result.returncode == 0 else PhaseOutcome.UNEXPECTED_FAIL,
                    f"exit {result.returncode}",
                )
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
            if reporter:
                reporter.record(
                    PhaseOutcome.PASS if result.returncode == 0 else PhaseOutcome.UNEXPECTED_FAIL,
                    f"uv sync, exit {result.returncode}",
                )
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


def _head_sha(config: ExecutorConfig) -> str:
    """Current HEAD, or "" when there is no commit yet."""
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _record_tdd_phase(config: ExecutorConfig, task: Task, phase, detail=None) -> None:
    """Record a lifecycle transition when the task runs under `tdd`.

    Opens its own short-lived state handle: `post_done_hook` does not hold one,
    and a phase record must not become a reason to restructure the hook.
    Bookkeeping only — the gates decide, this remembers.
    """
    if config.resolve_execution_mode(task) != "tdd":
        return
    from .lifecycle import IllegalTransition, advance
    from .state import ExecutorState
    from .tdd import resolve_namespace

    try:
        with ExecutorState(config) as state:
            advance(state, resolve_namespace(config), task.id, phase, detail)
    except IllegalTransition as exc:
        logger.warning("Lifecycle transition refused", task_id=task.id, error=str(exc))
    except Exception as exc:  # never fail a task over bookkeeping
        logger.warning("Could not record lifecycle phase", task_id=task.id, error=str(exc))


def _detect_candidate_drift(config: ExecutorConfig, gated_sha: str, task_id: str) -> str | None:
    """Has anything but us moved the tree since the gate approved ``gated_sha``?

    Returns a refusal, or None when HEAD is the gated commit or a descendant
    this run created. The check is a compare-and-swap in spirit: the merge is
    authorised for a specific tree, and a tree that has since acquired
    someone else's commit is a different one.
    """
    head = _head_sha(config)
    if not head or head == gated_sha:
        return None

    # Ancestry first, explicitly. `git log A..B` exits 0 even when A is not an
    # ancestor of B — it just lists what B has and A does not — so a diverged
    # or rewritten branch would slip through as "no foreign commits" (measured,
    # not assumed: verified in a scratch repo).
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", gated_sha, head],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )
    if ancestry.returncode != 0:
        return (
            f"the tree no longer descends from the gated commit {gated_sha[:12]} "
            "(rewritten or moved elsewhere); refusing to merge"
        )

    # Commits between the gate and now. Ours is the bookkeeping commit and
    # carries the task label; anything else did not come from this run.
    subjects = subprocess.run(
        ["git", "log", "--format=%H %s", f"{gated_sha}..{head}"],
        capture_output=True,
        text=True,
        cwd=config.project_root,
    )
    if subjects.returncode != 0:
        return f"cannot inspect commits after {gated_sha[:12]}; refusing to merge"
    foreign = [
        line
        for line in subjects.stdout.strip().splitlines()
        if line and not _is_our_bookkeeping_commit(line, task_id)
    ]
    if foreign:
        return (
            f"the tree changed after the gate approved {gated_sha[:12]}: "
            f"{len(foreign)} commit(s) this run did not make "
            f"({foreign[0].split(' ', 1)[-1][:60]}); refusing to merge"
        )
    return None


def _is_our_bookkeeping_commit(log_line: str, task_id: str) -> bool:
    """Ours are labelled with the task id — `commit_task_work` writes
    ``"<TASK-ID>: <name>"``. Anything else between the gate and the merge came
    from somewhere this run does not control."""
    subject = log_line.split(" ", 1)[-1]
    return subject.startswith(f"{task_id}:")


def _run_pre_terminal_gates(
    task: Task,
    config: ExecutorConfig,
    candidate_sha: str | None = None,
    facts: dict[str, object] | None = None,
) -> str | None:
    """Evaluate registered gates against HEAD. Returns a reason, or None to pass.

    An unsatisfied gate does not get its own terminal state: it reuses the
    existing "this attempt did not finish" path, which keeps the task
    resumable and leaves the checkpoint commit in place. An exhausted
    instrument error is reported as infrastructure — the work was never
    judged, which is a different sentence from "the work is wrong".
    """
    from .state import ExecutorState

    merge_candidate = candidate_sha or _head_sha(config)
    if not merge_candidate:
        # No commit to judge. Refusing here would block fresh-repo bootstrap
        # over bookkeeping, so the gates simply have nothing to run against.
        logger.warning("No checkpoint commit — pre-terminal gates skipped", task_id=task.id)
        return None

    with ExecutorState(config) as state:
        outcome = evaluate_pre_terminal(
            GateContext(
                task_id=task.id,
                checkpoint_sha=merge_candidate,
                config=config,
                state=state,
                facts=dict(facts or {}),
            )
        )
    if outcome.status is GateStatus.SATISFIED:
        return None

    detail = "; ".join(
        f"{r.gate_id}: {r.detail}" for r in outcome.results if r.status is not GateStatus.SATISFIED
    )
    if outcome.status is GateStatus.INSTRUMENT_ERROR:
        logger.error("Pre-terminal gate could not answer", task_id=task.id, detail=detail)
        # The prefix is a contract, not prose: `execution` reads it to record
        # INFRASTRUCTURE rather than HOOK_FAILURE, which is what makes the run
        # exit 2 instead of 1.
        return f"{GATE_INSTRUMENT_ERROR_PREFIX}: {detail}"
    logger.warning("Pre-terminal gate unsatisfied — not merging", task_id=task.id, detail=detail)
    return f"Pre-terminal gate unsatisfied: {detail}"


def _commit_blocked_status(
    task: Task, config: ExecutorConfig, blocked: str, candidate_sha: str
) -> str:
    """Commit the blocked task's `REVIEW` flip, and say so if that failed (#192).

    Without this, a blocked task leaves `tasks.md` dirty with a status flip the
    *harness* wrote, and the next run refuses at the dirty-spec guard until the
    operator commits a change they did not make — a recovery deadlock in which
    both halves are individually correct.

    The commit carries only the status line and is a child of the candidate, so
    the SHA the gate judged is unchanged: a later evaluation happens against a
    different tree and is a fresh evaluation, never this verdict reapplied to
    code that has since moved.

    Only under `auto_commit`. Without it the run does not commit the task's work
    either, so the tree is dirty for reasons this cannot fix, and committing on
    behalf of an operator who switched auto-commit off would be a surprise.
    """
    if not config.auto_commit:
        return blocked
    from .bookkeeping import commit_status_flip

    try:
        problem = commit_status_flip(config, task.id, reason=blocked, candidate_sha=candidate_sha)
    except Exception as exc:  # pragma: no cover - defensive
        problem = f"the status flip could not be committed: {exc}"
    if not problem:
        return blocked
    # Visible, and part of the failure the caller records: a stop that left the
    # deadlock in place must not read as a clean resumable stop.
    logger.warning("Blocked task left a dirty spec", task_id=task.id, detail=problem)
    return f"{blocked} — {problem}"


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

        # Scope tests to changed files when running in parallel mode.
        # `scope_reason` is not decoration: when the gate narrows, it runs a
        # different set than the config declares, and the run's evidence has
        # to say so (#139) — a full-suite contract is not proven by a subset.
        scope_reason = "not parallel mode"
        if changed_since is not None:
            if not getattr(config, "scoped_tests", True):
                scope_reason = "disabled by scoped_tests: false"
            else:
                changed_files = find_changed_source_files(config.project_root, changed_since)
                if not changed_files:
                    scope_reason = "no changed source files"
                else:
                    test_files = map_source_to_test_files(changed_files, config.project_root)
                    if not test_files:
                        scope_reason = "no matching test files"
                    else:
                        test_cmd = build_scoped_test_command(
                            config.test_command,
                            test_files,
                            config.project_root,
                        )
                        if test_cmd == config.test_command:
                            # build_scoped_test_command refused — composite
                            # command it cannot narrow without guessing.
                            scope_reason = "composite test_command"
                        else:
                            scope_reason = f"{len(test_files)} changed test file(s)"

        scoped = test_cmd != config.test_command
        # One line, always emitted, carrying the mode. Previously only the
        # scoped branch logged anything, so "ran the full suite" and "narrowed
        # it" were indistinguishable in the record.
        logger.info(
            "Running tests",
            command=test_cmd,
            scope="scoped" if scoped else "full",
            scope_reason=scope_reason,
        )
        result = subprocess.run(
            test_cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=config.project_root,
        )
        test_output_str = (result.stdout + result.stderr)[:2048]
        if reporter:
            reporter.record(
                PhaseOutcome.PASS if result.returncode == 0 else PhaseOutcome.UNEXPECTED_FAIL,
                f"{'scoped' if scoped else 'full'} suite, exit {result.returncode}",
            )
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
                    if reporter:
                        reporter.record(PhaseOutcome.UNEXPECTED_FAIL, "errors remain after fix")
                    return (
                        False,
                        f"Lint errors (not auto-fixable):\n{lint_output}",
                        ReviewVerdict.SKIPPED.value,
                        "",
                        False,
                    )
                else:
                    logger.warning("Lint warnings (non-blocking)")
                    if reporter:
                        reporter.record(PhaseOutcome.UNEXPECTED_FAIL, "non-blocking warnings")
            else:
                lint_output_str = "auto-fixed"
                logger.info("Lint auto-fixed")
                if reporter:
                    reporter.record(PhaseOutcome.PASS, "auto-fixed")
        else:
            lint_output_str = "clean"
            logger.info("Lint passed")
            if reporter:
                reporter.record(PhaseOutcome.PASS, "lint clean")

    # Commit the exec-stage work under the task label BEFORE review runs
    # (#103): the review stage commits its own fixes, and with nothing
    # committed yet that commit swept the ENTIRE feature under a
    # "code review fixes" label while the final task commit got only the
    # tasks.md leftovers — history inverted relative to content. An early
    # commit also protects the work from the next task's pre-start cleanup.
    # F-1: this commit is the **candidate** — the tree the policy gates judge and
    # the tree that will be merged. It is made whenever auto-commit is on, not
    # only when review is on. Gating it on review was the whole defect: with
    # review off nothing committed the work before the gate, so the gate judged
    # a tree without it and a task could rewrite its own claimed test and reach
    # DONE. The lock held exactly when an unrelated feature happened to be
    # enabled.
    committed_pre_review = False
    review_checkpoint_sha = ""
    #
    # Made when something will actually judge the tree — review, or a
    # registered gate. With neither, nothing is going to look at it, so the
    # single task commit of #103 stays exactly as it was: a project that opts
    # into nothing must not find its history split in two.
    wants_candidate = config.auto_commit and (config.run_review or has_gates())
    if wants_candidate:
        if reporter:
            reporter.enter("commit")
        committed_pre_review = commit_task_work(task, config) == "committed"
        # #157 §2.1: the tree review is about to judge. Recorded only when a
        # gate will actually use it — the dormant path stays free of git calls.
        if has_gates() and config.run_review:
            review_checkpoint_sha = _head_sha(config)

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
        # #138: the four outcomes are four different facts and are recorded as
        # such — "found issues", "never produced a verdict" and "the reviewer
        # itself broke" used to be one warning line or, worse, none at all.
        # Blocking behaviour is deliberately unchanged here: outside HITL all
        # of these stay advisory, and whether a review may fail a task is a
        # policy decision tracked separately.
        if reporter:
            from .phases import review_verdict_to_phase

            outcome, detail = review_verdict_to_phase(review_verdict)
            reporter.record(outcome, detail or review_error)
        if review_verdict == ReviewVerdict.FAILED:
            logger.warning("Review found issues", error=review_error)
        elif review_verdict == ReviewVerdict.NOT_RUN:
            logger.warning(
                "Review produced no verdict — this task was not reviewed",
                reason=review_error,
            )
        elif review_verdict == ReviewVerdict.ERROR:
            logger.error(
                "Review could not run — nothing was learned about this code",
                error=review_error,
            )

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

    # Pre-terminal policy gates (#164), evaluated BEFORE anything writes DONE.
    #
    # The checkpoint commit and the review fixes have already happened — that is
    # the point: a gate is evaluated *against* a stable SHA. What it withholds is
    # progress past that checkpoint, and "progress" starts with marking the task
    # done. Running this after the DONE write left a blocked task labelled `done`
    # in tasks.md, which is exactly the 2.23.0 class of defect (#164 criterion 1:
    # an artifact that exists read as work that finished) inside the mechanism
    # built to prevent it. It also made the merge candidate a tree that already
    # claimed the task was done — circular, since that is what is being decided.
    #
    # Dormant until a consumer registers: `has_gates` is checked before any SHA
    # is resolved or the state DB is opened, so a project that enables nothing
    # cannot tell this code is here (criterion 8).
    # The candidate SHA: HEAD after the work (and any review fixes) is
    # committed. A no-op task has one too — HEAD as it stands — so the gate is
    # never asked about nothing in particular.
    # Resolved only when something will use it — a gate to judge, or a merge to
    # protect. An unconditional `rev-parse` would be a git call on the path of
    # a project that enabled neither.
    # #141 4a: the deterministic checks and the candidate commit are what
    # "verifying the green" means; the phase is recorded here, once, after they
    # have all run and before anything decides on them.
    _record_tdd_phase(config, task, TddPhase.GREEN_VERIFYING)

    gated_sha = _head_sha(config) if (has_gates() or config.create_git_branch) else ""
    if has_gates():
        blocked = _run_pre_terminal_gates(
            task,
            config,
            candidate_sha=gated_sha,
            facts={
                "review_verdict": review_verdict.value,
                "review_checkpoint_sha": review_checkpoint_sha,
                # The RED gate is evaluated here too: "do not merge a task that
                # never had a confirmed red" is the same question it answers
                # before the implementation pass.
                "execution_mode": config.resolve_execution_mode(task),
            },
        )
        if blocked is not None:
            # tasks.md still says `review` (or `in_progress`), the candidate
            # commit stands, and nothing was merged — the task stays resumable,
            # and the work is committed rather than left dirty.
            blocked = _commit_blocked_status(task, config, blocked, gated_sha)
            return (False, blocked, review_verdict.value, (review_output or "")[:2048], False)

    # Materialised, never executed (3a). The phase exists so the machine is
    # complete and a later decision has somewhere to land; running an agent
    # here was not approved, and the record says `skipped` rather than leaving
    # a reader to wonder whether something ran.
    _record_tdd_phase(config, task, TddPhase.REFACTORING)

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
            final = commit_task_work(task, config)
        except Exception as e:
            logger.error("Commit failed", error=str(e))
            final = "failed"
        if wants_candidate:
            # The candidate carried the work, so this commit only ever carries
            # bookkeeping — "was it empty?" no longer answers the question. The
            # task produced nothing exactly when the candidate found nothing
            # and review committed no fixes of its own.
            no_op = not committed_pre_review and review_verdict != ReviewVerdict.FIXED
        else:
            # Single-commit shape (#97/#103): the one commit is the work, so an
            # empty one means there was none.
            no_op = final == "empty"
        if no_op:
            logger.info("No changes to commit — marking task as no-op")

    # The verdict was about a tree. If the tree moved under us between the gate
    # and the merge — another process, a hook, a person — the verdict no longer
    # describes what would be merged, so it must not authorise the merge.
    # Everything we do ourselves in between (the bookkeeping commit) is known
    # and allowed; anything else is not.
    if gated_sha:
        drift = _detect_candidate_drift(config, gated_sha, task.id)
        if drift is not None:
            logger.error("Refusing to merge", task_id=task.id, reason=drift)
            return (False, drift, review_verdict.value, (review_output or "")[:2048], no_op)

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
