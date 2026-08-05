"""Runtime state must never be committed (#62; root cause of #67 state loss)."""

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.git_ops import (
    RUNTIME_GITIGNORE_ENTRIES,
    ensure_runtime_gitignore,
    runtime_state_paths,
    stage_all_except_runtime,
)
from spec_runner.hooks import post_done_hook
from spec_runner.task import Task


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=root, check=False)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("hello\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")


def _write_runtime_state(root: Path) -> None:
    spec = root / "spec"
    spec.mkdir(exist_ok=True)
    (spec / ".executor-state.db").write_bytes(b"sqlite")
    (spec / ".executor-state.db-wal").write_bytes(b"wal")
    (spec / ".executor-state.db-shm").write_bytes(b"shm")
    (spec / ".executor-state.lock").write_text("PID: 1\n")
    (spec / ".executor-progress.txt").write_text("progress\n")
    (spec / ".task-history.log").write_text("history\n")
    logs = spec / ".executor-logs"
    logs.mkdir(exist_ok=True)
    (logs / "TASK-001.log").write_text("log\n")


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {"project_root": root, "create_git_branch": False}
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _staged_files(root: Path) -> set[str]:
    out = _git(root, "diff", "--cached", "--name-only").stdout
    return set(out.split())


class TestRuntimeStatePaths:
    def test_covers_all_runtime_files(self, tmp_path):
        cfg = _cfg(tmp_path)
        rels = {
            str(p.relative_to(tmp_path))
            for p in runtime_state_paths(cfg)
            if p.is_relative_to(tmp_path)
        }
        for expected in [
            "spec/.executor-state.db",
            "spec/.executor-state.db-wal",
            "spec/.executor-state.db-shm",
            "spec/.executor-state.lock",
            "spec/.executor-logs",
            "spec/.executor-progress.txt",
            "spec/.task-history.log",
        ]:
            assert expected in rels, f"{expected} missing from runtime_state_paths"


class TestStageAllExceptRuntime:
    def test_stages_code_not_runtime(self, tmp_path):
        _init_repo(tmp_path)
        _write_runtime_state(tmp_path)
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "new_module.py").write_text("x = 1\n")
        (tmp_path / "spec" / "tasks.md").write_text("# tasks\n")

        assert stage_all_except_runtime(_cfg(tmp_path)) is True
        staged = _staged_files(tmp_path)
        assert "lib/new_module.py" in staged
        assert "spec/tasks.md" in staged
        assert not any(".executor" in f or "task-history" in f for f in staged), staged

    def test_returns_false_when_only_runtime_changed(self, tmp_path):
        _init_repo(tmp_path)
        _write_runtime_state(tmp_path)
        assert stage_all_except_runtime(_cfg(tmp_path)) is False

    def test_untracks_previously_committed_state(self, tmp_path):
        """A repo contaminated by the old behavior gets repaired."""
        _init_repo(tmp_path)
        _write_runtime_state(tmp_path)
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", "contaminated")
        (tmp_path / "code.py").write_text("y = 2\n")

        assert stage_all_except_runtime(_cfg(tmp_path)) is True
        _git(tmp_path, "commit", "-q", "-m", "repair")
        tracked = _git(tmp_path, "ls-files").stdout
        assert ".executor-state.db" not in tracked
        assert "code.py" in tracked

    def test_works_in_fresh_repo_without_commits(self, tmp_path):
        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "config", "user.email", "t@e.c")
        _git(tmp_path, "config", "user.name", "T")
        _write_runtime_state(tmp_path)
        (tmp_path / "first.py").write_text("z = 3\n")

        assert stage_all_except_runtime(_cfg(tmp_path)) is True
        staged = _staged_files(tmp_path)
        assert "first.py" in staged
        assert not any(".executor" in f for f in staged), staged


class TestEnsureRuntimeGitignore:
    def test_creates_gitignore(self, tmp_path):
        (tmp_path / "spec").mkdir()
        ensure_runtime_gitignore(_cfg(tmp_path))
        content = (tmp_path / "spec" / ".gitignore").read_text()
        for entry in RUNTIME_GITIGNORE_ENTRIES:
            assert entry in content

    def test_idempotent(self, tmp_path):
        (tmp_path / "spec").mkdir()
        cfg = _cfg(tmp_path)
        ensure_runtime_gitignore(cfg)
        first = (tmp_path / "spec" / ".gitignore").read_text()
        ensure_runtime_gitignore(cfg)
        assert (tmp_path / "spec" / ".gitignore").read_text() == first

    def test_preserves_existing_entries(self, tmp_path):
        (tmp_path / "spec").mkdir()
        (tmp_path / "spec" / ".gitignore").write_text("custom-entry\n")
        ensure_runtime_gitignore(_cfg(tmp_path))
        content = (tmp_path / "spec" / ".gitignore").read_text()
        assert "custom-entry" in content
        assert ".executor-*" in content

    def test_noop_without_spec_dir(self, tmp_path):
        ensure_runtime_gitignore(_cfg(tmp_path))
        assert not (tmp_path / "spec" / ".gitignore").exists()

    def test_gitignore_patterns_actually_ignore_runtime_files(self, tmp_path):
        """The entries must make git ignore every runtime file, incl. prefixed."""
        _init_repo(tmp_path)
        _write_runtime_state(tmp_path)
        spec = tmp_path / "spec"
        (spec / ".executor-phase2-state.db").write_bytes(b"x")
        (spec / ".phase2-task-history.log").write_text("x")
        (spec / ".spec.lock").write_text("x")
        changes = spec / "changes" / "add-x"
        changes.mkdir(parents=True)
        (changes / ".executor-state.db").write_bytes(b"x")

        ensure_runtime_gitignore(_cfg(tmp_path))
        _git(tmp_path, "add", "-A")
        staged = _staged_files(tmp_path)
        offenders = [
            f for f in staged if ".executor" in f or "task-history" in f or "spec.lock" in f
        ]
        assert offenders == [], offenders


class TestHarnessGitignoreNotCommitted:
    """#96: the harness-written spec/.gitignore must stay out of auto-commits.

    Maestro's ex-post scope gate flags any file no agent chose to create;
    committing our own .gitignore sent green workstreams to NEEDS_REVIEW.
    """

    def test_harness_created_gitignore_not_staged(self, tmp_path):
        _init_repo(tmp_path)
        (tmp_path / "spec").mkdir(exist_ok=True)
        ensure_runtime_gitignore(_cfg(tmp_path))
        (tmp_path / "code.py").write_text("x = 1\n")

        assert stage_all_except_runtime(_cfg(tmp_path)) is True
        staged = _staged_files(tmp_path)
        assert "code.py" in staged
        assert "spec/.gitignore" not in staged, staged

    def test_only_gitignore_created_counts_as_no_changes(self, tmp_path):
        """A no-op task whose sole diff is the harness file stays a no-op."""
        _init_repo(tmp_path)
        (tmp_path / "spec").mkdir(exist_ok=True)
        ensure_runtime_gitignore(_cfg(tmp_path))

        assert stage_all_except_runtime(_cfg(tmp_path)) is False

    def test_user_tracked_gitignore_still_staged(self, tmp_path):
        """A spec/.gitignore the user committed themselves keeps its old
        behavior: modifications travel with auto-commits."""
        _init_repo(tmp_path)
        (tmp_path / "spec").mkdir(exist_ok=True)
        (tmp_path / "spec" / ".gitignore").write_text("user-entry\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", "user tracks spec/.gitignore")

        ensure_runtime_gitignore(_cfg(tmp_path))  # appends runtime entries

        assert stage_all_except_runtime(_cfg(tmp_path)) is True
        assert "spec/.gitignore" in _staged_files(tmp_path)

    def test_user_tracked_gitignore_not_deleted(self, tmp_path):
        """The exclusion must never stage a deletion of a tracked file —
        that would be another scope escape."""
        _init_repo(tmp_path)
        (tmp_path / "spec").mkdir(exist_ok=True)
        (tmp_path / "spec" / ".gitignore").write_text("user-entry\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", "user tracks spec/.gitignore")
        (tmp_path / "code.py").write_text("x = 1\n")

        assert stage_all_except_runtime(_cfg(tmp_path)) is True
        _git(tmp_path, "commit", "-q", "-m", "task commit")
        tracked = _git(tmp_path, "ls-files").stdout
        assert "spec/.gitignore" in tracked

    def test_fresh_repo_without_commits(self, tmp_path):
        _git(tmp_path, "init", "-q", "-b", "main")
        _git(tmp_path, "config", "user.email", "t@e.c")
        _git(tmp_path, "config", "user.name", "T")
        (tmp_path / "spec").mkdir()
        ensure_runtime_gitignore(_cfg(tmp_path))
        (tmp_path / "first.py").write_text("z = 3\n")

        assert stage_all_except_runtime(_cfg(tmp_path)) is True
        staged = _staged_files(tmp_path)
        assert "first.py" in staged
        assert "spec/.gitignore" not in staged, staged

    def test_auto_commit_excludes_harness_gitignore(self, tmp_path):
        """End-to-end through post_done_hook: the first task's commit must
        not contain spec/.gitignore (the exact M-03/maestro#122 failure)."""
        _init_repo(tmp_path)
        (tmp_path / "spec").mkdir(exist_ok=True)
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "feature.py").write_text("f = 1\n")

        cfg = _cfg(
            tmp_path,
            auto_commit=True,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=False,
        )
        ensure_runtime_gitignore(cfg)  # what pre_start_hook does
        task = Task(
            id="TASK-001",
            name="demo",
            priority="p0",
            status="in_progress",
            estimate="",
            description="",
            checklist=[],
        )
        ok, err, _, _, _ = post_done_hook(task, cfg, True)
        assert ok is True, err

        committed = _git(tmp_path, "show", "--name-only", "--format=", "HEAD").stdout
        assert "lib/feature.py" in committed
        assert "spec/.gitignore" not in committed


class TestPostDoneHookCommit:
    def _task(self) -> Task:
        return Task(
            id="TASK-001",
            name="demo",
            priority="p0",
            status="in_progress",
            estimate="",
            description="",
            checklist=[],
        )

    def test_auto_commit_includes_code_excludes_state(self, tmp_path):
        _init_repo(tmp_path)
        _write_runtime_state(tmp_path)
        # post_done_hook opens the state DB for review context — it must be
        # a real SQLite file, so drop the placeholder and let it create one.
        for name in (".executor-state.db", ".executor-state.db-wal", ".executor-state.db-shm"):
            (tmp_path / "spec" / name).unlink()
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "feature.py").write_text("f = 1\n")

        cfg = _cfg(
            tmp_path,
            auto_commit=True,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=False,
        )
        ok, err, _, _, _ = post_done_hook(self._task(), cfg, True)
        assert ok is True, err

        committed = _git(tmp_path, "show", "--name-only", "--format=", "HEAD").stdout
        assert "lib/feature.py" in committed
        assert ".executor-state.db" not in committed
        tracked = _git(tmp_path, "ls-files").stdout
        assert ".executor-state.db" not in tracked

    def test_auto_commit_skipped_when_only_runtime_changes(self, tmp_path):
        _init_repo(tmp_path)
        head_before = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        _write_runtime_state(tmp_path)
        for name in (".executor-state.db", ".executor-state.db-wal", ".executor-state.db-shm"):
            (tmp_path / "spec" / name).unlink()

        cfg = _cfg(
            tmp_path,
            auto_commit=True,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=False,
        )
        ok, err, _, _, _ = post_done_hook(self._task(), cfg, True)
        assert ok is True, err
        assert _git(tmp_path, "rev-parse", "HEAD").stdout.strip() == head_before
