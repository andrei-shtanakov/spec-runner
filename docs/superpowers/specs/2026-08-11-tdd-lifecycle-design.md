# TDD lifecycle as an executor contract — design

**Status:** approved with amendments (owner, 2026-08-11). Design only — no code
ships with this document.
**Issue:** #141 (D7-A, from the disputatio pilot)
**Owner decision this follows:** accept #141 as a *design track*, not a minor
release. Build the general phase-result contract first, then design the full
lifecycle, then implement in vertical slices.

---

## 1. Why this exists

The pilot ran 100 leaf tasks under TDD discipline that had to be built
**outside** spec-runner: a `tdd-gate` plugin plus a ~1500-line external script.
The plugin works. Every place it strains is the same place:

> A task has no phases, so "the test is written and confirmed failing" has
> nowhere to live except bespoke evidence files on disk.

The pilot wrote `spec/.tdd-evidence/{claims,verdicts,waivers}/<ns>/` by hand
because the state contract knows nothing about phases. That is the gap.

This is not a request for a TDD feature. It is a request for a **lifecycle
contract** an executor can enforce, of which TDD is the first consumer.

### Why it is not one minor release

`execution_mode: tdd` adds a state machine, durable phase checkpoints, a new
evidence model, replay, operator remedies, state migration and new terminal
semantics. Each is a contract change with its own blast radius. Shipping them
together would mean a failure anywhere is a failure everywhere, and the pilot's
own experience is that the interesting failures are at the seams.

---

## 2. Part A — the phase execution result contract (TDD-independent)

Ships first, on its own, useful without TDD.

### 2.1 What exists today

`stages.py` already names the phases of a task:

```
sync_deps → branch → exec → parse → tests → lint → commit → merge → review
```

They have **no typed result**. `StageReporter.enter(name)` records where we
are, `error_stage` records where we died — and that is the whole vocabulary.
A stage either fell over or it did not.

One phase already grew a real result vocabulary, under pressure: `review`, in
#138, because "no verdict" was being recorded as `passed`. It landed on

```
passed | failed | not_run | error       (+ fixed, skipped)
```

That was not designed as a general contract, but it is one — and the same
distinctions the pilot needs.

### 2.2 The vocabulary

```
PASS             ran; its expectation held
EXPECTED_FAIL    ran; failed exactly as it was supposed to
UNEXPECTED_FAIL  ran; failed some other way
NOT_RUN          ran, but produced no usable verdict
ERROR            could not run — the instrument itself broke
SKIPPED          deliberately not executed
```

Six, because each one implies a different move by whoever reads it: proceed;
proceed (in TDD); fix the work; investigate the agent; fix the environment;
nothing to do.

**`EXPECTED_FAIL` vs `UNEXPECTED_FAIL` is the load-bearing distinction.** A test
that fails because of a typo in an import looks exactly like an honest red, and
without the split it *becomes* one: the harness records "the test failed as
required" and lets the task proceed to green against a test that never
exercised anything. The pilot hit this and it is the reason the split is first
in their list.

**`NOT_RUN` is not a nicety, it is the whole point of #138.** A phase that
executed and produced nothing usable — a timeout, an empty response, output
with no verdict marker — is not a pass and not a failure. Recording it as
either is precisely the defect that shipped in #138: silence was being written
down as `passed`. Generalizing the vocabulary while dropping `not_run` would
re-open that hole for every other phase (Copilot, PR #162).

The name is inherited from the shipped `ReviewVerdict` value rather than
invented here. It is imperfect — the phase *did* run — but a second name for a
value already in the state DB and in `docs/state-schema.md` would cost more
than the imprecision.

**`ERROR` vs `NOT_RUN`**: the instrument broke, versus the instrument ran and
said nothing. Different fix, different owner. Collapsing them makes the
operator debug the agent when the CLI is missing, or the environment when the
model rambled.

**`SKIPPED` vs `NOT_RUN`**: deliberate versus disappointing. A phase disabled by
config, or unreachable because an earlier phase already failed, is a
non-event — it must not read as a gap in the evidence.

### 2.2.0 Not every stage supports every outcome

`PhaseOutcome` is the **base** vocabulary, not a set every stage must
implement (owner amendment 2). The admissible outcomes are declared **per stage
type**, and a stage producing one outside its set is a bug, not a surprise.

`EXPECTED_FAIL` is the clearest case: it is meaningful for RED verification and
meaningless for `commit` — nothing about a commit is supposed to fail. Declaring
the per-stage set makes that a checkable property instead of a convention, and
gives consumers something to switch on without a catch-all branch.

### 2.2.1 Convergence with what already exists

Two surfaces already carry most of this vocabulary; the contract has to absorb
them rather than become a third:

| Phase result | `ReviewVerdict` (#138) | `preflight` status (#142a) |
|---|---|---|
| `PASS` | `passed` (also `fixed`) | `ok` |
| `UNEXPECTED_FAIL` | `failed` | `empty` (an empty suite is a real, wrong answer) |
| `NOT_RUN` | `not_run` | `unavailable` |
| `ERROR` | `error` | `broken`, `missing` |
| `SKIPPED` | `skipped` | `skipped` |
| `EXPECTED_FAIL` | — (review never expects failure) | — |

That the two arrived independently at the same three-way split — passed /
could-not-establish / broken — is the main evidence that this shape is right
and not invented for TDD.

**Convergence requirement (owner amendment 3):** `ReviewVerdict` does **not**
stay a parallel enum. Review reports the shared outcome plus a
review-specific detail:

```
outcome: PASS
detail:  passed | fixed
```

So `fixed` stops being a peer of `passed` at the phase level — it is what kind
of pass it was — and every consumer that only cares whether the phase held can
read `outcome` and stop. The wire values already in the state DB and in
`docs/state-schema.md` keep working; the migration is that they are read as
`(outcome, detail)` rather than as one flat enum.

### 2.3 `WAIVED` is not a result

The pilot lists `WAIVED` alongside the four. It must not be a fifth value of
the same enum.

A result is **what the instrument observed**. A waiver is **an operator
overriding what the instrument observed**. Collapsing them destroys exactly the
information a waiver exists to preserve — that a human, identifiable, at a
known time, for a stated reason, decided to proceed anyway.

Shape:

```
PhaseResult   observed, written by the harness, never by a human
Waiver        { phase, task, waived_result, reason, actor, timestamp, provenance }
```

A waived phase keeps its observed result **and** carries a waiver record. Any
report that shows "green" for a waived phase must show it as waived. Only an
operator can create one; the agent never can.

### 2.4 Where results live

In the state DB, as an **append-only** history per (task, phase): a phase can
run more than once and the earlier verdicts are evidence, not noise. Not in
bespoke files under `spec/` — that is what the pilot was forced into.

This is additive to the interop contract (new table, `schema_version` bumped in
`docs/state-schema.md`); `--json-result` is untouched by Part A.

### 2.5 Acceptance for Part A

- Every existing stage records a typed result; `standard` mode behaviour is
  **byte-identical** (the results are additive record-keeping, nothing gates on
  them yet).
- `review` uses the shared vocabulary or is documented as a refinement of it,
  and `not_run` keeps meaning what it means today — the generalization must not
  quietly drop the distinction #138 was built to introduce.
- A waiver cannot be created by an agent, and a waived phase is never displayed
  as a plain pass.

---

## 3. Part B — the TDD lifecycle

Design only. Depends on Part A.

### 3.1 Mode

`execution_mode: standard | tdd` — **project-level default with an optional
per-task override** (owner amendment 4).

The checkpoint records the **effective mode and the hash of the config that
produced it**. Without that, a checkpoint written under one policy is
indistinguishable from one written under another, and replay silently
re-interprets old evidence under today's rules — which is the same class of
error as re-baselining a harness snapshot between attempts (#137).

**The `standard`-mode guarantee, stated precisely** (owner amendment 1):
**execution, terminal state and external contracts do not change.** Not
"byte-identical" — Part A adds append-only rows to the state DB, which makes
byte identity impossible by construction, so promising it would be a promise
we break on day one.

Concretely, for a project that does not opt in: the same commands run in the
same order, a task reaches the same terminal state for the same reasons, and
`--json-result`, `status --json` and the exit-code surface are unchanged. New
phase-result rows are additive record-keeping and nothing gates on them.
Enforced the way C1 was — a golden no-observable-change test.

### 3.2 Phases

```
READY → RED_AUTHORING → RED_VERIFYING → GREEN_IMPLEMENTING → GREEN_VERIFYING
      → REFACTORING → DONE
```

The transition into `GREEN_IMPLEMENTING` is **forbidden without a confirmed
red**. Confirmed means: the selector was actually executed and failed in the
expected way — `EXPECTED_FAIL`, not `UNEXPECTED_FAIL`, and not "the agent said
it failed". An agent's report of its own red is exactly the evidence this
lifecycle exists to replace.

### 3.3 Durable checkpoints

A checkpoint is not a flag. It is:

```
(commit SHA, selector, baseline SHA, namespace)
```

Each component earns its place from the pilot:

| Component | Why |
|---|---|
| **commit SHA** | without it replay is impossible, and without replay "red confirmed" is trust in the agent's report |
| **selector** | the *full* pytest node-id, not a test name: `-k` matches several, and a checkpoint that matches several proves nothing about the one |
| **baseline SHA** | red *against what*; without it nobody can say what was actually demonstrated |
| **namespace** | after several branches merge into one integration branch, identical `TASK-NNN` ids from different workstreams collide. This nearly restored one task's claim from another's honest red commit; the pilot closed it with a fourth trailer |

### 3.4 Evidence

Phase verdicts and checkpoints live in the state DB (Part A), append-only.
The pilot's `spec/.tdd-evidence/...` tree exists only because there was
nowhere else; it should not be reproduced as a design.

### 3.5 What the pilot learned that the contract must not omit

1. **Byte-immutability covers every claimed file, not just the current
   task's.** Their first version checked only the current selector, so the lock
   on neighbouring tests was held by prompt text rather than by the instrument.
   A rule enforced by asking nicely is not enforced — same shape as the
   harness-guard bypass (#137), where the barrier held once and a retry removed
   it.
2. **Typed operator remedies.** Without them, a mistake in an already-frozen
   test is fixed by rewriting history. That happened twice in one phase, each
   time with a state freeze and a second signature. Two commands close it:
   - `abandon` — this red is void, start again honestly; the commit stays in
     history;
   - `repair` — editing the locked file is legitimate here, its bytes are
     accepted with provenance.
   Both write a **record**. An explanation in a commit body is not readable by
   the instrument, and the instrument is the thing that has to decide.
3. **`red` must lint the file it freezes.** After the checkpoint the file is
   byte-immutable, so lint debt inside it is unfixable without an operator and
   hits every subsequent task in the same suite. One I001 trap fired three
   times in wave 1.
4. **Type checking belongs in the per-task gate.** The pilot had pyrefly only
   in the acceptance checklist; the debt accumulated for 22 tasks and surfaced
   on files that were by then locked.
5. **A deliberate refusal is not retried.** Shipped already as `TASK_BLOCKED`
   (#140) — in `tdd` mode it is critical rather than merely correct, because
   every escalation there is structural.

### 3.6 Prerequisite (not owned by review)

`post_done` fires **after** commit/merge, so a phase check cannot sit before the
commit. For TDD that is fatal: `RED_VERIFYING` must gate the commit, not follow
it.

The mechanism this needs — **checkpoint commit + pre-terminal policy gates** —
is its own thing, tracked as **#164**, and *not* a part of the review policy
(owner amendment 5). Review (#157) and TDD (#141) are its two **consumers**.

That separation is not tidiness. If the prerequisite lived inside #157, a later
edit to `review_policy` would silently be an edit to the TDD contract, and the
two have different owners and different reasons to change.

Shared shape, from #164:

```
execute → deterministic gates → checkpoint commit → policy gates
→ fixes as separate commit → repeat deterministic gates
→ policy satisfied → merge → DONE
```

The checkpoint commit is kept — it exists for a stable SHA and provenance,
which TDD needs even more than review does — but it is **not merged and does
not mean DONE**, and a task whose policy is unsatisfied stays **resumable and
non-terminal**.

**#141 cannot be implemented before #164 exists.** #157 is a sibling consumer,
not a gate on this track.

### 3.7 Open questions — resolved (owner, 2026-08-11)

- **Replay isolation.** A **disposable worktree with an environment identified
  by lockfile hash** — not an arbitrary shared `.venv`. The pilot shared the
  project venv and had to substitute commands with module constants because the
  temp repo was not a uv project; that makes the replay environment
  unidentifiable, and a replay you cannot identify proves nothing about the run
  it claims to reproduce.
- **Mutation-probe threshold.** **Out of TDD v1.** A confirmed RED is
  mandatory; additional mutation testing becomes a separate policy. (Related
  boundary: certification by breaking something lives in a disposable worktree
  and is its own untriggered track — see #159, closed.)

---

## 4. Vertical slices

Each slice is independently mergeable and independently useful. Order matters:
every later slice depends on the earlier ones being real.

| # | Slice | Done when |
|---|---|---|
| 0 | **Phase result contract** (Part A) | every stage records a typed result; `standard` byte-identical; waivers are operator-only records |
| 1 | **RED checkpoint** | `(SHA, selector, baseline, namespace)` persisted and replayable; green refused without a confirmed `EXPECTED_FAIL` |
| 2 | **Immutable claimed files** | byte-lock across *all* claimed files, enforced by the instrument; red lints the file it freezes |
| 3 | **Operator remedies** | `abandon` / `repair` write typed records with provenance; history is never rewritten to fix a frozen test |
| 4 | **GREEN / REFACTOR** | the remaining transitions, per-task type check in the gate |

**Slice 0 ships separately, as general hardening** (owner ruling): not as part
of this track. It has no TDD surface at all, and if the lifecycle is never
built it is still worth having — which is the test of whether the split is
real. The order of the remaining slices stands as written.

---

## 5. Non-goals

- Replacing the pilot's plugin. The contract should make it unnecessary, but
  migrating it is theirs.
- `bootstrap` / scaffolding, and the mutation probe as a working-tree
  diagnostic: both sit in #159, and PR #158 deliberately shipped `preflight`
  without the probe — certification by breaking something belongs in a
  disposable worktree.
- Changing `standard` mode in any observable way.
- `--json-result` changes. Part A adds state; the Maestro-facing payload is
  frozen until there is a reason and a major bump.

---

## 6. Decisions taken (was: open for the owner)

Answered 2026-08-11; kept here so the reasoning is not lost:

1. **Slice 0 ships separately**, as ordinary hardening of the existing stages,
   not as part of the TDD track.
2. **`execution_mode`** is a project-level default with an optional per-task
   override; the checkpoint records the effective mode and the config hash.
3. **`ReviewVerdict`** becomes `outcome` + review-specific `detail`, not a
   parallel enum.
4. **Replay** uses a disposable worktree with a lockfile-hash-identified
   environment; the **mutation probe** is out of v1.

Still open, deliberately: nothing in this document. The next decision point is
#164 (the prerequisite), which this track consumes rather than owns.
