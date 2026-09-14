"""Tests for MCP server tool handlers."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from spec_runner.config import ExecutorConfig
from spec_runner.mcp_server import spec_runner_stop
from spec_runner.state import ExecutorState, check_stop_requested


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


def _popen_double(config: ExecutorConfig, *, pid: int = 4242):
    """A `subprocess.Popen` double that completes the ready handshake (#485)
    the instant it is invoked: writes `config.ready_file` with its own pid
    and never appears to exit. Simulates a child that took its lock and
    published ready immediately -- what `TestMCPRunTask` needs now that
    `run_task` waits for the handshake before answering `started`.
    """

    def _popen(*args, **kwargs):
        proc = MagicMock()
        proc.pid = pid
        proc.poll.return_value = None
        config.ready_file.parent.mkdir(parents=True, exist_ok=True)
        config.ready_file.write_text(f"PID: {pid}\nStarted: now\n")
        return proc

    return _popen


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
        assert not config.state_file.exists()

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

    def test_empty_query_does_not_create_absent_state_db(self, tmp_path: Path) -> None:
        from spec_runner.mcp_server import _handle_costs

        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [("TASK-001", "Login", "p0", "todo")])

        result = json.loads(_handle_costs(config))

        assert result["summary"]["total_cost"] == 0.0
        assert not config.state_file.exists()

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


class TestMCPStop:
    """Tests for spec_runner_stop tool."""

    def test_stop_creates_the_marker_consumed_in_every_namespace(self, tmp_path: Path) -> None:
        configs = {
            "default": ExecutorConfig(project_root=tmp_path / "default"),
            "prefixed": ExecutorConfig(project_root=tmp_path / "prefixed", spec_prefix="phase5-"),
            "change": ExecutorConfig(project_root=tmp_path / "change", change_id="add-x"),
            "explicit-state": ExecutorConfig(
                project_root=tmp_path / "custom",
                state_file=tmp_path / "custom" / "runtime" / "custom.db",
            ),
        }

        for name, config in configs.items():
            wrong_marker = config.state_file.with_suffix(".stop")
            with patch("spec_runner.mcp_server._build_config", return_value=config):
                result = json.loads(spec_runner_stop(spec_prefix=config.spec_prefix))

            assert result == {
                "status": "stop_requested",
                "stop_file": str(config.stop_file),
            }, name
            assert check_stop_requested(config) is True, name
            if wrong_marker != config.stop_file:
                assert not wrong_marker.exists(), name

    def test_cli_passes_resolved_config_to_server(self, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from spec_runner.cli_info import cmd_mcp

        config = ExecutorConfig(project_root=tmp_path, change_id="add-x")
        with patch("spec_runner.mcp_server.run_server") as run_server:
            cmd_mcp(SimpleNamespace(), config)

        run_server.assert_called_once_with(config)

    def test_change_scoped_server_preserves_launch_stop_namespace(self, tmp_path: Path) -> None:
        import spec_runner.mcp_server as server

        config = ExecutorConfig(project_root=tmp_path, change_id="add-x")
        observed: list[dict] = []

        def invoke(*, transport: str) -> None:
            assert transport == "stdio"
            observed.append(json.loads(server.spec_runner_stop()))

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)

        assert observed == [
            {
                "status": "stop_requested",
                "stop_file": str(config.stop_file),
            }
        ]
        assert check_stop_requested(config) is True
        assert server._scope is None

    def test_explicit_prefix_keeps_launch_project_root(self, tmp_path: Path) -> None:
        import spec_runner.mcp_server as server

        project_root = tmp_path / "external-project"
        config = ExecutorConfig(project_root=project_root, spec_prefix="phase5-")
        observed: list[dict] = []

        def invoke(*, transport: str) -> None:
            assert transport == "stdio"
            observed.append(json.loads(server.spec_runner_stop(spec_prefix="phase5-")))

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)

        assert observed[0]["stop_file"] == str(project_root / "spec/.executor-stop")
        assert check_stop_requested(config) is True


class TestMCPNextTasks:
    """Tests for spec_runner_next_tasks tool."""

    def test_returns_ready_tasks(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from spec_runner.mcp_server import spec_runner_next_tasks

        config = _make_config(tmp_path)
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Done task", "p0", "done"),
                ("TASK-002", "Ready task", "p1", "todo"),
            ],
        )
        with patch("spec_runner.mcp_server._build_config", return_value=config):
            result = json.loads(spec_runner_next_tasks())
        assert len(result) == 1
        assert result[0]["id"] == "TASK-002"


class TestMCPTaskDetail:
    """Tests for spec_runner_task_detail tool."""

    def test_returns_task_detail(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from spec_runner.mcp_server import spec_runner_task_detail

        config = _make_config(tmp_path)
        _write_tasks(
            config.tasks_file,
            [("TASK-001", "Login", "p0", "done")],
        )
        _seed_state(config, {"TASK-001": [(True, 0.10, 500, 200)]})

        with patch("spec_runner.mcp_server._build_config", return_value=config):
            result = json.loads(spec_runner_task_detail("TASK-001"))
        assert result["id"] == "TASK-001"
        assert result["execution"]["cost_usd"] == 0.10
        assert result["execution"]["attempts"] == 1

    def test_task_not_found(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from spec_runner.mcp_server import spec_runner_task_detail

        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [])

        with patch("spec_runner.mcp_server._build_config", return_value=config):
            result = json.loads(spec_runner_task_detail("TASK-999"))
        assert "error" in result

    def test_empty_query_does_not_create_absent_state_db(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from spec_runner.mcp_server import spec_runner_task_detail

        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [("TASK-001", "Login", "p0", "todo")])

        with patch("spec_runner.mcp_server._build_config", return_value=config):
            result = json.loads(spec_runner_task_detail("TASK-001"))

        assert result["id"] == "TASK-001"
        assert result["execution"]["state_status"] == "pending"
        assert not config.state_file.exists()


class TestMCPRunTask:
    """Tests for spec_runner_run_task tool's spec-governance gate.

    Mirrors the CLI `run`/`watch`/`retry` gate: refuses to spawn under
    strict governance with a draft managed tasks.md, and is a no-op
    (proceeds to spawn) under default 'off' governance or an unmanaged file.
    """

    def test_strict_governance_blocks_spawn(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from spec_runner.mcp_server import spec_runner_run_task
        from spec_runner.spec import SpecMeta, write_spec

        config = _make_config(tmp_path, spec_governance="strict")
        write_spec(config.tasks_file, SpecMeta("tasks", "draft"), "# Tasks\n")

        with (
            patch("spec_runner.mcp_server._build_config", return_value=config),
            patch("subprocess.Popen") as mock_popen,
        ):
            result = json.loads(spec_runner_run_task("TASK-001"))

        mock_popen.assert_not_called()
        assert result["status"] == "error"
        assert "governance" in result["error"].lower()

    def test_off_governance_allows_spawn(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from spec_runner.mcp_server import spec_runner_run_task
        from spec_runner.spec import SpecMeta, write_spec

        config = _make_config(tmp_path, spec_governance="off")
        write_spec(config.tasks_file, SpecMeta("tasks", "draft"), "# Tasks\n")

        with (
            patch("spec_runner.mcp_server._build_config", return_value=config),
            patch("subprocess.Popen", side_effect=_popen_double(config, pid=4242)) as mock_popen,
        ):
            result = json.loads(spec_runner_run_task("TASK-001"))

        mock_popen.assert_called_once()
        assert result["status"] == "started"
        assert result["pid"] == 4242

    def test_unmanaged_tasks_file_allows_spawn(self, tmp_path: Path) -> None:
        from unittest.mock import patch

        from spec_runner.mcp_server import spec_runner_run_task

        config = _make_config(tmp_path, spec_governance="strict")
        _write_tasks(config.tasks_file, [("TASK-001", "Task", "p0", "todo")])

        with (
            patch("spec_runner.mcp_server._build_config", return_value=config),
            patch("subprocess.Popen", side_effect=_popen_double(config, pid=4242)) as mock_popen,
        ):
            result = json.loads(spec_runner_run_task("TASK-001"))

        mock_popen.assert_called_once()
        assert result["status"] == "started"
        assert result["pid"] == 4242
