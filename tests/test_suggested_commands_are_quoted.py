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
    message = trust_remedy(cfg, "TASK-070", bind=True, task_branch=EVIL)
    _assert_quoted(message, "--bind-branch", EVIL)
    argv = shlex.split(_commands(message)[0])
    assert argv[:3] == ["git", "checkout", EVIL]


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
    # C1: the remedy names the task's own branch, never the checked-out one.
    assert "$(" not in refusal
    _assert_quoted(refusal, "--bind-branch", "task/task-070-w")


def test_the_abandon_note_quotes_the_branch(evil_repo):
    from spec_runner.remedy import _abandon_trust_note

    cfg = ExecutorConfig(project_root=evil_repo, create_git_branch=True)
    note = _abandon_trust_note(cfg, "TASK-070")
    assert "$(" not in note
    assert EVIL not in note


def test_the_task_id_is_quoted_too(evil_repo):
    from spec_runner.harness import trust_remedy

    cfg = ExecutorConfig(project_root=evil_repo, create_git_branch=True)
    [command] = _commands(trust_remedy(cfg, "T$(id)", bind=False, task_branch=None))
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


def test_export_gh_quotes_every_argument(capsys):
    """Pre-acceptance I3: `task export-gh` prints `gh issue create` lines built
    from tasks.md, which an agent can write."""
    from spec_runner.github_sync import export_gh
    from spec_runner.task import Task

    task = Task(
        id="TASK-001",
        name='evil $(touch pwned) `id` "q"',
        priority="p1",
        status="todo",
        estimate="1d $(id)",
        milestone="M0 $(id)",
        checklist=[("step $(touch pwned2)", False)],
    )
    export_gh(None, [task])
    [line] = [x for x in capsys.readouterr().out.splitlines() if x.startswith("gh issue")]
    assert _no_unquoted_substitution(line), line
    argv = shlex.split(line)
    assert argv[:3] == ["gh", "issue", "create"]
    assert argv[argv.index("--title") + 1] == 'TASK-001: evil $(touch pwned) `id` "q"'
    assert "step $(touch pwned2)" in argv[argv.index("--body") + 1]
    assert argv[argv.index("--label") + 1].startswith("priority:p1,milestone:m0-$(id)")
