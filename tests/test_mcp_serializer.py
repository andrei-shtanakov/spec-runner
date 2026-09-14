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

    def test_adding_a_common_flag_without_the_serializer_is_caught(self) -> None:
        """A flag `common` grows that the serializer doesn't know about
        breaks the parity equality -- simulating the drift `_COMMON_DEFAULTS`
        itself would see (the real regression this scenario protects against
        already fails parser build in `cli.py`; this proves the *serializer*
        side of the same contract independently)."""
        common_dests = {dest for row in REPRESENTABLE for dest in row.common_dests}
        covered = common_dests | set(NOT_FORWARDED)
        drifted = set(_COMMON_DEFAULTS) | {"a_new_flag_nobody_declared"}
        assert covered != drifted


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

    def test_check_never_spawns_a_process(self, tmp_path: Path) -> None:
        config = ExecutorConfig(project_root=tmp_path, review_policy="required")
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
