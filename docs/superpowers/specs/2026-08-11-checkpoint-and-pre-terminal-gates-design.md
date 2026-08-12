# Checkpoint commit + pre-terminal policy gates — design

**Status:** design only. No code ships with this document.
**Issue:** #164
**Consumers:** #157 (review policy), #141 (TDD lifecycle). Neither owns this.

---

## 1. The terminological trap this document exists to avoid

It is tempting — and wrong — to say a policy gate must run *before* the commit.

The earlier phrasing in the TDD design said exactly that: "`RED_VERIFYING` must
gate the commit, not follow it." That is backwards. **A stable red SHA is
precisely what the verification needs.** Replay without a commit to replay
against is not verification, it is trust in whatever happens to be in the
working tree at the moment.

What a gate withholds is not the commit. It is **progress past it**:

> The checkpoint commit always happens. What a policy gate blocks is the
> transition to the next phase and, ultimately, merge and terminal completion.

Restated per consumer:

- **review** — the checkpoint commit is made, review runs against that SHA, and
  an unsatisfied policy blocks merge/DONE, not the commit;
- **TDD** — the red checkpoint commit is made, `RED_VERIFYING` replays against
  that SHA, and an outcome other than `EXPECTED_FAIL` blocks the transition to
  `GREEN_IMPLEMENTING`, not the commit.

Getting this backwards would have removed the very artifact both consumers
depend on.

---

## 2. Target shape

```
work
→ deterministic checks
→ checkpoint commit
→ pre-terminal policy gate, evaluated against the checkpoint SHA
   ├─ satisfied        → merge → DONE
   ├─ unsatisfied      → resumable / non-terminal
   └─ instrument error → bounded recovery → infrastructure error
```

For TDD:

```
red authoring
→ red checkpoint commit
→ replay / RED_VERIFYING against the checkpoint SHA
→ EXPECTED_FAIL required
→ GREEN_IMPLEMENTING
```

The three outcomes of a gate are deliberately not two. "The gate says no" and
"the gate could not answer" are different facts with different owners — the
same distinction [Part A of the TDD design](2026-08-11-tdd-lifecycle-design.md#22-the-vocabulary) makes with `NOT_RUN`/`ERROR`, and the same one
#138 had to introduce after a timeout was being recorded as a pass. An
instrument error is not a defect in the work: bounded automatic recovery first,
and only after it is exhausted an **infrastructure error**, which is not the
same as NEEDS_HUMAN.

---

## 3. What a gate is

A declarative registration, not a hook that consumers monkey-patch:

```
gate:
  id            stable identifier, e.g. "review", "tdd.red"
  phase         where it runs in the lifecycle above
  evaluates     (checkpoint SHA, config hash) -> PhaseOutcome
  policy        how outcomes map to satisfied / unsatisfied / error
```

Two properties follow from the acceptance criteria below rather than from
taste:

- **A gate is evaluated against an exact `(SHA, config hash)` pair.** Not
  "the task", not "the branch". A verdict is a statement about a specific tree
  under a specific policy, and it stops being one the moment either changes.
- **A gate never edits history.** Fixes are new commits on top of the
  checkpoint. This is what makes a crash mid-gate recoverable, and it is why
  the pilot's "fix a frozen test by rewriting history" is not a supported
  motion (see [the TDD design, §3.5](2026-08-11-tdd-lifecycle-design.md#35-what-the-pilot-learned-that-the-contract-must-not-omit), issue #141).

`PhaseOutcome` comes from [the TDD design, Part A](2026-08-11-tdd-lifecycle-design.md#2-part-a--the-phase-execution-result-contract-tdd-independent) (issue #141, slice 0) and is *not* re-invented here.
That ordering is deliberate: writing this code before slice 0 would produce a
second temporary vocabulary, and the whole point of #164 is that there is one.

---

## 4. Acceptance criteria

Owner-stated; each with the failure it prevents.

| # | Criterion | What it prevents |
|---|---|---|
| 1 | A checkpoint never by itself means DONE | the 2.23.0 class of defect: an artifact that exists is read as work that finished |
| 2 | No merge until the policy gate is satisfied | the checkpoint escaping into the integration branch unreviewed/unverified |
| 3 | The gate is bound to an exact checkpoint SHA **and** config hash | a verdict from another tree, or from another policy, being reused as this one's |
| 4 | Fixes create a new commit; history is never rewritten | losing the provenance the checkpoint exists to provide (#103) |
| 5 | A stale verdict for an old SHA does not clear a new SHA | exactly the harness-guard bypass of #137, one level up: evidence from before the change legitimising the change |
| 6 | A crash after the checkpoint is resumable | the run being unable to say what state it was in, and redoing (or skipping) verified work |
| 7 | Consumers register declaratively | a second consumer arriving as a special case inside the first one's code |
| 8 | With no consumer enabled, execution and terminal behaviour are unchanged | the same guarantee as [the TDD design, §3.1](2026-08-11-tdd-lifecycle-design.md#31-mode) (issue #141): opt-in means undetectable when not opted into |
| 9 | HITL stays a separate authority mechanism | conflating "the instrument reported" with "a human decided" — the distinction a **waiver** rests on (a recorded operator decision beside the observation, never an outcome value; `phases.py`) |

Criterion 5 deserves its own note. It is the same shape as #137 (a snapshot
re-taken per attempt legitimised an edit that survived one failure) and as the
`no_op`/reconciliation work: **evidence must be tied to what it is evidence
about, or persistence defeats it.** Binding to `(SHA, config hash)` is what
makes that mechanical rather than a matter of care.

---

## 5. Ordering (owner-stated)

1. **This document** — design only. ← *you are here*
2. **Slice 0** — general `PhaseOutcome` + append-only history (from
   [the TDD design, Part A](2026-08-11-tdd-lifecycle-design.md#2-part-a--the-phase-execution-result-contract-tdd-independent), issue #141), shipped as ordinary hardening, no TDD surface.
3. **#164 implementation**, on top of typed outcomes.
4. **#157** — review policy as the first consumer.
5. **#141 RED checkpoint** as the second consumer.

Why not slice 0 first: **#164 has to define the lifecycle boundary and what a
policy gate is allowed to return** before the vocabulary is frozen. Why not
#164's code first: it would need a vocabulary that does not exist yet, and
would grow a temporary one. Hence design here, vocabulary there, code after
both.

---

## 6. Non-goals

- **Deciding any specific policy.** Which verdicts block is the consumer's
  business; #157 already fixes the table for review.
- **Replacing HITL.** It remains a separate authority stage.
- **Changing `standard`-mode behaviour** where no consumer is enabled
  (criterion 8).
- **Touching `--json-result`.** New state is additive; the Maestro-facing
  payload is frozen without a reason and a major bump.

---

## 7. Open — as resolved by the implementation

The mechanism shipped in `spec_runner/gates.py`. What the three open questions
turned into:

1. **Where the registry lives.** A `GateRegistry` in `gates.py`, with a
   module-level `REGISTRY` consumers attach to; `evaluate_gates` also takes an
   explicit registry, which is what the tests use. Exposing registration
   through config or the `plugins/` surface stays open — nothing here forecloses
   it, and no third-party consumer exists yet to justify the surface.
2. **Bounded recovery.** `gate_recovery_attempts` (default 1), applied **per
   gate**, no backoff. Only `INSTRUMENT_ERROR` is retried: "the gate says no"
   is an answer, and asking again does not make it a different one. After the
   bound is exhausted the result is an infrastructure error — not NEEDS_HUMAN.
3. **Exit code.** Resolved by *not* adding a surface. An unsatisfied gate takes
   the existing "this attempt did not succeed" path: the task does not reach
   DONE and stays resumable, the checkpoint commit stays in place, and the run's
   exit code follows from the existing failed-task rules. So it does reach the
   exit code — through the surface that already exists, with no new value and no
   change to `RUN_STOP_REASONS`. Note this is not the "terminal state only"
   reading: a gate failure is visible to CI exactly as any other unfinished task
   is. If gate failures should instead be distinguishable at the exit surface,
   that is a deliberate follow-up, not something to discover later.

## 8. What shipped

- `GateStatus` / `GateResult` / `GateContext` / `GateRegistry`, `evaluate_gates`
  (per phase) and `evaluate_pre_terminal` (every registered phase, at the
  pre-terminal site).
- The `gate_verdicts` table keyed on `(task_id, gate_id, checkpoint_sha,
  config_hash)` — see [`docs/state-schema.md`](../../state-schema.md).
- The call site in `post_done_hook`, between the checkpoint commit and the
  merge, guarded by `has_gates()` so the dormant path resolves no SHA and opens
  no state.
- No consumer. `POLICY_KEYS` does not yet list `review_policy`: that key arrives
  with #157, and listing it early would hash a permanent `None`.
