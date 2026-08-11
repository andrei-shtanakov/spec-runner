"""Read-only preflight: what is missing, and what of that blocks a run (#142a).

There is no zero stage. On a greenfield repo "what do I need before tasks can
run" was answered one task failure at a time, and a gate that is green on an
empty project answers nothing at all: an empty suite exits 0, and so does a
linter with no files. The instrument has to be examined before it is believed.

Deliberate boundaries:

- **Writes nothing.** A diagnostic that quietly repairs the tree cannot be part
  of a gate, and cannot be run for information without side effects.
- **No `bootstrap`.** Creating a `pyproject.toml`, a layout and a toolchain is a
  separate product decision — it would make spec-runner a project scaffolder.
- **No mutation probe.** Certifying the oracle by breaking something on purpose
  belongs in a disposable worktree, not in diagnostics of the working tree.
- **Never guesses.** Where the answer cannot be established honestly (a
  composite `test_command`, an unrecognized runner), the status is
  ``unavailable`` — not a cheerful ``ok``.

This is not `doctor` and not `validate`: `doctor` runs a real mini-task to probe
a CLI/model pair, `validate` checks the spec's contents. Preflight asks whether
this project can run tasks at all, and touches nothing.
"""

import json
import shutil
import subprocess
import sys
from dataclasses import dataclass

from .config import ExecutorConfig
from .git_ops import is_composite_shell_command
from .logging import get_logger

logger = get_logger("preflight")

#: Bumped when the `--json` payload changes shape. Consumers pin it.
PREFLIGHT_SCHEMA_VERSION = 1

#: Check outcomes. Deliberately more than ok/not-ok: "the tool is absent",
#: "the suite is empty", "the oracle is broken" and "cannot be established"
#: are four different situations and only one of them is a typo away from fine.
STATUSES = ("ok", "missing", "empty", "broken", "unavailable", "skipped")


@dataclass(frozen=True)
class Check:
    """One preflight finding.

    ``blocking`` is separate from ``status`` on purpose: whether a missing
    thing prevents a run depends on the configuration (no git needed when git
    automation is off), and an orchestrator wants both facts.
    """

    id: str
    status: str
    blocking: bool
    detail: str

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"unknown status {self.status!r}; expected one of {STATUSES}")


@dataclass(frozen=True)
class PreflightReport:
    checks: list[Check]

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.blocking]

    @property
    def verdict(self) -> str:
        return "blocked" if self.blockers else "ready"

    @property
    def exit_code(self) -> int:
        return 1 if self.blockers else 0


def _first_program(command: str) -> str:
    """The executable a simple command runs, or "" when it cannot be told."""
    parts = command.split()
    if not parts:
        return ""
    # `uv run pytest ...` — the interesting binary is uv; it is what must exist.
    return parts[0]


def _check_spec(config: ExecutorConfig) -> list[Check]:
    if not config.tasks_file.exists():
        return [
            Check(
                "spec.tasks",
                "missing",
                True,
                f"{config.tasks_file} not found — nothing to execute",
            ),
            Check("spec.validation", "skipped", False, "no tasks file to validate"),
        ]

    checks = [Check("spec.tasks", "ok", False, f"{config.tasks_file} present")]
    try:
        from .task import parse_tasks
        from .validate import validate_task_fields

        result = validate_task_fields(parse_tasks(config.tasks_file))
    except Exception as exc:  # unreadable/unparseable file
        checks.append(Check("spec.validation", "broken", True, f"cannot parse: {exc}"))
        return checks

    if result.errors:
        checks.append(
            Check(
                "spec.validation",
                "broken",
                True,
                f"{len(result.errors)} validation error(s): {result.errors[0]}",
            )
        )
    else:
        checks.append(Check("spec.validation", "ok", False, "spec validates"))
    return checks


def _check_tool(check_id: str, command: str, *, blocking: bool, label: str) -> Check:
    program = _first_program(command)
    if not program:
        return Check(check_id, "missing", blocking, f"{label} is not configured")
    if shutil.which(program) is None:
        return Check(check_id, "missing", blocking, f"{label} {program!r} is not on PATH")
    return Check(check_id, "ok", False, f"{label} {program!r} found")


def _check_suite(config: ExecutorConfig) -> Check:
    """Is there a test suite, and can it even be collected?

    Collection is the read-only way to tell "no tests" from "the oracle is
    broken" — both of which a plain run reports as a non-zero-or-zero exit with
    no useful distinction. An empty suite is a blocker: `0 passed` and exit 0
    is precisely the green that proves nothing.
    """
    command = config.test_command
    if is_composite_shell_command(command):
        return Check(
            "tests.suite",
            "unavailable",
            False,
            "composite test_command: cannot tell which program collects tests "
            "without guessing, so the suite was not inspected",
        )
    if "pytest" not in command:
        return Check(
            "tests.suite",
            "unavailable",
            False,
            f"unrecognized test runner in {command!r}: only pytest collection is understood",
        )
    try:
        result = subprocess.run(
            f"{command} --collect-only -q",
            shell=True,
            capture_output=True,
            text=True,
            cwd=config.project_root,
            timeout=120,
        )
    except Exception as exc:
        return Check("tests.suite", "unavailable", False, f"collection could not run: {exc}")

    # pytest exit codes: 5 = "no tests collected", 4 = usage error, which on a
    # greenfield repo is overwhelmingly "the configured test path is not there
    # yet". Both mean there is no suite to believe; the raw reason is kept in
    # the detail rather than flattened away. Anything else non-zero is a
    # collection failure — a suite that exists but cannot be loaded.
    if result.returncode in (4, 5):
        raw = (result.stdout or result.stderr).strip().splitlines()
        why = raw[-1] if raw else "no tests collected"
        return Check(
            "tests.suite",
            "empty",
            True,
            f"no suite to run — an empty suite exits 0 and proves nothing ({why})",
        )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        return Check(
            "tests.suite",
            "broken",
            True,
            f"collection failed ({result.returncode}): {detail[-1] if detail else 'no output'}",
        )
    return Check(
        "tests.suite", "ok", False, (result.stdout.strip().splitlines() or ["collected"])[-1]
    )


def _check_git(config: ExecutorConfig) -> Check:
    needed = bool(
        getattr(config, "create_git_branch", False) or getattr(config, "auto_commit", False)
    )
    if shutil.which("git") is None:
        return Check("git.repo", "missing", needed, "git is not on PATH")
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            cwd=config.project_root,
            timeout=30,
        )
    except Exception as exc:
        return Check("git.repo", "unavailable", needed, f"git could not run: {exc}")
    if result.returncode != 0:
        return Check(
            "git.repo",
            "unavailable",
            needed,
            "not a git repository"
            + ("" if needed else " (git automation is off, so this does not block)"),
        )
    return Check("git.repo", "ok", False, "git repository present")


def _check_state_dir(config: ExecutorConfig) -> Check:
    import os

    target = config.state_file.parent
    probe = target if target.exists() else config.project_root
    if not probe.exists():
        return Check("state.writable", "missing", True, f"{probe} does not exist")
    if not os.access(probe, os.W_OK):
        return Check("state.writable", "broken", True, f"{probe} is not writable")
    return Check("state.writable", "ok", False, f"{probe} is writable")


def run_preflight(config: ExecutorConfig) -> PreflightReport:
    """Collect every check. Writes nothing, raises nothing."""
    checks: list[Check] = list(_check_spec(config))
    checks.append(_check_tool("agent.cli", config.claude_command, blocking=True, label="agent CLI"))

    if getattr(config, "run_tests_on_done", True):
        checks.append(
            _check_tool("tests.runner", config.test_command, blocking=True, label="test runner")
        )
        checks.append(_check_suite(config))
    else:
        reason = "run_tests_on_done is false"
        checks.append(Check("tests.runner", "skipped", False, reason))
        checks.append(Check("tests.suite", "skipped", False, reason))

    if getattr(config, "run_lint_on_done", True):
        checks.append(
            _check_tool(
                "lint.runner",
                config.lint_command,
                blocking=bool(getattr(config, "lint_blocking", True)),
                label="lint runner",
            )
        )
    else:
        checks.append(Check("lint.runner", "skipped", False, "run_lint_on_done is false"))

    checks.append(_check_git(config))
    checks.append(_check_state_dir(config))
    return PreflightReport(checks=checks)


def preflight_to_dict(report: PreflightReport) -> dict:
    """The `--json` payload. Shape is pinned by `schemas/preflight-result.schema.json`."""
    return {
        "schema_version": PREFLIGHT_SCHEMA_VERSION,
        "verdict": report.verdict,
        "exit_code": report.exit_code,
        "blockers": [c.id for c in report.blockers],
        "checks": [
            {"id": c.id, "status": c.status, "blocking": c.blocking, "detail": c.detail}
            for c in report.checks
        ],
    }


_ICON = {
    "ok": "✅",
    "missing": "❌",
    "empty": "⚠️",
    "broken": "💥",
    "unavailable": "❓",
    "skipped": "⏭️",
}


def format_preflight(report: PreflightReport) -> str:
    lines = ["🚦 Preflight", ""]
    for check in report.checks:
        mark = " (blocks)" if check.blocking else ""
        lines.append(f"  {_ICON[check.status]} {check.id}: {check.status}{mark} — {check.detail}")
    lines.append("")
    if report.blockers:
        lines.append(f"⛔ blocked: {', '.join(c.id for c in report.blockers)}")
    else:
        lines.append("✅ ready: nothing blocks a run")
    return "\n".join(lines)


def cmd_preflight(args, config: ExecutorConfig) -> None:
    """`spec-runner preflight [--json]`. Exit 0 = ready, 1 = blockers."""
    report = run_preflight(config)
    if getattr(args, "json", False):
        # One document on stdout, diagnostics nowhere else (the #116 rule).
        print(json.dumps(preflight_to_dict(report), indent=2))
    else:
        print(format_preflight(report))
    logger.info("Preflight complete", verdict=report.verdict, blockers=len(report.blockers))
    if report.exit_code:
        sys.exit(report.exit_code)


__all__ = [
    "PREFLIGHT_SCHEMA_VERSION",
    "Check",
    "PreflightReport",
    "cmd_preflight",
    "format_preflight",
    "preflight_to_dict",
    "run_preflight",
]
