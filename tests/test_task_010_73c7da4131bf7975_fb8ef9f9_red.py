"""BEH-02: a measured sample of the workspace's file `checked_by` targets is
declared and accepted without manual expansion into node ids.

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-02
        workstreams/verify-first-file-scope-group-targets-20260908/spec/30-decomposition.md#DT-10
Traces: FR-01

DT-10 owns `tests/test_verify_file_target_declaration.py`, which does not
exist yet in this tree, and neither does the fixture BEH-02 stands on: a
sample of the 145 file-form `checked_by` targets measured across the
workspace's own bundles on 2026-09-08 (190 `checked_by` lines total, 45 of
them carrying `::` and therefore already node ids). BEH-02 requires that
sample committed to *this* repository — DT-02 says as much: it is "not read
from neighbouring repositories on the fly". Nothing under `tests/fixtures/`
holds it yet, so this test names the fixture directly and fails on its
absence first — an assertion about the missing artefact, not an import
error or a collection error.
"""

from pathlib import Path

from spec_runner.tdd_runners import PytestAdapter, Selector, SelectorRefusal

FIXTURE = Path(__file__).parent / "fixtures" / "verify_file_target_checked_by_sample.txt"


class TestMeasuredWorkspaceSampleDeclaresWithoutManualExpansion:
    """kind: e2e — BEH-02."""

    def test_committed_sample_of_file_targets_is_declared_without_expansion(self):
        assert FIXTURE.exists(), (
            "BEH-02's measured sample (145 file `checked_by` targets drawn "
            "from the workspace's own bundles, measured 2026-09-08) is not "
            "committed to this repository yet — DT-10 owns adding it under "
            f"{FIXTURE}"
        )

        lines = [line.strip() for line in FIXTURE.read_text().splitlines() if line.strip()]
        assert len(lines) == 145, (
            f"expected the measured sample of 145 file targets, found {len(lines)}"
        )

        adapter = PytestAdapter()
        root = Path(__file__).resolve().parents[1]

        accepted: list[str] = []
        refused_by_name: list[tuple[str, str, str]] = []
        for target in lines:
            result = adapter.parse_group_element(target, root)
            if isinstance(result, Selector):
                accepted.append(target)
            else:
                assert isinstance(result, SelectorRefusal)
                refused_by_name.append((target, result.code, result.message))

        # BEH-02: a target that stays unrepresentable is named with a reason
        # — never silently dropped and never lumped into "прочее" — but the
        # class as a whole is declared without a manual expansion step into
        # node ids first.
        assert accepted, (
            "expected at least some of the measured sample to be accepted "
            "as file targets without expansion; every one refused: "
            f"{refused_by_name[:5]}"
        )
        for target, code, message in refused_by_name:
            assert target in message, (
                f"refusal for {target!r} must name the rejected value by "
                f"itself, got code={code!r} message={message!r}"
            )
