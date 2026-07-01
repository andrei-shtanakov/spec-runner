"""Test bundled template loading and content hash versioning."""

from spec_runner.prompt import load_bundled_template, template_hash


def test_load_bundled_template_has_sections():
    text = load_bundled_template("requirements")
    assert text.strip()
    # Rich template, not a 3-line stub.
    assert len(text) > 200


def test_template_hash_is_sha256_prefixed_and_stable():
    h1 = template_hash("design")
    h2 = template_hash("design")
    assert h1.startswith("sha256:")
    assert h1 == h2


def test_template_hash_differs_by_stage():
    assert template_hash("requirements") != template_hash("tasks")
