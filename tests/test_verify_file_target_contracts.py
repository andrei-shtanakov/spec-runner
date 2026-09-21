"""BEH-30, BEH-33 (#367 file-scope group targets, TASK-012, DT-12, group
regression).

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-30 (-BEH-33)
Traces: FR-13, FR-16

`verify_composition` (FR-21/FR-22) is already emitted by
`build_task_json_result` and already described in
`schemas/json-result.schema.json` — TASK-007 shipped the surface. What was
missing is the frozen Maestro-interop contract test's own allow-list,
`OPTIONAL_TASK_RESULT_FIELDS` in tests/test_json_result_contract.py, which
did not know the key the code already prints.

BEH-30 pins that the whole extension is additive: no existing field's type
or meaning changed, every pre-existing golden fixture still validates and
still matches byte-for-byte, and a consumer that never learned about
`verify_composition` reads exactly what it read before this workstream —
no major version bump required, and the new field is documented in the
schema rather than appearing in the output undocumented.

BEH-33 pins the fix itself: `OPTIONAL_TASK_RESULT_FIELDS` now recognizes
`verify_composition`, and that recognition is a test-only construct — the
allow-list is applied only inside tests/test_json_result_contract.py, and no
production path reads or enforces it.
"""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path

from spec_runner import cli as cli_module
from spec_runner.cli import build_task_json_result
from spec_runner.config import ExecutorConfig
from spec_runner.live_verify import CompositionMember, VerifyRunResult
from spec_runner.state import ExecutorState, ReviewVerdict, TaskAttempt
from spec_runner.task import Task
from tests.test_json_result_contract import (
    FIXTURES_DIR,
    SCHEMAS_DIR,
    _assert_field_set,
    _validate_against_schema,
)


def _make_state(tmp_path: Path) -> tuple[ExecutorState, ExecutorConfig]:
    config = ExecutorConfig(state_file=tmp_path / ".executor-state.db", project_root=tmp_path)
    return ExecutorState(config), config


def _seed_success(state: ExecutorState, task_id: str) -> None:
    ts = state.get_task_state(task_id)
    ts.status = "success"
    ts.attempts.append(
        TaskAttempt(
            timestamp="2026-09-08T10:00:00",
            success=True,
            duration_seconds=1.0,
            input_tokens=10,
            output_tokens=10,
            cost_usd=0.01,
            review_status=ReviewVerdict.PASSED.value,
            claude_output="stub",
        )
    )


def _verify_first_task(task_id: str) -> Task:
    return Task(
        id=task_id,
        name="verify-first task",
        priority="p1",
        status="todo",
        estimate="1h",
        execution_mode="verify_first",
        verifies=["tests/test_group.py::test_it", "tests/test_group.py::test_other"],
    )


def _record_evidence_with_composition(
    state: ExecutorState, config: ExecutorConfig, task: Task
) -> None:
    result = VerifyRunResult(
        sha="deadbeef",
        ran=True,
        passed=True,
        detail="stub",
        group_executed=tuple(task.verifies or ()),
        adapter="pytest",
        composition=(
            CompositionMember(member="tests/test_group.py::test_it", outcome="passed"),
            CompositionMember(
                member="tests/test_group.py::test_other", outcome="skipped", reason="xfail"
            ),
        ),
    )
    recorded = state.record_verify_evidence(task=task, config=config, result=result)
    assert recorded, "setup: verify evidence must be recorded for this test to mean anything"


# --- BEH-33: the allow-list knows verify_composition ---------------------


class TestAllowListKnowsVerifyComposition:
    def test_a_real_entry_carrying_it_passes_the_allow_list(self, tmp_path: Path) -> None:
        state, config = _make_state(tmp_path)
        task = _verify_first_task("TASK-101")
        _seed_success(state, task.id)
        _record_evidence_with_composition(state, config, task)

        result = build_task_json_result(task.id, state, config)
        assert "verify_composition" in result
        _assert_field_set(result)  # must not raise: an unknown/extra field would
        _validate_against_schema(result, "json-result.schema.json")

    def test_allow_list_is_test_only_no_production_path_reads_it(self) -> None:
        """The subject is a test contract, not runtime (BEH-33).

        Asserted by imports rather than by text (spec-runner#450). The
        previous version read `inspect.getsource(cli_module)` and required
        the two names to be absent from it, which was both too narrow and too
        brittle: the allow-list applied in any OTHER production module
        (`cli_info.py`, `mcp_server.py`, `review_pr.py`) went unseen, while a
        mere sentence — "keep in sync with `OPTIONAL_TASK_RESULT_FIELDS`" in
        a docstring — would have reddened the contract group without any
        change in behaviour. `20-design.md` forbids proving BEH-33 by grep
        for exactly this reason.

        The allow-list is defined in `tests/test_json_result_contract.py`, so
        the real property is structural and covers the whole package: no
        module under `spec_runner` imports the test suite. What
        `build_task_json_result` actually prints is asserted by the test
        above, against a real result and the schema.

        Two checks, because the import check alone cannot fail for the
        defect BEH-33 names (spec-runner#462). A production module does not
        have to *import* the list to apply it — the ordinary shape of this
        defect is a copy: the same names bound in `cli.py` and filtered
        against, which no import ever mentions. So the same ASTs are walked
        a second time for a binding or a use of those names anywhere under
        `spec_runner`. Still structural, not grep: an occurrence inside a
        string or a comment — the "keep in sync with
        `OPTIONAL_TASK_RESULT_FIELDS`" sentence `20-design.md` explicitly
        does not want reddening a contract — is not a node.
        """
        package = Path(inspect.getfile(cli_module)).parent
        trees = {
            module.relative_to(package): ast.parse(module.read_text(encoding="utf-8"))
            for module in sorted(package.rglob("*.py"))
        }

        offenders: list[str] = []
        for module, tree in trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                if any(name == "tests" or name.startswith("tests.") for name in names):
                    offenders.append(f"{module}:{node.lineno}")

        assert not offenders, (
            f"production modules import the test suite, so a test-only "
            f"allow-list could reach runtime: {offenders}"
        )

        allow_list_names = {
            "REQUIRED_TASK_RESULT_FIELDS",
            "OPTIONAL_TASK_RESULT_FIELDS",
            "ALLOWED_TASK_RESULT_FIELDS",
        }
        applied: list[str] = []
        for module, tree in trees.items():
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and node.id in allow_list_names:
                    applied.append(f"{module}:{node.lineno}:{node.id}")
                elif isinstance(node, ast.Attribute) and node.attr in allow_list_names:
                    applied.append(f"{module}:{node.lineno}:{node.attr}")

        assert not applied, (
            f"the allow-list is a test-only construct (BEH-33), but a "
            f"production module binds or reads it: {applied}"
        )


# --- BEH-30: the extension is additive ------------------------------------


class TestExtensionIsAdditive:
    def test_new_field_is_documented_in_the_schema(self) -> None:
        schema = json.loads((SCHEMAS_DIR / "json-result.schema.json").read_text(encoding="utf-8"))
        properties = schema["definitions"]["TaskResult"]["properties"]
        assert "verify_composition" in properties
        sub_properties = properties["verify_composition"]["properties"]
        assert set(sub_properties) == {"size", "executed", "skipped"}
        assert properties["verify_composition"]["required"] == ["size", "executed", "skipped"]

    def test_existing_golden_fixtures_still_validate(self) -> None:
        for name in (
            "json-result-single-success.json",
            "json-result-single-noop.json",
            "json-result-single-failure.json",
            "json-result-multi.json",
            "json-result-empty.json",
        ):
            payload = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
            _validate_against_schema(payload, "json-result.schema.json")

    def test_existing_golden_fixtures_carry_no_new_field(self) -> None:
        """A consumer that never learned about `verify_composition` reads
        exactly what it read before this workstream: none of the
        pre-existing golden fixtures mention it."""
        for name in (
            "json-result-single-success.json",
            "json-result-single-noop.json",
            "json-result-single-failure.json",
            "json-result-multi.json",
        ):
            payload = json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))
            entries = payload if isinstance(payload, list) else [payload]
            for entry in entries:
                assert "verify_composition" not in entry
                assert "verify_outcome" not in entry

    def test_task_without_composition_gets_neither_field(self, tmp_path: Path) -> None:
        state, config = _make_state(tmp_path)
        _seed_success(state, "TASK-001")

        result = build_task_json_result("TASK-001", state, config)
        _assert_field_set(result)
        assert "verify_composition" not in result
        assert "verify_outcome" not in result

    def test_verify_first_without_composition_gets_outcome_but_not_composition(
        self, tmp_path: Path
    ) -> None:
        """A group of node ids only writes `verify_outcome` and stays silent
        on `verify_composition` — the field is additive on top of BEH-32,
        not a replacement for it."""
        state, config = _make_state(tmp_path)
        task = _verify_first_task("TASK-102")
        _seed_success(state, task.id)
        result_run = VerifyRunResult(
            sha="deadbeef",
            ran=True,
            passed=True,
            detail="stub",
            group_executed=tuple(task.verifies or ()),
            adapter="pytest",
        )
        recorded = state.record_verify_evidence(task=task, config=config, result=result_run)
        assert recorded

        result = build_task_json_result(task.id, state, config)
        assert result["verify_outcome"] == "green"
        assert "verify_composition" not in result

    def test_verify_composition_shape_matches_schema_documentation(self, tmp_path: Path) -> None:
        state, config = _make_state(tmp_path)
        task = _verify_first_task("TASK-103")
        _seed_success(state, task.id)
        _record_evidence_with_composition(state, config, task)

        result = build_task_json_result(task.id, state, config)
        _validate_against_schema(result, "json-result.schema.json")
        composition = result["verify_composition"]
        assert composition == {"size": 2, "executed": 1, "skipped": 1}
