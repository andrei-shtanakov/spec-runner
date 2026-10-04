# Retry continues from the previous attempt's work (WIP), against a trusted harness baseline

Status: design approved by the owner section by section, 2026-10-04
(TODO `retry-continues-from-wip`). Ships as **5.0.0**: it adds tables to the
state DB, and any change to the SQLite surface is a major (AGENTS.md; owner
reconfirmed for this work).

## Problem

Under `create_git_branch: true` every attempt starts with `pre_start_hook`,
which rescues the tree into a stash (`rescue_uncommitted`, #231), checks out
the main branch, runs `git checkout -- .` and `git clean -fd`, and switches
back to the task branch. Under `integration_pr` the run start does the same
before forking the integration branch (`rescue_run_uncommitted`). What the
task branch carries in commits survives; what the agent left uncommitted
goes to a stash. The GREEN agent does not commit, and the candidate commit is
made only in `post_done_hook` after the tests. So after a timeout or a failed
gate the work of the attempt is in a stash, and the retry starts from scratch.

#480 TASK-002 would have lost 60 minutes of work this way; an operator's
manual `wip(TASK-002): …` commit saved it.

A second, related gap: the harness guard's baseline (`HarnessBaseline`) lives
in memory for one `run_with_retries`. A separate `spec-runner retry`
re-captures it from the tree. Once work of a previous attempt is carried
forward in a commit, a harness edit in that commit (or in a red commit, which
`_commit_red` makes from the whole tree) would become the next invocation's
oracle — the #137 class.

## Goals

- A retry — automatic within a run, or a separate `spec-runner retry`,
  including after a killed process — continues from the previous attempt's
  work, by default, under `create_git_branch: true` (`integration_pr`
  included). No config key.
- The carried work is not a candidate: it gives no ground to the red gate,
  confirms no claim, and is never the SHA a gate verdict is bound to.
- The harness is judged against a baseline that existed **before the first
  attempt**, persisted, never re-captured from carried work.
- `tasks.md` status flips stay with bookkeeping.
- The next attempt's prompt says it is continuing unfinished work, that the
  work is unverified, and that the approach may be revised.
- If the work cannot be saved, nothing destructive runs.
- `create_git_branch: false`: the tree and the absence of WIP stay as today;
  the persistent baseline applies there too.

## Design

### 1. Saving WIP before every destructive switch

There are two destructive points: the branch stage of `pre_start_hook`
(stash, `checkout <main>`, `checkout -- .`, `clean -fd`) and the run start
under `integration_pr` (stash, fork). Before either, the tree is inspected:

- **HEAD is the branch recorded for a started task** — `task_workspaces` holds
  a row for `(namespace, task_id)` whose `branch` equals the current branch
  exactly — and the tree has eligible changes → the changes are committed on
  that branch as WIP instead of being stashed.
- **The tree belongs to a started task that has no workspace row** (see §2,
  "started") → under `strict`, refusal before any stash or switch; under
  `warn`/`off`, today's behaviour for this start (stash).
- **Anything else** (dirt on the main branch, on another or unknown branch, a
  branch name that matches a task but no row with that exact branch and
  namespace) → today's rescue stash.

The trailer marks a WIP commit; it never proves branch ownership or harness
trust — those come from the rows.

**What a WIP commit contains.** The same set the rescue would stash, minus
runtime paths and `spec_contract_paths(config)`: an explicit path list, staged
with `git add -- <paths>`, committed with `git commit -- <paths>` semantics so
that anything already in the index (a staged runtime file, a staged
`tasks.md`) cannot ride along. No eligible change → no WIP commit (never an
empty one).

**The index is not overwritten.** `git add` would replace a staged version of
an eligible path with the working-tree version. Before touching the index,
every eligible path is checked (a `git diff` that cannot be read is an
`instrument` refusal, never "nothing partially staged"): if its index entry differs from HEAD **and**
from the working tree (a partially staged file), the start is refused —
the paths are named, nothing was staged or committed, no destructive step
ran; the operator commits or unstages and retries. Anything else (index
equal to HEAD, or equal to the working tree) loses nothing when staged.

**What WIP leaves behind, per destructive point.**

- *`integration_pr` run start.* WIP takes the eligible changes; the
  spec/config paths `rescue_run_uncommitted` deliberately excludes stay in
  the tree under the existing dirty-spec guard (`_enforce_clean_spec`, which
  already answers before the fork) and the checkout's own rules. If the
  checkout refuses because of them, the run stops, as today. Runtime paths
  are ignored, as today.
- *`pre_start` branch stage.* WIP takes the eligible changes; whatever
  remains (spec contract paths, e.g. an uncommitted status flip) goes to the
  existing task-level rescue stash before the cleanup. If that rescue fails
  after a successful WIP commit, the cleanup does not run and the start is
  refused; the WIP commit stays.

**Form.**

```
wip(TASK-X): unfinished work of attempt N — not a candidate

Spec-Runner-WIP: TASK-X
Spec-Runner-WIP-Attempt: N
```

`N` is the number of the last recorded attempt of the task in the state DB at
the moment of the commit. It is stored in the trailer because a later
invocation cannot reconstruct it from the counter.

**Failure.** If staging or committing fails, the start is refused: the work
is still in the tree, and no destructive step ran. (The index may have been
touched; the refusal does not claim otherwise.)

Committing at the end of a failed attempt is not needed for the guarantee: a
killed process never sees its end, and the next start always runs.

### 2. Persistent state: task workspace and harness baseline

Four tables; `namespace` is `tdd.resolve_namespace(config)`:

- **`task_workspaces`** — key `(namespace, task_id)`; `branch` (NULL under
  `create_git_branch: false`), `started_at`, `run_id`, `bound_by`
  (`run` | `operator`). The fact that the task started, and on which exact
  branch. Written **in every guard mode** (WIP depends on it, not on the
  snapshot), after the current `pre_start` has itself checked out this task's
  branch — ownership established by our own action, not by a name.
- **`harness_baselines`** — key `(namespace, task_id)`; `captured_at`,
  `run_id`, `guard_mode`, `provenance` (`initial` | `operator` |
  `recaptured`), `surface` (JSON: every surface **candidate** at capture —
  `HARNESS_CANDIDATES`, the control-plane keys, `harness_files` — each with
  its state `file` | `dir` | `absent`).
- **`harness_baseline_files`** — key `(namespace, task_id, path)`; `state`
  (`present` | `unreadable`), `digest`, `content` (BLOB, NULL when
  `unreadable`). One row per file the surface held at capture.
- **`harness_trust_audit`** — key `id` (one row per event, several per task);
  `namespace`, `task_id`, `at`, `actor`, `reason`, `branch`, `bound_branch`
  (1 when the command created the workspace binding), `replaced_provenance`
  (NULL when nothing was replaced). Append-only, and **kept** after DONE and
  `tdd abandon`.

**Directory candidates are snapshotted whole.** A candidate that is a
directory (`.github/workflows`, a directory in `harness_files`) records
`dir` in `surface` and one file row per file under it. A file under a
recorded directory that has no row is `created` — an ordinary violation, not
an unknown surface. A candidate recorded `absent` (file or directory) that
exists later is `created` for every file it now holds. "Unknown" means only
a **candidate** that is not in the recorded `surface` at all (the surface
definition grew, e.g. `harness_files` was extended).

**"Started"** is decided **before** this attempt's `pre_start`, from what
existed before it: a `task_workspaces` row, or recorded attempts of the task,
or an existing task branch. A branch the current `pre_start` creates and the
attempt row of the current attempt do not count.

**Capture.** Once, in `_execute_task`, after `pre_start_hook` (so `uv sync`
is legitimate preparation) and before any agent call — where
`HarnessBaseline.capture` sits today — in one transaction, only under
`warn`/`strict`. A capture that cannot be written is an `instrument`
refusal before the agent. Later attempts and invocations only read;
`HarnessBaseline` becomes a reader of the persisted row.

**Trust rules under `strict`** (all refusals happen before the agent and
before any destructive step):

| state | result |
|---|---|
| `initial` or `operator` snapshot, every current surface candidate is in the recorded `surface` | proceed |
| started, no snapshot (incl. a task begun under `off`, or before 5.0.0) | refuse: no trusted harness state → `harness trust` |
| snapshot `recaptured` | refuse: an automatic re-capture is not trusted → `harness trust` |
| a current surface candidate is not in the recorded `surface` (e.g. `harness_files` extended) | refuse: the new surface cannot be checked → `harness trust` |
| a row is `unreadable` | refuse: not trusted content → fix, then `harness trust` |
| the DB cannot be read or written | refuse, kind `instrument` |

Under `warn`, a started task without a snapshot gets a `recaptured` one with
a warning (nothing is enforced); under `off`, no snapshot is taken. Neither
becomes trusted when `strict` is switched on.

**Lifecycle.** Workspace, snapshot and file rows are deleted together, and
atomically with the record that ends the task:

- **DONE** — inside the same transaction `record_attempt` uses to write the
  successful attempt. If that transaction fails (degraded mode), the DONE is
  not durable and neither is the deletion: the two never disagree.
- **`tdd abandon`** — its checkpoint status change, claims retirement, remedy
  row and the deletion are one `BEGIN IMMEDIATE` transaction (today they are
  three separate commits). A repeat call that finds the abandon applied
  therefore finds the rows gone.

They survive errors, timeouts, guard refusals and separate `retry`
invocations. **`spec-runner reset` keeps them**: it rebuilds the state DB in a
temporary file, carries the four tables (workspace, snapshot, files, audit)
over, and replaces the DB file atomically; any failure before the replace
leaves the original DB untouched and the command exits 2. Without this, under
`create_git_branch: false` a reset task would look new and a modified
harness would become `initial`. The audit is never deleted.

**`create_git_branch: false`.** The tree is not touched and no WIP is made,
as today. The persistent baseline applies all the same: captured before the
first agent call, read by later attempts and invocations, the same trust
rules under `strict`. The workspace row is written with `branch` NULL (the
"started" marker); no branch match is required to use the baseline, and
"started" is decided by the workspace row or recorded attempts.

**Failure before anything destructive.** Reading these rows happens before
the rescue/WIP step of both destructive points, the `integration_pr` fork
included. A DB that cannot be read or written stops the start there, kind
`instrument`.

### 3. `spec-runner harness trust`

```
spec-runner harness trust TASK-X --reason "…" [--bind-branch <branch>]
```

An operator's statement that the current harness surface of this task's tree
is trusted — modelled on `budget authorize`: mandatory reason, recorded
actor, refusal while the executor lock is held, refusal under
`SPEC_RUNNER_AGENT`.

- The task must exist in `tasks.md`; otherwise refused (`policy`, exit 1).
  A state DB that cannot be read or written: `instrument`, exit 2.
- With a workspace row: the namespace must match, and — when the row has a
  branch — the current branch must equal it; otherwise refused.
- Without a workspace row under `create_git_branch: true`: refused unless
  `--bind-branch <branch>` names the **current** branch; then the binding
  (`bound_by=operator`) is written. Under `create_git_branch: false` the
  binding is written with `branch` NULL and `--bind-branch` is not needed.
- Writes, in **one transaction**, the binding (if any), the snapshot
  (`provenance=operator`, replacing any existing one) and the audit row
  (with `replaced_provenance`). A failure leaves no partial rows.

It is not a way around a refusal: the refusal text and the migration note
say the operator first restores and checks the harness, then confirms it.

### 4. Gates, candidate, review, prompt

- **Red gate.** A WIP commit never creates, adopts or confirms a red; the
  ancestry check is unchanged. `_unregistered_red` (#261) requires HEAD to be
  the red commit; it now skips a **contiguous** chain of this task's WIP
  commits along first parents and stops at the first other commit. Every
  other admissibility check is unchanged; no harness-guard exception is
  introduced.
- **Claims.** Checked against the candidate commit as today. A WIP edit to a
  frozen test is in the candidate's tree and is caught there. WIP writes no
  evidence.
- **Candidate.** If HEAD is a WIP commit when the candidate stage runs and
  `commit_task_work` finds nothing new, an explicit empty commit
  `TASK-X: candidate` is made, so `gated_sha` and the review checkpoint never
  name a WIP commit.
- **No-op.** When the task branch carries WIP commits of this task, no-op is
  judged by the task's cumulative diff against its base (merge-base, as the
  review uses, #655), not by the last attempt's changes: work done entirely
  in earlier attempts is not a no-op; WIP fully undone later is.
- **Review.** The merge-base diff (#655) already includes WIP commits.
- **Prompt.** `RetryContext` gains the continuation: this task's WIP commits
  on the branch (SHA, attempt from the trailer, files). The prompt says:
  you are continuing unfinished work of attempt N, committed as …; it is not
  verified — review it before relying on it; you may revise the approach.
- **Harness guard.** The GREEN check compares with the persisted baseline. A
  harness edit carried in a WIP or red commit stays a violation until
  reverted; the baseline is never re-captured from it. The pre-GREEN check of
  each attempt's RED/verify-first passes (#658) is unchanged. A red confirmed
  on a corrupted oracle remains its own TODO item.

### 5. Refusal kinds

Missing or untrusted harness state, a started task without a workspace under
`strict`, an ownership mismatch in `harness trust`: `policy` (exit 1 — an
operator must act). DB read/write failure, WIP commit failure: `instrument`
(exit 2).

## Tests

All with a real git repository; only `paid_call._spawn` is replaced where an
agent call is needed.

1. Attempt 1 writes a file and times out; attempt 2 in the same process sees
   the file, the branch has a WIP commit with both trailers, the prompt names
   the continuation, the SHA and attempt 1.
2. A separate `retry` — new `ExecutorState`, new connection, **the same DB
   file** — continues from WIP and uses the original baseline
   (`captured_at`, `provenance=initial` unchanged).
3. A `pyproject.toml` edit carried in WIP, and one in a red commit, under
   `strict`: the next invocation refuses until reverted; the baseline row is
   not rewritten.
4. `integration_pr`: dirt on the task branch at run start becomes WIP, not a
   stash.
5. Ownership: matching branch name but a row with another branch or
   namespace → stash; a started task without a row under `strict` → refused
   with no stash, no checkout, no clean; a branch created by the current
   start does not block the first capture.
6. WIP content: a pre-staged runtime file and `tasks.md` are not committed;
   no eligible change → no WIP commit. A path whose HEAD, index and working
   tree all differ → refused before the index is touched; index and working
   tree bytes unchanged. After WIP, a leftover status flip in `pre_start`
   goes to the task rescue stash; a failing rescue after WIP blocks the
   cleanup. At the `integration_pr` start a dirty spec is left to the
   dirty-spec guard.
7. WIP commit failure: refused, work still in the tree, no destructive step.
8. `_unregistered_red` finds a red through a WIP chain and stops at any other
   commit.
9. Candidate: explicit candidate commit over WIP; `gated_sha` is never a WIP
   SHA. No-op: WIP with content + empty last attempt → not no-op; WIP fully
   undone → no-op.
10. `harness trust`: reason required; lock and agent refusals; `--bind-branch`
    required without a row and must be the current branch; replacement is
    audited; binding + snapshot + audit are atomic (a forced failure leaves
    no partial rows).
11. `strict` refusals: unknown surface path, `unreadable`, `recaptured`;
    `off`/`warn` → `strict` without a trusted snapshot is refused.
12. Under `off`, WIP is still saved (workspace row without a snapshot).
13. Lifecycle: DONE and `tdd abandon` delete workspace, snapshot and file
    rows atomically with their own records (a fault injected in the deletion
    rolls back the DONE attempt row / the abandon); `reset` keeps all four
    tables, and a fault during the rebuild leaves the original DB untouched.
14. DB unreadable/unwritable → `instrument` refusal before any destructive
    step, the `integration_pr` fork included.
15. `create_git_branch: false`: the tree is untouched and no WIP is made; a
    second invocation still uses the original persisted baseline; under
    `strict` a started task without a snapshot is refused, and
    `harness trust` works without `--bind-branch`.
16. Directories: a new file under `.github/workflows` recorded at capture is
    a `created` violation (not a trust refusal); a candidate directory
    absent at capture and created later → `created` for its files.

## Contract and release

- `docs/state-schema.md`: the four tables. `schemas/executor-state.schema.json`
  describes the legacy JSON state, not SQLite, and is not extended. Tests pin
  the tables' structure — columns, keys, CHECK constraints — and the
  atomicity of every multi-row write.
- The golden regeneration command (`uv run pytest
  tests/test_json_result_contract.py --update-golden`) is run; no diff is the
  expected result, since no golden lists DB tables.
- `--json-result` is unchanged.
- CLI: `harness trust` documented in CLAUDE.md and README.
- Version 5.0.0 (from 4.5.0). CHANGELOG gains a migration section: a task
  started before 5.0.0 has no trusted harness state; under `strict` its next
  start is refused. The operator first restores the harness and checks it
  (e.g. against the main branch), and only then confirms it with
  `spec-runner harness trust TASK-X --bind-branch <branch> --reason "…"`.
- Publishing follows `docs/release-runbook.md` as a separate step after the
  merge, on the owner's command.

## Out of scope

- Restoring a refused GREEN edit after the last attempt
  (`harness-guard-refused-edit-inherited`).
- A red confirmed on a corrupted oracle.
- A configurable retry mode (`retry_from`).
