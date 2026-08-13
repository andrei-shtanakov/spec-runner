# What a claim protects — design

**Status:** design only. No code ships with this document. **Pending owner sign-off.**
**Issue:** #252 (F-30), the door behind #232/#249/#253.
**Related:** [post-green resume](2026-08-13-post-green-resume-design.md) (#232, whose sign-off settled that a claim runs to the terminal gate), [claim and remedy contracts](2026-08-11-claim-and-remedy-contracts.md) §1.5–1.6

---

## 1. What happened

`tdd resume` reinstated the checkpoint and its claim, warned that the claimed
bytes had moved, and the operator restored the file. The gate still refused:

```
⛔ RED not confirmed, refusing to implement: claim violated — modified
test/kapelle/providers/catalog_test.exs (claimed 08d560c92561, found 3cfbf07f76c3)
```

| version | blob | who wrote it |
|---|---|---|
| red commit `6c24cd9` | `08d560c92561` | the RED pass — **the claim** |
| green commit `b60de77` | `3cfbf07f76c3` | the implementation, appending four tests |
| review-fix `98e16ab` | `ecc2e2586e08` | the review agent, appending four more |

The green appended tests to the same file the red had frozen, and the pipeline
allowed it: the byte-lock is checked at the **RED gate** and at the
**pre-terminal gate**, and between them the implementation pass ran, committed,
passed tests and lint. So after a resume, **no tree containing the
implementation as committed can satisfy the reinstated claim**. The door #232
opened leads to a wall the task's own green built.

The operator's manual resolution, for the record: restore the claimed file to
the red bytes exactly, and move the eight appended tests verbatim into a
separate file. That worked, and it is a hint about the shape of the fix.

## 2. What the claim is actually for

From #232's sign-off, in the owner's words: *a claim protects the evidence from
the RED until the terminal gate.* The thing being protected is **the
evidential test** — the one whose failure was replayed and recorded. What the
implementation is forbidden to do is weaken, delete, rename or neuter that
test, because the red is the only proof the work was driven by a failing test.

The current instrument freezes **the whole file** the test lives in. That is
coarser than the contract in one direction (it forbids things the contract does
not: adding a *different* test) and, notably, not looser in any — so it is a
sound over-approximation that has now met a case where the over-approximation
bites.

Two facts about the case matter:

- appending a new test to the same file **does not weaken the evidence**; and
- the pipeline actively invites it, because a project's convention is usually
  one test file per module, and the implementation's own tests belong there.

## 3. Candidates

**A. Freeze the test, not the file.** The claim records the evidential test's
own source span, extracted by the runner adapter that parsed the selector. The
file may grow; the frozen test may not change.

- The adapters can already do most of this: `ExUnitAdapter.definition_lines`
  parses the file with Elixir's own parser to prove a `path:line` defines a
  test, and pytest node ids can be resolved with Python's `ast`.
- Cost: a per-runner "extract this test's source" operation, and a decision
  about what counts as a change (bytes of the span? the AST? does
  reformatting count?).
- Risk: a span moves when text above it changes, so the record has to key on
  something stable (the node id / definition name), and comparison has to
  re-extract rather than re-read a line range.

**B. The claim ends at green.** After the green gate, the evidence is the red
*commit*, which is immutable in history; the working tree may then diverge.

- Cheapest, and wrong on its own terms: #232's sign-off rejected exactly this.
  It reopens the laundering chain — a test weakened after green and before
  merge would pass every remaining check.

**C. The green's commit re-claims.** At the pre-terminal gate, if the
evidential test still *behaves* as the red did (re-replay it against the red's
parent), the claim is updated to the candidate's bytes.

- Principled: it tests the property the claim stands for rather than the bytes.
- Expensive: another replay per task, in a disposable worktree, and a new
  failure mode when the replay itself cannot run (the #245 class).
- Also subtle: re-claiming *at* the pre-terminal gate means the merge candidate
  is judged against a claim derived from itself. The re-claim would have to
  happen at the green checkpoint, before review, or the check is circular.

**D. The evidential test lives in its own file.** The RED pass writes it to a
dedicated path (e.g. `<module>_red_test.exs`); the byte-lock freezes that file,
and the implementation's own tests go where they always went.

- The instrument stays exactly as it is — a whole-file byte-lock, which is the
  cheapest thing that cannot be fooled.
- It is what the operator did by hand to get out.
- Cost: it constrains the RED prompt and the project's layout, and a project
  whose test discovery is path-sensitive needs the path to be discoverable
  (both pytest and ExUnit are fine with a second file in the same directory).
- Honest downside: the evidential test is separated from its neighbours, so a
  human reading the suite finds the module's tests in two places. For the
  duration of a task; after the merge, nothing stops a follow-up from moving it.

## 4. Recommendation

**D now, A later, never B.**

D is the only candidate that needs no new instrument, and the instrument is the
part that must not be clever: a whole-file byte-lock cannot be fooled by
formatting, by moved lines, or by an AST that parses differently in two
versions of a runner. It converts the conflict from "the lock is wrong" into "a
convention keeps the evidence apart", and the convention is one line in the RED
prompt plus a check that the RED pass obeyed it.

A is where this should end up, because the contract is about a *test*, not a
file — but it is a real piece of per-runner machinery, and building it under
the pressure of an unwedged pilot is how instruments acquire subtle bugs. It
also composes with D rather than replacing it: with the evidential test in its
own file, span-level freezing is a refinement, not a rescue.

C stays on the shelf: it is the most principled and the most expensive, and its
circularity (§3) needs care that only matters if D and A both prove
insufficient.

B is rejected on the same grounds the sign-off rejected it.

## 5. What D means concretely

1. The RED prompt asks for the failing test in a **new file** named by
   convention from the selector (adapter-specific: pytest
   `tests/test_<x>_red.py`, ExUnit `test/<x>_red_test.exs`).
2. `run_red_phase` refuses a selector that points into a **pre-existing** file
   — the same shape as its existing refusals (a non-node-id selector, a
   composite `test_command`, an unknown SHA), and for the same reason: it is
   cheaper to refuse than to freeze a file the implementation will legitimately
   need. "Pre-existing" is decidable without heuristics and without the working
   tree: the checkpoint already records `baseline_sha`, so the question is
   `git cat-file -e <baseline_sha>:<claimed path>` — did this file exist before
   the red wrote it.
3. Nothing else changes. `claim_paths_for` still claims the selector's file,
   `check_claims` still compares blobs at the candidate commit, and the resume
   path from #232 keeps working — the claim it reinstates is now on a file
   nobody else writes.
4. Migration: an existing checkpoint whose claim is on a shared file keeps
   working exactly as today; the refusal in (2) applies to reds authored after
   the change. The pilot's TASK-101 is already resolved by hand along these
   lines.

## 6. Open questions for sign-off

1. **Is the separate-file convention acceptable for kapelle's Elixir suite**,
   where `test/kapelle/providers/catalog_test.exs` is the module's home? The
   red would live in `catalog_red_test.exs` beside it.
2. **Does the RED refusal in §5.2 apply to a selector in an existing file, or
   only to one in a file with tests it did not write?** The first is simpler to
   state and to enforce; the second is friendlier to a project whose red
   legitimately extends a file the task itself created earlier.
3. **Does A get scheduled now or on evidence?** My recommendation is on
   evidence: if a project's layout makes D awkward in practice, that is the
   trigger to build span-level claiming rather than to guess at it.
