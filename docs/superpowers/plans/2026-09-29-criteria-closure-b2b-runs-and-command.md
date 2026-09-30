# Criteria closure — B2b (probe run mode, isolated runs, aggregation, command) Implementation Plan — rev 4

Rev 1 is this path at `05cf3aa`. Rev 2 follows B2a rev 2 (probe/1 with strict validation, bounded processes, the clean child env, reference data from `product_sha`, `teardown: skipped`) and the owner's review of PR #620; Tasks 2 and 4–6 are given as exact interfaces and checkable criteria rather than full code.

Rev 3 follows B2a rev 3's process cleanup and byte-preserving subprocess transport. An absent selector now keeps `phases == {}`; the probe regression passes the emitted manifest through `valid_run`, and the runner regression requires response-level `SELECTOR_ABSENT`.

Rev 4 (spec-runner#623, design rev 4): Task 4 step 6 also yields `collection_excluded` (an `established` field from there on); step 8 reads blobs of product files ∪ test files ∪ `pyproject.toml` ∪ the tracked `.py` files under `skipped`/`ignored` exclusions. Nothing in the run mode changes; `teardown: skipped` was already rev 2.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish `spec-runner verify --criteria --request <file> --json`: the probe's run mode (in-process product lines during `call`, process operations, distribution detection), one fresh pytest process per selector run twice with the checkout reset between invocations, the §3.7 aggregation, the answer/error documents on every exit path, and the CPython-3.12 bench. The release carrying this plan is **X**.

**Architecture:** B2a's modules are consumed unchanged. New: run-mode hooks in `criteria_probe.py`; `criteria_run.py` (one isolated invocation → a run object); `criteria_aggregate.py` (pure: run outcome, selector status, BEH status); `criteria_measure.py` (the pipeline, the error branch, the global deadline); the `verify --criteria` flags in `cli.py` / `cli_info.py`. The orchestrator↔probe interface is **probe/1 as fixed in B2a rev 3** — this plan implements its run-mode half and changes nothing in it; run manifests are accepted only through `criteria_protocol.valid_run`.

**Tech Stack:** Python ≥ 3.11 (orchestrator), CPython ≥ 3.12 (`sys.monitoring`, the product environment), pytest, git, uv.

**Spec:** design rev 4 §3.6–3.7, §4; B2a rev 3 (probe/1, Tasks 3–8).

**Start condition:** after B2a is merged; B2a itself starts after devtools' «паритет подтверждён» in devtools#491.

## Global Constraints

- probe/1 exactly as B2a rev 3 fixes it: invocation (`-P`), the child-env rule, ownership, manifest written once via temp + `os.replace`, and **strict validation** — a run manifest is evidence only if `valid_run(data, child_pid=…, returncode=…)` accepts it; anything else is an error run (never `selector-absent`, never a complete run).
- A qualifying product line: the code object's file is a declared product file **and** `co_flags & CO_OPTIMIZED` (probe side), **and** the line lies in a function body by the rule devtools uses (orchestrator side): for every `FunctionDef`/`AsyncFunctionDef` anywhere in the file, `range(body[0].lineno, end_lineno + 1)` (devtools `criteria_close._function_lines` @ `540564f`, read).
- Tracing on only inside a hookwrapper around `pytest_runtest_call`; `sys.monitoring` with a free tool id; `DISABLE` after a location's first hit.
- Each selector: two runs, each a fresh `<env>/bin/python -P -m pytest -p _spec_runner_criteria_probe [-n 0 --dist no] -q <node_id>` through `run_bounded` (own process group, killed after exit), in a checkout reset by `reset_checkout` before the run, a fresh `TMPDIR`, a per-invocation manifest; afterwards the measured files are compared with their bytes **at `product_sha`** (`read_blobs`), not with anything read after product code ran.
- The global deadline bounds every run: `min(--selector-timeout, remaining)`; a `global` expiry is `CriteriaError(TIMEOUT)` for the whole measurement, a `local` one is that run's error run.
- spec-runner imports no pytest at runtime: `criteria_run`, `criteria_aggregate`, `criteria_measure` and `cli` join B2a's `test_criteria_no_runtime_pytest.py` `MODULES`.
- Aggregation exactly design §3.7; outcome over all three phases.
- stdout: exactly one JSON document on every exit path; exit 0 / kind.exit_code; `--json` required.
- The bench runs under CPython ≥ 3.12 in its own required workflow; on 3.11 it is skipped with a reason.
- Ruff 100; mypy strict; commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; branch `feat/603-b2b-runs-command`.

## Review Focus

1. A test that spawns a worker thread in a fixture *before* `call` and the worker runs product code during `call` — counted (observed execution), because `sys.monitoring` is interpreter-wide. Pinned in Task 5.
2. `os.fork()` in `call`: the child neither records nor writes the manifest; the parent's manifest is the only one. Pinned in Task 5.
3. A product whose tests delete or rewrite a product file — `measured-files-mutated`, error run, never a complete run over changed bytes. Pinned in Task 2.
4. Global `--timeout` expiring mid-measurement — `timeout` (exit 2) with only established fields, no partial `beh`. Pinned in Task 4.
5. A request file that is not JSON — `request-invalid`, exit 2, and still a JSON document on stdout. Pinned in Task 6.

---

### Task 1: The probe's run mode

**Files:** Modify `src/spec_runner/criteria_probe.py`; Test `tests/test_criteria_probe_run.py`.

**Interfaces — Produces (probe/1 run manifest):** `collected`, `phases` (`setup`/`call`/`teardown`, `call` = `not-reached` when setup did not pass), `call_in_owner`, `distributed`, `product_lines` (absolute file → sorted lines), `process_operations`, `exitstatus`, `complete`, plus `monitoring_error` (string) when no `sys.monitoring` tool id was free.

- [ ] **Step 1: Branch** — `git switch master && git pull --ff-only && git switch -c feat/603-b2b-runs-command`

- [ ] **Step 2: Write the failing tests** — `tests/test_criteria_probe_run.py` (whole module skipped below 3.12):

```python
"""#603 B2b: the probe's run mode (probe/1) — lines in call only, owner-only writes."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from spec_runner.criteria_inventory import deploy_probe
from spec_runner.criteria_process import Deadline, run_bounded
from spec_runner.criteria_protocol import (
    MANIFEST_ENV,
    MODE_ENV,
    PARENT_ENV,
    PROBE_MODULE,
    PRODUCT_FILES_ENV,
    valid_run,
)
from spec_runner.criteria_workspace import child_env

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs CPython >= 3.12")


def _run(tmp_path: Path, files: dict[str, str], node_id: str, product: list[str], extra: list[str] = ()) -> dict:
    checkout = tmp_path / "checkout"
    for rel, text in files.items():
        path = checkout / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    probe = deploy_probe(tmp_path / "probe")
    manifest = tmp_path / "run.json"
    product_json = tmp_path / "product.json"
    product_json.write_text(json.dumps([str((checkout / p).resolve()) for p in product]))
    env = child_env(probe, {
        PARENT_ENV: str(os.getpid()), MODE_ENV: "run",
        MANIFEST_ENV: str(manifest), PRODUCT_FILES_ENV: str(product_json),
    })
    finished = run_bounded(
        [sys.executable, "-P", "-m", "pytest", "-p", PROBE_MODULE, *extra, "-q", node_id],
        cwd=checkout, env=env, deadline=Deadline(120),
    )
    assert finished.timed_out is None and finished.returncode is not None
    result = json.loads(manifest.read_text())
    assert valid_run(result, child_pid=finished.pid, returncode=finished.returncode) is not None
    return result


PRODUCT = {"pkg/__init__.py": "", "pkg/mod.py": "CONST = 1\n\n\ndef work(x):\n    y = x + 1\n    return y\n"}


class TestLinesInCallOnly:
    def test_missing_selector_has_no_phases(self, tmp_path):
        files = {"tests/test_a.py": "def test_a():\n    pass\n"}
        m = _run(tmp_path, files, "tests/test_a.py::test_missing", [])
        assert m["collected"] == [] and m["phases"] == {}
        assert m["exitstatus"] == 4
        assert m["call_in_owner"] is False and m["distributed"] is False

    def test_call_lines_recorded_setup_lines_not(self, tmp_path):
        files = {**PRODUCT, "tests/test_a.py": (
            "import pytest\nfrom pkg.mod import work\n\n\n"
            "@pytest.fixture\ndef warm():\n    work(0)\n\n\n"
            "def test_a(warm):\n    assert work(1) == 2\n"
        ), "conftest.py": "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n"}
        m = _run(tmp_path, files, "tests/test_a.py::test_a", ["pkg/mod.py"])
        assert m["complete"] and m["mode"] == "run"
        assert m["collected"] == ["tests/test_a.py::test_a"]
        assert m["phases"] == {"setup": "passed", "call": "passed", "teardown": "passed"}
        lines = m["product_lines"][str((tmp_path / "checkout" / "pkg/mod.py").resolve())]
        assert lines == [5, 6]  # the body of work(); module-level CONST = 1 never counts
        assert m["call_in_owner"] is True and m["distributed"] is False

    def test_setup_failure_is_not_reached(self, tmp_path):
        files = {**PRODUCT, "tests/test_a.py": (
            "import pytest\n\n\n@pytest.fixture\ndef broken():\n    raise RuntimeError\n\n\n"
            "def test_a(broken):\n    pass\n"
        )}
        m = _run(tmp_path, files, "tests/test_a.py::test_a", ["pkg/mod.py"])
        assert m["phases"]["setup"] == "failed" and m["phases"]["call"] == "not-reached"

    def test_process_operations_recorded_in_call(self, tmp_path):
        files = {**PRODUCT, "tests/test_a.py": (
            "import subprocess, sys\n\n\ndef test_a():\n"
            "    subprocess.run([sys.executable, '-c', 'pass'], check=True)\n"
        )}
        m = _run(tmp_path, files, "tests/test_a.py::test_a", ["pkg/mod.py"])
        assert "subprocess.Popen" in m["process_operations"]
        assert m["product_lines"] == {}


class TestOwnership:  # Review Focus 2
    def test_fork_child_never_writes(self, tmp_path):
        files = {**PRODUCT, "conftest.py": "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n",
                 "tests/test_a.py": (
            "import os\nfrom pkg.mod import work\n\n\ndef test_a():\n"
            "    pid = os.fork()\n    if pid == 0:\n        work(5)\n        os._exit(0)\n"
            "    os.waitpid(pid, 0)\n"
        )}
        m = _run(tmp_path, files, "tests/test_a.py::test_a", ["pkg/mod.py"])
        assert m["pid"] != 0 and "os.fork" in m["process_operations"]
        assert m["product_lines"] == {}  # the child ran work(); its lines are not observed

    def test_forked_distribution_is_detected(self, tmp_path):
        pytest.importorskip("pytest_forked")
        files = {**PRODUCT, "tests/test_a.py": "def test_a():\n    pass\n"}
        m = _run(tmp_path, files, "tests/test_a.py::test_a", ["pkg/mod.py"], ["--forked"])
        assert m["distributed"] is True and m["call_in_owner"] is False
```

- [ ] **Step 3: Run to verify it fails** — `uv run pytest tests/test_criteria_probe_run.py -q` → FAIL: `KeyError: 'collected'` / manifest missing run fields.

- [ ] **Step 4: Implement** — in `src/spec_runner/criteria_probe.py`, import `sys`, `import pytest` at module top (the plugin is only ever imported by pytest), and add:

```python
_AUDITED = frozenset({"subprocess.Popen", "os.posix_spawn", "os.exec", "os.system", "os.fork", "os.forkpty"})
_run: dict[str, Any] = {"collected": [], "phases": {}, "call_ran": set(), "call_reports": [], "lines": {}, "ops": []}
_tracing = False
_tool: int | None = None
_real_cache: dict[str, str] = {}


def _product_files() -> frozenset[str]:
    path = os.environ.get(PRODUCT_FILES_ENV)
    if not path:
        return frozenset()
    with open(path, encoding="utf-8") as handle:
        return frozenset(os.path.realpath(p) for p in json.load(handle))


_PRODUCT: frozenset[str] = _product_files() if _manifest["mode"] == "run" else frozenset()


def _real(path: str) -> str:
    cached = _real_cache.get(path)
    if cached is None:
        cached = _real_cache[path] = os.path.realpath(path)
    return cached


def _on_line(code: Any, line: int) -> Any:
    if _tracing and _owner() and code.co_flags & inspect.CO_OPTIMIZED:
        path = _real(code.co_filename)
        if path in _PRODUCT:
            _run["lines"].setdefault(path, set()).add(line)
    return sys.monitoring.DISABLE


def _audit(event: str, args: Any) -> None:
    if _tracing and _owner() and event in _AUDITED:
        _run["ops"].append(event)


if _manifest["mode"] == "run" and OWNER_PID is not None:
    sys.addaudithook(_audit)
    for candidate in (3, 4, sys.monitoring.COVERAGE_ID, sys.monitoring.PROFILER_ID):
        if sys.monitoring.get_tool(candidate) is None:
            sys.monitoring.use_tool_id(candidate, "spec-runner-criteria")
            sys.monitoring.register_callback(candidate, sys.monitoring.events.LINE, _on_line)
            _tool = candidate
            break
    else:
        _manifest["monitoring_error"] = "no free sys.monitoring tool id"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: Any) -> Any:
    global _tracing
    owner = _owner() and _manifest["mode"] == "run"
    if owner:
        _run["call_ran"].add(item.nodeid)
        if _tool is not None:
            sys.monitoring.set_events(_tool, sys.monitoring.events.LINE)
        _tracing = True
    try:
        yield
    finally:
        if owner:
            _tracing = False
            if _tool is not None:
                sys.monitoring.set_events(_tool, 0)


def pytest_runtest_logreport(report: Any) -> None:
    if not (_owner() and _manifest["mode"] == "run"):
        return
    _run["phases"][report.when] = report.outcome
    if report.when == "call":
        _run["call_reports"].append(report.nodeid)
```

In `pytest_collection_finish`, for run mode record `_run["collected"] = [item.nodeid for item in session.items]`. In `pytest_sessionfinish`, before writing, for run mode:

```python
    if _manifest["mode"] == "run":
        phases = dict(_run["phases"])
        if phases.get("setup") in {"failed", "skipped"}:
            phases["call"] = "not-reached"
        _manifest.update(
            collected=_run["collected"],
            phases=phases,
            call_in_owner=all(n in _run["call_ran"] for n in _run["call_reports"]) and bool(_run["call_reports"]),
            distributed=any(n not in _run["call_ran"] for n in _run["call_reports"]),
            product_lines={p: sorted(ls) for p, ls in _run["lines"].items()},
            process_operations=list(_run["ops"]),
        )
```

Only an observed failed/skipped setup establishes `call: not-reached`. No collected item means no setup, so preserve `phases == {}` for the absent-selector manifest (exit 4 or 5); missing phase evidence for a collected item stays invalid. `call_in_owner` is false when no call report arrived — including an absent selector or a failed/skipped setup — and `distributed` stays false then; B2b's orchestrator reads the validated collection and phases for these cases.

- [ ] **Step 5: Run** — `uv run pytest tests/test_criteria_probe_run.py tests/test_criteria_inventory.py -q` → PASS (the collect-mode tests of B2a must stay green). `uv run mypy src && uv run ruff check .` → clean. mypy runs on 3.11 semantics (`python_version = "3.11"`): guard `sys.monitoring` uses with `if sys.version_info >= (3, 12):` blocks so mypy accepts them.

- [ ] **Step 6: Commit** — `feat(#603): the probe's run mode — call-only product lines, process operations, distribution`

---

### Task 2: One isolated run

**Files:** Create `src/spec_runner/criteria_run.py`; add `function_body_lines` to `src/spec_runner/criteria_tokens.py`; Test `tests/test_criteria_run.py`.

**Interfaces:**
- Consumes: probe/1 and `valid_run`, `read_manifest` (B2a Task 6); `run_bounded`, `Deadline` (Task 3); `Environment`, `child_env`, `distribution_args`, `reset_checkout` (Task 5); `run_outcome` (this plan's Task 3).
- Produces:

```python
def function_body_lines(source: str) -> frozenset[int]:
    """Lines inside any function or method body — devtools' rule (criteria_close._function_lines):
    for every `def`/`async def` anywhere, body[0].lineno through end_lineno. BOM-tolerant."""
    lines: set[int] = set()
    for node in ast.walk(ast.parse(_without_bom(source))):
        if isinstance(node, _FUNCTIONS) and node.body:
            lines.update(range(node.body[0].lineno, (node.end_lineno or node.lineno) + 1))
    return frozenset(lines)

def run_selector(env: Environment, checkout: Path, sha: str, probe_dir: Path, work: Path,
                 node_id: str, product_files: Sequence[str], measured: Mapping[str, bytes],
                 blobs: Mapping[str, bytes], deadline: Deadline,
                 selector_timeout: float) -> dict[str, object]:
    """One fresh pytest process for `node_id` → a `complete_run` or an `error_run` object
    (response schema); raises CriteriaError(TIMEOUT | SELECTOR_ABSENT | DISTRIBUTED_EXECUTION)."""
```

`measured` are the bytes **at `product_sha`** of every measured file (product files ∪ test files); `blobs` supplies the product files' text for `function_body_lines` — both from `read_blobs`, never re-read from the checkout.

**Behaviour, in order:** `reset_checkout(checkout, sha, deadline)` → fresh invocation dir with `TMPDIR`, manifest path, product-files JSON → `run_bounded([python, "-P", "-m", "pytest", "-p", PROBE_MODULE, *distribution_args(env), "-q", node_id], cwd=checkout, env=child_env(probe_dir, {...}), deadline=deadline, local_timeout=selector_timeout)` → then:

| observation | result |
|---|---|
| `timed_out == "global"` | raise `CriteriaError(TIMEOUT)` |
| `timed_out == "local"` | error run: `reason runner`, `detail "<node_id> exceeded <n>s"`, `timed_out: true` |
| any measured file's bytes in the checkout ≠ `measured[path]` (or missing) | error run: `reason runner`, `detail "measured-files-mutated"`, `mutated_paths` sorted, `exit_status` |
| `valid_run(read_manifest(path), child_pid=finished.pid, returncode=finished.returncode)` is `None` | error run: `reason runner`, `detail` naming the rejected manifest (never absent, never complete) |
| valid, `monitoring_error` present | error run: `reason runner`, `detail = monitoring_error` |
| valid, `distributed` true | raise `CriteriaError(DISTRIBUTED_EXECUTION)` |
| valid, `collected == []` | raise `CriteriaError(SELECTOR_ABSENT)` |
| valid, `collected != [node_id]` | error run (a valid manifest cannot say this — defensive) |
| otherwise | complete run: `collected`, `phases` (incl. `teardown: skipped`), `outcome = run_outcome(phases)`, `product_lines` = for each file in the manifest, lines ∩ `function_body_lines(blobs[file])`, dropping files left empty, repository-relative, sorted; `product_line_count` = total; `process_operations` |

The invocation dir is removed on every path.

- [ ] **Step 1: Failing tests** (3.12+ for runs; `function_body_lines` everywhere):
  - `function_body_lines`: module constant and class-body line excluded, a comprehension inside a function included, `async def` body included, BOM tolerated (`{2}` for `"\ufeffdef f():\n    return 1\n"`);
  - a complete run with `pkg/mod.py` lines `[2]`, `product_line_count == 1`;
  - a fixture teardown that calls `pytest.skip` → complete run, `teardown: "skipped"`, `outcome: "skipped"`;
  - a test rewriting `pkg/mod.py` → error run, `mutated_paths == ["pkg/mod.py"]`, and the next `run_selector` sees the original bytes (Review Focus 3);
  - a node id that does not exist → a real probe manifest with `collected: []`, `phases: {}`, exit 4 passes `valid_run` → `SELECTOR_ABSENT` (not an error run); also cover a valid exit-5 empty manifest;
  - a deployed probe with its manifest write patched to drop `phases` → error run, **not** `SELECTOR_ABSENT` and not complete;
  - `selector_timeout=2` on a sleeping test → error run `timed_out: true`; `Deadline(3)` with `selector_timeout=60` → `CriteriaError(TIMEOUT)`;
  - a test that starts `sleep 60 &` with inherited output descriptors and passes → `run_selector` returns a complete run before its local timeout, the background process is no longer running, and no false timeout/error run is produced.
- [ ] Step 2 RED; Step 3 implement; Step 4 GREEN + ruff + mypy; Step 5 commit `feat(#603): one isolated, bounded, validated selector run`.

---

### Task 3: Aggregation (§3.7), pure

**Files:** Create `src/spec_runner/criteria_aggregate.py`; Test `tests/test_criteria_aggregate.py`.

**Interfaces — Produces:** `run_outcome(phases: Mapping[str, str]) -> str`; `selector_status(runs: Sequence[Mapping[str, object]]) -> tuple[str, str | None]`; `beh_status(selectors: Sequence[tuple[str, str | None]]) -> tuple[str, str | None]`.

- [ ] **Step 1: Failing tests** — the §3.7 table:

```python
"""#603 B2b: design §3.7 — outcome over three phases, selector and BEH status."""

import pytest

from spec_runner.criteria_aggregate import beh_status, run_outcome, selector_status

P = {"setup": "passed", "call": "passed", "teardown": "passed"}


def complete(phases=P, lines=1, ops=()):
    return {"result": "complete", "phases": phases, "outcome": run_outcome(phases),
            "product_line_count": lines, "process_operations": list(ops)}


ERR = {"result": "error", "reason": "runner", "detail": "x"}


@pytest.mark.parametrize(("phases", "outcome"), [
    (P, "passed"),
    ({**P, "teardown": "failed"}, "failed"),
    ({"setup": "failed", "call": "not-reached", "teardown": "passed"}, "failed"),
    ({"setup": "skipped", "call": "not-reached", "teardown": "passed"}, "skipped"),
    ({**P, "call": "skipped"}, "skipped"),
    ({**P, "teardown": "skipped"}, "skipped"),
])
def test_run_outcome(phases, outcome):
    assert run_outcome(phases) == outcome


@pytest.mark.parametrize(("runs", "expected"), [
    ([complete(), complete()], ("traced", None)),
    ([ERR, complete()], ("error", "runner")),
    ([complete(), {**ERR, "reason": "io"}], ("error", "io")),
    ([complete({**P, "teardown": "failed"}), complete()], ("unconfirmed", "nondeterministic")),
    ([complete({**P, "teardown": "failed"})] * 2, ("unconfirmed", "not-passed")),
    ([complete(lines=0, ops=["subprocess.Popen"]), complete()], ("unconfirmed", "subprocess-only")),
    ([complete(), complete(lines=0)], ("unconfirmed", "no-product-execution")),
    # skipped: identical triples -> not-passed; different triples -> nondeterministic
    ([complete({**P, "teardown": "skipped"})] * 2, ("unconfirmed", "not-passed")),
    ([complete({"setup": "skipped", "call": "not-reached", "teardown": "passed"})] * 2, ("unconfirmed", "not-passed")),
    ([complete({**P, "teardown": "skipped"}), complete({**P, "call": "skipped"})], ("unconfirmed", "nondeterministic")),
    ([complete({**P, "teardown": "skipped"}), complete()], ("unconfirmed", "nondeterministic")),
])
def test_selector_status(runs, expected):
    assert selector_status(runs) == expected


@pytest.mark.parametrize(("selectors", "expected"), [
    ([], ("unconfirmed", "no-test")),
    ([("traced", None)] * 3, ("traced", None)),
    ([("traced", None), ("error", "io"), ("unconfirmed", "not-passed")], ("error", "io")),
    ([("unconfirmed", "subprocess-only"), ("unconfirmed", "no-product-execution")], ("unconfirmed", "no-product-execution")),
    ([("unconfirmed", "nondeterministic"), ("unconfirmed", "not-passed")], ("unconfirmed", "not-passed")),
])
def test_beh_status(selectors, expected):
    assert beh_status(selectors) == expected
```

- [ ] **Step 2: RED**, **Step 3: Implement**:

```python
"""Design §3.7 as pure functions: run outcome, selector status, BEH status (#603)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

_PRECEDENCE = ("not-passed", "nondeterministic", "no-product-execution", "subprocess-only")


def run_outcome(phases: Mapping[str, str]) -> str:
    """passed only if setup, call and teardown all passed; any failure → failed; else skipped."""
    values = [phases.get(k) for k in ("setup", "call", "teardown")]
    if all(v == "passed" for v in values):
        return "passed"
    return "failed" if "failed" in values else "skipped"


def selector_status(runs: Sequence[Mapping[str, object]]) -> tuple[str, str | None]:
    """The first rule of §3.7 that applies; both runs must satisfy every check."""
    for run in runs:
        if run["result"] == "error":
            return "error", str(run["reason"])
    outcomes = [run["outcome"] for run in runs]
    if any(o != "passed" for o in outcomes):
        triples = [tuple(sorted(dict(run["phases"]).items())) for run in runs]  # type: ignore[arg-type]
        return "unconfirmed", "nondeterministic" if len(set(triples)) > 1 else "not-passed"
    for run in runs:
        if run["product_line_count"] == 0:
            return "unconfirmed", "subprocess-only" if run["process_operations"] else "no-product-execution"
    return "traced", None


def beh_status(selectors: Sequence[tuple[str, str | None]]) -> tuple[str, str | None]:
    """no selectors → no-test; any error → error; any unconfirmed → by precedence; else traced."""
    if not selectors:
        return "unconfirmed", "no-test"
    errors = [reason for status, reason in selectors if status == "error"]
    if errors:
        return "error", errors[0]
    reasons = {reason for status, reason in selectors if status == "unconfirmed"}
    for reason in _PRECEDENCE:
        if reason in reasons:
            return "unconfirmed", reason
    return "traced", None
```

- [ ] **Step 4: GREEN**; **Step 5: Commit** — `feat(#603): §3.7 aggregation as pure functions`

---

### Task 4: The pipeline and the documents

**Files:** Create `src/spec_runner/criteria_measure.py`; Test `tests/test_criteria_measure.py`.

**Interfaces — Produces:**

```python
def measure(project_root: Path, request_data: object, *, selector_timeout: float,
            timeout: float, version: str) -> tuple[int, dict[str, object]]:
    """The exit code and the one response document (answer or error)."""
```

**Order** (one `Deadline(timeout)` for all of it; each step's output becomes an `established` field for a later failure; the temp workspace is removed in `finally` after every process group is gone):

1. `parse_request` → `request` (echoed from here on);
2. `check_origin`;
3. `clone_at` (temp dir);
4. `read_product_criteria` at `product_sha` (roots declared, environment selection);
5. `sync_environment(..., criteria)` → `environment` (with `groups`, `extras`);
6. `deploy_probe`; `collect` (validated, mutation-checked, checkout reset) → `test_files`, `test_items`;
7. `resolve_roots` + `check_overlap` → `product_roots`;
8. `read_blobs(product files ∪ test files)` at `product_sha` → `content_sha256(roots, lock, groups, extras, blobs)`;
9. `select(items, beh_ids, blobs)`;
10. for every **distinct** selector node id, in inventory order: `run_selector` twice → `selector_status`; each BEH lists its selectors (a shared node id reuses the same two runs) → `beh_status`;
11. the answer (design §4, rev 4 `environment`).

`CriteriaError` at any step → `(kind.exit_code, {"protocol": 1, "request"?: raw, "spec_runner_version": version, "error": {"kind", "retryable", "detail"}, **established})`; an unexpected exception → **not** swallowed into a kind (it is a bug; let it surface as a traceback and a non-contract exit — devtools reads "other exit code" as a failed step).

**Criteria (tests, with the heavy steps monkeypatched; the real ones are B2a's and Task 2's):**
- request not an object / unreadable → exit 2, `request-invalid`, no `request` field;
- `product-roots-undeclared` from step 4 → exit 3, no `environment` (it was not established yet), no `beh`;
- `environment-selection-invalid` from step 5 → exit 3;
- `collection-mutated-checkout` from step 6 → exit 3, carrying `environment`;
- deadline exhausted between runs → exit 2 `timeout`, established fields up to `content_sha256`, **no `beh`** (Review Focus 4);
- happy path: one traced BEH, one `no-test` BEH, two BEHs sharing one selector (one pair of runs, listed under both) → exit 0; the document validates against `response.schema.json`;
- every document of these tests validates against the schema; the `retryable` of each error equals `kind.retryable`.
- [ ] Steps: RED → implement → GREEN (+ruff, mypy) → commit `feat(#603): the measurement pipeline under one deadline, and its documents`.

---

### Task 5: The bench (CPython ≥ 3.12, own workflow)

**Files:** Create `tests/test_criteria_bench.py`, `.github/workflows/criteria-probe.yml`.

Each case is a tiny product in `tmp_path` (committed to a git repo), run through `run_selector` twice and `selector_status`, under this interpreter (`Environment(sys.executable, …)`). Expected statuses:

| case | expected |
|---|---|
| test reads the product's docstring, never calls it | `unconfirmed: no-product-execution` |
| lazy `import pkg.mod` / `importlib.reload(pkg.mod)` inside the test | `unconfirmed: no-product-execution` |
| bare `assert True` | `unconfirmed: no-product-execution` |
| calls the product, then `assert True` | `traced` (the named boundary) |
| product only via `subprocess.run([sys.executable, "-m", "pkg"])` | `unconfirmed: subprocess-only` |
| flaky test: fails on the first run, passes on the second — a counter file **outside** the checkout, its path passed in an env var (the checkout and `TMPDIR` are reset per run, so state inside them cannot carry over) | `unconfirmed: nondeterministic` |
| setup-created worker thread running product code during `call` (Review Focus 1) | `traced` |
| `async def` test (via `asyncio.run` in the test) | `traced` |
| module-level list comprehension / generator expression in the product, called nowhere | `no-product-execution` |
| product function with a comprehension and a generator expression, called | `traced`, lines include the body |
| `os.fork()` child calling the product (Review Focus 2) | `subprocess-only` — zero in-process product lines plus the observed `os.fork` operation (§3.7); the child's lines are never observed |
| setup failure | `not-passed` (`call: not-reached`) |
| call passes, fixture teardown fails in both runs / in one run | `not-passed` / `nondeterministic` |
| inherited test method, decorated test | resolved definitions (via B2a `collect` + `select`) |
| product with `addopts = -n 2` and pytest-xdist importable | `traced`, run in the owner |
| `--forked` in `addopts` with pytest-forked importable | `CriteriaError(DISTRIBUTED_EXECUTION)` |

Cases needing `pytest-xdist` / `pytest-forked` use `pytest.importorskip`; the workflow installs both.

`.github/workflows/criteria-probe.yml`:

```yaml
name: criteria-probe bench
on:
  pull_request:
  push:
    branches: [master]
jobs:
  bench:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with:
          python-version: "3.12"
      - run: uv sync --all-groups
      - run: uv pip install pytest-xdist pytest-forked
      - run: uv run pytest -q tests/test_criteria_bench.py tests/test_criteria_probe_run.py tests/test_criteria_run.py -rs
```

(Follow `exunit-contract.yml` for the checkout/uv action versions the repo already pins; copy them rather than the ones above if they differ.)

- [ ] Steps: write the bench; prove each row can fail by running it once with `_on_line` returning before recording (every `traced` row must then turn `no-product-execution`) and restoring it; run GREEN; add the workflow; commit `test(#603): the §8.3 bench and boundary cases, CPython 3.12 workflow`.

---

### Task 6: The command, end to end

**Files:** Modify `src/spec_runner/cli.py` (`verify` subparser), `src/spec_runner/cli_info.py` (`cmd_verify`); Test `tests/test_criteria_cli.py`.

- [ ] **Step 1: Failing tests:**
  - `verify --criteria` without `--request` or without `--json` → argparse usage error (exit 2 from argparse is acceptable here: it is a caller bug, not a measurement), documented in `--help`;
  - a request path that does not exist / holds invalid JSON → stdout is one JSON document, `error.kind == "request-invalid"`, exit 2 (Review Focus 5);
  - `@pytest.mark.slow` end to end: a git repo with `uv.lock` (pytest in a dev group), `criteria.product_roots`, a traced BEH, a docstring-reader BEH, a Must with no test, and `criteria.environment.groups` declaring the dev group → exit 0, statuses `traced` / `no-product-execution` / `no-test`, the document validates, `environment.groups` echoes the declaration, `content_sha256` recomputes from the rev-4 object over the bytes **at `product_sha`** (`git show`), and `test_items` agree with every selector's definition;
  - the command path with `pytest` unimportable (`python -c` with `sys.modules['pytest'] = None`, then `spec_runner.cli.main(["verify", "--criteria", "--request", <missing>, "--json"])`) prints the `request-invalid` document and exits 2 — the command needs no pytest to run.
- [ ] **Step 2: RED**; **Step 3: Implement** — flags `--criteria`, `--request PATH`, `--selector-timeout SECONDS` (default 300), `--timeout SECONDS` (default 3600); `cmd_verify` branches to `criteria_measure.measure(config.project_root, …, version=__version__)`, prints `json.dumps(doc)`, exits with the code. No other `verify` behaviour changes.
- [ ] **Step 4: GREEN** (fast and slow); **Step 5: Commit** — `feat(#603): spec-runner verify --criteria (criteria-closure/v1)`

---

### Task 7: Docs, CHANGELOG, TODO, the gate

(Release X's rehearsal — the runbook's step 7 — adds: `spec-runner verify --criteria` from the `uv tool` install, which has no pytest, on a disposable product repo: one `traced`, one `no-test`, exit 0; and a bad request → exit 2 with a JSON document.)

- `CLAUDE.md`: rows for `criteria_run.py`, `criteria_aggregate.py`, `criteria_measure.py`; the `verify --criteria` line in *CLI entry points*; test files in the Testing list.
- `CHANGELOG.md` `[Unreleased]` › Added: the command, its contract (`schemas/criteria-closure/v1/`), exit codes, CPython ≥ 3.12 product environments, the new workflow.
- `TODO.md`: `B2b` checked; the release step names X and `schemas/criteria-closure/v1/min-spec-runner.env` (`MIN_SPEC_RUNNER_VERSION=<X>`) written in the release PR — not here.
- Full gate: format, ruff, mypy, changelog links, fast and slow suites, and the bench workflow green on the PR. Then one local review round (`--max-diff-files` raised explicitly if needed), PR, acceptance review, merge on approve with green checks.
- After merge: a comment in devtools#491 (or their `criteria-closure-v1-signoff` thread) that B2 is in `master` at `<sha>`, unreleased; the release follows as X.
