"""Tests for spec_runner.verify module."""

import json
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.verify import (
    VerificationReport,
    VerifyResult,
    format_verify_json,
    format_verify_text,
    verify_all,
)


def _setup_project(tmp_path: Path, tasks_md: str, reqs_md: str = "", design_md: str = ""):
    """Create a minimal spec-runner project structure."""
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
🟢 P3 | ✅ DONE
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


class TestVerifyAll:
    def test_verify_done_tasks(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD, DESIGN_MD)
        with ExecutorState(config) as state:
            state.record_attempt("TASK-001", True, 10.0, review_status="passed")

        report = verify_all(config)
        # Only done tasks are verified by default
        assert len(report.results) == 2  # TASK-001 and TASK-003 are done
        task_001 = [r for r in report.results if r.task_id == "TASK-001"][0]
        assert task_001.compliant
        assert task_001.review_verdict == "passed"

    def test_verify_specific_task(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD)
        report = verify_all(config, task_id="TASK-002")
        assert len(report.results) == 1
        assert report.results[0].task_id == "TASK-002"
        assert not report.results[0].compliant  # not executed

    def test_verify_task_not_found(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD)
        report = verify_all(config, task_id="TASK-999")
        assert len(report.results) == 1
        assert "not found" in report.results[0].issues[0]

    def test_uncovered_requirements(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD)
        report = verify_all(config)
        # REQ-003 has no task tracing to it
        assert "REQ-003" in report.uncovered_requirements

    def test_strict_mode_flags_no_traceability(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD)
        report = verify_all(config, strict=True)
        task_003 = [r for r in report.results if r.task_id == "TASK-003"][0]
        assert any("strict" in i.lower() for i in task_003.issues)

    def test_failed_review_is_issue(self, tmp_path: Path):
        config = _setup_project(tmp_path, TASKS_MD, REQUIREMENTS_MD)
        with ExecutorState(config) as state:
            state.record_attempt("TASK-001", True, 10.0, review_status="failed")

        report = verify_all(config)
        task_001 = [r for r in report.results if r.task_id == "TASK-001"][0]
        assert any("review failed" in i.lower() for i in task_001.issues)


class TestVerifyResult:
    def test_compliant_when_done_no_issues(self):
        r = VerifyResult(task_id="T", task_name="T", status="done", traces_to=["REQ-001"])
        assert r.compliant

    def test_not_compliant_with_issues(self):
        r = VerifyResult(
            task_id="T", task_name="T", status="done", traces_to=[], issues=["problem"]
        )
        assert not r.compliant

    def test_not_compliant_if_not_done(self):
        r = VerifyResult(
            task_id="T", task_name="T", status="failed", traces_to=[], issues=["Task failed"]
        )
        assert not r.compliant


class TestVerifyFormat:
    def test_text_format(self):
        report = VerificationReport(
            results=[
                VerifyResult(
                    task_id="TASK-001", task_name="Login", status="done", traces_to=["REQ-001"]
                )
            ]
        )
        text = format_verify_text(report)
        assert "TASK-001" in text
        assert "PASS" in text

    def test_json_format(self):
        report = VerificationReport(
            results=[
                VerifyResult(
                    task_id="TASK-001",
                    task_name="Login",
                    status="done",
                    traces_to=["REQ-001"],
                    cost_usd=0.12,
                )
            ]
        )
        data = json.loads(format_verify_json(report))
        assert data["ok"] is True
        assert data["tasks"][0]["task_id"] == "TASK-001"
        assert data["tasks"][0]["cost_usd"] == 0.12
