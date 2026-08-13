"""#141 slice 1c: the RED gate — TDD as the second consumer of #164.

The transition into GREEN is forbidden without a **confirmed** red: the
selector was executed and failed, replayed against a commit. Not "the agent
said it failed" — an agent's report of its own red is exactly the evidence this
replaces.

The same gate is evaluated at two moments, because it answers the same question
at both: before implementing (do not write code without a demonstrated red) and
before merging (do not merge a task that never had one). Registering it once
for the `tests` phase and evaluating it twice is the honest shape; a second
gate saying the same thing in the pre-terminal position would be two things to
keep in step.

Design: `docs/superpowers/specs/2026-08-11-tdd-lifecycle-design.md` §3.2, §3.6
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.gates import (
    GateContext,
    GateRegistry,
    GateStatus,
    evaluate_gates,
    register_builtin_gates,
)
from spec_runner.state import ExecutorState
from spec_runner.tdd import RedCheckpoint, RedOutcome, resolve_namespace


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
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
        "execution_mode": "tdd",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _checkpoint(cfg, sha="a" * 40, **overrides) -> RedCheckpoint:
    defaults: dict = {
        "task_id": "TASK-001",
        "namespace": resolve_namespace(cfg),
        "commit_sha": sha,
        "baseline_sha": "b" * 40,
        "selector": "tests/test_x.py::test_y",
        "environment_id": "uv.lock:abc",
        "execution_mode": "tdd",
        "config_hash": "h",
        "outcome": RedOutcome.EXPECTED_FAIL,
    }
    defaults.update(overrides)
    return RedCheckpoint(**defaults)


def _evaluate(cfg, state, *, mode="tdd", sha="a" * 40):
    registry = GateRegistry()
    register_builtin_gates(cfg, registry=registry)
    ctx = GateContext(
        task_id="TASK-001",
        checkpoint_sha=sha,
        config=cfg,
        state=state,
        facts={"execution_mode": mode},
    )
    return evaluate_gates("tests", ctx, registry=registry)


class TestStandardModeIsUntouched:
    def test_a_standard_project_registers_no_red_gate(self, tmp_path):
        registry = GateRegistry()
        register_builtin_gates(_cfg(_repo(tmp_path), execution_mode="standard"), registry=registry)
        assert "tests" not in registry.phases()

    def test_a_standard_task_passes_even_in_a_tdd_project(self, tmp_path):
        """The per-task opt-out has to reach the gate, or it is not an opt-out."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        with ExecutorState(cfg) as state:
            outcome = _evaluate(cfg, state, mode="standard")
        assert outcome.status is GateStatus.SATISFIED


class TestGreenIsRefusedWithoutAConfirmedRed:
    def test_no_checkpoint_at_all_blocks(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        with ExecutorState(cfg) as state:
            outcome = _evaluate(cfg, state)
        assert outcome.status is GateStatus.UNSATISFIED
        assert "no confirmed red" in (outcome.results[0].detail or "").lower()

    def test_a_confirmed_red_for_this_tree_satisfies(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(_checkpoint(cfg, sha=head))
            outcome = _evaluate(cfg, state, sha=head)
        assert outcome.status is GateStatus.SATISFIED

    def test_a_checkpoint_from_another_workstream_does_not_count(self, tmp_path):
        """The collision the pilot hit: same TASK-NNN, different workstream."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(_checkpoint(cfg, namespace="someone-else", sha="c" * 40))
            outcome = _evaluate(cfg, state, sha="c" * 40)
        assert outcome.status is GateStatus.UNSATISFIED

    def test_an_unverifiable_checkpoint_is_an_instrument_error_not_a_refusal(self, tmp_path):
        """ "We could not find out" is a fact about us, not about the code, and
        the two earn different answers — the split `RedOutcome` exists for."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(
                _checkpoint(cfg, sha="c" * 40, outcome=RedOutcome.UNVERIFIABLE)
            )
            outcome = _evaluate(cfg, state, sha="c" * 40)
        assert outcome.status is GateStatus.INSTRUMENT_ERROR

    def test_a_not_red_checkpoint_blocks(self, tmp_path):
        """The agent claimed a red and the replay disagreed. That is a fact
        about the work, and it blocks."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(_checkpoint(cfg, sha="c" * 40, outcome=RedOutcome.NOT_RED))
            outcome = _evaluate(cfg, state, sha="c" * 40)
        assert outcome.status is GateStatus.UNSATISFIED


class TestTheCheckpointMustBelongToThisTree:
    """#164 criterion 5, applied here: a red confirmed on an older tree says
    nothing about the tree about to be merged."""

    def test_a_checkpoint_whose_sha_does_not_resolve_is_an_instrument_error(self, tmp_path):
        """A recorded SHA that no longer exists is not evidence about anything
        present — but it is also not a *verdict* about the work.

        **This reverses what this test asserted from #141 slice 1c until
        #245.** It used to expect `UNSATISFIED` ("the confirmed red is on a
        different tree"), which reads as a statement about the code when the
        truth is that git could not look: the commit is missing from this
        clone, or history was rewritten under us. Same fail-closed outcome —
        nothing merges either way — but the run now exits 2 rather than 1, and
        the message says what actually happened.
        """
        root = _repo(tmp_path)
        cfg = _cfg(root)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(_checkpoint(cfg, sha="c" * 40))
            outcome = _evaluate(cfg, state, sha=head)
        assert outcome.status is GateStatus.INSTRUMENT_ERROR
        assert "different tree" not in (outcome.results[0].detail or "").lower()
        assert "could not compare" in (outcome.results[0].detail or "").lower()

    def test_a_descendant_tree_is_accepted(self, tmp_path):
        """Green *is* new commits on top of the red. Requiring SHA equality
        would make the gate unsatisfiable the moment the work it gates
        happens; requiring descent is the honest version."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        red = _git(root, "rev-parse", "HEAD").stdout.strip()
        (root / "impl.py").write_text("x = 1\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "green")
        green = _git(root, "rev-parse", "HEAD").stdout.strip()

        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(_checkpoint(cfg, sha=red))
            outcome = _evaluate(cfg, state, sha=green)
        assert outcome.status is GateStatus.SATISFIED

    def test_an_unrelated_tree_is_not_accepted(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        red = _git(root, "rev-parse", "HEAD").stdout.strip()
        _git(root, "checkout", "-q", "--detach")
        (root / "other.py").write_text("y = 2\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "unrelated")
        unrelated = _git(root, "rev-parse", "HEAD").stdout.strip()
        # A sibling of the red, not a descendant.
        _git(root, "checkout", "-q", red)
        (root / "sibling.py").write_text("z = 3\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "sibling")
        sibling = _git(root, "rev-parse", "HEAD").stdout.strip()

        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(_checkpoint(cfg, sha=unrelated))
            outcome = _evaluate(cfg, state, sha=sibling)
        assert outcome.status is GateStatus.UNSATISFIED
        assert "different tree" in (outcome.results[0].detail or "").lower()


class TestTheModeMustReachTheGate:
    def test_a_missing_mode_fact_is_an_instrument_error(self, tmp_path):
        """Same posture as the review gate: the site failing to report is our
        bug, and must not be laundered into a verdict about the code."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)
        with ExecutorState(cfg) as state:
            ctx = GateContext(
                task_id="TASK-001", checkpoint_sha="c" * 40, config=cfg, state=state, facts={}
            )
            outcome = evaluate_gates("tests", ctx, registry=registry)
        assert outcome.status is GateStatus.INSTRUMENT_ERROR


class TestExecutionModeIsPartOfTheVerdictIdentity:
    def test_execution_mode_is_a_policy_key(self):
        from spec_runner.gates import POLICY_KEYS

        assert "execution_mode" in POLICY_KEYS

    def test_flipping_the_mode_invalidates_an_earlier_verdict(self, tmp_path):
        root = _repo(tmp_path)
        a = GateContext("TASK-001", "c" * 40, _cfg(root, execution_mode="standard"))
        b = GateContext("TASK-001", "c" * 40, _cfg(root, execution_mode="tdd"))
        assert a.config_hash != b.config_hash


@pytest.mark.slow
class TestTheRedPhaseEndToEnd:
    """The authoring pass, the checkpoint commit, and the verification that
    turns a claim into evidence."""

    def _agent(self, monkeypatch, *, output: str, writes: dict[str, str] | None = None):
        """Stand in for the coding agent: write files, then report."""
        from spec_runner import tdd

        def _fake(config, prompt, **kwargs):
            for name, body in (writes or {}).items():
                (Path(config.project_root) / name).write_text(body)
            return tdd.AgentCall(text=output)

        monkeypatch.setattr(tdd, "_run_agent", _fake)

    def test_a_genuine_red_produces_a_checkpoint(self, tmp_path, monkeypatch):
        from spec_runner.task import Task
        from spec_runner.tdd import run_red_phase

        root = _repo(tmp_path)
        cfg = _cfg(root, test_command="python -m pytest")
        self._agent(
            monkeypatch,
            output="TDD_SELECTOR: test_thing.py::test_thing\nTASK_COMPLETE",
            writes={"test_thing.py": "def test_thing():\n    assert False\n"},
        )
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")

        with ExecutorState(cfg) as state:
            result = run_red_phase(task, cfg, state)
            stored = state.red_checkpoint("TASK-001", resolve_namespace(cfg))

        assert result.outcome is RedOutcome.EXPECTED_FAIL
        assert stored is not None and stored.selector == "test_thing.py::test_thing"

    def test_a_claimed_red_that_passes_is_caught(self, tmp_path, monkeypatch):
        """The load-bearing case. The agent says red; the replay says green."""
        from spec_runner.task import Task
        from spec_runner.tdd import run_red_phase

        root = _repo(tmp_path)
        cfg = _cfg(root, test_command="python -m pytest")
        self._agent(
            monkeypatch,
            output="TDD_SELECTOR: test_thing.py::test_thing\nTASK_COMPLETE",
            writes={"test_thing.py": "def test_thing():\n    assert True\n"},
        )
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")

        with ExecutorState(cfg) as state:
            result = run_red_phase(task, cfg, state)
            stored = state.red_checkpoint("TASK-001", resolve_namespace(cfg))

        assert result.outcome is RedOutcome.NOT_RED
        assert stored is not None and stored.outcome is RedOutcome.NOT_RED, (
            "a refuted claim is evidence too — it must be recorded, not dropped"
        )

    def test_no_selector_marker_is_unverifiable(self, tmp_path, monkeypatch):
        from spec_runner.task import Task
        from spec_runner.tdd import run_red_phase

        root = _repo(tmp_path)
        cfg = _cfg(root, test_command="python -m pytest")
        self._agent(
            monkeypatch,
            output="I wrote a failing test, trust me.\nTASK_COMPLETE",
            writes={"test_thing.py": "def test_thing():\n    assert False\n"},
        )
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")

        with ExecutorState(cfg) as state:
            result = run_red_phase(task, cfg, state)

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "TDD_SELECTOR" in (result.detail or "")

    def test_an_agent_that_writes_nothing_is_unverifiable(self, tmp_path, monkeypatch):
        """No commit means no SHA to replay, and no SHA means no evidence."""
        from spec_runner.task import Task
        from spec_runner.tdd import run_red_phase

        root = _repo(tmp_path)
        cfg = _cfg(root, test_command="python -m pytest")
        self._agent(monkeypatch, output="TDD_SELECTOR: test_thing.py::test_thing")
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")

        with ExecutorState(cfg) as state:
            result = run_red_phase(task, cfg, state)

        assert result.outcome is RedOutcome.UNVERIFIABLE

    def test_the_red_is_committed_so_it_can_be_replayed(self, tmp_path, monkeypatch):
        from spec_runner.task import Task
        from spec_runner.tdd import run_red_phase

        root = _repo(tmp_path)
        cfg = _cfg(root, test_command="python -m pytest")
        before = _git(root, "rev-parse", "HEAD").stdout.strip()
        self._agent(
            monkeypatch,
            output="TDD_SELECTOR: test_thing.py::test_thing",
            writes={"test_thing.py": "def test_thing():\n    assert False\n"},
        )
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")

        with ExecutorState(cfg) as state:
            run_red_phase(task, cfg, state)
            stored = state.red_checkpoint("TASK-001", resolve_namespace(cfg))

        after = _git(root, "rev-parse", "HEAD").stdout.strip()
        assert after != before, "the red checkpoint commit is what makes replay possible"
        assert stored.commit_sha == after
        assert stored.baseline_sha == before


class _ReachedImplementation(Exception):
    """Raised in place of the implementation pass, to prove it was reached."""


@pytest.mark.slow
class TestExecutionRefusesToImplementWithoutARed:
    """The wiring: `execute_task` must not reach the implementation pass when
    the gate is unsatisfied. Slices 1a and 1b each shipped a guard asserting
    nothing was wired; this is the PR that deletes them, so these take over."""

    def _task(self, **overrides):
        from spec_runner.task import Task

        defaults: dict = {
            "id": "TASK-001",
            "name": "t",
            "priority": "p1",
            "status": "todo",
            "estimate": "1h",
        }
        defaults.update(overrides)
        return Task(**defaults)

    def _prepare(self, tmp_path, monkeypatch, *, red_output, red_writes):
        from spec_runner import execution, tdd

        root = _repo(tmp_path)
        cfg = _cfg(
            root, test_command="python -m pytest", create_git_branch=False, auto_commit=False
        )

        def _fake_red_agent(config, prompt, **kwargs):
            for name, body in (red_writes or {}).items():
                (Path(config.project_root) / name).write_text(body)
            return tdd.AgentCall(text=red_output)

        monkeypatch.setattr(tdd, "_run_agent", _fake_red_agent)

        implemented: list = []

        def _stop_at_the_implementation(*a, **k):
            # Narrower than patching `subprocess.run`: that is the same module
            # object `tdd` uses, so stubbing it would break the RED phase's own
            # git calls and the test would pass for the wrong reason.
            implemented.append(1)
            raise _ReachedImplementation

        monkeypatch.setattr(execution, "build_task_prompt", _stop_at_the_implementation)
        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
        monkeypatch.setattr(execution, "update_task_status", lambda *a, **k: True)
        return execution, cfg, implemented

    def test_a_refuted_red_stops_before_the_implementation(self, tmp_path, monkeypatch):
        execution, cfg, implemented = self._prepare(
            tmp_path,
            monkeypatch,
            red_output="TDD_SELECTOR: test_thing.py::test_thing",
            red_writes={"test_thing.py": "def test_thing():\n    assert True\n"},
        )
        with ExecutorState(cfg) as state:
            result = execution.execute_task(self._task(), cfg, state)
            attempts = state.get_task_state("TASK-001").attempts

        assert result is False
        assert implemented == [], "the implementation pass ran despite an unconfirmed red"
        assert "RED not confirmed" in (attempts[-1].error or "")

    def test_a_confirmed_red_lets_the_implementation_proceed(self, tmp_path, monkeypatch):
        """The companion: without it, the refusal test would also pass if the
        implementation pass were simply unreachable in this setup."""
        execution, cfg, implemented = self._prepare(
            tmp_path,
            monkeypatch,
            red_output="TDD_SELECTOR: test_thing.py::test_thing",
            red_writes={"test_thing.py": "def test_thing():\n    assert False\n"},
        )
        with ExecutorState(cfg) as state, pytest.raises(_ReachedImplementation):
            execution.execute_task(self._task(), cfg, state)

        assert implemented == [1], "a confirmed red must not block the implementation pass"

    def test_a_standard_task_never_enters_the_red_phase(self, tmp_path, monkeypatch):
        execution_mod, cfg, implemented = self._prepare(
            tmp_path,
            monkeypatch,
            red_output="",
            red_writes={},
        )
        with ExecutorState(cfg) as state, pytest.raises(_ReachedImplementation):
            execution_mod.execute_task(self._task(execution_mode="standard"), cfg, state)

        assert implemented == [1], "a standard task went through the RED phase"


@pytest.mark.slow
class TestPreReplayFailuresAreStillDurable:
    """A `RedCheckpoint` is a statement about a commit, so the failures that
    happen before there is one cannot be checkpoints. They must still survive
    the run — in the append-only phase history, like every other observation."""

    def _run(self, tmp_path, monkeypatch, *, output, writes):
        from spec_runner import execution, tdd
        from spec_runner.task import Task

        root = _repo(tmp_path)
        cfg = _cfg(
            root, test_command="python -m pytest", create_git_branch=False, auto_commit=False
        )

        def _agent(config, prompt, **kwargs):
            for name, body in (writes or {}).items():
                (Path(config.project_root) / name).write_text(body)
            return tdd.AgentCall(text=output)

        monkeypatch.setattr(tdd, "_run_agent", _agent)
        monkeypatch.setattr(execution, "pre_start_hook", lambda *a, **k: True)
        monkeypatch.setattr(execution, "update_task_status", lambda *a, **k: True)

        def _stop(*a, **k):
            raise _ReachedImplementation

        monkeypatch.setattr(execution, "build_task_prompt", _stop)
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")
        with ExecutorState(cfg) as state:
            result = execution.execute_task(task, cfg, state)
            history = state.phase_history("TASK-001")
            checkpoint = state.red_checkpoint("TASK-001", resolve_namespace(cfg))
        return result, history, checkpoint

    def test_a_missing_marker_leaves_no_checkpoint_but_does_leave_history(
        self, tmp_path, monkeypatch
    ):
        result, history, checkpoint = self._run(
            tmp_path,
            monkeypatch,
            output="I wrote a failing test, honest.",
            writes={"test_thing.py": "def test_thing():\n    assert False\n"},
        )
        assert result is False
        assert checkpoint is None, "there is no commit for a checkpoint to be about"
        assert any(r.phase == "tests" for r in history), (
            "the refusal must survive the run somewhere, or the next run relearns it"
        )

    def test_an_authoring_pass_that_writes_nothing_is_recorded_the_same_way(
        self, tmp_path, monkeypatch
    ):
        result, history, checkpoint = self._run(
            tmp_path,
            monkeypatch,
            output="TDD_SELECTOR: test_thing.py::test_thing",
            writes={},
        )
        assert result is False
        assert checkpoint is None
        assert any(r.phase == "tests" for r in history)
