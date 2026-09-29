# Criteria closure — B2a (contract, workspace, inventory, selection) Implementation Plan — rev 2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Rev 1** of this plan — with the full code this revision refers to as "rev 1" — is this same path at commit **`05cf3aa`**. Rev 2 answers the owner's review of PR #620: rejected `--all-groups --all-extras`, no runtime pytest, strict manifest validation, a deadline that bounds running steps, reference data from `product_sha`, a clean child environment, `teardown: skipped`, and structural vs semantic request validation.

**Goal:** Everything `verify --criteria` needs *before* a test is run: the frozen `criteria-closure/v1` schemas, the request and the error-kind table, bounded processes under one deadline, the product's criteria config read at `product_sha`, the fresh clone and the declared environment, the collection inventory (the probe's collect mode, fully validated), selection of BEH selectors and `content_sha256`. B2b adds the probe's run mode, the isolated runs, aggregation and the command.

**Architecture:** Flat modules, one job each: `criteria_contract.py` (kinds, errors, request), `criteria_process.py` (deadline, bounded process groups), `criteria_protocol.py` (probe/1 constants and manifest validators — orchestrator side, imports nothing from pytest), `criteria_config.py` (the product's criteria config at `product_sha`), `criteria_workspace.py` (origin, clone, blobs, env sync, child env), `criteria_probe.py` (the probe: **never imported by spec-runner**, only copied as source), `criteria_inventory.py` (deploy + collect + validate + mutation check), `criteria_select.py` (selectors, digest). No CLI surface until B2b.

**Tech Stack:** Python ≥ 3.11 (spec-runner), CPython ≥ 3.12 (product env), pytest (product env only), uv, git, PyYAML, jsonschema (dev only).

**Spec:** design rev 4 (Task 1 of this plan) — rev 3 §3.2–3.5, §4, §6.1 plus the deltas below.

**Start condition:** execution starts after devtools' «паритет подтверждён» with a SHA in devtools#491 **and** devtools' sign-off of the contract additions in Task 1 (they change §6.1, §3.2 and the response schema agreed in rev 3).

## Global Constraints

- spec-runner never imports pytest at runtime; `criteria_probe.py` is read as text (`importlib.resources`) and never executed by spec-runner. Pinned by a test that blocks `pytest` and imports every `criteria_*` orchestrator module.
- Every blocking operation (git, uv, the product's pytest, the interpreter check) runs through `criteria_process.run_bounded` in its **own process group**, bounded by `min(local timeout, deadline remaining)`; the group is killed (SIGKILL) on timeout **and** after a normal exit, so a test's background process survives neither into cleanup nor into the next run. Which limit fired is reported: `global` → `CriteriaError(TIMEOUT)`; `local` → the caller's per-step meaning.
- Reference data comes from `product_sha`, never from the working tree after product code has run: the criteria config via `git show <sha>:<path>`, measured bytes via `git cat-file --batch`.
- Child environment of the product's Python: every `PYTHON*`, `PYTEST_*` and `VIRTUAL_ENV` variable removed; then `PYTHONPATH=<probe dir>` (nothing else) and `PYTHONNOUSERSITE=1`; the interpreter runs with `-P` (no cwd on `sys.path` — the `sys.path` the product's own `pytest` entry point gets).
- Environment selection is the product's: `criteria.environment.{groups, extras}` in its spec-runner config at `product_sha`. Undeclared → `uv sync --locked` (uv's default groups, fixed by the product's own `pyproject.toml`); declared groups → `uv sync --locked --no-default-groups` + `--group g` per declared group; declared extras → `--extra e` each. Never `--all-groups` / `--all-extras` (measured: uv refuses a conflicting pair declared in `[tool.uv] conflicts`).
- A probe manifest counts as evidence only after full probe/1 validation (Task 6); `"complete": true` alone is never enough. An invalid collect manifest → `collection-failed`; never an empty inventory.
- Kinds, `retryable`, exit codes exactly as the rev-4 table (Task 1). Paths: repository-relative POSIX.
- Ruff 100; mypy strict; commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; branch `feat/603-b2a-contract-inventory`.

## The orchestrator ↔ probe interface — `probe/1`

Fixed here for B2a (collect) and B2b (run). The constants live **twice by design**: in `criteria_protocol.py` (imported by the orchestrator) and literally in `criteria_probe.py` (which runs inside the product and cannot import spec-runner); a test parses the probe's source with `ast` and asserts they agree.

**Invocation** (cwd = checkout; a process group of its own):

```
<env>/bin/python -P -m pytest -p _spec_runner_criteria_probe [-n 0 --dist no] --collect-only -q   # collect
<env>/bin/python -P -m pytest -p _spec_runner_criteria_probe [-n 0 --dist no] -q <node_id>       # run (B2b)
```

`-n 0 --dist no` when the product environment can import `xdist` (measured to override `addopts = -n 2`).

**Environment:** the child-env rule above, plus

| variable | value |
|---|---|
| `SPEC_RUNNER_PROBE_PARENT` | the orchestrator's PID |
| `SPEC_RUNNER_PROBE_MODE` | `collect` or `run` |
| `SPEC_RUNNER_PROBE_MANIFEST` | absolute manifest path, fresh per invocation |
| `SPEC_RUNNER_PROBE_PRODUCT_FILES` | run mode: absolute path of a JSON list of absolute product-file paths |
| `TMPDIR` | a fresh directory per invocation |

**Ownership:** owner iff `str(os.getppid()) == SPEC_RUNNER_PROBE_PARENT` at import; the probe acts only while `os.getpid() == OWNER_PID`. The orchestrator additionally requires `manifest.pid == <the child's PID>` — a manifest written by any other process is invalid.

**Manifest — collect mode** (every field required, types as shown):

```json
{"probe": 1, "mode": "collect", "pid": 123,
 "python": {"implementation": "CPython", "version": "3.12.13"},
 "rootpath": "/abs/checkout", "inipath": "/abs/checkout/pyproject.toml",
 "plugins": ["pytest-9.0.2"], "conftests": ["/abs/checkout/tests/conftest.py"],
 "items": [{"node_id": "tests/test_a.py::test_x[1]", "function": true, "module": "/abs/…/test_a.py",
            "definition": {"file": "/abs/…/test_a.py", "qualname": "test_x", "line": 8}}],
 "errors": [{"node_id": "tests/test_b.py", "message": "…"}],
 "exitstatus": 0, "complete": true}
```

`inipath` may be `null`. Consistency (all must hold): `exitstatus == <process return code>`; `errors == []` ⇒ `exitstatus ∈ {0, 5}` and (`items == []` ⇔ `exitstatus == 5`); `errors != []` ⇒ `exitstatus ∈ {1, 2}`; `definition` is `null` only when unwrapping failed for a `function: true` item, and always `null` when `function` is false.

**Manifest — run mode** (B2b):

```json
{"probe": 1, "mode": "run", "pid": 123, "collected": ["tests/test_a.py::test_x[1]"],
 "phases": {"setup": "passed", "call": "passed", "teardown": "passed"},
 "call_in_owner": true, "distributed": false,
 "product_lines": {"/abs/checkout/pkg/mod.py": [12, 13]},
 "process_operations": ["subprocess.Popen"], "exitstatus": 0, "complete": true}
```

plus optional `"monitoring_error": "<why>"`. Phases: `setup ∈ {passed, failed, skipped}`; `call ∈ {passed, failed, skipped, not-reached}` with `call == not-reached ⇔ setup != passed`; `teardown ∈ {passed, failed, skipped}`. Consistency: `exitstatus == return code`; `collected == []` ⇔ `phases == {}` and `exitstatus ∈ {4, 5}` (the selector is absent — measured: for a node id that does not exist pytest still calls `sessionfinish`, exit 4, `collected: []`); otherwise all three phases present and `exitstatus == 1` ⇔ some phase `failed` (else `0`). Filtering product lines to function bodies happens in the orchestrator.

## Review Focus

1. A collect manifest missing `items` (or with a wrong `pid`, or `complete` with `exitstatus` ≠ return code) — `collection-failed`, never an empty inventory and never `no-test`. Task 6/7.
2. A test module that rewrites a tracked file at import — `collection-mutated-checkout`, and the checkout restored before anything else reads it. Task 7.
3. The global deadline expiring **during** `uv sync` or a collection — the process group killed, `timeout` (exit 2), no orphan left. Tasks 3, 5, 7.
4. A module importable only through the parent's `PYTHONPATH` — invisible to the product (a collection error), never silently satisfied. Task 5/7.
5. `import spec_runner.criteria_*` with `pytest` unimportable — succeeds; the probe is deployed as text. Task 7.

---

### Task 1: Design rev 4 (owner-confirmed deltas; devtools sign-off for the contract ones)

**Files:** `docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md`.

- [ ] **Step 1: Branch** — `git switch master && git pull --ff-only && git switch -c feat/603-b2a-contract-inventory`
- [ ] **Step 2: Edit the design** (each item says whether devtools must sign off):
  1. §3.3 — environment selection as in Global Constraints, the measured conflict refusal, the undeclared default. **Contract:** response `environment.groups` (sorted declared names, or `null` when undeclared) and `environment.extras` (sorted, `[]` when undeclared); `content_sha256`'s object gains `"environment": {"groups": [...] | null, "extras": [...]}` — *pending devtools sign-off*.
  2. §3.2 — two kinds, exit 3 / `retryable: false`: `environment-selection-invalid` (a malformed `criteria.environment`, or uv refusing the declared selection — measured messages "is not defined in the project's" and "are incompatible with the conflicts") and `collection-mutated-checkout` (collection changed a tracked file). *Pending devtools sign-off* (closed enum).
  3. §4 — `teardown` may be `skipped`, as pytest reports it. *Pending devtools sign-off* (rev 3 listed `passed|failed`).
  4. §4 — `owner_repo`: `name` or `owner/name`; **a bare name does not check the owner** (named boundary). `bundle_pin`: a 40-hex SHA, echoed verbatim. Owner-accepted; devtools already sends both forms.
  5. §3.3/§3.5/§3.6 — the bounded-process rule, reference data from `product_sha`, the child-env rule (`-P`, `PYTHONNOUSERSITE`, only the probe on `PYTHONPATH`). Mechanism, no contract change.
  6. §9 — a "Rev 4" entry listing the above and the owner's rejection of `--all-groups --all-extras`.
- [ ] **Step 3: Commit** — `docs(#603): design rev 4 — declared env selection, two kinds, teardown skipped, request forms`

---

### Task 2: Schemas v1, the kind table, the request

**Files:** `schemas/criteria-closure/v1/{request,response}.schema.json`; `tests/fixtures/criteria-closure/v1/responses/{answer,error-retryable,error-blocked,not-applicable}.json`; `src/spec_runner/criteria_contract.py`; `tests/test_criteria_contract.py`.

**Interfaces — Produces:** `PROTOCOL = 1`; `ErrorKind(str, Enum)` — **21** kinds, `.retryable`, `.exit_code`; `CriteriaError(kind, detail, **established)`; `Request(raw, owner_repo, code, product_sha, beh_ids)`; `parse_request(data) -> Request`; `normalise_remote(url) -> (owner, name) | None`; `owner_matches(requested, origin_url) -> bool`.

Rev 1's code (`05cf3aa`, Task 2) stands with these changes:

- `ErrorKind` adds `ENVIRONMENT_SELECTION_INVALID = "environment-selection-invalid"` and `COLLECTION_MUTATED_CHECKOUT = "collection-mutated-checkout"` (neither retryable). The schema's `error_kind` enum gains both; `retryable_kinds` stays the 9 of rev 1. `test_the_nineteen_kinds` becomes `test_the_twenty_one_kinds` (`len(ErrorKind) == 21`).
- `response.schema.json`: `teardown` enum `["passed", "failed", "skipped"]`; `environment` gains required `groups` (`{"oneOf": [{"type": "null"}, {"type": "array", "uniqueItems": true, "items": {"type": "string"}}]}`) and `extras` (`{"type": "array", "uniqueItems": true, "items": {"type": "string"}}`). Goldens carry `"groups": null, "extras": []` wherever `environment` appears.
- **Structural vs semantic validation are separate tests.** The schema pins shape; `parse_request` additionally enforces what JSON Schema cannot: every `test_criteria[].id` carries the request's `code`, and ids are unique regardless of `verify_task`. The parametrized invalid-field test drops its `XYZ:BEH-01` row (the schema cannot refuse it — reproduced by the owner); a new class pins the split:

```python
class TestSemanticRules:
    """Rules the schema cannot express — parse_request enforces them, the schema accepts."""

    def test_foreign_code_is_schema_valid_but_refused(self):
        data = copy.deepcopy(REQUEST)
        data["test_criteria"][0]["id"] = "XYZ:BEH-01"
        assert not list(_schema("request").iter_errors(data))
        with pytest.raises(CriteriaError) as raised:
            parse_request(data)
        assert raised.value.kind is ErrorKind.REQUEST_INVALID

    def test_same_id_different_verify_task_is_schema_valid_but_refused(self):
        data = copy.deepcopy(REQUEST)
        data["test_criteria"].append({"id": "ENC:BEH-01", "verify_task": True})
        assert not list(_schema("request").iter_errors(data))
        with pytest.raises(CriteriaError):
            parse_request(data)
```

- `Request.raw` stays the request verbatim (including `bundle_pin`); `owner_matches`'s docstring states the boundary: "a bare name checks the name only — a repository of that name under any owner matches".
- A skipped-teardown check: a copy of `answer.json` with one run's `teardown: "skipped"`, `outcome: "skipped"` validates.

- [ ] Steps: failing tests (RED: module missing) → schemas + goldens + module → GREEN (`uv run pytest tests/test_criteria_contract.py -q`, ruff, mypy) → commit `feat(#603): criteria-closure/v1 schemas, error-kind table, request parsing`.

---

### Task 3: Bounded processes under one deadline

**Files:** `src/spec_runner/criteria_process.py`; `tests/test_criteria_process.py`.

**Interfaces — Produces:**

```python
@dataclass
class Deadline:
    seconds: float
    started: float = field(default_factory=time.monotonic)
    def remaining(self) -> float: ...            # never negative
    def check(self) -> None: ...                  # CriteriaError(TIMEOUT) when exhausted

@dataclass(frozen=True)
class Finished:
    returncode: int | None                        # None when killed on a timeout
    stdout: str
    stderr: str
    pid: int
    timed_out: Literal["local", "global"] | None

def run_bounded(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None,
                deadline: Deadline, local_timeout: float | None = None,
                stdin: str | None = None) -> Finished: ...
def run_or_raise(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str] | None,
                 deadline: Deadline, kind: ErrorKind, what: str,
                 local_timeout: float | None = None, stdin: str | None = None) -> Finished: ...
    # global timeout → CriteriaError(TIMEOUT); local timeout or non-zero exit → CriteriaError(kind, f"{what}: …")
```

Behaviour: `deadline.check()` first; `subprocess.Popen(..., start_new_session=True)`; `communicate(timeout=min(local, remaining))`; on `TimeoutExpired` → `os.killpg(pid, SIGKILL)`, `communicate()` to reap, `timed_out` = `"local"` when the local limit was the smaller one, else `"global"`; after **every** exit also `os.killpg(pid, SIGKILL)` guarded by `ProcessLookupError`/`PermissionError`, so no member of the group outlives the call. POSIX only (macOS, Linux).

- [ ] **Step 1: Failing tests:**
  - within both limits → `returncode 0`, `timed_out None`;
  - `sleep 30`, `local_timeout=1`, deadline 60 → `timed_out == "local"`, returns in < 5 s;
  - `sleep 30`, deadline 1 → `timed_out == "global"`; `run_or_raise` raises `ErrorKind.TIMEOUT`;
  - `sh -c 'sleep 30 & echo $! > pid; exit 0'` → after return, `os.kill(<pid>, 0)` raises `ProcessLookupError` (Review Focus 3);
  - `Deadline(0).check()` raises `TIMEOUT`.
- [ ] Steps 2–5; commit `feat(#603): bounded process groups under one measurement deadline`.

---

### Task 4: The product's criteria config at `product_sha`

**Files:** `src/spec_runner/criteria_config.py` (replaces rev 1's `criteria_roots.py`); `tests/test_criteria_config.py`.

**Interfaces — Produces:**

```python
@dataclass(frozen=True)
class ProductCriteria:
    roots: tuple[str, ...]               # normalised, sorted, unique
    groups: tuple[str, ...] | None       # None = undeclared (uv's default groups)
    extras: tuple[str, ...]              # () when undeclared

def read_product_criteria(checkout: Path, sha: str, deadline: Deadline) -> ProductCriteria
def resolve_roots(checkout: Path, sha: str, roots: Sequence[str], deadline: Deadline) -> list[str]
def check_overlap(files: Sequence[str], test_files: Sequence[str]) -> None
```

Reading: `git show <sha>:spec-runner.config.yaml`, else `<sha>:spec/executor.config.yaml`, through `run_bounded` — never the working tree; neither present → `product-roots-undeclared`. Section = `data.get("executor", data)`. `criteria.product_roots`: rev 1's rules (`undeclared` / `empty` / `invalid`, normalisation, duplicates refused). `criteria.environment` (optional): a mapping holding only `groups` and/or `extras`, each a list of names matching `^[A-Za-z0-9][A-Za-z0-9._-]*$` without duplicates; anything else → `environment-selection-invalid`. `resolve_roots` / `check_overlap`: rev 1's (tracked symlink → `invalid`; no `.py` → `no-python`; overlap → `overlap-tests`), `git ls-tree` through `run_bounded`.

- [ ] **Step 1: Failing tests** — rev 1's roots table, each read from a commit whose **working tree was edited after committing** (the edit must not be seen); plus `criteria.environment`: absent → `(None, ())`; `{groups: [governance]}` → `(("governance",), ())`; `{groups: [dev, governance], extras: [cli]}`; `{groups: governance}`, `{groups: ["bad name"]}`, `{groups: [a, a]}`, `{unknown: []}` → `environment-selection-invalid`.
- [ ] Steps 2–5; commit `feat(#603): product criteria config read at product_sha`.

---

### Task 5: The workspace — origin, clone, blobs, declared env, child env

**Files:** `src/spec_runner/criteria_workspace.py`; `tests/test_criteria_workspace.py`.

**Interfaces — Produces:**

```python
def check_origin(project_root: Path, owner_repo: str, deadline: Deadline) -> None
def clone_at(project_root: Path, sha: str, into: Path, deadline: Deadline) -> Path
def read_blobs(checkout: Path, sha: str, paths: Sequence[str], deadline: Deadline) -> dict[str, bytes]
    # one `git cat-file --batch`; a path absent at sha → CriteriaError(CLONE_FAILED)
def reset_checkout(checkout: Path, sha: str, deadline: Deadline) -> None      # reset --hard + clean -ffdx
def tracked_changes(checkout: Path, deadline: Deadline) -> list[str]         # status --porcelain --untracked-files=no -z

@dataclass(frozen=True)
class Environment:
    python: Path
    implementation: str
    version: str
    lock_sha256: str
    has_xdist: bool
    groups: tuple[str, ...] | None
    extras: tuple[str, ...]
    @property
    def label(self) -> str: ...

def sync_environment(checkout: Path, env_dir: Path, criteria: ProductCriteria, deadline: Deadline) -> Environment
def child_env(probe_dir: Path, extra: Mapping[str, str]) -> dict[str, str]
def distribution_args(env: Environment) -> list[str]
```

`sync_environment`: no `uv.lock` → `lock-not-current`; argv `uv sync --locked` + the declared selection (Global Constraints), **no `--quiet`** (measured: it hides the stale-lock line); stderr → "`--locked` was provided" → `lock-not-current`; "is not defined in the project's" or "are incompatible with the conflicts" → `environment-selection-invalid`; else `environment-sync-failed`; global timeout → `timeout`. The interpreter check through `run_bounded` with `child_env`; not CPython ≥ 3.12 → `unsupported-runtime`.

`child_env`: from `os.environ` drop every key starting with `PYTHON` or `PYTEST_`, and `VIRTUAL_ENV`; set `PYTHONPATH = str(probe_dir)`, `PYTHONNOUSERSITE = "1"`; apply `extra`.

- [ ] **Step 1: Failing tests** — rev 1's origin/clone tests with a `Deadline`, plus:
  - `read_blobs` returns committed bytes although the working tree was changed; a path absent at `sha` raises;
  - `child_env` with `PYTHONPATH=/elsewhere`, `PYTHONHOME`, `PYTHONSTARTUP`, `PYTEST_ADDOPTS`, `VIRTUAL_ENV` set → none survive; `PYTHONPATH == str(probe_dir)`, `PYTHONNOUSERSITE == "1"`;
  - `@slow` sync (a dependency-free project; offline where possible): missing lock and stale lock (version bump) → `lock-not-current`; a declared undefined group → `environment-selection-invalid`; a declared conflicting pair in a `[tool.uv] conflicts` project → `environment-selection-invalid`; a declared valid group installs and is reported in `Environment.groups`; `.venv` never created inside the checkout. The global deadline during sync (Review Focus 3): monkeypatch `criteria_process.run_bounded` to record the `deadline` it receives and return `timed_out="global"` → `sync_environment` raises `TIMEOUT` (that the group is then really killed is Task 3's test, not repeated with a live network here).
- [ ] Steps 2–5; commit `feat(#603): criteria workspace — origin, clone, blobs, declared env, clean child env`.

---

### Task 6: probe/1 — constants and manifest validators (orchestrator side)

**Files:** `src/spec_runner/criteria_protocol.py`; `tests/test_criteria_protocol.py`.

**Interfaces — Produces:**

```python
PROTOCOL = 1
PROBE_MODULE = "_spec_runner_criteria_probe"
PARENT_ENV = "SPEC_RUNNER_PROBE_PARENT"
MODE_ENV = "SPEC_RUNNER_PROBE_MODE"
MANIFEST_ENV = "SPEC_RUNNER_PROBE_MANIFEST"
PRODUCT_FILES_ENV = "SPEC_RUNNER_PROBE_PRODUCT_FILES"

def read_manifest(path: Path) -> object | None            # parsed JSON, or None; never raises
def valid_collect(data: object, *, child_pid: int, returncode: int) -> dict[str, Any] | None
def valid_run(data: object, *, child_pid: int, returncode: int) -> dict[str, Any] | None   # B2b
```

A validator returns the manifest only when **every** rule of the probe/1 section holds — fields, types, `probe == 1`, `mode`, `pid == child_pid`, `complete is True`, every consistency rule; otherwise `None`. No partial acceptance, no defaults filled in.

- [ ] **Step 1: Failing tests** — a valid collect and a valid run manifest accepted; then table-driven rejections, one row per rule: each required field removed; each field with a wrong type; `probe: 2`; wrong `mode`; `pid` off by one; `complete: false`; `exitstatus` ≠ return code; `items == []` with `exitstatus 0`; `errors` with `exitstatus 0`; `definition` non-null with `function: false`; run — `call: not-reached` with `setup: passed`, `collected == []` with phases present, `exitstatus 0` with a failed teardown, `exitstatus 1` with no failed phase; `teardown: skipped` **accepted**. Plus the constant-agreement test: parse `criteria_probe.py` with `ast`, read its module-level assignments of the six names, assert equal to `criteria_protocol`'s.
- [ ] Steps 2–5; commit `feat(#603): probe/1 constants and strict manifest validation`.

---

### Task 7: The probe's collect mode and the inventory

**Files:** `src/spec_runner/criteria_probe.py`; `src/spec_runner/criteria_inventory.py`; `tests/test_criteria_inventory.py`; `tests/test_criteria_no_runtime_pytest.py`.

**Interfaces — Produces:** `deploy_probe(into: Path) -> Path` — the source via `importlib.resources.files("spec_runner").joinpath("criteria_probe.py").read_text(encoding="utf-8")`, **not** an import; `TestItem(node_id, file, qualname, line)`; `Inventory(items, test_files, inipath, plugins)`; `collect(env, checkout, sha, probe_dir, work, deadline, local_timeout) -> Inventory`.

The probe: rev 1's collect-mode code with its constants literal (Task 6 keeps them equal); a top-level `import pytest` is allowed (it only ever runs inside the product). `collect`: argv per probe/1 (`-P`); `run_bounded`; `global` → `timeout`, `local` → `collection-failed`; `valid_collect(...)` is `None` → `collection-failed` (detail: return code and the last stderr lines); `errors` → `collection-error`; config outside the checkout → `collection-config-outside-checkout`; unresolvable definitions → `definition-unresolved`. **Then** `tracked_changes(checkout)` non-empty → `collection-mutated-checkout` naming the paths — and in every case `reset_checkout` before returning or raising.

- [ ] **Step 1: Failing tests** — rev 1's inventory tests (inherited definition in a helper module, parametrized line = decorator line, zero tests = a valid empty inventory, an import error = `collection-error`, a local timeout = `collection-failed`, config outside), plus:
  - a test module that rewrites a tracked product file at import → `collection-mutated-checkout`, the file restored afterwards (Review Focus 2);
  - a probe deployed with its `items` write removed (patch the deployed text) → `collection-failed`, not an empty inventory (Review Focus 1);
  - a module reachable only through the parent's `PYTHONPATH`, imported by a product test → `collection-error` (Review Focus 4).

`tests/test_criteria_no_runtime_pytest.py` (Review Focus 5):

```python
"""#603: spec-runner must not need pytest at runtime; the probe is shipped as text."""

import subprocess
import sys

MODULES = ["criteria_contract", "criteria_process", "criteria_protocol", "criteria_config",
           "criteria_workspace", "criteria_inventory", "criteria_select"]


def test_orchestrator_modules_import_without_pytest():
    code = (
        "import sys; sys.modules['pytest'] = None; sys.modules['_pytest'] = None\n"
        + "".join(f"import spec_runner.{m}\n" for m in MODULES)
        + "import pathlib, tempfile\n"
        + "from spec_runner.criteria_inventory import deploy_probe\n"
        + "d = deploy_probe(pathlib.Path(tempfile.mkdtemp()))\n"
        + "assert (d / '_spec_runner_criteria_probe.py').read_text().startswith('\"\"\"')\n"
        + "assert 'spec_runner.criteria_probe' not in sys.modules\n"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
```

(B2b appends `criteria_run`, `criteria_aggregate`, `criteria_measure`, `cli` to `MODULES`; the release rehearsal of X runs `spec-runner verify --criteria` from the `uv tool` install, which has no pytest.)
- [ ] Steps 2–5; commit `feat(#603): the probe's collect mode and a validated, mutation-checked inventory`.

---

### Task 8: Selection and `content_sha256`

**Files:** `src/spec_runner/criteria_select.py`; `tests/test_criteria_select.py`.

**Interfaces — Produces:** `select(items, beh_ids, blobs: Mapping[str, bytes]) -> dict[str, list[TestItem]]` — definition text from `product_sha` blobs, decoded UTF-8; unparseable → `definition-unresolved`; `(qualname, line)` not held by `owned_definitions` → `definition-unresolved`. `content_sha256(roots, lock_sha256, groups, extras, files: Mapping[str, bytes]) -> str` over the rev-4 object `{"v": 1, "product_roots": [...], "lock": …, "environment": {"groups": [...] | null, "extras": [...]}, "files": [[path, sha256], …]}` (sorted groups and extras; the canonical JSON of rev 3 §6.1).

- [ ] **Step 1: Failing tests** — rev 1's selection tests (reading from a blobs mapping); the digest recomputed from its definition; each of roots, lock, groups (including `null` vs `[]`), extras and one file byte moving it.
- [ ] Steps 2–5; commit `feat(#603): BEH selection by token ownership and the rev-4 content digest`.

---

### Task 9: Docs and the gate

`CLAUDE.md` rows for the eight modules and the test files; `CHANGELOG.md` `[Unreleased]` › Added ("criteria-closure/v1 schemas and the measurement's pre-run stages; no command yet"); `TODO.md` B2a checked under `criteria-closure-b2`. Full gate: format, ruff, mypy, changelog links, fast + slow suites. One local review round (raise `--max-diff-files` explicitly if needed), PR, acceptance review, merge on approve with green checks.
