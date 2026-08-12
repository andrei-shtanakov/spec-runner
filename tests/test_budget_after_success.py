"""#219: a task that succeeded is not un-finished by the budget running out.

The post-attempt budget check ran *before* the success branch, so a task that
finished, committed, merged and deleted its branch was recorded as a
`BUDGET_EXCEEDED` failure and flipped `done → blocked` in tasks.md. Three
things followed, and the third is the expensive one:

- the state DB and tasks.md disagreed about a task that plainly succeeded;
- one task was reported as both `completed=1` and `failed=1`;
- `resolve_dependencies` promotes `blocked` → `todo`, so the **next run could
  re-execute already-merged work** — paying an agent to author a red against a
  feature that is already implemented.

The last one is why the required test here is about a *second* run, not about a
status string.

Found by the free budget rehearsal for #213; pre-existing, and made common by
#216 now that review spend is counted at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from spec_runner.config import ExecutorConfig
from spec_runner.execution import run_with_retries
from spec_runner.state import ErrorCode, ExecutorState
from spec_runner.task import Task, get_next_tasks, parse_tasks, resolve_dependencies

TASKS_MD = """# Tasks

### TASK-001: Deterministic thing
🟠 P1 | ⬜ TODO | Est: 1d

### TASK-002: The next thing
🟠 P1 | ⬜ TODO | Est: 1d
"""


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "spec").mkdir(parents=True)
    for args in (
        ("init", "-q"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "t"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    (root / "spec" / "tasks.md").write_text(TASKS_MD)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True, capture_output=True)
    return root


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / "spec" / ".state.db",
        "logs_dir": root / "spec" / ".logs",
        "task_budget_usd": 1.0,
        "create_git_branch": False,
        "auto_commit": False,
        "run_tests_on_done": False,
        "run_lint_on_done": False,
        "run_review": False,
        "max_retries": 2,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(task_id: str = "TASK-001") -> Task:
    return Task(id=task_id, name="Deterministic thing", priority="p1", status="todo", estimate="1d")


def _succeed_expensively(cost: float = 1.50):
    """An attempt that finishes the work and exhausts the cap doing it."""

    def _fake(task, config, state, harness_baseline=None):
        state.record_attempt(task.id, True, 1.0, cost_usd=cost)
        from spec_runner.task import update_task_status

        update_task_status(config.tasks_file, task.id, "done")
        return True  # `record_attempt(success=True)` already set the DB status

    return _fake


def _meta_line(cfg: ExecutorConfig, task_id: str) -> str:
    lines = cfg.tasks_file.read_text().splitlines()
    header = next(i for i, ln in enumerate(lines) if ln.startswith(f"### {task_id}:"))
    return lines[header + 1]


class TestTheTaskStaysDone:
    def test_success_is_not_converted_into_a_budget_failure(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))

        with ExecutorState(cfg) as state:
            with patch("spec_runner.execution.execute_task", _succeed_expensively()):
                result = run_with_retries(_task(), cfg, state)
            attempts = state.tasks["TASK-001"].attempts
            status = state.tasks["TASK-001"].status

        assert result is True
        assert status == "success"
        assert [a.error_code for a in attempts] == [None]
        assert not any(a.error_code == ErrorCode.BUDGET_EXCEEDED for a in attempts)

    def test_tasks_md_still_says_done(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))

        with ExecutorState(cfg) as state:
            with patch("spec_runner.execution.execute_task", _succeed_expensively()):
                run_with_retries(_task(), cfg, state)

        assert "DONE" in _meta_line(cfg, "TASK-001")
        assert "BLOCKED" not in _meta_line(cfg, "TASK-001")


class TestTheSecondRunDoesNotRedoIt:
    """The clause that costs money if it regresses."""

    def test_the_finished_task_is_not_selected_again(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))

        with ExecutorState(cfg) as state:
            with patch("spec_runner.execution.execute_task", _succeed_expensively()):
                run_with_retries(_task(), cfg, state)

        # A fresh read of tasks.md, exactly as the next run does it — including
        # the dependency resolution that promotes `blocked` back to `todo`.
        tasks = resolve_dependencies(parse_tasks(cfg.tasks_file))
        ready = get_next_tasks(tasks)

        assert "TASK-001" not in [t.id for t in ready]
        assert "TASK-002" in [t.id for t in ready]

    def test_no_second_agent_call_for_it(self, tmp_path):
        """The property directly: running again does not re-execute the task.

        Asserted through `execute_task` rather than through a status, because
        the status is only evidence — the money is spent by the call.
        """
        cfg = _cfg(_repo(tmp_path))

        with ExecutorState(cfg) as state:
            with patch("spec_runner.execution.execute_task", _succeed_expensively()):
                run_with_retries(_task(), cfg, state)

            tasks = resolve_dependencies(parse_tasks(cfg.tasks_file))
            again = [t for t in get_next_tasks(tasks) if t.id == "TASK-001"]
            with patch("spec_runner.execution.execute_task") as executed:
                for task in again:
                    run_with_retries(task, cfg, state)

        executed.assert_not_called()


class TestTheRunStillStops:
    """Removing the lie must not remove the halt it was propping up."""

    def test_the_state_reports_a_budget_stop(self, tmp_path):
        cfg = _cfg(_repo(tmp_path), budget_usd=1.0)

        with ExecutorState(cfg) as state:
            with patch("spec_runner.execution.execute_task", _succeed_expensively()):
                run_with_retries(_task(), cfg, state)

            assert state.should_stop() is True
            reason, detail = state.stop_cause()

        assert reason == "budget_exceeded"
        assert "1.50" in detail

    def test_the_loop_stops_after_a_successful_task(self, tmp_path):
        """`cli` used to ask this only when the task had *failed*.

        With the failure removed, a stop condition keyed on failure would never
        fire and the run would carry on spending against an exhausted cap —
        the opposite of the fix. Driven through `_run_tasks` rather than
        asserted against the source text: what matters is that TASK-002 is
        never executed, not how the branch is written.
        """
        import argparse

        from spec_runner.cli import _run_tasks

        cfg = _cfg(_repo(tmp_path), budget_usd=1.0)
        executed: list[str] = []

        def _spend(task, config, state, harness_baseline=None):
            executed.append(task.id)
            return _succeed_expensively()(task, config, state, harness_baseline)

        args = argparse.Namespace(
            command="run",
            all=True,
            no_reset_failed=False,
            force=True,
            task=None,
            milestone=None,
            restart=False,
            dry_run=False,
            json_result=False,
            max_retries=None,
            timeout=None,
            no_tests=False,
            no_branch=False,
            no_commit=False,
            no_review=False,
            hitl_review=False,
            callback_url="",
            tui=False,
        )
        with (
            patch("spec_runner.cli.execute_task", _spend),
            patch("spec_runner.execution.execute_task", _spend),
            patch("spec_runner.cli.sys.exit"),
        ):
            _run_tasks(args, cfg)

        assert executed == ["TASK-001"], executed


class TestAFailedAttemptIsStillCapped:
    def test_a_failure_over_budget_is_still_recorded_as_such(self, tmp_path):
        """The check keeps its original job for the case it was written for."""
        cfg = _cfg(_repo(tmp_path))

        def _fail_expensively(task, config, state, harness_baseline=None):
            state.record_attempt(task.id, False, 1.0, cost_usd=1.50, error="nope")
            return False

        with ExecutorState(cfg) as state:
            with patch("spec_runner.execution.execute_task", _fail_expensively):
                result = run_with_retries(_task(), cfg, state)
            attempts = state.tasks["TASK-001"].attempts

        assert result is False
        assert any(a.error_code == ErrorCode.BUDGET_EXCEEDED for a in attempts)
        assert "BLOCKED" in _meta_line(cfg, "TASK-001")
