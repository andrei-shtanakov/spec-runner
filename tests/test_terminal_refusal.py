"""A deliberate escalation is not a transient failure (#140).

Retry policy could not tell "I could not do this" from "this cannot be done
within the rules, an operator is needed". Both produced `TASK_FAILED` and both
got the full `max_retries` cycle.

Observed on disputatio (2026-08-10): TASK-016 hit a conflict between two
byte-locked tests and the agent behaved exactly as the project constitution
prescribes — it refused to edit an assertion to get green, named the reason,
and stopped. The harness then started attempt 2 of 3 with "Do not repeat the
same mistake", although the only non-erroneous path (release the claim, start a
new red cycle) is forbidden to the agent by the harness itself. Attempts 2 and
3 were structurally doomed. The same shape repeated on TASK-021 and TASK-025 —
and on TASK-025 attempt 2 crossed a scope boundary that attempt 1 had correctly
escalated about. The barrier held once and was removed by a retry, the same
mechanism as the harness-guard bypass (#137).

`TASK_BLOCKED: <reason>` gives the agent a way to say the difference.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.execution import classify_retry_strategy, run_with_retries
from spec_runner.state import ErrorCode, ExecutorState
from spec_runner.task import Task

REASON = "implementation is complete, operator must release the claim for a new red cycle"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "tasks.md").write_text(
        "# Spec\n\n## M0\n\n### TASK-016: Locked tests\n"
        "🔴 P0 | ⬜ TODO | Est: 0.5d\n\n"
        "**Description:** x\n\n**Checklist:**\n- [ ] do it\n\n"
        "**Traces to:** [REQ-1]\n**Depends on:** —\n"
    )
    (tmp_path / "logs").mkdir()
    return tmp_path


def _cfg(project: Path, **overrides):
    from spec_runner.config import ExecutorConfig

    defaults: dict = {
        "state_file": project / "state.db",
        "project_root": project,
        "logs_dir": project / "logs",
        "create_git_branch": False,
        "auto_commit": False,
        "run_tests_on_done": False,
        "run_review": False,
        "max_retries": 3,
        "retry_delay_seconds": 0,
        "harness_guard": "off",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _task() -> Task:
    return Task(id="TASK-016", name="Locked tests", priority="p0", status="todo", estimate="0.5d")


@pytest.fixture
def isolate(monkeypatch):
    from spec_runner import execution

    monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
    monkeypatch.setattr(
        execution, "post_done_hook", lambda *a, **k: (True, None, "skipped", "", False)
    )
    return execution


def _agent_saying(marker: str, calls: list[int]):
    def _run(*a, **k):
        calls.append(1)
        return subprocess.CompletedProcess(args=["x"], returncode=0, stdout=marker, stderr="")

    return _run


class TestTerminalRefusalIsNotRetried:
    def test_task_blocked_spends_exactly_one_attempt(self, project, isolate, monkeypatch):
        calls: list[int] = []
        monkeypatch.setattr(
            isolate.subprocess, "run", _agent_saying(f"TASK_BLOCKED: {REASON}\n", calls)
        )

        cfg = _cfg(project)
        with ExecutorState(cfg) as state:
            result = run_with_retries(_task(), cfg, state)

        assert result is not True
        assert len(calls) == 1, (
            f"a deliberate escalation was retried {len(calls)} times — retries spend "
            "time and tokens on the knowingly impossible, and push the agent to "
            "work around a rule it just honoured (#140)"
        )

    def test_reason_is_preserved_verbatim(self, project, isolate, monkeypatch):
        calls: list[int] = []
        monkeypatch.setattr(
            isolate.subprocess, "run", _agent_saying(f"TASK_BLOCKED: {REASON}\n", calls)
        )

        cfg = _cfg(project)
        with ExecutorState(cfg) as state:
            run_with_retries(_task(), cfg, state)
            attempt = state.get_task_state("TASK-016").attempts[-1]

        assert attempt.error_code == ErrorCode.TASK_BLOCKED
        assert REASON in (attempt.error or ""), (
            "the operator needs the agent's own words to act on the escalation"
        )

    def test_plain_task_failed_still_retries(self, project, isolate, monkeypatch):
        """Guard against over-correction: a transient failure keeps its retries."""
        calls: list[int] = []
        monkeypatch.setattr(
            isolate.subprocess, "run", _agent_saying("TASK_FAILED: flaky network\n", calls)
        )

        cfg = _cfg(project)
        with ExecutorState(cfg) as state:
            run_with_retries(_task(), cfg, state)

        assert len(calls) == 3

    def test_blocked_wins_when_both_markers_appear(self, project, isolate, monkeypatch):
        """Fail closed on the stronger claim: an agent that says both has
        stated a reason a retry cannot resolve."""
        calls: list[int] = []
        monkeypatch.setattr(
            isolate.subprocess,
            "run",
            _agent_saying(f"TASK_FAILED: tried\nTASK_BLOCKED: {REASON}\n", calls),
        )

        cfg = _cfg(project)
        with ExecutorState(cfg) as state:
            run_with_retries(_task(), cfg, state)
            attempt = state.get_task_state("TASK-016").attempts[-1]

        assert len(calls) == 1
        assert attempt.error_code == ErrorCode.TASK_BLOCKED

    def test_blocked_never_counts_as_success(self, project, isolate, monkeypatch):
        """`TASK_COMPLETE` alongside a block must not close the task."""
        calls: list[int] = []
        monkeypatch.setattr(
            isolate.subprocess,
            "run",
            _agent_saying(f"TASK_COMPLETE\nTASK_BLOCKED: {REASON}\n", calls),
        )

        cfg = _cfg(project)
        with ExecutorState(cfg) as state:
            result = run_with_retries(_task(), cfg, state)
            assert state.get_task_state("TASK-016").status != "success"
        assert result is not True


class TestRetryClassification:
    def test_task_blocked_is_fatal(self):
        assert classify_retry_strategy(ErrorCode.TASK_BLOCKED) == "fatal"

    def test_task_failed_is_not_fatal(self):
        assert classify_retry_strategy(ErrorCode.TASK_FAILED) != "fatal"


class TestPromptTeachesTheMarker:
    """An agent cannot use a marker nobody told it about."""

    def test_builtin_prompt_documents_task_blocked(self, project):
        """The built-in prompt, with no project template in the way.

        No monkeypatching needed since #153: templates are resolved from
        `config.prompts_dir`, and this fixture's project has none. Before that
        fix this test picked up *this repository's* template, because
        `PROMPTS_DIR` was relative to the process CWD.
        """
        from spec_runner import prompt as prompt_mod

        text = prompt_mod.build_task_prompt(_task(), _cfg(project))
        assert "TASK_BLOCKED" in text
        assert "TASK_FAILED" in text
