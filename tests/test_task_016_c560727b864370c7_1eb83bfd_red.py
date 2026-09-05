"""RED for TASK-016 (spec-runner#341 / #334, BEH-26).

`Given` a reader hunting for a description of the TDD checkpoint machinery
this workstream changed — the evidential red file's name now carries a
namespace segment (TASK-010/011, #334), a declared fix invocation is required
before the machine fix runs at all (TASK-004/008, #341 FR-05), a composite
`lint_command` gets one specific, undeclared-guessing-free refusal
(TASK-008, FR-09), and the pre-freeze repair is bounded to the mechanical fix
plus exactly one cold agent round (TASK-006, BEH-07) — `docs/architecture.md`
and `CHANGELOG.md` today say none of it: `docs/architecture.md` never mentions
`tdd.py`/`tdd_runners.py` or the word "namespace" at all, and the
`[Unreleased]` section of `CHANGELOG.md` covers only #330's budget-domain
naming, nothing about #341/#334.

`When` this test reads both files.

`Then` it fails on a plain assertion naming exactly which BEH-26 facts are
still missing — not on a missing import or a file that does not exist, since
both files are already tracked and this red is about their *content*.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestDocsAndChangelogAnnounceTheRenameAndNewBehaviour:
    def test_beh26_facts_are_documented(self):
        architecture = (REPO_ROOT / "docs" / "architecture.md").read_text().lower()
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text().lower()

        missing: list[str] = []

        # FR-10: the evidential file's name carries a namespace segment, with
        # a distinct form for a declared `tdd_namespace` (a readable slug)
        # versus the computed fallback (a digest) — `namespace_segment`'s own
        # two-part slug+digest formula.
        if "evidential" not in architecture:
            missing.append("architecture.md: the term 'evidential' (red file naming)")
        if "namespace" not in architecture:
            missing.append("architecture.md: 'namespace' segment in the evidential path")
        if "tdd_namespace" not in architecture:
            missing.append("architecture.md: the declared `tdd_namespace` config key")
        if not ("slug" in architecture and "digest" in architecture):
            missing.append(
                "architecture.md: the slug (declared) vs digest (computed) segment forms"
            )

        # FR-10: the distinguishability boundary — same input (same
        # spec_prefix, no declared tdd_namespace) yields the same namespace,
        # not a silently different one — plus the remedy.
        if "spec_prefix" not in architecture:
            missing.append("architecture.md: `spec_prefix` as the other half of the boundary")
        if not any(
            phrase in architecture
            for phrase in ("same namespace", "same input", "indistinguishable", "collide")
        ):
            missing.append(
                "architecture.md: the boundary that identical input yields one namespace"
            )

        # FR-05/BEH-29 (#341): machine fix requires a declared fix invocation;
        # the python-shaped default is not itself a declaration. Migration
        # hint for repos already relying on the old, always-on default.
        if "lint_fix_command_declared" not in changelog and "commands.lint_fix" not in changelog:
            missing.append("CHANGELOG.md: the declared fix-invocation requirement (FR-05)")
        if "declared" not in changelog:
            missing.append("CHANGELOG.md: that the fix invocation must be declared")

        # FR-09: composite `lint_command` gets one specific, named refusal —
        # fix mode is not applied at all, never a guess at which component.
        if "composite lint_command" not in changelog and "composite lint_command" not in architecture:
            missing.append("CHANGELOG.md/architecture.md: the composite lint_command behaviour")

        # BEH-07: the pre-freeze repair's boundary — the mechanical fix plus
        # exactly one cold agent round, never more.
        if not any("agent round" in doc for doc in (changelog, architecture)):
            missing.append("CHANGELOG.md/architecture.md: the one-cold-agent-round ceiling")

        # The rename itself must be called out as observable, with old paths
        # (pre-#341 evidential file names, without a namespace segment)
        # explicitly still valid without any migration step.
        if not ("rename" in changelog or "renamed" in changelog):
            missing.append("CHANGELOG.md: the evidential file rename called out as observable")
        if "no migration" not in changelog and "without migration" not in changelog:
            missing.append("CHANGELOG.md: that old paths remain valid without migration")

        assert not missing, "BEH-26 facts not yet documented:\n" + "\n".join(
            f"- {item}" for item in missing
        )
