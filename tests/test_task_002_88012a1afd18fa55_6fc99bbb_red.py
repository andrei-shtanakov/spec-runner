"""RED for BEH-13: override of a non-representable field refuses before `Popen`.

Source: workstreams/mcp-launch-scope-spec-runner-485-20260914/spec/15-behaviour-spec.md#BEH-13

`Given` a parent `ExecutorConfig` (the programmatic config passed to
`run_server(config)`) that overrides `review_policy` to `"required"` -- a
field with no CLI flag, so the child rebuilding its own config from the
(nonexistent, here) YAML at `project_root` would get the class default
`"advisory"` instead.
`When` `spec_runner_run_task` is invoked.
`Then` the response is `status: error` naming the field that cannot be
carried to the child, `Popen` is never called, and no state/lock/stop/ready
file appears in the namespace.

Today none of this exists: `run_task` (`mcp_server.py`) has no
reproducibility check (design §2.2, `simulate_child_config`) before
`Popen`, so it spawns the child regardless and only discovers trouble via
the unrelated ready-handshake timeout -- an error that never names
`review_policy` and is reached only after `Popen` already ran.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import spec_runner.mcp_server as server
from spec_runner.config import ExecutorConfig


def _config(tmp_path: Path) -> ExecutorConfig:
    return ExecutorConfig(
        project_root=tmp_path,
        review_policy="required",
        mcp_ready_timeout_seconds=0,
    )


def _never_ready_popen_double(pid: int = 4242):
    """A `Popen` double that never publishes the ready handshake (#485):
    the child neither exits nor writes `config.ready_file`, so the parent's
    `wait_for_ready` runs out its (zero-length, here) timeout."""

    def _popen(*args, **kwargs):
        proc = MagicMock()
        proc.pid = pid
        proc.poll.return_value = None
        return proc

    return _popen


class TestIrreproducibleConfigRefusesBeforePopen:
    def test_override_of_non_representable_field_is_named_and_blocks_popen(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path)
        captured: dict = {}

        def invoke(*, transport: str) -> None:
            assert transport == "stdio"
            captured["result"] = json.loads(server.spec_runner_run_task("TASK-001"))

        with (
            patch.object(server.mcp_app, "run", side_effect=invoke),
            patch("subprocess.Popen", side_effect=_never_ready_popen_double()) as mock_popen,
        ):
            server.run_server(config)

        result = captured["result"]

        mock_popen.assert_not_called()
        assert result["status"] == "error"
        assert "review_policy" in result["error"]
        assert not config.ready_file.exists()
        assert not config.state_file.exists()
        assert not config.stop_file.exists()
        assert not config.state_file.with_suffix(".lock").exists()
