"""M3: delta specs + deterministic merge into the source-of-truth requirements.

A change carries only what changes, in ``spec/changes/<id>/specs/requirements.md``:
``## ADDED / MODIFIED / REMOVED / RENAMED Requirements`` sections whose entries
are the same id-keyed blocks M1's parser understands. On archive the delta is
merged into the flat ``spec/requirements.md``. Identity is the REQ/NFR id (not
the header text as in OpenSpec), which makes matching exact:

- ADDED    — id must be new; block appended to the target.
- MODIFIED — id must exist; the whole block is replaced.
- REMOVED  — id must exist; block deleted (Reason + Migration mandatory).
- RENAMED  — id must exist; only the heading name changes (FROM:/TO:).

Conflicts are hard errors naming the requirement id. Design:
docs/plans/2026-07-13-openspec-inspired-roadmap.md (M3).
"""

import pytest

from spec_runner.requirements import parse_delta
from spec_runner.spec_merge import MergeConflictError, apply_merge, plan_merge

TARGET = """# Requirements

## Out of Scope
- none

## 2. Functional

#### REQ-001: Login
**Priority**: P0

**Acceptance Criteria**:
- [ ] user can log in

#### REQ-002: Logout
Old logout behavior.

## 3. Non-Functional

### NFR-001: Performance
| metric | value |
"""

DELTA = """# Delta for requirements

## ADDED Requirements

#### REQ-010: Dark mode
**Priority**: P1

**Acceptance Criteria**:
- [ ] theme toggles

## MODIFIED Requirements

#### REQ-002: Logout
New logout behavior with session revocation.

**Acceptance Criteria**:
- [ ] tokens revoked on logout

## REMOVED Requirements

#### REQ-001: Login
**Reason**: replaced by SSO
**Migration**: use REQ-010 flow

## RENAMED Requirements

- FROM: `### NFR-001: Performance`
- TO: `### NFR-001: Performance & Latency`
"""


class TestParseDelta:
    def test_sections_parsed(self):
        d = parse_delta(DELTA)
        assert [r.id for r in d.added] == ["REQ-010"]
        assert [r.id for r in d.modified] == ["REQ-002"]
        assert [r.id for r in d.removed] == ["REQ-001"]
        assert [(r.req_id, r.old_name, r.new_name) for r in d.renamed] == [
            ("NFR-001", "Performance", "Performance & Latency")
        ]

    def test_removed_carries_reason_and_migration(self):
        (removed,) = parse_delta(DELTA).removed
        assert removed.reason == "replaced by SSO"
        assert removed.migration == "use REQ-010 flow"

    def test_empty_delta(self):
        d = parse_delta("# Nothing here\n")
        assert not d.added and not d.modified and not d.removed and not d.renamed
        assert d.is_empty

    def test_frontmatter_stripped(self):
        d = parse_delta("---\nspec_stage: requirements\n---\n" + DELTA)
        assert [r.id for r in d.added] == ["REQ-010"]


class TestApplyMerge:
    def test_full_delta_applies(self):
        out = apply_merge(TARGET, parse_delta(DELTA))
        # ADDED appended
        assert "#### REQ-010: Dark mode" in out
        # MODIFIED replaced whole block
        assert "session revocation" in out
        assert "Old logout behavior." not in out
        # REMOVED gone
        assert "REQ-001" not in out
        # RENAMED heading rewritten, body kept
        assert "### NFR-001: Performance & Latency" in out
        assert "| metric | value |" in out

    def test_added_appends_at_end(self):
        delta = parse_delta("## ADDED Requirements\n\n#### REQ-010: X\nbody\n")
        out = apply_merge(TARGET, delta)
        assert out.index("REQ-010") > out.index("NFR-001")

    def test_merge_is_deterministic(self):
        d = parse_delta(DELTA)
        assert apply_merge(TARGET, d) == apply_merge(TARGET, d)

    def test_reapplying_same_delta_conflicts(self):
        # Idempotence guard: archiving the same delta twice must conflict,
        # not silently duplicate/re-delete.
        d = parse_delta(DELTA)
        merged = apply_merge(TARGET, d)
        with pytest.raises(MergeConflictError):
            apply_merge(merged, d)

    def test_bootstrap_when_target_missing(self):
        # First archived delta on a project without requirements.md: ADDED
        # bootstraps the file; any other op conflicts.
        delta = parse_delta("## ADDED Requirements\n\n#### REQ-001: X\nbody\n")
        out = apply_merge("", delta)
        assert "#### REQ-001: X" in out


class TestConflicts:
    def test_added_existing_id(self):
        delta = parse_delta("## ADDED Requirements\n\n#### REQ-001: Dup\nbody\n")
        with pytest.raises(MergeConflictError, match="REQ-001"):
            apply_merge(TARGET, delta)

    def test_modified_missing_id(self):
        delta = parse_delta("## MODIFIED Requirements\n\n#### REQ-999: Ghost\nbody\n")
        with pytest.raises(MergeConflictError, match="REQ-999"):
            apply_merge(TARGET, delta)

    def test_removed_missing_id(self):
        delta = parse_delta(
            "## REMOVED Requirements\n\n#### REQ-999: Ghost\n**Reason**: r\n**Migration**: m\n"
        )
        with pytest.raises(MergeConflictError, match="REQ-999"):
            apply_merge(TARGET, delta)

    def test_renamed_missing_id(self):
        delta = parse_delta(
            "## RENAMED Requirements\n\n- FROM: `#### REQ-999: A`\n- TO: `#### REQ-999: B`\n"
        )
        with pytest.raises(MergeConflictError, match="REQ-999"):
            apply_merge(TARGET, delta)

    def test_duplicate_op_on_same_id(self):
        delta = parse_delta(
            "## MODIFIED Requirements\n\n#### REQ-002: A\nx\n"
            "## REMOVED Requirements\n\n#### REQ-002: A\n**Reason**: r\n**Migration**: m\n"
        )
        with pytest.raises(MergeConflictError, match="REQ-002"):
            apply_merge(TARGET, delta)


class TestPlanMerge:
    def test_plan_lists_operations(self):
        plan = plan_merge(TARGET, parse_delta(DELTA))
        assert plan.ok
        joined = "\n".join(plan.operations)
        assert "ADD REQ-010" in joined
        assert "MODIFY REQ-002" in joined
        assert "REMOVE REQ-001" in joined
        assert "RENAME NFR-001" in joined

    def test_plan_reports_conflicts_without_raising(self):
        delta = parse_delta("## ADDED Requirements\n\n#### REQ-001: Dup\nbody\n")
        plan = plan_merge(TARGET, delta)
        assert not plan.ok
        assert any("REQ-001" in c for c in plan.conflicts)

    def test_plan_flags_removed_without_reason(self):
        delta = parse_delta("## REMOVED Requirements\n\n#### REQ-001: Login\nno fields\n")
        plan = plan_merge(TARGET, delta)
        assert not plan.ok
        assert any("Reason" in c or "Migration" in c for c in plan.conflicts)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
