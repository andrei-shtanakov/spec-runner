"""MCP launch scope: holder, contradiction predicate, YAML by project_root,
parent-side handshake (spec-runner#485, DT-01).

Covers BEH-01, BEH-02, BEH-03, BEH-05, BEH-06, BEH-07, BEH-18, BEH-20,
BEH-21, BEH-22, BEH-25 from
workstreams/mcp-launch-scope-spec-runner-485-20260914/spec/15-behaviour-spec.md.
BEH-04 has its own RED
(tests/test_task_001_88012a1afd18fa55_6fc99bbb_red.py); BEH-28's changes live
in tests/test_mcp.py, which owns the pre-existing suite.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import spec_runner.mcp_server as server
from spec_runner.cli import _build_parser
from spec_runner.config import (
    ExecutorConfig,
    _resolve_config_path,
    build_config,
    load_config_from_yaml,
)
from spec_runner.mcp_server import (
    spec_runner_costs,
    spec_runner_logs,
    spec_runner_next_tasks,
    spec_runner_run_task,
    spec_runner_status,
    spec_runner_stop,
    spec_runner_task_detail,
    spec_runner_tasks,
)
from spec_runner.spec import SpecMeta, write_spec


def _config(tmp_path: Path, **overrides) -> ExecutorConfig:
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": spec_dir / ".executor-state.db",
        "logs_dir": spec_dir / ".executor-logs",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _write_tasks(tasks_file: Path, tasks: list[tuple[str, str, str, str]]) -> None:
    priority_emoji = {
        "p0": "\U0001f534",
        "p1": "\U0001f7e0",
        "p2": "\U0001f7e1",
        "p3": "\U0001f7e2",
    }
    status_emoji = {
        "todo": "⬜",
        "in_progress": "\U0001f504",
        "done": "✅",
        "blocked": "⏸️",
    }
    lines = ["# Tasks\n"]
    for tid, name, prio, status in tasks:
        p = priority_emoji.get(prio, "\U0001f534")
        s = status_emoji.get(status, "⬜")
        lines.append(f"### {tid}: {name}")
        lines.append(f"{p} {prio.upper()} | {s} {status.upper()} | Est: 1d")
        lines.append("")
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text("\n".join(lines))


def _popen_double(config: ExecutorConfig, *, pid: int = 4242):
    """A `subprocess.Popen` double that publishes ready the instant it runs."""

    def _popen(*args, **kwargs):
        proc = MagicMock()
        proc.pid = pid
        proc.poll.return_value = None
        config.ready_file.parent.mkdir(parents=True, exist_ok=True)
        config.ready_file.write_text(f"PID: {pid}\nStarted: now\n")
        return proc

    return _popen


class TestBEH01AllToolsServeLaunchScope:
    def test_read_tools_and_stop_serve_the_change_scope_not_server_cwd(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        external = tmp_path / "external"
        _write_tasks(
            external / "spec" / "changes" / "add-x" / "tasks.md",
            [
                ("TASK-001", "Change task 1", "p0", "todo"),
                ("TASK-002", "Change task 2", "p1", "todo"),
            ],
        )
        _write_tasks(external / "spec" / "tasks.md", [("TASK-999", "External flat", "p0", "todo")])

        cwd_dir = tmp_path / "cwd-project"
        _write_tasks(
            cwd_dir / "spec" / "tasks.md", [("TASK-777", "CWD project task", "p0", "todo")]
        )
        monkeypatch.chdir(cwd_dir)

        config = ExecutorConfig(project_root=external, change_id="add-x")
        observed: dict = {}

        def invoke(*, transport: str) -> None:
            observed["status"] = json.loads(spec_runner_status())
            observed["tasks"] = json.loads(spec_runner_tasks())
            observed["next_tasks"] = json.loads(spec_runner_next_tasks())
            observed["task_detail"] = json.loads(spec_runner_task_detail("TASK-001"))
            observed["costs"] = json.loads(spec_runner_costs())
            observed["logs"] = spec_runner_logs(task_id="TASK-001")
            observed["stop"] = json.loads(spec_runner_stop())

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)

        assert observed["status"]["total_tasks"] == 2
        assert {t["id"] for t in observed["tasks"]} == {"TASK-001", "TASK-002"}
        assert {t["id"] for t in observed["next_tasks"]} == {"TASK-001", "TASK-002"}
        assert observed["task_detail"]["id"] == "TASK-001"
        assert len(observed["costs"]["tasks"]) == 2
        assert "No logs" in observed["logs"]  # no run happened; just proves no crash on the scope
        assert observed["stop"]["stop_file"] == str(
            external / "spec" / "changes" / "add-x" / ".executor-stop"
        )
        assert (external / "spec" / "changes" / "add-x" / ".executor-stop").exists()
        assert not (external / "spec" / ".executor-stop").exists()
        assert not (cwd_dir / "spec" / ".executor-stop").exists()


class TestBEH02YamlByProjectRoot:
    def _resolved_config(self, argv: list[str]) -> ExecutorConfig:
        args = _build_parser().parse_args(argv)
        config_path = _resolve_config_path(Path(args.project_root) if args.project_root else None)
        yaml_config = load_config_from_yaml(config_path)
        return build_config(yaml_config, args)

    def test_governance_gate_follows_project_root_yaml_not_cwd(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        external = tmp_path / "external"
        write_spec(
            external / "spec" / "changes" / "add-x" / "tasks.md",
            SpecMeta("tasks", "draft"),
            "# Tasks\n",
        )
        (external / "spec-runner.config.yaml").write_text("spec_governance: strict\n")

        cwd_dir = tmp_path / "cwd-project"
        cwd_dir.mkdir(parents=True)
        (cwd_dir / "spec-runner.config.yaml").write_text("spec_governance: off\n")
        monkeypatch.chdir(cwd_dir)

        config = self._resolved_config(
            ["run", "--project-root", str(external), "--change", "add-x"]
        )
        assert config.spec_governance == "strict"

        with patch("subprocess.Popen") as mock_popen:

            def invoke(*, transport: str) -> None:
                result = json.loads(spec_runner_run_task("TASK-001"))
                assert result["status"] == "error"
                assert "governance" in result["error"].lower()

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)

        mock_popen.assert_not_called()

    def test_legacy_config_location_also_resolves_by_project_root(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        external = tmp_path / "external"
        (external / "spec").mkdir(parents=True)
        (external / "spec" / "executor.config.yaml").write_text("spec_governance: strict\n")
        write_spec(external / "spec" / "tasks.md", SpecMeta("tasks", "draft"), "# Tasks\n")

        (tmp_path / "elsewhere").mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(tmp_path / "elsewhere")

        config = self._resolved_config(["run", "--project-root", str(external)])
        assert config.spec_governance == "strict"

    def test_mirrored_case_external_off_cwd_strict_still_launches(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        external = tmp_path / "external"
        write_spec(
            external / "spec" / "changes" / "add-x" / "tasks.md",
            SpecMeta("tasks", "draft"),
            "# Tasks\n",
        )
        (external / "spec-runner.config.yaml").write_text('spec_governance: "off"\n')

        cwd_dir = tmp_path / "cwd-project"
        cwd_dir.mkdir(parents=True)
        (cwd_dir / "spec-runner.config.yaml").write_text("spec_governance: strict\n")
        monkeypatch.chdir(cwd_dir)

        config = self._resolved_config(
            ["run", "--project-root", str(external), "--change", "add-x"]
        )
        assert config.spec_governance == "off"

        with patch("subprocess.Popen", side_effect=_popen_double(config, pid=111)) as mock_popen:

            def invoke(*, transport: str) -> None:
                result = json.loads(spec_runner_run_task("TASK-001"))
                assert result["status"] == "started"

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)

        mock_popen.assert_called_once()


class TestBEH03ReadToolsLeaveNoTraces:
    def test_six_read_tools_create_no_executor_files(self, tmp_path: Path) -> None:
        external = tmp_path / "external"
        _write_tasks(
            external / "spec" / "changes" / "add-x" / "tasks.md",
            [("TASK-001", "T", "p0", "todo")],
        )
        config = ExecutorConfig(project_root=external, change_id="add-x")

        def invoke(*, transport: str) -> None:
            spec_runner_status()
            spec_runner_tasks()
            spec_runner_next_tasks()
            spec_runner_task_detail("TASK-001")
            spec_runner_task_detail("TASK-999")
            spec_runner_costs()
            spec_runner_logs(task_id="TASK-001")
            spec_runner_logs(task_id="TASK-999")

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)

        assert list((external / "spec").rglob(".executor-*")) == []


class TestBEH05And06ContradictingPrefix:
    def test_run_task_with_conflicting_prefix_names_both_sides(self, tmp_path: Path) -> None:
        external = tmp_path / "external"
        _write_tasks(
            external / "spec" / "changes" / "add-x" / "tasks.md",
            [("TASK-001", "T", "p0", "todo")],
        )
        config = ExecutorConfig(project_root=external, change_id="add-x")

        with patch("subprocess.Popen") as mock_popen:

            def invoke(*, transport: str) -> None:
                result = json.loads(spec_runner_run_task("TASK-001", spec_prefix="other-"))
                assert result["status"] == "error"
                assert "add-x" in result["error"]
                assert "other-" in result["error"]
                assert result["launch_namespace"] == {"kind": "change", "id": "add-x"}
                assert result["requested_spec_prefix"] == "other-"

                status = json.loads(spec_runner_status())
                assert status["namespace"] == {"kind": "change", "id": "add-x"}

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)

        mock_popen.assert_not_called()

    def test_spec_prefix_launch_also_refuses_a_different_prefix(self, tmp_path: Path) -> None:
        config = _config(tmp_path, spec_prefix="p-")
        _write_tasks(config.tasks_file, [("TASK-001", "T", "p0", "todo")])

        with patch("subprocess.Popen") as mock_popen:

            def invoke(*, transport: str) -> None:
                result = json.loads(spec_runner_run_task("TASK-001", spec_prefix="other-"))
                assert result["status"] == "error"
                assert "p-" in result["error"]
                assert "other-" in result["error"]

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)

        mock_popen.assert_not_called()

    def test_predicate_is_shared_by_the_other_seven_tools(self, tmp_path: Path) -> None:
        external = tmp_path / "external"
        _write_tasks(
            external / "spec" / "changes" / "add-x" / "tasks.md",
            [("TASK-001", "T", "p0", "todo")],
        )
        config = ExecutorConfig(project_root=external, change_id="add-x")

        results: list[dict] = []

        def invoke(*, transport: str) -> None:
            results.append(json.loads(spec_runner_status(spec_prefix="other-")))
            results.append(json.loads(spec_runner_tasks(spec_prefix="other-")))
            results.append(json.loads(spec_runner_costs(spec_prefix="other-")))
            results.append(json.loads(spec_runner_logs(task_id="TASK-001", spec_prefix="other-")))
            results.append(json.loads(spec_runner_next_tasks(spec_prefix="other-")))
            results.append(json.loads(spec_runner_task_detail("TASK-001", spec_prefix="other-")))
            results.append(json.loads(spec_runner_stop(spec_prefix="other-")))

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)

        assert len(results) == 7
        for result in results:
            assert result["status"] == "error"
            assert result["launch_namespace"] == {"kind": "change", "id": "add-x"}
            assert result["requested_spec_prefix"] == "other-"

        assert not (external / "spec" / "changes" / "other-").exists()
        assert not config.stop_file.exists()
        assert not (external / "spec" / "changes" / "add-x" / ".executor-stop").exists()


class TestBEH07MatchingEmptyAndRefiningPrefix:
    def test_matching_prefix_on_a_prefixed_server_succeeds(self, tmp_path: Path) -> None:
        config = _config(tmp_path, spec_prefix="p-")
        _write_tasks(config.tasks_file, [("TASK-001", "T", "p0", "todo")])

        def invoke(*, transport: str) -> None:
            result = json.loads(spec_runner_status(spec_prefix="p-"))
            assert result["namespace"] == {"kind": "prefix", "prefix": "p-"}
            assert result["total_tasks"] == 1

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)

    def test_empty_prefix_is_the_default_on_any_server(self, tmp_path: Path) -> None:
        for suffix, config in (
            (1, _config(tmp_path / "a", spec_prefix="p-")),
            (2, ExecutorConfig(project_root=tmp_path / "b", change_id="add-x")),
        ):
            _write_tasks(config.tasks_file, [(f"TASK-{suffix:03d}", "T", "p0", "todo")])

            def invoke(*, transport: str) -> None:
                result = json.loads(spec_runner_status(spec_prefix=""))
                assert result["total_tasks"] == 1

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)

    def test_flat_launch_with_a_prefix_refines_scope_project_root_unchanged(
        self, tmp_path: Path
    ) -> None:
        external = tmp_path / "external"
        _write_tasks(external / "spec" / "p-tasks.md", [("TASK-001", "T", "p0", "todo")])
        config = ExecutorConfig(project_root=external)

        def invoke(*, transport: str) -> None:
            result = json.loads(spec_runner_status(spec_prefix="p-"))
            assert result["total_tasks"] == 1
            assert result["project_root"] == str(external.resolve())
            assert result["namespace"] == {"kind": "prefix", "prefix": "p-"}

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)


class TestBEH18ReadyTimeoutConfigurable:
    def test_overridden_small_timeout_expires_before_the_default_would(
        self, tmp_path: Path
    ) -> None:
        config = _config(tmp_path, mcp_ready_timeout_seconds=0.2)
        _write_tasks(config.tasks_file, [("TASK-001", "T", "p0", "todo")])

        def hanging_popen(*args, **kwargs):
            proc = MagicMock()
            proc.pid = 999
            proc.poll.return_value = None
            return proc

        with patch("subprocess.Popen", side_effect=hanging_popen):

            def invoke(*, transport: str) -> None:
                start = time.monotonic()
                result = json.loads(spec_runner_run_task("TASK-001"))
                elapsed = time.monotonic() - start
                assert result["status"] == "error"
                assert "timeout" in result["error"].lower()
                assert elapsed < 5.0

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)

    def test_default_timeout_does_not_block_a_fast_handshake(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        _write_tasks(config.tasks_file, [("TASK-001", "T", "p0", "todo")])

        with patch("subprocess.Popen", side_effect=_popen_double(config, pid=555)):

            def invoke(*, transport: str) -> None:
                result = json.loads(spec_runner_run_task("TASK-001"))
                assert result["status"] == "started"

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)


class TestBEH20BusyLock:
    def test_busy_lock_is_a_startup_error_second_executor_not_started(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        _write_tasks(config.tasks_file, [("TASK-001", "T", "p0", "todo")])

        lock_file = config.state_file.with_suffix(".lock")
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        holder_pid = os.getpid()
        lock_file.write_text(f"PID: {holder_pid}\nStarted: now\n")

        def dying_popen(*args, **kwargs):
            stdout = kwargs.get("stdout")
            if stdout is not None:
                stdout.write(
                    f"Another executor is already running lock_file={lock_file}\n".encode()
                )
                stdout.flush()
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = 1
            return proc

        with patch("subprocess.Popen", side_effect=dying_popen):

            def invoke(*, transport: str) -> None:
                result = json.loads(spec_runner_run_task("TASK-001"))
                assert result["status"] == "error"
                assert result["exit_code"] == 1
                assert "lock" in result["log_tail"].lower()
                assert str(lock_file) in result["log_tail"]

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)

        assert lock_file.read_text().splitlines()[0] == f"PID: {holder_pid}"
        assert not config.ready_file.exists()


class TestBEH21ChildDiesBeforeReady:
    def test_exit_code_and_log_tail_reach_the_response(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        _write_tasks(config.tasks_file, [("TASK-001", "T", "p0", "todo")])

        def dying_popen(*args, **kwargs):
            stdout = kwargs.get("stdout")
            if stdout is not None:
                stdout.write(b"ConfigError: something is wrong\n")
                stdout.flush()
            proc = MagicMock()
            proc.pid = 777
            proc.poll.return_value = 1
            return proc

        with patch("subprocess.Popen", side_effect=dying_popen):

            def invoke(*, transport: str) -> None:
                result = json.loads(spec_runner_run_task("TASK-001"))
                assert result["status"] == "error"
                assert result["exit_code"] == 1
                assert "ConfigError" in result["log_tail"]
                assert "log_file" in result

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)

        assert not config.ready_file.exists()


class TestBEH22ChildNeverPublishesReady:
    def test_timeout_response_and_no_leftover_ready_file(self, tmp_path: Path) -> None:
        config = _config(tmp_path, mcp_ready_timeout_seconds=0.2)
        _write_tasks(config.tasks_file, [("TASK-001", "T", "p0", "todo")])

        def hanging_popen(*args, **kwargs):
            proc = MagicMock()
            proc.pid = 4321
            proc.poll.return_value = None
            return proc

        with patch("subprocess.Popen", side_effect=hanging_popen):

            def invoke(*, transport: str) -> None:
                result = json.loads(spec_runner_run_task("TASK-001"))
                assert result["status"] == "error"
                assert "timeout" in result["error"].lower()
                assert result["terminated"] is True
                assert result["pid"] == 4321

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)

        assert not config.ready_file.exists()


class TestBEH25StatusAndTaskDetailNameTheScope:
    def test_project_root_and_namespace_for_three_launch_forms(self, tmp_path: Path) -> None:
        cases = [
            (
                ExecutorConfig(project_root=tmp_path / "change", change_id="add-x"),
                {"kind": "change", "id": "add-x"},
            ),
            (
                _config(tmp_path / "prefix", spec_prefix="p-"),
                {"kind": "prefix", "prefix": "p-"},
            ),
            (ExecutorConfig(project_root=tmp_path / "flat"), {"kind": "flat"}),
        ]
        for config, expected_namespace in cases:
            _write_tasks(config.tasks_file, [("TASK-001", "T", "p0", "todo")])

            def invoke(
                *, transport: str, config=config, expected_namespace=expected_namespace
            ) -> None:
                status = json.loads(spec_runner_status())
                detail = json.loads(spec_runner_task_detail("TASK-001"))
                assert status["project_root"] == str(config.project_root)
                assert status["namespace"] == expected_namespace
                assert detail["project_root"] == str(config.project_root)
                assert detail["namespace"] == expected_namespace
                assert {"total_tasks", "completed", "failed", "running", "total_cost"} <= set(
                    status
                )
                assert {"id", "name", "status", "checklist"} <= set(detail)

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)
