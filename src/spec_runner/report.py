"""Traceability matrix report.

Maps requirements -> design decisions -> tasks -> execution state
to produce a full pipeline visibility report.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .config import ExecutorConfig
from .state import ExecutorState
from .task import Task, parse_tasks, resolve_dependencies


@dataclass
class TraceRow:
    """One row of the traceability matrix."""

    requirement: str
    designs: list[str] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)
    status: str = "not covered"
    duration_seconds: float = 0.0
    cost_usd: float = 0.0
    review: str = ""


@dataclass
class TraceabilityReport:
    """Full traceability matrix."""

    rows: list[TraceRow] = field(default_factory=list)
    orphan_tasks: list[str] = field(default_factory=list)  # Tasks with no traceability

    @property
    def coverage(self) -> tuple[int, int]:
        total = len(self.rows)
        covered = sum(1 for r in self.rows if r.status != "not covered")
        return covered, total


def _extract_section_ids(text: str, prefix: str) -> list[str]:
    """Extract identifiers like REQ-001, DESIGN-002 from section headers."""
    return re.findall(rf"(?:####?)\s+({prefix}-\d+)", text)


def _extract_design_to_req_map(design_text: str) -> dict[str, list[str]]:
    """Parse design.md to find which DESIGN-XXX references which REQ-XXX."""
    mapping: dict[str, list[str]] = {}
    current_design: str | None = None
    for line in design_text.splitlines():
        dm = re.match(r"^###\s+(DESIGN-\d+)", line)
        if dm:
            current_design = dm.group(1)
            mapping[current_design] = []
        if current_design:
            refs = re.findall(r"\[REQ-\d+\]", line)
            for ref in refs:
                req_id = ref.strip("[]")
                if req_id not in mapping[current_design]:
                    mapping[current_design].append(req_id)
    return mapping


def build_report(
    config: ExecutorConfig,
    milestone: str | None = None,
    status_filter: str | None = None,
    uncovered_only: bool = False,
) -> TraceabilityReport:
    """Build a traceability matrix from specs and execution state.

    Args:
        config: Executor configuration.
        milestone: Filter tasks by milestone.
        status_filter: Filter rows by task status.
        uncovered_only: Show only uncovered requirements.

    Returns:
        TraceabilityReport with requirement->design->task->state mapping.
    """
    report = TraceabilityReport()
    tasks = parse_tasks(config.tasks_file) if config.tasks_file.exists() else []
    tasks = resolve_dependencies(tasks)

    if milestone:
        tasks = [t for t in tasks if milestone.lower() in t.milestone.lower()]

    # Extract all requirements from requirements.md
    all_reqs: list[str] = []
    if config.requirements_file.exists():
        req_text = config.requirements_file.read_text()
        all_reqs = _extract_section_ids(req_text, "REQ")

    # Parse design->requirement mapping
    design_to_req: dict[str, list[str]] = {}
    if config.design_file.exists():
        design_text = config.design_file.read_text()
        design_to_req = _extract_design_to_req_map(design_text)

    # Invert: req -> designs
    req_to_designs: dict[str, list[str]] = {}
    for design_id, req_ids in design_to_req.items():
        for req_id in req_ids:
            req_to_designs.setdefault(req_id, []).append(design_id)

    # Map: req -> tasks (via traces_to)
    req_to_tasks: dict[str, list[Task]] = {}
    for task in tasks:
        for ref in task.traces_to:
            if ref.startswith("REQ-"):
                req_to_tasks.setdefault(ref, []).append(task)

    # Find orphan tasks (no traceability)
    report.orphan_tasks = [t.id for t in tasks if not t.traces_to]

    # Build rows
    with ExecutorState(config) as state:
        for req_id in all_reqs:
            row = TraceRow(
                requirement=req_id,
                designs=req_to_designs.get(req_id, []),
                tasks=[t.id for t in req_to_tasks.get(req_id, [])],
            )

            # Determine status from tasks
            task_list = req_to_tasks.get(req_id, [])
            if not task_list:
                row.status = "not covered"
            else:
                statuses = set()
                for t in task_list:
                    ts = state.get_task_state(t.id)
                    if ts and ts.status == "success":
                        statuses.add("done")
                        row.cost_usd += state.task_cost(t.id)
                        row.duration_seconds += sum(a.duration_seconds for a in ts.attempts)
                        if ts.attempts and ts.attempts[-1].review_status:
                            row.review = ts.attempts[-1].review_status
                    elif ts and ts.status == "failed":
                        statuses.add("failed")
                    elif t.status == "in_progress":
                        statuses.add("in_progress")
                    else:
                        statuses.add("todo")

                if "done" in statuses and len(statuses) == 1:
                    row.status = "done"
                elif "failed" in statuses:
                    row.status = "failed"
                elif "in_progress" in statuses:
                    row.status = "in_progress"
                else:
                    row.status = "todo"

            row.cost_usd = round(row.cost_usd, 2)
            row.duration_seconds = round(row.duration_seconds, 1)

            if status_filter and row.status != status_filter:
                continue
            if uncovered_only and row.status != "not covered":
                continue

            report.rows.append(row)

    return report


def format_report_markdown(report: TraceabilityReport) -> str:
    """Format traceability report as markdown table."""
    lines: list[str] = []
    lines.append("\n## Traceability Matrix\n")
    lines.append("| Requirement | Design | Task | Status | Duration | Cost | Review |")
    lines.append("|-------------|--------|------|--------|----------|------|--------|")

    for row in report.rows:
        designs = ", ".join(row.designs) if row.designs else "\u2014"
        tasks = ", ".join(row.tasks) if row.tasks else "\u2014"
        dur = f"{row.duration_seconds:.0f}s" if row.duration_seconds > 0 else "\u2014"
        cost = f"${row.cost_usd:.2f}" if row.cost_usd > 0 else "\u2014"
        review = row.review or "\u2014"
        lines.append(
            f"| {row.requirement} | {designs} | {tasks} | {row.status} "
            f"| {dur} | {cost} | {review} |"
        )

    covered, total = report.coverage
    if total > 0:
        pct = covered * 100 // total
        lines.append(f"\n**Coverage: {covered}/{total} requirements ({pct}%)**")
    else:
        lines.append("\nNo requirements found.")

    if report.orphan_tasks:
        lines.append(f"\n**Orphan tasks** (no traceability): {', '.join(report.orphan_tasks)}")

    return "\n".join(lines)


def format_report_json(report: TraceabilityReport) -> str:
    """Format traceability report as JSON."""
    covered, total = report.coverage
    data = {
        "coverage": {"covered": covered, "total": total},
        "rows": [
            {
                "requirement": r.requirement,
                "designs": r.designs,
                "tasks": r.tasks,
                "status": r.status,
                "duration_seconds": r.duration_seconds,
                "cost_usd": r.cost_usd,
                "review": r.review,
            }
            for r in report.rows
        ],
        "orphan_tasks": report.orphan_tasks,
    }
    return json.dumps(data, indent=2)
