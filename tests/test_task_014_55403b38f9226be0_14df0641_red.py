"""RED for TASK-014 (#367, BEH-34).

`Given` the eliminable subclass named by the charter (WS-spec-runner-367,
`00-charter.md`): the two WS-341 waiver probes whose cost the mechanism can
remove entirely — `TASK-011` ($1.51) and `TASK-017` ($4.29), summing to the
$5.80 BEH-34/NFR-01 names — reproduced here as two `verify_first` tasks in a
fixture repository, each declaring a `**Verifies:**` group that is already
green (existing green coverage, matching the charter's description of this
subclass).
`When` both tasks run their green path live, in-process, under pytest, with
`tdd._run_agent` (the RED-authoring seam) spied on rather than invoked.
`Then` the RED-authoring seam is never called for either task (zero paid
RED-phase calls across the whole class, not just one task), both reach a
green outcome, and the live-measured numbers package into a
`tdd.ScenarioMeasurement` that is checked against a baseline point for this
class — 2 unproductive probes, $5.80 — the same way `tdd.BASELINE_341`
anchors the single-scenario measurement (NFR-01, `tdd.py:163`).

Today `spec_runner.tdd` has no such baseline for the subclass: only
`BASELINE_341`, which anchors the *single fixed scenario* TASK-014 measured
under WS-spec-runner-341, exists. There is no shared, importable place to
hold "what the two-probe eliminable subclass used to cost" next to "what it
costs now" — so this test fails on `tdd.BASELINE_367_ELIMINABLE_SUBCLASS`, not
on the green-path mechanics themselves (already delivered by TASK-001..013/015
of this workstream).

Note (#387 review, finding 4): this constant names the eliminable *subclass*
(2 probes / $5.80, charter AC-11) that a live run is checked against — not the
full class the charter measured (5 probes / $10.26), which is kept only as
the class measurement, never as a comparison base.

Red-design frame (owner decision, 2026-09-05, carried over from the sibling
WS-341 red): a red asserting a *measurement artifact file* exists is invalid
twice over — a static file fakes the red, and an untracked artifact path
cannot survive `verify_red`'s commit-only worktree replay. This red performs
the measurement live, inside the test, and asserts on the resulting object's
properties only; recording it into an artifact next to
`workstreams/WS-spec-runner-341/measurements/` is a separate, later, green
concern.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from spec_runner import tdd
from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.live_verify import reusable_verify_evidence
from spec_runner.runner import CliInvocation
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import resolve_namespace


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


class TestScenario367ClassCostMeasuredLiveUnderPytest:
    """kind: e2e — BEH-34: the eliminable subclass's RED-phase cost is
    eliminated on the green path, and the live measurement of that class
    compares favorably to the $5.80 / two-probe baseline the charter
    recorded for it."""

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
    def test_the_class_pays_zero_red_calls_and_beats_the_58_dollar_baseline(
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

        # And (NFR-01): the live-measured subclass packages into the shared
        # `ScenarioMeasurement` precedent and beats the eliminable subclass's
        # own documented baseline point — 2 unproductive probes, $5.80
        # (charter, WS-spec-runner-367, AC-11) — not the unrelated
        # single-scenario `BASELINE_341` nor the full class figure (5 probes /
        # $10.26).
        measurement = tdd.ScenarioMeasurement(
            elapsed_seconds=elapsed,
            cost_usd=cost,
            paid_call_count=len(red_authoring_calls),
            checkpoint_reached=True,
        )

        assert measurement.paid_call_count == 0
        assert measurement.paid_call_count < tdd.BASELINE_367_ELIMINABLE_SUBCLASS.paid_call_count
        assert measurement.cost_usd is not None
        assert measurement.cost_usd < tdd.BASELINE_367_ELIMINABLE_SUBCLASS.cost_usd
        assert tdd.BASELINE_367_ELIMINABLE_SUBCLASS.cost_usd == 5.80
        assert tdd.BASELINE_367_ELIMINABLE_SUBCLASS.paid_call_count == 2
