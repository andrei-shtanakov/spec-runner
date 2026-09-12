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
    ALLOWED_TASK_RESULT_FIELDS,
    FIXTURES_DIR,
    OPTIONAL_TASK_RESULT_FIELDS,
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
    def test_verify_composition_is_optional(self) -> None:
        assert "verify_composition" in OPTIONAL_TASK_RESULT_FIELDS
        assert "verify_composition" in ALLOWED_TASK_RESULT_FIELDS

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
        """The subject is a test contract, not runtime (BEH-33): the fix
        lives entirely in tests/test_json_result_contract.py, and
        `build_task_json_result` neither imports nor references the
        allow-list to decide what it prints."""
        source = inspect.getsource(cli_module)
        assert "OPTIONAL_TASK_RESULT_FIELDS" not in source
        assert "ALLOWED_TASK_RESULT_FIELDS" not in source


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
