"""BEH-09 (DT-04): `validate` judges a declared verify-first group through the
declared-group-element vocabulary (`tdd_runners.parse_group_element`), not
through `parse_selector` — and a file target missing from the *working tree*
is a warning, not an error, because `validate` judges nothing against a
commit: existence there is a fact for the live run to establish, not for
static validation to assume can never change before the candidate is built.

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/30-decomposition.md#DT-04
        workstreams/verify-first-file-scope-group-targets-20260908/spec/20-design.md (точка врезки 4)
"""

from pathlib import Path

from spec_runner.validate import validate_all


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tasks.md"
    path.write_text(body)
    return path


class TestFileTargetMissingFromWorkingTreeIsAWarningNotAnError:
    """kind: integration — BEH-09: a bare file-path declaration (no `::`)
    that does not exist in the working tree is accepted as a legal group
    element and reported as a warning, distinct from the defective forms
    (glob, `-k`, marker, symlink, directory, outside-repo, empty element,
    unsupported adapter, empty group) that remain errors."""

    def test_missing_file_target_warns_and_does_not_fail_validation(self, tmp_path):
        tasks_path = _write(
            tmp_path,
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_missing_file.py\n"
            "Est: 1d\n",
        )

        result = validate_all(tasks_file=tasks_path, config_file=None)

        # The new contract: an unresolved-but-well-formed file target does
        # not fail validation at all — it is deferred to the live run.
        assert result.ok, f"unexpected errors: {result.errors}"
        joined_warnings = "\n".join(result.warnings)
        assert "TASK-001" in joined_warnings
        assert "tests/test_missing_file.py" in joined_warnings
        # The retired boundary must not resurface as an error under the new
        # contract: this declaration is no longer judged "not a node id".
        joined_errors = "\n".join(result.errors)
        assert "not a node id" not in joined_errors
