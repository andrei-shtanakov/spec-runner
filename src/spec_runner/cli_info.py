"""CLI info/query commands: status, costs, logs, stop, reset, validate, verify, report, tui, mcp."""

import argparse
import json
import shutil
import sys
from datetime import datetime

from .config import (
    ExecutorConfig,
    _resolve_config_path,
)
from .logging import get_logger
from .state import (
    ExecutorState,
    clear_stop_file,
)
from .task import (
    Task,
    parse_tasks,
)

logger = get_logger("cli")


def cmd_status(args, config: ExecutorConfig):
    """Execution status"""

    with ExecutorState(config) as state:
        # Parse tasks from tasks.md to cross-reference
        all_tasks: list[Task] = []
        if config.tasks_file.exists():
            all_tasks = parse_tasks(config.tasks_file)

        # --json: output matching MCP server format
        if getattr(args, "json_output", False):
            completed = sum(1 for ts in state.tasks.values() if ts.status == "success")
            failed = sum(1 for ts in state.tasks.values() if ts.status == "failed")
            running = sum(1 for ts in state.tasks.values() if ts.status == "running")
            cost = state.total_cost()
            inp, out = state.total_tokens()
            print(
                json.dumps(
                    {
                        "total_tasks": len(all_tasks),
                        "completed": completed,
                        "failed": failed,
                        "running": running,
                        "not_started": len(all_tasks) - completed - failed - running,
                        "total_cost": round(cost, 2),
                        "input_tokens": inp,
                        "output_tokens": out,
                        "budget_usd": config.budget_usd,
                    }
                )
            )
            return
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

        print("\n📊 spec-runner Status")
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


def cmd_validate(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Validate tasks file and config, print results."""
    from .validate import format_results, validate_all

    result = validate_all(
        tasks_file=config.tasks_file,
        config_file=_resolve_config_path(),
    )
    output = format_results(result)
    print(output)
    if not result.ok:
        sys.exit(1)


def cmd_verify(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Verify post-execution compliance against spec."""
    from .verify import format_verify_json, format_verify_text, verify_all

    task_id = getattr(args, "task", None)
    strict = getattr(args, "strict", False)
    report = verify_all(config, task_id=task_id, strict=strict)

    if getattr(args, "json_output", False):
        print(format_verify_json(report))
    else:
        print(format_verify_text(report))

    if not report.ok:
        sys.exit(1)


def cmd_audit(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Run pre-execution static audit of the spec triangle."""
    from .audit import (
        audit_all,
        format_audit_csv,
        format_audit_json,
        format_audit_text,
    )

    strict = getattr(args, "strict", False)
    report = audit_all(config, strict=strict)

    output_format = getattr(args, "output_format", "text")
    if output_format == "json":
        print(format_audit_json(report))
    elif output_format == "csv":
        print(format_audit_csv(report), end="")
    else:
        print(format_audit_text(report))

    if not report.ok:
        sys.exit(1)


def cmd_report(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Generate traceability matrix report."""
    from .report import build_report, format_report_json, format_report_markdown

    milestone = getattr(args, "milestone", None)
    status_filter = getattr(args, "status", None)
    uncovered = getattr(args, "uncovered_only", False)
    report = build_report(
        config, milestone=milestone, status_filter=status_filter, uncovered_only=uncovered
    )

    if getattr(args, "json_output", False):
        print(format_report_json(report))
    else:
        print(format_report_markdown(report))


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


def cmd_mcp(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Launch MCP server (stdio transport)."""
    from .mcp_server import run_server

    run_server()
