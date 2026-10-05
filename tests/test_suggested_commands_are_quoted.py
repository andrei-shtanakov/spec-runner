"""Suggested shell commands quote every interpolated value (PR #661 item 5).

A remedy message the operator copies into a shell interpolated values an
agent can influence — a branch name above all: an agent can
`git checkout -b 'x$(…)'`, and the copied command would run it. Every
value interpolated into a suggested command goes through `shlex.quote`.
"""

import re
import shlex
import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig

EVIL = "x$(touch${IFS}pwned)"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


def _commands(message: str) -> list[str]:
    return re.findall(r"`([^`]+)`", message)


def _no_unquoted_substitution(command: str) -> bool:
    return "$(" not in re.sub(r"'[^']*'", "", command)


@pytest.fixture
def evil_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@e.c")
    _git(tmp_path, "config", "user.name", "T")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "base")
    _git(tmp_path, "checkout", "-q", "-b", EVIL)
    assert _git(tmp_path, "branch", "--show-current").strip() == EVIL
    return tmp_path


def _assert_quoted(message: str, flag: str, value: str) -> None:
    [command] = [c for c in _commands(message) if flag in c]
    assert _no_unquoted_substitution(command), command
    argv = shlex.split(command)
    assert argv[argv.index(flag) + 1] == value, argv


def test_trust_remedy_quotes_the_branch(evil_repo):
    from spec_runner.harness import trust_remedy

    cfg = ExecutorConfig(project_root=evil_repo, create_git_branch=True)
    _assert_quoted(trust_remedy(cfg, "TASK-070", bind=True), "--bind-branch", EVIL)


def test_the_strict_refusal_quotes_the_branch(evil_repo):
    """Through `HarnessBaseline.prepare`, the refusal a started task meets."""
    from spec_runner.harness import HarnessBaseline
    from spec_runner.state import ExecutorState
    from spec_runner.task import Task

    cfg = ExecutorConfig(
        project_root=evil_repo,
        state_file=evil_repo / "state.db",
        create_git_branch=True,
        harness_guard="strict",
    )
    task = Task(id="TASK-070", name="w", priority="p0", status="todo", estimate="1d")
    with ExecutorState(cfg) as st:
        refusal = HarnessBaseline().prepare(cfg, st, task, started=True)
    assert refusal is not None
    _assert_quoted(refusal, "--bind-branch", EVIL)


def test_the_abandon_note_quotes_the_branch(evil_repo):
    from spec_runner.remedy import _abandon_trust_note

    cfg = ExecutorConfig(project_root=evil_repo, create_git_branch=True)
    _assert_quoted(_abandon_trust_note(cfg, "TASK-070"), "--bind-branch", EVIL)


def test_the_task_id_is_quoted_too(evil_repo):
    from spec_runner.harness import trust_remedy

    cfg = ExecutorConfig(project_root=evil_repo, create_git_branch=True)
    [command] = _commands(trust_remedy(cfg, "T$(id)", bind=False))
    assert _no_unquoted_substitution(command)
    assert shlex.split(command)[3] == "T$(id)"


def test_return_to_base_message_quotes_the_base(tmp_path, capsys, monkeypatch):
    from spec_runner import git_ops

    msg = git_ops.checkout_back_hint(EVIL) if hasattr(git_ops, "checkout_back_hint") else None
    assert msg is not None, "the hint is built by one quoted helper"
    [command] = _commands(msg)
    assert _no_unquoted_substitution(command)
    assert shlex.split(command) == ["git", "checkout", EVIL]


def test_git_reset_remedy_quotes_its_sha():
    from spec_runner.tdd import _quoted_reset

    command = _quoted_reset("abc$(id)def")
    assert _no_unquoted_substitution(command)
    assert shlex.split(command) == ["git", "reset", "--hard", "abc$(id)def"]
