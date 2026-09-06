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

    def test_blank_line_then_prose_bullet_closes_the_block_instead_of_swallowing_it(self, tmp_path):
        """A blank line surviving inside the block (above) must not also let
        a following prose bullet be mistaken for a selector. A block item is
        recognized by looking like a pytest target (contains `::`, #372
        round 3) — not by absence of whitespace (round 2's `\\S+`, which
        round 3 found rejected legal node ids with a space in their
        parametrize suffix); a bullet with no `::` closes the block and
        falls through to description, same as before."""
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:**\n"
            "- tests/test_a.py::test_x\n"
            "\n"
            "- перепроверить после мержа WS-341\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)
        task = tasks[0]

        assert task.verifies == ["tests/test_a.py::test_x"]
        assert "перепроверить после мержа WS-341" in task.description

    def test_legal_node_id_with_comma_and_space_in_parametrize_suffix_is_kept(self, tmp_path):
        """FR-02's escape hatch for a node id with a comma (`test_y[a,b]`) is
        the block form (the comma-form refuses it, see
        TestCommaFormRefusesOnUnclosedBracketBeforeComma) — but pytest can
        also produce a parametrize suffix with a comma AND a space
        (`test_y[a, b]`), which round 2's whitespace-free `\\S+` rejected
        too, silently truncating the group (#372 round 3, major). The block
        form must accept any selector shaped like a pytest target regardless
        of embedded whitespace, and every selector after it in the same
        contiguous block must survive too."""
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:**\n"
            "- tests/test_a.py::test_x\n"
            "- tests/test_b.py::test_y[a, b]\n"
            "- tests/test_c.py::test_z\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)

        assert tasks[0].verifies == [
            "tests/test_a.py::test_x",
            "tests/test_b.py::test_y[a, b]",
            "tests/test_c.py::test_z",
        ]
        assert "tests/test_b.py::test_y[a, b]" not in tasks[0].description
        assert "tests/test_c.py::test_z" not in tasks[0].description

    def test_selector_shaped_bullet_after_the_block_closes_is_not_reopened_into_it(self, tmp_path):
        """Deliberate choice (#372 round 3): once a non-selector bullet
        closes the block, a LATER bullet that happens to look like a pytest
        target is not folded back into the group — BEH-03 forbids inferring
        the declared group from anything outside the one contiguous block
        under the marker. It stays ordinary description text, exactly like
        the prose bullet that closed the block ahead of it."""
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:**\n"
            "- tests/test_a.py::test_x\n"
            "- перепроверить после мержа WS-341\n"
            "- tests/test_b.py::test_y\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)
        task = tasks[0]

        assert task.verifies == ["tests/test_a.py::test_x"]
        assert "перепроверить после мержа WS-341" in task.description
        assert "tests/test_b.py::test_y" in task.description

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
    multi-line block form. The comma-form parse marks the declaration
    unparseable instead of splitting it into fragments the operator never
    wrote — this is FR-02's own contract, not an adapter-level rejection
    deferred to TASK-003 (see module docstring). The refusal is localized
    to the task (NFR-03/BEH-04: no traceback, the rest of the file still
    parses) — round 2 of #372's review found the first cut (a bare
    `raise ValueError` out of `parse_tasks`) crashed every command that
    reads tasks.md over one bad line in one task; `validate_tasks`
    surfaces the named, quoted error instead (see test_validate.py)."""

    def test_marks_the_task_unparseable_without_raising(self, tmp_path):
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_a.py::test_y[a,b]\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)  # must not raise

        task = tasks[0]
        assert task.verifies is None
        assert task.verifies_error is not None
        # The message quotes the original declared line whole — never a
        # fragment the operator did not write (BEH-05's rule, held early).
        assert "tests/test_a.py::test_y[a,b]" in task.verifies_error
        assert "test_y[a" not in task.verifies_error.replace("test_y[a,b]", "")

    def test_other_tasks_in_the_same_file_still_parse(self, tmp_path):
        """One malformed **Verifies:** line must not take the whole file
        down — every command that reads tasks.md (status/run/plan/tui)
        needs the rest of the tasks readable."""
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: broken\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_a.py::test_y[a,b]\n"
            "Est: 1d\n"
            "### TASK-002: fine\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_b.py::test_z\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)

        assert len(tasks) == 2
        assert tasks[0].verifies_error is not None
        assert tasks[1].verifies == ["tests/test_b.py::test_z"]
        assert tasks[1].verifies_error is None

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
