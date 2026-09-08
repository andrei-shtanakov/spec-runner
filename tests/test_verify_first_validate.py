"""BEH-04 and BEH-05: an invalid verify-first declaration refuses before any
execution — on the `validate` surface and on CLI start — and the refusal
reproduces the adapter's own wording plus the expected selector form.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-04
        workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-05
Traces: FR-03, FR-24

`validate_all` is the one function both surfaces route through: the
`spec-runner validate` command calls it directly, and `_run_tasks`/`cmd_watch`
call it before a single task executes (`cli.py`) — so extending it here
closes both doors BEH-04 asks for at once. This slice exercises `validate_all`
directly rather than the CLI, the same way TASK-001/002's sibling test files
do (`test_verify_first_mode.py`, `test_verify_first_declaration.py`).
"""

from pathlib import Path

from spec_runner.validate import validate_all


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "tasks.md"
    path.write_text(body)
    return path


class TestUnknownModeRefusesBeforeExecution:
    """kind: integration — BEH-04, defect 1: an unrecognised `**Mode:**`."""

    def test_unknown_mode_value_names_the_task_and_the_value(self, tmp_path):
        tasks_path = _write(
            tmp_path,
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n**Mode:** verify-frist\nEst: 1d\n",
        )

        result = validate_all(tasks_file=tasks_path, config_file=None)

        assert not result.ok
        joined = "\n".join(result.errors)
        assert "TASK-001" in joined
        assert "verify-frist" in joined


class TestVerifyFirstWithoutADeclaredGroupRefuses:
    """kind: integration — BEH-04, defect 2: verify_first with no
    `**Verifies:**` line at all."""

    def test_missing_verifies_line_is_named(self, tmp_path):
        tasks_path = _write(
            tmp_path,
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n**Mode:** verify_first\nEst: 1d\n",
        )

        result = validate_all(tasks_file=tasks_path, config_file=None)

        assert not result.ok
        joined = "\n".join(result.errors)
        assert "TASK-001" in joined
        assert "verify_first" in joined


class TestEmptyDeclaredGroupRefuses:
    """kind: integration — BEH-04, defect 3: a bare `**Verifies:**` marker
    declaring zero selectors — distinct from no line at all (#372)."""

    def test_bare_marker_with_no_selectors_is_named_empty_not_missing(self, tmp_path):
        tasks_path = _write(
            tmp_path,
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n**Verifies:**\nEst: 1d\n",
        )

        result = validate_all(tasks_file=tasks_path, config_file=None)

        assert not result.ok
        joined = "\n".join(result.errors)
        assert "TASK-001" in joined
        assert "empty" in joined


class TestAdapterRefusedSelectorRefuses:
    """kind: integration — BEH-04 defect 4 / BEH-05: a selector the project's
    adapter will not accept, mixed in with a selector it does accept.

    #367/DT-04 (BEH-09): a bare file target mixed into a group (once refused
    as `not_a_node_id`) is now read through the declared-group-element
    vocabulary, and a copy missing from the working tree warns instead of
    refusing — the scenario stays a mixed group, but the assertion moves from
    "the group fails" to "the file target warns, the group still passes"."""

    def test_the_missing_file_target_in_a_mixed_group_warns_not_refuses(self, tmp_path):
        tasks_path = _write(
            tmp_path,
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_a.py::test_x, tests/test_b.py\n"
            "Est: 1d\n",
        )

        # Hermetic (sr397 review): judged against tmp_path, not whatever cwd
        # pytest happens to run from — "missing" must be a property of the
        # fixture, not of the repository's current contents.
        result = validate_all(tasks_file=tasks_path, config_file=None, project_root=tmp_path)

        assert result.ok, f"unexpected errors: {result.errors}"
        joined = "\n".join(result.warnings)
        assert "TASK-001" in joined
        # BEH-05's guarantee survives the boundary move: the offending value
        # is still quoted verbatim …
        assert "tests/test_b.py" in joined
        # … but the retired wording ("not a node id" / "path::test") must not
        # resurface as an error under the new contract.
        joined_errors = "\n".join(result.errors)
        assert "not a node id" not in joined_errors
        assert "path::test" not in joined_errors
        # The accepted node-id selector in the same group is not itself
        # flagged — only the file target produced a warning, and it is named
        # specifically, not the whole group.
        bad_selector_warnings = [w for w in result.warnings if "tests/test_b.py" in w]
        assert len(bad_selector_warnings) == 1

    def test_a_still_defective_form_in_a_mixed_group_gives_exactly_one_named_error(self, tmp_path):
        """sr397 review, major finding: rewriting the sibling test above (to
        cover the retired "missing file" boundary) dropped the last live
        coverage of validate.py's adapter-refusal branch for a MIXED group —
        "one error per defective element, the valid neighbour untouched" and
        "the message names the mode" were no longer asserted anywhere. A
        glob pattern stays a genuine BEH-03 form defect under the new
        vocabulary (unlike a bare missing file target), so it exercises the
        exact same error branch (`validate.py`'s `for raw in
        task.verifies` loop) without reintroducing the retired contract."""
        tasks_path = _write(
            tmp_path,
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_a.py::test_x, tests/test_*.py\n"
            "Est: 1d\n",
        )

        result = validate_all(tasks_file=tasks_path, config_file=None)

        assert not result.ok
        joined = "\n".join(result.errors)
        # The refusal names the resolved mode (BEH-05's guarantee).
        assert "verify_first" in joined
        # Exactly one error — per defective ELEMENT, not per group — and it
        # names only the glob, never the valid node-id neighbour.
        bad_selector_errors = [e for e in result.errors if "refused by the" in e]
        assert len(bad_selector_errors) == 1
        assert "selector 'tests/test_*.py'" in bad_selector_errors[0]
        assert "glob_pattern" in bad_selector_errors[0]

    def test_a_selector_naming_no_file_is_refused_too(self, tmp_path):
        """A different `SelectorRefusal` code (`not_a_node_id` for an empty
        path) must surface with its own wording, not a generic note."""
        tasks_path = _write(
            tmp_path,
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** ::test_x\n"
            "Est: 1d\n",
        )

        result = validate_all(tasks_file=tasks_path, config_file=None)

        assert not result.ok
        joined = "\n".join(result.errors)
        assert "names no file" in joined


class TestDeclaredGroupOnANonVerifyFirstTaskRefuses:
    """kind: integration — BEH-04 defect 5: a task whose *resolved* mode is
    not verify_first still carries a `**Verifies:**` group — a false
    expectation, since that group would never run."""

    def test_explicit_standard_mode_with_a_declared_group_is_refused(self, tmp_path):
        tasks_path = _write(
            tmp_path,
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** standard\n"
            "**Verifies:** tests/test_a.py::test_x\n"
            "Est: 1d\n",
        )

        result = validate_all(tasks_file=tasks_path, config_file=None)

        assert not result.ok
        joined = "\n".join(result.errors)
        assert "TASK-001" in joined
        assert "standard" in joined
        assert "tests/test_a.py::test_x" in joined

    def test_default_project_mode_with_a_declared_group_is_refused(self, tmp_path):
        """No per-task `**Mode:**` at all: the task resolves to the
        project's default (`standard`), and the declared group is just as
        false an expectation as an explicit `**Mode:** standard`."""
        tasks_path = _write(
            tmp_path,
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Verifies:** tests/test_a.py::test_x\n"
            "Est: 1d\n",
        )

        result = validate_all(tasks_file=tasks_path, config_file=None)

        assert not result.ok
        joined = "\n".join(result.errors)
        assert "TASK-001" in joined


class TestGroupWithoutModeLineUnderProjectWideVerifyFirstIsLegal:
    """kind: integration — BEH-01/BEH-04 boundary: a project-level
    `execution_mode: verify_first` default legitimises a task that declares
    a group without its own `**Mode:**` line — this must NOT be treated as
    "declared group on a non-verify-first task"."""

    def test_no_error_is_raised_for_this_task(self, tmp_path):
        tasks_path = _write(
            tmp_path,
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Verifies:** tests/test_a.py::test_x\n"
            "Est: 1d\n",
        )
        config_path = tmp_path / "spec-runner.config.yaml"
        config_path.write_text("execution_mode: verify_first\n")

        result = validate_all(tasks_file=tasks_path, config_file=config_path)

        joined = "\n".join(result.errors)
        assert "TASK-001" not in joined


class TestAValidDeclarationPassesCleanly:
    """kind: integration — sanity: a well-formed verify-first declaration is
    not itself a validation error."""

    def test_two_valid_node_id_selectors_produce_no_error(self, tmp_path):
        tasks_path = _write(
            tmp_path,
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_a.py::test_x, tests/test_b.py::test_y\n"
            "Est: 1d\n",
        )

        result = validate_all(tasks_file=tasks_path, config_file=None)

        assert result.ok


class TestRefusalNeverEndsInATraceback:
    """kind: integration — BEH-04/NFR-03: every defect above is a named
    ValidationResult error, never an uncaught exception — and the rest of
    the file's tasks still validate independently.

    #367/DT-04 (BEH-09): a bare missing file path (`tests/test_x.py`) is no
    longer a defective declaration — it warns instead of refusing — so it can
    no longer stand as *this* test's "broken" task. A glob pattern keeps the
    scenario a genuine `validate` error under the new declared-group-element
    vocabulary (`glob_pattern`, one of BEH-03's defective forms) while
    preserving the property under test: one broken task's error does not
    swallow, or get swallowed by, its neighbour's clean result."""

    def test_a_defective_task_does_not_take_the_whole_file_down(self, tmp_path):
        tasks_path = _write(
            tmp_path,
            "### TASK-001: broken\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_*.py\n"
            "Est: 1d\n"
            "### TASK-002: fine\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_a.py::test_x\n"
            "Est: 1d\n",
        )

        result = validate_all(tasks_file=tasks_path, config_file=None)  # must not raise

        joined = "\n".join(result.errors)
        assert "TASK-001" in joined
        assert "TASK-002" not in joined


class TestFileTargetExistenceIsJudgedAgainstProjectRootNotCwd:
    """sr397 review, minor finding: the new file-existence check
    (`_validate_verify_first_declarations` via `parse_group_element`) must
    resolve declared paths against `project_root` — the same tree `run`/
    `watch` operate on (CLI `--project-root`-aware) — not against whatever
    directory the calling process happens to have as cwd. Proven directly:
    the SAME declaration gets a different verdict depending on which root
    `validate_all` is told to judge it against."""

    def test_a_file_present_under_project_root_gets_no_warning(self, tmp_path):
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_present.py").write_text("def test_it():\n    assert True\n")
        tasks_path = _write(
            tmp_path,
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_present.py\n"
            "Est: 1d\n",
        )

        # Judged against tmp_path, where the file genuinely exists: no
        # file-existence warning (unrelated task-field warnings, e.g. a
        # missing traceability reference, are not this test's concern).
        present = validate_all(tasks_file=tasks_path, config_file=None, project_root=tmp_path)
        assert present.ok
        assert not any("tests/test_present.py" in w for w in present.warnings), present.warnings

        # The exact same declaration, judged with no project_root override —
        # falls back to cwd (the repo root under pytest), where this file
        # does not exist — must still warn: proof the first call's silence
        # came from actually resolving against tmp_path, not from the check
        # being a no-op.
        without_root = validate_all(tasks_file=tasks_path, config_file=None)
        assert without_root.ok
        joined = "\n".join(without_root.warnings)
        assert "tests/test_present.py" in joined
