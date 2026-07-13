"""Zero-behaviour-change invariant for the C1 STAGES→profile refactor (TASK-307).

C1 replaced three hardcoded stage maps (prompt markers, templates, validators)
with a single `StageProfile` model. REQ-305/REQ-307 require the default `lite`
profile to be byte-for-byte identical to the pre-C1 behaviour. These tests lock
that surface so any future drift in `lite` is caught:

- `SPEC_STAGES` export is frozen to its pre-C1 shape (marker + instruction).
- The gated generation prompts for every stage match committed golden fixtures
  (regenerate deliberately with `--update-golden`).
- The full `plan --gated` pipeline writes identical requirements/design/tasks.
- An unknown `--profile` fails with a clean message, never a traceback.

The one-time proof that `lite == pre-C1` was a git-worktree diff of the pre-C1
commit against HEAD; these tests keep the invariant from regressing.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from spec_runner import cli, cli_plan
from spec_runner.config import ConfigError, ExecutorConfig
from spec_runner.prompt import SPEC_STAGES, build_gated_generation_prompt
from spec_runner.spec import read_spec_meta, stage_path, write_spec

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "c1-zero-behaviour"

# Upstream context reused when building the design/tasks prompts (mirrors the
# fixture-generation inputs — keep in sync when regenerating goldens).
_REQ_CTX = "# Requirements\n\n#### REQ-001: X\n**Acceptance Criteria:**\nGIVEN a WHEN b THEN c\n"
_DES_CTX = "# Design\n\n### DESIGN-001: Y trace [REQ-001]\n"
_DESC = "Build a widget factory with retries"

_GATED_CONTEXT = {
    "requirements": {},
    "design": {"requirements": _REQ_CTX},
    "tasks": {"requirements": _REQ_CTX, "design": _DES_CTX},
}


@pytest.fixture
def update_golden(request) -> bool:
    return bool(request.config.getoption("--update-golden", default=False))


def test_spec_stages_export_frozen() -> None:
    """`SPEC_STAGES` must keep its pre-C1 shape: same names, markers, instructions."""
    assert list(SPEC_STAGES) == ["requirements", "design", "tasks"]
    assert SPEC_STAGES["requirements"]["marker"] == "SPEC_REQUIREMENTS"
    assert SPEC_STAGES["design"]["marker"] == "SPEC_DESIGN"
    assert SPEC_STAGES["tasks"]["marker"] == "SPEC_TASKS"
    for stage in SPEC_STAGES.values():
        assert set(stage) == {"marker", "instruction"}
        # Markers are embedded verbatim in each instruction's READY/END fence.
        assert f"{stage['marker']}_READY" in stage["instruction"]
        assert f"{stage['marker']}_END" in stage["instruction"]


@pytest.mark.parametrize("stage", ["requirements", "design", "tasks"])
def test_gated_prompt_matches_golden(stage: str, update_golden: bool) -> None:
    """Each gated generation prompt is byte-for-byte the pre-C1 golden."""
    prompt = build_gated_generation_prompt(stage, _DESC, _GATED_CONTEXT[stage])
    golden = FIXTURES_DIR / f"gated_prompt_{stage}.txt"
    if update_golden:
        golden.write_text(prompt, encoding="utf-8")
    assert prompt == golden.read_text(encoding="utf-8")


def _pipeline_cfg(tmp_path: Path) -> SimpleNamespace:
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
    )


_BODIES = {
    "requirements": "# Requirements\n\n## Out of Scope\n- none\n\n"
    "#### REQ-001: X\n**Acceptance Criteria:**\nGIVEN a WHEN b THEN c\n",
    "design": "# Design\n\n### DESIGN-001: Y trace [REQ-001]\n",
    "tasks": "# Tasks\n\n### TASK-001: Do X [REQ-001] [DESIGN-001]\nP0 | TODO\n- [ ] do it\n",
}
_MARK = {"requirements": "SPEC_REQUIREMENTS", "design": "SPEC_DESIGN", "tasks": "SPEC_TASKS"}


def _fake_invoke(stage: str):
    body = _BODIES[stage]
    out = f"{_MARK[stage]}_READY\n{body}\n{_MARK[stage]}_END\n"
    return lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=out, stderr="")


def _normalize(text: str) -> str:
    """Strip volatile frontmatter fields (timestamps) before golden comparison."""
    text = re.sub(r"generated_at:.*", "generated_at: <NORM>", text)
    text = re.sub(r"approved_at:.*", "approved_at: <NORM>", text)
    return text


@pytest.mark.parametrize("stage", ["requirements", "design", "tasks"])
def test_gated_pipeline_files_match_golden(stage: str, tmp_path: Path, update_golden: bool) -> None:
    """The full `plan --gated` pipeline writes pre-C1-identical stage files.

    Runs generate→approve for each stage so downstream gates open, then compares
    the written file (frontmatter + body, timestamps normalized) to the golden.
    """
    cfg = _pipeline_cfg(tmp_path)
    for s in ("requirements", "design", "tasks"):
        rc = cli_plan.run_gated_stage(s, "Build X", cfg, invoke=_fake_invoke(s))
        assert rc == 0, f"{s} generation failed rc={rc}"
        path = stage_path(cfg, s)
        meta = read_spec_meta(path)
        assert meta is not None
        meta.status = "approved"
        body = path.read_text(encoding="utf-8").split("---", 2)[2].lstrip("\n")
        write_spec(path, meta, body, lock=None)
        if s == stage:
            break

    written = _normalize(stage_path(cfg, stage).read_text(encoding="utf-8"))
    golden = FIXTURES_DIR / f"pipeline_{stage}.md"
    if update_golden:
        golden.write_text(written, encoding="utf-8")
    assert written == golden.read_text(encoding="utf-8")


def test_unknown_profile_raises_config_error() -> None:
    """An unknown profile name resolves to a `ConfigError` listing the valid ones."""
    config = ExecutorConfig(spec_profile="nonexistent")
    with pytest.raises(ConfigError) as exc_info:
        config.resolve_spec_profile()
    msg = str(exc_info.value)
    assert "nonexistent" in msg
    assert "available:" in msg
    assert "lite" in msg


def test_unknown_profile_cli_exits_cleanly(monkeypatch, tmp_path: Path) -> None:
    """`plan --gated --profile nonexistent` exits via SystemExit with a clean
    message — not an uncaught `ConfigError` traceback."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["spec-runner", "plan", "--gated", "--profile", "nonexistent", "hello"],
    )
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    # A string exit code is the human-readable message argparse/SystemExit prints
    # to stderr with a non-zero status — i.e. no traceback reaches the user.
    assert isinstance(exc_info.value.code, str)
    assert "nonexistent" in exc_info.value.code
    assert "lite" in exc_info.value.code
