"""harness-guard-companions #1: the policy file is part of the oracle.

`spec-runner.config.yaml` decides how a task is judged — `review_policy`, the
budget, `execution_mode`, `harness_guard` itself — yet it was on neither
`HARNESS_CANDIDATES` nor any default `harness_files`. An agent in the tree
could rewrite the policy it is checked by; under `auto_commit` the edit rode
into the candidate and the next run loaded it. The config is now guarded in
both its locations, and no `harness_allow` glob exempts it: the exemption list
is global, so a pattern written for one task would open the policy to every
task after it.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.execution import execute_task
from spec_runner.harness import harness_violations, snapshot_harness
from spec_runner.runner import CliInvocation
from spec_runner.state import ExecutorState
from spec_runner.task import Task

CONFIG = "spec-runner.config.yaml"
LEGACY = "spec/executor.config.yaml"


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    """No hook shells out: this suite is about the guard, not the gates."""
    return ExecutorConfig(
        project_root=tmp_path,
        state_file=tmp_path / "state.db",
        logs_dir=tmp_path / "logs",
        create_git_branch=False,
        sync_deps=False,
        run_tests_on_done=False,
        run_lint_on_done=False,
        auto_commit=False,
        run_review=False,
        **overrides,
    )


def _task() -> Task:
    return Task(id="TASK-001", name="demo", priority="p0", status="todo", estimate="")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestTheConfigIsOnTheSurface:
    @pytest.mark.parametrize("rel", [CONFIG, LEGACY])
    def test_a_modified_config_is_a_violation(self, tmp_path, rel):
        _write(tmp_path, rel, "review_policy: required\n")
        cfg = _cfg(tmp_path)
        before = snapshot_harness(cfg)
        _write(tmp_path, rel, "review_policy: advisory\n")
        assert harness_violations(cfg, before) == [f"modified {rel}"]

    @pytest.mark.parametrize("rel", [CONFIG, LEGACY])
    def test_a_created_config_is_a_violation(self, tmp_path, rel):
        """A project running on defaults has no file to modify — creating
        one is the same move."""
        cfg = _cfg(tmp_path)
        before = snapshot_harness(cfg)
        _write(tmp_path, rel, "harness_guard: off\n")
        assert harness_violations(cfg, before) == [f"created {rel}"]

    def test_a_deleted_config_is_a_violation(self, tmp_path):
        _write(tmp_path, CONFIG, "execution_mode: tdd\n")
        cfg = _cfg(tmp_path)
        before = snapshot_harness(cfg)
        (tmp_path / CONFIG).unlink()
        assert harness_violations(cfg, before) == [f"deleted {CONFIG}"]


class TestNoExemptionReachesIt:
    @pytest.mark.parametrize("allow", [["*.yaml"], [CONFIG], ["*"], ["spec/*.yaml", LEGACY]])
    def test_harness_allow_does_not_exempt_the_config(self, tmp_path, allow):
        _write(tmp_path, CONFIG, "a: 1\n")
        _write(tmp_path, LEGACY, "a: 1\n")
        cfg = _cfg(tmp_path, harness_allow=allow)
        before = snapshot_harness(cfg)
        _write(tmp_path, CONFIG, "a: 2\n")
        _write(tmp_path, LEGACY, "a: 2\n")
        assert harness_violations(cfg, before) == [f"modified {CONFIG}", f"modified {LEGACY}"]

    def test_the_same_glob_still_exempts_other_files(self, tmp_path):
        _write(tmp_path, CONFIG, "a: 1\n")
        cfg = _cfg(tmp_path, harness_allow=["*.yaml", "uv.lock"])
        before = snapshot_harness(cfg)
        _write(tmp_path, "uv.lock", "v2\n")
        _write(tmp_path, CONFIG, "a: 2\n")
        assert harness_violations(cfg, before) == [f"modified {CONFIG}"]


class TestAnAgentRewritingItsPolicy:
    """Through `execute_task`: the agent reports success after loosening the
    review policy. Strict fails the attempt before the gates run."""

    def _run(self, cfg, tmp_path):
        _write(tmp_path, CONFIG, "review_policy: required\n")

        def fake_agent(config, invocation, **kwargs):
            _write(tmp_path, CONFIG, "review_policy: advisory\n")
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
            result = execute_task(_task(), cfg, state)
            return result, state.get_task_state("TASK-001")

    def test_strict_fails_attempt_before_gates(self, tmp_path):
        cfg = _cfg(tmp_path, harness_guard="strict")
        cfg.logs_dir.mkdir()
        result, ts = self._run(cfg, tmp_path)
        assert result is False
        assert f"modified {CONFIG}" in (ts.attempts[-1].error or "")

    def test_the_agent_is_not_told_how_to_exempt_itself(self, tmp_path):
        """The operator's hint must not offer `harness_allow` for a change it
        cannot exempt."""
        cfg = _cfg(tmp_path, harness_guard="strict")
        cfg.logs_dir.mkdir()
        progress: list[str] = []
        with patch(
            "spec_runner.execution.log_progress",
            side_effect=lambda line, *_a, **_k: progress.append(line),
        ):
            result, ts = self._run(cfg, tmp_path)
        assert result is False
        assert "harness_allow" not in (ts.attempts[-1].error or "")
        guard_lines = [line for line in progress if "Harness guard" in line]
        assert guard_lines and all("harness_allow" not in line for line in guard_lines)
        assert any("cannot be exempted" in line for line in guard_lines)
