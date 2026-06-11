"""spec-runner doctor — empirical CLI/model compatibility probe.

Runs a real mini-task through execution.execute_task() and reports, per
capability, whether the configured (or ad-hoc) CLI+model works with spec-runner.
Capability signals are read from the recorded TaskAttempt and the filesystem —
never from execute_task's success verdict, which treats a 0 exit as implicit
success and a review without a marker as PASSED.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import ExecutorConfig
from .execution import execute_task
from .state import ExecutorState, TaskAttempt
from .task import parse_tasks

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


# ---------------------------------------------------------------------------
# build_scratch — ephemeral workspace for the probe
# ---------------------------------------------------------------------------

DOCTOR_TIMEOUT_MIN = 3

_CANNED_TASK_MD = """# Doctor probe

### TASK-001: Doctor smoke probe
P0 | TODO

**Checklist:**
- [ ] Create a file named SMOKE.txt in the project root whose entire contents \
are exactly: PONG
"""


def _git(root: Path, *args: str) -> None:
    """Run a git command in *root*, ignoring return code."""
    subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)


def build_scratch(
    base: ExecutorConfig,
    with_review: bool,
    budget: float,
    timeout_min: int | None,
) -> tuple[ExecutorConfig, Path]:
    """Create an ephemeral workspace + scratch ExecutorConfig for the probe.

    Returns (scratch_config, scratch_root). Caller is responsible for cleanup
    (shutil.rmtree) unless --keep is set.
    """
    root = Path(tempfile.mkdtemp(prefix="spec-runner-doctor-"))
    (root / "spec").mkdir(parents=True, exist_ok=True)
    (root / "spec" / "tasks.md").write_text(_CANNED_TASK_MD)

    cfg = copy.deepcopy(base)

    # Reset project_root + relative path defaults BEFORE calling __post_init__
    # so that __post_init__ resolves state_file/logs_dir under the new root.
    cfg.project_root = root
    cfg.spec_prefix = ""
    cfg.state_file = Path("spec/.executor-state.db")
    cfg.logs_dir = Path("spec/.executor-logs")
    cfg.__post_init__()

    # Hook flags
    cfg.sync_deps = False
    cfg.create_git_branch = False
    cfg.run_tests_on_done = False
    cfg.run_lint_on_done = False
    cfg.task_budget_usd = budget
    cfg.task_timeout_minutes = timeout_min if timeout_min is not None else DOCTOR_TIMEOUT_MIN

    if with_review:
        cfg.run_review = True
        cfg.auto_commit = True
        _git(root, "init")
        _git(root, "config", "user.email", "doctor@spec-runner.local")
        _git(root, "config", "user.name", "spec-runner doctor")
        (root / ".gitkeep").write_text("")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "doctor baseline")
    else:
        cfg.run_review = False
        cfg.auto_commit = False

    return cfg, root


# ---------------------------------------------------------------------------
# run_probe — integration entry point
# ---------------------------------------------------------------------------


def run_probe(cfg: ExecutorConfig) -> TaskAttempt:
    """Run the canned task through the real execute_task() and return the
    recorded attempt."""
    tasks = parse_tasks(cfg.tasks_file)
    task = tasks[0]
    last: TaskAttempt
    with ExecutorState(cfg) as _state:
        state: ExecutorState = _state
        execute_task(task, cfg, state)
        last = state.get_task_state(task.id).attempts[-1]
    return last


# ---------------------------------------------------------------------------
# Cost gate + top-level orchestrator
# ---------------------------------------------------------------------------


def _confirm(cfg: ExecutorConfig, with_review: bool, budget: float) -> bool:
    """Print probe info and ask the user to confirm real model calls."""
    calls = 2 if with_review else 1
    model = cfg.claude_model or "(default)"
    print("spec-runner doctor — compatibility probe")
    print(f"  CLI:    {cfg.claude_command}")
    print(f"  Model:  {model}")
    if with_review:
        print(f"  Review: {cfg.review_command or cfg.claude_command} ({cfg.review_model or model})")
    print(f"  Budget: capped at ${budget:.2f} (enforceable only if cost parsing is supported)")
    answer = input(f"This makes {calls} real, billable model call(s). Proceed? [y/N] ")
    return answer.strip().lower() in ("y", "yes")


def run_doctor(
    base: ExecutorConfig,
    *,
    cli: str | None,
    model: str | None,
    with_review: bool,
    budget: float,
    timeout_min: int | None,
    assume_yes: bool,
    strict: bool,
    as_json: bool,
    keep: bool,
) -> int:
    """Run the probe end to end. Returns the process exit code.

    Exit: 0 = READY/DEGRADED, 1 = BROKEN (or DEGRADED under --strict),
          2 = user declined at the cost gate.
    """
    target = resolve_target(base, cli, model)

    if not assume_yes and not _confirm(target, with_review, budget):
        print("Aborted.")
        return 2

    cfg, root = build_scratch(target, with_review, budget, timeout_min)
    # In JSON mode redirect stdout → stderr during the probe so no progress
    # text leaks into what must be a single parseable JSON object.
    _saved_stdout = sys.stdout if as_json else None
    if _saved_stdout is not None:
        sys.stdout = sys.stderr
    try:
        attempt = run_probe(cfg)
        report = extract(attempt, root, with_review)
        report.cli = target.claude_command
        report.model = target.claude_model
    finally:
        if _saved_stdout is not None:
            sys.stdout = _saved_stdout
        if keep:
            print(f"(scratch kept at {root})")
        else:
            shutil.rmtree(root, ignore_errors=True)

    if as_json:
        print(json.dumps(report_to_dict(report)))
    else:
        print(render_human(report))

    if report.verdict == "broken":
        return 1
    if report.verdict == "degraded" and strict:
        return 1
    return 0
