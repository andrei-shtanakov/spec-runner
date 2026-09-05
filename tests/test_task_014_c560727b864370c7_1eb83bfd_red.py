"""RED for TASK-014 (spec-runner#341, BEH-23).

`Given` a live, in-process reproduction of the boевой scenario #341 — a
red-file whose lint findings are fully clearable by the declared fix command
(TASK-001/TASK-004's scenario, already fixed by this workstream) — run under
pytest, with no real agent CLI invoked (`tdd._run_agent` is monkeypatched).
`When` the run completes, this test times it in-process and reads the
resulting cost straight from `ExecutorState`, then packages both numbers
together with the outcome into a measurement and checks its properties
against the documented baseline: 5.5 minutes and one burned attempt on
TASK-004, $0.88 on TASK-001 (charter, WS-spec-runner-341, AC-11).

Red-design frame (owner decision, 2026-09-05, after two unusable reds): a
red that asserts a *measurement artifact file* exists is invalid twice over —
a static file fakes the red (nothing forces it to reflect a real run), and an
untracked artifact path cannot survive `verify_red`'s commit-only worktree
replay. This red instead performs the measurement live, inside the test, and
asserts on the resulting object's properties. Recording that measurement into
`workstreams/WS-spec-runner-341/measurements/` is a separate, later, green
test's job — this one checks properties only.

Today `spec_runner.tdd` has no `ScenarioMeasurement`/`BASELINE_341` — there is
no shared, importable place to hold "what we measured" next to "what #341
used to cost", so this test fails on `tdd.ScenarioMeasurement`, not on the
scenario itself (which already reaches a checkpoint — that part of #341 was
fixed by TASK-001..008 of this workstream; TASK-014 is the still-missing
measurement of it).
"""

import shlex
import subprocess
import sys
import time
from pathlib import Path

import pytest

from spec_runner import tdd
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import RedOutcome, run_red_phase

_CHECK_SCRIPT = """
import sys
from pathlib import Path

bad = any("BADWORD" in Path(p).read_text() for p in sys.argv[1:])
sys.exit(1 if bad else 0)
"""

_FIX_SCRIPT = """
import sys
from pathlib import Path

for p in sys.argv[1:]:
    path = Path(p)
    path.write_text(path.read_text().replace("BADWORD", ""))
"""


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _shell_command(script_path: Path) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script_path))}"


def _cfg(root: Path, lint_command: str, lint_fix_command: str) -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        execution_mode="tdd",
        test_command="python -m pytest",
        lint_command=lint_command,
        lint_command_declared=True,
        lint_fix_command=lint_fix_command,
        lint_fix_command_declared=True,
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task() -> Task:
    return Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")


def _scripted_red_authoring_call(monkeypatch, *, cost: float) -> None:
    """One paid RED-authoring call, writing a single mechanically-fixable finding."""

    def fake(config, prompt, **kwargs):
        path = Path(config.project_root) / "tests/test_x.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_y():  # BADWORD\n    assert False\n")
        return tdd.AgentCall(text="TDD_SELECTOR: tests/test_x.py::test_y", cost_usd=cost)

    monkeypatch.setattr(tdd, "_run_agent", fake)


class TestScenario341CostMeasuredLiveUnderPytest:
    def test_the_measurement_compares_favorably_to_the_burned_baseline(
        self, tmp_path_factory, monkeypatch
    ):
        root = _repo(tmp_path_factory.mktemp("proj"))
        scripts = tmp_path_factory.mktemp("scripts")
        check_script = scripts / "check_lint.py"
        check_script.write_text(_CHECK_SCRIPT)
        fix_script = scripts / "fix_lint.py"
        fix_script.write_text(_FIX_SCRIPT)

        cfg = _cfg(
            root,
            lint_command=_shell_command(check_script),
            lint_fix_command=_shell_command(fix_script),
        )
        _scripted_red_authoring_call(monkeypatch, cost=0.05)

        start = time.perf_counter()
        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            elapsed = time.perf_counter() - start
            cost = state.task_cost("TASK-001")
            calls = state.agent_calls("TASK-001")

        # Given/When: the scenario really ran, live, under pytest, no real
        # agent involved, and really reached a confirmed-red checkpoint —
        # #341's original burn (BEH-01/BEH-04) is fixed, this only measures it.
        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
        assert result.checkpoint is not None

        # Then/And (BEH-23): package the live numbers into a measurement and
        # check its properties against the documented baseline point.
        measurement = tdd.ScenarioMeasurement(
            elapsed_seconds=elapsed,
            cost_usd=cost,
            paid_call_count=len(calls),
            checkpoint_reached=result.checkpoint is not None,
        )

        assert measurement.checkpoint_reached is True
        assert measurement.paid_call_count == 1
        assert measurement.elapsed_seconds < tdd.BASELINE_341.elapsed_seconds
        # Bounded from below too: task_cost returns 0.0 (never None) when
        # accounting breaks, and a broken ledger must not read as savings.
        assert measurement.cost_usd == pytest.approx(0.05)
        assert 0 < measurement.cost_usd < tdd.BASELINE_341.cost_usd
