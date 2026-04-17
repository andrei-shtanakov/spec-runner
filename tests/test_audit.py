"""Tests for pre-execution compliance audit (LABS-37)."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

from spec_runner.audit import (
    CAT_DANGLING_DESIGN_REF,
    CAT_DANGLING_REQ_REF,
    CAT_DEAD_DESIGN,
    CAT_ORPHAN_TASK,
    CAT_UNCOVERED_DESIGN,
    CAT_UNCOVERED_REQ,
    audit_all,
    format_audit_csv,
    format_audit_json,
    format_audit_text,
)
from spec_runner.config import ExecutorConfig

# --- Fixture helpers ------------------------------------------------


TASKS_ALL_CLEAN = """\
# Tasks

## Milestone: MVP

### TASK-001: Bootstrap
🔴 P0 | ⬜ TODO
**Traces to:** [REQ-001], [DESIGN-001]
Est: 1d

- [ ] Init repo

### TASK-002: Add auth
🟠 P1 | ⬜ TODO
**Traces to:** [REQ-002], [DESIGN-002]
Est: 2d

- [ ] Login endpoint
"""

REQS_CLEAN = """\
### REQ-001: Project must exist

### REQ-002: Auth required
"""

DESIGN_CLEAN = """\
### DESIGN-001: Directory layout

Covers [REQ-001].

### DESIGN-002: JWT auth

Covers [REQ-002].
"""


def _write_specs(
    tmp_path: Path,
    tasks: str,
    requirements: str | None = None,
    design: str | None = None,
) -> ExecutorConfig:
    spec = tmp_path / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    (spec / "tasks.md").write_text(tasks)
    if requirements is not None:
        (spec / "requirements.md").write_text(requirements)
    if design is not None:
        (spec / "design.md").write_text(design)
    return ExecutorConfig(project_root=tmp_path)


# --- Happy path -----------------------------------------------------


class TestAuditClean:
    def test_clean_spec_has_no_findings(self, tmp_path):
        config = _write_specs(tmp_path, TASKS_ALL_CLEAN, REQS_CLEAN, DESIGN_CLEAN)
        report = audit_all(config)
        assert report.findings == []
        assert report.ok is True


# --- Single-category failures ---------------------------------------


class TestOrphanTasks:
    def test_task_without_traces_is_flagged(self, tmp_path):
        tasks = """\
### TASK-001: Rogue work
🔴 P0 | ⬜ TODO
Est: 1d
"""
        config = _write_specs(tmp_path, tasks)
        report = audit_all(config)

        orphans = [f for f in report.findings if f.category == CAT_ORPHAN_TASK]
        assert len(orphans) == 1
        assert orphans[0].subject == "TASK-001"
        assert orphans[0].severity == "warning"

    def test_orphan_is_warning_but_becomes_failure_in_strict(self, tmp_path):
        tasks = """\
### TASK-001: Rogue
🔴 P0 | ⬜ TODO
"""
        config = _write_specs(tmp_path, tasks)
        report_lenient = audit_all(config)
        report_strict = audit_all(config, strict=True)

        assert report_lenient.ok is True
        assert report_strict.ok is False


class TestDanglingReferences:
    def test_task_refs_missing_req_is_error(self, tmp_path):
        tasks = """\
### TASK-001: Needs ghost req
🔴 P0 | ⬜ TODO
**Traces to:** [REQ-999], [DESIGN-001]
"""
        config = _write_specs(tmp_path, tasks, REQS_CLEAN, DESIGN_CLEAN)
        report = audit_all(config)

        dangling = [f for f in report.findings if f.category == CAT_DANGLING_REQ_REF]
        assert len(dangling) == 1
        assert dangling[0].subject == "REQ-999"
        assert dangling[0].severity == "error"
        assert report.ok is False

    def test_task_refs_missing_design_is_error(self, tmp_path):
        tasks = """\
### TASK-001: Needs ghost design
🔴 P0 | ⬜ TODO
**Traces to:** [REQ-001], [DESIGN-999]
"""
        config = _write_specs(tmp_path, tasks, REQS_CLEAN, DESIGN_CLEAN)
        report = audit_all(config)

        dangling = [f for f in report.findings if f.category == CAT_DANGLING_DESIGN_REF]
        assert len(dangling) == 1
        assert dangling[0].subject == "DESIGN-999"
        assert report.ok is False


class TestUncoveredSpec:
    def test_unreferenced_req_is_warning(self, tmp_path):
        tasks = """\
### TASK-001: Covers one req
🔴 P0 | ⬜ TODO
**Traces to:** [REQ-001]
"""
        config = _write_specs(tmp_path, tasks, REQS_CLEAN, DESIGN_CLEAN)
        report = audit_all(config)

        uncovered = [f for f in report.findings if f.category == CAT_UNCOVERED_REQ]
        assert {f.subject for f in uncovered} == {"REQ-002"}
        assert all(f.severity == "warning" for f in uncovered)

    def test_unreferenced_design_is_warning(self, tmp_path):
        tasks = """\
### TASK-001: Covers design 1
🔴 P0 | ⬜ TODO
**Traces to:** [REQ-001], [DESIGN-001]

### TASK-002: Covers req 2 but no design
🟠 P1 | ⬜ TODO
**Traces to:** [REQ-002]
"""
        config = _write_specs(tmp_path, tasks, REQS_CLEAN, DESIGN_CLEAN)
        report = audit_all(config)

        uncovered_designs = [
            f for f in report.findings if f.category == CAT_UNCOVERED_DESIGN
        ]
        assert {f.subject for f in uncovered_designs} == {"DESIGN-002"}


class TestDeadDesign:
    def test_design_refs_missing_req_is_warning(self, tmp_path):
        design = """\
### DESIGN-001: Orphan design

Covers [REQ-001] and [REQ-404].
"""
        config = _write_specs(tmp_path, TASKS_ALL_CLEAN, REQS_CLEAN, design)
        report = audit_all(config)

        dead = [f for f in report.findings if f.category == CAT_DEAD_DESIGN]
        assert len(dead) == 1
        assert dead[0].subject == "DESIGN-001"
        assert "REQ-404" in dead[0].message
        assert dead[0].severity == "warning"


# --- Missing-file tolerance -----------------------------------------


class TestMissingSpecFiles:
    def test_no_requirements_file_skips_req_checks(self, tmp_path):
        config = _write_specs(tmp_path, TASKS_ALL_CLEAN)  # no reqs, no design
        report = audit_all(config)

        # Only orphan checks apply when reqs/design are absent. TASK-001 has
        # traces_to, so no orphan. Nothing dangling to flag either.
        assert all(
            f.category not in {CAT_DANGLING_REQ_REF, CAT_DANGLING_DESIGN_REF}
            for f in report.findings
        )
        assert report.ok is True

    def test_no_tasks_file_does_not_crash(self, tmp_path):
        # No tasks.md at all.
        config = ExecutorConfig(project_root=tmp_path)
        report = audit_all(config)
        # No tasks → no orphans, no dangling refs. Everything in spec files
        # (if they exist) would appear as uncovered, but they don't either.
        assert report.ok is True


# --- Multi-category aggregation -------------------------------------


class TestAggregation:
    def test_multiple_categories_in_one_run(self, tmp_path):
        tasks = """\
### TASK-001: Orphan task
🔴 P0 | ⬜ TODO

### TASK-002: Dangling ref
🟠 P1 | ⬜ TODO
**Traces to:** [REQ-404]
"""
        reqs = "### REQ-001: Exists"
        config = _write_specs(tmp_path, tasks, reqs)

        report = audit_all(config)
        cats = {f.category for f in report.findings}

        assert CAT_ORPHAN_TASK in cats
        assert CAT_DANGLING_REQ_REF in cats
        assert CAT_UNCOVERED_REQ in cats  # REQ-001 is not referenced by any task
        assert report.ok is False  # dangling is an error

    def test_counts_property_summarizes_categories(self, tmp_path):
        tasks = """\
### TASK-001: Orphan
🔴 P0 | ⬜ TODO

### TASK-002: Also orphan
🟠 P1 | ⬜ TODO
"""
        config = _write_specs(tmp_path, tasks)
        report = audit_all(config)
        assert report.counts == {CAT_ORPHAN_TASK: 2}


# --- Output formatters ----------------------------------------------


class TestFormatters:
    def test_text_format_lists_findings_by_severity(self, tmp_path):
        tasks = """\
### TASK-001: Orphan
🔴 P0 | ⬜ TODO

### TASK-002: Dangling ref
🟠 P1 | ⬜ TODO
**Traces to:** [REQ-404]
"""
        config = _write_specs(tmp_path, tasks, "### REQ-001: Exists")
        report = audit_all(config)
        out = format_audit_text(report)

        assert "Audit Report" in out
        assert "ERROR" in out or "error" in out
        assert "orphan_task" in out
        assert "FAIL" in out

    def test_text_format_shows_pass_when_clean(self, tmp_path):
        config = _write_specs(tmp_path, TASKS_ALL_CLEAN, REQS_CLEAN, DESIGN_CLEAN)
        out = format_audit_text(audit_all(config))
        assert "OK" in out or "no audit issues" in out

    def test_json_format_is_parseable(self, tmp_path):
        tasks = """\
### TASK-001: Orphan
🔴 P0 | ⬜ TODO
"""
        config = _write_specs(tmp_path, tasks)
        report = audit_all(config)
        payload = json.loads(format_audit_json(report))

        assert payload["ok"] is True  # orphan is warning only
        assert payload["strict"] is False
        assert payload["counts"] == {CAT_ORPHAN_TASK: 1}
        assert len(payload["findings"]) == 1
        entry = payload["findings"][0]
        assert entry["category"] == CAT_ORPHAN_TASK
        assert entry["subject"] == "TASK-001"
        assert entry["severity"] == "warning"
        assert "message" in entry
        assert "location" in entry

    def test_json_strict_propagates(self, tmp_path):
        tasks = """\
### TASK-001: Orphan
🔴 P0 | ⬜ TODO
"""
        config = _write_specs(tmp_path, tasks)
        report = audit_all(config, strict=True)
        payload = json.loads(format_audit_json(report))

        assert payload["strict"] is True
        assert payload["ok"] is False

    def test_csv_format_has_header_and_rows(self, tmp_path):
        tasks = """\
### TASK-001: Orphan
🔴 P0 | ⬜ TODO
"""
        config = _write_specs(tmp_path, tasks)
        report = audit_all(config)

        csv_out = format_audit_csv(report)
        reader = csv.reader(io.StringIO(csv_out))
        rows = list(reader)

        assert rows[0] == ["severity", "category", "subject", "location", "message"]
        assert len(rows) == 2
        assert rows[1][0] == "warning"
        assert rows[1][1] == CAT_ORPHAN_TASK
        assert rows[1][2] == "TASK-001"
