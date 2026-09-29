# `verify --criteria` — the producer side of `criteria-closure/v1`

Status: **revision 3 — contract agreed with devtools**, 2026-09-29. Inbox
spec-runner#603 (from devtools, DarkFactory E), TODO `criteria-closure-verify`.
Revision 1 (PR #609) was a draft with open questions; revision 2 (PR #610)
recorded the owner's decisions and closed its §8; revision 3 records devtools'
sign-off in devtools#491 (the §6 addendum is now agreed, with their three
freeze conditions and four requests accepted — §9). Slice A shipped in PR #612.

## 0. What is asked, and against which text

devtools closes a workstream by asking, per test-criterion of an approved bundle,
"is it traced to a green test that executes the product?". The measurement is
ours; devtools vendors our schemas and re-checks the answer.

**Norm:** devtools `docs/superpowers/specs/2026-09-28-bundle-criteria-oracle-design.md`
at **`b7edcca`** (merged, devtools#482, rev 10). The issue cites `5727151`
(rev 8); rev 9 (`7fe603b`, devtools#477) and rev 10 changed §1.1–1.2 (charter
`plan_item`, code registry), §2.1 (oracle availability), §3.3 (Must-AC), §5.4
(vendoring) and §7 (rollout). The sections this document answers — **§1.3–1.4,
§2.2, §4, §5.1–5.3** — are byte-identical across all three. The annex
(`…-spec-runner-annex.md`) is a proposed mechanism, not norm; §5 says where this
design follows it.

Already merged in devtools: `contracts/criteria-closure/v1/min-spec-runner.env`
(`MIN_SPEC_RUNNER_VERSION=0.0.0`). The oracle is available to devtools only once
`PIN`, our schemas and a converging manifest sit next to it.

**Out of scope:** the acceptance automaton and AC statuses (devtools §3, §5.2);
non-Python tests (`not-applicable: language`, decided by devtools); whether a
test is able to fail or means its BEH (norm §4.1 boundary).

## 1. Deliverables and slices

| # | deliverable | norm | slice |
|---|---|---|---|
| 1 | `**Scenarios:**` accepts qualified ids `CODE:BEH-NN` | §1.3, §2.1 | A |
| 2 | token ownership by AST for `.py` group files; the task gate tightened | §1.4, §2.2 | A |
| 3 | `spec-runner verify --criteria --request <file.json> --json` | §4.1–4.3, §5.1 | B |
| 4 | request/response schemas + the minimum version | §5.1, §5.4 | B |

One design, two plans, two PRs. **A ships first as a minor release** and is
useful on its own. **B is its own minor release; that release is X**, the
minimum version devtools pins. Shipping A alone does not satisfy devtools'
slice-1 dependency.

## 2. Slice A — qualified ids and token ownership

### 2.1 Parser first (a defect that precedes the feature)

`task.SCENARIO_ID` is `[A-Z]+-\d+[a-z]?`, so `_parse_scenarios` refuses
`ENC:BEH-03`. Norm §2.1 has devtools' bridge emit
`**Scenarios:** ENC:BEH-03, ENC:BEH-04` once the oracle is available; every such
task would fail `validate`. A therefore starts by accepting `CODE:ID`, `CODE` =
`^[A-Z]{2,6}$` (norm §1.1), `ID` the existing shape.

**No mixing within one line.** A single `**Scenarios:**` line is either all
qualified or all bare; a mixed line is a named parse error on that task. This is
an additional validation constraint, documented beside the gate tightening in
`FORMAT.md` and the CHANGELOG — it is not implied by the norm.

### 2.2 Token matching and ownership

The token pattern is §1.3's for both forms:
`(?<![A-Za-z0-9_])<id>(?![A-Za-z0-9_])` — `XENC:BEH-03`, `ENC:BEH-030`,
`ENC:BEH-03a` do not match `ENC:BEH-03`.

**Ownership (§1.4), in one module:** `criteria_tokens.py`. Pure AST over a
module's source text; regions are computed on source lines because comments are
invisible to `ast`:

- a function/method region runs from its first decorator
  (`decorator_list[0].lineno`, else `lineno`) to `end_lineno`, **minus** the
  ranges of nested `def`/`class` — a nested helper's token does not count for the
  enclosing test;
- a class region is its decorators, header, docstring and body outside nested
  definitions; a token there counts for every test method of the class; a
  method's token is not lifted to the class and does not reach its siblings;
- the module header and neighbouring definitions own nothing.

**Agreed with devtools (devtools#491 D), rev 3.** Both parsers follow these
rules, pinned by the shared ownership fixtures (§6.3): lines split on
`\r\n|\r|\n` as `ast` numbers them; a leading U+FEFF is dropped; the last
definition of a name wins and removes the earlier one with everything nested in
it — including one name in both branches of an `if/else`, where the survivor is
the last in source order; a definition under a module- or class-level
`if`/`try`/`with`/loop is indexed — at module level the compound statement's own
lines are module header and own nothing, while inside a class they are class
body outside nested definitions and so, by norm §1.4's class region, count for
every method of the class (corrected after B1's review: rev 3 said "own nothing"
for both levels); a qualified token is `CODE:(BEH|AC)-<digits>[a-z]?` — only
criterion ids, as in norm §1.3; a method gets the regions of **every** enclosing class (the chain up to
the first non-class), never the reverse; a `def` nested in a function is not a
definition and is subtracted from its region. Whether a definition is a *test*
is decided by pytest's collection (§3.5), not by the parser's naming.

The gate (A) and the measurement (B) both call this module, so "which test
definitions carry `ENC:BEH-09`" has one answer in this repo — the lesson of
#270/#241. devtools re-derives it with its own parser (§5.3 completeness); the
golden fixtures in §7 are what both sides should pass.

### 2.3 The gate, tightened (§2.2)

Where #402's coverage check sits today (`execution._run_verify_first_phase`, at
the entry run, before any paid call), against the group's files **at the task's
commit** (`git show <sha>:./<path>`, never the tree). The rule is chosen by the
**file type**, not by whether the id is qualified:

- **`.py` group file — AST ownership, for bare and qualified ids alike.**
  - *Node-id entry* (`tests/test_x.py::TestLogin::test_ok[a]`): the parametrize
    suffix is stripped before resolution; the id counts only if its owner is
    exactly that definition or the region of a class containing it. A token on a
    neighbouring test in the same file does not count for this entry. A qualname
    the AST at that commit does not contain is an **INSTRUMENT** refusal that
    says so (`TestLogin::test_ok not defined in tests/test_x.py at <sha>` —
    dynamically generated or inherited-from-elsewhere tests are named as the
    likely cause).
  - *File-only entry* (`tests/test_x.py`): a **static approximation of default
    pytest collection** — module-level `test*` functions, and `test*` methods of
    `Test*` classes (module-level, or nested in a `Test*` class). Custom
    `python_functions`/`python_classes`/`python_files`, collection hooks and
    runtime behaviour can change what pytest actually collects; the gate does not
    read them. This limitation is documented in `FORMAT.md`.
- **Non-`.py` group file** (ExUnit `path:line`) — today's per-file whole-token
  match, unchanged. §1.4 has no rule for it; closure answers such bundles with
  `not-applicable: language`.

Refusal shapes stay #402's: terminal POLICY when a declared id is uncovered,
INSTRUMENT when a file cannot be read or a qualname cannot be resolved. The
POLICY message stops suggesting "e.g. in a docstring" and names the place: *the
test definition, or the containing test class*.

**Modes (closes rev 1 §8 q1).** The gate is `verify_first`-only in v1 — only
that mode has a group. For `tdd` and `standard`, qualified ids are accepted and
`validate` keeps its **warning** (never an error), reworded to say that
task-level token ownership is not checked in that mode. Criteria closure (B) is
independent of the task mode.

### 2.4 Release note

Minor, with a prominent **Changed** entry: labels that used to satisfy the gate
from a module docstring, module header or helper function in a `.py` group file
no longer do; move them into the test definition or its test class.

## 3. Slice B — `verify --criteria`

### 3.1 Command

`spec-runner verify --criteria --request <file.json> --json`. `--json` is
required (v1 has no human rendering). stdout carries exactly one JSON document on
every exit path. No agent is called, so no budget, ledger or `executor_sandbox`
applies. Flags: `--selector-timeout` (per isolated pytest process, default
300 s) and `--timeout` (whole measurement).

### 3.2 Exit codes and error kinds (§5.1)

`error.kind` is a **closed enum** in the response schema, and the kind fixes
both `retryable` and the exit code: `retryable` is `true` exactly for the exit-2
kinds. A response whose `retryable` contradicts its kind, or a kind outside the
enum, fails the schema; a new kind is a schema change (devtools#491 A).

| kind | exit | `retryable` | when |
|---|---|---|---|
| `request-invalid` | 2 | true | request unreadable or schema-invalid |
| `owner-repo-mismatch` | 2 | true | `owner_repo` ≠ the source repo's `origin` (SSH/HTTPS normalised) |
| `product-sha-absent` | 2 | true | `product_sha` not in the local object store ("fetch and retry") |
| `clone-failed` | 2 | true | clone or checkout failed, or `HEAD` ≠ `product_sha` after checkout |
| `environment-sync-failed` | 2 | true | `uv sync --locked` failed on network or cache |
| `unsupported-runtime` | 2 | true | the product environment is not CPython ≥ 3.12 (a machine property) |
| `collection-config-outside-checkout` | 2 | true | the pytest config actually read (`config.inipath`) lies outside the checkout (a machine property) |
| `collection-failed` | 2 | true | collection infrastructure failure, collection timeout, or missing/malformed collection evidence |
| `timeout` | 2 | true | the global `--timeout` |
| `lock-not-current` | 3 | false | `uv.lock` missing or stale (`uv sync --locked` refuses) |
| `collection-error` | 3 | false | pytest reported a collection error in the provisioned environment (syntax, import) |
| `product-roots-undeclared` / `-empty` / `-invalid` / `-no-python` / `-overlap-tests` | 3 | false | §3.4 |
| `definition-unresolved` | 3 | false | a `pytest.Function` whose definition does not resolve inside the checkout (§3.5) |
| `selector-absent` | 3 | false | an isolated run's **valid** report shows its requested selector absent |
| `distributed-execution` | 3 | false | test execution observed outside the designated process (§3.6) |

Exit 0 is an answer (per-BEH statuses may be `unconfirmed` or `error`); any
other exit code is a failed step.

A successful collection with zero tests is a valid inventory: every BEH is
`unconfirmed: no-test`, exit 0.

### 3.3 Source, clone and environment (G1)

1. Validate the request against our request schema.
2. Before cloning, compare `owner_repo` with the source repo's `origin`,
   normalising equivalent SSH and HTTPS forms (`git@github.com:o/r.git` ≡
   `https://github.com/o/r`). The clone's own `origin` names the local path and
   is not used.
3. `git clone --no-local --no-checkout <project_root> <tmp>/src`, then
   `git checkout --detach <product_sha>`, then `git rev-parse HEAD` must equal
   `product_sha`. The clone is taken from the local object store, not the
   network.
4. `uv sync --locked` with `UV_PROJECT_ENVIRONMENT` pointing **outside** the
   checkout (so the checkout can be reset, §3.6), the shared uv cache (the
   annex's choice), and every `PYTEST_*` variable removed from the environment.
   `--locked` refuses a missing or stale lock without updating it; `--frozen`
   would silently skip the freshness check and is not used.
5. The environment's interpreter must be CPython ≥ 3.12, else exit 2
   `unsupported-runtime`. spec-runner's own `requires-python` stays `>=3.11`.

**Injected instrumentation, stated plainly:** dependency installation is frozen
to the product's lock and nothing is added to it. What is added is one
stdlib-only file of ours — the probe plugin (§3.6) — on `PYTHONPATH`, loaded with
`-p`, never through the project's `conftest.py`. The project's own pytest
configuration is part of the product and is used as is, **except distribution**:
every test must run inside the designated probe process (§3.6), so
distribution the project configures is disabled or refused there.

### 3.4 Product roots — declared, read at `product_sha`

The annex's wheel-`RECORD` mapping is **not** used: devtools, the first consumer,
is `[tool.uv] package = false` with no build backend, and a heuristic ("every
`.py` that is not a test") turns helper modules in `tests/` into product. Roots
are **declared by the product repo** in its spec-runner config at `product_sha`:

```yaml
criteria:
  product_roots: [governance/, selfcheck/, todo_worker.py]
```

- Entries are repository-relative files or directories. Normalisation: POSIX
  separators, no leading `./`, no trailing `/`, no `.` segments. Absolute paths,
  `..` segments and any path whose resolution passes through a symlink (in the
  git tree at `product_sha`: an entry or ancestor of mode `120000`) are refused.
  Two entries equal after normalisation are refused (a duplicate, not a
  deduplication); overlapping entries (a file inside a declared directory) are
  allowed and resolve to their union.
- Directories expand deterministically to the tracked regular `.py` files under
  them at `product_sha` (`git ls-tree -r`), sorted by path.
- Refused at the response level (exit 3), each with its own `error.kind`:
  `product-roots-undeclared` (no key), `product-roots-empty` (empty list),
  `product-roots-invalid` (escape, symlink, duplicate, missing at
  `product_sha`), `product-roots-no-python` (resolves to no `.py` file),
  `product-roots-overlap-tests` (a resolved file is in the **full** collection
  inventory — a collected test module or a loaded `conftest.py` — checked before
  narrowing to BEH selectors).
- The response carries the normalised declaration **and** the resolved file list,
  so devtools can reconstruct the boundary on its own.

**Trust boundary.** The declaration says what the project considers product
code. The overlap check catches known test infrastructure; it cannot establish
that every remaining file is product. A too-wide declaration that sweeps in a
test helper outside the test files would count that helper's lines as product
execution. The declaration is committed and reviewed like any product change,
and it is visible in every response.

### 3.5 Collection inventory and selection

One `pytest --collect-only` of the whole suite, with the probe plugin loaded,
is the authoritative inventory. For every item the plugin reports the **actual
definition identity** from collection, not from the node id — inherited methods
and decorators make the two differ: the underlying function after
`inspect.unwrap`, its source file (`inspect.getsourcefile`), `__qualname__` and
first line. The first line is `__code__.co_firstlineno` of the unwrapped
function, which is **the line of the first decorator, else of `def`** — the
same line where the §2.2 region starts (measured on CPython 3.11 and 3.12,
through `@pytest.mark.parametrize` and a `functools.wraps` decorator). The
inventory also lists every loaded `conftest.py` and the pytest configuration
file actually read (`config.inipath`, when there is one).

Every collected `pytest.Function` is reported in the response as `test_items`
(§4), not only the selected ones: devtools checks completeness per `node_id`
against it (devtools#491 B.6) — comparing definitions alone would miss a lost
parametrized case, whose definition stays the same.

- A `pytest.Function` item whose definition cannot be resolved to a source file
  inside the checkout is a response-level error (`definition-unresolved`,
  exit 3): its tokens are unknown, so no BEH's selector set could be claimed
  complete.
- Items that are not Python functions (doctests, plugin-defined items) own no
  tokens; they are counted in the inventory as such — a named boundary, not a
  silent zero.
- A BEH's selectors are every item whose resolved definition owns its token by
  §2.2 (a parametrized function's token applies to all its collected ids). None
  → `unconfirmed: no-test`.

### 3.6 Isolated runs, twice (G0, G3)

**Each BEH selector runs in its own fresh pytest process, twice** — 2×N
processes, each narrowed to one node id. Attribution between tests is settled by
construction: another test's background work has no process to run in.

**Filesystem isolation between invocations.** A fresh process does not reset the
checkout. Before every invocation: `git reset --hard <product_sha>` and
`git clean -ffdx` in the checkout (the environment lives outside it, §3.3), and a
fresh per-invocation `TMPDIR`. After every invocation, the measured files
(resolved product files ∪ inventory test files) are re-hashed against
`product_sha`; any difference makes that run `error: runner`
(`measured-files-mutated`) — otherwise the next process would no longer measure
`product_sha`. Anything else a test may alter (the environment directory,
`$HOME`) is outside this guarantee and named as a boundary.

**The designated process.** The orchestrator starts the environment's
interpreter directly (`<env>/bin/python -m pytest …`, not through `uv run`), so
the pytest process is its direct child, and passes its own PID in
`SPEC_RUNNER_PROBE_PARENT`. At plugin load a process is the **report owner**
only if `os.getppid()` equals that PID; it then records its own PID. Every other
process that loads the plugin — a distribution worker, whose parent is the
pytest controller, or a fork, whose parent is the owner — is not the owner,
however it captured its own PID. Only the owner records and writes (see the PID
guard below).

**Distribution.** Project configuration (`addopts`, ini options, plugins in the
lock) can move test execution out of the designated process.

- *Supported and disabled:* pytest-xdist. When the collection pass reports
  `xdist` among the loaded plugins, every isolated invocation appends `-n 0
  --dist no`, which xdist documents as running in-process and which overrides a
  configured `-n` (later arguments win over `addopts`). `-p no:xdist` is not
  used: a configured `-n` would then be an unrecognised argument.
- *Everything else is detected and refused.* The owner's hookwrapper around
  `pytest_runtest_call` marks that `call` ran **in the owner**. If the owner
  receives a `call`-phase report (`pytest_runtest_logreport`) for the item
  without that mark — `--forked`, an unknown distribution plugin, or xdist
  somehow still active — execution left the designated process: response-level
  error `distributed-execution`, exit 3. It is a property of the product's
  configuration, so a retry would repeat it.

**The probe plugin** (`criteria_probe.py`, stdlib only, deployed to its own temp
dir like #583's verify reporter):

- **Collection check.** The process must collect exactly the requested node id,
  parametrization included; the manifest records what it collected.
- **Phases.** Outcomes of setup, call and teardown, as pytest reports them. A
  `call` that did not occur because setup failed or skipped is recorded as
  `not-reached` — a known outcome, distinct from an unknown one, which only an
  error run can express (§4).
- **G0 lines.** `sys.monitoring` (a dedicated tool id, `LINE` events,
  interpreter-wide, so worker threads created before `call` are seen) is enabled
  only inside a hookwrapper around `pytest_runtest_call`, so setup and teardown
  execution never counts. A line qualifies when its code object's file is a
  resolved product file and the code object is a function
  (`CO_OPTIMIZED`: not module code, not a class body) — and, afterwards in the
  orchestrator, when the same AST rule devtools uses (§5.3) places the line in a
  function or method body. Both checks are kept because neither suffices alone:
  a module-level generator expression is still its own `CO_OPTIMIZED` code
  object on 3.12 (PEP 709 inlines only list/set/dict comprehensions), so only
  the AST check excludes it; a lazy import or `importlib.reload` inside a test
  runs module and class bodies, which the code-object check excludes.
- **Observed, not caused.** Product lines executed in the `call` window by a
  thread belonging to the test's own fixtures (a server fixture, a worker made
  in setup) are credited as **observed execution** during the test's call. The
  design does not claim the test caused every observed line.
- **Process operations.** `sys.addaudithook` records, during `call`,
  `subprocess.Popen`, `os.posix_spawn`, `os.exec`, `os.system` and `os.fork`.
  An event proves a process operation was attempted, not that a child executed
  product code.
- **PID guard.** Recording (the monitoring callback, the audit hook, the phase
  and call-mark hooks) and report writing all do nothing unless the process is
  the owner **and** `os.getpid()` still equals the owner PID. A fork of the
  owner inherits the tracer and hooks but never records into, and never writes
  or overwrites, the owner's manifest. The child's lines are not observed — a
  named boundary.
- **Manifest.** Written once at session end by the owner, by atomic rename, to a
  per-invocation path. A missing or malformed manifest, a crash or a
  `--selector-timeout` makes that run an **error run** (§4) — `error: runner`.

### 3.7 Status per selector and per BEH (§4.2)

**Per run** the evidence is: collection status, phase outcomes, qualifying
product lines, process operations — or an error (§4). **Both runs must satisfy
every check**; no union or count across runs may let one good run hide an empty
second run.

**A run's test outcome** is computed over all three phases, the way pytest
decides whether a test passed: `passed` only if setup, call and teardown all
passed. A test that executes the product in `call` and then fails fixture
teardown is **not** passed. Otherwise the outcome is `failed` (any phase
failed) or `skipped` (setup skipped, nothing failed).

Per selector, in this order (the first that applies decides):

1. either run is an error run (runner/io, mutated files) → `error`, that run's
   reason;
2. both runs' test outcome `passed`? — no, and the two outcomes (as the full
   triple of phase results) differ → `unconfirmed: nondeterministic`; no, and
   they agree → `unconfirmed: not-passed`;
3. either run has zero qualifying lines → `unconfirmed: subprocess-only` if that
   run recorded a process operation during `call`, else
   `unconfirmed: no-product-execution`;
4. otherwise the selector is traced.

`subprocess-only` means exactly: zero qualifying in-process product lines plus
an observed process-operation attempt during `call`. It does not establish that
the child executed the product.

Per BEH: no selectors → `unconfirmed: no-test`. Otherwise **every** selected
test must be traced for `traced` (norm G3: "каждый"). Else `error` if any
selector is `error`; else `unconfirmed` with the reason of the first selector
failing in the precedence `not-passed` > `nondeterministic` >
`no-product-execution` > `subprocess-only`. Selector-level statuses and reasons
stay in the response, so a BEH's single reason never hides the others.

## 4. Response and schemas

`schemas/criteria-closure/v1/request.schema.json` and
`response.schema.json`, `additionalProperties: false`, pinned by a contract test
with golden fixtures (the `--json-result` pattern). A breaking change is `v2`
beside `v1`, never an edit of `v1`. The release PR for X writes
`schemas/criteria-closure/v1/min-spec-runner.env`.

**Request (§5.1, as pinned):** `protocol: 1`, `owner_repo`, `workstream`,
`code`, `bundle_pin`, `product_sha`, `test_criteria: [{id, verify_task}]`.
`verify_task` is echoed and has no effect: the `verify-human` path it served was
removed in norm rev 8.

**Response — two emitted branches** (and one reserved, below), so no field is ever filled with an invented value:

- **Answer** (exit 0): `protocol`, `request` (verbatim echo),
  `spec_runner_version`, `product_roots: {declared, files}`, `test_files` (the
  inventory's files, `conftest.py` and the read pytest config included),
  `test_items: [{node_id, definition: {file, qualname, line}}]` (every collected
  `pytest.Function`, `node_id` unique; each selector's `definition` equals the
  one its `node_id` has here), `environment: {lock_sha256, python,
  pytest_plugins}`, `content_sha256`, `beh[]`. `definition.line` is the line of
  the first decorator, else of `def` — stated in the schema, since devtools
  matches definitions by `(file, qualname, line)`. Per BEH: `id`, `status`,
  `reason` (required iff not `traced`, absent when `traced`), `selectors[]`.
  Per selector: `node_id`, `definition: {file, qualname, line}`, `status`,
  `reason`, `runs` — exactly two, each one of two shapes:
  - **complete run:** `{result: "complete", collected: [node ids],
    phases: {setup, call, teardown}, outcome, product_lines: [{file, lines[]}],
    product_line_count, process_operations[]}` — every field required.
    `product_line_count` is the total length of the `lines` lists. The norm's
    "child-process indicator" (§4.3) is `process_operations` being non-empty;
    there is no separate boolean, so the two cannot disagree (devtools#491 A). `setup` ∈ `passed|failed|skipped`; `call` ∈
    `passed|failed|skipped|not-reached`; `teardown` ∈ `passed|failed`;
    `outcome` ∈ `passed|failed|skipped` (§3.7). When `call` is `not-reached`,
    the empty line and operation lists are true statements — no call window
    existed.
  - **error run:** `{result: "error", reason: runner|io, detail}` plus only the
    evidence established before the failure, each field optional: what the
    orchestrator observed itself (`exit_status`, `timed_out`,
    `mutated_paths`) and, when a manifest exists but is incomplete or
    malformed, none of its contents — a manifest that fails validation is not
    evidence. A `call` outcome is never inferred: if it is unknown (timeout
    mid-call, crash, no manifest), the run is an error run, never a complete run
    with a guessed `call`.

  Lines are listed, not only counted, so devtools' AST check (§5.3) has
  something to check.
- **Error** (exit 2 or 3): `protocol`, `request` (when it parsed),
  `spec_runner_version`, `error: {kind, retryable, detail}` — and **only** the
  fields the measurement established truthfully before failing (`environment`
  after a successful sync, `product_roots` after resolution, and so on), each
  optional. Never `beh`.

**Reserved: not applicable.** The v1 response schema also defines a third branch,
`{protocol, request, spec_runner_version, not_applicable: {reason: "language"}}`
(no `beh`, no `error`), which v1 **never emits**: the three reasons of norm §3.6
are devtools' to decide today. It is reserved now because `additionalProperties:
false` and a closed `oneOf` would make adding it later a breaking change that
existing validators reject; reserving it keeps a future producer-side
`not_applicable: language` within v1. Any other reason, or any other new branch,
is `v2`.

The optional thread split (lines on the call thread vs other threads) is **not**
in v1; per-run evidence is the essential addition.

## 5. Where this follows the annex, and where not

| annex | here |
|---|---|
| M0 fresh clone, `PYTEST_*` cleared | followed; `uv sync --locked`, not `--frozen`; env outside the checkout |
| M0 roots via wheel `RECORD` | **not followed**: declared roots (§3.4) — `RECORD` fails a repo with no build backend |
| M1 `--collect-only` authoritative | followed; definition identity from collection, ownership in one shared module |
| M2 coverage.py with `--cov-context=test` | **not followed**: stdlib `-p` plugin, `sys.monitoring`, CPython ≥ 3.12; audit hooks for process operations |
| M3 two runs | followed, per selector in fresh processes, with per-run evidence |
| M4 `content_sha256` | followed; definition extended to the declaration, the lock and the read pytest config (§6.1) |

## 6. Agreed with devtools (devtools#491, 2026-09-29)

Rev 2 carried these as an addendum pending devtools' sign-off. devtools agreed
to every row and point, and devtools adapts its consumer (`criteria_check.py`,
`criteria_close.py`, `criteria_tokens.py`) to our schemas and fixtures — their
TODO `criteria-closure-v1-signoff`, blocked on release X.

### 6.1 `content_sha256`

Hex sha256 of the ASCII bytes of
`json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)`, where

```
obj = {"v": 1,
       "product_roots": <sorted normalised declared paths>,
       "lock": <environment.lock_sha256>,
       "files": [[<path>, <hex sha256 of the file's bytes at product_sha>], …]}
```

`files` = resolved product files ∪ `test_files` (collected test modules, loaded
`conftest.py`, and the pytest config file actually read — `config.inipath` —
when there is one), each path once, POSIX, repository-relative, sorted by UTF-8
bytes; normalisation and duplicate handling as in §3.4. A declaration change, a
lock change or a collection-config change each change the digest even when the
other inputs do not (devtools#491 C.1–C.2). Non-`.py` test data (fixture JSON
files) is **not** in the digest — a boundary devtools names in its own spec
(C.3). How devtools pre-checks G6 before a measurement is theirs to decide (C.4);
it places no requirement on this contract today.

### 6.2 Response and aggregation

- The shape of §4: `request` echo; `product_roots.{declared,files}`;
  `test_files`; `test_items`; exactly two runs per selector, complete or error;
  `not-reached`; the three-phase `outcome`; `process_operations` as the only
  child-process evidence; selector-level `status`/`reason`; `definition.line` as
  stated; `spec_runner_version`; the reserved, never-emitted
  `not_applicable: language` branch.
- The aggregation and precedence of §3.7 ("passed" = all three phases passed).
  devtools recomputes `outcome` and every status from the evidence and does not
  trust the producer's values.
- The error branch with the closed `kind` enum and the kind → `retryable` → exit
  mapping of §3.2; a JSON document on exit 2 as well as 3 (devtools treats 2 as a
  retryable step failure, 3 as `blocked`).

### 6.3 Shared ownership fixtures

Produced here, vendored by devtools under `PIN` with `manifest.json` (their path
`contracts/criteria-closure/v1/fixtures/ownership/`). Parity is a
**prerequisite** of slice B, not something the final §5.3 cross-check resolves.

- `tests/fixtures/criteria-closure/v1/ownership/<case>.py` + `<case>.expected.json`:
  - `owned`: a list of `{qualname, line, tokens}` sorted by `line`, one entry for
    every function and method `criteria_tokens` indexes (including non-`test*`
    ones; no classes; no `def` nested in a function), `tokens` including the
    regions of every enclosing class; `(qualname, line)` is the key devtools
    matches `definition.line` against;
  - `{"error": "syntax"}` instead of `owned` for an unparseable file;
  - `selection` (entry → selected qualnames) — ours, for the task gate; devtools
    does not read it.
- The fixture bytes are the cases (CRLF, lone `\r`, BOM, NUL, `\x0c`):
  `.gitattributes` marks `tests/fixtures/criteria-closure/** -text`, and the
  manifest catches any drift.
- Cases: the seven measured differences (form feed, redefined class, conditional
  definition, nested class with an outer-class token, BOM, a `test_*` method of a
  non-`Test*` class, a `test_*` nested in a test function) plus decorator line,
  nested helper, module header, class docstring, `async def`, parametrize, CRLF,
  lone `\r`, syntax error, NUL — and devtools' two additions: one name in both
  branches of an `if/else` (only the survivor in `owned`), and a module-level
  non-`test` helper carrying a token (it owns it; it is not a collected item);
  and two added after B1's review: the token grammar with its boundaries
  (`AC` ids, one-letter suffix, other codes, `REQ`/`TASK` ids and a 7-letter code
  rejected), and a class-level `if/else` with module-level `with`/`for` (the
  class-level compound lines count for the methods; the module-level ones own
  nothing); and case 22, proposed by devtools after mutation-testing their
  parser — definitions inside `except`, `finally` and `match case` bodies —
  22 cases in all.
- Response-verification cases (pytest collected the definition from the other
  `if/else` branch → `line` mismatch; a parametrized case's `node_id` lost →
  completeness failure) are devtools' verdicts and live in devtools. This repo
  ships positive golden responses in its schema contract tests.

## 7. Proof

- **Slice A (runs on every CI Python):** qualified and bare ids through the parser
  and the gate; a mixed line refused; class-region tokens counting for methods
  and not the reverse; module header, neighbouring test and nested-helper tokens
  refused; node-id entry vs file-only entry; parametrize suffix stripped; an
  unresolved qualname → INSTRUMENT with its message; the default-naming
  approximation and its documented custom-naming gap; ExUnit `path:line`
  unchanged; the `tdd`/`standard` warning not failing `validate`.
- **Probe bench (CPython ≥ 3.12):** norm §8.3's negative doubles — docstring
  reader, lazy import / `importlib.reload` reader, a bare `assert True`,
  `assert True` after a product call (`traced`, the named boundary),
  subprocess-only, a seeded flaky test — plus: setup/teardown execution excluded
  from `call`; a fixture worker thread started before `call`; `async def` tests;
  a list comprehension and a generator expression, each at module level and
  inside a function; `os.fork` (the child neither
  records nor writes); a skip before `call`; a test mutating a product file
  (`measured-files-mutated`); an inherited test method and a decorated test
  resolving to their real definitions; a selector absent in its isolated run;
  a test that executes the product and passes `call` but fails fixture
  teardown (`not-passed`, and `nondeterministic` when only one run's teardown
  fails); a setup failure (`call: not-reached`, a complete run); a timeout
  mid-call (an error run with no `call` outcome); a product whose `addopts`
  configures `-n 2` with xdist in its lock (runs in-process via `-n 0`, still
  `traced`); a product configured with `--forked` or an equivalent
  distribution (`distributed-execution`, exit 3, and no worker or forked
  child ever writing the owner's manifest).
  Run in its own workflow pinned to CPython 3.12 (the `exunit-contract.yml`
  precedent) and merged only on a green run; on 3.11 the bench is skipped with
  a stated reason, never silently.
- **Orchestration (every CI Python, incl. 3.11):** request validation, origin
  normalisation, the exit-code table, `unsupported-runtime` on a < 3.12
  environment, roots refusals, both response branches validating against the
  schema, one JSON document on every exit path. Clone + `uv sync --locked` on a
  mini-product offline from the cache is `@slow`.
- **Shared fixtures (every CI Python):** every `tests/fixtures/criteria-closure/v1/ownership/`
  case against `criteria_tokens` — `owned` and `selection` — byte-exact
  (`-text`); the answer's `test_items` agreeing with the selectors'
  definitions; `content_sha256` recomputed from its definition, including
  `lock` and the read pytest config; a `retryable` contradicting its `kind`
  rejected by the schema.
- **Live (devtools' acceptance):** `criteria-close` on a schema-2 bundle against
  released X: Must with no test → `no-test`; docstring reader (incl. lazy import)
  → `no-product-execution`; executing green test → `traced`.

## 8. Cost and risk

- Collection once, plus 2×N narrowed processes; per-process cost is import time.
  i5's 21 selectors → 42 processes. Tracing cost with `DISABLE` after a line's
  first hit is not measured yet — measure on devtools before promising a number.
- The trust boundary of declared roots (§3.4) and the fixture-thread boundary
  (§3.6) are visible in the response, not closed.

## 9. Decisions (owner, 2026-09-29) — rev 1 questions closed

**Rev 3 (devtools#491 sign-off, accepted by the owner):** `test_items` with
per-`node_id` completeness; `lock` and the read pytest config in
`content_sha256`; `child_process` removed in favour of `process_operations`; a
closed `error.kind` enum with a fixed kind → `retryable` → exit mapping;
`definition.line` = first decorator else `def`, stated in the schema; the
shared ownership fixture form with `-text`; devtools' negative
response-verification cases stay in devtools. The parser rules devtools adopts
are §2.2's, as shipped in slice A.

- **q1 (gate outside `verify_first`)**: `verify_first` only in v1; qualified ids
  accepted in every mode; `tdd`/`standard` get a non-failing warning. Closure is
  mode-independent.
- **q2 (no build backend)**: moot — roots are declared (§3.4), no wheel.
- **q3 (slice order)**: A, then B; A is a minor on its own; X is B's release.

Overturned from rev 1: bare ids no longer keep per-file matching in `.py` files;
the gate matches a node-id entry's own definition, not any test in the file;
`uv sync --locked` replaces `--frozen`; roots are declared, not mapped from a
wheel `RECORD`; no `sys.settrace` fallback (CPython ≥ 3.12 only — `threading.settrace`
misses worker threads started before it); one process per selector per run
instead of one per run; audit hooks instead of patching process functions;
`not_applicable` is not emitted; the response gains per-run evidence and an
error branch.
