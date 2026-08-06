"""Commit provenance with review enabled (#103, battle-testing F-23).

The review stage commits its own fixes. With nothing committed before it,
that commit swept the ENTIRE exec-stage feature under a "code review fixes"
label, while the final task commit got only the tasks.md leftovers — git
history inverted relative to content. The exec-stage work must be committed
under the task label BEFORE review runs.
"""

import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

from spec_runner.config import ExecutorConfig
from spec_runner.hooks import post_done_hook
from spec_runner.state import ReviewVerdict
from spec_runner.task import Task

TASKS_MD = """\
# Tasks

### TASK-001: Demo feature
\U0001f7e0 P1 | 🔄 IN_PROGRESS | Est: 1h

**Checklist:**
- [ ] Build the feature
"""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=root, check=False)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "spec").mkdir()
    (root / "spec" / "tasks.md").write_text(TASKS_MD)
    (root / "README.md").write_text("hello\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")


def _task() -> Task:
    return Task(
        id="TASK-001",
        name="Demo feature",
        priority="p1",
        status="in_progress",
        estimate="1h",
        description="",
        checklist=[("Build the feature", False)],
    )


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "create_git_branch": False,
        "auto_commit": True,
        "run_tests_on_done": False,
        "run_lint_on_done": False,
        "run_review": True,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _commits(root: Path) -> list[tuple[str, list[str]]]:
    """(subject, files) per commit, newest first, excluding the init commit."""
    shas = _git(root, "log", "--format=%H", "HEAD").stdout.split()[:-1]
    out = []
    for sha in shas:
        subject = _git(root, "log", "-1", "--format=%s", sha).stdout.strip()
        files = _git(root, "show", "--name-only", "--format=", sha).stdout.split()
        out.append((subject, files))
    return out


class TestExecWorkCommittedBeforeReview:
    def test_feature_in_task_commit_not_review_commit(self, tmp_path):
        """The exact F-23 scenario, with a real review CLI returning
        REVIEW_FIXED after editing a file: the feature must sit in the
        task-labelled commit, the review commit only in its own delta."""
        _init_repo(tmp_path)
        (tmp_path / "feature.py").write_text("f = 1\n")  # exec-stage work
        (tmp_path / "spec" / ".executor-logs").mkdir()  # review log target

        review_cli = tmp_path.parent / f"{tmp_path.name}-review.sh"
        review_cli.write_text(
            "#!/usr/bin/env bash\necho 'review_fix' > review_fix.py\necho REVIEW_FIXED\n"
        )
        review_cli.chmod(review_cli.stat().st_mode | stat.S_IEXEC)

        cfg = _cfg(
            tmp_path,
            review_command=str(review_cli),
            review_command_template="{cmd} -p {prompt}",
        )
        ok, err, review_status, _, _ = post_done_hook(_task(), cfg, True)
        assert ok is True, err
        assert review_status == ReviewVerdict.FIXED.value

        commits = _commits(tmp_path)
        review_commits = [(s, f) for s, f in commits if "review fixes" in s]
        task_commits = [(s, f) for s, f in commits if s.startswith("TASK-001: Demo feature")]
        assert review_commits, commits
        assert task_commits, commits
        # Review commit carries the review delta (plus its own tasks.md
        # bookkeeping — the #66 REVIEW status flip), but never the feature
        assert "review_fix.py" in review_commits[0][1], review_commits
        assert "feature.py" not in review_commits[0][1], review_commits
        # The feature belongs to a task-labelled commit
        assert any("feature.py" in files for _, files in task_commits), task_commits

    def test_feature_committed_even_when_review_passes(self, tmp_path):
        _init_repo(tmp_path)
        (tmp_path / "feature.py").write_text("f = 1\n")
        cfg = _cfg(tmp_path)
        with patch(
            "spec_runner.hooks.run_code_review",
            return_value=(ReviewVerdict.PASSED, None, "ok"),
        ):
            ok, err, _, _, _ = post_done_hook(_task(), cfg, True)
        assert ok is True, err
        commits = _commits(tmp_path)
        first_task_commit = commits[-1]  # oldest after init = pre-review commit
        assert first_task_commit[0].startswith("TASK-001: Demo feature")
        assert "feature.py" in first_task_commit[1]

    def test_no_pre_review_commit_when_review_off(self, tmp_path):
        """Review disabled → single task commit as before (no empty extras)."""
        _init_repo(tmp_path)
        (tmp_path / "feature.py").write_text("f = 1\n")
        cfg = _cfg(tmp_path, run_review=False)
        ok, err, _, _, _ = post_done_hook(_task(), cfg, True)
        assert ok is True, err
        commits = _commits(tmp_path)
        assert len(commits) == 1, commits
        assert "feature.py" in commits[0][1]


class TestCommitTaskWork:
    """Copilot review on PR #105: staging failures and message hygiene."""

    def test_staging_failure_returns_failed_not_empty(self, tmp_path):
        """A git add failure must not masquerade as 'nothing to commit' —
        that would flow into a false no-op verdict."""
        from spec_runner.hooks import commit_task_work

        _init_repo(tmp_path)
        (tmp_path / "feature.py").write_text("f = 1\n")
        cfg = _cfg(tmp_path)
        with patch(
            "spec_runner.hooks.stage_all_except_runtime",
            side_effect=RuntimeError("git add -A failed: index locked"),
        ):
            assert commit_task_work(_task(), cfg) == "failed"

    def test_no_empty_completed_section(self, tmp_path):
        """Unchecked checklist (the pre-review commit case) must not emit a
        dangling 'Completed:' header."""
        from spec_runner.hooks import commit_task_work

        _init_repo(tmp_path)
        (tmp_path / "feature.py").write_text("f = 1\n")
        cfg = _cfg(tmp_path)
        assert commit_task_work(_task(), cfg) == "committed"
        body = _git(tmp_path, "log", "-1", "--format=%B").stdout
        assert "Completed:" not in body

    def test_checked_items_and_milestone_formatting(self, tmp_path):
        from spec_runner.hooks import commit_task_work

        _init_repo(tmp_path)
        (tmp_path / "feature.py").write_text("f = 1\n")
        cfg = _cfg(tmp_path)
        task = _task()
        task.checklist = [("Build the feature", True), ("Skip me", False)]
        task.milestone = "M1"
        assert commit_task_work(task, cfg) == "committed"
        body = _git(tmp_path, "log", "-1", "--format=%B").stdout
        assert "Completed:\n  - Build the feature" in body
        assert "Skip me" not in body
        assert "Milestone: M1" in body
        assert "\n\n\n" not in body  # no stray blank lines


class TestNoOpInterplay:
    def test_task_with_work_not_flagged_noop(self, tmp_path):
        """Pre-review commit captures the work; the final commit stage
        finding only bookkeeping must NOT flag no_op (#97 regression)."""
        _init_repo(tmp_path)
        (tmp_path / "feature.py").write_text("f = 1\n")
        cfg = _cfg(tmp_path)
        with patch(
            "spec_runner.hooks.run_code_review",
            return_value=(ReviewVerdict.PASSED, None, "ok"),
        ):
            ok, err, _, _, no_op = post_done_hook(_task(), cfg, True)
        assert ok is True, err
        assert no_op is False

    def test_true_noop_still_flagged_with_review_on(self, tmp_path):
        # maestro-style repo: spec/ excluded BEFORE anything is tracked,
        # so the DONE flip is invisible to git
        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "config", "user.email", "test@example.com")
        _git(tmp_path, "config", "user.name", "Test")
        (tmp_path / ".git" / "info" / "exclude").write_text("spec/\n")
        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / "tasks.md").write_text(TASKS_MD)
        (tmp_path / "README.md").write_text("hello\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", "init")
        cfg = _cfg(tmp_path)
        with patch(
            "spec_runner.hooks.run_code_review",
            return_value=(ReviewVerdict.PASSED, None, "ok"),
        ):
            ok, err, _, _, no_op = post_done_hook(_task(), cfg, True)
        assert ok is True, err
        assert no_op is True
