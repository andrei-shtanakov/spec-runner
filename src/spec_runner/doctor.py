"""spec-runner doctor — empirical CLI/model compatibility probe.

Runs a real mini-task through execution.execute_task() and reports, per
capability, whether the configured (or ad-hoc) CLI+model works with spec-runner.
Capability signals are read from the recorded TaskAttempt and the filesystem —
never from execute_task's success verdict, which treats a 0 exit as implicit
success and a review without a marker as PASSED.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path

from .config import ExecutorConfig
from .state import TaskAttempt

CHECK_OK = "ok"
CHECK_UNSUPPORTED = "unsupported"
CHECK_FAIL = "fail"
CHECK_NA = "na"

# Checks that gate the BROKEN verdict.
_CORE_CHECKS = ("invocation", "completion_marker", "task_action")


@dataclass
class CheckResult:
    """Outcome of a single capability check."""

    status: str  # one of CHECK_OK / CHECK_UNSUPPORTED / CHECK_FAIL / CHECK_NA
    detail: str = ""


@dataclass
class DoctorReport:
    """Aggregated probe result."""

    cli: str
    model: str
    review: bool
    checks: dict[str, CheckResult] = field(default_factory=dict)
    measured_cost_usd: float | None = None
    duration_s: float = 0.0
    budget_enforceable: bool = False

    @property
    def verdict(self) -> str:
        if any(
            self.checks.get(k, CheckResult(CHECK_NA)).status == CHECK_FAIL for k in _CORE_CHECKS
        ):
            return "broken"
        if any(c.status in (CHECK_UNSUPPORTED, CHECK_FAIL) for c in self.checks.values()):
            return "degraded"
        return "ready"


def _not_in_path(error: str) -> bool:
    e = error.lower()
    return "no such file" in e or "not found" in e


def extract(attempt: TaskAttempt, scratch_root: Path, with_review: bool) -> DoctorReport:
    """Derive per-capability checks from a recorded attempt + filesystem."""
    checks: dict[str, CheckResult] = {}

    # --- invocation ---
    invocation_ok = attempt.success
    if attempt.success:
        checks["invocation"] = CheckResult(CHECK_OK, f"exit 0 in {attempt.duration_seconds:.1f}s")
    else:
        error = attempt.error or ""
        if _not_in_path(error):
            checks["invocation"] = CheckResult(CHECK_FAIL, "command not in PATH")
        else:
            kind = attempt.error_kind or "error"
            checks["invocation"] = CheckResult(CHECK_FAIL, f"{kind}: {error[:80]}")

    # --- error_classification (diagnostic; only meaningful on failure) ---
    if attempt.success:
        checks["error_classification"] = CheckResult(CHECK_NA, "probe succeeded")
    elif attempt.error_kind and attempt.error_kind != "unknown":
        checks["error_classification"] = CheckResult(CHECK_OK, attempt.error_kind)
    else:
        checks["error_classification"] = CheckResult(CHECK_UNSUPPORTED, "generic fallback")

    # If the CLI never ran, downstream checks are not applicable.
    if not invocation_ok:
        for name in ("completion_marker", "task_action", "cost_tracking"):
            checks[name] = CheckResult(CHECK_NA, "invocation failed")
        if with_review:
            checks["review"] = CheckResult(CHECK_NA, "invocation failed")
        return DoctorReport(
            cli="",
            model="",
            review=with_review,
            checks=checks,
            measured_cost_usd=attempt.cost_usd,
            duration_s=attempt.duration_seconds,
            budget_enforceable=attempt.cost_usd is not None,
        )

    # --- completion_marker ---
    out = attempt.claude_output or ""
    if "TASK_COMPLETE" in out and "TASK_FAILED" not in out:
        checks["completion_marker"] = CheckResult(CHECK_OK, "TASK_COMPLETE detected")
    else:
        checks["completion_marker"] = CheckResult(CHECK_FAIL, "TASK_COMPLETE not found in output")

    # --- task_action ---
    smoke = scratch_root / "SMOKE.txt"
    if not smoke.exists():
        checks["task_action"] = CheckResult(CHECK_FAIL, "SMOKE.txt not created")
    elif smoke.read_text().strip() == "PONG":
        checks["task_action"] = CheckResult(CHECK_OK, 'SMOKE.txt == "PONG"')
    else:
        checks["task_action"] = CheckResult(
            CHECK_UNSUPPORTED, f"SMOKE.txt present but content={smoke.read_text()!r}"
        )

    # --- cost_tracking ---
    has_cost = (
        attempt.cost_usd is not None
        or attempt.input_tokens is not None
        or attempt.output_tokens is not None
    )
    if has_cost:
        checks["cost_tracking"] = CheckResult(CHECK_OK, f"cost=${attempt.cost_usd}")
    else:
        checks["cost_tracking"] = CheckResult(CHECK_UNSUPPORTED, "no cost/tokens in stderr")

    # --- review (only with --with-review) ---
    if with_review:
        findings = (attempt.review_findings or "").upper()
        if not findings.strip():
            checks["review"] = CheckResult(CHECK_FAIL, "review produced no output")
        elif any(m in findings for m in ("REVIEW_PASSED", "REVIEW_FAILED", "REVIEW_FIXED")):
            checks["review"] = CheckResult(CHECK_OK, "review marker detected")
        else:
            checks["review"] = CheckResult(
                CHECK_UNSUPPORTED, "review ran but no recognizable marker"
            )

    return DoctorReport(
        cli="",
        model="",
        review=with_review,
        checks=checks,
        measured_cost_usd=attempt.cost_usd,
        duration_s=attempt.duration_seconds,
        budget_enforceable=has_cost,
    )


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

# Order checks are displayed in.
_CHECK_ORDER = (
    "invocation",
    "completion_marker",
    "task_action",
    "cost_tracking",
    "error_classification",
    "review",
)

_STATUS_GLYPH = {
    CHECK_OK: "ok  ",
    CHECK_UNSUPPORTED: "warn",
    CHECK_FAIL: "FAIL",
    CHECK_NA: "n/a ",
}


def report_to_dict(report: DoctorReport) -> dict:  # type: ignore[type-arg]
    """Serialize a report to the stable --json shape."""
    return {
        "cli": report.cli,
        "model": report.model,
        "review": report.review,
        "verdict": report.verdict,
        "checks": {
            name: {"status": res.status, "detail": res.detail}
            for name, res in report.checks.items()
        },
        "budget_enforceable": report.budget_enforceable,
        "measured_cost_usd": report.measured_cost_usd,
        "duration_s": round(report.duration_s, 2),
    }


def render_human(report: DoctorReport) -> str:
    """Human-readable report."""
    lines = [f"spec-runner doctor — {report.cli} / {report.model or '(default)'}", ""]
    for name in _CHECK_ORDER:
        res = report.checks.get(name)
        if res is None:
            continue
        glyph = _STATUS_GLYPH.get(res.status, res.status)
        detail = f"  {res.detail}" if res.detail else ""
        lines.append(f"  {glyph} {name:<20}{detail}")
    lines.append("")
    lines.append(f"  Verdict: {report.verdict.upper()}")
    if not report.budget_enforceable:
        lines.append("           Budget cap NOT enforceable (cost not parsed for this CLI).")
    cost = "n/a" if report.measured_cost_usd is None else f"${report.measured_cost_usd}"
    lines.append(f"  Measured cost: {cost}")
    return "\n".join(lines)


def resolve_target(base: ExecutorConfig, cli: str | None, model: str | None) -> ExecutorConfig:
    """Copy `base` and apply --cli/--model overrides for the probe.

    - --cli also clears command/review templates so build_cli_command
      auto-detects flags for the new CLI (a claude-shaped template would
      otherwise break e.g. codex).
    - --model overrides claude_model/review_model AND the implementer/reviewer
      persona models, since get_model_for_role prefers a persona's model.
    """
    cfg = copy.deepcopy(base)
    if cli:
        cfg.claude_command = cli
        cfg.review_command = ""
        cfg.command_template = ""
        cfg.review_command_template = ""
    if model:
        cfg.claude_model = model
        cfg.review_model = model
        for role in ("implementer", "reviewer"):
            persona = cfg.personas.get(role)
            if persona is not None:
                persona.model = model
    return cfg
