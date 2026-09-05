"""RED for TASK-015 (spec-runner#341 / #334, BEH-24).

`Given` the two battle-scenario regressions this workstream shipped — #341's
lint-fixable red (TASK-014, `run_red_phase` + `tdd._run_agent`) and #334's
surviving-neighbour-file red (TASK-013, same seam) — `conftest._no_real_agent_calls`
already refuses a bare paid CLI name on that one seam, and both scenarios go
through it, so they are covered today.

But the harness guarantee this file pins is stated as a *suite-wide* property
("не может оплатить вызов по случайности"), not "the RED seam is guarded" —
and `execute_task`'s **standard** (non-tdd) pass reaches the agent through a
second, separate seam: a bare `subprocess.run(invocation.argv, ...)` in
`execution.py`, built straight from `config.claude_command`. Nothing patches
that seam the way `conftest.py` patches `tdd._run_agent`, so a test that
exercises the standard path — the shape TASK-016+'s doc/CHANGELOG work and any
future regression against #341/#334's *symptom* (an agent call slipping past
review of a task) would use — can reach a real, billed CLI invocation with no
guard in the way, exactly the failure mode this whole file exists to prevent
for the RED seam.

This red proves the gap safely: it stubs `execution.subprocess.run` to explode
rather than run anything (so the assertion below is provably the only thing
that can fail — no real process, whatever the outcome), then asks for the same
refusal `tdd._run_agent`'s guard already gives on the other seam. Today
`execute_task` catches that explosion in its own broad `except Exception`,
records a normal failed attempt, and returns `False` — no `AssertionError`,
no distinguishable "this was about to spend money" signal. So this fails on
`pytest.raises` seeing nothing raised, not on an import or a stray exception.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.runner import CliInvocation
from spec_runner.state import ExecutorState
from spec_runner.task import Task


def _config(tmp_path: Path) -> ExecutorConfig:
    return ExecutorConfig(
        project_root=tmp_path,
        state_file=tmp_path / "state.db",
        logs_dir=tmp_path / "logs",
        create_git_branch=False,
        run_tests_on_done=False,
        auto_commit=False,
        run_review=False,
        callback_url="",
        # Deliberately left at the default "claude" (bare paid CLI name) —
        # the case the tdd-seam guard already refuses on the other seam.
    )


class TestTheStandardExecutionSeamIsGuardedLikeTheRedSeam:
    @patch("spec_runner.execution.update_task_status")
    @patch("spec_runner.execution.build_task_prompt", return_value="prompt")
    @patch(
        "spec_runner.execution.build_cli_invocation",
        return_value=CliInvocation(["claude", "prompt"], "text"),
    )
    @patch("spec_runner.execution.pre_start_hook", return_value=True)
    @patch("spec_runner.execution.subprocess.run")
    def test_a_bare_agent_name_is_refused_on_the_standard_path_too(
        self, mock_run, mock_pre, mock_cmd, mock_prompt, mock_status, tmp_path
    ):
        def _explode(*_a, **_k):
            raise RuntimeError("nothing may be executed by this test")

        mock_run.side_effect = _explode

        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")
        config = _config(tmp_path)
        state = ExecutorState(config)

        with pytest.raises(AssertionError, match="would call the real agent"):
            execute_task(task, config, state)
