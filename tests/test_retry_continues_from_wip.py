"""End to end: a retry continues from the previous attempt's work (spec §1, §4)."""

import argparse
import sqlite3
import subprocess
from pathlib import Path

import pytest

from spec_runner import paid_call
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import resolve_namespace


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


TASKS = (
    "# Spec\n\n## M0\n\n### TASK-070: work\n🔴 P0 | ⬜ TODO | Est: 1d\n\n"
    "**Description:** work\n\n**Checklist:**\n- [ ] do it\n\n"
    "**Traces to:** [REQ-0]\n**Depends on:** —\n"
)

BRANCH = "task/task-070-work"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@e.c")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "tasks.md").write_text(TASKS)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='d'\n")
    (tmp_path / ".gitignore").write_text("spec/.executor*\nlogs/\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    return tmp_path


def _cfg(repo: Path, **kw) -> ExecutorConfig:
    base = {
        "project_root": repo,
        "state_file": repo / "spec" / ".executor-state.db",
        "logs_dir": repo / "logs",
        "create_git_branch": True,
        "auto_commit": True,
        "run_tests_on_done": False,
        "run_review": False,
        "task_timeout_minutes": 1,
        "max_retries": 2,
        "retry_delay_seconds": 0,
        "sync_deps": False,
        "harness_guard": "strict",
    }
    base.update(kw)
    return ExecutorConfig(**base)


def _task() -> Task:
    return Task(
        id="TASK-070", name="work", priority="p0", status="todo", description="work", estimate="1d"
    )


def _timeout_then_complete(repo: Path, prompts: list[str]):
    calls: list[int] = []

    def _spawn(invocation, *, timeout, cwd, env):
        calls.append(1)
        prompts.append(" ".join(invocation.argv))
        if len(calls) == 1:
            (repo / "feature.py").write_text("def f():\n    return 1\n")
            raise subprocess.TimeoutExpired(invocation.argv, timeout)
        assert (repo / "feature.py").exists(), "attempt 2 started from scratch"
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    return _spawn


def _fail_once(repo: Path, monkeypatch) -> None:
    """One failed attempt: the task branch and its workspace row exist, dirt stays."""

    def _fail(invocation, *, timeout, cwd, env):
        (repo / "feature.py").write_text("def f():\n    return 1\n")
        raise subprocess.TimeoutExpired(invocation.argv, timeout)

    monkeypatch.setattr(paid_call, "_spawn", _fail)
    from spec_runner.execution import run_with_retries

    cfg = _cfg(repo, max_retries=1)
    with ExecutorState(cfg) as st:
        run_with_retries(_task(), cfg, st)
    assert _git(repo, "branch", "--show-current").strip() == BRANCH
    assert (repo / "feature.py").exists()


def _stash_list(repo: Path) -> str:
    return _git(repo, "stash", "list")


def test_attempt_two_sees_attempt_ones_file(repo, monkeypatch):
    prompts: list[str] = []
    monkeypatch.setattr(paid_call, "_spawn", _timeout_then_complete(repo, prompts))
    from spec_runner.execution import run_with_retries

    cfg = _cfg(repo)
    with ExecutorState(cfg) as st:
        run_with_retries(_task(), cfg, st)
    log = _git(repo, "log", "--all", "--format=%s%n%b")
    assert "wip(TASK-070): unfinished work of attempt 1 — not a candidate" in log
    assert "Spec-Runner-WIP-Attempt: 1" in log


def test_attempt_two_prompt_names_the_continuation(repo, monkeypatch):
    prompts: list[str] = []
    monkeypatch.setattr(paid_call, "_spawn", _timeout_then_complete(repo, prompts))
    from spec_runner.execution import run_with_retries

    cfg = _cfg(repo)
    with ExecutorState(cfg) as st:
        run_with_retries(_task(), cfg, st)
    assert "continuing unfinished work of attempt 1" in prompts[1]


def test_off_still_saves_wip(repo, monkeypatch):
    """The workspace row is written in every guard mode; WIP depends on it, not on a snapshot."""
    prompts: list[str] = []
    monkeypatch.setattr(paid_call, "_spawn", _timeout_then_complete(repo, prompts))
    from spec_runner.execution import run_with_retries

    cfg = _cfg(repo, harness_guard="off")
    with ExecutorState(cfg) as st:
        run_with_retries(_task(), cfg, st)
        assert st.get_harness_baseline(resolve_namespace(cfg), "TASK-070") is None
    log = _git(repo, "log", "--all", "--format=%s%n%b")
    assert "wip(TASK-070): unfinished work of attempt 1 — not a candidate" in log


def test_operator_dirt_on_task_branch_is_preserved_as_wip(repo, monkeypatch):
    """Review Focus 1: an operator's edits on the recorded branch are kept."""

    # first attempt creates branch + workspace, then fails
    def _fail(invocation, *, timeout, cwd, env):
        raise subprocess.TimeoutExpired(invocation.argv, timeout)

    monkeypatch.setattr(paid_call, "_spawn", _fail)
    from spec_runner.execution import run_with_retries

    cfg = _cfg(repo, max_retries=1)
    with ExecutorState(cfg) as st:
        run_with_retries(_task(), cfg, st)
    (repo / "operator_note.py").write_text("# by hand\n")
    seen: list[bool] = []

    def _look(invocation, *, timeout, cwd, env):
        seen.append((repo / "operator_note.py").exists())
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    monkeypatch.setattr(paid_call, "_spawn", _look)
    with ExecutorState(cfg) as st:
        st.get_task_state("TASK-070").attempts = []
        run_with_retries(_task(), cfg, st)
    assert seen == [True]


def test_failing_rescue_after_wip_blocks_the_cleanup(repo, monkeypatch):
    from spec_runner import hooks

    def _fail(invocation, *, timeout, cwd, env):
        (repo / "feature.py").write_text("x\n")
        (repo / "spec" / "tasks.md").write_text(TASKS + "\n<!-- flip -->\n")
        raise subprocess.TimeoutExpired(invocation.argv, timeout)

    monkeypatch.setattr(paid_call, "_spawn", _fail)
    # The first start's rescue is the real one (its tree is clean); only the
    # second start — the one after WIP — fails to stash what remains.
    real_rescue = hooks.rescue_uncommitted
    rescues: list[int] = []

    def _rescue(task, config):
        rescues.append(1)
        return real_rescue(task, config) if len(rescues) == 1 else (False, "stash failed")

    monkeypatch.setattr(hooks, "rescue_uncommitted", _rescue)
    from spec_runner.execution import run_with_retries

    cfg = _cfg(repo)
    with ExecutorState(cfg) as st:
        run_with_retries(_task(), cfg, st)
    assert len(rescues) == 2
    assert "<!-- flip -->" in (repo / "spec" / "tasks.md").read_text()
    assert "wip(TASK-070)" in _git(repo, "log", "-1", "--format=%s")
    assert _git(repo, "branch", "--show-current").strip() == BRANCH


def test_create_git_branch_false_is_untouched(repo, monkeypatch):
    calls: list[int] = []

    def _spawn(invocation, *, timeout, cwd, env):
        calls.append(1)
        if len(calls) == 1:
            (repo / "feature.py").write_text("x\n")
            raise subprocess.TimeoutExpired(invocation.argv, timeout)
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    monkeypatch.setattr(paid_call, "_spawn", _spawn)
    from spec_runner.execution import run_with_retries

    cfg = _cfg(repo, create_git_branch=False, auto_commit=False)
    with ExecutorState(cfg) as st:
        run_with_retries(_task(), cfg, st)
    assert "wip(" not in _git(repo, "log", "--all", "--format=%s")
    assert (repo / "feature.py").exists()


class TestWipRefusalKeepsItsKind:
    """A WIP refusal is recorded with its own kind and the stage `branch`."""

    def _retry(self, repo: Path, monkeypatch) -> list[int]:
        calls: list[int] = []

        def _spawn(invocation, *, timeout, cwd, env):
            calls.append(1)
            return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

        monkeypatch.setattr(paid_call, "_spawn", _spawn)
        from spec_runner.execution import run_with_retries

        cfg = _cfg(repo, max_retries=1)
        with ExecutorState(cfg) as st:
            st.get_task_state("TASK-070").attempts = []
            run_with_retries(_task(), cfg, st)
        return calls

    def _last_attempt(self, repo: Path):
        with ExecutorState(_cfg(repo)) as st:
            return st.get_task_state("TASK-070").attempts[-1]

    def _nothing_destructive_ran(self, repo: Path) -> None:
        assert _stash_list(repo) == ""
        assert _git(repo, "branch", "--show-current").strip() == BRANCH
        assert "wip(" not in _git(repo, "log", "--all", "--format=%s")

    def test_partially_staged_path_is_a_policy_refusal(self, repo, monkeypatch):
        _fail_once(repo, monkeypatch)
        (repo / "feature.py").write_text("staged\n")
        _git(repo, "add", "feature.py")
        (repo / "feature.py").write_text("unstaged\n")

        calls = self._retry(repo, monkeypatch)

        assert calls == [], "no agent after a refusal"
        attempt = self._last_attempt(repo)
        assert attempt.error_kind == "policy"
        assert attempt.error_code == "HOOK_FAILURE"
        assert attempt.error_stage == "branch"
        assert "feature.py" in (attempt.error or "")
        assert (repo / "feature.py").read_text() == "unstaged\n"
        self._nothing_destructive_ran(repo)

    def test_unreadable_index_is_an_instrument_refusal(self, repo, monkeypatch):
        from spec_runner import wip

        _fail_once(repo, monkeypatch)

        def _broken(config, paths):
            raise wip.IndexUnreadable("index.lock exists")

        monkeypatch.setattr(wip, "_partially_staged", _broken)
        calls = self._retry(repo, monkeypatch)

        assert calls == []
        attempt = self._last_attempt(repo)
        assert attempt.error_kind == "instrument"
        assert attempt.error_code == "INFRASTRUCTURE"
        assert attempt.error_stage == "branch"
        assert (repo / "feature.py").exists()
        self._nothing_destructive_ran(repo)

    def test_db_failure_while_finding_the_owner_is_an_instrument_refusal(self, repo, monkeypatch):
        _fail_once(repo, monkeypatch)

        def _boom(self, namespace, branch):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(ExecutorState, "workspace_for_branch", _boom)
        calls = self._retry(repo, monkeypatch)

        assert calls == []
        attempt = self._last_attempt(repo)
        assert attempt.error_kind == "instrument"
        assert attempt.error_stage == "branch"
        assert (repo / "feature.py").exists()
        self._nothing_destructive_ran(repo)


# --- the integration_pr fork ---------------------------------------------------


def _write_cli_config(repo: Path, *, guard: str = "strict") -> None:
    """The flat CLI config shape (`tests/test_run_exit_contract.py`), committed."""
    (repo / "spec-runner.config.yaml").write_text(
        f"""max_retries: 2
retry_delay_seconds: 0
task_timeout_minutes: 1
integration_pr: true
harness_guard: {guard}
hooks:
  pre_start:
    create_git_branch: true
    sync_deps: false
  post_done:
    run_tests: false
    run_lint: false
    run_review: false
    auto_commit: true
"""
    )
    _git(repo, "add", "spec-runner.config.yaml")
    _git(repo, "commit", "-qm", "config")


def _fork_args() -> argparse.Namespace:
    return argparse.Namespace(dry_run=False)


class TestIntegrationFork:
    def test_retry_saves_wip_before_the_fork(self, repo, monkeypatch):
        from spec_runner import cli

        _write_cli_config(repo)
        _fail_once(repo, monkeypatch)
        monkeypatch.setattr(paid_call, "_spawn", _timeout_then_complete(repo, []))
        monkeypatch.setattr(cli, "finalize_integration_branch", lambda *a, **k: None)
        monkeypatch.chdir(repo)

        try:
            cli.main(["retry", "TASK-070"])
        except SystemExit as exc:
            assert not exc.code, f"retry exited {exc.code}"

        log = _git(repo, "log", BRANCH, "--format=%s%n%b")
        assert "wip(TASK-070): unfinished work of attempt 1 — not a candidate" in log
        assert "spec-runner rescue: run" not in _stash_list(repo)

    def test_db_failure_refuses_before_the_integration_fork(self, repo, monkeypatch):
        from spec_runner import cli

        _write_cli_config(repo)
        _fail_once(repo, monkeypatch)
        monkeypatch.setattr(paid_call, "_spawn", _timeout_then_complete(repo, []))

        def _boom(self, namespace, branch):
            raise sqlite3.OperationalError("disk I/O error")

        monkeypatch.setattr(ExecutorState, "workspace_for_branch", _boom)
        monkeypatch.chdir(repo)

        with pytest.raises(SystemExit) as exc:
            cli.main(["retry", "TASK-070"])

        assert exc.value.code == 2, "a DB failure is an instrument refusal"
        assert _stash_list(repo) == ""
        assert _git(repo, "branch", "--show-current").strip() == BRANCH
        assert (repo / "feature.py").exists()
        assert "wip(" not in _git(repo, "log", "--all", "--format=%s")

    def test_untrusted_owner_refuses_the_fork(self, repo, monkeypatch):
        """R4: the owner's trust check runs before WIP and before the stash."""
        from spec_runner import cli
        from spec_runner.harness import surface_snapshot

        _fail_once(repo, monkeypatch)
        cfg = _cfg(repo, integration_pr=True)
        surface, files = surface_snapshot(cfg)
        with ExecutorState(cfg) as st:
            st.store_harness_baseline(
                resolve_namespace(cfg),
                "TASK-070",
                provenance="recaptured",
                guard_mode="warn",
                surface=surface,
                files=files,
                run_id=None,
            )

        with pytest.raises(SystemExit) as exc:
            cli._maybe_start_integration(_fork_args(), cfg)

        assert exc.value.code == 1
        assert cfg.integration_branch_active is False
        assert _stash_list(repo) == ""
        assert _git(repo, "branch", "--show-current").strip() == BRANCH
        assert (repo / "feature.py").exists()
        assert "wip(" not in _git(repo, "log", "--all", "--format=%s")

    def test_unreadable_owner_baseline_refuses_with_instrument(self, repo, monkeypatch):
        from spec_runner import cli

        _fail_once(repo, monkeypatch)
        cfg = _cfg(repo, integration_pr=True)

        def _boom(self, namespace, task_id):
            raise sqlite3.OperationalError("database disk image is malformed")

        monkeypatch.setattr(ExecutorState, "get_harness_baseline", _boom)

        with pytest.raises(SystemExit) as exc:
            cli._maybe_start_integration(_fork_args(), cfg)

        assert exc.value.code == 2
        assert _stash_list(repo) == ""
        assert _git(repo, "branch", "--show-current").strip() == BRANCH

    def test_owner_missing_from_tasks_md_refuses_the_fork(self, repo):
        from spec_runner import cli

        cfg = _cfg(repo, integration_pr=True)
        _git(repo, "checkout", "-q", "-b", "task/task-999-gone")
        (repo / "stray.py").write_text("x\n")
        with ExecutorState(cfg) as st:
            st.record_workspace(
                resolve_namespace(cfg), "TASK-999", branch="task/task-999-gone", run_id=None
            )

        with pytest.raises(SystemExit) as exc:
            cli._maybe_start_integration(_fork_args(), cfg)

        assert exc.value.code == 1
        assert _stash_list(repo) == ""
        assert (repo / "stray.py").exists()

    def test_unowned_task_branch_dirt_refuses_under_strict(self, repo, capsys):
        from spec_runner import cli

        cfg = _cfg(repo, integration_pr=True)
        _git(repo, "checkout", "-q", "-b", "task/task-555-x")
        (repo / "stray.py").write_text("x\n")

        with pytest.raises(SystemExit) as exc:
            cli._maybe_start_integration(_fork_args(), cfg)

        assert exc.value.code == 1
        assert "belongs to no recorded task" in capsys.readouterr().err
        assert _stash_list(repo) == ""
        assert (repo / "stray.py").exists()

    def test_unowned_task_branch_dirt_is_stashed_under_warn(self, repo):
        from spec_runner import cli

        cfg = _cfg(repo, integration_pr=True, harness_guard="warn")
        _git(repo, "checkout", "-q", "-b", "task/task-555-x")
        (repo / "stray.py").write_text("x\n")

        run = cli._maybe_start_integration(_fork_args(), cfg)

        assert run is not None
        assert "spec-runner rescue: run" in _stash_list(repo)


class TestUnownedTaskBranchDirtAtTaskStart:
    """R5 (spec §1, second bullet): the same rule at the pre_start branch stage."""

    def _start_on_foreign_branch(self, repo: Path, monkeypatch, guard: str) -> list[int]:
        _git(repo, "checkout", "-q", "-b", "task/task-555-x")
        (repo / "stray.py").write_text("x\n")
        calls: list[int] = []

        def _spawn(invocation, *, timeout, cwd, env):
            calls.append(1)
            return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

        monkeypatch.setattr(paid_call, "_spawn", _spawn)
        from spec_runner.execution import run_with_retries

        cfg = _cfg(repo, max_retries=1, harness_guard=guard)
        with ExecutorState(cfg) as st:
            run_with_retries(_task(), cfg, st)
        return calls

    def test_strict_refuses_before_the_stash(self, repo, monkeypatch):
        calls = self._start_on_foreign_branch(repo, monkeypatch, "strict")

        assert calls == []
        with ExecutorState(_cfg(repo)) as st:
            attempt = st.get_task_state("TASK-070").attempts[-1]
        assert attempt.error_kind == "policy"
        assert attempt.error_stage == "branch"
        assert "belongs to no recorded task" in (attempt.error or "")
        assert _stash_list(repo) == ""
        assert _git(repo, "branch", "--show-current").strip() == "task/task-555-x"
        assert (repo / "stray.py").exists()

    def test_warn_still_stashes(self, repo, monkeypatch):
        calls = self._start_on_foreign_branch(repo, monkeypatch, "warn")

        assert calls == [1]
        assert "spec-runner rescue: TASK-070" in _stash_list(repo)


def test_unreadable_tasks_md_at_the_fork_is_an_instrument_refusal(repo, monkeypatch):
    from spec_runner import cli

    _fail_once(repo, monkeypatch)
    cfg = _cfg(repo, integration_pr=True)

    def _broken(path):
        raise ValueError("malformed task header")

    monkeypatch.setattr(cli, "parse_tasks", _broken)

    with pytest.raises(SystemExit) as exc:
        cli._maybe_start_integration(_fork_args(), cfg)

    assert exc.value.code == 2
    assert _stash_list(repo) == ""
    assert _git(repo, "branch", "--show-current").strip() == BRANCH
    assert (repo / "feature.py").exists()


def test_unreadable_wip_refuses_before_the_paid_green_call(repo, monkeypatch):
    """A WipReadError while building the prompt is an INSTRUMENT refusal, no spend."""
    from spec_runner import hooks
    from spec_runner.execution import run_with_retries
    from spec_runner.wip import WipReadError

    _fail_once(repo, monkeypatch)
    spawned: list[int] = []

    def _spawn(invocation, *, timeout, cwd, env):
        spawned.append(1)
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    def _boom(config):
        raise WipReadError("git exploded")

    monkeypatch.setattr(paid_call, "_spawn", _spawn)
    monkeypatch.setattr(hooks, "_wip_base", _boom)
    cfg = _cfg(repo, max_retries=1)
    with ExecutorState(cfg) as st:
        st.get_task_state("TASK-070").attempts = []
        run_with_retries(_task(), cfg, st)
        last = st.get_task_state("TASK-070").attempts[-1]
    assert not spawned
    assert not last.success
    assert "git exploded" in (last.error or "")
