"""Pre-run dirty-spec guard (#69): the spec is the run's contract."""

import argparse
import subprocess
from pathlib import Path

import pytest

from spec_runner.cli import _build_parser, _enforce_clean_spec
from spec_runner.config import ExecutorConfig
from spec_runner.git_ops import spec_dirty_paths

TASKS_MD = (
    "# Spec\n\n## M0\n\n### TASK-001: Work\n"
    "🟢 P0 | ⬜ TODO | Est: 1d\n\n"
    "**Description:** todo\n\n**Checklist:**\n- [ ] a\n\n"
    "**Traces to:** [REQ-1]\n**Depends on:** —\n"
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=root, check=False)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@e.c")
    _git(root, "config", "user.name", "T")
    (root / "README.md").write_text("hi\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")


def _write_spec(root: Path) -> None:
    spec = root / "spec"
    spec.mkdir(exist_ok=True)
    (spec / "tasks.md").write_text(TASKS_MD)


def _commit_all(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "spec")


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {"project_root": root}
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _args(**overrides) -> argparse.Namespace:
    base: dict = {"allow_dirty_spec": False}
    base.update(overrides)
    return argparse.Namespace(**base)


class TestSpecDirtyPaths:
    def test_untracked_spec_is_dirty(self, tmp_path):
        _init_repo(tmp_path)
        _write_spec(tmp_path)
        dirty = spec_dirty_paths(_cfg(tmp_path))
        assert any("tasks.md" in line for line in dirty)

    def test_committed_clean_spec_passes(self, tmp_path):
        _init_repo(tmp_path)
        _write_spec(tmp_path)
        _commit_all(tmp_path)
        assert spec_dirty_paths(_cfg(tmp_path)) == []

    def test_modified_tracked_spec_is_dirty(self, tmp_path):
        _init_repo(tmp_path)
        _write_spec(tmp_path)
        _commit_all(tmp_path)
        (tmp_path / "spec" / "tasks.md").write_text(TASKS_MD + "\nedit\n")
        dirty = spec_dirty_paths(_cfg(tmp_path))
        assert any("tasks.md" in line for line in dirty)

    def test_uncommitted_config_is_dirty(self, tmp_path):
        _init_repo(tmp_path)
        _write_spec(tmp_path)
        _commit_all(tmp_path)
        (tmp_path / "spec-runner.config.yaml").write_text("max_retries: 1\n")
        dirty = spec_dirty_paths(_cfg(tmp_path))
        assert any("spec-runner.config.yaml" in line for line in dirty)

    def test_gitignored_spec_is_not_dirty(self, tmp_path):
        """Maestro keeps generated specs untracked via ignore rules — no dirt."""
        _init_repo(tmp_path)
        (tmp_path / ".gitignore").write_text("spec/\nspec-runner.config.yaml\n")
        _git(tmp_path, "add", ".gitignore")
        _git(tmp_path, "commit", "-q", "-m", "ignore spec")
        _write_spec(tmp_path)
        (tmp_path / "spec-runner.config.yaml").write_text("max_retries: 1\n")
        assert spec_dirty_paths(_cfg(tmp_path)) == []

    def test_no_git_repo_passes(self, tmp_path):
        _write_spec(tmp_path)
        assert spec_dirty_paths(_cfg(tmp_path)) == []

    def test_fresh_repo_without_commits_passes(self, tmp_path):
        _git(tmp_path, "init", "-q", "-b", "main")
        _write_spec(tmp_path)
        assert spec_dirty_paths(_cfg(tmp_path)) == []


class TestEnforceCleanSpec:
    def test_refuses_with_exit_1(self, tmp_path, capsys):
        _init_repo(tmp_path)
        _write_spec(tmp_path)
        cfg = _cfg(tmp_path, auto_commit=True)
        with pytest.raises(SystemExit) as excinfo:
            _enforce_clean_spec(_args(), cfg)
        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "uncommitted changes" in out
        assert "--allow-dirty-spec" in out

    def test_allow_dirty_spec_overrides(self, tmp_path):
        _init_repo(tmp_path)
        _write_spec(tmp_path)
        cfg = _cfg(tmp_path, auto_commit=True)
        _enforce_clean_spec(_args(allow_dirty_spec=True), cfg)  # no raise

    def test_skipped_when_git_automation_off(self, tmp_path):
        """Subdir projects keep a permanently dirty tasks.md by design."""
        _init_repo(tmp_path)
        _write_spec(tmp_path)
        cfg = _cfg(tmp_path, auto_commit=False, create_git_branch=False)
        _enforce_clean_spec(_args(), cfg)  # no raise

    def test_passes_on_clean_spec(self, tmp_path):
        _init_repo(tmp_path)
        _write_spec(tmp_path)
        _commit_all(tmp_path)
        cfg = _cfg(tmp_path, auto_commit=True)
        _enforce_clean_spec(_args(), cfg)  # no raise


class TestAllowDirtySpecFlag:
    def test_run_flag_parses(self):
        ns = _build_parser().parse_args(["run", "--allow-dirty-spec"])
        assert ns.allow_dirty_spec is True

    def test_run_flag_default_false(self):
        ns = _build_parser().parse_args(["run"])
        assert ns.allow_dirty_spec is False

    def test_watch_flag_parses(self):
        ns = _build_parser().parse_args(["watch", "--allow-dirty-spec"])
        assert ns.allow_dirty_spec is True


class TestGuardHardening:
    """Copilot review findings on #87."""

    def test_tracked_deletion_is_dirty(self, tmp_path):
        _init_repo(tmp_path)
        _write_spec(tmp_path)
        _commit_all(tmp_path)
        (tmp_path / "spec" / "tasks.md").unlink()
        dirty = spec_dirty_paths(_cfg(tmp_path))
        assert any("tasks.md" in line for line in dirty)

    def test_git_status_failure_fails_closed(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        _write_spec(tmp_path)
        _commit_all(tmp_path)
        import subprocess as sp

        from spec_runner import git_ops

        real_run = sp.run

        def flaky(argv, **kwargs):
            if argv[:2] == ["git", "status"]:
                return sp.CompletedProcess(argv, 128, stdout="", stderr="boom")
            return real_run(argv, **kwargs)

        monkeypatch.setattr(git_ops.subprocess, "run", flaky)
        dirty = spec_dirty_paths(_cfg(tmp_path))
        assert dirty and "git status failed" in dirty[0]

    def test_retry_flag_parses(self):
        ns = _build_parser().parse_args(["retry", "TASK-001", "--allow-dirty-spec"])
        assert ns.allow_dirty_spec is True
