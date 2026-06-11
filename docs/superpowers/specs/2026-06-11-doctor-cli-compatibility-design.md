# Design: `spec-runner doctor` — CLI/model compatibility probe

**Date:** 2026-06-11
**Status:** Approved (brainstorm, revised after code-grounded review), pending implementation plan
**Author:** brainstormed with Claude

## Problem

spec-runner can drive several coding-agent CLIs — `claude` / `codex` /
`opencode` / `pi` / `ollama` / `llama-cli`. Command construction for each is
already solved (`runner.build_cli_command()`), but the **runtime interpretation**
of each CLI is not uniform:

- **Completion markers** (`TASK_COMPLETE` / `TASK_FAILED`, `REVIEW_PASSED` /
  `REVIEW_FAILED` / `REVIEW_FIXED`) are prompt-driven; models comply
  inconsistently (small local models often drop or paraphrase them).
- **Cost/token parsing** (`runner.parse_token_usage()`) is regex-tuned to
  Claude's stderr format. For codex/pi/ollama/llama it silently returns `None`,
  so `costs`, `--budget`, `--task-budget` degrade with no error.
- **Error classification** (`errors.classify()`) is tuned to claude/codex
  stderr; other CLIs fall through to the generic last-5-lines fallback.

A config generator alone would give **false confidence**: a tidy YAML with the
right flags, while cost silently isn't tracked and a small model fails the
marker contract. We therefore build the **empirical capability probe first**
(`doctor`); a config generator is a separate later iteration layered on top.

## Goal

`spec-runner doctor` runs the configured (or an ad-hoc) CLI+model through a
**real mini-task on the actual code paths** and reports, per capability, whether
it works — so the user knows *before* a real run whether spec-runner correctly
interprets that CLI/model.

Non-goals (this iteration): the config generator/wizard; auto-tuning prompts per
model; a CI-hosted compatibility matrix (doctor's `--json` makes that buildable
later, but we don't ship it now).

## Key decisions

1. **doctor first**, config generator later.
2. **Reuse `execution.execute_task()`** for the real run — but extract
   capability signals from the recorded `TaskAttempt` + filesystem, **not** from
   `execute_task`'s success verdict (see "Why we don't trust the verdict").
3. Reads the **current project config by default**; `--cli` / `--model`
   override for ad-hoc probes.
4. **Executor-only by default** (1 model call, no git). Review is **opt-in via
   `--with-review`** (2 calls; sets up scratch git so the existing review path
   works). Rationale below.
5. **Budget**: a hard cap is enforceable *only when cost parsing is supported*
   for the CLI; otherwise the cap is a **preflight disclosure**, not enforcement.
6. **Interactive confirmation by default** before billable calls; `--yes`
   overrides. `--strict` makes DEGRADED a non-zero exit (CI gate).

### Why we don't trust `execute_task`'s verdict

Verified against the code:

- `execution.py` computes
  `success = (has_complete_marker and not has_failed_marker) or implicit_success`,
  where `implicit_success = returncode == 0 and not TASK_FAILED`. So a CLI that
  exits 0 **without** printing `TASK_COMPLETE` is still recorded as a success.
  Trusting the verdict would make the marker probe always green.
- `review.run_code_review()` returns `ReviewVerdict.PASSED` when **no** marker is
  found ("No explicit marker — treat as passed"). The verdict therefore cannot
  distinguish "marker recognized" from "marker absent".

doctor must read the **raw signals**, not the verdict.

### Why review is opt-in (revised decision)

`review.run_code_review()` builds its diff with `git diff HEAD~1`, and
`post_done_hook` runs review **before** the auto-commit step. In a fresh scratch
repo there is no `HEAD~1`, so a naive `git init` yields an empty/erroring diff —
the review probe would be meaningless. Making review faithful requires
fabricating commit history (baseline commit → commit the model's work → review
sees `HEAD~1`), which also forces `auto_commit` on and makes "git OFF" mostly
false. We therefore keep the **default probe lightweight (executor-only, no
git)** and confine all git/commit/`HEAD~1` machinery to the explicit
`--with-review` path.

## Architecture & components

New module `src/spec_runner/doctor.py` holds all logic. A thin `cmd_doctor`
dispatcher lives in `cli.py` (next to `cmd_run`), and a `doctor` subparser is
added to `_build_parser()`.

**Invariant:** the run in step 4 uses the *same* `execute_task()` code as a
production run. doctor only *prepares the input* and *reads the recorded
result*. A green doctor therefore predicts a green `run`.

Flow:

```
spec-runner doctor [--cli X --model Y] [--with-review] [--budget B] [--yes] [--strict] [--json] [--keep]
  1. resolve_target()  project config → ExecutorConfig; --cli/--model override (see precedence)
  2. cost gate         show "will invoke <cmd> <model>, N call(s), cap $B"; y/N unless --yes
  3. make_scratch()    tempfile.mkdtemp → minimal spec/tasks.md (1 canned task)
                       + ExecutorConfig: all hooks OFF, sync_deps=False, task_budget_usd=cap,
                       state DB inside temp. If --with-review: git init + baseline commit +
                       auto_commit=True + run_review=True (so review's HEAD~1 diff works)
  4. run_probe()       REAL execution.execute_task()  (review runs inside it via post_done_hook
                       when --with-review; we do NOT call run_code_review() separately)
  5. extract()         raw signals from recorded TaskAttempt + filesystem → DoctorReport
  6. render()          human table | --json ; cleanup temp in finally (unless --keep)
```

Files touched:

| File | Change |
|---|---|
| `src/spec_runner/doctor.py` | **new** — scratch, probe, extract, render |
| `src/spec_runner/cli.py` | `cmd_doctor` dispatcher + `doctor` subparser |
| `src/spec_runner/config.py` | **new field** `sync_deps: bool = True` + YAML parse |
| `src/spec_runner/hooks.py` | `pre_start_hook` respects `config.sync_deps` (skip `uv sync`) |
| `schemas/doctor-result.schema.json` | **new** — `--json` output contract |
| `tests/test_doctor.py` | **new** — built on `fake_claude.sh` variants |

### `sync_deps` config flag (fixes unconditional `uv sync`)

`pre_start_hook` currently runs `uv sync` **unconditionally** (no config gate),
before the git-branch check. In a scratch workspace with no real project this is
slow and pointless (it only warns on failure, so it won't crash, but it wastes
time and can spawn a venv). We add a small, general hook-API flag
`sync_deps: bool = True`; `pre_start_hook` skips `uv sync` when it is `False`.
doctor sets it `False`. This is cleaner than writing a fake `pyproject.toml`
into the scratch dir.

## Canned task & scratch workspace

Canned task (written to temp `spec/tasks.md`) — trivial, deterministic,
verifiable. It is reduced to the **minimal form `task.parse_tasks()` accepts**
(the plan confirms the exact required tokens against the parser); conceptually:

> TASK-SMOKE (P0, TODO): "Create a file `SMOKE.txt` in the project root whose
> entire contents are exactly `PONG`." The prompt instructs the model to end
> with `TASK_COMPLETE`.

Verifiability: doctor reads `SMOKE.txt` and compares its content — the "model
actually did the work, not just printed the marker" probe.

Scratch workspace (`tempfile.mkdtemp`, removed in `finally`; `--keep` retains it):

- `spec/tasks.md` — the canned task;
- `ExecutorConfig` derived from the resolved target but with **all hooks OFF**:
  `create_git_branch=false`, `auto_commit=false`, `run_tests_on_done=false`,
  `run_lint_on_done=false`, `run_review=false`, `sync_deps=false`;
  `task_budget_usd = cap`; state DB inside the temp dir.

**`--with-review` only:** doctor additionally `git init`s the scratch repo,
makes an empty **baseline commit**, sets `auto_commit=true` and `run_review=true`
so that: model creates `SMOKE.txt` → post_done_hook reviews (diff `HEAD~1` =
baseline vs the about-to-be-committed work) → commits. This is the only path that
touches git, and it exists solely to satisfy the existing review diff mechanism.
Without `--with-review`, no git at all.

## Capability model & extraction (the heart)

doctor extracts **raw signals** from the recorded `TaskAttempt` + filesystem
(never the success verdict). Each check has a status string: `ok` ·
`unsupported` (degraded, not fatal) · `fail` (broken) · `na`.

| Check | Raw signal source | `ok` | `unsupported` | `fail` |
|---|---|---|---|---|
| **invocation** | `attempt.success` + `attempt.error` / `error_kind` / `error_code`; special-case `FileNotFoundError` (see below) | ran, exit 0 | — | nonzero/timeout/not-found → show cause (auth/network/cli_error/not-in-PATH) |
| **completion_marker** | scan `attempt.claude_output` for `TASK_COMPLETE` (and absence of `TASK_FAILED`) | found | — | not found (model doesn't print the contract) |
| **task_action** | `SMOKE.txt` exists and == `PONG` | yes | file present, text differs | file missing (marker but no action) |
| **cost_tracking** | `attempt.cost_usd` / `attempt.input_tokens` / `attempt.output_tokens` | parsed (non-None) | all `None` → CLI gives no cost in `parse_token_usage` format | — |
| **error_classification** | on failure — `attempt.error_kind` is a specific kind, not the generic fallback | specific kind | generic fallback | `na` if probe succeeded |
| **review** *(only with `--with-review`)* | scan the review output (task review log / `attempt.review_findings`) for `REVIEW_PASSED`/`FAILED`/`FIXED` | marker found | review ran but no recognizable marker (`run_code_review` defaults to PASSED — we must detect this from raw output) | review subprocess failed/empty |

**FileNotFoundError special-case:** `execute_task` wraps execution in a generic
`except Exception`, so a missing CLI is recorded with an `error` string but no
`error_kind`. `extract()` detects this (error mentions the command / "No such
file") and maps `invocation` → `fail` with "command not in PATH", rather than a
vague generic error.

Overall verdict:

- **READY** — invocation/marker/action `ok` (cost `unsupported` tolerated;
  review `ok`/`unsupported`).
- **DEGRADED** — core works but a `unsupported` is present (e.g. cost not
  tracked) → the report prints an explicit line on what won't work
  (budgets / `costs` / budget cap not enforceable).
- **BROKEN** — any `fail` in invocation/marker/action.

`error_classification` is diagnostic: in the happy path it is `na`. It only
"lights up" if the probe itself fails.

## CLI surface, precedence, cost gate, output

Flags for `spec-runner doctor`:

| Flag | Purpose | Default |
|---|---|---|
| `--cli NAME` | override `claude_command` **and clear `command_template`/`review_command_template`** so build_cli_command auto-detects for the new CLI | from config |
| `--model ID` | override the effective model for implementer **and** reviewer (overrides persona models too — see precedence) | from config |
| `--with-review` | add the review probe (sets up scratch git; 2nd model call) | review OFF |
| `--budget USD` | cap on the probe | `0.50` |
| `--timeout MIN` | inherited from the common parser; doctor defaults to **3** when the user does not pass it | 3 (doctor) |
| `--yes` / `-y` | skip interactive confirmation (CI) | confirm ON |
| `--strict` | exit non-zero on DEGRADED too (not just BROKEN) | off |
| `--json` | machine output | human-readable |
| `--keep` | do not delete the scratch dir | deleted |

**Precedence fixes:**

- **`--cli`** (#8): `runner.build_cli_command()` honors a configured
  `command_template` *before* auto-detect. So overriding only `claude_command`
  would keep a claude-shaped template and break e.g. codex. When `--cli` is
  given, doctor clears `command_template` and `review_command_template` to force
  auto-detection. Without `--cli`, the config's template is used and the report
  notes "using command_template from config".
- **`--model`** (#9): `config.get_model_for_role()` prefers a persona's `model`
  over `claude_model`. When `--model` is given, doctor overrides the implementer
  and reviewer persona models (and `claude_model`/`review_model`) so the probe
  truly uses the requested model. Without `--model`, doctor probes the
  *effective* model the config resolves to (personas included) — the honest
  "what my config does" probe.

Cost gate (default, executor-only):

```
spec-runner doctor — compatibility probe
  CLI:    codex (exec -m gpt-5.4)
  Budget: capped at $0.50 (enforceable only if cost parsing is supported)
This makes 1 real, billable model call. Proceed? [y/N]
```

With `--with-review` the gate says "2 real, billable model calls" and lists the
review CLI/model. `--yes` skips the prompt.

Human-readable report (executor-only example):

```
🩺 spec-runner doctor — codex / gpt-5.4

  ok   invocation        exit 0 in 7.2s
  ok   completion_marker TASK_COMPLETE detected
  ok   task_action       SMOKE.txt == "PONG"
  warn cost_tracking     no cost in stderr — `costs`/`--budget` won't work for codex
  na   error_classify    probe succeeded

  Verdict: DEGRADED — usable, but cost/budget tracking unavailable for codex.
           Budget cap was NOT enforceable (cost not parsed).
  Measured cost: n/a
```

`--json` (fixed schema, validated against `schemas/doctor-result.schema.json`).
Status values are stable strings (`ok` / `unsupported` / `fail` / `na`):

```json
{
  "cli": "codex", "model": "gpt-5.4", "review": false,
  "verdict": "degraded",
  "checks": {
    "invocation": {"status": "ok", "detail": "exit 0 in 7.2s"},
    "completion_marker": {"status": "ok"},
    "task_action": {"status": "ok"},
    "cost_tracking": {"status": "unsupported", "detail": "no cost in stderr"},
    "error_classification": {"status": "na"}
  },
  "budget_enforceable": false,
  "measured_cost_usd": null, "duration_s": 9.1
}
```

Exit codes: `0` for READY/DEGRADED, `1` for BROKEN; with `--strict`, DEGRADED
also exits `1`.

## Error handling

Everything maps into a check; doctor never surfaces a raw traceback.

| Situation | Behavior |
|---|---|
| CLI not in PATH | `FileNotFoundError` special-case → `invocation` fail "command not found", verdict BROKEN, rest `na` |
| Auth failure (nonzero exit + stderr matches auth pattern) | `invocation` fail with classified "authentication" cause + hint |
| Timeout | doctor default 3-min timeout; `invocation` fail "timeout" |
| Budget exceeded (cost-supported CLI) | `invocation` fail "budget exceeded — raise `--budget`" |
| Budget cap unenforceable (no cost parsing) | not a failure; disclosed in the cost gate and verdict line |
| Marker printed but file not created | marker `ok` + action `fail` → BROKEN, hint "prints contract but doesn't do the work" |
| Review command broken (with `--with-review`) | review `fail`, executor checks `ok` → DEGRADED |
| Interrupt (Ctrl-C) | cleanup scratch in `finally`; no partial report written |

## Testing

`tests/test_doctor.py`, no real network in CI:

- Add variant fake CLIs (shell scripts like the existing
  `tests/fixtures/fake_claude.sh`). Crucially, `fake_cli_ok` must **create
  `SMOKE.txt` in its cwd** (the current fixture only prints to stdout/stderr):
  - `fake_cli_ok` — prints `TASK_COMPLETE` + a `cost: $0.01` line + writes
    `SMOKE.txt` → all `ok`, verdict READY;
  - `fake_cli_nocost` — same but no cost line → `cost_tracking` unsupported,
    DEGRADED, `budget_enforceable=false`;
  - `fake_cli_nomarker` — exits 0, writes the file, but prints no marker →
    `completion_marker` fail, BROKEN (proves we don't trust the verdict);
  - `fake_cli_noaction` — prints the marker but writes no file →
    `task_action` fail;
  - `fake_cli_authfail` — exit 1 + auth stderr → `invocation` fail classified;
  - missing binary → `FileNotFoundError` special-case → `invocation` fail.
- Unit tests for `extract()` (raw signals → DoctorReport) and `render()`
  (human + `--json` matches the schema; status strings stable).
- Contract test: a sample `--json` validates against
  `schemas/doctor-result.schema.json`.
- A `--with-review` test using a fake review CLI that does/doesn't print
  `REVIEW_PASSED`, asserting review `ok` vs `unsupported` (from raw output, not
  verdict), with the scratch git baseline-commit setup.
- `sync_deps=false` test: `pre_start_hook` does not invoke `uv sync`.
- Real CLI runs (codex/pi/ollama…) → `@pytest.mark.slow` / manual, not in CI
  (no auth available).
- Cost gate: without `--yes`, it asks for confirmation and on `n` makes no
  model call.

## Out of scope / future

- **Config generator/wizard** — a later iteration that uses doctor as its
  validation core and annotates generated YAML with measured limitations
  ("cost tracking not supported for this CLI").
- **Compatibility matrix** — `--json` output is matrix-buildable; a `doctor`
  run loop across CLIs/models is left to the user/CI.
- **Fixing review's `HEAD~1` diff** to use a working-tree diff (would simplify a
  future always-on review probe) — deliberately not bundled here to avoid
  changing production review behavior in this iteration.
