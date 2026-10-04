"""The harness guard must see what is written after the GREEN check.

`harness_violations` ran once, around the GREEN call. Everything that writes
into the tree later in `post_done_hook` — a reviewer that answers
`REVIEW_FIXED` after editing, a `post_review` plugin (allowed to write
committable evidence, #307) — reached the DONE flip and the commit without
being compared with the task's baseline, so under `harness_guard: strict` a
reviewer could rewrite `pyproject.toml` and the task still closed.

The surface is now compared once more right before the DONE flip, against the
same task baseline `execute_task` took.
"""

from pathlib import Path

import pytest

from spec_runner import hooks
from spec_runner.config import ExecutorConfig
from spec_runner.harness import snapshot_harness
from spec_runner.state import ReviewVerdict
from spec_runner.task import Task, parse_tasks

PYPROJECT = "[project]\nname = 'demo'\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "tasks.md").write_text(
        "# Spec\n\n## M0\n\n### TASK-032: change code\n"
        "🔴 P0 | 🔄 IN_PROGRESS | Est: 0.5d\n\n"
        "**Description:** change code\n\n**Checklist:**\n- [ ] do it\n\n"
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
        "run_lint_on_done": False,
        "run_review": True,
        "harness_guard": "strict",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _task() -> Task:
    return Task(
        id="TASK-032",
        name="change code",
        priority="p0",
        status="in_progress",
        description="change code",
        estimate="0.5d",
    )


def _touch_pyproject(project: Path) -> None:
    (project / "pyproject.toml").write_text(PYPROJECT + "\n[tool.pytest.ini_options]\n")


def _status(cfg: ExecutorConfig) -> str:
    return next(t.status for t in parse_tasks(cfg.tasks_file) if t.id == "TASK-032")


def _reviewer(project: Path, *, touches: bool):
    def _review(task, config, **kwargs):
        if touches:
            _touch_pyproject(project)
        return ReviewVerdict.FIXED, None, "REVIEW_FIXED"

    return _review


def _post_done(cfg: ExecutorConfig):
    return hooks.post_done_hook(_task(), cfg, True, harness_before=snapshot_harness(cfg))


class TestReviewerEdits:
    def test_reviewer_fixing_pyproject_fails_under_strict(self, project, monkeypatch):
        monkeypatch.setattr(hooks, "run_code_review", _reviewer(project, touches=True))
        cfg = _cfg(project)

        ok, error, *_ = _post_done(cfg)

        assert ok is False, "a reviewer rewrote pyproject.toml and the task still passed"
        assert "Harness guard" in (error or "") and "pyproject.toml" in (error or "")
        assert _status(cfg) != "done"

    def test_warn_reports_and_proceeds(self, project, monkeypatch):
        monkeypatch.setattr(hooks, "run_code_review", _reviewer(project, touches=True))
        cfg = _cfg(project, harness_guard="warn")

        ok, *_ = _post_done(cfg)

        assert ok is True
        assert _status(cfg) == "done"

    def test_reviewer_fixing_only_code_still_passes(self, project, monkeypatch):
        monkeypatch.setattr(hooks, "run_code_review", _reviewer(project, touches=False))
        cfg = _cfg(project)

        ok, *_ = _post_done(cfg)

        assert ok is True
        assert _status(cfg) == "done"


class TestPostReviewPluginEdits:
    def test_plugin_writing_pyproject_fails_under_strict(self, project, monkeypatch):
        real = hooks.run_plugin_hooks_for

        def _plugins(point, task, config, **kwargs):
            if point == "post_review":
                _touch_pyproject(project)
                return None
            return real(point, task, config, **kwargs)

        monkeypatch.setattr(hooks, "run_plugin_hooks_for", _plugins)
        cfg = _cfg(project, run_review=False)

        ok, error, *_ = _post_done(cfg)

        assert ok is False, "a post_review plugin rewrote pyproject.toml and the task passed"
        assert "pyproject.toml" in (error or "")
        assert _status(cfg) != "done"


class TestExecutionHandsItsBaseline:
    def test_execute_task_passes_the_task_baseline(self, project, monkeypatch):
        """The check is only as good as the baseline it receives."""
        import subprocess as sp

        from spec_runner import execution
        from spec_runner.execution import run_with_retries
        from spec_runner.state import ExecutorState

        seen: list[object] = []

        def _spy(*args, **kwargs):
            seen.append(kwargs.get("harness_before"))
            return True, None, "skipped", "", False

        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
        monkeypatch.setattr(execution, "post_done_hook", _spy)
        monkeypatch.setattr(
            execution,
            "_run_agent_process",
            lambda *a, **k: sp.CompletedProcess(["x"], 0, "TASK_COMPLETE\n", ""),
        )
        cfg = _cfg(project, run_review=False, max_retries=1)
        with ExecutorState(cfg) as state:
            assert run_with_retries(_task(), cfg, state) is True

        assert seen and isinstance(seen[0], dict) and "pyproject.toml" in seen[0]
