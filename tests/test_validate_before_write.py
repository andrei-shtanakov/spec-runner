"""Generation writes a file only after it validates, and repairs boundedly (#160).

The order was: generate → write → validate → record the verdict on the file
just written. So an invalid spec landed on disk and stayed there, and the
operator got a DRAFT that looks like an artifact and is not one.

The owner's decision on the shape (rejecting the general normalizer proposed in
#133): generate, validate with **the same parser contract the runtime uses**,
on failure re-generate a bounded number of times with the concrete diagnostics
fed back, and write only once it passes. Unrecognized output is rejected, never
rewritten — a tool that canonicalizes its own guess about an unfamiliar format
makes the mistake invisible.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from spec_runner.cli_plan import run_gated_stage
from spec_runner.spec import LITE, read_spec_meta

GOOD_REQ = """# Requirements

## Out of Scope
- none

#### REQ-001: X
**Acceptance Criteria:**
GIVEN a WHEN b THEN c
"""

# No REQ heading and no Out of Scope: two hard validation errors.
BAD_REQ = "# Requirements\n\nSome prose the model felt like writing.\n"


def _cfg(tmp_path: Path, **overrides):
    spec = tmp_path / "spec"
    spec.mkdir(parents=True, exist_ok=True)
    defaults = dict(
        project_root=tmp_path,
        requirements_file=spec / "requirements.md",
        design_file=spec / "design.md",
        tasks_file=spec / "tasks.md",
        spec_lock_file=spec / ".spec.lock",
        spec_dir=spec,
        claude_command="claude",
        claude_model="",
        command_template="",
        skip_permissions=True,
        task_timeout_minutes=1,
        spec_context="",
        spec_rules={},
        spec_prefix="",
        spec_repair_attempts=2,
        resolve_spec_profile=lambda: LITE,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _wrap(body: str) -> str:
    return f"SPEC_REQUIREMENTS_READY\n{body}\nSPEC_REQUIREMENTS_END\n"


def _invoker(bodies: list[str], prompts: list[str] | None = None):
    """Return a fake CLI that yields `bodies` in order, recording prompts."""
    calls = {"n": 0}

    def _run(cmd, **kwargs):
        idx = min(calls["n"], len(bodies) - 1)
        calls["n"] += 1
        if prompts is not None:
            prompts.append(" ".join(cmd) if isinstance(cmd, list) else str(cmd))
        return SimpleNamespace(returncode=0, stdout=_wrap(bodies[idx]), stderr="")

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


class TestInvalidOutputNeverLands:
    def test_no_file_is_created_when_validation_fails(self, tmp_path):
        cfg = _cfg(tmp_path, spec_repair_attempts=0)
        rc = run_gated_stage("requirements", "Build X", cfg, invoke=_invoker([BAD_REQ]))
        assert rc != 0, "a spec that never validated reported success"
        assert not cfg.requirements_file.exists(), "invalid spec was left on disk"

    def test_an_existing_file_is_left_untouched(self, tmp_path):
        """A failed regeneration must not destroy the previous good draft."""
        cfg = _cfg(tmp_path)
        assert run_gated_stage("requirements", "X", cfg, invoke=_invoker([GOOD_REQ])) == 0
        good = cfg.requirements_file.read_text()

        rc = run_gated_stage(
            "requirements", "X", _cfg(tmp_path, spec_repair_attempts=0), invoke=_invoker([BAD_REQ])
        )
        assert rc != 0
        assert cfg.requirements_file.read_text() == good, "the previous draft was clobbered"

    def test_valid_output_still_writes_and_passes(self, tmp_path):
        cfg = _cfg(tmp_path)
        assert run_gated_stage("requirements", "X", cfg, invoke=_invoker([GOOD_REQ])) == 0
        meta = read_spec_meta(cfg.requirements_file)
        assert meta is not None and meta.status == "draft" and meta.validation == "pass"


class TestBoundedRepair:
    def test_a_second_attempt_can_succeed(self, tmp_path):
        cfg = _cfg(tmp_path, spec_repair_attempts=2)
        invoke = _invoker([BAD_REQ, GOOD_REQ])
        assert run_gated_stage("requirements", "X", cfg, invoke=invoke) == 0
        assert invoke.calls["n"] == 2
        assert cfg.requirements_file.exists()

    def test_attempts_are_bounded(self, tmp_path):
        cfg = _cfg(tmp_path, spec_repair_attempts=2)
        invoke = _invoker([BAD_REQ])
        assert run_gated_stage("requirements", "X", cfg, invoke=invoke) != 0
        assert invoke.calls["n"] == 3, "expected the first attempt plus two repairs"

    def test_zero_repairs_means_one_attempt(self, tmp_path):
        cfg = _cfg(tmp_path, spec_repair_attempts=0)
        invoke = _invoker([BAD_REQ])
        run_gated_stage("requirements", "X", cfg, invoke=invoke)
        assert invoke.calls["n"] == 1

    def test_the_repair_prompt_carries_the_actual_errors(self, tmp_path):
        """ "Try again" is not a diagnostic — the model needs what was wrong."""
        prompts: list[str] = []
        cfg = _cfg(tmp_path, spec_repair_attempts=1)
        run_gated_stage(
            "requirements", "X", cfg, invoke=_invoker([BAD_REQ, GOOD_REQ], prompts=prompts)
        )
        assert len(prompts) == 2
        assert "Out of Scope" in prompts[1], prompts[1][:400]
        assert "REQ" in prompts[1]

    def test_the_first_prompt_has_no_repair_section(self, tmp_path):
        prompts: list[str] = []
        cfg = _cfg(tmp_path)
        run_gated_stage("requirements", "X", cfg, invoke=_invoker([GOOD_REQ], prompts=prompts))
        assert "previous attempt" not in prompts[0].lower()


class TestSameContractAsRuntime:
    def test_validation_uses_the_runtime_validator(self, tmp_path, monkeypatch):
        """Not a lookalike check: a second implementation drifts from the one
        the run actually enforces, and then the spec passes here and fails
        there."""
        from spec_runner import cli_plan

        seen: list[str] = []
        real = cli_plan.validate_spec_stage

        def _spy(stage, config, profile):
            seen.append(stage)
            return real(stage, config, profile)

        monkeypatch.setattr(cli_plan, "validate_spec_stage", _spy)
        run_gated_stage("requirements", "X", _cfg(tmp_path), invoke=_invoker([GOOD_REQ]))
        assert seen == ["requirements"]


class TestNothingIsRewritten:
    def test_the_written_body_is_byte_identical_to_the_generated_one(self, tmp_path):
        """No canonicalization: the tool must not silently reshape output it
        did not fully understand (#133 decision)."""
        from spec_runner.spec import read_spec_body

        cfg = _cfg(tmp_path)
        run_gated_stage("requirements", "X", cfg, invoke=_invoker([GOOD_REQ]))
        assert read_spec_body(cfg.requirements_file).strip() == GOOD_REQ.strip()


@pytest.mark.parametrize("attempts", [0, 1, 3])
def test_repair_budget_is_configurable(tmp_path, attempts):
    cfg = _cfg(tmp_path, spec_repair_attempts=attempts)
    invoke = _invoker([BAD_REQ])
    run_gated_stage("requirements", "X", cfg, invoke=invoke)
    assert invoke.calls["n"] == attempts + 1
