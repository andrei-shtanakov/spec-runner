"""BEH-32 (#367 TASK-012): external contracts change only additively.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-32
Traces: FR-04

`--json-result` (`build_task_json_result`, `cli.py`) has no way yet to tell a
Maestro-side consumer that a task's completion rests on a live verify-first
run rather than a standard/tdd one. This checkpoint fails because the field
does not exist yet — nothing here touches `schemas/json-result.schema.json`
or `cli.py`, so a task that never recorded verify-evidence stays byte-for-byte
today's shape (the existing golden fixtures in
`tests/fixtures/maestro-interop/` are untouched by this checkpoint).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from spec_runner.cli import build_task_json_result
from spec_runner.config import ExecutorConfig
from spec_runner.live_verify import VerifyRunResult
from spec_runner.state import ExecutorState, ReviewVerdict, TaskAttempt
from spec_runner.task import Task


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _head(root: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("x")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


class TestJsonResultExposesVerifyOutcomeAdditively:
    """kind: contract — BEH-32: a task with recorded verify-evidence
    surfaces its outcome as an additive `verify_outcome` field in
    `--json-result`, so a Maestro-side consumer can tell a verify-first
    completion from a standard/tdd one without disturbing the existing
    field set for tasks that never recorded evidence."""

    def test_recorded_green_verify_evidence_surfaces_as_verify_outcome(
        self, tmp_path: Path
    ) -> None:
        root = _repo(tmp_path)
        config = ExecutorConfig(
            project_root=root,
            state_file=root / ".executor-state.db",
            logs_dir=root / ".executor-logs",
        )
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        task = Task(
            id="TASK-101",
            name="verify-first task",
            priority="p1",
            status="todo",
            estimate="1h",
            execution_mode="verify_first",
            verifies=["tests/test_group.py::test_it"],
        )

        state = ExecutorState(config)
        ts = state.get_task_state(task.id)
        ts.status = "success"
        ts.attempts.append(
            TaskAttempt(
                timestamp="2026-09-07T10:00:00",
                success=True,
                duration_seconds=1.0,
                review_status=ReviewVerdict.PASSED.value,
            )
        )

        result = VerifyRunResult(
            sha=_head(root),
            ran=True,
            passed=True,
            detail="ok",
            group_executed=tuple(task.verifies or ()),
            adapter="pytest",
        )
        recorded = state.record_verify_evidence(task=task, config=config, result=result)
        assert recorded, "setup: verify evidence must be recorded for this test to mean anything"

        entry = build_task_json_result(task.id, state)

        assert entry.get("verify_outcome") == "green", (
            "BEH-32: a task with recorded verify-evidence should surface its "
            "outcome ('green'/'test_failure'/'instrument_error') as an "
            "additive `verify_outcome` field in --json-result, so Maestro "
            "can tell a verify-first completion from a standard/tdd one "
            f"without widening any existing required field. got: {entry!r}"
        )
