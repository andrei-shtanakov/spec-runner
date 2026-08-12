# Free rehearsals — the byte-lock under a disobedient GREEN, and the budget guard

**Subject:** the spec-runner working tree at `d2bb389` (#215, #216, #217 merged;
nothing released yet), run from a disposable kapelle clone. **No money was
spent**: the agent is a scripted stand-in named `claude`, which is what makes
its JSON — and therefore its *cost* — parseable by the same code path a real
claude run uses.

Two rehearsals, both required before any further paid run:

| # | claim under test | verdict |
|---|---|---|
| 4 | GREEN edits the claimed file → the early gate stops the run, review is never called, review spend is zero | **pass**, after one product finding |
| 5 | RED + GREEN exhaust the cap → the review subprocess never starts | **pass**, after fixing a defect in the guard itself |

Harness: `scratchpad/rehearsal2/` — `setup.sh` (clone, drop the remote, reset
to the pre-pilot commit, write the config), `bin/claude` (the stand-in, logging
every prompt it receives), `evidence.py` / `evidence_budget.py` (the chains
below, item by item).

## 1. Rehearsal 4 — a disobedient GREEN

The stand-in deliberately disobeys: told the test file is frozen, it adds a
test to it anyway. That is the point. A lock only ever exercised against agents
that comply is not exercised at all.

```
🔴 RED: authoring a failing test
🔍 RED: replaying test/kapelle/providers/catalog_test.exs:111
⏳ stage: exec  →  Tests passed  →  Committed changes
Claim violated before review — not reviewing, not merging
  detail='tdd.claims: claim violated — modified test/kapelle/providers/catalog_test.exs
          (claimed by TASK-101, checkpoint a74f39ed7359;
           claimed 5b6913ea7258, found 10842a1d1581)'
```

Every item of the chain, from the artefacts rather than from the log:

```
PASS  the GREEN prompt carries the frozen-files block
PASS  it names the claimed file
PASS  it names the operator route out            [TASK_BLOCKED + "operator"]
PASS  the agent was called exactly twice         [red → green]
PASS  no review call was ever made
PASS  the ledger holds one row per call and no review row   [red_authoring=0.6]
PASS  review spend is zero because review never ran
PASS  exactly one red checkpoint, confirmed and active
PASS  the claimed file is locked
PASS  lifecycle reached red_authoring / red_verifying / green_implementing
PASS  it never reached green_verifying
PASS  the run is still on the task branch — nothing merged
PASS  master is untouched
PASS  the candidate commit is kept, not rolled back
PASS  tasks.md does NOT record this task DONE    [🔄 IN_PROGRESS]
PASS  the claimed file really did change between the red and the candidate
```

The last line is read with `git show <sha>:<path>` against both commits — the
check that was vacuous in the paid pilot, when a shell variable interpolated
into the path made two failed commands compare equal and print "IDENTICAL".

**What this settles that attempt 3 did not.** Attempt 3 also ended at the claims
gate, but *after* paying for a review that then died on an account limit. Here
the same violation is caught **before** the review call: the seam #214 added is
walked end to end, and the money it saves is measured as zero rather than
argued for.

## 2. Rehearsal 5 — the budget guard, and the defect it found

The first run **failed the rehearsal**, which is what a rehearsal is for.

```
✅ Code review passed
Task budget exceeded ($1.80 >= $1.00)
```

$1.80 against a $1.00 cap: three calls at $0.60 where the guarantee allows two.
The guard read spend from the database, and `record_attempt` — which carries the
GREEN call's cost — runs **after** `post_done_hook` returns. So at the review
site the tool knew about $0.60 (the RED call, ledgered immediately) and was
blind to the $0.60 it had just spent on the implementation.

That is the guarantee broken by exactly the call it was written for. Fixed by
handing the hook what the attempt has already spent but not yet recorded
(`pending_cost`), with `None` — an unknown amount — treated as an unprovable
remainder rather than as free. Re-run:

```
Review produced no verdict — this task was not reviewed
  reason='Task budget reached before the review call ($1.20 >= $1.00) — not starting it'
```

```
PASS  the agent was called exactly twice — RED and GREEN
PASS  no review subprocess was ever launched
PASS  no review prompt was even written
PASS  the RED call is in the ledger at its real price       [red_authoring=0.6]
PASS  the GREEN call's cost is on an attempt row
PASS  nothing is recorded as unpriced
PASS  total spend is two calls ($1.20), not three ($1.80)
PASS  the overshoot is one call's worth, exactly as the guarantee allows
PASS  the review is recorded not_run — never passed, never skipped
PASS  the lifecycle still reached green_verifying
```

The overshoot is $0.20 — one call minus the remainder — which is what "bounded
by one call" means and is why the sentence is worded that way rather than as a
cap.

## 3. Two product findings, neither of them the thing being rehearsed

**#220 — TDD cannot run on a project that configures no linter.** The
rehearsal's *first* attempt died before reaching its subject:

```
OUTCOME: RedOutcome.UNVERIFIABLE || lint failed on the file about to be frozen
(test/kapelle/providers/catalog_test.exs): ... Found 251 errors..
```

`lint_command` defaults to `uv run ruff check .`, the RED phase lints the file
it is about to freeze regardless of `run_lint: false`, and so ruff read an
`.exs` file and refused every red. Every red then being `unverifiable`, the
gate treats it as an instrument error and *retries* — paid retries, against a
linter that will fail identically every time. The paid pilot missed this only
because its config happened to declare `commands.lint`.

**#219 — a successful, merged task is marked BLOCKED when the budget runs out
after it.** Visible in the fixed run's tail:

```
✅ Completed in 0.6s
Task budget exceeded ($1.20 >= $1.00)
Committed the blocked task's status flip   previous=done new=blocked
Execution summary   completed=1 failed=1
```

The post-attempt check runs before the success branch, so a task that finished,
committed, merged and deleted its branch is recorded as a failed attempt and
flipped to `blocked` in `tasks.md`. Since `resolve_dependencies` promotes
`blocked` → `todo`, the next run can re-execute work that is already merged —
paying to author a red against code that already implements the feature.
Pre-existing, and made common by #216: totals now reach caps they used to reach
invisibly.

## 4. What was not rehearsed

- **A published artifact.** #215–#217 are merged and unreleased, so both
  rehearsals ran against the working tree. The pilot's rule — test what PyPI
  ships, assert the printed version — still applies to the next paid run and is
  not satisfied by this document.
- **A compliant GREEN.** The stand-in was written to disobey. Whether a real
  agent obeys the frozen-files block is the question #214 exists to answer, and
  only a paid run answers it.
- **Parallel review under a budget.** Serialisation is covered by unit tests;
  the rehearsal config runs a single reviewer.
- **3b evidence.** Still empty. These rehearsals prove instrument behaviour,
  not whether post-GREEN debt is a repeatable class.
