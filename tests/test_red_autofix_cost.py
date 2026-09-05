"""Acceptance test for BEH-23 (spec-runner#341, TASK-014).

`Given` a live, in-process reproduction of the burned scenario #341 — a
red-file whose lint findings are fully clearable by the declared fix command
(TASK-001/TASK-004's scenario) — run under pytest, with no real agent CLI
invoked (`tdd._run_agent` is monkeypatched).
`When` the run completes.
`Then` one paid RED session reaches a checkpoint where the scenario used to
burn an attempt, the number of extra agent rounds stays within BEH-05's
ceiling, and the live measurement — timed and priced in-process, never a
separate script — compares favorably to the documented baseline (5.5 minutes
and one burned attempt on TASK-004; $0.88 on TASK-001, charter AC-11).
`And` the measurement, with its own actual numbers rather than only the
baseline's constants, is recorded into the workstream's tracked measurements
artifact — a separate concern from the property-only red (TASK-014's
red-design frame, owner decision 2026-09-05).

Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-23
`checked_by`: kind=e2e, owner=qa, target=tests/test_red_autofix_cost.py
"""

import json
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

REPO_ROOT = Path(__file__).resolve().parents[1]
MEASUREMENTS_DIR = REPO_ROOT / "workstreams" / "WS-spec-runner-341" / "measurements"
ARTIFACT_PATH = MEASUREMENTS_DIR / "task-014-scenario-341-cost.json"

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


@pytest.fixture
def update_golden(request) -> bool:
    return bool(request.config.getoption("--update-golden"))


class TestScenario341CostMeasuredLiveUnderPytest:
    def test_the_measurement_is_recorded_and_compares_favorably_to_the_baseline(
        self, tmp_path_factory, monkeypatch, update_golden
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
        # agent involved, and reached a confirmed-red checkpoint where #341
        # used to burn the attempt (BEH-01/BEH-04 are already fixed).
        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
        assert result.checkpoint is not None

        measurement = tdd.ScenarioMeasurement(
            elapsed_seconds=elapsed,
            cost_usd=cost,
            paid_call_count=len(calls),
            checkpoint_reached=result.checkpoint is not None,
        )

        # Then: one paid RED session reaches the checkpoint. This scripted
        # scenario's lint finding is mechanically fixable, so no BEH-05 fix
        # round is needed — exactly one call, matching the frozen red.
        assert measurement.checkpoint_reached is True
        assert measurement.paid_call_count == 1

        # And: the live measurement compares favorably to the documented
        # baseline (5.5 minutes / one burned attempt on TASK-004; $0.88 on
        # TASK-001).
        assert measurement.elapsed_seconds < tdd.BASELINE_341.elapsed_seconds
        # The cost must be what actually round-tripped the ledger — the
        # scripted call's 0.05 — not merely "less than baseline": task_cost
        # returns 0.0 (never None) on broken accounting, and 0.0 < 0.88 would
        # publish a bookkeeping failure as record savings (#362 review).
        assert measurement.cost_usd == pytest.approx(0.05)
        assert 0 < measurement.cost_usd < tdd.BASELINE_341.cost_usd

        # And: the measurement — its own actual numbers, not only the
        # baseline's constants — is recorded into the workstream's tracked
        # measurements artifact. Writing a tracked file from a test is
        # opt-in by repo convention (--update-golden, conftest); the normal
        # run READS the committed artifact and checks it instead of
        # regenerating it (a read-back of one's own write proves nothing).
        if update_golden:
            MEASUREMENTS_DIR.mkdir(parents=True, exist_ok=True)
            ARTIFACT_PATH.write_text(
                json.dumps(
                    {
                        "task_id": "TASK-014",
                        "scenario": "spec-runner#341",
                        "elapsed_seconds": measurement.elapsed_seconds,
                        "cost_usd": measurement.cost_usd,
                        "paid_call_count": measurement.paid_call_count,
                        "checkpoint_reached": measurement.checkpoint_reached,
                        "baseline": {
                            "elapsed_seconds": tdd.BASELINE_341.elapsed_seconds,
                            "cost_usd": tdd.BASELINE_341.cost_usd,
                            "paid_call_count": tdd.BASELINE_341.paid_call_count,
                        },
                    },
                    indent=2,
                )
                + "\n"
            )

        # The COMMITTED artifact (a past real run's numbers) is checked for
        # schema and baseline consistency; its measured values must be real
        # positives that beat the baseline. Live-elapsed equality is not
        # asserted — wall-clock varies run to run by design.
        recorded = json.loads(ARTIFACT_PATH.read_text())
        assert recorded["task_id"] == "TASK-014"
        assert recorded["checkpoint_reached"] is True
        assert recorded["paid_call_count"] == 1
        assert 0 < recorded["cost_usd"] < tdd.BASELINE_341.cost_usd
        assert 0 < recorded["elapsed_seconds"] < tdd.BASELINE_341.elapsed_seconds
        assert recorded["baseline"]["elapsed_seconds"] == tdd.BASELINE_341.elapsed_seconds
        assert recorded["baseline"]["cost_usd"] == tdd.BASELINE_341.cost_usd
