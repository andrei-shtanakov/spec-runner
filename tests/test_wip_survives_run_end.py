"""`run --all` exhausts its retries, then a separate `retry` continues from WIP.

Final-review Critical #1: the run's end switched to the main branch
(`ensure_on_main_branch`, and under `integration_pr` the finalizer's checkout of
the base), which carried the owned task's dirt off its branch. The next
`retry` found no owner and stashed the work. Spec §1: WIP is saved before
every switch that would leave the owned branch.

Agent calls are stubbed only at `paid_call._spawn`; everything else is real.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner import cli, paid_call
from spec_runner.state import ExecutorState
from tests.test_retry_continues_from_wip import BRANCH, _cfg, _git
from tests.test_retry_continues_from_wip import repo as repo  # noqa: F401


def _write_config(root: Path, *, integration_pr: bool) -> None:
    (root / "spec-runner.config.yaml").write_text(
        f"""max_retries: 2
retry_delay_seconds: 0
task_timeout_minutes: 1
integration_pr: {"true" if integration_pr else "false"}
harness_guard: strict
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
    _git(root, "add", "spec-runner.config.yaml")
    _git(root, "commit", "-qm", "config")


def _always_time_out(root: Path):
    """Each attempt writes a new untracked file — dirt `git checkout` carries silently."""
    calls: list[int] = []

    def _spawn(invocation, *, timeout, cwd, env):
        calls.append(1)
        (root / f"attempt_{len(calls)}.py").write_text(f"# attempt {len(calls)}\n")
        raise subprocess.TimeoutExpired(invocation.argv, timeout)

    return _spawn


def _stashed_paths(root: Path) -> set[str]:
    """Every path any stash holds, tracked or untracked.

    A task start may legitimately stash a leftover status flip (spec §1); what
    must never be stashed is the task's own work.
    """
    paths: set[str] = set()
    for ref in _git(root, "stash", "list", "--format=%gd").split():
        shown = _git(root, "stash", "show", "--include-untracked", "--name-only", ref)
        paths.update(shown.split())
    return paths


def _main(argv: list[str]) -> int:
    try:
        cli.main(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


@pytest.fixture(params=[False, True], ids=["per-task-branch", "integration_pr"])
def integration_pr(request) -> bool:
    return bool(request.param)


def test_retry_after_run_all_sees_the_work(repo, monkeypatch, integration_pr):  # noqa: F811
    _write_config(repo, integration_pr=integration_pr)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_post_pr_review_stage", lambda *a, **k: None)
    monkeypatch.setattr(paid_call, "_spawn", _always_time_out(repo))

    assert _main(["run", "--all"]) != 0, "every attempt timed out"

    log = _git(repo, "log", BRANCH, "--format=%s%n%b")
    assert "wip(TASK-070): unfinished work of attempt 2 — not a candidate" in log
    assert "Spec-Runner-WIP: TASK-070" in log
    assert _git(repo, "show", f"{BRANCH}:attempt_2.py") == "# attempt 2\n"
    assert not {"attempt_1.py", "attempt_2.py"} & _stashed_paths(repo)

    seen: list[bool] = []

    def _look(invocation, *, timeout, cwd, env):
        seen.append((repo / "attempt_1.py").exists() and (repo / "attempt_2.py").exists())
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    monkeypatch.setattr(paid_call, "_spawn", _look)
    _main(["retry", "TASK-070"])

    assert seen == [True], "the retry lost the last attempt's work"
    assert not {"attempt_1.py", "attempt_2.py"} & _stashed_paths(repo)


def test_a_refused_wip_save_at_run_end_leaves_tree_and_branch(repo, monkeypatch, capsys):  # noqa: F811
    """A refusal is reported and the switch is skipped: nothing is carried off.

    The last attempt leaves a partially staged file, which `save_wip` refuses
    (spec §1, "the index is not overwritten") — a real refusal, no stub.
    """
    _write_config(repo, integration_pr=False)
    monkeypatch.chdir(repo)
    calls: list[int] = []

    def _spawn(invocation, *, timeout, cwd, env):
        calls.append(1)
        if len(calls) == 2:
            (repo / "feature.py").write_text("staged\n")
            _git(repo, "add", "feature.py")
            (repo / "feature.py").write_text("unstaged\n")
        raise subprocess.TimeoutExpired(invocation.argv, timeout)

    monkeypatch.setattr(paid_call, "_spawn", _spawn)

    assert _main(["run", "--all"]) != 0

    assert len(calls) == 2
    err = capsys.readouterr().err
    assert "feature.py" in err and "WIP" in err
    assert _git(repo, "branch", "--show-current").strip() == BRANCH
    assert (repo / "feature.py").read_text() == "unstaged\n"
    assert _git(repo, "show", ":feature.py") == "staged\n"
    assert not {"attempt_1.py", "attempt_2.py"} & _stashed_paths(repo)


def test_unowned_dirt_still_switches(repo, monkeypatch):  # noqa: F811
    """Not a task's work: the run's end switches to main as before."""
    from spec_runner.git_ops import ensure_on_main_branch

    cfg = _cfg(repo)
    _git(repo, "checkout", "-q", "-b", "task/task-555-x")
    (repo / "stray.py").write_text("x\n")
    with ExecutorState(cfg) as st:
        assert cli._leave_owned_branch(cfg, st, ensure_on_main_branch) is True
    assert _git(repo, "branch", "--show-current").strip() == "main"
