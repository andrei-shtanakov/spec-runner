"""Harness-mutation tripwire (#64): the oracle must not be silently rewritable."""

import subprocess
from pathlib import Path
from unittest.mock import patch

from spec_runner.config import ExecutorConfig, build_config
from spec_runner.execution import execute_task
from spec_runner.harness import harness_violations, snapshot_harness
from spec_runner.runner import CliInvocation
from spec_runner.state import ExecutorState
from spec_runner.task import Task


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "state.db",
        "logs_dir": tmp_path / "logs",
        "create_git_branch": False,
        "sync_deps": False,
        "run_tests_on_done": False,
        "auto_commit": False,
        "run_review": False,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


class TestSnapshotAndDiff:
    def test_off_mode_skips_snapshotting(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        cfg = _cfg(tmp_path, harness_guard="off")
        assert snapshot_harness(cfg) is None
        assert harness_violations(cfg, None) == []

    def test_created_file_is_violation(self, tmp_path):
        cfg = _cfg(tmp_path)
        before = snapshot_harness(cfg)
        (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -p no:cacheprovider\n")
        assert harness_violations(cfg, before) == ["created pytest.ini"]

    def test_modified_file_is_violation(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        cfg = _cfg(tmp_path)
        before = snapshot_harness(cfg)
        (tmp_path / "pyproject.toml").write_text("[project]\nname='y'\n")
        assert harness_violations(cfg, before) == ["modified pyproject.toml"]

    def test_deleted_file_is_violation(self, tmp_path):
        (tmp_path / "tox.ini").write_text("[tox]\n")
        cfg = _cfg(tmp_path)
        before = snapshot_harness(cfg)
        (tmp_path / "tox.ini").unlink()
        assert harness_violations(cfg, before) == ["deleted tox.ini"]

    def test_untouched_harness_is_clean(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        cfg = _cfg(tmp_path)
        before = snapshot_harness(cfg)
        (tmp_path / "lib.py").write_text("x = 1\n")  # product code, not harness
        assert harness_violations(cfg, before) == []

    def test_workflow_dir_watched_recursively(self, tmp_path):
        cfg = _cfg(tmp_path)
        before = snapshot_harness(cfg)
        wf = tmp_path / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / "ci.yml").write_text("on: push\n")
        assert harness_violations(cfg, before) == ["created .github/workflows/ci.yml"]

    def test_harness_files_config_extends_surface(self, tmp_path):
        cfg = _cfg(tmp_path, harness_files=["scripts/verify.sh"])
        before = snapshot_harness(cfg)
        (tmp_path / "scripts").mkdir()
        (tmp_path / "scripts" / "verify.sh").write_text("#!/bin/sh\n")
        assert harness_violations(cfg, before) == ["created scripts/verify.sh"]

    def test_harness_allow_exempts_globs(self, tmp_path):
        (tmp_path / "uv.lock").write_text("v1\n")
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        cfg = _cfg(tmp_path, harness_allow=["uv.lock"])
        before = snapshot_harness(cfg)
        (tmp_path / "uv.lock").write_text("v2\n")
        (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = -x\n")
        assert harness_violations(cfg, before) == ["modified pytest.ini"]


class TestConfigPlumbing:
    def test_defaults(self):
        cfg = ExecutorConfig()
        assert cfg.harness_guard == "warn"
        assert cfg.harness_files == []
        assert cfg.harness_allow == []

    def test_yaml_keys_flow(self):
        cfg = build_config(
            {
                "harness_guard": "strict",
                "harness_files": ["scripts/verify.sh"],
                "harness_allow": ["uv.lock"],
            },
            args=None,
        )
        assert cfg.harness_guard == "strict"
        assert cfg.harness_files == ["scripts/verify.sh"]
        assert cfg.harness_allow == ["uv.lock"]


def _task() -> Task:
    return Task(
        id="TASK-001",
        name="demo",
        priority="p0",
        status="todo",
        estimate="",
        description="",
        checklist=[],
    )


class TestExecuteTaskIntegration:
    """The agent 'succeeds' but rewrote the oracle — strict fails, warn passes."""

    def _run(self, cfg: ExecutorConfig, tmp_path: Path):
        def fake_agent(argv, **kwargs):
            # The agent invents a pytest bridge (the kapelle move).
            (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --co\n")
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="TASK_COMPLETE\n", stderr=""
            )

        with (
            patch("spec_runner.execution.subprocess.run", side_effect=fake_agent),
            patch(
                "spec_runner.execution.build_cli_invocation",
                return_value=CliInvocation(["fake"], "text"),
            ),
            patch("spec_runner.execution.build_task_prompt", return_value="p"),
            patch("spec_runner.execution.update_task_status"),
            ExecutorState(cfg) as state,
        ):
            result = execute_task(_task(), cfg, state)
            ts = state.get_task_state("TASK-001")
            return result, ts

    def test_strict_fails_attempt_before_gates(self, tmp_path):
        cfg = _cfg(tmp_path, harness_guard="strict")
        cfg.logs_dir.mkdir()
        result, ts = self._run(cfg, tmp_path)
        assert result is False
        assert "Harness guard" in (ts.attempts[-1].error or "")
        assert "created pytest.ini" in (ts.attempts[-1].error or "")

    def test_warn_logs_but_succeeds(self, tmp_path):
        cfg = _cfg(tmp_path, harness_guard="warn")
        cfg.logs_dir.mkdir()
        result, ts = self._run(cfg, tmp_path)
        assert result is True
        assert ts.attempts[-1].success is True

    def test_off_ignores_mutations(self, tmp_path):
        cfg = _cfg(tmp_path, harness_guard="off")
        cfg.logs_dir.mkdir()
        result, _ = self._run(cfg, tmp_path)
        assert result is True
