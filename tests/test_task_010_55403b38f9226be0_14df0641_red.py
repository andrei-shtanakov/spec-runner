"""RED for BEH-26 (#367 milestone 2, TASK-010).

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-26
`checked_by`: kind=integration, owner=qa, target=tests/test_verify_claims.py

FR-19 asks that a `verify_first` task's declared group stays byte-locked for
the duration of the task, the same guarantee a `tdd` red already gets
(`claims.py::record_claims`, called from `tdd.py::_judge_red_commit`). Today
that call site is the *only* one: a green-on-entry `verify_first` task (BEH-20)
never authors a red and so never claims anything, and `gates.py::evaluate_claims`
says so plainly — "`check_claims` finds nothing, since freezing a verify-first
group's files is FR-19/TASK-010, not yet wired".

This drives a real `execute_task()` -> real `post_done_hook()` for a
green-on-entry `verify_first` task whose paid implementation pass rewrites the
declared group's evidential test file — weakening its assertion while keeping
it green, so the live re-verify of the candidate commit
(`hooks._reverify_before_review` / `_reverify_live_evidence_for_candidate`)
has nothing to object to either; only a byte-lock claim can catch this. Until
TASK-010 wires `record_claims` into the green `verify_first` path, nothing
freezes the file and the task reaches DONE anyway.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from spec_runner import tdd
from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.runner import CliInvocation
from spec_runner.state import ExecutorState
from spec_runner.task import Task


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


def _cfg(root: Path) -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        test_command="python -m pytest",
        max_retries=1,
        retry_delay_seconds=0,
        create_git_branch=False,
        run_tests_on_done=False,
        auto_commit=True,
        run_review=False,
        callback_url="",
        lint_command="",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


class TestBEH26DeclaredGroupIsFrozenForTheDurationOfTheTask:
    """kind: integration — a green-on-entry verify-first task's own paid pass
    rewrites the evidential test the declared group named, keeping it green.
    The claims gate must catch the rewrite and the task must not reach DONE."""

    def test_a_rewritten_but_still_green_group_file_blocks_the_task(self, tmp_path, monkeypatch):
        root = tmp_path / "repo"
        root.mkdir()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "t")
        (root / "tests").mkdir()
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert 2 + 2 == 4\n")
        (root / "spec").mkdir()
        (root / "spec" / "tasks.md").write_text(
            "# Tasks\n\n### TASK-101: verify-first task\nP1 | TODO Est: 1h\n"
        )
        (root / "spec" / ".gitignore").write_text(".executor-*\n")
        _commit(root, "base")

        red_agent = MagicMock(
            side_effect=AssertionError("BEH-26: no red authoring for a green group")
        )
        monkeypatch.setattr(tdd, "_run_agent", red_agent)

        def _rewrite_the_group(config, invocation):
            # The paid implementation pass rewrites what the group's test
            # proves, but keeps it green — a live re-verify of the candidate
            # commit would find nothing wrong with it either. Only the
            # byte-lock a claim provides can catch this.
            path = Path(config.project_root) / "tests" / "test_group.py"
            path.write_text("def test_it():\n    assert True\n")
            return MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)

        task = Task(
            id="TASK-101",
            name="verify-first task",
            priority="p1",
            status="todo",
            estimate="1h",
            execution_mode="verify_first",
            verifies=["tests/test_group.py::test_it"],
        )
        config = _cfg(root)

        with (
            patch("spec_runner.execution.update_task_status"),
            patch("spec_runner.execution.log_progress"),
            patch(
                "spec_runner.execution.build_cli_invocation",
                return_value=CliInvocation(["echo", "hi"], "text"),
            ),
            patch("spec_runner.execution.build_task_prompt", return_value="test prompt"),
            patch("spec_runner.execution.pre_start_hook", return_value=True),
            patch(
                "spec_runner.execution._run_agent_process",
                side_effect=_rewrite_the_group,
            ),
            ExecutorState(config) as state,
        ):
            result = execute_task(task, config, state)

        red_agent.assert_not_called()
        assert result is not True, (
            "BEH-26: a paid pass that rewrites the declared group's "
            "evidential test — even while keeping it green — must be caught "
            "by the claims gate before the terminal transition; the task "
            "must not reach DONE"
        )
