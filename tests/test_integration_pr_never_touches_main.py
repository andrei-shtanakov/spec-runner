"""#254 (F-32): under `integration_pr`, the tool must not touch main.

The pilot's config says it in one line — *"One branch per run, single PR at the
end, human reviews and merges; **master is never touched by the tool**"* — and
phase-1 runs honoured it. Then the completing `retry` of TASK-101 did:

```
checkout master
merge task/task-101-deterministic-provider-fallbac   (ort)
```

leaving local master 11 commits ahead of origin, no PR, and the human gate
bypassed.

The cause is small and the shape is familiar: `_maybe_start_integration` — the
function that forks the branch and redirects `config.main_branch` onto it — is
called by `run` and by nothing else. `retry` never called it, so `main_branch`
resolved to the real master and the merge stage did what it always does.

Two halves, and both matter:

- the **guard** lives where the forbidden operation happens, not in each
  command, so a future command that forgets to fork cannot reintroduce this;
- `retry` **participates** in the mode instead of being blocked by it: a retry
  is a run of one task, so it forks, collects, pushes and opens the PR.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.hooks import post_done_hook
from spec_runner.task import Task


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", "master")
    _git(root, "config", "user.email", "o@e.c")
    _git(root, "config", "user.name", "O")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root.parent / ".state.db",
        "logs_dir": root.parent / ".logs",
        "integration_pr": True,
        "create_git_branch": True,
        "run_tests_on_done": False,
        "run_lint_on_done": False,
        "run_review": False,
        "auto_commit": True,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task() -> Task:
    return Task(id="TASK-101", name="t", priority="p1", status="todo", estimate="1h")


def _work_on_a_task_branch(root: Path) -> str:
    """A finished task, committed on its own branch — where post_done starts."""
    branch = "task/task-101-t"
    _git(root, "checkout", "-q", "-b", branch)
    (root / "src.py").write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "TASK-101: work")
    return branch


@pytest.mark.slow
class TestTheMergeStageRefuses:
    def test_master_is_untouched_when_no_integration_branch_was_opened(self, tmp_path):
        """The pilot's exact failure, at the point it happened."""
        root = _repo(tmp_path)
        master_before = _git(root, "rev-parse", "master").stdout.strip()
        branch = _work_on_a_task_branch(root)

        ok, error, *_ = post_done_hook(_task(), _cfg(root), True)

        assert ok is True, "the work is finished; not merging is not a failure"
        assert error is None
        assert _git(root, "rev-parse", "master").stdout.strip() == master_before
        assert _git(root, "branch", "--show-current").stdout.strip() == branch, (
            "the work stays where a human can open a PR from it"
        )

    def test_it_says_what_it_did_not_do(self, tmp_path, monkeypatch):
        """A silent refusal would read as a merge that happened."""
        from spec_runner import runner

        said: list[str] = []
        monkeypatch.setattr(runner, "log_progress", lambda msg, tid=None: said.append(msg))
        root = _repo(tmp_path)
        _work_on_a_task_branch(root)

        post_done_hook(_task(), _cfg(root), True)

        note = "\n".join(said)
        assert "integration_pr" in note
        assert "unmerged" in note
        assert "master" in note, "name the branch it refused to touch"

    def test_an_active_integration_branch_is_merged_into(self, tmp_path):
        """The refusal must not break the mode it protects."""
        root = _repo(tmp_path)
        master_before = _git(root, "rev-parse", "master").stdout.strip()
        _git(root, "checkout", "-q", "-b", "spec-runner/run-20260813")
        integration_before = _git(root, "rev-parse", "HEAD").stdout.strip()
        branch = _work_on_a_task_branch(root)

        cfg = _cfg(root, main_branch="spec-runner/run-20260813")
        cfg.integration_branch_active = True
        ok, _error, *_ = post_done_hook(_task(), cfg, True)

        assert ok is True
        assert _git(root, "rev-parse", "master").stdout.strip() == master_before
        assert _git(root, "rev-parse", "spec-runner/run-20260813").stdout.strip() != (
            integration_before
        ), "the task branch was merged into the integration branch"
        assert branch  # the branch existed to be merged

    def test_without_the_mode_nothing_changes(self, tmp_path):
        """A project that never asked for `integration_pr` self-merges exactly
        as before — the guard is dormant, not a new policy."""
        root = _repo(tmp_path)
        master_before = _git(root, "rev-parse", "master").stdout.strip()
        _work_on_a_task_branch(root)

        post_done_hook(_task(), _cfg(root, integration_pr=False), True)

        assert _git(root, "rev-parse", "master").stdout.strip() != master_before


class TestTheMarkerIsSetByTheForkItself:
    """Otherwise the guard is pinned only against tests that set the flag by
    hand — and a fork that stopped setting it would sail through, which is
    exactly how a guard rots."""

    def test_forking_sets_it(self, tmp_path):
        import argparse

        from spec_runner import cli

        root = _repo(tmp_path)
        cfg = _cfg(root)

        run = cli._maybe_start_integration(argparse.Namespace(dry_run=False), cfg)

        assert run is not None
        assert cfg.integration_branch_active is True
        assert cfg.main_branch == run.branch
        assert run.branch != "master"

    def test_a_failed_fork_leaves_it_off(self, tmp_path, monkeypatch):
        """A fork that could not happen must not leave the mode believing it
        did — that is the state in which the merge stage would touch main."""
        import argparse

        from spec_runner import cli

        monkeypatch.setattr(cli, "create_integration_branch", lambda *a, **k: None)
        root = _repo(tmp_path)
        cfg = _cfg(root)

        assert cli._maybe_start_integration(argparse.Namespace(dry_run=False), cfg) is None
        assert cfg.integration_branch_active is False

    def test_the_mode_off_leaves_it_off(self, tmp_path):
        import argparse

        from spec_runner import cli

        cfg = _cfg(_repo(tmp_path), integration_pr=False)

        assert cli._maybe_start_integration(argparse.Namespace(dry_run=False), cfg) is None
        assert cfg.integration_branch_active is False


class TestRetryParticipates:
    def test_it_forks_and_finalizes_like_run(self, tmp_path, monkeypatch):
        """A retry is a run of one task. Under `integration_pr` it must collect
        its work the same way, or the guard above would leave every retry's
        output stranded with nobody told what to do next."""
        import argparse

        from spec_runner import cli

        calls: list[str] = []
        monkeypatch.setattr(
            cli, "_maybe_start_integration", lambda *a, **k: calls.append("fork") or None
        )
        monkeypatch.setattr(cli, "execute_task", lambda *a, **k: False)
        monkeypatch.setattr(cli, "_enforce_spec_governance", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_enforce_clean_spec", lambda *a, **k: None)

        root = _repo(tmp_path)
        cfg = _cfg(root)
        cfg.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.tasks_file.write_text(
            "### TASK-101: t\n🔴 P1 | ⬜ TODO | Est: 1h\n\n**Depends on:** —\n"
        )

        cli.cmd_retry(argparse.Namespace(task_id="TASK-101", fresh=False), cfg)

        assert calls == ["fork"], "retry must open the run's integration branch"

    def test_the_pr_is_opened_even_when_the_retry_fails(self, tmp_path, monkeypatch):
        """`run` finalizes in a `finally` for the same reason: the work that
        did land still needs somewhere to be reviewed."""
        import argparse

        from spec_runner import cli
        from spec_runner.git_ops import IntegrationRun

        finalized: list[str] = []
        monkeypatch.setattr(
            cli,
            "_maybe_start_integration",
            lambda *a, **k: IntegrationRun("spec-runner/run-x", "master"),
        )
        monkeypatch.setattr(
            cli, "finalize_integration_branch", lambda *a, **k: finalized.append("done") or None
        )
        monkeypatch.setattr(cli, "execute_task", lambda *a, **k: False)
        monkeypatch.setattr(cli, "_enforce_spec_governance", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_enforce_clean_spec", lambda *a, **k: None)

        root = _repo(tmp_path)
        cfg = _cfg(root)
        cfg.tasks_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.tasks_file.write_text(
            "### TASK-101: t\n🔴 P1 | ⬜ TODO | Est: 1h\n\n**Depends on:** —\n"
        )

        cli.cmd_retry(argparse.Namespace(task_id="TASK-101", fresh=False), cfg)

        assert finalized == ["done"]
