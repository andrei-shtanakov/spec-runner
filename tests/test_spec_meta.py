from spec_runner.spec import (
    SpecMeta,
    meta_from_dict,
    meta_to_dict,
    split_frontmatter,
    strip_frontmatter,
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
