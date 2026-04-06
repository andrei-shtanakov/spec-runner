"""Post-execution compliance verification.

Checks whether executed tasks satisfy the traceability chain:
requirements.md -> design.md -> tasks.md -> execution state.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .config import ExecutorConfig
from .state import ExecutorState
from .task import Task, parse_tasks


@dataclass
class VerifyResult:
    """Result of a compliance verification."""

    task_id: str
    task_name: str
    status: str  # done, failed, not_executed
    traces_to: list[str]
    tests_passed: bool | None = None
    review_verdict: str | None = None
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    issues: list[str] = field(default_factory=list)

    @property
    def compliant(self) -> bool:
        return self.status == "done" and not self.issues


@dataclass
class VerificationReport:
    """Overall verification report."""

    results: list[VerifyResult] = field(default_factory=list)
    uncovered_requirements: list[str] = field(default_factory=list)
    uncovered_designs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.compliant for r in self.results) and not self.uncovered_requirements

    @property
    def coverage(self) -> tuple[int, int]:
        """Return (covered, total) requirement count."""
        total = len(self.uncovered_requirements) + sum(
            1 for r in self.results for ref in r.traces_to if ref.startswith("REQ-")
        )
        covered = total - len(self.uncovered_requirements)
        return covered, total


def _extract_identifiers(text: str, prefix: str) -> list[str]:
    """Extract all [PREFIX-NNN] identifiers from markdown text."""
    pattern = rf"(?:####?)\s+({prefix}-\d+)"
    return re.findall(pattern, text)


def verify_task(
    task: Task,
    config: ExecutorConfig,
    state: ExecutorState,
) -> VerifyResult:
    """Verify a single task against its traceability chain."""
    result = VerifyResult(
        task_id=task.id,
        task_name=task.name,
        status=task.status,
        traces_to=list(task.traces_to),
    )

    ts = state.get_task_state(task.id)

    if task.status != "done":
        if ts and ts.status == "success":
            result.status = "done"
        elif ts and ts.status == "failed":
            result.status = "failed"
            result.issues.append("Task failed")
        else:
            result.status = "not_executed"
            result.issues.append("Task not yet executed")

    if ts:
        result.cost_usd = round(state.task_cost(task.id), 2)
        result.duration_seconds = round(sum(a.duration_seconds for a in ts.attempts), 1)
        if ts.attempts:
            last = ts.attempts[-1]
            result.review_verdict = last.review_status
            if last.review_status == "failed":
                result.issues.append("Code review failed")
            elif last.review_status == "rejected":
                result.issues.append("Code review rejected")

    if not task.traces_to:
        result.issues.append("No traceability references")

    return result


def verify_all(
    config: ExecutorConfig,
    task_id: str | None = None,
    strict: bool = False,
) -> VerificationReport:
    """Run verification for all done tasks or a specific task.

    Args:
        config: Executor configuration.
        task_id: Optional specific task to verify.
        strict: If True, missing traceability is an issue.

    Returns:
        VerificationReport with per-task results and uncovered requirements.
    """
    report = VerificationReport()
    tasks = parse_tasks(config.tasks_file) if config.tasks_file.exists() else []

    with ExecutorState(config) as state:
        # Verify requested tasks
        if task_id:
            matched = [t for t in tasks if t.id == task_id.upper()]
            if not matched:
                r = VerifyResult(
                    task_id=task_id,
                    task_name="(not found)",
                    status="not_found",
                    traces_to=[],
                    issues=["Task not found in tasks.md"],
                )
                report.results.append(r)
                return report
            targets = matched
        else:
            targets = [t for t in tasks if t.status == "done"]

        for task in targets:
            result = verify_task(task, config, state)
            if strict and not result.traces_to:
                result.issues.append("No traceability (strict mode)")
            report.results.append(result)

    # Find uncovered requirements
    all_traced_reqs: set[str] = set()
    for task in tasks:
        for ref in task.traces_to:
            if ref.startswith("REQ-"):
                all_traced_reqs.add(ref)

    if config.requirements_file.exists():
        req_text = config.requirements_file.read_text()
        all_reqs = set(_extract_identifiers(req_text, "REQ"))
        report.uncovered_requirements = sorted(all_reqs - all_traced_reqs)

    if config.design_file.exists():
        design_text = config.design_file.read_text()
        all_traced_designs: set[str] = set()
        for task in tasks:
            for ref in task.traces_to:
                if ref.startswith("DESIGN-"):
                    all_traced_designs.add(ref)
        all_designs = set(_extract_identifiers(design_text, "DESIGN"))
        report.uncovered_designs = sorted(all_designs - all_traced_designs)

    return report


def format_verify_text(report: VerificationReport) -> str:
    """Format verification report as human-readable text."""
    lines: list[str] = []
    lines.append("\n== Verification Report ==\n")

    for r in report.results:
        icon = "pass" if r.compliant else "FAIL"
        lines.append(f"[{icon}] {r.task_id}: {r.task_name}")
        if r.traces_to:
            lines.append(f"       Traces to: {', '.join(r.traces_to)}")
        if r.review_verdict:
            lines.append(f"       Review: {r.review_verdict}")
        if r.cost_usd > 0:
            lines.append(f"       Cost: ${r.cost_usd:.2f}  Duration: {r.duration_seconds:.0f}s")
        for issue in r.issues:
            lines.append(f"       !! {issue}")

    covered, total = report.coverage
    if total > 0:
        pct = covered * 100 // total
        lines.append(f"\nRequirement coverage: {covered}/{total} ({pct}%)")
    else:
        lines.append("\nNo requirements found in spec files.")

    if report.uncovered_requirements:
        lines.append(f"\nUncovered requirements: {', '.join(report.uncovered_requirements)}")
    if report.uncovered_designs:
        lines.append(f"Uncovered designs: {', '.join(report.uncovered_designs)}")

    status = "PASS" if report.ok else "FAIL"
    lines.append(f"\nOverall: {status}")
    return "\n".join(lines)


def format_verify_json(report: VerificationReport) -> str:
    """Format verification report as JSON."""
    covered, total = report.coverage
    data = {
        "ok": report.ok,
        "coverage": {"covered": covered, "total": total},
        "tasks": [
            {
                "task_id": r.task_id,
                "task_name": r.task_name,
                "compliant": r.compliant,
                "status": r.status,
                "traces_to": r.traces_to,
                "review_verdict": r.review_verdict,
                "cost_usd": r.cost_usd,
                "duration_seconds": r.duration_seconds,
                "issues": r.issues,
            }
            for r in report.results
        ],
        "uncovered_requirements": report.uncovered_requirements,
        "uncovered_designs": report.uncovered_designs,
    }
    return json.dumps(data, indent=2)
