"""Integration coverage for BEH-07 (TASK-002, DT-02): a declared file
target's member composition is resolved against the judged commit's tree,
never against whatever the working tree happens to hold when the live run
executes.

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-07
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/30-decomposition.md#DT-02

kind: integration — real git/pytest subprocesses against a fixture repo.
"""

import json
import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.live_verify import run_live_verify
from spec_runner.task import Task
from spec_runner.tdd_runners import read_file_composition


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


class TestFileTargetEarlyStopStillReportsAGenuineFailure:
    """Review sr395, major finding 1 (`live_verify.py:168`): `-x`/`--maxfail`
    stops the run after the first accounted failure, leaving later members
    of the file silent for a known reason — a real, accounted failure must
    still classify as `test_failure`, never as `instrument_error` (FR-11's
    unconditional "падение любого члена даёт test_failure")."""

    def test_minus_x_failure_is_test_failure_not_instrument_error(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_a():\n    assert False, 'genuine failure'\n\n\n"
            "def test_b():\n    assert True\n"
        )
        _commit(root, "test_a fails, test_b would pass but -x stops before it runs")

        config = _cfg(root)
        config.test_command = "python -m pytest -x"
        result = run_live_verify(_task(), config)

        assert result.ran and not result.passed, (
            "test_a's accounted failure must be a real test_failure even "
            f"though -x stopped before test_b ran: {result.detail}"
        )


class TestFileTargetDeselectionStaysGreen:
    """Review sr395, major finding 2 (`live_verify.py:168`): `-k`/`-m`
    deselection happens after the reporter's composition snapshot, so a
    deselected member must be recorded as a NAMED skip (FR-10), not left
    silent — otherwise a fully green run is misread as `instrument_error`."""

    def test_deselected_member_does_not_block_green(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_a():\n    assert True\n\n\n"
            "def test_slow():\n    assert False, 'must not run: deselected'\n"
        )
        _commit(root, "test_a passes, test_slow is deselected by -k and never runs")

        config = _cfg(root)
        config.test_command = 'python -m pytest -k "not test_slow"'
        result = run_live_verify(_task(), config)

        assert result.ran and result.passed, (
            f"test_slow is deselected, not silent — the run must stay green: {result.detail}"
        )


class TestFileTargetXpassedIsNotProvenExecution:
    """Review sr395, minor finding 3 (`live_verify.py:172`): a non-strict
    xpass reports `report.outcome == "passed"` but is not an executed pass
    by the ExecutionProof standard FR-08 references — `xfailed`/`xpassed`
    are excluded from proven execution on the node-id path too."""

    def test_lone_xpassed_member_is_instrument_error_not_green(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "import pytest\n\n\n"
            "@pytest.mark.xfail(reason='expected to fail but does not', strict=False)\n"
            "def test_a():\n    assert True\n"
        )
        _commit(root, "test_a is an xfail marker that actually passes (xpass)")

        config = _cfg(root)
        result = run_live_verify(_task(), config)

        assert not result.ran and not result.passed, (
            "an xpassed member is not proven execution by itself; it must "
            f"not be the sole evidence of green: {result.detail}"
        )


class TestFileTargetCompositionAccumulatesAcrossCollectedRecords:
    """Review sr395, minor finding 4 (`vopros avtoru`, `tdd_runners.py:609`):
    `read_file_composition` used to REPLACE `members` on every "collected"
    record while `outcomes` accumulated — a manifest written by more than
    one process (e.g. pytest-xdist workers, each reporting its own chunk)
    would end up with `members` equal to only the last writer's chunk, and
    a failure collected by an earlier chunk would fall outside `members`
    entirely and never be judged (fail-open). Members must UNION across
    every "collected" record instead."""

    def test_two_collected_records_union_their_members(self, tmp_path):
        manifest = tmp_path / "composition.jsonl"
        lines = [
            {"phase": "collected", "members": ["tests/test_group.py::test_a"]},
            {
                "phase": "outcome",
                "nodeid": "tests/test_group.py::test_a",
                "outcome": "failed",
            },
            {"phase": "collected", "members": ["tests/test_group.py::test_b"]},
            {
                "phase": "outcome",
                "nodeid": "tests/test_group.py::test_b",
                "outcome": "passed",
            },
            {"phase": "done"},
        ]
        manifest.write_text("\n".join(json.dumps(line) for line in lines) + "\n")

        composition = read_file_composition(manifest)

        assert composition is not None
        assert composition.members == (
            "tests/test_group.py::test_a",
            "tests/test_group.py::test_b",
        ), (
            "both workers' collected members must be present, not just the "
            f"last writer's chunk: {composition.members}"
        )
        assert composition.outcomes["tests/test_group.py::test_a"] == "failed"
        assert composition.outcomes["tests/test_group.py::test_b"] == "passed"
