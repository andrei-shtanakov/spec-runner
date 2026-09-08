"""RED for BEH-03: the declared-group-element dictionary stays finite.

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/30-decomposition.md#DT-01

DT-01's subject is a second adapter entry point — parsing one *element of a
declared group*, which accepts both a node id and a file target — plus a
third `Selector.locator` variant that carries no pointer to a single test.
Every defective form named by FR-02 (directory, glob, `-k`, marker, a path
outside the repository, a symlink, a regular file `is_discoverable` would not
collect, an empty element) must refuse with its **own** stable code: a
dictionary that collapses two different defects onto the same code, or onto a
shared catch-all, fails BEH-03 even if every input is correctly refused.

Today `PytestAdapter` has no `parse_group_element` at all — only
`parse_selector`, which is a RED-checkpoint vocabulary about exactly one test
and is explicitly out of scope for this widening (DT-01: "parse_selector не
расширяется ни на строку"). This test fails because that second entry point
does not exist yet.
"""

from pathlib import Path

from spec_runner.tdd_runners import PytestAdapter, SelectorRefusal

ADAPTER = PytestAdapter()


class TestGroupElementDictionaryIsFinite:
    def test_every_defective_form_refuses_with_its_own_stable_code(self, tmp_path: Path) -> None:
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
        (tmp_path / "tests" / "test_b.py").write_text("def test_b():\n    pass\n")
        (tmp_path / "tests" / "helpers.py").write_text("def helper():\n    pass\n")
        target = tmp_path / "tests" / "test_a.py"
        link = tmp_path / "tests" / "test_link.py"
        link.symlink_to(target)

        assert hasattr(ADAPTER, "parse_group_element"), (
            "PytestAdapter needs parse_group_element(raw, root) — the "
            "declared-group-element parser accepting both a node id and a "
            "file target (BEH-03, DT-01's second adapter entry point)"
        )

        forms = {
            "directory": "tests",
            "glob": "tests/test_*.py",
            "dash_k_expression": "-k test_a",
            "marker": "-m slow",
            "outside_repository": "../outside.py",
            "symlink": "tests/test_link.py",
            "not_discoverable": "tests/helpers.py",
            "empty_element": "",
        }

        codes: dict[str, str] = {}
        for label, raw in forms.items():
            result = ADAPTER.parse_group_element(raw, tmp_path)
            assert isinstance(result, SelectorRefusal), (
                f"{label} ({raw!r}) must be a defective declared-group element "
                f"and refuse, got {result!r}"
            )
            codes[label] = result.code

        assert len(set(codes.values())) == len(codes), (
            f"each defective form must refuse by its own name, not a shared catch-all code: {codes}"
        )
