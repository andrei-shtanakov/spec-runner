"""Structured, tolerant parsing of requirements documents (M1).

Turns a ``requirements.md`` into id-keyed :class:`Requirement` blocks so a
requirement becomes a diffable/mergeable unit — the foundation for delta specs
and archive merge (M3).

Requirements in the wild use heterogeneous sub-structure (gherkin acceptance
criteria, ``- [ ]`` checklists, or plain prose), so the parser anchors only on
two firm signals:

* the requirement heading ``#+ (REQ|NFR)-NNN[: name]`` at any depth, and
* the block boundary: the next heading whose level is *the same or higher*
  (fewer or equal ``#``).

Each block's exact source text is preserved in :attr:`Requirement.raw` (the
round-trip / merge unit); the remaining fields (priority, acceptance criteria,
traceability refs) are best-effort and default to empty when absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .spec import strip_frontmatter

# Requirement heading: capture leading hashes, the id, and the trailing name.
_REQ_HEADING = re.compile(r"^(#+)\s*((?:REQ|NFR)-\d+)\s*:?\s*(.*?)\s*$")
# Any markdown ATX heading (used for block-boundary detection).
_ANY_HEADING = re.compile(r"^(#+)\s")
# Traceability-style identifiers anywhere in a block.
_REF = re.compile(r"\b((?:REQ|DESIGN|TASK|NFR)-\d+)\b")
# A **Priority** field, tolerant of the colon being inside or outside the bold
# (both "**Priority:** P0" and "**Priority**: P0" occur in the wild).
_PRIORITY = re.compile(r"^\*\*Priority:?\*\*\s*:?\s*(.+?)\s*$", re.MULTILINE)
# An **Acceptance Criteria** field marker, colon inside or outside the bold.
_ACCEPTANCE = re.compile(r"^\*\*Acceptance Criteria:?\*\*\s*:?\s*$", re.MULTILINE)
# A bold field marker line, e.g. "**Traces to:**" — ends an acceptance block.
_BOLD_FIELD = re.compile(r"^\*\*[^*]+\*\*\s*:?\s*$")


@dataclass(frozen=True)
class Requirement:
    """A single requirement parsed from a requirements document."""

    id: str  # e.g. "REQ-101" / "NFR-001"
    name: str  # heading text after the id (may be empty)
    level: int  # number of leading '#' on the heading line
    raw: str  # exact source block, including the heading (round-trip unit)
    acceptance_criteria: str = ""  # text under an Acceptance Criteria marker
    priority: str = ""  # value of a **Priority** field, if any
    traces_to: tuple[str, ...] = ()  # other REQ/DESIGN/TASK/NFR refs in the block

    @property
    def kind(self) -> str:
        """``"non-functional"`` for ``NFR-*`` ids, else ``"functional"``."""
        return "non-functional" if self.id.startswith("NFR-") else "functional"

    @property
    def number(self) -> int:
        """The numeric part of the id (e.g. 101 for ``REQ-101``)."""
        return int(self.id.split("-", 1)[1])


def parse_requirements(text: str) -> list[Requirement]:
    """Parse a requirements document into id-keyed :class:`Requirement` blocks.

    Frontmatter is stripped first. Returns requirements in document order;
    non-requirement content (section headers, intro prose) is not returned but
    is preserved implicitly via block boundaries.
    """
    body = strip_frontmatter(text)
    lines = body.splitlines(keepends=True)

    heads: list[tuple[int, int, str, str]] = []
    for i, line in enumerate(lines):
        m = _REQ_HEADING.match(line)
        if m:
            heads.append((i, len(m.group(1)), m.group(2), m.group(3)))

    reqs: list[Requirement] = []
    for start, level, rid, name in heads:
        end = len(lines)
        for k in range(start + 1, len(lines)):
            hm = _ANY_HEADING.match(lines[k])
            if hm and len(hm.group(1)) <= level:
                end = k
                break
        raw = "".join(lines[start:end])
        reqs.append(_build(rid, name, level, raw))
    return reqs


def _build(rid: str, name: str, level: int, raw: str) -> Requirement:
    """Assemble a :class:`Requirement`, extracting best-effort fields from raw."""
    priority_match = _PRIORITY.search(raw)
    priority = priority_match.group(1).strip() if priority_match else ""

    refs = tuple(dict.fromkeys(r for r in _REF.findall(raw) if r != rid))

    return Requirement(
        id=rid,
        name=name,
        level=level,
        raw=raw,
        acceptance_criteria=_extract_acceptance(raw),
        priority=priority,
        traces_to=refs,
    )


def _extract_acceptance(raw: str) -> str:
    """Return the text following an Acceptance Criteria marker, best-effort.

    Captures from just after the marker up to the next bold field marker or the
    end of the block. Returns ``""`` when no marker is present.
    """
    marker = _ACCEPTANCE.search(raw)
    if not marker:
        return ""
    rest = raw[marker.end() :].lstrip("\n")
    collected: list[str] = []
    for line in rest.splitlines():
        if _BOLD_FIELD.match(line):
            break
        collected.append(line)
    return "\n".join(collected).strip()


def serialize_requirement(req: Requirement) -> str:
    """Return the exact source block for ``req`` (round-trip / merge unit)."""
    return req.raw


def find_requirement(reqs: list[Requirement], req_id: str) -> Requirement | None:
    """Return the requirement with ``req_id``, or ``None`` if absent."""
    return next((r for r in reqs if r.id == req_id), None)
