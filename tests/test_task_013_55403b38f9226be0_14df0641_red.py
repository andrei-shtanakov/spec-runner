"""RED for TASK-013 (spec-runner#367, BEH-33).

`Given` a reader hunting `docs/architecture.md` and `CHANGELOG.md` for a
description of `verify_first` — the third execution mode this workstream adds
(FR-01: a per-task `**Mode:** verify_first` line, resolved by the same
`ExecutorConfig.resolve_execution_mode` as `standard`/`tdd`) — today neither
file says anything about it: `docs/architecture.md` never mentions
`verify_first` at all, and the `[Unreleased]` section of `CHANGELOG.md` only
covers the additive `verify_outcome` JSON field (BEH-32) and unrelated #334/
#341 work, never the mode itself, its group-declaration form, its
verify-evidence, its three outcomes and branching rule, or its declared
boundaries (Q-05/Q-06).

`When` this test reads both files.

`Then` it fails on a plain assertion naming exactly which BEH-33 facts are
still undocumented — not on a missing import or a nonexistent file, since both
files are already tracked and this red is about their *content*.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestDocsAndChangelogAnnounceVerifyFirstAndItsBoundaries:
    def test_beh33_facts_are_documented(self):
        architecture = (REPO_ROOT / "docs" / "architecture.md").read_text().lower()
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text().lower()

        missing: list[str] = []

        # FR-01: the mode itself, declared per task, and what it means.
        if "verify_first" not in architecture and "verify-first" not in architecture:
            missing.append("architecture.md: the `verify_first` execution mode")
        if "third" not in architecture:
            missing.append(
                "architecture.md: verify_first named as the third execution mode"
            )

        # FR-02: the group-declaration form — a dedicated, non-inferred
        # metadata line.
        if "**verifies:**" not in architecture and "verifies:" not in architecture:
            missing.append(
                "architecture.md: the `**Verifies:**` group-declaration line"
            )

        # FR-10: verify-evidence composition and meaning.
        if "verify-evidence" not in architecture and "verify_evidence" not in architecture:
            missing.append("architecture.md: verify-evidence and its composition")
        if not any(
            term in architecture for term in ("environment_id", "config hash", "config_hash")
        ):
            missing.append(
                "architecture.md: verify-evidence identity fields "
                "(environment_id / config hash)"
            )

        # FR-16: the three outcomes and the exhaustive branching rule.
        if not (
            "green" in architecture
            and ("test-failure" in architecture or "test_failure" in architecture)
            and ("instrument-error" in architecture or "instrument_error" in architecture)
        ):
            missing.append("architecture.md: the three outcomes green/test-failure/instrument-error")
        if not any(
            phrase in architecture
            for phrase in ("no silent fourth", "not a silent fourth", "exhaustive by construction")
        ):
            missing.append("architecture.md: the branching rule has no silent fourth outcome")

        # Q-05: declared boundary — one run, no retry policy.
        if not any(
            phrase in architecture
            for phrase in ("one run", "single run", "no retry policy", "retry policy is not introduced")
        ):
            missing.append("architecture.md: Q-05 — one run, no retry policy on a flaky group")

        # Q-06: declared boundary — group frozen by claims, released on DONE.
        if not (
            "claim" in architecture
            and "done" in architecture
            and ("freeze" in architecture or "frozen" in architecture)
        ):
            missing.append(
                "architecture.md: Q-06 — the group frozen by claims and released on DONE"
            )

        # NFR-02 / charter: the deliberately preserved double test run —
        # verify-first and post_done_hook ask different questions of
        # different trees, without deduplication.
        if "post_done_hook" not in architecture:
            missing.append(
                "architecture.md: the intentionally preserved double test run "
                "(verify-first vs post_done_hook, no deduplication)"
            )

        # Selector dictionary boundary: a file target is not declarable
        # today — the consumer must emit a node id.
        if "node id" not in architecture:
            missing.append(
                "architecture.md: the selector dictionary is node ids "
                "(a file target is not declarable today)"
            )

        # CHANGELOG: names the change and its observable consequences,
        # including BEH-17 checkpoint devaluation for a task whose mode
        # line changed.
        if "verify_first" not in changelog and "verify-first" not in changelog:
            missing.append("CHANGELOG.md: the verify_first mode itself")
        if not any(
            phrase in changelog
            for phrase in ("checkpoints for a task whose", "obsolete", "invalidat")
        ):
            missing.append(
                "CHANGELOG.md: checkpoints obsoleted for tasks whose mode line "
                "changed (BEH-17)"
            )

        assert not missing, "BEH-33 facts not yet documented:\n" + "\n".join(
            f"- {item}" for item in missing
        )
