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
    adapter will not accept, mixed in with a selector it does accept."""

    def test_only_the_bad_selector_in_a_mixed_group_is_named(self, tmp_path):
        tasks_path = _write(
            tmp_path,
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_a.py::test_x, tests/test_b.py\n"
            "Est: 1d\n",
        )

        result = validate_all(tasks_file=tasks_path, config_file=None)

        assert not result.ok
        joined = "\n".join(result.errors)
        assert "TASK-001" in joined
        assert "verify_first" in joined
        # BEH-05: the offending value is quoted verbatim …
        assert "tests/test_b.py" in joined
        # … and the message reproduces the adapter's own wording and names
        # the expected form, not just the fact of a refusal.
        assert "not a node id" in joined
        assert "path::test" in joined
        # The accepted selector in the same group is not itself flagged as
        # the culprit — only one selector in this group is unusable, and it
        # is named specifically, not the whole group.
        bad_selector_errors = [e for e in result.errors if "refused by the" in e]
        assert len(bad_selector_errors) == 1
        assert "selector 'tests/test_b.py'" in bad_selector_errors[0]

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
    the file's tasks still validate independently."""

    def test_a_defective_task_does_not_take_the_whole_file_down(self, tmp_path):
        tasks_path = _write(
            tmp_path,
            "### TASK-001: broken\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_x.py\n"
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
