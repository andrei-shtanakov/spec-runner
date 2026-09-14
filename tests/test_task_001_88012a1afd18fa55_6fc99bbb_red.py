"""RED for BEH-04: no MCP tool rebuilds config from CWD (M-02: 7/8 -> 0/8).

Source: workstreams/mcp-launch-scope-spec-runner-485-20260914/spec/15-behaviour-spec.md#BEH-04

`Given` a server with a filled launch-scope holder (`run_server(config)`) and
`_build_config` -- the function that rebuilds config from CWD -- replaced by a
double that raises on every call.
`When` all eight tools are invoked with an empty tool-level `spec_prefix`.
`Then` none of them raises the double's exception: every tool is meant to
route through a single holder helper instead of rebuilding config from CWD.

Today only `spec_runner_stop` reads the holder (`_launch_stop_config`); the
other seven tools (`status`, `tasks`, `costs`, `logs`, `next_tasks`,
`task_detail`, `run_task`) call `_build_config(spec_prefix)` directly (see
`mcp_server.py`), so this test fails for seven of the eight tool calls.
"""

from pathlib import Path
from unittest.mock import patch

import spec_runner.mcp_server as server
from spec_runner.config import ExecutorConfig


def _config(tmp_path: Path) -> ExecutorConfig:
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    return ExecutorConfig(
        project_root=tmp_path,
        state_file=spec_dir / ".executor-state.db",
        logs_dir=spec_dir / ".executor-logs",
    )


class TestNoToolRebuildsConfigFromCwd:
    def test_all_eight_tools_survive_a_build_config_double_that_raises(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)

        def exploding_build_config(spec_prefix: str = "") -> ExecutorConfig:
            raise AssertionError(
                "tool rebuilt config from CWD instead of using the launch scope holder"
            )

        calls = [
            ("status", lambda: server.spec_runner_status()),
            ("tasks", lambda: server.spec_runner_tasks()),
            ("costs", lambda: server.spec_runner_costs()),
            ("logs", lambda: server.spec_runner_logs(task_id="TASK-001")),
            ("next_tasks", lambda: server.spec_runner_next_tasks()),
            (
                "task_detail",
                lambda: server.spec_runner_task_detail(task_id="TASK-001"),
            ),
            ("run_task", lambda: server.spec_runner_run_task(task_id="TASK-001")),
            ("stop", lambda: server.spec_runner_stop()),
        ]

        failures: list[str] = []

        def invoke(*, transport: str) -> None:
            assert transport == "stdio"
            with patch.object(server, "_build_config", side_effect=exploding_build_config):
                for name, call in calls:
                    try:
                        call()
                    except AssertionError:
                        failures.append(name)

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)

        assert failures == [], (
            f"tools still rebuild config from CWD instead of the launch scope holder: {failures}"
        )
