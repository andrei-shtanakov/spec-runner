"""M4: generalize the stage graph from a linear chain to a true DAG.

The spec-generation profile's `requires`/`upstream` edges already exist on
`StageDef`, but `downstream_stages` / `resolve_next_stage` / `mark_downstream_stale`
treated a profile as a flat ordered list (downstream = list slice). For a
non-linear profile that mis-stales *sibling* stages. These tests pin the
graph-aware behavior while proving the built-in linear `lite` profile is
byte-for-byte unchanged.
"""

import pytest

from spec_runner.spec import (
    LITE,
    SpecMeta,
    StageDef,
    StageProfile,
    downstream_stages,
    resolve_next_stage,
    stage_readiness,
    validate_profile_graph,
)

# proposal ──► specs  ──┐
#          └─► design ──┴─► tasks   (specs ∥ design after proposal)
DAG = StageProfile(
    name="dag",
    stages=(
        StageDef("proposal", "t", "P", "proposal", upstream=()),
        StageDef("specs", "t", "S", "specs", upstream=("proposal",)),
        StageDef("design", "t", "D", "design", upstream=("proposal",)),
        StageDef("tasks", "t", "T", "tasks", upstream=("specs", "design")),
    ),
)


def _m(stage: str, status: str) -> SpecMeta:
    return SpecMeta(spec_stage=stage, status=status)


class TestDownstreamGraph:
    def test_transitive_successors(self):
        assert downstream_stages("proposal", DAG) == ["specs", "design", "tasks"]

    def test_sibling_not_downstream(self):
        # design depends on proposal, NOT on specs — approving specs must not
        # stale design. A linear list-slice would wrongly include it.
        assert downstream_stages("specs", DAG) == ["tasks"]
        assert downstream_stages("design", DAG) == ["tasks"]

    def test_leaf_has_no_downstream(self):
        assert downstream_stages("tasks", DAG) == []

    def test_legacy_name_list_stays_linear(self):
        # A plain Sequence[str] keeps the historical list-slice behavior.
        seq = ("proposal", "specs", "design", "tasks")
        assert downstream_stages("specs", seq) == ["design", "tasks"]


class TestLiteEquivalence:
    def test_downstream_matches_linear(self):
        for s in LITE.names():
            assert downstream_stages(s, LITE) == downstream_stages(s, LITE.names())

    def test_resolve_matches_linear_across_states(self):
        names = LITE.names()
        states = [None, "draft", "approved", "stale"]
        # Exhaustively compare graph vs linear resolution for lite.
        import itertools

        for combo in itertools.product(states, repeat=len(names)):
            metas = {
                n: (None if st is None else _m(n, st)) for n, st in zip(names, combo, strict=True)
            }
            assert resolve_next_stage(metas, LITE) == resolve_next_stage(metas, names)


class TestResolveDag:
    def test_parallel_stage_generated_in_order(self):
        metas = {
            "proposal": _m("proposal", "approved"),
            "specs": None,
            "design": None,
            "tasks": None,
        }
        assert resolve_next_stage(metas, DAG) == ("generate", "specs")

    def test_join_stage_blocked_until_all_deps_approved(self):
        # specs approved, design still missing → tasks must not be generated.
        metas = {
            "proposal": _m("proposal", "approved"),
            "specs": _m("specs", "approved"),
            "design": None,
            "tasks": None,
        }
        assert resolve_next_stage(metas, DAG) == ("generate", "design")

    def test_all_approved_is_done(self):
        metas = {s: _m(s, "approved") for s in DAG.names()}
        assert resolve_next_stage(metas, DAG) == ("done", "tasks")

    def test_draft_awaits_approval(self):
        metas = {"proposal": _m("proposal", "draft"), "specs": None, "design": None, "tasks": None}
        assert resolve_next_stage(metas, DAG) == ("await_approval", "proposal")

    def test_stale_takes_priority(self):
        metas = {
            "proposal": _m("proposal", "approved"),
            "specs": _m("specs", "stale"),
            "design": _m("design", "approved"),
            "tasks": _m("tasks", "approved"),
        }
        assert resolve_next_stage(metas, DAG) == ("stale", "specs")


class TestReadiness:
    def test_ready_blocked_missing_deps(self):
        metas = {
            "proposal": _m("proposal", "approved"),
            "specs": None,
            "design": None,
            "tasks": None,
        }
        r = stage_readiness(metas, DAG)
        assert r["proposal"]["state"] == "done"
        assert r["specs"]["state"] == "ready"
        assert r["design"]["state"] == "ready"
        assert r["tasks"]["state"] == "blocked"
        assert set(r["tasks"]["missing_deps"]) == {"specs", "design"}

    def test_partial_deps_still_blocked(self):
        metas = {
            "proposal": _m("proposal", "approved"),
            "specs": _m("specs", "approved"),
            "design": None,
            "tasks": None,
        }
        r = stage_readiness(metas, DAG)
        assert r["design"]["state"] == "ready"
        assert r["tasks"]["state"] == "blocked"
        assert r["tasks"]["missing_deps"] == ["design"]


class TestGraphValidation:
    def test_cycle_rejected(self):
        cyclic = StageProfile(
            name="cyclic",
            stages=(
                StageDef("a", "t", "A", "a", upstream=("b",)),
                StageDef("b", "t", "B", "b", upstream=("a",)),
            ),
        )
        with pytest.raises(ValueError, match="cycle"):
            validate_profile_graph(cyclic)

    def test_unknown_upstream_rejected(self):
        bad = StageProfile(
            name="bad",
            stages=(StageDef("a", "t", "A", "a", upstream=("ghost",)),),
        )
        with pytest.raises(ValueError, match="unknown"):
            validate_profile_graph(bad)

    def test_valid_dag_passes(self):
        validate_profile_graph(DAG)  # no raise
        validate_profile_graph(LITE)

    def test_requires_alias_reads_upstream(self):
        sd = StageDef("tasks", "t", "T", "tasks", upstream=("specs", "design"))
        assert sd.requires == ("specs", "design")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
