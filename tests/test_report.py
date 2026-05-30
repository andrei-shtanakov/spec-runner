"""Tests for spec_runner.report module."""

import json
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.report import (
    TraceabilityReport,
    TraceRow,
    build_report,
    format_report_json,
    format_report_markdown,
)
from spec_runner.state import ExecutorState


def _setup_project(tmp_path: Path, tasks_md: str, reqs_md: str = "", design_md: str = ""):
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "tasks.md").write_text(tasks_md)
    if reqs_md:
        (spec_dir / "requirements.md").write_text(reqs_md)
    if design_md:
        (spec_dir / "design.md").write_text(design_md)
    return ExecutorConfig(
        project_root=tmp_path,
        state_file=tmp_path / "state.db",
    )


TASKS_MD = """\
### TASK-001: Login endpoint
🔴 P0 | ✅ DONE

**Traces to:** [REQ-001], [DESIGN-001]

### TASK-002: Rate limiting
🟡 P2 | ⬜ TODO

**Traces to:** [REQ-002]

### TASK-003: Orphan task
🟢 P3 | ⬜ TODO
"""

REQUIREMENTS_MD = """\
#### REQ-001: User authentication
Users must be able to log in.

#### REQ-002: Rate limiting
API must enforce rate limits.

#### REQ-003: Audit logging
All actions must be logged.
"""

DESIGN_MD = """\
### DESIGN-001: JWT Auth
Design for [REQ-001].

### DESIGN-002: Rate limiter
Design for [REQ-002].
"""


class TestBuildReport:
    def test_basic_report(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD, DESIGN_MD)
        report = build_report(config)
        assert len(report.rows) == 3  # REQ-001, REQ-002, REQ-003

    def test_coverage(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD, DESIGN_MD)
        report = build_report(config)
        covered, total = report.coverage
        assert total == 3
        # REQ-001 and REQ-002 have tasks, REQ-003 does not
        assert covered == 2

    def test_uncovered_requirement(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD, DESIGN_MD)
        report = build_report(config)
        req3 = [r for r in report.rows if r.requirement == "REQ-003"]
        assert len(req3) == 1
        assert req3[0].status == "not covered"

    def test_design_mapping(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD, DESIGN_MD)
        report = build_report(config)
        req1 = [r for r in report.rows if r.requirement == "REQ-001"][0]
        assert "DESIGN-001" in req1.designs

    def test_task_mapping(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD, DESIGN_MD)
        report = build_report(config)
        req1 = [r for r in report.rows if r.requirement == "REQ-001"][0]
        assert "TASK-001" in req1.tasks

    def test_orphan_tasks(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD)
        report = build_report(config)
        assert "TASK-003" in report.orphan_tasks

    def test_uncovered_only_filter(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD, DESIGN_MD)
        report = build_report(config, uncovered_only=True)
        assert all(r.status == "not covered" for r in report.rows)

    def test_done_task_with_state(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD, DESIGN_MD)
        with ExecutorState(config) as state:
            state.record_attempt("TASK-001", True, 15.0, cost_usd=0.10, review_status="passed")

        report = build_report(config)
        req1 = [r for r in report.rows if r.requirement == "REQ-001"][0]
        assert req1.status == "done"
        assert req1.cost_usd == 0.10
        assert req1.review == "passed"

    def test_empty_project(self, tmp_path: Path):
        config = _setup_project(tmp_path, "# No tasks\n")
        report = build_report(config)
        assert len(report.rows) == 0


class TestFormatReport:
    def test_markdown_table(self):
        report = TraceabilityReport(
            rows=[
                TraceRow(
                    requirement="REQ-001",
                    designs=["DESIGN-001"],
                    tasks=["TASK-001"],
                    status="done",
                    cost_usd=0.12,
                    duration_seconds=15.0,
                    review="passed",
                )
            ]
        )
        md = format_report_markdown(report)
        assert "REQ-001" in md
        assert "DESIGN-001" in md
        assert "TASK-001" in md
        assert "$0.12" in md
        assert "Coverage: 1/1" in md

    def test_json_format(self):
        report = TraceabilityReport(
            rows=[TraceRow(requirement="REQ-001", status="done", cost_usd=0.5)],
            orphan_tasks=["TASK-003"],
        )
        data = json.loads(format_report_json(report))
        assert data["coverage"]["covered"] == 1
        assert data["rows"][0]["requirement"] == "REQ-001"
        assert "TASK-003" in data["orphan_tasks"]


class TestGapWarnings:
    """LABS-42: TraceabilityReport must flag uncovered requirements,
    unreferenced designs, and orphan tasks for CI consumption."""

    def test_uncovered_requirement_tracked(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD, DESIGN_MD)
        report = build_report(config)

        # REQ-003 (Audit logging) is defined but no task traces to it
        assert "REQ-003" in report.uncovered_requirements
        # REQ-001/002 are covered
        assert "REQ-001" not in report.uncovered_requirements
        assert "REQ-002" not in report.uncovered_requirements

    def test_unreferenced_design_tracked(self, tmp_path: Path):
        tasks_md = """\
### TASK-001: Login
🔴 P0 | ⬜ TODO
**Traces to:** [REQ-001], [DESIGN-001]

### TASK-002: Rate limit
🟠 P1 | ⬜ TODO
**Traces to:** [REQ-002]
"""
        config = _setup_project(tmp_path, tasks_md, REQUIREMENTS_MD, DESIGN_MD)
        report = build_report(config)

        # DESIGN-002 is defined in design.md but no task traces to it
        assert "DESIGN-002" in report.unreferenced_designs
        assert "DESIGN-001" not in report.unreferenced_designs

    def test_has_gaps_flag_set_when_any_gap_present(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD, DESIGN_MD)
        report = build_report(config)
        assert report.has_gaps is True

    def test_has_gaps_false_when_spec_is_clean(self, tmp_path: Path):
        clean_tasks = """\
### TASK-001: Cover everything
🔴 P0 | ⬜ TODO
**Traces to:** [REQ-001], [REQ-002], [REQ-003], [DESIGN-001], [DESIGN-002]
"""
        config = _setup_project(tmp_path, clean_tasks, REQUIREMENTS_MD, DESIGN_MD)
        report = build_report(config)
        assert report.has_gaps is False
        assert report.orphan_tasks == []
        assert report.uncovered_requirements == []
        assert report.unreferenced_designs == []

    def test_markdown_renders_gap_sections(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD, DESIGN_MD)
        report = build_report(config)
        md = format_report_markdown(report)

        assert "Orphan tasks" in md
        assert "TASK-003" in md
        assert "Uncovered requirements" in md
        assert "REQ-003" in md

    def test_json_exposes_has_gaps_and_lists(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD, DESIGN_MD)
        report = build_report(config)
        data = json.loads(format_report_json(report))

        assert data["has_gaps"] is True
        assert "TASK-003" in data["orphan_tasks"]
        assert "REQ-003" in data["uncovered_requirements"]
        assert isinstance(data["unreferenced_designs"], list)
