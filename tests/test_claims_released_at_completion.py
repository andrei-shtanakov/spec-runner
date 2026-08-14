"""#260 (F-35): a completed task's claim was never released.

TASK-102 completed and merged through the human gate. Its test file then changed
twice, both legitimately and both through that gate — a review fix adding
broadcast coverage, then an assertion hardening. TASK-104, a brand new task,
tried to author its red:

```
⛔ RED not confirmed, refusing to implement: no confirmed red for this task in
this workstream; claim violated — modified
test/kapelle/orchestrator/workers/evaluate_worker_test.exs
(claimed by TASK-102, checkpoint 46b2cfeacd18; claimed 5f52c8d7, found b5b0be5f)
```

`tdd status` showed all three DONE tasks still holding 🔒 active claims. So the
shipped invariant was: **once a task completes, its claimed file is frozen for
the workstream forever**, and every later legitimate edit wedges every
subsequent task. A TDD workstream degrades exactly as fast as its code lives.

A claim guards the evidential test from the confirmed red to the terminal gate.
Past that gate the lifecycle it protected is over: the lock guards nothing and
taxes everything. Completion now releases it, with `released` as its own
status — nothing went wrong, so `abandoned` ("this red was no good") and
`superseded` ("a later lineage replaced it") would both be lies about it.

For the state the shipped version left behind there is a door: `tdd release`.
Its admissibility is evidence, not a flag — the lifecycle must have reached
DONE. Releasing earlier would be the laundering the lock exists to prevent.
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
from pathlib import Path

import pytest

from spec_runner.claims import ClaimStatus, check_claims, record_claims
from spec_runner.config import ExecutorConfig
from spec_runner.lifecycle import TddPhase, advance
from spec_runner.remedy import RemedyError, RemedyOperation, cmd_tdd, release
from spec_runner.state import ExecutorState
from spec_runner.tdd import RedCheckpoint, RedOutcome, _config_hash, resolve_namespace

REASON = "TASK-102 completed and merged; its evidence no longer needs the lock"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "execution_mode": "tdd",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _completed_task(tmp_path: Path, *, reach_done: bool = True):
    """The pilot's shape: TASK-102 confirmed a red, claimed its test file, and
    finished."""
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "operator@example.com")
    _git(root, "config", "user.name", "Operator")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")

    cfg = _cfg(root)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_worker.py").write_text("def test_evaluates():\n    assert False\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "red")
    red_sha = _git(root, "rev-parse", "HEAD").stdout.strip()

    checkpoint = RedCheckpoint(
        task_id="TASK-102",
        namespace=resolve_namespace(cfg),
        commit_sha=red_sha,
        baseline_sha=red_sha,
        selector="tests/test_worker.py::test_evaluates",
        environment_id="unpinned",
        execution_mode="tdd",
        config_hash=_config_hash(cfg),
        outcome=RedOutcome.EXPECTED_FAIL,
        timestamp="2026-08-13T00:00:00",
    )
    phases = [
        TddPhase.RED_AUTHORING,
        TddPhase.RED_VERIFYING,
        TddPhase.GREEN_IMPLEMENTING,
        TddPhase.GREEN_VERIFYING,
    ]
    if reach_done:
        phases.append(TddPhase.DONE)
    with ExecutorState(cfg) as state:
        state.record_red_checkpoint(checkpoint)
        record_claims(cfg, state, checkpoint)
        for phase in phases:
            advance(state, resolve_namespace(cfg), "TASK-102", phase)
    return root, cfg, checkpoint


def _a_legitimate_later_edit(root: Path) -> str:
    """A review fix adding a test to the same file — merged through the human
    gate, exactly as the pilot's PR #9 was."""
    (root / "tests" / "test_worker.py").write_text(
        "def test_evaluates():\n    assert False\n\n\ndef test_broadcasts():\n    assert True\n"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "review fix: cover the broadcast")
    return _git(root, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.slow
class TestTheWedgeItself:
    def test_a_later_edit_no_longer_blocks_the_workstream(self, tmp_path):
        """#260 at the level it was reported: the claim of a *completed* task
        against a tree the project legitimately moved on to."""
        root, cfg, _cp = _completed_task(tmp_path)
        candidate = _a_legitimate_later_edit(root)

        with ExecutorState(cfg) as state:
            release(cfg, state, "TASK-102", reason=REASON)
            violations = check_claims(cfg, state, resolve_namespace(cfg), candidate)

        assert violations == []

    def test_and_it_did_block_while_the_claim_stood(self, tmp_path):
        """The other half of the same measurement — without it the test above
        proves only that nothing is broken, not that anything was fixed."""
        root, cfg, _cp = _completed_task(tmp_path)
        candidate = _a_legitimate_later_edit(root)

        with ExecutorState(cfg) as state:
            violations = check_claims(cfg, state, resolve_namespace(cfg), candidate)

        assert [v.task_id for v in violations] == ["TASK-102"]

    def test_a_running_task_still_freezes_its_file(self, tmp_path):
        """The guarantee that must survive: while the lifecycle is live, the
        evidence is locked. Releasing at completion is not weakening it."""
        root, cfg, _cp = _completed_task(tmp_path, reach_done=False)
        candidate = _a_legitimate_later_edit(root)

        with ExecutorState(cfg) as state:
            violations = check_claims(cfg, state, resolve_namespace(cfg), candidate)

        assert [v.task_id for v in violations] == ["TASK-102"]


@pytest.mark.slow
class TestCompletionReleasesThem:
    """Not the helper — the call site. A release nobody calls is the same
    wedge with a nicer function in it."""

    def _run_to_done(self, tmp_path, monkeypatch):
        from spec_runner import execution, hooks, tdd
        from spec_runner.task import Task

        root = tmp_path / "repo"
        root.mkdir(parents=True, exist_ok=True)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "t")
        (root / "README.md").write_text("x\n")
        (root / "spec").mkdir(exist_ok=True)
        (root / "spec" / "tasks.md").write_text("# Tasks\n\n### TASK-001: t\n🟠 P1 | ⬜ TODO\n")
        (root / "spec" / ".gitignore").write_text(".executor-*\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")
        cfg = _cfg(
            root,
            test_command="python -m pytest",
            lint_command="",
            auto_commit=True,
            create_git_branch=False,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=False,
        )

        def _red(config, prompt, **kwargs):
            path = Path(config.project_root) / "tests" / "test_thing.py"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("def test_thing():\n    assert False\n")
            return tdd.AgentCall(text="TDD_SELECTOR: tests/test_thing.py::test_thing")

        monkeypatch.setattr(tdd, "_run_agent", _red)
        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
        monkeypatch.setattr(execution, "update_task_status", lambda *a, **k: True)
        monkeypatch.setattr(execution, "build_task_prompt", lambda *a, **k: "prompt")
        monkeypatch.setattr(
            execution,
            "build_cli_invocation",
            lambda **k: type("I", (), {"argv": ["true"], "result_format": "text"})(),
        )
        monkeypatch.setattr(hooks, "post_done_hook", hooks.post_done_hook)

        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")
        with ExecutorState(cfg) as state, contextlib.suppress(Exception):
            execution.execute_task(task, cfg, state)
        return cfg

    def test_a_finished_run_leaves_no_active_claim(self, tmp_path, monkeypatch):
        cfg = self._run_to_done(tmp_path, monkeypatch)
        with ExecutorState(cfg) as state:
            namespace = resolve_namespace(cfg)
            history = [h["phase"] for h in state.tdd_phase_history("TASK-001", namespace)]
            claims = state.claims_for(namespace, "TASK-001")

        assert "done" in history, "the fixture must actually finish, or this proves nothing"
        assert claims, "the red must have claimed something for the release to mean anything"
        # `claims_for` rows are (task_id, path, blob_sha, status, checkpoint_id).
        assert [row[3] for row in claims] == [ClaimStatus.RELEASED.value] * len(claims)


@pytest.mark.slow
class TestTheOperatorDoor:
    def _args(self, task_id="TASK-102", reason=REASON):
        return argparse.Namespace(tdd_command="release", task_id=task_id, reason=reason, actor=None)

    def test_it_refuses_before_the_task_is_done(self, tmp_path):
        """The one thing this command must never become: a way to unlock the
        evidence of a task still in flight. That is the laundering the lock
        exists to prevent, and `abandon` — which retires the red *with* the
        lock — is the honest door mid-flight."""
        _root, cfg, _cp = _completed_task(tmp_path, reach_done=False)

        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="has not reached DONE"):
            release(cfg, state, "TASK-102", reason=REASON)

    def test_it_names_the_door_that_does_fit(self, tmp_path):
        _root, cfg, _cp = _completed_task(tmp_path, reach_done=False)

        with ExecutorState(cfg) as state, pytest.raises(RemedyError) as exc:
            release(cfg, state, "TASK-102", reason=REASON)

        assert "tdd abandon" in str(exc.value)

    def test_it_records_the_decision_with_an_actor(self, tmp_path):
        _root, cfg, _cp = _completed_task(tmp_path)

        with ExecutorState(cfg) as state:
            release(cfg, state, "TASK-102", reason=REASON, actor="operator@example.com")
            records = state.remedies("TASK-102", resolve_namespace(cfg))

        assert [r.operation for r in records] == [RemedyOperation.RELEASE]
        assert records[0].actor == "operator@example.com"
        assert records[0].reason == REASON

    def test_a_reason_is_required(self, tmp_path):
        _root, cfg, _cp = _completed_task(tmp_path)

        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="reason"):
            release(cfg, state, "TASK-102", reason="  ")

    def test_an_agent_cannot_release(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SPEC_RUNNER_AGENT", "1")
        _root, cfg, _cp = _completed_task(tmp_path)

        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="agent"):
            release(cfg, state, "TASK-102", reason=REASON)

    def test_repeating_it_is_idempotent(self, tmp_path):
        _root, cfg, _cp = _completed_task(tmp_path)

        with ExecutorState(cfg) as state:
            first = release(cfg, state, "TASK-102", reason=REASON)
            second = release(cfg, state, "TASK-102", reason=REASON)
            records = state.remedies("TASK-102", resolve_namespace(cfg))

        assert first.released == 1
        assert second.already_applied is True
        assert len(records) == 1

    def test_the_claims_are_retired_as_released_not_abandoned(self, tmp_path):
        """The status is the whole point: `abandoned` says the red was no good
        and `superseded` says a later lineage replaced it. Neither happened."""
        _root, cfg, _cp = _completed_task(tmp_path)

        with ExecutorState(cfg) as state:
            release(cfg, state, "TASK-102", reason=REASON)
            claims = state.claims_for(resolve_namespace(cfg), "TASK-102")

        assert [row[3] for row in claims] == [ClaimStatus.RELEASED.value]

    def test_the_checkpoint_itself_is_untouched(self, tmp_path):
        """A release unlocks the file; it does not retract the evidence that
        the red was real. That record is what `tdd status` reads back."""
        _root, cfg, cp = _completed_task(tmp_path)

        with ExecutorState(cfg) as state:
            release(cfg, state, "TASK-102", reason=REASON)
            still = state.red_checkpoint("TASK-102", resolve_namespace(cfg))

        assert still is not None and still.checkpoint_id == cp.checkpoint_id

    def test_the_command_says_how_many_it_freed(self, tmp_path, capsys):
        _root, cfg, _cp = _completed_task(tmp_path)

        code = cmd_tdd(self._args(), cfg)
        out = capsys.readouterr().out

        assert code == 0
        assert "Released 1 claim(s)" in out

    def test_the_command_says_when_there_was_nothing_to_free(self, tmp_path, capsys):
        """An operator running this to unwedge a workstream needs to know the
        wedge is elsewhere, rather than reading a success and moving on."""
        _root, cfg, _cp = _completed_task(tmp_path)
        with ExecutorState(cfg) as state:
            state.supersede_claims(resolve_namespace(cfg), "TASK-102", ClaimStatus.ABANDONED)

        code = cmd_tdd(self._args(), cfg)
        out = capsys.readouterr().out

        assert code == 0
        assert "no active claims" in out

    def test_a_refusal_is_a_message_not_a_traceback(self, tmp_path, capsys):
        _root, cfg, _cp = _completed_task(tmp_path, reach_done=False)

        code = cmd_tdd(self._args(), cfg)

        assert code == 1
        assert "⛔" in capsys.readouterr().out

    def test_the_parser_requires_a_reason(self):
        from spec_runner.cli import _build_parser

        with pytest.raises(SystemExit):
            _build_parser().parse_args(["tdd", "release", "TASK-102"])
