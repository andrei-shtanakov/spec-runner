"""The harness guard must survive a retry (#137, Critical).

`harness_guard: strict` is documented as fail-closed: the oracle surface (test
config, dependency manifests, CI workflows) must be unchanged by the task being
verified. It snapshotted that surface *inside each attempt*, so a forbidden edit
that survived a failed attempt became the next attempt's baseline and was
legalised. The barrier held exactly once and was disarmed by persistence.

Observed in production (disputatio, 2026-08-10, TASK-022 — a task required to
edit `pyproject.toml`, which is in the default candidate set):

    attempt 1 → FAIL "the agent modified verification files: modified pyproject.toml"
    git status → ` M pyproject.toml`      (edit still in the working tree)
    attempt 2 → snapshot taken from the already-modified file → PASS
    task closed, edit merged

The agent was not gaming the policy and never touched `harness_allow`; one
retry was enough. With the default `max_retries: 3`, the guarantee "the oracle
surface is immutable" simply did not hold.
"""

from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task

PYPROJECT = "[project]\nname = 'demo'\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "tasks.md").write_text(
        "# Spec\n\n## M0\n\n### TASK-022: register entrypoint\n"
        "🔴 P0 | ⬜ TODO | Est: 0.5d\n\n"
        "**Description:** edit pyproject.toml\n\n**Checklist:**\n- [ ] do it\n\n"
        "**Traces to:** [REQ-0]\n**Depends on:** —\n"
    )
    (tmp_path / "logs").mkdir()
    return tmp_path


def _cfg(project: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "state_file": project / "state.db",
        "project_root": project,
        "logs_dir": project / "logs",
        "create_git_branch": False,
        "auto_commit": False,
        "run_tests_on_done": False,
        "run_review": False,
        "harness_guard": "strict",
        "max_retries": 3,
        "retry_delay_seconds": 0,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _task() -> Task:
    return Task(
        id="TASK-022",
        name="register entrypoint",
        priority="p0",
        status="todo",
        description="edit pyproject.toml",
        estimate="0.5d",
    )


def _fake_cli(
    project: Path,
    mutate_on: set[int],
    calls: list[int],
    fail_on: set[int] | None = None,
):
    """Agent stub: edits pyproject.toml on `mutate_on` attempts, reports
    TASK_FAILED on `fail_on` attempts and TASK_COMPLETE otherwise."""
    import subprocess as sp

    fail_on = fail_on or set()

    def _run(*args, **kwargs):
        n = len(calls) + 1
        calls.append(n)
        if n in mutate_on:
            (project / "pyproject.toml").write_text(PYPROJECT + f"\n# touched on attempt {n}\n")
        marker = "TASK_FAILED: nope" if n in fail_on else "TASK_COMPLETE: done"
        return sp.CompletedProcess(args=["x"], returncode=0, stdout=f"{marker}\n", stderr="")

    return _run


@pytest.fixture
def isolate(monkeypatch, project: Path):
    """Neutralise everything except the guard: no git, no gates, no hooks."""
    from spec_runner import execution

    monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
    # (hook_success, hook_error, review_status, review_findings, hook_no_op)
    monkeypatch.setattr(
        execution, "post_done_hook", lambda *a, **k: (True, None, "skipped", "", False)
    )
    return execution


class TestSnapshotSurvivesRetry:
    def test_forbidden_edit_is_not_legalised_by_a_retry(self, project, isolate, monkeypatch):
        """Attempt 1 mutates the harness; attempts 2-3 touch nothing.

        The mutation is still present in the working tree, so every attempt
        must fail. Before the fix, attempt 2 re-snapshotted the mutated file
        and passed.
        """
        from spec_runner.execution import run_with_retries

        calls: list[int] = []
        monkeypatch.setattr(
            isolate, "_run_agent_process", _fake_cli(project, mutate_on={1}, calls=calls)
        )

        cfg = _cfg(project)
        with ExecutorState(cfg) as state:
            result = run_with_retries(_task(), cfg, state)
            attempts = state.get_task_state("TASK-022").attempts

        assert result is not True, (
            "the task passed on a retry with the harness still mutated — "
            "the guard was disarmed by persistence (#137)"
        )
        assert len(calls) == 3, "all retries should have been spent, each blocked by the guard"
        assert all(not a.success for a in attempts)
        assert all("pyproject.toml" in (a.error or "") for a in attempts), (
            f"later attempts stopped naming the violation: {[a.error for a in attempts]}"
        )

    def test_clean_agent_still_passes(self, project, isolate, monkeypatch):
        """Guard against over-correction: an untouched harness passes attempt 1."""
        from spec_runner.execution import run_with_retries

        calls: list[int] = []
        monkeypatch.setattr(
            isolate, "_run_agent_process", _fake_cli(project, mutate_on=set(), calls=calls)
        )

        cfg = _cfg(project)
        with ExecutorState(cfg) as state:
            result = run_with_retries(_task(), cfg, state)

        assert result is True
        assert len(calls) == 1

    def test_mutation_on_a_later_attempt_is_caught(self, project, isolate, monkeypatch):
        """A first-clean, later-dirty agent is caught: the baseline is the
        task's starting state, not the previous attempt's end state."""
        from spec_runner.execution import run_with_retries

        calls: list[int] = []
        monkeypatch.setattr(
            isolate,
            "_run_agent_process",
            _fake_cli(project, mutate_on={2}, calls=calls, fail_on={1}),
        )

        cfg = _cfg(project)
        with ExecutorState(cfg) as state:
            result = run_with_retries(_task(), cfg, state)
            attempts = state.get_task_state("TASK-022").attempts

        assert result is not True
        assert "pyproject.toml" in (attempts[1].error or ""), (
            "the attempt that mutated the harness was not blocked"
        )


class TestBaselineCapturedAfterPreStart:
    def test_pre_start_hook_changes_are_not_violations(self, project, isolate, monkeypatch):
        """`uv sync` in pre_start legitimately rewrites uv.lock/pyproject.

        The snapshot is taken after the hook, and that ordering must survive
        the move out of the per-attempt path — otherwise dependency sync
        becomes a guard violation on every run.
        """
        from spec_runner import execution
        from spec_runner.execution import run_with_retries

        def _hook_that_syncs(*args, **kwargs):
            (project / "pyproject.toml").write_text(PYPROJECT + "\n# uv sync\n")
            return True

        monkeypatch.setattr(execution, "pre_start_hook", _hook_that_syncs)
        calls: list[int] = []
        monkeypatch.setattr(
            execution, "_run_agent_process", _fake_cli(project, mutate_on=set(), calls=calls)
        )

        cfg = _cfg(project)
        with ExecutorState(cfg) as state:
            result = run_with_retries(_task(), cfg, state)

        assert result is True, "pre_start_hook's own edit was counted as an agent violation"


class TestHarnessBaselineHelper:
    def test_capture_is_idempotent(self, project):
        from spec_runner.harness import HarnessBaseline

        cfg = _cfg(project)
        baseline = HarnessBaseline()
        first = baseline.capture(cfg)
        assert first is not None and "pyproject.toml" in first

        (project / "pyproject.toml").write_text(PYPROJECT + "\n# changed\n")
        assert baseline.capture(cfg) == first, "re-capture must not follow the file"

    def test_guard_off_captures_nothing(self, project):
        from spec_runner.harness import HarnessBaseline

        cfg = _cfg(project, harness_guard="off")
        baseline = HarnessBaseline()
        assert baseline.capture(cfg) is None
