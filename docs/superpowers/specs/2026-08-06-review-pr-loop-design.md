# `spec-runner review-pr` — the PR review-bot loop (issue #102)

**Status:** design approved by the owner 2026-08-06 (decision recorded on
[#102](https://github.com/andrei-shtanakov/spec-runner/issues/102)); not yet
implemented.
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

Maestro later gets a thin hook —
`PR_CREATED → external post_pr_command → PR_REVIEWED / NEEDS_REVIEW` —
and can invoke the same `spec-runner review-pr` without re-implementing the
loop. One mechanism serves both modes without turning Maestro into a
GitHub-review client.

## Why not inline in `run`

Review bots are asynchronous: comments may appear minutes later, again
after a push, or never. Inlining a poll into `run` would turn it into a
long-lived fragile process. Instead the command is a **durable state
machine** that can be invoked repeatedly (by an operator, a cron, a
Maestro hook) and resumes from persisted state:

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

## Implementation phases

- **M1 — read-only loop:** `review-pr` collects comments past the cursor,
  runs verification, prints/persists verdicts, exits with a machine-readable
  report (`--json`). No mutations, no replies. This alone de-risks the
  verify step (on kapelle PR #6, 1 of 3 Copilot comments was empirically
  refuted).
- **M2 — fix + reply:** TDD fixes, gate re-runs, push, thread replies,
  bounded rounds, `NEEDS_HUMAN` surfacing in `status`.
- **M3 — wiring:** optional post-PR stage after `integration_pr`; document
  the exit-code contract for external callers (the future Maestro hook).

## Non-goals

- Approving or merging PRs (approval policy stays in maestro#137).
- Answering human reviewers.
- Supporting review systems other than GitHub in v1.
