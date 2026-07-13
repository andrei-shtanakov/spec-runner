"""Tests for the interactive `plan` [y/N/edit] confirmation handling.

Regression: choosing `edit` used to print "Edit tasks.md manually" without
writing the proposed tasks anywhere — with no pre-existing tasks.md there was
literally nothing to edit and the generated proposal was lost.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from spec_runner.cli_plan import apply_plan_confirmation


@pytest.fixture(autouse=True)
def _isolated_progress_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep `log_progress` writes out of the test runner's CWD."""
    monkeypatch.setattr("spec_runner.runner.PROGRESS_FILE", tmp_path / "progress.txt")


TASK_BLOCKS = [
    "TASK-001: First task\n- [ ] do a thing",
    "TASK-002: Second task\n- [ ] do another thing",
]


def _cfg(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(tasks_file=tmp_path / "spec" / "tasks.md")


def test_edit_writes_draft_before_opening_editor(tmp_path: Path):
    cfg = _cfg(tmp_path)
    seen: dict[str, object] = {}

    def fake_editor(path: Path) -> None:
        seen["path"] = path
        seen["content_at_open"] = path.read_text()

    apply_plan_confirmation("edit", TASK_BLOCKS, cfg, editor_fn=fake_editor)

    assert seen["path"] == cfg.tasks_file
    content_at_open = seen["content_at_open"]
    assert isinstance(content_at_open, str)
    assert "TASK-001" in content_at_open and "TASK-002" in content_at_open
    assert cfg.tasks_file.exists()


def test_edit_appends_to_existing_tasks_file(tmp_path: Path):
    cfg = _cfg(tmp_path)
    cfg.tasks_file.parent.mkdir(parents=True)
    cfg.tasks_file.write_text("# Tasks\n\n### TASK-000: Existing\n")

    apply_plan_confirmation("edit", TASK_BLOCKS, cfg, editor_fn=lambda p: None)

    content = cfg.tasks_file.read_text()
    assert "TASK-000" in content
    assert "TASK-001" in content and "TASK-002" in content


def test_yes_writes_tasks_without_editor(tmp_path: Path):
    cfg = _cfg(tmp_path)

    def boom(path: Path) -> None:
        raise AssertionError("editor must not be launched on 'y'")

    apply_plan_confirmation("y", TASK_BLOCKS, cfg, editor_fn=boom)

    content = cfg.tasks_file.read_text()
    assert "TASK-001" in content and "TASK-002" in content


def test_cancel_writes_nothing(tmp_path: Path):
    cfg = _cfg(tmp_path)

    def boom(path: Path) -> None:
        raise AssertionError("editor must not be launched on cancel")

    apply_plan_confirmation("n", TASK_BLOCKS, cfg, editor_fn=boom)
    apply_plan_confirmation("", TASK_BLOCKS, cfg, editor_fn=boom)

    assert not cfg.tasks_file.exists()
