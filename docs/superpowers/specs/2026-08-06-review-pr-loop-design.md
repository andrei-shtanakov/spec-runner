# `spec-runner review-pr` — the PR review-bot loop (issue #102)

**Status:** design approved by the owner 2026-08-06 (decision recorded on
[#102](https://github.com/andrei-shtanakov/spec-runner/issues/102)).
Delivered: M1 in v2.18.0 (PR #110), M2 in v2.19.0 (PR #112), M3 in PR #114.
**Origin:** battle-testing finding F-22 (run d4d33ad0, TASK-007, v2.16.0).
Live precedents: kapelle PRs #1, #5, #6 — the cycle "integration PR opened →
Copilot leaves comments → verify each against the code → fix the justified
ones (TDD) → reply in threads → re-run gates → push" was performed manually
by an operator agent three times.

## Decision

Implement the loop **in spec-runner**, as a **separate resumable command**
that Maestro can later invoke — NOT as a Maestro-only hook, and NOT inline
in `spec-runner run`.

```
spec-runner review-pr <url-or-number>
```

The command is the durable primary operation. An **optional** post-PR stage
after `integration_pr` simply invokes it.

### Why spec-runner

- spec-runner already owns the integration branch, opens the PR, persists
  its URL, notifies about it (`pr_opened`), runs a review agent, commits its
  separate delta (#103), and re-runs tests/lint after mutations (#65):
  `git_ops.py` (integration branch lifecycle), `hooks.py` (gates + review).
- Direct spec-runner runs are a real, important mode. A Maestro-only
  solution would not have covered kapelle PRs #1/#5/#6 and would leave a
  functional hole.
- Review comments belong to the PR and its head SHA, not to a Maestro
  workstream gate. Hosting the mechanism in Maestro would force it to
  acquire foreign responsibilities: the GitHub thread API, a comment
  cursor, reply bookkeeping, review iterations, and PRs it did not create.
- maestro#137 answers a different question — whether a specific gate
  verdict can be machine-approved. That is a policy/evidence boundary.
  #102 is a code-mutation cycle driven by external review. Merging the two
  mechanisms would mix approval with mutation.

### The boundary

| Concern | Owner |
|---|---|
| GitHub transport + verify/fix/reply loop | **spec-runner** (`review-pr`) |
| When and for which PR to run it | lifecycle owner (spec-runner post-PR stage, or Maestro) |
| Approval policy | Maestro `approver_cmd` (maestro#137) — never this command |

One mechanism serves both modes: an external orchestrator invokes the same
`spec-runner review-pr` instead of re-implementing the loop, and never has
to become a GitHub-review client. **How** it invokes it — and what it does
with the outcome — is that consumer's design, not this document's (see
"External caller contract" below).

## Why not inline in `run`

Review bots are asynchronous: comments may appear minutes later, again
after a push, or never. Inlining a poll into `run` would turn it into a
long-lived fragile process. Instead the command is a **durable state
machine** that can be invoked repeatedly (by an operator, a cron, an
external orchestrator) and resumes from persisted state:

```
WAITING_REVIEW
  → COLLECTED          # new comments fetched past the cursor
  → VERIFIED           # each comment has an independent verdict
  → FIXED | REFUTED    # per-comment outcome applied
  → GATES_PASSED       # full tests + lint after any mutation
  → REPLIED            # thread replies published (post-push only)
  → WAITING_REVIEW     # next round (new head SHA ⇒ new bounded round)
  → COMPLETE | NEEDS_HUMAN
```

State is keyed by `{repo, pr_number, head_sha, thread_id, comment_id}`.
After a crash the command continues the cycle: it never replies twice and
never re-processes an already-handled comment. Storage: new tables in the
existing SQLite state DB (WAL, idempotent migration — the established
pattern from `attempts.no_op`).

## Hard constraints (all normative)

1. **Opt-in.** Without configuration, current `integration_pr` behavior is
   byte-identical. Config sketch: a `review_pr:` section with `enabled`,
   `allowed_bots`, limits, and `post_pr: true` to wire the optional stage.
2. **Allowed bot identities only.** Comments from any other author are
   ignored by the loop (humans are never auto-answered).
3. **Independent per-comment verdict:** `valid`, `refuted`, or `uncertain`.
4. **`uncertain` → human** (`NEEDS_HUMAN`), never an auto-fix and never an
   automatic pushback.
5. **Fixes are TDD** and land as a **separate commit** with provenance
   (comment/thread ID in the commit message trailer).
6. **Full tests + lint after any mutation, before push** (the #65
   invariant extends to this loop).
7. **Replies are published only after a successful push** and contain the
   actual commit SHA.
8. **Refutations carry verifiable evidence** (a command run, a code
   reference with line numbers), not free-form agent disagreement.
9. **Limits on everything:** iterations, comments, changed files/lines,
   cost (USD), wall time. A new head SHA opens the next *bounded* round;
   exceeding any limit → `NEEDS_HUMAN`.
10. **Fail-closed** on: draft/pending review, API rate limit, deleted
    comment, force-push (head-SHA mismatch with stored state), and
    permission failures.
11. **No auto-merge and no automatic approving review.** Ever.

## External caller contract (M3)

This section is the whole of what spec-runner promises to an external
caller. It deliberately says nothing about how any particular consumer
invokes the command or maps its outcome — that belongs to the consumer
(see the pointer at the end).

Invocation: ``spec-runner review-pr <url-or-number> --json``. Two stable
surfaces:

- **Exit code**:
  - ``0`` — processing complete (every comment fixed-or-refuted AND
    replied; in ``--verify-only`` mode: every comment verified
    valid/refuted).
  - ``1`` — infrastructure or protocol failure, **not** a review verdict
    (draft/closed PR, API failure, dirty tree, head-SHA mismatch,
    force-push, push failure). Nothing was published.
  - ``2`` — a human decision is required (uncertain, unverified,
    limit-stopped or deleted comments). Re-invocation is safe.

  Persisted state makes re-invocation idempotent: the loop resumes where
  it stopped, never re-verifies what already has a verdict, and never
  replies to a comment twice.
- **``--json`` report**: per-comment ``verdict``/``resolution``/
  ``fix_sha``/``replied_at``, counts, a top-level ``needs_human`` boolean,
  and ``exit_code`` (mirrors the process exit code, so a stored report is
  self-describing). With ``--json``, **stdout carries exactly one JSON
  document on every exit path**, including fail-closed: every diagnostic —
  limit stops, fail-closed messages, progress — goes to stderr
  (spec-runner#116, requested by Maestro's wrapper, which stores the
  report verbatim in an audit table). On exit ``1`` the document is
  ``{repo, pr_number, error, exit_code}`` with ``repo``/``pr_number``
  ``null`` when the ref could not be resolved.

Precondition for the mutating mode (anything other than the read-only
``--verify-only`` and ``--no-verify`` modes): a clean checkout with local
``HEAD`` equal to the PR head. Otherwise the command fail-closes with exit
``1`` before touching anything.

**Known consumer: Maestro.** Its invocation, lifecycle, persistence and
outcome-mapping semantics are owned by Maestro — see maestro#147 and the
accepted ``post-pr-command`` track. They are not part of this contract.
The link is deliberately a pointer, not a copy: restating a neighbour's
lifecycle here is how this document went stale within a day of being
written.

A pinned JSON schema is **not** part of this arrangement today. While a
consumer only invokes the CLI behind a version gate, the stable public
contract above plus that pointer are enough. If the ``--json`` envelope
ever becomes a formally validated cross-repo contract, spec-runner owns
the schema and consumers vendor a pinned copy — the rule this repo already
follows for the Maestro state/interop schemas.

The optional **post-PR stage** wires the same call into spec-runner's own
``integration_pr`` flow: ``review_pr.post_pr: off | verify | full``
(default ``off`` — constraint 1 holds, the flow is byte-identical without
configuration). ``verify`` runs the read-only triage; ``full`` checks the
run branch out, runs the whole loop, and always returns to the base
branch. ``post_pr_wait_seconds`` (default 120) gives the review bot time
to comment. The stage never changes the run's exit status — its outcome
lives in the loop's own report, the persisted state, and ``status``.

## Implementation phases

- **M1 — read-only loop:** `review-pr` collects comments past the cursor,
  runs verification, prints/persists verdicts, exits with a machine-readable
  report (`--json`). No mutations, no replies. This alone de-risks the
  verify step (on kapelle PR #6, 1 of 3 Copilot comments was empirically
  refuted).
- **M2 — fix + reply:** TDD fixes, gate re-runs, push, thread replies,
  bounded rounds, `NEEDS_HUMAN` surfacing in `status`.
- **M3 — wiring:** optional post-PR stage after `integration_pr`; document
  the exit-code contract for external callers.

## Non-goals

- Approving or merging PRs (approval policy stays in maestro#137).
- Answering human reviewers.
- Supporting review systems other than GitHub in v1.
