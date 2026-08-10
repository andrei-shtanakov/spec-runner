"""DEC-008 authoring contract: `traces_to` links and `upstream_hashes` pins.

steward is the *validator* of these two frontmatter fields and never rewrites a
generated artifact — materializing them belongs to spec-runner as the owner of
the SpecMeta format (inbox issue #135). Both are steward-owned governance keys
that ride through `SpecMeta.extra` as pass-through, so nothing here bumps
`SPEC_META_CONTRACT`.

The shapes are pinned by steward's reader (`src/steward/meta.py`):
  - `traces_to` — a LIST of non-empty ids; a bare scalar is a MetaError there.
  - `upstream_hashes` — a MAPPING of upstream stage id -> blob hash string,
    keyed by the DIRECT upstream only (an extra key raises GC-STALE-KEY).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from spec_runner import spec_commands
from spec_runner.spec import (
    LITE,
    SpecMeta,
    derive_traces_to,
    git_blob_hash,
    read_spec_meta,
    upstream_pins,
    write_spec,
)


def _cfg(tmp_path: Path):
    spec = tmp_path / "spec"
    return SimpleNamespace(
        project_root=tmp_path,
        spec_prefix="",
        resolve_spec_profile=lambda: LITE,
        requirements_file=spec / "requirements.md",
        design_file=spec / "design.md",
        tasks_file=spec / "tasks.md",
        spec_lock_file=spec / ".spec.lock",
        spec_dir=spec,
    )


GOOD_REQ = """# Requirements

## Out of Scope
- none

#### REQ-001: X
**Acceptance Criteria:**
GIVEN a WHEN b THEN c
"""

GOOD_DESIGN = """# Design

## Out of Scope
- none

### DESIGN-001: How X works
Implements [REQ-001].
"""


def _approve(cfg, stage: str) -> int:
    return spec_commands.cmd_spec_approve(SimpleNamespace(stage=stage, force=False), cfg)


# --- git blob hashing ---------------------------------------------------------


def test_git_blob_hash_matches_git_hash_object(tmp_path: Path):
    """The pin must be reproducible with `git hash-object <file>` — that is the
    contract steward verifies against, so a home-grown digest would be useless."""
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    path = tmp_path / "requirements.md"
    path.write_text("# Requirements\n\nsome body\n", encoding="utf-8")

    out = subprocess.run(
        ["git", "hash-object", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert git_blob_hash(path.read_bytes()) == out.stdout.strip()


def test_git_blob_hash_of_empty_content():
    """git's empty-blob hash is a well-known constant — a good canary for the
    header format (`blob <len>\\0`)."""
    assert git_blob_hash(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


# --- pins ---------------------------------------------------------------------


def test_upstream_pins_cover_direct_upstream_only(tmp_path: Path):
    """`tasks` requires `design`, which requires `requirements`. Pinning the
    transitive ancestor would make steward emit GC-STALE-KEY, so the map holds
    the direct upstream and nothing else."""
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "approved"), GOOD_REQ)
    write_spec(cfg.design_file, SpecMeta("design", "approved"), GOOD_DESIGN)

    pins = upstream_pins(cfg, "tasks", LITE)

    assert set(pins) == {"design"}
    assert pins["design"] == git_blob_hash(cfg.design_file.read_bytes())


def test_upstream_pins_empty_for_the_first_stage(tmp_path: Path):
    cfg = _cfg(tmp_path)
    assert upstream_pins(cfg, "requirements", LITE) == {}


def test_upstream_pins_skip_a_missing_upstream_file(tmp_path: Path):
    """Nothing to hash is not the same as a wrong hash: an absent upstream is
    left unpinned (steward warns GC-STALE-UNPINNED) rather than pinned to a lie."""
    cfg = _cfg(tmp_path)
    assert upstream_pins(cfg, "design", LITE) == {}


# --- traces ------------------------------------------------------------------


def test_traces_to_names_the_upstream_stage_at_minimum(tmp_path: Path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "approved"), GOOD_REQ)

    traces = derive_traces_to("# Design\n\nnothing traceable here\n", cfg, "design", LITE)

    assert traces == ["requirements"]


def test_traces_to_includes_ids_that_resolve_upstream(tmp_path: Path):
    """An id token carried by the downstream body earns its place only if it
    actually occurs upstream — steward errors (GC-TRACE) on one that resolves
    to nothing, which is worse than the empty-link warning we are fixing."""
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "approved"), GOOD_REQ)

    traces = derive_traces_to(
        "# Design\n\n### DESIGN-001\nImplements [REQ-001] and [REQ-999].\n",
        cfg,
        "design",
        LITE,
    )

    assert "REQ-001" in traces
    assert "REQ-999" not in traces  # not present in requirements.md
    assert "DESIGN-001" not in traces  # the stage's own id, not an upstream link
    assert traces[0] == "requirements"


def test_traces_to_is_empty_for_the_first_stage(tmp_path: Path):
    cfg = _cfg(tmp_path)
    assert derive_traces_to(GOOD_REQ, cfg, "requirements", LITE) == []


# --- wiring: approve ----------------------------------------------------------


def test_approve_stamps_pins_and_traces(tmp_path: Path):
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "approved"), GOOD_REQ)
    write_spec(cfg.design_file, SpecMeta("design", "draft"), GOOD_DESIGN)

    assert _approve(cfg, "design") == 0

    meta = read_spec_meta(cfg.design_file)
    assert meta is not None and meta.status == "approved"
    assert meta.extra["upstream_hashes"] == {
        "requirements": git_blob_hash(cfg.requirements_file.read_bytes())
    }
    assert "requirements" in meta.extra["traces_to"]
    assert "REQ-001" in meta.extra["traces_to"]


def test_approve_of_the_first_stage_adds_neither_key(tmp_path: Path):
    """Nothing upstream to link to or pin — the frontmatter must not grow empty
    keys that steward would then have to interpret."""
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), GOOD_REQ)

    assert _approve(cfg, "requirements") == 0

    meta = read_spec_meta(cfg.requirements_file)
    assert meta is not None
    assert "upstream_hashes" not in meta.extra
    assert "traces_to" not in meta.extra


def test_reapproving_upstream_leaves_the_downstream_pin_untouched(tmp_path: Path):
    """The whole point of the pin: after the upstream changes and is re-approved,
    the downstream still carries the OLD hash, so steward's stale-cascade sees
    the edge as broken instead of silently agreeing with itself."""
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "approved"), GOOD_REQ)
    write_spec(cfg.design_file, SpecMeta("design", "draft"), GOOD_DESIGN)
    assert _approve(cfg, "design") == 0
    pinned = read_spec_meta(cfg.design_file).extra["upstream_hashes"]["requirements"]

    # Upstream edited and re-approved (which also cascades design -> stale).
    write_spec(cfg.requirements_file, SpecMeta("requirements", "draft"), GOOD_REQ + "\nmore\n")
    assert _approve(cfg, "requirements") == 0

    still = read_spec_meta(cfg.design_file).extra["upstream_hashes"]["requirements"]
    assert still == pinned
    assert still != git_blob_hash(cfg.requirements_file.read_bytes())


def test_approve_preserves_foreign_extras(tmp_path: Path):
    """Stamping our two keys must not disturb another consumer's pass-through
    keys — losslessness is the v2 contract's central promise."""
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "approved"), GOOD_REQ)
    write_spec(
        cfg.design_file,
        SpecMeta("design", "draft", extra={"reviewer_roles": ["platform"]}),
        GOOD_DESIGN,
    )

    assert _approve(cfg, "design") == 0

    meta = read_spec_meta(cfg.design_file)
    assert meta is not None and meta.extra["reviewer_roles"] == ["platform"]


def test_adopt_as_approved_stamps_pins(tmp_path: Path):
    """`spec adopt` is the other door into APPROVED — it must pin too, or an
    adopted bundle reaches steward as GC-STALE-UNPINNED."""
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "approved"), GOOD_REQ)
    cfg.design_file.write_text(GOOD_DESIGN, encoding="utf-8")

    rc = spec_commands.cmd_spec_adopt(SimpleNamespace(stage="design", force=False), cfg)

    assert rc == 0
    meta = read_spec_meta(cfg.design_file)
    assert meta is not None and meta.status == "approved"
    assert meta.extra["upstream_hashes"] == {
        "requirements": git_blob_hash(cfg.requirements_file.read_bytes())
    }


def test_adopt_as_draft_traces_without_pinning(tmp_path: Path):
    """A draft is not an approval: it gets its traceability link, but pins are
    stamped at approval time and only then."""
    cfg = _cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "approved"), GOOD_REQ)
    cfg.design_file.write_text("# Design\n\nno Out of Scope section\n", encoding="utf-8")

    rc = spec_commands.cmd_spec_adopt(SimpleNamespace(stage="design", force=False), cfg)

    assert rc == 0
    meta = read_spec_meta(cfg.design_file)
    assert meta is not None and meta.status == "draft"
    assert "upstream_hashes" not in meta.extra
    assert meta.extra["traces_to"] == ["requirements"]


# --- wiring: gated generation -------------------------------------------------


def test_generated_draft_carries_traces_but_no_pins(tmp_path: Path):
    """`plan --gated` writes the link at generation time — the draft already knows
    what it was derived from — while the pins wait for the approval they record."""
    from spec_runner import cli_plan

    cfg = _cfg(tmp_path)
    cfg.claude_command = "claude"
    cfg.claude_model = ""
    cfg.command_template = ""
    cfg.skip_permissions = True
    cfg.task_timeout_minutes = 1
    cfg.spec_context = ""
    cfg.spec_rules = {}
    write_spec(cfg.requirements_file, SpecMeta("requirements", "approved"), GOOD_REQ)

    def _invoke(cmd, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=f"SPEC_DESIGN_READY\n{GOOD_DESIGN}\nSPEC_DESIGN_END\n",
            stderr="",
        )

    assert cli_plan.run_gated_stage("design", "Build X", cfg, invoke=_invoke) == 0

    meta = read_spec_meta(cfg.design_file)
    assert meta is not None and meta.status == "draft"
    assert meta.extra["traces_to"] == ["requirements", "REQ-001"]
    assert "upstream_hashes" not in meta.extra
