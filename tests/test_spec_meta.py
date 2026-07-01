import contextlib
from pathlib import Path

from spec_runner.spec import (
    SpecMeta,
    meta_from_dict,
    meta_to_dict,
    read_spec_body,
    read_spec_meta,
    split_frontmatter,
    strip_frontmatter,
    write_spec,
)

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
