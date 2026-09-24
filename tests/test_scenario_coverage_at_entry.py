"""#402 §5/§8: an uncovered declared scenario refuses at the live entry run —
terminal, before any paid call, on the commit and not the working tree.

Spec: docs/superpowers/specs/2026-09-23-verify-scenario-coverage-design.md
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runner import tdd
from spec_runner.config import ExecutorConfig
from spec_runner.execution import run_with_retries
from spec_runner.executor import execute_task
from spec_runner.runner import CliInvocation
from spec_runner.state import ExecutorState
from spec_runner.task import Task

FOREIGN = 'def test_it():\n    """kind: e2e — BEH-03 (another workstream)"""\n    assert True\n'
OWN = 'def test_it():\n    """kind: e2e — BEH-09"""\n    assert True\n'
FAILING = 'def test_it():\n    """kind: e2e — BEH-07"""\n    assert False\n'


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests" / "test_group.py").write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _cfg(root: Path, max_retries: int = 1) -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        test_command="python -m pytest",
        max_retries=max_retries,
        retry_delay_seconds=0,
        create_git_branch=False,
        run_tests_on_done=False,
        auto_commit=True,
        run_review=False,
        callback_url="",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(scenarios: list[str] | None) -> Task:
    return Task(
        id="TASK-001",
        name="t",
        priority="p1",
        status="todo",
        estimate="1h",
        execution_mode="verify_first",
        verifies=["tests/test_group.py::test_it"],
        scenarios=scenarios,
    )


@pytest.fixture
def paid(monkeypatch):
    """Both paid seams, spied: the implementation call and RED authoring."""
    red_calls: list[str] = []

    def _spy(config, prompt, **kwargs):
        red_calls.append(prompt)
        return tdd.AgentCall(text="TDD_SELECTOR: tests/test_group.py::test_it")

    monkeypatch.setattr(tdd, "_run_agent", _spy)
    impl = MagicMock(return_value=MagicMock(stdout="TASK_COMPLETE", stderr="", returncode=0))
    with (
        patch("spec_runner.execution._run_agent_process", impl),
        patch("spec_runner.execution.update_task_status"),
        patch("spec_runner.execution.log_progress"),
        patch(
            "spec_runner.execution.build_cli_invocation",
            return_value=CliInvocation(["echo", "hi"], "text"),
        ),
        patch("spec_runner.execution.build_task_prompt", return_value="p"),
        patch(
            "spec_runner.execution.post_done_hook",
            return_value=(True, None, "skipped", "", False),
        ),
        patch("spec_runner.execution.pre_start_hook", return_value=True),
    ):
        yield impl, red_calls


class TestForeignGreenGroupIsRefused:
    """kind: e2e — the issue's shape: an existing, green, foreign file."""

    def test_refused_terminally_before_any_paid_call(self, tmp_path, paid):
        impl, red_calls = paid
        cfg = _cfg(_repo(tmp_path, FOREIGN))
        state = ExecutorState(cfg)

        assert execute_task(_task(["BEH-09"]), cfg, state) == "TERMINAL_REFUSAL"

        impl.assert_not_called()
        assert red_calls == []
        error = state.get_task_state("TASK-001").last_error or ""
        assert "BEH-09" in error and "tests/test_group.py" in error
        assert state.task_cost("TASK-001") == 0.0

    def test_not_retried(self, tmp_path, paid):
        cfg = _cfg(_repo(tmp_path, FOREIGN), max_retries=3)
        state = ExecutorState(cfg)

        assert run_with_retries(_task(["BEH-09"]), cfg, state) is False
        assert state.get_task_state("TASK-001").attempt_count == 1


class TestCoveredGroupProceeds:
    def test_label_in_docstring_proceeds(self, tmp_path, paid):
        impl, _ = paid
        cfg = _cfg(_repo(tmp_path, OWN))
        assert execute_task(_task(["BEH-09"]), cfg, ExecutorState(cfg)) is not False
        impl.assert_called()

    def test_partial_coverage_names_only_the_missing(self, tmp_path, paid):
        cfg = _cfg(_repo(tmp_path, OWN))
        state = ExecutorState(cfg)
        assert execute_task(_task(["BEH-09", "BEH-10"]), cfg, state) == "TERMINAL_REFUSAL"
        error = state.get_task_state("TASK-001").last_error or ""
        assert "BEH-10" in error
        assert "BEH-09" not in error.split("uncovered")[1].split("(searched")[0]

    def test_no_scenarios_line_is_todays_path(self, tmp_path, paid):
        impl, _ = paid
        cfg = _cfg(_repo(tmp_path, FOREIGN))
        assert execute_task(_task(None), cfg, ExecutorState(cfg)) is not False
        impl.assert_called()


class TestCommitNotWorkingTree:
    def test_uncommitted_label_does_not_count(self, tmp_path, paid):
        impl, _ = paid
        root = _repo(tmp_path, FOREIGN)
        (root / "tests" / "test_group.py").write_text(OWN)  # not committed
        cfg = _cfg(root)
        assert execute_task(_task(["BEH-09"]), cfg, ExecutorState(cfg)) == "TERMINAL_REFUSAL"
        impl.assert_not_called()


class TestRedEntryRunIsCheckedToo:  # Review Focus 4
    def test_test_failure_entry_refused_before_red_authoring(self, tmp_path, paid):
        impl, red_calls = paid
        cfg = _cfg(_repo(tmp_path, FAILING))
        state = ExecutorState(cfg)
        assert execute_task(_task(["BEH-09"]), cfg, state) == "TERMINAL_REFUSAL"
        assert red_calls == []
        impl.assert_not_called()
