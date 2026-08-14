# TDD evidence run 3 — kapelle m2 TASK-105, needs-human hold path with inspectable state

Recorded against the protocol in
[`2026-08-12-tdd-evidence-protocol.md`](2026-08-12-tdd-evidence-protocol.md),
for the #285 trigger. From durable artefacts only — the canonical state domain
`spec/.executor-m2-state.db` in kapelle (`budget_domain_id c9051f20d1104a39`),
that repository's git history, the run's own progress trail, and the prompt
artefacts the run wrote. Unanswerable fields are marked **observability gap**.

Unlike runs 1 and 2, this one was **authorised in advance for a fixed
additional amount and watched from the start**, so its numbers are decisions
rather than reconstructions.

**Task:** a real product task from kapelle's m2 backlog — the needs-human hold
path. Chosen for its bounded scope (no human resume: blocked on impresario#14;
no fault-injection matrix, no LiveView, no real LLM, no second scenario), and
prepared for with a deterministic `golden/needs_human` oracle built **free**
from the pinned producer before any money was authorised (kapelle PR #18).

> **Counts as evidence run for the #285 trigger.** Third of three.

---

## 0. The authorization, and what it bought

Two rows, one decision, in the canonical domain — no copy, no reset, no new
domain:

| id | scope | task | previous | new limit | spend seen | reserve |
|---|---|---|---|---|---|---|
| 10 | task | TASK-105 | $1.82 | **$4.00** | $0.00 | review $1.25 |
| 11 | run | — | $28.00 | **$30.95** | $26.9479 | review $1.25 |

Both absolute, both computed as *spend at the decision + $4.00 of new money* —
not "the remaining budget". Verified after writing: no stage sees $4.00/$30.95,
GREEN sees **$2.75/$29.70** (the reserve withheld), review sees $4.00/$30.95.

**Actual spend: $4.1643**, all priced (`unmeasured_calls = 0`). Domain total
$26.95 → **$31.11**.

| stage | calls | cost |
|---|---|---|
| RED authoring | 1 | **$2.6944** |
| implementation (GREEN) | 1 | **$1.4700** |
| review | 0 | **$0.00 — refused before it started** |

## 1. Product verdict

**The tool did the right thing, first attempt, and the code is right.** One
attempt, 514 s, success. Both terminal gates satisfied, twice each (before the
implementation pass and again pre-terminal):

```
tdd.red     satisfied  red confirmed: test/task_105_red_test.exs:94 at 5a8ca2cd23fb
tdd.claims  satisfied  claims intact
```

The merged diff is three files: `lib/kapelle/product/workers/stage_shell.ex`
(+40/−19), the new `test/task_105_red_test.exs` (+142), and the task's own
status line. The full kapelle suite on the run branch: **444 tests, 0
failures**. The golden oracle was left byte-identical — it is read, never
written.

The change itself is the slice as specified: `needs_human` is treated as a
**hold**, not a final status, so a held loop is re-derived like a running one,
and the second reconcile is `:in_sync` because `Loops.set_status/3`'s own
`WHERE status = 'running'` guard makes the repeat a no-op. The red test at line
94 asserts the hold, the inspectable causal state, the empty queue and the
in_sync repeat — against the prepared `golden/needs_human`, whose `reason` it
compares field by field.

One honest note the agent left in the test, worth carrying forward:
`FixtureAgent.script_from_golden!/0` is hardcoded to the happy path, so the
needs-human script is assembled by hand from the same golden directory. The
generator was parameterised for this run; the fixture helper was not.

## 2. Harness verdict

**Nothing in the stand cost this run an attempt — and it still surfaced two
defects, both in the observability shipped four days earlier.**

Working, visibly, for the first time in production:

- **the review reserve (#267)** — GREEN was guarded at $2.75, review at $4.00;
- **the overshoot announcement (#255 part 1)** — the run stopped with
  `total cost $31.11 > budget $30.95`, on stderr, naming authorization #11 and
  the reserve, and saying *"The work already finished stands; no further paid
  work will start."* The completed task stayed completed;
- **claims released at DONE (#260)** — `Claims released count=1`; the claim on
  `test/task_105_red_test.exs` is `released`, not left `active`;
- **`integration_pr` (#254)** — master was never touched: the merge went into
  `spec-runner/run-20260814-183849` and PR #19 was opened for the human. The
  base branch still reads `⬜ TODO` for TASK-105, and the run said so.

### Defect A — the RED artefact records the question and not the answer

`tdd.py:_log_prompt` calls `log_prompt` and **never** `append_output`. The
other three sites (green, review, `review:<role>`) all append. So:

```
TASK-105-red-…log     3494 B   prompt only
TASK-105-green-…log   5092 B   prompt + === OUTPUT === + RETURN CODE + COST: 1.4699667
```

The most expensive call of this task — **$2.6944**, 27 520 output tokens —
left no record of what it produced. #282 was filed *because the RED pass logged
nothing*; it now logs half. Its stated motivation, "`agent_calls` recorded what
the call cost with no way to see what the money bought", is still unmet on
exactly the path it was filed about.

My rehearsal of 2.33.1 asserted `=== OUTPUT ===` on the green and review
artefacts and, for RED, only that the prompt text was present. It passed
vacuously.

### Defect B — a refused call leaves an artefact shaped like a crashed one

`review.py:539` writes the prompt; the budget guard lives inside
`_run_reviewer`, called at 547. The review here was refused before starting, so
`TASK-105-review-…log` (6881 B) holds a complete review prompt and no output,
no return code, no cost — **byte-shape identical to a call that started and
died**. An operator listing the log directory sees a review prompt for a review
that was never bought, and the artefact cannot tell them which happened.

## 3. Phase / checkpoint / claim lineage

Clean, and short enough to quote whole:

```
red_authoring → red_verifying [test/task_105_red_test.exs:94]
→ green_implementing → green_verifying → refactoring [skipped] → done
```

No remedies. No refusals. No wedge.

Checkpoint `7` — commit `5a8ca2cd23fb`, baseline `8d8e326b2245`, selector
`test/task_105_red_test.exs:94`, outcome `expected_fail`, status `active`,
environment
`runner=exunit;mix.lock=e1635cc8a80509a9;elixir=1.19.4;otp=28;mix_env=test;deps_source=133ba0aa6237`.

Claim: `test/task_105_red_test.exs` @ `6cfc7947eb79` → **released**.

The RED file is `test/task_105_red_test.exs` — the adapter's own name (#252
variant D), a file that did not exist at `baseline_sha`, discovered by
ExUnit's ordinary `test/**/*_test.exs`. The claim therefore covered only the
new file, and the green appended nothing to a frozen one. Run 1's whole
category of failure did not recur, by construction.

## 4. Interventions

**Two, both before the run, neither during it:**

- the two budget authorizations above, each with an actor and a written reason;
- the free preparation of `golden/needs_human` and the parameterisation of
  `scripts/gen_golden.sh` (kapelle PR #18) — so the oracle predates the money.

During the run: none. No remedy, no manual commit, no restoration.

## 5. Post-GREEN observation

**The task completed its full post-GREEN contour except review, which was
refused for lack of money — for the third task running.**

Recorded verbatim:

```
review  not_run  Task budget reached before the review call ($4.16 >= $4.00)
```

The arithmetic is the finding. The reserve withholds $1.25 from what GREEN's
*guard* may see, but the guard only refuses the **next** call: RED ended at
$2.6944, just under the $2.75 GREEN ceiling, so GREEN started and ran to
$4.1643. Overshoot $0.16 — bounded by one call, exactly as documented, and one
call was $1.47.

So a reserve smaller than one typical call cannot protect review. It is not a
defect in #267 — the guarantee it makes, it kept — but it is a limit worth
writing down: **the reserve buys review a chance only when the ceiling leaves
exec more headroom than a single call.** Here $4.00 was undersized for a task
whose RED alone cost $2.69.

Against the protocol's table this run reads as **"no post-GREEN defect
observed"**: tests pass, lint clean, both gates satisfied, the merged diff is
40 lines of one module and a new test file, and no duplication, dead
abstraction or stale naming attributable to the red is visible in it. The one
piece of debt the run left — the hardcoded happy-path fixture helper — is
*test-support* debt the agent itself named, not something a refactor pass over
the diff would have found.

Human review of the merged code is still pending: PR #19 is open, and this
task's review was never bought. **That absence is itself the observation** —
two of three evidence runs merged without any machine review at all.

## 6. Reading for #285

Three completed runs, and none of them argues for an automatic REFACTORING
pass:

| run | post-GREEN debt found | by what |
|---|---|---|
| 1 — TASK-101 | none in the code; the tool's own door was broken (#252) | the harness failing |
| 2 — TASK-104 | one correctness bug (JSON-safe rejection reasons) | a **human** review after the merge |
| 3 — TASK-105 | none | tests, lint, gates |

The evidence points the other way, consistently: **the stage that is missing is
the one that already exists and cannot be afforded.** Run 2's only real defect
was caught by review-after-the-fact; run 3 could not buy review at all. Adding
a fourth paid stage to a task whose third one is refused would make that worse.

**Trigger not met. 3/3 runs recorded, none supporting the change.**

## Tool versions per stage

Answerable for the first time, because this run was a single invocation of a
known binary: **spec-runner 2.33.2** throughout, asserted before the run
started. Still an **observability gap** in the artefacts themselves — no table
records it; the attribution here rests on the run being one process of a
version verified at the shell.
