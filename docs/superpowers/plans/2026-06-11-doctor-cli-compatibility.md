# `spec-runner doctor` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `spec-runner doctor` subcommand that runs a real mini-task through the actual `execute_task()` code path against the configured (or an ad-hoc) CLI+model, and reports per-capability compatibility (invocation / completion marker / task action / cost tracking / error classification / optional review) with a READY/DEGRADED/BROKEN verdict.

**Architecture:** A new `doctor.py` module prepares an ephemeral scratch workspace + `ExecutorConfig` (all hooks off, dependency sync off, budget-capped), runs the *real* `execution.execute_task()`, then extracts **raw signals** from the recorded `TaskAttempt` and the filesystem — never trusting `execute_task`'s success verdict (which treats `returncode==0` as implicit success and review-without-marker as PASSED). A thin `cmd_doctor` dispatcher wires it into the CLI.

**Tech Stack:** Python 3.10+, dataclasses, argparse, pytest, `uv`, ruff, mypy. Reuses `execution.execute_task`, `state.ExecutorState`/`TaskAttempt`, `config.ExecutorConfig`, `task.parse_tasks`.

**Spec:** `docs/superpowers/specs/2026-06-11-doctor-cli-compatibility-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `src/spec_runner/config.py` | **Modify** — add `sync_deps: bool = True` field + `build_config` parse |
| `src/spec_runner/hooks.py` | **Modify** — `pre_start_hook` skips `uv sync` when `config.sync_deps` is False |
| `src/spec_runner/doctor.py` | **Create** — data model, target resolution, scratch builder, probe runner, signal extraction, rendering, orchestrator |
| `src/spec_runner/cli.py` | **Modify** — `cmd_doctor` dispatcher + `doctor` subparser + dispatch entry |
| `schemas/doctor-result.schema.json` | **Create** — `--json` output contract |
| `tests/fixtures/doctor/` | **Create** — fake CLI shell scripts (`ok`, `nocost`, `nomarker`, `noaction`, `authfail`) + fake review CLI |
| `tests/test_doctor.py` | **Create** — unit + e2e tests on the fakes |

**Verified facts the code below relies on:**
- `execution.execute_task(task, config, state) -> bool | str`; runs the CLI subprocess with `cwd=config.project_root` (execution.py:130).
- Success logic is `(has_complete_marker and not has_failed_marker) or (returncode==0 and not TASK_FAILED)` — so a 0-exit without `TASK_COMPLETE` is still "success". We must read the marker ourselves.
- `review.run_code_review` returns `ReviewVerdict.PASSED` when no marker is found; `post_done_hook` stores the **raw** review output as `attempt.review_findings` (first 2048 chars).
- `TaskAttempt` fields: `success`, `duration_seconds`, `error`, `claude_output`, `error_code`, `input_tokens`, `output_tokens`, `cost_usd`, `review_status`, `review_findings`, `error_kind`, `error_stage`.
- Task header regex requires `TASK-\d+` (so the canned task is `TASK-001`, not `TASK-SMOKE`); plain `P0 | TODO` meta (no emoji) parses.
- `ExecutorConfig` is a dataclass; `config.tasks_file` is a property = `project_root/spec/{spec_prefix}tasks.md`. `ExecutorState(config)` is a context manager.
- `pre_start_hook` runs `uv sync` unconditionally at hooks.py:58-65.
- CLI dispatch is a dict `commands = {"run": cmd_run, ...}` in `cli.py:main()`; `cmd_func(args, config)`.

---

## Task 1: Add `sync_deps` config flag

**Files:**
- Modify: `src/spec_runner/config.py` (ExecutorConfig dataclass + `build_config`)
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_doctor.py` with:

```python
from pathlib import Path

from spec_runner.config import ExecutorConfig, build_config


def test_sync_deps_defaults_true():
    cfg = ExecutorConfig(project_root=Path("."))
    assert cfg.sync_deps is True


def test_build_config_reads_sync_deps_false():
    cfg = build_config({"sync_deps": False}, args=None)
    assert cfg.sync_deps is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_doctor.py -k sync_deps -v`
Expected: FAIL — `AttributeError: 'ExecutorConfig' object has no attribute 'sync_deps'`.

- [ ] **Step 3: Add the field**

In `src/spec_runner/config.py`, in the `ExecutorConfig` dataclass next to the other hook flags (near `create_git_branch`), add:

```python
    sync_deps: bool = True  # Run `uv sync` in pre_start_hook (doctor disables this)
```

- [ ] **Step 4: Parse it in `build_config`**

In `build_config()` (config.py), in the dict of values pulled from `executor_config`, add an entry alongside the other top-level keys (e.g. near `"claude_command"`):

```python
            "sync_deps": executor_config.get("sync_deps"),
```

(`build_config` already drops `None` values so the dataclass default applies when the key is absent — match the existing pattern in that function.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_doctor.py -k sync_deps -v`
Expected: PASS (both tests).

- [ ] **Step 6: Commit**

```bash
git add src/spec_runner/config.py tests/test_doctor.py
git commit -m "feat(config): add sync_deps flag to gate pre_start uv sync"
```

---

## Task 2: Gate `uv sync` on `sync_deps` in `pre_start_hook`

**Files:**
- Modify: `src/spec_runner/hooks.py:58-65` (the `uv sync` block)
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_doctor.py`:

```python
from unittest.mock import patch

from spec_runner.hooks import pre_start_hook
from spec_runner.task import Task


def _smoke_task() -> Task:
    # Task dataclass requires `estimate` (no default), positioned before description.
    return Task(
        id="TASK-001",
        name="probe",
        priority="p0",
        status="todo",
        estimate="",
        description="",
        checklist=[],
    )


def test_pre_start_skips_uv_sync_when_disabled(tmp_path):
    cfg = ExecutorConfig(
        project_root=tmp_path,
        sync_deps=False,
        create_git_branch=False,
    )
    with patch("spec_runner.hooks.subprocess.run") as mock_run:
        pre_start_hook(_smoke_task(), cfg)
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert ["uv", "sync"] not in calls


def test_pre_start_runs_uv_sync_when_enabled(tmp_path):
    cfg = ExecutorConfig(
        project_root=tmp_path,
        sync_deps=True,
        create_git_branch=False,
    )
    with patch("spec_runner.hooks.subprocess.run") as mock_run:
        mock_run.return_value.returncode = 0
        pre_start_hook(_smoke_task(), cfg)
    calls = [c.args[0] for c in mock_run.call_args_list]
    assert ["uv", "sync"] in calls
```

> Note: confirm the `Task` constructor field names by reading `src/spec_runner/task.py` (`@dataclass class Task`). If a required field is missing in the helper, add it with a trivial value — do not change `Task`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_doctor.py -k pre_start -v`
Expected: FAIL — `test_pre_start_skips_uv_sync_when_disabled` fails because `uv sync` is still called.

- [ ] **Step 3: Add the gate**

In `src/spec_runner/hooks.py`, wrap the existing sync block. Replace:

```python
    # Sync dependencies
    if reporter:
        reporter.enter("sync_deps")
    logger.info("Syncing dependencies")
    result = subprocess.run(["uv", "sync"], capture_output=True, text=True, cwd=config.project_root)
    if result.returncode == 0:
        logger.info("Dependencies synced")
    else:
        logger.warning("uv sync warning", stderr=result.stderr[:200])
```

with:

```python
    # Sync dependencies (skippable — doctor and other lightweight runs disable this)
    if config.sync_deps:
        if reporter:
            reporter.enter("sync_deps")
        logger.info("Syncing dependencies")
        result = subprocess.run(
            ["uv", "sync"], capture_output=True, text=True, cwd=config.project_root
        )
        if result.returncode == 0:
            logger.info("Dependencies synced")
        else:
            logger.warning("uv sync warning", stderr=result.stderr[:200])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_doctor.py -k pre_start -v`
Expected: PASS (both).

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/hooks.py tests/test_doctor.py
git commit -m "feat(hooks): skip uv sync in pre_start when sync_deps is False"
```

---

## Task 3: Doctor data model + `extract()` (the heart)

`extract()` is a pure function from a recorded `TaskAttempt` + scratch root → `DoctorReport`. It reads raw signals, never the success verdict. TDD it with synthetic `TaskAttempt` objects (no subprocess needed).

**Files:**
- Create: `src/spec_runner/doctor.py`
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_doctor.py`:

```python
from spec_runner.doctor import (
    CHECK_FAIL,
    CHECK_NA,
    CHECK_OK,
    CHECK_UNSUPPORTED,
    DoctorReport,
    extract,
)
from spec_runner.state import TaskAttempt


def _attempt(**kw) -> TaskAttempt:
    base = dict(timestamp="t", success=True, duration_seconds=1.0)
    base.update(kw)
    return TaskAttempt(**base)


def _write_smoke(root: Path, text: str = "PONG") -> None:
    (root / "SMOKE.txt").write_text(text)


def test_extract_all_ok(tmp_path):
    _write_smoke(tmp_path)
    att = _attempt(claude_output="working... TASK_COMPLETE", cost_usd=0.01)
    rep = extract(att, tmp_path, with_review=False)
    assert rep.checks["invocation"].status == CHECK_OK
    assert rep.checks["completion_marker"].status == CHECK_OK
    assert rep.checks["task_action"].status == CHECK_OK
    assert rep.checks["cost_tracking"].status == CHECK_OK
    assert rep.checks["error_classification"].status == CHECK_NA
    assert rep.verdict == "ready"
    assert rep.budget_enforceable is True


def test_extract_no_cost_is_degraded(tmp_path):
    _write_smoke(tmp_path)
    att = _attempt(claude_output="TASK_COMPLETE", cost_usd=None)
    rep = extract(att, tmp_path, with_review=False)
    assert rep.checks["cost_tracking"].status == CHECK_UNSUPPORTED
    assert rep.budget_enforceable is False
    assert rep.verdict == "degraded"


def test_extract_no_marker_is_broken(tmp_path):
    # exit 0, file created, but model never printed the marker -> BROKEN.
    # Proves we do NOT trust attempt.success.
    _write_smoke(tmp_path)
    att = _attempt(success=True, claude_output="all done!", cost_usd=0.01)
    rep = extract(att, tmp_path, with_review=False)
    assert rep.checks["completion_marker"].status == CHECK_FAIL
    assert rep.verdict == "broken"


def test_extract_marker_but_no_file_is_broken(tmp_path):
    att = _attempt(claude_output="TASK_COMPLETE", cost_usd=0.01)
    rep = extract(att, tmp_path, with_review=False)  # no SMOKE.txt written
    assert rep.checks["task_action"].status == CHECK_FAIL
    assert rep.verdict == "broken"


def test_extract_wrong_file_content_is_degraded(tmp_path):
    _write_smoke(tmp_path, text="pong\n")
    att = _attempt(claude_output="TASK_COMPLETE", cost_usd=0.01)
    rep = extract(att, tmp_path, with_review=False)
    assert rep.checks["task_action"].status == CHECK_UNSUPPORTED
    assert rep.verdict == "degraded"


def test_extract_command_not_found(tmp_path):
    att = _attempt(
        success=False,
        error="[Errno 2] No such file or directory: 'codexx'",
        error_kind=None,
        claude_output=None,
    )
    rep = extract(att, tmp_path, with_review=False)
    assert rep.checks["invocation"].status == CHECK_FAIL
    assert "PATH" in rep.checks["invocation"].detail
    assert rep.checks["completion_marker"].status == CHECK_NA
    assert rep.verdict == "broken"


def test_extract_auth_failure_classified(tmp_path):
    att = _attempt(
        success=False,
        error="auth error",
        error_kind="auth",
        claude_output="",
    )
    rep = extract(att, tmp_path, with_review=False)
    assert rep.checks["invocation"].status == CHECK_FAIL
    assert rep.checks["error_classification"].status == CHECK_OK
    assert "auth" in rep.checks["error_classification"].detail


def test_extract_review_marker_ok(tmp_path):
    _write_smoke(tmp_path)
    att = _attempt(
        claude_output="TASK_COMPLETE",
        cost_usd=0.01,
        review_findings="Looks good.\nREVIEW_PASSED",
    )
    rep = extract(att, tmp_path, with_review=True)
    assert rep.checks["review"].status == CHECK_OK


def test_extract_review_marker_unrecognized(tmp_path):
    _write_smoke(tmp_path)
    att = _attempt(
        claude_output="TASK_COMPLETE",
        cost_usd=0.01,
        review_findings="The code seems fine to me.",  # no marker
    )
    rep = extract(att, tmp_path, with_review=True)
    assert rep.checks["review"].status == CHECK_UNSUPPORTED
    assert rep.verdict == "degraded"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_doctor.py -k extract -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'spec_runner.doctor'`.

- [ ] **Step 3: Implement the data model + `extract()`**

Create `src/spec_runner/doctor.py`:

```python
"""spec-runner doctor — empirical CLI/model compatibility probe.

Runs a real mini-task through execution.execute_task() and reports, per
capability, whether the configured (or ad-hoc) CLI+model works with spec-runner.
Capability signals are read from the recorded TaskAttempt and the filesystem —
never from execute_task's success verdict, which treats a 0 exit as implicit
success and a review without a marker as PASSED.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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
        if any(self.checks.get(k, CheckResult(CHECK_NA)).status == CHECK_FAIL for k in _CORE_CHECKS):
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
            cli="", model="", review=with_review, checks=checks,
            measured_cost_usd=attempt.cost_usd, duration_s=attempt.duration_seconds,
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
            checks["review"] = CheckResult(CHECK_UNSUPPORTED, "review ran but no recognizable marker")

    return DoctorReport(
        cli="", model="", review=with_review, checks=checks,
        measured_cost_usd=attempt.cost_usd, duration_s=attempt.duration_seconds,
        budget_enforceable=has_cost,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_doctor.py -k extract -v`
Expected: PASS (all 9 extract tests).

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/doctor.py tests/test_doctor.py
git commit -m "feat(doctor): capability data model + raw-signal extraction"
```

---

## Task 4: Rendering (human + JSON) + JSON schema + contract test

**Files:**
- Modify: `src/spec_runner/doctor.py` (add `report_to_dict`, `render_human`)
- Create: `schemas/doctor-result.schema.json`
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_doctor.py`:

```python
import json

from spec_runner.doctor import CheckResult, render_human, report_to_dict


def _ready_report() -> DoctorReport:
    return DoctorReport(
        cli="codex", model="gpt-5.4", review=False,
        checks={
            "invocation": CheckResult(CHECK_OK, "exit 0 in 7.2s"),
            "completion_marker": CheckResult(CHECK_OK),
            "task_action": CheckResult(CHECK_OK),
            "cost_tracking": CheckResult(CHECK_UNSUPPORTED, "no cost"),
            "error_classification": CheckResult(CHECK_NA),
        },
        measured_cost_usd=None, duration_s=9.1, budget_enforceable=False,
    )


def test_report_to_dict_shape():
    d = report_to_dict(_ready_report())
    assert d["cli"] == "codex"
    assert d["verdict"] == "degraded"
    assert d["budget_enforceable"] is False
    assert d["checks"]["cost_tracking"]["status"] == "unsupported"
    assert set(d["checks"]["cost_tracking"].keys()) == {"status", "detail"}


def test_render_human_mentions_verdict_and_checks():
    text = render_human(_ready_report())
    assert "DEGRADED" in text
    assert "cost_tracking" in text
    assert "codex" in text


def test_json_matches_schema():
    schema = json.loads(Path("schemas/doctor-result.schema.json").read_text())
    import jsonschema  # already a transitive dep via the project; if missing, add to dev

    jsonschema.validate(report_to_dict(_ready_report()), schema)
```

> If `jsonschema` is not importable, add it as a dev dependency: `uv add --dev jsonschema`. Check `tests/test_json_result_contract.py` first — the repo already validates JSON against schemas, so the import pattern there is the one to copy.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_doctor.py -k "report_to_dict or render_human or schema" -v`
Expected: FAIL — `ImportError: cannot import name 'render_human'` and missing schema file.

- [ ] **Step 3: Add rendering functions to `doctor.py`**

Append to `src/spec_runner/doctor.py`:

```python
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


def report_to_dict(report: DoctorReport) -> dict:
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
    lines = [f"🩺 spec-runner doctor — {report.cli} / {report.model or '(default)'}", ""]
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
```

- [ ] **Step 4: Create the JSON schema**

Create `schemas/doctor-result.schema.json`:

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "spec-runner doctor result",
  "type": "object",
  "required": ["cli", "model", "review", "verdict", "checks", "budget_enforceable"],
  "additionalProperties": false,
  "properties": {
    "cli": {"type": "string"},
    "model": {"type": "string"},
    "review": {"type": "boolean"},
    "verdict": {"type": "string", "enum": ["ready", "degraded", "broken"]},
    "budget_enforceable": {"type": "boolean"},
    "measured_cost_usd": {"type": ["number", "null"]},
    "duration_s": {"type": "number"},
    "checks": {
      "type": "object",
      "additionalProperties": {
        "type": "object",
        "required": ["status"],
        "additionalProperties": false,
        "properties": {
          "status": {"type": "string", "enum": ["ok", "unsupported", "fail", "na"]},
          "detail": {"type": "string"}
        }
      }
    }
  }
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_doctor.py -k "report_to_dict or render_human or schema" -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/spec_runner/doctor.py schemas/doctor-result.schema.json tests/test_doctor.py
git commit -m "feat(doctor): human + JSON rendering and result schema"
```

---

## Task 5: `resolve_target` — apply `--cli`/`--model` overrides

`resolve_target` returns a copy of the base config with the CLI/model overrides applied, fixing the template-precedence (#8) and persona-precedence (#9) traps.

**Files:**
- Modify: `src/spec_runner/doctor.py`
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_doctor.py`:

```python
from spec_runner.config import Persona
from spec_runner.doctor import resolve_target


def test_resolve_cli_clears_templates(tmp_path):
    base = ExecutorConfig(
        project_root=tmp_path,
        claude_command="claude",
        command_template="{cmd} -p {prompt} --model {model}",
        review_command_template="{cmd} -p {prompt}",
    )
    out = resolve_target(base, cli="codex", model=None)
    assert out.claude_command == "codex"
    assert out.command_template == ""
    assert out.review_command_template == ""


def test_resolve_model_overrides_personas(tmp_path):
    base = ExecutorConfig(
        project_root=tmp_path,
        claude_model="sonnet",
        personas={"implementer": Persona(model="haiku"), "reviewer": Persona(model="haiku")},
    )
    out = resolve_target(base, cli=None, model="gpt-5.4")
    assert out.claude_model == "gpt-5.4"
    assert out.review_model == "gpt-5.4"
    assert out.get_model_for_role("implementer") == "gpt-5.4"
    assert out.get_model_for_role("reviewer") == "gpt-5.4"


def test_resolve_no_overrides_keeps_config(tmp_path):
    base = ExecutorConfig(project_root=tmp_path, claude_command="pi", claude_model="x")
    out = resolve_target(base, cli=None, model=None)
    assert out.claude_command == "pi"
    assert out.claude_model == "x"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_doctor.py -k resolve -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_target'`.

- [ ] **Step 3: Implement `resolve_target`**

Append to `src/spec_runner/doctor.py` (add `import copy` and `from .config import ExecutorConfig, Persona` at the top):

```python
def resolve_target(
    base: ExecutorConfig, cli: str | None, model: str | None
) -> ExecutorConfig:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_doctor.py -k resolve -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/doctor.py tests/test_doctor.py
git commit -m "feat(doctor): resolve_target applies --cli/--model overrides safely"
```

---

## Task 6: `build_scratch` — ephemeral workspace + canned task + scratch config

**Files:**
- Modify: `src/spec_runner/doctor.py`
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_doctor.py`:

```python
import subprocess

from spec_runner.doctor import DOCTOR_TIMEOUT_MIN, build_scratch
from spec_runner.task import parse_tasks


def test_build_scratch_executor_only(tmp_path):
    base = ExecutorConfig(project_root=tmp_path, claude_command="claude")
    cfg, root = build_scratch(base, with_review=False, budget=0.5, timeout_min=None)
    try:
        # Hooks all off, sync off, budget capped, short timeout default.
        assert cfg.sync_deps is False
        assert cfg.create_git_branch is False
        assert cfg.auto_commit is False
        assert cfg.run_tests_on_done is False
        assert cfg.run_lint_on_done is False
        assert cfg.run_review is False
        assert cfg.task_budget_usd == 0.5
        assert cfg.task_timeout_minutes == DOCTOR_TIMEOUT_MIN
        # Canned task parses and is TASK-001.
        tasks = parse_tasks(cfg.tasks_file)
        assert tasks and tasks[0].id == "TASK-001"
        # No git for executor-only.
        assert not (root / ".git").exists()
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


def test_build_scratch_with_review_inits_git(tmp_path):
    base = ExecutorConfig(project_root=tmp_path, claude_command="claude")
    cfg, root = build_scratch(base, with_review=True, budget=0.5, timeout_min=None)
    try:
        assert cfg.run_review is True
        assert cfg.auto_commit is True
        assert (root / ".git").exists()
        # A baseline commit exists so review's `git diff HEAD~1` resolves.
        log = subprocess.run(
            ["git", "log", "--oneline"], cwd=root, capture_output=True, text=True
        )
        assert log.returncode == 0 and log.stdout.strip()
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


def test_build_scratch_honors_user_timeout(tmp_path):
    base = ExecutorConfig(project_root=tmp_path, claude_command="claude")
    cfg, root = build_scratch(base, with_review=False, budget=0.5, timeout_min=10)
    try:
        assert cfg.task_timeout_minutes == 10
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_doctor.py -k build_scratch -v`
Expected: FAIL — `ImportError: cannot import name 'build_scratch'`.

- [ ] **Step 3: Implement `build_scratch` + the canned task**

Append to `src/spec_runner/doctor.py` (add `import subprocess`, `import tempfile` at the top):

```python
DOCTOR_TIMEOUT_MIN = 3

_CANNED_TASK_MD = """# Doctor probe

### TASK-001: Doctor smoke probe
P0 | TODO

**Checklist:**
- [ ] Create a file named SMOKE.txt in the project root whose entire contents \
are exactly: PONG
"""


def _git(root: Path, *args: str) -> None:
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
    cfg.project_root = root
    cfg.spec_prefix = ""
    cfg.sync_deps = False
    cfg.create_git_branch = False
    cfg.run_tests_on_done = False
    cfg.run_lint_on_done = False
    cfg.task_budget_usd = budget
    cfg.task_timeout_minutes = timeout_min if timeout_min is not None else DOCTOR_TIMEOUT_MIN
    cfg.state_file = root / "spec" / ".executor-state.db"
    cfg.logs_dir = root / "spec" / ".executor-logs"

    if with_review:
        cfg.run_review = True
        cfg.auto_commit = True
        _git(root, "init")
        _git(root, "config", "user.email", "doctor@spec-runner.local")
        _git(root, "config", "user.name", "spec-runner doctor")
        # Baseline commit so review's `git diff HEAD~1` has a parent.
        (root / ".gitkeep").write_text("")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "doctor baseline")
    else:
        cfg.run_review = False
        cfg.auto_commit = False

    # Re-run path resolution now that project_root changed.
    cfg.__post_init__()
    return cfg, root
```

> Note: `ExecutorConfig.__post_init__` re-resolves `state_file`/`logs_dir` relative to `project_root`. Setting them before calling `__post_init__()` keeps them inside the scratch dir. Confirm the attribute names (`state_file`, `logs_dir`) by reading the dataclass; adjust if they differ.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_doctor.py -k build_scratch -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/doctor.py tests/test_doctor.py
git commit -m "feat(doctor): ephemeral scratch workspace + canned smoke task"
```

---

## Task 7: Fake CLI fixtures + `run_probe` (real execute_task integration)

**Files:**
- Create: `tests/fixtures/doctor/ok.sh`, `nocost.sh`, `nomarker.sh`, `noaction.sh`, `authfail.sh`
- Modify: `src/spec_runner/doctor.py` (`run_probe`)
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Create the fake CLI fixtures**

Each script ignores its args and emulates a CLI. The probe runs them with `cwd=scratch_root`, so file creation lands in the scratch root.

Create `tests/fixtures/doctor/ok.sh`:

```bash
#!/usr/bin/env bash
# Happy path: creates the file, prints the marker and a cost line.
printf 'PONG' > SMOKE.txt
echo "Created SMOKE.txt"
echo "input_tokens: 120  output_tokens: 8  cost: \$0.01"
echo "TASK_COMPLETE"
exit 0
```

Create `tests/fixtures/doctor/nocost.sh`:

```bash
#!/usr/bin/env bash
# Works, but emits no parseable cost.
printf 'PONG' > SMOKE.txt
echo "done"
echo "TASK_COMPLETE"
exit 0
```

Create `tests/fixtures/doctor/nomarker.sh`:

```bash
#!/usr/bin/env bash
# Exits 0, does the work, but never prints the marker.
printf 'PONG' > SMOKE.txt
echo "all finished, looks good"
exit 0
```

Create `tests/fixtures/doctor/noaction.sh`:

```bash
#!/usr/bin/env bash
# Prints the marker but never creates the file.
echo "I have completed the task."
echo "cost: \$0.01"
echo "TASK_COMPLETE"
exit 0
```

Create `tests/fixtures/doctor/authfail.sh`:

```bash
#!/usr/bin/env bash
echo "Error: invalid API key / not authenticated" >&2
exit 1
```

Make them executable:

```bash
chmod +x tests/fixtures/doctor/*.sh
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_doctor.py`:

```python
import os

from spec_runner.doctor import run_probe

FIXTURES = Path(__file__).parent / "fixtures" / "doctor"


def _probe_with(tmp_path, script_name):
    base = ExecutorConfig(
        project_root=tmp_path,
        claude_command=str(FIXTURES / script_name),
    )
    cfg, root = build_scratch(base, with_review=False, budget=0.5, timeout_min=1)
    try:
        attempt = run_probe(cfg)
        return extract(attempt, root, with_review=False)
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


def test_probe_ok(tmp_path):
    rep = _probe_with(tmp_path, "ok.sh")
    assert rep.verdict == "ready"
    assert rep.checks["completion_marker"].status == CHECK_OK
    assert rep.checks["task_action"].status == CHECK_OK


def test_probe_nomarker_broken(tmp_path):
    rep = _probe_with(tmp_path, "nomarker.sh")
    assert rep.checks["completion_marker"].status == CHECK_FAIL
    assert rep.verdict == "broken"


def test_probe_noaction_broken(tmp_path):
    rep = _probe_with(tmp_path, "noaction.sh")
    assert rep.checks["task_action"].status == CHECK_FAIL


def test_probe_authfail(tmp_path):
    rep = _probe_with(tmp_path, "authfail.sh")
    assert rep.checks["invocation"].status == CHECK_FAIL
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_doctor.py -k probe -v`
Expected: FAIL — `ImportError: cannot import name 'run_probe'`.

- [ ] **Step 4: Implement `run_probe`**

Append to `src/spec_runner/doctor.py` (add imports `from .execution import execute_task`, `from .state import ExecutorState`, `from .task import parse_tasks`):

```python
def run_probe(cfg: ExecutorConfig) -> TaskAttempt:
    """Run the canned task through the real execute_task() and return the
    recorded attempt."""
    tasks = parse_tasks(cfg.tasks_file)
    task = tasks[0]
    with ExecutorState(cfg) as state:
        execute_task(task, cfg, state)
        attempts = state.get_task_state(task.id).attempts
    return attempts[-1]
```

> Note: confirm `ExecutorState(cfg)` is the constructor and that `get_task_state(id).attempts` holds `TaskAttempt`s by reading state.py (verified: `__init__(self, config)`, `get_task_state` at state.py:399, `record_attempt` appends a `TaskAttempt`). The `with` block flushes SQLite via `__exit__`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_doctor.py -k probe -v`
Expected: PASS (4 tests). These exercise the real `execute_task` against fake CLIs.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/doctor/ src/spec_runner/doctor.py tests/test_doctor.py
git commit -m "feat(doctor): run_probe via real execute_task + fake CLI fixtures"
```

---

## Task 8: Cost gate + `run_doctor` orchestrator

`run_doctor` ties it together and returns an exit code. The cost gate prompts before billable calls unless `--yes`.

**Files:**
- Modify: `src/spec_runner/doctor.py`
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_doctor.py`:

```python
from spec_runner.doctor import run_doctor


def test_run_doctor_declined_makes_no_call(tmp_path, monkeypatch, capsys):
    base = ExecutorConfig(project_root=tmp_path, claude_command=str(FIXTURES / "ok.sh"))
    monkeypatch.setattr("builtins.input", lambda _="": "n")
    code = run_doctor(base, cli=None, model=None, with_review=False,
                      budget=0.5, timeout_min=1, assume_yes=False,
                      strict=False, as_json=False, keep=False)
    assert code == 2  # declined
    assert not (tmp_path / "SMOKE.txt").exists()


def test_run_doctor_ready_exit_zero(tmp_path, capsys):
    base = ExecutorConfig(project_root=tmp_path, claude_command=str(FIXTURES / "ok.sh"))
    code = run_doctor(base, cli=None, model=None, with_review=False,
                      budget=0.5, timeout_min=1, assume_yes=True,
                      strict=False, as_json=False, keep=False)
    assert code == 0
    assert "READY" in capsys.readouterr().out


def test_run_doctor_broken_exit_one(tmp_path):
    base = ExecutorConfig(project_root=tmp_path, claude_command=str(FIXTURES / "nomarker.sh"))
    code = run_doctor(base, cli=None, model=None, with_review=False,
                      budget=0.5, timeout_min=1, assume_yes=True,
                      strict=False, as_json=False, keep=False)
    assert code == 1


def test_run_doctor_strict_degraded_exit_one(tmp_path):
    base = ExecutorConfig(project_root=tmp_path, claude_command=str(FIXTURES / "nocost.sh"))
    code = run_doctor(base, cli=None, model=None, with_review=False,
                      budget=0.5, timeout_min=1, assume_yes=True,
                      strict=True, as_json=False, keep=False)
    assert code == 1  # degraded + strict


def test_run_doctor_json_output(tmp_path, capsys):
    base = ExecutorConfig(project_root=tmp_path, claude_command=str(FIXTURES / "ok.sh"))
    run_doctor(base, cli=None, model=None, with_review=False,
               budget=0.5, timeout_min=1, assume_yes=True,
               strict=False, as_json=True, keep=False)
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "ready"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_doctor.py -k run_doctor -v`
Expected: FAIL — `ImportError: cannot import name 'run_doctor'`.

- [ ] **Step 3: Implement the gate + orchestrator**

Append to `src/spec_runner/doctor.py` (add `import json`, `import shutil`, `import sys`):

```python
def _confirm(cfg: ExecutorConfig, with_review: bool, budget: float) -> bool:
    calls = 2 if with_review else 1
    model = cfg.claude_model or "(default)"
    print("spec-runner doctor — compatibility probe")
    print(f"  CLI:    {cfg.claude_command}")
    print(f"  Model:  {model}")
    if with_review:
        print(f"  Review: {cfg.review_command or cfg.claude_command} "
              f"({cfg.review_model or model})")
    print(f"  Budget: capped at ${budget:.2f} "
          f"(enforceable only if cost parsing is supported)")
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
    try:
        attempt = run_probe(cfg)
        report = extract(attempt, root, with_review)
        report.cli = target.claude_command
        report.model = target.claude_model
    finally:
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_doctor.py -k run_doctor -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/doctor.py tests/test_doctor.py
git commit -m "feat(doctor): cost gate + run_doctor orchestrator with exit codes"
```

---

## Task 9: CLI wiring — `cmd_doctor` + subparser + dispatch

**Files:**
- Modify: `src/spec_runner/cli.py` (`cmd_doctor`, `doctor` subparser, dispatch dict)
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Write the failing test (parser + dispatch)**

Append to `tests/test_doctor.py`:

```python
from spec_runner.cli import _build_parser


def test_doctor_parser_accepts_flags():
    parser = _build_parser()
    args = parser.parse_args(
        ["doctor", "--cli", "codex", "--model", "gpt-5.4", "--with-review",
         "--budget", "0.25", "--yes", "--strict", "--json", "--keep"]
    )
    assert args.command == "doctor"
    assert args.cli == "codex"
    assert args.model == "gpt-5.4"
    assert args.with_review is True
    assert args.budget == 0.25
    assert args.yes is True
    assert args.strict is True
    assert args.json is True
    assert args.keep is True


def test_doctor_parser_defaults():
    parser = _build_parser()
    args = parser.parse_args(["doctor"])
    assert args.cli is None
    assert args.with_review is False
    assert args.budget == 0.5
    assert args.yes is False
```

> Confirm the parser builder's name by reading `cli.py` (the project uses `_build_parser()` per CLAUDE.md). If it differs, use the real name.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_doctor.py -k doctor_parser -v`
Expected: FAIL — `argument command: invalid choice: 'doctor'`.

- [ ] **Step 3: Add the subparser**

In `src/spec_runner/cli.py`, inside `_build_parser()` next to the other `subparsers.add_parser(...)` calls (e.g. after the `mcp` parser), add:

```python
    doctor_parser = subparsers.add_parser(
        "doctor", parents=[common], help="Probe CLI/model compatibility (real mini-task)"
    )
    doctor_parser.add_argument("--cli", help="Override the CLI command (claude/codex/pi/...)")
    doctor_parser.add_argument("--model", help="Override the model (executor + review)")
    doctor_parser.add_argument(
        "--with-review", action="store_true", help="Also probe the review stage (2nd model call)"
    )
    doctor_parser.add_argument(
        "--budget", type=float, default=0.5, help="Budget cap in USD (default: 0.50)"
    )
    doctor_parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip the cost-gate confirmation"
    )
    doctor_parser.add_argument(
        "--strict", action="store_true", help="Exit non-zero on DEGRADED too"
    )
    doctor_parser.add_argument("--json", action="store_true", help="Machine-readable output")
    doctor_parser.add_argument("--keep", action="store_true", help="Keep the scratch workspace")
```

> The `common` parent parser already provides `--timeout` (and `--spec-prefix`, `--log-level`). doctor reads `args.timeout` (None unless the user passed it) and forwards it as `timeout_min`.

- [ ] **Step 4: Add `cmd_doctor` dispatcher**

In `src/spec_runner/cli.py`, near `cmd_run`, add:

```python
def cmd_doctor(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Run the CLI/model compatibility probe and exit with its status code."""
    from .doctor import run_doctor

    code = run_doctor(
        config,
        cli=args.cli,
        model=args.model,
        with_review=args.with_review,
        budget=args.budget,
        timeout_min=getattr(args, "timeout", None),
        assume_yes=args.yes,
        strict=args.strict,
        as_json=args.json,
        keep=args.keep,
    )
    raise SystemExit(code)
```

- [ ] **Step 5: Register in the dispatch dict**

In `cli.py:main()`, add to the `commands` dict:

```python
        "doctor": cmd_doctor,
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_doctor.py -k doctor_parser -v`
Expected: PASS (2 tests).

- [ ] **Step 7: Run the whole doctor suite + lint + types**

Run:
```bash
uv run pytest tests/test_doctor.py -v
uv run ruff format . && uv run ruff check .
uv run mypy src/spec_runner/doctor.py
```
Expected: all tests PASS, lint clean, mypy clean.

- [ ] **Step 8: Commit**

```bash
git add src/spec_runner/cli.py tests/test_doctor.py
git commit -m "feat(cli): wire spec-runner doctor subcommand"
```

---

## Task 10: `--with-review` end-to-end test (fake review CLI)

**Files:**
- Create: `tests/fixtures/doctor/review_ok.sh`, `tests/fixtures/doctor/review_nomarker.sh`
- Test: `tests/test_doctor.py`

- [ ] **Step 1: Create fake review CLIs**

Create `tests/fixtures/doctor/review_ok.sh`:

```bash
#!/usr/bin/env bash
# Reviewer that emits a recognized marker.
printf 'PONG' > SMOKE.txt
echo "Reviewed the diff, no issues."
echo "cost: \$0.01"
echo "TASK_COMPLETE"
echo "REVIEW_PASSED"
exit 0
```

> The same script is used for both the executor and review calls in the probe
> (doctor uses the same command for both unless `review_command` is set). It must
> create the file and print `TASK_COMPLETE` for the executor pass, and
> `REVIEW_PASSED` so the review extraction sees a marker.

Create `tests/fixtures/doctor/review_nomarker.sh`:

```bash
#!/usr/bin/env bash
printf 'PONG' > SMOKE.txt
echo "Looks fine to me overall."
echo "cost: \$0.01"
echo "TASK_COMPLETE"
exit 0
```

Make them executable:

```bash
chmod +x tests/fixtures/doctor/review_ok.sh tests/fixtures/doctor/review_nomarker.sh
```

- [ ] **Step 2: Write the failing test**

Append to `tests/test_doctor.py`:

```python
def _review_probe(tmp_path, script_name):
    base = ExecutorConfig(
        project_root=tmp_path,
        claude_command=str(FIXTURES / script_name),
    )
    cfg, root = build_scratch(base, with_review=True, budget=0.5, timeout_min=1)
    try:
        attempt = run_probe(cfg)
        return extract(attempt, root, with_review=True)
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.slow
def test_with_review_marker_ok(tmp_path):
    rep = _review_probe(tmp_path, "review_ok.sh")
    assert "review" in rep.checks
    assert rep.checks["review"].status in (CHECK_OK, CHECK_UNSUPPORTED)
    # When the reviewer prints REVIEW_PASSED, it should be detected.
    assert rep.checks["review"].status == CHECK_OK


@pytest.mark.slow
def test_with_review_no_marker_degraded(tmp_path):
    rep = _review_probe(tmp_path, "review_nomarker.sh")
    assert rep.checks["review"].status == CHECK_UNSUPPORTED
```

> Add `import pytest` at the top of the test file if not already present.
> These are marked `slow` because they exercise the full review path including
> git operations in the scratch repo. If `attempt.review_findings` is empty
> because the review path didn't run, read `src/spec_runner/hooks.py:302-345`
> and `review.py` to confirm the review output is propagated; the design relies
> on `post_done_hook` returning the raw review output as the 4th tuple element
> (verified during planning).

- [ ] **Step 3: Run test to verify it fails, then passes**

Run: `uv run pytest tests/test_doctor.py -k with_review -v -m slow`
Expected first: FAIL (fixtures missing) → after Step 1, PASS.

If `review` check is unexpectedly `na`/`fail`, debug by running with `--keep`
semantics: temporarily build the scratch with `keep=True` in the test and
inspect the review behavior. Do not weaken the assertion to make it pass.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/doctor/review_ok.sh tests/fixtures/doctor/review_nomarker.sh tests/test_doctor.py
git commit -m "test(doctor): --with-review e2e probes (marker recognized vs not)"
```

---

## Task 11: Documentation

**Files:**
- Modify: `README.md` (CLI commands + a Doctor section)
- Modify: `CLAUDE.md` (module table + CLI list + test file list)
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Update README**

In `README.md`, under the `spec-runner` CLI command list (the "Integration" or a new "Diagnostics" block), add:

```bash
# Diagnostics
spec-runner doctor                         # Probe the configured CLI/model (real mini-task)
spec-runner doctor --cli=codex --model=gpt-5.4  # Probe an ad-hoc CLI+model
spec-runner doctor --with-review           # Also probe the review stage
spec-runner doctor --json --yes            # Machine-readable, no confirmation (CI)
spec-runner doctor --strict                # Exit non-zero on DEGRADED too
```

And add a short subsection after "Supported CLIs":

```markdown
### Checking CLI/model compatibility

`spec-runner doctor` runs a real one-task probe through the actual execution
path and reports, per capability, whether your CLI/model works:

- **invocation** — the command runs and authenticates
- **completion_marker** — the model prints `TASK_COMPLETE` (not all models do)
- **task_action** — the model actually performs the work
- **cost_tracking** — token/cost parsing works (needed for `costs`/`--budget`)
- **error_classification** — failures are classified (diagnostic)
- **review** *(with `--with-review`)* — the reviewer prints `REVIEW_PASSED`/`FAILED`

Verdict: **READY** / **DEGRADED** (works, but something like cost tracking is
unavailable) / **BROKEN**. It makes real, billable model calls (capped by
`--budget`, default $0.50) and asks for confirmation unless `--yes`.
```

- [ ] **Step 2: Update CLAUDE.md**

In the module table, add a row:

```markdown
| `doctor.py` | ~210 | CLI/model compatibility probe: ephemeral scratch workspace + real `execute_task()`, raw-signal extraction (marker/action/cost/error/review), READY/DEGRADED/BROKEN verdict, `--json` schema (`schemas/doctor-result.schema.json`) |
```

In the CLI entry-points block, add:

```bash
spec-runner doctor                         # Probe CLI/model compatibility (real mini-task)
spec-runner doctor --cli=codex --model=X   # Ad-hoc CLI+model probe
spec-runner doctor --with-review --json    # Include review stage, machine output
```

In the Testing section's test-file list, add `test_doctor.py` (doctor extraction, scratch builder, probe via fake CLIs, parser, run_doctor exit codes). Also note the new `config.sync_deps` flag in the `config.py` row and the `pre_start_hook` gate in the `hooks.py` description.

- [ ] **Step 3: Update CHANGELOG**

Under `## [Unreleased]`, add:

```markdown
### Added

- **`spec-runner doctor`** — empirical CLI/model compatibility probe. Runs a
  real one-task run through `execute_task()` against the configured (or
  `--cli`/`--model`) backend and reports per-capability status (invocation,
  completion marker, task action, cost tracking, error classification, optional
  `--with-review`) with a READY/DEGRADED/BROKEN verdict. `--json` output is
  pinned by `schemas/doctor-result.schema.json`. Budget-capped (default $0.50)
  with a confirmation gate (`--yes` to skip); `--strict` fails CI on DEGRADED.
- **`sync_deps` config flag** — gates the `uv sync` step in `pre_start_hook`
  (doctor disables it for the scratch workspace).
```

- [ ] **Step 4: Verify docs build / no broken references + run full suite**

Run:
```bash
uv run ruff check .
uv run pytest tests/ -m "not slow"
```
Expected: lint clean, full non-slow suite green.

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md CHANGELOG.md
git commit -m "docs: document spec-runner doctor and sync_deps flag"
```

---

## Final verification

- [ ] **Run the complete suite (incl. slow doctor e2e):**

```bash
uv run pytest tests/test_doctor.py -v
uv run pytest tests/ -m "not slow"
uv run ruff format --check . && uv run ruff check . && uv run mypy src
```
Expected: all green.

- [ ] **Manual smoke (optional, real CLI):** with an authenticated CLI,
  `spec-runner doctor --cli=claude --yes` and confirm a READY/DEGRADED report.
  (Not run in CI — no auth.)
