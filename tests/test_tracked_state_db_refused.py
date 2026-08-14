"""#273: a git-tracked state database is emptied by a run that exits 0.

Found by the free rehearsal of the published v2.32.0 artifact, not in a paid
run, and measured there before being fixed here:

```
$ sqlite3 spec/.executor-state.db "select id,new_limit_usd from budget_authorizations"
1|9.0
2|12.0
$ git add -A -f && git commit -m "commit runtime state on purpose"
$ spec-runner run --task=TASK-002
… Execution summary  completed=1 failed=0 remaining=0        ← success
$ sqlite3 spec/.executor-state.db "select id from budget_authorizations"
Error: in prepare, no such table: budget_authorizations
$ wc -c < spec/.executor-state.db
0
```

The mechanism is the guard for the *other* half of this hazard (#62/#67) seen
from behind: `stage_all_except_runtime` untracks the live file with
`git rm --cached` — which is right, and is what keeps runtime state out of a
task's commit — the task commit then removes it from the tree, and the next
`git checkout -- .` writes that absence over the open SQLite connection. Gone
with it: the cost ledger, the budget authorizations, every red checkpoint and
every claim. The run says nothing, because `ExecutorState` keeps serving from a
handle to a file that no longer has contents.

The tool prevents *creating* this state and never checked whether it was *in*
it. A repository that committed the database before adopting the gitignore, or
lost the gitignore in a merge, is one run away from losing its ledger.
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.git_ops import tracked_state_paths
from spec_runner.state import ExecutorState


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _project(tmp_path: Path, **overrides) -> ExecutorConfig:
    root = tmp_path / "repo"
    (root / "spec").mkdir(parents=True, exist_ok=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "o@e.c")
    _git(root, "config", "user.name", "O")
    (root / "README.md").write_text("x\n")
    (root / "spec" / "tasks.md").write_text(
        "### TASK-001: t\n🔴 P1 | ⬜ TODO | Est: 1h\n\n**Depends on:** —\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")

    defaults: dict = {
        "project_root": root,
        "state_file": root / "spec" / ".executor-state.db",
        "logs_dir": root / "spec" / ".logs",
        "auto_commit": True,
        "create_git_branch": True,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _track_the_database(cfg: ExecutorConfig) -> None:
    """What a project reaches by committing before `spec/.gitignore` exists —
    which is exactly how the rehearsal reached it."""
    with ExecutorState(cfg) as state:
        state.set_meta("something", "worth keeping")
    _git(cfg.project_root, "add", "-A", "-f")
    _git(cfg.project_root, "commit", "-qm", "commit runtime state")


class TestTheQuestionIsAskedOfGit:
    def test_a_healthy_project_answers_empty(self, tmp_path):
        cfg = _project(tmp_path)
        with ExecutorState(cfg) as state:
            state.set_meta("x", "y")

        assert tracked_state_paths(cfg) == []

    def test_a_tracked_database_is_named(self, tmp_path):
        cfg = _project(tmp_path)
        _track_the_database(cfg)

        assert tracked_state_paths(cfg) == ["spec/.executor-state.db"]

    def test_a_tracked_sidecar_counts_too(self, tmp_path):
        """A tracked `-wal` is the same hazard with a smaller blast radius:
        SQLite's write-ahead log reverted under an open connection loses the
        transactions that have not been checkpointed. Found by mutation —
        dropping the sidecars from the query passed every other test here."""
        cfg = _project(tmp_path)
        with ExecutorState(cfg) as state:
            state.set_meta("x", "y")
        wal = cfg.state_file.with_name(cfg.state_file.name + "-wal")
        wal.touch()
        _git(cfg.project_root, "add", "-f", "--", str(wal.relative_to(cfg.project_root)))
        _git(cfg.project_root, "commit", "-qm", "track the wal only")

        assert tracked_state_paths(cfg) == ["spec/.executor-state.db-wal"]

    def test_a_state_file_outside_the_repo_is_not_git_business(self, tmp_path):
        """`--spec-prefix` and orchestrators can put it anywhere; a path git
        never saw cannot be tracked, and asking would be a category error."""
        cfg = _project(tmp_path, state_file=tmp_path / "elsewhere.db")

        assert tracked_state_paths(cfg) == []

    def test_a_directory_that_is_not_a_repo_answers_empty(self, tmp_path):
        plain = tmp_path / "plain"
        (plain / "spec").mkdir(parents=True)
        cfg = ExecutorConfig(
            project_root=plain,
            state_file=plain / "spec" / ".executor-state.db",
            logs_dir=plain / "spec" / ".logs",
        )
        cfg.logs_dir.mkdir(parents=True, exist_ok=True)

        assert tracked_state_paths(cfg) == []


class TestTheRunIsRefused:
    def _args(self, **kw):
        defaults = {
            "task": "TASK-001",
            "all": False,
            "dry_run": False,
            "milestone": None,
            "no_reset_failed": False,
            "hitl_review": False,
            "tui": False,
            "force": False,
            "allow_dirty_spec": True,
            "json_result": False,
        }
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_it_stops_before_anything_runs(self, tmp_path, capsys):
        from spec_runner import cli

        cfg = _project(tmp_path)
        _track_the_database(cfg)

        with pytest.raises(SystemExit) as exc:
            cli.cmd_run(self._args(), cfg)

        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "tracking the executor's state database" in out
        assert "spec/.executor-state.db" in out

    def test_it_prints_the_way_out(self, tmp_path, capsys):
        """A refusal an operator cannot act on is a wall. The fix is two
        commands and the file survives both, so the refusal says them."""
        from spec_runner import cli

        cfg = _project(tmp_path)
        _track_the_database(cfg)

        with pytest.raises(SystemExit):
            cli.cmd_run(self._args(), cfg)

        out = capsys.readouterr().out
        assert "git rm --cached spec/.executor-state.db" in out

    def test_the_ledger_is_still_there_after_the_refusal(self, tmp_path):
        """The point of stopping early: what the run would have destroyed is
        untouched."""
        from spec_runner import cli

        cfg = _project(tmp_path)
        _track_the_database(cfg)

        with pytest.raises(SystemExit):
            cli.cmd_run(self._args(), cfg)

        with ExecutorState(cfg) as state:
            assert state.get_meta("something") == "worth keeping"

    def test_a_healthy_project_is_not_stopped(self, tmp_path, monkeypatch):
        """Dormant where the hazard does not exist — the guard must not become
        a new reason for a working project to refuse to run."""
        from spec_runner import cli

        cfg = _project(tmp_path)
        with ExecutorState(cfg) as state:
            state.set_meta("x", "y")

        cli._enforce_untracked_state(cfg)  # no SystemExit

    def test_without_git_automation_it_is_dormant(self, tmp_path):
        """Nothing then touches the tree, so nothing can empty the file — the
        same dormancy rule as the dirty-spec guard (#69)."""
        from spec_runner import cli

        cfg = _project(tmp_path, auto_commit=False, create_git_branch=False)
        _track_the_database(cfg)

        cli._enforce_untracked_state(cfg)  # no SystemExit

    def test_retry_is_guarded_too(self, tmp_path, capsys):
        """`retry` runs a task, so it destroys the same file — and it is the
        command that has twice been found not participating in a guard the run
        path had (#254, #255)."""
        from spec_runner import cli

        cfg = _project(tmp_path)
        _track_the_database(cfg)

        with pytest.raises(SystemExit) as exc:
            cli.cmd_retry(argparse.Namespace(task_id="TASK-001", fresh=False), cfg)

        assert exc.value.code == 1
        assert "state database" in capsys.readouterr().out

    def test_watch_is_guarded_too(self, tmp_path, capsys):
        from spec_runner import cli

        cfg = _project(tmp_path)
        _track_the_database(cfg)

        with pytest.raises(SystemExit) as exc:
            cli.cmd_watch(
                argparse.Namespace(
                    interval=1, tui=False, allow_dirty_spec=True, strict=False, no_strict=False
                ),
                cfg,
            )

        assert exc.value.code == 1
        assert "state database" in capsys.readouterr().out


@pytest.mark.slow
class TestTheLossItself:
    """What the guard prevents, demonstrated once rather than assumed.

    Without this the suite would pin a refusal whose justification lives only
    in a commit message — and the justification is the whole reason the refusal
    is allowed to stop a run.
    """

    def test_a_tracked_database_does_not_survive_a_run(self, tmp_path, monkeypatch):
        """The guard is switched off here on purpose: what is being shown is
        the loss it exists to prevent, through the ordinary run path."""
        from spec_runner import cli

        cfg = _project(tmp_path)
        _track_the_database(cfg)
        assert cfg.state_file.stat().st_size > 0

        script = cfg.project_root / "fake-cli"
        script.write_text(
            "#!/usr/bin/env bash\ncat <<'EOF'\n"
            '{"total_cost_usd": 0.0, "result": "done\\nTASK_COMPLETE\\n"}\n'
            "EOF\n"
        )
        script.chmod(0o755)
        # Committed: the branch stage stashes untracked files before it starts
        # (#231), so an uncommitted stand-in would not survive to be run.
        _git(cfg.project_root, "add", "-A", "-f")
        _git(cfg.project_root, "commit", "-qm", "stand-in cli")
        cfg.claude_command = str(script)
        cfg.run_tests_on_done = False
        cfg.run_lint_on_done = False
        cfg.run_review = False

        monkeypatch.setattr(cli, "_enforce_untracked_state", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_enforce_clean_spec", lambda *a, **k: None)
        args = argparse.Namespace(
            task="TASK-001",
            all=False,
            dry_run=False,
            milestone=None,
            no_reset_failed=False,
            hitl_review=False,
            tui=False,
            force=False,
            allow_dirty_spec=True,
            json_result=False,
        )
        with contextlib.suppress(SystemExit):
            cli.cmd_run(args, cfg)

        with ExecutorState(cfg) as state:
            survived = state.get_meta("something")
        assert survived != "worth keeping", (
            "if the ledger ever survives this, the guard can be reconsidered — "
            "until then it is protecting against a measured loss"
        )
