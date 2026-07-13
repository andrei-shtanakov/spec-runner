"""M1: tolerant, id-keyed parsing of requirements docs.

Requirements in the wild use heterogeneous sub-structure (gherkin,
`- [ ]` checklists, or prose). The parser anchors only on the
`#+ (REQ|NFR)-NNN:` heading and block boundaries at the next same-or-higher
level heading, capturing the exact block `raw` (the merge/round-trip unit for
delta specs, M3) plus best-effort optional fields.
"""

from pathlib import Path

import pytest

from spec_runner.requirements import (
    find_requirement,
    parse_requirements,
    serialize_requirement,
)

DOC = """# Requirements

## Out of Scope
- none

## 2. Functional

### 2.1 Group A
#### REQ-001: First thing
**Priority**: P0
**Acceptance Criteria**:
- [ ] does X
- [ ] does Y

**Traces to:** [DESIGN-001], [TASK-005]

---

#### REQ-002: Second thing
Some description referencing [REQ-001].
**Acceptance Criteria**:
GIVEN a WHEN b THEN c

### 2.2 Group B
#### REQ-010: Third thing
No acceptance here.

## 3. Non-Functional
### NFR-001: Performance
| metric | value |
"""


class TestParsing:
    def test_finds_all_requirements_in_order(self):
        reqs = parse_requirements(DOC)
        assert [r.id for r in reqs] == ["REQ-001", "REQ-002", "REQ-010", "NFR-001"]

    def test_names_captured(self):
        reqs = {r.id: r for r in parse_requirements(DOC)}
        assert reqs["REQ-001"].name == "First thing"
        assert reqs["NFR-001"].name == "Performance"

    def test_level_and_kind(self):
        reqs = {r.id: r for r in parse_requirements(DOC)}
        assert reqs["REQ-001"].level == 4
        assert reqs["REQ-001"].kind == "functional"
        assert reqs["NFR-001"].level == 3
        assert reqs["NFR-001"].kind == "non-functional"

    def test_priority_best_effort(self):
        reqs = {r.id: r for r in parse_requirements(DOC)}
        assert reqs["REQ-001"].priority == "P0"
        assert reqs["REQ-002"].priority == ""

    def test_acceptance_criteria_best_effort(self):
        reqs = {r.id: r for r in parse_requirements(DOC)}
        assert "does X" in reqs["REQ-001"].acceptance_criteria
        assert "GIVEN a WHEN b THEN c" in reqs["REQ-002"].acceptance_criteria
        assert reqs["REQ-010"].acceptance_criteria == ""

    def test_traces_exclude_self(self):
        reqs = {r.id: r for r in parse_requirements(DOC)}
        assert "DESIGN-001" in reqs["REQ-001"].traces_to
        assert "TASK-005" in reqs["REQ-001"].traces_to
        assert "REQ-001" not in reqs["REQ-001"].traces_to
        # REQ-002 references REQ-001 (another requirement) → kept.
        assert "REQ-001" in reqs["REQ-002"].traces_to

    def test_block_boundaries(self):
        reqs = {r.id: r for r in parse_requirements(DOC)}
        # REQ-001 ends before the next same-level heading (#### REQ-002).
        assert "REQ-002" not in reqs["REQ-001"].raw
        # REQ-002 ends at the higher-level "### 2.2 Group B".
        assert "Group B" not in reqs["REQ-002"].raw
        # REQ-010 ends at "## 3. Non-Functional".
        assert "Non-Functional" not in reqs["REQ-010"].raw


class TestRoundTrip:
    def test_serialize_returns_exact_block(self):
        reqs = parse_requirements(DOC)
        for r in reqs:
            assert serialize_requirement(r) == r.raw
            assert r.raw in DOC

    def test_reparse_of_block_is_idempotent(self):
        for r in parse_requirements(DOC):
            reparsed = parse_requirements(r.raw)
            assert len(reparsed) == 1
            assert reparsed[0].id == r.id
            assert reparsed[0].raw == r.raw

    def test_blocks_appear_in_document_order(self):
        idxs = [DOC.index(r.raw) for r in parse_requirements(DOC)]
        assert idxs == sorted(idxs)


class TestHelpers:
    def test_find_requirement(self):
        reqs = parse_requirements(DOC)
        assert find_requirement(reqs, "REQ-010").name == "Third thing"
        assert find_requirement(reqs, "REQ-999") is None

    def test_frontmatter_stripped(self):
        doc = "---\nspec_stage: requirements\n---\n" + DOC
        reqs = parse_requirements(doc)
        assert [r.id for r in reqs] == ["REQ-001", "REQ-002", "REQ-010", "NFR-001"]

    def test_empty_and_no_requirements(self):
        assert parse_requirements("") == []
        assert parse_requirements("# Just a title\n\nProse only.\n") == []


class TestRealFile:
    def test_repo_requirements_parse_and_roundtrip(self):
        path = Path(__file__).resolve().parents[1] / "spec" / "requirements.md"
        if not path.exists():
            pytest.skip("no repo spec/requirements.md")
        reqs = parse_requirements(path.read_text())
        assert len(reqs) > 0
        ids = [r.id for r in reqs]
        assert len(ids) == len(set(ids)), "duplicate ids in repo requirements.md"
        for r in reqs:
            reparsed = parse_requirements(r.raw)
            assert len(reparsed) == 1 and reparsed[0].id == r.id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
