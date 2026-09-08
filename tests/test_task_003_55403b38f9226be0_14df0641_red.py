"""BEH-04, BEH-05 and BEH-09: an invalid verify-first declaration refuses
before any execution — on the `validate` surface — and the refusal
reproduces the adapter's own wording plus the expected selector form.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-04
        workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-05
        workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-09
Traces: FR-03, FR-24, FR-07

Originally pinned that `validate_all` refused a bare file target
(`tests/test_x.py`) the same way it refused a malformed node id
(`not_a_node_id`, `path::test`): `_validate_verify_first_declarations` read
every declared element through `PytestAdapter.parse_selector`, a
RED-checkpoint vocabulary about exactly one test, which has no notion of a
file target at all.

DT-04 (#367 follow-up, BEH-09) retires that boundary: `validate` now reads a
declared group through `tdd_runners.parse_group_element`, the
declared-group-element vocabulary (DT-01), under which a bare file path is a
legal element. A file missing from the *working tree* is a warning, not an
error — `validate` judges the tree in hand, never a commit a live run has not
built yet — so this same declaration now passes `validate` with a warning
instead of failing it. This file keeps standing on that exact declaration
(`tests/test_x.py`) so the transition is visible in the diff rather than
silently dropped; only the asserted contract changed.
"""

from pathlib import Path

from spec_runner.validate import validate_all


class TestFileTargetDeclarationWarnsInsteadOfRefusingOnValidate:
    def test_missing_file_target_is_named_in_a_warning_not_an_error(self, tmp_path: Path):
        tasks_path = tmp_path / "tasks.md"
        tasks_path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_x.py\n"
            "Est: 1d\n"
        )

        # Hermetic (sr397 review): judged against tmp_path, not whatever cwd
        # pytest happens to run from — "missing" must be a property of the
        # fixture, not of the repository's current contents.
        result = validate_all(tasks_file=tasks_path, config_file=None, project_root=tmp_path)

        # BEH-09: a bare file-path declaration missing from the working tree
        # no longer fails `validate` — existence there is a live-run concern.
        assert result.ok, f"unexpected errors: {result.errors}"
        joined = "\n".join(result.warnings)
        assert "TASK-001" in joined
        # The rejected value is still quoted verbatim …
        assert "tests/test_x.py" in joined
        # … but the retired wording ("not a node id" / "path::test") must not
        # resurface: this declaration is no longer judged against the
        # RED-checkpoint vocabulary.
        joined_errors = "\n".join(result.errors)
        assert "not a node id" not in joined_errors
        assert "path::test" not in joined_errors
