"""Tests for `plan --gated` — single-stage generation, validation, DRAFT write."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from spec_runner import cli_plan
from spec_runner.cli_plan import run_gated_stage
from spec_runner.spec import STAGES, SpecMeta, read_spec_meta, stage_path, write_spec


def _cfg(tmp_path: Path):
    from spec_runner.spec import LITE

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
        spec_context="",
        spec_rules={},
        spec_prefix="",
        resolve_spec_profile=lambda: LITE,
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


def _good_out() -> str:
    return f"SPEC_REQUIREMENTS_READY\n{GOOD_REQ_BODY}\nSPEC_REQUIREMENTS_END\n"


def test_interactive_approve_via_menu(tmp_path: Path):
    cfg = _cfg(tmp_path)
    rc = run_gated_stage(
        "requirements",
        "Build X",
        cfg,
        invoke=_fake_invoke(_good_out()),
        interactive=True,
        input_fn=lambda _: "a",
    )
    assert rc == 0
    meta = read_spec_meta(cfg.requirements_file)
    assert meta is not None and meta.status == "approved"


def test_interactive_stop_leaves_draft(tmp_path: Path):
    cfg = _cfg(tmp_path)
    inputs = iter(["s"])
    rc = run_gated_stage(
        "requirements",
        "Build X",
        cfg,
        invoke=_fake_invoke(_good_out()),
        interactive=True,
        input_fn=lambda _: next(inputs),
    )
    assert rc == 0
    meta = read_spec_meta(cfg.requirements_file)
    assert meta is not None and meta.status == "draft"


def test_interactive_edit_then_stop_calls_editor(tmp_path: Path):
    cfg = _cfg(tmp_path)
    inputs = iter(["e", "s"])
    editor_calls = []

    def _editor(path):
        editor_calls.append(path)
        # Mutate the file to a valid body (still DRAFT) so re-validation passes.
        meta = read_spec_meta(path)
        write_spec(path, meta, GOOD_REQ_BODY, lock=None)

    rc = run_gated_stage(
        "requirements",
        "Build X",
        cfg,
        invoke=_fake_invoke(_good_out()),
        interactive=True,
        input_fn=lambda _: next(inputs),
        editor_fn=_editor,
    )
    assert rc == 0
    assert len(editor_calls) == 1
    meta = read_spec_meta(cfg.requirements_file)
    assert meta is not None and meta.status == "draft"


def test_interactive_regenerate_then_stop_invokes_again(tmp_path: Path):
    cfg = _cfg(tmp_path)
    inputs = iter(["r", "s"])
    calls = {"n": 0}

    def _invoke(cmd, **kwargs):
        calls["n"] += 1
        return SimpleNamespace(returncode=0, stdout=_good_out(), stderr="")

    rc = run_gated_stage(
        "requirements",
        "Build X",
        cfg,
        invoke=_invoke,
        interactive=True,
        input_fn=lambda _: next(inputs),
    )
    assert rc == 0
    assert calls["n"] == 2
    meta = read_spec_meta(cfg.requirements_file)
    assert meta is not None and meta.status == "draft"


def _plan_args(*, no_interactive: bool = False) -> SimpleNamespace:
    """Build the argparse-shaped namespace `cmd_plan` reads for `--gated`."""
    return SimpleNamespace(
        description="Build X",
        from_file=None,
        full=False,
        gated=True,
        stage=None,
        no_interactive=no_interactive,
    )


def test_gated_interactive_auto_continue_terminates_at_first_draft(tmp_path, monkeypatch):
    """Drive `cmd_plan`'s interactive auto-continue loop across two stages and prove
    it stops instead of looping forever.

    `run_gated_stage` is monkeypatched with a spy: call #1 (requirements) writes an
    APPROVED stage, so `resolve_next_stage` advances to design; call #2 (design)
    leaves the stage as a DRAFT (simulating the user picking "stop" in the real
    checkpoint menu), so the loop's top-of-iteration `_print_gate_status` check
    sees `await_approval` and breaks. A hard call-count assertion (> len(STAGES))
    guards against a hang/infinite loop if the termination logic ever regresses —
    the test can never block on real I/O since `run_gated_stage` itself is stubbed.
    """
    cfg = _cfg(tmp_path)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    calls: list[str] = []

    def _fake_run_gated_stage(stage, description, config, invoke=None, **kwargs):
        calls.append(stage)
        assert len(calls) <= len(STAGES), "run_gated_stage looped past len(STAGES) calls"
        if len(calls) == 1:
            # Simulate the user approving requirements via the checkpoint menu.
            write_spec(
                stage_path(config, stage),
                SpecMeta(spec_stage=stage, status="approved", version=2),
                GOOD_REQ_BODY,
            )
        else:
            # Simulate the user picking "stop": design stays a DRAFT.
            write_spec(
                stage_path(config, stage),
                SpecMeta(spec_stage=stage, status="draft"),
                "# Design\n",
            )
        return 0

    monkeypatch.setattr(cli_plan, "run_gated_stage", _fake_run_gated_stage)

    cli_plan.cmd_plan(_plan_args(), cfg)

    assert calls == ["requirements", "design"]
    design_meta = read_spec_meta(cfg.design_file)
    assert design_meta is not None and design_meta.status == "draft"


def test_gated_no_interactive_does_not_auto_continue(tmp_path, monkeypatch):
    """`--no-interactive` (or a non-TTY stdout) must engage the single-stage path:
    exactly one `run_gated_stage` call, no auto-continue loop, exit via SystemExit.
    """
    cfg = _cfg(tmp_path)
    # isatty() would say "interactive" if the flag didn't override it.
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    calls = {"n": 0}

    def _fake_run_gated_stage(stage, description, config, invoke=None, **kwargs):
        calls["n"] += 1
        return 0

    monkeypatch.setattr(cli_plan, "run_gated_stage", _fake_run_gated_stage)

    with pytest.raises(SystemExit) as exc_info:
        cli_plan.cmd_plan(_plan_args(no_interactive=True), cfg)

    assert exc_info.value.code == 0
    assert calls["n"] == 1


def test_regenerate_draft_preserves_existing_version(tmp_path: Path):
    """Regenerating a stage that already has a managed frontmatter (e.g. an
    approved/stale stage at version 3) must preserve that version instead of
    resetting the counter to 1 (Copilot PR#28)."""
    cfg = _cfg(tmp_path)
    write_spec(
        cfg.requirements_file,
        SpecMeta(spec_stage="requirements", status="approved", version=3),
        GOOD_REQ_BODY,
    )

    rc = cli_plan._generate_stage_draft(
        "requirements", "Build X", cfg, invoke=_fake_invoke(_good_out())
    )

    assert rc == 0
    meta = read_spec_meta(cfg.requirements_file)
    assert meta is not None
    assert meta.version == 3
    assert meta.status == "draft"


def test_open_editor_splits_editor_with_args(monkeypatch, tmp_path: Path):
    """`$EDITOR` with embedded arguments (e.g. "code --wait") must be
    shell-word-split, not passed as a single (invalid) argv element."""
    monkeypatch.setenv("EDITOR", "myed --wait")
    captured = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv

    monkeypatch.setattr(cli_plan.subprocess, "run", _fake_run)

    path = tmp_path / "requirements.md"
    cli_plan._open_editor(path)

    assert captured["argv"] == ["myed", "--wait", str(path)]
