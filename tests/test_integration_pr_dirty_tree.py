"""`integration_pr` on a dirty working copy (devtools battle run, 2026-09-30).

An interrupted run left the working copy on a task branch with uncommitted
work. The restart's `git checkout <base>` refused, `create_integration_branch`
returned None, and the run went on **without** the mode its config declared:
every task branched from master, a task already accepted in the state DB was
executed a third time, and the run finally stopped on a state/spec mismatch
after the extra work.

Pinned here:

1. the run's guards (governance, dirty spec, tracked state) answer *before*
   the integration branch is forked — the fork used to come first;
2. stray uncommitted work is rescued into a stash once per run, before the
   fork, by the same mechanism the task start uses (#231);
3. a declared `integration_pr` that cannot be honoured refuses the run;
4. a task the state DB already calls successful is never selected again by
   `run`/`run --all` — the mismatch is refused before any work, not after;
5. the mismatch stop names the branch tasks.md was read from and how to
   recover;
6. a timed-out attempt says that a retry with the same timeout will likely
   time out again.
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import spec_runner.cli as cli_mod
from spec_runner.cli import _maybe_start_integration, _run_tasks
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _task_block(task_id: str, status: str = "⬜ TODO", depends_on: str = "—") -> str:
    return (
        f"### {task_id}: Work {task_id}\n"
        f"\U0001f534 P0 | {status} | Est: 0.5d\n\n"
        "**Description:** x\n\n**Checklist:**\n- [ ] do it\n\n"
        f"**Traces to:** [NFR-1]\n**Depends on:** {depends_on}\n"
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A repo on master with a committed spec and a task branch that diverges."""
    _git(tmp_path, "init", "-q", "-b", "master")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".gitignore").write_text("spec/.executor*\nlogs/\n")
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec" / "tasks.md").write_text(
        "# Spec\n\n## M0\n\n" + _task_block("TASK-001") + "\n" + _task_block("TASK-002")
    )
    (tmp_path / "work.py").write_text("base\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    # The interrupted attempt: a task branch whose committed work.py differs
    # from master, plus uncommitted edits on top — `git checkout master` then
    # refuses ("Your local changes … would be overwritten by checkout").
    _git(tmp_path, "checkout", "-q", "-b", "task/task-002-work")
    (tmp_path / "work.py").write_text("task branch\n")
    _git(tmp_path, "commit", "-q", "-am", "task work")
    (tmp_path / "work.py").write_text("uncommitted attempt\n")
    (tmp_path / "scratch.txt").write_text("untracked attempt\n")
    return tmp_path


def _config(repo: Path, **overrides: object) -> ExecutorConfig:
    values: dict = {
        "project_root": repo,
        "state_file": repo / "spec" / ".executor-state.db",
        "logs_dir": repo / "logs",
        "create_git_branch": True,
        "auto_commit": True,
        "run_tests_on_done": False,
        "run_review": False,
        "integration_pr": True,
        "main_branch": "master",
    }
    values.update(overrides)
    cfg = ExecutorConfig(**values)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _run_args(**overrides: object) -> argparse.Namespace:
    base: dict = {
        "command": "run",
        "all": True,
        "no_reset_failed": False,
        "force": True,
        "task": None,
        "milestone": None,
        "restart": False,
        "dry_run": False,
        "json_result": False,
        "max_retries": None,
        "timeout": None,
        "no_tests": False,
        "no_branch": False,
        "no_commit": False,
        "no_review": False,
        "hitl_review": False,
        "callback_url": "",
        "tui": False,
        "allow_dirty_spec": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class TestRescueBeforeTheFork:
    def test_dirty_task_branch_still_gets_its_integration_branch(self, repo: Path) -> None:
        config = _config(repo)

        run = _maybe_start_integration(SimpleNamespace(dry_run=False), config)

        assert run is not None
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == run.branch
        assert config.main_branch == run.branch
        assert config.integration_branch_active is True
        # Forked from the real base, not from the task branch it started on.
        assert (repo / "work.py").read_text() == "base\n"

    def test_the_rescued_work_is_recoverable_byte_for_byte(self, repo: Path) -> None:
        config = _config(repo)

        _maybe_start_integration(SimpleNamespace(dry_run=False), config)

        stashes = _git(repo, "stash", "list")
        assert "spec-runner rescue: run" in stashes
        _git(repo, "checkout", "-q", "task/task-002-work")
        _git(repo, "stash", "pop", "-q")
        assert (repo / "work.py").read_text() == "uncommitted attempt\n"
        assert (repo / "scratch.txt").read_text() == "untracked attempt\n"

    def test_a_clean_tree_stashes_nothing(self, repo: Path) -> None:
        _git(repo, "checkout", "-q", "--", ".")
        (repo / "scratch.txt").unlink()
        config = _config(repo)

        assert _maybe_start_integration(SimpleNamespace(dry_run=False), config) is not None
        assert _git(repo, "stash", "list") == ""

    def test_a_failed_rescue_refuses_without_touching_the_tree(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = _config(repo)
        monkeypatch.setattr(
            cli_mod, "rescue_run_uncommitted", lambda config: (False, "git stash failed")
        )

        with pytest.raises(SystemExit) as exc:
            _maybe_start_integration(SimpleNamespace(dry_run=False), config)

        assert exc.value.code == 1
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "task/task-002-work"
        assert (repo / "work.py").read_text() == "uncommitted attempt\n"
        assert "integration_pr" in capsys.readouterr().err


class TestDeclaredModeIsNotDroppedSilently:
    def test_a_fork_that_fails_refuses_the_run(
        self, repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = _config(repo)
        monkeypatch.setattr(cli_mod, "create_integration_branch", lambda config, name: None)

        with pytest.raises(SystemExit) as exc:
            _maybe_start_integration(SimpleNamespace(dry_run=False), config)

        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "integration_pr" in err
        assert "integration branch could not be created" in err
        assert config.integration_branch_active is False

    def test_mode_off_is_untouched(self, repo: Path) -> None:
        config = _config(repo, integration_pr=False)

        assert _maybe_start_integration(SimpleNamespace(dry_run=False), config) is None
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "task/task-002-work"
        assert _git(repo, "stash", "list") == ""


class TestGuardsAnswerBeforeTheFork:
    def test_dirty_spec_is_refused_before_any_branch_or_stash(self, repo: Path) -> None:
        tasks_md = repo / "spec" / "tasks.md"
        tasks_md.write_text(tasks_md.read_text() + "\n<!-- edited -->\n")
        config = _config(repo)

        with pytest.raises(SystemExit) as exc:
            _run_tasks(_run_args(), config)

        assert exc.value.code == 1
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "task/task-002-work"
        assert _git(repo, "stash", "list") == ""
        assert not [
            b for b in _git(repo, "branch", "--list", "spec-runner/run-*").splitlines() if b
        ]


def _plain_config(tmp_path: Path) -> ExecutorConfig:
    cfg = ExecutorConfig(
        state_file=tmp_path / "state.db",
        project_root=tmp_path,
        logs_dir=tmp_path / "logs",
        create_git_branch=False,
        auto_commit=False,
        run_tests_on_done=False,
        run_review=False,
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _write_tasks(tmp_path: Path, body: str) -> Path:
    spec = tmp_path / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    tasks_md = spec / "tasks.md"
    tasks_md.write_text("# Spec\n\n## M0\n\n" + body)
    return tasks_md


class TestAcceptedTaskIsNotRunAgain:
    @pytest.mark.parametrize(
        "overrides",
        [{"all": True}, {"all": False}],
        ids=["run --all", "run (next task)"],
    )
    def test_success_in_state_but_todo_in_tasks_md_is_refused_before_work(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict
    ) -> None:
        tasks_md = _write_tasks(tmp_path, _task_block("TASK-001") + "\n" + _task_block("TASK-002"))
        cfg = _plain_config(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_attempt("TASK-001", success=True, duration=1.0)
        ran: list[str] = []
        monkeypatch.setattr(
            cli_mod, "run_with_retries", lambda task, config, state: ran.append(task.id)
        )

        with pytest.raises(SystemExit) as exc:
            _run_tasks(_run_args(**overrides), cfg)

        assert exc.value.code == 1
        assert ran == []
        with ExecutorState(cfg) as state:
            assert state.get_meta("last_run_stop_reason") == "state_spec_mismatch"
            assert "TASK-001" in (state.get_meta("last_run_stop_detail") or "")
        assert "⬜ TODO" in tasks_md.read_text()

    def test_an_explicit_task_is_still_the_operators_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _write_tasks(tmp_path, _task_block("TASK-001"))
        cfg = _plain_config(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_attempt("TASK-001", success=True, duration=1.0)
        ran: list[str] = []

        def _fake(task, config, state):
            ran.append(task.id)
            return False

        monkeypatch.setattr(cli_mod, "run_with_retries", _fake)

        with contextlib.suppress(SystemExit):
            _run_tasks(_run_args(all=False, task="TASK-001"), cfg)

        assert ran == ["TASK-001"]

    def test_a_success_not_yet_ready_is_left_to_the_backstop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Unchanged #124 behaviour: a stale success the loop never reaches is
        # still reported by gate 2, and the ready task before it does run.
        _write_tasks(
            tmp_path,
            _task_block("TASK-000") + "\n" + _task_block("TASK-001", depends_on="[TASK-000]"),
        )
        cfg = _plain_config(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_attempt("TASK-001", success=True, duration=1.0)
        ran: list[str] = []

        def _fake(task, config, state):
            ran.append(task.id)
            state.record_attempt(task.id, success=False, duration=1.0, error="boom")
            from spec_runner.task import update_task_status

            update_task_status(config.tasks_file, task.id, "blocked")
            return "SKIP"

        monkeypatch.setattr(cli_mod, "run_with_retries", _fake)

        with pytest.raises(SystemExit):
            _run_tasks(_run_args(), cfg)

        assert ran == ["TASK-000"]


class TestMismatchSaysWhereAndHow:
    def test_the_stop_names_the_branch_and_the_way_out(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _git(repo, "checkout", "-q", "--", ".")
        (repo / "scratch.txt").unlink()
        cfg = _config(repo, integration_pr=False, create_git_branch=False, auto_commit=False)
        with ExecutorState(cfg) as state:
            state.record_attempt("TASK-001", success=True, duration=1.0)
        monkeypatch.setattr(cli_mod, "run_with_retries", lambda *a: pytest.fail("ran"))

        with pytest.raises(SystemExit):
            _run_tasks(_run_args(), cfg)

        err = capsys.readouterr().err
        assert "task/task-002-work" in err
        assert "--task=TASK-001" in err


class TestTimeoutSaysRaiseTheLimit:
    def test_timeout_progress_line_suggests_the_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spec_runner import execution

        lines: list[str] = []
        monkeypatch.setattr(execution, "log_progress", lambda msg, *a, **k: lines.append(msg))

        execution._report_timeout(30, "TASK-002")

        assert any("--timeout" in line and "30" in line for line in lines)
