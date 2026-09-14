"""Serializer effective config and the reproducibility check before `Popen`
(spec-runner#485, DT-02).

Covers BEH-10, BEH-13, BEH-14 from
workstreams/mcp-launch-scope-spec-runner-485-20260914/spec/15-behaviour-spec.md.
BEH-13's live end-to-end shape (`run_task` through `run_server`) has its own
RED (tests/test_task_002_88012a1afd18fa55_6fc99bbb_red.py); this file is the
contract-level owner of `mcp_launch`'s serializer/reproducibility surface:
`REPRESENTABLE`/`NOT_FORWARDED`/`REPRESENTABLE_RUN`, `child_argv`, and
`simulate_child_config`/`Irreproducible`.
"""

import argparse
from json import dumps as json_dumps
from pathlib import Path
from unittest.mock import patch

from spec_runner.cli import _COMMON_DEFAULTS, _build_parser
from spec_runner.config import ExecutorConfig
from spec_runner.mcp_launch import (
    NOT_FORWARDED,
    REPRESENTABLE,
    REPRESENTABLE_RUN,
    Irreproducible,
    LaunchScope,
    child_argv,
    resolve_tool_config,
    simulate_child_config,
)


def _run_action_dests() -> set[str]:
    parser = _build_parser()
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    run_parser = subparsers_action.choices["run"]
    return {a.dest for a in run_parser._actions}


def _write_yaml(project_root: Path, text: str) -> None:
    (project_root / "spec-runner.config.yaml").write_text(text)


class TestBEH10SerializerAndCommonParserAgree:
    """The serializer's declared universe and `_COMMON_DEFAULTS` never drift
    apart silently (BEH-10)."""

    def test_every_representable_common_dest_has_a_common_defaults_key(self) -> None:
        common_dests = {dest for row in REPRESENTABLE for dest in row.common_dests}
        assert common_dests <= set(_COMMON_DEFAULTS)

    def test_representable_and_not_forwarded_cover_common_defaults_exactly(self) -> None:
        common_dests = {dest for row in REPRESENTABLE for dest in row.common_dests}
        covered = common_dests | set(NOT_FORWARDED)
        assert covered == set(_COMMON_DEFAULTS), f"gap or drift: {covered ^ set(_COMMON_DEFAULTS)}"

    def test_not_forwarded_entries_carry_a_reason(self) -> None:
        assert set(NOT_FORWARDED) <= set(_COMMON_DEFAULTS)
        for _key, reason in NOT_FORWARDED.items():
            assert reason and isinstance(reason, str)

    def test_representable_run_field_has_an_action_on_run(self) -> None:
        run_dests = _run_action_dests()
        for _field, dests in REPRESENTABLE_RUN.items():
            assert set(dests) <= run_dests


class TestBEH13IrreproducibleOverrideRefusesBeforePopen:
    """A parent override the child cannot rebuild is refused -- named,
    before anything is spawned (BEH-13)."""

    def test_non_representable_override_without_yaml_is_named(self, tmp_path: Path) -> None:
        config = ExecutorConfig(project_root=tmp_path, review_policy="required")
        scope = LaunchScope.of(config)
        argv = child_argv(config, "TASK-001")

        result = simulate_child_config(scope, argv)

        assert isinstance(result, Irreproducible)
        names = {d.field for d in result.diffs}
        assert "review_policy" in names
        assert "review_policy" in str(result)
        as_dict = result.to_dict()
        assert as_dict["status"] == "error"
        assert any(f["field"] == "review_policy" for f in as_dict["fields"])

    def test_non_representable_override_disagreeing_with_yaml_is_named(
        self, tmp_path: Path
    ) -> None:
        _write_yaml(tmp_path, "review_policy: advisory\n")
        config = ExecutorConfig(project_root=tmp_path, review_policy="required")
        scope = LaunchScope.of(config)
        argv = child_argv(config, "TASK-001")

        result = simulate_child_config(scope, argv)

        assert isinstance(result, Irreproducible)
        assert any(d.field == "review_policy" for d in result.diffs)

    def test_non_representable_string_field_override_is_named(self, tmp_path: Path) -> None:
        """`review_policy`/`execution_mode` aren't the only fields with no
        CLI flag -- a plain `str` field (`main_branch`) is refused the same
        way, proving the branch isn't keyed to those two names."""
        config = ExecutorConfig(project_root=tmp_path, main_branch="develop")
        scope = LaunchScope.of(config)
        argv = child_argv(config, "TASK-001")

        result = simulate_child_config(scope, argv)

        assert isinstance(result, Irreproducible)
        assert any(d.field == "main_branch" for d in result.diffs)

    def test_non_representable_int_field_override_is_named(self, tmp_path: Path) -> None:
        """Same branch, a plain `int` field (`retry_delay_seconds`)."""
        config = ExecutorConfig(project_root=tmp_path, retry_delay_seconds=99)
        scope = LaunchScope.of(config)
        argv = child_argv(config, "TASK-001")

        result = simulate_child_config(scope, argv)

        assert isinstance(result, Irreproducible)
        assert any(d.field == "retry_delay_seconds" for d in result.diffs)

    def test_yaml_deleted_after_parent_read_it_is_also_refused(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "review_policy: required\nexecution_mode: tdd\n")
        config = ExecutorConfig(
            project_root=tmp_path, review_policy="required", execution_mode="tdd"
        )
        scope = LaunchScope.of(config)
        argv = child_argv(config, "TASK-001")

        # The file the parent read vanishes before the check runs.
        (tmp_path / "spec-runner.config.yaml").unlink()

        result = simulate_child_config(scope, argv)

        assert isinstance(result, Irreproducible)
        names = {d.field for d in result.diffs}
        assert "review_policy" in names
        assert "execution_mode" in names

    def test_yaml_deleted_but_values_already_match_defaults_is_not_refused(
        self, tmp_path: Path
    ) -> None:
        """`yaml_missing` alone must not trigger a refusal -- only a value
        the vanished YAML actually changed relative to class defaults does
        (the sibling test above only covers the case where it does)."""
        _write_yaml(tmp_path, "review_policy: advisory\n")  # already the class default
        config = ExecutorConfig(project_root=tmp_path, review_policy="advisory")
        scope = LaunchScope.of(config)
        argv = child_argv(config, "TASK-001")

        (tmp_path / "spec-runner.config.yaml").unlink()

        result = simulate_child_config(scope, argv)

        assert not isinstance(result, Irreproducible)

    def test_check_never_spawns_a_process(self, tmp_path: Path) -> None:
        config = ExecutorConfig(project_root=tmp_path, review_policy="required")
        scope = LaunchScope.of(config)
        argv = child_argv(config, "TASK-001")

        with patch("subprocess.Popen") as mock_popen:
            result = simulate_child_config(scope, argv)

        mock_popen.assert_not_called()
        assert isinstance(result, Irreproducible)


class TestRefinementKeepsLaunchOverrides:
    """Flat launch + tool-level prefix (Q-01 refinement) is rebuilt through
    the same serializer/parser pair as the child, so launch-time
    representable overrides survive (DT-02 review finding)."""

    def test_refined_config_keeps_representable_overrides(self, tmp_path: Path) -> None:
        config = ExecutorConfig(
            project_root=tmp_path,
            run_tests_on_done=False,
            budget_usd=5.0,
            max_retries=7,
            spec_governance="strict",
        )
        scope = LaunchScope.of(config)

        refined = resolve_tool_config(scope, "p-")

        assert refined.spec_prefix == "p-"
        assert refined.project_root == tmp_path.resolve()
        assert refined.run_tests_on_done is False
        assert refined.budget_usd == 5.0
        assert refined.max_retries == 7
        assert refined.spec_governance == "strict"
        assert refined.tasks_file == tmp_path.resolve() / "spec" / "p-tasks.md"

    def test_refined_config_reads_yaml_by_project_root(self, tmp_path: Path) -> None:
        _write_yaml(tmp_path, "review_policy: required\n")
        scope = LaunchScope.of(ExecutorConfig(project_root=tmp_path))

        refined = resolve_tool_config(scope, "p-")

        assert refined.review_policy == "required"


class TestIrreproducibleNeverLeaksValues:
    def test_error_text_and_dict_carry_field_and_reason_only(self, tmp_path: Path) -> None:
        config = ExecutorConfig(project_root=tmp_path, telegram_bot_token="SECRET-TOKEN-1")
        scope = LaunchScope.of(config)

        result = simulate_child_config(scope, child_argv(config, "TASK-001"))

        assert isinstance(result, Irreproducible)
        assert "telegram_bot_token" in str(result)
        assert "SECRET-TOKEN-1" not in str(result)
        as_dict = result.to_dict()
        assert "SECRET-TOKEN-1" not in json_dumps(as_dict)
        assert set(as_dict["fields"][0]) == {"field", "reason"}


class TestMalformedInputsAreRefusedNotRaised:
    """A crash while building/comparing the simulated child config would
    defeat the whole point of checking before `Popen` -- a broken input is
    reported as `Irreproducible` like any other mismatch, never propagates."""

    def test_log_level_outside_run_parser_choices_is_refused_not_raised(
        self, tmp_path: Path
    ) -> None:
        """`ExecutorConfig.log_level` is a bare `str` with no validation,
        but `child_argv` always emits `--log-level <value>`, and the `run`
        subparser restricts `--log-level` to a fixed `choices=[...]` --
        parsing that argv back would otherwise raise `SystemExit`."""
        config = ExecutorConfig(project_root=tmp_path, log_level="trace")
        scope = LaunchScope.of(config)
        argv = child_argv(config, "TASK-001")

        with patch("subprocess.Popen") as mock_popen:
            result = simulate_child_config(scope, argv)

        mock_popen.assert_not_called()
        assert isinstance(result, Irreproducible)
        assert "trace" in str(result) or "argv" in str(result).lower()

    def test_unreadable_yaml_is_refused_not_raised(self, tmp_path: Path) -> None:
        """A config file that fails to parse (`ConfigError`) is reported as
        `Irreproducible`, not left to propagate out of the MCP tool call."""
        (tmp_path / "spec-runner.config.yaml").write_text("not: valid: yaml: [")
        config = ExecutorConfig(project_root=tmp_path)
        scope = LaunchScope.of(config)
        argv = child_argv(config, "TASK-001")

        with patch("subprocess.Popen") as mock_popen:
            result = simulate_child_config(scope, argv)

        mock_popen.assert_not_called()
        assert isinstance(result, Irreproducible)


class TestBEH14ReproducibleConfigSpawnsChildExactlyOnce:
    """A parent config that is fully reproducible from argv + YAML forms an
    argv without refusal, and `run_task` spawns the child exactly once
    (BEH-14) -- the same check that decides BEH-13 finding nothing wrong."""

    def test_fully_representable_overrides_yield_no_diff(self, tmp_path: Path) -> None:
        config = ExecutorConfig(
            project_root=tmp_path,
            max_retries=7,
            task_timeout_minutes=42,
            run_tests_on_done=False,
            create_git_branch=False,
            auto_commit=False,
            run_review=False,
            integration_pr=True,
            hitl_review=True,
            budget_usd=12.5,
            task_budget_usd=3.0,
            callback_url="https://example.invalid/hook",
            log_level="debug",
            spec_governance="strict",
        )
        scope = LaunchScope.of(config)
        argv = child_argv(config, "TASK-001")

        result = simulate_child_config(scope, argv)

        assert not isinstance(result, Irreproducible)
        assert isinstance(result, ExecutorConfig)

    def test_defaults_only_config_yields_no_diff(self, tmp_path: Path) -> None:
        config = ExecutorConfig(project_root=tmp_path)
        scope = LaunchScope.of(config)
        argv = child_argv(config, "TASK-001")

        result = simulate_child_config(scope, argv)

        assert not isinstance(result, Irreproducible)

    def test_run_task_spawns_the_validated_argv_exactly_once(self, tmp_path: Path) -> None:
        """BEH-14 end to end: with the full representable override set,
        `run_task` refuses nothing, calls `Popen` exactly once, answers
        `started` -- and the spawned command IS the validated argv (the
        DT-02 review found a second, shorter command being spawned)."""
        import json
        from unittest.mock import MagicMock

        import spec_runner.mcp_server as server
        from spec_runner.mcp_server import spec_runner_run_task

        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / "tasks.md").write_text(
            "# Tasks\n\n### TASK-001: T\n\U0001f534 P0 | \u2b1c TODO | Est: 1d\n"
        )
        config = ExecutorConfig(
            project_root=tmp_path,
            max_retries=7,
            task_timeout_minutes=9,
            run_tests_on_done=False,
            create_git_branch=False,
            auto_commit=False,
            run_review=False,
            integration_pr=True,
            hitl_review=True,
            budget_usd=5.0,
            task_budget_usd=1.5,
            callback_url="http://callback.invalid/hook",
            log_level="debug",
            spec_governance="strict",
        )

        def _popen(*args, **kwargs):
            proc = MagicMock()
            proc.pid = 777
            proc.poll.return_value = None
            config.ready_file.parent.mkdir(parents=True, exist_ok=True)
            config.ready_file.write_text("PID: 777\nStarted: now\n")
            return proc

        with patch("subprocess.Popen", side_effect=_popen) as mock_popen:

            def invoke(*, transport: str) -> None:
                result = json.loads(spec_runner_run_task("TASK-001"))
                assert result["status"] == "started", result

            with patch.object(server.mcp_app, "run", side_effect=invoke):
                server.run_server(config)

        mock_popen.assert_called_once()
        cmd = mock_popen.call_args.args[0]
        assert cmd == ["spec-runner", *child_argv(config, "TASK-001")]
        for flag in (
            "--max-retries",
            "7",
            "--timeout",
            "9",
            "--no-tests",
            "--no-branch",
            "--no-commit",
            "--no-review",
            "--integration-pr",
            "--hitl-review",
            "--budget",
            "5.0",
            "--task-budget",
            "1.5",
            "--callback-url",
            "http://callback.invalid/hook",
            "--log-level",
            "debug",
            "--strict",
        ):
            assert flag in cmd, flag
        assert mock_popen.call_args.kwargs["cwd"] == config.project_root

    def test_check_itself_never_spawns_a_process(self, tmp_path: Path) -> None:
        """Same non-spawning guarantee as BEH-13's refusal path (§2.2: one
        check, either outcome) -- proceeding never spawns anything either;
        `run_task`'s own single real `Popen` call (BEH-11/BEH-15, DT-04) is
        exercised end to end by `tests/test_mcp_launch_scope.py` and the
        `TestMCPRunTask` suite in `tests/test_mcp.py`, both of which already
        assert `mock_popen.assert_called_once()` past this same check.
        """
        config = ExecutorConfig(project_root=tmp_path, max_retries=7)
        scope = LaunchScope.of(config)
        argv = child_argv(config, "TASK-001")

        with patch("subprocess.Popen") as mock_popen:
            result = simulate_child_config(scope, argv)

        mock_popen.assert_not_called()
        assert not isinstance(result, Irreproducible)
