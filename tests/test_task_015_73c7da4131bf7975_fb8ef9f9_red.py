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

ARCHITECTURE_DOC = Path(__file__).resolve().parent.parent / "docs" / "architecture.md"


class TestBEH31DeclaredBoundaryReplacedByContract:
    def test_removed_wording_gone_and_contract_named(self):
        text = ARCHITECTURE_DOC.read_text(encoding="utf-8")
        normalized = re.sub(r"\s+", " ", text.replace("**", "")).lower()

        assert "a file target is not declarable today" not in normalized, (
            "BEH-31: the retired wording must be removed from "
            "docs/architecture.md, replaced by the accepted contract"
        )
        assert "asymmetry" in text, (
            "BEH-31: the doc must name the declared asymmetry between the "
            "node-id group and the file target explicitly"
        )
        assert "not execution" in text, (
            "BEH-31: the node-id group's existing rule (BEH-06 — a skip is "
            "not execution) must still be named as one half of the asymmetry"
        )
        assert "accounted" in text, (
            "BEH-31: the file target's own rule (BEH-16 — every member is "
            "accounted for) must be named as the other half of the asymmetry"
        )
        assert "devtools" in text, (
            "BEH-31: the accepted contract must record that it was "
            "announced to the pipeline owner (devtools)"
        )
        assert "issue" in text or "handoff" in text, (
            "BEH-31: the announcement to devtools must be via issue or "
            "handoff, not a silent doc edit"
        )
