# Criteria closure — B2b (probe run mode, isolated runs, aggregation, command) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish `spec-runner verify --criteria --request <file> --json`: the probe's run mode (in-process product lines during `call`, process operations, distribution detection), one fresh pytest process per selector run twice with the checkout reset between invocations, the §3.7 aggregation, the answer/error documents on every exit path, and the CPython-3.12 bench. The release carrying this plan is **X**.

**Architecture:** B2a's modules are consumed unchanged. New: run-mode hooks in `criteria_probe.py`; `criteria_run.py` (one isolated invocation → a run object); `criteria_aggregate.py` (pure: run outcome, selector status, BEH status); `criteria_measure.py` (the pipeline, the error branch, the global deadline); the `verify --criteria` flags in `cli.py` / `cli_info.py`. The orchestrator↔probe interface is **probe/1 as fixed in the B2a plan** — this plan implements its run-mode half and changes nothing in it.

**Tech Stack:** Python ≥ 3.11 (orchestrator), CPython ≥ 3.12 (`sys.monitoring`, the product environment), pytest, git, uv.

**Spec:** design rev 4 §3.6–3.7, §4; B2a plan (probe/1 interface, Tasks 2-6).

**Start condition:** after B2a is merged; B2a itself starts after devtools' «паритет подтверждён» in devtools#491.

## Global Constraints

- probe/1 exactly as the B2a plan fixes it: invocation, environment variables, ownership (`os.getppid() == SPEC_RUNNER_PROBE_PARENT` at import; act only while `os.getpid() == OWNER_PID`), manifest written once via temp + `os.replace`, `"complete": true`.
- A qualifying product line: the code object's file is a declared product file **and** `co_flags & CO_OPTIMIZED` (probe side), **and** the line lies in a function body by the rule devtools uses (orchestrator side): for every `FunctionDef`/`AsyncFunctionDef` anywhere in the file, `range(body[0].lineno, end_lineno + 1)` (devtools `criteria_close._function_lines` @ `540564f`, read).
- Tracing on only inside a hookwrapper around `pytest_runtest_call`; `sys.monitoring` with a free tool id; `DISABLE` after a location's first hit.
- Each selector: two runs, each a fresh `<env>/bin/python -m pytest -p _spec_runner_criteria_probe [-n 0 --dist no] -q <node_id>` in a checkout reset by `git reset --hard <sha>` + `git clean -ffdx`, a fresh `TMPDIR`, a per-invocation manifest; measured files re-hashed afterwards.
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
import subprocess
import sys
from pathlib import Path

import pytest

from spec_runner.criteria_inventory import PROBE_MODULE, deploy_probe
from spec_runner.criteria_probe import MANIFEST_ENV, MODE_ENV, PARENT_ENV, PRODUCT_FILES_ENV

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
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update({
        "PYTHONPATH": str(probe), PARENT_ENV: str(os.getpid()), MODE_ENV: "run",
        MANIFEST_ENV: str(manifest), PRODUCT_FILES_ENV: str(product_json),
    })
    subprocess.run(
        [sys.executable, "-m", "pytest", "-p", PROBE_MODULE, *extra, "-q", node_id],
        cwd=checkout, env=env, capture_output=True, text=True, timeout=120,
    )
    return json.loads(manifest.read_text())


PRODUCT = {"pkg/__init__.py": "", "pkg/mod.py": "CONST = 1\n\n\ndef work(x):\n    y = x + 1\n    return y\n"}


class TestLinesInCallOnly:
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
        if phases.get("setup") != "passed":
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

(`call_in_owner` is false when no call report arrived at all — setup failed — and `distributed` stays false then; B2b's orchestrator reads `phases`, not `call_in_owner`, for that case.)

- [ ] **Step 5: Run** — `uv run pytest tests/test_criteria_probe_run.py tests/test_criteria_inventory.py -q` → PASS (the collect-mode tests of B2a must stay green). `uv run mypy src && uv run ruff check .` → clean. mypy runs on 3.11 semantics (`python_version = "3.11"`): guard `sys.monitoring` uses with `if sys.version_info >= (3, 12):` blocks so mypy accepts them.

- [ ] **Step 6: Commit** — `feat(#603): the probe's run mode — call-only product lines, process operations, distribution`

---

### Task 2: One isolated run

**Files:** Create `src/spec_runner/criteria_run.py`; add `function_body_lines` to `src/spec_runner/criteria_tokens.py`; Test `tests/test_criteria_run.py`.

**Interfaces:**
- Consumes: probe/1; `Environment`, `child_env`, `distribution_args`; `PROBE_MODULE`; `CriteriaError`, `ErrorKind`.
- Produces:
  - `criteria_tokens.function_body_lines(source: str) -> frozenset[int]` — devtools' rule, BOM-tolerant
  - `reset_checkout(checkout: Path, sha: str) -> None`
  - `run_selector(env, checkout, sha, probe_dir, work, node_id, product_files: Sequence[str], measured: Mapping[str, bytes], timeout: float) -> dict[str, object]` — a `complete_run` or `error_run` object per the response schema; raises `CriteriaError(SELECTOR_ABSENT | DISTRIBUTED_EXECUTION)`

- [ ] **Step 1: Failing tests** — `tests/test_criteria_run.py` (3.12+ for the runs; `function_body_lines` on every Python). Measured: for a node id that does not exist pytest still calls `pytest_sessionfinish` (exit 4, `collected: []`), so the manifest is valid and absence is `selector-absent`, not an error run:

```python
"""#603 B2b: one isolated invocation → one run object."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_inventory import deploy_probe
from spec_runner.criteria_run import reset_checkout, run_selector
from spec_runner.criteria_tokens import function_body_lines
from spec_runner.criteria_workspace import Environment

needs_312 = pytest.mark.skipif(sys.version_info < (3, 12), reason="sys.monitoring needs CPython >= 3.12")


class TestFunctionBodyLines:
    def test_devtools_rule(self):
        source = "X = 1\n\n\ndef f():\n    '''doc'''\n    y = [i for i in range(3)]\n    return y\n\n\nclass K:\n    Z = 2\n\n    async def g(self):\n        return 1\n"
        assert function_body_lines(source) == frozenset({5, 6, 7, 14})

    def test_bom(self):
        assert function_body_lines("\ufeffdef f():\n    return 1\n") == frozenset({2})


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def product(tmp_path: Path):
    root = tmp_path / "checkout"
    files = {
        "conftest.py": "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n",
        "pkg/__init__.py": "",
        "pkg/mod.py": "def work(x):\n    return x + 1\n",
        "tests/test_a.py": "from pkg.mod import work\n\n\ndef test_a():\n    assert work(1) == 2\n",
        "tests/test_mutate.py": "import pathlib\n\n\ndef test_m():\n    pathlib.Path('pkg/mod.py').write_text('changed')\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "c")
    sha = _git(root, "rev-parse", "HEAD")
    env = Environment(Path(sys.executable), "CPython", "3.12", "0" * 64, False)
    measured = {p: (root / p).read_bytes() for p in ("pkg/mod.py", "tests/test_a.py", "tests/test_mutate.py", "conftest.py")}
    return root, sha, env, deploy_probe(tmp_path / "probe"), measured


@needs_312
class TestRunSelector:
    def test_complete_run(self, product, tmp_path):
        root, sha, env, probe, measured = product
        run = run_selector(env, root, sha, probe, tmp_path / "w", "tests/test_a.py::test_a", ["pkg/mod.py"], measured, 120)
        assert run["result"] == "complete" and run["outcome"] == "passed"
        assert run["product_lines"] == [{"file": "pkg/mod.py", "lines": [2]}] and run["product_line_count"] == 1

    def test_mutated_measured_file_is_an_error_run(self, product, tmp_path):  # Review Focus 3
        root, sha, env, probe, measured = product
        run = run_selector(env, root, sha, probe, tmp_path / "w", "tests/test_mutate.py::test_m", ["pkg/mod.py"], measured, 120)
        assert run["result"] == "error" and run["reason"] == "runner"
        assert run["mutated_paths"] == ["pkg/mod.py"]
        reset_checkout(root, sha)
        assert (root / "pkg/mod.py").read_bytes() == measured["pkg/mod.py"]

    def test_absent_selector_is_response_level(self, product, tmp_path):
        root, sha, env, probe, measured = product
        with pytest.raises(CriteriaError) as raised:
            run_selector(env, root, sha, probe, tmp_path / "w", "tests/test_a.py::test_gone", ["pkg/mod.py"], measured, 120)
        assert raised.value.kind is ErrorKind.SELECTOR_ABSENT

    def test_timeout_is_an_error_run(self, product, tmp_path):
        root, sha, env, probe, measured = product
        (root / "tests/test_a.py").write_text("import time\n\n\ndef test_a():\n    time.sleep(30)\n")
        _git(root, "commit", "-qam", "slow")
        sha2 = _git(root, "rev-parse", "HEAD")
        measured = {**measured, "tests/test_a.py": (root / "tests/test_a.py").read_bytes()}
        run = run_selector(env, root, sha2, probe, tmp_path / "w", "tests/test_a.py::test_a", ["pkg/mod.py"], measured, 3)
        assert run["result"] == "error" and run["timed_out"] is True
```

- [ ] **Step 2: RED** — `uv run pytest tests/test_criteria_run.py -q` → ImportError.

- [ ] **Step 3: Implement** — `function_body_lines` in `criteria_tokens.py`:

```python
def function_body_lines(source: str) -> frozenset[int]:
    """Lines inside any function or method body — devtools' rule (criteria_close._function_lines):
    for every `def`/`async def` anywhere, `body[0].lineno` through `end_lineno`."""
    lines: set[int] = set()
    for node in ast.walk(ast.parse(_without_bom(source))):
        if isinstance(node, _FUNCTIONS) and node.body:
            lines.update(range(node.body[0].lineno, (node.end_lineno or node.lineno) + 1))
    return frozenset(lines)
```

`src/spec_runner/criteria_run.py`:

```python
"""One isolated invocation of one selector → one run object (#603 design §3.6-3.7, §4)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .criteria_aggregate import run_outcome
from .criteria_contract import CriteriaError, ErrorKind
from .criteria_inventory import PROBE_MODULE
from .criteria_probe import MANIFEST_ENV, MODE_ENV, PARENT_ENV, PRODUCT_FILES_ENV
from .criteria_tokens import function_body_lines
from .criteria_workspace import Environment, child_env, distribution_args


def reset_checkout(checkout: Path, sha: str) -> None:
    """Back to exactly `sha`: tracked files restored, everything untracked removed."""
    for args in (("reset", "-q", "--hard", sha), ("clean", "-q", "-ffdx")):
        subprocess.run(["git", *args], cwd=checkout, check=True, capture_output=True)


def _error(detail: str, **observed: object) -> dict[str, object]:
    return {"result": "error", "reason": "runner", "detail": detail, **observed}


def run_selector(
    env: Environment, checkout: Path, sha: str, probe_dir: Path, work: Path, node_id: str,
    product_files: Sequence[str], measured: Mapping[str, bytes], timeout: float,
) -> dict[str, object]:
    """Run `node_id` alone in a fresh pytest process; a complete or an error run."""
    reset_checkout(checkout, sha)
    work.mkdir(parents=True, exist_ok=True)
    invocation = Path(tempfile.mkdtemp(prefix="run-", dir=work))
    manifest_path, product_json = invocation / "run.json", invocation / "product.json"
    product_json.write_text(json.dumps([str((checkout / p).resolve()) for p in product_files]))
    tmp = invocation / "tmp"
    tmp.mkdir()
    variables = {
        PARENT_ENV: str(os.getpid()), MODE_ENV: "run", MANIFEST_ENV: str(manifest_path),
        PRODUCT_FILES_ENV: str(product_json), "TMPDIR": str(tmp),
    }
    argv = [str(env.python), "-m", "pytest", "-p", PROBE_MODULE, *distribution_args(env), "-q", node_id]
    try:
        done = subprocess.run(argv, cwd=checkout, env=child_env(probe_dir, variables),
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return _error(f"{node_id} exceeded {timeout:.0f}s", timed_out=True)
    finally:
        mutated = [p for p, data in measured.items() if _bytes(checkout / p) != data]
    if mutated:
        return _error("measured-files-mutated", exit_status=done.returncode, mutated_paths=sorted(mutated))
    manifest = _read(manifest_path)
    shutil.rmtree(invocation, ignore_errors=True)
    if manifest is None or manifest.get("monitoring_error"):
        why = (manifest or {}).get("monitoring_error") or "no complete run manifest"
        return _error(str(why), exit_status=done.returncode)
    if manifest.get("distributed"):
        raise CriteriaError(ErrorKind.DISTRIBUTED_EXECUTION, f"{node_id} ran outside the probe's process")
    if manifest.get("collected") != [node_id]:
        raise CriteriaError(ErrorKind.SELECTOR_ABSENT, f"{node_id} collected {manifest.get('collected')!r}")
    phases = manifest.get("phases") or {}
    if phases.get("teardown") not in ("passed", "failed") or phases.get("call") is None:
        return _error("the run manifest lacks a phase outcome", exit_status=done.returncode)
    lines = _body_lines(checkout, manifest.get("product_lines") or {})
    return {
        "result": "complete", "collected": [node_id], "phases": phases,
        "outcome": run_outcome(phases), "product_lines": lines,
        "product_line_count": sum(len(entry["lines"]) for entry in lines),
        "process_operations": list(manifest.get("process_operations") or []),
    }


def _bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("complete") is True and data.get("mode") == "run" else None


def _body_lines(checkout: Path, raw: Mapping[str, list[int]]) -> list[dict[str, object]]:
    base = checkout.resolve()
    out: list[dict[str, object]] = []
    for path, lines in raw.items():
        rel = Path(path).resolve().relative_to(base).as_posix()
        body = function_body_lines((checkout / rel).read_text(encoding="utf-8", errors="replace"))
        kept = sorted(set(lines) & body)
        if kept:
            out.append({"file": rel, "lines": kept})
    return sorted(out, key=lambda e: str(e["file"]))
```

(`criteria_aggregate.run_outcome` arrives in Task 3; implement Task 3's `run_outcome` first if executing strictly in order, or write Task 3 before Task 2 — the plan's order is for reading.)

- [ ] **Step 4: GREEN** — `uv run pytest tests/test_criteria_run.py -q` → PASS; mypy/ruff clean.
- [ ] **Step 5: Commit** — `feat(#603): one isolated selector run with reset, timeout and mutation check`

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

**Interfaces — Produces:** `measure(project_root: Path, request_data: object, *, selector_timeout: float, timeout: float, version: str) -> tuple[int, dict[str, object]]` — the exit code and the one response document.

Order (each step's output becomes an `established` field on a later failure): parse request → `check_origin` → temp workspace → `clone_at` → `sync_environment` (→ `environment`) → `deploy_probe` → `collect` (→ `test_files`, `test_items`) → `read_declaration` + `resolve_roots` + `check_overlap` (→ `product_roots`) → measured bytes (roots ∪ test_files, read from the clean checkout) → `content_sha256` → `select` → for every distinct selector, two `run_selector` calls → `selector_status`, `beh_status` → answer. A shared deadline: before each step and each run, `time.monotonic() > deadline` raises `CriteriaError(TIMEOUT)`. The workspace is removed in `finally`.

- [ ] **Step 1: Failing tests** — with the pipeline's heavy steps monkeypatched (the real ones are covered in B2a and Task 2, and end to end in Task 6):
  - a request that fails parsing → `(2, error)` with no `request` field;
  - `CriteriaError(PRODUCT_ROOTS_UNDECLARED)` raised after sync → `(3, error)` carrying `environment` and not `beh`;
  - the deadline passing between runs → `(2, error kind timeout)`, no `beh` (Review Focus 4);
  - a happy path with one traced and one no-test BEH → `(0, answer)` that validates against `response.schema.json`;
  - each document validates against the schema (`Draft7Validator`).
- [ ] **Step 2: RED**; **Step 3: Implement** `measure` exactly in the order above; the error document is `{"protocol": 1, "request": raw (when parsed), "spec_runner_version": version, "error": {"kind", "retryable", "detail"}, **established}`; the answer as design §4. Selectors run once per distinct `node_id` even when several BEHs share one; each BEH's `selectors` list repeats that result.
- [ ] **Step 4: GREEN**; **Step 5: Commit** — `feat(#603): the measurement pipeline, deadline and response documents`

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
| `os.fork()` child calling the product (Review Focus 2) | `no-product-execution` (`os.fork` in operations) |
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
  - `@pytest.mark.slow` end to end: a git repo with `uv.lock` (pytest in a dev group), `criteria.product_roots`, a traced BEH, a docstring-reader BEH, a Must with no test → exit 0, statuses `traced` / `no-product-execution` / `no-test`, the document validates, `content_sha256` recomputes from §6.1 over the checkout's bytes, and `test_items` agree with every selector's definition.
- [ ] **Step 2: RED**; **Step 3: Implement** — flags `--criteria`, `--request PATH`, `--selector-timeout SECONDS` (default 300), `--timeout SECONDS` (default 3600); `cmd_verify` branches to `criteria_measure.measure(config.project_root, …, version=__version__)`, prints `json.dumps(doc)`, exits with the code. No other `verify` behaviour changes.
- [ ] **Step 4: GREEN** (fast and slow); **Step 5: Commit** — `feat(#603): spec-runner verify --criteria (criteria-closure/v1)`

---

### Task 7: Docs, CHANGELOG, TODO, the gate

- `CLAUDE.md`: rows for `criteria_run.py`, `criteria_aggregate.py`, `criteria_measure.py`; the `verify --criteria` line in *CLI entry points*; test files in the Testing list.
- `CHANGELOG.md` `[Unreleased]` › Added: the command, its contract (`schemas/criteria-closure/v1/`), exit codes, CPython ≥ 3.12 product environments, the new workflow.
- `TODO.md`: `B2b` checked; the release step names X and `schemas/criteria-closure/v1/min-spec-runner.env` (`MIN_SPEC_RUNNER_VERSION=<X>`) written in the release PR — not here.
- Full gate: format, ruff, mypy, changelog links, fast and slow suites, and the bench workflow green on the PR. Then one local review round (`--max-diff-files` raised explicitly if needed), PR, acceptance review, merge on approve with green checks.
- After merge: a comment in devtools#491 (or their `criteria-closure-v1-signoff` thread) that B2 is in `master` at `<sha>`, unreleased; the release follows as X.
