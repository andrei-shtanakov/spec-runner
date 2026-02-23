"""Tests for MCP server tool handlers."""

import json
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState


def _make_config(tmp_path: Path, **overrides) -> ExecutorConfig:
    """Create minimal config for MCP tests."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": spec_dir / ".executor-state.db",
        "logs_dir": spec_dir / ".executor-logs",
        "budget_usd": 5.0,
        "max_retries": 3,
        "max_consecutive_failures": 2,
        "retry_delay_seconds": 0,
        "create_git_branch": False,
        "run_tests_on_done": False,
        "auto_commit": False,
        "run_review": False,
        "callback_url": "",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _write_tasks(tasks_file: Path, tasks: list[tuple[str, str, str, str]]) -> None:
    """Write tasks.md from (id, name, priority, status) tuples."""
    priority_emoji = {
        "p0": "\U0001f534",
        "p1": "\U0001f7e0",
        "p2": "\U0001f7e1",
        "p3": "\U0001f7e2",
    }
    status_emoji = {
        "todo": "\u2b1c",
        "in_progress": "\U0001f504",
        "done": "\u2705",
        "blocked": "\u23f8\ufe0f",
    }
    lines = ["# Tasks\n"]
    for tid, name, prio, status in tasks:
        p = priority_emoji.get(prio, "\U0001f534")
        s = status_emoji.get(status, "\u2b1c")
        lines.append(f"### {tid}: {name}")
        lines.append(f"{p} {prio.upper()} | {s} {status.upper()} | Est: 1d")
        lines.append("")
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text("\n".join(lines))


def _seed_state(config: ExecutorConfig, task_data: dict) -> None:
    """Populate state.db with attempts."""
    with ExecutorState(config) as state:
        for task_id, attempts in task_data.items():
            for success, cost, inp, out in attempts:
                state.record_attempt(
                    task_id,
                    success=success,
                    duration=10.0,
                    error=None if success else "test error",
                    input_tokens=inp,
                    output_tokens=out,
                    cost_usd=cost,
                )


class TestMCPStatus:
    """Tests for spec_runner_status handler."""

    def test_status_returns_json(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_status

        config = _make_config(tmp_path)
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Login", "p0", "done"),
                ("TASK-002", "Signup", "p1", "todo"),
            ],
        )
        _seed_state(config, {"TASK-001": [(True, 0.50, 1000, 500)]})
        result = json.loads(_handle_status(config))
        assert result["total_tasks"] == 2
        assert result["completed"] == 1
        assert result["total_cost"] == 0.50

    def test_status_empty_state(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_status

        config = _make_config(tmp_path)
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Login", "p0", "todo"),
            ],
        )
        result = json.loads(_handle_status(config))
        assert result["total_tasks"] == 1
        assert result["completed"] == 0
        assert result["total_cost"] == 0.0

    def test_status_no_tasks_file(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_status

        config = _make_config(tmp_path)
        result = json.loads(_handle_status(config))
        assert result["total_tasks"] == 0


class TestMCPTasks:
    """Tests for spec_runner_tasks handler."""

    def test_tasks_returns_list(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_tasks

        config = _make_config(tmp_path)
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Login", "p0", "done"),
                ("TASK-002", "Signup", "p1", "todo"),
            ],
        )
        result = json.loads(_handle_tasks(config))
        assert len(result) == 2
        assert result[0]["id"] == "TASK-001"
        assert result[0]["priority"] == "p0"

    def test_tasks_filter_by_status(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_tasks

        config = _make_config(tmp_path)
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Login", "p0", "done"),
                ("TASK-002", "Signup", "p1", "todo"),
            ],
        )
        result = json.loads(_handle_tasks(config, status="todo"))
        assert len(result) == 1
        assert result[0]["id"] == "TASK-002"

    def test_tasks_no_file(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_tasks

        config = _make_config(tmp_path)
        result = json.loads(_handle_tasks(config))
        assert result == []


class TestMCPCosts:
    """Tests for spec_runner_costs handler."""

    def test_costs_returns_breakdown(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_costs

        config = _make_config(tmp_path)
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Login", "p0", "done"),
            ],
        )
        _seed_state(config, {"TASK-001": [(True, 0.45, 12500, 3200)]})
        result = json.loads(_handle_costs(config))
        assert "tasks" in result
        assert "summary" in result
        assert result["summary"]["total_cost"] == 0.45
        assert len(result["tasks"]) == 1
        assert result["tasks"][0]["task_id"] == "TASK-001"

    def test_costs_sort_by_cost(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_costs

        config = _make_config(tmp_path)
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Login", "p0", "done"),
                ("TASK-002", "Signup", "p1", "done"),
            ],
        )
        _seed_state(
            config,
            {
                "TASK-001": [(True, 0.20, 5000, 1000)],
                "TASK-002": [(True, 0.80, 20000, 5000)],
            },
        )
        result = json.loads(_handle_costs(config, sort="cost"))
        assert result["tasks"][0]["task_id"] == "TASK-002"  # Most expensive first


class TestMCPLogs:
    """Tests for spec_runner_logs handler."""

    def test_logs_returns_text(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_logs

        config = _make_config(tmp_path)
        log_dir = config.logs_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "TASK-001-20260101-120000.log"
        log_file.write_text("line 1\nline 2\nline 3\n")
        result = _handle_logs(config, task_id="TASK-001", lines=2)
        assert "line 2" in result
        assert "line 3" in result

    def test_logs_no_dir(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_logs

        config = _make_config(tmp_path)
        result = _handle_logs(config, task_id="TASK-001")
        assert "No logs" in result

    def test_logs_no_matching_files(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_logs

        config = _make_config(tmp_path)
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        result = _handle_logs(config, task_id="TASK-999")
        assert "No logs" in result
