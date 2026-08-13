# Post-green resume — design

**Status:** **implemented** (PR for #232). Signed off by the owner 2026-08-13 — with this document's own recommendation on claims **overruled**: `resume` reinstates the checkpoint *and* its lineage's claims, in one transaction (§7 "Claims"). Ready to implement; no code ships with this document.
**Issue:** #232 (F-28), terminal member of the F-25 → F-26 → F-27 → F-28 cascade.
**Related:** [TDD lifecycle](2026-08-11-tdd-lifecycle-design.md) (#141), [claim and remedy contracts](2026-08-11-claim-and-remedy-contracts.md) §3a, [budget authorization](2026-08-13-budget-authorization-design.md) (#230 part 2 — the wedge is expensive, and that is its problem)

---

## 1. What is wedged, from the pilot's own rows

kapelle TASK-101, `.executor-m2-state.db`:

| table | row |
|---|---|
| `red_checkpoints` #1 | `expected_fail` at `6c24cd9`, **status `superseded`** |
| `red_checkpoints` #2 | `not_red` at `98e16ab`, status `active` |
| `tdd_claims` #1 | the frozen test file, **status `superseded`** |
| `tdd_remedies` #1 | `repair`, actor recorded, reason recorded |
| `tdd_phases` | `red_authoring → red_verifying → green_implementing → green_verifying`, then `red_authoring` twice more |

The work is finished and green (252/0). The lifecycle **did** reach
`green_verifying` on 2026-08-12 — with a genuinely confirmed red behind it. What
the task cannot do is finish.

Every `retry` now re-enters `red_authoring`, pays for a RED authoring call
(`$0.6304` on the last one, from `agent_calls`) and is refused by the gate:

```
⛔ RED not confirmed, refusing to implement: the claimed red did not fail on replay
```

Of course it did not fail: the implementation exists. **A red cannot exist
precisely because the work is done**, and `red_authoring` is the only door the
lifecycle offers. The wedge is not merely permanent — each attempt to poke it
costs another paid call.

## 2. Why none of the existing doors open

| door | what happens | why it is not the answer |
|---|---|---|
| `retry` | re-authors a red, gate refuses | measured above; ~$0.63 a time |
| `tdd abandon` | retires the active checkpoint | leaves *no* checkpoint → the gate answers "no confirmed red for this task" and refuses. A different wall |
| `tdd repair` | new lineage, re-replays | already used; it is what produced the `not_red` row. Repair expects a red and honestly records that it did not get one |
| per-task `**Mode:** standard` | the RED gate returns SATISFIED for a non-tdd task | works, and is a lie: it says this task is not under the TDD contract when it demonstrably was. It also re-pays for an implementation pass |

## 3. The reframing this deserves

The issue reads as "the lifecycle has no post-green resume". True, but the
sharper statement is: **the post-green half has no remedy at all.** #141 slice 3
built remedies for the pre-green half — `abandon` and `repair` both answer
questions about a *red*. An operator meeting a post-green crash reaches for the
nearest tool, and the nearest tool was `repair`.

Applying it after green is a category error the tool allowed. Worse, it is the
step that closed the last door: repair **superseded the confirmed red**. Before
the repair, checkpoint #1 (`expected_fail` at `6c24cd9`) was active, and — this
is checkable, and true in the pilot right now —

```
git merge-base --is-ancestor 6c24cd9 98e16ab   # → YES
```

the confirmed red is an ancestor of the current head. With that row still
active, the RED gate would be satisfied and the task would proceed to review and
merge. The evidence for finishing this task has existed all along; a remedy
retired it.

## 4. Proposal: `spec-runner tdd resume`

```
spec-runner tdd resume TASK-101 \
  --reason "green established 2026-08-12; review never ran (session limit, #229)"
```

**What it does:** reinstates the task's confirmed red as its standing evidence
(status `superseded`/`abandoned` → `active`) **together with the claims of that
same lineage, in one transaction** (§7 "Claims"), and records the decision.

**What it refuses:** everything else. The command is admissible only when

1. a checkpoint with outcome `expected_fail` exists for this task in this
   namespace — *any* status, because supersession retires a red as the standing
   lineage, not as history; and
2. its `commit_sha` is an **ancestor of the current candidate** — the same
   descent test `_red_gate` already applies; and
3. the lifecycle has reached `green_implementing` or later — resuming past a
   green that never happened is not a resume.

Fail any of the three and the answer is "this task cannot be resumed past a
green it never had", which is the contract holding rather than bending.

**What it deliberately does not do: it introduces no new way to satisfy the RED
gate.** The gate is untouched. It keeps demanding a confirmed `expected_fail`
whose commit is an ancestor of the tree in hand; `resume` only changes *which
row is the standing one*, and only when such a row already exists. This is the
property that makes the proposal safe to ship: no code path gains the ability to
merge a task that never had a red.

**Guardrails**, identical to the existing remedies (#141 slice 3) — mandatory
`--reason`, recorded actor, refusal while the PID-checked executor lock is held,
`SPEC_RUNNER_AGENT` refusal, and idempotency checked before the swap. The record
is a `tdd_remedies` row with `operation='resume'`, so the audit trail reads in
order: *repair superseded this red (reason…), resume reinstated it (reason…)*.

### What the next run then does

With the confirmed red active again, `run_red_phase` takes its existing
"reused a confirmed red" path — **no RED authoring call is made**. The run then
proceeds through the implementation pass, tests, lint, review, the pre-terminal
gates and the merge.

That still pays for one implementation call against work that is already
finished. Two variants, and the choice is the owner's:

- **(a) Accept it.** The agent is asked to complete the task, finds it complete,
  and says so; the review then judges the real tree. Cost: one cheap call.
  Code: the command only — **no execution-path change at all.**
- **(b) Skip the implementation pass too**, entering at the post-done stages.
  Cheaper by one call, but it needs a resume flag threaded through
  `execute_task`, and it introduces a second entry point into the pipeline —
  the kind of thing that grows its own bugs.

Recommendation: **(a)**. It buys the fix with no new execution path, and if the
extra call turns out to matter, (b) is a later refinement of a working door
rather than a bigger first step.

## 5. Companion: refuse `repair` after green

`tdd repair` should refuse when the lifecycle has passed `green_implementing`,
naming `resume` instead. Repair asks "is this changed test still a red?", which
is a question that has no honest answer once the implementation exists — and
answering it costs a replay and retires the evidence, which is exactly how the
pilot arrived here.

This is a behaviour change to a shipped command: what works today (and produces
a wedge) would refuse tomorrow. Deliberate, and small enough to ship with §4.

## 6. Rejected

- **Teaching the gate to accept a `not_red` lineage whose commit contains the
  implementation** (the issue's first sketch). "The test passes because the code
  is written" and "the test never failed" are the same observation; a gate that
  accepts the first accepts the second, and the red contract stops being
  enforceable. The whole point of §4 is that the evidence already exists — no
  new admissibility rule is needed to use it.
- **Automatic resume** when the tool notices a green with a retired red. A human
  must state that the green is real; the tool's own record is what got confused
  in the first place.
- **A `--force` that skips the three conditions.** Then it is `Mode: standard`
  with extra steps.

## 7. Settled at sign-off (2026-08-13)

**Naming:** `spec-runner tdd resume`, as recommended.

**Execution path:** variant **(a)** — reuse the existing path, introduce no
skip of the implementation pass.

**Claims — the recommendation was wrong, and is overruled.** This document
argued that a claim protects a red only while the implementation is being
written, so a resume need not reinstate it. The owner's counter-example is the
pilot itself, and it is decisive:

```
confirmed RED + claim
→ GREEN modifies the frozen test
→ repair supersedes both
→ resume reinstates only the RED
→ merge, with no byte-lock on the evidence
```

Attempt №3 walked the first two steps for real. Reinstating the checkpoint
alone would make the remaining two a *legal* way to launder exactly the
violation the byte-lock exists to catch — and it would be laundered by the
command built to help. **A claim protects the evidence from the RED until the
terminal gate, not merely during implementation.** That is the correct reading
of what a confirmed red asserts, and this document had it too narrow.

Requirements that follow:

- **Reinstate only the claims of the same lineage** — the ones recorded with
  the checkpoint being reinstated. An unrelated claim retired for its own
  reasons stays retired; resume is not an amnesty.
- **The checkpoint and its claims flip in one transaction**, under the same CAS.
  A resume that reinstated a red and then failed to reinstate its lock would
  leave precisely the state the previous bullet forbids, which is worse than
  refusing.
- **A mismatch between the current bytes and the claimed blob does not block
  the record, but the gate must then refuse with a precise diff.** Preferably
  the command surfaces the conflict in its own preflight, so the operator meets
  it before the record exists rather than after.
- **No automatic acceptance of new bytes.** Ever, under any flag on this
  command. Accepting a changed proof is a different operation, and it has not
  been designed.

Consequence, accepted deliberately: **kapelle must first restore the evidential
test to its claimed bytes**, or go through a separate — and as yet undesigned —
change-of-evidence procedure. That is more honest than finishing the pilot by
quietly removing its principal guarantee.

---

## 8. As built

Two things the implementation added to the signed-off design, both found by
writing the tests:

1. **`--checkpoint`, and a refusal to guess.** A task can hold more than one
   confirmed red. Picking the newest would reinstate the wrong byte-lock along
   with the wrong lineage, so `resume` refuses and lists them — the rule the
   other remedies already follow (F-5).
2. **A conflict exits 2.** The record is allowed when the claimed bytes have
   moved (§7), but a zero exit would promise a merge that the gate will refuse.
   The command prints the paths with claimed-vs-HEAD blobs and exits 2.

One interaction worth knowing, not a defect: the free reuse in §4 depends on the
reinstated checkpoint's `config_hash` still matching current policy. Change a
`POLICY_KEYS` value between the green and the resume and the red will be
re-authored — the gate still passes, so the task completes, but it costs one
call.
