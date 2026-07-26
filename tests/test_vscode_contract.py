"""Contract tests for the spec-runner-vscode extension read surfaces.

Pins the three JSON/YAML shapes the extension depends on, so drift in
spec-runner is caught here rather than in the extension at runtime:

- `status --json`  → schemas/status.schema.json         (RUN aggregate)
- `costs --json`   → schemas/costs.schema.json           (TASKS per-task list)
- spec frontmatter → schemas/spec-frontmatter.schema.json (SPEC governance)

Design: docs/superpowers/specs/2026-07-01-spec-runner-vscode-design.md
(Prerequisites). Each shape is validated both from a golden sample fixture
(what the extension vendors) and from live command output (drift guard).
"""

from __future__ import annotations

import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from spec_runner.cli_info import cmd_costs, cmd_status
from spec_runner.config import ExecutorConfig
from spec_runner.spec import SpecMeta, split_frontmatter, write_spec
from spec_runner.state import ExecutorState
from spec_runner.task import STATUS_EMOJI

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMAS_DIR = REPO_ROOT / "schemas"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "vscode-contract"


def _validator(schema_name: str) -> Draft7Validator:
    schema = json.loads((SCHEMAS_DIR / schema_name).read_text())
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def _make_config(tmp_path: Path, **overrides) -> ExecutorConfig:
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": spec_dir / ".executor-state.db",
        "budget_usd": 5.0,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _write_tasks(tasks_file: Path, tasks: list[tuple[str, str, str, str]]) -> None:
    priority_emoji = {"p0": "\U0001f534", "p1": "\U0001f7e0", "p2": "\U0001f7e1"}
    lines = ["# Tasks\n"]
    for task_id, name, priority, status in tasks:
        p = priority_emoji.get(priority, "\U0001f534")
        s = STATUS_EMOJI.get(status, "⬜")
        lines.append(f"### {task_id}: {name}")
        lines.append(f"{p} {priority.upper()} | {s} {status.upper()} | Est: 1d")
        lines.append("")
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text("\n".join(lines))


def _seed_project(tmp_path: Path) -> ExecutorConfig:
    """A project with a run task (DB state) and an unstarted task (tasks.md only)."""
    config = _make_config(tmp_path)
    _write_tasks(
        config.tasks_file,
        [
            ("TASK-001", "Login page", "p0", "done"),
            ("TASK-002", "Dashboard", "p2", "todo"),
        ],
    )
    with ExecutorState(config) as state:
        state.record_attempt(
            "TASK-001",
            success=True,
            duration=10.0,
            error=None,
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.5,
        )
    return config


# --- status --json --------------------------------------------------------


def test_status_sample_fixture_validates() -> None:
    sample = json.loads((FIXTURES_DIR / "status.sample.json").read_text())
    _validator("status.schema.json").validate(sample)


def test_live_status_json_matches_schema(tmp_path: Path, capsys) -> None:
    config = _seed_project(tmp_path)
    cmd_status(Namespace(json_output=True), config)
    payload = json.loads(capsys.readouterr().out)
    _validator("status.schema.json").validate(payload)


# --- costs --json ---------------------------------------------------------


def test_costs_sample_fixture_validates() -> None:
    sample = json.loads((FIXTURES_DIR / "costs.sample.json").read_text())
    _validator("costs.schema.json").validate(sample)


def test_live_costs_json_matches_schema(tmp_path: Path, capsys) -> None:
    config = _seed_project(tmp_path)
    cmd_costs(Namespace(json=True, sort="id"), config)
    payload = json.loads(capsys.readouterr().out)
    _validator("costs.schema.json").validate(payload)


def test_costs_json_empty_project_is_valid_json(tmp_path: Path, capsys) -> None:
    """`costs --json` with no tasks must emit valid JSON, not the prose fallback.

    A fresh gated spec has requirements/design but no tasks.md yet; the extension
    still polls `costs --json` and must be able to parse it.
    """
    config = _make_config(tmp_path)  # no tasks.md written
    cmd_costs(Namespace(json=True, sort="id"), config)
    payload = json.loads(capsys.readouterr().out)
    _validator("costs.schema.json").validate(payload)
    assert payload["tasks"] == []


def test_status_json_stdout_not_polluted_by_logs_in_git_subdir(tmp_path: Path) -> None:
    """Machine `--json` stdout must stay clean when the project is nested in a git
    repo — the subdir-detection warning must go to stderr, never stdout.

    Regression for the leak that broke the spec-runner-vscode extension's refresh
    (JSON.parse failing on a leading log line).
    """
    outer = tmp_path / "outer"
    outer.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=outer, check=True, capture_output=True)
    proj = outer / "proj"
    proj.mkdir()

    result = subprocess.run(
        [sys.executable, "-c", "from spec_runner.executor import main; main()", "status", "--json"],
        cwd=proj,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)  # raises if a log line leaked to stdout
    assert "total_tasks" in payload
    assert "subdir_project_detected" in result.stderr  # warning belongs on stderr


def test_costs_status_enum_covers_both_vocabularies() -> None:
    """The mixed DB + tasks.md status vocabularies must both fit the pinned enum.

    This is the drift guard flagged in the design: if spec-runner adds a status
    value to either vocabulary, costs.schema.json must be updated in lockstep.
    """
    schema = json.loads((SCHEMAS_DIR / "costs.schema.json").read_text())
    pinned = set(schema["definitions"]["TaskCost"]["properties"]["status"]["enum"])

    exec_state = json.loads((SCHEMAS_DIR / "executor-state.schema.json").read_text())
    db_vocab = set(exec_state["definitions"]["TaskEntry"]["properties"]["status"]["enum"])
    tasks_md_vocab = set(STATUS_EMOJI.keys())

    missing = (db_vocab | tasks_md_vocab) - pinned
    assert not missing, f"costs.schema.json status enum missing: {sorted(missing)}"


# --- spec frontmatter -----------------------------------------------------


def test_frontmatter_sample_fixture_validates() -> None:
    sample = json.loads((FIXTURES_DIR / "spec-frontmatter.sample.json").read_text())
    _validator("spec-frontmatter.schema.json").validate(sample)


def test_live_frontmatter_matches_schema(tmp_path: Path) -> None:
    path = tmp_path / "tasks.md"
    meta = SpecMeta(
        spec_stage="tasks",
        status="approved",
        version=2,
        generated_by="claude",
        generated_at="2026-07-01T00:00:00Z",
        source_prompt_version="abc123",
        validation="pass",
        approved_by="operator",
        approved_at="2026-07-01T01:00:00Z",
    )
    write_spec(path, meta, body="# Tasks\n")
    fm, _ = split_frontmatter(path.read_text())
    assert fm is not None
    _validator("spec-frontmatter.schema.json").validate(fm)


def test_live_frontmatter_draft_defaults_match_schema(tmp_path: Path) -> None:
    """A freshly generated draft (empty/None optional fields) still validates."""
    path = tmp_path / "requirements.md"
    write_spec(path, SpecMeta(spec_stage="requirements"), body="# Requirements\n")
    fm, _ = split_frontmatter(path.read_text())
    assert fm is not None
    _validator("spec-frontmatter.schema.json").validate(fm)


@pytest.mark.parametrize(
    "schema_name",
    [
        "status.schema.json",
        "costs.schema.json",
        "spec-frontmatter.schema.json",
    ],
)
def test_schema_is_valid_draft7(schema_name: str) -> None:
    Draft7Validator.check_schema(json.loads((SCHEMAS_DIR / schema_name).read_text()))


def test_schema_lists_every_canonical_field() -> None:
    """The schema's properties must cover every field spec-runner owns.

    This is the schema half of the drift guard: canonical_fields() is the SSOT,
    and a new SpecMeta field must be described here too.
    """
    from spec_runner.spec import canonical_fields

    schema = json.loads((SCHEMAS_DIR / "spec-frontmatter.schema.json").read_text())
    missing = canonical_fields() - set(schema["properties"])
    assert not missing, f"schema does not describe canonical field(s): {sorted(missing)}"


def test_schema_accepts_foreign_extension_keys() -> None:
    """Extending layers (steward) add their own keys; they must validate."""
    sample = {
        "spec_stage": "tasks",
        "status": "approved",
        "version": 2,
        "traces_to": "REQ-001",
        "upstream_hashes": {"design": "deadbeef"},
        "owner_role": "@platform,@sre",
    }
    _validator("spec-frontmatter.schema.json").validate(sample)


def test_schema_accepts_a_custom_profile_stage() -> None:
    """Stage membership belongs to the active profile, not to JSON Schema."""
    sample = {"spec_stage": "acceptance", "status": "draft", "version": 1}
    _validator("spec-frontmatter.schema.json").validate(sample)


def test_schema_rejects_empty_spec_stage() -> None:
    from jsonschema.exceptions import ValidationError

    with pytest.raises(ValidationError):
        _validator("spec-frontmatter.schema.json").validate(
            {"spec_stage": "", "status": "draft", "version": 1}
        )


def test_schema_still_type_checks_canonical_properties() -> None:
    """Opening the object must not weaken the fields the schema does describe."""
    from jsonschema.exceptions import ValidationError

    validator = _validator("spec-frontmatter.schema.json")
    for bad in (
        {"spec_stage": "tasks", "status": "approvd", "version": 1},
        {"spec_stage": "tasks", "status": "draft", "version": "two"},
        {"spec_stage": "tasks", "status": "draft", "version": 1, "owner_role": 7},
        {"spec_stage": "tasks", "status": "draft", "version": 1, "validation": "weird"},
    ):
        with pytest.raises(ValidationError):
            validator.validate(bad)


def test_live_frontmatter_with_extras_matches_schema(tmp_path: Path) -> None:
    """End-to-end: a written file carrying extras and owner_role validates."""
    from spec_runner.spec import meta_from_dict

    meta = meta_from_dict(
        {
            "spec_stage": "tasks",
            "status": "approved",
            "version": 2,
            "owner_role": "@platform",
            "traces_to": "REQ-001",
        }
    )
    path = tmp_path / "tasks.md"
    write_spec(path, meta, "# body\n")
    fm, _ = split_frontmatter(path.read_text())
    assert fm is not None
    _validator("spec-frontmatter.schema.json").validate(fm)


# --- version pin ----------------------------------------------------------


def test_version_flag_prints_semver(capsys) -> None:
    """`spec-runner --version` is the extension's activation compatibility check."""
    from spec_runner.cli import _build_parser

    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(["--version"])
    assert exc.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out.startswith("spec-runner ")
    semver = out.split(" ", 1)[1]
    assert semver.split(".")[0].isdigit()
