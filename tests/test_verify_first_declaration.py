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

    def test_loose_list_with_blank_lines_between_items_keeps_every_selector_in_order(
        self, tmp_path
    ):
        """A blank line between two list items — a "loose list" in markdown
        terms — is legal markdown and renders identically to a tight list in
        every renderer. Blank lines inside the block are fully transparent
        REGARDLESS of position (#372 round 5, correcting round 4): leading
        (round 1's idiomatic-markdown case) and between items alike. Round
        4 closed the block on a blank line once an item had been read,
        which silently truncated exactly this loose-list shape to its first
        selector — the same silent-loss class this whole feature exists to
        close."""
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:**\n"
            "\n"
            "- tests/test_a.py::test_x\n"
            "\n"
            "- tests/test_b.py::test_y\n"
            "\n"
            "- tests/test_c.py::test_z\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)

        assert tasks[0].verifies == [
            "tests/test_a.py::test_x",
            "tests/test_b.py::test_y",
            "tests/test_c.py::test_z",
        ]
        assert "tests/test_a.py::test_x" not in tasks[0].description
        assert "tests/test_b.py::test_y" not in tasks[0].description
        assert "tests/test_c.py::test_z" not in tasks[0].description

    def test_indented_bulleted_list_is_accepted_verbatim(self, tmp_path):
        """A block item is recognized POSITIONALLY (any bullet), never by
        content, and that includes tolerating indentation — the same
        allowance `TASK_META` already gives its own bullet prefix
        (`[ \\t]*[-*]\\s+`, #123: agents editing tasks.md mid-run introduce
        it). Round 5: an indented block list previously matched nothing,
        so the whole declared group silently vanished (`verifies == []`,
        indistinguishable from a deliberately empty group)."""
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:**\n"
            "  - tests/test_a.py::test_x\n"
            "  - tests/test_b.py::test_y\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)

        assert tasks[0].verifies == [
            "tests/test_a.py::test_x",
            "tests/test_b.py::test_y",
        ]

    def test_prose_bullet_after_a_blank_line_is_consumed_verbatim_declared_by_position(
        self, tmp_path
    ):
        """A block item is declared by POSITION (any bullet between the
        marker and the nearest structural boundary), and judged later by
        the adapter — not filtered here by content (#372 round 5 corrects
        round 4's assumption that a blank line closes the block: it
        doesn't, blank lines are fully transparent, see the loose-list test
        above). A bulleted line that reads as prose is therefore captured
        the same as any other declared selector, to be refused later with
        a verbatim quote (BEH-02/BEH-05) — round 4 already sanctioned this
        for a prose bullet with no blank line in front of it; round 5
        extends it to one that does."""
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

        assert task.verifies == [
            "tests/test_a.py::test_x",
            "перепроверить после мержа WS-341",
        ]

    def test_prose_paragraph_closes_the_block_and_the_bullet_after_it_is_not_reopened(
        self, tmp_path
    ):
        """Unlike a bulleted line (previous test), an ordinary prose
        PARAGRAPH — a non-blank line with no leading bullet — is the actual
        structural boundary: it closes the block and stays in `description`
        like any other body text. Once closed, the block does not reopen
        (round 4's rule, still true here): a bullet appearing after the
        closing paragraph lands in `description` too, deliberately — the
        boundary is structural, not a search for more selectors further
        down (#372 round 5)."""
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:**\n"
            "- tests/test_a.py::test_x\n"
            "Some prose paragraph, not a bullet at all.\n"
            "- tests/test_b.py::test_y\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)
        task = tasks[0]

        assert task.verifies == ["tests/test_a.py::test_x"]
        assert "Some prose paragraph, not a bullet at all." in task.description
        assert "tests/test_b.py::test_y" in task.description

    def test_legal_node_id_with_comma_and_space_in_parametrize_suffix_is_kept(self, tmp_path):
        """FR-02's escape hatch for a node id with a comma (`test_y[a,b]`) is
        the block form (the comma-form refuses it, see
        TestCommaFormRefusesOnUnclosedBracketBeforeComma) — but pytest can
        also produce a parametrize suffix with a comma AND a space
        (`test_y[a, b]`), which round 2's whitespace-free `\\S+` rejected
        too, silently truncating the group (#372 round 3). The block form
        accepts any bullet in the contiguous run verbatim (round 4), so
        embedded whitespace is irrelevant, and every selector after it in
        the same block survives too."""
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

    def test_prose_bullet_inside_a_contiguous_run_is_stored_verbatim_not_filtered(self, tmp_path):
        """#372 round 4: rounds 2 and 3 each tried to detect "this bullet is
        prose, not a selector" by shape (no whitespace, then requiring
        `::`), and each silently dropped a legal selector from some other
        input as a result. The settled design: ANY bullet in the contiguous
        run under the marker is a declared selector, stored verbatim, in
        order — including one that reads as prose. This is deliberate, not
        a defect: BEH-02 stores the value exactly as written, and
        BEH-04/BEH-05 refuse it later, quoting it back to the operator —
        the same contract `**Mode:**` already holds for an unrecognized
        value. Only a blank line, a checklist item, a `**...**` field, or
        the `Est:`/priority-status line closes the block — never a
        bullet's shape."""
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

        assert tasks[0].verifies == [
            "tests/test_a.py::test_x",
            "перепроверить после мержа WS-341",
            "tests/test_b.py::test_y",
        ]

    def test_exunit_style_selector_is_stored_verbatim_in_the_block_form(self, tmp_path):
        """FR-02: a selector lives 'in the vocabulary the project's adapter
        accepts' — ExUnitAdapter.parse_selector wants `path:LINE` and
        refuses anything containing `::`. Round 3's `::`-shaped heuristic
        made every legal ExUnit selector undeclarable in the block form
        (#372 round 4). The block form must accept any registered adapter's
        vocabulary, unjudged, same as the single-line form already does."""
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:**\n"
            "- test/foo_test.exs:12\n"
            "- test/bar_test.exs:20\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)

        assert tasks[0].verifies == [
            "test/foo_test.exs:12",
            "test/bar_test.exs:20",
        ]

    def test_adapter_refusable_selector_is_stored_verbatim_in_the_block_form_too(self, tmp_path):
        """Symmetry with `test_a_selector_the_adapter_would_refuse_is_stored_verbatim`
        (single-line form, below): the exact same file-target-without-`::`
        value must be kept verbatim when declared via the block form
        instead — round 3's `::` requirement rejected precisely this value
        in the block form only, so the two declared forms disagreed on
        identical input (#372 round 4)."""
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:**\n"
            "- tests/test_x.py\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)

        assert tasks[0].verifies == ["tests/test_x.py"]

    def test_block_form_and_single_line_form_agree_on_the_same_declared_group(self, tmp_path):
        """#372 round 4: the two declared forms must not disagree on what
        counts as a selector — the block form accepts exactly what the
        single-line comma form already accepts verbatim (when the comma
        form itself isn't the ambiguous bracket-comma case, see
        TestCommaFormRefusesOnUnclosedBracketBeforeComma)."""
        single_line = tmp_path / "single_line.md"
        single_line.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_a.py::test_x, tests/test_x.py, test/foo_test.exs:12\n"
            "Est: 1d\n"
        )
        block = tmp_path / "block.md"
        block.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:**\n"
            "- tests/test_a.py::test_x\n"
            "- tests/test_x.py\n"
            "- test/foo_test.exs:12\n"
            "Est: 1d\n"
        )

        assert parse_tasks(single_line)[0].verifies == parse_tasks(block)[0].verifies

    def test_em_dash_means_no_group_like_depends_on_and_blocks(self, tmp_path):
        """`**Verifies:** —` follows the same "— = nothing" convention as
        the neighboring `**Depends on:**`/`**Blocks:**` fields in the same
        metadata row (and the generator template itself emits
        `**Blocks:** —`) — it must not be read as a single selector
        literally named `—` (#372 round 3, minor)."""
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** —\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)

        assert tasks[0].verifies == []

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
