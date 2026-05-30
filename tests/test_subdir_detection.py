"""Tests for subdir-repo detection (v2.3.0)."""

import subprocess

import pytest

from spec_runner.config import _detect_subdir_repo


@pytest.fixture
def git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


class TestDetectSubdirRepo:
    def test_project_root_is_toplevel_returns_none(self, git_repo):
        assert _detect_subdir_repo(git_repo) is None

    def test_subdir_returns_toplevel(self, git_repo):
        sub = git_repo / "sub"
        sub.mkdir()
        result = _detect_subdir_repo(sub)
        assert result == git_repo.resolve()

    def test_not_a_git_repo_returns_none(self, tmp_path):
        assert _detect_subdir_repo(tmp_path) is None
