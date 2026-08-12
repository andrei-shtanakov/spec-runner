"""CLI commands and argument parsing for spec-runner."""

import argparse
import json
import signal
import sys
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from uuid import uuid4

# Re-exports from submodules for backward compatibility
from .cli_info import (  # noqa: E402, F401
    cmd_audit,
    cmd_costs,
    cmd_logs,
    cmd_mcp,
    cmd_report,
    cmd_reset,
    cmd_status,
    cmd_stop,
    cmd_tui,
    cmd_validate,
    cmd_verify,
)
from .cli_plan import cmd_plan  # noqa: E402, F401
from .config import (
    ExecutorConfig,
    ExecutorLock,
    _resolve_config_path,
    build_config,
    load_config_from_yaml,
)
from .execution import (
    execute_task,
    run_with_retries,
)
from .git_ops import (
    create_integration_branch,
    ensure_on_main_branch,
    finalize_integration_branch,
    make_integration_branch_name,
    spec_dirty_paths,
)
from .logging import get_logger
from .preflight import cmd_preflight
from .preset_cmd import cmd_config
from .runner import (
    log_progress,
)
from .spec import SpecMetaError, read_spec_meta
from .state import (
    ErrorCode,
    ExecutorState,
    check_stop_requested,
    clear_stop_file,
    recover_stale_tasks,
)
from .sync_cmd import cmd_sync
from .task import (
    Task,
    diff_task_statuses,
    format_task_status_diff,
    get_next_tasks,
    get_task_by_id,
    mark_all_checklist_done,
    parse_tasks,
    resolve_dependencies,
    snapshot_task_statuses,
    update_task_status,
)
from .validate import format_results, validate_all

logger = get_logger("cli")


# === CLI Commands ===


def build_task_json_result(task_id: str, state: ExecutorState) -> dict:
    """Build a single task's `--json-result` entry.

    Stable contract: see docs/state-schema.md and schemas/json-result.schema.json.
    Golden-fixed by tests/test_json_result_contract.py. Changes here follow the
    breaking-change policy in docs/state-schema.md: removing/renaming/retyping a
    key requires a major version bump; adding an optional key is non-breaking.
    """
    ts = state.get_task_state(task_id)
    entry: dict = {"task_id": task_id, "status": "unknown", "attempts": 0}
    if not ts:
        return entry
    entry["status"] = "done" if ts.status == "success" else "failed"
    entry["attempts"] = ts.attempt_count
    entry["cost_usd"] = round(state.task_cost(task_id), 2)
    inp_t = sum(a.input_tokens or 0 for a in ts.attempts)
    out_t = sum(a.output_tokens or 0 for a in ts.attempts)
    entry["tokens"] = {"input": inp_t, "output": out_t}
    total_dur = sum(a.duration_seconds for a in ts.attempts)
    entry["duration_seconds"] = round(total_dur, 1)
    if ts.attempts:
        last = ts.attempts[-1]
        entry["review"] = last.review_status or "skipped"
        if last.error:
            entry["error"] = last.error[:200]
        # v2.16.0 (#97): only emitted when true, so pre-existing consumers
        # and golden fixtures are unaffected.
        if last.no_op and ts.status == "success":
            entry["no_op"] = True
    entry["exit_code"] = 0 if ts.status == "success" else 1
    return entry


def _print_dry_run(tasks_to_run: list[Task], config: ExecutorConfig, state: ExecutorState) -> None:
    """Print what tasks would execute without running them."""
    data = []
    for t in tasks_to_run:
        # Checklist tuples are (item, checked) — unpacking them as
        # (done, _) counted truthy item TEXTS, reporting done == total
        # for untouched tasks (#71). checklist_progress owns the order.
        checklist_done, checklist_total = t.checklist_progress
        entry = {
            "task_id": t.id,
            "name": t.name,
            "priority": t.priority,
            "status": t.status,
            "depends_on": t.depends_on,
            "checklist_total": checklist_total,
            "checklist_done": checklist_done,
        }
        ts = state.get_task_state(t.id)
        if ts:
            entry["previous_attempts"] = ts.attempt_count
            entry["previous_cost_usd"] = round(state.task_cost(t.id), 2)
        data.append(entry)

    print(json.dumps({"dry_run": True, "tasks": data}, indent=2))


def _acquire_run_lock(config: ExecutorConfig) -> ExecutorLock:
    """Acquire the exclusive executor lock, or exit(1) if another run holds it."""
    lock = ExecutorLock(config.state_file.with_suffix(".lock"))
    if not lock.acquire():
        held_by = getattr(lock, "_held_by", {})
        alive = held_by.get("alive", "true")
        logger.error(
            "Another executor is already running",
            lock_file=str(config.state_file.with_suffix(".lock")),
            held_by_pid=held_by.get("pid", "unknown"),
            started=held_by.get("started", "unknown"),
            process_alive=alive,
        )
        if alive == "false":
            logger.error(
                "Lock holder is dead. Use --force to override, or delete the lock file manually."
            )
        sys.exit(1)
    return lock


def cmd_run(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Execute tasks."""
    # HITL review incompatible with TUI mode
    if config.hitl_review and getattr(args, "tui", False):
        logger.warning("--hitl-review ignored in TUI mode (TUI owns the screen)")
        config.hitl_review = False

    # Acquire the exclusive lock unless --force. TUI mode also holds it (one
    # executor per project) — when held, stale-task recovery can safely reset all
    # orphaned 'running' tasks; with --force a concurrent runner may exist, so we
    # fall back to the age-based heuristic.
    if getattr(args, "force", False):
        logger.warning("Skipping lock check (--force)")
        lock = None
    else:
        lock = _acquire_run_lock(config)
    lock_held = lock is not None

    try:
        if getattr(args, "tui", False):
            import threading

            from .logging import setup_logging
            from .tui import SpecRunnerApp

            # TUI mode: log to file, TUI owns screen
            log_file = config.logs_dir / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
            config.logs_dir.mkdir(parents=True, exist_ok=True)
            setup_logging(level=config.log_level, tui_mode=True, log_file=log_file)

            app = SpecRunnerApp(config=config)

            # #129: the run lives in a daemon thread, and `sys.exit(1)` there
            # raises SystemExit *in that thread* — the interpreter discards it
            # and the process still exits 0. Every fail-closed gate
            # (state_spec_mismatch, governance refusal, on_task_failure=stop)
            # was therefore advisory under --tui. Carry the code across the
            # thread boundary and re-raise it once the TUI has released the
            # screen.
            thread_exit: list[int] = []

            def _start_execution() -> None:
                def _target() -> None:
                    try:
                        _run_tasks(args, config, lock_held=lock_held)
                    except SystemExit as exc:
                        code = exc.code
                        thread_exit.append(code if isinstance(code, int) else 1)

                t = threading.Thread(target=_target, daemon=True)
                t.start()

            app.call_later(_start_execution)
            app.run()
            if thread_exit and thread_exit[0]:
                sys.exit(thread_exit[0])
        else:
            _run_tasks(args, config, lock_held=lock_held)
    finally:
        if lock is not None:
            lock.release()


def spec_run_gate_ok(config: ExecutorConfig) -> tuple[bool, str]:
    """Return (allowed, reason). Blocks unapproved managed tasks.md in strict mode.

    Governance is off by default: unless ``config.spec_governance == "strict"``,
    or the tasks.md is unmanaged (no frontmatter), the gate always allows the run.
    """
    if getattr(config, "spec_governance", "off") != "strict":
        return True, ""
    meta = read_spec_meta(config.tasks_file, config.resolve_spec_profile().names())
    if meta is None:
        return True, ""  # unmanaged: backward-compatible
    if meta.status == "approved":
        return True, ""
    return False, (
        f"tasks.md is {meta.status} (v{meta.version}); "
        f"approve with `spec-runner spec approve tasks` or run with --no-strict"
    )


def _enforce_spec_governance(config: ExecutorConfig) -> None:
    """Refuse the run when the governance gate blocks it — fail-closed (#134).

    A policy rejection used to print to stdout and return, i.e. exit 0: for a
    CI caller it was indistinguishable from "there was nothing to execute"
    (found on steward's live V1 run of the gated cycle). The refusal now exits
    non-zero, and its diagnostics go to stderr — stdout is reserved for
    machine payloads like ``--json-result``.
    """
    allowed, reason = spec_run_gate_ok(config)
    if allowed:
        return
    logger.error("Refusing to run: spec governance gate", reason=reason)
    print(f"⛔ spec governance: {reason}", file=sys.stderr)
    sys.exit(1)


def _maybe_start_integration(args, config: ExecutorConfig):
    """Fork a per-run integration branch when ``integration_pr`` is enabled.

    Returns an ``IntegrationRun`` (and redirects task merges onto it via
    ``config.main_branch``) or None when the mode is off/unavailable — in
    which case the run behaves exactly as before (self-merge into main).
    """
    if not getattr(config, "integration_pr", False):
        return None
    if not config.create_git_branch:
        logger.warning("integration_pr ignored: create_git_branch is off")
        return None
    if getattr(args, "dry_run", False):
        return None
    run = create_integration_branch(config, make_integration_branch_name())
    if run is not None:
        # Redirect every task's merge target to the integration branch; the
        # existing merge stage reads config.main_branch, so main is untouched.
        config.main_branch = run.branch
    return run


def run_exit_code(*, failed: int, infrastructure: int, prior: int) -> int:
    """The run's exit code, decided once for every way tasks were selected.

    F-2, from the battle test on v2.25.0: `run --task=X` exited 0 after the
    task failed every attempt, while `run --all` on the same repository exited
    1. The difference was not the selector — it was that `--all` happens to
    reach an idle-stop verdict afterwards while the fixed-list path had no
    final judgement at all. So the exit code was reporting *whether the loop
    chose to stop early*, not whether the work succeeded.

    - ``prior`` is an exit code the loop already decided (a stop reason). It is
      never downgraded.
    - ``failed`` outranks ``infrastructure``: something concrete is wrong and
      someone can act on it, which is the more useful thing to report.
    - ``infrastructure`` is 2, because "the instrument broke, so I cannot tell
      you whether the work is good" is a different sentence from "the work is
      bad" — the same distinction `GateStatus` draws.
    """
    if prior:
        return prior
    if failed:
        return 1
    if infrastructure:
        return 2
    return 0


def _run_tasks(args, config: ExecutorConfig, *, lock_held: bool = False):
    """Run tasks, optionally collecting them on one integration branch + PR.

    Thin wrapper around :func:`_run_tasks_inner`: sets up the integration
    branch first (when enabled) and always finalizes it (push + open PR, or
    clean up) afterwards, regardless of how the inner run exits.
    """
    integration = _maybe_start_integration(args, config)
    try:
        _run_tasks_inner(args, config, lock_held=lock_held)
    finally:
        if integration is not None:
            pr_url = finalize_integration_branch(config, integration)
            if pr_url:
                _announce_integration_pr(config, pr_url)
                _post_pr_review_stage(config, pr_url, integration)


def _post_pr_review_stage(config: ExecutorConfig, pr_url: str, integration) -> None:
    """Optional post-PR stage (#102 M3): invoke the review-pr loop.

    Opt-in via ``review_pr.post_pr`` — ``off`` (default, integration_pr
    behavior byte-identical), ``verify`` (read-only triage: verdicts land in
    state and `status`), or ``full`` (check out the integration branch, run
    the whole fix+reply loop, return to the base branch). Waits
    ``post_pr_wait_seconds`` first so the review bot has a chance to
    comment. The stage's outcome never changes the run's exit status — the
    run already succeeded; the review loop reports through its own report,
    the persisted state, and `status`.
    """
    import subprocess
    import time as _time
    from types import SimpleNamespace

    mode = config.review_pr_post_pr
    if mode == "off":
        return
    if mode not in ("verify", "full"):
        logger.warning(
            "Unknown review_pr.post_pr value — stage skipped",
            value=mode,
            allowed=["off", "verify", "full"],
        )
        return

    from .review_pr import EXIT_NEEDS_HUMAN, cmd_review_pr

    wait = config.review_pr_post_pr_wait_seconds
    if wait > 0:
        print(
            f"\n🔍 post-PR review stage ({mode}): waiting {wait}s for the review bot…",
            file=sys.stderr,
        )
        _time.sleep(wait)

    stage_args = SimpleNamespace(
        pr_ref=pr_url,
        json_output=False,
        no_verify=False,
        verify_only=(mode == "verify"),
    )
    checked_out = False
    try:
        if mode == "full":
            # The fix path needs local HEAD == PR head; the run left the
            # working copy on the base branch, so check the run branch out
            # and always return, whatever the loop does.
            result = subprocess.run(
                ["git", "checkout", integration.branch],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if result.returncode != 0:
                logger.error(
                    "post-PR stage: cannot check out the integration branch — skipped",
                    branch=integration.branch,
                    stderr=result.stderr.strip()[:200],
                )
                return
            checked_out = True
        code = cmd_review_pr(stage_args, config)
        if code == EXIT_NEEDS_HUMAN:
            print(
                "🔍 post-PR review: comments await a human (see `spec-runner status`)",
                file=sys.stderr,
            )
        logger.info("post-PR review stage finished", mode=mode, exit_code=code)
    except Exception as exc:  # the stage must never break a finished run
        logger.error("post-PR review stage failed", error=str(exc), exc_info=True)
    finally:
        if checked_out:

            def _stash_loop_leftovers() -> None:
                # A loop crash mid-fix can leave uncommitted changes: either
                # they block the checkout, or (identical blobs on both
                # branches) the checkout silently carries them over. Both
                # ways they are the crashed loop's dirt, not the operator's
                # — stash them loudly rather than strand or pollute. Only
                # the loop's leftovers are stashed: executor runtime state
                # (the live state DB) is excluded via _dirty_paths.
                from .review_pr import _dirty_paths

                paths = [line[3:].strip().strip('"') for line in _dirty_paths(config)]
                if not paths:
                    return
                stash = subprocess.run(
                    [
                        "git",
                        "stash",
                        "push",
                        "--include-untracked",
                        "-m",
                        "spec-runner post-PR review stage leftovers",
                        "--",
                        *paths,
                    ],
                    capture_output=True,
                    text=True,
                    cwd=config.project_root,
                )
                if stash.returncode == 0 and "No local changes" not in stash.stdout:
                    print(
                        "⚠️  post-PR review: the loop left uncommitted changes — "
                        "stashed as 'spec-runner post-PR review stage leftovers' "
                        "(inspect with `git stash list`)",
                        file=sys.stderr,
                    )

            back = subprocess.run(
                ["git", "checkout", integration.base],
                capture_output=True,
                text=True,
                cwd=config.project_root,
            )
            if back.returncode != 0:
                _stash_loop_leftovers()
                back = subprocess.run(
                    ["git", "checkout", integration.base],
                    capture_output=True,
                    text=True,
                    cwd=config.project_root,
                )
            else:
                _stash_loop_leftovers()
            if back.returncode != 0:
                print(
                    f"❌ post-PR review: could not return to '{integration.base}' "
                    f"(working copy left on '{integration.branch}'):\n"
                    f"   {back.stderr.strip()[:200]}",
                    file=sys.stderr,
                )


def _announce_integration_pr(config: ExecutorConfig, pr_url: str) -> None:
    """Make the merge requirement explicit and durable (#73).

    The "Opened integration PR" info-log line scrolls away; nothing told
    the operator that the next run can only start from a merged base. The
    announcement goes to stderr (stdout may carry --json-result), and the
    URL is persisted so `status` keeps repeating it until `spec-runner
    sync` clears it after the merge. #101: the human-merge gate must not
    depend on someone watching the terminal — the event is also pushed
    through the configured notification channels (Telegram/webhook).
    """
    print(
        f"\n🔗 Integration PR opened: {pr_url}\n"
        "   Merge it before the next run (the base branch has neither the\n"
        "   spec update nor the code until then), then run `spec-runner sync`.",
        file=sys.stderr,
    )
    try:
        from .sync_cmd import PR_URL_META_KEY

        with ExecutorState(config) as state:
            state.set_meta(PR_URL_META_KEY, pr_url)
    except Exception as exc:  # pragma: no cover — announcement must not fail the run
        logger.warning("Could not persist PR URL", error=str(exc))

    from .notifications import notify_pr_opened

    notify_pr_opened(config, pr_url)


# Stop reasons a run can persist as `last_run_stop_reason`. `status` renders
# them and external callers (Maestro's audit table) key off them, so the
# vocabulary is part of the interop surface: add freely, rename with a
# CHANGELOG note. Error-classified reasons are dynamic (`error_<kind>`) and
# therefore not enumerable here.
RUN_STOP_REASONS: tuple[str, ...] = (
    "completed",
    "task_failed_stop",
    "dependency_blocked_after_skip",
    "state_spec_mismatch",
    "max_consecutive_failures",
    "budget_exceeded",
    "validation_failed",
)


def _warn_orphaned_successes(state: ExecutorState, all_tasks: list) -> set[str]:
    """Report state-DB successes whose task id is gone from tasks.md.

    A success ID absent from tasks.md entirely (the task was removed from the
    spec, not merely left non-done) has nothing to reconcile against, so it is
    surfaced but not fatal. Returns the ids present in tasks.md that the DB
    calls successful, for the caller's `missing` comparison.
    """
    present_ids = {t.id for t in all_tasks}
    success_ids = {task_id for task_id, ts in state.tasks.items() if ts.status == "success"}
    orphaned = success_ids - present_ids
    if orphaned:
        logger.warning(
            "success in state-DB but task no longer present in tasks.md",
            task_ids=sorted(orphaned),
        )
    return success_ids & present_ids


def _idle_stop_verdict(state: ExecutorState, all_tasks: list) -> tuple[str, str, int]:
    """Classify "nothing (more) to run" as a clean finish or stuck work.

    Returns ``(stop_reason, stop_detail, exit_code)``. Work is stuck when a
    task gave up — blocked in tasks.md, or terminally failed in the state DB —
    because nothing in the run can revive it and its dependents wait forever.
    That is not the same as "everything is done", and reporting it as
    completed/0 is how a production workstream closed DONE at 1/11 and got
    merged (#136). Anything else — including todo tasks merely filtered out by
    a milestone — stays a clean exit 0.
    """
    present_ids = {t.id for t in all_tasks}
    stuck = sorted(
        {t.id for t in all_tasks if t.status == "blocked"}
        | {
            task_id
            for task_id, ts in state.tasks.items()
            if ts.status == "failed" and task_id in present_ids
        }
    )
    if stuck:
        return (
            "dependency_blocked_after_skip",
            f"blocked/skipped tasks remain: {stuck}",
            1,
        )
    return "completed", "", 0


def _stop_reason_for(state: ExecutorState, config: ExecutorConfig) -> tuple[str, str]:
    """Resolve the (stop_reason, stop_detail) pair for a mid-run stop.

    Budget stops are reported as such (#67 — they used to masquerade as
    "max_consecutive_failures"); otherwise a classified error kind from the
    most recent failed attempt wins over the generic counter.
    """
    cause = state.stop_cause()
    if cause is not None and cause[0] == "budget_exceeded":
        return cause
    last = state.most_recent_failed_attempt()
    if last and last.error_kind and last.error_kind != "unknown":
        return f"error_{last.error_kind}", last.error or ""
    if cause is not None:
        return cause
    return (
        "max_consecutive_failures",
        f"{state.consecutive_failures}/{config.max_consecutive_failures}",
    )


def _exit_on_state_spec_mismatch(
    state: ExecutorState,
    *,
    config: ExecutorConfig,
    detail: str,
    completed: int,
    failed: int,
    remaining: int,
) -> None:
    """Record `state_spec_mismatch` as this run's stop reason and exit non-zero.

    Shared tail for both reconciliation gates (#124): a state-DB "success"
    that tasks.md never reflects as "done" is a hard integrity failure, not
    a normal stop — the run must not report the same exit-0 "completed" a
    caller like Maestro reads as a finished workstream. Funnels through the
    same `last_run_stop_reason`/audit-log plumbing every other refusal
    already uses, rather than a parallel mechanism.
    """
    from .audit_log import EVENT_RUN_ENDED

    logger.error("Stopping run: state/spec mismatch", detail=detail)
    state.set_meta("last_run_stop_reason", "state_spec_mismatch")
    state.set_meta("last_run_stop_detail", detail)
    state.audit_logger.record(
        EVENT_RUN_ENDED,
        completed=completed,
        failed=failed,
        remaining=remaining,
        stop_reason="state_spec_mismatch",
    )
    # #130: this exit used to happen before the run's notify tail, so the
    # owners of the Telegram/webhook channel heard about every ordinary finish
    # and nothing about the heaviest stop there is. Notify on the same terms as
    # a normal run end — best-effort: a dead webhook must not swallow the
    # integrity failure we are exiting on.
    from .notifications import notify_run_complete

    try:
        total_cost_val = state.total_cost()
        notify_run_complete(
            config,
            completed=completed,
            failed=failed,
            total_cost=total_cost_val if total_cost_val > 0 else None,
            stop_reason="state_spec_mismatch",
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("run_complete notification failed", error=str(exc))
    sys.exit(1)


def _enforce_clean_spec(args, config: ExecutorConfig) -> None:
    """Refuse to execute when spec/config files are uncommitted or dirty (#69).

    Fail-closed: the spec is the run's contract, and executing an uncommitted
    spec muddies provenance (the auto-commit mixes spec and code into one
    commit; an interrupted run leaves DONE edits in an uncommitted file).
    Only enforced when spec-runner's own git automation is on — without
    auto-commit/branching there is no provenance to protect, and projects
    with git automation auto-disabled (subdir repos) keep a permanently
    dirty tasks.md by design. Override with --allow-dirty-spec. Gitignored
    spec files (Maestro's generated specs) never count as dirt.
    """
    if getattr(args, "allow_dirty_spec", False):
        return
    automation_on = getattr(config, "auto_commit", False) or getattr(
        config, "create_git_branch", False
    )
    if not automation_on:
        return
    dirty = spec_dirty_paths(config)
    if not dirty:
        return

    # A run killed between writing a status and committing it leaves exactly
    # one kind of dirt behind: a harness-authored status flip. Committing that
    # on its own is the recovery — the operator must not have to commit a
    # change they did not make, and `--allow-dirty-spec` would disarm the guard
    # for real spec edits at the same time (#192). Anything the proof cannot
    # confirm as status-only stays dirt and is refused below.
    from .bookkeeping import recover_interrupted_flip

    recovered = recover_interrupted_flip(config)
    if recovered is not None:
        print(
            f"↻ Recovered an interrupted run: committed {recovered.task_id} "
            f"{recovered.previous} → {recovered.new} as bookkeeping"
        )
        dirty = spec_dirty_paths(config)
        if not dirty:
            return

    logger.error("Refusing to run: spec/config files are not committed", files=dirty)
    print("⛔ Refusing to run: spec/config files have uncommitted changes:")
    for line in dirty:
        print(f"   {line}")
    print("   Commit them first (the spec is the run's contract), or override")
    print("   with --allow-dirty-spec.")
    sys.exit(1)


def _run_tasks_inner(args, config: ExecutorConfig, *, lock_held: bool = False):
    """Internal task execution logic.

    lock_held: True when the caller holds the exclusive executor lock, so any
    orphaned 'running' task can be safely reset regardless of age.
    """
    _enforce_spec_governance(config)

    _enforce_clean_spec(args, config)

    # Clear any leftover stop file from previous runs
    clear_stop_file(config)

    tasks = parse_tasks(config.tasks_file)

    with ExecutorState(config) as state:
        from .audit_log import EVENT_RUN_ENDED, EVENT_RUN_STARTED

        state.audit_logger.record(
            EVENT_RUN_STARTED,
            total_tasks=len(tasks),
            mode="all" if getattr(args, "all", False) else "single",
            task_filter=getattr(args, "task", None),
        )

        # Recover tasks stuck in 'running' from a previous crashed/interrupted run.
        # When we hold the exclusive lock (lock_held), no other runner exists — any
        # 'running' task is orphaned and is reset regardless of age (otherwise a
        # session interruption, e.g. a dropped remote shell, leaves a half-done
        # task that the next run re-picks first and hangs re-doing it). Without the
        # lock (--force), a concurrent runner may be active, so fall back to the
        # age-based heuristic (2x the task timeout).
        stale_timeout = config.task_timeout_minutes * 2
        recovered = recover_stale_tasks(
            state, stale_timeout, config.tasks_file, recover_all=lock_held
        )
        if recovered:
            logger.warning("Recovered stale tasks", task_ids=recovered)
            tasks = parse_tasks(config.tasks_file)

        # v2.3.0: reset failed-task state on `run --all` unless opted out.
        reset_enabled = getattr(args, "all", False) and not getattr(args, "no_reset_failed", False)
        previously_failed: set[str] = set()  # used by T17 second-pass detection
        if reset_enabled:
            previously_failed = state.reset_failed_to_pending()
            state.consecutive_failures = 0
            state.clear_second_pass_fails()
            state._save()
        stop_reason: str = "completed"  # used by T18 stop-reason capture
        stop_detail: str = ""  # used by T18 stop-reason capture
        # Process exit code for the run, decided by the loop and applied once
        # at the very end — after notify/audit/--json-result have run. A
        # mid-loop sys.exit() would skip exactly the reporting a caller needs
        # in order to understand the failure it is being told about.
        exit_code: int = 0

        # #104: total_completed/total_failed are cumulative ACROSS runs
        # (monotonic executor_meta counters). The end-of-run summary must
        # report THIS run's numbers, so snapshot the baseline here.
        completed_before = state.total_completed
        failed_before = state.total_failed
        failed_attempts_before = sum(
            1 for ts in state.tasks.values() for a in ts.attempts if not a.success
        )
        # Per-task attempt counts, so the run's verdict reads only its own
        # attempts — an earlier run's failure must not fail this one.
        attempts_before = {tid: len(ts.attempts) for tid, ts in state.tasks.items()}

        # Pre-run validation
        from .validate import format_results, validate_all

        pre_result = validate_all(
            tasks_file=config.tasks_file,
            config_file=_resolve_config_path(),
        )
        if not pre_result.ok:
            # H-1 (governed-run finding): a silent `return` here exited 0 and
            # orchestrators (Maestro) read that as workstream success — an
            # unparseable spec became a mergeable empty run. Fail loudly.
            logger.error("Validation failed before execution")
            print(format_results(pre_result))
            # Close the audit pair: EVENT_RUN_STARTED was already recorded,
            # and a dangling start would make the trail ambiguous.
            state.audit_logger.record(
                EVENT_RUN_ENDED,
                completed=0,
                failed=0,
                remaining=len(tasks),
                stop_reason="validation_failed",
            )
            sys.exit(1)

        # Check failure/budget limits — name the actual cause and exit
        # non-zero (#67: this used to say "consecutive failures ... 0" on a
        # budget stop and exit 0, stranding the operator without a diagnosis).
        cause = state.stop_cause()
        if cause is not None:
            reason, detail = cause
            logger.error("Refusing to run", reason=reason, detail=detail)
            print(f"⛔ Refusing to run: {reason} ({detail})")
            if reason == "budget_exceeded":
                print("   Raise budget_usd, or `spec-runner reset` to clear recorded costs.")
            # Persist the refusal as this run's stop reason so `status`
            # reports it instead of the previous run's outcome.
            state.set_meta("last_run_stop_reason", reason)
            state.set_meta("last_run_stop_detail", detail)
            state.audit_logger.record(
                EVENT_RUN_ENDED,
                completed=0,
                failed=0,
                remaining=len(tasks),
                stop_reason=reason,
            )
            sys.exit(1)

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
            # Deliberately NOT the `_idle_stop_verdict` treatment the loop's
            # "no more ready tasks" branch gets. Two reasons this path must
            # stay a quiet exit 0:
            #   - `--task`/`--milestone` narrow the selection, so an empty
            #     `tasks_to_run` routinely coexists with blocked work outside
            #     the filter — failing there would be a false alarm about
            #     tasks this invocation never claimed to run;
            #   - a blocked task whose dependencies are satisfied is promoted
            #     back to todo by `resolve_dependencies`, so it is *ready*,
            #     not stuck, and never reaches this branch anyway.
            # The stuck-work verdict therefore belongs to the loop, which
            # observes what actually happened this run.
            _warn_orphaned_successes(state, tasks)
            if getattr(args, "json_result", False):
                print(json.dumps({"tasks": [], "message": "No tasks ready to execute"}))
            state.set_meta("last_run_stop_reason", stop_reason)
            state.set_meta("last_run_stop_detail", stop_detail)
            # Close the audit pair: EVENT_RUN_STARTED was recorded above, and
            # returning here left a dangling start in the trail (Copilot,
            # PR #144). No notification — "nothing to do" is the one outcome
            # an owner does not need pinged about, and `watch` reaches this
            # branch on every idle poll.
            state.audit_logger.record(
                EVENT_RUN_ENDED,
                completed=0,
                failed=0,
                remaining=len([t for t in tasks if t.status != "done"]),
                stop_reason=stop_reason,
            )
            return

        # --dry-run: show what would execute and exit
        if getattr(args, "dry_run", False):
            _print_dry_run(tasks_to_run, config, state)
            return

        logger.info("Tasks to execute", count=len(tasks_to_run))
        for t in tasks_to_run:
            logger.info("Queued task", task_id=t.id, name=t.name)

        # Execute
        if args.all:
            # For --all mode, continuously re-evaluate ready tasks after each completion
            executed_ids: set[str] = set()
            include_in_progress = not getattr(args, "restart", False)
            session_start = time.monotonic()
            last_activity = time.monotonic()
            while True:
                # Check for pause request (SIGQUIT / Ctrl+\)
                from .executor import _pause_requested

                if _pause_requested:
                    import spec_runner.executor as _executor_mod

                    _executor_mod._pause_requested = False
                    pause_snapshot = snapshot_task_statuses(tasks)
                    log_progress(
                        "⏸️ Paused. Edit spec/tasks.md, then press Enter to resume (q to quit)."
                    )
                    choice = input("> ").strip().lower()
                    if choice == "q":
                        break
                    # Re-parse tasks to pick up edits AND external changes made
                    # while we were paused (another session, Maestro, manual
                    # edits). Diff against the pre-pause snapshot so the
                    # operator can see newly-completed parents and downstream
                    # tasks that just became ready — LABS-38.
                    tasks = parse_tasks(config.tasks_file)
                    diff = diff_task_statuses(pause_snapshot, tasks)
                    executed_ids.clear()
                    logger.info(
                        "Resumed after pause, tasks re-read",
                        changes=format_task_status_diff(diff),
                    )
                    if not diff.is_empty:
                        log_progress(f"▶️ {format_task_status_diff(diff)}")

                # Check for graceful shutdown request
                if check_stop_requested(config):
                    clear_stop_file(config)
                    logger.info("Graceful shutdown requested")
                    log_progress("🛑 Graceful shutdown requested")
                    break

                # Session timeout check
                if config.session_timeout_minutes > 0:
                    elapsed = (time.monotonic() - session_start) / 60
                    if elapsed >= config.session_timeout_minutes:
                        logger.warning(
                            "Session timeout reached",
                            elapsed_minutes=round(elapsed, 1),
                            limit_minutes=config.session_timeout_minutes,
                        )
                        break

                # Idle timeout check
                if config.idle_timeout_minutes > 0:
                    idle = (time.monotonic() - last_activity) / 60
                    if idle >= config.idle_timeout_minutes:
                        logger.warning(
                            "Idle timeout reached",
                            idle_minutes=round(idle, 1),
                            limit_minutes=config.idle_timeout_minutes,
                        )
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

                    # Backstop (#124, state_spec_mismatch): gate 1 above only
                    # checks the task it just ran — a task the loop never
                    # touched this run (e.g. a stale success row left over
                    # from an earlier run/crash) would slip past it. Before
                    # accepting "nothing more to do", confirm every state-DB
                    # success is reflected as done in tasks.md. A legitimate
                    # block (TODO waiting on a documented failure/skip) never
                    # recorded a success in the first place, so it leaves
                    # both sets in agreement and does not trip this check.
                    nonterminal_tasks = [t for t in all_tasks if t.status != "done"]
                    done_ids = {t.id for t in all_tasks if t.status == "done"}
                    # #132: the orphan warning used to sit inside `if
                    # nonterminal_tasks`, so the "everything done + an orphaned
                    # success row" case — the spec edited under a finished run
                    # — passed silently. It does not depend on unfinished work
                    # existing, so it is reported unconditionally.
                    present_success_ids = _warn_orphaned_successes(state, all_tasks)
                    if nonterminal_tasks:
                        missing = present_success_ids - done_ids
                        if missing:
                            _exit_on_state_spec_mismatch(
                                state,
                                config=config,
                                detail=(
                                    "success in state-DB but not done in "
                                    f"tasks.md: {sorted(missing)}"
                                ),
                                completed=state.total_completed - completed_before,
                                failed=state.total_failed - failed_before,
                                remaining=len(nonterminal_tasks),
                            )

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
                    elif nonterminal_tasks:
                        # Before this branch existed, this case fell into the
                        # "all done" branch below (todo_tasks was empty) and
                        # still got ensure_on_main_branch — keep that git side
                        # effect so only the diagnostics change.
                        ensure_on_main_branch(config)
                    else:
                        logger.info("All tasks completed")
                        # Ensure we're on main branch at the end
                        ensure_on_main_branch(config)

                    # Blocked-after-skip (#131/#136 item 2): work is left over
                    # and none of it can ever become ready — a task gave up
                    # (blocked/failed) and its dependents are waiting on a
                    # corpse. That is not a clean finish.
                    #
                    # The v2.22.0 version of this check lived in the `elif`
                    # above, so it only fired once *nothing* was todo: one
                    # blocked task plus ten waiting TODOs took the plain "No
                    # more ready tasks" path and reported completed/0. That is
                    # exactly how a production workstream closed DONE at 1/11
                    # and got merged. Exit code is now non-zero — the interop
                    # question #131 deferred, answered by the incident.
                    idle_reason, idle_detail, idle_code = _idle_stop_verdict(state, all_tasks)
                    if idle_code:
                        stop_reason, stop_detail, exit_code = idle_reason, idle_detail, idle_code
                        logger.warning(
                            "Stopping run: tasks blocked after skip",
                            detail=idle_detail,
                            waiting_tasks=sorted(t.id for t in todo_tasks) or None,
                        )
                    break

                task = ready_tasks[0]
                executed_ids.add(task.id)

                logger.info("Next ready task", task_id=task.id, name=task.name)

                result = run_with_retries(task, config, state)
                last_activity = time.monotonic()

                # Gate 1 (#124, state_spec_mismatch): the state DB just
                # recorded "success" for the task that ran — tasks.md must
                # agree it is "done" before the loop moves on. Reread from
                # disk rather than trusting the stale `tasks` list, so a
                # write that silently missed its mark (Task 1's
                # confirm-after-write can return False without writing) or
                # a mid-run edit by the agent itself is caught immediately,
                # not after the run reports a misleading "completed".
                if state.get_task_state(task.id).status == "success":
                    reread_tasks = parse_tasks(config.tasks_file)
                    reread = get_task_by_id(reread_tasks, task.id)
                    if reread is None or reread.status != "done":
                        _exit_on_state_spec_mismatch(
                            state,
                            config=config,
                            detail=(
                                f"{task.id}: state-DB=success but tasks.md="
                                f"{reread.status if reread else 'missing'}"
                            ),
                            completed=state.total_completed - completed_before,
                            failed=state.total_failed - failed_before,
                            # Same "remaining" definition as gate 2's backstop:
                            # tasks not yet done, not a raw file count.
                            remaining=len([t for t in reread_tasks if t.status != "done"]),
                        )

                # v2.3.0: detect tasks that fail again on a second pass.
                # Use the persisted task status (set to "failed" when retries
                # are exhausted) rather than `result is False`, because the
                # default on_task_failure="skip" mode returns "SKIP" for a
                # fully-failed task — so a result-based check would miss it.
                # Must run BEFORE the SKIP `continue` below, which short-circuits.
                if (
                    task.id in previously_failed
                    and state.get_task_state(task.id).status == "failed"
                ):
                    log_progress(
                        f"💡 [{task.id}] repeated failure — review logs at "
                        f"{config.logs_dir}/{task.id}-*.log"
                    )
                    state.add_second_pass_fail(task.id)

                # "SKIP" means continue to next task
                if result == "SKIP":
                    continue

                # #136: `on_task_failure: stop` must actually stop, and must
                # not report success. It marked the task blocked and returned
                # False, but leaving the loop hung on `should_stop()` —
                # "consecutive failures >= max_consecutive_failures (default 2)
                # OR budget exhausted". One failed task never tripped that, so
                # the run drifted on and finished as completed/0. This is the
                # setting release notes 2.22.0 recommend to orchestrators
                # precisely so a failure halts the workstream, so the gap made
                # the documented remedy a placebo.
                if result is False and config.on_task_failure == "stop":
                    stop_reason = "task_failed_stop"
                    stop_detail = f"{task.id} failed and on_task_failure=stop"
                    exit_code = 1
                    logger.warning(
                        "Stopping run: task failed under on_task_failure=stop",
                        task_id=task.id,
                    )
                    break

                if result is False and state.should_stop():
                    stop_reason, stop_detail = _stop_reason_for(state, config)
                    logger.warning("Stopping run", reason=stop_reason, detail=stop_detail)
                    exit_code = 1
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

                # v2.3.0: detect tasks that fail again on a second pass.
                # Use the persisted task status (set to "failed" when retries
                # are exhausted) rather than `result is False`, because the
                # default on_task_failure="skip" mode returns "SKIP" for a
                # fully-failed task — so a result-based check would miss it.
                # Must run BEFORE the SKIP `continue` below, which short-circuits.
                if (
                    task.id in previously_failed
                    and state.get_task_state(task.id).status == "failed"
                ):
                    log_progress(
                        f"💡 [{task.id}] repeated failure — review logs at "
                        f"{config.logs_dir}/{task.id}-*.log"
                    )
                    state.add_second_pass_fail(task.id)

                if result == "SKIP":
                    continue

                # #136: `on_task_failure: stop` must actually stop, and must
                # not report success. It marked the task blocked and returned
                # False, but leaving the loop hung on `should_stop()` —
                # "consecutive failures >= max_consecutive_failures (default 2)
                # OR budget exhausted". One failed task never tripped that, so
                # the run drifted on and finished as completed/0. This is the
                # setting release notes 2.22.0 recommend to orchestrators
                # precisely so a failure halts the workstream, so the gap made
                # the documented remedy a placebo.
                if result is False and config.on_task_failure == "stop":
                    stop_reason = "task_failed_stop"
                    stop_detail = f"{task.id} failed and on_task_failure=stop"
                    exit_code = 1
                    logger.warning(
                        "Stopping run: task failed under on_task_failure=stop",
                        task_id=task.id,
                    )
                    break

                if result is False and state.should_stop():
                    stop_reason, stop_detail = _stop_reason_for(state, config)
                    logger.warning("Stopping run", reason=stop_reason, detail=stop_detail)
                    exit_code = 1
                    break

        # One verdict for both selectors (F-2). Counts THIS run's outcomes:
        # a task left unfinished is not a success, however the loop ended.
        #
        # Scope is every task this run *touched or promised to touch* — not
        # `tasks_to_run`, which in `--all` mode is only the initially-ready
        # list, so a task that became ready and then failed mid-loop would have
        # gone unnoticed (Copilot, PR #183).
        touched = {
            tid for tid, ts in state.tasks.items() if len(ts.attempts) > attempts_before.get(tid, 0)
        }
        considered = touched | {t.id for t in tasks_to_run}
        run_failures = 0
        run_infrastructure = 0
        for task_id in sorted(considered):
            ts = state.tasks.get(task_id)
            if ts is not None and ts.status == "success":
                continue
            attempts = ts.attempts[attempts_before.get(task_id, 0) :] if ts else []
            if any(a.error_code == ErrorCode.INFRASTRUCTURE for a in attempts):
                run_infrastructure += 1
            else:
                # Includes a selected task with no attempts at all — a run
                # interrupted before it started did not do the work, and
                # "resumable" is not "success".
                run_failures += 1
        exit_code = run_exit_code(
            failed=run_failures, infrastructure=run_infrastructure, prior=exit_code
        )

        # v2.3.0: persist stop-reason for this run
        state.set_meta("last_run_stop_reason", stop_reason)
        state.set_meta("last_run_stop_detail", stop_detail)

        # Summary
        # Re-read tasks to get updated statuses after execution
        tasks = parse_tasks(config.tasks_file)

        # Calculate statistics (#104: this run's failed attempts, not history)
        failed_attempts = (
            sum(1 for ts in state.tasks.values() for a in ts.attempts if not a.success)
            - failed_attempts_before
        )
        remaining = len([t for t in tasks if t.status == "todo"])

        # #104: report this run's delta, not the cumulative meta counters —
        # a single-task run used to end with "completed=2" because earlier
        # runs' completions leaked into the summary.
        run_completed = state.total_completed - completed_before
        run_failed = state.total_failed - failed_before

        logger.info(
            "Execution summary",
            completed=run_completed,
            failed=run_failed,
            remaining=remaining,
            failed_attempts=failed_attempts if failed_attempts > 0 else None,
        )

        # Notify run completion
        from .notifications import notify_run_complete

        total_cost_val = state.total_cost()
        notify_run_complete(
            config,
            completed=run_completed,
            failed=run_failed,
            total_cost=total_cost_val if total_cost_val > 0 else None,
            stop_reason=stop_reason,
        )

        # stop_reason= matches the kwarg every other EVENT_RUN_ENDED call site
        # already carries (validation_failed/budget refusal/state_spec_mismatch
        # above) — this loop-exit tail was the one place missing it, so every
        # stop reason (not just blocked-after-skip) is now visible in the
        # audit trail, not just in the `last_run_stop_reason` meta key.
        state.audit_logger.record(
            EVENT_RUN_ENDED,
            completed=run_completed,
            failed=run_failed,
            remaining=remaining,
            total_cost_usd=round(total_cost_val, 4),
            stop_reason=stop_reason,
        )

        # --json-result: structured JSON result per task (for Maestro interop)
        if getattr(args, "json_result", False):
            results = [build_task_json_result(t.id, state) for t in tasks_to_run]
            print(json.dumps(results if len(results) > 1 else results[0], indent=2))

    # #136: apply the run's exit code last, outside the state context manager,
    # so the DB is closed cleanly and every consumer-facing artifact (summary,
    # notification, audit record, --json-result) has already been produced. A
    # caller that only reads the exit code still learns the run did not
    # finish; a caller that reads the payload gets the reason too.
    if exit_code:
        sys.exit(exit_code)


def cmd_retry(args, config: ExecutorConfig):
    """Retry failed task, preserving error context from previous attempts."""
    # Spec governance gate — must run before any task execution/lock so a
    # blocked retry has zero side effects (same bypass class as `watch`).
    _enforce_spec_governance(config)

    # Dirty-spec guard (#69) — retry executes tasks and runs the git
    # automation hooks, so it must not bypass the guard either.
    _enforce_clean_spec(args, config)

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


def cmd_watch(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Continuously watch tasks.md and execute ready tasks."""
    # Spec governance gate — must run before anything else (before the TUI
    # branch, before pre-run validation, before any lock/stop-file handling)
    # so a blocked watch has zero side effects. `run` gates via `_run_tasks`;
    # `watch` has its own loop and previously bypassed the gate entirely.
    _enforce_spec_governance(config)

    # Dirty-spec guard (#69) — same enforcement as `run`, checked once
    # before the loop starts (mid-run DONE writes dirty tasks.md by design).
    _enforce_clean_spec(args, config)

    # Pre-run validation
    pre_result = validate_all(
        tasks_file=config.tasks_file,
        config_file=_resolve_config_path(),
    )
    if not pre_result.ok:
        logger.error("Validation failed before watch")
        print(format_results(pre_result))
        return

    # TUI mode
    if getattr(args, "tui", False):
        import threading

        from .logging import setup_logging
        from .tui import SpecRunnerApp

        log_file = config.logs_dir / f"watch-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        setup_logging(level=config.log_level, tui_mode=True, log_file=log_file)

        app = SpecRunnerApp(config=config)

        def _start_watch() -> None:
            def watch_loop() -> None:
                consecutive_failures = 0
                while True:
                    if check_stop_requested(config):
                        break
                    if consecutive_failures >= config.max_consecutive_failures:
                        break
                    tasks = parse_tasks(config.tasks_file)
                    tasks = resolve_dependencies(tasks)
                    ready = get_next_tasks(tasks)
                    if not ready:
                        time.sleep(5)
                        continue
                    task = ready[0]
                    with ExecutorState(config) as state:
                        result = run_with_retries(task, config, state)
                    if result is True:
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                    time.sleep(1)

            t = threading.Thread(target=watch_loop, daemon=True)
            t.start()

        app.call_later(_start_watch)
        app.run()
        return

    print(f"Watching {config.tasks_file} for changes...")
    print(f"Polling every 5s | Stop: Ctrl+C or touch {config.stop_file}")

    consecutive_failures = 0

    while True:
        if check_stop_requested(config):
            logger.info("Stop requested, exiting watch mode")
            break

        if consecutive_failures >= config.max_consecutive_failures:
            logger.error(
                "Watch stopped: too many consecutive failures",
                consecutive_failures=consecutive_failures,
            )
            break

        tasks = parse_tasks(config.tasks_file)
        tasks = resolve_dependencies(tasks)
        ready = get_next_tasks(tasks)

        if not ready:
            time.sleep(5)
            continue

        task = ready[0]
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] Starting {task.id}: {task.name}")

        with ExecutorState(config) as state:
            result = run_with_retries(task, config, state)

        if result is True:
            consecutive_failures = 0
            cost = 0.0
            with ExecutorState(config) as state:
                cost = state.task_cost(task.id)
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] {task.id} completed (${cost:.2f})")
        else:
            consecutive_failures += 1
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(
                f"[{timestamp}] {task.id} failed "
                f"({consecutive_failures}/{config.max_consecutive_failures})"
            )

        time.sleep(1)


# Default probe budget for `doctor` when --budget is not given. Applied in
# cmd_doctor (not via parser defaults) so it cannot leak into other subcommands.
DOCTOR_DEFAULT_BUDGET_USD = 0.5


def cmd_doctor(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Run the CLI/model compatibility probe and exit with its status code."""
    from .doctor import run_doctor

    code = run_doctor(
        config,
        cli=args.cli,
        model=args.model,
        with_review=args.with_review,
        budget=args.budget if args.budget is not None else DOCTOR_DEFAULT_BUDGET_USD,
        timeout_min=getattr(args, "timeout", None),
        assume_yes=args.yes,
        strict=args.strict,
        as_json=args.json,
        keep=args.keep,
    )
    raise SystemExit(code)


# === Main ===


def _dispatch_task_command(args: argparse.Namespace) -> None:
    """Dispatch `spec-runner task <subcommand>` to task_commands functions."""
    from .github_sync import cmd_sync_from_gh, cmd_sync_to_gh, export_gh
    from .task import parse_tasks
    from .task_commands import (
        TASKS_FILE,
        cmd_block,
        cmd_check,
        cmd_done,
        cmd_graph,
        cmd_list,
        cmd_next,
        cmd_show,
        cmd_start,
        cmd_stats,
    )

    task_cmd = getattr(args, "task_command", None)
    if not task_cmd:
        print("Usage: spec-runner task <command>\n")
        print("Commands: list, show, start, done, block, check, stats, next, graph,")
        print("          export-gh, sync-to-gh, sync-from-gh")
        return

    prefix = getattr(args, "spec_prefix", "")
    change = getattr(args, "change", "")
    if change:
        from .config import ConfigError, _validate_change_id

        if prefix:
            raise SystemExit("⛔ --change and --spec-prefix are mutually exclusive")
        try:
            _validate_change_id(change)
        except ConfigError as exc:
            raise SystemExit(f"⛔ {exc}") from None
        tasks_file = Path(f"spec/changes/{change}/tasks.md")
    elif prefix:
        tasks_file = Path(f"spec/{prefix}tasks.md")
    else:
        tasks_file = TASKS_FILE
    tasks = parse_tasks(tasks_file)

    write_commands: dict[str, Callable[..., object]] = {
        "start": cmd_start,
        "done": cmd_done,
        "block": cmd_block,
        "check": cmd_check,
        "sync-from-gh": cmd_sync_from_gh,
    }
    read_commands = {
        "list": cmd_list,
        "ls": cmd_list,
        "show": cmd_show,
        "stats": cmd_stats,
        "next": cmd_next,
        "graph": cmd_graph,
        "export-gh": export_gh,
        "sync-to-gh": cmd_sync_to_gh,
    }

    if task_cmd in write_commands:
        write_commands[task_cmd](args, tasks, tasks_file)
    elif task_cmd in read_commands:
        read_commands[task_cmd](args, tasks)


# Defaults for every option in the shared `common` parent parser. The parser
# itself uses argparse.SUPPRESS (see _CommonDefaultsParser) so a value parsed
# before the subcommand survives the subparser pass — with plain defaults the
# subparser re-applied its own default AFTER the top-level parse and silently
# swallowed e.g. `spec-runner --spec-prefix=phase2- run` (the exact argv order
# spec-runner-vscode emits).
_COMMON_DEFAULTS: dict[str, object] = {
    "max_retries": None,
    "timeout": None,
    "no_tests": False,
    "no_branch": False,
    "no_commit": False,
    "no_review": False,
    "integration_pr": None,
    "hitl_review": False,
    "callback_url": "",
    "spec_prefix": "",
    "change": "",
    "project_root": "",
    "log_level": None,
    "log_json": False,
    "budget": None,
    "task_budget": None,
}


class _CommonDefaultsParser(argparse.ArgumentParser):
    """Top-level parser that fills common-option defaults after parsing.

    The `common` parent is built with ``argument_default=SUPPRESS``: an option
    the user did not pass sets no attribute at all, so a value parsed at one
    level (before the subcommand) is never clobbered by another level's
    default. The flip side is that unset options are missing from the
    namespace — this hook restores the documented defaults exactly once,
    after the full parse.
    """

    # The explicit signature documents intent, but typeshed's overloads for
    # parse_args (generic over a caller-supplied namespace type) cannot be
    # matched by a plain override — the ignore stays by necessity.
    def parse_args(  # type: ignore[override]
        self,
        args: Sequence[str] | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> argparse.Namespace:
        parsed = super().parse_args(args, namespace)
        for key, value in _COMMON_DEFAULTS.items():
            if not hasattr(parsed, key):
                setattr(parsed, key, value)
        return parsed


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level argument parser.

    Extracted from main() to allow programmatic use and testing.
    """
    # Shared options available to every subcommand. SUPPRESS defaults — see
    # _CommonDefaultsParser; real defaults live in _COMMON_DEFAULTS.
    common = argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    common.add_argument("--max-retries", type=int, help="Max retries per task (default: 3)")
    common.add_argument("--timeout", type=int, help="Task timeout in minutes (default: 30)")
    common.add_argument("--no-tests", action="store_true", help="Skip tests on task completion")
    common.add_argument("--no-branch", action="store_true", help="Skip git branch creation")
    common.add_argument("--no-commit", action="store_true", help="Skip auto-commit on success")
    common.add_argument("--no-review", action="store_true", help="Skip code review after task")
    common.add_argument(
        "--integration-pr",
        action="store_true",
        help="Collect all tasks on one branch and open a single PR (never merge into main)",
    )
    common.add_argument(
        "--hitl-review",
        action="store_true",
        help="Enable interactive approval gate after code review",
    )
    common.add_argument("--callback-url", type=str, help="URL to POST task status updates to")
    common.add_argument(
        "--spec-prefix",
        type=str,
        help='Spec file prefix (e.g. "phase5-" for phase5-tasks.md)',
    )
    common.add_argument(
        "--change",
        type=str,
        help="Operate inside spec/changes/<id>/ (change-as-folder; see `change` command)",
    )
    common.add_argument(
        "--project-root",
        type=str,
        help="Project root directory (default: current directory)",
    )
    common.add_argument(
        "--log-level",
        type=str,
        choices=["debug", "info", "warning", "error"],
        help="Log level (default: info)",
    )
    common.add_argument(
        "--log-json",
        action="store_true",
        help="Output logs as JSON lines",
    )
    common.add_argument(
        "--budget",
        type=float,
        help="Global budget in USD (stop when exceeded)",
    )
    common.add_argument(
        "--task-budget",
        type=float,
        help="Per-task budget in USD (block task when exceeded)",
    )
    # Drift guard: with SUPPRESS defaults, a common option missing from
    # _COMMON_DEFAULTS would silently vanish from the namespace and surface
    # later as an AttributeError. Fail at parser-build time instead.
    _common_dests = {a.dest for a in common._actions}
    assert _common_dests == set(_COMMON_DEFAULTS), (
        f"common options and _COMMON_DEFAULTS diverged: {_common_dests ^ set(_COMMON_DEFAULTS)}"
    )

    # Gated spec-generation profile selector (plan --gated and the spec family).
    profile_parent = argparse.ArgumentParser(add_help=False)
    profile_parent.add_argument(
        "--profile",
        type=str,
        default=None,
        help="Gated spec-generation profile name (default: lite)",
    )

    parser = _CommonDefaultsParser(
        description="spec-runner — task automation from markdown specs via Claude CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    from importlib.metadata import PackageNotFoundError, version

    try:
        _pkg_version = version("spec-runner")
    except PackageNotFoundError:
        _pkg_version = "0.0.0.dev"
    parser.add_argument(
        "--version",
        action="version",
        version=f"spec-runner {_pkg_version}",
        help="Print the spec-runner version and exit",
    )

    # Subparsers stay plain ArgumentParser: the defaults-filling hook only
    # needs to run once, on the top-level parse.
    subparsers = parser.add_subparsers(
        dest="command", help="Commands", parser_class=argparse.ArgumentParser
    )

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
        "--tui",
        action="store_true",
        help="Show TUI dashboard during execution",
    )
    run_parser.add_argument(
        "--force",
        action="store_true",
        help="Skip lock check (use when lock is stale)",
    )
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show which tasks would execute without running them",
    )
    run_parser.add_argument(
        "--json-result",
        action="store_true",
        help="Output structured JSON result per task (for Maestro interop)",
    )
    run_parser.add_argument(
        "--no-reset-failed",
        action="store_true",
        help="Do not reset failed→pending or clear consecutive_failures "
        "at the start of `run --all` (default: reset enabled).",
    )
    run_parser.add_argument(
        "--strict",
        action="store_true",
        help="Enforce spec governance: block unapproved managed tasks.md",
    )
    run_parser.add_argument(
        "--no-strict",
        action="store_true",
        help="Disable spec governance gate (default behavior)",
    )
    run_parser.add_argument(
        "--allow-dirty-spec",
        action="store_true",
        help="Execute even when spec/config files have uncommitted changes "
        "(default: refuse when git automation is on)",
    )

    # status
    status_parser = subparsers.add_parser("status", parents=[common], help="Show execution status")
    status_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Output status as JSON"
    )

    # retry
    retry_parser = subparsers.add_parser("retry", parents=[common], help="Retry failed task")
    retry_parser.add_argument("task_id", help="Task ID to retry")
    retry_parser.add_argument(
        "--allow-dirty-spec",
        action="store_true",
        help="Retry even when spec/config files have uncommitted changes "
        "(default: refuse when git automation is on)",
    )
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
    plan_parser = subparsers.add_parser(
        "plan", parents=[common, profile_parent], help="Interactive task planning"
    )
    plan_parser.add_argument(
        "description", nargs="?", default=None, help="Feature description (or use --from-file)"
    )
    plan_parser.add_argument(
        "--from-file",
        metavar="PATH",
        help="Read the feature description from a file instead of the positional argument",
    )
    plan_parser.add_argument(
        "--full",
        action="store_true",
        help="Generate full spec (requirements + design + tasks)",
    )
    plan_parser.add_argument(
        "--gated",
        action="store_true",
        help="Generate one gated spec stage, validate, write DRAFT, and stop",
    )
    plan_parser.add_argument(
        "--stage",
        choices=["requirements", "design", "tasks"],
        default=None,
        help="Stage to generate with --gated (default: auto-resolved next stage)",
    )
    plan_parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Disable the interactive checkpoint menu in --gated mode",
    )

    # validate
    subparsers.add_parser("validate", parents=[common], help="Validate tasks and config")

    # config (CLI profile presets)
    config_parser = subparsers.add_parser(
        "config", parents=[common], help="Apply a CLI profile preset to config"
    )
    config_parser.add_argument("--preset", help="CLI for both exec and review (mono)")
    config_parser.add_argument("--exec", dest="exec_cli", help="CLI for the exec/implementer stage")
    config_parser.add_argument("--review", dest="review_cli", help="CLI for the review stage")
    config_parser.add_argument("--model", help="Model for both slots")
    config_parser.add_argument(
        "--review-model", dest="review_model", help="Model for the review slot only"
    )
    config_parser.add_argument("--list-presets", action="store_true", help="List available presets")
    config_parser.add_argument(
        "--dry-run", action="store_true", help="Print keys that would change; write nothing"
    )
    config_parser.add_argument(
        "--apply", action="store_true", help="Update the CLI profile in an existing config"
    )

    # verify
    verify_parser = subparsers.add_parser(
        "verify", parents=[common], help="Verify post-execution compliance"
    )
    verify_parser.add_argument("--task", "-t", help="Verify specific task ID")
    verify_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Output as JSON"
    )
    verify_parser.add_argument(
        "--strict", action="store_true", help="Fail on warnings (missing traceability)"
    )

    # preflight (read-only readiness diagnostics, #142a)
    preflight_parser = subparsers.add_parser(
        "preflight",
        parents=[common],
        help="Read-only check of what is missing before tasks can run",
    )
    preflight_parser.add_argument(
        "--json",
        action="store_true",
        help="Machine-readable report (schemas/preflight-result.schema.json)",
    )

    # audit (pre-execution compliance)
    audit_parser = subparsers.add_parser(
        "audit",
        parents=[common],
        help="Static pre-execution audit of the spec triangle",
    )
    audit_group = audit_parser.add_mutually_exclusive_group()
    audit_group.add_argument(
        "--json",
        action="store_const",
        dest="output_format",
        const="json",
        help="Output as JSON",
    )
    audit_group.add_argument(
        "--csv",
        action="store_const",
        dest="output_format",
        const="csv",
        help="Output as CSV",
    )
    audit_parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings (orphans, uncovered) as failures",
    )

    # report
    report_parser = subparsers.add_parser(
        "report", parents=[common], help="Generate traceability matrix"
    )
    report_parser.add_argument("--milestone", "-m", help="Filter by milestone")
    report_parser.add_argument("--status", help="Filter by status (done/failed/todo/not covered)")
    report_parser.add_argument(
        "--uncovered-only", action="store_true", help="Show only uncovered requirements"
    )
    report_parser.add_argument(
        "--json", action="store_true", dest="json_output", help="Output as JSON"
    )

    # tui
    subparsers.add_parser("tui", parents=[common], help="Launch read-only TUI dashboard")

    # watch
    watch_parser = subparsers.add_parser(
        "watch", parents=[common], help="Continuously execute ready tasks"
    )
    watch_parser.add_argument(
        "--tui",
        action="store_true",
        help="Show TUI dashboard during watch",
    )
    watch_parser.add_argument(
        "--strict",
        action="store_true",
        help="Enforce spec governance: block unapproved managed tasks.md",
    )
    watch_parser.add_argument(
        "--no-strict",
        action="store_true",
        help="Disable spec governance gate (default behavior)",
    )
    watch_parser.add_argument(
        "--allow-dirty-spec",
        action="store_true",
        help="Watch even when spec/config files have uncommitted changes "
        "(default: refuse when git automation is on)",
    )

    # costs
    costs_parser = subparsers.add_parser(
        "costs", parents=[common], help="Show cost breakdown per task"
    )
    costs_parser.add_argument("--json", action="store_true", help="Output as JSON")
    costs_parser.add_argument(
        "--sort",
        choices=["id", "cost", "tokens", "name"],
        default="id",
        help="Sort order (default: task id)",
    )

    # mcp
    subparsers.add_parser("mcp", parents=[common], help="Launch read-only MCP server")

    # sync (post-merge closer for the integration-PR loop, #73)
    review_pr_parser = subparsers.add_parser(
        "review-pr",
        parents=[common],
        help="Review-bot loop: collect, verify, fix valid, gate, push, reply (#102)",
    )
    review_pr_parser.add_argument("pr_ref", help="PR URL or bare number (number = this repo)")
    review_pr_parser.add_argument(
        "--json", dest="json_output", action="store_true", help="Machine-readable report"
    )
    review_pr_parser.add_argument(
        "--verify-only",
        dest="verify_only",
        action="store_true",
        help="Stop after per-comment verdicts — no fixes, no replies (read-only)",
    )
    review_pr_parser.add_argument(
        "--no-verify",
        dest="no_verify",
        action="store_true",
        help="Collect and persist comments only; skip the verification agent",
    )

    sync_parser = subparsers.add_parser(
        "sync",
        parents=[common],
        help="Post-merge sync: pull base, prune merged run/task branches, check state",
    )
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without changing anything",
    )

    # tdd — operator remedies for a frozen red (#141 slice 3)
    tdd_parser = subparsers.add_parser(
        "tdd", parents=[common], help="TDD operator remedies (abandon / repair a red checkpoint)"
    )
    tdd_sub = tdd_parser.add_subparsers(dest="tdd_command", required=True)

    tdd_abandon = tdd_sub.add_parser(
        "abandon", parents=[common], help="This red was no good; start again (commit stays)"
    )
    tdd_repair = tdd_sub.add_parser(
        "repair",
        parents=[common],
        help="The edit to the locked file is legitimate; open a new lineage from a commit",
    )
    for sub in (tdd_abandon, tdd_repair):
        sub.add_argument("task_id", help="Task whose checkpoint is being remedied")
        sub.add_argument(
            "--checkpoint",
            help=(
                "The checkpoint id this remedy applies to (compare-and-swap). "
                "Optional when exactly one is active — the chosen id is printed"
            ),
        )
        sub.add_argument("--reason", required=True, help="Why — recorded, and not optional")
        sub.add_argument("--actor", help="Who (default: git user.email)")
    tdd_repair.add_argument("--commit", required=True, help="Commit carrying the repaired bytes")

    tdd_status = tdd_sub.add_parser(
        "status", parents=[common], help="Show checkpoints, claims and remedies (#141)"
    )
    tdd_checkpoints = tdd_sub.add_parser(
        "checkpoints", parents=[common], help="List active checkpoint ids"
    )
    for sub in (tdd_status, tdd_checkpoints):
        sub.add_argument("task_id", nargs="?", help="Limit to one task")
        sub.add_argument("--json", action="store_true", help="Machine-readable output")

    # doctor
    doctor_parser = subparsers.add_parser(
        "doctor", parents=[common], help="Probe CLI/model compatibility (real mini-task)"
    )
    doctor_parser.add_argument("--cli", help="Override the CLI command (claude/codex/pi/...)")
    doctor_parser.add_argument("--model", help="Override the model (executor + review)")
    doctor_parser.add_argument(
        "--with-review",
        action="store_true",
        help="Also probe the review stage (2nd model call)",
    )
    doctor_parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip the cost-gate confirmation"
    )
    doctor_parser.add_argument(
        "--strict", action="store_true", help="Exit non-zero on DEGRADED too"
    )
    doctor_parser.add_argument("--json", action="store_true", help="Machine-readable output")
    doctor_parser.add_argument("--keep", action="store_true", help="Keep the scratch workspace")
    # --budget is inherited from common (default None). Do NOT set_defaults(budget=...)
    # here: argparse shares Action objects across subparsers built with
    # parents=[common], so a doctor-local default would mutate the shared action
    # and leak into every other subcommand (#68). cmd_doctor resolves
    # None → DOCTOR_DEFAULT_BUDGET_USD itself.

    # spec (gated spec lifecycle: status, approve, reject, adopt, check)
    spec_parser = subparsers.add_parser(
        "spec", parents=[common], help="Manage spec lifecycle (gated governance)"
    )
    spec_sub = spec_parser.add_subparsers(dest="spec_command", help="Spec lifecycle commands")

    spec_sub.add_parser("status", parents=[profile_parent, common], help="Show per-stage status")

    spec_approve = spec_sub.add_parser(
        "approve", parents=[profile_parent, common], help="Approve a spec stage"
    )
    spec_approve.add_argument("stage", choices=["requirements", "design", "tasks"])

    spec_reject = spec_sub.add_parser(
        "reject", parents=[profile_parent, common], help="Reopen a spec stage as draft"
    )
    spec_reject.add_argument("stage", choices=["requirements", "design", "tasks"])

    spec_check = spec_sub.add_parser(
        "check", parents=[profile_parent, common], help="Refresh cached validation for a stage"
    )
    spec_check.add_argument("stage", choices=["requirements", "design", "tasks"])

    spec_adopt = spec_sub.add_parser(
        "adopt", parents=[profile_parent, common], help="Adopt an unmanaged spec file"
    )
    spec_adopt.add_argument("stage", choices=["requirements", "design", "tasks"])
    spec_adopt.add_argument(
        "--force", action="store_true", help="Adopt as approved even if validation fails"
    )

    # change (change-as-folder lifecycle, M2). Deliberately NOT parented on
    # `common`: flags like --change/--spec-prefix are meaningless here (the
    # change id is the positional arg) and would mutate config paths under
    # the archive gate. Only the options the family actually uses.
    change_common = argparse.ArgumentParser(add_help=False)
    change_common.add_argument(
        "--project-root",
        type=str,
        default="",
        help="Project root directory (default: current directory)",
    )
    change_common.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["debug", "info", "warning", "error"],
        help="Log level (default: info)",
    )
    change_common.add_argument("--log-json", action="store_true", help="Output logs as JSON lines")
    change_parser = subparsers.add_parser(
        "change", parents=[change_common], help="Manage change folders (new, list, archive)"
    )
    change_sub = change_parser.add_subparsers(dest="change_command", help="Change commands")

    ch_new = change_sub.add_parser("new", help="Create spec/changes/<id>/ with a tasks.md stub")
    ch_new.add_argument("change_id", help="Change id (kebab-case, e.g. add-dark-mode)")

    ch_list = change_sub.add_parser("list", help="List in-flight changes")
    ch_list.add_argument("--json", action="store_true", help="JSON output")

    ch_archive = change_sub.add_parser(
        "archive", help="Move a completed change to spec/changes/archive/"
    )
    ch_archive.add_argument("change_id", help="Change id to archive")
    ch_archive.add_argument(
        "--force", action="store_true", help="Archive even if tasks are not all done"
    )
    ch_archive.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the delta merge plan and archive destination without changing anything",
    )

    # task (unified: replaces spec-task binary)
    task_parser = subparsers.add_parser(
        "task", help="Task management (list, show, start, done, graph, sync)"
    )
    task_sub = task_parser.add_subparsers(dest="task_command", help="Task commands")

    task_common = argparse.ArgumentParser(add_help=False)
    task_common.add_argument(
        "--spec-prefix", type=str, default="", help='Spec file prefix (e.g. "phase5-")'
    )
    task_common.add_argument(
        "--change", type=str, default="", help="Operate on spec/changes/<id>/tasks.md"
    )

    t_list = task_sub.add_parser("list", aliases=["ls"], parents=[task_common], help="List tasks")
    t_list.add_argument("--status", "-s", choices=["todo", "in_progress", "done", "blocked"])
    t_list.add_argument("--priority", "-p", choices=["p0", "p1", "p2", "p3"])
    t_list.add_argument("--milestone", "-m", help="Filter by milestone")

    t_show = task_sub.add_parser("show", parents=[task_common], help="Task details")
    t_show.add_argument("task_id", help="Task ID (e.g., TASK-001)")

    t_start = task_sub.add_parser("start", parents=[task_common], help="Start task")
    t_start.add_argument("task_id", help="Task ID")
    t_start.add_argument("--force", "-f", action="store_true", help="Ignore dependencies")

    t_done = task_sub.add_parser("done", parents=[task_common], help="Complete task")
    t_done.add_argument("task_id", help="Task ID")
    t_done.add_argument("--force", "-f", action="store_true", help="Ignore incomplete checklist")

    t_block = task_sub.add_parser("block", parents=[task_common], help="Block task")
    t_block.add_argument("task_id", help="Task ID")

    t_check = task_sub.add_parser("check", parents=[task_common], help="Mark checklist item")
    t_check.add_argument("task_id", help="Task ID")
    t_check.add_argument("item_index", help="Item index (0, 1, 2...)")

    task_sub.add_parser("stats", parents=[task_common], help="Statistics")
    task_sub.add_parser("next", parents=[task_common], help="Next ready tasks")
    task_sub.add_parser("graph", parents=[task_common], help="Dependency graph")
    task_sub.add_parser("export-gh", parents=[task_common], help="Export to GitHub Issues")

    t_sync_to = task_sub.add_parser(
        "sync-to-gh", parents=[task_common], help="Sync tasks to GitHub Issues"
    )
    t_sync_to.add_argument("--dry-run", action="store_true", help="Preview without changes")

    task_sub.add_parser(
        "sync-from-gh", parents=[task_common], help="Sync GitHub Issues to tasks.md"
    )

    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Load config from YAML file, then override with CLI args. Resolve the
    # path once — _resolve_config_path() prints a deprecation warning for the
    # legacy location, which must not appear twice.
    config_path = _resolve_config_path()
    from .config import ConfigError

    # A config the loader refuses (#182: flat keys silently discarded by an
    # `executor:` wrapper) must stop the run with a readable line, not a
    # traceback — and certainly not by falling back to the defaults it was
    # written to avoid.
    try:
        yaml_config = load_config_from_yaml(config_path)
    except ConfigError as exc:
        if args.command != "validate":
            raise SystemExit(f"⛔ {exc}") from None
        # `validate` is the command whose job is to report exactly this. Dying
        # here would hand the operator one problem at a time; let it run and
        # list everything wrong with the setup in one pass. Nothing executes
        # under `validate`, so the empty config below cannot start a run on
        # defaults.
        yaml_config = {}
    config = build_config(yaml_config, args)
    config.config_found = config_path.exists()

    # Fail fast with a clean message (no traceback) on an unknown spec profile,
    # or on a `tdd_runner` that does not exist or that the test command cannot
    # carry (#198 — a declared runner does not prove the command can carry it,
    # and reading one runner's exit codes as another's is the defect itself).
    try:
        config.resolve_spec_profile()
        config.resolve_tdd_runner()
    except ConfigError as exc:
        raise SystemExit(f"⛔ {exc}") from None

    # #157: a required review that never runs can only ever block. `validate`
    # catches this in YAML; catching it here too covers `--no-review`, which
    # sets the flag after the file is read. Refusing at startup beats
    # discovering it at the merge gate with the work already done.
    if config.review_policy == "required" and not config.run_review:
        raise SystemExit(
            "⛔ review_policy is 'required' but review is disabled "
            "(run_review: false or --no-review) — a required review that never "
            "runs can only ever block"
        )

    # Attach the gates this config asks for, once per process. Under the
    # default `review_policy: advisory` nothing is registered at all, so the
    # pre-terminal site stays dormant (#164 criterion 8).
    from .gates import register_builtin_gates

    register_builtin_gates(config)

    from .logging import setup_logging

    setup_logging(level=config.log_level, json_output=getattr(args, "log_json", False))

    import structlog

    structlog.contextvars.bind_contextvars(run_id=uuid4().hex[:8])

    # #63: a run without a config file silently used all defaults — including
    # self-merge into main and a Python test command on non-Python repos.
    # Warn loudly before any execution command proceeds.
    if args.command in ("run", "watch", "retry"):
        from .config import missing_config_warning

        warning = missing_config_warning(config)
        if warning is not None:
            print(warning, file=sys.stderr)
            logger.warning("No config file found — using built-in defaults")

    # Register signal handlers for graceful shutdown (late import to avoid circular)
    from .executor import _pause_handler, _signal_handler

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGQUIT, _pause_handler)

    # Dispatch
    try:
        commands = {
            "run": cmd_run,
            "status": cmd_status,
            "costs": cmd_costs,
            "retry": cmd_retry,
            "logs": cmd_logs,
            "stop": cmd_stop,
            "reset": cmd_reset,
            "plan": cmd_plan,
            "validate": cmd_validate,
            "verify": cmd_verify,
            "audit": cmd_audit,
            "preflight": cmd_preflight,
            "report": cmd_report,
            "tui": cmd_tui,
            "watch": cmd_watch,
            "mcp": cmd_mcp,
            "doctor": cmd_doctor,
            "sync": cmd_sync,
            "config": cmd_config,
        }

        # review-pr (#102 M1): stable exit-code contract for external callers
        # (0 = all verified, 1 = fail-closed, 2 = NEEDS_HUMAN)
        if args.command == "review-pr":
            from .review_pr import cmd_review_pr

            raise SystemExit(cmd_review_pr(args, config))

        # tdd remedies: a refusal is an operator-facing message, not a traceback
        if args.command == "tdd":
            if args.tdd_command in ("status", "checkpoints"):
                from .tdd_status import cmd_tdd_checkpoints, cmd_tdd_status

                handler = cmd_tdd_status if args.tdd_command == "status" else cmd_tdd_checkpoints
                raise SystemExit(handler(args, config))

            from .remedy import cmd_tdd

            raise SystemExit(cmd_tdd(args, config))

        # Handle unified task subcommand
        if args.command == "task":
            _dispatch_task_command(args)
            return

        # Handle change-as-folder subcommand (new/list/archive)
        if args.command == "change":
            from . import change_commands

            handler = {
                "new": change_commands.cmd_change_new,
                "list": change_commands.cmd_change_list,
                "archive": change_commands.cmd_change_archive,
            }.get(args.change_command)
            if handler is None:
                # no sub-subcommand given -> default to `change list`
                raise SystemExit(change_commands.cmd_change_list(args, config))
            raise SystemExit(handler(args, config))

        # Handle spec lifecycle subcommand (status/approve/reject/adopt/check)
        if args.command == "spec":
            from . import spec_commands

            handler = {
                "status": spec_commands.cmd_spec_status,
                "approve": spec_commands.cmd_spec_approve,
                "reject": spec_commands.cmd_spec_reject,
                "adopt": spec_commands.cmd_spec_adopt,
                "check": spec_commands.cmd_spec_check,
            }.get(args.spec_command)
            if handler is None:
                # no sub-subcommand given -> default to `spec status`
                raise SystemExit(spec_commands.cmd_spec_status(args, config))
            raise SystemExit(handler(args, config))

        cmd_func = commands.get(args.command)
        if cmd_func:
            cmd_func(args, config)
    except SpecMetaError as exc:
        raise SystemExit(f"⛔ {exc}") from None


if __name__ == "__main__":
    main()
