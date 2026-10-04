"""The harness snapshot must precede the RED and verify-first passes.

The guard's baseline was captured lazily, right before the GREEN call — after
`_run_verify_first_phase` and `_run_red_phase_gate` had already run. The RED
authoring agent writes into the same tree, and `_commit_red` stages the whole
tree, so an edit it made to `pyproject.toml` was committed with the red and
became the baseline the guard compared against: under `harness_guard: strict`
the oracle was rewritten and nothing was refused. The same class as #137 — an
edit made before the snapshot is an edit the snapshot legalises.

The passes are stubbed here: what is under test is where the baseline is taken
and where it is compared, not how a red is authored.
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
        "# Spec\n\n## M0\n\n### TASK-031: add a test\n"
        "🔴 P0 | ⬜ TODO | Est: 0.5d\n\n"
        "**Description:** add a test\n\n**Checklist:**\n- [ ] do it\n\n"
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
        "execution_mode": "tdd",
        "max_retries": 2,
        "retry_delay_seconds": 0,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _task() -> Task:
    return Task(
        id="TASK-031",
        name="add a test",
        priority="p0",
        status="todo",
        description="add a test",
        estimate="0.5d",
    )


def _touch_pyproject(project: Path) -> None:
    (project / "pyproject.toml").write_text(PYPROJECT + "\n[tool.pytest.ini_options]\n")


@pytest.fixture
def isolate(monkeypatch, project: Path):
    """No git, no gates, no hooks; a GREEN agent that touches nothing."""
    import subprocess as sp

    from spec_runner import execution

    monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
    monkeypatch.setattr(
        execution, "post_done_hook", lambda *a, **k: (True, None, "skipped", "", False)
    )
    green_calls: list[int] = []

    def _green(*args, **kwargs):
        green_calls.append(1)
        return sp.CompletedProcess(args=["x"], returncode=0, stdout="TASK_COMPLETE\n", stderr="")

    monkeypatch.setattr(execution, "_run_agent_process", _green)
    return execution, green_calls


def _red_that_touches(project: Path):
    def _red(task, config, state, reporter):
        _touch_pyproject(project)
        return None  # the gate is satisfied: a red was "confirmed"

    return _red


class TestRedPassIsUnderTheGuard:
    def test_red_agent_editing_pyproject_fails_under_strict(self, project, isolate, monkeypatch):
        from spec_runner.execution import run_with_retries

        execution, green_calls = isolate
        monkeypatch.setattr(execution, "_run_red_phase_gate", _red_that_touches(project))

        cfg = _cfg(project)
        with ExecutorState(cfg) as state:
            result = run_with_retries(_task(), cfg, state)
            attempts = state.get_task_state("TASK-031").attempts

        assert result is not True, (
            "the RED pass rewrote pyproject.toml and the task passed — the edit "
            "was taken into the guard's baseline"
        )
        assert attempts and all(not a.success for a in attempts)
        assert all(a.error_kind == "harness_guard" for a in attempts)
        assert all("pyproject.toml" in (a.error or "") for a in attempts)
        assert green_calls == [], "the paid GREEN call ran for an attempt already refused"
        # `_commit_red` committed the edit; under `create_git_branch: false`
        # the next task's baseline would read it as the oracle.
        assert (project / "pyproject.toml").read_text() == PYPROJECT, (
            "the refused RED edit outlived the refusal"
        )

    def test_green_reverting_the_red_edit_on_retry_passes(self, project, isolate, monkeypatch):
        """The edit survives into attempt 2 with the red; the GREEN agent there
        is told to revert it and must be allowed to."""
        import subprocess as sp

        from spec_runner.execution import run_with_retries

        execution, green_calls = isolate
        reds: list[int] = []

        def _red(task, config, state, reporter):
            reds.append(1)
            if len(reds) == 1:
                _touch_pyproject(project)
            return None

        def _green(*args, **kwargs):
            green_calls.append(1)
            (project / "pyproject.toml").write_text(PYPROJECT)
            return sp.CompletedProcess(["x"], 0, "TASK_COMPLETE\n", "")

        monkeypatch.setattr(execution, "_run_red_phase_gate", _red)
        monkeypatch.setattr(execution, "_run_agent_process", _green)
        cfg = _cfg(project)
        with ExecutorState(cfg) as state:
            result = run_with_retries(_task(), cfg, state)

        assert result is True
        assert green_calls == [1]

    def test_warn_reports_and_proceeds(self, project, isolate, monkeypatch):
        from spec_runner.execution import run_with_retries

        execution, green_calls = isolate
        monkeypatch.setattr(execution, "_run_red_phase_gate", _red_that_touches(project))

        cfg = _cfg(project, harness_guard="warn")
        with ExecutorState(cfg) as state:
            result = run_with_retries(_task(), cfg, state)

        assert result is True
        assert green_calls == [1]

    def test_red_pass_that_touches_nothing_still_passes(self, project, isolate, monkeypatch):
        from spec_runner.execution import run_with_retries

        execution, green_calls = isolate
        monkeypatch.setattr(execution, "_run_red_phase_gate", lambda *a, **k: None)

        cfg = _cfg(project)
        with ExecutorState(cfg) as state:
            result = run_with_retries(_task(), cfg, state)

        assert result is True
        assert green_calls == [1]

    def test_pre_start_changes_are_still_not_violations(self, project, isolate, monkeypatch):
        """The baseline moves earlier but stays after `uv sync`."""
        from spec_runner.execution import run_with_retries

        execution, green_calls = isolate

        def _hook_that_syncs(*args, **kwargs):
            _touch_pyproject(project)
            return True

        monkeypatch.setattr(execution, "pre_start_hook", _hook_that_syncs)
        monkeypatch.setattr(execution, "_run_red_phase_gate", lambda *a, **k: None)

        cfg = _cfg(project)
        with ExecutorState(cfg) as state:
            result = run_with_retries(_task(), cfg, state)

        assert result is True


class TestVerifyFirstPassIsUnderTheGuard:
    def test_verify_first_pass_editing_pyproject_fails_under_strict(
        self, project, isolate, monkeypatch
    ):
        from spec_runner.execution import run_with_retries

        execution, green_calls = isolate

        def _verify_first(task, config, state, reporter):
            _touch_pyproject(project)
            return None, None

        monkeypatch.setattr(execution, "_run_verify_first_phase", _verify_first)

        cfg = _cfg(project, execution_mode="verify_first")
        with ExecutorState(cfg) as state:
            result = run_with_retries(_task(), cfg, state)
            attempts = state.get_task_state("TASK-031").attempts

        assert result is not True
        assert attempts and all(a.error_kind == "harness_guard" for a in attempts)
        assert green_calls == []
        assert (project / "pyproject.toml").read_text() == PYPROJECT
