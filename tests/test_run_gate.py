"""Tests for the `run` governance gate (spec_run_gate_ok)."""

from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from spec_runner.cli import cmd_retry, spec_run_gate_ok
from spec_runner.spec import SpecMeta, write_spec


def _cfg(tmp_path, governance):
    from spec_runner.spec import LITE

    spec = tmp_path / "spec"
    return SimpleNamespace(
        spec_governance=governance,
        tasks_file=spec / "tasks.md",
        resolve_spec_profile=lambda: LITE,
    )


def test_gate_off_always_allows(tmp_path: Path):
    cfg = _cfg(tmp_path, "off")
    write_spec(cfg.tasks_file, SpecMeta("tasks", "draft"), "x\n")
    ok, _ = spec_run_gate_ok(cfg)
    assert ok


def test_gate_strict_allows_unmanaged(tmp_path: Path):
    cfg = _cfg(tmp_path, "strict")
    cfg.tasks_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.tasks_file.write_text("### TASK-001: x\n")  # no frontmatter
    ok, _ = spec_run_gate_ok(cfg)
    assert ok


def test_gate_strict_blocks_draft(tmp_path: Path):
    cfg = _cfg(tmp_path, "strict")
    write_spec(cfg.tasks_file, SpecMeta("tasks", "draft"), "x\n")
    ok, reason = spec_run_gate_ok(cfg)
    assert not ok and "draft" in reason.lower()


def test_gate_strict_allows_approved(tmp_path: Path):
    cfg = _cfg(tmp_path, "strict")
    write_spec(cfg.tasks_file, SpecMeta("tasks", "approved"), "x\n")
    ok, _ = spec_run_gate_ok(cfg)
    assert ok


def test_gate_strict_allows_non_spec_frontmatter(tmp_path: Path):
    """A tasks.md carrying unrelated (non-spec) frontmatter must be treated as
    unmanaged and allowed through — not crash `spec_run_gate_ok` (Copilot PR#28)."""
    cfg = _cfg(tmp_path, "strict")
    cfg.tasks_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.tasks_file.write_text("---\ntitle: notes\n---\n# Tasks\n")
    ok, _ = spec_run_gate_ok(cfg)
    assert ok


def test_gate_strict_blocks_draft_under_custom_profile(tmp_path: Path, acceptance_profile):
    """A managed draft on a non-lite stage must not read as unmanaged (bypass)."""
    cfg = _cfg(tmp_path, "strict")
    cfg.resolve_spec_profile = lambda: acceptance_profile
    write_spec(cfg.tasks_file, SpecMeta("acceptance", "draft"), "x\n")

    ok, reason = spec_run_gate_ok(cfg)
    assert not ok, "draft on a custom-profile stage bypassed the governance gate"
    assert "draft" in reason.lower()


class TestRetryGovernanceGate:
    """`retry` must be gated by spec governance, same as `run`/`watch` (no bypass)."""

    @patch("spec_runner.cli.execute_task")
    @patch("spec_runner.cli.parse_tasks")
    def test_strict_governance_blocks_before_execution(
        self,
        mock_parse_tasks,
        mock_execute_task,
        tmp_path: Path,
    ) -> None:
        """A draft managed tasks.md under strict governance blocks retry
        entirely: no task parsing, no state, no execution."""
        cfg = _cfg(tmp_path, "strict")
        write_spec(cfg.tasks_file, SpecMeta("tasks", "draft"), "# Tasks\n")

        # #134: the refusal exits non-zero — a policy rejection must not look
        # like "there was nothing to retry" to a CI caller.
        with pytest.raises(SystemExit) as exc:
            cmd_retry(Namespace(task_id="task-001", fresh=False), cfg)
        assert exc.value.code == 1

        mock_parse_tasks.assert_not_called()
        mock_execute_task.assert_not_called()

    @patch("spec_runner.cli.ExecutorState")
    @patch("spec_runner.cli.execute_task")
    @patch("spec_runner.cli.parse_tasks")
    def test_off_governance_does_not_block(
        self,
        mock_parse_tasks,
        mock_execute_task,
        mock_state_cls,
        tmp_path: Path,
    ) -> None:
        """Default governance ('off') is a no-op: retry proceeds past the gate
        (task not found is a fine outcome here — the point is the gate itself
        does not short-circuit the call)."""
        cfg = _cfg(tmp_path, "off")
        write_spec(cfg.tasks_file, SpecMeta("tasks", "draft"), "# Tasks\n")
        mock_parse_tasks.return_value = []

        cmd_retry(Namespace(task_id="task-001", fresh=False), cfg)

        mock_parse_tasks.assert_called_once()
