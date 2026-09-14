"""MCP server for spec-runner -- exposes status, tasks, costs, logs, and execution tools.

Security: the stdio transport inherits the trust boundary of the process that
launched it (typically a developer's terminal or Claude Code). There is no
built-in authentication. Write tools (`run_task`, `stop`) spawn subprocesses
with full filesystem access. See README.md#security-model for deployment
guidance.
"""

import json

from mcp.server import MCPServer

from . import mcp_launch
from .config import ExecutorConfig, build_config, load_config_from_yaml
from .mcp_launch import LaunchScope, ScopeContradiction
from .state import ExecutorState
from .task import parse_tasks, resolve_dependencies

mcp_app = MCPServer("spec-runner")
# The one holder for the server's launch scope (#485 §1.2). Empty only when
# tools are invoked without `run_server` -- exercised by tests only; see
# `_tool_config`.
_scope: LaunchScope | None = None


def _build_config(spec_prefix: str = "") -> ExecutorConfig:
    """Build ExecutorConfig from YAML (by CWD) + optional spec_prefix.

    Used only for the flat/no-launch-scope case: `run_server(None)` and the
    holder-empty fallback in `_tool_config` (#485 §1.2/§1.3) -- once a server
    has a launch scope, no tool calls this (M-02: 0/8, BEH-04).
    """
    import argparse

    yaml_config = load_config_from_yaml()
    args = argparse.Namespace(
        spec_prefix=spec_prefix,
        project_root="",
        max_retries=None,
        timeout=None,
        no_tests=False,
        no_branch=False,
        no_commit=False,
        no_review=False,
        hitl_review=False,
        callback_url="",
        log_level=None,
        budget=None,
        task_budget=None,
    )
    return build_config(yaml_config, args)


def _tool_config(spec_prefix: str = "") -> ExecutorConfig:
    """The one helper all eight tools resolve their config through (§1.3).

    Raises `ScopeContradiction` when `spec_prefix` conflicts with the launch
    namespace (BEH-05/06); callers turn that into the tool's JSON error.
    """
    scope = _scope if _scope is not None else LaunchScope.of(_build_config(""))
    return mcp_launch.resolve_tool_config(scope, spec_prefix)


def _handle_status(config: ExecutorConfig) -> str:
    """Get execution status summary."""
    tasks = parse_tasks(config.tasks_file) if config.tasks_file.exists() else []
    with ExecutorState.for_read(config) as state:
        completed = sum(1 for ts in state.tasks.values() if ts.status == "success")
        failed = sum(1 for ts in state.tasks.values() if ts.status == "failed")
        running = sum(1 for ts in state.tasks.values() if ts.status == "running")
        cost = state.total_cost()
        inp, out = state.total_tokens()

    scope = LaunchScope.of(config)
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
            "project_root": str(scope.project_root),
            "namespace": scope.namespace,
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
    with ExecutorState.for_read(config) as state:
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
    try:
        config = _tool_config(spec_prefix)
    except ScopeContradiction as exc:
        return json.dumps(exc.to_dict())
    return _handle_status(config)


@mcp_app.tool()
def spec_runner_tasks(status: str = "", spec_prefix: str = "") -> str:
    """List tasks from tasks.md with id, name, priority, status, dependencies."""
    try:
        config = _tool_config(spec_prefix)
    except ScopeContradiction as exc:
        return json.dumps(exc.to_dict())
    return _handle_tasks(config, status=status or None)


@mcp_app.tool()
def spec_runner_costs(sort: str = "id", spec_prefix: str = "") -> str:
    """Per-task cost breakdown with summary totals."""
    try:
        config = _tool_config(spec_prefix)
    except ScopeContradiction as exc:
        return json.dumps(exc.to_dict())
    return _handle_costs(config, sort=sort)


@mcp_app.tool()
def spec_runner_logs(task_id: str, lines: int = 50, spec_prefix: str = "") -> str:
    """Get last N lines of a task's execution log."""
    try:
        config = _tool_config(spec_prefix)
    except ScopeContradiction as exc:
        return json.dumps(exc.to_dict())
    return _handle_logs(config, task_id=task_id, lines=lines)


@mcp_app.tool()
def spec_runner_run_task(task_id: str, spec_prefix: str = "") -> str:
    """Start execution of a specific task. Returns immediately with status.

    WRITE tool. Spawns `spec-runner run --task {task_id}` as a subprocess,
    which runs the Claude CLI with full filesystem access to the workspace:
    the task can edit files, create git branches, run hooks (tests/lint),
    auto-commit, and spend API budget. Do not expose this MCP server over
    the network — it has no authentication. See README.md#security-model.

    Under `spec_governance: strict`, refuses to start when the managed
    tasks.md is not approved (mirrors the CLI's `run`/`watch`/`retry` gate).
    No-ops (always allows) under default `off` governance and for unmanaged
    (frontmatter-less) tasks.md files.

    `started` means the child has taken its run lock and published ready
    (#485) -- not merely that `Popen` returned. A busy lock, an early exit,
    or a child that never publishes ready all come back as `status: error`
    instead (see README.md#mcp-server).
    """
    import subprocess
    from datetime import datetime

    from .cli import spec_run_gate_ok

    try:
        config = _tool_config(spec_prefix)
    except ScopeContradiction as exc:
        return json.dumps(exc.to_dict())

    allowed, reason = spec_run_gate_ok(config)
    if not allowed:
        return json.dumps({"status": "error", "error": f"⛔ spec governance: {reason}"})

    reproducibility_scope = LaunchScope.of(config)
    argv = mcp_launch.child_argv(config, task_id)
    simulated = mcp_launch.simulate_child_config(reproducibility_scope, argv)
    if isinstance(simulated, mcp_launch.Irreproducible):
        return json.dumps(simulated.to_dict())

    # The SAME argv the simulation just validated (mcp_launch.child_argv):
    # scope, namespace and every representable override travel to the child
    # (FR-03). Building a second, shorter command here made the check
    # fail-open (review of the DT-02 integration PR). `child_entry()` is the
    # interpreter-bound prefix (BEH-11): the parent's own venv, never a
    # `spec-runner` console script that PATH might resolve elsewhere or not
    # at all.
    cmd = [*mcp_launch.child_entry(), *argv]

    mcp_launch.clear_stale_ready_file(config.ready_file)

    config.logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = config.logs_dir / f"{task_id}-{datetime.now():%Y%m%d-%H%M%S}.log"

    try:
        with open(log_file, "wb") as log_fh:
            proc = subprocess.Popen(
                cmd,
                cwd=config.project_root,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
            )
    except Exception as e:
        return json.dumps({"status": "error", "error": str(e)})

    outcome = mcp_launch.wait_for_ready(
        proc, config.ready_file, config.mcp_ready_timeout_seconds, log_file=log_file
    )

    if isinstance(outcome, mcp_launch.Ready):
        return json.dumps(
            {
                "status": "started",
                "task_id": task_id,
                "pid": outcome.pid,
                "lock_file": str(config.state_file.with_suffix(".lock")),
                "log_file": str(log_file),
            }
        )
    if isinstance(outcome, mcp_launch.Exited):
        return json.dumps(
            {
                "status": "error",
                "error": (f"child exited with code {outcome.returncode} before publishing ready"),
                "exit_code": outcome.returncode,
                "log_tail": outcome.log_tail,
                "log_file": str(log_file),
            }
        )
    return json.dumps(
        {
            "status": "error",
            "error": (
                f"timeout waiting {config.mcp_ready_timeout_seconds}s "
                "for the child to publish ready"
            ),
            "pid": outcome.pid,
            "terminated": outcome.terminated,
            "log_file": str(log_file),
        }
    )


@mcp_app.tool()
def spec_runner_stop(spec_prefix: str = "") -> str:
    """Request graceful shutdown of running execution.

    WRITE tool. Writes a stop-file that asks any running executor on the
    same workspace to finish the current task and exit. Does not kill
    processes. See README.md#security-model.
    """
    try:
        config = _tool_config(spec_prefix)
    except ScopeContradiction as exc:
        return json.dumps(exc.to_dict())
    stop_file = config.stop_file
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text("stop")
    return json.dumps({"status": "stop_requested", "stop_file": str(stop_file)})


@mcp_app.tool()
def spec_runner_next_tasks(spec_prefix: str = "") -> str:
    """Get list of tasks ready to execute (resolved dependencies, TODO status)."""
    from .task import get_next_tasks

    try:
        config = _tool_config(spec_prefix)
    except ScopeContradiction as exc:
        return json.dumps(exc.to_dict())
    tasks = parse_tasks(config.tasks_file) if config.tasks_file.exists() else []
    ready = get_next_tasks(tasks)
    return json.dumps([{"id": t.id, "name": t.name, "priority": t.priority} for t in ready])


@mcp_app.tool()
def spec_runner_task_detail(task_id: str, spec_prefix: str = "") -> str:
    """Get full detail for a task: checklist, attempts, review verdicts, cost."""
    try:
        config = _tool_config(spec_prefix)
    except ScopeContradiction as exc:
        return json.dumps(exc.to_dict())
    tasks = parse_tasks(config.tasks_file) if config.tasks_file.exists() else []
    task = next((t for t in tasks if t.id == task_id.upper()), None)
    if not task:
        return json.dumps({"error": f"Task {task_id} not found"})

    scope = LaunchScope.of(config)
    detail: dict = {
        "id": task.id,
        "name": task.name,
        "priority": task.priority,
        "status": task.status,
        "depends_on": task.depends_on,
        "traces_to": task.traces_to,
        "checklist": [{"done": done, "text": text} for text, done in task.checklist],
        "project_root": str(scope.project_root),
        "namespace": scope.namespace,
    }

    with ExecutorState.for_read(config) as state:
        ts = state.get_task_state(task.id)
        if ts:
            detail["execution"] = {
                "state_status": ts.status,
                "attempts": ts.attempt_count,
                "cost_usd": round(state.task_cost(task.id), 2),
                "last_error": ts.last_error,
            }
            if ts.attempts:
                last = ts.attempts[-1]
                detail["execution"]["last_review"] = last.review_status
                detail["execution"]["last_duration"] = last.duration_seconds

    return json.dumps(detail)


def run_server(config: ExecutorConfig | None = None) -> None:
    """Run the MCP server (stdio transport).

    `config` becomes the launch scope every tool serves (#485). `None` --
    the programmatic `mcp_run_server()` entry point and the flat
    `spec-runner mcp` with no `--project-root`/namespace flags -- builds one
    from CWD via `_build_config("")`, same as before (BEH-23).
    """
    global _scope

    previous = _scope
    _scope = LaunchScope.of(config) if config is not None else LaunchScope.of(_build_config(""))
    try:
        mcp_app.run(transport="stdio")
    finally:
        _scope = previous
