"""BEH-24 (#367 milestone 1, TASK-015): gate registration and the mode-read
audit, before green-only branching exists.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-24
Traces: FR-17

kind: contract — the gate mechanism (`gates.py`) evaluated directly, plus one
reachability check through `execute_task`'s real verify-first path. TASK-009
adds `kind: integration` scenarios to this same file for BEH-25/28/29.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.gates import (
    GateContext,
    GateRegistry,
    GateStatus,
    evaluate_claims,
    evaluate_gates,
    has_gates,
    is_registered,
    register_builtin_gates,
)
from spec_runner.live_verify import run_live_verify
from spec_runner.runner import CliInvocation
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


def _task(**overrides) -> Task:
    defaults: dict = {
        "id": "TASK-101",
        "name": "verify-first task",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
        "execution_mode": "verify_first",
        "verifies": ["tests/test_group.py::test_it"],
    }
    defaults.update(overrides)
    return Task(**defaults)


def _ctx(state, cfg, sha, *, mode: str, task_id: str = "TASK-101") -> GateContext:
    return GateContext(
        task_id=task_id, checkpoint_sha=sha, config=cfg, state=state, facts={"execution_mode": mode}
    )


class TestRegistrationCoversBothConfigurations:
    """kind: contract — BEH-24 Given: a per-task opt-in in a `standard`-default
    project, and separately a project that declares the mode wholesale."""

    def test_a_project_wide_verify_first_default_registers_the_gate(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root, execution_mode="verify_first")
        registry = GateRegistry()

        register_builtin_gates(cfg, registry=registry)

        assert is_registered("tdd.red", "tests", registry)
        assert is_registered("tdd.claims", "tests", registry)

    def test_a_standard_project_wide_default_still_registers_nothing(self, tmp_path):
        """Regression guard: a genuinely ungated project stays dormant."""
        root = _repo(tmp_path)
        cfg = _cfg(root, execution_mode="standard")
        registry = GateRegistry()

        register_builtin_gates(cfg, registry=registry)

        assert not is_registered("tdd.red", "tests", registry)
        assert not is_registered("tdd.claims", "tests", registry)
        assert not has_gates(registry)

    def test_reverting_the_project_default_to_standard_unregisters_the_gate(self, tmp_path):
        """The registration side of the same coin: a project that turns
        `verify_first` back off must lose the gate, or a stale registration
        from an earlier call would keep judging tasks the config no longer
        asks anything of."""
        root = _repo(tmp_path)
        registry = GateRegistry()
        register_builtin_gates(_cfg(root, execution_mode="verify_first"), registry=registry)
        assert is_registered("tdd.red", "tests", registry)
        assert is_registered("tdd.claims", "tests", registry)

        register_builtin_gates(_cfg(root, execution_mode="standard"), registry=registry)

        assert not is_registered("tdd.red", "tests", registry)
        assert not is_registered("tdd.claims", "tests", registry)
        assert not has_gates(registry)

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
    def test_a_per_task_opt_in_registers_the_gate_before_branching(
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
        """The project default is `standard` — the BEH-01 per-task opt-in
        configuration. Without an `ensure_red_gate()` call reachable from the
        verify-first path, `has_gates()` stays false for this run and the
        pre-terminal block / pre-review claims check never fire at all
        (fail-open). Asserted through the real `execute_task` path, before
        any branching on the run's outcome.
        """
        import spec_runner.gates as gates_mod

        fresh = GateRegistry()
        monkeypatch.setattr(gates_mod, "REGISTRY", fresh)

        root = _repo(tmp_path)
        task = _task()
        config = _cfg(root, execution_mode="standard")
        state = ExecutorState(config)
        mock_run.return_value = MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)

        assert not has_gates(fresh), "nothing should be registered before the task runs"

        execute_task(task, config, state)

        assert has_gates(fresh), (
            "a per-task verify_first opt-in in a standard-default project left "
            "has_gates() false — the pre-terminal block and pre-review claims "
            "check would never run for this task at all"
        )
        assert is_registered("tdd.red", "tests", fresh)
        assert is_registered("tdd.claims", "tests", fresh)
        state.close()


class TestRedGateSeesVerifyFirstAsGated:
    """kind: contract — BEH-24 Then: `_red_gate` does not read `verify_first`
    as "execution_mode is not tdd" and skip to SATISFIED; it asks the same
    question of the durable verify evidence a verify-first task produces."""

    def test_a_missing_verify_evidence_does_not_pass(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root, execution_mode="verify_first")
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)

        with ExecutorState(cfg) as state:
            outcome = evaluate_gates(
                "tests", _ctx(state, cfg, _head(root), mode="verify_first"), registry=registry
            )

        assert outcome.status is GateStatus.UNSATISFIED
        detail = "; ".join(r.detail or "" for r in outcome.results)
        assert "no verify evidence" in detail

    def test_a_green_verify_run_for_this_tree_satisfies(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root, execution_mode="verify_first")
        task = _task()
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)

        result = run_live_verify(task, cfg)
        assert result.passed, result.detail

        with ExecutorState(cfg) as state:
            state.record_verify_evidence(task=task, config=cfg, result=result)
            outcome = evaluate_gates(
                "tests", _ctx(state, cfg, result.sha, mode="verify_first"), registry=registry
            )

        assert outcome.status is GateStatus.SATISFIED

    def test_a_descendant_commit_still_satisfies(self, tmp_path):
        """Descent, not equality — the same rule `_red_gate` applies to a
        confirmed red: the paid implementation pass adds commits on top of
        the judged tree, and the gate must still recognise it."""
        root = _repo(tmp_path)
        cfg = _cfg(root, execution_mode="verify_first")
        task = _task()
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)

        result = run_live_verify(task, cfg)
        assert result.passed, result.detail

        with ExecutorState(cfg) as state:
            state.record_verify_evidence(task=task, config=cfg, result=result)

        (root / "README.md").write_text("more work\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "implementation")
        candidate = _head(root)

        with ExecutorState(cfg) as state:
            outcome = evaluate_gates(
                "tests", _ctx(state, cfg, candidate, mode="verify_first"), registry=registry
            )

        assert outcome.status is GateStatus.SATISFIED

    def test_a_test_failure_verify_run_does_not_satisfy(self, tmp_path):
        root = _repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_it():\n    assert False, 'deliberate failure'\n"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "make it fail")
        cfg = _cfg(root, execution_mode="verify_first")
        task = _task()
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)

        result = run_live_verify(task, cfg)
        assert result.ran and not result.passed, result.detail

        with ExecutorState(cfg) as state:
            state.record_verify_evidence(task=task, config=cfg, result=result)
            outcome = evaluate_gates(
                "tests", _ctx(state, cfg, result.sha, mode="verify_first"), registry=registry
            )

        assert outcome.status is GateStatus.UNSATISFIED

    def test_evidence_on_an_unrelated_tree_does_not_count(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root, execution_mode="verify_first")
        task = _task()
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)

        result = run_live_verify(task, cfg)
        assert result.passed, result.detail
        with ExecutorState(cfg) as state:
            state.record_verify_evidence(task=task, config=cfg, result=result)

        with ExecutorState(cfg) as state:
            outcome = evaluate_gates(
                "tests", _ctx(state, cfg, "f" * 40, mode="verify_first"), registry=registry
            )

        assert outcome.status is GateStatus.INSTRUMENT_ERROR, (
            "an unresolvable candidate SHA cannot be judged either way"
        )

    def test_a_resolvable_unrelated_tree_is_not_accepted(self, tmp_path):
        """Unlike the unresolvable-SHA case above, this candidate exists in
        the repo — git can answer, and the answer is "no". A sibling of the
        evidence commit must read as UNSATISFIED (a fact about the work), not
        INSTRUMENT_ERROR (a fact about the tooling) — the same distinction
        `_red_gate`'s own `test_an_unrelated_tree_is_not_accepted` pins for
        `tdd`."""
        root = _repo(tmp_path)
        cfg = _cfg(root, execution_mode="verify_first")
        task = _task()
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)
        base = _head(root)

        _git(root, "checkout", "-q", "--detach")
        (root / "other.py").write_text("y = 2\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "unrelated")
        evidence_result = run_live_verify(task, cfg)
        assert evidence_result.passed, evidence_result.detail
        with ExecutorState(cfg) as state:
            state.record_verify_evidence(task=task, config=cfg, result=evidence_result)

        # A sibling of the evidence commit — same parent, no ancestor
        # relationship between the two.
        _git(root, "checkout", "-q", base)
        (root / "sibling.py").write_text("z = 3\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "sibling")
        sibling = _head(root)

        with ExecutorState(cfg) as state:
            outcome = evaluate_gates(
                "tests", _ctx(state, cfg, sibling, mode="verify_first"), registry=registry
            )

        assert outcome.status is GateStatus.UNSATISFIED
        detail = "; ".join(r.detail or "" for r in outcome.results)
        assert "different tree" in detail.lower()


class TestClaimsGateSeesVerifyFirstToo:
    """kind: contract — BEH-24 Then: the pred-терминальный claims check
    executes for `verify_first`, not skipped by the same mode-literal rule."""

    def test_verify_first_with_no_claims_yet_is_satisfied_by_actually_checking(
        self, tmp_path, monkeypatch
    ):
        root = _repo(tmp_path)
        cfg = _cfg(root, execution_mode="verify_first")
        called: list[str] = []

        from spec_runner import claims as claims_mod

        real = claims_mod.check_claims

        def _tracked(*args, **kwargs):
            called.append("check_claims")
            return real(*args, **kwargs)

        monkeypatch.setattr(claims_mod, "check_claims", _tracked)

        with ExecutorState(cfg) as state:
            result = evaluate_claims(_ctx(state, cfg, _head(root), mode="verify_first"))

        assert result.status is GateStatus.SATISFIED
        assert result.detail == "claims intact", (
            "a mode-keyed skip would answer 'execution_mode is verify_first' "
            "without ever calling check_claims"
        )
        assert called == ["check_claims"], "the claims gate never actually ran"

    def test_a_standard_task_is_skipped_by_mode_not_by_checking(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root, execution_mode="standard")

        with ExecutorState(cfg) as state:
            result = evaluate_claims(_ctx(state, cfg, _head(root), mode="standard"))

        assert result.status is GateStatus.SATISFIED
        assert result.detail == "execution_mode is standard"


class TestGateContextCarriesTheTaskOwnMode:
    """kind: contract — BEH-24 And: the per-task declaration reaches the gate
    through `GateContext.facts`, as the task's own resolved value."""

    def test_a_task_level_declaration_is_what_the_gate_receives(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root, execution_mode="standard")
        task = Task(
            id="TASK-202",
            name="t",
            priority="p1",
            status="todo",
            estimate="1h",
            execution_mode="verify_first",
        )

        assert cfg.resolve_execution_mode(task) == "verify_first"

        # The project default is `standard`, so `register_builtin_gates` alone
        # would unregister the gate — exactly like a real run, the per-task
        # opt-in is what registers it (`ensure_red_gate`, called from the
        # verify-first path before the live run).
        registry = GateRegistry()
        from spec_runner.gates import ensure_red_gate

        ensure_red_gate(registry)
        with ExecutorState(cfg) as state:
            outcome = evaluate_gates(
                "tests",
                _ctx(
                    state,
                    cfg,
                    _head(root),
                    mode=cfg.resolve_execution_mode(task),
                    task_id=task.id,
                ),
                registry=registry,
            )

        assert outcome.status is GateStatus.UNSATISFIED, (
            "the gate must see the task's own resolved mode, not the "
            "project's standard default, or it would silently pass"
        )


class TestStandardIsTheOnlyModeWithNoGuarantee:
    """kind: contract — regression guard: `standard` is a real "no
    guarantee" mode and must keep passing trivially; only the third
    (`verify_first`) mode was mis-read that way."""

    def test_a_standard_task_still_passes_the_red_gate_trivially(self, tmp_path):
        """A `standard` task can be evaluated even when the *project*
        default is `tdd`/`verify_first` (the per-task opt-out) — registration
        is a project-level decision, the mode fact is per task."""
        root = _repo(tmp_path)
        cfg = _cfg(root, execution_mode="tdd")
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)

        with ExecutorState(cfg) as state:
            outcome = evaluate_gates(
                "tests", _ctx(state, cfg, _head(root), mode="standard"), registry=registry
            )

        assert outcome.status is GateStatus.SATISFIED
        detail = "; ".join(r.detail or "" for r in outcome.results)
        assert "execution_mode is standard" in detail


class TestInstrumentErrorWithNoStateToRead:
    """kind: contract — every gate that needs `ctx.state` to answer must say
    so as `INSTRUMENT_ERROR`, not crash or silently pass, when it is absent
    — the same posture #245 established for a missing `execution_mode`."""

    def test_the_tdd_red_gate_without_state_is_an_instrument_error(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root, execution_mode="tdd")
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)

        outcome = evaluate_gates(
            "tests", _ctx(None, cfg, _head(root), mode="tdd"), registry=registry
        )

        assert outcome.status is GateStatus.INSTRUMENT_ERROR

    def test_the_verify_first_gate_without_state_is_an_instrument_error(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root, execution_mode="verify_first")
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)

        outcome = evaluate_gates(
            "tests", _ctx(None, cfg, _head(root), mode="verify_first"), registry=registry
        )

        assert outcome.status is GateStatus.INSTRUMENT_ERROR

    def test_the_claims_gate_without_state_is_an_instrument_error(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root, execution_mode="verify_first")

        result = evaluate_claims(_ctx(None, cfg, _head(root), mode="verify_first"))

        assert result.status is GateStatus.INSTRUMENT_ERROR


class TestCandidateEvidenceRefreshesBeforeTheGate:
    """#380 review finding 1 — kind: integration, through the real
    `execute_task` -> `post_done_hook` path (`post_done_hook` unmocked, unlike
    every other `execute_task` scenario in this module and in
    `test_verify_run_order.py`): a group red on entry, the today-working
    FR-14 path, must still be able to reach DONE once the implementation
    pass fixes it — `_verify_first_gate` must judge the candidate, not the
    pre-implementation snapshot `_run_verify_first_phase` recorded before the
    paid call."""

    def test_a_group_red_on_entry_that_the_fix_makes_green_reaches_done(self, tmp_path):
        root = _repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_it():\n    assert False, 'not implemented yet'\n"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "red group")

        cfg = _cfg(
            root,
            execution_mode="verify_first",
            auto_commit=True,
            run_lint_on_done=False,
        )
        task = _task()

        def fake_agent(config, invocation, **kwargs):
            # The agent implements the behaviour the declared group checks.
            (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
            return subprocess.CompletedProcess(
                args=invocation.argv, returncode=0, stdout="TASK_COMPLETE\n", stderr=""
            )

        with (
            patch("spec_runner.execution._run_agent_process", side_effect=fake_agent),
            patch(
                "spec_runner.execution.build_cli_invocation",
                return_value=CliInvocation(["fake"], "text"),
            ),
            patch("spec_runner.execution.build_task_prompt", return_value="p"),
            patch("spec_runner.execution.update_task_status"),
            ExecutorState(cfg) as state,
        ):
            result = execute_task(task, cfg, state)

        assert result is True, (
            "a group red on entry that the implementation pass fixed should reach "
            "DONE — the pre-terminal gate must judge the candidate's own evidence, "
            "not the pre-implementation snapshot"
        )

    def test_a_group_still_red_after_the_attempt_stays_blocked(self, tmp_path):
        """The other half of the same fix: re-verifying must not turn into a
        second, weaker gate that lets an unfixed group through."""
        root = _repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_it():\n    assert False, 'not implemented yet'\n"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "red group")

        cfg = _cfg(
            root,
            execution_mode="verify_first",
            auto_commit=True,
            run_lint_on_done=False,
        )
        task = _task()

        def fake_agent(config, invocation, **kwargs):
            # The agent touches something else and never fixes the group.
            (root / "README.md").write_text("notes\n")
            return subprocess.CompletedProcess(
                args=invocation.argv, returncode=0, stdout="TASK_COMPLETE\n", stderr=""
            )

        with (
            patch("spec_runner.execution._run_agent_process", side_effect=fake_agent),
            patch(
                "spec_runner.execution.build_cli_invocation",
                return_value=CliInvocation(["fake"], "text"),
            ),
            patch("spec_runner.execution.build_task_prompt", return_value="p"),
            patch("spec_runner.execution.update_task_status"),
            ExecutorState(cfg) as state,
        ):
            result = execute_task(task, cfg, state)

        assert result is False
