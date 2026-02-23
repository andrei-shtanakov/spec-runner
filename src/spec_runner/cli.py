"""CLI commands and argument parsing for spec-runner."""

import argparse
import asyncio
import json
import re
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime
from uuid import uuid4

from .config import (
    CONFIG_FILE,
    ExecutorConfig,
    ExecutorLock,
    build_config,
    load_config_from_yaml,
)
from .execution import (
    execute_task,
    run_with_retries,
)
from .hooks import ensure_on_main_branch
from .logging import get_logger
from .parallel import _run_tasks_parallel
from .prompt import (
    load_prompt_template,
    render_template,
)
from .runner import (
    build_cli_command,
    check_error_patterns,
    log_progress,
)
from .state import (
    ExecutorState,
    check_stop_requested,
    clear_stop_file,
    recover_stale_tasks,
)
from .task import (
    Task,
    get_next_tasks,
    get_task_by_id,
    mark_all_checklist_done,
    parse_tasks,
    resolve_dependencies,
    update_task_status,
)
from .validate import format_results, validate_all

logger = get_logger("executor")


# === CLI Commands ===


def cmd_run(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Execute tasks."""
    # HITL review incompatible with parallel/TUI modes
    if config.hitl_review and getattr(args, "parallel", False):
        logger.warning("--hitl-review ignored in parallel mode (interactive prompts not supported)")
        config.hitl_review = False
    if config.hitl_review and getattr(args, "tui", False):
        logger.warning("--hitl-review ignored in TUI mode (TUI owns the screen)")
        config.hitl_review = False

    if getattr(args, "tui", False):
        import threading

        from .logging import setup_logging
        from .tui import SpecRunnerApp

        # TUI mode: log to file, TUI owns screen
        log_file = config.logs_dir / f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        setup_logging(level=config.log_level, tui_mode=True, log_file=log_file)

        app = SpecRunnerApp(config=config)

        def _start_execution() -> None:
            def run() -> None:
                if getattr(args, "parallel", False):
                    config.create_git_branch = False
                    if getattr(args, "max_concurrent", 0) > 0:
                        config.max_concurrent = args.max_concurrent
                    asyncio.run(_run_tasks_parallel(args, config))
                else:
                    _run_tasks(args, config)

            t = threading.Thread(target=run, daemon=True)
            t.start()

        app.call_later(_start_execution)
        app.run()
        return

    if getattr(args, "parallel", False):
        # Parallel mode implies no branch
        config.create_git_branch = False
        if getattr(args, "max_concurrent", 0) > 0:
            config.max_concurrent = args.max_concurrent
        asyncio.run(_run_tasks_parallel(args, config))
    else:
        if getattr(args, "force", False):
            logger.warning("Skipping lock check (--force)")
            _run_tasks(args, config)
        else:
            # Acquire lock to prevent parallel runs
            lock = ExecutorLock(config.state_file.with_suffix(".lock"))
            if not lock.acquire():
                held_by = getattr(lock, "_held_by", {})
                pid = held_by.get("pid", "unknown")
                started = held_by.get("started", "unknown")
                alive = held_by.get("alive", "true")

                logger.error(
                    "Another executor is already running",
                    lock_file=str(config.state_file.with_suffix(".lock")),
                    held_by_pid=pid,
                    started=started,
                    process_alive=alive,
                )
                if alive == "false":
                    logger.error(
                        "Lock holder is dead. Use --force to override, "
                        "or delete the lock file manually."
                    )
                sys.exit(1)

            try:
                _run_tasks(args, config)
            finally:
                lock.release()


def _run_tasks(args, config: ExecutorConfig):
    """Internal task execution logic."""
    # Clear any leftover stop file from previous runs
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

        # Check failure limit
        if state.should_stop():
            logger.error(
                "Stopped due to consecutive failures",
                consecutive_failures=state.consecutive_failures,
            )
            return

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
            return

        logger.info("Tasks to execute", count=len(tasks_to_run))
        for t in tasks_to_run:
            logger.info("Queued task", task_id=t.id, name=t.name)

        # Execute
        if args.all:
            # For --all mode, continuously re-evaluate ready tasks after each completion
            executed_ids: set[str] = set()
            include_in_progress = not getattr(args, "restart", False)
            while True:
                # Check for graceful shutdown request
                if check_stop_requested(config):
                    clear_stop_file(config)
                    logger.info("Graceful shutdown requested")
                    log_progress("🛑 Graceful shutdown requested")
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
                    else:
                        logger.info("All tasks completed")
                        # Ensure we're on main branch at the end
                        ensure_on_main_branch(config)
                    break

                task = ready_tasks[0]
                executed_ids.add(task.id)

                logger.info("Next ready task", task_id=task.id, name=task.name)

                result = run_with_retries(task, config, state)

                # "SKIP" means continue to next task
                if result == "SKIP":
                    continue

                if result is False and state.should_stop():
                    logger.warning("Stopping: too many consecutive failures")
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

                if result == "SKIP":
                    continue

                if result is False and state.should_stop():
                    logger.warning("Stopping: too many consecutive failures")
                    break

        # Summary
        # Re-read tasks to get updated statuses after execution
        tasks = parse_tasks(config.tasks_file)

        # Calculate statistics
        failed_attempts = sum(
            1 for ts in state.tasks.values() for a in ts.attempts if not a.success
        )
        remaining = len([t for t in tasks if t.status == "todo"])

        logger.info(
            "Execution summary",
            completed=state.total_completed,
            failed=state.total_failed,
            remaining=remaining,
            failed_attempts=failed_attempts if failed_attempts > 0 else None,
        )


def cmd_status(args, config: ExecutorConfig):
    """Execution status"""

    with ExecutorState(config) as state:
        # Parse tasks from tasks.md to cross-reference
        all_tasks: list[Task] = []
        if config.tasks_file.exists():
            all_tasks = parse_tasks(config.tasks_file)
        total_in_spec = len(all_tasks)

        # Calculate statistics from actual task state
        completed_tasks = sum(1 for ts in state.tasks.values() if ts.status == "success")
        failed_tasks = sum(1 for ts in state.tasks.values() if ts.status == "failed")
        running_tasks = [ts for ts in state.tasks.values() if ts.status == "running"]
        failed_attempts = sum(
            1 for ts in state.tasks.values() for a in ts.attempts if not a.success
        )

        # Find tasks in spec but not in state (pending / never started)
        state_ids = set(state.tasks.keys())
        not_started = [t for t in all_tasks if t.id not in state_ids]

        print("\n📊 Executor Status")
        print(f"{'=' * 50}")
        print(f"Tasks in spec:         {total_in_spec}")
        print(f"Tasks completed:       {completed_tasks}")
        print(f"Tasks failed:          {failed_tasks}")
        if running_tasks:
            print(f"Tasks in progress:     {len(running_tasks)}")
        if not_started:
            print(f"Tasks not started:     {len(not_started)}")
        if failed_attempts > 0:
            print(f"Failed attempts:       {failed_attempts} (retried)")
        print(
            f"Consecutive failures:  {state.consecutive_failures}/{config.max_consecutive_failures}"
        )

        # Token/cost summary
        total_cost_val = state.total_cost()
        if total_cost_val > 0:
            total_inp, total_out = state.total_tokens()

            def _fmt_tokens(n: int) -> str:
                if n >= 1000:
                    return f"{n / 1000:.1f}K"
                return str(n)

            print(
                f"Tokens:                {_fmt_tokens(total_inp)} in / {_fmt_tokens(total_out)} out"
            )
            print(f"Total cost:            ${total_cost_val:.2f}")

        # Tasks with attempts
        attempted = [ts for ts in state.tasks.values() if ts.attempts]
        if attempted:
            print("\n📝 Task History:")
            for ts in attempted:
                icon = "✅" if ts.status == "success" else "❌" if ts.status == "failed" else "🔄"
                attempts_info = f"{ts.attempt_count} attempt"
                if ts.attempt_count > 1:
                    attempts_info += "s"
                task_cost = state.task_cost(ts.task_id)
                if task_cost > 0:
                    attempts_info += f", ${task_cost:.2f}"
                print(f"   {icon} {ts.task_id}: {ts.status} ({attempts_info})")
                # Show review verdict from last attempt
                if ts.attempts:
                    last_attempt = ts.attempts[-1]
                    if last_attempt.review_status and last_attempt.review_status != "skipped":
                        print(f"      Review: {last_attempt.review_status}")
                if ts.status == "failed" and ts.last_error:
                    print(f"      Last error: {ts.last_error[:50]}...")
                elif ts.status == "running" and ts.last_error:
                    print(f"      ⚠️  Last attempt failed: {ts.last_error[:50]}...")

        # Show tasks not yet in executor state
        if not_started:
            print(f"\n⏳ Not started ({len(not_started)}):")
            for t in not_started:
                print(f"   ⬜ {t.id}: {t.name}")


def cmd_costs(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Show cost breakdown per task with optional JSON output."""
    tasks = parse_tasks(config.tasks_file)

    if not tasks:
        print("No tasks found")
        return

    with ExecutorState(config) as state:
        # Build per-task cost info
        task_rows: list[dict] = []
        for t in tasks:
            ts = state.tasks.get(t.id)
            cost = state.task_cost(t.id)
            if ts:
                inp_tokens = sum(a.input_tokens for a in ts.attempts if a.input_tokens is not None)
                out_tokens = sum(
                    a.output_tokens for a in ts.attempts if a.output_tokens is not None
                )
                task_rows.append(
                    {
                        "task_id": t.id,
                        "name": t.name,
                        "status": ts.status,
                        "cost": cost,
                        "attempts": ts.attempt_count,
                        "input_tokens": inp_tokens,
                        "output_tokens": out_tokens,
                        "total_tokens": inp_tokens + out_tokens,
                    }
                )
            else:
                task_rows.append(
                    {
                        "task_id": t.id,
                        "name": t.name,
                        "status": t.status,
                        "cost": 0.0,
                        "attempts": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "no_state": True,
                    }
                )

        # Sort
        sort_key = getattr(args, "sort", "id")
        if sort_key == "cost":
            task_rows.sort(key=lambda r: r["cost"], reverse=True)
        elif sort_key == "tokens":
            task_rows.sort(key=lambda r: r["total_tokens"], reverse=True)
        elif sort_key == "name":
            task_rows.sort(key=lambda r: r["name"])
        # default "id" — already in parse order (task id order)

        # Summary
        total_cost = state.total_cost()
        total_inp, total_out = state.total_tokens()
        completed_costs = [r["cost"] for r in task_rows if r["cost"] > 0]
        avg_cost = sum(completed_costs) / len(completed_costs) if completed_costs else 0.0
        most_expensive = max(task_rows, key=lambda r: r["cost"]) if task_rows else None

        summary = {
            "total_cost": round(total_cost, 2),
            "total_input_tokens": total_inp,
            "total_output_tokens": total_out,
            "avg_cost_per_completed": round(avg_cost, 2),
            "most_expensive_task": (
                most_expensive["task_id"] if most_expensive and most_expensive["cost"] > 0 else None
            ),
        }
        if config.budget_usd is not None:
            pct = (total_cost / config.budget_usd * 100) if config.budget_usd > 0 else 0.0
            summary["budget_usd"] = config.budget_usd
            summary["budget_used_pct"] = round(pct, 1)

        if getattr(args, "json", False):
            # JSON output
            json_tasks = []
            for r in task_rows:
                json_tasks.append(
                    {
                        "task_id": r["task_id"],
                        "name": r["name"],
                        "status": r["status"],
                        "cost": r["cost"],
                        "attempts": r["attempts"],
                        "input_tokens": r["input_tokens"],
                        "output_tokens": r["output_tokens"],
                    }
                )
            print(json.dumps({"tasks": json_tasks, "summary": summary}, indent=2))
            return

        # Text table output
        print(f"\n{'Task':<12} {'Name':<30} {'Status':<10} {'Cost':>8} {'Att':>4} {'Tokens':>10}")
        print("-" * 78)
        for r in task_rows:
            if r.get("no_state"):
                cost_str = "--"
                att_str = "--"
                tok_str = "--"
            else:
                cost_str = f"${r['cost']:.2f}"
                att_str = str(r["attempts"])
                tok_str = f"{r['total_tokens']}"
            name = r["name"][:28]
            print(
                f"{r['task_id']:<12} {name:<30} {r['status']:<10} "
                f"{cost_str:>8} {att_str:>4} {tok_str:>10}"
            )

        # Summary section
        print(f"\n{'=' * 40}")
        print(f"Total cost:           ${total_cost:.2f}")
        if total_inp > 0 or total_out > 0:

            def _fmt_tok(n: int) -> str:
                return f"{n / 1000:.1f}K" if n >= 1000 else str(n)

            print(
                f"Total tokens:         {_fmt_tok(total_inp)} input, {_fmt_tok(total_out)} output"
            )
        if config.budget_usd is not None:
            pct = (total_cost / config.budget_usd * 100) if config.budget_usd > 0 else 0.0
            print(f"Budget used:          {pct:.0f}% of ${config.budget_usd:.2f}")
        if completed_costs:
            print(f"Avg per completed:    ${avg_cost:.2f}")
        if most_expensive and most_expensive["cost"] > 0:
            print(
                f"Most expensive:       {most_expensive['task_id']} (${most_expensive['cost']:.2f})"
            )


def cmd_retry(args, config: ExecutorConfig):
    """Retry failed task, preserving error context from previous attempts."""

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


def cmd_logs(args, config: ExecutorConfig):
    """Show task logs"""

    task_id = args.task_id.upper()
    log_files = sorted(config.logs_dir.glob(f"{task_id}-*.log"))

    if not log_files:
        logger.info("No logs found", task_id=task_id)
        return

    latest = log_files[-1]
    logger.info("Showing latest log", task_id=task_id, log_file=str(latest))
    print(latest.read_text()[:5000])  # Limit output — raw log content to stdout


def cmd_stop(args, config: ExecutorConfig):
    """Request graceful shutdown of the running executor."""
    stop_file = config.stop_file
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text(f"Stop requested at {datetime.now().isoformat()}\n")
    logger.info("Stop requested", stop_file=str(stop_file))


def cmd_reset(args, config: ExecutorConfig):
    """Reset executor state"""

    if config.state_file.exists():
        config.state_file.unlink()
        logger.info("State reset", state_file=str(config.state_file))

    clear_stop_file(config)

    if args.logs and config.logs_dir.exists():
        shutil.rmtree(config.logs_dir)
        logger.info("Logs cleared", logs_dir=str(config.logs_dir))


def cmd_plan(args, config: ExecutorConfig):
    """Interactive task planning via Claude.

    With --full flag, runs a three-stage pipeline to generate
    requirements, design, and tasks files from a description.
    """

    description = args.description

    if getattr(args, "full", False):
        from .prompt import build_generation_prompt, parse_spec_marker

        stages = ["requirements", "design", "tasks"]
        stage_files = {
            "requirements": config.requirements_file,
            "design": config.design_file,
            "tasks": config.tasks_file,
        }
        marker_names = {
            "requirements": "REQUIREMENTS",
            "design": "DESIGN",
            "tasks": "TASKS",
        }
        context: dict[str, str] = {}

        for stage in stages:
            logger.info("Generating spec", stage=stage)
            prompt = build_generation_prompt(stage, description, context)

            cmd = build_cli_command(
                cmd=config.claude_command,
                prompt=prompt,
                model=config.claude_model,
                template=config.command_template,
                skip_permissions=config.skip_permissions,
            )
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.task_timeout_minutes * 60,
                cwd=config.project_root,
            )

            if result.returncode != 0:
                logger.error(
                    "Generation failed",
                    stage=stage,
                    stderr=result.stderr[:500],
                )
                print(f"Failed at stage: {stage}")
                sys.exit(1)

            content = parse_spec_marker(result.stdout, marker_names[stage])
            if not content:
                logger.error("No spec marker found in output", stage=stage)
                print(f"Claude did not produce {stage} content.")
                sys.exit(1)

            output_file = stage_files[stage]
            output_file.parent.mkdir(parents=True, exist_ok=True)
            output_file.write_text(content + "\n")
            logger.info("Spec written", stage=stage, file=str(output_file))
            print(f"Written: {output_file}")

            context[stage] = content

        print("\nSpec generation complete!")
        print(f"  Requirements: {config.requirements_file}")
        print(f"  Design:       {config.design_file}")
        print(f"  Tasks:        {config.tasks_file}")
        return

    print(f"\n📝 Planning: {description}")
    print("=" * 60)

    # Load context
    requirements_summary = "No requirements.md found"
    if config.requirements_file.exists():
        content = config.requirements_file.read_text()
        # Extract just headers and first lines for summary
        lines = content.split("\n")[:100]
        requirements_summary = "\n".join(lines) + "\n...(truncated)"

    design_summary = "No design.md found"
    if config.design_file.exists():
        content = config.design_file.read_text()
        lines = content.split("\n")[:100]
        design_summary = "\n".join(lines) + "\n...(truncated)"

    # Get existing tasks
    existing_tasks = "No existing tasks"
    if config.tasks_file.exists():
        tasks = parse_tasks(config.tasks_file)
        task_lines = [f"- {t.id}: {t.name} ({t.status})" for t in tasks[-20:]]
        existing_tasks = "\n".join(task_lines) if task_lines else "No tasks yet"

    # Load template
    template = load_prompt_template("plan")

    if template:
        prompt = render_template(
            template,
            {
                "DESCRIPTION": description,
                "REQUIREMENTS_SUMMARY": requirements_summary,
                "DESIGN_SUMMARY": design_summary,
                "EXISTING_TASKS": existing_tasks,
            },
        )
    else:
        prompt = f"""# Task Planning Request

## Feature Description:
{description}

## Project Context:

### Requirements (excerpt):
{requirements_summary}

### Existing Tasks:
{existing_tasks}

## Instructions:

Create structured tasks for this feature. For each task use format:

### TASK-XXX: <title>
🔴 P0 | ⬜ TODO | Est: Xd

**Checklist:**
- [ ] Implementation items
- [ ] Tests

When done, respond with: PLAN_READY
"""

    log_progress(f"📝 Planning: {description}")

    # Save prompt
    log_file = config.logs_dir / f"plan-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    with open(log_file, "w") as f:
        f.write(f"=== PLAN PROMPT ===\n{prompt}\n\n")

    # Interactive loop
    conversation_history = []

    while True:
        # Run Claude
        try:
            cmd = [config.claude_command, "-p", prompt]
            if config.skip_permissions:
                cmd.append("--dangerously-skip-permissions")

            print("\n🤖 Claude is analyzing...")

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.task_timeout_minutes * 60,
                cwd=config.project_root,
            )

            output = result.stdout

            # Save output
            with open(log_file, "a") as f:
                f.write(f"=== OUTPUT ===\n{output}\n\n")

            # Check for API errors
            error_pattern = check_error_patterns(output + result.stderr)
            if error_pattern:
                print(f"\n⚠️  API error: {error_pattern}")
                return

            # Check for QUESTION
            question_match = re.search(r"QUESTION:\s*(.+?)(?:OPTIONS:|$)", output, re.DOTALL)
            if question_match:
                question = question_match.group(1).strip()
                print(f"\n❓ {question}")

                # Extract options
                options_match = re.search(r"OPTIONS:\s*(.+?)(?:$)", output, re.DOTALL)
                if options_match:
                    options_text = options_match.group(1)
                    options = re.findall(r"[-*]\s*(.+)", options_text)
                    if options:
                        print("\nOptions:")
                        for i, opt in enumerate(options, 1):
                            print(f"  {i}. {opt.strip()}")
                        print(f"  {len(options) + 1}. Other (type custom answer)")

                        choice = input("\nYour choice (number or text): ").strip()

                        # Determine answer
                        try:
                            idx = int(choice)
                            if 1 <= idx <= len(options):
                                answer = options[idx - 1].strip()
                            else:
                                answer = input("Enter your answer: ").strip()
                        except ValueError:
                            answer = choice

                        # Add to conversation
                        conversation_history.append(f"Q: {question}\nA: {answer}")
                        prompt = f"{prompt}\n\nPrevious Q&A:\n" + "\n".join(conversation_history)
                        prompt += f"\n\nContinue planning with the answer: {answer}"
                        continue

                # No parseable options, ask for freeform input
                answer = input("\nYour answer: ").strip()
                conversation_history.append(f"Q: {question}\nA: {answer}")
                prompt += f"\n\nAnswer: {answer}\n\nContinue planning."
                continue

            # Check for TASK_PROPOSAL or PLAN_READY
            if "PLAN_READY" in output or "TASK_PROPOSAL" in output:
                print("\n" + "=" * 60)
                print("📋 Proposed Tasks:")
                print("=" * 60)

                # Extract task proposals
                task_blocks = re.findall(
                    r"### (TASK-\d+:.+?)(?=### TASK-|\Z|PLAN_READY)",
                    output,
                    re.DOTALL,
                )

                for block in task_blocks:
                    print(f"\n### {block.strip()[:500]}")

                print("\n" + "=" * 60)

                # Ask for confirmation
                confirm = input("\nAdd these tasks to tasks.md? [y/N/edit]: ").strip().lower()

                if confirm == "y":
                    # Append tasks to tasks.md
                    tasks_file = config.tasks_file
                    content = tasks_file.read_text() if tasks_file.exists() else "# Tasks\n\n"

                    for block in task_blocks:
                        content += f"\n### {block.strip()}\n"

                    tasks_file.write_text(content)
                    print(f"\n✅ Added {len(task_blocks)} task(s) to {tasks_file}")
                    log_progress(f"✅ Created {len(task_blocks)} tasks")

                elif confirm == "edit":
                    print(f"\nEdit {config.tasks_file} manually, then run 'spec-runner run'")

                else:
                    print("\n❌ Cancelled")

                return

            # No recognizable signal, show output and exit
            print("\n📄 Claude response:")
            print(output[:2000])
            return

        except subprocess.TimeoutExpired:
            print(f"\n⏰ Planning timeout after {config.task_timeout_minutes}m")
            return
        except KeyboardInterrupt:
            print("\n\n❌ Cancelled by user")
            return
        except Exception as e:
            print(f"\n💥 Error: {e}")
            return


def cmd_validate(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Validate tasks file and config, print results."""
    from .validate import format_results, validate_all

    result = validate_all(
        tasks_file=config.tasks_file,
        config_file=config.project_root / "executor.config.yaml",
    )
    output = format_results(result)
    print(output)
    if not result.ok:
        sys.exit(1)


def cmd_watch(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Continuously watch tasks.md and execute ready tasks."""
    # Pre-run validation
    pre_result = validate_all(
        tasks_file=config.tasks_file,
        config_file=config.project_root / CONFIG_FILE,
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


def cmd_mcp(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Launch MCP server (stdio transport)."""
    from .mcp_server import run_server

    run_server()


def cmd_tui(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Launch read-only TUI dashboard."""
    from .logging import setup_logging
    from .tui import SpecRunnerApp

    # TUI mode: log to file, TUI owns screen
    log_file = config.logs_dir / f"tui-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(level=config.log_level, tui_mode=True, log_file=log_file)

    app = SpecRunnerApp(config=config)
    app.run()


# === Main ===


def main():
    # Shared options available to every subcommand
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--max-retries", type=int, default=3, help="Max retries per task (default: 3)"
    )
    common.add_argument(
        "--timeout", type=int, default=30, help="Task timeout in minutes (default: 30)"
    )
    common.add_argument("--no-tests", action="store_true", help="Skip tests on task completion")
    common.add_argument("--no-branch", action="store_true", help="Skip git branch creation")
    common.add_argument("--no-commit", action="store_true", help="Skip auto-commit on success")
    common.add_argument("--no-review", action="store_true", help="Skip code review after task")
    common.add_argument(
        "--hitl-review",
        action="store_true",
        help="Enable interactive approval gate after code review",
    )
    common.add_argument(
        "--callback-url", type=str, default="", help="URL to POST task status updates to"
    )
    common.add_argument(
        "--spec-prefix",
        type=str,
        default="",
        help='Spec file prefix (e.g. "phase5-" for phase5-tasks.md)',
    )
    common.add_argument(
        "--project-root",
        type=str,
        default="",
        help="Project root directory (default: current directory)",
    )
    common.add_argument(
        "--log-level",
        type=str,
        default=None,
        choices=["debug", "info", "warning", "error"],
        help="Log level (default: info)",
    )
    common.add_argument(
        "--log-json",
        action="store_true",
        help="Output logs as JSON lines",
    )

    parser = argparse.ArgumentParser(
        description="spec-runner — task automation from markdown specs via Claude CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

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
        "--parallel",
        action="store_true",
        help="Execute ready tasks in parallel (implies --no-branch)",
    )
    run_parser.add_argument(
        "--max-concurrent",
        type=int,
        default=0,
        help="Max parallel tasks (default: from config, typically 3)",
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

    # status
    subparsers.add_parser("status", parents=[common], help="Show execution status")

    # retry
    retry_parser = subparsers.add_parser("retry", parents=[common], help="Retry failed task")
    retry_parser.add_argument("task_id", help="Task ID to retry")
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
    plan_parser = subparsers.add_parser("plan", parents=[common], help="Interactive task planning")
    plan_parser.add_argument("description", help="Feature description")
    plan_parser.add_argument(
        "--full",
        action="store_true",
        help="Generate full spec (requirements + design + tasks)",
    )

    # validate
    subparsers.add_parser("validate", parents=[common], help="Validate tasks and config")

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

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Load config from YAML file, then override with CLI args
    yaml_config = load_config_from_yaml()
    config = build_config(yaml_config, args)

    from .logging import setup_logging

    setup_logging(level=config.log_level, json_output=getattr(args, "log_json", False))

    import structlog

    structlog.contextvars.bind_contextvars(run_id=uuid4().hex[:8])

    # Register signal handlers for graceful shutdown (late import to avoid circular)
    from .executor import _signal_handler

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # Dispatch
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
        "tui": cmd_tui,
        "watch": cmd_watch,
        "mcp": cmd_mcp,
    }

    cmd_func = commands.get(args.command)
    if cmd_func:
        cmd_func(args, config)


if __name__ == "__main__":
    main()
