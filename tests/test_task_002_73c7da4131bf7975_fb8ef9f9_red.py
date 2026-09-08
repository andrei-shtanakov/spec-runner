"""RED for TASK-002 (DT-02): invoking a declared file target through the
live verify-first run, and what tree its composition is judged against.

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/30-decomposition.md#DT-02
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-07

kind: integration — real git/pytest subprocesses against a fixture repo,
mirroring the existing "judges the commit, not the tree" node-id coverage in
tests/test_verify_run_order.py::TestLiveRunJudgesTheNamedCommit, but for a
declared file target instead of a node id.
"""

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.live_verify import run_live_verify
from spec_runner.task import Task


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests").mkdir()
    return root


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


def _cfg(root: Path) -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        test_command="python -m pytest",
        max_retries=1,
        retry_delay_seconds=0,
        create_git_branch=False,
        run_tests_on_done=False,
        auto_commit=False,
        run_review=False,
        callback_url="",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


class TestFileTargetCompositionJudgesTheCommit:
    """kind: integration — BEH-07: a declared file target's member
    composition is resolved against the judged commit's tree, never against
    whatever the working tree happens to hold when the run executes."""

    def test_a_dirty_working_tree_does_not_change_which_members_are_judged(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_a():\n    assert True\n")
        _commit(root, "base: only test_a exists in this commit")

        # The working tree now diverges from the judged commit: it adds a
        # test that would fail if it were ever collected. If the declared
        # file target's composition were resolved from this dirty tree
        # instead of the judged commit, this failing member would be picked
        # up by the live run and the group would not be green.
        (root / "tests" / "test_group.py").write_text(
            "def test_a():\n    assert True\n\n\n"
            "def test_c_never_committed():\n    assert False, 'must not run'\n"
        )

        task = Task(
            id="TASK-101",
            name="verify-first file target",
            priority="p1",
            status="todo",
            estimate="1h",
            execution_mode="verify_first",
            verifies=["tests/test_group.py"],
        )
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.ran and result.passed, (
            "a declared file target must be invoked and its composition "
            "resolved against the judged commit, not the dirty working "
            f"tree it happens to run near: {result.detail}"
        )
