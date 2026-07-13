"""Tests for integration-branch mode (one branch + one PR per run)."""

import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from spec_runner.cli import _maybe_start_integration
from spec_runner.config import ExecutorConfig
from spec_runner.git_ops import (
    IntegrationRun,
    create_integration_branch,
    finalize_integration_branch,
    has_remote,
    make_integration_branch_name,
)


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def _current_branch(cwd: Path) -> str:
    return subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=cwd,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _branches(cwd: Path) -> str:
    return subprocess.run(
        ["git", "branch"], cwd=cwd, capture_output=True, text=True
    ).stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A real git repo on ``master`` with one initial commit."""
    _run(tmp_path, "git", "init", "-b", "master")
    _run(tmp_path, "git", "config", "user.email", "t@example.com")
    _run(tmp_path, "git", "config", "user.name", "Test")
    (tmp_path / "README.md").write_text("init\n")
    _run(tmp_path, "git", "add", "-A")
    _run(tmp_path, "git", "commit", "-m", "initial")
    return tmp_path


def _config(root: Path) -> ExecutorConfig:
    return ExecutorConfig(project_root=root, main_branch="master")


def _commit(cwd: Path, name: str, subject: str) -> None:
    (cwd / name).write_text("x\n")
    _run(cwd, "git", "add", "-A")
    _run(cwd, "git", "commit", "-m", subject)


def test_branch_name_format():
    name = make_integration_branch_name(datetime(2026, 7, 13, 14, 30, 5))
    assert name == "spec-runner/run-20260713-143005"


def test_create_checks_out_integration_branch(git_repo):
    run = create_integration_branch(_config(git_repo), "spec-runner/run-x")
    assert run == IntegrationRun(branch="spec-runner/run-x", base="master")
    assert _current_branch(git_repo) == "spec-runner/run-x"


def test_finalize_empty_deletes_branch_and_returns_to_base(git_repo):
    config = _config(git_repo)
    run = create_integration_branch(config, "spec-runner/run-empty")
    url = finalize_integration_branch(config, run)
    assert url is None
    assert "spec-runner/run-empty" not in _branches(git_repo)
    assert _current_branch(git_repo) == "master"


def test_finalize_no_remote_keeps_work(git_repo):
    config = _config(git_repo)
    run = create_integration_branch(config, "spec-runner/run-work")
    _commit(git_repo, "f.txt", "TASK-001: work")
    assert has_remote(config) is False
    url = finalize_integration_branch(config, run)
    assert url is None  # cannot open a PR without a remote
    assert "spec-runner/run-work" in _branches(git_repo)  # work preserved
    assert _current_branch(git_repo) == "master"  # returned to base


def test_finalize_opens_pr_when_commits_and_remote(git_repo, monkeypatch):
    config = _config(git_repo)
    run = create_integration_branch(config, "spec-runner/run-pr")
    _commit(git_repo, "f.txt", "TASK-001: work")

    calls: dict[str, list[str]] = {}
    real_run = subprocess.run

    def fake_run(cmd, *a, **kw):
        # Intercept only the remote-touching commands; run local git for real.
        if cmd[:2] == ["git", "remote"]:
            return subprocess.CompletedProcess(cmd, 0, "origin\n", "")
        if cmd[:2] == ["git", "push"]:
            calls["push"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")
        if cmd[:1] == ["gh"]:
            calls["gh"] = cmd
            return subprocess.CompletedProcess(
                cmd, 0, "https://github.com/x/y/pull/1\n", ""
            )
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr("spec_runner.git_ops.subprocess.run", fake_run)
    url = finalize_integration_branch(config, run)

    assert url == "https://github.com/x/y/pull/1"
    assert calls["push"] == ["git", "push", "-u", "origin", "spec-runner/run-pr"]
    assert calls["gh"][:3] == ["gh", "pr", "create"]
    assert "--base" in calls["gh"] and "master" in calls["gh"]
    assert "--head" in calls["gh"] and "spec-runner/run-pr" in calls["gh"]
    assert _current_branch(git_repo) == "master"  # returned to base after PR


def test_config_default_is_off():
    assert ExecutorConfig().integration_pr is False


def test_maybe_start_disabled_returns_none(git_repo):
    config = _config(git_repo)  # integration_pr defaults to False
    assert _maybe_start_integration(SimpleNamespace(dry_run=False), config) is None


def test_maybe_start_dry_run_skips(git_repo):
    config = _config(git_repo)
    config.integration_pr = True
    assert _maybe_start_integration(SimpleNamespace(dry_run=True), config) is None
    assert _current_branch(git_repo) == "master"  # untouched


def test_maybe_start_requires_branch_creation(git_repo):
    config = _config(git_repo)
    config.integration_pr = True
    config.create_git_branch = False
    assert _maybe_start_integration(SimpleNamespace(dry_run=False), config) is None


def test_maybe_start_creates_branch_and_redirects_main(git_repo):
    config = _config(git_repo)
    config.integration_pr = True
    run = _maybe_start_integration(SimpleNamespace(dry_run=False), config)
    assert run is not None
    assert run.base == "master"
    assert run.branch.startswith("spec-runner/run-")
    # Task merges are redirected onto the integration branch; main is untouched.
    assert config.main_branch == run.branch
    assert _current_branch(git_repo) == run.branch
