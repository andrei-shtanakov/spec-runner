# Design: `spec-runner doctor` — CLI/model compatibility probe

**Date:** 2026-06-11
**Status:** Approved (brainstorm), pending implementation plan
**Author:** brainstormed with Claude

## Problem

spec-runner can drive several coding-agent CLIs — `claude` / `codex` /
`opencode` / `pi` / `ollama` / `llama-cli`. Command construction for each is
already solved (`runner.build_cli_command()`), but the **runtime interpretation**
of each CLI is not uniform:

- **Completion markers** (`TASK_COMPLETE` / `TASK_FAILED`, `REVIEW_PASSED` /
  `REVIEW_FAILED`) are prompt-driven; models comply inconsistently (small local
  models often drop or paraphrase them → a correct run is misclassified as a
  failure).
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
**real, end-to-end mini-task on the actual code paths** and reports, per
capability, whether it works — so the user knows *before* a real run whether
spec-runner correctly interprets that CLI/model.

Non-goals (this iteration): the config generator/wizard; auto-tuning prompts per
model; a CI-hosted compatibility matrix across all CLIs (doctor's `--json` makes
that buildable later, but we don't ship it now).

## Key decisions (from brainstorm)

1. **doctor first**, config generator later.
2. **Full e2e** probe — reuse `execution.execute_task()`, not a reimplementation.
3. Reads the **current project config by default**; `--cli` / `--model`
   override for ad-hoc probes.
4. Exercises **executor + review** by default (≈2 model calls); git-automation /
   tests / lint hooks are OFF.
5. **Budget cap always on + interactive confirmation by default**; `--yes`
   and `--budget` override.
6. Approach **A**: reuse `execute_task()` in an ephemeral workspace; capability
   signals are read from the recorded `TaskAttempt` + filesystem + review verdict.

## Architecture & components

New module `src/spec_runner/doctor.py` holds all logic. A thin `cmd_doctor`
dispatcher lives in `cli.py` (next to `cmd_run`, since it really executes), and
a `doctor` subparser is added to `_build_parser()`.

**Invariant:** step 4 below runs the *same* code as a production run
(`execute_task` / `run_code_review`). doctor only *prepares the input* and
*reads the result*. A green doctor therefore predicts a green `run`.

Flow:

```
spec-runner doctor [--cli X --model Y] [--no-review] [--budget B] [--yes] [--json] [--keep]
  1. resolve_target()  project config → ExecutorConfig; --cli/--model override
                       claude_command/claude_model (and review_command/model)
  2. cost gate         show "will invoke <cmd> <model>, ~N calls, cap $B"; y/N unless --yes
  3. make_scratch()    tempfile.mkdtemp → minimal spec/tasks.md (1 canned task)
                       + ExecutorConfig with hooks OFF, task_budget_usd=cap,
                       state DB inside temp; git init ONLY if review enabled
  4. run_probe()       REAL execution.execute_task()  [+ review.run_code_review() if review]
  5. extract()         signals from recorded TaskAttempt + filesystem + verdict → DoctorReport
  6. render()          human table | --json ; cleanup temp in finally (unless --keep)
```

Files touched:

| File | Change |
|---|---|
| `src/spec_runner/doctor.py` | **new** — scratch, probe, extract, render |
| `src/spec_runner/cli.py` | `cmd_doctor` dispatcher + `doctor` subparser |
| `schemas/doctor-result.schema.json` | **new** — `--json` output contract |
| `tests/test_doctor.py` | **new** — built on `fake_claude.sh` variants |

## Canned task & scratch workspace

Canned task (written to temp `spec/tasks.md`) — trivial, deterministic,
verifiable:

```markdown
### TASK-SMOKE: Doctor smoke probe
🔴 P0 | ⬜ TODO

**Checklist:**
- [ ] Create a file `SMOKE.txt` in the project root with exactly the text: PONG

**Traces to:** —
```

The prompt instructs the model to create the file and end with `TASK_COMPLETE`.
Verifiability: doctor reads `SMOKE.txt` and compares its content — this is the
"the model actually did the work, not just printed the marker" probe.

Scratch workspace (`tempfile.mkdtemp`, removed in `finally`; `--keep` retains it
for debugging):

- `spec/tasks.md` — the canned task;
- `ExecutorConfig` derived from the resolved target but with hooks force-OFF:
  `create_git_branch=false`, `auto_commit=false`, `run_tests=false`,
  `run_lint=false`; `task_budget_usd = cap`; state DB inside the temp dir;
- `run_review` set per flag (default ON).

**Git nuance:** the review stage (`run_code_review`) builds a prompt containing
`${GIT_DIFF}`. To reuse it faithfully, doctor runs `git init` in the scratch dir
and takes the diff of the model's changes (`git add -A` → `git diff --cached`).
So "git OFF" means the git-*automation* hooks (branch/commit/merge) are off, but
the scratch repo is still initialized purely as a diff source for the review
probe. With `--no-review`, no `git init` happens at all.

## Capability model & report

doctor extracts, from the recorded `TaskAttempt` + filesystem + verdict, a set of
checks, each with a status: ✅ ok · ⚠️ unsupported (degraded, not fatal) ·
❌ fail (broken) · ➖ n/a.

| Check | Signal source | ✅ | ⚠️ | ❌ |
|---|---|---|---|---|
| **invocation** | exit code + stderr via `errors.classify` | ran, exit 0 | — | nonzero/timeout → show classified cause (auth/network/cli_error) |
| **completion_marker** | `TASK_COMPLETE` detection (same as `execute_task`) | found | — | not found (model doesn't print the contract) |
| **task_action** | `SMOKE.txt` exists and == `PONG` | yes | file present, text differs | file missing (marker but no action) |
| **cost_tracking** | `attempt.cost` / `attempt.*_tokens` | parsed | `None` → CLI gives no cost in `parse_token_usage` format | — |
| **error_classification** | on failure — `errors.classify` returned a specific (non-fallback) kind | specific kind | fallback (last-5-lines) | ➖ if probe succeeded |
| **review** *(if enabled)* | review subprocess + `REVIEW_PASSED`/`FAILED` detection | marker found | review ran, marker unrecognized | review command failed |

Overall verdict:

- **READY** — invocation/marker/action ✅ (cost ⚠️ tolerated; review ✅/⚠️).
- **DEGRADED** — core works but a ⚠️ is present (e.g. cost not tracked) → the
  report prints an explicit line on what won't work (budgets / `costs`).
- **BROKEN** — any ❌ in invocation/marker/action → CLI/model unusable as-is,
  with a hint (auth? model ignoring the contract?).

`error_classification` is diagnostic: in the happy path it is ➖ n/a (we do not
induce an artificial failure; we mark it honestly). It only "lights up" if the
probe itself fails.

## CLI surface, cost gate, output

Flags for `spec-runner doctor`:

| Flag | Purpose | Default |
|---|---|---|
| `--cli NAME` | override `claude_command` | from config |
| `--model ID` | override model (executor + review) | from config |
| `--no-review` | skip review probe (and `git init`) | review ON |
| `--budget USD` | cap on the probe | `0.50` |
| `--yes` / `-y` | skip interactive confirmation (CI) | confirm ON |
| `--json` | machine output | human-readable |
| `--keep` | do not delete the scratch dir | deleted |

Cost gate (default):

```
spec-runner doctor — compatibility probe
  CLI:    codex (exec -m gpt-5.4)
  Review: codex (gpt-5.4)        [2 model calls]
  Budget: capped at $0.50
Proceed? This makes real, billable model calls. [y/N]
```

`--yes` skips the prompt; the cap is enforced regardless (`task_budget_usd`).

Human-readable report:

```
🩺 spec-runner doctor — codex / gpt-5.4

  ✅ invocation        exit 0 in 7.2s
  ✅ completion_marker TASK_COMPLETE detected
  ✅ task_action       SMOKE.txt == "PONG"
  ⚠️  cost_tracking     no cost in stderr — `costs`/`--budget` won't work for this CLI
  ➖ error_classify    n/a (probe succeeded)
  ✅ review            REVIEW_PASSED detected

  Verdict: DEGRADED — usable, but cost/budget tracking unavailable for codex.
  Measured cost: $0.03
```

`--json` (fixed schema, for a future matrix and CI), validated against
`schemas/doctor-result.schema.json`:

```json
{
  "cli": "codex", "model": "gpt-5.4", "review": true,
  "verdict": "degraded",
  "checks": {
    "invocation": {"status": "ok", "detail": "exit 0 in 7.2s"},
    "completion_marker": {"status": "ok"},
    "task_action": {"status": "ok"},
    "cost_tracking": {"status": "unsupported", "detail": "no cost in stderr"},
    "error_classification": {"status": "na"},
    "review": {"status": "ok"}
  },
  "measured_cost_usd": 0.03, "duration_s": 9.1
}
```

Exit codes: `0` for READY/DEGRADED, `1` for BROKEN (CI-gate friendly).

## Error handling

Everything maps into a check; doctor never surfaces a raw traceback.

| Situation | Behavior |
|---|---|
| CLI not in PATH | `invocation` ❌ "command not found", verdict BROKEN, rest ➖ skipped |
| Auth failure (nonzero exit + stderr matches auth pattern) | `invocation` ❌ with classified "authentication" cause + hint |
| Timeout | short doctor timeout (default 3 min); `invocation` ❌ timeout |
| Budget exceeded on the probe | `invocation` ⚠️/❌ "budget exceeded — raise `--budget`" |
| Marker printed but file not created | marker ✅ + action ❌ → BROKEN, hint "prints contract but doesn't do the work" |
| Review command broken, executor OK | review ❌, executor checks ✅ → DEGRADED |
| Interrupt (Ctrl-C) | cleanup scratch in `finally`; no partial report written |

## Testing

`tests/test_doctor.py`, no real network in CI:

- Reuse `tests/fixtures/fake_claude.sh` + add variant fakes:
  - `fake_cli_ok` — prints `TASK_COMPLETE` + `cost: $0.01` + creates `SMOKE.txt`
    → all ✅, verdict READY;
  - `fake_cli_nocost` — no cost line → `cost_tracking` ⚠️, DEGRADED;
  - `fake_cli_nomarker` — no marker → `completion_marker` ❌, BROKEN;
  - `fake_cli_noaction` — marker but no file → `task_action` ❌;
  - `fake_cli_authfail` — exit 1 + auth stderr → `invocation` ❌ classified.
- Unit tests for `extract()` (signals → DoctorReport) and `render()`
  (human + `--json` matches the schema).
- Contract test: a sample `--json` validates against
  `schemas/doctor-result.schema.json`.
- Real CLI runs (codex/pi/ollama…) → `@pytest.mark.slow` / manual, not in CI
  (no auth available).
- Cost gate: a test that without `--yes` it asks for confirmation and on `n`
  makes no model call.

## Out of scope / future

- **Config generator/wizard** — a later iteration that uses doctor as its
  validation core and annotates generated YAML with measured limitations
  ("cost tracking not supported for this CLI").
- **Compatibility matrix** — `--json` output is matrix-buildable; a `doctor`
  run loop across CLIs/models is left to the user/CI.
