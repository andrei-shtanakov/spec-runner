"""Integration coverage for BEH-07 (TASK-002, DT-02): a declared file
target's member composition is resolved against the judged commit's tree,
never against whatever the working tree happens to hold when the live run
executes.

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-07
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/30-decomposition.md#DT-02

kind: integration — real git/pytest subprocesses against a fixture repo.
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


def _task() -> Task:
    return Task(
        id="TASK-201",
        name="verify-first file target",
        priority="p1",
        status="todo",
        estimate="1h",
        execution_mode="verify_first",
        verifies=["tests/test_group.py"],
    )


class TestFileTargetCompositionIsResolvedAgainstTheJudgedCommit:
    """BEH-07: given a fixture repo where the judged commit's declared file
    holds tests A and B, and the working tree carries an uncommitted edit
    that drops B and adds a failing C — the resolved composition is the
    commit's (A and B), and C never enters it."""

    def test_dirty_tree_drops_and_adds_members_but_the_commits_composition_wins(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n"
        )
        _commit(root, "base: A and B exist in this commit")

        # The working tree now diverges from the judged commit: B is
        # dropped, and a failing C is added. A live run judging the DIRTY
        # tree would run A and the failing C — never B — and would not be
        # green. A live run judging the COMMIT runs A and B, never C, and
        # stays green.
        (root / "tests" / "test_group.py").write_text(
            "def test_a():\n    assert True\n\n\n"
            "def test_c_never_committed():\n    assert False, 'must not run'\n"
        )

        config = _cfg(root)
        result = run_live_verify(_task(), config)

        assert result.ran and result.passed, (
            "the judged commit's composition (A and B) must be what runs, "
            f"not the dirty tree's (A and C): {result.detail}"
        )

    def test_resolution_is_reproducible_from_the_same_sha_and_tracks_a_new_one(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_a():\n    assert True\n")
        _commit(root, "first commit: only test_a")
        config = _cfg(root)

        first = run_live_verify(_task(), config)
        second = run_live_verify(_task(), config)

        assert first.ran and first.passed, first.detail
        assert second.ran and second.passed, second.detail
        assert first.sha == second.sha
        assert first.group_executed == second.group_executed

        # A new commit changes the file's composition to include a failing
        # test. The live run must judge THAT commit's composition afresh,
        # not replay the earlier answer.
        (root / "tests" / "test_group.py").write_text(
            "def test_a():\n    assert True\n\n\ndef test_b_added_later():\n    assert False\n"
        )
        _commit(root, "second commit: test_b now fails")

        third = run_live_verify(_task(), config)

        assert third.sha != first.sha
        assert third.ran and not third.passed, (
            "the new commit's composition includes the failing test_b; the "
            f"live run must judge it, not replay the earlier commit's answer: {third.detail}"
        )
