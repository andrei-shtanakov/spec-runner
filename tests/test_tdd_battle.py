"""#141: the battle test for the claim + remedy block (slices 1–3).

Not more unit tests. This walks the sequences an operator actually hits, on a
real repository, through the real entry points — because the failures worth
finding here are the ones that live *between* the units: an interrupted run, a
second workstream, a remedy applied to the wrong lineage.

Owner-specified coverage: mutation · delete · rename · shared claim · abandon ·
repair · crash-resume.

Contract: `docs/superpowers/specs/2026-08-11-claim-and-remedy-contracts.md`
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.claims import ClaimStatus, check_claims
from spec_runner.config import ExecutorConfig
from spec_runner.gates import (
    GateContext,
    GateRegistry,
    GateStatus,
    evaluate_gates,
    register_builtin_gates,
)
from spec_runner.remedy import abandon, repair
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import RedOutcome, resolve_namespace, run_red_phase

pytestmark = pytest.mark.slow

FAILING = "def test_y():\n    assert False, 'not implemented'\n"
PASSING = "def test_y():\n    assert True\n"
OTHER_FAILING = "def test_b():\n    assert False, 'not implemented'\n"


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
        "lint_command": "",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(task_id: str) -> Task:
    return Task(id=task_id, name=task_id, priority="p1", status="todo", estimate="1h")


def _agent(monkeypatch, *, selector: str, writes: dict[str, str]):
    """Stand in for the coding agent: write files, then report a selector."""
    from spec_runner import tdd

    def _fake(config, prompt, **kwargs):
        for name, body in writes.items():
            path = Path(config.project_root) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body)
        return f"TDD_SELECTOR: {selector}\nTASK_COMPLETE"

    monkeypatch.setattr(tdd, "_run_agent", _fake)


def _commit(root: Path, files: dict[str, str], message: str) -> str:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _gate(cfg, state, candidate: str, task_id: str = "TASK-001"):
    registry = GateRegistry()
    register_builtin_gates(cfg, registry=registry)
    ctx = GateContext(
        task_id=task_id,
        checkpoint_sha=candidate,
        config=cfg,
        state=state,
        facts={"execution_mode": "tdd"},
    )
    return evaluate_gates("tests", ctx, registry=registry)


def _establish_red(cfg, monkeypatch, task_id="TASK-001", path="tests/test_x.py", body=FAILING):
    """Run the real RED phase to a confirmed red."""
    selector = f"{path}::{'test_b' if 'test_b' in body else 'test_y'}"
    _agent(monkeypatch, selector=selector, writes={path: body})
    with ExecutorState(cfg) as state:
        result = run_red_phase(_task(task_id), cfg, state)
    assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
    return result.checkpoint


class TestTheClaimHoldsAgainstEveryWayOfBreakingIt:
    """mutation · delete · rename, each through the gate an operator meets."""

    def test_mutation_is_caught(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _establish_red(cfg, monkeypatch)
        candidate = _commit(root, {"tests/test_x.py": PASSING}, "sneak the test green")

        with ExecutorState(cfg) as state:
            outcome = _gate(cfg, state, candidate)
        assert outcome.status is GateStatus.UNSATISFIED
        assert "modified" in " ".join(r.detail or "" for r in outcome.results)

    def test_deletion_is_caught(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _establish_red(cfg, monkeypatch)
        _git(root, "rm", "-q", "tests/test_x.py")
        _git(root, "commit", "-qm", "delete the inconvenient test")

        with ExecutorState(cfg) as state:
            outcome = _gate(cfg, state, _head(root))
        assert outcome.status is GateStatus.UNSATISFIED
        assert "deleted" in " ".join(r.detail or "" for r in outcome.results)

    def test_a_rename_is_caught_and_named_as_a_rename(self, tmp_path, monkeypatch):
        """Both block. Reporting a move as a deletion sends the operator
        looking for a file that is right there."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _establish_red(cfg, monkeypatch)
        _git(root, "mv", "tests/test_x.py", "tests/test_moved.py")
        _git(root, "commit", "-qm", "move it")

        with ExecutorState(cfg) as state:
            outcome = _gate(cfg, state, _head(root))
        detail = " ".join(r.detail or "" for r in outcome.results)
        assert outcome.status is GateStatus.UNSATISFIED
        assert "renamed" in detail and "tests/test_moved.py" in detail

    def test_work_that_leaves_the_claim_alone_proceeds(self, tmp_path, monkeypatch):
        """The lock must not be a wall around the whole repo."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _establish_red(cfg, monkeypatch)
        candidate = _commit(root, {"src/impl.py": "def f():\n    return 1\n"}, "implement")

        with ExecutorState(cfg) as state:
            assert _gate(cfg, state, candidate).status is GateStatus.SATISFIED


class TestTwoWorkstreamsOverOneFile:
    def test_a_second_task_cannot_edit_the_first_ones_claim(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _establish_red(cfg, monkeypatch, task_id="TASK-001")

        # TASK-002 authors its own red but also edits TASK-001's frozen file.
        _agent(
            monkeypatch,
            selector="tests/test_b.py::test_b",
            writes={"tests/test_b.py": OTHER_FAILING, "tests/test_x.py": PASSING},
        )
        with ExecutorState(cfg) as state:
            result = run_red_phase(_task("TASK-002"), cfg, state)
            owners = [c.task_id for c in state.active_claims(resolve_namespace(cfg))]

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "violates an active claim" in (result.detail or "")
        assert owners == ["TASK-001"], "the violating red must not add claims of its own"

    def test_the_violation_names_the_owner(self, tmp_path, monkeypatch):
        """An operator needs to know whose lock they hit, not just that they
        hit one."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _establish_red(cfg, monkeypatch, task_id="TASK-001")
        candidate = _commit(root, {"tests/test_x.py": PASSING}, "edit someone else's test")

        with ExecutorState(cfg) as state:
            violations = check_claims(cfg, state, resolve_namespace(cfg), candidate)
        assert [v.task_id for v in violations] == ["TASK-001"]

    def test_a_second_task_working_elsewhere_is_unaffected(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _establish_red(cfg, monkeypatch, task_id="TASK-001")
        _agent(
            monkeypatch,
            selector="tests/test_b.py::test_b",
            writes={"tests/test_b.py": OTHER_FAILING},
        )
        with ExecutorState(cfg) as state:
            result = run_red_phase(_task("TASK-002"), cfg, state)
            owners = sorted(c.task_id for c in state.active_claims(resolve_namespace(cfg)))
        assert result.outcome is RedOutcome.EXPECTED_FAIL
        assert owners == ["TASK-001", "TASK-002"]


class TestTheRemedyLoopEndToEnd:
    def test_abandon_unblocks_the_file_for_someone_else(self, tmp_path, monkeypatch):
        """The whole point of `abandon`: a bad red must not hold a file
        hostage, and the cure must not be rewriting history."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        checkpoint = _establish_red(cfg, monkeypatch, task_id="TASK-001")
        before = _head(root)

        with ExecutorState(cfg) as state:
            abandon(cfg, state, "TASK-001", checkpoint.checkpoint_id, reason="wrong test entirely")

        _agent(
            monkeypatch,
            selector="tests/test_x.py::test_y",
            writes={"tests/test_x.py": FAILING.replace("not implemented", "take two")},
        )
        with ExecutorState(cfg) as state:
            result = run_red_phase(_task("TASK-002"), cfg, state)
            owners = [c.task_id for c in state.active_claims(resolve_namespace(cfg))]

        assert result.outcome is RedOutcome.EXPECTED_FAIL
        assert owners == ["TASK-002"]
        assert _git(root, "cat-file", "-t", before).stdout.strip() == "commit", (
            "the abandoned red's commit stays in history"
        )

    def test_repair_moves_the_lock_to_the_new_lineage(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        checkpoint = _establish_red(cfg, monkeypatch, task_id="TASK-001")
        fixed = _commit(
            root,
            {"tests/test_x.py": "def test_y():\n    assert 1 == 2, 'still missing'\n"},
            "repair the frozen test",
        )

        with ExecutorState(cfg) as state:
            result = repair(cfg, state, "TASK-001", checkpoint.checkpoint_id, fixed, reason="typo")
            claims = state.active_claims(resolve_namespace(cfg))
            outcome = _gate(cfg, state, fixed)

        assert result.outcome is RedOutcome.EXPECTED_FAIL
        assert [c.checkpoint_sha for c in claims] == [fixed]
        assert outcome.status is GateStatus.SATISFIED, "the repaired lineage satisfies the gate"

    def test_a_repair_that_kills_the_red_leaves_the_task_gated(self, tmp_path, monkeypatch):
        """`repair` is not a way to get past the gate."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        checkpoint = _establish_red(cfg, monkeypatch, task_id="TASK-001")
        fixed = _commit(root, {"tests/test_x.py": PASSING}, "repair it into passing")

        with ExecutorState(cfg) as state:
            result = repair(cfg, state, "TASK-001", checkpoint.checkpoint_id, fixed, reason="oops")
            outcome = _gate(cfg, state, fixed)

        assert result.outcome is RedOutcome.NOT_RED
        assert outcome.status is GateStatus.UNSATISFIED

    def test_a_remedy_cannot_be_aimed_at_a_superseded_lineage(self, tmp_path, monkeypatch):
        """After a repair the old id is history, and CAS says so instead of
        applying the remedy to whatever is current."""
        from spec_runner.remedy import RemedyError

        root = _repo(tmp_path)
        cfg = _cfg(root)
        checkpoint = _establish_red(cfg, monkeypatch, task_id="TASK-001")
        fixed = _commit(root, {"tests/test_x.py": "def test_y():\n    assert 1 == 2\n"}, "repair")
        with ExecutorState(cfg) as state:
            repair(cfg, state, "TASK-001", checkpoint.checkpoint_id, fixed, reason="typo")
            with pytest.raises(RemedyError) as exc:
                abandon(cfg, state, "TASK-001", checkpoint.checkpoint_id, reason="second thoughts")
        assert "not the active checkpoint" in str(exc.value)


class TestCrashResume:
    """Everything here is about the moment *between* two writes. A run that
    dies there must leave a state that is wrong in the safe direction."""

    def test_a_crash_before_the_checkpoint_leaves_no_confirmed_red(self, tmp_path, monkeypatch):
        """The red commit exists but nothing recorded it. The next run must
        re-author rather than inherit an unrecorded claim."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _commit(root, {"tests/test_x.py": FAILING}, "TASK-001: red (run died here)")

        with ExecutorState(cfg) as state:
            outcome = _gate(cfg, state, _head(root))
        assert outcome.status is GateStatus.UNSATISFIED
        assert "no confirmed red" in " ".join(r.detail or "" for r in outcome.results)

    def test_a_confirmed_red_is_never_recorded_without_its_lock(self, tmp_path, monkeypatch):
        """The dangerous in-between: a checkpoint written, the claims not. That
        would be a red that counts over a file anyone may edit — the gate would
        pass and the byte-lock would not exist."""
        from spec_runner import tdd

        root = _repo(tmp_path)
        cfg = _cfg(root)
        _agent(monkeypatch, selector="tests/test_x.py::test_y", writes={"tests/test_x.py": FAILING})

        real_record_claims = tdd.record_claims

        def _die(*args, **kwargs):
            raise RuntimeError("the process died between the two writes")

        monkeypatch.setattr(tdd, "record_claims", _die)
        with ExecutorState(cfg) as state:
            result = run_red_phase(_task("TASK-001"), cfg, state)
        assert result.outcome is RedOutcome.UNVERIFIABLE

        monkeypatch.setattr(tdd, "record_claims", real_record_claims)
        with ExecutorState(cfg) as state:
            checkpoint = state.red_checkpoint("TASK-001", resolve_namespace(cfg))
            claims = state.active_claims(resolve_namespace(cfg))

        assert not (
            checkpoint is not None and checkpoint.outcome is RedOutcome.EXPECTED_FAIL and not claims
        ), (
            "a confirmed red survived without its claims: the gate would pass "
            "while the file it depends on is unlocked"
        )

    def test_state_survives_a_reopen(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        checkpoint = _establish_red(cfg, monkeypatch, task_id="TASK-001")
        with ExecutorState(cfg) as state:
            abandon(cfg, state, "TASK-001", checkpoint.checkpoint_id, reason="restart")

        with ExecutorState(cfg) as state:
            assert state.red_checkpoint("TASK-001", resolve_namespace(cfg)) is None
            assert state.active_claims(resolve_namespace(cfg)) == []
            [record] = state.remedies("TASK-001", resolve_namespace(cfg))
            statuses = [r[0] for r in state._conn.execute("SELECT status FROM tdd_claims")]
        assert record.reason == "restart"
        assert statuses == [ClaimStatus.ABANDONED.value], "evidence outlives the run that made it"

    def test_a_leaked_replay_worktree_does_not_break_the_next_red(self, tmp_path, monkeypatch):
        """A crash during replay can leave a registered worktree, which makes
        the next `git worktree add` fail. The next run must still work."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        stale = tmp_path / "stale-worktree"
        _git(root, "worktree", "add", "--detach", str(stale), _head(root))

        checkpoint = _establish_red(cfg, monkeypatch, task_id="TASK-001")
        assert checkpoint.outcome is RedOutcome.EXPECTED_FAIL

    def test_a_stale_running_row_does_not_block_the_remedy(self, tmp_path, monkeypatch):
        """The lock is PID-checked; a `running` row left by a crash must not
        lock the operator out of the tool recovery needs."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        checkpoint = _establish_red(cfg, monkeypatch, task_id="TASK-001")
        with ExecutorState(cfg) as state:
            state.mark_running("TASK-001")
        with ExecutorState(cfg) as state:
            result = abandon(
                cfg, state, "TASK-001", checkpoint.checkpoint_id, reason="cleaning up after a crash"
            )
        assert result.operation.value == "abandon"


class TestTheEvidenceReadsBack:
    def test_a_full_cycle_leaves_a_readable_trail(self, tmp_path, monkeypatch):
        """Red → repair → abandon. Every step must still be findable: the point
        of never deleting is that someone can reconstruct what was believed."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        first = _establish_red(cfg, monkeypatch, task_id="TASK-001")
        fixed = _commit(root, {"tests/test_x.py": "def test_y():\n    assert 1 == 2\n"}, "repair")
        with ExecutorState(cfg) as state:
            repaired = repair(cfg, state, "TASK-001", first.checkpoint_id, fixed, reason="typo")
            abandon(cfg, state, "TASK-001", repaired.new_checkpoint_id, reason="give up")
            records = state.remedies("TASK-001", resolve_namespace(cfg))
            claim_statuses = sorted(
                r[0] for r in state._conn.execute("SELECT status FROM tdd_claims")
            )
            checkpoint_statuses = sorted(
                r[0] for r in state._conn.execute("SELECT status FROM red_checkpoints")
            )

        assert [r.operation.value for r in records] == ["repair", "abandon"]
        assert all(r.actor and r.reason for r in records)
        assert claim_statuses == ["abandoned", "superseded"]
        assert checkpoint_statuses == ["abandoned", "superseded"]


class TestAnUnlockableRedIsRefused:
    """Copilot's finding on #179: the same hole by a different route. A path
    the claim contract refuses — a symlink, say — was skipped with a warning,
    so the checkpoint was recorded anyway and the gate passed over a file
    nobody was protecting."""

    def test_a_symlinked_test_file_does_not_yield_a_confirmed_red(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        (root / "tests").mkdir()
        (root / "tests" / "real.py").write_text(FAILING)
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "the real test")

        def _agent_symlinks(config, prompt, **kwargs):
            link = Path(config.project_root) / "tests" / "test_link.py"
            link.symlink_to(Path(config.project_root) / "tests" / "real.py")
            return "TDD_SELECTOR: tests/test_link.py::test_y\nTASK_COMPLETE"

        from spec_runner import tdd

        monkeypatch.setattr(tdd, "_run_agent", _agent_symlinks)
        with ExecutorState(cfg) as state:
            result = run_red_phase(_task("TASK-001"), cfg, state)
            checkpoint = state.red_checkpoint("TASK-001", resolve_namespace(cfg))
            claims = state.active_claims(resolve_namespace(cfg))

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert checkpoint is None, "an unlockable red must not be recorded as confirmed"
        assert claims == []

    def test_a_repair_whose_file_cannot_be_locked_keeps_the_old_lock(self, tmp_path, monkeypatch):
        """The sharp version: the repaired commit **does** re-establish a red,
        but its file cannot be claimed. Refusing must not cost the operator the
        lock they already had."""
        from spec_runner.remedy import RemedyError

        root = _repo(tmp_path)
        cfg = _cfg(root)
        checkpoint = _establish_red(cfg, monkeypatch, task_id="TASK-001")

        # Replace the frozen test with a symlink to a genuinely failing test:
        # pytest follows it and reports a red, so the replay confirms — and the
        # claim contract still refuses the path.
        (root / "tests" / "actual.py").write_text(FAILING)
        _git(root, "rm", "-q", "tests/test_x.py")
        (root / "tests" / "test_x.py").symlink_to(root / "tests" / "actual.py")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "replace the test with a symlink")
        fixed = _head(root)

        with ExecutorState(cfg) as state:
            with pytest.raises(RemedyError):
                repair(cfg, state, "TASK-001", checkpoint.checkpoint_id, fixed, reason="sneaky")
            claims = state.active_claims(resolve_namespace(cfg))
            still_active = state.red_checkpoint("TASK-001", resolve_namespace(cfg))

        assert [c.task_id for c in claims] == ["TASK-001"], (
            "a refused repair must not release the lock it failed to replace"
        )
        assert still_active is not None
