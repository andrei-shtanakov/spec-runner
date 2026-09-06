"""BEH-02 and BEH-03: the declared `**Verifies:**` group reaches parsing in
declared order and without interpretation, and is never inferred from
anything else.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-02
        workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-03
Traces: FR-02, FR-03

Whether an adapter-rejected selector is refused, or a verify-first task with
no declared group is refused, is BEH-04/BEH-05's contract (TASK-003), not
this one's — this slice only covers the declaration reaching `Task.verifies`
unchanged (or staying empty when absent). The comma-form's own refusal on an
unclosed `[...]` (below) is FR-02's contract directly: the comma split
itself would otherwise manufacture selectors the operator never wrote,
which BEH-02's "without interpretation" rules out — it is not deferred to
TASK-003 like an adapter-level rejection is.
"""

import pytest

from spec_runner.task import parse_tasks


class TestDeclaredGroupReachesParsingInDeclaredOrder:
    """kind: contract — BEH-02."""

    def test_single_line_comma_form_preserves_declared_order(self, tmp_path):
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_a.py::test_x, tests/test_b.py::test_y\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)

        assert tasks[0].verifies == [
            "tests/test_a.py::test_x",
            "tests/test_b.py::test_y",
        ]

    def test_multiline_block_form_preserves_order_and_is_kept_out_of_description(self, tmp_path):
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:**\n"
            "- tests/test_b.py::test_y\n"
            "- tests/test_a.py::test_x\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)

        assert tasks[0].verifies == [
            "tests/test_b.py::test_y",
            "tests/test_a.py::test_x",
        ]
        assert "tests/test_b.py::test_y" not in tasks[0].description
        assert "tests/test_a.py::test_x" not in tasks[0].description

    def test_blank_line_inside_multiline_block_does_not_close_it(self, tmp_path):
        """Symmetric with `**Checklist:**` (task.py's `in_checklist` survives
        a blank line): a blank line between the marker and its selectors, or
        between two selector lines, must not end the block — otherwise the
        remaining selectors leak into the description (#372 minor #2)."""
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:**\n"
            "\n"
            "- tests/test_b.py::test_y\n"
            "\n"
            "- tests/test_a.py::test_x\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)

        assert tasks[0].verifies == [
            "tests/test_b.py::test_y",
            "tests/test_a.py::test_x",
        ]
        assert "tests/test_b.py::test_y" not in tasks[0].description
        assert "tests/test_a.py::test_x" not in tasks[0].description

    def test_a_selector_the_adapter_would_refuse_is_stored_verbatim(self, tmp_path):
        """An unparseable value is kept exactly as written, not mapped to
        something plausible — the same rule `**Mode:**` already holds."""
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_x.py\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)

        assert tasks[0].verifies == ["tests/test_x.py"]

    def test_single_line_form_stands_alongside_the_other_metadata_fields(self, tmp_path):
        """`**Verifies:**` is read by the same one-line metadata parse as
        `**Mode:**`, `**Traces to:**`, `**Depends on:**`, `**Blocks:**`."""
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-002: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_a.py::test_x\n"
            "**Traces to:** [FR-02]\n"
            "**Depends on:** [TASK-001]\n"
            "**Blocks:** —\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)
        task = tasks[0]

        assert task.verifies == ["tests/test_a.py::test_x"]
        assert task.execution_mode == "verify_first"
        assert task.traces_to == ["FR-02"]
        assert task.depends_on == ["TASK-001"]


class TestGroupIsNeverInferred:
    """kind: integration — BEH-03."""

    def test_declared_group_wins_over_every_hint(self, tmp_path):
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_a.py::test_x\n"
            "Looks like it touches tests/test_b.py and tests/test_c.py too.\n"
            "**Traces to:** [FR-02]\n"
            "**Checklist:**\n"
            "- [ ] проверка группы: tests/test_d.py (kind: integration) зелёные\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)
        task = tasks[0]

        assert task.verifies == ["tests/test_a.py::test_x"]
        assert task.traces_to == ["FR-02"]

    def test_no_verifies_line_means_no_group_is_guessed(self, tmp_path):
        """Without a `**Verifies:**` line the group stays empty — it is not
        derived from `Traces to`, filenames, the diff, or checklist prose.
        Whether this is refused before run is BEH-04's contract (TASK-003)."""
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "Touches tests/test_a.py and tests/test_b.py.\n"
            "**Traces to:** [FR-02]\n"
            "**Checklist:**\n"
            "- [ ] проверка группы: tests/test_c.py (kind: integration) зелёные\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)

        assert tasks[0].verifies is None

    def test_bare_marker_with_no_selectors_is_distinguishable_from_no_line_at_all(self, tmp_path):
        """BEH-04 (TASK-003) must name "verify-first without a `**Verifies:**`
        line" and "empty declared group" as separate defects (#372 minor #3);
        that requires the model to keep them apart in the first place."""
        bare_marker = tmp_path / "bare_marker.md"
        bare_marker.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:**\n"
            "Est: 1d\n"
        )
        no_line = tmp_path / "no_line.md"
        no_line.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n**Mode:** verify_first\nEst: 1d\n"
        )

        assert parse_tasks(bare_marker)[0].verifies == []
        assert parse_tasks(no_line)[0].verifies is None


class TestCommaFormRefusesOnUnclosedBracketBeforeComma:
    """kind: contract — FR-02: a pytest node id with a comma in its own
    parametrize suffix (`test_y[a,b]`) can only be declared with the
    multi-line block form. The comma-form parse must refuse such a line
    instead of splitting it into fragments the operator never wrote — this
    is FR-02's own contract, not an adapter-level rejection deferred to
    TASK-003 (see module docstring)."""

    def test_refuses_instead_of_splitting_inside_the_brackets(self, tmp_path):
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_a.py::test_y[a,b]\n"
            "Est: 1d\n"
        )

        with pytest.raises(ValueError) as exc_info:
            parse_tasks(path)

        message = str(exc_info.value)
        assert "TASK-001" in message
        # The refusal quotes the original declared line whole — never a
        # fragment the operator did not write (BEH-05's rule, held early).
        assert "tests/test_a.py::test_y[a,b]" in message
        assert "test_y[a" not in message.replace("test_y[a,b]", "")

    def test_balanced_brackets_per_item_still_split_normally(self, tmp_path):
        """A comma that separates two *already-closed* bracketed selectors
        is unambiguous and must keep working via the comma form."""
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_a.py::test_x[a], tests/test_b.py::test_y[b]\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)

        assert tasks[0].verifies == [
            "tests/test_a.py::test_x[a]",
            "tests/test_b.py::test_y[b]",
        ]
