# Battle test of published v2.25.0 — report

**Subject:** `spec-runner==2.25.0` installed from PyPI (`uv tool install`), run from
`~/.local/share/uv/tools/spec-runner/` — **not** the editable checkout.
**Scope:** the owner's matrix for #141 slices 1–3, run in disposable git repos.
**Verdict:** **exit criteria not met. Slice 4a is blocked.**

---

## 0. How it was run, and what that does and does not prove

Each scenario got a fresh throwaway repository. The coding agent was a scripted
stand-in: a shell script that writes files and prints `TDD_SELECTOR:` /
`TASK_COMPLETE`. Everything else — config loading, the RED phase, replay in a
disposable worktree, claims, gates, remedies, the CLI — was the published
package.

So this tests **the harness, not agent behaviour**. An LLM's ability to write a
genuine failing test is untested here and needs a separate, priced run.

One honest note on method: the very first attempt sent a run to the **real
`claude` CLI** because my config mixed the flat v2.0 shape with an `executor:`
key, and the loader then reads *only* that section and silently discards
everything else. My harness bug, not a defect — but see F-7.

---

## 1. Results

| # | Scenario | Result |
|---|---|---|
| 1 | Happy path: RED confirmed → GREEN → complete | **pass** |
| 2 | Claimed test modified after RED | **fail** — F-1 (passes only when review is on) |
| 3 | Claimed test deleted / renamed | **fail** — F-1, same root cause |
| 4a | Two tasks, conflicting claims | **pass** — refused, names the owning task |
| 4b | Two tasks, identical bytes | **fail** — F-3 |
| 5 | `abandon` → back to RED authoring, evidence kept | **pass** (but see F-3) |
| 6 | `repair` → new lineage, mandatory replay, idempotent | **pass** |
| 7 | Crash / restart after checkpoint | **fail** — F-4 |
| 8 | Control: `execution_mode: standard` | **pass** — no gates, no checkpoints, lifecycle unchanged |

## 2. Exit criteria

| Criterion | Met? |
|---|---|
| No byte-lock bypass via retry/resume | **no** — F-1, F-4 |
| A stale verdict does not clear a new SHA | partially — held everywhere observed, but no scenario forced the case; unit-covered only |
| Remedies are idempotent | yes — both, with the verdict preserved on repeat |
| A blocked task is not marked DONE | yes **when the gate blocks** — but F-1 means it often does not |
| Evidence reconstructs checkpoint → claim → remedy → new checkpoint | in the database, yes. Through the product, **no** — F-5 |
| Cost and manual interventions recorded | interventions yes (`tdd_remedies`); **cost no** — F-6 |
| Findings classified | below |

---

## 3. Findings

### F-1 — the byte-lock is bypassable by the ordinary path — **product defect, blocking**

With `run_review: false`, a task can rewrite, delete or rename its own claimed
test and reach **DONE**. No refusal, no remedy, no record.

Observed: both gate evaluations judged the *red* commit `d3241ce`, while the
mutation landed in `356ec61` — the task commit, created **after** the gate.

Root cause: the pre-terminal gate runs before the final auto-commit. That
placement was deliberate (PR #170: a blocked task must not be labelled DONE
first) and it is right on its own terms — but it means that when nothing else
has committed the work, the gate judges a tree that does not contain the work.
The `#103` pre-review commit accidentally covers the review-on path, which is
why the same scenario **passes** with review enabled, reporting
`claim violated — modified tests/test_widget.py (claimed by TASK-001 …)` and
leaving the task at `🔍 REVIEW`.

So the lock holds exactly when an unrelated feature happens to be on.

*Fix direction:* the work must be committed before the pre-terminal gate, with
DONE and its bookkeeping commit after — the owner's stated lifecycle already
reads that way (`… → merge candidate → policy gate → merge → DONE`). This is a
lifecycle-ordering change and wants a decision, not a quiet patch.

### F-2 — `run --task=X` exits 0 when the task fails — **product defect, blocking, not TDD-specific**

Measured in `standard` mode, same repo, same failing agent:

- `spec-runner run --task=TASK-001` → `completed=0 failed=1 failed_attempts=3`,
  `last_run_stop_reason: completed`, **exit 0**;
- `spec-runner run --all` → same failures, **exit 1**.

`#144` fixed the false-green exit for `--all` and missed the single-task path —
the one CI and an orchestrator use for one task. Every gate refusal in this
battle also exited 0 for the same reason, so **it also invalidates a claim I
made in PR #168**: I wrote that an unsatisfied gate "does reach the exit code
through the existing failed-task rules". It does not, on this path.

### F-3 — a second task claiming identical bytes records no claim — **product defect**

TASK-002 authored the same test content TASK-001 had frozen. Enforcement
correctly saw no violation (bytes unchanged), TASK-002 got a **confirmed red**
— and **no claim of its own**, because `record_claims` skips a `(path, blob)`
already claimed by anyone in the namespace.

Then `tdd abandon TASK-001` released the file entirely, while TASK-002's
confirmed red still depends on it. The contract's "one task's remedy does not
release another task's independent claim" is defeated not by the remedy but
because the second claim was never recorded.

*Fix direction:* idempotency should be keyed per `(task, path, blob)`, not per
namespace. The "same bytes are permissible" rule stays; each task records its
own dependency.

### F-4 — every retry re-authors the red; checkpoints stack — **product defect**

A task whose GREEN pass fails is retried, and each attempt runs the whole RED
phase again. Three attempts produced **three red commits and three `active`
checkpoints** for one task.

Consequences: an agent call per retry that need not happen (real money in a
real run); a history with three "red for …" commits; and a state that
contradicts the model the remedies assume — CAS targets *the* active
checkpoint, and `set_checkpoint_status` retires only the one whose id matches,
so the orphans stay `active` forever.

*Fix direction:* a confirmed, still-valid checkpoint should be reused rather
than re-authored — which is what "durable checkpoint" was for.

### F-5 — checkpoint ids, claims and remedies are invisible — **harness/observability gap, blocking in practice**

`tdd abandon|repair --checkpoint <id>` **requires** an id that **no command
prints**. `status`, `status --json`, `costs`, `report` and `verify` mention
none of it. To run the remedies in this battle I read the SQLite database and
re-derived a SHA-256 by hand.

Worse for trust: after abandoning TASK-001's red, `status` still shows
`✅ TASK-001: success`.

The evidence is all there and none of it is reachable. Until it is, the remedy
commands are effectively unusable by an operator.

### F-6 — the RED authoring pass's cost is discarded — **product defect (accounting)**

`tdd._run_agent` parses the CLI result and returns only `.text`; tokens and
cost are dropped. TDD mode therefore makes an extra agent call per task (more
with F-4) that never reaches `attempts` or `spec-runner costs`. The battle's
`$0.00` is real only because the agent was a script — the columns are `None`,
which is the tell.

Directly fails the owner's "cost recorded" criterion.

### F-7 — mixing flat and `executor:` config silently discards the flat keys — **product defect (usability)**

`data.get("executor", data)`: one stray `executor:` key means every top-level
key is ignored — including `claude_command`, so the run went to the real
`claude`. `spec-runner validate` reported **0 errors**.

Low severity, high embarrassment potential: the failure mode is "your config
did nothing and the tool spent money elsewhere".

### F-8 — a gate-blocked task leaves the spec dirty and the next run refuses — **workflow gap**

Blocking flips `tasks.md` to `🔍 REVIEW`, which the dirty-spec guard then
treats as uncommitted spec changes, so the next `run` refuses until the
operator commits a status flip they did not make. Correct in isolation, wrong
in combination.

---

## 4. What this says about slice 4a

The gating worked as intended: three of the eight scenarios failed, and two of
the failures (F-1, F-2) are the kind that unit tests structurally cannot catch
— they live in the *ordering between* the pieces and in the process exit
surface. Designing GREEN on top of this would have inherited both.

**Recommended before 4a:** F-1 and F-2 (blocking correctness), then F-3 and
F-5. F-4 and F-6 are worth doing in the same pass since they touch the same
code. F-7 and F-8 are independent and can wait.
