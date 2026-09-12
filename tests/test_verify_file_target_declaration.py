"""BEH-01, BEH-02, BEH-04, BEH-18, BEH-19 (TASK-010, DT-10): the declared
group's outer vocabulary accepted from OUTSIDE — both declared forms, a
mixed group in either declared order, an intersecting/duplicated member,
and the group never inferred from anything but the declaration itself.

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-01
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-02
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-04
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-18
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-19
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/30-decomposition.md#DT-10
Traces: FR-01, FR-02, FR-12

BEH-18/BEH-19/BEH-04 are, per DT-10, properties of the already-delivered
cycle (DT-01..DT-04) rather than new implementation: this file owns proving
them from outside, not adding a line to `live_verify.py`/`task.py`/
`validate.py`. `tests/test_verify_first_declaration.py::TestGroupIsNeverInferred`
(WS-367) already covers BEH-04 for a group of node ids; this file adds the
file-target case beside it, not in place of it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.live_verify import run_live_verify
from spec_runner.task import Task, parse_tasks
from spec_runner.tdd_runners import PytestAdapter, Selector, SelectorRefusal, parse_group_element
from spec_runner.validate import _validate_verify_first_declarations

FIXTURE = Path(__file__).parent / "fixtures" / "verify_file_target_checked_by_sample.txt"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests").mkdir()
    return root


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "test_command": "python -m pytest",
        "max_retries": 1,
        "retry_delay_seconds": 0,
        "create_git_branch": False,
        "run_tests_on_done": False,
        "auto_commit": False,
        "run_review": False,
        "callback_url": "",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(**overrides) -> Task:
    defaults: dict = {
        "id": "TASK-501",
        "name": "verify-first declaration",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
        "execution_mode": "verify_first",
    }
    defaults.update(overrides)
    return Task(**defaults)


class TestFileTargetDeclaredAlongsideNodeIdInBothFormsAndAnyOrder:
    """kind: contract — BEH-01."""

    def test_single_line_form_is_accepted_as_a_file_target(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_x.py").write_text("def test_a():\n    assert True\n")
        _commit(root, "seed")
        tasks_path = root / "tasks.md"
        tasks_path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_x.py\n"
            "Est: 1d\n"
        )
        original = tasks_path.read_bytes()

        task = parse_tasks(tasks_path)[0]
        assert task.verifies == ["tests/test_x.py"]
        # BEH-01: not rewritten — what is read is what was written.
        assert tasks_path.read_bytes() == original

        parsed = parse_group_element(PytestAdapter(), task.verifies[0], root)
        assert isinstance(parsed, Selector), f"expected acceptance, got {parsed!r}"

        result = run_live_verify(task, _cfg(root))
        assert result.ran and result.passed, result.detail

    def test_block_form_is_accepted_as_a_file_target_and_kept_out_of_the_description(
        self, tmp_path
    ):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_x.py").write_text("def test_a():\n    assert True\n")
        (root / "tests" / "test_y.py").write_text("def test_b():\n    assert True\n")
        _commit(root, "seed")
        tasks_path = root / "tasks.md"
        tasks_path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:**\n"
            "- tests/test_x.py\n"
            "- tests/test_y.py\n"
            "Est: 1d\n"
        )

        task = parse_tasks(tasks_path)[0]
        assert task.verifies == ["tests/test_x.py", "tests/test_y.py"]
        assert "tests/test_x.py" not in task.description
        assert "tests/test_y.py" not in task.description

        for raw in task.verifies:
            parsed = parse_group_element(PytestAdapter(), raw, root)
            assert isinstance(parsed, Selector), f"{raw!r} refused: {parsed!r}"

        result = run_live_verify(task, _cfg(root))
        assert result.ran and result.passed, result.detail
        assert result.group_executed == ("tests/test_x.py", "tests/test_y.py")

    def test_mixed_declaration_and_its_reverse_execute_the_same_elements_in_declared_order(
        self, tmp_path
    ):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_x.py").write_text("def test_a():\n    assert True\n")
        (root / "tests" / "test_b.py").write_text("def test_y():\n    assert True\n")
        _commit(root, "seed")

        forward = root / "forward.md"
        forward.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_x.py, tests/test_b.py::test_y\n"
            "Est: 1d\n"
        )
        reverse = root / "reverse.md"
        reverse.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_b.py::test_y, tests/test_x.py\n"
            "Est: 1d\n"
        )

        forward_task = parse_tasks(forward)[0]
        reverse_task = parse_tasks(reverse)[0]
        assert forward_task.verifies == ["tests/test_x.py", "tests/test_b.py::test_y"]
        assert reverse_task.verifies == list(reversed(forward_task.verifies))

        forward_result = run_live_verify(forward_task, _cfg(root, state_file=root / ".fwd.db"))
        reverse_result = run_live_verify(reverse_task, _cfg(root, state_file=root / ".rev.db"))

        assert forward_result.ran and forward_result.passed, forward_result.detail
        assert reverse_result.ran and reverse_result.passed, reverse_result.detail
        assert forward_result.group_executed == (
            "tests/test_x.py",
            "tests/test_b.py::test_y",
        )
        # Same set of declared elements, opposite declared order — neither
        # form is rewritten into the other's order.
        assert reverse_result.group_executed == (
            "tests/test_b.py::test_y",
            "tests/test_x.py",
        )
        assert set(forward_result.group_executed) == set(reverse_result.group_executed)


class TestMeasuredWorkspaceSampleIsDeclaredWithoutManualExpansion:
    """kind: e2e — BEH-02."""

    def test_measured_sample_is_accepted_or_named_by_reason(self):
        assert FIXTURE.exists()
        lines = [line.strip() for line in FIXTURE.read_text().splitlines() if line.strip()]
        assert len(lines) == 145

        adapter = PytestAdapter()
        root = Path(__file__).resolve().parents[1]

        accepted: list[str] = []
        refused: list[tuple[str, str, str]] = []
        for target in lines:
            result = adapter.parse_group_element(target, root)
            if isinstance(result, Selector):
                accepted.append(target)
            else:
                assert isinstance(result, SelectorRefusal)
                refused.append((target, result.code, result.message))

        # BEH-02: the sample is declared without a manual expansion step —
        # every target is judged as a file target directly, and the class as
        # a whole is not vacuously refused. A weak `accepted` (non-empty
        # only) would still pass if acceptance regressed to a single lucky
        # target; the fixture's measured composition is 57 accepted / 85
        # `not_a_regular_file` / 3 `not_discoverable`, so pin a floor well
        # below that instead of the exact count (this repo's own tree can
        # gain or lose a handful of matching paths over time).
        assert len(accepted) >= 40, (
            f"only {len(accepted)}/{len(lines)} accepted, first refusals: {refused[:5]}"
        )
        refusal_codes = {code for _, code, _ in refused}
        assert len(refusal_codes) >= 2, (
            "refusals collapsed onto a single reason "
            f"({refusal_codes!r}); the sample is expected to exercise more than one"
        )
        for target, code, message in refused:
            assert target in message, (
                f"refusal for {target!r} must name the rejected value itself, "
                f"got code={code!r} message={message!r}"
            )


class TestGroupIsNeverInferredForAFileTarget:
    """kind: integration — BEH-04. The node-id case is
    `test_verify_first_declaration.py::TestGroupIsNeverInferred`; this is the
    file-target case beside it, on a live run rather than parsing alone.
    """

    def _decoy_task(self, **overrides) -> Task:
        defaults: dict = {
            "id": "TASK-502",
            "name": "tests/test_y.py looks like a test file itself",
            "traces_to": ["FR-77"],
            "description": (
                "The diff for this task touches tests/test_y.py and tests/test_z.py too."
            ),
            "checklist": [("проверка: tests/test_z.py зелёные", False)],
        }
        defaults.update(overrides)
        return _task(**defaults)

    def test_declared_file_target_wins_over_every_hint(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_x.py").write_text("def test_a():\n    assert True\n")
        (root / "tests" / "test_y.py").write_text(
            "def test_must_not_run():\n    assert False, 'must not run'\n"
        )
        (root / "tests" / "test_z.py").write_text(
            "def test_must_not_run():\n    assert False, 'must not run'\n"
        )
        _commit(root, "base")

        task = self._decoy_task(verifies=["tests/test_x.py"])
        result = run_live_verify(task, _cfg(root))

        assert result.ran and result.passed, (
            "only the declared file target must run; a hint-derived file "
            f"would have failed: {result.detail}"
        )
        assert result.group_executed == ("tests/test_x.py",), (
            "none of Traces to, the task name, the description, or the "
            f"checklist prose may contribute a member: {result.group_executed}"
        )

    def test_no_verifies_line_is_not_guessed_from_the_same_hints(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_x.py").write_text("def test_a():\n    assert True\n")
        (root / "tests" / "test_y.py").write_text("def test_b():\n    assert True\n")
        (root / "tests" / "test_z.py").write_text("def test_c():\n    assert True\n")
        _commit(root, "base")

        task = self._decoy_task(verifies=None)
        result = run_live_verify(task, _cfg(root))

        assert not result.ran and not result.passed, (
            "an undeclared group must refuse today, not run something "
            f"inferred from the hints: {result.detail}"
        )
        assert "tests/test_y.py" not in result.detail
        assert "tests/test_z.py" not in result.detail
        assert result.group_executed == ()


class TestMixedGroupIsJudgedAsDeclaredIncludingIntersection:
    """kind: integration — BEH-18."""

    def test_intersecting_declaration_is_not_a_validate_error(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_x.py").write_text(
            "def test_a():\n    assert True\n\n\ndef test_c():\n    assert True\n"
        )
        (root / "tests" / "test_y.py").write_text("def test_b():\n    assert True\n")
        _commit(root, "base")

        task = _task(
            verifies=[
                "tests/test_x.py",
                "tests/test_x.py::test_a",
                "tests/test_y.py::test_b",
            ]
        )
        result = _validate_verify_first_declarations([task], _cfg(root))

        assert result.errors == [], (
            f"an intersecting/duplicated element must not be a validate error: {result.errors}"
        )

    def test_full_group_executes_every_declared_element_in_order_not_deduplicated(self, tmp_path):
        # tests/test_x.py carries three tests; the group also re-declares
        # one of them by node id. Runner invocations (declared elements) is
        # therefore 3, while the number of UNIQUE tests actually executed is
        # 4 (test_a, test_c, test_d, test_b) — the two counts genuinely
        # differ, so asserting the former is not a coincidence of the fixture.
        root = _init_repo(tmp_path)
        (root / "tests" / "test_x.py").write_text(
            "def test_a():\n    assert True\n\n\n"
            "def test_c():\n    assert True\n\n\n"
            "def test_d():\n    assert True\n"
        )
        (root / "tests" / "test_y.py").write_text("def test_b():\n    assert True\n")
        _commit(root, "base")

        task = _task(
            verifies=[
                "tests/test_x.py",
                "tests/test_x.py::test_a",
                "tests/test_y.py::test_b",
            ]
        )
        result = run_live_verify(task, _cfg(root))

        assert result.ran and result.passed, result.detail
        assert result.group_executed == (
            "tests/test_x.py",
            "tests/test_x.py::test_a",
            "tests/test_y.py::test_b",
        ), "every declared element must run, in order, not deduplicated"
        assert len(result.group_executed) == 3

    def test_a_non_green_file_target_stops_the_group_immediately(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_x.py").write_text(
            "def test_a():\n    assert False, 'genuine failure'\n"
        )
        (root / "tests" / "test_y.py").write_text("def test_b():\n    assert True\n")
        remaining_marker = root / "remaining_ran.txt"
        (root / "tests" / "test_z.py").write_text(
            "from pathlib import Path\n\n\n"
            "def test_never():\n"
            f"    Path({str(remaining_marker)!r}).write_text('ran')\n"
        )
        _commit(root, "base")

        task = _task(
            verifies=["tests/test_x.py", "tests/test_y.py::test_b", "tests/test_z.py::test_never"]
        )
        result = run_live_verify(task, _cfg(root))

        assert result.ran and not result.passed, (
            f"a failing first element is a genuine test_failure: {result.detail}"
        )
        assert result.group_executed == ("tests/test_x.py",), (
            "the group must return on its first non-green element — nothing "
            f"after it runs: {result.group_executed}"
        )
        assert not remaining_marker.exists(), "an element after the failure must never execute"

    def test_a_non_green_node_id_stops_the_group_at_its_own_position(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_x.py").write_text("def test_a():\n    assert True\n")
        (root / "tests" / "test_y.py").write_text(
            "def test_b():\n    assert False, 'genuine failure'\n"
        )
        remaining_marker = root / "remaining_ran.txt"
        (root / "tests" / "test_z.py").write_text(
            "from pathlib import Path\n\n\n"
            "def test_never():\n"
            f"    Path({str(remaining_marker)!r}).write_text('ran')\n"
        )
        _commit(root, "base")

        task = _task(
            verifies=["tests/test_x.py", "tests/test_y.py::test_b", "tests/test_z.py::test_never"]
        )
        result = run_live_verify(task, _cfg(root))

        assert result.ran and not result.passed, (
            f"a failing node id member is a genuine test_failure: {result.detail}"
        )
        assert result.group_executed == ("tests/test_x.py", "tests/test_y.py::test_b"), (
            "the run count equals the ordinal position of the first "
            f"non-green element (2 here), not the full declaration: "
            f"{result.group_executed}"
        )
        assert not remaining_marker.exists(), "the element after the failure must never execute"


class TestDuplicatedMemberSatisfiesTheRuleBothTimes:
    """kind: contract — BEH-19."""

    def test_a_member_that_behaves_differently_across_its_two_occurrences_is_not_averaged(
        self, tmp_path
    ):
        # The SAME test is a member of the group twice: once inside the file
        # target (alongside a second, always-passing test) and once again by
        # its own node id. It is deterministic and stateful (a counter file,
        # written once per real invocation) rather than flaky: it passes on
        # its first invocation and fails on its second — proving the group
        # does not retry, average, or credit "one out of two" as green.
        root = _init_repo(tmp_path)
        counter = root / "counter.txt"
        (root / "tests" / "test_x.py").write_text(
            "from pathlib import Path\n\n"
            f"COUNTER = Path({str(counter)!r})\n\n\n"
            "def test_a():\n"
            "    n = int(COUNTER.read_text()) if COUNTER.exists() else 0\n"
            "    COUNTER.write_text(str(n + 1))\n"
            "    assert n == 0\n\n\n"
            "def test_c():\n"
            "    assert True\n"
        )
        _commit(root, "base")

        task = _task(verifies=["tests/test_x.py", "tests/test_x.py::test_a"])
        result = run_live_verify(task, _cfg(root))

        assert result.ran and not result.passed, (
            "the second occurrence's genuine failure must not be averaged "
            f"away by the first occurrence's pass: {result.detail}"
        )
        # Both occurrences were presented — the duplicate was neither
        # deduplicated away nor retried in place of the first.
        assert result.group_executed == (
            "tests/test_x.py",
            "tests/test_x.py::test_a",
        )
        assert counter.read_text() == "2", (
            "exactly two real invocations of the shared test — no retry "
            f"and no skipped re-run: counter={counter.read_text()!r}"
        )
