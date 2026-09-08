"""Contract: the declared-group-element dictionary stays finite (BEH-03).

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-03

`PytestAdapter.parse_group_element` is the declared-group vocabulary (DT-01) —
a second adapter entry point, distinct from `parse_selector`, that accepts
both a node id and a file target. Every defective form named by FR-02 must
refuse with its own stable `SelectorRefusal.code`: a dictionary that collapses
two different defects onto the same code, or onto a shared catch-all, fails
BEH-03 even if every input is correctly refused.
"""

from pathlib import Path

import pytest

from spec_runner.tdd_runners import (
    FileTarget,
    PytestAdapter,
    PytestNodeId,
    Selector,
    SelectorRefusal,
)

ADAPTER = PytestAdapter()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text("def test_a():\n    pass\n")
    (tmp_path / "tests" / "helpers.py").write_text("def helper():\n    pass\n")
    link = tmp_path / "tests" / "test_link.py"
    link.symlink_to(tmp_path / "tests" / "test_a.py")
    return tmp_path


class TestEveryDefectiveFormRefusesByItsOwnName:
    """BEH-03: each defective form refuses, naming both form and reason via
    its own stable code — never a shared catch-all."""

    @pytest.mark.parametrize(
        ("label", "raw"),
        [
            ("directory", "tests"),
            ("glob", "tests/test_*.py"),
            ("dash_k_expression", "-k test_a"),
            ("marker_expression", "-m slow"),
            ("outside_repository_relative", "../outside.py"),
            ("outside_repository_absolute", "/etc/passwd"),
            ("symlink", "tests/test_link.py"),
            ("not_discoverable", "tests/helpers.py"),
            ("empty_element", ""),
        ],
    )
    def test_defective_form_refuses(self, project: Path, label: str, raw: str) -> None:
        result = ADAPTER.parse_group_element(raw, project)

        assert isinstance(result, SelectorRefusal), (
            f"{label} ({raw!r}) must refuse as a defective declared-group element, got {result!r}"
        )
        assert result.code and result.message

    def test_codes_are_pairwise_distinct(self, project: Path) -> None:
        # BEH-03 groups "outside the repository" as one form covering both a
        # relative escape (`../x.py`) and an absolute path — the two are not
        # required to be distinguishable from each other, only from every
        # OTHER form.
        forms = {
            "directory": "tests",
            "glob": "tests/test_*.py",
            "dash_k_expression": "-k test_a",
            "marker_expression": "-m slow",
            "outside_repository": "../outside.py",
            "symlink": "tests/test_link.py",
            "not_discoverable": "tests/helpers.py",
            "empty_element": "",
        }
        codes = {
            label: ADAPTER.parse_group_element(raw, project).code  # type: ignore[union-attr]
            for label, raw in forms.items()
        }
        assert len(set(codes.values())) == len(codes), (
            f"each defective form must be distinguishable from every other: {codes}"
        )
        absolute_outside = ADAPTER.parse_group_element("/etc/passwd", project)
        assert isinstance(absolute_outside, SelectorRefusal)
        assert absolute_outside.code == codes["outside_repository"]

    def test_zero_defective_forms_accepted(self, project: Path) -> None:
        forms = [
            "tests",
            "tests/test_*.py",
            "-k test_a",
            "-m slow",
            "../outside.py",
            "/etc/passwd",
            "tests/test_link.py",
            "tests/helpers.py",
            "",
        ]
        accepted = [
            raw for raw in forms if isinstance(ADAPTER.parse_group_element(raw, project), Selector)
        ]
        assert accepted == []

    def test_refusal_never_raises(self, project: Path) -> None:
        # A parse call over every defective form must not end in a traceback.
        for raw in ("tests", "tests/test_*.py", "-k x", "-m slow", "", "../x.py"):
            ADAPTER.parse_group_element(raw, project)


class TestAcceptedForms:
    """The dictionary is finite, not empty: a genuine file target and a
    genuine node id are still accepted (parse_group_element widens the
    vocabulary, it does not narrow it)."""

    def test_file_target_is_accepted(self, project: Path) -> None:
        result = ADAPTER.parse_group_element("tests/test_a.py", project)

        assert isinstance(result, Selector)
        assert result.locator == FileTarget()
        assert str(result.path) == "tests/test_a.py"

    def test_node_id_is_still_accepted(self, project: Path) -> None:
        result = ADAPTER.parse_group_element("tests/test_a.py::test_a", project)

        assert isinstance(result, Selector)
        assert result.locator == PytestNodeId("tests/test_a.py::test_a")


class TestParseSelectorIsNotWidened:
    """Boundary (DT-01): `parse_selector` stays a RED-checkpoint vocabulary
    about exactly one test and is not extended to accept a file target."""

    def test_file_target_is_still_refused_by_parse_selector(self) -> None:
        result = ADAPTER.parse_selector("tests/test_a.py")

        assert isinstance(result, SelectorRefusal)
        assert result.code == "not_a_node_id"
