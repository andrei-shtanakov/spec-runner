# Review policy — design

**Status:** design only. No code ships with this document.
**Issue:** #157 (remainder of #138 + item 4 of #134)
**Depends on:** #164, the pre-terminal gate mechanism — this is its first consumer.
**Related:** [checkpoint + pre-terminal gates](2026-08-11-checkpoint-and-pre-terminal-gates-design.md), [TDD lifecycle](2026-08-11-tdd-lifecycle-design.md)

---

## 1. What is already decided

The owner settled the policy. It is recorded here rather than re-opened, because
a decision that lives only in a conversation is not a decision anyone else can
find.

**`review_policy: advisory | required`, default `advisory`** — so existing
projects see no change.

Under `required`:

| Verdict | Effect | Why |
|---|---|---|
| `failed` | **blocks** | review found issues; that is the whole point |
| `not_run` | **blocks** | the review did not happen. "I don't know" is not "fine" — this is the #138 defect one level up |
| `error` | bounded retry, then **infrastructure error** | the instrument broke. Not a defect in the work, and not NEEDS_HUMAN |
| `passed` / `fixed` | proceeds | `fixed` is a kind of pass, per slice 0's reading |
| `skipped` | config error, or an explicit operator waiver | see §5 — silently allowing it is how `required` becomes decorative |

Lifecycle:

```
execute
→ deterministic gates (tests, lint)
→ review checkpoint commit          ← the tree review judges
→ review
→ fixes as separate commits on top  ← only when the verdict is `fixed`
→ deterministic gates again
→ merge candidate                   ← the tree the gate judges
→ review policy satisfied
→ merge → DONE
```

The review checkpoint commit is **kept and not merged**, and does not mean DONE.
HITL remains a separate authority stage: it is a human deciding, not an
instrument reporting, and conflating the two is what `WAIVED` exists to prevent.

### 1.1 Two SHAs, and why they need different names

Ambiguity here is not pedantry — the whole of §2.1 is about the gap between
them, and "checkpoint SHA" was being used for both.

| Term | What it is |
|---|---|
| **review checkpoint** | the commit made before review (#103), so review has a stable tree and the work keeps its provenance |
| **merge candidate** | HEAD at the pre-terminal gate: the review checkpoint plus any fix commits. Equal to the review checkpoint when review changed nothing |

`GateContext.checkpoint_sha` in the #164 mechanism carries the **merge
candidate** — the tree whose merge the verdict authorises. It is named for the
gate's checkpoint, not review's. Where this document says "checkpoint SHA"
without a qualifier, it is a bug in the document.

---

## 2. What implementation surfaced that the decision does not cover

Three questions that only appear once the gate mechanism is real. Each has a
recommendation; none is a re-litigation of §1.

### 2.1 The gate judges a different tree than the review did

The pre-terminal gate is evaluated against the **merge candidate**. Review ran
earlier, against the **review checkpoint**, and any `fixed` verdict means the
tree moved between the two. Binding the review verdict to the merge candidate
alone would state that review approved a tree it never saw.

That is exactly the staleness criterion 5 of #164 exists to prevent, so it
cannot be waved through.

**Recommendation.** Record both, and let them differ honestly:

- `GateContext.checkpoint_sha` = the **merge candidate**. It is what the verdict
  authorises, and what a later run compares against to decide staleness.
- the gate's `detail` carries the **review checkpoint** SHA and the verdict.

The claim then reads: *"at merge candidate X, the review policy is satisfied on
the basis of a review of review-checkpoint Y, whose fixes are the commits
between Y and X, after which the deterministic gates passed again."* The gap
between Y and X is not hidden; it is bounded by the re-run of tests and lint
that already happens after review fixes (#65). Pretending Y and X are the same
SHA would be the dishonest option.

Note what this does **not** weaken: staleness is still judged on X. A later
commit makes the verdict stale exactly as it would for any other gate — the
recorded Y only says what the verdict was based on, never what it covers.

### 2.2 How the gate learns the verdict

Slice 0 already writes the review outcome into `phase_results`, so the gate
could read it back. It should not.

`record_phase` is **best-effort**: a storage failure is logged and swallowed,
because bookkeeping must never fail a task. Reading a blocking decision out of
it conflates two different facts — "review produced no verdict" (`not_run`,
which blocks) and "we could not read our own note about the review" (an
instrument error). The first is about the work; the second is about us.

**Recommendation.** `GateContext` grows `facts: dict[str, object]` — per-
evaluation observations the call site passes in directly. The review gate reads
`facts["review_verdict"]`. A missing key is an instrument error, not `not_run`:
the site failed to report, which is our bug and must not be laundered into a
verdict about the code.

This is a small addition to the #164 mechanism, and the right kind: the first
consumer is what reveals whether a seam is usable.

### 2.3 Where the `error` retry lives

"`error` → bounded retry" reads naturally as retrying the gate. It must not be.

`run_code_review` can *apply fixes*, and a fix is a commit. A gate that re-runs
review would therefore move the tree while judging it — breaking #164's
criterion 4 (a gate never edits history) and the test that pins it.

**Recommendation.** The retry belongs to the instrument, not to the reader. A
bounded re-run of review on `error` lives in the review stage
(`review_error_retries`, default 1); the gate stays pure and reads whatever
verdict finally came out. If that verdict is still `error`, the gate returns
`instrument_error`, which the mechanism already turns into an infrastructure
error after its own bound is spent.

Splitting it this way keeps one property worth keeping: **evaluating a gate
never changes what is being evaluated.**

---

## 3. `skipped` under `required`

`run_review` defaults to `true`, so `skipped` under `required` means someone
turned review off while demanding it. Two paths, and the choice matters:

- **Detect it at `validate` time as a config error.** `review_policy: required`
  with `run_review: false` is contradictory on its face, and the honest moment
  to say so is before a run starts, not at the merge gate after the work is
  done.
- **An explicit operator waiver** — the `phase_waivers` mechanism from slice 0,
  which requires an actor and a reason and leaves the observed outcome intact.

Not offered: silently proceeding. A `required` policy that lets `skipped`
through is decorative, and decorative gates are worse than no gates because
people trust them.

---

## 4. What this closes

**#134, item 4.** TASK-011's review died with an execution error and the task
still closed as "No-op: completed without changes", with the artifact merged
from the review checkpoint — a failed review masked by a successful completion.

Both halves are addressed and neither alone would be enough: #138/#156 made the
verdict honest (`error`, not silence), and this makes the lifecycle respect it.
Diagnostics never could have fixed this.

---

## 5. Non-goals

- **Changing `advisory` behaviour.** Default stays; a project that does not opt
  in cannot tell this shipped (#164 criterion 8).
- **Replacing HITL.** Separate authority stage, untouched.
- **Owning the lifecycle.** #157 is a *consumer* of #164, per the owner's
  amendment. TDD (#141) registers alongside it, not inside it.
- **Touching `--json-result`.** New state is additive.
- **Retry policy generally.** #140 is closed; `TASK_BLOCKED` stays terminal.

---

## 6. Open

1. Whether `review_policy` should also accept a per-task override, the way
   `execution_mode` will (#141 amendment 4). Nothing here forecloses it; adding
   it later is additive.
2. Whether a blocked task should be retryable in the same run or only on the
   next invocation. Current behaviour — an unsatisfied gate takes the existing
   "attempt did not succeed" path — makes it the latter by default.
