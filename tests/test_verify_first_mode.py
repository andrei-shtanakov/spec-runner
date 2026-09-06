"""BEH-01: `**Mode:** verify_first` resolves per-task under any project default.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-01
Traces: FR-01

Given the same tasks-file line `**Mode:** verify_first`, read in a project
defaulting to `standard` and one defaulting to `tdd`, the task must resolve to
`verify_first` in both — a per-task declaration works in both directions, as
it already does for `standard`/`tdd` (#141 amendment 4). A sibling task in the
same file with no `**Mode:**` line still resolves to the project default,
unaffected.

This slice only covers resolution: `verify_first` is a recognised mode, not
yet a driven one. Whether a verify-first task without its own `**Verifies:**`
group is refused is BEH-04's contract, not this one's.
"""

from pathlib import Path

import pytest

from spec_runner.config import ConfigError, ExecutorConfig
from spec_runner.task import Task, parse_tasks


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "state.db",
        "logs_dir": tmp_path / "logs",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _task(**overrides) -> Task:
    defaults: dict = {
        "id": "TASK-001",
        "name": "t",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
    }
    defaults.update(overrides)
    return Task(**defaults)


class TestVerifyFirstResolvesPerTaskUnderAnyProjectDefault:
    def test_resolves_to_verify_first_under_a_standard_default_project(self, tmp_path):
        cfg = _cfg(tmp_path, execution_mode="standard")
        assert cfg.resolve_execution_mode(_task(execution_mode="verify_first")) == "verify_first"

    def test_resolves_to_verify_first_under_a_tdd_default_project(self, tmp_path):
        cfg = _cfg(tmp_path, execution_mode="tdd")
        assert cfg.resolve_execution_mode(_task(execution_mode="verify_first")) == "verify_first"

    def test_a_project_can_default_to_verify_first_itself(self, tmp_path):
        cfg = _cfg(tmp_path, execution_mode="verify_first")
        assert cfg.resolve_execution_mode(_task()) == "verify_first"

    def test_a_sibling_task_without_the_mode_line_still_gets_the_project_default(self, tmp_path):
        """The declaration is per-task; a neighbour that says nothing is
        unaffected by it."""
        cfg = _cfg(tmp_path, execution_mode="tdd")
        assert cfg.resolve_execution_mode(_task(execution_mode=None)) == "tdd"

    def test_the_mode_line_is_parsed_from_the_tasks_file(self, tmp_path):
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n**Mode:** verify_first\nEst: 1d\n"
            "\n### TASK-002: t\n\U0001f7e0 P1 | ⬜ TODO\nEst: 1d\n"
        )
        tasks = parse_tasks(path)

        assert tasks[0].execution_mode == "verify_first"
        assert tasks[1].execution_mode is None

    def test_an_unknown_mode_is_still_refused(self, tmp_path):
        """`verify_first` joining the set does not loosen the refusal for an
        actual typo."""
        with pytest.raises(ConfigError):
            _cfg(tmp_path).resolve_execution_mode(_task(execution_mode="verify-frist"))
