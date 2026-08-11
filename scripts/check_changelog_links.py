"""Verify the CHANGELOG compare links match the declared version.

Cutting a release moves entries into a new section, and the link definitions
live ~1700 lines away at the bottom of the file — so the cut looks complete on
screen while the links still point at the previous release. That has now
happened twice: PR #100 left the version link definitions stale, PR #180 left
`[Unreleased]` comparing from the *previous* tag, so it claimed everything the
release contained.

Two invariants, checkable at any commit — not only at a release:

    [Unreleased]: …/compare/v<current>...HEAD
    [<current>]:  …/compare/v<previous>...v<current>

where `<current>` is the version in pyproject.toml and `<previous>` is the next
version section down in the CHANGELOG.

Exits 0 when both hold, 1 with a specific message when they do not.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

VERSION_RE = re.compile(r'^version = "(?P<v>[^"]+)"', re.MULTILINE)
SECTION_RE = re.compile(r"^## \[(?P<v>\d+\.\d+\.\d+)\]", re.MULTILINE)
LINK_RE = re.compile(r"^\[(?P<label>Unreleased|\d+\.\d+\.\d+)\]:\s*(?P<url>\S+)\s*$", re.MULTILINE)


def check(root: Path) -> list[str]:
    """Return a list of problems; empty means the links are consistent."""
    pyproject = (root / "pyproject.toml").read_text()
    match = VERSION_RE.search(pyproject)
    if not match:
        return ["could not read a version from pyproject.toml"]
    current = match.group("v")

    changelog = (root / "CHANGELOG.md").read_text()
    sections = SECTION_RE.findall(changelog)
    links = {m.group("label"): m.group("url") for m in LINK_RE.finditer(changelog)}
    problems: list[str] = []

    if not sections:
        return ["CHANGELOG has no released version sections"]
    if sections[0] != current:
        problems.append(
            f"pyproject declares {current} but the newest CHANGELOG section is "
            f"[{sections[0]}] — the release section was not cut"
        )
        return problems

    unreleased = links.get("Unreleased")
    want_unreleased = f"compare/v{current}...HEAD"
    if unreleased is None:
        problems.append("no [Unreleased] link definition")
    elif not unreleased.endswith(want_unreleased):
        problems.append(
            f"[Unreleased] should end with {want_unreleased!r}, got {unreleased!r} — "
            "it claims changes that are already released"
        )

    released = links.get(current)
    if released is None:
        problems.append(f"no [{current}] link definition")
    elif len(sections) > 1:
        previous = sections[1]
        want = f"compare/v{previous}...v{current}"
        if not released.endswith(want):
            problems.append(f"[{current}] should end with {want!r}, got {released!r}")
    return problems


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    problems = check(root)
    if not problems:
        print("✅ CHANGELOG compare links are consistent")
        return 0
    for problem in problems:
        print(f"::error title=CHANGELOG links::{problem}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
