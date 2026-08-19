# Executor state schema

This document describes the on-disk and CLI state surfaces that spec-runner exposes to external consumers (primarily Maestro, but also operator tooling and dashboards). Any breaking change to these surfaces requires a **major version bump** and a `BREAKING` note in `CHANGELOG.md`.

**Source of truth:** `src/spec_runner/state.py` — dataclasses `ExecutorState`, `TaskState`, `TaskAttempt`, and enums `ErrorCode`, `ReviewVerdict`.

**Pinned version (Maestro side):** `maestro.spec_runner.SPEC_RUNNER_REQUIRED_VERSION = "2.0.0"`.

## Contract surfaces

spec-runner exposes three distinct surfaces. Each has a separate stability guarantee:

| Surface | Path / form | Consumer | Stability |
|---|---|---|---|
| SQLite state (canonical) | `spec/.executor-state.db` | Maestro (read-only), TUI | **stable** |
| Legacy JSON state | `spec/.executor-state.json` (pre-2.0, renamed to `.bak` after migration) | Old Maestro builds | **deprecated**, read-only fallback |
| `spec-runner run --json-result` stdout | CLI output | Maestro invocation result | **stable** |
| `spec-runner status --json` stdout | CLI output | Dashboards, monitoring | **stable** |

Consumers **must not** rely on any other path, column, log field, or stdout line not listed below.

---

## 1. SQLite state (`.executor-state.db`)

Canonical format since spec-runner 2.0. Uses SQLite with WAL journaling and `busy_timeout=30000`. Read-only consumers should open via URI mode (`file:path?mode=ro`) to avoid write-lock contention with the executor.

### Schema

```sql
CREATE TABLE tasks (
    task_id      TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'pending',
    started_at   TEXT,
    completed_at TEXT
);

CREATE TABLE attempts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id          TEXT NOT NULL REFERENCES tasks(task_id),
    timestamp        TEXT NOT NULL,
    success          INTEGER NOT NULL,   -- 0 or 1
    duration_seconds REAL NOT NULL,
    error            TEXT,
    error_code       TEXT,               -- ErrorCode enum string
    claude_output    TEXT,
    -- Added in later migrations (detect via PRAGMA table_info):
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    cost_usd         REAL,
    review_status    TEXT,               -- ReviewVerdict enum string
    review_findings  TEXT,
    -- Added in v2.3.0 (detect via PRAGMA table_info):
    error_kind       TEXT,               -- classified failure kind (see errors.classify)
    error_stage      TEXT,               -- sub-stage when failure occurred
    -- Added in v2.16.0 (detect via PRAGMA table_info):
    no_op            INTEGER             -- 1 when the task completed without committable changes
);

CREATE TABLE executor_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
```

### `tasks` columns

| Column | Type | Stability | Notes |
|---|---|---|---|
| `task_id` | TEXT | stable | `TASK-###` identifier from `tasks.md` |
| `status` | TEXT | stable | One of: `pending`, `running`, `success`, `failed`, `skipped` |
| `started_at` | TEXT | stable | ISO 8601 timestamp; nullable (task never ran) |
| `completed_at` | TEXT | stable | ISO 8601 timestamp; nullable (not finished) |

### `attempts` columns

| Column | Type | Stability | Notes |
|---|---|---|---|
| `id` | INTEGER | stable | Autoincrement; use `ORDER BY id` for chronological order |
| `task_id` | TEXT | stable | Foreign key to `tasks.task_id` |
| `timestamp` | TEXT | stable | ISO 8601 |
| `success` | INTEGER | stable | 0 = failure, 1 = success |
| `duration_seconds` | REAL | stable | Wall-clock; `>= 0` |
| `error` | TEXT | stable | Human-readable error; nullable on success |
| `error_code` | TEXT | stable | See `ErrorCode` values below; nullable |
| `claude_output` | TEXT | experimental | Captured Claude CLI stdout; may be truncated, format changes allowed |
| `input_tokens` | INTEGER | stable | Prompt tokens; null if unavailable |
| `output_tokens` | INTEGER | stable | Completion tokens; null if unavailable |
| `cost_usd` | REAL | stable | Attempt cost in USD |
| `review_status` | TEXT | stable | See `ReviewVerdict` values below |
| `review_findings` | TEXT | experimental | Free-text review notes |
| `error_kind` | TEXT | experimental | Added v2.3.0, vocabulary corrected in #301. Classified failure kind, nullable. From `errors.classify`: `rate_limit`, `auth`, `network`, `cli_error`, `unknown`. From the execution path: `api_error`, `blocked`, `hook_failure`, `harness_guard`, `timeout`, `interrupted`, `internal_error`, and the three refusal kinds `policy` (a gate answered no), `instrument` (a gate could not answer — the run exits 2), `budget`. The single source is `errors.ERROR_KINDS`; a test compares it to this schema, because the enum had drifted — `blocked` and `api_error` were being written while the schema listed five values |
| `error_stage` | TEXT | experimental | Added v2.3.0. Sub-stage when failure occurred (one of `sync_deps`, `branch`, `exec`, `parse`, `tests`, `lint`, `commit`, `merge`, `review`); nullable. `exec` replaced `codex` in v2.13 — rows written by ≤2.12 may still carry `codex` |
| `no_op` | INTEGER | stable | Added v2.16.0 (#97). 1 when the attempt succeeded with nothing to commit (work already absorbed by earlier tasks); 0/null otherwise. Only meaningful with `auto_commit` on |

**Column detection:** older databases may lack `input_tokens`, `output_tokens`, `cost_usd`, `review_status`, `review_findings`, `error_kind`, `error_stage`, `no_op`. Consumers should probe with `PRAGMA table_info(attempts)` and treat missing columns as `None`.

### `pr_review_comments` (experimental, v2.18.0)

Owned by `spec-runner review-pr` (#102). One row per collected review-bot
comment, keyed `UNIQUE(repo, pr_number, comment_id)` — the durable cursor
that makes the command resumable. Columns: `repo`, `pr_number`,
`comment_id`, `head_sha`, `author`, `path`, `line`, `body`, `url`, `verdict`
(`valid`/`refuted`/`uncertain`; NULL = collected, not verified), `evidence`,
`collected_at`, `verified_at`, plus M2 (v2.19.0): `resolution`
(`fixed`/`refuted`/`needs_human`/`deleted`; NULL = unresolved), `fix_sha`,
`replied_at` (reply idempotency guard). A companion `pr_review_rounds` table
(`UNIQUE(repo, pr_number, head_sha)`) counts bounded rounds. Experimental:
shape may change in minor releases while the loop is in phase M2/M3;
external consumers should not depend on it yet.

### `pr_agent_calls` (experimental, v2.31.0)

The loop's own paid calls (#218 stage 2), **append-only**. A third table in the
same family rather than a nullable `task_id` on `agent_calls`: a review-pr call
belongs to a PR comment, `costs` groups the task ledger by task, and rows
belonging to no task would have to be special-cased by every reader of that
surface.

Columns: `repo`, `pr_number`, `comment_id`, `head_sha`, `round_number` (NULL
before the first round is started — verification runs before any round exists),
`kind` (`verify`/`fix`), `provenance` (`review_pr:<kind>`), `outcome`
(`completed`/`error`/`timeout`), `cost_usd` (**NULL when the CLI reported none**
— unknown is not zero), `input_tokens`, `output_tokens`, `timestamp`.

A row exists exactly when a subprocess **started**. A call refused by the cost
guard before spawning anything is not a call and gets no row; a verifier killed
by a timeout is one, because it was billed for the time it ran.

**Not backfilled.** Review-pr spend before v2.31.0 was recorded nowhere, and no
version reconstructs it. Sessions from earlier versions are missing from this
ledger — incomplete history, not free work — which `costs` says in as many
words whenever it prints the PR section.

### `budget_authorizations` (experimental, v2.31.0)

An operator raising a ceiling (#230 part 2), **append-only** — no row is ever
updated or deleted, and the standing limit for a scope is the newest row.

Columns: `domain_id` (see below), `scope` (`task`/`run`), `task_id` and
`namespace` (both set for a task scope, both **NULL** for a run scope — a
`CHECK` enforces it), `previous_limit_usd`, `new_limit_usd`,
`recorded_spend_usd`, `unmeasured_calls`, `actor`, `reason`, `timestamp`, and
(v2.32.0) `reserve_stage` / `reserve_usd` — both NULL or both set. A database
created by this version enforces that with a `CHECK`; one **upgraded** to it
has the columns without the constraint, because SQLite's `ALTER TABLE ... ADD
COLUMN` cannot add one to an existing table. The invariant is therefore
enforced by the writer, on both. A reserve withholds that much of the ceiling from every call whose
provenance is not that stage's (#267): review is the last paid call of an
attempt, so it is structurally the one the remainder starves. Added by
migration, so authorizations written before it read as "no reserve", which is
what they meant.

`recorded_spend_usd` and `unmeasured_calls` capture *what the human was looking
at*: $6.00 authorised against a proven $2.53 means something different from
$6.00 against a floor of $2.53 with unpriced calls behind it.

**The budget domain is the state file.** `executor_meta.budget_domain_id` is
minted on first use and stamped on every authorization, so a new state file
inherits no authorization and no spend. That is mechanical rather than a rule
to remember — in the pilot, three attempts ran against three state files and
the cap that refused had never seen the earlier spend. Moving a pilot to a new
state file therefore requires an explicit opening balance or a fresh decision;
archived state files are evidence, never runtime inputs.

### `phase_results` / `phase_waivers` (experimental, slice 0)

Added by slice 0 of the lifecycle contract (#164 / #141 Part A). **Nothing
gates on them yet** — they are additive record-keeping, and a project that opts
into nothing sees unchanged execution, terminal state and external contracts.

`phase_results` is **append-only**: one row per observed outcome of one phase.
A phase runs again on a retry, and the earlier verdicts are evidence, not
noise. Columns: `task_id`, `phase` (one of the `error_stage` vocabulary),
`outcome`, `detail`, `timestamp`.

`outcome` is a `PhaseOutcome`:

| Value | Means |
|---|---|
| `pass` | ran; its expectation held |
| `expected_fail` | ran; failed exactly as it was supposed to (TDD's confirmed red) |
| `unexpected_fail` | ran; failed some other way |
| `not_run` | ran, but produced no usable verdict (timeout, empty, no marker) |
| `error` | could not run — the instrument itself broke |
| `skipped` | deliberately not executed |

Not every phase can produce every value: the admissible set is declared per
stage in `spec_runner.phases.ALLOWED_OUTCOMES` (`expected_fail` is meaningful
for a test run and meaningless for `commit`).

`phase_waivers` records an **operator** overriding an observed outcome —
`task_id`, `phase`, `waived_outcome`, `reason`, `actor`, `timestamp`,
`provenance`. A waiver is not an outcome: the observed result stays in
`phase_results`, so a report showing green for a waived phase can show that it
was waived and by whom. The harness never writes one.

Experimental: shape may change while the later slices land; external consumers
should not depend on it yet.

### `gate_verdicts` (experimental, #164)

One row per pre-terminal policy gate evaluation. Columns: `task_id`,
`gate_id`, `checkpoint_sha`, `config_hash`, `status`, `detail`, `timestamp`.

`status` is a `GateStatus` — `satisfied`, `unsatisfied`, or
`instrument_error`. Three, not two: "the gate says no" and "the gate could not
answer" have different owners, and only the second is retried.

The load-bearing detail is the key. A lookup is
`(task_id, gate_id, checkpoint_sha, config_hash)` and deliberately **not**
"the latest verdict for this task": a verdict is a statement about a specific
tree under a specific policy, and it stops being one the moment either moves.
`config_hash` covers only the policy-bearing keys listed in
`spec_runner.gates.POLICY_KEYS`, so an unrelated config edit does not
invalidate a verdict — and a relevant one does.

Gates run after the checkpoint commit and before merge. The commit is not
withheld: a stable SHA is what the gate is evaluated *against*. What an
unsatisfied gate withholds is progress past the checkpoint.

Dormant until a consumer registers — with an empty registry no SHA is
resolved, no row is written, and behaviour is unchanged. The first consumer
is the review policy (#157), the second TDD's confirmed red (#141).

Experimental: shape may change while the consumers land; external consumers
should not depend on it yet.

### `red_checkpoints` (experimental, #141)

A durable, **verified** claim that one test failed on one tree. Columns:
`task_id`, `namespace`, `commit_sha`, `baseline_sha`, `selector`,
`environment_id`, `execution_mode`, `config_hash`, `outcome`, `timestamp`.

Each field earns its place:

| Field | Why |
|---|---|
| `commit_sha` | without it replay is impossible, and "red confirmed" is trust in the agent's report — the thing the checkpoint replaces |
| `selector` | the **full** node id. `-k`-style names match several tests, and a checkpoint matching several proves nothing about the one |
| `baseline_sha` | red *against what* |
| `namespace` | identical `TASK-NNN` ids from different workstreams collide once their branches meet |
| `environment_id` | `<lockfile>:<hash>`, or `unpinned`. A replay you cannot identify proves nothing about the run it claims to reproduce |
| `execution_mode` + `config_hash` | a checkpoint written under one policy must be distinguishable from one written under another, or replay silently re-interprets old evidence under today's rules |

Reads are keyed on `(task_id, namespace)`: a checkpoint from another
workstream is not this task's evidence, however identical the id.

`outcome` is a `RedOutcome` — `expected_fail`, `not_red`, or `unverifiable`.
Three, not two: "the test passes" is a fact about the code, "we could not find
out" is a fact about us, and only the first refutes the claim.

Experimental: nothing reads this yet; the gate that consumes it is slice 1c.

### `tdd_claims` (experimental, #141)

The byte-lock behind a confirmed RED. Columns: `namespace`, `task_id`,
`checkpoint_id`, `checkpoint_sha`, `path`, `blob_sha`, `created_at`, `status`.

Its own table rather than a JSON column on `red_checkpoints`: enforcement
queries by `(namespace, status, path)` **across tasks**, a claim's status
changes independently of the checkpoint that created it, and two tasks claiming
one path is precisely the case that has to be queryable rather than parsed out
of every row.

- `path` is canonical and project-relative. Symlinks, paths outside the
  repository and non-regular files are refused, not normalised.
- `blob_sha` is git's blob SHA over the file's **raw bytes** — no line-ending
  normalisation, since a claim that tolerates a CRLF flip is not a byte-lock.
- `status` is `active` · `superseded` · `abandoned`. Nothing is deleted; a
  retired claim is still evidence.
- `checkpoint_id` is derived (a short hash of namespace, task, commit,
  selector and timestamp), not the rowid: it has to be typeable in
  `--checkpoint <id>` and survive a state rebuild.

Enforcement reads every **active** claim in the namespace and checks it against
the **candidate commit**, never the working tree. Violations are distinguished
as modified / deleted / renamed — all three block, but they send an operator
looking in different places.

Experimental: the remedies that retire a claim are slice 3.

### `tdd_remedies` (experimental, #141)

One row per operator remedy. Columns: `namespace`, `task_id`, `checkpoint_id`,
`operation` (`abandon` / `repair`), `reason`, `actor`, `timestamp`,
`new_checkpoint_id`.

A remedy is an **authority decision**, not an observation — hence the mandatory
`actor` and `reason`, and hence the row being written fail-closed like a claim:
a remedy nobody can find is indistinguishable from one that never happened.

`red_checkpoints` gained a `status` column (`active` / `superseded` /
`abandoned`) in the same slice, and `red_checkpoint()` returns only the
**active** one. Nothing is deleted: `abandon` marks a checkpoint and its claims
abandoned, `repair` marks them superseded and records a new lineage whose
`baseline_sha` is the commit it replaces. Prior gate verdicts go stale by
construction, since a verdict is keyed on the tree it judged.

### `agent_calls` (experimental, #141)

One row per agent invocation whose cost has nowhere else to live. Columns:
`task_id`, `provenance`, `input_tokens`, `output_tokens`, `cost_usd`,
`timestamp`.

`provenance` is `red_authoring`, `review` (the single-pass reviewer) or
`review:<role>` (one row per parallel review role — never one aggregate, which
could not say which role was expensive or which was never measured). The
GREEN/exec pass keeps its cost on the attempt row, where the schema above
already publishes it, so the ledger holds only the calls that were previously
invisible — which is why `total_cost()` can sum both without double counting.

`cost_usd` is **nullable, and NULL is not zero**: a reviewer killed by a
timeout or an account limit was billed for as long as it ran, and recording
0.0 would make that indistinguishable from a cheap call in every later sum.
`ExecutorState.unmeasured_calls()` counts the NULL rows, and `costs --json`
publishes that count per task and overall, so a total can be read as the floor
it is. A call that never launched (missing binary) writes no row at all.

Added because the TDD RED pass parsed its CLI result and kept only the text:
its tokens and cost were discarded, so `spec-runner costs` reported `$0.00`
for a run that had made an extra paid call per task. A failed authoring
attempt is recorded too — money spent on a call that produced nothing usable
is still spent.

### `tdd_phases` (experimental, #141)

The TDD lifecycle as a recorded machine. Columns: `task_id`, `namespace`,
`phase`, `detail`, `timestamp`. Append-only: where a task has *been* is
evidence, and that includes refused transitions, recorded as
`refused:<target>`.

```
ready → red_authoring → red_verifying → green_implementing
      → green_verifying → refactoring (skipped) → done
```

Two properties worth knowing before reading it:

- **`refactoring` is materialised and never executed.** Its `detail` is
  `skipped`, so a reader is not left wondering whether something ran. An
  automatic refactor pass was deliberately not approved.
- **Backwards transitions are legal.** A remedy sends a task back to authoring
  and a retry re-enters implementation; only reaching a GREEN phase without a
  red is refused, because that is the one transition the contract is about.

Bookkeeping, not enforcement: the gates decide and read checkpoints and claims,
which are written fail-closed. This table remembers.

### `executor_meta` key-value pairs

| Key | Value type | Stability | Notes |
|---|---|---|---|
| `consecutive_failures` | int (stored as TEXT) | stable | Resets to 0 on any task success |
| `total_completed` | int (stored as TEXT) | stable | Monotonic counter |
| `total_failed` | int (stored as TEXT) | stable | Monotonic counter |
| `second_pass_fail_tasks` | comma-joined TEXT | experimental | Added v2.3.0. Task IDs that failed again across runs; empty string when none |
| `last_run_stop_reason` | TEXT | experimental | Added v2.3.0. One of `completed`, `task_failed_stop`, `dependency_blocked_after_skip`, `state_spec_mismatch`, `max_consecutive_failures`, `budget_exceeded`, `validation_failed`, `error_<kind>`. Enumerated in `spec_runner.cli.RUN_STOP_REASONS` (the `error_<kind>` family is dynamic). Only `completed` exits 0 |
| `last_run_stop_detail` | TEXT | experimental | Added v2.3.0. Free-text detail for the stop reason (e.g. `12/2`, or an error message) |

### `ErrorCode` enum values

Source: `src/spec_runner/state.py:ErrorCode`.

Stable: `TIMEOUT`, `RATE_LIMIT`, `TEST_FAILURE`, `LINT_FAILURE`, `TASK_FAILED`, `TASK_BLOCKED`, `HOOK_FAILURE`, `INFRASTRUCTURE`, `BUDGET_EXCEEDED`, `REVIEW_REJECTED`, `INTERRUPTED`, `UNKNOWN`.

`INFRASTRUCTURE` (added F-2) means **the instrument broke** — a pre-terminal
policy gate could not answer, so the run cannot say whether the work is good.
Distinct from `HOOK_FAILURE`, which is a gate that *said no*. A run whose only
failures are `INFRASTRUCTURE` exits **2**; one with any concrete failure exits
1, because something actionable is the more useful thing to report.

`TASK_BLOCKED` (added #140) is a *deliberate* escalation: the agent emitted
`TASK_BLOCKED: <reason>` to say the task cannot be done within the rules it was
given and needs an operator — as opposed to `TASK_FAILED`, "I did not manage
it". It is terminal (never retried) and `error` carries the agent's own wording
verbatim. A consumer distinguishing "needs a human" from "worth another run"
should key off this code; per the note above, unknown codes must be treated as
`UNKNOWN` rather than raising.

Consumers should treat unknown values as `UNKNOWN` rather than raising — new codes may be added in minor releases.

### `ReviewVerdict` enum values

Source: `src/spec_runner/state.py:ReviewVerdict`.

Stable: `passed`, `fixed`, `failed`, `skipped`, `rejected`, `not_run`, `error`.
(Lowercase — stored as-is.)

`not_run` and `error` were added in #138 and are the reason a consumer must not
read "not `failed`" as "reviewed and fine":

| Value | Means |
|---|---|
| `passed` | the reviewer answered and found nothing |
| `fixed` | the reviewer answered, found issues, and fixed them |
| `failed` | the reviewer answered and found issues it did not fix |
| `not_run` | **no verdict** — timed out, returned nothing, or said nothing recognizable |
| `error` | the review machinery failed (CLI error, rate limit, exception) |
| `skipped` | the stage was deliberately not run |

Before #138 a review that produced no recognizable marker was recorded as
`passed`, and a timeout as `failed`; both are now `not_run`. As with
`ErrorCode`, treat an unknown value as "unknown" rather than raising — new
values may be added in minor releases.

Since slice 0, `ReviewVerdict` is also **readable as a phase outcome plus a
review-specific detail** (`spec_runner.phases.review_verdict_to_phase`):
`passed` and `fixed` are both `pass`, differing only in detail, so a consumer
that just needs "did the phase hold" reads the outcome and stops. The stored
wire values here are unchanged — this is a reading, not a migration.

### Read-only access pattern

```python
import sqlite3

uri = f"file:{spec_dir / '.executor-state.db'}?mode=ro"
with sqlite3.connect(uri, uri=True) as conn:
    conn.row_factory = sqlite3.Row
    for row in conn.execute("SELECT task_id, status FROM tasks"):
        ...
```

### Concurrent-write safety

spec-runner acquires its own connection for writes. WAL journaling means readers never block writers and vice versa. Readers using `mode=ro` **must not** call `PRAGMA journal_mode` or any write operation.

---

## 2. Legacy JSON state (`.executor-state.json`)

**Status: deprecated. Read-only fallback only.**

Pre-2.0 spec-runner wrote state as JSON. On first run of 2.0+, the JSON file is migrated to SQLite and renamed to `.executor-state.json.bak`. New executor runs never write JSON.

Consumers should only read this file when the SQLite file does not exist (pre-migration snapshots, archived workspaces).

### Format

```json
{
  "tasks": {
    "TASK-001": {
      "status": "success",
      "started_at": "2026-04-16T10:00:00",
      "completed_at": "2026-04-16T10:05:00",
      "attempts": [
        {
          "timestamp": "2026-04-16T10:00:00",
          "success": true,
          "duration_seconds": 300.0,
          "error": null,
          "error_code": null,
          "claude_output": "..."
        }
      ]
    }
  },
  "consecutive_failures": 0,
  "total_completed": 1,
  "total_failed": 0
}
```

Field types and semantics match the SQLite columns above. Token/cost/review fields were added after the JSON-era; legacy JSON files will not contain them.

---

## 3. `spec-runner run --json-result` stdout

Emitted after a run completes. Used by Maestro to capture per-task outcomes without reopening the state file.

### Shape

Single task (one element list) → JSON object. Multiple tasks → JSON array.

```json
{
  "task_id": "TASK-001",
  "status": "done",
  "attempts": 1,
  "cost_usd": 0.42,
  "tokens": {"input": 1500, "output": 800},
  "duration_seconds": 120.3,
  "review": "passed",
  "exit_code": 0
}
```

### Fields

| Field | Type | Stability | Notes |
|---|---|---|---|
| `task_id` | string | stable | `TASK-###` |
| `status` | string | stable | `done` (success), `failed`, or `unknown` (task never reached state) |
| `attempts` | int | stable | Total attempt count |
| `cost_usd` | float | stable | Rounded to 2 decimals; sum across attempts |
| `tokens.input` | int | stable | Sum across attempts |
| `tokens.output` | int | stable | Sum across attempts |
| `duration_seconds` | float | stable | Rounded to 1 decimal; sum across attempts |
| `review` | string | stable | Last attempt's review verdict, or `skipped` |
| `error` | string | stable | Present only on failure; truncated to 200 chars |
| `no_op` | bool | stable | Added v2.16.0 (#97). Present **only when true**: the task completed successfully without any committable changes (work already absorbed by earlier tasks). Absent on every other task — consumers that don't know the key see unchanged output |
| `exit_code` | int | stable | 0 on success, 1 on failure |

### Empty-tasks edge case

If no tasks were ready to run:

```json
{"tasks": [], "message": "No tasks ready to execute"}
```

---

## 4. `spec-runner status --json` stdout

Aggregate snapshot for dashboards. Does not include per-task details.

### Shape

```json
{
  "total_tasks": 12,
  "completed": 8,
  "failed": 1,
  "running": 0,
  "not_started": 3,
  "total_cost": 12.34,
  "input_tokens": 45000,
  "output_tokens": 22000,
  "budget_usd": 50.0
}
```

All fields are **stable**. `budget_usd` is `null` when no budget is configured.

---

## Breaking change policy

A change is **breaking** if it:

- removes or renames a column, table, JSON key, or CLI flag listed above
- changes the semantic meaning of a field (e.g. redefining `status` values)
- changes a type (e.g. int → string)
- changes a stored value format (e.g. ISO 8601 → Unix epoch)
- drops a previously-documented `ErrorCode` or `ReviewVerdict` value

A change is **non-breaking** if it:

- adds a new column, table, JSON key, or CLI flag
- adds a new `ErrorCode` or `ReviewVerdict` value (consumers must tolerate unknowns)
- improves internal storage (indexes, triggers) without touching the surface above

Breaking changes require:

1. Major version bump (`2.x.y → 3.0.0`)
2. `CHANGELOG.md` entry prefixed with `BREAKING:`
3. Golden-test update (`tests/test_json_result_contract.py`)
4. Notification to Maestro (bump `SPEC_RUNNER_REQUIRED_VERSION`)

---

## Related files

- `src/spec_runner/state.py` — dataclasses and SQLite schema
- `src/spec_runner/cli.py` — `--json-result` emitter
- `src/spec_runner/cli_info.py` — `status --json` emitter
- `schemas/executor-state.schema.json` — generated JSON Schema
- `tests/fixtures/maestro-interop/` — golden fixtures for contract tests
- Maestro side: `maestro/maestro/spec_runner.py`, `maestro/maestro/models.py` (ExecutorState, ExecutorTaskEntry, ExecutorTaskAttempt, ExecutorTaskStatus)
