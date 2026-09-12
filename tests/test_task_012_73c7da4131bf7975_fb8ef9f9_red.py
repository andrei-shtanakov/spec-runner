"""RED checkpoint for TASK-012 (BEH-33, DT-12).

`build_task_json_result` already emits a top-level `verify_composition` key
(FR-21/FR-22, #367 file-scope group targets), but the frozen Maestro-interop
contract test's own allow-list of optional `--json-result` fields —
`OPTIONAL_TASK_RESULT_FIELDS` in tests/test_json_result_contract.py — does not
know about it yet. The contract test is green today only because nothing
currently exercises that key against the allow-list; the allow-list itself is
stale relative to the surface the code already prints.
"""

from __future__ import annotations

from tests.test_json_result_contract import OPTIONAL_TASK_RESULT_FIELDS


class TestOptionalFieldsKnowVerifyComposition:
    def test_verify_composition_is_an_allowed_optional_field(self) -> None:
        assert "verify_composition" in OPTIONAL_TASK_RESULT_FIELDS
