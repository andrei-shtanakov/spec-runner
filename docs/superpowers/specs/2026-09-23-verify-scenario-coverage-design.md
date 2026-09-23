# Verify-first scenario coverage — design (#402)

Status: **agreed in chat 2026-09-23, awaiting review of this document.**
Issue: spec-runner#402 (`slug: verify-task-baseline-evidence-guard`, from
devtools).

## 1. Problem, re-measured

The issue: a workstream carried seven `verify_first` tasks whose `**Verifies:**`
targets either did not exist or existed but belonged to another workstream and
did not carry the declared scenarios; `validate` passed and paid calls burned.

Measured against the code (read-only survey, 2026-09-23), the two halves cost
very differently:

- **A missing target costs nothing.** The live verify run is the task's first
  action, before any paid call (`execution._run_verify_first_phase`). A missing
  file or an empty selection is refused there (`instrument_error`), the
  attempt records cost 0.0, and retries repeat only the unpaid run.
- **An existing, green, foreign target is what burns.** The entry run comes
  back green, the group is frozen, the paid implementation pass runs, then the
  pre-review re-verify is green again (the foreign tests still pass) and the
  paid review runs. Nothing ever asks whether the green tests are *about* the
  task's scenarios.

So the defect to close is **coverage**, not existence. `validate`'s treatment
of a missing file stays as FR-07/BEH-09 of the verify-first workstream
requires: a warning, because the commit is judged, not the working tree
(`workstreams/verify-first-file-scope-group-targets-20260908/spec/10-requirements.md`
FR-07; `15-behaviour-spec.md` BEH-09; pinned by five tests).

What spec-runner has today:

- no structured notion of a task's scenarios: BEH ids reach `tasks.md` only
  as prose and checklist lines; `**Delivers:**` (emitted by devtools) carries
  **obligation** ids, not scenarios, and spec-runner ignores it;
- scenario labels in test files are free text — class names
  (`TestBEH08…`), docstrings (`kind: e2e — BEH-08`), module docstrings;
- BEH ids are **workstream-local** (`BEH-09` is defined in six workstreams);
- a node-id element of `**Verifies:**` gets **no** filesystem check in
  `validate` — not even the warning a file target gets.

## 2. Decisions (owner, 2026-09-23)

1. A new task field `**Scenarios:**` declares the scenarios a verify group must
   carry. `**Delivers:**` is not reused.
2. Labels are **unqualified** BEH-style ids. The contract states plainly: a
   label's presence proves the declared coverage is *claimed* by the file, not
   that the file belongs to this workstream.
3. A task without `**Scenarios:**` keeps today's behaviour exactly: the
   coverage check does not apply to it. No existing tasks.md changes meaning.
4. A missing file stays a `validate` **warning** (FR-07).

## 3. The field

```markdown
**Mode:** verify_first
**Verifies:** tests/test_verify_first_declaration.py
**Scenarios:** BEH-09, BEH-10, BEH-09a
```

- One line, comma-separated ids of the form `[A-Z]+-\d+[a-z]?` (the suffix
  exists in practice: `BEH-09a`). Stored in declared order on
  `Task.scenarios: list[str] | None` — `None` when the line is absent.
- A present but empty line, or an id of another shape, is a `validate`
  **error** quoting the line (the same contract as `**Verifies:**`: stored as
  written, judged later, never guessed).
- Declared on a task whose resolved mode is not `verify_first`: a `validate`
  **warning** — "not checked outside verify_first". Not an error: the ids are
  still true information about the task.
- Documented in `spec/FORMAT.md` beside `Verifies`.

## 4. What counts as coverage

For each declared scenario id, at least one **file** of the verify group must
carry it as a whole token: `(?<![A-Za-z0-9])BEH-09(?![0-9a-z])` — so `BEH-09`
matches `BEH-09:` and `TestBEH09…` does **not** (it has no hyphen; see below),
`BEH-091` and `BEH-09a` do not match `BEH-09`.

- The group's files are the paths of its elements — a file target's own path,
  a node id's file (`path::…` → `path`). Coverage is judged **per file**, not
  per test function: the unit a label reliably lives in today is the file
  (module docstring, class docstrings). This is weaker than per-test and is
  written into the contract.
- Class names like `TestBEH08…` do not carry the hyphen and are not labels.
  The docstring convention (`kind: <kind> — BEH-NN`) is the label. Accepting
  name-mangled forms would guess, and a guessed match is exactly the failure
  this check exists to stop.
- Every declared scenario must be covered. The refusal names each uncovered
  scenario and the files that were searched.
- **Limit, stated in the contract:** ids are unqualified and repeat across
  workstreams, so a foreign file carrying its own `BEH-09` satisfies the check.
  The check catches a group that claims **nothing** about the task's
  scenarios — the measured case — not a group that claims them falsely.

## 5. Where it is enforced

**At the live entry run, against the commit, before any paid call.**

In `_run_verify_first_phase`, after the entry run has reached a verdict
(`green` or `test_failure`) and before the group freeze and the first paid call
(implementation or RED authoring):

- if `task.scenarios` is `None` → nothing (decision 3);
- read each group file **at the entry run's commit** (`git show
  <sha>:<path>`), never the working tree;
- any scenario uncovered → `Refusal(kind=POLICY, terminal=True)`: the task is
  recorded as refused and not retried — the same commit gives the same answer,
  so a retry would only repeat it. Exit 1, like any policy refusal.
- a file that cannot be read at that commit → the entry run would already have
  failed for it; if it has not, `Refusal(kind=INSTRUMENT)` naming the path.

`instrument_error` entry runs are unchanged: nothing ran, and the existing
refusal already stops before any paid call.

Why here and not in `validate`: the money was spent between a green entry run
and the paid pass, and only a check at that point judges the commit that will
actually be worked on. `validate` sees a working tree.

## 6. `validate`

Early feedback only — it cannot refuse on the tree (FR-07):

- for a task with `**Scenarios:**`, each uncovered scenario in the **working
  tree's** group files → a **warning** naming the scenario;
- the gap found in the survey: a node-id element whose file does not exist in
  the working tree → the same **warning** a missing file target already gets
  (today it gets nothing). FR-07's rule, applied consistently.
- Zero runner invocations, as BEH-09 requires.

## 7. Unchanged

- Tasks without `**Scenarios:**`, every non-`verify_first` task, `standard`
  and `tdd` execution — byte-for-byte the same path.
- FR-07/BEH-09 — missing file is a warning; no amendment to that workstream.
- No state-DB, `--json-result` or schema change. Additive → minor.

## 8. Acceptance

- The issue's shape: a verify task whose group is an **existing, green file
  from another workstream** with none of the declared scenarios, `**Scenarios:**
  BEH-09` declared → refused at entry, naming `BEH-09`, with **no** paid call
  made (the agent seam is never reached) and no retry.
- The same group carrying `BEH-09` in a docstring → proceeds as today.
- A group covering `BEH-09` but not `BEH-10` → refused naming `BEH-10` only.
- `BEH-091` / `BEH-09a` / `TestBEH09` in the file do not cover `BEH-09`.
- Coverage read at the commit: a label present only in the uncommitted working
  tree does not count at entry (but silences the `validate` warning).
- No `**Scenarios:**` → today's behaviour (existing verify-first tests pass
  unchanged).
- `validate`: malformed / empty `**Scenarios:**` → error; declared outside
  `verify_first` → warning; uncovered in the tree → warning; missing node-id
  file → warning.

## 9. Out of scope / follow-ups

- devtools emitting `**Scenarios:**` from each DT's own scenarios (it already
  has them per task) — their change: an issue to devtools after merge.
- Qualified labels (`<ws>#BEH-09`) — rejected for now (decision 2); would need
  a test-label convention change across repos.
- Per-test (rather than per-file) coverage.
