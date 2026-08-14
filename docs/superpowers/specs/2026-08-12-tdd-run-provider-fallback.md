# TDD attempt record — kapelle, provider fallback

> [!NOTE]
> **This is an attempt record, not a completed evidence run.** It documents
> attempts on kapelle m2 **TASK-101** that did not finish. The completed record
> for that task — the one the #285 counter counts — is
> [`2026-08-14-tdd-run-task-101-provider-fallback.md`](2026-08-14-tdd-run-task-101-provider-fallback.md).
> Counting the files in this directory as runs would over-count the trigger,
> which is why each says which it is.

**Subject:** `spec-runner==2.28.1` installed from PyPI (`uv tool install --refresh
--reinstall`), version asserted before the run. Target: kapelle (Elixir/Phoenix),
`master` at `4824a92`, suite green at baseline (235 tests, 0 failures; `mix
format --check-formatted` and `mix credo` clean).

**Config:** `execution_mode: tdd`, `tdd_runner: exunit`, `review_policy:
advisory`, `budget_usd: 4.0`, `task_budget_usd: 3.0`, agent `claude`/`sonnet`
(the fixed S1/S2 agent), `integration_pr: true`.

**Outcome: the task did not complete.** The RED phase authored a correct test
and the replay could not verify it, for a reason that is a **product defect**:
the disposable replay worktree has no installed dependencies, so `mix test`
refuses to run at all there.

Recorded under the six-field protocol
(`2026-08-12-tdd-evidence-protocol.md`), including the fields that are empty —
an empty field is a fact about how far the run got.

## 1. Product verdict

**Correct where it acted, and blocked by a gap in the same feature.**

What worked, and is worth saying because none of it was true a day ago:

- the RED prompt asked for ExUnit's selector shape, and the agent complied:
  `TDD_SELECTOR: test/kapelle/providers/catalog_test.exs:85`;
- line 85 is a real `test "..." do` definition line, so the AST preflight passed;
- the checkpoint was written with the outcome it actually had —
  `unverifiable`, not a red;
- the gate refused, twice (before implementation), and the run stopped with
  `HOOK_FAILURE`, no implementation pass, no merge, no DONE;
- the integration branch was cleaned up because it was empty.

What failed:

```
$ git worktree add --detach <tmp> 0355c13   # what the replay does
$ mix test --trace test/kapelle/providers/catalog_test.exs:85
  the dependency is not available, run "mix deps.get"
** (Mix) Can't continue due to errors on dependencies
```

A `git worktree` carries tracked files only, and Elixir keeps `deps/` and
`_build/` **inside the project directory**, both gitignored. So the replay
worktree cannot compile, `mix` prints no run summary, `classify` returns
`UNRECOGNIZED`, and the verdict is `unverifiable`.

The adapter behaved correctly — it refused rather than inventing a verdict —
but the *replay mechanism* cannot verify a red in any language whose
dependencies live in the project directory. pytest never hit this because a
Python environment is installed outside the checkout, so a bare worktree can
run tests.

**Classification: product defect.** `execution_mode: tdd` + `tdd_runner:
exunit` is a supported combination as of v2.28.0 and cannot verify a single red
on a real Elixir project. Same class as #198: machinery that assumed Python's
shape.

## 2. Harness verdict

**No harness fault in the run itself.** The stand was prepared per the
pre-flight: published artifact, version asserted, spec committed, budgets set,
`review_policy` left advisory, baseline green.

One harness-adjacent note, recorded because it cost a round trip earlier: the
first `uv tool install` of 2.28.1 reported `unsatisfiable` and left 2.28.0 in
place. The version assertion caught it; the run used 2.28.1. Second occurrence
of the same trap, already in `docs/release-runbook.md`.

## 3. Phase / checkpoint / remedy lineage

```
phases:      red_authoring → red_verifying
checkpoint:  e07020ac026b
             commit   0355c13eb182…  (the red, committed)
             baseline 36956748f7c2…
             selector test/kapelle/providers/catalog_test.exs:85
             outcome  unverifiable
             env      mix.lock:51415a74983e96bc
claims:      none        (correctly — nothing was confirmed to lock)
remedies:    none
```

The lifecycle never reached `green_implementing`, which is the contract
holding: no red, no green.

## 4. Cost — RED against GREEN and review

| stage | cost | tokens |
|---|---|---|
| RED authoring | **$0.56** | 937 in / 5 644 out |
| GREEN implementation | — | never ran |
| review | — | never ran |

Wall clock: 103 s for the authoring call, ~104 s for the run. Against a $3.00
task budget, so the budget was never the constraint.

The number that matters for the 3b question — what an extra agent call costs
relative to the others — cannot be read from a run that made only one call.

## 5. Interventions

**None during the run.** It was started once and left alone.

Before it: I wrote the M2 spec and committed it (instructed), and set the four
config keys (instructed). After it: I reproduced the failure by hand in a
worktree to attribute the cause, which changed nothing in the repository.

kapelle's `spec-runner.config.yaml` is untracked by design; the original was
copied to `/tmp/kapelle-config-before-pilot.yaml` before editing.

## 6. Post-GREEN debt

**Nothing to report — GREEN never ran.** This run contributes no evidence to
the 3b question, in either direction.

## What this run does settle

It is not a wasted $0.56. Three things are now measured rather than assumed:

1. **The prompt fix works end to end on a real agent.** The selector shape a
   real `claude`/`sonnet` run emits for an Elixir project is the shape the
   adapter parses. That was a guess until now.
2. **The AST preflight accepts a real agent's real test** — line 85 of a
   200-line existing test file, inside a `describe` block.
3. **The next blocker is dependency installation in the replay worktree**, and
   it is a design decision rather than a bug fix: whether the replay may run
   `mix deps.get` (network, time), or share the project's `deps/` through
   `MIX_DEPS_PATH`, or copy them. All three have costs, and the choice belongs
   to the owner.

The evidence run for 3b has not started. This is run 1 of an attempt, not run 1
of three.
