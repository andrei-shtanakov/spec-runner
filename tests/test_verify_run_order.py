"""BEH-07/BEH-08/BEH-09 (#367 milestone 1, TASK-005): the live verify-first
run, its scope, and what commit it judges.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-07 (-BEH-09)
Traces: FR-05, FR-06, FR-07

kind: integration — every scenario here runs real `git`/`pytest` subprocesses
against a fixture repository; nothing about the group's outcome is mocked.
"""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.live_verify import run_live_verify
from spec_runner.runner import CliInvocation
from spec_runner.state import ErrorCode, ExecutorState, PhaseOutcome
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


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    )
    return head.stdout.strip()


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
        "id": "TASK-101",
        "name": "verify-first task",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
        "execution_mode": "verify_first",
    }
    defaults.update(overrides)
    return Task(**defaults)


class TestLiveRunIsTheFirstAction:
    """kind: integration — BEH-07: the live run happens for real, before the
    first paid call, and no agent call is ever recorded ahead of it."""

    @patch("spec_runner.execution.update_task_status")
    @patch("spec_runner.execution.log_progress")
    @patch(
        "spec_runner.execution.build_cli_invocation",
        return_value=CliInvocation(["echo", "hi"], "text"),
    )
    @patch("spec_runner.execution.build_task_prompt", return_value="test prompt")
    @patch(
        "spec_runner.execution.post_done_hook",
        return_value=(True, None, "skipped", "", False),
    )
    @patch("spec_runner.execution.pre_start_hook", return_value=True)
    @patch("spec_runner.execution._run_agent_process")
    def test_no_paid_call_is_recorded_before_the_live_run_completes(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        tmp_path,
        monkeypatch,
    ):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_marks_that_it_ran():\n    assert True\n"
        )
        _commit(root, "base")

        events: list[str] = []

        def _paid_call(*args, **kwargs):
            events.append("paid_call")
            return MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)

        mock_run.side_effect = _paid_call

        real_run_live_verify = run_live_verify

        def _tracked(*args, **kwargs):
            result = real_run_live_verify(*args, **kwargs)
            events.append("verify_evidence")
            return result

        monkeypatch.setattr("spec_runner.execution.run_live_verify", _tracked)

        task = _task(verifies=["tests/test_group.py::test_marks_that_it_ran"])
        config = _cfg(root)
        state = ExecutorState(config)

        execute_task(task, config, state)

        assert events, "the live verify run never happened"
        assert events[0] == "verify_evidence", (
            "an agent call was recorded before the verify-evidence: the ledger "
            f"order was {events!r}, and no call may precede the live run"
        )
        assert "paid_call" in events, "the paid call never ran either"


class TestLiveRunIsScopedToTheDeclaredGroup:
    """kind: integration — BEH-08: only the declared selectors execute, even
    on a default `test_command` that names the whole `tests/` directory."""

    def test_a_check_outside_the_group_never_runs(self, tmp_path):
        root = _init_repo(tmp_path)
        group_marker = tmp_path / "group_ran.txt"
        other_marker = tmp_path / "other_ran.txt"
        (root / "tests" / "test_group.py").write_text(
            "from pathlib import Path\n"
            "\n"
            "\n"
            "def test_in_group():\n"
            f"    Path({str(group_marker)!r}).write_text('ran')\n"
            "    assert True\n"
        )
        (root / "tests" / "test_other.py").write_text(
            "from pathlib import Path\n"
            "\n"
            "\n"
            "def test_outside_group():\n"
            f"    Path({str(other_marker)!r}).write_text('ran')\n"
            "    assert True\n"
        )
        _commit(root, "base")

        task = _task(verifies=["tests/test_group.py::test_in_group"])
        # The default shape FR-06 calls out explicitly: `test_command` already
        # names the directory, so a naive append would run the whole suite.
        config = _cfg(root, test_command="python -m pytest tests/")

        result = run_live_verify(task, config)

        assert result.passed, result.detail
        assert group_marker.exists(), "the declared selector itself never ran"
        assert not other_marker.exists(), (
            "a check outside the declared group executed: the live run must be "
            "restricted to the declared group, not the whole test_command scope"
        )


class TestLiveRunJudgesTheNamedCommit:
    """kind: integration — BEH-09: the verdict is about the commit named by
    HEAD when the live run starts, not about the working tree at that moment
    — a dirty tree that would flip the outcome must not change the verdict,
    and the same commit replayed again gives the same answer."""

    def test_a_dirty_working_tree_does_not_change_the_verdict(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        sha = _commit(root, "base")

        # Dirty the working tree with an uncommitted edit that would flip the
        # outcome if the live run judged the tree instead of the commit.
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert False\n")

        task = _task(verifies=["tests/test_group.py::test_it"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.sha == sha
        assert result.passed, (
            f"the verdict followed the dirty working tree instead of the named "
            f"commit {sha[:12]}: {result.detail}"
        )

        # The uncommitted edit is still sitting in the working tree, unrelated
        # to the replay just performed in its own worktree.
        assert (root / "tests" / "test_group.py").read_text() == (
            "def test_it():\n    assert False\n"
        )

        # Reproducible from the SHA: replaying the same commit again — even
        # with the tree still dirty — gives the same outcome.
        result_again = run_live_verify(task, config)
        assert result_again.sha == sha
        assert result_again.passed == result.passed


class TestLiveRunFailurePath:
    """kind: integration — a real failure in the declared group is a refusal,
    not a pass, and the failure names the selector that broke."""

    def test_a_failing_selector_is_reported_and_never_cleaned_up_as_a_pass(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_it():\n    assert False, 'deliberate failure'\n"
        )
        sha = _commit(root, "base")

        task = _task(verifies=["tests/test_group.py::test_it"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.sha == sha
        assert result.ran, "the selector was never actually executed"
        assert not result.passed
        assert "tests/test_group.py::test_it" in result.detail
        assert "deliberate failure" in result.detail

        worktrees = subprocess.run(
            ["git", "worktree", "list"], cwd=root, capture_output=True, text=True, check=True
        )
        assert worktrees.stdout.strip().count("\n") == 0, (
            f"a replay worktree was left behind after a failing run: {worktrees.stdout!r}"
        )

    @patch("spec_runner.execution.update_task_status")
    @patch("spec_runner.execution.log_progress")
    @patch(
        "spec_runner.execution.build_cli_invocation",
        return_value=CliInvocation(["echo", "hi"], "text"),
    )
    @patch("spec_runner.execution.build_task_prompt", return_value="test prompt")
    @patch(
        "spec_runner.execution.post_done_hook",
        return_value=(True, None, "skipped", "", False),
    )
    @patch("spec_runner.execution.pre_start_hook", return_value=True)
    @patch("spec_runner.execution._run_agent_process")
    def test_a_genuine_test_failure_is_observed_not_blocked(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        tmp_path,
    ):
        """#375 review, finding 3 / FR-14: a real red group is an
        observation, not a refusal — until the green/test-failure/
        instrument-error branching exists (#367 TASK-006/008), a genuine
        failure must not block the task; it proceeds exactly as `standard`
        would, and the failure is still recorded, named to the judged
        commit."""
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_it():\n    assert False, 'deliberate failure'\n"
        )
        sha = _commit(root, "base")

        mock_run.return_value = MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)

        task = _task(verifies=["tests/test_group.py::test_it"])
        config = _cfg(root)
        state = ExecutorState(config)

        execute_task(task, config, state)

        assert mock_run.called, "a genuine test failure must not block the paid call"
        phases = [p for p in state.phase_history(task.id) if p.phase == "tests"]
        assert phases, "no verify-first phase outcome was recorded"
        assert phases[-1].outcome is PhaseOutcome.UNEXPECTED_FAIL
        assert sha[:12] in (phases[-1].detail or ""), (
            "the phase record does not name the commit the live run judged (finding 6)"
        )
        attempts = state.get_task_state(task.id).attempts
        assert not any(a.error_code == ErrorCode.HOOK_FAILURE for a in attempts), (
            "a genuine test failure must not be recorded as a HOOK_FAILURE refusal"
        )


class TestLiveRunRefusalBeforeRunning:
    """kind: integration — a run that cannot even start (a composite
    `test_command`) is an instrument error, not a pass and not a test
    failure, and still refuses before any paid call."""

    def test_a_composite_test_command_refuses_without_running_anything(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")

        task = _task(verifies=["tests/test_group.py::test_it"])
        config = _cfg(root, test_command="pytest tests/ && echo done")

        result = run_live_verify(task, config)

        assert not result.ran, "a composite command should refuse before running"
        assert not result.passed
        assert "composite" in result.detail

    @patch("spec_runner.execution.update_task_status")
    @patch("spec_runner.execution.log_progress")
    @patch("spec_runner.execution._run_agent_process")
    def test_execute_task_reports_it_as_infrastructure_not_a_task_failure(
        self, mock_run, mock_log, mock_status, tmp_path
    ):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        sha = _commit(root, "base")

        task = _task(verifies=["tests/test_group.py::test_it"])
        config = _cfg(root, test_command="pytest tests/ && echo done")
        state = ExecutorState(config)

        outcome = execute_task(task, config, state)

        assert outcome is False
        mock_run.assert_not_called()
        attempts = state.get_task_state(task.id).attempts
        assert attempts[-1].error_code == ErrorCode.INFRASTRUCTURE
        assert sha[:12] in (attempts[-1].error or ""), (
            "the refusal does not name the commit the live run judged (finding 6)"
        )


class TestLiveRunMultipleSelectors:
    """kind: integration — the declared group is a single verdict: the first
    selector that fails stops the run, and later selectors never execute."""

    def test_the_first_failure_stops_the_group(self, tmp_path):
        root = _init_repo(tmp_path)
        second_marker = tmp_path / "second_ran.txt"
        (root / "tests" / "test_group.py").write_text(
            "def test_first():\n    assert False, 'first breaks'\n"
        )
        (root / "tests" / "test_second.py").write_text(
            "from pathlib import Path\n"
            "\n"
            "\n"
            "def test_second():\n"
            f"    Path({str(second_marker)!r}).write_text('ran')\n"
            "    assert True\n"
        )
        _commit(root, "base")

        task = _task(
            verifies=[
                "tests/test_group.py::test_first",
                "tests/test_second.py::test_second",
            ]
        )
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert not result.passed
        assert "tests/test_group.py::test_first" in result.detail
        assert not second_marker.exists(), (
            "the second selector ran even though the first one already failed"
        )


class TestLiveRunCleansUpOnEveryPath:
    """kind: integration — the replay worktree and temp directory are removed
    whether the group passes or fails."""

    def test_no_worktree_survives_a_passing_run(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")

        task = _task(verifies=["tests/test_group.py::test_it"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.passed
        worktrees = subprocess.run(
            ["git", "worktree", "list"], cwd=root, capture_output=True, text=True, check=True
        )
        assert worktrees.stdout.strip().count("\n") == 0


class TestLiveRunEmptyGroupRefuses:
    """kind: integration — #375 review, finding 1: an empty (or missing)
    declared group must not read as a vacuous pass — FR-09's "0 passed
    proves nothing" applies just as much to a group that never started."""

    def test_an_empty_verifies_group_is_an_instrument_error_not_a_pass(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")

        # Legal by the parser's own convention (`**Verifies:** —` parses to
        # `[]` without a `verifies_error`), and reachable only if the tasks
        # file changed between validate and this attempt.
        task = _task(verifies=[])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert not result.ran, "an empty group must refuse before anything runs"
        assert not result.passed
        assert "empty" in result.detail or "no group" in result.detail


class TestLiveRunVerdictGoesThroughTheAdapter:
    """kind: integration — #375 review, finding 2: the verdict is read from
    the adapter's classify/prove_selected/execution_proven dictionary, not a
    raw exit code — so a skipped selector (exit 0, node id in the output) is
    not green, and a selector that resolves to nothing is an instrument
    error rather than "did not pass"."""

    def test_a_skipped_selector_is_not_green(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "import pytest\n"
            "\n"
            "\n"
            "@pytest.mark.skip(reason='not ready')\n"
            "def test_it():\n"
            "    assert True\n"
        )
        _commit(root, "base")

        task = _task(verifies=["tests/test_group.py::test_it"])
        # `-v` so the SKIPPED line carries the node id verbatim — the exact
        # shape that makes pytest's `prove_selected` answer PROVEN even
        # though the test never executed (BEH-11).
        config = _cfg(root, test_command="python -m pytest -v")

        result = run_live_verify(task, config)

        assert not result.passed, "a skipped selector must not read as green"
        assert not result.ran, (
            "a skipped selector is an instrument error, not a genuine test failure"
        )

    def test_a_selector_matching_nothing_is_an_instrument_error(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")

        # Names a test that does not exist — pytest exits 4
        # (COLLECTION_OR_COMPILE_ERROR/SELECTION_FAILED), never "1" (tests
        # failed), so this must not be read as a genuine test failure.
        task = _task(verifies=["tests/test_group.py::test_does_not_exist"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert not result.passed
        assert not result.ran, (
            "a selector matching nothing must be an instrument error, not "
            "a test failure that would send the task down the TDD path"
        )


class TestLiveRunScopingWorksForAnyTestDirectory:
    """kind: integration — #375 review, finding 4: scoping is built by the
    adapter from the declared selector, not a hardcoded literal (`{"tests"}`)
    outside it — so a project whose suite lives under a differently named
    directory is scoped correctly too."""

    def test_a_non_default_test_directory_is_still_scoped(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "suite").mkdir()
        group_marker = tmp_path / "group_ran.txt"
        other_marker = tmp_path / "other_ran.txt"
        (root / "suite" / "test_group.py").write_text(
            "from pathlib import Path\n"
            "\n"
            "\n"
            "def test_in_group():\n"
            f"    Path({str(group_marker)!r}).write_text('ran')\n"
            "    assert True\n"
        )
        (root / "suite" / "test_other.py").write_text(
            "from pathlib import Path\n"
            "\n"
            "\n"
            "def test_outside_group():\n"
            f"    Path({str(other_marker)!r}).write_text('ran')\n"
            "    assert True\n"
        )
        _commit(root, "base")

        task = _task(verifies=["suite/test_group.py::test_in_group"])
        # The old hardcoded token set only recognised the literal "tests" —
        # a directory named anything else was never stripped, and the whole
        # directory ran alongside the declared selector.
        config = _cfg(root, test_command="python -m pytest suite/")

        result = run_live_verify(task, config)

        assert result.passed, result.detail
        assert group_marker.exists(), "the declared selector itself never ran"
        assert not other_marker.exists(), (
            "a check outside the declared group executed: scoping did not "
            "narrow a non-default test directory"
        )
