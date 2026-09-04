"""Acceptance e2e for BEH-18 (spec-runner#334, TASK-013) — the battle case.

WS-disputatio-65 TASK-001, reproduced end to end: the baseline carries a
survivor red from a PREVIOUS workstream at the old-style path, and a new,
input-distinguishable workstream runs its own TASK-001.
`Then` the new evidential path does not collide, #252 D does not fire, the
attempt reaches a confirmed checkpoint, and no manual rename is ever needed.
`And` the boundary is pinned separately: with indistinguishable input (same
empty `spec_prefix`, no `tdd_namespace`) the namespace coincides and the
standing #252 D refusal remains — the guarantee is not silently
over-extended (FR-10).

Delivered under a tdd-waiver (spec/.tdd-evidence/waivers/…/TASK-013.json):
the behaviour arrived with TASK-009 (`namespace_segment`) on top of the
standing #252 D machinery; this green IS the battle e2e, run for real.
"""

import subprocess
import sys
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import RedOutcome, resolve_namespace, run_red_phase
from spec_runner.tdd_runners import ADAPTERS

SURVIVOR = "tests/test_task_001_red.py"
FAILING = "def test_new_behaviour():\n    assert False\n"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo_with_survivor(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True, exist_ok=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "o@e.c")
    _git(root, "config", "user.name", "O")
    (root / "README.md").write_text("x\n")
    # The battle shape: a previous workstream's red survived on the base
    # branch under the old, namespace-less name.
    (root / SURVIVOR).write_text("def test_left_behind():\n    assert False\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base with a neighbour's survivor")
    return root


def _cfg(root: Path, *, spec_prefix: str = "", state_name: str = "default") -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / f".state-{state_name}.db",
        logs_dir=root / ".logs",
        execution_mode="tdd",
        test_command=f"{sys.executable} -m pytest",
        lint_command="",
        spec_prefix=spec_prefix,
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task() -> Task:
    return Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")


def _agent_writing_to(monkeypatch, path: str) -> None:
    from spec_runner import tdd

    def _red(config, prompt, **kwargs):
        target = Path(config.project_root) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(FAILING)
        return tdd.AgentCall(text=f"TDD_SELECTOR: {path}::test_new_behaviour")

    monkeypatch.setattr(tdd, "_run_agent", _red)


class TestANeighbourSurvivorDoesNotBlockTheFirstTask:
    """BEH-18 main path: the pipeline convention (a distinct `spec_prefix`
    per workstream) is enough input to tell the workstreams apart."""

    def test_the_first_task_reaches_a_checkpoint_with_no_manual_rename(self, tmp_path, monkeypatch):
        root = _repo_with_survivor(tmp_path)
        cfg = _cfg(root, spec_prefix="WS-neighbour-341-")

        evidential = ADAPTERS["pytest"].evidential_file(
            "TASK-001", namespace=resolve_namespace(cfg)
        )
        # Then: the new path does not collide with the survivor.
        assert str(evidential) != SURVIVOR

        _agent_writing_to(monkeypatch, str(evidential))
        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        # And: #252 D does not fire, the attempt reaches a checkpoint, and
        # the survivor sits untouched — no manual rename on any step.
        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
        assert result.checkpoint is not None
        assert (root / SURVIVOR).read_text().startswith("def test_left_behind")


class TestIndistinguishableInputStillRefuses:
    """BEH-18 boundary (FR-10): same empty `spec_prefix`, no declared
    `tdd_namespace` — one namespace, one path, and the standing #252 D
    refusal; distinguishability is honestly not claimed."""

    def test_the_same_input_yields_the_same_namespace_and_the_refusal_stands(
        self, tmp_path, monkeypatch
    ):
        root = _repo_with_survivor(tmp_path)
        # Two SEPARATE workstreams (each with its own state DB), whose input
        # is indistinguishable: same empty spec_prefix, no tdd_namespace.
        first = _cfg(root, spec_prefix="", state_name="first-ws")
        second = _cfg(root, spec_prefix="", state_name="second-ws")
        assert resolve_namespace(first) == resolve_namespace(second)

        evidential = ADAPTERS["pytest"].evidential_file(
            "TASK-001", namespace=resolve_namespace(first)
        )
        # The first indistinguishable workstream freezes the shared path…
        _agent_writing_to(monkeypatch, str(evidential))
        with ExecutorState(first) as state:
            result_one = run_red_phase(_task(), first, state)
        assert result_one.outcome is RedOutcome.EXPECTED_FAIL, result_one.detail
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "first ws red lands on the base")

        # …and the second, landing on the SAME path, is refused as today.
        _agent_writing_to(monkeypatch, str(evidential))
        with ExecutorState(second) as state:
            result_two = run_red_phase(_task(), second, state)
        assert result_two.outcome is RedOutcome.UNVERIFIABLE
        assert "already existed" in (result_two.detail or "")
