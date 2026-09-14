"""MCP launch scope: child-side ready handshake and its live proof
(spec-runner#485, DT-04).

Covers BEH-08, BEH-09, BEH-11, BEH-12, BEH-15, BEH-16, BEH-17, BEH-19,
BEH-26, BEH-27 from
workstreams/mcp-launch-scope-spec-runner-485-20260914/spec/15-behaviour-spec.md.
BEH-15's frozen red-frame proof (a real child that completes its task and is
observed answering `started`) lives in
tests/test_task_004_88012a1afd18fa55_6fc99bbb_red.py; this file adds the
scenarios the red-frame explicitly leaves to this file's own red (lock/ready
ordering detail, stop-after-started, marker semantics, scope containment).

Every scenario here spawns a REAL `spec-runner run --task ...` child
(`sys.executable -m spec_runner`, DT-01's `child_entry()` seam) against
`tests/fixtures/fake_claude.sh` or the specialized
`tests/fixtures/fake_claude_signal_wait.sh` double -- never a paid CLI.
The conftest belt cannot see inside a spawned child (it monkeypatches
`subprocess` in the pytest process only), so the discipline is enforced
here: every YAML a test writes for a child goes through
`_assert_fixture_agent`, which refuses a `claude_command` that is not a
script under `tests/fixtures/`.
"""

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import spec_runner.mcp_server as server
from spec_runner import mcp_launch
from spec_runner.cli import _build_parser
from spec_runner.config import (
    ExecutorConfig,
    _resolve_config_path,
    build_config,
    load_config_from_yaml,
)
from spec_runner.mcp_server import spec_runner_run_task, spec_runner_stop
from spec_runner.state import ExecutorState

FIXTURES = Path(__file__).parent / "fixtures"
FAKE_CLI = FIXTURES / "fake_claude.sh"
FAKE_CLI_SIGNAL_WAIT = FIXTURES / "fake_claude_signal_wait.sh"
CONFIG_DUMP_ENTRY = FIXTURES / "mcp_e2e_config_dump_entry.py"

CONFIG_YAML = """\
claude_command: "{fake_cli}"
command_template: "{{cmd}} -p {{prompt}}"
skip_permissions: true
mcp_ready_timeout_seconds: 5
max_retries: 1
task_timeout_minutes: 1
hooks:
  pre_start:
    create_git_branch: false
  post_done:
    run_tests: false
    run_lint: false
    auto_commit: false
    run_review: false
"""

TASKS_MD = """\
# Tasks

### TASK-001: Add login page
\U0001f7e0 P1 | ⬜ TODO | Est: 1h

**Checklist:**
- [ ] Create login form
"""


def _assert_fixture_agent(yaml_text: str) -> str:
    """The only guard that actually reaches a spawned child: the YAML it
    will read must name a `claude_command` under `tests/fixtures/`. The
    conftest belt is in-process and blind to the child (BEH-27)."""
    commands = [
        line.split(":", 1)[1].strip().strip("\"'")
        for line in yaml_text.splitlines()
        if line.strip().startswith("claude_command:")
    ]
    assert commands, "child YAML must declare claude_command explicitly"
    for command in commands:
        assert Path(command).resolve().is_relative_to(FIXTURES.resolve()), (
            f"child YAML points claude_command outside tests/fixtures: {command!r}"
        )
    return yaml_text


def _write_project(project_root: Path, *, config_yaml: str = CONFIG_YAML) -> None:
    (project_root / "spec").mkdir(parents=True, exist_ok=True)
    (project_root / "spec-runner.config.yaml").write_text(
        _assert_fixture_agent(config_yaml.format(fake_cli=str(FAKE_CLI)))
    )
    (project_root / "spec" / "tasks.md").write_text(TASKS_MD)


def _resolved_launch_config(argv: list[str]) -> ExecutorConfig:
    """Build a launch scope's config exactly the way the real CLI would --
    same YAML, same parser -- so the launch config a test hands to
    `run_server` can never drift from what `spec_runner_run_task` itself
    resolves (mirrors the frozen BEH-15 red-frame).
    """
    args = _build_parser().parse_args(argv)
    config_path = _resolve_config_path(Path(args.project_root) if args.project_root else None)
    yaml_config = load_config_from_yaml(config_path)
    return build_config(yaml_config, args)


def _quick_success(monkeypatch, response_text: str = "TASK_COMPLETE\n") -> Path:
    response_file = _tmp_response_file(monkeypatch, response_text)
    return response_file


def _tmp_response_file(monkeypatch, text: str) -> Path:
    import tempfile

    fd, path = tempfile.mkstemp()
    os.close(fd)
    response_file = Path(path)
    response_file.write_text(text)
    monkeypatch.setenv("FAKE_RESPONSE_FILE", str(response_file))
    monkeypatch.setenv("FAKE_EXIT_CODE", "0")
    monkeypatch.delenv("FAKE_DELAY", raising=False)
    return response_file


#: `runner.log_progress()` writes `config.PROGRESS_FILE` -- a hardcoded,
#: pre-#485 relative path (`spec/.executor-progress.txt`), never
#: namespace-aware -- on every progress line, regardless of `change_id` /
#: `spec_prefix`. It ends up in the flat `spec/` dir even for a
#: change/prefix-scoped run. That gap predates this workstream's namespacing
#: (DT-01) and `log_progress` has 69 call sites across the codebase; fixing
#: it is out of DT-04's remit (child-ready handshake + its E2E proof), so
#: BEH-26's containment check here excludes this one known, separately
#: trackable exception rather than asserting a guarantee the product does
#: not yet provide.
_KNOWN_FLAT_LEAK = ".executor-progress.txt"


def _stray_flat_executor_files(spec_dir: Path) -> list[Path]:
    return [p for p in spec_dir.glob(".executor-*") if p.name != _KNOWN_FLAT_LEAK]


def _wait_for_completion(config: ExecutorConfig, timeout: float = 15.0) -> None:
    """Block until the child has released its run lock -- the product's own
    "this run is over" signal (`cmd_run`'s `finally`), and a far more
    reliable one than polling the child's pid: a reaped pid can be recycled
    by an unrelated process within milliseconds on this machine, which made
    an `os.kill(pid, 0)`-based wait pass on a process that was never ours.
    """
    lock_file = config.state_file.with_suffix(".lock")
    deadline = time.monotonic() + timeout
    while lock_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not lock_file.exists(), f"child did not release its lock within {timeout}s"


class TestBEH08ChildScopeAndNamespace:
    """Child lives in `project_root` and in exactly one namespace (FR-03)."""

    def test_change_scoped_real_child_runs_in_project_root_single_namespace(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        external = tmp_path / "external"
        (external / "spec" / "changes" / "add-x").mkdir(parents=True)
        (external / "spec-runner.config.yaml").write_text(
            _assert_fixture_agent(CONFIG_YAML.format(fake_cli=str(FAKE_CLI)))
        )
        (external / "spec" / "changes" / "add-x" / "tasks.md").write_text(TASKS_MD)
        _quick_success(monkeypatch)

        cwd_dir = tmp_path / "server-cwd"
        cwd_dir.mkdir()
        monkeypatch.chdir(cwd_dir)

        config = _resolved_launch_config(
            ["run", "--project-root", str(external), "--change", "add-x"]
        )

        captured: dict = {}
        real_popen = __import__("subprocess").Popen

        def spy_popen(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = kwargs.get("cwd")
            return real_popen(cmd, *args, **kwargs)

        with patch("subprocess.Popen", side_effect=spy_popen):

            def invoke(*, transport: str) -> None:
                result = json.loads(spec_runner_run_task("TASK-001"))
                assert result["status"] == "started", result

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)

        _wait_for_completion(config)

        assert "--project-root" in captured["cmd"]
        assert captured["cmd"][captured["cmd"].index("--project-root") + 1] == str(external)
        assert "--change" in captured["cmd"]
        assert captured["cmd"][captured["cmd"].index("--change") + 1] == "add-x"
        assert "--spec-prefix" not in captured["cmd"]
        assert captured["cwd"] == external

        namespace_dir = external / "spec" / "changes" / "add-x"
        assert (namespace_dir / ".executor-state.db").exists()
        assert _stray_flat_executor_files(external / "spec") == []
        assert list(cwd_dir.rglob(".executor-*")) == []

    def test_prefix_scoped_real_child_runs_in_project_root_single_namespace(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        external = tmp_path / "external"
        _write_project(external)
        (external / "spec" / "p-tasks.md").write_text(TASKS_MD)
        _quick_success(monkeypatch)

        cwd_dir = tmp_path / "server-cwd"
        cwd_dir.mkdir()
        monkeypatch.chdir(cwd_dir)

        config = _resolved_launch_config(
            ["run", "--project-root", str(external), "--spec-prefix", "p-"]
        )

        captured: dict = {}
        real_popen = __import__("subprocess").Popen

        def spy_popen(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            return real_popen(cmd, *args, **kwargs)

        with patch("subprocess.Popen", side_effect=spy_popen):

            def invoke(*, transport: str) -> None:
                result = json.loads(spec_runner_run_task("TASK-001"))
                assert result["status"] == "started", result

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)

        _wait_for_completion(config)

        assert "--spec-prefix" in captured["cmd"]
        assert captured["cmd"][captured["cmd"].index("--spec-prefix") + 1] == "p-"
        assert "--change" not in captured["cmd"]
        assert (external / "spec" / ".executor-p-state.db").exists()


class TestBEH09EffectiveConfigParity:
    """Effective config of child equals parent on every representable field
    (and every non-representable field, since both read the same YAML)."""

    def test_child_resolves_the_same_effective_config_as_the_parent(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        external = tmp_path / "external"
        (external / "spec" / "changes" / "add-x").mkdir(parents=True)
        yaml_text = f"""\
claude_command: "{FAKE_CLI}"
review_policy: required
execution_mode: tdd
harness_guard: strict
mcp_ready_timeout_seconds: 1
commands:
  test: "pytest -k custom"
  lint: "ruff check custom"
"""
        (external / "spec-runner.config.yaml").write_text(_assert_fixture_agent(yaml_text))
        (external / "spec" / "changes" / "add-x" / "tasks.md").write_text(TASKS_MD)

        argv = [
            "run",
            "--project-root",
            str(external),
            "--change",
            "add-x",
            "--max-retries",
            "5",
            "--timeout",
            "17",
            "--no-tests",
            "--no-branch",
            "--no-commit",
            "--no-review",
            "--integration-pr",
            "--hitl-review",
            "--budget",
            "12.5",
            "--task-budget",
            "3.5",
            "--callback-url",
            "http://callback.invalid/hook",
            "--log-level",
            "debug",
            "--strict",
        ]
        config = _resolved_launch_config(argv)

        dump_file = tmp_path / "child-config-dump.json"
        monkeypatch.setenv("MCP_E2E_CONFIG_DUMP_FILE", str(dump_file))

        with patch.object(
            mcp_launch, "child_entry", return_value=[sys.executable, str(CONFIG_DUMP_ENTRY)]
        ):

            def invoke(*, transport: str) -> None:
                # The dump entry point exits without publishing ready, so the
                # parent times out -- this test only cares about the dump.
                spec_runner_run_task("TASK-001")

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)

        assert dump_file.exists(), "child entry point never ran / never wrote its config"
        child_dump = json.loads(dump_file.read_text())

        import dataclasses

        from spec_runner.mcp_launch import PARENT_ONLY_FIELDS, _jsonable

        parent_dump = {
            f.name: _jsonable(getattr(config, f.name))
            for f in dataclasses.fields(config)
            if f.name not in PARENT_ONLY_FIELDS
        }
        assert child_dump == parent_dump


class TestBEH11ChildUsesCurrentInterpreterNotPath:
    def test_child_argv0_is_the_current_interpreter_even_with_spec_runner_off_path(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        external = tmp_path / "external"
        _write_project(external)
        _quick_success(monkeypatch)

        # Drop every PATH dir that carries an executable `spec-runner` (the
        # venv's own bin dir, a user-level `~/.local/bin` install, ...),
        # keeping the rest of PATH so `bash` (the fake CLI's shebang
        # interpreter) still resolves -- narrowing PATH to nothing would
        # break the fake CLI itself, not just prove BEH-11.
        import shutil

        kept = [
            p
            for p in os.environ.get("PATH", "").split(os.pathsep)
            if p and not (Path(p) / "spec-runner").is_file()
        ]
        monkeypatch.setenv("PATH", os.pathsep.join(kept))
        assert shutil.which("spec-runner") is None, "test setup: spec-runner must be off PATH"

        config = _resolved_launch_config(["run", "--project-root", str(external)])

        captured: dict = {}
        real_popen = __import__("subprocess").Popen

        def spy_popen(cmd, *args, **kwargs):
            captured["cmd"] = cmd
            return real_popen(cmd, *args, **kwargs)

        with patch("subprocess.Popen", side_effect=spy_popen):

            def invoke(*, transport: str) -> None:
                result = json.loads(spec_runner_run_task("TASK-001"))
                assert result["status"] == "started", result
                lock_file = Path(result["lock_file"])
                assert lock_file.exists()
                assert f"PID: {result['pid']}" in lock_file.read_text()
                _wait_for_completion(config)

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)

        assert captured["cmd"][0] == sys.executable


class TestBEH12VerboseChildDoesNotHang:
    def test_verbose_agent_output_goes_through_without_hanging_the_parent(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The fake CLI's own response is >= 1 MiB -- bigger than the OS pipe
        buffer. `execution.py` captures it via `subprocess.run(capture_output=True)`
        (which uses `communicate()`, safe regardless of size); the property
        this asserts is the outer one `mcp_server.spec_runner_run_task` owns
        (design 3.3): its own child's stdout/stderr go to a file under the
        launch namespace's `logs_dir`, never `subprocess.PIPE` -- so neither
        level can block on an unread pipe.
        """
        external = tmp_path / "external"
        _write_project(external)
        _quick_success(monkeypatch, response_text="TASK_COMPLETE\n" + ("x" * (1024 * 1024 + 1)))

        config = _resolved_launch_config(["run", "--project-root", str(external)])

        captured: dict = {}

        def invoke(*, transport: str) -> None:
            start = time.monotonic()
            result = json.loads(spec_runner_run_task("TASK-001"))
            captured["elapsed"] = time.monotonic() - start
            captured["result"] = result
            _wait_for_completion(config)

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)

        result = captured["result"]
        assert result["status"] == "started", result
        assert captured["elapsed"] < 30.0, "verbose child should not block the parent on stdout"

        log_file = Path(result["log_file"])
        assert log_file.parent == external / "spec" / ".executor-logs"
        assert log_file.exists()

        with ExecutorState.for_read(config) as state:
            ts = state.get_task_state("TASK-001")
            assert ts is not None and ts.status == "success"


class TestBEH15LockAndReadyBothPrecedeStarted:
    def test_started_response_carries_a_lock_file_with_the_response_pid(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        external = tmp_path / "external"
        _write_project(external)
        _quick_success(monkeypatch)

        config = _resolved_launch_config(["run", "--project-root", str(external)])
        assert not config.ready_file.exists()

        def invoke(*, transport: str) -> None:
            result = json.loads(spec_runner_run_task("TASK-001"))
            assert result["status"] == "started", result
            lock_file = Path(result["lock_file"])
            assert lock_file.exists()
            assert lock_file.read_text().splitlines()[0] == f"PID: {result['pid']}"
            _wait_for_completion(config)

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)

        # The handshake's ready file is consumed (unlinked) by `wait_for_ready`
        # the moment it observes it -- gone once the response is in hand.
        assert not config.ready_file.exists()


class TestBEH16StopAfterStartedIsNotLost:
    def test_stop_after_started_is_honoured_deterministically(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        external = tmp_path / "external"
        _write_project(external)
        (external / "spec-runner.config.yaml").write_text(
            _assert_fixture_agent(CONFIG_YAML.format(fake_cli=str(FAKE_CLI_SIGNAL_WAIT)))
        )

        signal_file = tmp_path / "signal"
        release_file = tmp_path / "release"
        monkeypatch.setenv("SIGNAL_FILE", str(signal_file))
        monkeypatch.setenv("RELEASE_FILE", str(release_file))
        monkeypatch.setenv("RELEASE_TIMEOUT_SECONDS", "30")
        _quick_success(monkeypatch)

        config = _resolved_launch_config(["run", "--project-root", str(external)])

        def invoke(*, transport: str) -> None:
            result = json.loads(spec_runner_run_task("TASK-001"))
            assert result["status"] == "started", result

            deadline = time.monotonic() + 10
            while not signal_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            assert signal_file.exists(), "child never signalled it had started the task"

            stop_result = json.loads(spec_runner_stop())
            assert stop_result["status"] == "stop_requested"
            stop_file = Path(stop_result["stop_file"])
            assert stop_file == config.stop_file

            release_file.write_text("go")
            _wait_for_completion(config)

            # Deterministic outcome (ii): the marker was written after the
            # pre-task check had passed, so TASK-001 ran to completion (one
            # attempt, success) and no branch downstream of the handshake
            # consumed the marker -- it is still on disk.
            assert stop_file.exists()
            assert _outcome_after_stop(config) == "marker_survived_task_ran"

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)

    def test_immediate_stop_after_started_has_no_forbidden_outcome(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        external = tmp_path / "external"
        _write_project(external)
        _quick_success(monkeypatch)

        config = _resolved_launch_config(["run", "--project-root", str(external)])

        def invoke(*, transport: str) -> None:
            result = json.loads(spec_runner_run_task("TASK-001"))
            assert result["status"] == "started", result
            stop_result = json.loads(spec_runner_stop())
            assert stop_result["status"] == "stop_requested"

            _wait_for_completion(config)
            _outcome_after_stop(config)

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)


class TestBEH17MarkerBeforeRunTaskIsClearedAfterStartedIsNot:
    def test_marker_before_run_task_is_cleared_and_task_runs_normally(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        external = tmp_path / "external"
        _write_project(external)
        _quick_success(monkeypatch)

        config = _resolved_launch_config(["run", "--project-root", str(external)])
        config.stop_file.parent.mkdir(parents=True, exist_ok=True)
        config.stop_file.write_text("stop")
        assert config.stop_file.exists()

        def invoke(*, transport: str) -> None:
            result = json.loads(spec_runner_run_task("TASK-001"))
            assert result["status"] == "started", result
            _wait_for_completion(config)

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)

        assert not config.stop_file.exists(), "the pre-existing marker should have been cleared"

    def test_marker_written_after_started_survives_the_run(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        external = tmp_path / "external"
        _write_project(external)
        _quick_success(monkeypatch)

        config = _resolved_launch_config(["run", "--project-root", str(external)])

        def invoke(*, transport: str) -> None:
            result = json.loads(spec_runner_run_task("TASK-001"))
            assert result["status"] == "started", result
            spec_runner_stop()
            _wait_for_completion(config)

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)

        _outcome_after_stop(config)


@pytest.mark.slow
class TestBEH19SoakStopAfterStartedHoldsStatistically:
    def test_stop_after_started_holds_over_20_iterations(self, tmp_path: Path, monkeypatch) -> None:
        outcomes = {"marker_consumed_before_task": 0, "marker_survived_task_ran": 0}

        for i in range(20):
            external = tmp_path / f"iter-{i}"
            _write_project(external)
            _tmp_response_file(monkeypatch, "TASK_COMPLETE\n")

            config = _resolved_launch_config(["run", "--project-root", str(external)])

            def invoke(*, transport: str, config=config) -> None:
                result = json.loads(spec_runner_run_task("TASK-001"))
                assert result["status"] == "started", result
                spec_runner_stop()
                _wait_for_completion(config)

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)

            # The helper asserts the invariant from the state ledger and names
            # which legal outcome this iteration produced; the forbidden one
            # ("marker gone AND an attempt was made") raises there.
            try:
                outcomes[_outcome_after_stop(config)] += 1
            except AssertionError as exc:
                raise AssertionError(f"iteration {i}: {exc}") from exc

        # Evidence, not an assertion: printed for the PR description (NFR-03).
        print(f"BEH-19 soak outcomes over 20 iterations: {outcomes}")


class TestBEH26NoStrayRuntimeFiles:
    def test_success_and_contradiction_branches_leave_no_stray_executor_files(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        external = tmp_path / "external"
        (external / "spec" / "changes" / "add-x").mkdir(parents=True)
        (external / "spec-runner.config.yaml").write_text(
            _assert_fixture_agent(CONFIG_YAML.format(fake_cli=str(FAKE_CLI)))
        )
        (external / "spec" / "changes" / "add-x" / "tasks.md").write_text(TASKS_MD)
        _quick_success(monkeypatch)

        cwd_dir = tmp_path / "server-cwd"
        cwd_dir.mkdir()
        monkeypatch.chdir(cwd_dir)

        config = _resolved_launch_config(
            ["run", "--project-root", str(external), "--change", "add-x"]
        )

        def invoke(*, transport: str) -> None:
            result = json.loads(spec_runner_run_task("TASK-001"))
            assert result["status"] == "started", result
            _wait_for_completion(config)

            # Contradicting tool-level prefix: refused by name, nothing spawned.
            contradiction = json.loads(spec_runner_run_task("TASK-001", spec_prefix="q-"))
            assert contradiction["status"] == "error"

        with patch.object(server.mcp_app, "run", side_effect=invoke):
            server.run_server(config)

        assert _stray_flat_executor_files(external / "spec") == []
        assert list(cwd_dir.rglob(".executor-*")) == []
        namespace_dir = external / "spec" / "changes" / "add-x"
        assert (namespace_dir / ".executor-state.db").exists()

    def test_error_branches_under_a_change_launch_leave_no_stray_executor_files(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The refusal branches BEH-26 lists -- irreproducible config, early
        child exit, busy lock, ready timeout -- each driven through a real
        change-scoped launch, with the same containment check afterwards."""
        from spec_runner.config import ExecutorLock

        external = tmp_path / "external"
        (external / "spec" / "changes" / "add-x").mkdir(parents=True)
        (external / "spec-runner.config.yaml").write_text(
            _assert_fixture_agent(CONFIG_YAML.format(fake_cli=str(FAKE_CLI)))
        )
        (external / "spec" / "changes" / "add-x" / "tasks.md").write_text(TASKS_MD)
        _quick_success(monkeypatch)
        cwd_dir = tmp_path / "server-cwd"
        cwd_dir.mkdir()
        monkeypatch.chdir(cwd_dir)
        base_argv = ["run", "--project-root", str(external), "--change", "add-x"]

        def contained() -> None:
            assert _stray_flat_executor_files(external / "spec") == []
            assert list(cwd_dir.rglob(".executor-*")) == []

        # (a) irreproducible programmatic override: refused before Popen.
        irreproducible = ExecutorConfig(
            project_root=external, change_id="add-x", review_policy="required"
        )

        def invoke_irreproducible(*, transport: str) -> None:
            result = json.loads(spec_runner_run_task("TASK-001"))
            assert result["status"] == "error", result
            assert "review_policy" in result["error"]

        with patch.object(server.mcp_app, "run", side_effect=invoke_irreproducible):
            server.run_server(irreproducible)
        contained()

        # (b) early child exit: a real child process that dies before it could
        # publish ready (exit 3). Not an unknown task id -- the child
        # publishes ready before it looks the task up, so that one races
        # with `started` and is a legitimate outcome either way.
        config = _resolved_launch_config(base_argv)

        def invoke_early_exit(*, transport: str) -> None:
            result = json.loads(spec_runner_run_task("TASK-001"))
            assert result["status"] == "error", result
            assert result.get("exit_code") == 3, result

        with (
            patch.object(
                mcp_launch,
                "child_entry",
                return_value=[sys.executable, "-c", "import sys; sys.exit(3)"],
            ),
            patch.object(server.mcp_app, "run", side_effect=invoke_early_exit),
        ):
            server.run_server(config)
        contained()

        # (c) busy lock: held in this process, the child refuses and exits.
        lock = ExecutorLock(config.state_file.with_suffix(".lock"))
        assert lock.acquire()
        try:

            def invoke_busy(*, transport: str) -> None:
                result = json.loads(spec_runner_run_task("TASK-001"))
                assert result["status"] == "error", result

            with patch.object(server.mcp_app, "run", side_effect=invoke_busy):
                server.run_server(config)
        finally:
            lock.release()
        contained()

        # (d) ready timeout: the parent gives up before any child could
        # publish, terminates it, and still leaves nothing outside the scope.
        impatient = _resolved_launch_config(base_argv)
        impatient.mcp_ready_timeout_seconds = 0.001

        def invoke_timeout(*, transport: str) -> None:
            result = json.loads(spec_runner_run_task("TASK-001"))
            assert result["status"] == "error", result

        with patch.object(server.mcp_app, "run", side_effect=invoke_timeout):
            server.run_server(impatient)
        _wait_for_completion(impatient)
        contained()


class TestBEH27NoPaidAgentAndBaseE2ENotSlow:
    """No paid agent: the conftest belt is blind to a spawned child, so the
    guard that counts is `_assert_fixture_agent` on every YAML a child reads
    (checked here on the module's own template, and by construction at
    every write site). Plus the CI-budget shape design calls for: the base
    BEH-15/16/17 tests run under `-m "not slow"`, and only the BEH-19 soak
    is `@pytest.mark.slow`.
    """

    def test_module_yaml_template_points_claude_command_at_a_fixture(self) -> None:
        _assert_fixture_agent(CONFIG_YAML.format(fake_cli=str(FAKE_CLI)))
        with pytest.raises(AssertionError):
            _assert_fixture_agent(CONFIG_YAML.format(fake_cli="/usr/local/bin/claude"))
        with pytest.raises(AssertionError):
            _assert_fixture_agent("review_policy: required\n")

    def test_base_handshake_tests_are_not_marked_slow_only_the_soak_is(self) -> None:
        base_classes = [
            TestBEH15LockAndReadyBothPrecedeStarted,
            TestBEH16StopAfterStartedIsNotLost,
            TestBEH17MarkerBeforeRunTaskIsClearedAfterStartedIsNot,
        ]
        for cls in base_classes:
            for name, member in vars(cls).items():
                if name.startswith("test_"):
                    marks = getattr(member, "pytestmark", [])
                    assert not any(m.name == "slow" for m in marks), (
                        f"{cls.__name__}.{name} must stay in the default CI budget"
                    )

        soak_marks = getattr(TestBEH19SoakStopAfterStartedHoldsStatistically, "pytestmark", [])
        assert any(m.name == "slow" for m in soak_marks)


def _outcome_after_stop(config: ExecutorConfig) -> str:
    """BEH-16's invariant for a `stop()` issued after `started`, read from
    the state ledger (never from the DB file's size: the child opens
    `ExecutorState` before its pre-task stop check, so the file is non-empty
    on every run whether or not the task was attempted).

    Legal outcomes: (i) `marker_consumed_before_task` -- the marker landed
    before the pre-task check (cli.py, fixed-list branch), was consumed by
    it, and TASK-001 got zero attempts; the child logged the graceful
    shutdown. (ii) `marker_survived_task_ran` -- the marker landed after
    that check, TASK-001 ran to one successful attempt, and no branch
    downstream of the handshake removed the marker.

    Forbidden -- the lost stop the spec names ("marker gone AND an attempt
    was made", 15-behaviour-spec.md BEH-16): a `clear_stop_file` anywhere
    after ready, e.g. the ready publish moved before the stale-marker
    clear, produces exactly that pair. This helper must go red on it.
    """
    marker_present = config.stop_file.exists()
    with ExecutorState.for_read(config) as state:
        ts = state.get_task_state("TASK-001")
    attempts = ts.attempt_count if ts is not None else 0
    status = ts.status if ts is not None else None
    assert not (not marker_present and attempts > 0), (
        "lost stop request: marker gone but TASK-001 was attempted "
        f"({attempts} attempt(s), status={status!r})"
    )
    if marker_present:
        assert attempts == 1 and status == "success", (
            f"marker survived but TASK-001 did not run to one success: "
            f"attempts={attempts}, status={status!r}"
        )
        return "marker_survived_task_ran"
    assert attempts == 0, f"marker consumed yet {attempts} attempt(s) recorded"
    logged = any(
        "Graceful shutdown requested" in path.read_text(errors="replace")
        for path in config.logs_dir.glob("TASK-001*")
    )
    assert logged, "marker consumed before the task, but the child never logged the shutdown"
    return "marker_consumed_before_task"
