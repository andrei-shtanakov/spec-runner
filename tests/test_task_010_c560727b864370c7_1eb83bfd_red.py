"""RED test for BEH-14 (spec-runner#341, TASK-010).

BEH-14 (contract): for any registered runner adapter and any task-id, the
path `evidential_file` names must be one `is_discoverable` of the *same*
adapter accepts. Today `PytestAdapter.evidential_file` builds its slug from
`task_id` with only `-` folded to `_` — a task-id containing `/` (e.g. one
carried over from an external tracker's `owner/repo#N`-shaped id) survives
into the path unescaped and splits it into extra path segments, so the
resulting file's *name* no longer starts with `test_` and pytest's own
discovery would not collect it.

Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-14
`checked_by`: kind=contract, owner=qa, target=tests/test_tdd_runners.py
"""

from spec_runner.tdd_runners import ADAPTERS


class TestEvidentialFileIsDiscoverableByTheSameAdapter:
    """BEH-14: the named path passes discovery of the same adapter, for
    every adapter in the registry — not only the default one."""

    def test_every_adapter_discovers_its_own_evidential_file(self):
        task_id = "TASK/001"
        namespace = "ws-alpha"

        failures = []
        for name, adapter in ADAPTERS.items():
            path = adapter.evidential_file(task_id, namespace=namespace)
            if not adapter.is_discoverable(path):
                failures.append((name, path))

        assert not failures, (
            "evidential_file produced a path its own adapter's "
            f"is_discoverable rejects: {failures!r}"
        )
