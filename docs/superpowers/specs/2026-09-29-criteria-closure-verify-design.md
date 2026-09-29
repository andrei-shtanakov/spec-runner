# `verify --criteria` — the producer side of `criteria-closure/v1`

Status: draft for the owner, 2026-09-29. Inbox spec-runner#603 (from devtools,
DarkFactory E), TODO `criteria-closure-verify`.

## 0. What is asked, and against which text

devtools closes a workstream by asking, per test-criterion of an approved bundle,
"is it traced to a green test that executes the product?". The measurement is
ours; devtools vendors our schemas and re-checks the answer.

**Norm used here:** devtools `docs/superpowers/specs/2026-09-28-bundle-criteria-oracle-design.md`
at **`b7edcca`** (merged, PR devtools#482, rev 10) — not `5727151`, the commit the
issue cites. Between the two, the contract sections this document answers
(§1.3–1.4, §2.2, §4, §5.1–5.3) did not change; §1.2 (code registry), §2.1
(availability), §3.3 (Must-AC) and §5.4 (vendoring: `PIN` + `manifest.json`)
did. The annex (`…-spec-runner-annex.md`) is a proposed mechanism, not norm; §4
below says where this design follows it and where it does not.

Also merged there: `contracts/criteria-closure/v1/min-spec-runner.env`
(`MIN_SPEC_RUNNER_VERSION=0.0.0`, to be set by the vendoring PR). The oracle is
available to devtools only once `PIN`, our schemas and a converging manifest sit
next to it.

## 1. Four deliverables, three slices

| # | deliverable (issue) | norm | slice |
|---|---|---|---|
| 1 | qualified token `CODE:BEH-NN`, owned by the innermost test definition | §1.3–1.4 | A |
| 2 | task gate: a declared BEH is carried by at least one test of the group at the task's commit | §2.2 | A |
| 3 | `spec-runner verify --criteria --request <file.json> --json` | §4.1, §4.3, §5.1 | B |
| 4 | request/response schemas + the minimum version | §5.1, §5.4 | B, released in C |

Slice C is the release that sets the minimum version devtools pins.

## 2. Slice A — the token, and not breaking the bridge first

### 2.1 A defect that comes before the feature

`task._parse_scenarios` accepts `[A-Z]+-\d+[a-z]?` only and calls qualified ids
"deliberately not accepted". The norm (§2.1) has devtools' bridge emit
`**Scenarios:** ENC:BEH-03, ENC:BEH-04` once the oracle is available — every
such task would then fail `validate`. So slice A starts by accepting
`CODE:ID` with `CODE` = `^[A-Z]{2,6}$` (§1.1) and `ID` the existing shape. A line
may mix neither: all qualified or all bare, else a named `validate` error — a
mixed line has no single matching rule.

### 2.2 Two matching rules, chosen by the id's shape

- **Bare `BEH-09`** — today's #402 rule, unchanged: whole token anywhere in a
  group **file**. Existing specs keep their behaviour exactly.
- **Qualified `ENC:BEH-09`** — §1.3 boundaries
  `(?<![A-Za-z0-9_])ENC:BEH-09(?![A-Za-z0-9_])` and §1.4 **ownership**: the token
  counts only inside the innermost test definition whose *region* holds it —
  a function region runs from its first decorator to `end_lineno` minus nested
  `def`/`class` ranges; a class region (decorators, header, docstring, body
  outside nested definitions) counts for every test method of the class; the
  module header and neighbouring definitions count for nothing. Regions are
  computed on source lines because comments are invisible to `ast`.

One module owns the ownership rule: `criteria_tokens.py`
(`owned_tokens(source) -> {qualname: set[token]}`). The gate and the measurement
both call it, so "which tests carry `ENC:BEH-09`" has one answer — the lesson of
#270/#241 in this repo. devtools re-derives the same answer with its own parser
(§5.3 completeness); a disagreement is theirs to refuse, and our golden fixtures
(§6) are what both sides should pass.

A token in a non-Python test file does not count; a task whose test adapter is
not pytest is outside this rule (`not-applicable: language` at closure, §3.6).

### 2.3 The gate (§2.2)

At the entry run, before any paid call, where #402's coverage check sits today
(`execution._run_verify_first_phase`), for a qualified line: every declared id
must be owned by at least one test definition in the group's files **at the
task's commit** (`git show <sha>:<path>`, never the tree). Refusal shapes stay
#402's: terminal POLICY when uncovered, INSTRUMENT when a file cannot be read.
`validate` keeps warning on the working tree.

**Open (§8 q1):** the norm says "the group" and only `verify_first` tasks have
one. For `tdd`/`standard` tasks the bridge may emit the line too; this design
keeps today's `validate` warning ("not checked outside verify_first") and leaves
the gate to `verify_first` — the closure measures every mode anyway.

## 3. Slice B — `verify --criteria`

### 3.1 Command and exit codes (§5.1)

`spec-runner verify --criteria --request <file.json> --json` — stdout carries
exactly one JSON document on every exit path (the `review-pr --json` rule).

| exit | meaning |
|---|---|
| 0 | answer given (per-BEH statuses may be `unconfirmed`/`error`) |
| 2 | request or environment error — retryable (unreadable request, schema-invalid request, clone/sync failed) |
| 3 | response-level error — `blocked` (`error` at the top, no `beh`) |

Under `executor_sandbox` nothing changes: no agent is called.

### 3.2 The measurement

1. **Validate the request** against our request schema; echo it verbatim.
2. **Fresh clone at `product_sha`** into a temp dir: `git clone --no-hardlinks
   <project_root> <tmp>` + `git checkout --detach <product_sha>` — the clone, not
   a worktree, so the tree has no shared state with the operator's checkout.
   Unknown SHA → exit 2.
3. **Environment (G1):** `uv sync --frozen` in the clone (shared uv cache — the
   annex's choice, kept); every `PYTEST_*` variable removed from the child env;
   the project's own pytest config is product. Reported: sha256 of `uv.lock`,
   Python version, the pytest plugins actually loaded (from the plugin manager).
4. **Product roots:** the annex maps wheel files back to sources by sha256 via
   `RECORD`. Adopted — it is the one definition that does not guess a layout.
   `uv build --wheel` in the clone; a wheel file with no source twin →
   `error: product-roots-unknown` (exit 3). No build backend → same error (§8 q2).
5. **Collection:** one `pytest --collect-only` of the whole suite — the
   authoritative set; each item → (file, qualname) via our reporter plugin
   (`item.location`, `item.function.__qualname__`); a BEH's selectors = every
   collected item whose definition owns its token (§2.2). None →
   `unconfirmed: no-test`.
6. **Two runs (G3):** the union of BEH selectors, run twice, each run a separate
   pytest process, with the reporter plugin loaded by `-p` (as verify-first does
   today — never via the project's conftest). Per item and run: the `call`-phase
   outcome.
7. **In-process execution (G0):** the plugin records, during each item's `call`
   phase only, the product-root lines executed — via `sys.monitoring` (3.12+) or
   `sys.settrace` (3.11). **Not coverage.py** (the annex's M2): the product's
   locked environment need not contain it, and adding it changes the environment
   G1 pins. Lines outside function/method bodies (module level, class bodies —
   run at import, lazy imports included) are discarded by the same AST pass
   devtools uses. A child process started during `call` (`subprocess.Popen`,
   `os.posix_spawn`, `os.exec*` patched by the plugin for that phase) is flagged.
8. **Status per BEH (§4.2):** all selectors `passed` in both runs and each with
   > 0 product body lines in process → `traced`; zero lines but a child process →
   `unconfirmed: subprocess-only`; zero lines → `no-product-execution`; a
   non-`passed` → `not-passed`; runs disagree → `nondeterministic`; a read/run
   failure for that BEH → `error: io` / `runner`.
9. **`content_sha256`:** sha256 over the sorted `(path, sha256(bytes))` of every
   product-root file and every test file collected — the G6 key devtools uses.
   The exact definition is in the response schema's description, since devtools
   recomputes it.

### 3.3 Response fields (§4.3)

Top level: `protocol: 1`, `request` (echo), `product_roots` (paths),
`environment` (`lock_sha256`, `python`, `pytest_plugins`), `content_sha256`,
then exactly one of `beh` / `not_applicable` (`language`) / `error`. Per BEH:
`id`, `status`, `reason` (required iff not `traced`), `selectors[]`: `node_id`,
`definition` (`file`, `qualname`), `runs` (two `call` outcomes),
`product_lines_in_process`, `child_process` (bool). Numbers devtools re-derives
(lines inside function bodies) are reported as the line list, not only a count,
so their AST check (§5.3) has something to check.

## 4. Where this follows the annex, and where not

| annex | here |
|---|---|
| M0 fresh clone, `uv sync --frozen`, `PYTEST_*` cleared, roots via wheel `RECORD` | followed |
| M1 `--collect-only` authoritative, selector → definition → tokens | followed; ownership in one shared module |
| M2 coverage.py with `--cov-context=test` | **not followed**: a `-p` plugin with `sys.monitoring`/`settrace`, so the locked environment is measured as locked |
| M3 two runs | followed |
| M4 `content_sha256` | followed; definition fixed in the schema |

## 5. Schemas and vendoring

`schemas/criteria-closure-request.v1.schema.json` and
`schemas/criteria-closure-response.v1.schema.json`, pinned by a contract test
with golden fixtures (the `--json-result` pattern, `tests/fixtures/`). devtools
vendors them with `PIN: spec-runner @ <sha>` and a manifest; their drift check
reads upstream by that ref. A breaking change is `v2` beside `v1`, never an
edit of `v1`.

## 6. Proof

- **Token/ownership table** — golden fixtures shared in spirit with devtools'
  §1.4 cases: decorator line, nested def, class docstring counted for methods,
  method token not lifted to the class, module header ignored, `XENC:`/`BEH-030`
  /`BEH-03a` not matching, parametrize counting for every collected id.
- **Negative doubles (§8.3 there):** a docstring-reading test → `no-product-execution`;
  the same with a lazy import inside the test → still `no-product-execution`
  (module-level lines discarded); a CLI-only test → `subprocess-only`; a flaky
  test (seeded) → `nondeterministic`; an executing green test → `traced`.
- **Contract:** every response validates; exit 0/2/3 each with one JSON document.
- **Live (their acceptance):** devtools' `criteria-close` against a released
  spec-runner on a schema-2 bundle.

## 7. Cost and risk

- Two full runs of the BEH selectors plus one collection per closure; tracing
  overhead — `sys.monitoring` is cheaper than coverage.py's tracer, not measured
  here yet.
- `sys.settrace` on 3.11 is slower; measure before promising.
- A project with no build backend has no product roots by this definition (§8 q2).

## 8. Open questions for the owner

1. Gate for `tdd`/`standard` tasks carrying a qualified `**Scenarios:**` line —
   keep it `verify_first`-only (proposal), or define a group for other modes?
2. No build backend → `error: product-roots-unknown` (proposal, fail-closed), or
   a declared fallback (`src/` layout)?
3. Slice order: A (accepting qualified ids — needed before devtools' bridge emits
   them), then B, then the release that devtools pins. Agreed?
