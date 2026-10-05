"""The WIP trailer names the attempt the work came from (PR #661 blocker 2).

Spec §1: `N` is the last recorded attempt of the task in the state DB at the
moment of the commit. Three commands erase those records before the next
start saves the WIP: `retry --fresh`, `run --all` (failed → pending) and
`reset`. Each now saves the owned task's work as WIP *before* erasing, so
the trailer never says "attempt 0" about work a real attempt produced.
"""

import argparse
import subprocess

import pytest

from spec_runner import paid_call
from spec_runner.state import ExecutorState
from tests.test_retry_continues_from_wip import BRANCH, _cfg, _fail_once, _git, repo

__all__ = ["repo"]

pytestmark = pytest.mark.slow


def _wip_attempts(root) -> list[str]:
    log = _git(root, "log", "--all", "--format=%(trailers:key=Spec-Runner-WIP-Attempt,valueonly)")
    return [line.strip() for line in log.splitlines() if line.strip()]


def _attempt_rows(cfg) -> int:
    with ExecutorState(cfg) as st:
        return len(st.get_task_state("TASK-070").attempts)


def _complete(prompts: list[str]):
    def _spawn(invocation, *, timeout, cwd, env):
        prompts.append(" ".join(invocation.argv))
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    return _spawn


class TestRetryFresh:
    def test_trailer_and_prompt_name_attempt_one(self, repo, monkeypatch):
        from spec_runner import cli

        _fail_once(repo, monkeypatch)
        cfg = _cfg(repo, max_retries=1)
        assert _attempt_rows(cfg) == 1
        prompts: list[str] = []
        monkeypatch.setattr(paid_call, "_spawn", _complete(prompts))
        cli.cmd_retry(argparse.Namespace(task_id="TASK-070", fresh=True), cfg)
        assert _wip_attempts(repo) == ["1"]
        assert prompts, "the retry made no paid call"
        assert "continuing unfinished work of attempt 1" in prompts[0]
        assert "attempt 0" not in prompts[0]

    def test_partially_staged_path_refuses_and_clears_nothing(self, repo, monkeypatch):
        from spec_runner import cli

        _fail_once(repo, monkeypatch)
        cfg = _cfg(repo, max_retries=1)
        (repo / "feature.py").write_text("staged\n")
        _git(repo, "add", "feature.py")
        (repo / "feature.py").write_text("unstaged\n")
        head = _git(repo, "rev-parse", "HEAD")
        spawned: list[int] = []
        monkeypatch.setattr(paid_call, "_spawn", lambda *a, **k: spawned.append(1))
        with pytest.raises(SystemExit) as exc:
            cli.cmd_retry(argparse.Namespace(task_id="TASK-070", fresh=True), cfg)
        assert exc.value.code == 1
        assert spawned == []
        assert _attempt_rows(cfg) == 1, "the attempts were cleared despite the refusal"
        assert _git(repo, "rev-parse", "HEAD") == head
        assert _git(repo, "branch", "--show-current").strip() == BRANCH
        assert (repo / "feature.py").read_text() == "unstaged\n"
        assert _git(repo, "show", ":feature.py") == "staged\n"
        assert _git(repo, "stash", "list") == ""

    def test_an_existing_wip_is_left_alone(self, repo, monkeypatch):
        """Work already saved as WIP: nothing to save, the trailer is unchanged."""
        from spec_runner import cli
        from spec_runner.wip import save_wip

        _fail_once(repo, monkeypatch)
        cfg = _cfg(repo, max_retries=1)
        with ExecutorState(cfg) as st:
            assert save_wip(cfg, st).saved_sha is not None
        head = _git(repo, "rev-parse", "HEAD")
        prompts: list[str] = []
        monkeypatch.setattr(paid_call, "_spawn", _complete(prompts))
        cli.cmd_retry(argparse.Namespace(task_id="TASK-070", fresh=True), cfg)
        assert _wip_attempts(repo) == ["1"]
        assert _git(repo, "rev-list", "--count", f"{head.strip()}..{BRANCH}").strip() != ""
        assert "continuing unfinished work of attempt 1" in prompts[0]


class TestOtherPathsThatEraseAttempts:
    def test_run_all_reset_saves_first(self, repo, monkeypatch):
        """`run --all`'s default failed → pending reset erases the attempts."""
        from spec_runner import cli
        from tests.test_cli_run_reset import _run_args

        _fail_once(repo, monkeypatch)
        cfg = _cfg(repo, max_retries=1, run_lint_on_done=False)
        with ExecutorState(cfg) as st:
            assert st.get_task_state("TASK-070").status == "failed"
        prompts: list[str] = []
        monkeypatch.setattr(paid_call, "_spawn", _complete(prompts))
        cli._run_tasks(_run_args(), cfg)
        assert _wip_attempts(repo) == ["1"]
        assert prompts and "continuing unfinished work of attempt 1" in prompts[0]

    def test_run_all_refusal_keeps_the_attempts(self, repo, monkeypatch):
        from spec_runner import cli
        from tests.test_cli_run_reset import _run_args

        _fail_once(repo, monkeypatch)
        cfg = _cfg(repo, max_retries=1)
        (repo / "feature.py").write_text("staged\n")
        _git(repo, "add", "feature.py")
        (repo / "feature.py").write_text("unstaged\n")
        spawned: list[int] = []
        monkeypatch.setattr(paid_call, "_spawn", lambda *a, **k: spawned.append(1))
        with pytest.raises(SystemExit) as exc:
            cli._run_tasks(_run_args(), cfg)
        assert exc.value.code == 1
        assert spawned == []
        with ExecutorState(cfg) as st:
            assert st.get_task_state("TASK-070").status == "failed"
            assert len(st.get_task_state("TASK-070").attempts) == 1

    def test_reset_command_saves_first(self, repo, monkeypatch):
        from spec_runner import cli_info

        _fail_once(repo, monkeypatch)
        cfg = _cfg(repo, max_retries=1)
        cli_info.cmd_reset(argparse.Namespace(logs=False), cfg)
        assert _wip_attempts(repo) == ["1"]
        assert _attempt_rows(cfg) == 0

    def test_reset_command_refusal_keeps_the_db(self, repo, monkeypatch):
        from spec_runner import cli_info

        _fail_once(repo, monkeypatch)
        cfg = _cfg(repo, max_retries=1)
        (repo / "feature.py").write_text("staged\n")
        _git(repo, "add", "feature.py")
        (repo / "feature.py").write_text("unstaged\n")
        with pytest.raises(SystemExit) as exc:
            cli_info.cmd_reset(argparse.Namespace(logs=False), cfg)
        assert exc.value.code == 1
        assert _attempt_rows(cfg) == 1
