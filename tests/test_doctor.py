import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from jsonschema import Draft7Validator

from spec_runner.cli import _build_parser
from spec_runner.config import ExecutorConfig, Persona, build_config
from spec_runner.doctor import (
    CHECK_FAIL,
    CHECK_NA,
    CHECK_OK,
    CHECK_UNSUPPORTED,
    DOCTOR_TIMEOUT_MIN,
    CheckResult,
    DoctorReport,
    build_scratch,
    extract,
    render_human,
    report_to_dict,
    resolve_target,
    run_doctor,
    run_probe,
)
from spec_runner.hooks import pre_start_hook
from spec_runner.state import TaskAttempt
from spec_runner.task import Task, parse_tasks

FIXTURES = Path(__file__).parent / "fixtures" / "doctor"


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


# ---------------------------------------------------------------------------
# Task 5: resolve_target
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Task 6: build_scratch
# ---------------------------------------------------------------------------


def test_build_scratch_executor_only(tmp_path):
    base = ExecutorConfig(project_root=tmp_path, claude_command="claude")
    cfg, root = build_scratch(base, with_review=False, budget=0.5, timeout_min=None)
    try:
        assert cfg.sync_deps is False
        assert cfg.create_git_branch is False
        assert cfg.auto_commit is False
        assert cfg.run_tests_on_done is False
        assert cfg.run_lint_on_done is False
        assert cfg.run_review is False
        assert cfg.task_budget_usd == 0.5
        assert cfg.task_timeout_minutes == DOCTOR_TIMEOUT_MIN
        tasks = parse_tasks(cfg.tasks_file)
        assert tasks and tasks[0].id == "TASK-001"
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
        log = subprocess.run(["git", "log", "--oneline"], cwd=root, capture_output=True, text=True)
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


# ---------------------------------------------------------------------------
# Task 7: fake CLI fixtures + run_probe
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Task 8: cost gate + run_doctor orchestrator
# ---------------------------------------------------------------------------


def test_run_doctor_declined_makes_no_call(tmp_path, monkeypatch):
    base = ExecutorConfig(project_root=tmp_path, claude_command=str(FIXTURES / "ok.sh"))
    monkeypatch.setattr("builtins.input", lambda _="": "n")
    code = run_doctor(
        base,
        cli=None,
        model=None,
        with_review=False,
        budget=0.5,
        timeout_min=1,
        assume_yes=False,
        strict=False,
        as_json=False,
        keep=False,
    )
    assert code == 2
    assert not (tmp_path / "SMOKE.txt").exists()


def test_run_doctor_ready_exit_zero(tmp_path, capsys):
    base = ExecutorConfig(project_root=tmp_path, claude_command=str(FIXTURES / "ok.sh"))
    code = run_doctor(
        base,
        cli=None,
        model=None,
        with_review=False,
        budget=0.5,
        timeout_min=1,
        assume_yes=True,
        strict=False,
        as_json=False,
        keep=False,
    )
    assert code == 0
    assert "READY" in capsys.readouterr().out


def test_run_doctor_broken_exit_one(tmp_path):
    base = ExecutorConfig(project_root=tmp_path, claude_command=str(FIXTURES / "nomarker.sh"))
    code = run_doctor(
        base,
        cli=None,
        model=None,
        with_review=False,
        budget=0.5,
        timeout_min=1,
        assume_yes=True,
        strict=False,
        as_json=False,
        keep=False,
    )
    assert code == 1


def test_run_doctor_strict_degraded_exit_one(tmp_path):
    base = ExecutorConfig(project_root=tmp_path, claude_command=str(FIXTURES / "nocost.sh"))
    code = run_doctor(
        base,
        cli=None,
        model=None,
        with_review=False,
        budget=0.5,
        timeout_min=1,
        assume_yes=True,
        strict=True,
        as_json=False,
        keep=False,
    )
    assert code == 1


def test_run_doctor_json_output(tmp_path, capsys):
    base = ExecutorConfig(project_root=tmp_path, claude_command=str(FIXTURES / "ok.sh"))
    run_doctor(
        base,
        cli=None,
        model=None,
        with_review=False,
        budget=0.5,
        timeout_min=1,
        assume_yes=True,
        strict=False,
        as_json=True,
        keep=False,
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["verdict"] == "ready"


def test_run_doctor_json_with_keep_is_clean(tmp_path, capsys):
    import re
    import shutil

    base = ExecutorConfig(project_root=tmp_path, claude_command=str(FIXTURES / "ok.sh"))
    run_doctor(
        base,
        cli=None,
        model=None,
        with_review=False,
        budget=0.5,
        timeout_min=1,
        assume_yes=True,
        strict=False,
        as_json=True,
        keep=True,
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)  # stdout must be a single valid JSON object
    assert payload["verdict"] == "ready"
    # --keep leaves the scratch dir; clean it up so the test doesn't litter /tmp.
    # The path is announced on stderr as "(scratch kept at <root>)".
    match = re.search(r"\(scratch kept at (.+)\)", captured.err)
    if match:
        shutil.rmtree(match.group(1), ignore_errors=True)


@pytest.mark.slow
def test_run_doctor_json_with_review_is_clean(tmp_path, capfd):
    base = ExecutorConfig(project_root=tmp_path, claude_command=str(FIXTURES / "review_ok.sh"))
    run_doctor(
        base,
        cli=None,
        model=None,
        with_review=True,
        budget=0.5,
        timeout_min=1,
        assume_yes=True,
        strict=False,
        as_json=True,
        keep=False,
    )
    out = capfd.readouterr().out
    payload = json.loads(out)  # must be a single valid JSON object (no git leak)
    assert payload["verdict"] in ("ready", "degraded")
    assert payload["review"] is True


def test_doctor_parser_accepts_flags():
    parser = _build_parser()
    args = parser.parse_args(
        [
            "doctor",
            "--cli",
            "codex",
            "--model",
            "gpt-5.4",
            "--with-review",
            "--budget",
            "0.25",
            "--yes",
            "--strict",
            "--json",
            "--keep",
        ]
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


# ---------------------------------------------------------------------------
# Task 10: --with-review e2e probes (fake review CLIs)
# ---------------------------------------------------------------------------


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
    assert rep.checks["review"].status == CHECK_OK


@pytest.mark.slow
def test_with_review_no_marker_degraded(tmp_path):
    rep = _review_probe(tmp_path, "review_nomarker.sh")
    assert rep.checks["review"].status == CHECK_UNSUPPORTED


# ---------------------------------------------------------------------------
# Copilot review fixes
# ---------------------------------------------------------------------------


def test_extract_tokens_without_cost_not_enforceable(tmp_path):
    """Fix 1: tokens-only must be UNSUPPORTED/not-enforceable, not OK."""
    _write_smoke(tmp_path)
    att = _attempt(claude_output="TASK_COMPLETE", cost_usd=None, input_tokens=120)
    rep = extract(att, tmp_path, with_review=False)
    assert rep.checks["cost_tracking"].status == CHECK_UNSUPPORTED
    assert rep.budget_enforceable is False
    assert "None" not in rep.checks["cost_tracking"].detail
    assert rep.verdict == "degraded"


def test_build_scratch_isolates_plugins_dir(tmp_path):
    """Fix 2: plugins_dir must be resolved under the scratch root, not real project."""
    base = ExecutorConfig(project_root=tmp_path, claude_command="claude")
    cfg, root = build_scratch(base, with_review=False, budget=0.5, timeout_min=1)
    try:
        assert str(cfg.plugins_dir).startswith(str(root.resolve()))
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


def test_run_probe_no_progress_file_pollution(tmp_path, monkeypatch):
    """Fix 3: progress file must NOT leak into the caller's CWD."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    proj = tmp_path / "proj"
    proj.mkdir()
    base = ExecutorConfig(project_root=proj, claude_command=str(FIXTURES / "ok.sh"))
    cfg, root = build_scratch(base, with_review=False, budget=0.5, timeout_min=1)
    try:
        run_probe(cfg)
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)
    assert not (cwd / "spec" / ".executor-progress.txt").exists()
