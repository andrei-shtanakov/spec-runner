"""TASK-009 (#367 milestone 1): a green-only verify-first task must reach
GREEN_IMPLEMENTING on its own evidence, not on a faked confirmed red.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-29
Traces: FR-21

kind: contract — BEH-29 Then: for a green-only task `has_confirmed_red` is
false, and the transition is legal on verify-evidence — a separate basis, not
a forged red. `lifecycle.advance` today only ever asks `has_confirmed_red`
before allowing (READY, GREEN_IMPLEMENTING); it has no notion of verify
evidence at all, so a green-only task is refused exactly like a `tdd` task
that skipped its red — the regression BEH-29 exists to rule out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.lifecycle import IllegalTransition, TddPhase, advance, has_confirmed_red
from spec_runner.live_verify import run_live_verify
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import resolve_namespace


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests").mkdir()
    (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _cfg(root: Path) -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        test_command="python -m pytest",
        execution_mode="verify_first",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task() -> Task:
    return Task(
        id="TASK-101",
        name="verify-first task",
        priority="p1",
        status="todo",
        estimate="1h",
        execution_mode="verify_first",
        verifies=["tests/test_group.py::test_it"],
    )


class TestGreenOnlyReachesImplementingOnVerifyEvidence:
    """kind: contract — BEH-29 Given a verify-first task that went green-only
    (a real live-verify run recorded as evidence, no red checkpoint ever
    taken)."""

    def test_a_green_only_task_advances_to_green_implementing_without_a_red(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        task = _task()
        namespace = resolve_namespace(cfg)

        result = run_live_verify(task, cfg)
        assert result.passed, result.detail

        with ExecutorState(cfg) as state:
            recorded = state.record_verify_evidence(task=task, config=cfg, result=result)
            assert recorded

            assert has_confirmed_red(state, namespace, task.id) is False, (
                "a green-only task has no red checkpoint at all — the legality "
                "of its transition must not run through has_confirmed_red"
            )

            try:
                phase = advance(state, namespace, task.id, TddPhase.GREEN_IMPLEMENTING)
            except IllegalTransition as exc:
                raise AssertionError(
                    "a green-only verify-first task with recorded verify "
                    "evidence was refused as if it had skipped its red "
                    f"entirely: {exc}"
                ) from None

            assert phase is TddPhase.GREEN_IMPLEMENTING
