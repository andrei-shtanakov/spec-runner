"""Pre-execution compliance audit.

Runs **static** checks against the spec triangle — `tasks.md`,
`requirements.md`, `design.md` — before any task executes. Unlike
`verify`, audit does not look at execution state or cost; it only
validates the spec itself:

- tasks missing any traceability reference
- requirements defined in `requirements.md` that no task covers
- designs defined in `design.md` that no task covers
- tasks that reference a `REQ-XXX` / `DESIGN-XXX` identifier which is
  not defined in the spec files (dangling references)
- designs that reference a `REQ-XXX` not defined in `requirements.md`
  (dead designs)

Together these catch the common pre-run drift where someone adds a
new requirement or renames an ID but forgets to update the downstream
files. `spec-runner audit` is intended to run in CI before `run`.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from dataclasses import dataclass, field

from .config import ExecutorConfig
from .task import Task, parse_tasks

# Finding categories — stable strings used in JSON/CSV output.
CAT_ORPHAN_TASK = "orphan_task"
CAT_UNCOVERED_REQ = "uncovered_req"
CAT_UNCOVERED_DESIGN = "uncovered_design"
CAT_DANGLING_REQ_REF = "dangling_req_ref"
CAT_DANGLING_DESIGN_REF = "dangling_design_ref"
CAT_DEAD_DESIGN = "dead_design"


@dataclass
class AuditFinding:
    """One entry in the audit report."""

    severity: str  # "error" | "warning"
    category: str  # one of the CAT_* constants above
    subject: str  # e.g. "TASK-001" or "REQ-015"
    message: str
    location: str = ""  # optional human-readable pointer (file / task id)


@dataclass
class AuditReport:
    """Aggregate audit output."""

    findings: list[AuditFinding] = field(default_factory=list)
    strict: bool = False

    @property
    def ok(self) -> bool:
        """True if no errors (and, in strict mode, no warnings either)."""
        for f in self.findings:
            if f.severity == "error":
                return False
            if self.strict and f.severity == "warning":
                return False
        return True

    @property
    def counts(self) -> dict[str, int]:
        """Category → count mapping, handy for summary lines."""
        return dict(Counter(f.category for f in self.findings))


# --- Parsing helpers (mirror verify.py / report.py) -----------------


_ID_HEADER = r"(?:####?)\s+({prefix}-\d+)"


def _extract_ids(text: str, prefix: str) -> set[str]:
    """Extract all `### PREFIX-NNN` / `#### PREFIX-NNN` identifiers."""
    return set(re.findall(_ID_HEADER.format(prefix=prefix), text))


def _design_to_req_refs(design_text: str) -> dict[str, set[str]]:
    """Parse design.md and return `{DESIGN-XXX: {REQ-YYY, ...}}`.

    Each DESIGN section owns the `[REQ-YYY]` tokens that appear in its
    body until the next `### DESIGN-ZZZ` heading.
    """
    mapping: dict[str, set[str]] = {}
    current: str | None = None
    for line in design_text.splitlines():
        heading = re.match(r"^###\s+(DESIGN-\d+)", line)
        if heading:
            current = heading.group(1)
            mapping.setdefault(current, set())
            continue
        if current:
            for ref in re.findall(r"\[(REQ-\d+)\]", line):
                mapping[current].add(ref)
    return mapping


# --- Core audit -----------------------------------------------------


def audit_all(config: ExecutorConfig, *, strict: bool = False) -> AuditReport:
    """Run every static spec check and return an `AuditReport`.

    Missing spec files are *not* an audit failure: a project may have a
    `tasks.md` but no `design.md`, and that is a valid state. Each
    check simply skips when its inputs aren't available.
    """
    report = AuditReport(strict=strict)

    tasks: list[Task] = (
        parse_tasks(config.tasks_file) if config.tasks_file.exists() else []
    )

    req_ids: set[str] = set()
    if config.requirements_file.exists():
        req_ids = _extract_ids(config.requirements_file.read_text(), "REQ")

    design_ids: set[str] = set()
    design_to_reqs: dict[str, set[str]] = {}
    if config.design_file.exists():
        design_text = config.design_file.read_text()
        design_ids = _extract_ids(design_text, "DESIGN")
        design_to_reqs = _design_to_req_refs(design_text)

    # 1. Orphan tasks (no traceability at all)
    for task in tasks:
        if not task.traces_to:
            report.findings.append(
                AuditFinding(
                    severity="warning",
                    category=CAT_ORPHAN_TASK,
                    subject=task.id,
                    message="Task has no traceability references",
                    location=task.id,
                )
            )

    # 2/3. Dangling refs — task points at an ID that doesn't exist
    if req_ids or design_ids:
        for task in tasks:
            for ref in task.traces_to:
                if ref.startswith("REQ-") and req_ids and ref not in req_ids:
                    report.findings.append(
                        AuditFinding(
                            severity="error",
                            category=CAT_DANGLING_REQ_REF,
                            subject=ref,
                            message=(
                                f"{task.id} references {ref} but it is not "
                                "defined in requirements.md"
                            ),
                            location=task.id,
                        )
                    )
                elif ref.startswith("DESIGN-") and design_ids and ref not in design_ids:
                    report.findings.append(
                        AuditFinding(
                            severity="error",
                            category=CAT_DANGLING_DESIGN_REF,
                            subject=ref,
                            message=(
                                f"{task.id} references {ref} but it is not "
                                "defined in design.md"
                            ),
                            location=task.id,
                        )
                    )

    # 4. Uncovered requirements — REQ defined but no task references it
    if req_ids:
        covered_reqs = {
            ref for task in tasks for ref in task.traces_to if ref.startswith("REQ-")
        }
        for req in sorted(req_ids - covered_reqs):
            report.findings.append(
                AuditFinding(
                    severity="warning",
                    category=CAT_UNCOVERED_REQ,
                    subject=req,
                    message=f"{req} is defined but no task traces to it",
                    location="requirements.md",
                )
            )

    # 5. Uncovered designs — DESIGN defined but no task references it
    if design_ids:
        covered_designs = {
            ref
            for task in tasks
            for ref in task.traces_to
            if ref.startswith("DESIGN-")
        }
        for design in sorted(design_ids - covered_designs):
            report.findings.append(
                AuditFinding(
                    severity="warning",
                    category=CAT_UNCOVERED_DESIGN,
                    subject=design,
                    message=f"{design} is defined but no task traces to it",
                    location="design.md",
                )
            )

    # 6. Dead designs — DESIGN body references a non-existent REQ
    if req_ids and design_to_reqs:
        for design_id, ref_reqs in design_to_reqs.items():
            for ref_req in sorted(ref_reqs - req_ids):
                report.findings.append(
                    AuditFinding(
                        severity="warning",
                        category=CAT_DEAD_DESIGN,
                        subject=design_id,
                        message=(
                            f"{design_id} references {ref_req} but it is not "
                            "defined in requirements.md"
                        ),
                        location="design.md",
                    )
                )

    return report


# --- Formatters -----------------------------------------------------


def format_audit_text(report: AuditReport) -> str:
    """Human-readable output for the terminal."""
    lines: list[str] = ["\n== Audit Report ==\n"]

    if not report.findings:
        lines.append("OK — no audit issues found.\n")
        return "\n".join(lines)

    by_category: dict[str, list[AuditFinding]] = {}
    for f in report.findings:
        by_category.setdefault(f.category, []).append(f)

    # Stable section order
    section_order = [
        CAT_DANGLING_REQ_REF,
        CAT_DANGLING_DESIGN_REF,
        CAT_ORPHAN_TASK,
        CAT_UNCOVERED_REQ,
        CAT_UNCOVERED_DESIGN,
        CAT_DEAD_DESIGN,
    ]
    for cat in section_order:
        items = by_category.get(cat)
        if not items:
            continue
        sev = items[0].severity.upper()
        lines.append(f"[{sev}] {cat} ({len(items)}):")
        for f in items:
            lines.append(f"  - {f.subject}: {f.message}")

    counts = report.counts
    lines.append("")
    lines.append("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    status = "PASS" if report.ok else "FAIL"
    lines.append(f"Overall: {status}")
    return "\n".join(lines)


def format_audit_json(report: AuditReport) -> str:
    """Machine-readable JSON output (one object)."""
    data = {
        "ok": report.ok,
        "strict": report.strict,
        "counts": report.counts,
        "findings": [
            {
                "severity": f.severity,
                "category": f.category,
                "subject": f.subject,
                "message": f.message,
                "location": f.location,
            }
            for f in report.findings
        ],
    }
    return json.dumps(data, indent=2)


def format_audit_csv(report: AuditReport) -> str:
    """CSV suitable for spreadsheet review."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["severity", "category", "subject", "location", "message"])
    for f in report.findings:
        writer.writerow([f.severity, f.category, f.subject, f.location, f.message])
    return buf.getvalue()
