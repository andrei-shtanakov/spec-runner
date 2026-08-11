"""F-4 and F-6: stop re-authoring a confirmed red, and account for what it cost.

The battle test on published v2.25.0 ran a task whose GREEN pass failed three
times. Each retry ran the **whole RED phase again**, leaving three red commits
and three `active` checkpoints for one task — an agent call per retry that need
not happen, a history that lies about how many reds there were, and a state the
CAS-based remedies do not model.

The same run showed `spec-runner costs` reporting `$0.00` with `None` in every
token column: `tdd._run_agent` parsed the CLI result and returned only `.text`,
so TDD's extra agent call was invisible. In a priced run that is money nobody
can see.

Report: `docs/superpowers/specs/2026-08-11-tdd-battle-report.md`, F-4 and F-6.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import RedOutcome, resolve_namespace, run_red_phase

pytestmark = pytest.mark.slow

FAILING = "def test_thing():\n    assert False, 'not implemented'\n"


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
        "execution_mode": "tdd",
        "test_command": "python -m pytest",
        "lint_command": "",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(task_id="TASK-001") -> Task:
    return Task(id=task_id, name="t", priority="p1", status="todo", estimate="1h")


def _agent(monkeypatch, calls: list, *, body=FAILING, cost=0.25):
    """A scripted agent that also reports what the call cost."""
    from spec_runner import tdd

    def _fake(config, prompt, **kwargs):
        calls.append(prompt[:40])
        path = Path(config.project_root) / "tests" / "test_thing.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        return tdd.AgentCall(
            text="TDD_SELECTOR: tests/test_thing.py::test_thing\nTASK_COMPLETE",
            input_tokens=1000,
            output_tokens=200,
            cost_usd=cost,
        )

    monkeypatch.setattr(tdd, "_run_agent", _fake)
    return calls


class TestAConfirmedRedIsReused:
    """F-4. "Durable checkpoint" was the point; re-deriving it each retry
    spends money to reach a conclusion already recorded."""

    def test_a_second_run_does_not_call_the_agent_again(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        calls: list = []
        _agent(monkeypatch, calls)

        with ExecutorState(cfg) as state:
            first = run_red_phase(_task(), cfg, state)
            second = run_red_phase(_task(), cfg, state)

        assert first.outcome is RedOutcome.EXPECTED_FAIL
        assert second.outcome is RedOutcome.EXPECTED_FAIL
        assert len(calls) == 1, "the RED agent was asked to re-derive a conclusion already recorded"

    def test_the_reused_run_leaves_one_checkpoint_and_one_red_commit(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _agent(monkeypatch, [])

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)
            run_red_phase(_task(), cfg, state)
            active = state.active_checkpoints(resolve_namespace(cfg), "TASK-001")

        assert len(active) == 1
        reds = [
            line
            for line in _git(root, "log", "--format=%s").stdout.splitlines()
            if line.startswith("TASK-001: red")
        ]
        assert len(reds) == 1, f"one red per task, not one per attempt: {reds}"

    def test_a_checkpoint_from_another_config_is_not_reused(self, tmp_path, monkeypatch):
        """The checkpoint records the policy it was produced under; a different
        policy is a different question, so its answer does not carry over."""
        root = _repo(tmp_path)
        calls: list = []
        _agent(monkeypatch, calls)

        with ExecutorState(_cfg(root)) as state:
            run_red_phase(_task(), _cfg(root), state)
        with ExecutorState(_cfg(root, gate_recovery_attempts=7)) as state:
            run_red_phase(_task(), _cfg(root, gate_recovery_attempts=7), state)

        assert len(calls) == 2

    def test_a_checkpoint_not_reachable_from_here_is_not_reused(self, tmp_path, monkeypatch):
        """A red on a branch this one does not descend from proves nothing
        about this tree."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        calls: list = []
        _agent(monkeypatch, calls)

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)
        # Move somewhere the red commit is not an ancestor of.
        _git(root, "checkout", "-q", "--orphan", "elsewhere")
        _git(root, "rm", "-rq", "--cached", ".")
        (root / "README.md").write_text("y\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "unrelated root")
        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)

        assert len(calls) == 2, "a red from an unreachable branch was reused"

    def test_several_suitable_checkpoints_is_a_state_error(self, tmp_path, monkeypatch):
        """Not "take the newest": two active lineages mean the state is wrong,
        and guessing would hide it."""
        from spec_runner.tdd import RedCheckpoint

        root = _repo(tmp_path)
        cfg = _cfg(root)
        _agent(monkeypatch, [])
        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)
            active = state.active_checkpoints(resolve_namespace(cfg), "TASK-001")[0]
            twin = RedCheckpoint(**{**active.__dict__, "timestamp": "2099-01-01T00:00:00"})
            state.record_red_checkpoint(twin)

            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "active checkpoints" in (result.detail or "")


class TestRemediesDecideWhetherAuthoringResumes:
    def test_after_abandon_the_agent_is_called_again(self, tmp_path, monkeypatch):
        from spec_runner.remedy import abandon

        root = _repo(tmp_path)
        cfg = _cfg(root)
        calls: list = []
        _agent(monkeypatch, calls)

        with ExecutorState(cfg) as state:
            first = run_red_phase(_task(), cfg, state)
            abandon(cfg, state, "TASK-001", first.checkpoint.checkpoint_id, reason="start over")
            run_red_phase(_task(), cfg, state)

        assert len(calls) == 2, "abandon means author a new red, not reuse the old one"

    def test_after_repair_the_repaired_lineage_is_reused(self, tmp_path, monkeypatch):
        from spec_runner.remedy import repair

        root = _repo(tmp_path)
        cfg = _cfg(root)
        calls: list = []
        _agent(monkeypatch, calls)

        with ExecutorState(cfg) as state:
            first = run_red_phase(_task(), cfg, state)
            (root / "tests" / "test_thing.py").write_text(
                "def test_thing():\n    assert 1 == 2, 'still missing'\n"
            )
            _git(root, "add", "-A")
            _git(root, "commit", "-qm", "operator: repair")
            fixed = _git(root, "rev-parse", "HEAD").stdout.strip()
            repair(cfg, state, "TASK-001", first.checkpoint.checkpoint_id, fixed, reason="typo")
            run_red_phase(_task(), cfg, state)

        assert len(calls) == 1, "a repaired lineage is a confirmed red; re-authoring wastes a call"


class TestTheRedPassIsPaidForVisibly:
    """F-6. The battle's `$0.00` was true only because the agent was a script;
    the `None` token columns were the tell."""

    def test_the_authoring_call_reaches_the_task_total(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _agent(monkeypatch, [], cost=0.25)

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)
            assert state.task_cost("TASK-001") == pytest.approx(0.25)
            assert state.total_cost() == pytest.approx(0.25)

    def test_a_failed_authoring_attempt_is_still_paid_for(self, tmp_path, monkeypatch):
        """Money spent on a call that produced nothing usable is still spent."""
        from spec_runner import tdd

        root = _repo(tmp_path)
        cfg = _cfg(root)

        def _no_marker(config, prompt, **kwargs):
            return tdd.AgentCall(text="I forgot the marker", cost_usd=0.4)

        monkeypatch.setattr(tdd, "_run_agent", _no_marker)
        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            assert result.outcome is RedOutcome.UNVERIFIABLE
            assert state.task_cost("TASK-001") == pytest.approx(0.4)

    def test_a_reused_checkpoint_is_not_charged_twice(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _agent(monkeypatch, [], cost=0.25)

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)
            run_red_phase(_task(), cfg, state)
            assert state.task_cost("TASK-001") == pytest.approx(0.25)

    def test_provenance_names_the_phase_that_spent_it(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _agent(monkeypatch, [], cost=0.25)

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)
            calls = state.agent_calls("TASK-001")

        assert [c["provenance"] for c in calls] == ["red_authoring"]
        assert calls[0]["cost_usd"] == pytest.approx(0.25)
        assert calls[0]["input_tokens"] == 1000

    def test_tokens_and_cost_come_from_the_same_places(self, tmp_path, monkeypatch):
        """Found by re-running the battle matrix: cost summed attempts plus the
        ledger while tokens summed attempts alone, so `costs` reported $0.73
        spent on 10,000 tokens when 15,600 were used. A report that contradicts
        itself is worse than one that under-reports consistently."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _agent(monkeypatch, [], cost=0.42)

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)
            state.record_attempt(
                "TASK-001", True, 1.0, input_tokens=8000, output_tokens=2000, cost_usd=0.31
            )
            inp, out = state.task_tokens("TASK-001")
            total_in, total_out = state.total_tokens()
            cost = state.task_cost("TASK-001")

        assert cost == pytest.approx(0.73)
        assert (inp, out) == (9000, 2200), "the RED pass's tokens must be in the task total"
        assert (total_in, total_out) == (9000, 2200)

    def test_ledger_only_spend_is_reported(self, tmp_path, monkeypatch, capsys):
        """Copilot on #187, and the same shape as F-9: a RED authoring call
        that fails before any attempt is recorded leaves money in the ledger
        and no `tasks` row, which the costs table hid behind a `--`."""
        import argparse

        from spec_runner import tdd
        from spec_runner.cli_info import cmd_costs

        root = _repo(tmp_path)
        (root / "spec").mkdir(exist_ok=True)
        (root / "spec" / "tasks.md").write_text(
            "# Tasks\n\n### TASK-001: t\n🟠 P1 | ⬜ TODO\nEst: 1d\n\n- [ ] x\n"
        )
        cfg = _cfg(root)

        def _no_marker(config, prompt, **kwargs):
            return tdd.AgentCall(text="no marker here", input_tokens=900, cost_usd=0.37)

        monkeypatch.setattr(tdd, "_run_agent", _no_marker)
        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)

        cmd_costs(argparse.Namespace(json=False, sort="id"), cfg)
        out = capsys.readouterr().out
        assert "0.37" in out, f"ledger-only spend was hidden:\n{out}"
