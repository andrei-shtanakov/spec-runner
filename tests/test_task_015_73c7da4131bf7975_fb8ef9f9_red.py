"""RED for TASK-015 / BEH-31.

The declared boundary wording ("a file target is not declarable today") is
retired from docs/architecture.md and replaced by the accepted file-target
contract: what is accepted, how composition resolves, what proves green, and
the declared asymmetry between the node-id group's existing rule (BEH-06 — a
skip is not execution) and the file target's own rule (BEH-16 — every member
is accounted for), plus the announcement of that contract to the pipeline
owner (devtools) via issue or handoff.
"""

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
ARCHITECTURE_DOC = _REPO / "docs" / "architecture.md"

#: Every file DT-15 names as carrying the retired wording. The red used to
#: read only the first, so putting "node ids only, a bare file target is not
#: declarable" back into `CHANGELOG.md` left the suite green and the task
#: DONE (spec-runner#458). A boundary retired in three places has to be
#: guarded in three places.
DECLARED_DOCS = (
    ARCHITECTURE_DOC,
    _REPO / "CHANGELOG.md",
    _REPO / "CLAUDE.md",
)

RETIRED_WORDING = "a file target is not declarable today"


class TestBEH31DeclaredBoundaryReplacedByContract:
    def test_removed_wording_gone_and_contract_named(self):
        for doc in DECLARED_DOCS:
            normalized = re.sub(
                r"\s+", " ", doc.read_text(encoding="utf-8").replace("**", "")
            ).lower()
            assert RETIRED_WORDING not in normalized, (
                f"BEH-31: the retired wording must be removed from "
                f"{doc.name}, replaced by the accepted contract"
            )

        text = ARCHITECTURE_DOC.read_text(encoding="utf-8")

        assert "asymmetry" in text, (
            "BEH-31: the doc must name the declared asymmetry between the "
            "node-id group and the file target explicitly"
        )

        # Both halves must be named **in one asymmetry paragraph**.
        # Unscoped, `"not execution" in text` was satisfied by unrelated
        # #367 prose elsewhere in the document, so the assertion held even if
        # the asymmetry were described with only one of its two halves
        # (spec-runner#458). Scoping it to *exactly one* such paragraph
        # (spec-runner#462) overshot in the other direction: BEH-31 is about
        # the contract being declared, not about the document mentioning the
        # word once — a second paragraph elaborating the same asymmetry would
        # have reddened this red for no behavioural reason.
        paragraphs = [p for p in text.split("\n\n") if "asymmetry" in p]
        assert paragraphs, "BEH-31: no paragraph declares the asymmetry at all"
        whole = [p for p in paragraphs if "not execution" in p and "accounted" in p]
        assert whole, (
            "BEH-31: one paragraph must name BOTH halves — the node-id "
            "group's existing rule (BEH-06 — a skip is not execution) and "
            "the file target's own (BEH-16 — every member is accounted "
            f"for). Paragraphs mentioning the asymmetry: {paragraphs!r}"
        )
        assert "devtools" in text, (
            "BEH-31: the accepted contract must record that it was "
            "announced to the pipeline owner (devtools)"
        )
        assert "issue" in text or "handoff" in text, (
            "BEH-31: the announcement to devtools must be via issue or "
            "handoff, not a silent doc edit"
        )
