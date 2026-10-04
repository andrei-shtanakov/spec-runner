"""A GREEN call killed by its timeout still leaves a trace (#295/#296 invariant).

Observed on #480 TASK-002 (run `1f1a0932`): the 60-minute GREEN call that
timed out left no `agent_calls` row and a prompt artefact that ended with the
prompt — the shape the invariant reserves for "the runner died mid-call". The
hour of work was invisible to the budget.

Measured 2026-10-04 before the fix: the ledger row is there now — the
paid-call seam that writes it is TASK-002's own work (`0cce4c6`, committed
after the timed-out run) — but the artefact was still left open.

Only `paid_call._spawn` is replaced, so the call goes through the production
seam: ledger row, artefact, attempt row.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner import paid_call
from spec_runner.config import ExecutorConfig
from spec_runner.state import ErrorCode, ExecutorState
from spec_runner.task import Task


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "tasks.md").write_text(
        "# Spec\n\n## M0\n\n### TASK-040: slow work\n"
        "🔴 P0 | ⬜ TODO | Est: 0.5d\n\n"
        "**Description:** slow work\n\n**Checklist:**\n- [ ] do it\n\n"
        "**Traces to:** [REQ-0]\n**Depends on:** —\n"
    )
    (tmp_path / "logs").mkdir()
    return tmp_path


def _cfg(project: Path) -> ExecutorConfig:
    return ExecutorConfig(
        state_file=project / "state.db",
        project_root=project,
        logs_dir=project / "logs",
        create_git_branch=False,
        auto_commit=False,
        run_tests_on_done=False,
        run_review=False,
        task_timeout_minutes=1,
        max_retries=1,
    )


def _task() -> Task:
    return Task(
        id="TASK-040",
        name="slow work",
        priority="p0",
        status="todo",
        description="slow work",
        estimate="0.5d",
    )


@pytest.fixture
def timed_out(monkeypatch, project: Path):
    from spec_runner import execution

    monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
    spawned: list[int] = []

    def _spawn(invocation, *, timeout, cwd, env):
        spawned.append(1)
        raise subprocess.TimeoutExpired(invocation.argv, timeout)

    monkeypatch.setattr(paid_call, "_spawn", _spawn)
    cfg = _cfg(project)
    with ExecutorState(cfg) as state:
        result = execution.execute_task(_task(), cfg, state)
        attempts = state.get_task_state("TASK-040").attempts
        calls = state.agent_calls("TASK-040")
    assert spawned == [1], "the fixture must reach the process"
    return cfg, result, attempts, calls


def test_the_attempt_is_a_timeout(timed_out):
    _, result, attempts, _ = timed_out
    assert result is False
    assert [a.error_code for a in attempts] == [ErrorCode.TIMEOUT]


def test_the_ledger_has_a_row_with_an_unknown_price(timed_out):
    _, _, _, calls = timed_out
    green = [c for c in calls if c["provenance"] == "green"]
    assert len(green) == 1, f"the timed-out GREEN call left no ledger row: {calls}"
    assert green[0]["cost_usd"] is None, "an unreported price is unknown, never zero"


def test_the_prompt_artefact_says_why_it_is_empty(timed_out):
    cfg, _, _, _ = timed_out
    logs = sorted(Path(cfg.logs_dir).glob("TASK-040-green-*.log"))
    assert len(logs) == 1, list(Path(cfg.logs_dir).iterdir())
    body = logs[0].read_text()
    assert "=== NO RESULT: " in body, (
        "the artefact ends with the prompt — indistinguishable from a runner "
        f"that died mid-call:\n{body[-400:]}"
    )
    assert "timed out" in body.split("=== NO RESULT: ", 1)[1]
