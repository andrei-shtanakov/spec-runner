"""Acceptance test for BEH-34 (spec-runner#367, TASK-014).

`Given` the eliminable subclass named by the charter (WS-spec-runner-367,
`00-charter.md`): the two WS-341 waiver probes whose cost the mechanism can
remove entirely — TASK-011 ($1.51) and TASK-017 ($4.29), summing to the
$5.80 / 2 paid calls BEH-34/NFR-01 names — reproduced here as two
`verify_first` tasks in a fixture repository, each declaring a
`**Verifies:**` group that is already green.
`When` both tasks run their green path live, in-process, under pytest, with
`spec_runner.tdd._run_agent` (the RED-authoring seam) spied on rather than
invoked.
`Then` the RED-authoring seam is never called for either task (zero paid
RED-phase calls across the whole class), both reach a green outcome, and the
live-measured numbers package into a `tdd.ScenarioMeasurement` that beats the
class's documented baseline (`tdd.BASELINE_367_CLASS`), the same way
`tdd.BASELINE_341` anchors the single-scenario measurement (NFR-01).
`And` the measurement is recorded into the workstream's tracked measurements
artifact, next to the WS-spec-runner-341 precedent — a read-back of a
committed past run, not a self-write regenerated on every pass.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-34
`checked_by`: kind=e2e, owner=qa, target=tests/test_verify_first_cost.py
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runner import tdd
from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.live_verify import reusable_verify_evidence
from spec_runner.runner import CliInvocation
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import resolve_namespace

REPO_ROOT = Path(__file__).resolve().parents[1]
MEASUREMENTS_DIR = REPO_ROOT / "workstreams" / "WS-spec-runner-367" / "measurements"
ARTIFACT_PATH = MEASUREMENTS_DIR / "task-014-scenario-367-class-cost.json"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    tests_dir = root / "tests"
    tests_dir.mkdir()
    # Two already-green groups: existing green coverage for both probes of
    # the eliminable subclass, exactly as the charter describes it.
    (tests_dir / "test_probe_011.py").write_text("def test_it():\n    assert True\n")
    (tests_dir / "test_probe_017.py").write_text("def test_it():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _cfg(root: Path) -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        test_command="python -m pytest",
        max_retries=1,
        retry_delay_seconds=0,
        create_git_branch=False,
        run_tests_on_done=False,
        auto_commit=True,
        run_review=False,
        callback_url="",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _probe_task(task_id: str, group: str) -> Task:
    return Task(
        id=task_id,
        name=f"verify-first probe {task_id}",
        priority="p1",
        status="todo",
        estimate="1h",
        execution_mode="verify_first",
        verifies=[group],
    )


@pytest.fixture
def update_golden(request) -> bool:
    return bool(request.config.getoption("--update-golden"))


class TestScenario367ClassCostMeasuredLiveUnderPytest:
    """kind: e2e — BEH-34: the eliminable subclass's RED-phase cost is
    eliminated on the green path, the live measurement compares favorably to
    the $5.80 / two-call baseline, and is recorded into the tracked
    measurements artifact."""

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
    def test_the_measurement_is_recorded_and_beats_the_58_dollar_baseline(
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
        update_golden,
    ):
        root = _repo(tmp_path)
        mock_run.return_value = MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)

        red_authoring_calls: list[str] = []

        def _spy_run_agent(config, prompt, **kwargs):
            red_authoring_calls.append(config.project_root and "called")
            return tdd.AgentCall(text="TDD_SELECTOR: tests/should_not_exist.py::never")

        monkeypatch.setattr(tdd, "_run_agent", _spy_run_agent)

        probes = [
            _probe_task("TASK-011", "tests/test_probe_011.py::test_it"),
            _probe_task("TASK-017", "tests/test_probe_017.py::test_it"),
        ]

        cfg = _cfg(root)
        state = ExecutorState(cfg)
        namespace = resolve_namespace(cfg)

        start = time.perf_counter()
        for task in probes:
            outcome = execute_task(task, cfg, state)
            assert outcome is not False, f"{task.id} did not reach a completed outcome"
        elapsed = time.perf_counter() - start

        cost = sum(state.task_cost(task.id) for task in probes)
        for task in probes:
            evidence = state.verify_evidence(namespace, task.id)
            assert evidence is not None, f"{task.id} left no verify-evidence (BEH-15)"
            assert evidence.outcome == "green", f"{task.id} did not take the green path"
            assert reusable_verify_evidence(cfg, state, task) is not None
        state.close()

        # Then (BEH-34/FR-13): zero paid RED-authoring calls for the whole
        # class — not merely for one task.
        assert red_authoring_calls == []

        measurement = tdd.ScenarioMeasurement(
            elapsed_seconds=elapsed,
            cost_usd=cost,
            paid_call_count=len(red_authoring_calls),
            checkpoint_reached=True,
        )

        # And (NFR-01): the live-measured class beats the class's own
        # documented baseline point — 2 unproductive probes, $5.80.
        assert measurement.paid_call_count == 0
        assert measurement.paid_call_count < tdd.BASELINE_367_CLASS.paid_call_count
        assert measurement.cost_usd is not None
        # The cost must be what actually round-tripped the ledger — nothing
        # ran a paid call, so it is exactly 0.0, not merely "< baseline":
        # task_cost never returns None, and treating an unrelated 0.0 as
        # savings would publish a bookkeeping failure as a win (#362 review).
        assert measurement.cost_usd == pytest.approx(0.0)
        assert measurement.cost_usd < tdd.BASELINE_367_CLASS.cost_usd

        # And: the measurement — its own actual numbers, not only the
        # baseline's constants — is recorded into the workstream's tracked
        # measurements artifact, next to the WS-spec-runner-341 precedent.
        # Writing a tracked file from a test is opt-in by repo convention
        # (--update-golden, conftest); the normal run READS the committed
        # artifact and checks it instead of regenerating it.
        if update_golden:
            MEASUREMENTS_DIR.mkdir(parents=True, exist_ok=True)
            ARTIFACT_PATH.write_text(
                json.dumps(
                    {
                        "task_id": "TASK-014",
                        "scenario": "spec-runner#367-class",
                        "elapsed_seconds": measurement.elapsed_seconds,
                        "cost_usd": measurement.cost_usd,
                        "paid_call_count": measurement.paid_call_count,
                        "checkpoint_reached": measurement.checkpoint_reached,
                        "baseline": {
                            "elapsed_seconds": tdd.BASELINE_367_CLASS.elapsed_seconds,
                            "cost_usd": tdd.BASELINE_367_CLASS.cost_usd,
                            "paid_call_count": tdd.BASELINE_367_CLASS.paid_call_count,
                        },
                    },
                    indent=2,
                )
                + "\n"
            )

        recorded = json.loads(ARTIFACT_PATH.read_text())
        assert recorded["task_id"] == "TASK-014"
        assert recorded["checkpoint_reached"] is True
        assert recorded["paid_call_count"] == 0
        assert recorded["cost_usd"] == pytest.approx(0.0)
        assert recorded["cost_usd"] < tdd.BASELINE_367_CLASS.cost_usd
        assert recorded["paid_call_count"] < tdd.BASELINE_367_CLASS.paid_call_count
        assert recorded["baseline"]["cost_usd"] == tdd.BASELINE_367_CLASS.cost_usd
        assert recorded["baseline"]["paid_call_count"] == tdd.BASELINE_367_CLASS.paid_call_count
