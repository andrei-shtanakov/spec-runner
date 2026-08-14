# TDD evidence run 2 — kapelle m2 TASK-104, wire the fallback chain into routed execution

Recorded against the protocol in
[`2026-08-12-tdd-evidence-protocol.md`](2026-08-12-tdd-evidence-protocol.md),
for the #285 trigger. From durable artefacts only — the canonical state domain
`spec/.executor-m2-state.db` in kapelle (`budget_domain_id c9051f20d1104a39`),
that repository's git history, and the stored attempt outputs. Unanswerable
fields are marked **observability gap** rather than reconstructed.

**Task:** a real product task from kapelle's m2 backlog — wiring the
deterministic fallback chain into routed execution. Not written for this
experiment, and the successor of run 1's work.

---

## 1. Product verdict

**Completed, and the code was right; the review was never bought.** Final
attempt `2026-08-14T08:36` succeeded — tests `pass` (full suite, exit 0), lint
`pass`, both terminal gates satisfied — and merged through the human gate. But
the recorded review verdict is **`not_run`**, with the reason in
`phase_results`:

```
review  not_run  Task budget reached before the review call
```

Seven attempts:

| attempt | outcome | recorded reason |
|---|---|---|
| 2026-08-13T22:10 | fail | `HOOK_FAILURE` |
| 2026-08-13T22:11 | fail | `INTERRUPTED` |
| 2026-08-13T22:13 | fail | `HOOK_FAILURE` |
| 2026-08-13T22:27 | fail | `TASK_BLOCKED` at `exec` — *"REQ-104's typed-error requirement structurally conflicts with a frozen assertion in `pipeline_test.exs`"* |
| 2026-08-14T08:24 | fail | `TASK_BLOCKED` at `exec` — *"agent reported TASK_BLOCKED without a reason"* |
| 2026-08-14T08:25 | fail | `BUDGET_EXCEEDED` |
| 2026-08-14T08:36 | **success** | review `not_run` (budget) |

The fourth attempt is the interesting one and it is a **product** success
disguised as a failure: the agent implemented, discovered empirically that a
frozen assertion contradicted REQ-104, reverted, and escalated with a reason.
That is the contract working — an agent may not edit a claimed test, so it
stopped and said why.

## 2. Harness verdict

**The stand cost this task two paid attempts, and both causes are shipped
fixes.**

The fifth attempt — **$3.5894** — was failed by #266: the agent finished the
job and ended with `TASK_COMPLETE`, but its summary opened with

> "TASK-104 is complete. The prior `TASK_BLOCKED` was resolved upstream: an
> operator repair (commit `7c3b3fe`) already updated the frozen
> `pipeline_test.exs` …"

and a substring match read that mention as the verdict. The stored output ends
`TASK_COMPLETE` with `mix test` 306/0 and `git diff` empty on the frozen files.
Fixed in 2.33.0 (markers are lines); the price of the defect here is exact:
**$3.59 and one attempt.**

Also visible in this run and since fixed: `repair` refusing the state it was
built for (#263, the fourth attempt's escalation is precisely its input), and
`retry` merging into master under `integration_pr` (#254) — the run's own git
trail carries `spec-runner rescue: TASK-104` stashes and a bookkeeping commit
for the interim `blocked` status.

## 3. Phase / checkpoint / remedy lineage

Standing checkpoint `10b2ecd89834` — commit `7c3b3fe1811e`, baseline
`298d3aab9389`, selector `test/kapelle/orchestrator/pipeline_test.exs:70`,
outcome `expected_fail`, environment
`runner=exunit;mix.lock=51415a74983e96bc;elixir=1.19.4;otp=28;mix_env=test;deps_source=133ba0aa6237`.

Claims on `pipeline_test.exs`: `94c313039252` **superseded**, `b6e3b12e3871`
**active** — the lineage of the repair.

Phases:

```
red_authoring ×3 → red_verifying → green_implementing
→ red_authoring → green_implementing        (the TASK_BLOCKED cycle)
→ red_authoring ×2 → green_implementing
→ green_verifying → refactoring (skipped) → done
```

One remedy: `repair b1d9594cd86d → 10b2ecd89834` (2026-08-14T08:15), reason on
the record — the exec agent proved the structural conflict, the operator
updated test-7's expected shape *preserving its no-evaluation intent*, and the
red stayed at line 70. That remedy was blocked by #263 until 2.33.0 shipped;
the reason field says so.

`phase_results` also carries the parse-stage history: `unexpected_fail —
blocked marker`, then `pass — completion marker recognized`.

## 4. Cost, RED against GREEN and review

Task total **$13.1457**, all priced (`unmeasured_calls = 0`).

| stage | calls | cost |
|---|---|---|
| RED authoring | 3 | **$3.5918** ($1.5397 + $0.4420 + $1.6101) |
| implementation (GREEN) | 3 recorded attempts | **$9.5540** ($2.0032 + $3.5894 + $3.9614) |
| review | 0 | **$0.00 — never ran** |

The arithmetic that matters for #285: **the review of this task cost nothing
because it never happened**, and the run before it (§ run 1) spent $2.09 on
review alone. A refactor pass would be a *fourth* paid stage in a task whose
third one could not be afforded.

Of the $9.55 spent implementing, **$3.59 bought nothing** — that is the #266
attempt.

## 5. Interventions

- one operator remedy (§3), with a written reason and an actor;
- budget authorizations #7 (task, $7.00), #8 (task, $13.00) and #9 (run,
  $28.00) — the ceiling was raised twice during this task;
- the operator's own commit `7c3b3fe` supplying the repaired test bytes;
- after the merge, a **human PR review (Copilot)** produced `59fe660`,
  "rejection reasons are normalized JSON-safe before jsonb".

## 6. Post-GREEN debt

**One real post-GREEN defect, and it is not a refactoring class.** `59fe660`
fixed rejection reasons that were not JSON-safe before being written to
`jsonb` — a correctness bug in the merged code, found by a human reviewer after
the fact.

Read against the protocol's table, this is the row *"a repeatable class that
review already catches"* — except that here review **did not run at all**,
because the budget was exhausted before it. The defect is evidence for funding
the review stage (which #267's reserve now makes possible), not for adding a
refactor pass after it.

No duplication, dead abstraction or stale naming attributable to the red was
observed in the merged diff.

## Tool versions per stage

**Observability gap**, as in run 1: nothing records which version ran which
stage. The run spans 2026-08-13 → 2026-08-14, across the 2.31.0 → 2.33.0
publications; the `repair` at 08:15 can only have worked on ≥ 2.33.0 by its own
reason text, but that is inference from prose, not a recorded fact. Not
attributed here.
