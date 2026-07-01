"""Tests for `plan --gated` — single-stage generation, validation, DRAFT write."""

from pathlib import Path
from types import SimpleNamespace

from spec_runner.cli_plan import run_gated_stage
from spec_runner.spec import SpecMeta, read_spec_meta, write_spec


def _cfg(tmp_path: Path):
    spec = tmp_path / "spec"
    return SimpleNamespace(
        project_root=tmp_path,
        requirements_file=spec / "requirements.md",
        design_file=spec / "design.md",
        tasks_file=spec / "tasks.md",
        spec_lock_file=spec / ".spec.lock",
        claude_command="claude",
        claude_model="",
        command_template="",
        skip_permissions=True,
        task_timeout_minutes=1,
    )


GOOD_REQ_BODY = """# Requirements

## Out of Scope
- none

#### REQ-001: X
**Acceptance Criteria:**
GIVEN a WHEN b THEN c
"""


def _fake_invoke(output: str):
    def _run(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    return _run


def test_gated_stage_writes_draft_with_frontmatter(tmp_path: Path):
    cfg = _cfg(tmp_path)
    out = f"SPEC_REQUIREMENTS_READY\n{GOOD_REQ_BODY}\nSPEC_REQUIREMENTS_END\n"
    rc = run_gated_stage("requirements", "Build X", cfg, invoke=_fake_invoke(out))
    assert rc == 0
    meta = read_spec_meta(cfg.requirements_file)
    assert meta is not None and meta.status == "draft" and meta.spec_stage == "requirements"
    assert meta.source_prompt_version.startswith("sha256:")
    assert meta.validation == "pass"


def test_gated_stage_gate_requires_upstream_approved(tmp_path: Path):
    cfg = _cfg(tmp_path)
    # requirements only draft -> generating design must refuse.
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), GOOD_REQ_BODY)
    rc = run_gated_stage(
        "design", "Build X", cfg, invoke=_fake_invoke("SPEC_DESIGN_READY\nx\nSPEC_DESIGN_END")
    )
    assert rc != 0
    assert not cfg.design_file.exists()


def test_gated_stage_generation_failure_returns_nonzero(tmp_path: Path):
    cfg = _cfg(tmp_path)

    def _fail(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    rc = run_gated_stage("requirements", "Build X", cfg, invoke=_fail)
    assert rc == 1
    assert not cfg.requirements_file.exists()


def test_gated_stage_missing_marker_returns_nonzero(tmp_path: Path):
    cfg = _cfg(tmp_path)
    rc = run_gated_stage(
        "requirements",
        "Build X",
        cfg,
        invoke=_fake_invoke("no markers here"),
    )
    assert rc == 1
    assert not cfg.requirements_file.exists()
