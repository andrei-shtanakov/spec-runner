# Claim and remedy contracts — design amendment

**Status:** design only. No code ships with this document.
**Amends:** [TDD lifecycle design](2026-08-11-tdd-lifecycle-design.md) §3.5 (pilot findings 1–3) and §4 (slices 2–4).
**Issue:** #141. **Depends on:** #164 (merged), slice 1 (merged).
**Decided by the owner, 2026-08-11.** Where this differs from the issue text, this is authoritative.

---

## 0. Why an amendment rather than new prose in the design

Slices 2–4 were one line each in the original slice table. That was enough to
order them and not enough to build them: "byte-lock across all claimed files"
does not say what a claim *is*, who owns it, when it is checked, or how it is
released. This fills that in, and splits slice 4, whose second half turned out
to be a design question wearing an implementation's clothes.

Two release conditions are part of the decision, not commentary:

- **Slice 2 does not ship without slice 3.** They are built in that order —
  the lock has to exist before there is anything to remedy — but they form one
  release block. A byte-lock without typed remedies leaves exactly one cure for
  a mistake in a frozen test: rewriting history. The pilot did that twice in a
  single phase, each time with a state freeze and a second signature. Shipping
  the lock alone would make that the supported path.
- **Slice 4 does not ship as one PR.** See §3.

---

## 1. Slice 2 — the claim contract

### 1.1 What a claim is

A claim is a statement that a specific file, at specific bytes, is frozen
because a confirmed RED depends on it.

| Field | Notes |
|---|---|
| `namespace` | the workstream, as in the RED checkpoint |
| `task_id` | who claimed it |
| `checkpoint_id` | which checkpoint the claim descends from |
| `checkpoint_sha` | the commit that checkpoint is about |
| `path` | canonical, **project-relative** |
| `blob_sha` | git blob SHA over the **raw bytes** |
| `created_at` | |
| `status` | `active` · `superseded` · `abandoned` |

`checkpoint_id` does not exist yet — `red_checkpoints` has only an
autoincrement rowid. Slice 2 adds a stable one, derived as a short hash of
`(namespace, task_id, commit_sha, selector, created_at)`. A rowid would be
unusable in `--checkpoint <id>` on the command line and would not survive a
state rebuild.

### 1.2 Storage: a separate table, not a JSON column

Claims live in their own table (or append-only claim events), **not** as a JSON
list inside `red_checkpoints`. Owner's decision, and the reasons are the ones
that always apply to a list in a column: enforcement queries by
`(namespace, path, status)` across tasks, which a JSON blob cannot index; a
claim's `status` changes independently of the checkpoint that created it; and
two tasks claiming the same path is precisely the case that must be
*queryable*, not parsed out of every row.

### 1.3 Where the initial claim set comes from

From the files of the **full selector** in the RED checkpoint.

A pytest node id `tests/test_x.py::TestY::test_z` names exactly one file, so
the initial set is one file. **This is a real limitation and should be stated
rather than discovered:** a test that depends on a fixture in `conftest.py`
does not claim that `conftest.py`, so editing the fixture is not blocked even
though it can turn the red green. Widening the set — by import graph, by
coverage — is a separate decision with its own cost, and guessing at it here
would be worse than the honest gap.

### 1.4 Path validation

Rejected, not normalised:

- a path that resolves outside the repository;
- a symlink;
- anything that is not a regular file.

Each of these breaks the thing a claim is for. A symlink's bytes are the link
target's, so hashing it freezes something the claim does not name; a path
outside the repo is not in any commit, so there is nothing to check against.

Hashing is over **raw bytes**, with no line-ending normalisation. A claim that
tolerates a CRLF flip is not a byte-lock.

### 1.5 Enforcement

**All active claims in the namespace, not only the current task's.** The pilot's
first version checked only the current selector, so neighbouring tests were
protected by a sentence in the agent's prompt rather than by the instrument.
That is the finding this slice exists for.

Checked **against the candidate commit**, never the mutable working tree — the
same reason the RED replay judges a commit. A check against the working tree
answers a question about a moment that has already passed by the time anything
acts on it.

Run at **two points**: before GREEN, and again before merge. Same reasoning as
the RED gate: "do not build on a violated claim" and "do not merge one" are the
same question at two moments, and one registration answering both is better
than two things to keep in step.

Three violations, distinguished:

| | Meaning |
|---|---|
| **modified** | path present, `blob_sha` differs |
| **deleted** | path absent, and its blob is not reachable at another path |
| **renamed** | path absent, its blob present elsewhere |

Rename detection is git's similarity heuristic (`--find-renames`), so an
edit-plus-move can read as a delete-plus-add. That is a known imprecision; both
readings block, so the imprecision affects the *message*, not the decision.

A conflict between a new checkpoint and another task's active claim **blocks**.
Claiming the same path with the same blob is **idempotent** — a re-run must not
be a violation, and must not stack duplicate rows.

### 1.6 Lint before the checkpoint is fixed

Pilot finding 3: after a checkpoint the file is byte-immutable, so lint debt
that got in is uncurable without an operator and hits every later task in the
suite. The same I001 trap fired three times in one wave.

So: the claimed file is linted **before** the checkpoint is recorded, and a
failure means no checkpoint — the red is re-authored.

Open: which command. `config.lint_command` is a shell string and may be
composite, and narrowing it to one file has the failure mode `#139` already
documented — guessing which component of `a && b && c` takes a path is how you
run the wrong program and believe its answer. The likely answer is to reuse
`git_ops.build_scoped_test_command`'s posture: narrow when it is safe, and when
it is not, run the whole declared lint gate rather than guess. Decide at
implementation, do not invent a second narrowing rule.

---

## 2. Slice 3 — the remedy contract

**Built after slice 2, released together with it.** Not a contradiction: the
lock has to exist before there is anything to remedy, but a release containing
the lock and not the remedies would make rewriting history the only cure for a
mistake in a frozen test. Two PRs, one release block — no version ships with
§1 and without §2.

```
spec-runner tdd abandon TASK-ID --checkpoint <id> --reason <text>
spec-runner tdd repair  TASK-ID --checkpoint <id> --commit <sha> --reason <text>
```

`--checkpoint` is **compare-and-swap** against the currently active checkpoint:
if it is not the active one, the command refuses. Without that, a remedy issued
against what the operator last saw silently applies to whatever arrived since.

### 2.1 Semantics

- `actor` and `timestamp` are recorded. A remedy is an authority decision, and
  an authority decision without an author is an anonymous one.
- **Nothing is deleted.** Old checkpoints and claims are marked `superseded` /
  `abandoned` and stay. History is never rewritten to fix a frozen test — that
  is the practice being replaced.
- `abandon` returns the task to RED authoring. The red was no good; start again
  honestly, with the commit still in history.
- A red commit the gate rejected is **evidence, not litter** (#261). It stays
  on the branch, and the next authoring pass may adopt it rather than demand a
  fresh diff — only when HEAD's own subject names this task and the reported
  selector, and no checkpoint was ever recorded for it. Deleting it would be
  the other way to end the starvation, and the wrong one: it is the agent's
  work.
- A claim's life ends at the **terminal gate**, not at the end of time (#260).
  Completion retires the task's claims as `released` — a status of its own,
  because nothing went wrong. Holding them past completion froze the file for
  the whole workstream, so every later legitimate edit wedged every subsequent
  task. `tdd release` is the same act performed by an operator on state written
  before this rule, and it is admissible only once the lifecycle reached DONE:
  releasing a live task's lock is the laundering the lock exists to prevent.
- `repair` is **not** "allow these new bytes". It opens a **new lineage**: a
  fresh checkpoint descending from the repaired commit, with the previous one
  superseded and linked.
- After `repair`, a **new RED replay is mandatory**. Accepting repaired bytes
  without re-demonstrating the red would make `repair` a way to launder an
  unverified claim — the exact hole the whole contract closes.
- The replay runs **before anything is written** (#263). Only `expected_fail`
  supersedes the standing lineage; a repaired test that passes, or a replay
  that cannot run, leaves the checkpoint and its claims exactly as they were.
  This is also the whole of the post-green protection: a test that the
  implementation satisfies cannot re-establish a red, and the operator is sent
  to `resume`. It is deliberately **not** a phase-history check — "an
  implementation call was started" is not "an implementation exists", and an
  attempted-and-reverted green must leave `repair` available.
- Gate verdicts from before the remedy become **stale**. They are statements
  about a tree and a policy that no longer apply, which is #164 criterion 5
  arriving here on schedule.
- Repeating the same remedy is **idempotent**: same checkpoint, same operation,
  no second lineage.
- A remedy is **refused while the task is running**. Concretely: the executor
  lock is held, or the task's state is `running`. Mutating a checkpoint under a
  live run is how two writers produce a history neither intended.
- **One task's remedy does not release another task's independent claim** on the
  same file. Two claims on one path are two facts; resolving one leaves the
  other standing.

### 2.2 "The agent cannot call remedy" — what is actually enforceable

The requirement is right and the wording needs care, because I checked: agent
subprocesses are spawned with no `env=` at all, so they inherit the operator's
environment and **nothing currently distinguishes an agent's shell from a
person's**.

Proposal: the runner sets a marker (`SPEC_RUNNER_AGENT=1`) in the child
environment, and `tdd abandon|repair` refuse when it is present.

State plainly what that is worth: the agent runs arbitrary shell, so it can
unset the variable. This is a **guardrail against the ordinary path**, not a
security boundary, and calling it one would be the kind of claim that gets
believed. The real boundary is that a remedy is an operator's decision with an
operator's name on it; if that is not enough, the answer is an out-of-band
approval channel, not a bigger env var.

---

## 3. Slice 4 — split

GREEN_IMPLEMENTING and GREEN_VERIFYING fall out of the existing executor and
deterministic gates. `REFACTORING` does not, and the questions are not
cosmetic: is it a mandatory second LLM call or a logical stage; what is its
input and its exit criterion; may it modify claimed tests; does review repeat
after it; what bounds cost and cycle count; and what happens when the
implementation improves with no diff.

### 3a (approved for v1)

```
GREEN_IMPLEMENTING → GREEN_VERIFYING → REFACTORING: SKIPPED
→ pre-terminal gates → merge → DONE
```

Materialise the phase transitions and recovery. Do **not** run a refactor agent
automatically. `REFACTORING` exists as a phase whose outcome is `skipped` —
which the vocabulary already has a word for, and which is honest: the stage was
deliberately not executed.

### 3b (not approved; needs evidence first)

A real refactor pass, added later as an explicit opt-in, once the battle test
says what it should do. The risk being avoided is specific: shipped inside 4,
the word `REFACTORING` quietly becomes a new expensive and ill-defined agent
stage that nobody chose.

---

## 4. Order

1. Update #141 with current status and canonical decisions — **done**.
2. This amendment.
3. Slice 2.
4. Slice 3.
5. Battle test: mutation, delete, rename, shared claim, abandon, repair,
   crash-resume.
6. Release 2.25.0.
7. Slice 4a — the GREEN state machine, no automatic refactor pass.
8. Decide the fate of full `REFACTORING` on the evidence.

---

## 5. Open, flagged rather than invented

1. **Claim-set width** (§1.3). One file per selector today; a `conftest.py`
   fixture is reachable and unclaimed.
2. **Which lint command** narrows to a claimed file (§1.6), given #139.
3. **What identifies "the task is running"** for the remedy refusal (§2.1) —
   the executor lock, the state row, or both. They can disagree after a crash,
   and the answer decides whether a remedy is possible during recovery.
4. Whether `superseded` claims should remain queryable by default in
   `status`/reporting output, or only on request. They are evidence, but a
   report that shows every superseded claim from every repair becomes unreadable
   fast.
