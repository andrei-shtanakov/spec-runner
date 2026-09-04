"""RED test for BEH-12 (spec-runner#341, TASK-009).

BEH-12 (integration): two workstreams with different namespaces, each
running its own TASK-001 in `tdd` mode, must get non-overlapping
evidential file paths — the namespace segment (BEH-13/FR-10) is what
keeps them apart. Today `evidential_file` takes only a task-id; nothing
distinguishes two workstreams sharing a task-id in the same checkout, so
their RED passes would name the same file.

Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-12
`checked_by`: kind=integration, owner=qa, target=tests/test_ws_scoped_red_names.py
"""

from spec_runner.tdd_runners import PytestAdapter


class TestWorkstreamScopedEvidentialFileNames:
    """BEH-12: two workstreams' RED passes for the same task-id must not
    collide on the same evidential file."""

    def test_two_namespaces_get_non_overlapping_evidential_paths(self):
        adapter = PytestAdapter()

        path_alpha = adapter.evidential_file("TASK-001", namespace="ws-alpha")
        path_beta = adapter.evidential_file("TASK-001", namespace="ws-beta")

        assert path_alpha != path_beta, (
            "two workstreams with different namespaces produced the same "
            f"evidential file path for the same task-id: {path_alpha!r}"
        )
