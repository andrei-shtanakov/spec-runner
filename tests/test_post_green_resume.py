"""#232: the post-green half gets a remedy, and it does not launder anything.

`abandon` and `repair` both answer questions about a *red*. An operator whose
run died after green reached for the nearest one, and `repair` — which asks "is
this changed test still a red?" — answered honestly that it is not, superseding
the confirmed red the task still needed. The lifecycle then offered
`red_authoring` as its only door, and a red cannot exist precisely because the
work is done.

`tdd resume` reinstates the evidence. The property that makes it safe is
negative: **it introduces no new way to satisfy the RED gate.** The gate is
untouched and still demands a confirmed `expected_fail` whose commit is an
ancestor of the tree in hand; resume only changes which row is standing, and
only when such a row already exists.

The half this file exists to defend hardest is claims. The design recommended
reinstating the checkpoint alone; the owner overruled it, and was right — that
would make this chain legal:

    confirmed RED + claim
    → GREEN modifies the frozen test
    → repair supersedes both
    → resume returns only the RED
    → merge, with no byte-lock on the evidence

which launders the exact violation the byte-lock exists to catch, using the
command built to help.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.claims import ClaimStatus, record_claims
from spec_runner.config import ExecutorConfig
from spec_runner.lifecycle import TddPhase, advance
from spec_runner.remedy import RemedyError, RemedyOperation, repair, resume
from spec_runner.state import ExecutorState
from spec_runner.tdd import RedCheckpoint, RedOutcome, _config_hash, resolve_namespace

REASON = "green established; review died on a provider session limit (#229)"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        # Outside the repo: `git add -A` in the fixture would otherwise commit
        # the live state DB, and a later checkout refuses to move over it.
        "state_file": root.parent / ".state.db",
        "logs_dir": root.parent / ".logs",
        "execution_mode": "tdd",
        "test_command": "python -m pytest",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _pilot(tmp_path: Path, *, green_touches_test: bool = False):
    """The pilot's shape: a confirmed red, a green built on it, then a repair
    that superseded both — which is where TASK-101 is stuck right now."""
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
    (root / "tests" / "test_x.py").write_text("def test_y():\n    assert False\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "red")
    red_sha = _git(root, "rev-parse", "HEAD").stdout.strip()

    checkpoint = RedCheckpoint(
        task_id="TASK-101",
        namespace=resolve_namespace(cfg),
        commit_sha=red_sha,
        baseline_sha=red_sha,
        selector="tests/test_x.py::test_y",
        environment_id="unpinned",
        execution_mode="tdd",
        config_hash=_config_hash(cfg),
        outcome=RedOutcome.EXPECTED_FAIL,
        timestamp="2026-08-12T17:11:57",
    )
    with ExecutorState(cfg) as state:
        state.record_red_checkpoint(checkpoint)
        record_claims(cfg, state, checkpoint)
        for phase in (
            TddPhase.RED_AUTHORING,
            TddPhase.RED_VERIFYING,
            TddPhase.GREEN_IMPLEMENTING,
        ):
            advance(state, resolve_namespace(cfg), "TASK-101", phase)

    # The green. In the pilot it also edited the frozen test — the case the
    # claims requirement is about.
    (root / "src.py").write_text("def thing():\n    return 1\n")
    if green_touches_test:
        (root / "tests" / "test_x.py").write_text(
            "def test_y():\n    assert True\n\n\ndef test_extra():\n    assert True\n"
        )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "green")
    return root, cfg, checkpoint


def _retire(cfg: ExecutorConfig, checkpoint: RedCheckpoint) -> None:
    """What `repair` did: checkpoint and claim both superseded."""
    from spec_runner.remedy import CheckpointStatus

    with ExecutorState(cfg) as state:
        state.set_checkpoint_status(
            resolve_namespace(cfg), checkpoint.checkpoint_id, CheckpointStatus.SUPERSEDED
        )
        state.supersede_claims(
            resolve_namespace(cfg),
            "TASK-101",
            ClaimStatus.SUPERSEDED,
            checkpoint_id=checkpoint.checkpoint_id,
        )


@pytest.mark.slow
class TestTheWedgeOpens:
    def test_the_gate_refuses_before_the_resume(self, tmp_path):
        from spec_runner.gates import GateContext, GateStatus, _red_gate

        root, cfg, cp = _pilot(tmp_path)
        _retire(cfg, cp)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()

        with ExecutorState(cfg) as state:
            result = _red_gate(
                GateContext(
                    task_id="TASK-101",
                    checkpoint_sha=head,
                    config=cfg,
                    state=state,
                    facts={"execution_mode": "tdd"},
                )
            )
        assert result.status is GateStatus.UNSATISFIED

    def test_and_is_satisfied_after_it(self, tmp_path):
        from spec_runner.gates import GateContext, GateStatus, _red_gate

        root, cfg, cp = _pilot(tmp_path)
        _retire(cfg, cp)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()

        with ExecutorState(cfg) as state:
            resume(cfg, state, "TASK-101", reason=REASON)
            result = _red_gate(
                GateContext(
                    task_id="TASK-101",
                    checkpoint_sha=head,
                    config=cfg,
                    state=state,
                    facts={"execution_mode": "tdd"},
                )
            )
        assert result.status is GateStatus.SATISFIED

    def test_the_red_phase_reuses_it_rather_than_paying_again(self, tmp_path, monkeypatch):
        """Variant (a): no execution-path change. With the red standing again,
        `run_red_phase` takes its existing reuse branch and no agent is called."""
        from spec_runner import tdd
        from spec_runner.task import Task
        from spec_runner.tdd import run_red_phase

        root, cfg, cp = _pilot(tmp_path)
        _retire(cfg, cp)
        calls: list[str] = []
        monkeypatch.setattr(
            tdd, "_run_agent", lambda *a, **k: calls.append("paid") or tdd.AgentCall(text="")
        )

        with ExecutorState(cfg) as state:
            resume(cfg, state, "TASK-101", reason=REASON)
            result = run_red_phase(
                Task(id="TASK-101", name="t", priority="p1", status="todo", estimate="1h"),
                cfg,
                state,
            )

        assert result.outcome is RedOutcome.EXPECTED_FAIL
        assert calls == [], "resuming must not re-author a red that already exists"


@pytest.mark.slow
class TestClaimsComeBackWithIt:
    def test_the_lock_is_reinstated_not_just_the_red(self, tmp_path):
        """The overruled recommendation. Reinstating the red alone would make
        the laundering chain in this module's docstring legal."""
        root, cfg, cp = _pilot(tmp_path)
        _retire(cfg, cp)

        with ExecutorState(cfg) as state:
            resume(cfg, state, "TASK-101", reason=REASON)
            claims = state.active_claims(resolve_namespace(cfg))

        assert [c.path for c in claims] == ["tests/test_x.py"]

    def test_a_green_that_edited_the_frozen_test_still_cannot_merge(self, tmp_path):
        """End to end: the pilot's actual violation. Resume records the
        decision, and the claims gate refuses the candidate anyway."""
        from spec_runner.gates import GateContext, GateStatus, evaluate_claims

        root, cfg, cp = _pilot(tmp_path, green_touches_test=True)
        _retire(cfg, cp)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()

        with ExecutorState(cfg) as state:
            _result, conflicts = resume(cfg, state, "TASK-101", reason=REASON)
            verdict = evaluate_claims(
                GateContext(
                    task_id="TASK-101",
                    checkpoint_sha=head,
                    config=cfg,
                    state=state,
                    facts={"execution_mode": "tdd"},
                )
            )

        assert [c.path for c in conflicts] == ["tests/test_x.py"]
        assert verdict.status is GateStatus.UNSATISFIED
        assert "claim violated" in (verdict.detail or "")

    def test_the_conflict_is_reported_by_the_command_not_only_by_the_gate(self, tmp_path):
        """Preflight, so an operator learns before the merge attempt rather
        than after it."""
        root, cfg, cp = _pilot(tmp_path, green_touches_test=True)
        _retire(cfg, cp)

        with ExecutorState(cfg) as state:
            _result, conflicts = resume(cfg, state, "TASK-101", reason=REASON)

        assert len(conflicts) == 1
        assert conflicts[0].claimed != conflicts[0].found
        assert conflicts[0].found is not None, "the file exists in HEAD, with other bytes"

    def test_an_unrelated_retired_claim_is_not_revived(self, tmp_path):
        """Resume is not an amnesty: only the reinstated lineage's own claims."""
        root, cfg, cp = _pilot(tmp_path)
        other = RedCheckpoint(
            task_id="TASK-101",
            namespace=resolve_namespace(cfg),
            commit_sha=cp.commit_sha,
            baseline_sha=cp.commit_sha,
            selector="tests/test_other.py::test_z",
            environment_id="unpinned",
            execution_mode="tdd",
            config_hash="h",
            outcome=RedOutcome.EXPECTED_FAIL,
            timestamp="2026-08-10T00:00:00",
        )
        (root / "tests" / "test_other.py").write_text("def test_z():\n    assert False\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "other")
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(other)
            record_claims(cfg, state, other)
        _retire(cfg, cp)
        with ExecutorState(cfg) as state:
            state.supersede_claims(
                resolve_namespace(cfg),
                "TASK-101",
                ClaimStatus.ABANDONED,
                checkpoint_id=other.checkpoint_id,
            )

        with ExecutorState(cfg) as state:
            resume(cfg, state, "TASK-101", reason=REASON, checkpoint_id=cp.checkpoint_id)
            paths = {c.path for c in state.active_claims(resolve_namespace(cfg))}

        assert paths == {"tests/test_x.py"}

    def test_the_two_flips_are_one_transaction(self, tmp_path):
        """A resume that reinstated a red and then failed to reinstate its lock
        would leave a confirmed red with nothing protecting its evidence — and
        would report success. Both roll back together."""
        root, cfg, cp = _pilot(tmp_path)
        _retire(cfg, cp)

        import sqlite3

        with ExecutorState(cfg) as state:
            # The claims flip cannot succeed; the checkpoint flip must not
            # survive it. (Dropping the table is a blunt way to break the
            # second statement, and a faithful one: whatever the cause, the
            # transaction either lands whole or not at all.)
            state._conn.execute("DROP TABLE tdd_claims")
            with pytest.raises(sqlite3.OperationalError):
                state.reinstate_checkpoint_with_claims(
                    resolve_namespace(cfg), "TASK-101", cp.checkpoint_id
                )
            assert state.red_checkpoint("TASK-101", resolve_namespace(cfg)) is None, (
                "the checkpoint flip must not survive a failed claim flip"
            )


@pytest.mark.slow
class TestRefusals:
    def test_a_task_with_no_confirmed_red_cannot_be_resumed(self, tmp_path):
        root, cfg, cp = _pilot(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="no confirmed red"):
            resume(cfg, state, "TASK-999", reason=REASON)

    def test_a_red_that_is_not_an_ancestor_of_head_is_refused(self, tmp_path):
        """A tree not built on that red must not inherit its evidence."""
        root, cfg, cp = _pilot(tmp_path)
        _retire(cfg, cp)
        base = _git(root, "rev-list", "--max-parents=0", "HEAD").stdout.strip()
        _git(root, "checkout", "-q", "-b", "elsewhere", base)

        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="not an ancestor"):
            resume(cfg, state, "TASK-101", reason=REASON)

    def test_a_task_that_never_reached_green_is_refused(self, tmp_path):
        root = tmp_path / "repo"
        root.mkdir(parents=True)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "o@e.c")
        _git(root, "config", "user.name", "O")
        (root / "README.md").write_text("x\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "base")
        cfg = _cfg(root)
        sha = _git(root, "rev-parse", "HEAD").stdout.strip()
        cp = RedCheckpoint(
            task_id="TASK-101",
            namespace=resolve_namespace(cfg),
            commit_sha=sha,
            baseline_sha=sha,
            selector="tests/test_x.py::test_y",
            environment_id="unpinned",
            execution_mode="tdd",
            config_hash="h",
            outcome=RedOutcome.EXPECTED_FAIL,
            timestamp="2026-08-12T00:00:00",
        )
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(cp)
            advance(state, resolve_namespace(cfg), "TASK-101", TddPhase.RED_AUTHORING)
            with pytest.raises(RemedyError, match="no established green"):
                resume(cfg, state, "TASK-101", reason=REASON)

    def test_several_confirmed_reds_are_not_resolved_by_guessing(self, tmp_path):
        """The rule the other remedies follow (F-5). Reinstating the wrong
        lineage reinstates the wrong byte-lock with it, which is a worse
        mistake than making the operator name one."""
        root, cfg, cp = _pilot(tmp_path)
        second = RedCheckpoint(
            task_id="TASK-101",
            namespace=resolve_namespace(cfg),
            commit_sha=cp.commit_sha,
            baseline_sha=cp.commit_sha,
            selector="tests/test_other.py::test_z",
            environment_id="unpinned",
            execution_mode="tdd",
            config_hash=_config_hash(cfg),
            outcome=RedOutcome.EXPECTED_FAIL,
            timestamp="2026-08-12T18:00:00",
        )
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(second)
        _retire(cfg, cp)

        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="2 confirmed reds"):
            resume(cfg, state, "TASK-101", reason=REASON)

    def test_naming_one_resolves_it(self, tmp_path):
        root, cfg, cp = _pilot(tmp_path)
        second = RedCheckpoint(
            task_id="TASK-101",
            namespace=resolve_namespace(cfg),
            commit_sha=cp.commit_sha,
            baseline_sha=cp.commit_sha,
            selector="tests/test_other.py::test_z",
            environment_id="unpinned",
            execution_mode="tdd",
            config_hash=_config_hash(cfg),
            outcome=RedOutcome.EXPECTED_FAIL,
            timestamp="2026-08-12T18:00:00",
        )
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(second)
        _retire(cfg, cp)

        with ExecutorState(cfg) as state:
            result, _conflicts = resume(
                cfg, state, "TASK-101", reason=REASON, checkpoint_id=cp.checkpoint_id
            )

        assert result.checkpoint_id == cp.checkpoint_id

    def test_an_unknown_checkpoint_is_refused(self, tmp_path):
        root, cfg, cp = _pilot(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="not a confirmed red"):
            resume(cfg, state, "TASK-101", reason=REASON, checkpoint_id="deadbeefcafe")

    def test_a_reason_is_required(self, tmp_path):
        root, cfg, cp = _pilot(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="reason"):
            resume(cfg, state, "TASK-101", reason="")

    def test_an_agent_cannot_resume(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SPEC_RUNNER_AGENT", "1")
        root, cfg, cp = _pilot(tmp_path)
        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="agent"):
            resume(cfg, state, "TASK-101", reason=REASON)

    def test_repeating_it_is_a_no_op(self, tmp_path):
        root, cfg, cp = _pilot(tmp_path)
        _retire(cfg, cp)
        with ExecutorState(cfg) as state:
            resume(cfg, state, "TASK-101", reason=REASON)
            result, _conflicts = resume(cfg, state, "TASK-101", reason=REASON)

        assert result.already_applied is True


@pytest.mark.slow
class TestRepairAfterGreen:
    def test_it_is_refused_and_points_at_resume(self, tmp_path):
        """§5: repair asks whether a changed test is still a red, which has no
        honest answer once the implementation exists — and answering it retires
        the evidence. That is how the pilot got here."""
        root, cfg, cp = _pilot(tmp_path)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()

        with ExecutorState(cfg) as state, pytest.raises(RemedyError, match="resume") as exc:
            repair(cfg, state, "TASK-101", cp.checkpoint_id, head, reason="fix it")

        assert "green_implementing" in str(exc.value)

    def test_before_green_it_still_works(self, tmp_path):
        """The refusal must not swallow the case repair was built for."""
        root = tmp_path / "repo"
        root.mkdir(parents=True)
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "o@e.c")
        _git(root, "config", "user.name", "O")
        (root / "tests").mkdir()
        (root / "tests" / "test_x.py").write_text("def test_y():\n    assert False\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "red")
        cfg = _cfg(root)
        sha = _git(root, "rev-parse", "HEAD").stdout.strip()
        cp = RedCheckpoint(
            task_id="TASK-101",
            namespace=resolve_namespace(cfg),
            commit_sha=sha,
            baseline_sha=sha,
            selector="tests/test_x.py::test_y",
            environment_id="unpinned",
            execution_mode="tdd",
            config_hash="h",
            outcome=RedOutcome.EXPECTED_FAIL,
            timestamp="2026-08-12T00:00:00",
        )
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(cp)
            record_claims(cfg, state, cp)
            advance(state, resolve_namespace(cfg), "TASK-101", TddPhase.RED_AUTHORING)
            # Not an assertion about the verdict — only that the remedy is
            # allowed to run and reach one.
            result = repair(cfg, state, "TASK-101", cp.checkpoint_id, sha, reason="legit edit")

        assert result.operation is RemedyOperation.REPAIR
