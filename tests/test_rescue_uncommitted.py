"""#231 (F-27): a task start must not destroy uncommitted work.

The branch stage begins every task by reverting tracked files and deleting
untracked ones, so one task's leftovers cannot contaminate the next one's
tests. It did that silently and irreversibly. In the pilot, a review agent's
fixes — two modified files, four new fixtures, suite 252/0 green, later
accepted via `tdd repair` — were destroyed by the next `retry`, and only a
byte-exact snapshot taken by hand beforehand recovered them.

Two things worth stating precisely, because both differ from how the finding
was first written up:

- The loss was attributed to the claim-violation refusal. It is not: the wipe
  happens in `pre_start_hook`, **before any gate is consulted**. Every task
  start on a repo with git automation on did this, whatever the tree held and
  whatever the run later decided.
- "Refusing to implement" reads as a no-op. It was not — the tree had already
  been mutated by the time that line was printed.

The contract pinned here: whatever the tree carries is preserved before the
cleanup, recoverably and out loud; and if it cannot be preserved, the task does
not start. Destroying work is never the fallback for failing to save it.
"""

import hashlib
import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.hooks import pre_start_hook, rescue_uncommitted
from spec_runner.task import Task


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    _git(root, "config", "user.email", "t@e.c")
    _git(root, "config", "user.name", "T")
    (root / "lib.py").write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".executor-state.db",
        "logs_dir": root / ".executor-logs",
        "create_git_branch": True,
        "sync_deps": False,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task() -> Task:
    return Task(id="TASK-101", name="t", priority="p1", status="todo", estimate="1h")


def _tree_digest(root: Path) -> str:
    """md5 over every non-git file's name and bytes — the pilot's own method."""
    h = hashlib.md5()
    for p in sorted(root.rglob("*")):
        if ".git" in p.parts or not p.is_file():
            continue
        h.update(str(p.relative_to(root)).encode())
        h.update(p.read_bytes())
    return h.hexdigest()


def _strand_work(root: Path) -> None:
    """The pilot's shape: a modified tracked file plus new untracked fixtures."""
    (root / "lib.py").write_text("x = 1\ndef validate_unique_ids():\n    pass\n")
    (root / "fixture_a.toml").write_text("a = 1\n")
    (root / "fixture_b.toml").write_text("b = 2\n")


@pytest.mark.slow
class TestTheWorkSurvives:
    def test_the_bytes_come_back_identical(self, tmp_path):
        """The whole point, measured the way the pilot measured its loss."""
        root = _repo(tmp_path)
        _strand_work(root)
        before = _tree_digest(root)

        pre_start_hook(_task(), _cfg(root))

        assert _git(root, "status", "--porcelain").stdout.strip() == "", (
            "the cleanup should still happen — this fix is about recoverability, "
            "not about leaving one task's leftovers in the next one's tree"
        )
        _git(root, "checkout", "main")
        assert _git(root, "stash", "pop").returncode == 0
        assert _tree_digest(root) == before

    def test_untracked_files_are_rescued_too(self, tmp_path):
        """`git clean -fd` deleted these outright; a stash without
        `--include-untracked` would leave them exactly as lost."""
        root = _repo(tmp_path)
        _strand_work(root)

        pre_start_hook(_task(), _cfg(root))
        assert not (root / "fixture_a.toml").exists()  # cleaned, as before

        _git(root, "checkout", "main")
        _git(root, "stash", "pop")
        assert (root / "fixture_a.toml").read_text() == "a = 1\n"
        assert (root / "fixture_b.toml").read_text() == "b = 2\n"

    def test_the_stash_names_the_task_that_took_it(self, tmp_path):
        """An operator finding four stashes needs to know which run made which."""
        root = _repo(tmp_path)
        _strand_work(root)

        pre_start_hook(_task(), _cfg(root))

        assert "TASK-101" in _git(root, "stash", "list").stdout

    def test_it_says_so_out_loud(self, tmp_path, monkeypatch):
        """Silence is what made this data loss instead of an inconvenience."""
        from spec_runner import hooks

        lines: list[str] = []
        monkeypatch.setattr(
            "spec_runner.runner.log_progress", lambda msg, tid=None: lines.append(msg)
        )
        root = _repo(tmp_path)
        _strand_work(root)

        hooks.rescue_uncommitted(_task(), _cfg(root))

        said = "\n".join(lines)
        assert "stashed" in said
        assert "git stash" in said, "the message must name the way back"


@pytest.mark.slow
class TestTheOrdinaryCaseIsUnchanged:
    def test_a_clean_tree_creates_no_stash(self, tmp_path):
        root = _repo(tmp_path)

        ok, note = rescue_uncommitted(_task(), _cfg(root))

        assert (ok, note) == (True, "")
        assert _git(root, "stash", "list").stdout.strip() == ""

    def test_runtime_state_is_never_stashed(self, tmp_path):
        """The state DB is open by the very process doing the stashing —
        stashing it would pull the file out from under the run."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        cfg.state_file.write_text("live db")
        (cfg.logs_dir / "TASK-101.log").write_text("log")

        ok, note = rescue_uncommitted(_task(), cfg)

        assert (ok, note) == (True, "")
        assert cfg.state_file.read_text() == "live db"
        assert _git(root, "stash", "list").stdout.strip() == ""

    def test_without_git_automation_nothing_is_touched(self, tmp_path):
        """No branch stage means no cleanup, so there is nothing to rescue
        from — and a run that never destroys must not start stashing."""
        root = _repo(tmp_path)
        _strand_work(root)
        before = _tree_digest(root)

        pre_start_hook(_task(), _cfg(root, create_git_branch=False))

        assert _tree_digest(root) == before
        assert _git(root, "stash", "list").stdout.strip() == ""


@pytest.mark.slow
class TestWhenItCannotBeSaved:
    def _break_stash(self, monkeypatch) -> None:
        from spec_runner import hooks

        real = subprocess.run

        def fake(argv, *a, **k):
            if isinstance(argv, list) and argv[:2] == ["git", "stash"]:
                return subprocess.CompletedProcess(argv, 1, "", "fatal: cannot stash")
            return real(argv, *a, **k)

        monkeypatch.setattr(hooks.subprocess, "run", fake)

    def test_the_task_does_not_start(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        _strand_work(root)
        self._break_stash(monkeypatch)

        assert pre_start_hook(_task(), _cfg(root)) is False

    def test_and_the_work_is_left_exactly_as_it_was(self, tmp_path, monkeypatch):
        """The one thing that must never happen: cleaning after failing to
        save. A refusal to start is recoverable; a wiped tree is not."""
        root = _repo(tmp_path)
        _strand_work(root)
        before = _tree_digest(root)
        self._break_stash(monkeypatch)

        pre_start_hook(_task(), _cfg(root))

        assert _tree_digest(root) == before
        assert (root / "fixture_a.toml").exists()

    def _break_status(self, monkeypatch) -> None:
        from spec_runner import git_ops

        real = git_ops._git

        def fake(config, *args):
            if args[:2] == ("status", "--porcelain"):
                return subprocess.CompletedProcess(
                    ["git", *args], 1, "", "fatal: unable to read index"
                )
            return real(config, *args)

        monkeypatch.setattr(git_ops, "_git", fake)

    def test_an_unreadable_tree_is_not_a_clean_tree(self, tmp_path, monkeypatch):
        """`uncommitted_work_paths` fails open by design — it is a report. Read
        that way here and an index lock would read as "nothing to save" and
        license the wipe (Copilot, PR #234). The guard asks in strict mode."""
        root = _repo(tmp_path)
        _strand_work(root)
        before = _tree_digest(root)
        self._break_status(monkeypatch)

        assert pre_start_hook(_task(), _cfg(root)) is False
        assert _tree_digest(root) == before
        assert (root / "fixture_a.toml").exists()

    def test_the_report_still_fails_open(self, tmp_path, monkeypatch):
        """The other half of the same distinction: naming stranded work must
        never become a new way for a blocked task to fail."""
        from spec_runner.git_ops import uncommitted_work_paths

        root = _repo(tmp_path)
        _strand_work(root)
        self._break_status(monkeypatch)

        assert uncommitted_work_paths(_cfg(root)) == []

    def test_the_refusal_explains_itself(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        _strand_work(root)
        self._break_stash(monkeypatch)

        ok, detail = rescue_uncommitted(_task(), _cfg(root))

        assert ok is False
        assert "could not preserve" in detail
        assert "lib.py" in detail
