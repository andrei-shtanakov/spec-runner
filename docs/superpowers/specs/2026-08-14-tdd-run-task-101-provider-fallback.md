# TDD evidence run 1 — kapelle m2 TASK-101, deterministic provider fallback

Recorded against the protocol in
[`2026-08-12-tdd-evidence-protocol.md`](2026-08-12-tdd-evidence-protocol.md),
for the #285 trigger. Written **after** the run, from durable artefacts only:
the canonical state domain `spec/.executor-m2-state.db` in the kapelle
repository (`budget_domain_id c9051f20d1104a39`), that repository's git
history, and the run's own progress trail. Nothing here is reconstructed from
memory; fields that no artefact answers are marked **observability gap**.

**Task:** a real product task from kapelle's m2 backlog — deterministic
provider fallback in the catalog. Not written for this experiment.

> **Counts as evidence run for the #285 trigger.**
> Earlier attempts on this same task are recorded separately as attempt
> records (`2026-08-12-tdd-run-provider-fallback.md` and
> `…-attempt-3.md`). They are **not** additional runs: this file is the
> completed one, and the counter counts completed runs.

---

## 1. Product verdict

**The tool did the right thing, and eventually said so.** Final attempt
`2026-08-13T20:55` succeeded with review `passed`; the task is `done` in the
lifecycle and merged through the human gate (kapelle PR #7).

Six attempts, of which five failed. Their recorded shape:

| attempt | outcome | recorded reason |
|---|---|---|
| 2026-08-12T17:26 | fail | `HOOK_FAILURE`, review `error` — *"You've hit your session limit · resets 5:30pm"* |
| 2026-08-12T17:26 | fail | `BUDGET_EXCEEDED` |
| 2026-08-13T12:30 | fail | `HOOK_FAILURE` |
| 2026-08-13T12:56 | fail | `HOOK_FAILURE` |
| 2026-08-13T20:43 | fail | `HOOK_FAILURE` |
| 2026-08-13T20:55 | **success** | review `passed` |

Terminal gates on the successful attempt, from `gate_verdicts`:

```
tdd.red     satisfied  red confirmed: test/kapelle/providers/catalog_test.exs:85 at 6c24cd9dc136
tdd.claims  satisfied  claims intact
```

Both were `unsatisfied` earlier (`2026-08-12T17:26` and `2026-08-13T12:30`,
`claim violated — modified …catalog_test.exs; claimed 08d560c92561, found
3cfbf07f76c3`). The gate refused a merge it should have refused, twice, and
the refusals name the bytes.

## 2. Harness verdict

**The stand misled, repeatedly, and every one of those failures is now a
shipped fix.** This run is where five of them were found:

| what happened | filed | shipped |
|---|---|---|
| review agent hit a provider session limit; the reset time was in the message and nowhere in the record | #229 | 2.30.0 |
| the claim-violation path wiped the working tree before any gate ran | #231 | 2.30.0 |
| `repair` after green superseded the confirmed red, leaving `not_red` and no way to finish | #232 | 2.31.0 (`tdd resume`) |
| `resume` was inadmissible in the exact state it was built for | #249 | 2.31.0 |
| the claim froze the whole test file, and the green legitimately appended to it | #252 | 2.33.0 |

Attribution matters here: **the task's code was never the problem.** Every
failed attempt died in the harness or in the contour around it.

## 3. Phase / checkpoint / remedy lineage

Standing checkpoint `a9a0a5a0a1a8` — commit `6c24cd9dc136`, baseline
`36956748f7c2`, selector `test/kapelle/providers/catalog_test.exs:85`, outcome
`expected_fail`, environment
`runner=exunit;mix.lock=51415a74983e96bc;elixir=1.19.4;otp=28;mix_env=test;deps_source=133ba0aa6237`.

Claim: `test/kapelle/providers/catalog_test.exs` @ `08d560c92561`, **active**.

Phases, in order recorded:

```
red_authoring → red_verifying → green_implementing → green_verifying
→ red_authoring ×4  (the wedge: every retry re-enters authoring)
→ refused:green_implementing, refused:green_verifying   (#253, since fixed)
→ refactoring (skipped) → done
```

Remedies, all human-authored with reasons on the record:

- `repair a9a0a5a0a1a8 → 8ee03944407d` (12:53) — the review agent's legitimate
  fixes to the claimed file, after the session-limit death;
- `resume a9a0a5a0a1a8` (20:41) — reinstating the confirmed red **and its
  claims** to finish through the contour;
- `abandon 8ee03944407d` (20:42) — retiring the `not_red` lineage the
  pre-#244 repair had created.

The two `refused:` rows are #253 (the machine asked the previous row instead of
the evidence) — non-fatal then, fixed since.

## 4. Cost, RED against GREEN and review

Task total **$5.9225**, all of it priced (`unmeasured_calls = 0`).

| stage | calls | cost |
|---|---|---|
| RED authoring | 2 | **$1.1506** ($0.5202 + $0.6304) |
| implementation (GREEN) | 2 recorded attempts | **$2.6841** ($2.0126 + $0.6715) |
| review | 1 ledger row | **$2.0878** |

Two readings worth keeping:

- **review was the single most expensive call of the task** — more than both
  RED calls together, and more than the successful implementation pass;
- the earlier review that died on the session limit is **not** in the ledger:
  review calls only began to be recorded in 2.29.0 (#213). So the true review
  spend is a floor, not a total. **Observability gap**, and the reason the
  ledger's `unmeasured_calls = 0` is honest about what it counted rather than
  about what happened.

## 5. Interventions

Every human touch, from the durable record:

- three operator remedies (§3), each with a written reason and an actor;
- manual restoration of the claimed bytes after the tree wipe — commits
  `98e16ab` ("review fixes stranded by session-limit crash"), `f147c6d`
  ("restore the claimed RED bytes; review tests move to their own file"),
  `f21e6b7` ("the claimed bytes are the RED commit's, not the green's");
- budget authorizations #1 (task, $5.00) and #2 (run, $5.00), later #3 (run,
  $9.00), each with an actor and a reason.

That is five interventions across six attempts. None of them was about the
product code.

## 6. Post-GREEN debt

**No repeatable class of post-GREEN code defect was observed.** The review
verdict was `passed`, and the commits that follow the green are all harness
recovery, not code cleanup:

- `98e16ab`, `f147c6d`, `f21e6b7` — restoring claimed bytes and **moving the
  review's own tests into a separate file**.

That last one is the observation that mattered, and it is a *tool* finding, not
a code one: it became #252 and shipped as variant D, so the same green would
not create the same conflict today.

For #285 this run reads as: **a completed post-GREEN lifecycle, and no
evidence for an automatic refactor pass.** What the run argues for is what was
already done — fixing the doors.

## Tool versions per stage

**Observability gap.** No artefact records which spec-runner version ran which
stage: `executor_meta` holds no version, and neither `attempts` nor
`agent_calls` carries one. `gate_verdicts.config_hash = 1d5a875f570101df`
identifies the *policy* the gate judged under, not the binary.

The run spans 2026-08-12 → 2026-08-13, during which 2.29.0, 2.30.0 and 2.31.0
were published; attributing a stage to a version would be a guess, so it is not
attempted here. Worth filing if a third run is to be attributable.
