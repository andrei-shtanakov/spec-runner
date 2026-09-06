"""RED for BEH-24 (write side, FR-17): the checkpoint records a literal, not
the task's own resolved mode.

`_judge_red_commit` (tdd.py:841, called from `run_red_phase`) always writes
`RedCheckpoint(..., execution_mode="tdd")`, regardless of what mode the task
actually ran under. `_reusable_checkpoint` (tdd.py:1860) matches a candidate
checkpoint only when `cp.execution_mode == config.resolve_execution_mode(task)`
— so a `verify_first` task's confirmed red is written as `"tdd"` and can never
match its own resolved mode (`"verify_first"`) on a later attempt. Every retry
of a verify-first task therefore re-authors and re-pays for a red that was
already confirmed (AC FR-14): the fix is to record the actual resolved value,
`resolve_execution_mode(task)`, not the literal.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-24
Traces: FR-17
"""

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import RedOutcome, run_red_phase


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        # Project default stays "standard" — the task opts into verify_first
        # on its own, which is exactly the BEH-24 "per-task" configuration.
        "test_command": "python -m pytest",
        "lint_command": "",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(task_id: str = "TASK-101") -> Task:
    return Task(
        id=task_id,
        name="verify-first task",
        priority="p1",
        status="todo",
        estimate="1h",
        execution_mode="verify_first",
    )


def _agent(monkeypatch, calls: list, *, cost: float = 0.25):
    """A scripted RED-authoring agent, standing in for the paid CLI call."""
    from spec_runner import tdd

    def _fake(config, prompt, **kwargs):
        calls.append(prompt[:40])
        path = Path(config.project_root) / "tests" / "test_thing.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_thing():\n    assert False, 'not implemented'\n")
        return tdd.AgentCall(
            text="TDD_SELECTOR: tests/test_thing.py::test_thing\nTASK_COMPLETE",
            input_tokens=1000,
            output_tokens=200,
            cost_usd=cost,
        )

    monkeypatch.setattr(tdd, "_run_agent", _fake)
    return calls


class TestVerifyFirstCheckpointCarriesItsOwnMode:
    """kind: contract — BEH-24: a checkpoint's recorded mode must be the
    task's own resolved value, so a verify-first retry can find it again."""

    def test_a_verify_first_retry_reuses_its_checkpoint_and_buys_no_second_red(
        self, tmp_path, monkeypatch
    ):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        calls: list = []
        _agent(monkeypatch, calls)

        with ExecutorState(cfg) as state:
            first = run_red_phase(_task(), cfg, state)
            second = run_red_phase(_task(), cfg, state)

        assert first.outcome is RedOutcome.EXPECTED_FAIL
        assert first.checkpoint is not None
        assert first.checkpoint.execution_mode == "verify_first", (
            "the checkpoint recorded the literal 'tdd' instead of the task's "
            "own resolved execution_mode ('verify_first') — the exact reason "
            "_reusable_checkpoint can never match it again"
        )
        assert second.outcome is RedOutcome.EXPECTED_FAIL
        assert len(calls) == 1, (
            "a verify-first task's confirmed red was re-authored on retry: the "
            "checkpoint's recorded execution_mode did not match the task's own "
            "resolved mode, so _reusable_checkpoint never found a match and a "
            "second RED authoring call was paid for"
        )
