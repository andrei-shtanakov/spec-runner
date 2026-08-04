"""Wire-level smoke: the MCP server serves its tools under SDK v2."""

import pytest
from mcp import Client

pytestmark = pytest.mark.anyio


async def test_mcp_server_lists_all_tools_in_memory() -> None:
    from spec_runner.mcp_server import mcp_app

    async with Client(mcp_app) as client:
        result = await client.list_tools()
        names = {t.name for t in result.tools}
    assert {
        "spec_runner_status",
        "spec_runner_tasks",
        "spec_runner_costs",
        "spec_runner_logs",
        "spec_runner_run_task",
        "spec_runner_stop",
        "spec_runner_next_tasks",
        "spec_runner_task_detail",
    } <= names
