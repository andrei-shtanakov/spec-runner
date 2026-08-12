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

Two enforcement points, one script — because two implementations of one rule is
how the rule comes to mean two things:

    pull_request   early feedback, on every commit          (changelog-links.yml)
    tag / publish  fail-closed barrier, `--tag vX.Y.Z`      (publish.yml)

`--tag` adds what only makes sense when an artifact is going out: the tag agrees
with the pyproject version, and that version has a CHANGELOG section — the one
the GitHub Release notes are written from. `publish` needs `build`, so a failure
means nothing reaches PyPI.

Exits 0 when everything holds, 1 with a specific message when it does not.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

VERSION_RE = re.compile(r'^version = "(?P<v>[^"]+)"', re.MULTILINE)
SECTION_RE = re.compile(r"^## \[(?P<v>\d+\.\d+\.\d+)\]", re.MULTILINE)
VERSION_HEADING_RE = re.compile(r"^## \[(?P<label>[^\]]+)\]", re.MULTILINE)
SUBSECTION_RE = re.compile(r"^### (?P<name>.+)$", re.MULTILINE)
LINK_RE = re.compile(r"^\[(?P<label>Unreleased|\d+\.\d+\.\d+)\]:\s*(?P<url>\S+)\s*$", re.MULTILINE)


def _read(path: Path) -> str | None:
    """File contents as UTF-8, or None when it is not there.

    A guard whose failure mode is a traceback teaches people to ignore its
    output, so a missing file becomes a stated problem like any other. UTF-8 is
    explicit because a CI runner's locale is not ours to assume, and this file
    is full of em-dashes.
    """
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


TAG_RE = re.compile(r"^v?(?P<v>\d+\.\d+\.\d+)$")


def check(root: Path, tag: str | None = None) -> list[str]:
    """Return a list of problems; empty means the links are consistent.

    ``tag`` turns on the tag-time checks — the second enforcement point. The
    invariants are the same ones; what changes is that a tag is being published
    from this tree, so the tag itself has to agree with the version and the
    version has to have a section (that section is what the GitHub Release is
    written from). Passing None keeps the pull-request behaviour exactly as it
    was: a release PR legitimately has no tag yet.
    """
    pyproject = _read(root / "pyproject.toml")
    if pyproject is None:
        return [f"cannot read {root / 'pyproject.toml'} — is this the repository root?"]
    match = VERSION_RE.search(pyproject)
    if not match:
        return ["could not read a version from pyproject.toml"]
    current = match.group("v")

    changelog = _read(root / "CHANGELOG.md")
    if changelog is None:
        return [f"cannot read {root / 'CHANGELOG.md'} — is this the repository root?"]
    sections = SECTION_RE.findall(changelog)
    links = {m.group("label"): m.group("url") for m in LINK_RE.finditer(changelog)}
    problems: list[str] = []
    problems.extend(_duplicate_subsections(changelog))
    if tag is not None:
        problems.extend(_tag_problems(tag, current, sections))

    if not sections:
        # Appended, not returned on its own: anything already collected — a tag
        # that does not match the version, a duplicated subsection — is still
        # true and still what the operator needs to fix.
        problems.append("CHANGELOG has no released version sections")
        return problems
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


def _tag_problems(tag: str, current: str, sections: list[str]) -> list[str]:
    """Checks that only make sense when a tag is being published.

    `publish.yml` verified the tag against pyproject in inline shell. That check
    lives here now — one script, two enforcement points, rather than two
    implementations of the same rule drifting in two files.
    """
    match = TAG_RE.match(tag.strip())
    if not match:
        return [f"tag {tag!r} is not a vX.Y.Z release tag"]
    tagged = match.group("v")
    problems: list[str] = []
    if tagged != current:
        problems.append(
            f"tag v{tagged} does not match the pyproject version {current} — "
            "publishing would put the wrong version on PyPI"
        )
    if tagged not in sections:
        problems.append(
            f"no CHANGELOG section [{tagged}] — the release notes are written "
            "from it, so publishing would ship without any"
        )
    return problems


def _duplicate_subsections(changelog: str) -> list[str]:
    """One `### Added` per version, not two.

    Twice now an insertion has produced a second `### Added` inside
    `[Unreleased]`, splitting one list in half. Cheap to check, and the same
    reason the link check exists: the file is long enough that the duplicate is
    invisible while editing.
    """
    problems: list[str] = []
    starts = [(m.start(), m.group("label")) for m in VERSION_HEADING_RE.finditer(changelog)]
    for index, (offset, label) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(changelog)
        seen: set[str] = set()
        for sub in SUBSECTION_RE.finditer(changelog[offset:end]):
            name = sub.group("name").strip()
            if name in seen:
                problems.append(f"[{label}] has two '### {name}' sections; merge them")
            seen.add(name)
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", help="repository root (default: this file's repo)")
    parser.add_argument(
        "--tag",
        help="the release tag being published (e.g. v2.27.0) — adds the tag-time checks",
    )
    args = parser.parse_args()
    root = Path(args.root) if args.root else Path(__file__).resolve().parent.parent
    problems = check(root, tag=args.tag)
    if not problems:
        where = f" for {args.tag}" if args.tag else ""
        print(f"✅ CHANGELOG compare links are consistent{where}")
        return 0
    for problem in problems:
        print(f"::error title=CHANGELOG links::{problem}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
