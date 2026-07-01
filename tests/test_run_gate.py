"""Tests for the `run` governance gate (spec_run_gate_ok)."""

from pathlib import Path
from types import SimpleNamespace

from spec_runner.cli import spec_run_gate_ok
from spec_runner.spec import SpecMeta, write_spec


def _cfg(tmp_path, governance):
    spec = tmp_path / "spec"
    return SimpleNamespace(
        spec_governance=governance,
        tasks_file=spec / "tasks.md",
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
