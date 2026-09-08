"""RED for BEH-28 (DT-07, group state-and-surfaces): the size of the
resolved composition must be announced in the run's own progress, as soon
as the collection phase of the *same* process has reported it — earlier
than any per-member outcome or the run's final result, and without a
second runner invocation or a read of the working tree.

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-28
Traces: FR-21

Today `run_live_verify` only ever mirrors the *declared* group's length
(`len(task.verifies)`, i.e. the number of `**Verifies:**` elements) via its
pre-run budget line — never the *resolved* composition size a file target
expands into. A file target that names one file containing several tests
therefore never gets its member count announced anywhere in the run's
progress stream; a caller watching `log_progress` cannot tell how large the
composition turned out to be until the whole task finishes and evidence is
read back. This fails on that missing announcement, not on an import error
or a crash: `run_live_verify` runs for real, passes, and simply never says
"3" anywhere.

kind: integration — a real git repo, a real pytest subprocess replay
(`run_live_verify`), no mocked runner and no paid agent call.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.live_verify import run_live_verify
from spec_runner.task import Task


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    tests_dir = root / "tests"
    tests_dir.mkdir()
    # A single file-target selector that resolves to THREE collected
    # members (two executed, one accounted skip) — deliberately more than
    # the ONE declared `**Verifies:**` element, so a line that merely
    # echoes `len(task.verifies)` (today's only pre-run announcement)
    # cannot be mistaken for the resolved composition size.
    (tests_dir / "test_target.py").write_text(
        "import pytest\n\n"
        "def test_a():\n"
        "    assert True\n\n"
        "def test_b():\n"
        "    assert True\n\n"
        "@pytest.mark.skip(reason='not ready yet')\n"
        "def test_c():\n"
        "    assert True\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "test_command": "python -m pytest",
        "max_retries": 1,
        "retry_delay_seconds": 0,
        "create_git_branch": False,
        "run_tests_on_done": False,
        "auto_commit": False,
        "run_review": False,
        "callback_url": "",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(**overrides) -> Task:
    defaults: dict = {
        "id": "TASK-BEH28",
        "name": "verify-first file target",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
        "execution_mode": "verify_first",
        "verifies": ["tests/test_target.py"],
    }
    defaults.update(overrides)
    return Task(**defaults)


class TestBEH28ComposedSizeIsAnnouncedFromTheCollectionPhase:
    """kind: integration — BEH-28: once the same run's collection phase has
    reported the resolved composition, its size is announced in the run's
    progress before anything about individual outcomes or the run's
    result — not derived from a second invocation or a working-tree read."""

    def test_collected_composition_size_is_announced_in_progress(self, tmp_path):
        root = _repo(tmp_path)
        task = _task()
        config = _cfg(root)

        lines: list[str] = []
        result = run_live_verify(task, config, log_progress=lines.append)

        assert result.passed, f"fixture file target must be green to test BEH-28: {result!r}"
        assert len(result.composition) == 3, (
            "sanity: the file target must resolve to all three collected members "
            f"(two executed, one accounted skip) — got {result.composition!r}"
        )

        composition_size_lines = [
            line
            for line in lines
            if "3" in line
            and any(kw in line.lower() for kw in ("composition", "collected", "member"))
        ]
        assert composition_size_lines, (
            "BEH-28: the resolved composition size (3 collected members) must be "
            "announced via log_progress as soon as this run's own collection "
            "phase reported it — before any per-member outcome or the run's "
            f"final result. Declaring only the group's pre-run budget line is not "
            f"enough (the declared group has just ONE `**Verifies:**` element, "
            f"not three). Progress lines actually seen: {lines!r}"
        )
