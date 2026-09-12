"""BEH-22, BEH-26 (TASK-009, DT-09).

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-22
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-26
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/30-decomposition.md#DT-09
Traces: FR-15, FR-19

TDD-waiver (class: characterisation, sanction: batch-approve-2026-09-09).
All five conditions of the class, confirmed:

- the behaviour is already delivered by this task's own dependency (TASK-006/
  DT-06, which put a file target's composition into durable evidence) and by
  the two mechanisms design doc 20-design.md#Q-02 names as already covering
  drift more strictly than a new axis could: `reusable_verify_evidence`'s
  full-tree byte-identity check (`live_verify.py`) and the pre-merge
  re-verify pair `_reverify_before_review`/`_reverify_live_evidence_for_
  candidate` (`hooks.py`) that replays the declared group fresh against the
  candidate rather than trusting a prior row. None of `gates.py`, `hooks.py`
  or `live_verify.py` is modified by this task;
- this task adds the missing characterisation coverage: that the two
  mechanisms above hold for a **file target** exactly as they do for a
  node-id group (BEH-22), and that the pre-merge gate's verdict classes are
  the same for a file target as for a node-id group on the same five inputs
  (BEH-26);
- an honest baseline RED is not possible: the tree-hash axis, the re-verify
  ordering and `_verify_first_gate`'s verdict table already exist and already
  pass for a file target the same way they do for a node-id group — writing
  this file against unmodified `main` cannot fail without first reverting
  delivered code, which is not this task's job;
- every claim below carries a negative control that flips the observed
  result under a deliberately violated property, proving the assertion
  actually discriminates rather than passing vacuously. Per claim:
  - reuse-before-run (BEH-22, first half) — `live_verify._tree_hash` is
    monkeypatched to a constant, simulating the byte-identity axis removed,
    and a tree that gained a member is then (wrongly) reused;
  - pre-merge gate (BEH-22, second half) — the isolated gate is evaluated
    directly against stale evidence, *skipping* the re-verify call the real
    pipeline always makes first, and is shown to (wrongly) accept a
    candidate whose declared file gained a failing member;
  - parity (BEH-26) — `gates._verify_first_gate` is monkeypatched to
    special-case any evidence carrying a `composition` (i.e. a file target)
    as always `SATISFIED`, and the file-target/node-id parity assertion is
    shown to catch the resulting divergence;
- baseline commit at the start of this task: `54d9acf44194e0fe42e021d8deb16637efba6ceb`.

Negative control for the one condition with no machine gate (a real
mutation-kill on "the new test discriminates") is, per the class, left to
task review — not asserted by any test here.

`tests/test_verify_gates.py` is not this task's file: it carries
WS-spec-runner-367's own BEH-24/25/28/29 for a node-id group, and neither
BEH-22 nor BEH-26 of this bundle is a line in it. This file owns the file-
target case beside it, and reuses that file's own fixture/helper shapes.

kind: integration — real git commits, a real pytest subprocess replay
(`run_live_verify`), and the real gate registry (`gates.py`) evaluated
through `hooks.py`'s own call sites, never a mocked verdict.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import spec_runner.gates as gates_mod
import spec_runner.live_verify as live_verify_module
from spec_runner import hooks
from spec_runner.config import ExecutorConfig
from spec_runner.gates import (
    GateContext,
    GateRegistry,
    GateStatus,
    evaluate_gates,
    register_builtin_gates,
)
from spec_runner.live_verify import (
    VerifyOutcome,
    VerifyRunResult,
    reusable_verify_evidence,
    run_live_verify,
)
from spec_runner.phases import RefusalKind
from spec_runner.state import ExecutorState
from spec_runner.task import Task


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests").mkdir()
    (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


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


def _file_task(**overrides) -> Task:
    """A verify-first task whose declared group is a **file target**."""
    defaults: dict = {
        "id": "TASK-FILE",
        "name": "verify-first file target",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
        "execution_mode": "verify_first",
        "verifies": ["tests/test_group.py"],
    }
    defaults.update(overrides)
    return Task(**defaults)


def _node_task(**overrides) -> Task:
    """The node-id-group sibling, used only for the BEH-26 parity checks."""
    defaults: dict = {
        "id": "TASK-NODE",
        "name": "verify-first node id group",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
        "execution_mode": "verify_first",
        "verifies": ["tests/test_group.py::test_it"],
    }
    defaults.update(overrides)
    return Task(**defaults)


def _ctx(state, cfg, sha, *, task_id: str) -> GateContext:
    return GateContext(
        task_id=task_id,
        checkpoint_sha=sha,
        config=cfg,
        state=state,
        facts={"execution_mode": "verify_first", "waiver_applied": False},
    )


class TestBEH22ReuseBeforeRunDoesNotInheritChangedComposition:
    """kind: integration — BEH-22, first half: a candidate declared file whose
    byte string is unchanged but whose *tree* gained a member must not let a
    prior green row be reused before the live run — the declared string being
    byte-identical is explicitly named insufficient (design Q-02, BEH-22)."""

    def test_reuse_refuses_after_a_member_is_added_even_though_the_line_is_unchanged(
        self, tmp_path
    ):
        root = _repo(tmp_path)
        task = _file_task()
        cfg = _cfg(root, execution_mode="verify_first")

        result = run_live_verify(task, cfg)
        assert result.passed, result.detail
        with ExecutorState(cfg) as state:
            state.record_verify_evidence(task=task, config=cfg, result=result)
            reused = reusable_verify_evidence(cfg, state, task)
        assert reused is not None, "sanity: an unmoved tree must still be reusable"

        # The declared line ("tests/test_group.py") is untouched — only the
        # file's own contents grow a second, passing member.
        (root / "tests" / "test_group.py").write_text(
            "def test_it():\n    assert True\n\n\ndef test_added():\n    assert True\n"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "add a member")
        assert task.verifies == ["tests/test_group.py"], "declared string is untouched"

        with ExecutorState(cfg) as state:
            reused = reusable_verify_evidence(cfg, state, task)

        assert reused is None, (
            "a tree in which the declared file's composition changed must not "
            "be answered from a prior row, even though the declared string "
            "stayed byte-identical"
        )

    def test_reuse_still_holds_on_the_reverse_input_an_unmoved_tree(self, tmp_path):
        """The other half of the same criterion (design Q-02's "осознанная
        цена"): a tree where nothing changed keeps today's consistency and
        does not become stricter — "always refuse" would satisfy the first
        half and break this one."""
        root = _repo(tmp_path)
        task = _file_task()
        cfg = _cfg(root, execution_mode="verify_first")

        result = run_live_verify(task, cfg)
        assert result.passed, result.detail
        with ExecutorState(cfg) as state:
            state.record_verify_evidence(task=task, config=cfg, result=result)
            reused = reusable_verify_evidence(cfg, state, task)

        assert reused is not None
        assert reused.commit_sha == result.sha

    def test_negative_control_without_the_tree_hash_axis_stale_evidence_would_be_reused(
        self, tmp_path, monkeypatch
    ):
        """Mutation-kill: simulate the violated property — the byte-identity
        axis `reusable_verify_evidence` relies on is disabled — and confirm a
        tree that gained a member is then (wrongly) treated as reusable,
        proving the refusal above is not vacuous."""
        root = _repo(tmp_path)
        task = _file_task()
        cfg = _cfg(root, execution_mode="verify_first")

        result = run_live_verify(task, cfg)
        assert result.passed, result.detail
        with ExecutorState(cfg) as state:
            state.record_verify_evidence(task=task, config=cfg, result=result)

        (root / "tests" / "test_group.py").write_text(
            "def test_it():\n    assert True\n\n\ndef test_added():\n    assert True\n"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "add a member")

        # Regressed: every tree hashes the same, so byte-identity can never
        # fail the reuse decision.
        monkeypatch.setattr(live_verify_module, "_tree_hash", lambda config, sha: "constant")

        with ExecutorState(cfg) as state:
            reused = reusable_verify_evidence(cfg, state, task)

        assert reused is not None, (
            "negative control: with the tree-hash axis disabled, the changed "
            "tree is wrongly reused — proving the real refusal above actually "
            "depends on that axis"
        )


class TestBEH22PreMergeGateDoesNotInheritChangedComposition:
    """kind: integration — BEH-22, second half: the pre-merge path
    (`_reverify_live_evidence_for_candidate`) replays the declared file
    fresh against the candidate rather than trusting a stale green row, so a
    member added after the entry run is judged for what it is now, not
    silently inherited."""

    def test_reverify_finds_a_newly_added_failing_member_and_refuses(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        task = _file_task()
        cfg = _cfg(root, execution_mode="verify_first", auto_commit=True, run_review=False)
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)
        monkeypatch.setattr(gates_mod, "REGISTRY", registry)

        entry = run_live_verify(task, cfg)
        assert entry.passed, entry.detail
        with ExecutorState(cfg) as state:
            assert state.record_verify_evidence(task=task, config=cfg, result=entry)

        # The declared file gains a member that fails — after the stale
        # green row was already recorded for the base commit.
        (root / "tests" / "test_group.py").write_text(
            "def test_it():\n    assert True\n\n\n"
            "def test_added():\n    assert False, 'not implemented'\n"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "implementation adds a failing member")

        blocked = hooks._reverify_live_evidence_for_candidate(task, cfg, None, "")

        assert blocked is not None, (
            "a candidate whose declared file gained a failing member must not "
            "merge on the strength of a stale green row"
        )
        assert blocked.kind is RefusalKind.POLICY

        from spec_runner.tdd import resolve_namespace

        with ExecutorState(cfg) as state:
            fresh = state.verify_evidence(resolve_namespace(cfg), task.id)
        assert fresh is not None
        assert fresh.outcome == VerifyOutcome.TEST_FAILURE.value, (
            "the recorded evidence must reflect the fresh replay's own "
            "verdict, not the stale entry-run green"
        )

    def test_reverify_confirms_green_again_when_the_added_member_also_passes(
        self, tmp_path, monkeypatch
    ):
        """The reverse input for the gate axis: composition legitimately
        grew but the group is still green — the guarantee is "re-ask the
        question", not "always refuse a grown composition"."""
        root = _repo(tmp_path)
        task = _file_task()
        cfg = _cfg(root, execution_mode="verify_first", auto_commit=True, run_review=False)
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)
        monkeypatch.setattr(gates_mod, "REGISTRY", registry)

        entry = run_live_verify(task, cfg)
        assert entry.passed, entry.detail
        with ExecutorState(cfg) as state:
            assert state.record_verify_evidence(task=task, config=cfg, result=entry)
        assert len(entry.composition) == 1

        (root / "tests" / "test_group.py").write_text(
            "def test_it():\n    assert True\n\n\ndef test_added():\n    assert True\n"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "implementation adds a passing member")

        blocked = hooks._reverify_live_evidence_for_candidate(task, cfg, None, "")

        assert blocked is None
        from spec_runner.tdd import resolve_namespace

        with ExecutorState(cfg) as state:
            fresh = state.verify_evidence(resolve_namespace(cfg), task.id)
        assert fresh is not None
        assert fresh.outcome == VerifyOutcome.GREEN.value
        assert len(fresh.composition) == 2, (
            "the fresh row must name the recomputed composition, not repeat "
            "the entry run's one-member snapshot"
        )

    def test_negative_control_without_the_reverify_the_naked_gate_would_wrongly_accept(
        self, tmp_path
    ):
        """Mutation-kill for the gate axis: skip the re-verify call the real
        pipeline always makes first (`_reverify_live_evidence_for_candidate`)
        and ask `_verify_first_gate` directly about the stale green row —
        `evidence.outcome == GREEN` and the candidate descends from it, so
        the isolated gate is satisfied even though the declared file now
        contains a failing member. This is exactly the silent inheritance
        BEH-22 forbids, and it is only prevented because the real pipeline
        never evaluates the gate without re-verifying first — proving that
        ordering, not the gate alone, is what BEH-22 depends on."""
        root = _repo(tmp_path)
        task = _file_task()
        cfg = _cfg(root, execution_mode="verify_first", auto_commit=True, run_review=False)
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)

        entry = run_live_verify(task, cfg)
        assert entry.passed, entry.detail
        with ExecutorState(cfg) as state:
            assert state.record_verify_evidence(task=task, config=cfg, result=entry)

        (root / "tests" / "test_group.py").write_text(
            "def test_it():\n    assert True\n\n\n"
            "def test_added():\n    assert False, 'not implemented'\n"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "implementation adds a failing member")
        candidate = _head(root)

        with ExecutorState(cfg) as state:
            outcome = evaluate_gates(
                "tests", _ctx(state, cfg, candidate, task_id=task.id), registry=registry
            )

        assert outcome.status is GateStatus.SATISFIED, (
            "negative control: bypassing the re-verify step and asking the "
            "isolated gate about stale evidence wrongly accepts a candidate "
            "whose declared file now fails — proving the real pipeline's "
            "re-verify-before-gate ordering is what BEH-22 actually rests on"
        )


class TestBEH26PreMergeGateAsksTheSameQuestionForAFileTarget:
    """kind: integration — BEH-26: the pre-merge gate's verdict class for a
    file target matches the class for a node-id group, on each of the five
    named inputs, evaluated side by side against the same fixture."""

    def _run_axis(self, tmp_path, build) -> tuple[GateStatus, GateStatus]:
        """Run one axis for both a file-target task and a node-id task
        against the same repo, returning (file_status, node_status)."""
        root = _repo(tmp_path)
        file_task = _file_task()
        node_task = _node_task()
        cfg = _cfg(root, execution_mode="verify_first")
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)

        candidate = build(root, cfg, file_task, node_task)

        with ExecutorState(cfg) as state:
            file_outcome = evaluate_gates(
                "tests", _ctx(state, cfg, candidate, task_id=file_task.id), registry=registry
            )
        with ExecutorState(cfg) as state:
            node_outcome = evaluate_gates(
                "tests", _ctx(state, cfg, candidate, task_id=node_task.id), registry=registry
            )
        return file_outcome.status, node_outcome.status

    def test_no_evidence_reads_the_same(self, tmp_path):
        def build(root, cfg, file_task, node_task):
            return _head(root)

        file_status, node_status = self._run_axis(tmp_path, build)

        assert file_status is node_status is GateStatus.UNSATISFIED

    def test_evidence_not_green_reads_the_same(self, tmp_path):
        def build(root, cfg, file_task, node_task):
            (root / "tests" / "test_group.py").write_text(
                "def test_it():\n    assert False, 'deliberate failure'\n"
            )
            _git(root, "add", "-A")
            _git(root, "commit", "-qm", "make it fail")
            for task in (file_task, node_task):
                result = run_live_verify(task, cfg)
                assert result.ran and not result.passed
                with ExecutorState(cfg) as state:
                    state.record_verify_evidence(task=task, config=cfg, result=result)
            return _head(root)

        file_status, node_status = self._run_axis(tmp_path, build)

        assert file_status is node_status is GateStatus.UNSATISFIED

    def test_evidence_for_a_different_tree_reads_the_same(self, tmp_path):
        def build(root, cfg, file_task, node_task):
            base = _head(root)
            _git(root, "checkout", "-q", "--detach")
            (root / "other.py").write_text("y = 2\n")
            _git(root, "add", "-A")
            _git(root, "commit", "-qm", "unrelated")
            for task in (file_task, node_task):
                result = run_live_verify(task, cfg)
                assert result.passed, result.detail
                with ExecutorState(cfg) as state:
                    state.record_verify_evidence(task=task, config=cfg, result=result)

            _git(root, "checkout", "-q", base)
            (root / "sibling.py").write_text("z = 3\n")
            _git(root, "add", "-A")
            _git(root, "commit", "-qm", "sibling")
            return _head(root)

        file_status, node_status = self._run_axis(tmp_path, build)

        assert file_status is node_status is GateStatus.UNSATISFIED

    def test_green_evidence_for_the_judged_tree_reads_the_same(self, tmp_path):
        def build(root, cfg, file_task, node_task):
            for task in (file_task, node_task):
                result = run_live_verify(task, cfg)
                assert result.passed, result.detail
                with ExecutorState(cfg) as state:
                    state.record_verify_evidence(task=task, config=cfg, result=result)
            return _head(root)

        file_status, node_status = self._run_axis(tmp_path, build)

        assert file_status is node_status is GateStatus.SATISFIED

    def test_instrument_error_reads_the_same(self, tmp_path):
        def build(root, cfg, file_task, node_task):
            head = _head(root)
            for task in (file_task, node_task):
                broken = VerifyRunResult(
                    sha=head,
                    ran=False,
                    passed=False,
                    detail="tests/test_group.py selected nothing (exit 4)",
                    adapter="pytest",
                )
                with ExecutorState(cfg) as state:
                    state.record_verify_evidence(task=task, config=cfg, result=broken)
            return head

        file_status, node_status = self._run_axis(tmp_path, build)

        assert file_status is node_status is GateStatus.INSTRUMENT_ERROR

    def test_a_negative_gate_refuses_to_merge_and_instrument_error_stays_instrumental(
        self, tmp_path, monkeypatch
    ):
        """BEH-26's own And-clauses: a file-target task with a negative gate
        does not merge, and an instrument error is reported as an
        infrastructure refusal (`RefusalKind.INSTRUMENT`), never as though
        the work itself were judged and rejected (`RefusalKind.POLICY`) —
        through the real pre-terminal call site, `hooks._run_pre_terminal_
        gates`."""
        root = _repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_it():\n    assert False, 'deliberate failure'\n"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "make it fail")
        task = _file_task()
        cfg = _cfg(root, execution_mode="verify_first", auto_commit=True)
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)
        monkeypatch.setattr(gates_mod, "REGISTRY", registry)

        result = run_live_verify(task, cfg)
        assert result.ran and not result.passed
        with ExecutorState(cfg) as state:
            state.record_verify_evidence(task=task, config=cfg, result=result)

        blocked = hooks._run_pre_terminal_gates(
            task,
            cfg,
            candidate_sha=_head(root),
            facts={"execution_mode": "verify_first", "waiver_applied": False},
        )
        assert blocked is not None
        assert blocked.kind is RefusalKind.POLICY

        broken = VerifyRunResult(
            sha=_head(root),
            ran=False,
            passed=False,
            detail="tests/test_group.py selected nothing (exit 4)",
            adapter="pytest",
        )
        with ExecutorState(cfg) as state:
            state.record_verify_evidence(task=task, config=cfg, result=broken)

        blocked_instrument = hooks._run_pre_terminal_gates(
            task,
            cfg,
            candidate_sha=_head(root),
            facts={"execution_mode": "verify_first", "waiver_applied": False},
        )
        assert blocked_instrument is not None
        assert blocked_instrument.kind is RefusalKind.INSTRUMENT, (
            "an instrument error must not be reported as a policy "
            "refusal — the work was never judged"
        )

    def test_negative_control_a_regressed_gate_that_special_cases_file_targets_is_caught(
        self, tmp_path, monkeypatch
    ):
        """Mutation-kill for parity: regress `_verify_first_gate` to treat
        any evidence carrying a `composition` (i.e. a file target's own
        GREEN row — design Q-04: a node-id-only group never writes one) as
        always `SATISFIED`, regardless of the candidate — a plausible defect
        class introducing a *new* axis keyed on target kind, which BEH-26
        forbids — and confirm the file-target/node-id parity check would
        have caught it. Built on the same "different tree" fixture as
        `test_evidence_for_a_different_tree_reads_the_same` above, so the
        true verdict for both kinds is UNSATISFIED and only the regression
        diverges."""
        root = _repo(tmp_path)
        file_task = _file_task()
        node_task = _node_task()
        cfg = _cfg(root, execution_mode="verify_first")
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)

        base = _head(root)
        _git(root, "checkout", "-q", "--detach")
        (root / "other.py").write_text("y = 2\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "unrelated")
        for task in (file_task, node_task):
            result = run_live_verify(task, cfg)
            assert result.passed, result.detail
            with ExecutorState(cfg) as state:
                state.record_verify_evidence(task=task, config=cfg, result=result)
        with ExecutorState(cfg) as state:
            from spec_runner.tdd import resolve_namespace

            file_evidence = state.verify_evidence(resolve_namespace(cfg), file_task.id)
            node_evidence = state.verify_evidence(resolve_namespace(cfg), node_task.id)
        assert file_evidence is not None and file_evidence.composition, (
            "sanity: a green file-target row carries a composition (design Q-04)"
        )
        assert node_evidence is not None and not node_evidence.composition, (
            "sanity: a node-id-only group never writes one"
        )

        _git(root, "checkout", "-q", base)
        (root / "sibling.py").write_text("z = 3\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "sibling")
        candidate = _head(root)

        original_gate = gates_mod._verify_first_gate

        def regressed_verify_first_gate(ctx):
            from spec_runner.gates import GateResult
            from spec_runner.phases import PhaseOutcome
            from spec_runner.tdd import resolve_namespace as _resolve_namespace

            evidence = ctx.state.verify_evidence(_resolve_namespace(ctx.config), ctx.task_id)
            if evidence is not None and evidence.composition:
                return GateResult(GateStatus.SATISFIED, PhaseOutcome.PASS, "file target: skipped")
            return original_gate(ctx)

        monkeypatch.setattr(gates_mod, "_verify_first_gate", regressed_verify_first_gate)

        with ExecutorState(cfg) as state:
            file_outcome = evaluate_gates(
                "tests", _ctx(state, cfg, candidate, task_id=file_task.id), registry=registry
            )
        with ExecutorState(cfg) as state:
            node_outcome = evaluate_gates(
                "tests", _ctx(state, cfg, candidate, task_id=node_task.id), registry=registry
            )

        assert node_outcome.status is GateStatus.UNSATISFIED, (
            "sanity: the node-id sibling is correctly refused — the regression never touches it"
        )
        assert file_outcome.status is GateStatus.SATISFIED, (
            "the regression wrongly accepts the file target's sibling tree"
        )
        assert file_outcome.status is not node_outcome.status, (
            "negative control: a gate regressed to special-case file targets "
            "diverges from the node-id verdict on the same 'different tree' "
            "input — proving the parity assertions above would have caught "
            "exactly this class of defect"
        )
