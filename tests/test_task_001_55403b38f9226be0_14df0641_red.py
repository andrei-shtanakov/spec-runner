"""RED for BEH-01: `**Mode:** verify_first` resolves per-task under any project default.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-01

Given the same tasks-file line `**Mode:** verify_first`, read in a project
defaulting to `standard` and one defaulting to `tdd`, the task must resolve to
`verify_first` in both — a per-task declaration works in both directions, as
it already does for `standard`/`tdd` (#141 amendment 4). A sibling task in the
same file with no `**Mode:**` line still resolves to the project default,
unaffected.

Today `verify_first` is not a recognised execution mode at all
(`config.EXECUTION_MODES == ("standard", "tdd")`), so
`resolve_execution_mode` raises `ConfigError` for a task declaring it,
regardless of the project's own default.
"""

from pathlib import Path

from spec_runner.config import ExecutorConfig


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "state.db",
        "logs_dir": tmp_path / "logs",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


class TestVerifyFirstResolvesPerTaskUnderAnyProjectDefault:
    def test_resolves_to_verify_first_under_a_standard_default_project(self, tmp_path):
        from spec_runner.task import Task

        config = _cfg(tmp_path, execution_mode="standard")
        task = Task(
            id="TASK-001",
            name="t",
            priority="p1",
            status="todo",
            estimate="1h",
            execution_mode="verify_first",
        )

        assert config.resolve_execution_mode(task) == "verify_first"

    def test_resolves_to_verify_first_under_a_tdd_default_project(self, tmp_path):
        from spec_runner.task import Task

        config = _cfg(tmp_path, execution_mode="tdd")
        task = Task(
            id="TASK-002",
            name="t",
            priority="p1",
            status="todo",
            estimate="1h",
            execution_mode="verify_first",
        )

        assert config.resolve_execution_mode(task) == "verify_first"
