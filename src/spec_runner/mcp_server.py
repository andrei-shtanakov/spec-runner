"""Read-only MCP server for spec-runner -- exposes status, tasks, costs, logs as tools."""

import json

from mcp.server.fastmcp import FastMCP

from .config import ExecutorConfig, build_config, load_config_from_yaml
from .state import ExecutorState
from .task import parse_tasks, resolve_dependencies

mcp_app = FastMCP("spec-runner")


def _build_config(spec_prefix: str = "") -> ExecutorConfig:
    """Build ExecutorConfig from YAML + optional spec_prefix."""
    import argparse

    yaml_config = load_config_from_yaml()
    args = argparse.Namespace(
        spec_prefix=spec_prefix,
        project_root="",
        max_retries=3,
        timeout=30,
        no_tests=False,
        no_branch=False,
        no_commit=False,
        no_review=False,
        hitl_review=False,
        callback_url="",
        log_level=None,
    )
    return build_config(yaml_config, args)


def _handle_status(config: ExecutorConfig) -> str:
    """Get execution status summary."""
    tasks = parse_tasks(config.tasks_file) if config.tasks_file.exists() else []
    with ExecutorState(config) as state:
        completed = sum(1 for ts in state.tasks.values() if ts.status == "success")
        failed = sum(1 for ts in state.tasks.values() if ts.status == "failed")
        running = sum(1 for ts in state.tasks.values() if ts.status == "running")
        cost = state.total_cost()
        inp, out = state.total_tokens()

    return json.dumps(
        {
            "total_tasks": len(tasks),
            "completed": completed,
            "failed": failed,
            "running": running,
            "not_started": len(tasks) - completed - failed - running,
            "total_cost": round(cost, 2),
            "input_tokens": inp,
            "output_tokens": out,
            "budget_usd": config.budget_usd,
        }
    )


def _handle_tasks(config: ExecutorConfig, status: str | None = None) -> str:
    """List tasks from tasks.md."""
    tasks = parse_tasks(config.tasks_file) if config.tasks_file.exists() else []
    tasks = resolve_dependencies(tasks)
    result = []
    for t in tasks:
        if status and t.status != status:
            continue
        result.append(
            {
                "id": t.id,
                "name": t.name,
                "priority": t.priority,
                "status": t.status,
                "depends_on": t.depends_on,
            }
        )
    return json.dumps(result)


def _handle_costs(config: ExecutorConfig, sort: str = "id") -> str:
    """Per-task cost breakdown."""
    tasks = parse_tasks(config.tasks_file) if config.tasks_file.exists() else []
    rows: list[dict] = []
    with ExecutorState(config) as state:
        for t in tasks:
            ts = state.tasks.get(t.id)
            cost = state.task_cost(t.id)
            inp = sum(a.input_tokens for a in ts.attempts if a.input_tokens) if ts else 0
            out = sum(a.output_tokens for a in ts.attempts if a.output_tokens) if ts else 0
            rows.append(
                {
                    "task_id": t.id,
                    "name": t.name,
                    "status": ts.status if ts else t.status,
                    "cost": round(cost, 4),
                    "attempts": ts.attempt_count if ts else 0,
                    "input_tokens": inp,
                    "output_tokens": out,
                }
            )
        total_cost = state.total_cost()
        total_inp, total_out = state.total_tokens()

    if sort == "cost":
        rows.sort(key=lambda r: r["cost"], reverse=True)
    elif sort == "tokens":
        rows.sort(key=lambda r: r["input_tokens"] + r["output_tokens"], reverse=True)

    return json.dumps(
        {
            "tasks": rows,
            "summary": {
                "total_cost": round(total_cost, 2),
                "total_input_tokens": total_inp,
                "total_output_tokens": total_out,
                "budget_usd": config.budget_usd,
            },
        }
    )


def _handle_logs(config: ExecutorConfig, task_id: str, lines: int = 50) -> str:
    """Get last N lines of task log."""
    log_dir = config.logs_dir
    if not log_dir.exists():
        return f"No logs directory at {log_dir}"
    # Find log files matching task_id
    matching = sorted(log_dir.glob(f"{task_id}*"), reverse=True)
    if not matching:
        return f"No logs found for {task_id}"
    log_file = matching[0]
    all_lines = log_file.read_text().splitlines()
    return "\n".join(all_lines[-lines:])


# === MCP Tool Definitions ===


@mcp_app.tool()
def spec_runner_status(spec_prefix: str = "") -> str:
    """Get spec-runner execution status: tasks completed/failed/running, cost, tokens."""
    config = _build_config(spec_prefix)
    return _handle_status(config)


@mcp_app.tool()
def spec_runner_tasks(status: str = "", spec_prefix: str = "") -> str:
    """List tasks from tasks.md with id, name, priority, status, dependencies."""
    config = _build_config(spec_prefix)
    return _handle_tasks(config, status=status or None)


@mcp_app.tool()
def spec_runner_costs(sort: str = "id", spec_prefix: str = "") -> str:
    """Per-task cost breakdown with summary totals."""
    config = _build_config(spec_prefix)
    return _handle_costs(config, sort=sort)


@mcp_app.tool()
def spec_runner_logs(task_id: str, lines: int = 50, spec_prefix: str = "") -> str:
    """Get last N lines of a task's execution log."""
    config = _build_config(spec_prefix)
    return _handle_logs(config, task_id=task_id, lines=lines)


def run_server() -> None:
    """Run the MCP server (stdio transport)."""
    mcp_app.run(transport="stdio")
