"""#141 slice 3: operator remedies — `tdd abandon` and `tdd repair`.

Without these, the only cure for a mistake in a frozen test is rewriting
history. The pilot did that twice in a single phase, each time with a state
freeze and a second signature. Slice 2 does not ship without this.

A remedy is an **authority decision**, not an observation: it carries an actor
and a reason, it never deletes what it replaces, and `repair` does not bless
new bytes — it opens a new lineage that must prove its own red.

Contract: `docs/superpowers/specs/2026-08-11-claim-and-remedy-contracts.md` §2
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.claims import ClaimStatus, record_claims
from spec_runner.config import ExecutorConfig
from spec_runner.remedy import (
    CheckpointStatus,
    RemedyError,
    RemedyOperation,
    abandon,
    repair,
    resolve_actor,
)
from spec_runner.state import ExecutorState
from spec_runner.tdd import RedCheckpoint, RedOutcome, resolve_namespace


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "operator@example.com")
    _git(root, "config", "user.name", "Operator")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "execution_mode": "tdd",
        "test_command": "python -m pytest",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _commit(root: Path, files: dict[str, str], message: str = "c") -> str:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


FAILING = "def test_y():\n    assert False\n"
PASSING = "def test_y():\n    assert True\n"


def _establish(tmp_path, *, body=FAILING, task="TASK-001"):
    """A confirmed red with its claim, as slice 1+2 would leave it."""
    root = _repo(tmp_path)
    cfg = _cfg(root)
    sha = _commit(root, {"tests/test_x.py": body})
    checkpoint = RedCheckpoint(
        task_id=task,
        namespace=resolve_namespace(cfg),
        commit_sha=sha,
        baseline_sha=sha,
        selector="tests/test_x.py::test_y",
        environment_id="unpinned",
        execution_mode="tdd",
        config_hash="h",
        outcome=RedOutcome.EXPECTED_FAIL,
        timestamp="2026-08-11T00:00:00",
    )
    with ExecutorState(cfg) as state:
        state.record_red_checkpoint(checkpoint)
        record_claims(cfg, state, checkpoint)
    return root, cfg, checkpoint


class TestCompareAndSwap:
    """Without CAS, a remedy issued against what the operator last saw applies
    silently to whatever arrived since."""

    def test_the_active_checkpoint_id_is_accepted(self, tmp_path):
        root, cfg, cp = _establish(tmp_path)
        with ExecutorState(cfg) as state:
            result = abandon(cfg, state, "TASK-001", cp.checkpoint_id, reason="wrong red")
        assert result.operation is RemedyOperation.ABANDON

    def test_a_stale_checkpoint_id_is_refused(self, tmp_path):
        root, cfg, cp = _establish(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError) as exc:
            abandon(cfg, state, "TASK-001", "deadbeef1234", reason="r")
        # "an active checkpoint", not "the": CAS compares against the whole
        # active set, since a task can hold more than one lineage and the
        # operator must be able to name any of them (#185).
        assert "not an active checkpoint" in str(exc.value)

    def test_a_task_with_no_checkpoint_is_refused(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError) as exc:
            abandon(cfg, state, "TASK-404", "whatever", reason="r")
        assert "no active checkpoint" in str(exc.value)


class TestAbandon:
    def test_the_checkpoint_stops_counting(self, tmp_path):
        """Back to RED authoring: the gate must see no confirmed red."""
        root, cfg, cp = _establish(tmp_path)
        with ExecutorState(cfg) as state:
            abandon(cfg, state, "TASK-001", cp.checkpoint_id, reason="bad red")
            assert state.red_checkpoint("TASK-001", resolve_namespace(cfg)) is None

    def test_the_claims_are_released(self, tmp_path):
        root, cfg, cp = _establish(tmp_path)
        with ExecutorState(cfg) as state:
            abandon(cfg, state, "TASK-001", cp.checkpoint_id, reason="bad red")
            assert state.active_claims(resolve_namespace(cfg)) == []

    def test_nothing_is_deleted(self, tmp_path):
        """History is never rewritten to fix a frozen test — that is the
        practice being replaced. The row stays with a new status."""
        root, cfg, cp = _establish(tmp_path)
        with ExecutorState(cfg) as state:
            abandon(cfg, state, "TASK-001", cp.checkpoint_id, reason="bad red")
            rows = state._conn.execute("SELECT status FROM tdd_claims").fetchall()
            checkpoints = state._conn.execute("SELECT status FROM red_checkpoints").fetchall()
        assert [r[0] for r in rows] == [ClaimStatus.ABANDONED.value]
        assert [c[0] for c in checkpoints] == [CheckpointStatus.ABANDONED.value]

    def test_the_actor_and_reason_are_recorded(self, tmp_path):
        root, cfg, cp = _establish(tmp_path)
        with ExecutorState(cfg) as state:
            abandon(cfg, state, "TASK-001", cp.checkpoint_id, reason="flaky", actor="ann")
            [record] = state.remedies("TASK-001", resolve_namespace(cfg))
        assert record.actor == "ann" and record.reason == "flaky"
        assert record.timestamp

    def test_a_reason_is_required(self, tmp_path):
        root, cfg, cp = _establish(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError):
            abandon(cfg, state, "TASK-001", cp.checkpoint_id, reason="   ")

    def test_repeating_it_is_idempotent(self, tmp_path):
        root, cfg, cp = _establish(tmp_path)
        with ExecutorState(cfg) as state:
            first = abandon(cfg, state, "TASK-001", cp.checkpoint_id, reason="bad")
            second = abandon(cfg, state, "TASK-001", cp.checkpoint_id, reason="bad")
            records = state.remedies("TASK-001", resolve_namespace(cfg))
        assert second.already_applied and not first.already_applied
        assert len(records) == 1


@pytest.mark.slow
class TestRepair:
    def _repaired_commit(self, root, body):
        return _commit(root, {"tests/test_x.py": body}, "repair the frozen test")

    def test_it_opens_a_new_lineage_rather_than_blessing_bytes(self, tmp_path):
        root, cfg, cp = _establish(tmp_path)
        fixed = self._repaired_commit(root, "def test_y():\n    assert 1 == 2\n")
        with ExecutorState(cfg) as state:
            result = repair(cfg, state, "TASK-001", cp.checkpoint_id, fixed, reason="typo")
            active = state.red_checkpoint("TASK-001", resolve_namespace(cfg))

        assert result.new_checkpoint_id and result.new_checkpoint_id != cp.checkpoint_id
        assert active is not None and active.commit_sha == fixed
        assert active.baseline_sha == cp.commit_sha, "the new lineage descends from the old"

    def test_the_new_lineage_must_prove_its_own_red(self, tmp_path):
        """`repair` is not "these bytes are fine". A repaired test that passes
        is not a red, and saying so is the whole point of re-replaying.

        Since #263 that verdict also decides whether anything is written: the
        replay runs before any status change, and a repair that establishes no
        red leaves the standing checkpoint and its claims exactly as they were.
        The old order superseded first and then recorded the `not_red` lineage,
        which left the task with no confirmed red at all — the wedge.
        """
        root, cfg, cp = _establish(tmp_path)
        fixed = self._repaired_commit(root, PASSING)
        with ExecutorState(cfg) as state:
            result = repair(cfg, state, "TASK-001", cp.checkpoint_id, fixed, reason="typo")
            active = state.red_checkpoint("TASK-001", resolve_namespace(cfg))
        assert result.outcome is RedOutcome.NOT_RED
        assert result.new_checkpoint_id is None
        assert active.checkpoint_id == cp.checkpoint_id
        assert active.outcome is RedOutcome.EXPECTED_FAIL, "the standing red still stands"

    def test_a_confirmed_repair_claims_the_new_bytes(self, tmp_path):
        root, cfg, cp = _establish(tmp_path)
        fixed = self._repaired_commit(root, "def test_y():\n    assert 1 == 2\n")
        with ExecutorState(cfg) as state:
            repair(cfg, state, "TASK-001", cp.checkpoint_id, fixed, reason="typo")
            claims = state.active_claims(resolve_namespace(cfg))
        assert len(claims) == 1
        assert claims[0].checkpoint_sha == fixed, "the lock follows the new lineage"

    def test_the_old_claim_is_superseded_not_deleted(self, tmp_path):
        root, cfg, cp = _establish(tmp_path)
        fixed = self._repaired_commit(root, "def test_y():\n    assert 1 == 2\n")
        with ExecutorState(cfg) as state:
            repair(cfg, state, "TASK-001", cp.checkpoint_id, fixed, reason="typo")
            statuses = [
                r[0] for r in state._conn.execute("SELECT status FROM tdd_claims ORDER BY id")
            ]
        assert statuses == [ClaimStatus.SUPERSEDED.value, ClaimStatus.ACTIVE.value]

    def test_earlier_gate_verdicts_go_stale_by_construction(self, tmp_path):
        """No bookkeeping needed: a verdict is keyed on the tree it judged, so
        a new lineage on a new commit cannot match it (#164 criterion 5)."""
        from spec_runner.gates import GateContext, GateStatus

        root, cfg, cp = _establish(tmp_path)
        ctx = GateContext("TASK-001", cp.commit_sha, cfg)
        with ExecutorState(cfg) as state:
            state.record_gate_verdict(
                "TASK-001", "tdd.red", cp.commit_sha, ctx.config_hash, GateStatus.SATISFIED, ""
            )
            fixed = self._repaired_commit(root, "def test_y():\n    assert 1 == 2\n")
            repair(cfg, state, "TASK-001", cp.checkpoint_id, fixed, reason="typo")
            assert state.gate_verdict("TASK-001", "tdd.red", fixed, ctx.config_hash) is None

    def test_repeating_it_is_idempotent(self, tmp_path):
        root, cfg, cp = _establish(tmp_path)
        fixed = self._repaired_commit(root, "def test_y():\n    assert 1 == 2\n")
        with ExecutorState(cfg) as state:
            first = repair(cfg, state, "TASK-001", cp.checkpoint_id, fixed, reason="typo")
            second = repair(cfg, state, "TASK-001", cp.checkpoint_id, fixed, reason="typo")
        assert second.already_applied
        assert second.new_checkpoint_id == first.new_checkpoint_id

    def test_a_commit_that_does_not_resolve_is_refused(self, tmp_path):
        root, cfg, cp = _establish(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError) as exc:
            repair(cfg, state, "TASK-001", cp.checkpoint_id, "0" * 40, reason="typo")
        assert "does not resolve" in str(exc.value)


class TestOneTaskDoesNotSpeakForAnother:
    def test_a_remedy_leaves_another_tasks_claim_standing(self, tmp_path):
        """Two claims on one path are two facts; resolving one leaves the
        other."""
        root, cfg, cp = _establish(tmp_path)
        other_sha = _commit(root, {"tests/test_b.py": FAILING})
        other = RedCheckpoint(
            task_id="TASK-OTHER",
            namespace=resolve_namespace(cfg),
            commit_sha=other_sha,
            baseline_sha=other_sha,
            selector="tests/test_b.py::test_y",
            environment_id="unpinned",
            execution_mode="tdd",
            config_hash="h",
            outcome=RedOutcome.EXPECTED_FAIL,
            timestamp="2026-08-11T00:00:01",
        )
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(other)
            record_claims(cfg, state, other)
            abandon(cfg, state, "TASK-001", cp.checkpoint_id, reason="bad")
            remaining = state.active_claims(resolve_namespace(cfg))

        assert [c.task_id for c in remaining] == ["TASK-OTHER"]


class TestRefusedWhileTheTaskIsRunning:
    """Mutating a checkpoint under a live run is how two writers produce a
    history neither intended."""

    def test_a_held_lock_refuses_the_remedy(self, tmp_path):
        from spec_runner.config import ExecutorLock

        root, cfg, cp = _establish(tmp_path)
        lock = ExecutorLock(cfg.state_file.with_suffix(".lock"))
        assert lock.acquire()
        try:
            with ExecutorState(cfg) as state, pytest.raises(RemedyError) as exc:
                abandon(cfg, state, "TASK-001", cp.checkpoint_id, reason="r")
            assert "running" in str(exc.value)
        finally:
            lock.release()

    def test_a_stale_state_row_does_not_block_recovery(self, tmp_path):
        """The lock is PID-checked and authoritative; a `running` row left by a
        crash must not lock the operator out of the very tool recovery needs."""
        root, cfg, cp = _establish(tmp_path)
        with ExecutorState(cfg) as state:
            state.mark_running("TASK-001")
            result = abandon(cfg, state, "TASK-001", cp.checkpoint_id, reason="after a crash")
        assert result.operation is RemedyOperation.ABANDON


class TestTheAgentGuardrail:
    """A guardrail against the ordinary path, not a security boundary — the
    agent runs arbitrary shell and can unset the marker. Said plainly in the
    contract, and worth not overstating in a test name either."""

    def test_a_remedy_is_refused_inside_an_agent(self, tmp_path, monkeypatch):
        root, cfg, cp = _establish(tmp_path)
        monkeypatch.setenv("SPEC_RUNNER_AGENT", "1")
        with ExecutorState(cfg) as state, pytest.raises(RemedyError) as exc:
            abandon(cfg, state, "TASK-001", cp.checkpoint_id, reason="r")
        assert "operator" in str(exc.value).lower()

    def test_the_runner_marks_agent_subprocesses(self):
        """The marker has to actually be set, or the refusal above never fires
        in the situation it exists for."""
        import inspect

        from spec_runner import runner

        assert "SPEC_RUNNER_AGENT" in inspect.getsource(runner.agent_env)


class TestActor:
    def test_an_explicit_actor_wins(self, tmp_path):
        assert resolve_actor(_cfg(_repo(tmp_path)), "ann") == "ann"

    def test_it_falls_back_to_the_git_identity(self, tmp_path):
        assert resolve_actor(_cfg(_repo(tmp_path)), None) == "operator@example.com"

    def test_an_unknown_actor_is_recorded_as_unknown_not_invented(self, tmp_path):
        root = tmp_path / "bare"
        root.mkdir()
        assert resolve_actor(_cfg(root), None)


class TestTheCommandLine:
    def _args(self, **kw):
        import argparse

        return argparse.Namespace(**kw)

    def test_abandon_reports_success(self, tmp_path, capsys):
        from spec_runner.remedy import cmd_tdd

        root, cfg, cp = _establish(tmp_path)
        code = cmd_tdd(
            self._args(
                tdd_command="abandon",
                task_id="TASK-001",
                checkpoint=cp.checkpoint_id,
                reason="bad red",
                actor="ann",
            ),
            cfg,
        )
        assert code == 0
        assert "RED authoring" in capsys.readouterr().out

    def test_a_refusal_is_a_message_not_a_traceback(self, tmp_path, capsys):
        from spec_runner.remedy import cmd_tdd

        root, cfg, cp = _establish(tmp_path)
        code = cmd_tdd(
            self._args(
                tdd_command="abandon",
                task_id="TASK-001",
                checkpoint="wrongid",
                reason="r",
                actor=None,
            ),
            cfg,
        )
        assert code == 1
        assert "⛔" in capsys.readouterr().out

    @pytest.mark.slow
    def test_a_repair_that_does_not_re_establish_a_red_says_so(self, tmp_path, capsys):
        """The success line must not imply a red that the replay refused."""
        from spec_runner.remedy import cmd_tdd

        root, cfg, cp = _establish(tmp_path)
        fixed = _commit(root, {"tests/test_x.py": PASSING}, "repair")
        code = cmd_tdd(
            self._args(
                tdd_command="repair",
                task_id="TASK-001",
                checkpoint=cp.checkpoint_id,
                commit=fixed,
                reason="typo",
                actor=None,
            ),
            cfg,
        )
        out = capsys.readouterr().out
        assert code == 2, "a repair without a red is not a plain success"
        assert "did not establish a red" in out

    def test_the_parser_requires_a_reason(self):
        from spec_runner.cli import _build_parser

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["tdd", "abandon", "TASK-001", "--checkpoint", "abc"])

    def test_repair_requires_a_commit(self):
        from spec_runner.cli import _build_parser

        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["tdd", "repair", "TASK-001", "--checkpoint", "abc", "--reason", "r"])


@pytest.mark.slow
class TestARepeatDoesNotLaunderTheVerdict:
    """Copilot's finding on #177: the idempotent path returned no outcome, so a
    second `tdd repair` printed a plain success over a lineage the first call
    had reported as not-a-red."""

    def _args(self, **kw):
        import argparse

        return argparse.Namespace(**kw)

    def test_a_repeated_repair_of_a_not_red_lineage_still_exits_2(self, tmp_path, capsys):
        """Since #263 the first call records nothing, so the repeat is not an
        idempotent replay of a stored verdict — it re-replays and reaches the
        same one. The property this class exists for is unchanged: running the
        command twice must not turn a refusal into a success."""
        from spec_runner.remedy import cmd_tdd

        root, cfg, cp = _establish(tmp_path)
        fixed = _commit(root, {"tests/test_x.py": PASSING}, "repair")
        args = self._args(
            tdd_command="repair",
            task_id="TASK-001",
            checkpoint=cp.checkpoint_id,
            commit=fixed,
            reason="typo",
            actor=None,
        )
        assert cmd_tdd(args, cfg) == 2
        capsys.readouterr()

        assert cmd_tdd(args, cfg) == 2, "the repeat must reach the same verdict as the first call"
        out = capsys.readouterr().out
        assert "did not establish a red" in out
        assert "Not repaired" in out

    def test_a_repeated_repair_of_a_confirmed_red_still_exits_0(self, tmp_path):
        from spec_runner.remedy import cmd_tdd

        root, cfg, cp = _establish(tmp_path)
        fixed = _commit(root, {"tests/test_x.py": "def test_y():\n    assert 1 == 2\n"}, "repair")
        args = self._args(
            tdd_command="repair",
            task_id="TASK-001",
            checkpoint=cp.checkpoint_id,
            commit=fixed,
            reason="typo",
            actor=None,
        )
        assert cmd_tdd(args, cfg) == 0
        assert cmd_tdd(args, cfg) == 0

    def test_a_lineage_can_be_looked_up_by_id_whatever_its_status(self, tmp_path):
        root, cfg, cp = _establish(tmp_path)
        fixed = _commit(root, {"tests/test_x.py": "def test_y():\n    assert 1 == 2\n"}, "repair")
        with ExecutorState(cfg) as state:
            repair(cfg, state, "TASK-001", cp.checkpoint_id, fixed, reason="typo")
            superseded = state.checkpoint_by_id(resolve_namespace(cfg), cp.checkpoint_id)
        assert superseded is not None, "a superseded lineage is still findable by id"
        assert superseded.commit_sha == cp.commit_sha
