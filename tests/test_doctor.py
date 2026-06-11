import json
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft7Validator

from spec_runner.config import ExecutorConfig, build_config
from spec_runner.doctor import (
    CHECK_FAIL,
    CHECK_NA,
    CHECK_OK,
    CHECK_UNSUPPORTED,
    CheckResult,
    DoctorReport,
    extract,
    render_human,
    report_to_dict,
)
from spec_runner.hooks import pre_start_hook
from spec_runner.state import TaskAttempt
from spec_runner.task import Task


def test_sync_deps_defaults_true():
    cfg = ExecutorConfig(project_root=Path("."))
    assert cfg.sync_deps is True


def test_build_config_reads_sync_deps_false():
    cfg = build_config({"sync_deps": False}, args=None)
    assert cfg.sync_deps is False


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


def _attempt(**kw) -> TaskAttempt:
    base: dict[str, object] = {"timestamp": "t", "success": True, "duration_seconds": 1.0}
    base.update(kw)
    return TaskAttempt(**base)  # type: ignore[arg-type]


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


# ---------------------------------------------------------------------------
# Task 4: rendering + JSON schema
# ---------------------------------------------------------------------------


def _ready_report() -> DoctorReport:
    return DoctorReport(
        cli="codex",
        model="gpt-5.4",
        review=False,
        checks={
            "invocation": CheckResult(CHECK_OK, "exit 0 in 7.2s"),
            "completion_marker": CheckResult(CHECK_OK),
            "task_action": CheckResult(CHECK_OK),
            "cost_tracking": CheckResult(CHECK_UNSUPPORTED, "no cost"),
            "error_classification": CheckResult(CHECK_NA),
        },
        measured_cost_usd=None,
        duration_s=9.1,
        budget_enforceable=False,
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
    Draft7Validator.check_schema(schema)
    Draft7Validator(schema).validate(report_to_dict(_ready_report()))
