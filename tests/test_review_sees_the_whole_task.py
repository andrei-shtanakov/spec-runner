"""The review reads the whole task, not its last commit (TODO review-sees-only-last-commit).

TASK-002 of #480 passed `review_policy: required` in 23 s: review collected
`git diff HEAD~1`, the last commit was a bookkeeping flip of tasks.md, and
~3,900 lines in the commits before it were never read.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.review import (
    MAX_PROMPT_PATCH,
    build_review_prompt,
    task_base,
    task_diff,
)
from spec_runner.task import Task


def _git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _commit(root: Path, path: str, text: str, message: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    _git(root, "add", path)
    _git(root, "commit", "-qm", message)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "o@e.c")
    _git(root, "config", "user.name", "O")
    _commit(root, "spec/tasks.md", "### TASK-001: t\nP1 | TODO\n", "plan")
    _git(root, "switch", "-qc", "task/task-001")
    return root


def _cfg(root: Path, **overrides: object) -> ExecutorConfig:
    kwargs: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "main_branch": "main",
        "create_git_branch": True,
        "auto_commit": True,
    }
    kwargs.update(overrides)
    return ExecutorConfig(**kwargs)


def _task() -> Task:
    return Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")


def _flip_status(root: Path) -> None:
    _commit(root, "spec/tasks.md", "### TASK-001: t\nP1 | REVIEW\n", "bookkeeping")


class TestTheDiffStartsWhereTheTaskBegan:
    def test_every_commit_of_the_task_is_in_it(self, repo):
        """The #480 shape: work, more work, then a bookkeeping flip last."""
        _commit(repo, "src/a.py", "A = 1\n", "wip 1")
        _commit(repo, "src/b.py", "B = 2\n", "wip 2")
        _flip_status(repo)

        diff = task_diff(_cfg(repo))

        assert diff is not None
        assert diff.base == _git(repo, "merge-base", "HEAD", "main")
        assert sorted(diff.files or []) == ["spec/tasks.md", "src/a.py", "src/b.py"]
        assert "A = 1" in diff.patch and "B = 2" in diff.patch

    def test_the_reviewer_is_handed_the_earlier_commits(self, repo):
        """What the reviewer reads, not only what `task_diff` returns — on
        master's `HEAD~1` this prompt carried only the status flip."""
        _commit(repo, "src/a.py", "A_FROM_WIP = 1\n", "wip")
        _flip_status(repo)

        assert "A_FROM_WIP" in build_review_prompt(_task(), _cfg(repo))

    def test_main_merged_into_the_task_is_not_its_work(self, repo):
        _commit(repo, "src/a.py", "A = 1\n", "wip")
        _git(repo, "switch", "-q", "main")
        _commit(repo, "src/other.py", "O = 0\n", "someone else's work")
        _git(repo, "switch", "-q", "task/task-001")
        _git(repo, "merge", "-q", "--no-edit", "main")

        diff = task_diff(_cfg(repo))

        assert diff is not None and diff.files == ["src/a.py"]

    def test_the_base_follows_the_branch_the_task_merges_into(self, repo):
        """During an integration run `main_branch` names the run branch."""
        _git(repo, "switch", "-q", "main")
        _git(repo, "switch", "-qc", "spec-runner/run-1")
        _commit(repo, "src/earlier.py", "E = 1\n", "an earlier task of this run")
        _git(repo, "switch", "-qc", "task/task-002")
        _commit(repo, "src/mine.py", "M = 1\n", "this task")

        diff = task_diff(_cfg(repo, main_branch="spec-runner/run-1"))

        assert diff is not None and diff.files == ["src/mine.py"]

    def test_a_task_branch_with_no_commit_of_its_own_is_not_given_a_foreign_one(self, repo):
        """Review of #655: HEAD == merge-base on a task branch used to fall
        back to HEAD~1 — the previous main commit, presented as the task's."""
        _git(repo, "switch", "-q", "main")
        _commit(repo, "src/foreign.py", "F = 1\n", "someone else's commit on main")
        _git(repo, "switch", "-qc", "task/task-009")

        cfg = _cfg(repo)

        assert task_base(cfg) == "HEAD"
        assert "F = 1" not in build_review_prompt(_task(), cfg)

    def test_an_unknown_main_branch_falls_back_to_the_last_commit(self, repo):
        _commit(repo, "src/a.py", "A = 1\n", "work")
        cfg = _cfg(repo, main_branch="no-such-branch")

        assert task_base(cfg) == "HEAD~1"

    def test_a_project_template_is_given_the_base(self, repo):
        _commit(repo, "src/a.py", "A = 1\n", "work")
        cfg = _cfg(repo)
        cfg.prompts_dir.mkdir(parents=True, exist_ok=True)
        (cfg.prompts_dir / "review.md").write_text("BASE={{TASK_BASE}}")

        prompt = build_review_prompt(_task(), cfg)

        assert f"BASE={_git(repo, 'merge-base', 'HEAD', 'main')}" in prompt

    def test_a_path_with_a_space_stays_one_path(self, repo):
        _commit(repo, "docs/release notes.md", "x\n", "doc")

        diff = task_diff(_cfg(repo))

        assert diff is not None and diff.files == ["docs/release notes.md"]

    def test_work_committed_on_main_itself_falls_back_to_the_last_commit(self, repo):
        _git(repo, "switch", "-q", "main")
        _commit(repo, "src/a.py", "A = 1\n", "work on main")

        assert task_base(_cfg(repo)) == "HEAD~1"


class TestANoOpTaskIsStillReviewable:
    """Acceptance review of #655: a #97 no-op task (its work absorbed by an
    earlier task) changes only the task file. The review runs on that diff as
    it always did — refusing it as "nothing to review" would block such a
    task forever under `review_policy: required`."""

    def test_the_diff_is_the_bookkeeping_and_the_prompt_is_built(self, repo):
        _flip_status(repo)
        cfg = _cfg(repo)

        diff = task_diff(cfg)

        assert diff is not None and diff.files == ["spec/tasks.md"]
        assert "spec/tasks.md" in build_review_prompt(_task(), cfg)


def test_a_truncated_patch_names_the_base_to_read_the_rest(repo):
    _commit(repo, "src/big.py", "X = 1\n" * (MAX_PROMPT_PATCH // 4), "big")

    prompt = build_review_prompt(_task(), _cfg(repo))
    base = _git(repo, "merge-base", "HEAD", "main")

    assert f"read the rest with `git diff {base}`" in prompt
    assert f"Base of the task: `{base}`" in prompt
