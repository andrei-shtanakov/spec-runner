"""Contract tests for the `--json-result` output and executor-state schema.

Freezes the Maestro interop surface. Any change here is a **breaking change**
requiring a major version bump and a `BREAKING` note in `CHANGELOG.md`.

See also:
- docs/state-schema.md
- schemas/executor-state.schema.json
- schemas/json-result.schema.json
- tests/fixtures/maestro-interop/

To intentionally update the golden fixtures after a deliberate breaking change,
run: `uv run pytest tests/test_json_result_contract.py --update-golden`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

from spec_runner.cli import build_task_json_result
from spec_runner.config import ExecutorConfig
from spec_runner.state import (
    ErrorCode,
    ExecutorState,
    ReviewVerdict,
    TaskAttempt,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "maestro-interop"
SCHEMAS_DIR = REPO_ROOT / "schemas"


@pytest.fixture
def update_golden(request) -> bool:
    return bool(request.config.getoption("--update-golden", default=False))


# --- Helpers -------------------------------------------------------------


def _make_state(tmp_path: Path) -> ExecutorState:
    state_file = tmp_path / ".executor-state.db"
    config = ExecutorConfig(state_file=state_file, project_root=tmp_path)
    return ExecutorState(config)


def _seed_task(
    state: ExecutorState,
    task_id: str,
    *,
    success: bool,
    duration: float,
    input_tokens: int,
    output_tokens: int,
    cost: float,
    review: ReviewVerdict,
    error: str | None = None,
    error_code: ErrorCode | None = None,
    attempts: int = 1,
) -> None:
    ts = state.get_task_state(task_id)
    ts.status = "success" if success else "failed"
    for i in range(attempts):
        last = i == attempts - 1
        ts.attempts.append(
            TaskAttempt(
                timestamp=f"2026-04-17T10:0{i}:00",
                success=success if last else False,
                duration_seconds=duration / attempts,
                error=error if last and not success else None,
                error_code=error_code if last and not success else None,
                input_tokens=input_tokens // attempts,
                output_tokens=output_tokens // attempts,
                cost_usd=round(cost / attempts, 4),
                review_status=review.value if last else None,
                claude_output="stub",
            )
        )


def _assert_matches_golden(
    actual: dict | list,
    golden_name: str,
    update: bool,
) -> None:
    path = FIXTURES_DIR / golden_name
    serialized = json.dumps(actual, indent=2, sort_keys=True) + "\n"
    if update or not path.exists():
        path.write_text(serialized, encoding="utf-8")
        return
    expected = path.read_text(encoding="utf-8")
    assert serialized == expected, (
        f"Contract drift in {golden_name}.\n"
        f"If this is an intentional breaking change:\n"
        f"  1. Bump to a new major version\n"
        f"  2. Add BREAKING note to CHANGELOG.md\n"
        f"  3. Run: uv run pytest {Path(__file__).name} --update-golden"
    )


def _validate_against_schema(payload: dict | list, schema_name: str) -> None:
    schema = json.loads((SCHEMAS_DIR / schema_name).read_text(encoding="utf-8"))
    Draft7Validator.check_schema(schema)
    Draft7Validator(schema).validate(payload)


# --- Stable surface: field sets ------------------------------------------


REQUIRED_TASK_RESULT_FIELDS = {"task_id", "status", "attempts"}
OPTIONAL_TASK_RESULT_FIELDS = {
    "cost_usd",
    "tokens",
    "duration_seconds",
    "review",
    "error",
    "exit_code",
}
ALLOWED_TASK_RESULT_FIELDS = REQUIRED_TASK_RESULT_FIELDS | OPTIONAL_TASK_RESULT_FIELDS


def _assert_field_set(entry: dict) -> None:
    extra = set(entry) - ALLOWED_TASK_RESULT_FIELDS
    missing = REQUIRED_TASK_RESULT_FIELDS - set(entry)
    assert not extra, f"Unknown fields in --json-result entry: {sorted(extra)}"
    assert not missing, f"Missing required fields: {sorted(missing)}"


# --- Schemas are well-formed --------------------------------------------


class TestSchemaWellFormed:
    def test_executor_state_schema_is_valid(self) -> None:
        schema = json.loads(
            (SCHEMAS_DIR / "executor-state.schema.json").read_text(encoding="utf-8")
        )
        Draft7Validator.check_schema(schema)

    def test_json_result_schema_is_valid(self) -> None:
        schema = json.loads((SCHEMAS_DIR / "json-result.schema.json").read_text(encoding="utf-8"))
        Draft7Validator.check_schema(schema)


# --- Golden fixtures -----------------------------------------------------


class TestJsonResultGolden:
    def test_golden_single_success(self, tmp_path: Path, update_golden: bool) -> None:
        state = _make_state(tmp_path)
        _seed_task(
            state,
            "TASK-001",
            success=True,
            duration=123.456,
            input_tokens=1500,
            output_tokens=800,
            cost=0.42,
            review=ReviewVerdict.PASSED,
        )
        result = build_task_json_result("TASK-001", state)
        _assert_field_set(result)
        _validate_against_schema(result, "json-result.schema.json")
        _assert_matches_golden(result, "json-result-single-success.json", update_golden)

    def test_golden_single_failure(self, tmp_path: Path, update_golden: bool) -> None:
        state = _make_state(tmp_path)
        _seed_task(
            state,
            "TASK-002",
            success=False,
            duration=45.6,
            input_tokens=500,
            output_tokens=200,
            cost=0.18,
            review=ReviewVerdict.FAILED,
            error="Tests failed: 3 assertions mismatched in module foo",
            error_code=ErrorCode.TEST_FAILURE,
            attempts=2,
        )
        result = build_task_json_result("TASK-002", state)
        _assert_field_set(result)
        _validate_against_schema(result, "json-result.schema.json")
        _assert_matches_golden(result, "json-result-single-failure.json", update_golden)

    def test_golden_multi(self, tmp_path: Path, update_golden: bool) -> None:
        state = _make_state(tmp_path)
        _seed_task(
            state,
            "TASK-001",
            success=True,
            duration=10.0,
            input_tokens=1000,
            output_tokens=500,
            cost=0.10,
            review=ReviewVerdict.PASSED,
        )
        _seed_task(
            state,
            "TASK-002",
            success=False,
            duration=20.0,
            input_tokens=2000,
            output_tokens=1000,
            cost=0.25,
            review=ReviewVerdict.REJECTED,
            error="Budget exceeded after 3 retries",
            error_code=ErrorCode.BUDGET_EXCEEDED,
        )
        result = [build_task_json_result(tid, state) for tid in ("TASK-001", "TASK-002")]
        for entry in result:
            _assert_field_set(entry)
        _validate_against_schema(result, "json-result.schema.json")
        _assert_matches_golden(result, "json-result-multi.json", update_golden)

    def test_golden_empty(self, update_golden: bool) -> None:
        result = {"tasks": [], "message": "No tasks ready to execute"}
        _validate_against_schema(result, "json-result.schema.json")
        _assert_matches_golden(result, "json-result-empty.json", update_golden)


# --- Error truncation (documented contract: 200 chars) ------------------


class TestLegacyJsonStateFixture:
    """The legacy `.executor-state.json` fixture is Maestro's read-only fallback."""

    def test_legacy_fixture_matches_executor_state_schema(self) -> None:
        fixture = json.loads(
            (FIXTURES_DIR / "json-result-legacy-json-state.json").read_text(encoding="utf-8")
        )
        _validate_against_schema(fixture, "executor-state.schema.json")


class TestErrorTruncation:
    def test_error_truncated_to_200_chars(self, tmp_path: Path) -> None:
        state = _make_state(tmp_path)
        long_err = "x" * 500
        _seed_task(
            state,
            "TASK-001",
            success=False,
            duration=1.0,
            input_tokens=0,
            output_tokens=0,
            cost=0.0,
            review=ReviewVerdict.FAILED,
            error=long_err,
            error_code=ErrorCode.UNKNOWN,
        )
        result = build_task_json_result("TASK-001", state)
        assert len(result["error"]) == 200
        assert result["error"] == "x" * 200
