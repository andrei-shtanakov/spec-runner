import contextlib
from pathlib import Path

from spec_runner.spec import (
    SpecMeta,
    apply_approval,
    downstream_stages,
    mark_downstream_stale,
    meta_from_dict,
    meta_to_dict,
    read_spec_body,
    read_spec_meta,
    resolve_next_stage,
    split_frontmatter,
    strip_frontmatter,
    write_spec,
)

# A custom (non-lite) stage sequence used to prove the four spec.py entry
# points read their ordering from the passed-in profile, not the module
# constant (DESIGN-303 / REQ-302).
CUSTOM_STAGES = ("spec", "design", "plan", "tasks")

FM = """---
spec_stage: requirements
status: draft
version: 1
validation: pass
---
# Requirements
body line
"""


def test_split_frontmatter_extracts_meta_and_body():
    meta, body = split_frontmatter(FM)
    assert meta is not None
    assert meta["spec_stage"] == "requirements"
    assert meta["version"] == 1
    assert body.startswith("# Requirements")


def test_split_frontmatter_none_when_absent():
    meta, body = split_frontmatter("# Just a doc\nno frontmatter")
    assert meta is None
    assert body == "# Just a doc\nno frontmatter"


def test_strip_frontmatter_returns_body_only():
    assert strip_frontmatter(FM).startswith("# Requirements")
    assert "spec_stage" not in strip_frontmatter(FM)


def test_strip_frontmatter_noop_without_frontmatter():
    text = "# No FM\nline"
    assert strip_frontmatter(text) == text


def test_meta_roundtrip():
    m = SpecMeta(spec_stage="design", status="approved", version=3)
    d = meta_to_dict(m)
    m2 = meta_from_dict(d)
    assert m2 == m


def test_split_frontmatter_missing_closing_delimiter():
    """Test defensive fallback when closing --- delimiter is missing."""
    text = "---\nspec_stage: requirements\nno closing delimiter"
    meta, body = split_frontmatter(text)
    assert meta is None
    assert body == text


def test_split_frontmatter_malformed_yaml():
    """Test defensive fallback when YAML inside frontmatter is unparsable."""
    text = "---\n:\n  - [unbalanced\n---\n# Body"
    meta, body = split_frontmatter(text)
    assert meta is None
    assert body == text


def test_write_then_read_roundtrip(tmp_path: Path):
    p = tmp_path / "requirements.md"
    write_spec(p, SpecMeta(spec_stage="requirements", version=2), "# Body\ntext\n")
    meta = read_spec_meta(p)
    assert meta is not None and meta.version == 2 and meta.spec_stage == "requirements"
    assert read_spec_body(p).startswith("# Body")


def test_read_meta_none_for_unmanaged(tmp_path: Path):
    p = tmp_path / "tasks.md"
    p.write_text("# Tasks\nno frontmatter\n")
    assert read_spec_meta(p) is None


def test_read_meta_none_for_missing(tmp_path: Path):
    assert read_spec_meta(tmp_path / "nope.md") is None


def test_read_meta_none_for_non_spec_frontmatter(tmp_path: Path):
    """A dict-shaped frontmatter block without `spec_stage` (e.g. unrelated
    notes frontmatter) must be treated as unmanaged, not crash `SpecMeta(**...)`
    with a missing-required-argument TypeError."""
    p = tmp_path / "notes.md"
    p.write_text("---\ntitle: notes\n---\nbody\n")
    assert read_spec_meta(p) is None


def test_read_meta_none_for_unknown_spec_stage(tmp_path: Path):
    """An unrecognized `spec_stage` value is also unmanaged."""
    p = tmp_path / "tasks.md"
    p.write_text("---\nspec_stage: bogus\nstatus: draft\nversion: 1\n---\nbody\n")
    assert read_spec_meta(p) is None


def test_read_meta_valid_spec_still_returns_meta(tmp_path: Path):
    """Sanity check: a real spec file with proper frontmatter is unaffected."""
    p = tmp_path / "requirements.md"
    write_spec(p, SpecMeta(spec_stage="requirements", version=1), "# Body\n")
    meta = read_spec_meta(p)
    assert meta is not None
    assert meta.spec_stage == "requirements"


def test_write_is_atomic_no_partial_on_replace(tmp_path: Path, monkeypatch):
    # Simulate os.replace failing: the original file must remain intact.
    p = tmp_path / "design.md"
    write_spec(p, SpecMeta(spec_stage="design", version=1), "original\n")
    import spec_runner.spec as specmod

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(specmod.os, "replace", boom)
    with contextlib.suppress(OSError):
        write_spec(p, SpecMeta(spec_stage="design", version=9), "new body\n")
    # Original content preserved; no temp file left behind.
    assert read_spec_meta(p).version == 1
    assert not any(x.name.startswith(".design.md.") for x in tmp_path.iterdir())


def test_downstream_stages():
    assert downstream_stages("requirements") == ["design", "tasks"]
    assert downstream_stages("tasks") == []


def test_downstream_stages_uses_passed_profile():
    assert downstream_stages("design", CUSTOM_STAGES) == ["plan", "tasks"]
    assert downstream_stages("tasks", CUSTOM_STAGES) == []
    assert downstream_stages("spec", CUSTOM_STAGES) == ["design", "plan", "tasks"]


def _m(stage, status):
    return SpecMeta(spec_stage=stage, status=status)


def test_resolve_next_stage_table():
    # nothing yet -> generate requirements
    assert resolve_next_stage({"requirements": None, "design": None, "tasks": None}) == (
        "generate",
        "requirements",
    )
    # requirements draft -> await approval
    assert resolve_next_stage(
        {"requirements": _m("requirements", "draft"), "design": None, "tasks": None}
    ) == ("await_approval", "requirements")
    # requirements approved, design missing -> generate design
    assert resolve_next_stage(
        {"requirements": _m("requirements", "approved"), "design": None, "tasks": None}
    ) == ("generate", "design")
    # all approved -> done
    assert resolve_next_stage(
        {
            "requirements": _m("requirements", "approved"),
            "design": _m("design", "approved"),
            "tasks": _m("tasks", "approved"),
        }
    ) == ("done", "tasks")
    # a stale stage takes priority
    assert resolve_next_stage(
        {
            "requirements": _m("requirements", "approved"),
            "design": _m("design", "stale"),
            "tasks": _m("tasks", "approved"),
        }
    ) == ("stale", "design")


def test_resolve_next_stage_uses_passed_profile():
    # With the custom profile, an approved requirements-equivalent chain still
    # advances through the profile order, and "done" reports the profile's last
    # stage, not the lite tail.
    assert resolve_next_stage({"spec": _m("spec", "approved"), "design": None}, CUSTOM_STAGES) == (
        "generate",
        "design",
    )
    assert resolve_next_stage({s: _m(s, "approved") for s in CUSTOM_STAGES}, CUSTOM_STAGES) == (
        "done",
        "tasks",
    )


def test_read_spec_meta_uses_passed_profile(tmp_path: Path):
    # "plan" is not a lite stage, so it is unmanaged under the default profile
    # but recognized when the custom profile is supplied.
    p = tmp_path / "plan.md"
    p.write_text("---\nspec_stage: plan\nstatus: draft\nversion: 1\n---\nbody\n")
    assert read_spec_meta(p) is None
    meta = read_spec_meta(p, CUSTOM_STAGES)
    assert meta is not None and meta.spec_stage == "plan"


class _Cfg:
    def __init__(self, root: Path):
        self.project_root = root
        self._spec = root / "spec"

    @property
    def requirements_file(self):
        return self._spec / "requirements.md"

    @property
    def design_file(self):
        return self._spec / "design.md"

    @property
    def tasks_file(self):
        return self._spec / "tasks.md"

    @property
    def spec_lock_file(self):
        return self._spec / ".spec.lock"


def test_apply_approval_bumps_and_cascades_stale(tmp_path: Path):
    cfg = _Cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "approved", version=1), "r\n")
    write_spec(cfg.design_file, SpecMeta("design", "approved", version=1), "d\n")
    write_spec(cfg.tasks_file, SpecMeta("tasks", "approved", version=1), "t\n")

    # Re-approve requirements (version 1 -> 2) must cascade stale downstream.
    apply_approval(
        cfg, "requirements", approver="tester", now="2026-07-01T00:00:00Z", fresh_validation="pass"
    )

    assert read_spec_meta(cfg.requirements_file).version == 2
    assert read_spec_meta(cfg.requirements_file).status == "approved"
    assert read_spec_meta(cfg.design_file).status == "stale"
    assert read_spec_meta(cfg.tasks_file).status == "stale"


def test_mark_downstream_stale_flips_downstream_only(tmp_path: Path):
    from spec_runner.config import ExecutorLock

    cfg = _Cfg(tmp_path)
    write_spec(cfg.requirements_file, SpecMeta("requirements", "approved", version=1), "r\n")
    write_spec(cfg.design_file, SpecMeta("design", "approved", version=1), "d\n")
    write_spec(cfg.tasks_file, SpecMeta("tasks", "approved", version=1), "t\n")

    lock = ExecutorLock(cfg.spec_lock_file)
    mark_downstream_stale(cfg, "design", lock)

    # Upstream + the stage itself untouched; strictly-downstream flipped stale.
    assert read_spec_meta(cfg.requirements_file).status == "approved"
    assert read_spec_meta(cfg.design_file).status == "approved"
    assert read_spec_meta(cfg.tasks_file).status == "stale"
