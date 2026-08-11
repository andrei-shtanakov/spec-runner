"""#164: checkpoint commit + pre-terminal policy gates — the mechanism.

Dormant by design. With no gate registered, execution and terminal behaviour
are unchanged (criterion 8); the first real consumer is the review policy
(#157), the second is TDD's confirmed red (#141).

The load-bearing property is criterion 5: **a verdict is a statement about a
specific tree under a specific policy**, and it stops being one the moment
either changes. Without that, evidence from before a change legitimises the
change — the same shape as the harness-guard bypass (#137), one level up.

Design: `docs/superpowers/specs/2026-08-11-checkpoint-and-pre-terminal-gates-design.md`
"""

from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.gates import (
    GateContext,
    GateRegistry,
    GateResult,
    GateStatus,
    evaluate_gates,
)
from spec_runner.state import ExecutorState, PhaseOutcome


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "state.db",
        "logs_dir": tmp_path / "logs",
        "create_git_branch": False,
        "auto_commit": False,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(task_id: str = "TASK-001"):
    from spec_runner.task import Task

    return Task(id=task_id, name="t", priority="p1", status="in_progress", estimate="1h")


def _ctx(state, cfg, sha="abc1234", task="TASK-001") -> GateContext:
    return GateContext(task_id=task, checkpoint_sha=sha, config=cfg, state=state)


def _gate(gate_id: str, result: GateResult, phase: str = "review"):
    calls: list[GateContext] = []

    def _evaluate(ctx: GateContext) -> GateResult:
        calls.append(ctx)
        return result

    return gate_id, phase, _evaluate, calls


class TestDormantWithoutConsumers:
    def test_no_gates_means_satisfied(self, tmp_path):
        """Criterion 8: a project that enables nothing sees nothing."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            outcome = evaluate_gates("review", _ctx(state, cfg), registry=GateRegistry())
        assert outcome.status is GateStatus.SATISFIED
        assert outcome.results == []

    def test_a_registry_is_empty_by_default(self):
        assert GateRegistry().for_phase("review") == []


class TestDeclarativeRegistration:
    """Criterion 7: a second consumer must not arrive as a special case inside
    the first one's code."""

    def test_gates_run_for_their_phase_only(self, tmp_path):
        cfg = _cfg(tmp_path)
        registry = GateRegistry()
        gid, phase, ev, calls = _gate("review", GateResult(GateStatus.SATISFIED, PhaseOutcome.PASS))
        registry.register(gid, phase, ev)
        other = _gate(
            "tdd.red", GateResult(GateStatus.SATISFIED, PhaseOutcome.EXPECTED_FAIL), "tests"
        )
        registry.register(other[0], other[1], other[2])

        with ExecutorState(cfg) as state:
            evaluate_gates("review", _ctx(state, cfg), registry=registry)

        assert len(calls) == 1
        assert other[3] == [], "a gate ran outside its phase"

    def test_two_consumers_coexist(self, tmp_path):
        cfg = _cfg(tmp_path)
        registry = GateRegistry()
        a = _gate("a", GateResult(GateStatus.SATISFIED, PhaseOutcome.PASS))
        b = _gate("b", GateResult(GateStatus.UNSATISFIED, PhaseOutcome.UNEXPECTED_FAIL))
        for g in (a, b):
            registry.register(g[0], g[1], g[2])

        with ExecutorState(cfg) as state:
            outcome = evaluate_gates("review", _ctx(state, cfg), registry=registry)

        assert outcome.status is GateStatus.UNSATISFIED, (
            "one unsatisfied gate holds the whole phase"
        )
        assert {r.gate_id for r in outcome.results} == {"a", "b"}


class TestVerdictIsBoundToTheTree:
    """Criterion 3 and criterion 5."""

    def test_a_verdict_is_stored_against_sha_and_config_hash(self, tmp_path):
        cfg = _cfg(tmp_path)
        registry = GateRegistry()
        g = _gate("review", GateResult(GateStatus.SATISFIED, PhaseOutcome.PASS))
        registry.register(g[0], g[1], g[2])

        with ExecutorState(cfg) as state:
            ctx = _ctx(state, cfg, sha="aaa1111")
            evaluate_gates("review", ctx, registry=registry)
            stored = state.gate_verdict("TASK-001", "review", "aaa1111", ctx.config_hash)

        assert stored is not None and stored.status is GateStatus.SATISFIED

    def test_a_stale_verdict_does_not_clear_a_new_sha(self, tmp_path):
        """The whole point: evidence about the old tree says nothing about the
        new one. Recording it and then moving the tree must not read as pass."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            old = _ctx(state, cfg, sha="aaa1111")
            state.record_gate_verdict(
                "TASK-001",
                "review",
                old.checkpoint_sha,
                old.config_hash,
                GateStatus.SATISFIED,
                "looked fine",
            )
            assert state.gate_verdict("TASK-001", "review", "bbb2222", old.config_hash) is None

    def test_a_verdict_does_not_survive_a_policy_change(self, tmp_path):
        """Same tree, different policy: the earlier answer was to a different
        question."""
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            state.record_gate_verdict(
                "TASK-001", "review", "aaa1111", "cfghash-1", GateStatus.SATISFIED, ""
            )
            assert state.gate_verdict("TASK-001", "review", "aaa1111", "cfghash-2") is None

    def test_the_config_hash_changes_with_the_policy(self, tmp_path):
        """`review_policy` joins POLICY_KEYS when #157 lands; the mechanism is
        checked here with a key that exists today."""
        a = _ctx(None, _cfg(tmp_path, gate_recovery_attempts=1))
        b = _ctx(None, _cfg(tmp_path, gate_recovery_attempts=3))
        assert a.config_hash != b.config_hash

    def test_the_config_hash_is_stable_for_the_same_policy(self, tmp_path):
        a = _ctx(None, _cfg(tmp_path, gate_recovery_attempts=2))
        b = _ctx(None, _cfg(tmp_path, gate_recovery_attempts=2))
        assert a.config_hash == b.config_hash

    def test_an_unrelated_config_edit_does_not_invalidate_a_verdict(self, tmp_path):
        """Hashing the whole config would invalidate verdicts on any edit and
        train people to ignore staleness."""
        a = _ctx(None, _cfg(tmp_path, max_retries=3))
        b = _ctx(None, _cfg(tmp_path, max_retries=7))
        assert a.config_hash == b.config_hash


class TestResumable:
    """Criterion 6: a crash after the checkpoint must not lose what was known."""

    def test_a_verdict_survives_a_reopen(self, tmp_path):
        cfg = _cfg(tmp_path)
        with ExecutorState(cfg) as state:
            ctx = _ctx(state, cfg)
            state.record_gate_verdict(
                "TASK-001",
                "review",
                ctx.checkpoint_sha,
                ctx.config_hash,
                GateStatus.UNSATISFIED,
                "findings",
            )
        with ExecutorState(cfg) as state:
            ctx = _ctx(state, cfg)
            stored = state.gate_verdict("TASK-001", "review", ctx.checkpoint_sha, ctx.config_hash)
        assert stored is not None and stored.status is GateStatus.UNSATISFIED


class TestInstrumentErrorIsRecoveredBoundedly:
    """An instrument error is not a defect in the work: retry, then give up as
    infrastructure — never NEEDS_HUMAN on the first stumble."""

    def test_an_error_is_retried_up_to_the_bound(self, tmp_path):
        cfg = _cfg(tmp_path, gate_recovery_attempts=2)
        registry = GateRegistry()
        attempts = {"n": 0}

        def _flaky(ctx):
            attempts["n"] += 1
            return GateResult(GateStatus.INSTRUMENT_ERROR, PhaseOutcome.ERROR, "cli died")

        registry.register("review", "review", _flaky)
        with ExecutorState(cfg) as state:
            outcome = evaluate_gates("review", _ctx(state, cfg), registry=registry)

        assert attempts["n"] == 3, "expected the first attempt plus two recoveries"
        assert outcome.status is GateStatus.INSTRUMENT_ERROR

    def test_recovery_stops_as_soon_as_it_succeeds(self, tmp_path):
        cfg = _cfg(tmp_path, gate_recovery_attempts=3)
        registry = GateRegistry()
        attempts = {"n": 0}

        def _recovers(ctx):
            attempts["n"] += 1
            if attempts["n"] == 1:
                return GateResult(GateStatus.INSTRUMENT_ERROR, PhaseOutcome.ERROR)
            return GateResult(GateStatus.SATISFIED, PhaseOutcome.PASS)

        registry.register("review", "review", _recovers)
        with ExecutorState(cfg) as state:
            outcome = evaluate_gates("review", _ctx(state, cfg), registry=registry)

        assert attempts["n"] == 2
        assert outcome.status is GateStatus.SATISFIED

    def test_an_unsatisfied_gate_is_not_retried(self, tmp_path):
        """ "The gate says no" is an answer. Only "could not answer" is retried."""
        cfg = _cfg(tmp_path, gate_recovery_attempts=3)
        registry = GateRegistry()
        attempts = {"n": 0}

        def _says_no(ctx):
            attempts["n"] += 1
            return GateResult(GateStatus.UNSATISFIED, PhaseOutcome.UNEXPECTED_FAIL)

        registry.register("review", "review", _says_no)
        with ExecutorState(cfg) as state:
            evaluate_gates("review", _ctx(state, cfg), registry=registry)
        assert attempts["n"] == 1


class TestOutcomesAreRecordedToo:
    def test_the_phase_outcome_lands_in_the_append_only_history(self, tmp_path):
        """Gates speak the slice-0 vocabulary; their evidence goes where all
        the other phase evidence goes."""
        cfg = _cfg(tmp_path)
        registry = GateRegistry()
        registry.register(
            "review",
            "review",
            lambda ctx: GateResult(
                GateStatus.UNSATISFIED, PhaseOutcome.UNEXPECTED_FAIL, "2 findings"
            ),
        )
        with ExecutorState(cfg) as state:
            evaluate_gates("review", _ctx(state, cfg), registry=registry)
            history = state.phase_history("TASK-001")

        assert [r.outcome for r in history] == [PhaseOutcome.UNEXPECTED_FAIL]
        assert history[0].detail == "2 findings"


class TestGatesNeverTouchGit:
    """Criterion 4: fixes create a new commit; a gate does not rewrite
    history, and evaluating one must not move the tree at all."""

    def test_evaluation_runs_no_git_command(self, tmp_path, monkeypatch):
        import subprocess

        called: list = []
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a) or None)
        cfg = _cfg(tmp_path)
        registry = GateRegistry()
        registry.register(
            "review", "review", lambda ctx: GateResult(GateStatus.SATISFIED, PhaseOutcome.PASS)
        )
        with ExecutorState(cfg) as state:
            evaluate_gates("review", _ctx(state, cfg), registry=registry)
        assert called == []


@pytest.mark.parametrize(
    "statuses,expected",
    [
        ([GateStatus.SATISFIED], GateStatus.SATISFIED),
        ([GateStatus.SATISFIED, GateStatus.UNSATISFIED], GateStatus.UNSATISFIED),
        ([GateStatus.SATISFIED, GateStatus.INSTRUMENT_ERROR], GateStatus.INSTRUMENT_ERROR),
        # A concrete "no" outranks "could not tell": there is something to act on.
        ([GateStatus.UNSATISFIED, GateStatus.INSTRUMENT_ERROR], GateStatus.UNSATISFIED),
    ],
)
def test_aggregate_precedence(tmp_path, statuses, expected):
    cfg = _cfg(tmp_path, gate_recovery_attempts=0)
    registry = GateRegistry()
    for i, st in enumerate(statuses):
        registry.register(f"g{i}", "review", lambda ctx, st=st: GateResult(st, PhaseOutcome.PASS))
    with ExecutorState(cfg) as state:
        assert evaluate_gates("review", _ctx(state, cfg), registry=registry).status is expected


class TestTheLifecycleSite:
    """Criteria 1 and 2: the checkpoint is made, the merge waits on the gate."""

    def test_the_dormant_path_resolves_no_sha_and_opens_no_state(self, tmp_path, monkeypatch):
        """Criterion 8 has to be mechanical, not a promise: with nothing
        registered the site must not even ask git what HEAD is."""
        from spec_runner import hooks

        monkeypatch.setattr(hooks, "REGISTRY", GateRegistry(), raising=False)
        called: list = []
        monkeypatch.setattr(hooks.subprocess, "run", lambda *a, **k: called.append(a[0]) or None)
        assert hooks.has_gates(GateRegistry()) is False
        assert called == []

    def test_an_unsatisfied_gate_blocks_before_the_merge(self, tmp_path, monkeypatch):
        from spec_runner import gates as gates_mod
        from spec_runner import hooks

        registry = GateRegistry()
        registry.register(
            "review",
            "review",
            lambda ctx: GateResult(
                GateStatus.UNSATISFIED, PhaseOutcome.UNEXPECTED_FAIL, "2 findings"
            ),
        )
        monkeypatch.setattr(gates_mod, "REGISTRY", registry)

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(
            hooks.subprocess,
            "run",
            lambda *a, **k: _CompletedStub(0, "abc1234\n"),
        )
        reason = hooks._run_pre_terminal_gates(_task(), cfg)
        assert reason is not None and "2 findings" in reason

    def test_a_satisfied_gate_lets_the_merge_through(self, tmp_path, monkeypatch):
        from spec_runner import gates as gates_mod
        from spec_runner import hooks

        registry = GateRegistry()
        registry.register(
            "review", "review", lambda ctx: GateResult(GateStatus.SATISFIED, PhaseOutcome.PASS)
        )
        monkeypatch.setattr(gates_mod, "REGISTRY", registry)

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(hooks.subprocess, "run", lambda *a, **k: _CompletedStub(0, "abc1234\n"))
        assert hooks._run_pre_terminal_gates(_task(), cfg) is None

    def test_no_commit_to_judge_does_not_block_bootstrap(self, tmp_path, monkeypatch):
        """A fresh repo has no checkpoint. Refusing there would block bootstrap
        over bookkeeping."""
        from spec_runner import gates as gates_mod
        from spec_runner import hooks

        registry = GateRegistry()
        registry.register(
            "review",
            "review",
            lambda ctx: GateResult(GateStatus.UNSATISFIED, PhaseOutcome.UNEXPECTED_FAIL),
        )
        monkeypatch.setattr(gates_mod, "REGISTRY", registry)

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(hooks.subprocess, "run", lambda *a, **k: _CompletedStub(128, ""))
        assert hooks._run_pre_terminal_gates(_task(), cfg) is None


class _CompletedStub:
    def __init__(self, returncode: int, stdout: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


class TestEvaluatePreTerminalSpansPhases:
    def test_every_registered_phase_is_evaluated(self, tmp_path):
        from spec_runner.gates import evaluate_pre_terminal

        cfg = _cfg(tmp_path)
        registry = GateRegistry()
        registry.register(
            "review", "review", lambda ctx: GateResult(GateStatus.SATISFIED, PhaseOutcome.PASS)
        )
        registry.register(
            "tdd.red",
            "tests",
            lambda ctx: GateResult(GateStatus.UNSATISFIED, PhaseOutcome.UNEXPECTED_FAIL),
        )
        with ExecutorState(cfg) as state:
            outcome = evaluate_pre_terminal(_ctx(state, cfg), registry=registry)

        assert {r.gate_id for r in outcome.results} == {"review", "tdd.red"}
        assert outcome.status is GateStatus.UNSATISFIED
