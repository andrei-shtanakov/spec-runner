"""#402: `**Scenarios:**` — parsing, the coverage core and `validate`.

Spec: docs/superpowers/specs/2026-09-23-verify-scenario-coverage-design.md
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

from spec_runner.phases import RefusalKind
from spec_runner.scenarios import (
    coverage_refusal,
    group_files,
    read_at_commit,
    uncovered_scenarios,
)
from spec_runner.task import Task, parse_tasks
from spec_runner.validate import validate_all, validate_task_fields

HEADER = "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\nEst: 1d\n"


def _tasks(tmp_path: Path, body: str):
    path = tmp_path / "tasks.md"
    path.write_text(HEADER + body)
    return parse_tasks(path)


class TestParsing:
    def test_absent_line_is_none(self, tmp_path):
        (task,) = _tasks(tmp_path, "**Mode:** verify_first\n")
        assert task.scenarios is None
        assert task.scenarios_error is None

    def test_ids_kept_in_declared_order(self, tmp_path):
        (task,) = _tasks(tmp_path, "**Scenarios:** BEH-10, BEH-09, BEH-09a\n")
        assert task.scenarios == ["BEH-10", "BEH-09", "BEH-09a"]
        assert task.scenarios_error is None

    def test_line_after_a_verifies_block_is_still_read(self, tmp_path):
        (task,) = _tasks(
            tmp_path,
            "**Verifies:**\n- tests/test_a.py::test_x\n**Scenarios:** BEH-09\n",
        )
        assert task.verifies == ["tests/test_a.py::test_x"]
        assert task.scenarios == ["BEH-09"]


class TestMalformedLineIsAValidateError:
    def _errors(self, tmp_path, line: str) -> str:
        tasks = _tasks(tmp_path, line + "\n")
        assert tasks[0].scenarios is None
        return "\n".join(validate_task_fields(tasks).errors)

    def test_empty_line(self, tmp_path):
        joined = self._errors(tmp_path, "**Scenarios:**")
        assert "TASK-001" in joined and "**Scenarios:**" in joined and "empty" in joined

    def test_empty_item(self, tmp_path):
        joined = self._errors(tmp_path, "**Scenarios:** BEH-09,")
        assert "BEH-09," in joined  # the line is quoted back

    def test_wrong_shape(self, tmp_path):
        for bad in ("beh-09", "BEH09", "BEH-09A", "BEH-09-a", "BEH-09; BEH-10", "—"):
            joined = self._errors(tmp_path, f"**Scenarios:** {bad}")
            assert "TASK-001" in joined, bad
            assert bad in joined, bad


# --- Task 2: the coverage core -------------------------------------------


class TestTokenBoundaries:
    @pytest.mark.parametrize(
        "text", ["BEH-09:", "(BEH-09)", "BEH-09,", "kind: e2e — BEH-09", "BEH-09"]
    )
    def test_covers(self, text):
        assert uncovered_scenarios(["BEH-09"], [text]) == []

    @pytest.mark.parametrize(
        "text", ["BEH-091", "BEH-09a", "BEH-09A", "BEH-09_extra", "xBEH-09", "TestBEH09"]
    )
    def test_does_not_cover(self, text):
        assert uncovered_scenarios(["BEH-09"], [text]) == ["BEH-09"]

    def test_suffixed_id_is_its_own_token(self):
        assert uncovered_scenarios(["BEH-09a"], ["BEH-09a"]) == []
        assert uncovered_scenarios(["BEH-09a"], ["BEH-09"]) == ["BEH-09a"]

    def test_any_file_covers_and_order_is_declared(self):
        assert uncovered_scenarios(["BEH-10", "BEH-09", "BEH-11"], ["BEH-09", "nothing"]) == [
            "BEH-10",
            "BEH-11",
        ]

    def test_duplicate_declaration_named_once(self):  # Review Focus 1
        assert uncovered_scenarios(["BEH-09", "BEH-09"], ["x"]) == ["BEH-09"]


class TestGroupFiles:
    def test_node_ids_and_file_targets(self):  # Review Focus 2
        assert group_files(
            ["tests/a.py::T::test_x", "./tests/b.py", "tests/a.py::test_y[1,2]"]
        ) == [
            PurePosixPath("tests/a.py"),
            PurePosixPath("tests/b.py"),
        ]

    def test_exunit_path_line(self):
        assert group_files(["test/x_test.exs:12"]) == [PurePosixPath("test/x_test.exs")]


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests" / "test_a.py").write_text('"""kind: e2e — BEH-09"""\n')
    (root / "tests" / "test_bin.py").write_bytes(b"# \xff\xfe BEH-10\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


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


class TestAtCommit:
    def test_read_at_commit(self, repo):
        sha = _git(repo, "rev-parse", "HEAD")
        assert "BEH-09" in (read_at_commit(repo, sha, PurePosixPath("tests/test_a.py")) or "")
        assert read_at_commit(repo, sha, PurePosixPath("tests/missing.py")) is None

    def test_non_utf8_file_does_not_crash(self, repo):  # Review Focus 3
        sha = _git(repo, "rev-parse", "HEAD")
        task = _task(["tests/test_bin.py::test_x"], ["BEH-10"])
        assert coverage_refusal(task, repo, sha) is None

    def test_no_scenarios_is_no_check(self, repo):
        sha = _git(repo, "rev-parse", "HEAD")
        assert coverage_refusal(_task(["tests/missing.py::t"], None), repo, sha) is None

    def test_uncovered_is_terminal_policy_naming_scenario_and_files(self, repo):
        sha = _git(repo, "rev-parse", "HEAD")
        refusal = coverage_refusal(
            _task(["tests/test_a.py::test_x"], ["BEH-09", "BEH-10"]), repo, sha
        )
        assert refusal is not None
        assert refusal.kind is RefusalKind.POLICY and refusal.terminal
        assert "BEH-10" in refusal and "BEH-09" not in refusal.split("uncovered")[1]
        assert "tests/test_a.py" in refusal and sha[:12] in refusal

    def test_working_tree_label_does_not_count(self, repo):
        sha = _git(repo, "rev-parse", "HEAD")
        (repo / "tests" / "test_a.py").write_text('"""BEH-09 BEH-10"""\n')
        refusal = coverage_refusal(_task(["tests/test_a.py::test_x"], ["BEH-10"]), repo, sha)
        assert refusal is not None and "BEH-10" in refusal

    def test_unreadable_file_is_instrument(self, repo):
        sha = _git(repo, "rev-parse", "HEAD")
        refusal = coverage_refusal(_task(["tests/missing.py::t"], ["BEH-09"]), repo, sha)
        assert refusal is not None
        assert refusal.kind is RefusalKind.INSTRUMENT
        assert "tests/missing.py" in refusal


def _validate(tmp_path, body: str, files: dict[str, str] | None = None):
    for rel, text in (files or {}).items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    tasks = tmp_path / "tasks.md"
    tasks.write_text(HEADER + body)
    return validate_all(tasks_file=tasks, config_file=None, project_root=tmp_path)


class TestValidateWarnings:
    def test_outside_verify_first_is_a_warning(self, tmp_path):
        result = _validate(tmp_path, "**Scenarios:** BEH-09\n")
        assert result.ok
        assert any(
            "TASK-001" in w and "not checked outside verify_first" in w for w in result.warnings
        )

    def test_uncovered_in_the_tree_is_a_warning(self, tmp_path):
        result = _validate(
            tmp_path,
            "**Mode:** verify_first\n**Verifies:** tests/test_a.py::test_x\n"
            "**Scenarios:** BEH-09, BEH-10\n",
            {"tests/test_a.py": '"""BEH-09"""\ndef test_x():\n    pass\n'},
        )
        assert result.ok
        joined = "\n".join(result.warnings)
        assert "BEH-10" in joined and "uncovered" in joined
        assert "BEH-09," not in joined

    def test_covered_in_the_tree_is_silent(self, tmp_path):
        result = _validate(
            tmp_path,
            "**Mode:** verify_first\n**Verifies:** tests/test_a.py::test_x\n"
            "**Scenarios:** BEH-09\n",
            {"tests/test_a.py": '"""BEH-09"""\ndef test_x():\n    pass\n'},
        )
        assert not any("uncovered" in w for w in result.warnings)

    def test_missing_node_id_file_is_a_warning(self, tmp_path):
        result = _validate(
            tmp_path, "**Mode:** verify_first\n**Verifies:** tests/nope.py::test_x\n"
        )
        assert result.ok
        assert any("tests/nope.py" in w and "does not exist" in w for w in result.warnings)

    def test_unparseable_verifies_does_not_add_coverage_noise(self, tmp_path):  # Review Focus 5
        result = _validate(
            tmp_path,
            "**Mode:** verify_first\n**Verifies:** tests/a.py::t[a,b\n**Scenarios:** BEH-09\n",
        )
        assert any("unclosed" in e for e in result.errors)
        assert not any("uncovered" in w for w in result.warnings)
