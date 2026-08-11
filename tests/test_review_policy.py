"""#157: the review policy as the first pre-terminal gate consumer.

The policy the owner settled, as executable claims:

    advisory (default) — nothing changes; review stays a report
    required          — `failed` blocks, `not_run` blocks ("I don't know" is
                        not "fine"), `error` is an instrument error, `skipped`
                        is a contradiction caught before the run starts

Design: `docs/superpowers/specs/2026-08-11-review-policy-design.md`
Mechanism: #164 (`spec_runner.gates`), which this does not own.
"""

from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.gates import (
    POLICY_KEYS,
    GateContext,
    GateRegistry,
    GateStatus,
    evaluate_gates,
    register_builtin_gates,
)
from spec_runner.state import ExecutorState, PhaseOutcome, ReviewVerdict


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


def _ctx(state, cfg, **facts) -> GateContext:
    return GateContext(
        task_id="TASK-001",
        checkpoint_sha="merge0candidate",
        config=cfg,
        state=state,
        facts=facts,
    )


def _run(tmp_path, verdict, *, policy="required", **facts):
    cfg = _cfg(tmp_path, review_policy=policy)
    registry = GateRegistry()
    register_builtin_gates(cfg, registry=registry)
    with ExecutorState(cfg) as state:
        ctx = _ctx(state, cfg, review_verdict=verdict, **facts)
        return evaluate_gates("review", ctx, registry=registry)


class TestAdvisoryIsTheDefaultAndChangesNothing:
    def test_the_default_is_advisory(self, tmp_path):
        assert _cfg(tmp_path).review_policy == "advisory"

    def test_advisory_registers_no_gate(self, tmp_path):
        """Not "registers a gate that always passes": that would resolve a SHA
        and open the state DB on every task, and #164 criterion 8 is supposed
        to be mechanical."""
        registry = GateRegistry()
        register_builtin_gates(_cfg(tmp_path, review_policy="advisory"), registry=registry)
        assert registry.phases() == []

    @pytest.mark.parametrize(
        "verdict",
        [ReviewVerdict.FAILED, ReviewVerdict.NOT_RUN, ReviewVerdict.ERROR, ReviewVerdict.SKIPPED],
    )
    def test_advisory_blocks_on_nothing(self, tmp_path, verdict):
        assert _run(tmp_path, verdict, policy="advisory").status is GateStatus.SATISFIED


class TestTheVerdictTable:
    """One test per row of the owner's table."""

    @pytest.mark.parametrize("verdict", [ReviewVerdict.PASSED, ReviewVerdict.FIXED])
    def test_passed_and_fixed_proceed(self, tmp_path, verdict):
        """`fixed` is a kind of pass, per slice 0's reading — not a peer of
        `passed` and not a failure."""
        assert _run(tmp_path, verdict).status is GateStatus.SATISFIED

    def test_failed_blocks(self, tmp_path):
        assert _run(tmp_path, ReviewVerdict.FAILED).status is GateStatus.UNSATISFIED

    def test_rejected_blocks(self, tmp_path):
        assert _run(tmp_path, ReviewVerdict.REJECTED).status is GateStatus.UNSATISFIED

    def test_not_run_blocks(self, tmp_path):
        """The #138 defect one level up: the review did not happen, and
        "I don't know" is not "fine"."""
        outcome = _run(tmp_path, ReviewVerdict.NOT_RUN)
        assert outcome.status is GateStatus.UNSATISFIED
        assert outcome.results[0].outcome is PhaseOutcome.NOT_RUN

    def test_error_is_an_instrument_error_not_a_block(self, tmp_path):
        """The instrument broke. That is not a defect in the work, and the
        mechanism — not this gate — decides what an exhausted error becomes."""
        outcome = _run(tmp_path, ReviewVerdict.ERROR)
        assert outcome.status is GateStatus.INSTRUMENT_ERROR
        assert outcome.results[0].outcome is PhaseOutcome.ERROR

    def test_skipped_blocks_and_says_why(self, tmp_path):
        """Reaching the gate at all means `validate` was bypassed. Blocking is
        the fail-closed answer; a `required` policy that lets `skipped` through
        is decorative."""
        outcome = _run(tmp_path, ReviewVerdict.SKIPPED)
        assert outcome.status is GateStatus.UNSATISFIED
        assert "run_review" in (outcome.results[0].detail or "")


class TestTheGateDoesNotReadItsOwnBookkeeping:
    """§2.2: `record_phase` is best-effort, so reading a blocking decision out
    of it would make a storage failure indistinguishable from `not_run` — one
    is our bug, the other is a fact about the code."""

    def test_a_missing_verdict_is_an_instrument_error_not_not_run(self, tmp_path):
        cfg = _cfg(tmp_path, review_policy="required")
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)
        with ExecutorState(cfg) as state:
            outcome = evaluate_gates("review", _ctx(state, cfg), registry=registry)

        assert outcome.status is GateStatus.INSTRUMENT_ERROR, (
            "the site failing to report must not be laundered into a verdict about the code"
        )
        assert outcome.results[0].outcome is PhaseOutcome.ERROR

    def test_the_gate_never_reads_phase_history(self, tmp_path, monkeypatch):
        cfg = _cfg(tmp_path, review_policy="required")
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)
        with ExecutorState(cfg) as state:
            monkeypatch.setattr(
                type(state),
                "phase_history",
                lambda *a, **k: pytest.fail("the gate read phase_results"),
            )
            evaluate_gates(
                "review",
                _ctx(state, cfg, review_verdict=ReviewVerdict.PASSED),
                registry=registry,
            )


class TestTheEvidenceNamesBothTrees:
    """§2.1: the gate judges the merge candidate; review judged the review
    checkpoint. Recording only one of them would claim review approved a tree
    it never saw."""

    def test_the_detail_carries_the_review_checkpoint(self, tmp_path):
        outcome = _run(
            tmp_path,
            ReviewVerdict.FIXED,
            review_checkpoint_sha="reviewed0tree",
        )
        assert "reviewed0tree" in (outcome.results[0].detail or "")

    def test_the_verdict_is_stored_against_the_merge_candidate(self, tmp_path):
        cfg = _cfg(tmp_path, review_policy="required")
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)
        with ExecutorState(cfg) as state:
            ctx = _ctx(state, cfg, review_verdict=ReviewVerdict.PASSED)
            evaluate_gates("review", ctx, registry=registry)
            stored = state.gate_verdict("TASK-001", "review", "merge0candidate", ctx.config_hash)

        assert stored is not None, "staleness is judged on the merge candidate, not on Y"

    def test_a_review_of_an_older_tree_still_goes_stale_on_a_new_candidate(self, tmp_path):
        cfg = _cfg(tmp_path, review_policy="required")
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)
        with ExecutorState(cfg) as state:
            ctx = _ctx(state, cfg, review_verdict=ReviewVerdict.PASSED)
            evaluate_gates("review", ctx, registry=registry)
            assert state.gate_verdict("TASK-001", "review", "another0tree", ctx.config_hash) is None


class TestThePolicyIsPartOfTheVerdictIdentity:
    def test_review_policy_is_a_policy_key(self):
        assert "review_policy" in POLICY_KEYS

    def test_flipping_the_policy_invalidates_an_earlier_verdict(self, tmp_path):
        advisory = _ctx(None, _cfg(tmp_path, review_policy="advisory"))
        required = _ctx(None, _cfg(tmp_path, review_policy="required"))
        assert advisory.config_hash != required.config_hash


class TestRegistrationIsIdempotent:
    """`watch` runs many tasks through one process; registering per run must
    not stack duplicate gates."""

    def test_registering_twice_leaves_one_gate(self, tmp_path):
        cfg = _cfg(tmp_path, review_policy="required")
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)
        register_builtin_gates(cfg, registry=registry)
        assert len(registry.for_phase("review")) == 1

    def test_switching_to_advisory_unregisters(self, tmp_path):
        registry = GateRegistry()
        register_builtin_gates(_cfg(tmp_path, review_policy="required"), registry=registry)
        register_builtin_gates(_cfg(tmp_path, review_policy="advisory"), registry=registry)
        assert registry.phases() == [], "a stale gate would outlive the policy that asked for it"


class TestTheContradictionIsCaughtBeforeTheRun:
    """§3: `required` + review switched off can only ever block. The honest
    moment to say so is before the work, not at the merge gate after it."""

    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "spec-runner.config.yaml"
        path.write_text(body)
        return path

    def test_required_with_run_review_false_is_a_config_error(self, tmp_path):
        from spec_runner.validate import validate_config

        cfg = self._write(
            tmp_path,
            "review_policy: required\nhooks:\n  post_done:\n    run_review: false\n",
        )
        result = validate_config(cfg)
        assert not result.ok
        assert any("run_review" in e for e in result.errors)

    def test_required_with_review_on_is_fine(self, tmp_path):
        from spec_runner.validate import validate_config

        cfg = self._write(
            tmp_path, "review_policy: required\nhooks:\n  post_done:\n    run_review: true\n"
        )
        assert validate_config(cfg).ok

    def test_an_unknown_policy_value_is_a_config_error(self, tmp_path):
        from spec_runner.validate import validate_config

        cfg = self._write(tmp_path, "review_policy: mandatory\n")
        result = validate_config(cfg)
        assert not result.ok
        assert any("advisory" in e and "required" in e for e in result.errors)

    def test_advisory_with_review_off_is_fine(self, tmp_path):
        """Only `required` makes the combination contradictory."""
        from spec_runner.validate import validate_config

        cfg = self._write(
            tmp_path,
            "review_policy: advisory\nhooks:\n  post_done:\n    run_review: false\n",
        )
        assert validate_config(cfg).ok


class TestTheLifecycleActuallyBlocks:
    """End to end through the real call site, not just the evaluator."""

    def _prepare(self, tmp_path, monkeypatch, verdict, policy="required"):
        from spec_runner import gates as gates_mod
        from spec_runner import hooks

        registry = GateRegistry()
        cfg = _cfg(tmp_path, review_policy=policy)
        register_builtin_gates(cfg, registry=registry)
        monkeypatch.setattr(gates_mod, "REGISTRY", registry)

        class _Head:
            returncode = 0
            stdout = "merge0candidate\n"
            stderr = ""

        monkeypatch.setattr(hooks.subprocess, "run", lambda *a, **k: _Head())
        from spec_runner.task import Task

        task = Task(id="TASK-001", name="t", priority="p1", status="in_progress", estimate="1h")
        return hooks, cfg, task

    def test_a_failed_review_stops_short_of_the_merge(self, tmp_path, monkeypatch):
        hooks, cfg, task = self._prepare(tmp_path, monkeypatch, ReviewVerdict.FAILED)
        reason = hooks._run_pre_terminal_gates(
            task, cfg, facts={"review_verdict": ReviewVerdict.FAILED.value}
        )
        assert reason is not None and "unsatisfied" in reason.lower()

    def test_a_passed_review_lets_the_merge_through(self, tmp_path, monkeypatch):
        hooks, cfg, task = self._prepare(tmp_path, monkeypatch, ReviewVerdict.PASSED)
        assert (
            hooks._run_pre_terminal_gates(
                task, cfg, facts={"review_verdict": ReviewVerdict.PASSED.value}
            )
            is None
        )

    def test_a_review_error_reads_as_infrastructure_not_as_bad_code(self, tmp_path, monkeypatch):
        hooks, cfg, task = self._prepare(tmp_path, monkeypatch, ReviewVerdict.ERROR)
        reason = hooks._run_pre_terminal_gates(
            task, cfg, facts={"review_verdict": ReviewVerdict.ERROR.value}
        )
        assert reason is not None and "infrastructure error" in reason

    def test_advisory_leaves_the_site_dormant(self, tmp_path, monkeypatch):
        """#134 item 4's shape must stay possible only under advisory: the
        default is unchanged, which is the point of a default."""
        from spec_runner import gates as gates_mod

        registry = GateRegistry()
        register_builtin_gates(_cfg(tmp_path, review_policy="advisory"), registry=registry)
        monkeypatch.setattr(gates_mod, "REGISTRY", registry)
        assert gates_mod.has_gates() is False
