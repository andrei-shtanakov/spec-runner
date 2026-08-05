"""`run --dry-run` checklist counting (#71): done must not equal total for untouched tasks."""

import json
from pathlib import Path

from spec_runner.cli import _print_dry_run
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task


def _task(checklist: list[tuple[str, bool]]) -> Task:
    return Task(
        id="TASK-002",
        name="demo",
        priority="p1",
        status="todo",
        estimate="",
        description="",
        checklist=checklist,
    )


def _dry_run_entry(tmp_path: Path, task: Task, capsys) -> dict:
    cfg = ExecutorConfig(project_root=tmp_path, create_git_branch=False)
    with ExecutorState(cfg) as state:
        _print_dry_run([task], cfg, state)
    return json.loads(capsys.readouterr().out)["tasks"][0]


class TestDryRunChecklistCount:
    def test_untouched_checklist_reports_zero_done(self, tmp_path, capsys):
        items: list[tuple[str, bool]] = [(f"item {i}", False) for i in range(5)]
        entry = _dry_run_entry(tmp_path, _task(items), capsys)
        assert entry["checklist_total"] == 5
        assert entry["checklist_done"] == 0

    def test_partial_checklist_counts_checked_only(self, tmp_path, capsys):
        items = [("a", True), ("b", False), ("c", True)]
        entry = _dry_run_entry(tmp_path, _task(items), capsys)
        assert entry["checklist_total"] == 3
        assert entry["checklist_done"] == 2
