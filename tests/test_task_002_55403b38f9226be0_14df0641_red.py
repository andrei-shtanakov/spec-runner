"""RED for BEH-02: Объявленная группа доходит до исполнения в объявленном
порядке и без интерпретации.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-02

Given a task declares
`**Verifies:** tests/test_b.py::test_y, tests/test_a.py::test_x`, the group
must be parsed as two selectors in exactly the declared order — not sorted,
not deduplicated, not otherwise interpreted — the single-line form standing
alongside `**Mode:**`, `**Traces to:**`, `**Depends on:**`, `**Blocks:**` in
the same one-line metadata parse.

Today `Task` carries no `verifies` field at all and `parse_tasks` never reads
a `**Verifies:**` line, so the declared group is silently dropped on the
floor instead of reaching parsing.
"""


from spec_runner.task import parse_tasks


class TestDeclaredGroupReachesParsingInDeclaredOrder:
    def test_verifies_selectors_are_parsed_in_declared_order(self, tmp_path):
        path = tmp_path / "tasks.md"
        path.write_text(
            "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\n"
            "**Mode:** verify_first\n"
            "**Verifies:** tests/test_b.py::test_y, tests/test_a.py::test_x\n"
            "Est: 1d\n"
        )

        tasks = parse_tasks(path)

        assert tasks[0].verifies == [
            "tests/test_b.py::test_y",
            "tests/test_a.py::test_x",
        ]
