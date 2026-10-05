"""Path lists travel on stdin, never argv (final review #3, E2BIG).

`git status -uall` lists every untracked file singly, and those paths went to
`git add`, `commit --only`, `stash push` and `reset` as arguments. With enough
of them (≈40k ordinary names, or a few thousand long ones) `execve` fails with
`OSError: [Errno 7] Argument list too long` — a traceback at the very point
that must either save the work or refuse cleanly.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.hooks import rescue_uncommitted
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from tests.test_wip_save import BRANCH, _cfg, _git, _own, _save
from tests.test_wip_save import repo as repo  # noqa: F401

# Long names make the argv limit (ARG_MAX: 1 MiB on macOS, 2 MiB on Linux)
# reachable with a few thousand files, which keeps the test fast.
COUNT = 6000
DIR = "d" * 200


def _many_untracked(root: Path) -> list[str]:
    (root / DIR).mkdir()
    names = [f"{DIR}/{'f' * 200}_{i:05d}.py" for i in range(COUNT)]
    for name in names:
        (root / name).write_text("x\n")
    assert sum(len(n) + 1 for n in names) > 2 * 1024 * 1024
    return names


@pytest.mark.slow
def test_wip_saves_thousands_of_untracked_files(repo):  # noqa: F811
    cfg = _cfg(repo)
    _own(cfg)
    names = _many_untracked(repo)

    result = _save(cfg)

    assert result.refusal is None and result.saved_sha
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").split("\n")
    assert len([n for n in committed if n]) == COUNT
    assert names[-1] in committed
    assert _git(repo, "status", "--porcelain") == ""


@pytest.mark.slow
def test_rescue_stashes_thousands_of_untracked_files(repo):  # noqa: F811
    cfg = _cfg(repo)
    _git(repo, "checkout", "-q", "main")
    _many_untracked(repo)
    task = Task(
        id="TASK-060", name="w", priority="p0", status="todo", description="w", estimate="1d"
    )

    ok, note = rescue_uncommitted(task, cfg)

    assert ok, note
    assert "spec-runner rescue: TASK-060" in _git(repo, "stash", "list")
    assert not (repo / DIR).exists() or not any((repo / DIR).iterdir())


def test_rescue_maps_an_oserror_to_its_refusal(repo, monkeypatch):  # noqa: F811
    from spec_runner import hooks

    cfg = _cfg(repo)
    _git(repo, "checkout", "-q", "main")
    (repo / "app.py").write_text("x = 2\n")
    real_run = subprocess.run

    def _run(cmd, *args, **kwargs):
        if "stash" in cmd:
            raise OSError(7, "Argument list too long")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(hooks.subprocess, "run", _run)
    task = Task(
        id="TASK-060", name="w", priority="p0", status="todo", description="w", estimate="1d"
    )

    ok, detail = rescue_uncommitted(task, cfg)

    assert not ok
    assert "Argument list too long" in detail
    assert (repo / "app.py").read_text() == "x = 2\n"


def test_save_wip_oserror_is_an_instrument_refusal_at_the_start(repo, monkeypatch):  # noqa: F811
    """`_save_wip_or_refuse` already maps OSError; pinned here for the stdin path."""
    from spec_runner import hooks, wip
    from spec_runner.phases import RefusalKind

    cfg = _cfg(repo)
    _own(cfg)
    (repo / "app.py").write_text("x = 2\n")

    def _boom(*args, **kwargs):
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(wip, "git_with_paths", _boom)
    task = Task(
        id="TASK-060", name="w", priority="p0", status="todo", description="w", estimate="1d"
    )
    with ExecutorState(cfg) as st, pytest.raises(hooks.StartRefused) as refused:
        hooks._save_wip_or_refuse(task, cfg, st, None)
    assert refused.value.refusal.kind is RefusalKind.INSTRUMENT
    assert _git(repo, "branch", "--show-current").strip() == BRANCH
    assert (repo / "app.py").read_text() == "x = 2\n"


class TestStashPathspecs:
    """The collapse is exact: a directory is named once only when all of it goes."""

    def test_a_wholly_untracked_directory_is_named_once(self, repo):  # noqa: F811
        from spec_runner.git_ops import stash_pathspecs

        (repo / "u" / "v").mkdir(parents=True)
        (repo / "u" / "v" / "x.py").write_text("1\n")
        (repo / "u" / "y.py").write_text("2\n")
        (repo / "app.py").write_text("x = 2\n")
        paths = ["app.py", "u/v/x.py", "u/y.py"]
        assert stash_pathspecs(_cfg(repo), paths) == ["u/", "app.py"]

    def test_a_directory_holding_an_excluded_file_stays_per_file(self, repo):  # noqa: F811
        from spec_runner.git_ops import stash_pathspecs

        (repo / "u").mkdir()
        (repo / "u" / "keep.md").write_text("excluded\n")
        (repo / "u" / "y.py").write_text("2\n")
        assert stash_pathspecs(_cfg(repo), ["u/y.py"]) == ["u/y.py"]

    def test_the_excluded_file_survives_the_rescue(self, repo):  # noqa: F811
        from spec_runner.hooks import _rescue_uncommitted

        _git(repo, "checkout", "-q", "main")
        (repo / "u").mkdir()
        (repo / "u" / "keep.md").write_text("excluded\n")
        (repo / "u" / "y.py").write_text("2\n")
        ok, _ = _rescue_uncommitted(
            _cfg(repo), owner="run", task_id=None, exclude=[repo / "u" / "keep.md"]
        )
        assert ok
        assert (repo / "u" / "keep.md").read_text() == "excluded\n"
        assert not (repo / "u" / "y.py").exists()
