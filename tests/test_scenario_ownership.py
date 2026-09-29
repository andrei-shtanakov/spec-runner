"""#603 slice A: the verify_first gate judges ownership per group entry.

Spec: docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md §2.3
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

from spec_runner.phases import RefusalKind
from spec_runner.scenarios import coverage_refusal, group_coverage, split_entry
from spec_runner.task import Task

F = PurePosixPath("tests/test_x.py")

TWO_TESTS = (
    '"""Module header BEH-01 ENC:BEH-01"""\n'
    "\n"
    "\n"
    "def helper():\n"
    '    """BEH-02"""\n'
    "\n"
    "\n"
    "def test_a():\n"
    '    """BEH-03 ENC:BEH-03"""\n'
    "\n"
    "\n"
    "def test_b():\n"
    '    """BEH-04"""\n'
    "\n"
    "\n"
    "class TestK:\n"
    '    """BEH-05"""\n'
    "\n"
    "    def test_m(self):\n"
    "        pass\n"
)


def _missing(scenarios, entries, text=TWO_TESTS):
    return group_coverage(scenarios, entries, {F: text}).missing


class TestSplitEntry:  # Review Focus 2
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("tests/test_x.py::test_a", (F, "test_a")),
            ("./tests/test_x.py::TestK::test_m", (F, "TestK.test_m")),
            ("tests/test_x.py::test_a[a::b]", (F, "test_a")),
            ("tests/test_x.py::test_a[x[1]]", (F, "test_a")),
            ("tests/test_x.py", (F, None)),
            ("test/x_test.exs:12", (PurePosixPath("test/x_test.exs"), None)),
        ],
    )
    def test_split(self, raw, expected):
        assert split_entry(raw) == expected


class TestOwnership:
    def test_node_id_entry_counts_only_its_own_definition(self):
        assert _missing(["BEH-03", "BEH-04"], ["tests/test_x.py::test_a"]) == ["BEH-04"]

    def test_module_header_and_helper_do_not_count_bare_or_qualified(self):
        entries = ["tests/test_x.py::test_a"]
        assert _missing(["BEH-01", "BEH-02"], entries) == ["BEH-01", "BEH-02"]
        assert _missing(["ENC:BEH-01"], entries) == ["ENC:BEH-01"]

    def test_class_region_counts_for_its_method(self):
        assert _missing(["BEH-05"], ["tests/test_x.py::TestK::test_m"]) == []

    def test_class_entry(self):  # Review Focus 1
        assert _missing(["BEH-05", "BEH-03"], ["tests/test_x.py::TestK"]) == ["BEH-03"]

    def test_file_entry_uses_default_naming(self):
        assert _missing(["BEH-02", "BEH-03", "BEH-04", "BEH-05"], ["tests/test_x.py"]) == ["BEH-02"]

    def test_parametrize_suffix_resolves(self):  # Review Focus 2
        assert _missing(["ENC:BEH-03"], ["tests/test_x.py::test_a[a::b]"]) == []

    def test_any_entry_may_carry_an_id(self):
        entries = ["tests/test_x.py::test_a", "tests/test_x.py::test_b"]
        assert _missing(["BEH-03", "BEH-04"], entries) == []

    def test_non_python_file_keeps_the_per_file_match(self):
        exs = PurePosixPath("test/x_test.exs")
        text = '# BEH-09 at the top of the file\ntest "x" do\nend\n'
        coverage = group_coverage(["BEH-09"], ["test/x_test.exs:2"], {exs: text})
        assert coverage.missing == [] and coverage.problems == []

    def test_file_absent_from_texts_is_skipped(self):
        coverage = group_coverage(["BEH-03"], ["tests/other.py::test_a"], {F: TWO_TESTS})
        assert coverage.missing == ["BEH-03"] and coverage.problems == []


class TestBoundaries:
    def test_bare_id_is_carried_by_its_qualified_spelling(self):  # Review Focus 5
        assert _missing(["BEH-03"], ["tests/test_x.py::test_a"]) == []
        text = 'def test_a():\n    """ENC:BEH-03"""\n'
        assert _missing(["BEH-03"], ["tests/test_x.py::test_a"], text) == []

    @pytest.mark.parametrize("label", ["XENC:BEH-03", "ENC:BEH-030", "ENC:BEH-03a"])
    def test_qualified_boundaries(self, label):
        text = f'def test_a():\n    """{label}"""\n'
        assert _missing(["ENC:BEH-03"], ["tests/test_x.py::test_a"], text) == ["ENC:BEH-03"]


class TestProblems:
    def test_unresolved_qualname(self):
        coverage = group_coverage(["BEH-03"], ["tests/test_x.py::test_nope"], {F: TWO_TESTS})
        assert (
            coverage.problems
            and "test_nope is not defined in tests/test_x.py" in (coverage.problems[0])
        )

    @pytest.mark.parametrize("text", ["def (:\n", "x = '\x00'\n"])  # Review Focus 3
    def test_unparseable_file(self, text):
        coverage = group_coverage(["BEH-03"], ["tests/test_x.py::test_a"], {F: text})
        assert coverage.problems and "tests/test_x.py cannot be parsed" in coverage.problems[0]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(tmp_path: Path, text: str) -> tuple[Path, str]:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests" / "test_x.py").write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _task(verifies, scenarios):
    return Task(
        id="TASK-001",
        name="t",
        priority="p1",
        status="todo",
        estimate="1h",
        execution_mode="verify_first",
        verifies=verifies,
        scenarios=scenarios,
    )


class TestCoverageRefusal:
    def test_module_header_label_is_terminal_policy_naming_the_place(self, tmp_path):
        root, sha = _commit(tmp_path, TWO_TESTS)
        refusal = coverage_refusal(_task(["tests/test_x.py::test_a"], ["BEH-01"]), root, sha)
        assert refusal is not None
        assert refusal.kind is RefusalKind.POLICY and refusal.terminal
        assert "BEH-01" in refusal and "test definition" in refusal
        assert "docstring" not in refusal

    def test_unresolved_qualname_is_instrument(self, tmp_path):
        root, sha = _commit(tmp_path, TWO_TESTS)
        refusal = coverage_refusal(_task(["tests/test_x.py::test_nope"], ["BEH-03"]), root, sha)
        assert refusal is not None and refusal.kind is RefusalKind.INSTRUMENT
        assert "test_nope" in refusal and sha[:12] in refusal

    def test_unparseable_file_is_instrument(self, tmp_path):  # Review Focus 3
        root, sha = _commit(tmp_path, "def (:\n")
        refusal = coverage_refusal(_task(["tests/test_x.py::test_a"], ["BEH-03"]), root, sha)
        assert refusal is not None and refusal.kind is RefusalKind.INSTRUMENT
        assert "cannot be parsed" in refusal

    def test_owned_label_passes(self, tmp_path):
        root, sha = _commit(tmp_path, TWO_TESTS)
        task = _task(["tests/test_x.py::test_a"], ["ENC:BEH-03"])
        assert coverage_refusal(task, root, sha) is None
