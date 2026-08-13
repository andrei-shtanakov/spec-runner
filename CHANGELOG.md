# Changelog

All notable changes to spec-runner are documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Per CLAUDE.md: any change to `.executor-state.json` / `--json-result` format
is a **breaking change** and requires a major version bump plus an entry here.

## [Unreleased]

### Fixed

- **The run ceiling has one reader again** (#256, F-34). `budget authorize`
  raised it to $9.00 on the record; the next `run` refused anyway against
  `budget_usd` from the config file — the number the authorization exists to
  supersede. So the ceiling had three readers that disagreed: `retry` honoured
  authorizations, `run`'s preflight read raw config, and the success path
  enforced nothing (#255, still open). `state.stop_cause` now reads
  `budget.effective_limits`, like every other enforcement site, and its refusal
  quotes the number it actually compared against.

- **A budget refusal no longer advises destroying the evidence.** It said
  *"Raise `budget_usd`, or `spec-runner reset` to clear recorded costs"* — the
  first points at the boundary the audited mechanism replaced, the second tells
  an operator to erase the spend history to fit under a ceiling. It now points
  at `budget authorize`.

- **`status` and `costs` name the ceiling in force.** The pinned `budget_usd`
  key keeps its documented meaning (the *configured* value) — changing what it
  carries would be a silent semantic change on a surface spec-runner-vscode
  vendors — and a line beside it names the authorised ceiling with its id,
  actor and timestamp, per #230 §4.

## [2.31.0] - 2026-08-13

**Minor.** Everything added is additive and everything changed is a diagnosis:
two new operator commands (`budget authorize`, `tdd resume`), two new state
tables (`pr_agent_calls`, `budget_authorizations`), optional `costs --json`
keys for the PR ledger — checked against v2.30.0 by diffing `schemas/`,
`docs/state-schema.md` and the `add_argument` lines rather than asserted. The
`--json-result` contract and the state-DB format for existing consumers are
untouched; the schema change is guarded by spec-runner-vscode having vendored
the same file first (their #25, byte-identical).

The theme is **money and evidence you can account for**. `review-pr` now has a
ledger of its own, so a session is no longer free by omission; a ceiling can be
raised by a named human with a reason instead of being worked around; a task
whose green survived an infrastructure crash has a door back; and two more
instrument failures stopped being reported as verdicts about the code.

Four of the six were found by running the tool rather than reading it — and
one, #249, was found by the owner within the hour of the remedy it fixes
shipping, in the exact state that remedy was built for.

### Fixed

- **`tdd resume` was inadmissible in the exact state it was built for** (#249,
  F-29 — found by the owner within the hour of #244 shipping). Its third
  condition asked `current_phase`, the latest recorded row. But **backwards is
  legal in this lifecycle**: every wedge retry re-enters `red_authoring`, which
  is what being wedged *is*, so any task that actually hit the wedge read as
  `red_authoring` and was refused. The remedy was admissible only for an
  operator who ran it before ever retrying — which is not how the wedge is
  discovered.

  Both remedies now ask the history's high-water mark
  (`lifecycle.has_reached`), not the latest row. The mirrored half was the more
  dangerous one and the issue did not name it: the guard that refuses `repair`
  after green went **quiet** in the same state, so the repair that creates the
  wedge was still allowed — which is how the pilot arrived there.

  The design said "has reached `green_implementing` or later"; the code asked
  "is at". The test fixture agreed with the code because it stopped at green
  and never simulated the retries that define the wedge, even though the design
  document's own table lists them.

- **One reading of an agent's result, shared by both call sites** (#241).
  `run_code_review` and `verify_comment` each run one agent and get back the
  same four signals — text, return code, the CLI's own error flag, a timeout —
  and disagreed about what they mean. A reviewer that crashed after printing
  `REVIEW_PASSED` was disbelieved (#156); a verifier that crashed after
  printing `VERDICT: REFUTED` was **believed, and its refutation posted to the
  PR as evidence**.

  The discriminator was "did it print anything", which cannot work: a CLI
  killed mid-answer prints a partial answer and so does one that crashed after
  its conclusion. `runner.classify_agent_answer` now answers that question once
  — `answered` / `empty` / `crashed` / `timed_out` — and both sites read it.
  Consumers still act differently on a verdict; they no longer differ on
  whether there is one.

  **Behaviour change:** a verification whose process did not finish is now
  `uncertain` (a human's call) even when it printed a marker, and the evidence
  says which marker was discarded and why. `is_error` at exit 0 — claude's JSON
  reports it that way — counts as a crash on both sides.

- **A git error at the RED gate is an instrument error, not a verdict** (#245).
  `_descends_from` returned `returncode == 0`, so exit 1 ("not an ancestor")
  and exit 128 ("bad object / missing repo / unreadable") were the same
  `False`, and the gate answered *"the confirmed red is on a different tree"*
  for a commit that is simply not in this clone — sending an operator to read
  branch topology when the fix is `git fetch`. Split three ways: 0 satisfies,
  1 is an honest `UNSATISFIED`, anything else (including a git that cannot be
  executed) is `INSTRUMENT_ERROR`, carrying git's own message, so the run
  exits 2 rather than 1.

  **Behaviour reversal, deliberate:** a checkpoint whose SHA no longer resolves
  used to be `UNSATISFIED` (pinned since #141 slice 1c). Nothing merges either
  way — the change is what the operator and CI are told.

### Added

- **`spec-runner tdd resume`** (#232) — the post-green half finally has a
  remedy. `abandon` and `repair` both answer questions about a *red*, so an
  operator whose run died after green reached for the nearest one; `repair`
  honestly recorded that the changed test no longer fails, **superseding the
  confirmed red the task still needed**, and `red_authoring` was then the only
  door the lifecycle offered — to a task whose red cannot exist precisely
  because the work is done.

  `resume` reinstates that evidence, and the property that makes it safe is
  negative: **it introduces no new way to satisfy the RED gate.** The gate is
  untouched and still demands a confirmed `expected_fail` whose commit is an
  ancestor of the tree in hand; this only changes which row is standing, and
  only when such a row already exists. Admissible when the task has a confirmed
  red here (any status — supersession retires a lineage, not an observation),
  its commit is an ancestor of HEAD, and the lifecycle reached
  `green_implementing` or later; with more than one confirmed red it refuses
  and asks for `--checkpoint` rather than guessing.

  **The checkpoint and its lineage's claims are reinstated in one transaction.**
  Reinstating the red alone would make this legal: *confirmed red + claim →
  GREEN edits the frozen test → repair supersedes both → resume returns only
  the red → merge with no byte-lock* — laundering the exact violation the lock
  exists to catch, with the command built to help. A claim protects the
  evidence from the RED until the terminal gate. If the claimed bytes have
  moved, the command's preflight says so, the decision is still recorded, the
  exit code is 2, and the gate refuses until they match. Nothing accepts new
  bytes.

- **`tdd repair` is refused after green** — it asks whether a changed test is
  still a red, a question with no honest answer once the implementation exists,
  and answering it retires the evidence. It points at `resume`.

- **`spec-runner budget authorize`** (#230 part 2) — an operator raising a
  ceiling, audited. Refunds and a separate infrastructure budget were rejected
  at design time: both stop *"the number bounds the money"* from being true.
  This decides **which number** the limit is, never whether it binds, so the
  #213 guarantee is unchanged.

  ```
  spec-runner budget authorize TASK-101 --task-limit 6.00 --run-limit 6.00 \
      --reason "continuing the pilot after F-25…F-28 were fixed"
  ```

  Both axes, because neither implies the other. Mandatory `--reason`, recorded
  actor, refusal while a run holds the executor lock, refusal from inside an
  agent, compare-and-swap via `--after`, and **raises only** — lowering is not
  supported by this command or any flag on it. Every budget refusal now quotes
  the standing authorization's id, limit, actor and timestamp, so an operator
  never has to go looking for the id before they can supersede it.

  The record (`budget_authorizations`, append-only) keeps the previous and new
  absolute limits, **the recorded spend at the moment of the decision and how
  many calls in scope were unpriced** — $6.00 authorised against a proven $2.53
  means something different from $6.00 against a floor.

  **The budget domain is the state DB.** A task ceiling is scoped
  `(domain, namespace, task)`; a run ceiling belongs to the whole domain and
  carries no namespace, enforced by a `CHECK` — otherwise several workstreams
  would each hold an independent "global" cap. A new state file inherits no
  authorization and no spend, which is the mechanism behind the rule the pilot
  broke by accident when three attempts ran against three state files.

- **`review-pr` records its own paid calls** (#218 stage 2). The loop makes one
  verification call per collected bot comment and one fix call per valid one;
  neither reached any ledger, so `spec-runner costs` showed a review-pr session
  as free and the money existed only inside one invocation's memory. They now
  land in a **separate `pr_agent_calls` table** — never a nullable `task_id` on
  `agent_calls`, because such a call belongs to a PR comment and `costs` groups
  the task ledger by task.

  Append-only, one row per call whose process **started** (a verifier killed by
  a timeout was billed for the time it ran; a call the cost guard refused before
  spawning anything is not a call). Cost is **NULL when the CLI reported none** —
  unknown is not zero.

  `costs` keeps the two ledgers apart and sums them only at the point of asking:
  `total_cost` remains the **task** total and is unchanged, `pr_review_cost` is
  what the loop spent, `repo_total_cost` is their sum. `costs --json` gains an
  optional `pr_reviews` array and four summary keys
  (`schemas/costs.schema.json`), all absent when the loop has never run — a
  project that does not use `review-pr` sees the surface it always saw.

  **Not backfilled**: spend from before this version is missing from the ledger,
  which the output states rather than implying that older sessions were free.

## [2.30.0] - 2026-08-13

**Minor.** No schema, no CLI flag and no config key was added — verified by
diffing `schemas/`, `docs/state-schema.md` and the `add_argument` lines against
v2.29.0. One **externally visible behaviour change** earns the minor bump: a
TDD run whose replay environment is broken now exits **2** ("the instrument
broke, so I cannot tell whether the work is good") where it exited **1** ("the
work is bad"). That exit surface has been declared since v2.25.0; this is the
first release in which the RED site can honour it. A consumer treating any
non-zero exit as failure is unaffected.

The theme is what the tool did when something other than the work went wrong.
Four of the five fixes come from one battle-testing cascade (F-25…F-28) on a
real TDD pilot, and the pattern they share is worth naming: in each case the
tool knew what had happened and told the operator something else — a provider
session limit reported as a failing test suite, a byte-lock violation reported
as "tests/lint check", an instrument failure reported as a bad task, and
uncommitted work deleted with no mention that it had ever existed.

### Fixed

- **A broken instrument at the RED site is reported as one** (#230, part 1).
  `run_exit_code` has meant exit 1 = "the work did not finish" and exit 2 =
  "the instrument broke, so I cannot tell you whether the work is good" since
  v2.25.0 — but the RED site could never reach 2. Its refusals were classified
  by a **prefix match on the message**, and it wrote a different sentence, so a
  replay that failed for environment reasons was recorded `HOOK_FAILURE` and
  reported to CI as a failed task. From the pilot's state DB: `HOOK_FAILURE |
  RED could not be verified (infrastructure): …` — the word was in the message
  and nothing read it.

  The classification is now typed rather than textual: `gates.refusal_for`
  turns a gate's own `GateStatus` into a `Refusal` carrying its kind
  (`policy` → `HOOK_FAILURE` → exit 1, `instrument` → `INFRASTRUCTURE` →
  exit 2, `budget` → `BUDGET_EXCEEDED`), and appending context to a refusal
  preserves it. A new refusal site cannot inherit the wrong exit code by
  phrasing itself differently, because phrasing no longer decides.

  **Interop note:** `--json-result` and the state-DB format are unchanged, but
  a TDD run whose replay environment is broken now exits **2** where it exited
  1. A consumer that treats any non-zero exit as "task failed" is unaffected;
  one that distinguishes 1 from 2 gets the honest answer for the first time.

- **A task start no longer destroys uncommitted work** (#231). The branch stage
  begins every task with `git checkout -- .` and `git clean -fd`, so one task's
  leftovers cannot contaminate the next one's tests — silently and
  irreversibly. In the pilot that deleted a review agent's stranded fixes (two
  modified files, four new fixtures, suite green, later accepted via `tdd
  repair`); only a byte-exact snapshot taken by hand recovered them. Whatever
  the tree carries is now stashed first, under a label naming the task
  (`spec-runner rescue: TASK-101 at <time>`), and announced with the way back.
  If the stash **fails**, the task refuses to start rather than clean:
  destroying work is never the fallback for failing to save it.

  Worth knowing: the loss was reported as a consequence of the TDD
  claim-violation refusal, but the wipe happens before any gate is consulted —
  it was every task start, on any repo with git automation on. Runtime state
  (the live state DB, logs) is excluded, and a clean tree still creates no
  stash, so nothing changes for a run that starts from a committed tree.

- **A provider session limit is retryable infrastructure again** (#229). The
  wordings CLIs actually print for exhaustion — `You've hit your session limit
  · resets 5:30pm`, `Claude usage limit reached. Your limit will reset at 3pm`,
  `5-hour limit reached ∙ resets 3pm` — matched none of the six substrings in
  `ERROR_PATTERNS`, so on the implementation pass the run recorded a plain
  `TASK_FAILED` and retried it on a **5-second linear backoff** against a cap
  that resets hours later. Now recognised as `RATE_LIMIT` (exponential
  backoff), and the recorded message carries the reset time, which is the only
  actionable fact in such a response.
- **A failed task says what refused.** `❌ Failed: tests/lint check` was
  printed for *every* post-done failure — a byte-lock violation, a review the
  reviewer never finished, a refused merge. In the pilot an operator read
  "tests/lint" for a claims-gate refusal while the suite was green. The line
  now carries the actual reason.
- **A blocked task reports work left in the tree.** An agent that dies mid-way
  leaves its edits uncommitted; the task went `blocked` with nothing recording
  that they exist (in the pilot: six modified and untracked files, one `git
  checkout` from being lost). The block reason and the progress log now name
  the stranded paths. A report, never a new failure — if git cannot answer,
  the reason is unchanged.

- **TDD no longer requires the project to be a Python project** (#220). The RED
  phase lints the file it is about to freeze — reasonably, since a claim makes
  it byte-immutable — using `lint_command`, whose default is
  `uv run ruff check .`. On an Elixir project that declared `commands.test` and
  no `commands.lint`, ruff read a `.exs` file, reported 251 errors, and made
  every red `unverifiable`, so `execution_mode: tdd` could not run at all. The
  pre-freeze lint now runs **only a linter the project declared**
  (`commands.lint`). A declared one runs exactly as before, and this lint stays
  deliberately independent of `hooks.post_done.run_lint` — "do not gate
  finished work on lint" is not "freeze a file that does not lint".
- **A refused RED now says why.** The refusal quoted the gate's generic "no
  confirmed red for this task in this workstream" and dropped the reason the
  red phase had already found — a failing lint, an unparseable selector, an
  unmeasured runner. Both halves are reported now, so the diagnosis lands on
  the cause rather than on its symptom.
- **`review-pr`'s own cost limit counts every paid call it makes** (#218, stage 1).
  `review_pr.max_cost_usd` was summed over the **fix** agents alone and checked
  *after* each of them. The loop also makes one verification call per collected
  bot comment, so a PR with twenty comments spent twenty calls the limit never
  saw — the number an operator set bounded roughly half the spend, and which
  half depended on how many comments turned out valid. Both kinds of call now
  count against one shared limit, checked **before** each call, with the same
  guarantee the task-loop guard carries (#213): once recorded spend reaches the
  limit no new paid call starts, and the overshoot is bounded by one call.
  Comments the loop stops short of keep no verdict and no resolution, which the
  existing `NEEDS_HUMAN` exit (2) already reports.
- **A verification call that reports no cost is unknown, not free.**
  `verify_comment` never parsed its result at all, so no cost was available;
  it now goes through the same `build_cli_invocation` → `parse_cli_result` seam
  as every other paid call, which also means an explicit claude verifier is
  asked for JSON and the `VERDICT:`/`EVIDENCE:` markers are read from the parsed
  text. An unpriced call (timeout, account limit, or a CLI that never reports
  cost) stops the next one — the remaining budget cannot be proven from a floor.

### Changed

- **`review_pr.max_cost_usd: 0` (or negative) disables the limit.** Needed
  because of the rule above: a CLI that never reports cost would otherwise stall
  the loop after its first call. Previously a `0` limit was simply never
  reached for unpriced calls and stopped after the first priced fix.

## [2.29.0] - 2026-08-13

**Minor.** One public-surface change: `costs --json` gains an optional
`unmeasured_calls` integer per task and on the summary
(`schemas/costs.schema.json`). Additive — but the schema pins
`additionalProperties: false`, so a consumer validating against a vendored
pre-2.29 copy must update it (spec-runner-vscode did, in their #23).
`--json-result` and the state-DB format are untouched, and no config key or CLI
flag was added.

The theme is money the tool was spending without being able to say so. Review
calls were recorded **nowhere**, so both budget caps and every total the tool
printed were blind to roughly a third of a TDD attempt's spend; the caps were
checked between attempts, which stopped bounding anything once an attempt
became three paid calls; and a task that finished and merged could be marked
BLOCKED because the budget ran out after it, which invited the next run to
re-execute already-merged work. Found by three paid pilot attempts and two free
rehearsals, not by reading the code.

### Added

- **A pre-call budget guard (#213, second half).** `budget_usd` and
  `task_budget_usd` were checked **between attempts**, which was a bound when
  an attempt meant one agent call. Under `execution_mode: tdd` an attempt makes
  three (RED authoring → GREEN implementation → review), so the first attempt
  could overshoot by whatever those three happened to cost — the third pilot
  run spent at least $2.53 against a $1.82 cap, and the cap then correctly
  refused a *second* attempt, after the money was gone.

  Both caps are now checked immediately before each paid call, in the order the
  calls happen. The guarantee is deliberately narrow, and it is what the docs
  now say:

  > Once recorded spend has reached the limit, no new paid call is started; the
  > maximum consecutive overshoot is bounded by one call.

  It is a **guard, not a hard cap**: a call's cost is known only once it
  returns, so no state-based check can stop the call that crosses the line.
  (The one true hard cap, claude's native `--max-budget-usd`, stays unwired for
  the reason recorded in `execution.py` — it turned a slight overage into a
  hard failure.)

  Each refusal leaves a resumable state and names the call that did not happen:
  before RED, the task does not start; before GREEN, the confirmed red is kept,
  so resuming reuses it rather than paying to re-author it; before review, the
  candidate commit stands and the verdict is recorded `not_run` — never
  `skipped` and never `passed`, so an advisory policy cannot read the absence
  of a review as a good one, and `review_policy: required` still withholds the
  merge.

  Spend that has **happened but is not yet recorded** counts too: the
  implementation call's cost reaches the state DB only after `post_done_hook`
  returns, so the amount is handed to the guard directly. Without that the
  free budget rehearsal spent $1.80 against a $1.00 cap — the guarantee broken
  by the one call it was written for. An unknown amount is an unprovable
  remainder, not a free call.

### Changed

- **Parallel review runs one role at a time while a budget is set.** Five roles
  launched together all pass the same check before any of them reports a cost,
  which would make "at most one call of overshoot" false. Without a cap
  configured, review is unchanged and still parallel.
- **An unpriced call fails the guard closed** — and so does a guard that
  cannot read spend at all. A call whose cost the CLI never reported (timeout,
  account limit, or a CLI that reports none) makes the remaining budget
  unprovable, so the next paid call is refused rather than spent against a
  figure known to be a floor. Practical consequence: a CLI that
  never reports cost cannot be combined with a budget. That is the honest
  answer — you cannot enforce a limit you cannot measure — and it is stated in
  the README rather than discovered in a bill.
- `costs --json` gains an optional `unmeasured_calls` integer on each task and
  on the summary (`schemas/costs.schema.json`). Additive: consumers that ignore
  unknown keys are unaffected, but the schema pins `additionalProperties:
  false`, so a consumer validating against a vendored pre-2.29 copy must update
  it.

### Fixed

- **A successful task is no longer un-finished by the budget running out
  (#219).** The post-attempt budget check ran before the success branch, so a
  task that had finished, committed, merged and deleted its branch was recorded
  as a `BUDGET_EXCEEDED` failure and flipped `done → blocked` in `tasks.md`.
  Since `resolve_dependencies` promotes `blocked` → `todo`, the next run could
  re-execute already-merged work — paying an agent to author a red against a
  feature that is already implemented.

  A successful attempt now stays successful. Stopping the rest of the run is
  the run loop's job, and it had to be made honest for that: `cli` asked
  `should_stop()` only after a *failed* task, so an exhausted budget could halt
  a run only through the very failure this fix removes. It is now asked
  whatever the task returned. A successful task no longer forces a non-zero
  exit — but a stop that leaves work **ready** still does, re-checked from disk
  at the stop, because in `--all` mode a task unblocked by the success that just
  happened is in neither set the exit code is otherwise computed from.

  Found by the free budget rehearsal for #213; pre-existing, and made common by
  the review-cost accounting, which lets totals reach caps they used to reach
  invisibly.

- **Review calls are counted (#213, first half).** `run_code_review` ran a bare
  `subprocess.run` and threw the CLI result away, so review spend was recorded
  **nowhere** — not on the attempt row, not in the `agent_calls` ledger. Every
  total the tool printed, and both budget caps, were blind to it. Not a TDD
  regression: review has never been counted, in any mode. A TDD attempt makes
  three paid calls and thereby made the hole visible; with `review_parallel` it
  was one invisible call per role.

  Each reviewer subprocess now goes through the same `build_cli_invocation` /
  `parse_cli_result` seam the RED pass uses and writes its own ledger row:
  `review` for the single pass, `review:<role>` per parallel role. Passed,
  failed, timed out, or killed by an account limit — money spent on a call that
  produced nothing usable is still spent. A call that never launched writes no
  row.

  **Unknown is recorded as unknown, never as zero.** A cost the CLI did not
  report is stored NULL; `spec-runner costs` counts those rows and marks the
  total as a floor. Previously an unpriced call would have been silently summed
  as free.

  Asking a claude reviewer for its cost means asking for JSON output, so the
  verdict marker now comes from the parsed result rather than raw stdout — the
  path the exec pass has always used.

  Ledger writes from the parallel pool are serialised: five roles opening a
  state connection at once made SQLite return "database is locked" immediately
  and drop a row, which is how the new test failed on its first run.

  **Old databases stay historically under-counted** — nothing is back-filled,
  because the numbers were never recorded. New runs report the fuller figure,
  so a cost comparison across this version is not like for like.

- **The GREEN pass is told which files the byte-lock froze (#214).** Under
  `execution_mode: tdd` the implementation pass received exactly the prompt a
  `standard` task gets: no mention that a file was claimed, which one, or what
  changing it would cost. In the third pilot run the agent wrote four more
  tests into the file the red had frozen and the claims gate refused the
  merge — ~$1.3 of implementation spent on a candidate the tool was always
  going to reject, for a rule the agent was never given.

  A frozen-files block naming the claimed paths is now appended to the
  implementation prompt, the RED authoring prompt (a namespace can hold
  another workstream's claims), the review prompt, and every parallel review
  role. It is appended **after** rendering rather than offered as a template
  variable, so a project with its own `task`/`review` template cannot
  silently lose the constraint while the gate goes on enforcing it. Review is
  told to answer `REVIEW_FAILED`, not `TASK_BLOCKED`, which `review` does not
  parse — editing a frozen file is not a review fix.

  The claims gate is also evaluated once more **before** review: a candidate
  that already violates the lock cannot be merged whatever a reviewer says, so
  that call buys a verdict nothing can act on. The merge-time check is
  unchanged and remains the authority; a stop here keeps the existing
  resumable shape and records the review verdict as `skipped`.

  No public surface: no new config key, no new flag, no schema change. Nothing
  is read or opened for a run that did not enable TDD.

## [2.28.3] - 2026-08-12

**Patch.** No public surface: no new config key, no new flag, no schema change
(diffed against `v2.28.2`). The changed signatures are internal.

Found by the second paid pilot attempt, which got one step further than the
first: the replay worked, the red was confirmed, and then the byte-lock threw
it away.

### Fixed

- **An ExUnit red is no longer discarded as unclaimable** (#210).
  `claim_paths_for` split the selector on `::` and returned nothing for any
  other shape, with a comment saying `verify_red` had already refused such
  selectors. True when pytest was the only runner; false the moment ExUnit
  landed. A valid `path:line` therefore claimed no files, `record_claims`
  refused — correctly, since a red with nothing locked would pass the gate over
  an open file — and the checkpoint was discarded **after** the replay had
  confirmed it.

  **The selector is parsed once, by the adapter the config chose, and the
  typed object travels down the pipeline** — lint narrowing, replay, byte-lock.
  Re-deriving the shape from the raw string at each step was the actual defect;
  a first fix that looked up the adapter again at the claim site kept the same
  mistake in a quieter place, since `::` and `:line` merely happen not to
  overlap today. The raw string survives as evidence and as what a stored
  record holds; reading a stored record back is the one place an adapter is
  consulted again, and it is the config's, never a search.

  Guarantees are split in two, so wording cannot change semantics:
  `adapter.contract_selectors()` is the machine contract — every canonical
  selector parses and yields a claimable path — while a separate, narrower
  test asserts the human-readable `selector_instruction` shows an example the
  same adapter accepts.

  Found by the second paid pilot attempt. Nothing false was recorded and no
  implementation ran; the defect was that a valid selector was treated as
  nonsense.

## [2.28.2] - 2026-08-12

**Patch.** No public surface moves — no new config key, no new flag, no schema
change (diffed against `v2.28.1`). What changes is that `tdd_runner: exunit`,
which shipped in 2.28.0, can now actually verify a red on a real project.

Both fixes came from the first paid pilot run rather than from review: the run
authored a correct failing test and the replay could not execute it.

### Fixed

- **A RED can be replayed in a language whose dependencies live in the project
  directory** (#207). A `git worktree` carries tracked files only, and Elixir
  keeps `deps/` and `_build/` inside the project — both gitignored — so the
  replay tree could not compile and every red came back `unverifiable`. Found
  by the first paid pilot run on a real project.

  The adapter now prepares the environment before the replay: dependency
  **sources** are shared read-only through `MIX_DEPS_PATH` (they are a cache),
  while build **artifacts** are never shared — each replay gets its own build
  path, removed afterwards on every exit path including timeout and crash.
  `mix deps` proves the checkpoint's lock is satisfied by what is installed;
  **nothing is fetched, generated or repaired**, so a missing environment is a
  refusal (`environment_unavailable` → `unverifiable`) rather than a silent
  network call inside a gate.

  The environment identity recorded on a checkpoint grew accordingly:
  `runner=exunit;mix.lock=…;elixir=1.19.4;otp=28;mix_env=test;deps_source=…`.
  The same lock compiled by a different toolchain is a different environment.

  **The private build lives under the canonical `_build/`, and that is forced
  rather than chosen.** Mix links a dependency's `priv` into the build with a
  *relative* symlink computed for the standard layout, so a build outside the
  project gets no link at all and dependencies that read a sibling's assets at
  compile time fail deterministically. Measured: both `MIX_BUILD_PATH` and
  `MIX_BUILD_ROOT` pointing at a temp directory fail; a uniquely-named sibling
  of `_build/test` works. `_build` is gitignored and the directory is removed,
  so the project's tracked content is untouched — asserted by a test that
  compares `git status --porcelain` across a real replay.

- **The preflight reads the checkpoint's source, not the working tree** (#207).
  The selector describes a test in the commit being replayed; reading the file
  from the canonical tree quietly broke the module's whole premise. Measured on
  the pilot's own red: the agent's new test was at line 85 of the commit, and
  line 85 of `master` was something else, so a genuine red was refused as "not
  a definition line".

## [2.28.1] - 2026-08-12

**Patch.** One fix, no public surface: prompt text only. `schemas/`,
`docs/state-schema.md` and the CLI flags were diffed against `v2.28.0` and none
moved.

It ships now rather than waiting because the kapelle pilot cannot start without
it — on an Elixir project every RED authoring pass would have ended
`unverifiable` before a line of implementation, and the pilot would have spent
its budget discovering that the prompt and the parser disagreed.

### Fixed

- **The RED prompt asks for the selector shape the project's runner accepts**
  (#198). It hardcoded pytest's node id, so an agent on an Elixir project would
  comply with *that* shape — and `path::name` is exactly what the ExUnit
  adapter refuses. Every RED authoring pass would have ended `unverifiable`
  before a line of implementation was written.

  Found by reading the prompt the agent would receive, before the first paid
  pilot run rather than during it. The instruction now comes from the resolved
  adapter, and a test asserts the property directly: what the prompt asks for
  is what the adapter parses.

## [2.28.0] - 2026-08-12

**Minor: a new public config key.** `tdd_runner` is the outward change; the
rest is one defect (#198) fixed in depth. Nothing existing moves — pytest
projects behave exactly as before, `--json-result` and the state-DB schema are
untouched, and TDD mode stays opt-in.

The defect was worth this much work because of what it produced: a confirmed
RED checkpoint for a test that **never ran**, silently, with claims and a
satisfied gate behind it. On any non-pytest runner, and found before a single
paid pilot run — by checking compatibility rather than assuming it.

### Added

- **ExUnit is a supported TDD runner** (#198, build order §3–4):
  `tdd_runner: exunit`, canonical selector `path:line` where the line is the
  `test "..." do` line. TDD mode now works outside Python.

  **The line is proven to define a test before `mix` is invoked**, by Elixir's
  own parser (`Code.string_to_quoted` + a walk for the `test` macro) rather
  than by teaching Python to read Elixir. This is not belt-and-braces, it is
  the only way `not_red` can mean anything here: a passing ExUnit run prints no
  location, so after the fact `1 test, 0 failures` is exactly what a
  *misresolved* selector prints too — and `not_red` retires a claimed red and
  sends an operator to `repair`.

  Measured, and the reason the whole adapter exists: `mix test path:line`
  selects the nearest test **at or before** the line, so `:999` silently runs
  the last test in the file and reports an ordinary "1 test, 1 failure".
  Refused now, along with a line before the first test, a line inside a test
  body, a pytest-style `path::name`, a file that does not parse, a missing
  file, and a missing Elixir toolchain — each with a stable refusal code.

  Classification keys on the **run summary**, not on message text: a file that
  will not compile never reaches ExUnit and prints no summary, which is the
  structural difference from a test that runs and fails. A missing module
  inside a test that does run is an honest red (Elixir makes it a compile-time
  warning and a runtime error).

  Selection is proven by `mix test --trace`, whose per-test entry carries the
  definition line: `:999` reports a timed `[L#9]` and is refuted outright,
  rather than inferred from a count. The count-based rule it replaced disagreed
  between Elixir 1.18 and 1.19 — caught by the CI job below, not by the local
  run, which is the job earning its place on its first execution.

  The contract matrix runs against a **real** `mix` project in its own required
  CI job with a pinned Elixir/OTP, and the job fails if any of it was skipped —
  a green suite that quietly tested nothing is the same class of problem as the
  defect itself.

- **`tdd_runner` config key** (#198, build order §2): which runner adapter
  verifies a claimed RED. Empty infers, and inference is allowed only where it
  cannot be wrong — a command whose executable *is* a known runner's.

  A **declared runner the `test_command` cannot carry is refused** — a
  `ConfigError` at load, an error in `validate`, and a clean `⛔` at startup —
  rather than winning with a logged mismatch. The declaration chooses the
  semantics; it cannot prove the command can carry them, and letting a typo
  through would read one runner's exit codes as another's, which is #198
  returning through an explicit config key.

  `tdd_runner` joins `gates.POLICY_KEYS`, so changing the adapter changes the
  `config_hash` a gate verdict is bound to: an earlier "confirmed" was
  confirmed by a different judge and is not inherited.

  Accepted values are the adapters that exist, so today the only one is
  `pytest`; `exunit` becomes valid when its adapter lands.

### Changed

- **RED verification is per-runner behind an adapter** (#198, build order §1 of
  the approved design). Two independent answers now decide a red instead of one
  exit code: `RunOutcome` describes what the run did (`TESTS_PASSED`,
  `TESTS_FAILED`, `SELECTION_FAILED`, `COLLECTION_OR_COMPILE_ERROR`,
  `RUNNER_ERROR`, `UNRECOGNIZED`) and `SelectionProof` answers whether the
  *requested* test is what ran. Only `TESTS_FAILED` + `PROVEN` is a confirmed
  red; only `TESTS_PASSED` + `PROVEN` refutes one; everything else is
  `unverifiable`.

  The separation is what the next adapter needs: on ExUnit `mix test path:line`
  selects the nearest test at or before the line, so a line past the end of a
  file runs the *last* test and reports an ordinary "1 test, 1 failure". The
  observation is true and the red is still a lie, which no exit-code table can
  fix.

  **Behaviour is unchanged**: pytest is still the only adapter, with the same
  measured exit codes, and the full suite passes untouched. One internal change
  went with it — the replay now builds **argv** rather than a shell string. The
  selector comes from agent output; quoting it correctly was right and was one
  edit away from not being.

### Fixed

- **A confirmed RED now requires a runner whose exit codes were measured**
  (#198). `_TESTS_FAILED = 1` is pytest's convention, and the code called it
  "shared by most runners". Measured on Elixir/OTP 28 it is **inverted**:
  `mix test` exits 2 when tests fail and 1 when the run never happened — a
  nonexistent file, or a test file that would not compile.

  So on a non-pytest project the ordinary path produced a **false confirmed
  red**. The RED prompt asks the agent for `TDD_SELECTOR: path::test`; an agent
  working in Elixir complies with the shape; `verify_red` accepted it because
  the only check was `"::" in selector`; `mix test 'test/x_test.exs::name'`
  matched no file and exited 1; and 1 was read as "the selector failed on
  replay". A checkpoint, file claims and a satisfied gate all followed, for a
  test that **never ran** — which is precisely what the RED checkpoint exists
  to prevent.

  `verify_red` now recognises the runner *before* anything is executed and
  refuses anything it has not measured, with a message naming the command, the
  selector and what is missing. No fallback to pytest semantics: an
  unrecognised runner is `unverifiable`, never a red. Recognition is
  token-based (`uv run pytest` and `./venv/bin/pytest` count;
  `mix test --formatter PytestFormatter` does not), because believing the wrong
  runner is the whole defect. The `::` check moved behind it — it is one
  runner's syntax, not a universal proof that a selector is valid.

  pytest is unchanged and still reaches `expected_fail`: there a mis-selected
  node id exits 4, never 1, which is the property ExUnit lacks.

  This is the fail-closed half. A per-runner adapter — canonical selector form,
  a classification wider than an exit code, and proof that the selected test
  actually ran — is the next step, and it will bring an explicit `tdd_runner`
  config key with it.

## [2.27.1] - 2026-08-12

**Patch, not minor.** One defect (#192 / battle finding F-8) in three forms.
No public surface moves: `--json-result`, the state-DB schema and the CLI are
untouched, and no flag is added or changed. What a user sees that is new is a
bookkeeping commit where the tool previously left a dirty `tasks.md` — the
absence of a deadlock rather than a new capability.

### Fixed

- **A gate-blocked task no longer deadlocks the next run** (#192, battle
  finding F-8). Review start writes `🔍 REVIEW` into `tasks.md` so a run killed
  mid-review stays resumable (#66); that write is uncommitted by design,
  because the commit that would carry it comes later. When a pre-terminal gate
  then blocked, there was no later — the run stopped, and the next one refused
  at the dirty-spec guard (#69) because `tasks.md` was dirty. Both behaviours
  are right on their own; together they were a recovery deadlock whose only
  exits were `--allow-dirty-spec` (which disarms the guard for *real* spec
  edits too) or committing a status flip the operator did not make.

  The blocked path now commits it:
  `candidate commit → gate unsatisfied → bookkeeping commit (status only) →
  resumable stop`. The next run starts with no override.

  - **Only a proven status-only transition is committed.** The committed and
    working files may differ in exactly one line, that line must be the named
    task's meta line in both, and only its status may have changed. A checklist
    tick, a renamed task, an edited dependency, a new task, a line of prose —
    any of these and nothing is committed, the spec stays dirty, and the next
    run's guard is doing its job rather than deadlocking. Stricter than
    comparing parsed tasks on purpose: the parser ignores prose, and "the
    parser didn't notice" is not "nothing changed".
  - **The bookkeeping commit is not the new candidate.** It is a child of the
    SHA the gate judged, carries only `tasks.md`, and never becomes a verdict
    key — a later evaluation asks about a different tree and is a fresh
    evaluation, never the old verdict reapplied to code that has since moved.
  - **Resuming does not grow a chain of identical REVIEW commits**, because
    idempotence comes from the diff rather than from a marker: the second pass
    writes the same status, so there is nothing to commit.
  - **A failed bookkeeping commit is visible** and is appended to the block
    reason rather than passed off as a clean resumable stop. The
    instrument-error prefix that `execution` reads to report infrastructure
    (exit 2) survives, since the note is appended.
  - Only under `auto_commit`, and only reachable through a registered gate — so
    the `standard` / `advisory` paths acquire no commit they did not have.
  - The status is neutralised **positionally**, at the span the pattern
    matched, so a meta line carrying a note that mentions a status word cannot
    weaken the proof; and the file is read once, then verified to be what got
    staged, so an edit landing between the proof and the commit refuses rather
    than riding along.

- **…and the same for the other two harness-written statuses** — found by
  battle-testing the fix above against a build from master, which is the only
  reason they are in the same release:

  - **`⏸️ BLOCKED`**, written when a task stops without finishing, is committed
    the same way. Without it the deadlock simply arrived one run later:
    measured, run 1 blocked and committed `REVIEW` and left a clean tree, run 2
    wrote `BLOCKED` uncommitted, and run 3 refused.
  - **A run killed between writing a status and committing it** (`SIGKILL`
    during review, measured) leaves the same dirt with no stop path to clean
    it. The next run now recovers it where the guard would otherwise refuse,
    under the same proof, and says so: `↻ Recovered an interrupted run:
    committed TASK-001 in_progress → review as bookkeeping`. `in_progress`
    joins the set for this reason.

  `done` is still refused everywhere here — it is a claim about the work,
  carried by the task's own commit, and committing one found lying in a tree
  would complete a task nobody finished. So is `todo`, which comes from
  operator commands rather than from a run. A real spec edit still refuses, and
  a failing run is never taken down by a bookkeeping problem (#127's lesson).

## [2.27.0] - 2026-08-12

**Minor, not patch.** Everything here is a defect fix, but the outward
compatibility change is visible: a malformed or mixed config used to load as
nothing and let the run proceed on defaults, and now it stops the run before
anything executes. That is the right incompatibility — the defaults invoke a
paid external model with write access to the working tree — but it is one
operators should meet as a minor release rather than a patch. `--json-result`
and the state-DB schema are untouched.

### Fixed

- **A config that mixes the flat and `executor:` shapes is refused** (#182,
  battle finding F-7). One stray `executor:` key made the loader read *only*
  that section and discard every top-level key — silently, with
  `spec-runner validate` reporting zero errors.

  This is a fail-open safety bug, not a usability wart: `claude_command` is
  among the discarded keys, so the tool falls back to its default and invokes a
  paid external model with write access to the working tree. It is how a battle
  run whose config named a scripted stand-in agent reached the real `claude`
  CLI and spent real tokens. Any safety knob set that way — `skip_permissions`,
  `run_review`, `execution_mode`, a sandboxed command — was silently off.

  Now an **error** naming the discarded keys, at load time and in `validate`;
  not a warning, because "your settings did nothing and money was spent
  elsewhere" is exactly the class of message that scrolls past. An `executor:`
  key that is not a mapping is refused for the same reason (it used to crash
  into the loader's broad handler and return an empty config, i.e. defaults).

  **A config that cannot be read is refused for the same reason.** Malformed
  YAML, or any other failure to load, used to log a warning and return an empty
  config — which is the identical fail-open one level up. A file the loader
  cannot read is not consent to run on defaults.

  Either shape alone is untouched: the legacy `executor:`-only layout is the
  documented v1.x shape and keeps working, and the top-level
  `execution_order`/`skip_tasks`/`environment` sections carried by every
  bundled legacy template stay warnings — detection intersects with the keys
  the loader actually reads, so an unrecognised key is noise rather than a
  setting that stopped working. Every command stops at the refusal; `validate`
  runs on, because listing the setup's problems in one pass is what it is for.

## [2.26.0] - 2026-08-12

### Added

- **The TDD lifecycle is a recorded state machine** (#141, slice 4a):
  `ready → red_authoring → red_verifying → green_implementing →
  green_verifying → refactoring → done`, persisted append-only in a new
  `tdd_phases` table and shown by `tdd status`. Slices 1–3 built the parts;
  where a task *was* still lived in inference.
  - **`refactoring` is materialised and never executed.** Its record says
    `skipped` — the vocabulary already has the word, and it is honest about a
    stage deliberately not run. An automatic refactor pass was **not
    approved**: under that one word a new expensive and ill-defined agent stage
    could otherwise arrive without anyone choosing it. A test asserts nothing
    resembling one exists.
  - **Backwards transitions are legal**, because a remedy sends a task back to
    authoring and a retry re-enters implementation. Only reaching a GREEN phase
    without a red is refused — the one transition the contract is about — and
    a refusal is itself recorded, so the history is not a record of successes
    only.
  - Bookkeeping, not enforcement: the gates decide and read checkpoints and
    claims, which are written fail-closed. A refused transition here is logged
    rather than raised, so the machine cannot become a second and weaker
    enforcement point beside them.

- **`spec-runner tdd status` and `tdd checkpoints`** (F-5). The remedies
  require a `--checkpoint <id>` that **no command printed**: running them in
  the battle test meant reading SQLite and re-deriving a SHA-256 by hand.
  Evidence nobody can reach is not evidence.
  - Both take an optional `TASK-ID` and `--json`, from one reader, so the text
    a person sees and the payload a script parses cannot drift apart.
  - They show the lifecycle rather than the attempt history, which is why
    plain `status` said `✅ success` after an abandon — true of the last
    attempt, misleading about a task that has no confirmed red.
  - `--checkpoint` is now **optional when exactly one lineage is active**, and
    the chosen id is printed — never silently assumed. With several it fails
    closed and names them: "probably that one" is not a thing to guess about an
    authority decision.

### Fixed

- **`costs` contradicted itself** (F-9, found re-running the battle matrix on a
  build from master). Cost summed attempts plus the new agent-call ledger while
  tokens summed attempts alone, so the table reported **$0.73 spent on 10,000
  tokens** when 15,600 were used — half of F-6, done. Tokens now come from the
  same two sources as cost. A report that contradicts itself is worse than one
  that under-reports consistently.

- **Every retry re-authored the RED** (F-4). A task whose GREEN pass failed
  three times ran the whole RED phase three times, leaving three red commits
  and three `active` checkpoints for one task — an agent call per retry that
  need not happen, and a state the CAS-based remedies do not model.
  - A confirmed red that still covers this tree is now **reused**. Matching is
    narrow: same workstream and task, same effective mode and policy hash (a
    checkpoint records the question it answered), and the red commit must be an
    **ancestor of HEAD** — a red on a branch this one does not descend from
    proves nothing about this tree.
  - Several matches is a **state error**, not a choice: two active lineages
    mean something upstream is wrong, and quietly taking the newest would hide
    it. `tdd abandon` / `tdd repair` resolve it.
  - `abandon` means author afresh; `repair` produces a lineage that is itself
    reusable.
- **The RED pass's cost was discarded** (F-6). `_run_agent` parsed the CLI
  result and returned only the text, so TDD's extra call never reached
  `spec-runner costs` — a `$0.00` that was true only because the battle test's
  agent was a script.
  - A new `agent_calls` ledger records tokens and cost with a **provenance**,
    and `total_cost()` / `task_cost()` include it. The exec pass keeps its cost
    on the attempt, so summing both cannot double count.
  - A **failed** authoring attempt is recorded too: money spent on a call that
    produced nothing usable is still spent. A **reused** checkpoint is not
    charged again.

- **A claim belonged to a byte pattern rather than to a task** (F-3). A second
  task authoring the *same* content on the same file recorded **no claim of its
  own**, so `tdd abandon` by the first released a file the second's confirmed
  red still depended on — the contract's "one task's remedy does not release
  another task's independent claim" defeated not by the remedy but by the
  missing record.
  - Claim identity is now `(task, lineage, path, bytes)`. Re-claiming inside
    one lineage is still idempotent.
  - A remedy retires only **its own lineage's** claims, so a task holding
    claims from more than one after a repair does not lose the others.
  - The file stays locked while any active claim remains.

- **The pre-terminal gate judged a tree without the work** (F-1, found by the
  battle test of published v2.25.0). With review off, a task could rewrite,
  delete or rename its own claimed test and reach **DONE**: both gate
  evaluations judged the *red* commit, and the mutation landed in the task
  commit created after the gate.
  - Cause was the #170 fix. Moving the gate before the DONE write was right —
    a blocked task must not be labelled done first — but it also moved the gate
    before the commit containing the work. The `#103` pre-review commit
    happened to cover the review-on path, so the byte-lock held exactly when an
    unrelated feature was enabled.
  - The order is now: deterministic checks → **candidate commit** (no DONE in
    it) → pre-terminal gates against that SHA → bookkeeping/status commit →
    merge → DONE. A blocked task keeps its candidate commit, so the refusal is
    resumable and the work is committed rather than left dirty.
  - **An external commit between the gate and the merge is now caught.** The
    verdict is about a tree; if anything this run did not create lands in
    between, the merge is refused rather than authorised by a verdict that no
    longer describes it.
  - The candidate commit is made only when something will judge it — review, or
    a registered gate. With neither, the single task commit of #103 is
    unchanged: a project that opts into nothing must not find its history split
    in two.
  - `no_op` is decided from the candidate rather than from the final commit
    being empty, since that commit now always carries the DONE bookkeeping.

- **`run` reported success for a run that did not finish** (F-2, found by the
  battle test of published v2.25.0). `run --task=X` exited **0** after the task
  failed every attempt, while `run --all` on the same repository exited 1.
  - The selector was not the cause. The exit code was decided by *whether the
    loop chose to stop early*, not by whether the work succeeded: `--all`
    happens to reach an idle-stop verdict afterwards, and the fixed-list path
    had no final judgement at all. So `--all` could report 0 the same way given
    a failure that did not trip the stop threshold.
  - There is now one verdict, `cli.run_exit_code`, computed from **this run's**
    outcomes and used by both paths. A task left unfinished is not a success,
    however the loop ended, and an exit code the loop already decided is never
    downgraded.
  - **New exit code 2: the instrument broke.** A pre-terminal gate that could
    not answer is reported apart from one that said no — "I cannot tell you
    whether the work is good" is a different sentence from "the work is bad",
    and CI can act on the difference. Carried by a new
    `ErrorCode.INFRASTRUCTURE` on the attempt. A concrete failure outranks it:
    something actionable is the more useful thing to report.
  - The verdict covers every task the run **touched or promised to touch**,
    not the initially-ready list: in `--all` mode a task that became ready and
    then failed mid-loop would otherwise go unnoticed, and a selected task
    never attempted would count as success.
  - Tested through the real CLI entrypoint, because the defect lived in the
    wiring between the loop and `sys.exit` — every test that called the helper
    directly passed throughout.
- **The published state schema had drifted from `ErrorCode`.** It never gained
  `TASK_BLOCKED` (shipped in #140), so state from a deliberately blocked task
  failed validation against the contract this repo publishes. Added, along with
  the new `INFRASTRUCTURE`, and a test now compares the enum to the schema so
  the two cannot part again.

## [2.25.0] - 2026-08-11

### Added

- **Operator remedies — `spec-runner tdd abandon` / `tdd repair`** (#141,
  slice 3). Without them the only cure for a mistake in a byte-locked test is
  rewriting history; the pilot did that twice in one phase. Slice 2 does not
  ship without this, and they are one release block.
  - Both take `--checkpoint <id>` as **compare-and-swap** against the active
    checkpoint: a remedy issued against what the operator last saw must not
    silently apply to whatever arrived since. `--reason` is mandatory, and the
    actor is recorded (explicit `--actor`, else the git identity).
  - **Nothing is deleted.** `abandon` marks the checkpoint and its claims
    abandoned and returns the task to RED authoring; `repair` marks them
    superseded. A retired claim is still evidence of what was believed.
  - **`repair` does not bless bytes.** It opens a new lineage descending from
    the checkpoint it replaces, and **re-runs the replay immediately**. A
    repaired test that turns out to pass is recorded as `not_red` and exits 2
    rather than reporting a plain success — otherwise `repair` would be a way
    to launder an unconfirmed claim, the exact hole the contract closes.
  - A repeated `repair` reaches the **same verdict** as the first call: the
    idempotent path carries the lineage's outcome, so running the command twice
    cannot turn an exit 2 into a bare success. Running something twice must not
    launder its verdict.
  - Repeating a remedy is idempotent, checked **before** the swap: applying it
    retires the very checkpoint CAS compares against, so a repeat would
    otherwise report a stale id and turn "run it twice" into an error.
  - Refused while a live run holds the executor lock. The lock is PID-checked
    and authoritative; a `running` row left by a crash does **not** block —
    locking an operator out of the tool recovery needs would be the wrong
    failure.
  - One task's remedy leaves another task's independent claim standing.
  - Agent subprocesses now carry `SPEC_RUNNER_AGENT=1` and the remedies refuse
    when it is set. A **guardrail against the ordinary path, not a security
    boundary**: the agent runs arbitrary shell and can unset it. What holds is
    that a remedy carries an operator's name.
- **File claims — the byte-lock behind a confirmed RED** (#141, slice 2). The
  pilot's first version checked only the file of the current selector, so
  neighbouring tests were protected by a sentence in the agent's prompt rather
  than by the instrument. This is the instrument.
  - A confirmed RED freezes the files its selector names, in a new
    `tdd_claims` table. A refuted or unverifiable red freezes nothing — an
    unproven claim must not hold a suite hostage.
  - Enforcement covers **every active claim in the workstream**, whoever made
    it, and runs **against the candidate commit** rather than the working tree:
    a check against a mutable tree answers a question about a moment that has
    already passed. Evaluated at both points the RED gate is — before GREEN and
    again before merge.
  - `modified`, `deleted` and `renamed` are distinguished. All three block; the
    distinction is so an operator is not sent looking for a deleted file that
    was actually moved.
  - Hashing is git's blob SHA over **raw bytes**, with no line-ending
    normalisation. Symlinks, paths outside the repository and non-regular files
    are refused rather than normalised — a symlink's bytes are its target's, so
    hashing it would freeze something the claim does not name.
  - **The claimed file is linted before it is frozen.** After the checkpoint it
    is byte-immutable, so lint debt that got in is uncurable without an
    operator and hits every later task in the suite; the same trap fired three
    times in one of the pilot's waves.
  - A red whose commit violates someone else's active claim is refused, and
    adds no claims of its own.
  - **Fails closed in three places**, because a byte-lock that silently does
    not exist is worse than none — the run believes it is there. A claim that
    cannot be persisted raises rather than logging and continuing (unlike the
    other bookkeeping writers, the gate *reads* claims); a candidate commit
    that cannot be read raises rather than reporting "no violations"; and both
    reach the gate as an instrument error rather than a pass.
  - Claim paths must be **project-relative and canonical**. An absolute path,
    or one carrying `.`/`..`, could never match its own `git ls-tree` entry and
    would read as a deletion on a tree where the file is untouched — a false
    violation blocks work for a reason that is not true.
  - Known and documented limitation: a selector names exactly one file, so a
    test depending on a `conftest.py` fixture does **not** claim that conftest.
    Editing the fixture can turn the red green and is not blocked. Widening the
    claim set by import graph or coverage is a separate decision.
    Contract: `docs/superpowers/specs/2026-08-11-claim-and-remedy-contracts.md`.
- **The RED gate — TDD as the second consumer of #164** (#141, slice 1c).
  `execution_mode: tdd` goes from declared to enforced: the implementation pass
  does not run until a red has been *demonstrated* — authored, committed, and
  replayed against that commit.
  - A RED authoring pass runs first, told to write one failing test and **no
    implementation** (a red that passes because the code was written alongside
    it demonstrates nothing) and to report the full node id via a
    `TDD_SELECTOR:` marker. The claim is replayed either way, so a wrong or
    missing selector fails verification rather than sliding through.
  - **The gate decides, the red phase only observes.** `run_red_phase` authors,
    commits and replays, recording whatever it found — including a refuted
    claim, which is evidence too. Folding the decision into the observer is how
    the review policy and this one would drift apart, which is the reason #164
    is its own mechanism.
  - The same gate is evaluated at **two moments**: before implementing, and
    again at the pre-terminal site, because "do not merge a task that never had
    a confirmed red" is the same question. One registration, two evaluations —
    a second gate saying the same thing would be two things to keep in step.
  - The checkpoint must cover the tree in hand: **descent, not equality**.
    Green *is* commits on top of the red, so demanding the same SHA would make
    the gate unsatisfiable the moment the work it gates happens. A checkpoint
    from another workstream, from an unrelated branch, or whose SHA no longer
    resolves does not satisfy it.
  - `unverifiable` reaches the gate as an *instrument error*, not a refusal:
    "we could not find out" is a fact about us, and a refuted red is a fact
    about the work.
  - `execution_mode` joins `POLICY_KEYS`, so flipping the mode invalidates
    earlier verdicts by construction. `standard` remains untouched: no gate is
    registered, and a per-task `**Mode:** standard` opt-out reaches the gate.
- **RED checkpoint verification** (#141, slice 1b) — the machinery that decides
  whether a claimed red is real, standalone and not yet wired in.
  - A confirmed red means **the selector was executed and failed**, replayed
    against its *commit* in a disposable `git worktree`. An agent's report of
    its own red is exactly the evidence this replaces, and replaying a commit
    rather than the working tree is why the checkpoint commit exists at all —
    a test proven red by an uncommitted edit proves nothing.
  - Three outcomes, not two: `expected_fail`, `not_red`, and **`unverifiable`**.
    "The test passes" is a fact about the code; "we could not find out" is a
    fact about us, and only the first refutes the claim.
  - Refused without running anything: a selector that is not a full node id (a
    `-k`-style name matches several tests, so it proves nothing about the one),
    a composite `test_command` (#139's reasoning — guessing which component
    takes a node id is how you run the wrong program and believe it), and an
    unknown SHA.
  - The selector is **shell-quoted**. It comes from an agent's output, and
    `test_command` is a shell string by contract, so interpolating it raw made
    `tests/x.py::t; rm -rf ~` a command the harness runs on the operator's
    machine.
  - The baseline is checked to be an ancestor of the red commit. A pair whose
    red does not descend from its claimed baseline is a false record, and
    refusing costs less than storing it as evidence.
  - The worktree is removed on every path, including a crash mid-replay: a
    leaked worktree makes the next `git worktree add` fail. A removal that
    itself fails is logged and pruned rather than swallowed, since swallowing
    it would turn that guarantee into a hope.
  - New `red_checkpoints` table storing `(commit, selector, baseline,
    namespace)` plus the environment identity (`<lockfile>:<hash>`, or an
    honest `unpinned`) and the effective mode + config hash. Reads are
    namespaced, because identical `TASK-NNN` ids from different workstreams
    collide once their branches meet.
  - The pytest exit-code mapping was **measured rather than assumed**: an
    unresolvable node id and a file with a syntax error both exit 4, not the 5
    ("no tests collected") one would guess. Only exit 1 can mean a red.
- **`execution_mode: standard | tdd`** (#141, slice 1a) — the mode surface,
  **declared but not yet enforced**. A project default plus an optional
  per-task `**Mode:**` line in tasks.md, overriding in both directions: a
  single task can opt into the contract without converting a repo, and out
  while the project is `tdd`. A one-way override would let one unsuitable task
  force the whole project back.
  - An unrecognised mode is refused — by `validate` before a run, and by
    `resolve_execution_mode` at run time, naming the task when the task carries
    the typo. Silently defaulting a typo to `standard` is how a project comes
    to believe it is under the TDD contract while running without it.
  - The parser trims and case-folds `**Mode:**` (`TDD` and `tdd` are the same
    word) but never interprets it: an unknown word is stored as written and
    refused by the resolver. Mapping it to a known mode at parse time would
    hide exactly the typo the resolver exists to catch.
  - **Nothing branches on it.** A test asserts that no module on the execution
    path (`execution`, `hooks`, `runner`, `review`, `gates`, `cli`) so much as
    mentions the key — a claim that stays checkable, unlike a promise. It is to
    be deleted when the RED checkpoint (1b/1c) arrives, not widened.
    Design: `docs/superpowers/specs/2026-08-11-tdd-lifecycle-design.md` §3.1.

- **`review_policy: advisory | required`** (#157) — review can finally withhold
  completion, and the default (`advisory`) leaves every existing project
  untouched. First consumer of the #164 gate mechanism, which it does not own.
  - Under `required`: `failed` and `rejected` block; **`not_run` blocks too** —
    the review did not happen, and "I don't know" is not "fine", which is the
    #138 defect one level up; `error` is an *instrument* error (bounded
    recovery, then infrastructure error — not a defect in the work and not
    NEEDS_HUMAN); `passed`/`fixed` proceed, since `fixed` is a kind of pass
    rather than a peer of `passed`.
  - **The evidence names both trees.** The gate judges the *merge candidate*;
    review judged the *review checkpoint* (the pre-review commit, #103), and a
    `fixed` verdict means they differ. The verdict is stored against the merge
    candidate — staleness is still judged there — while the detail records
    which tree review actually saw. Claiming they are one tree would be the
    dishonest option.
  - **The gate does not read its own bookkeeping.** The verdict arrives through
    the new `GateContext.facts`, not from `phase_results`: that write is
    best-effort, so reading a blocking decision out of it would make "review
    produced no verdict" indistinguishable from "we could not read our own
    note". The first is a fact about the code, the second is our bug — a
    missing fact is an instrument error, never a verdict.
  - **`required` with review switched off is refused before the run starts**,
    by `validate` for YAML and at startup for `--no-review`. A required review
    that never runs can only ever block, and the merge gate is the wrong place
    to learn that. The gate still fails closed if both are bypassed.
  - Under `advisory` **no gate is registered at all** — not one that always
    passes, which would resolve a SHA and open the state DB on every task and
    turn #164 criterion 8 from a property into a claim.
  - The gate is evaluated **before** anything writes DONE to `tasks.md`. Running
    it after that write left a blocked task labelled `done` — the 2.23.0 class
    of defect (#164 criterion 1) inside the mechanism built to prevent it — and
    made the merge candidate a tree that already claimed the task was finished,
    which is circular. A blocked task now keeps its `review` status, its
    checkpoint commit, and its resumability.
  - Closes #134 item 4: a review that died with an execution error while the
    task closed as "No-op". #138/#156 made the verdict honest; this makes the
    lifecycle respect it.
    Design: `docs/superpowers/specs/2026-08-11-review-policy-design.md`.
- **Pre-terminal policy gates** (#164) — the mechanism the review policy (#157)
  and TDD's confirmed red (#141) will both hang off, so that the second
  consumer does not arrive as a special case inside the first one's code.
  - The naming trap this exists to avoid: a gate does **not** withhold the
    checkpoint commit. That commit always happens — a stable SHA is precisely
    what the gate is evaluated *against*, and replay without a commit to replay
    against is trust in whatever is in the working tree. What an unsatisfied
    gate withholds is progress past the checkpoint: merge and terminal
    completion.
  - Three statuses, not two: `satisfied`, `unsatisfied`, `instrument_error`.
    "The gate says no" and "the gate could not answer" have different owners.
    Only the latter is retried (`gate_recovery_attempts`, default 1, per gate);
    exhausting the bound is an *infrastructure error*, not NEEDS_HUMAN.
  - A verdict is bound to `(checkpoint_sha, config_hash)` and stored in a new
    `gate_verdicts` table. A stale verdict never clears a new tree — the
    harness-guard bypass of #137, one level up. `config_hash` covers only the
    policy-bearing keys (`gates.POLICY_KEYS`), so an unrelated config edit does
    not invalidate a verdict and a relevant one does.
  - **Dormant.** With nothing registered, no SHA is resolved, no state is
    opened, no row is written, and behaviour is unchanged. No consumer ships
    here: `review_policy` joins `POLICY_KEYS` with #157.
  - An unsatisfied gate deliberately adds no exit-code surface: it takes the
    existing "this attempt did not succeed" path, leaving the task resumable
    and the checkpoint in place. Design and the resolved open questions:
    `docs/superpowers/specs/2026-08-11-checkpoint-and-pre-terminal-gates-design.md`.
- **A typed outcome per phase, recorded append-only** (slice 0 of the lifecycle
  contract, #164 / #141 Part A). Until now a stage said only where it was, and
  — if it died — where it died: a stage either fell over or it did not. One
  phase already grew a real vocabulary under pressure (`review`, #138, because
  "no verdict" was being recorded as `passed`); this generalizes it.
  - `PhaseOutcome`: `pass` · `expected_fail` · `unexpected_fail` · `not_run` ·
    `error` · `skipped`. Six, because each implies a different move by whoever
    reads it — proceed; proceed (in TDD); fix the work; investigate the agent;
    fix the environment; nothing to do.
  - The vocabulary is a **base** set: admissible outcomes are declared per
    stage (`phases.ALLOWED_OUTCOMES`), so `expected_fail` is available to a
    test run and rejected for `commit` as a caller bug.
  - New `phase_results` (append-only) and `phase_waivers` tables. A **waiver is
    not an outcome**: it is an operator overriding one, it requires an actor
    and a reason, the harness never writes it, and the observed result stays —
    a report showing green for a waived phase can show that it was waived.
  - `ReviewVerdict` is readable as outcome + detail
    (`phases.review_verdict_to_phase`): `passed` and `fixed` are both `pass`.
    The stored wire values are unchanged — a reading, not a migration.
  - **Nothing gates on any of this.** The guarantee is the design's:
    *execution, terminal state and external contracts do not change* —
    deliberately not "byte identical", since these very rows make byte identity
    impossible. Recording is best-effort: a storage failure is logged and
    swallowed rather than able to fail a task.


### Fixed

- **A confirmed RED could be recorded without its claims** (#141). The
  checkpoint was written before the files were claimed, so a process dying
  between the two writes left a red that **counts** over a file **anyone may
  edit** — the gate would pass while the byte-lock silently did not exist. The
  order is now claims-first: the same crash leaves no confirmed red, and the
  next run re-authors. Found by the battle test, which is what a battle test is
  for.
- **An unlockable file still produced a confirmed RED.** A path the claim
  contract refuses — a symlink, a non-regular file — was skipped with a warning
  and the checkpoint recorded anyway, so the gate passed over a file nobody was
  protecting: the same hole as above by a different route. Claimability is now
  checked *before* anything is written, in both the RED phase and `repair`, and
  a red whose files cannot be locked is refused. A refused `repair` also keeps
  the lock it failed to replace.
- **A claim violation did not say where a renamed file went.** The message read
  `renamed tests/x.py` and stopped there, discarding the reason the violation
  kinds are distinguished at all — an operator was told a file had moved and
  not where to.

## [2.24.0] — 2026-08-11

Instruments that could not report failure. Every entry is a place where the
run's record said "fine" without having established it: a review that never
produced a verdict, a spec that never validated, a prompt from whichever
project happened to be the working directory. Plus the first read-only answer
to "what is missing before tasks can run at all".

Nothing here changes what a passing run does. The exit-code surface is
unchanged since 2.23.0 with one exception, called out under Changed.

### Added

- **`spec-runner preflight [--json]` — read-only readiness diagnostics**
  (issue #142, first slice). There was no zero stage: on a greenfield repo
  "what do I need before tasks can run" was answered one task failure at a
  time, and a gate that is green on an empty project answers nothing at all —
  an empty suite exits 0, and so does a linter with no files.
  - Eight checks (spec present, spec validates, agent CLI, test runner, test
    suite, lint runner, git, state dir), each with a status and a **separate**
    `blocking` flag: whether a missing thing stops a run depends on the config,
    and no git is not a blocker when git automation is off.
  - Six statuses, because "the tool is absent", "the suite is empty", "the
    oracle is broken" and "cannot be established" are four different
    situations: `ok`, `missing`, `empty`, `broken`, `unavailable`, `skipped`.
  - **An empty suite is a blocker, never `ok`** — `0 passed` and exit 0 is
    exactly the green that proves nothing.
  - **Never guesses.** A composite `test_command` is reported `unavailable`
    rather than inspected by picking a component (the #139 rule), as is an
    unrecognized runner.
  - `--json` is pinned by `schemas/preflight-result.schema.json` with a
    `schema_version` for consumers, and stdout carries exactly one document.
    Exit codes: 0 ready, 1 blocked.
  - Deliberately **not** included: `bootstrap` (creating a layout and choosing
    a toolchain is a separate product decision — it would make spec-runner a
    project scaffolder) and the mutation probe (certifying an oracle by
    breaking it belongs in a disposable worktree, not in diagnostics of the
    working tree). Preflight writes nothing at all; a test asserts no file in
    the tree is created, removed or touched.

### Fixed

- **A review that did not happen no longer reports as one that passed**
  (issue #138, correctness half). The stage could not fail a task by any
  route, yet the log read like a quality gate — and the worst path was silent
  approval: **output with no recognizable marker was recorded as `passed`**,
  so a reviewer that produced prose, ran out of context, or misunderstood the
  protocol counted as a clean review. Measured on a 26-task pilot: six
  15-minute timeouts, an hour and a half of wall time for advice that was
  never given, every one of those tasks closed DONE without a single finding.
  - Two new `ReviewVerdict` values make the four outcomes four different
    facts: `not_run` (timeout, empty response, no verdict marker) and `error`
    (CLI failure, rate limit, exception). `passed` now requires the reviewer
    to have actually said so.
  - Parallel review follows the same rule per role, and aggregation is
    ordered findings → error → not-run → passed, so a role that never
    answered can no longer be averaged away into an overall pass.
  - Progress lines and the run log name the outcome: the old
    `✅ Code review completed (no explicit status marker)` — tick and all —
    was precisely what made a non-review look like a review.
  - `docs/state-schema.md` documents the enlarged vocabulary; consumers should
    treat unknown values as unknown, as with `ErrorCode`.
  - A **non-zero exit is never a verdict**: the guard used to require empty
    output too, so a reviewer that crashed after printing `REVIEW_PASSED` was
    believed. Its output is still reported, just not read as a decision.
  - Parallel review commits a role's fixes whenever a role made them, rather
    than only when the aggregate says so — otherwise the edits were left for
    the general auto-commit, which runs no gates. `fixed` therefore outranks
    `error`/`not_run` in the aggregate (it is the verdict `post_done_hook`
    re-runs tests and lint on), while findings still outrank everything; the
    silent roles are named in the reason instead of vanishing.
  - `schemas/executor-state.schema.json` enumerates the new values. It is the
    frozen interop contract, so a value the code writes but the schema rejects
    would fail validation for consumers on perfectly healthy state.
  - **Blocking behaviour is deliberately unchanged.** Outside HITL every
    verdict stays advisory. Whether a review may fail a task, and whether the
    stage should move relative to the commit, are policy decisions tracked
    separately — this change only makes the record honest.

- **Prompt templates resolve from the project, never from the current
  directory** (issue #153). `PROMPTS_DIR` was the module-level relative
  `Path("spec/prompts")`, i.e. resolved against the *process* CWD: running
  spec-runner against one project from inside another silently used the
  latter's templates — and since a project template replaces the built-in
  prompt **wholesale**, that quietly changed what the agent was told. It had
  already substituted a result: a test asserting the built-in prompt documents
  `TASK_BLOCKED` received this repository's template, because pytest runs from
  the repo root.
  - Templates now come from `config.prompts_dir` — `spec_dir /
    "{spec_prefix}prompts"`, so it is namespaced by `--spec-prefix` and moves
    into the change dir under `--change`, like every other spec path. For a
    project laid out normally (`spec/prompts/`, run from its own root) nothing
    changes.
  - **The CWD lookup is gone, not kept as a fallback.** It only ever worked by
    accident, and an accidental match is exactly how the wrong project's
    template gets picked up. `load_prompt_template` now takes an explicit
    `prompts_dir`; omitting it means "no project template", not "search
    somewhere sensible".
  - Precedence is explicit and logged: CLI-specific template → generic project
    template → built-in prompt. One `Prompt template resolved` line records
    which file answered (or `built-in`), because that answer decides the
    agent's instructions and previously left no trace.
  - Inheritance of the built-in prompt by a custom template is **not** part of
    this fix — it is a separate composition contract, and mixing it in here
    would have hidden the isolation bug behind a feature.

### Changed

- **`plan --gated` writes a stage only after it validates, and repairs
  boundedly** (issue #160, the remainder of #133). The order was generate →
  write → validate → stamp the verdict onto the file just written, so an
  invalid spec landed on disk and stayed there: a DRAFT that looks like an
  artifact and is not one.
  - Validation now runs on the candidate **before** it is committed, using the
    same validator the run enforces — not a lookalike, which would drift and
    let a spec pass here and fail there. A rejected candidate leaves the stage
    exactly as it was: the previous draft is restored byte for byte, and a
    stage that had no file still has none.
  - On failure the stage is regenerated up to `spec_repair_attempts` times
    (default 2) with **the actual validation errors** in the prompt — "try
    again" is not a diagnostic. Bounded on purpose: a model that cannot
    satisfy the validator twice will not satisfy it on the tenth try, and each
    attempt costs money.
  - When no attempt validates, the command exits non-zero and writes nothing,
    where it used to exit 0 with `validation=fail` on disk.
  - The rollback itself is atomic (temp file + `os.replace`, shared with
    `write_spec` as `spec.atomic_write_bytes`): a plain write truncates first,
    so an interruption mid-restore would destroy the very draft the rollback
    exists to protect.
  - **No canonicalizing normalizer.** The alternative proposal — parse the
    generated file and rewrite it into canonical form — was rejected: a tool
    that guesses the meaning of an unfamiliar LLM format and then canonicalizes
    its own guess makes the mistake invisible. Unrecognized output is
    rejected, never rewritten; the written body is byte-identical to what was
    generated.

## [2.23.0] — 2026-08-10

Refusals that used to look like success. Every entry below comes from a live
run — the disputatio pilot and steward's V1 gated-cycle run — and the batch has
one theme: a gate that could not fail, a marker that was guessed at, or an exit
code that said "done" when nothing was.

**Read before upgrading if you consume the exit code**: `run` now exits 1 in
situations that previously exited 0 (see the first Fixed entry). The
`--json-result` payload and the state-DB schema are unchanged, and the
`stop_reason` vocabulary only gained one string, so a caller reading either of
those sees no difference.


### Fixed

- **`run` no longer exits 0 when the run did not finish** (issues #127, #129,
  #130, #131, #132, #134, #136). One class of defect with several entry
  points: the process exit code — the single signal an orchestrator like
  Maestro actually reads — said success while work was blocked, refused, or
  never executed. **Read this before upgrading if you consume the exit code:**
  runs that previously exited 0 in these situations now exit 1. The
  `stop_reason` vocabulary is unchanged apart from one addition, so a consumer
  reading `last_run_stop_reason` sees the same strings.
  - `on_task_failure: stop` now actually stops the run, immediately and
    non-zero, with `stop_reason=task_failed_stop` (#136). It marked the task
    blocked and returned `False`, but leaving the loop depended on
    `should_stop()` — "consecutive failures ≥ `max_consecutive_failures`
    (default 2) OR budget exhausted" — which a single failed task never
    tripped. Since v2.22.0's release notes recommend exactly this setting to
    orchestrator-managed runs, the documented remedy was a placebo: a
    production workstream followed it, closed DONE at 1 of 11 tasks, and was
    merged and turned into a PR.
  - Leftover blocked/failed work reports `dependency_blocked_after_skip` and
    exits 1 whether or not TODO tasks are still waiting (#131, #136). v2.22.0
    fired this only when *nothing* was todo, so the common shape — one blocked
    task, ten dependents waiting on it — took the plain "no more ready tasks"
    path and reported `completed`/0. The verdict belongs to the run loop,
    which observes what actually happened; the "nothing was ready to begin
    with" early return deliberately stays a quiet exit 0, because `--task` and
    `--milestone` routinely leave blocked work outside the selection.
  - A mid-run stop on `max_consecutive_failures`/budget exits non-zero, like
    the pre-run refusal for the same cause already did (#67).
  - `run`/`watch`/`retry` refusing on the spec-governance gate exit 1 and
    write their diagnostics to **stderr**, not stdout (#134, found by
    steward's live V1 run of the gated cycle). A policy rejection was
    indistinguishable from an empty queue, and the prose could land in the
    middle of `--json-result` output.
  - `--tui` propagates the exit code out of the run thread (#129). `sys.exit`
    inside a daemon thread is discarded by the interpreter, so every
    fail-closed gate was advisory under the TUI.
  - `state_spec_mismatch` sends the `run_complete` notification before
    exiting (#130) — the heaviest stop there is was the one Telegram/webhook
    owners never heard about.
  - The orphaned-success warning fires even when every task is done (#132);
    it used to be nested under "unfinished work exists".
  - Budget exhaustion no longer crashes with `KeyError` (#127). It wrote
    status `failed` into tasks.md, which has no such status — the terminal
    outcome stays in the state DB (`error_code=BUDGET_EXCEEDED`) and the file
    gets `blocked`, like every other terminal failure and, unlike `failed`,
    recoverable once the budget is raised. `update_task_status` now refuses an
    unknown status with `False` instead of raising.
- **A `tasks.md` meta line is recognized exactly, or the spec is refused**
  (issues #128, #133). Two symptoms, one defect — the parser guessing instead
  of refusing.
  - `TASK_META` read the status as `(\w+)`, so any prose starting `P0 | …`
    parsed as a meta line: `- P0 | high priority stuff` yielded the status
    `high` (#128). v2.22.0's bullet allowance widened the exposure to
    description bullets, and the damage is not cosmetic — `update_task_status`
    rewrites the *first* meta match under a header, so a prose bullet ahead of
    the real meta silently took the status write. The status is now one of
    `TODO|IN_PROGRESS|REVIEW|DONE|BLOCKED` (any case); every previously valid
    form — bare, bulleted, emoji — still parses.
  - A task whose meta line matched *nothing* is now a **validation error**
    rather than silent defaults (#133). `plan --full` has been observed
    emitting at least three meta orderings in a single pilot; the unrecognized
    ones left the task at its parse defaults (`p0`/`todo`), which read as
    perfectly ready — validation passed, dependencies resolved off invented
    statuses, and the run died on the first task at the 2.22.0 reconciliation
    gate. `Task.has_meta` records whether the values were stated or defaulted;
    the status/priority checks now only vouch for values that were actually
    read. The two halves ship together on purpose: tightening the pattern
    alone would have turned more unparseable metas into ready-looking TODOs.
  - The bundled `spec-generator-skill` template copy was kept in sync.
- **Test scoping no longer corrupts a composite `test_command`, and says when
  it narrowed the gate** (issue #139). `test_command` is a shell string, and
  real ones chain several programs:
  `pin_check.py && uv run pytest -q && uv run pyrefly check`. With no `tests/`
  token to substitute, the mapped test paths were appended to the end of the
  *whole* chain — i.e. handed to `pyrefly check`; the substitution branch was
  no safer, replacing the first `tests/` substring wherever it occurred.
  Composite commands are now left untouched (running the full declared gate is
  always safe; guessing which program takes test paths is not), and a
  non-composite command has its test-path *argument* replaced wholesale, so
  `pytest tests/unit` narrows properly instead of becoming
  `pytest <files>unit`. Scoping only ever applies in parallel mode.
  - The run's evidence now records the mode: one `Running tests` line always
    carries `scope=scoped|full` plus the reason. Before, only the scoped
    branch logged at all, so "ran the full suite" and "quietly ran a subset"
    looked identical in the record.
  - New `scoped_tests` config key (default `true`) forbids narrowing outright,
    for contracts where a subset is not a proof — workstream acceptance, a
    release gate.
- **`harness_guard: strict` is no longer disarmed by a retry** (#137,
  Critical). The guard snapshotted the oracle surface *inside each attempt*,
  so a forbidden edit that outlived a failed attempt became the next
  attempt's baseline and was legalised — the barrier held exactly once. Seen
  in production: attempt 1 failed with "the agent modified verification
  files: modified pyproject.toml", the edit stayed in the working tree,
  attempt 2 re-snapshotted the mutated file, passed, and the edit reached the
  history. With the default `max_retries: 3` the documented guarantee "the
  oracle surface is immutable" did not hold, and from the outside the run was
  indistinguishable from a clean one — one FAIL line, then green. The
  snapshot now belongs to the task's lifecycle (`harness.HarnessBaseline`,
  captured once after `pre_start_hook` so `uv sync` is still not a violation)
  and a divergence blocks every attempt regardless of its number.
- **`plan --gated` no longer demands a description for every stage** (#134
  item 2). README documents `spec-runner plan --gated --stage design` bare, but
  the command exited with "plan: provide a description argument or --from-file
  PATH" for stages past the first — found on steward's live V1 run of the gated
  cycle. A stage whose upstream is approved now inherits its description from
  that upstream (whose body is reproduced in the generation prompt anyway); only
  the first stage of the chain still requires one, and it says so by name.

### Added

- **`TASK_BLOCKED: <reason>` — a refusal the harness does not retry**
  (issue #140). Retry policy could not tell "I did not manage it" from "this
  cannot be done within the rules, an operator is needed": both produced
  `TASK_FAILED` and both got the full `max_retries` cycle. Observed in
  production: an agent hit a conflict between two byte-locked tests, behaved
  exactly as the project constitution prescribes — refused to edit an
  assertion for green, named the reason, stopped — and the harness answered
  with attempt 2 of 3 and "Do not repeat the same mistake", although the only
  non-erroneous path was forbidden to it. Attempts 2 and 3 were structurally
  doomed, and on one task attempt 2 crossed a scope boundary that attempt 1
  had correctly escalated about: the barrier held once and was removed by a
  retry, the same mechanism as #137.
  - The marker outranks the other two: `TASK_COMPLETE` alongside it does not
    close the task, and `TASK_FAILED` alongside it does not earn retries. A
    bare `TASK_BLOCKED` with no reason is terminal too — refusing to retry a
    stated refusal is the safe side of that ambiguity.
  - New `ErrorCode.TASK_BLOCKED`, classified fatal; `error` carries the
    agent's own wording verbatim, because the operator has to act on it.
    Documented in `docs/state-schema.md` for consumers separating "needs a
    human" from "worth another run".
  - The built-in task prompt now teaches the marker. Note that a project with
    its own `spec/prompts/task.*` overrides the built-in prompt **wholesale**,
    so such projects must add the instruction to their template themselves —
    there is no inheritance (see #153).
- **Gated authoring now materializes `traces_to` and `upstream_hashes`**
  (#135, DEC-008). Both are steward-owned governance keys that already rode
  through `SpecMeta.extra` as pass-through — nobody ever wrote them, so every
  spec-runner-authored bundle reached steward's gate as `GC-TRACE-EMPTY` +
  `GC-STALE-UNPINNED`. spec-runner is the only party that knows, at generation
  and approval time, what a stage was derived from and what the upstream bytes
  were, so it writes them now:
  - `traces_to` — a list: the stage's **direct** upstream stage name(s), then id
    tokens (`REQ-001`, `DESIGN-207`) carried by the body that actually resolve in
    the upstream text. Stamped whenever content is authored (`plan --gated`
    draft, `spec approve`, `spec adopt`). An existing value is kept and appended
    to, never replaced; a legacy scalar is normalized into the list shape.
  - `upstream_hashes` — `{direct upstream stage: git blob hash}`, reproducible
    with `git hash-object <file>`. Stamped at approval only, since that is what
    it records. Re-approving an upstream deliberately leaves the downstream pin
    alone: the mismatch *is* the stale signal.
  - No `SPEC_META_CONTRACT` bump: these are extras, not canonical fields.
  - The shipped golden fixture is corrected — it showed `traces_to` as a scalar
    and pinned a transitive ancestor, neither of which a consumer's reader
    accepts. `docs/CONTRACTS.md` documents both shapes and the rules behind them.

### Changed

- **Docs: `run` executes one task; `run --all` drains the queue** (#134 item
  3). The same live run read `completed=1, remaining=11` on a fully approved
  `tasks.md` as a defect. The behavior is by design — a single `run` is the unit
  an orchestrator schedules — and README now says so where the commands are
  listed. Also names `--no-interactive` next to the gated checkpoint menu, which
  the docs described without ever giving its flag.
- **Docs: the `review-pr` caller contract is consumer-agnostic** (PR #119).
  Clarified that Maestro lifecycle mapping is consumer-owned and not part
  of the `review-pr` CLI contract. The design doc's "External caller
  contract" section now states only what spec-runner promises (invocation,
  exit codes 0/1/2, one-JSON-document stdout, stderr diagnostics,
  idempotent resume, mutating-mode preconditions) and points at the
  consumer's own track instead of restating its lifecycle. The v2.20.0
  release notes are left as the historical artifact they are.

## [2.22.0] — 2026-08-08

Task-status integrity: fail-closed fixes plus honest stop-reason
diagnostics, found by a live disputatio run (D3, 2026-08-08; maestro#164)
whose forensic snapshot (`tasks-193159.md`) is now this release's golden
regression fixture (`tests/fixtures/maestro-interop/alternating-bullet-tasks.md`).
Minor, not patch: `run --all` now exits non-zero on a state/spec
disagreement it used to silently report as success (see the exit-behavior
matrix below).

### Fixed

- **`update_task_status` is task-bounded** (issue #123). The status rewrite
  now matches the target task's header by exact ID — not substring, so
  `TASK-001` no longer matches `TASK-0011` — and only searches for a meta
  line between that header and the next one. A write that can't find its
  own task's meta in that window returns `False` without touching the file
  or the history log, instead of falling through onto a neighboring task's
  meta line (the incident: updating `TASK-001` repainted `TASK-002`).
  `update_checklist_item`/`mark_all_checklist_done` got the same exact-ID
  fix. The bundled `spec-generator-skill` template copy was kept in sync.
- **`TASK_META` recognizes bullet-prefixed meta lines** (issue #123).
  Agents editing `tasks.md` mid-run introduce a bullet prefix on meta lines
  (`- 🔴 P0 | ...` / `* P0 | ...`), confirmed forensically via git-status
  correlation on the incident snapshot — not the generator templates,
  which emit the bare form. The parser and `update_task_status` previously
  only matched the bare form, so those tasks' meta was invisible to both;
  the parser must now accept both formats regardless of source. The
  bullet prefix requires the `P\d |` form immediately after it, so plain
  description bullets and checklist items stay unaffected. The bundled
  `spec-generator-skill` template copy of both fixes was kept in sync.
- **`run --all` fails closed on a state-DB/tasks.md mismatch** (issue #124).
  Two gates now stop the run non-zero (`state_spec_mismatch`) instead of
  reporting exit 0: immediately after each task, if the state DB just
  recorded success but tasks.md doesn't show `done`; and as a backstop when
  the loop runs out of ready tasks, if any state-DB success was never
  reflected in tasks.md. A legitimate block (a TODO waiting on a documented
  failed/skipped dependency) leaves both sets in agreement and is
  unaffected. A state-DB success whose task ID isn't in tasks.md *at all*
  (removed from the spec, not merely left non-done) has nothing to
  reconcile against — that case only warns, it doesn't fail the run closed.

### Changed

- **Honest `stop_reason` for blocked-after-skip runs.** `run --all` used to
  report `stop_reason="completed"` (the default, unchanged) whenever
  nothing was left `todo` — even when the reason nothing was left `todo`
  was that `on_task_failure="skip"` gave up on a task and left it `blocked`.
  The "no more ready tasks" branch now tells the two apart: if every
  remaining task is non-`todo` *and* non-`done` (blocked, or an interrupted
  `review`), `stop_reason` becomes `dependency_blocked_after_skip` with the
  stuck task IDs in `stop_detail`, instead of the misleading "All tasks
  completed". **Exit code is unchanged (still 0)** — this is diagnostics
  only; making it non-zero is a separate interop follow-up. The reason now
  also reaches the `run_ended` audit event and the `run_complete`
  notification's message (a "Stop reason: ..." line, appended whenever the
  reason isn't `completed` — not just for this new one) — both already
  carried other stop reasons the same way; this fills the one gap in the
  loop-exit path. Note: Maestro doesn't read `stop_reason` today (it isn't
  wired to consume that meta key), so this is a spec-runner-side fix only —
  picking it up needs work on Maestro's side too, which reinforces the
  min-gate recommendation below.

### Exit-behavior matrix for `run --all` (this release)

| Situation | Exit code before 2.22.0 | Exit code in 2.22.0 | `stop_reason` |
|---|---|---|---|
| Every task reaches `done` | 0 | 0 (unchanged) | `completed` |
| A task is `blocked`/stuck after `on_task_failure="skip"` gives up, and no task is left `todo` | 0 | 0 (unchanged) | `completed` → **`dependency_blocked_after_skip`** |
| Downstream TODOs remain unreachable behind a failed/blocked dependency | 0 | 0 (unchanged) | `completed` (unchanged — see the `on_task_failure: stop` recommendation below) |
| State-DB records success but tasks.md never shows `done` for that task | 0 | **1** | `completed` → **`state_spec_mismatch`** |
| State-DB success for a task ID no longer in tasks.md at all | 0 | 0 (unchanged, now with a warning) | unaffected |

Orchestrator guidance: callers that must not silently absorb a stuck run
(Maestro and similar) should set `on_task_failure: stop` rather than the
default `skip` — `stop` surfaces a failing task as a non-zero exit
immediately instead of leaving it `blocked` for a caller to discover later
via `stop_reason`. Maestro should raise its minimum/capability gate to
require spec-runner ≥ 2.22.0 for the `state_spec_mismatch` exit=1 behavior
(#124) — a caller pinned below this version will keep reading a real
state/tasks disagreement as a successful run.

## [2.21.0] — 2026-08-06

Unblocks Maestro's accepted `post-pr-command` work (design maestro#147):
`review-pr --json` is now a clean machine interface on every exit path.
Minor, not patch — the payload gains an additive `exit_code` key
(same rule as `no_op` in 2.16.0). Maestro pins this version before
shipping its wrapper.

### Fixed

- **`review-pr --json`: stdout is exactly one JSON document** (inbox issue
  #116 from maestro's `post-pr-command`; PR #117). Limit stops and
  fail-closed paths printed diagnostics to stdout before the report, and
  exit 1 emitted no JSON at all — a consumer storing the report verbatim
  (Maestro's `review-pr` wrapper, design maestro#147) could not parse it.
  All diagnostics now go to stderr; every exit path (0/1/2) emits one JSON
  document, and the payload gains `exit_code` so a stored report is
  self-describing. On exit 1 the document is
  `{repo, pr_number, error, exit_code}` (`repo`/`pr_number` `null` when the
  ref could not be resolved). Text mode is unchanged.

## [2.20.0] — 2026-08-06

Phase M3 completes the review-bot loop (issue #102, now closed): the
optional post-PR stage wires `review-pr` into the run itself, and the
external caller contract is documented for orchestrators. All three
phases shipped the same day the design was approved (M1 v2.18.0,
M2 v2.19.0, M3 here). Maestro interop contract unchanged.

### Added

- **`review-pr` phase M3 — optional post-PR stage + external caller
  contract** (issue #102, final phase; PR #114). `review_pr.post_pr:
  off | verify | full` (default `off` — the `integration_pr` flow stays
  byte-identical without configuration) wires the loop into the run
  itself: after the integration PR is announced, the stage waits
  `post_pr_wait_seconds` (default 120) for the review bot, then runs the
  read-only triage (`verify`) or checks the run branch out, runs the full
  fix+reply loop, and always returns to the base branch (`full`). The
  stage never changes the run's exit status. The external caller contract
  (exit codes 0/1/2 + `--json` surface, Maestro hook mapping) is now
  documented in the design doc. This completes #102.

## [2.19.0] — 2026-08-06

Phase M2 of the review-bot loop (issue #102): `review-pr` now closes the
whole cycle — verify, fix, gate, push, reply — under hard limits and
fail-closed rules. Only the optional post-PR stage (M3) remains. The
Maestro interop contract is unchanged; the experimental review-pr state
tables gain resolution/reply bookkeeping.

### Added

- **`review-pr` phase M2 — fix + reply** (issue #102; PR #112). The default
  invocation now runs the full loop: valid comments are fixed by a TDD
  agent (each fix is a separate commit with a `Review-Comment-Id`
  provenance trailer), the project gates (tests + lint) run after every
  mutation and a failing gate reverts the fix, all fix commits are pushed
  once, and only after a successful push does the loop reply in each
  thread — with the actual fix SHA, or with the verification evidence for
  refuted comments. `uncertain` comments are never fixed and never
  auto-answered. Hard limits (`review_pr.max_rounds` / `max_comments` /
  `max_changed_lines` / `max_cost_usd` / `max_wall_minutes`) stop the loop
  with `NEEDS_HUMAN`; dirty tree, head-SHA mismatch, force-push and push
  failures are fail-closed (exit 1, no replies). Reply idempotency is
  persisted (`replied_at`) — re-invocations never answer twice.
  `--verify-only` preserves the read-only M1 behavior and its exit
  semantics; `spec-runner status` now surfaces comments awaiting a human.

## [2.18.0] — 2026-08-06

Phase M1 of the review-bot loop (issue #102): the read-only
`review-pr` command ships; fix/reply (M2) and the post-PR stage (M3)
follow. The Maestro interop contract is unchanged — the new
`pr_review_comments` state table is experimental and additive.

### Added

- **`spec-runner review-pr <url-or-number>` — phase M1 of the review-bot
  loop** (issue #102, battle-testing F-22; design:
  `docs/superpowers/specs/2026-08-06-review-pr-loop-design.md`; PR #110).
  Read-only: collects inline PR comments from allowed bot identities
  (`review_pr.allowed_bots` config, default Copilot), verifies each against
  the codebase with an agent call, persists per-comment verdicts
  (`valid`/`refuted`/`uncertain`) in a new `pr_review_comments` table — the
  durable cursor makes re-invocations resume instead of re-processing — and
  prints a text or `--json` report. Fail-closed throughout: draft/closed
  PRs and API errors exit 1; missing verdict markers and verifiers that
  mutate the working tree become `uncertain`; any uncertain/unverified
  comment exits 2 (`NEEDS_HUMAN`). Stable exit-code contract (0/1/2) for
  external callers (the future Maestro hook). No fixes, no replies, no
  pushes in M1.

## [2.17.0] — 2026-08-06

Battle-testing round 4 (kapelle TASK-007 on v2.16.0, run d4d33ad0):
three of the four findings fixed the day they were filed. The Maestro
interop contract is unchanged; the notification surface gains one event.
The fourth finding (#102, review-bot loop) awaits an ownership decision.

### Added

- **`pr_opened` notification event** (battle-testing F-21, issue #101;
  PR #107). When a run opens an integration PR, the event is now pushed
  through the configured Telegram/webhook channels (and is in the default
  `notify_on`), so the human-merge gate no longer depends on someone
  watching the terminal. External consoles (e.g. a dispatcher inbox) can
  consume the webhook.

### Fixed

- **Exec-stage work is committed under the task label before review runs**
  (battle-testing F-23, issue #103; PR #105). The review stage commits its
  own fixes, and with nothing committed before it, that commit swept the
  entire feature under a "code review fixes" label while the final task
  commit got only the tasks.md leftovers — git history inverted relative
  to content (kapelle PR #6). The pre-review commit also protects the work
  from the next task's pre-start cleanup, and the #97 no-op detection
  accounts for it (a task whose work was captured pre-review is not
  flagged no-op by the bookkeeping-only final commit).
- **Execution summary reports this run's counts** (battle-testing F-24,
  issue #104; PR #106). `completed`/`failed`/`failed_attempts` in the
  end-of-run summary, the `run_complete` notification and the `run_ended`
  audit record were cumulative across runs (monotonic executor_meta
  counters and full attempt history); a single-task run could end with
  `completed=2`. They now report the delta for the run; the cumulative
  counters themselves are unchanged.

## [2.16.0] — 2026-08-05

Battle-testing S2 round 3 (kapelle, Maestro orchestration): both findings
fixed the day they were filed. The Maestro interop contract gains one
additive `--json-result` key (`no_op`, emitted only when true) — non-breaking
per the change policy in `docs/state-schema.md`; all pre-existing golden
fixtures are byte-identical.

### Added

- **Explicit `no-op` completion marker** (battle-testing S2 finding F-20,
  issue #97, maestro side maestro#123; PR #99). A task that completes with
  nothing to commit (its work was already absorbed by earlier tasks) is now
  visibly a no-op instead of looking like a silently-missing task: new
  `no_op` column on `attempts` (idempotent migration), `"no_op": true` in
  `--json-result` (additive, emitted only when true — existing consumers see
  byte-identical output; new golden fixture `json-result-single-noop.json`),
  `[no-op]` tag in `spec-runner status` task history, and a
  `✔️ No-op: completed without changes` progress line. Non-breaking per the
  change policy in `docs/state-schema.md`.

### Fixed

- **Harness-written `spec/.gitignore` no longer lands in auto-commits**
  (battle-testing S2 finding M-03, issue #96, counterpart of maestro#122;
  PR #98). The #62 fix writes `spec/.gitignore` to protect executor runtime
  state, but `git add -A` in `stage_all_except_runtime` swept it into the
  first subtask's auto-commit — a file no agent chose to create in the
  workstream diff, which Maestro's ex-post scope gate rightly flags as a
  scope escape (both parallel kapelle S2 workstreams went NEEDS_REVIEW
  with all subtasks green). The file is now excluded from the commit set
  when it is not tracked in HEAD; a user-tracked `spec/.gitignore` keeps
  the old travels-with-the-spec behavior and is never staged for deletion.

## [2.15.0] — 2026-08-05

Long-tail cleanup after the two battle-testing waves: the oldest open user
breakage (`--spec-prefix` swallowed by the CLI parser) and the DEC-007
documentation drift. The Maestro interop contract is unchanged;
`SPEC_META_CONTRACT` stays 2 (the golden fixture's example value changes,
not the contract shape).

### Fixed

- **Common flags placed before the subcommand are no longer swallowed**
  (TODO slug `spec-prefix-swallow`, found during the C1 dogfood; PR #93).
  Every option of the shared `common` parent was declared on the top-level
  parser AND on each subparser; the subparser re-applied its default after
  the top-level parse, silently clobbering values given before the
  subcommand — `spec-runner --spec-prefix=phase2- run` ran unprefixed, and
  spec-runner-vscode emits exactly that argv order, so its `specPrefix`
  setting never reached the CLI. The same swallow hit every common flag
  (`--budget`, `--no-review`, …). The `spec status/approve/reject/adopt/
  check` family additionally rejected the flag outright (not parented on
  `common`). Fix: `common` uses SUPPRESS defaults and the top-level parser
  restores documented defaults once after the full parse (with a build-time
  drift-guard assertion); the `spec` family gains the common options. When
  a flag is given both before and after the subcommand, the subcommand
  position wins. The VSCode extension needs no changes — its argv order now
  works as-is.

### Changed

- **`owner_role` documentation aligned with DEC-007** (steward role catalog,
  decided 2026-07-26). `docs/CONTRACTS.md`, the spec-frontmatter schema, the
  inline comment and the **golden fixture shipped as package data** now show
  the canonical single role-slug form (`platform` — one accountable role, no
  `@`) instead of the retired `"@role[,@role]"`. No behaviour change:
  spec-runner remains a pure carrier (string-or-None), and legacy values are
  still round-tripped verbatim — pinned by a new regression test — because
  steward's own data has not fully migrated. `SPEC_META_CONTRACT` stays 2.

## [2.14.0] — 2026-08-05

Second battle-testing wave: the five enhancement/hardening issues from the
2.11.0 field trial (#64, #66, #69, #72, #73). The Maestro interop contract
(`.executor-state.db` schema, `--json-result` stdout) is unchanged; the one
contract edit is an additive enum value on the VSCode read surface
(`costs.schema.json`), updated in lockstep with its drift-guard test.

### Added

- **Fail-closed dirty-spec pre-run guard** (#69; PR #87). `run`/`watch`/
  `retry` refuse (exit 1, offending `git status` lines printed) when the
  spec content files or the config have uncommitted changes — an executed
  spec now always has a committed version predating the run. Enforced only
  when spec-runner's own git automation is on (subdir projects keep a
  permanently dirty tasks.md by design); tracked deletions count as dirt; a
  failing `git status` fails closed; `--allow-dirty-spec` overrides.
  Orchestrators that keep generated specs gitignored (Maestro's
  info/exclude pattern) see zero dirt — verified against Maestro's
  workspace lifecycle before choosing the fail-closed default.
- **Integration-PR closed loop** (#73; PR #88). When a run opens an
  integration PR it now prints an explicit stderr block (merge required
  before the next run) and persists `last_run_pr_url` in `executor_meta`;
  `status` repeats the marker until the new **`spec-runner sync`** command
  clears it. `sync` is the post-merge closer: no-active-run lock check,
  clean worktree (executor runtime state never counts), switch to base,
  `pull --ff-only` + `fetch --prune`, deletion of **merged-only** `task/*`
  and `spec-runner/run-*` branches locally and on the remote
  (ancestor-check, never force, foreign branches untouched), state sanity.
  Each step reports ✓/✗ with detail; failed deletions fail the step;
  `--dry-run` previews; non-zero exit when the tree can't host the next run.
- **Task ids accept any uppercase prefix** (#72; PR #89). `### KAP-002:`
  headers parse natively (`ID_PATTERN`); `Depends on:`/`Blocks:` refs are
  filtered against the prefixes task headers actually use, so `[REQ-001]`
  in those lines stays documentation. gh-sync title matching generalized.
  Zero config — the proposed `task_id_prefix` key turned out unnecessary.
- **Harness-mutation tripwire** (#64; PR #90). The verification harness
  (dependency manifests, pytest/tox/setup configs, conftest.py,
  package.json/mix.exs/Cargo.toml/go.mod, Makefile, CI workflows, plus
  `harness_files` extras) is snapshotted before the agent runs;
  created/modified/deleted files are violations checked BEFORE the gates.
  `harness_guard: warn` (default) logs provenance; `strict` fails the
  attempt with a retry-prompt-feeding error (exemptions via
  `harness_allow` globs); `off` disables. Unreadable files get a sentinel
  hash (a chmod-000 file cannot bypass the guard); config values are
  validated at load.
- **Intermediate `🔍 REVIEW` file status** (#66; PR #91). Written to
  tasks.md when the tests/lint gates pass and code review starts — a run
  killed during review leaves an honest intermediate state instead of a
  premature DONE. Scheduled like IN_PROGRESS (resumable, not done for
  dependents, skipped by `--restart`). Maestro is unaffected (it reads
  only the SQLite state, whose vocabulary is untouched); the VSCode read
  surface maps unknown statuses to an explicit `unknown`, and its vendored
  `costs.schema.json` status enum gains `review` here in lockstep
  (re-vendor requested via spec-runner-vscode#16).

## [2.13.0] — 2026-08-05

Battle-testing release: every change comes from a field trial of 2.11.0 on an
external Elixir/Phoenix repo with the claude CLI (issues #62–#74). The Maestro
interop contract (`.executor-state.db` schema, `--json-result` stdout) is
unchanged — the one schema edit is additive on an experimental-tier column.

### Fixed

- **`doctor`'s $0.50 budget default leaked into every subcommand** (#68, #67;
  PR #78). Subparsers are built with `parents=[common]`, so argparse shares
  the `--budget` Action object across all of them —
  `doctor_parser.set_defaults(budget=0.5)` mutated that shared action and
  every command (`run`, `status`, `costs`, …) silently ran with a $0.50
  budget, overriding the YAML `budget_usd`. Once a previous run's recorded
  cost crossed $0.50, the next `run` refused to start. The doctor default now
  lives in `cmd_doctor` (`DOCTOR_DEFAULT_BUDGET_USD`); the parser default is
  `None` everywhere.
- **A refused run now names its actual cause and exits non-zero** (#67;
  PR #78). The pre-run `should_stop()` refusal always logged "Stopped due to
  consecutive failures" — with a contradictory counter of 0 when the real
  cause was the budget — and exited 0, so orchestrators read it as success.
  New `ExecutorState.stop_cause()` distinguishes `max_consecutive_failures`
  from `budget_exceeded`; the refusal prints the cause, persists it as
  `last_run_stop_reason` (new meta value `budget_exceeded`, experimental
  tier), records the audit event, and exits 1. Mid-run budget stops are
  persisted as `budget_exceeded` too instead of masquerading as
  `max_consecutive_failures`.
- **Executor runtime state can no longer be committed** (#62, root cause of
  #67's state loss; PR #79). `git add -A` in the auto-commit and in both
  review-fix commit sites swept `spec/.executor-state.db` (+`-wal`/`-shm`),
  the executor lock, progress file, task history and executor logs into the
  task branch; the tracked DB was then reverted by the next branch switch
  *under the open SQLite connection*, losing the run's success status and
  costs (`status` showed `running: 1`, `costs` showed $0.00) and blocking
  return-to-base in `integration_pr` mode. All commit sites now stage via
  `git_ops.stage_all_except_runtime()`, which also untracks state files
  committed by the old behavior; `pre_start_hook` maintains a
  `spec/.gitignore` covering runtime files (spec-prefix and change-dir
  aware); runtime-only churn no longer produces commits. The
  return-to-base failure is a loud stderr error with recovery instructions
  instead of a scrolled-away warning.
- **Review fixes are gated again before commit** (#65; PR #80). A
  `REVIEW_FIXED` verdict mutates the code after the tests/lint gates ran, so
  a broken review fix could be committed and merged as a "successful" run.
  `post_done_hook` now re-runs the full test suite and a strict lint check
  (deliberately without auto-fix) after a FIXED verdict; red gates fail the
  attempt with the usual `TEST_FAILURE`/`LINT_FAILURE` classification.
- **`run --dry-run --json-result` reported `checklist_done == total` for
  untouched tasks** (#71; PR #82). Checklist tuples are `(item, checked)` but
  were unpacked as `(done, _)`, counting truthy item *strings*. Now derived
  from `Task.checklist_progress`.
- **`status` no longer shows file-DONE tasks as "Not started"** (#68;
  PR #85). Tasks ticked ✅ DONE in `tasks.md` that the executor never ran
  (manual bootstrap, another tool) appear under a new "Done outside
  executor" bucket in the text display. `status --json` is a stable contract
  surface and is untouched.

### Added

- **Loud warning when no config file backs an execution command** (#63;
  PR #81). A missing `spec-runner.config.yaml` used to silently flip a run
  to all defaults — including `integration_pr=false` (self-merge into the
  main branch) and a Python test command on non-Python repos. `run`/`watch`/
  `retry` now print an operator-facing stderr warning naming the *effective*
  merge mode, test command and model; when prior run state exists (evidence
  the config vanished rather than never existed) the hint is sharper.
- **`commands.sync` config key + stack-aware dependency sync** (#70; PR #83).
  `pre_start_hook` hardcoded `uv sync`, producing per-run stderr noise on
  every non-Python project. A configured `commands.sync` (e.g.
  `mix deps.get`) now runs instead; with no key set, `uv sync` runs only
  when `pyproject.toml` exists, else the stage is skipped quietly.
  `sync_deps: false` still disables the stage entirely.

### Changed

- **Execution stage renamed `codex` → `exec`** (#74; PR #84). Run logs said
  `⏳ stage: codex` even when running the claude CLI. `error_stage` is an
  experimental-tier column: the state schema lists `exec` and keeps `codex`
  valid for rows written by ≤2.12.
- **Branch slugs strip punctuation** (#74; PR #84). Task names with commas
  or `+` produced branches like `task/task-004-fake-executor-+-fake-judge,-sy`
  — valid for git, brittle for tooling/URLs. Slugging now collapses
  non-alphanumeric runs into `-`, trims trailing dashes after truncation,
  and falls back to the bare task id for punctuation-only names.

## [2.12.0] — 2026-08-04

### Changed

- **`mcp` floor raised to `mcp>=2.0.0,<3`** — spec-runner now requires the MCP
  Python SDK v2 line; SDK v1 (`mcp<2`, the `[2.11.1]` ceiling) is no longer
  supported. This is a dependency-major bump for anyone pinning `mcp` directly
  alongside spec-runner.
- **`mcp_server.py` migrated from `FastMCP` to `MCPServer`.**
  `from mcp.server.fastmcp import FastMCP` → `from mcp.server import
  MCPServer`; `mcp_app = FastMCP("spec-runner")` → `mcp_app =
  MCPServer("spec-runner")`. All `@mcp_app.tool()` decorators and
  `mcp_app.run(transport="stdio")` are unchanged — the tool surface (status,
  tasks, costs, logs, run_task, stop, next_tasks, task_detail) is identical.
  Verified with a new in-memory wire test (`tests/test_mcp_v2_wire.py`) that
  drives SDK v2's `Client(mcp_app)` directly against the server object and
  asserts `list_tools()` returns all 8 tools — exercising the actual MCP
  protocol surface rather than only the Python-level handler functions.

### Fixed

- **`import spec_runner` no longer requires (or can be broken by) the `mcp`
  SDK.** `__init__.py` previously imported `mcp_server.run_server` eagerly at
  module load, so any problem with the `mcp` package — including the exact
  v1/v2 breakage this release line exists to fix — took down the whole
  package, not just the MCP server. `mcp_run_server` is now resolved lazily
  through `__getattr__`, imported only when actually accessed; a
  `TYPE_CHECKING`-guarded import keeps the name visible to static analyzers
  (resolving a pyrefly `bad-dunder-all` error) without importing `mcp` at
  runtime.

## [2.11.1] — 2026-08-04

### Added

- **CI guard against an untagged release.** `publish.yml` fires on a pushed tag
  and verifies it matches the pyproject version; nothing verified the reverse —
  a release commit landing on `master` with a bumped version and no tag. That is
  the failure that actually happened, twice: v2.4.0 and v2.10.0 both sat on
  `master` untagged, so PyPI lagged and consumers pinning the published version
  stayed blocked. The new `release-tag-guard` workflow never runs on pull
  requests, so a release PR stays green while its tag legitimately does not exist
  yet; it goes red the moment an untagged release commit merges. It also runs on
  `v*` tag pushes, so tagging the release commit re-runs it on the same SHA and
  the passing run supersedes the failed one — without that trigger the red would
  linger until an unrelated commit landed, since a tag push does not re-run a
  branch-triggered workflow. `workflow_dispatch` is available for the case where
  the tag lands on an older commit than master's HEAD.

### Fixed

- **`mcp` ceiling-pinned to `<2`.** mcp 2.0.0 (released 2026-07-28) removed
  `mcp.server.fastmcp`, which `mcp_server.py`'s `FastMCP` import depends on.
  The previous unbounded floor (`mcp>=1.26.0`) meant a fresh install pulled
  mcp 2.x and broke immediately — `src/spec_runner/__init__.py` imported the
  MCP server eagerly at the time, so even `import spec_runner` crashed, not
  just `spec-runner mcp`. Pinned `mcp>=1.26.0,<2` as an interim hotfix until
  the SDK v2 migration lands (see `[2.12.0]`).

## [2.11.0] — 2026-07-26

The SpecMeta frontmatter contract becomes losslessly extensible, and the spec
surface becomes genuinely profile-aware. Two governance defects that were live
in released versions are fixed: the `run --strict` gate could be bypassed under
a custom stage profile, and `plan --gated` crashed on one. The Maestro interop
contract (`.executor-state.db` schema, `--json-result` stdout) is unchanged.

Additive only — nothing is removed from any contract surface. Consumers pinning
the frontmatter contract should read `docs/CONTRACTS.md` for the field table, the
frozen public surface and the contract changelog, and pin `SPEC_META_CONTRACT = 2`.

### Added

- **`SpecMeta.owner_role: str | None`** — a first-class field carrying
  CODEOWNERS role(s) (`"@role[,@role]"`, e.g. `"@platform,@sre"`). The role
  semantics belong to the consumer (steward); spec-runner is only the
  carrier. `None` is omitted from rendered frontmatter, so existing spec
  files don't gain an `owner_role: null` key on their next write.
- **`SpecMeta.extra: dict[str, Any]`** — foreign frontmatter keys (e.g.
  steward's `traces_to`, `upstream_hashes`) are now preserved verbatim
  through parse and render. The canonical wire fields are
  `fields(SpecMeta) - {"extra"}`, computed by subtraction so an internal
  dataclass field can never silently widen the wire contract — meaning a
  frontmatter key literally named `extra` is itself foreign data and lands
  in `meta.extra["extra"]`.
- **`SPEC_META_CONTRACT = 2`**, declared upstream for the first time. v1 was
  the implicit historical contract a consumer (steward) had pinned by
  inferring it from observed behaviour. Bump policy: adding an optional
  field does not bump the contract; removing or renaming one does; the
  existence of `extra` never bumps it by itself.
- **A frozen public surface** exported from `spec_runner`: `SpecMeta`,
  `SpecMetaError`, `SPEC_META_CONTRACT`, `SPEC_STAGES`, `split_frontmatter`,
  `strip_frontmatter`, `split_frontmatter_raw`, `read_spec_meta`,
  `read_spec_body`, `write_spec`, `meta_from_dict`, `meta_to_dict`.
  Everything else in `spec.py` is private and outside the contract.
- **`docs/CONTRACTS.md`** — the field table, semantics, round-trip
  guarantee, bump policy and contract changelog for the SpecMeta frontmatter
  contract.
- **A golden fixture**
  (`src/spec_runner/contract_fixtures/spec_meta_contract_v2.md`) shipped as
  package data (`importlib.resources`), so a consumer can validate its own
  parser against the exact same bytes spec-runner tests against.

### Changed

- **Gated generation now gates on a stage's *direct* `requires` only.** The
  removed `_UPSTREAM` map demanded that both `requirements` and `design` be
  approved before `tasks` could generate, while `lite.yaml` declares `tasks`
  as requiring only `design`. The stage profile is the single source of
  truth, and `requires` describes direct DAG edges, not the transitive
  closure — which matters for branching profiles, where a hardcoded closure
  is actively wrong. In a normal lifecycle nothing changes: re-approving an
  upstream stales its downstream, so a staled `design` still blocks `tasks`.
  What's newly allowed is reachable through `spec reject` alone, with no file
  editing required: `cmd_spec_reject` writes only the rejected stage's own
  file and does not cascade stale, so `spec reject requirements` after
  `design` is already approved leaves `design` approved while `requirements`
  returns to draft. Even then, `resolve_next_stage` still auto-resolves to
  `requirements`, so only an explicit `plan --gated --stage tasks` reaches
  the `tasks` generator in that state — and it produces a draft that still
  needs `spec approve tasks` before `run --strict` will pass.
- **The generation prompt's context is deliberately *not* narrowed to match.**
  A new `ancestor_stages()` helper in `spec.py` (the mirror of the existing
  `downstream_stages()`) computes the transitive closure of a stage's
  ancestors, so the `tasks` generation prompt still embeds both the approved
  requirements and design, in topological order — gate and prompt context are
  now separate concerns fed by different graph walks. The default `lite`
  pipeline's generated prompts stay byte-identical to before, reproven by the
  unchanged C1 zero-behaviour golden fixtures.
- **spec-runner no longer discards foreign frontmatter keys on write.**
  Previously `meta_to_dict` was `asdict(meta)` and `meta_from_dict` silently
  dropped unknown keys, so every `spec approve` / `write_spec` / stale
  cascade erased them — a real data-loss bug for extending layers (steward),
  not a missing convenience. Frontmatter is now losslessly extensible. The
  round-trip guarantee is *semantic*, not textual: keys and YAML values
  survive; comments, quoting style and original key order do not.
- **Canonical frontmatter fields are now validated.** They were previously
  accepted unchecked — dataclasses don't enforce types, so `version:
  "three"` was silently stored as the string `'three'` and a non-string
  YAML key was silently dropped. A violation now raises `SpecMetaError`.
  `version` uses `type(v) is int` so `version: true` cannot pass as `1`;
  `status` is value-checked against `draft`/`approved`/`stale` because it
  drives the state machine; `validation` is type-checked only, since it
  drives no decision. As a documented compatibility exception,
  `generated_at` and `approved_at` also accept YAML's native date scalars
  and normalize them via `.isoformat()`.
- **A recognized-but-malformed spec now fails loud instead of silently
  reading as unmanaged.** Unmanaged/foreign documents still return `None`
  permissively — that matters, since "unmanaged" passes the governance gate
  — but once `spec_stage` is recognized, the document can no longer
  silently degrade. Syntactically invalid frontmatter YAML remains
  unmanaged, since the stage cannot be read at all in that case.
- **The VS Code frontmatter schema is now open**
  (`additionalProperties: true`) and gained `owner_role`. It previously
  declared `additionalProperties: false` as a drift alarm, which
  contradicts a deliberately extensible frontmatter; that protection moved
  to an exact canonical-field test, which is stricter. This is a cross-repo
  contract consumed by `spec-runner-vscode`.

### Fixed

- **`requires-python` corrected to `>=3.11`.** The package declared `>=3.10`, but
  `from datetime import UTC` (3.11+) has been imported by five shipped modules —
  `obs.py`, `audit_log.py`, `cli_plan.py`, `spec_commands.py`, `change_commands.py` —
  for some time, so `import spec_runner` raised `ImportError` on 3.10 while pip
  happily installed it. The supporting config already assumed 3.11: ruff is set to
  `target-version = "py311"` and CI tests 3.11/3.12/3.13 with no 3.10 job. Metadata,
  the `Programming Language :: Python :: 3.10` classifier and the docs now match the
  code. No runtime behaviour changes; 3.10 was never actually functional.

- **Governance gate could be bypassed under a custom stage profile.**
  `spec_run_gate_ok` read `tasks.md` via `read_spec_meta`'s default `lite`
  stage tuple, so a managed spec whose stage is not part of `lite` resolved
  to `None` (= unmanaged) and passed `run --strict` / `watch --strict` even
  while in `draft`. Live in every released version since stage profiles
  shipped in v2.9.0. The run gate now resolves stages from the configured
  profile.
- **`spec approve` / `reject` / `check` were profile-blind in the same way**
  and wrongly reported a managed custom-profile stage as unmanaged (exit 2),
  refusing to act on it. All four `read_spec_meta` calls in
  `spec_commands.py` now resolve stages from the configured profile.
- **`plan --gated` crashed on any non-`lite` profile.** `_MARKER` and
  `_UPSTREAM` in `cli_plan.py` were module-level dicts hardcoded to the three
  `lite` stages; `_generate_stage_draft` indexed them directly and raised
  `KeyError` for any other profile. Both are gone: markers now come from
  `StageDef.marker_prefix` via a new internal `_parse_stage_marker` (the
  exported `parse_spec_marker` is unchanged, since it prepends `SPEC_` to a
  bare name while `marker_prefix` is already the full prefix).
- **`validate` could not resolve a custom stage's file path.** `validate.py`
  had its own hardcoded three-key stage→file map and raised
  `ValueError: unknown stage: <name>` for anything else; it now resolves the
  path via the shared `spec.stage_path` convention.
- **Custom-profile stages were rejected by the VS Code frontmatter schema.**
  `spec_stage` carried an `enum` of the three `lite` stage names, so a spec
  on any other stage profile failed the contract — a latent bug since stage
  profiles shipped in v2.9.0. Stage membership is a runtime check against
  the configured profile and cannot be expressed in JSON Schema without it,
  so the schema now only requires a non-empty string.

## [2.10.0] — 2026-07-14

OpenSpec-inspired spec lifecycle: this release delivers five milestones drawn
from a study of OpenSpec (2026-07-13) — per-stage prompt context/rules, a
structured requirements parser, DAG stage profiles, change-as-folder, and
delta-spec merge on archive. See `docs/plans/2026-07-13-openspec-inspired-roadmap.md`.
The Maestro interop contract (state-db schema, `--json-result`) is unchanged.

### Added

- **Delta specs + archive merge** (M3, the culmination of the OpenSpec-inspired
  roadmap). A change may carry `spec/changes/<id>/specs/requirements.md` — a
  delta with `## ADDED / MODIFIED / REMOVED / RENAMED Requirements` sections of
  id-keyed blocks (see `spec/FORMAT.md`). `change archive` now validates the
  delta, prints the merge plan, merges it into the flat `spec/requirements.md`
  (all-or-nothing: ADDED appends a new id, MODIFIED replaces the whole block,
  REMOVED deletes it — `**Reason**`/`**Migration**` mandatory, RENAMED rewrites
  the heading name only), then moves the folder to the archive. Conflicts
  (unknown id, duplicate ADDED, several ops on one id, FROM-name mismatch)
  abort the archive and name the offending requirement; `--force` never
  overrides merge safety. New `change archive --dry-run` prints the plan
  without changing anything; `validate --change <id>` reports delta conflicts
  early (fail fast). Re-archiving the same delta conflicts instead of
  double-applying. A missing target is bootstrapped by the first archived
  delta's ADDED blocks. New public API: `parse_delta` (`requirements.py`) and
  `plan_merge`/`apply_merge`/`MergeConflictError` (`spec_merge.py`, new).
  Non-contract, additive change.

- **Change-as-folder lifecycle** (M2 of the OpenSpec-inspired roadmap; design:
  `docs/plans/2026-07-13-m2-change-folder-design.md`). A change is a
  self-rooted spec dir at `spec/changes/<id>/` selected with `--change <id>`:
  every spec path (`tasks.md`, gated pipeline files, locks, state-db, logs)
  scopes to the change through the config path seam, so `run`, `plan --gated`,
  governance, `verify` and reports all work inside a change unchanged. The
  per-change state-db yields a per-change executor lock, so parallel
  `run --change A` and `run --change B` don't contend. New `change` command
  family: `change new <id>` (scaffold with a tasks.md stub), `change list`
  (`--json`), `change archive <id>` (moves to
  `spec/changes/archive/YYYY-MM-DD-<id>/`; refuses while a run is live or
  tasks are unfinished — `--force` overrides the task gate only). Archive here
  only moves the folder; delta-spec merge is M3. `--change` and
  `--spec-prefix` are mutually exclusive. **No contract change**: the state-db
  schema and `--json-result` stdout are untouched (per the M2 design decision,
  the db *location* is configuration — same precedent as `--spec-prefix`);
  the flat `spec/` layout is byte-identical when `--change` is not used.

- **DAG stage profiles** (M4 of the OpenSpec-inspired roadmap). Spec-generation
  profiles are now a true dependency graph rather than a flat ordered list.
  `StageDef.requires` (alias of `upstream`, and the new canonical `requires:`
  key in profile YAML) defines edges; `downstream_stages` follows them
  transitively so a *sibling* stage (sharing an upstream) is no longer
  wrongly stale-cascaded when another stage is approved. New
  `stage_readiness()` reports per-stage `ready`/`blocked`/`done`/`draft`/`stale`
  + `missing_deps`, exposing parallelism (several stages `ready` at once).
  `resolve_next_stage` is dependency-gated, `load_profile` rejects unknown
  `requires` refs and cycles, and `stage_path` / `spec status` / the gated
  planner resolve stages from the configured profile (custom stage names work).
  The built-in linear `lite` profile is byte-for-byte unchanged (proven by an
  exhaustive graph-vs-linear equivalence test). No contract surface touched.

- **Structured requirements parser** (M1 of the OpenSpec-inspired roadmap).
  New `requirements.py` parses `requirements.md` into id-keyed `Requirement`
  blocks — a diffable/mergeable unit that lays the groundwork for delta specs.
  Tolerant of heterogeneous bodies (gherkin, `- [ ]` checklists, prose): it
  anchors only on the `#+ (REQ|NFR)-NNN` heading and block boundaries (next
  same-or-higher-level heading), preserving each block's exact text for
  round-trip. Public API: `parse_requirements`, `serialize_requirement`,
  `find_requirement`, `Requirement`. `spec-runner validate` now also warns per
  functional requirement that has no acceptance-criteria section (NFRs exempt).
  `spec/FORMAT.md` documents the grammar. Non-contract, additive change.

- **Per-stage rules & project context injection for spec generation** (M0 of
  the OpenSpec-inspired roadmap, `docs/plans/2026-07-13-openspec-inspired-roadmap.md`).
  Two new opt-in config keys: `spec_context` (project-wide text prepended to
  every generation stage inside a `<context>` block) and `spec_rules` (per-stage
  rules keyed by stage name, injected only for the matching stage inside a
  `<rules>` block). Both flow through `plan --full` and `plan --gated`.
  `validate` flags an oversized `spec_context` (>50KB) as an error and unknown
  `spec_rules` stage keys as a warning. Non-contract change; default (no config)
  produces byte-identical prompts to 2.9.0.

## [2.9.0] — 2026-07-07

### Added

- **Loadable stage profiles for gated spec generation** — the previously
  hardcoded stage chain `requirements → design → tasks` is now data. A
  `StageProfile` (ordered `StageDef`s carrying name, template, marker prefix,
  validator key, and upstream stages) is loaded from a bundled YAML profile;
  the built-in `lite` profile (`src/spec_runner/profiles/lite.yaml`) reproduces
  the old chain 1:1. `spec.py` (stage ordering / next-stage resolution / stale
  cascade / `spec_stage` validation), `prompt.py` (templates + markers), and
  `validate.py` (per-stage validator dispatch) all read from the profile
  instead of scattered module-level maps.
- **Profile selection** — `spec_profile` config key (default `lite`) and a
  `--profile` flag on `plan --gated` and the `spec` command family. An unknown
  profile raises a clear `ConfigError` listing the available profiles instead
  of a traceback.

  This is additive and behaviour-preserving: the default (`lite`) pipeline is
  identical to 2.8.x, the `SPEC_STAGES` export is unchanged, and existing specs
  with `spec_stage` in `{requirements, design, tasks}` need no migration. Full
  suite stays green with no test edits (976 passed). Unblocks richer
  governance-layer profiles downstream.

## [2.8.1] — 2026-07-05

### Fixed

- **`costs --json` on a project without `tasks.md` emits valid empty JSON** —
  previously `parse_tasks()` hard-exited when `tasks.md` was missing, and with
  zero tasks the command printed the prose fallback `No tasks found`, breaking
  machine consumers (the `spec-runner-vscode` extension polls `costs --json`
  on fresh gated specs that have no tasks stage yet). Now emits a
  schema-conformant `{"tasks": [], "summary": {…}}` payload.
- **Pre-init log lines no longer leak to stdout** — structlog's built-in
  default prints to stdout until `init_logging()` runs, so logs emitted during
  `build_config()` (e.g. the subdir-project warning) corrupted machine output
  (`status --json` failed `JSON.parse` in the VSCode extension). `obs.py` now
  installs a pre-init default that routes logging to the *current*
  `sys.stderr`, keeping stdout reserved for `--json` / `--json-result`.

## [2.8.0] — 2026-07-02

### Added

- **VSCode extension read-surface contracts** — pinned schemas for the surfaces
  the `spec-runner-vscode` extension reads: `schemas/status.schema.json`
  (`status --json` flat aggregate), `schemas/costs.schema.json` (`costs --json`
  per-task list; pins the *mixed* status enum — DB
  `pending/running/success/failed/skipped` + tasks.md
  `todo/in_progress/done/blocked`), and `schemas/spec-frontmatter.schema.json`
  (`SpecMeta` governance frontmatter). Golden fixtures + contract tests
  (`tests/test_vscode_contract.py`) validate both sample fixtures and *live*
  command output, with a drift guard on the union status enum. Adds
  `spec-runner --version` for the extension's activation compatibility check.
  Additive only — no change to existing output shapes.
- **Gated spec generation** (`plan --gated`, `spec status/approve/reject/adopt/check`):
  an opt-in workflow that stamps `requirements.md`/`design.md`/`tasks.md` with
  frontmatter (`spec_stage`, `status: draft|approved|stale`, `version`,
  `generated_by`, `validation`, `approved_by`/`approved_at`) tracked through
  atomic, file-locked writes. `plan --gated [--stage S]` generates one stage at
  a time from rich single-source templates (content-hashed as
  `source_prompt_version`), enforces that upstream stages are already
  `approved`, writes the result as `draft`, validates it, and stops.
  `spec approve <stage>` always re-validates the body before approving (never
  trusts the cached `validation` field) and cascades `stale` to downstream
  stages on any version bump; `spec adopt` validates first and refuses to
  silently stamp an invalid file as `approved` (unless `--force`); `spec
  reject` reopens a stage as `draft`. `run`/`watch` gain a hard gate: with
  `spec_governance: strict` (default `off`) in config, or `--strict`/
  `--no-strict` on the CLI, an unapproved *managed* `tasks.md` blocks
  execution; unmanaged (frontmatter-less) and Maestro-produced specs run
  unchanged for backward compatibility. `task.py` parsing strips leading
  frontmatter transparently and write-back preserves it.

## [2.7.0] — 2026-06-14

### Added

- **`--model` now applies to the `qwen` and `copilot` presets.** Previously these
  template-driven presets ignored `--model` (model was set in the CLI's own
  settings/env). Now `spec-runner config --preset qwen --model qwen-coder-plus`
  (or `copilot --model claude-haiku-4.5`) appends `--model <id>` to the generated
  `command_template`. A blank model still omits the flag (no dangling `--model`).

## [2.6.0] — 2026-06-13

### Added

- **`config` presets for `qwen` and `copilot`.** `spec-runner config --preset
  qwen` (Qwen Code CLI) and `--preset copilot` (GitHub Copilot CLI) now write the
  correct headless `command_template` / `review_command_template` (these CLIs are
  not auto-detected). qwen uses `--approval-mode yolo` for exec and `plan` for the
  read-only review; copilot uses `-s --no-ask-user --allow-all-tools` for exec and
  `--allow-tool='shell'` for review. The model is configured in each CLI's own
  settings/env (see the printed note); `--model` does not apply to these two.
  Preset list is now: claude, codex, opencode, pi, ollama, llama-cli, qwen, copilot.

## [2.5.0] — 2026-06-13

### Added

- **`spec-runner config`** — apply a CLI profile preset to
  `spec-runner.config.yaml`. `--preset X` sets both the exec and review CLI
  (mono); `--exec X --review Y` mixes them (multi). Presets: claude, codex,
  opencode, pi, ollama, llama-cli. `--model` / `--review-model` override the
  model; `--list-presets` lists them; `--dry-run` previews; `--apply` updates an
  existing config (surgical merge of the 7 CLI-profile keys, other settings
  preserved, backed up to `.bak`). Note: on `--apply`, PyYAML normalises
  comments and key ordering.

### Fixed

- **`validate` now checks flat v2.0 configs**, not only `executor:`-wrapped
  ones, so unknown top-level keys in `spec-runner.config.yaml` are caught.

## [2.4.1] — 2026-06-12

### Fixed

- **`doctor` mislabelled auth/API errors as "command not in PATH".** The
  invocation check matched a bare "not found" substring, so an error whose text
  contained it (e.g. a CLI returning Google's "API Key not found") was reported
  as a missing executable. It now matches the actual `No such file or directory`
  FileNotFoundError, so auth/network failures surface with their real cause.

- **Crash/interruption recovery — orphaned `in_progress` tasks.** When the run
  holds the exclusive executor lock, any task still marked `running` is orphaned
  from a dead run, so recovery now resets **all** such tasks regardless of age
  (previously only those running longer than 2× the task timeout, ~60 min).
  Otherwise an interrupted session (e.g. a dropped remote shell) left a half-done
  task that the next run re-picked first (`in_progress` goes first) and hung
  re-doing it. **TUI runs now also take the lock** (one executor per project);
  `--force` runs hold no lock and keep the conservative age-based heuristic.
- **Review diff against a parent repo.** Code review skips `git diff HEAD~1` when
  git automation is off (a subdir of a larger repo, or `--no-branch --no-commit`).
  There the diff was taken against the **parent** repository — a huge, unrelated
  diff that made the reviewer slow or hang. (The review subprocess timeout
  `review_timeout_minutes` was already enforced.)

## [2.4.0] — 2026-06-12

### Added

- **`spec-runner doctor`** — empirical CLI/model compatibility probe. Runs a
  real one-task run through `execute_task()` against the configured (or
  `--cli`/`--model`) backend and reports per-capability status (invocation,
  completion marker, task action, cost tracking, error classification, optional
  `--with-review`) with a READY/DEGRADED/BROKEN verdict. `--json` output is
  pinned by `schemas/doctor-result.schema.json`. Budget-capped (default $0.50)
  with a confirmation gate (`--yes` to skip); `--strict` fails CI on DEGRADED.
- **`sync_deps` config flag** (under `hooks.pre_start`) — gates the `uv sync`
  step in `pre_start_hook` (doctor disables it for the scratch workspace).
- **`spec-runner plan --from-file PATH`** — read the feature description from a
  file instead of the positional argument (the positional is now optional). Handy
  for long descriptions; `--from-file` takes priority and errors on a
  missing/empty file or when neither source is given.

### Fixed

- **Cost tracking for the claude CLI.** `execute_task` now invokes claude with
  `--output-format json` and parses `total_cost_usd` / `usage` from the result —
  the old stderr regex (`parse_token_usage`) no longer matches modern claude
  (2.x), so cost/tokens were silently `None` and `costs` / `--budget` /
  `--task-budget` did nothing for claude. Implemented behind a per-CLI result
  seam: `build_cli_invocation() -> CliInvocation{argv, result_format}` and
  `parse_cli_result(result_format, …) -> CliResult`. JSON mode is gated to an
  **explicit** `claude` / `claude-code` binary (no template), so other CLIs,
  templated claude, and custom wrappers are unaffected (`build_cli_command` stays
  a thin argv wrapper, so the review path and other callers are unchanged). A
  claude `is_error` JSON payload now forces a task failure. Verified end to end:
  `spec-runner doctor --cli=claude` → READY with a real measured cost. Claude's
  native `--max-budget-usd` cap is supported by the builder but intentionally not
  wired into runs yet (it would hard-fail on slight overage) — deferred. Review-
  stage cost is still not tracked (follow-up).
- **Task `DONE` status is now committed to git.** The `tasks.md` done-status +
  checklist update happened in `execution.py` *after* `post_done_hook`'s
  commit/merge, so it landed in the working tree post-merge, was never committed,
  and got clobbered by the next task's branch — leaving completed tasks stuck at
  `IN_PROGRESS` and desyncing `get_next_tasks` from the executor DB. The update
  now runs inside `post_done_hook` before the auto-commit.

## [2.3.1] — 2026-06-10

### Added

- **Pi-driven dev→review→test loop.** Bundled `pi/` skill templates
  (`pi-implementer`, `pi-reviewer`, `pi-tester`) plus `spec-runner.pi.config.yaml`
  with per-stage command templates — full tools for develop, a read-only review
  gate — letting `pi` run the entire cycle with no core code. Documented in
  `docs/pi-workflow.md` with a runnable `examples/pi-loop/` example.

### Changed

- **`review.pi.md` is now a strict read-only gate.** The reviewer inspects and
  reports findings (no self-fixes); the implementer fixes on retry. Dropped the
  `REVIEW_FIXED` outcome from the pi review prompt.
- Dependency bumps via Dependabot: `urllib3` 2.6.3→2.7.0,
  `python-multipart` 0.0.26→0.0.27, `python-dotenv` 1.2.1→1.2.2,
  `pyjwt` 2.11.0→2.12.0.
- CI: minimal `GITHUB_TOKEN` permissions.

## [2.3.0] — 2026-05-30

### Added

- **Version in `status` header.** First line of `spec-runner status` now
  reads `📊 spec-runner v<version>`.
- **Human-readable error reasons.** Failed-task lines in `status` now show
  `[error_kind] message` instead of "Unknown error", with the failing
  sub-stage tagged as `[at: <stage>]`. Driven by a small pattern library in
  `src/spec_runner/errors.py` (codex usage-limit, generic rate-limit, auth,
  network, generic CLI error) with a last-5-lines-of-stderr fallback.
- **Run stop-reason summary.** When a run halts abnormally (e.g.,
  `max_consecutive_failures`, codex rate limit), `status` prints a
  `⚠️ Last run stopped: …` line above the totals.
- **Repeated-failure log hint (`💡`).** When a task that was already failed
  before the current run fails again, spec-runner emits a `💡` warning to
  stderr immediately and shows a persistent hint under the task in `status`
  with the path to its log file.
- **Per-stage progress mirror.** Extends 2.2.2's stderr progress with one
  `⏳ stage: <name>` line per sub-stage (`sync_deps`, `branch`, `codex`,
  `parse`, `tests`, `lint`, `commit`, `merge`, `review`). Stages are emitted
  only when the corresponding step actually runs.

### Changed

- **`run --all` now resets failed→pending and consecutive_failures→0 by
  default.** Use the new `--no-reset-failed` flag to preserve the old sticky-
  failed behavior. Single-task runs (`run TASK-X`) and `retry` are unaffected.
- **Subdir-project safety: git automation defaults OFF when `project_root`
  is a strict subdirectory of a larger git repo.** Prior behavior could
  commit unrelated files across the whole repo and merge them to `main`.
  Explicit `create_git_branch=true` / `auto_commit=true` in YAML or via CLI
  are respected; a warning log is emitted when the auto-default triggers.

### Fixed

- **codex CLI adapter.** `build_cli_command` now builds `codex exec [-m
  MODEL] <PROMPT>` instead of `codex -p <PROMPT>`. `-p` in the codex CLI is
  `--profile`, not the prompt, so the previous form crashed every codex run
  with an `invalid --profile value` error that spec-runner surfaced as the
  generic "Unknown error". Existing `command_template` overrides are
  preserved (template path is checked before auto-detect).

### Schema

- `attempts` gains two TEXT columns: `error_kind`, `error_stage`. Idempotent
  on-startup migration; legacy rows with NULL values render in the old
  format. Three new keys appear in `executor_meta`: `last_run_stop_reason`,
  `last_run_stop_detail`, `second_pass_fail_tasks`. Forward-compatible:
  downgrading to 2.2.2 simply ignores the extras.

## [2.2.2] — 2026-05-29

### Added

- **Console progress for non-TUI runs.** Plain `spec-runner run` / `watch`
  were silent because `obs` routed all structlog output to the per-PID JSONL
  file only. A compact, human-readable progress line is now mirrored to
  **stderr** (opt-in `obs.init_logging(..., console=True)`, wired through
  `setup_logging`'s existing `tui_mode` flag — on for normal runs, off in
  TUI mode so the dashboard isn't corrupted). Trace/transport fields
  (`pipeline_id`, span/trace ids) are stripped from the console line and
  secrets are redacted upstream. The JSON file sink is byte-identical, so the
  vendored OTel observability contract is unchanged.

### Fixed

- **Task estimate parsing for decimals and en-dash ranges.** The `ESTIMATE`
  regex only accepted integer day/hour values with ASCII-hyphen ranges, so
  estimates like `1.5d` or `1–1.5d` (en-dash, U+2013) were silently dropped
  and surfaced as spurious "missing estimate" validation warnings. The pattern
  now accepts decimals and en-dash ranges (backward-compatible superset).

## [2.2.1] — 2026-05-28

### Changed

- **CI: bump GitHub Actions off the deprecated Node 20 runtime** (forced to
  Node 24 on 2026-06-02): `actions/checkout` v4→v6, `actions/setup-python`
  v5→v6, `astral-sh/setup-uv` v4→v8.1.0 (pinned exactly — setup-uv has no
  floating `v8` major tag). All three now run on `node24`.

### Fixed

- `tests/test_obs_contract.py` no longer crashes pytest collection in
  standalone CI checkouts: it read the shared `log-schema.json` from the
  external cowork workspace at module load. Guarded with a module-level
  `pytest.skip` when the contract file is absent; full coverage still runs
  locally where the workspace is present.

## [2.2.0] — 2026-05-28

### Added

- **CLI auto-detection for OpenCode and Pi Agent.** `runner.build_cli_command()`
  now recognizes two more coding agents alongside Claude / Codex / Ollama /
  llama-cli:
  - **[OpenCode](https://opencode.ai)** (sst/opencode) — `opencode run [--model provider/id] <prompt>`
  - **[Pi Agent](https://pi.dev)** (earendil-works/pi) — `pi -p [--model X] <prompt>` (non-interactive mode)
  Pi uses basename matching (not substring) to avoid false positives on
  command names containing the literal "pi" (e.g. `pipe-cli`). Bundled review
  prompts added under `skills/spec-generator-skill/templates/prompts/`.
  Either CLI can be wired to either role (executor / reviewer / persona) via
  `claude_command` / `review_command` / `personas` in the config — same as
  any other supported CLI.

### Docs

- Architecture diagrams (4 Mermaid views: system context, module map,
  task-execution sequence, storage) under `docs/architecture.{md,html}`.

### Fixed

- Green CI: resolved `ruff format --check` drift and all `mypy` errors
  (red since v2.1.0). No behavior change — Optional narrowing, type casts,
  and supertype-compatible TUI signatures.

### Notes

- No changes to the Maestro interop contract (`.executor-state.db`,
  `--json-result`) — additive feature + docs + type fixes only.

## [2.1.0] — 2026-05-23

### Added — observability module (`spec_runner.obs`)

New canonical observability emitter shared across the ecosystem. Reference
implementation of the cross-project contract at
`_cowork_output/observability-contract/log-schema.json` (OpenTelemetry Logs
Data Model JSONL, one file per PID).

Public API:

- `obs.init_logging(project, level=..., log_dir=...)` — canonical entrypoint
- `obs.get_logger(module=...)` — bound structlog logger
- `obs.span(event, **attrs)` — context manager for spans with error chains
- `obs.child_env()` — emits `TRACEPARENT` env vars for subprocess trace propagation
- `obs.current_trace_id()` / `current_span_id()` / `current_pipeline_id()` — accessors

Features:

- `TRACEPARENT` ingress: parses W3C trace context, uses parent span_id as initial
  `_span_id`; malformed values fall back to root span (warned, not fatal)
- Redaction processor with default blocklist (`api_key`, `token`, `password`,
  `secret`, `authorization`, `cookie`, `private_key`, …) extensible via env
- Timestamps emitted as both ns-string and ISO micros (UTC)
- Contract validation against shared schema/fixtures (`tests/test_obs_contract.py`)

### Changed

- `spec_runner.logging` reduced to a 45-line back-compat shim that delegates
  to `obs.init_logging` / `obs.get_logger`. Existing imports of
  `setup_logging`, `get_logger`, `redact_sensitive` continue to work unchanged.

### Notes

- No changes to the Maestro interop contract (`.executor-state.db`,
  `--json-result`) — observability is additive and does not affect R-04.
- Minor bump (additive feature, fully back-compatible). Already vendored
  into Maestro (M1+M2), arbiter (Rust `arbiter-core::obs`), and ATP.

### Also

- Dependabot: patched 5 alerts (urllib3 2.6.3→2.7.0, python-multipart
  0.0.26→0.0.29, idna 3.11→3.16, python-dotenv 1.2.1→1.2.2). Transitive
  bumps only — no direct dependency changes.
- `.gitignore`: ignore `COWORK_CONTEXT.md`, `_cowork_output/`, and obs
  runtime output under `logs/`.

## [2.0.0] — 2026-04-17

Baseline release. See `TODO.md` and `docs/state-schema.md` for the frozen
R-04 Maestro interop contract (SQLite state schema, `--json-result` stdout,
golden fixtures under `tests/fixtures/maestro-interop/`).

[Unreleased]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.31.0...HEAD
[2.31.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.30.0...v2.31.0
[2.30.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.29.0...v2.30.0
[2.29.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.28.3...v2.29.0
[2.28.3]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.28.2...v2.28.3
[2.28.2]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.28.1...v2.28.2
[2.28.1]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.28.0...v2.28.1
[2.28.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.27.1...v2.28.0
[2.27.1]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.27.0...v2.27.1
[2.27.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.26.0...v2.27.0
[2.26.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.25.0...v2.26.0
[2.25.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.24.0...v2.25.0
[2.24.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.23.0...v2.24.0
[2.23.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.22.0...v2.23.0
[2.22.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.21.0...v2.22.0
[2.21.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.20.0...v2.21.0
[2.20.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.19.0...v2.20.0
[2.19.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.18.0...v2.19.0
[2.18.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.17.0...v2.18.0
[2.17.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.16.0...v2.17.0
[2.16.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.15.0...v2.16.0
[2.15.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.14.0...v2.15.0
[2.14.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.13.0...v2.14.0
[2.13.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.12.0...v2.13.0
[2.12.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.11.1...v2.12.0
[2.11.1]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.11.0...v2.11.1
[2.11.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.10.0...v2.11.0
[2.10.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.9.0...v2.10.0
[2.9.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.8.1...v2.9.0
[2.8.1]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.8.0...v2.8.1
[2.8.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.7.0...v2.8.0
[2.7.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.6.0...v2.7.0
[2.6.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.5.0...v2.6.0
[2.5.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.4.1...v2.5.0
[2.4.1]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.4.0...v2.4.1
[2.4.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.3.1...v2.4.0
[2.3.1]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.3.0...v2.3.1
[2.3.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.2.2...v2.3.0
[2.2.2]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.2.1...v2.2.2
[2.2.1]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.2.0...v2.2.1
[2.2.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.1.0...v2.2.0
[2.1.0]: https://github.com/andrei-shtanakov/spec-runner/compare/v2.0.0...v2.1.0
