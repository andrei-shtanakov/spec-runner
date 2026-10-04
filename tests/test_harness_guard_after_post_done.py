"""The harness guard must see what is written after the GREEN check.

`harness_violations` ran once, around the GREEN call. Everything that writes
into the tree later in `post_done_hook` — a reviewer that answers
`REVIEW_FIXED` after editing, a `post_review` plugin (allowed to write
committable evidence, #307) — reached the DONE flip and the commit without
being compared with anything, so under `harness_guard: strict` a reviewer
could rewrite `pyproject.toml` and the task still closed.

Each of those steps is now judged by itself: a snapshot right before it, a
comparison right after it. Not against the task's baseline — the harness's
own writes in between (a repo-wide `lint_fix_command`) are not the
reviewer's, and blaming them made a task unrecoverable.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner import hooks
from spec_runner.config import ExecutorConfig
from spec_runner.state import ReviewVerdict
from spec_runner.task import Task, parse_tasks

PYPROJECT = "[project]\nname = 'demo'\n"
CONFTEST = "import pytest\n"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    (tmp_path / "conftest.py").write_text(CONFTEST)
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
        (project / "app.py").write_text("x = 2\n")
        if touches:
            _touch_pyproject(project)
        return ReviewVerdict.FIXED, None, "REVIEW_FIXED"

    return _review


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


class TestReviewerEdits:
    def test_reviewer_fixing_pyproject_fails_under_strict(self, project, monkeypatch):
        monkeypatch.setattr(hooks, "run_code_review", _reviewer(project, touches=True))
        cfg = _cfg(project)

        ok, error, *_ = hooks.post_done_hook(_task(), cfg, True)

        assert ok is False, "a reviewer rewrote pyproject.toml and the task still passed"
        assert "Harness guard: the reviewer" in (error or "")
        assert "pyproject.toml" in (error or "")
        assert _status(cfg) != "done"

    def test_warn_reports_and_proceeds(self, project, monkeypatch):
        monkeypatch.setattr(hooks, "run_code_review", _reviewer(project, touches=True))
        cfg = _cfg(project, harness_guard="warn")

        ok, *_ = hooks.post_done_hook(_task(), cfg, True)

        assert ok is True
        assert _status(cfg) == "done"

    def test_reviewer_fixing_only_code_still_passes(self, project, monkeypatch):
        monkeypatch.setattr(hooks, "run_code_review", _reviewer(project, touches=False))
        cfg = _cfg(project)

        ok, *_ = hooks.post_done_hook(_task(), cfg, True)

        assert ok is True
        assert _status(cfg) == "done"

    def test_the_harness_own_lint_fix_is_not_the_reviewers(self, project, monkeypatch):
        """`post_done_hook` runs `lint_fix_command` over the whole tree before
        review. What it rewrites — a root `conftest.py` here — is the harness's
        own write, and must not be blamed on the reviewer (or anyone)."""
        monkeypatch.setattr(hooks, "run_code_review", _reviewer(project, touches=False))
        cfg = _cfg(
            project,
            run_lint_on_done=True,
            lint_command="test -f .linted",
            lint_fix_command="touch .linted && echo '# fixed' >> conftest.py",
        )

        ok, error, *_ = hooks.post_done_hook(_task(), cfg, True)

        assert (project / "conftest.py").read_text() != CONFTEST, "the fixture must fix"
        assert ok is True, error
        assert _status(cfg) == "done"

    @staticmethod
    def _repo(project: Path) -> None:
        _git(project, "init", "-q")
        _git(project, "config", "user.email", "t@e.c")
        _git(project, "config", "user.name", "T")
        _git(project, "add", "-A")
        _git(project, "commit", "-qm", "base")

    @staticmethod
    def _real_reviewer(project: Path, monkeypatch, *, touches: bool) -> None:
        """The production `run_code_review`, which commits a FIXED verdict's
        edits itself; only the reviewer subprocess is replaced."""
        from spec_runner import review

        def _call(*args, **kwargs):
            (project / "app.py").write_text("x = 2\n")
            if touches:
                _touch_pyproject(project)
            return review.ReviewCall(text="REVIEW_FIXED\n", stderr="", returncode=0, cost_usd=None)

        monkeypatch.setattr(review, "_run_reviewer", _call)

    @pytest.mark.parametrize("parallel", [False, True])
    def test_a_refused_reviewer_edit_is_neither_committed_nor_left(
        self, project, monkeypatch, parallel
    ):
        """The review call commits FIXED edits itself. Under `strict` it must
        not commit an oracle edit, and the refusal must undo it in the tree:
        under `create_git_branch: false` the next task's baseline would
        otherwise read it as the oracle."""
        self._repo(project)
        self._real_reviewer(project, monkeypatch, touches=True)
        cfg = _cfg(project, auto_commit=True, review_parallel=parallel)

        ok, error, *_ = hooks.post_done_hook(_task(), cfg, True)

        assert ok is False
        assert "Harness guard: the reviewer" in (error or "")
        assert _git(project, "show", "HEAD:pyproject.toml") == PYPROJECT
        assert (project / "pyproject.toml").read_text() == PYPROJECT
        log = _git(project, "log", "--format=%s")
        assert "review fixes" not in log

    @pytest.mark.parametrize("parallel", [False, True])
    def test_a_clean_reviewer_fix_is_still_committed(self, project, monkeypatch, parallel):
        self._repo(project)
        self._real_reviewer(project, monkeypatch, touches=False)
        cfg = _cfg(project, auto_commit=True, review_parallel=parallel)

        ok, error, *_ = hooks.post_done_hook(_task(), cfg, True)

        assert ok is True, error
        assert "review fixes" in _git(project, "log", "--format=%s")

    def test_warn_still_commits_the_reviewer_edit(self, project, monkeypatch):
        self._repo(project)
        self._real_reviewer(project, monkeypatch, touches=True)
        cfg = _cfg(project, auto_commit=True, harness_guard="warn")

        ok, *_ = hooks.post_done_hook(_task(), cfg, True)

        assert ok is True
        assert _git(project, "show", "HEAD:pyproject.toml") != PYPROJECT


class TestPostReviewPluginEdits:
    def test_a_plugin_blocking_for_its_own_reason_still_has_its_edit_undone(
        self, project, monkeypatch
    ):
        real = hooks.run_plugin_hooks_for

        def _plugins(point, task, config, **kwargs):
            if point == "post_review":
                _touch_pyproject(project)
                return "exporter failed"
            return real(point, task, config, **kwargs)

        monkeypatch.setattr(hooks, "run_plugin_hooks_for", _plugins)
        cfg = _cfg(project, run_review=False)

        ok, error, *_ = hooks.post_done_hook(_task(), cfg, True)

        assert ok is False
        assert "exporter failed" in (error or "")
        assert "Harness guard: a post_review plugin" in (error or "")
        assert (project / "pyproject.toml").read_text() == PYPROJECT

    def test_plugin_writing_pyproject_fails_under_strict(self, project, monkeypatch):
        real = hooks.run_plugin_hooks_for

        def _plugins(point, task, config, **kwargs):
            if point == "post_review":
                _touch_pyproject(project)
                return None
            return real(point, task, config, **kwargs)

        monkeypatch.setattr(hooks, "run_plugin_hooks_for", _plugins)
        cfg = _cfg(project, run_review=False)

        ok, error, *_ = hooks.post_done_hook(_task(), cfg, True)

        assert ok is False, "a post_review plugin rewrote pyproject.toml and the task passed"
        assert "Harness guard: a post_review plugin" in (error or "")
        assert _status(cfg) != "done"
        assert (project / "pyproject.toml").read_text() == PYPROJECT, (
            "the refused plugin edit was left in the tree for the next task's baseline"
        )
