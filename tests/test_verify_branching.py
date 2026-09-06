"""BEH-20, BEH-21, BEH-22 (#367 milestone 1, TASK-008).

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-20 (-BEH-22)
Traces: FR-13, FR-05, FR-14, FR-15

The three outcomes a live verify-first run can reach (BEH-23,
`tests/test_verify_outcomes.py`) now drive three different paths through
`execute_task`:

- `green` (BEH-20) — never authors a red; the paid implementation call is the
  attempt's only paid call, and the red-gate is satisfied by verify-evidence.
- `test_failure` (BEH-21) — walks the ordinary red-authoring cycle `tdd`
  already has: replay, checkpoint, claims, a red-gate judged "с сегодняшним
  классом и текстом отказа" (`tests/test_task_008_..._red.py` pins the
  single-selector case; this file adds the mixed-group case BEH-21 also
  names).
- `instrument_error` (BEH-22) — stops fail-closed before either branch, with
  no paid call at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from spec_runner import tdd
from spec_runner.claims import ClaimStatus, check_claims
from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.runner import CliInvocation
from spec_runner.state import ErrorCode, ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import AgentCall, resolve_namespace


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
        "auto_commit": True,
        "run_review": False,
        "callback_url": "",
        "lint_command": "",
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


def _fake_red_agent(config, prompt, **kwargs):
    """Stand-in RED-authoring pass, mirroring the frozen BEH-21 test."""
    red_test = Path(config.project_root) / "tests" / "test_red_authored.py"
    red_test.write_text("def test_red_authored():\n    assert False, 'red'\n")
    return AgentCall(
        text="TDD_SELECTOR: tests/test_red_authored.py::test_red_authored\nTASK_COMPLETE"
    )


class TestBEH20GreenReachesDoneWithoutBuyingARed:
    """kind: e2e — a declared group that is already green on entry reaches
    DONE without a single red-authoring call, and its red-gate is satisfied
    by a reference to the recorded verify-evidence."""

    def test_a_green_group_reaches_done_with_no_red_authoring_call(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")

        red_agent = MagicMock(
            side_effect=AssertionError("BEH-20: no red authoring for a green group")
        )
        monkeypatch.setattr(tdd, "_run_agent", red_agent)

        with (
            patch("spec_runner.execution.update_task_status"),
            patch("spec_runner.execution.log_progress"),
            patch(
                "spec_runner.execution.build_cli_invocation",
                return_value=CliInvocation(["echo", "hi"], "text"),
            ),
            patch("spec_runner.execution.build_task_prompt", return_value="test prompt"),
            patch("spec_runner.execution.pre_start_hook", return_value=True),
            patch("spec_runner.execution._run_agent_process") as mock_run,
        ):
            mock_run.return_value = MagicMock(
                stdout="output TASK_COMPLETE", stderr="", returncode=0
            )
            task = _task(verifies=["tests/test_group.py::test_it"])
            config = _cfg(root)
            with ExecutorState(config) as state:
                result = execute_task(task, config, state)

                red_agent.assert_not_called()
                assert mock_run.called, (
                    "BEH-20: the paid implementation call must still happen for a "
                    "green-on-entry group"
                )

                namespace = resolve_namespace(config)
                assert state.red_checkpoint(task.id, namespace) is None, (
                    "BEH-20: a green-on-entry group must never produce a "
                    "RedCheckpoint — nothing to fabricate a red for"
                )
                evidence = state.verify_evidence(namespace, task.id)
                assert evidence is not None
                assert evidence.outcome == "green"

        assert result is True


class TestBEH21TestFailureEntersTheOrdinaryTddCycle:
    """kind: e2e — the mixed-group case BEH-21 names explicitly: one
    selector proven green, the other `TESTS_FAILED` (+ proven), still reads
    as `test_failure` overall (never `instrument_error`) and still walks the
    ordinary red-authoring cycle. The single-selector case is pinned by the
    frozen `tests/test_task_008_55403b38f9226be0_14df0641_red.py`."""

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
    def test_a_mixed_group_is_test_failure_and_enters_red_authoring(
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
        (root / "tests" / "test_a.py").write_text("def test_a():\n    assert True\n")
        (root / "tests" / "test_b.py").write_text(
            "def test_b():\n    assert False, 'not implemented'\n"
        )
        _commit(root, "base")

        mock_run.return_value = MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)

        red_calls: list[str] = []

        def _fake_red_agent_recording(config, prompt, **kwargs):
            red_calls.append("red_authoring")
            return _fake_red_agent(config, prompt, **kwargs)

        monkeypatch.setattr(tdd, "_run_agent", _fake_red_agent_recording)

        task = _task(verifies=["tests/test_a.py::test_a", "tests/test_b.py::test_b"])
        config = _cfg(root)
        with ExecutorState(config) as state:
            execute_task(task, config, state)

            namespace = resolve_namespace(config)
            evidence = state.verify_evidence(namespace, task.id)
            assert evidence is not None
            assert evidence.outcome == "test_failure", (
                "BEH-21: a group with one green and one failing selector must "
                "still read as test_failure, never instrument_error"
            )
            assert red_calls, (
                "BEH-21: a mixed red group must also walk the ordinary "
                "red-authoring cycle, not skip straight to the implementation call"
            )
            checkpoint = state.red_checkpoint(task.id, namespace)
            assert checkpoint is not None, (
                "BEH-21: the red-authoring pass must leave a confirmed red checkpoint"
            )


class TestBEH21ClaimsReleaseAtCompletion:
    """#381 review, major finding: a `verify_first` task that walks BEH-21's
    red-authoring cycle records byte-lock claims the same way `tdd` does
    (`tdd.py::_judge_red_commit`), but until this fix the DONE bookkeeping
    block (`execution.py`) released claims and recorded a terminal lifecycle
    row only for `mode == "tdd"` — so the claim stayed `ACTIVE` forever and
    the `tdd release` operator door stayed unreachable (no DONE row to check).

    The same measurement `tests/test_claims_released_at_completion.py` makes
    for `tdd`, built for this mode's own red cycle."""

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
    def test_a_red_cycle_task_releases_its_claim_and_reaches_done(
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
            "def test_it():\n    assert False, 'not implemented'\n"
        )
        _commit(root, "base")

        mock_run.return_value = MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)
        monkeypatch.setattr(tdd, "_run_agent", _fake_red_agent)

        task = _task(verifies=["tests/test_group.py::test_it"])
        config = _cfg(root)
        with ExecutorState(config) as state:
            result = execute_task(task, config, state)

            namespace = resolve_namespace(config)
            history = [h["phase"] for h in state.tdd_phase_history(task.id, namespace)]
            claims = state.claims_for(namespace, task.id)

            assert claims, "the red-authoring pass must have claimed a file"
            assert "done" in history, (
                "a verify_first task that walked the red cycle must leave a "
                "terminal lifecycle row, or `tdd release` has nothing to check"
            )
            assert [row[3] for row in claims] == [ClaimStatus.RELEASED.value] * len(claims), (
                "the claim on the authored red file must be released at "
                "completion, the same way a tdd task's is released (#260)"
            )

            # The measured consequence (#260/#381): a later, legitimate commit
            # touching the claimed file must not be refused by a claim that
            # outlived the task it was protecting.
            (root / "tests" / "test_red_authored.py").write_text(
                "def test_red_authored():\n    assert True  # fixed legitimately\n"
            )
            candidate = _commit(root, "a later legitimate edit")
            violations = check_claims(config, state, namespace, candidate)

        assert result is True
        assert violations == [], (
            "a completed verify_first task's stale claim must not block a "
            f"later legitimate edit to the same file, got {violations}"
        )


class TestBEH22InstrumentErrorStopsFailClosed:
    """kind: integration — each instrument-classified entry, on its own,
    must stop the task before either the green-only or the red-authoring
    branch runs, and before any paid call — the class is infrastructure
    (`ErrorCode.INFRASTRUCTURE`), not a verdict on the work."""

    def test_a_composite_test_command_stops_before_any_paid_call(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")

        red_agent = MagicMock(
            side_effect=AssertionError("no red authoring for an instrument error")
        )
        monkeypatch.setattr(tdd, "_run_agent", red_agent)

        task = _task(verifies=["tests/test_group.py::test_it"])
        config = _cfg(root, test_command="python -m pytest tests/ && echo done")

        with (
            patch("spec_runner.execution.update_task_status"),
            patch("spec_runner.execution._run_agent_process") as mock_run,
            ExecutorState(config) as state,
        ):
            outcome = execute_task(task, config, state)

            assert outcome is False
            mock_run.assert_not_called()
            red_agent.assert_not_called()

            attempts = state.get_task_state(task.id).attempts
            assert attempts, "an instrument-error attempt must still be recorded"
            assert attempts[-1].error_code is ErrorCode.INFRASTRUCTURE, (
                f"expected an infrastructure refusal, got {attempts[-1].error_code} — "
                f"{attempts[-1].error}"
            )

            namespace = resolve_namespace(config)
            evidence = state.verify_evidence(namespace, task.id)
            assert evidence is not None
            assert evidence.outcome == "instrument_error"
            assert state.red_checkpoint(task.id, namespace) is None

    def test_an_empty_declared_group_stops_before_any_paid_call(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")

        red_agent = MagicMock(
            side_effect=AssertionError("no red authoring for an instrument error")
        )
        monkeypatch.setattr(tdd, "_run_agent", red_agent)

        task = _task(verifies=[])
        config = _cfg(root)

        with (
            patch("spec_runner.execution.update_task_status"),
            patch("spec_runner.execution._run_agent_process") as mock_run,
            ExecutorState(config) as state,
        ):
            outcome = execute_task(task, config, state)

            assert outcome is False
            mock_run.assert_not_called()
            red_agent.assert_not_called()
            attempts = state.get_task_state(task.id).attempts
            assert attempts[-1].error_code is ErrorCode.INFRASTRUCTURE

    def test_an_unresolvable_adapter_stops_before_any_paid_call(self, tmp_path, monkeypatch):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")

        red_agent = MagicMock(
            side_effect=AssertionError("no red authoring for an instrument error")
        )
        monkeypatch.setattr(tdd, "_run_agent", red_agent)

        task = _task(verifies=["tests/test_group.py::test_it"])
        config = _cfg(root, test_command="some-unknown-runner")

        with (
            patch("spec_runner.execution.update_task_status"),
            patch("spec_runner.execution._run_agent_process") as mock_run,
            ExecutorState(config) as state,
        ):
            outcome = execute_task(task, config, state)

            assert outcome is False
            mock_run.assert_not_called()
            red_agent.assert_not_called()
            attempts = state.get_task_state(task.id).attempts
            assert attempts[-1].error_code is ErrorCode.INFRASTRUCTURE
