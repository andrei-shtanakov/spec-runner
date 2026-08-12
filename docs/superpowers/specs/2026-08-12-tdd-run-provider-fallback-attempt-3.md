# TDD evidence run — kapelle, provider fallback, attempt 3

**Subject:** `spec-runner==2.28.3` from PyPI, version asserted immediately
before the run. Target: kapelle `master`, `execution_mode: tdd`,
`tdd_runner: exunit`, `review_policy: advisory`, `budget_usd` and
`task_budget_usd` both **1.82** (the owner's remaining authorisation), agent
`claude`/`sonnet`, review on.

**Preflight, captured before spending:** `spec-runner 2.28.3`; `validate` →
`0 errors, 0 warnings`; `preflight --json` → `"verdict": "ready"`, no blockers;
`git status --short` → empty.

**Outcome: the task did not complete, and it is not an evidence run for 3b.**
The pipeline reached GREEN for the first time and was stopped by its own
byte-lock: the implementation edited the test the red had frozen, and the
claims gate refused the merge. The review call died separately, on an account
limit, and is advisory — it did not decide anything.

## 1. Product verdict

**Everything the TDD contract is made of worked, in order and for the first
time end to end with a real agent:**

```
red_authoring → red_verifying → green_implementing → green_verifying
checkpoint a9a0a5a0a1a8   expected_fail   test/kapelle/providers/catalog_test.exs:85
claim      test/kapelle/providers/catalog_test.exs   active
```

The agent wrote a genuine failing test at a real definition line, reported it
in ExUnit's form, the replay confirmed it in an isolated environment, the
checkpoint recorded it, and the claim locked the file. The RED gate let the
implementation proceed **because** a red had been demonstrated.

**Two findings.**

### 1.1 The budget cap was exceeded before it stopped anything

**At least `$2.53` against a `$1.82` cap — and $2.53 is the *observed lower
bound*, not the spend.** The check fires **between attempts**; a single TDD
attempt now makes **three** paid calls (RED authoring, GREEN implementation,
review), so a cap sized in the days when an attempt was one call is no longer a
bound on one attempt. It did prevent a second attempt:

```
Task budget exceeded ($2.53 >= $1.82)
```

That is a real limit doing half its job. The owner set both caps to the exact
remaining authorisation precisely so the boundary would be enforced rather than
trusted, and it was checked after the money was gone.

**The review call's cost is not in that number, and could not have been.**
Reading the code afterwards (`review.py`: a bare `subprocess.run`, no
`parse_cli_result`, no ledger row; `grep cost src/spec_runner/hooks.py` is
empty) shows review spend is recorded **nowhere** — not on the attempt row, not
in `agent_calls`. So `$2.53` is RED + GREEN as the cap saw them, the review call
is unmeasured, and the true total is higher by an unknown amount. This is not a
TDD regression: review has never been counted, in any mode. TDD made an attempt
three calls deep and thereby made the hole visible. With `review_parallel` it
would be one invisible call per role. Filed as part of #213.

### 1.2 The GREEN pass edited the locked file — and the gate refused the merge

The byte-lock's premise is that the implementation cannot change the test that
proves the red. The agent added four more tests to the claimed file between the
red commit and the candidate commit:

```
$ git diff 6c24cd9 b60de77 -- test/kapelle/providers/catalog_test.exs
+    test "parses a declared fallback chain onto the entry" do
+    test "defaults fallback to an empty list when absent" do
+    test "non-list fallback returns {:error, …}" do
+    test "a fallback cycle returns {:error, {:fallback_cycle, [ids]}}" do
```

**And the lock caught it.** I first reported that the gate was never asked,
because the review error happened in the same second and I read the failure as
review's. It was not: the review verdict is advisory and `post_done_hook` only
logs it, the run continued to the pre-terminal gates, and they refused —

```
Pre-terminal gate unsatisfied — not merging
  detail='tdd.claims: claim violated — modified test/kapelle/providers/catalog_test.exs
          (claimed by TASK-101, checkpoint a9a0a5a0a1a8;
           claimed 08d560c92561, found 3cfbf07f76c3)'
```

— followed by the blocked-path bookkeeping commits, which only exist on that
path. No merge, no DONE, the candidate commit kept, the task resumable.

So the seam the last three findings came from is now walked, in the direction
that matters: **an agent violated the byte-lock and the gate refused the
merge.** Slice 2 is enforced end to end on a real project, with a real agent,
from a published artifact.

What remains true is the other half: nothing tells the implementation agent
that the test file is frozen. It behaved reasonably by its own lights — asked
for a fallback feature, it wrote tests for it — and then $1.3 of implementation
became a candidate the gate had to reject. The lock working is not the same as
the pipeline being economical.

## 2. Harness verdict

**The review failure is not spec-runner's**, and it is the reason the run
stopped:

```
=== OUTPUT ===
You've hit your session limit · resets 5:30pm (Asia/Tbilisi)
=== RETURN CODE: 1 ===
```

An account limit on the agent CLI. spec-runner recorded it correctly as a
review *error* rather than a verdict.

No other harness fault: the stand was the published artifact, the version was
asserted, the spec was committed, both budgets were set to the authorised
remainder, and attempts 1 and 2 were preserved (`evidence/task-101-attempt-1`,
`evidence/task-101-attempt-2`, and their state databases) so this attempt began
on a fresh lineage.

**One of my own checks was vacuous again** and I caught it before reporting: a
shell comparison of the claimed file across two commits interpolated the SHA
variable into the path, so both `git show` calls failed and two empty strings
compared equal — printing "IDENTICAL". Redone with braces, the answer was the
opposite, and it is finding 1.2. Third time this class of mistake has appeared
in my own verification in this pilot.

## 3. Phase / checkpoint / remedy lineage

```
phases      red_authoring → red_verifying → green_implementing → green_verifying
checkpoint  a9a0a5a0a1a8  expected_fail
            red      6c24cd9  "TASK-101: red for …catalog_test.exs:85"
            green    b60de77  "TASK-101: Deterministic provider fallback"
            baseline 3695674
            env      runner=exunit;mix.lock=…;elixir=1.19.4;otp=28;mix_env=test;deps_source=…
claims      test/kapelle/providers/catalog_test.exs   active   (violated in b60de77)
remedies    none
```

No `refused:` transition. `done` was never reached.

## 4. Cost — RED against GREEN and review

| stage | cost | measured? |
|---|---|---|
| RED authoring | ~$0.6 (2 min 1 s wall) | yes — `agent_calls` ledger |
| GREEN implementation | ~$1.3 (8 min 33 s wall) | yes — attempt row |
| review | unknown; ran 5 min 45 s, ended on an account limit | **no — recorded nowhere** |
| **observed lower bound** | **$2.53** against a $1.82 cap | |

43 237 output tokens, from the same two measured calls. The RED/GREEN split is
reconstructed from stage timings against the ledger total; the review call is
not reconstructed but simply absent, so the row above says so rather than
guessing a plausible figure. Any total quoted for this attempt is a floor.

That is worth stating twice for the 3b question: "what would another agent call
cost" is exactly the number this evidence set needs, and the instrument cannot
currently answer it for a third of the calls it makes.

## 5. Interventions

**None during the run**, and none after: the working tree is left dirty on the
task branch exactly as the run left it, per the owner's rule that a failed
attempt is not to be finished by hand.

The tree carries the review agent's partial edits (it modifies files as it
reviews and died mid-way): `lib/kapelle/providers/catalog.ex`,
`test/kapelle/providers/catalog_test.exs`, and an untracked fixture.

Bookkeeping behaved as designed (#192): the status flips are committed —
`in_progress → review`, then `review → blocked` — so the spec is not left dirty
by the harness's own writes.

## 6. Post-GREEN debt

**No answer.** GREEN ran and was refused at the gate, so it was never merged
and never reviewed — the review call died on an account limit before producing
a verdict, and would have been advisory anyway. Whether the implementation left
a repeatable class of defect is unknown. The 3b evidence set remains empty.

## What this attempt settles

- The TDD pipeline runs end to end with a real agent on a real Elixir project,
  through the seam that broke in attempts 1 and 2.
- **The byte-lock is enforced end to end**: a real agent modified the claimed
  test file and the claims gate refused the merge, on a real project, from the
  published artifact. That is slice 2's central claim, demonstrated rather than
  unit-tested.
- Two things worth fixing before any further paid run: the budget cap does not
  bound one attempt, and nothing tells the GREEN agent that the claimed file is
  frozen — the second is why $1.3 of implementation became a candidate the gate
  had to reject.
- Observed spend across three attempts: **at least $3.71** against $3.00
  authorised. A floor, not a total: none of the three review calls was ever
  measured, so the overshoot is larger than the number the tool can show.
