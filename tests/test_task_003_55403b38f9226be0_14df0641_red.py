"""BEH-04 and BEH-05: an invalid verify-first declaration refuses before any
execution — on the `validate` surface — and the refusal reproduces the
adapter's own wording plus the expected selector form.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-04
        workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-05
Traces: FR-03, FR-24

`validate_all` — the function `spec-runner validate` runs — currently checks
task fields (`validate_task_fields`) and config (`validate_config`)
independently, and neither cross-references a task's resolved execution mode
against its declared `**Verifies:**` group through the project's test
adapter. A verify-first task that declares a selector `PytestAdapter` refuses
(`not_a_node_id`, since it is a bare file target rather than `path::test`)
therefore passes `validate` today — this test pins that it must not: BEH-04
requires the refusal before any live run, and BEH-05 requires the message to
name the task and mode, quote the offending selector verbatim, and reproduce
the adapter's own `SelectorRefusal.message` (which also names the expected
form, `path::test`) rather than a generic "selector rejected" note.
"""

from pathlib import Path

from spec_runner.validate import validate_all


class TestInvalidVerifyFirstDeclarationRefusesOnValidate:
    def test_adapter_refused_selector_is_named_task_mode_verbatim_value_and_adapter_wording(
        self, tmp_path: Path
    ):
        tasks_path = tmp_path / "tasks.md"
        tasks_path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_x.py\n"
            "Est: 1d\n"
        )

        result = validate_all(tasks_file=tasks_path, config_file=None)

        assert not result.ok, (
            "a verify-first task whose declared selector the project's adapter "
            "refuses must fail `validate` (BEH-04); it currently passes"
        )
        joined = "\n".join(result.errors)
        assert "TASK-001" in joined
        assert "verify_first" in joined
        # BEH-05: the rejected value is quoted dословно (verbatim) …
        assert "tests/test_x.py" in joined
        # … and the message reproduces the adapter's own wording (code
        # `not_a_node_id`) and names the expected form, not just the fact of
        # a refusal.
        assert "not a node id" in joined
        assert "path::test" in joined
