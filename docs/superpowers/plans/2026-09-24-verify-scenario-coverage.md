# Verify-first Scenario Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `verify_first` task may declare `**Scenarios:** BEH-09, BEH-10`; its live entry run is then refused — terminally, before any paid call — when the verify group's files at the judged commit do not carry every declared scenario id, and `validate` warns early about the same gap in the working tree.

**Architecture:** Parsing lives in `task.py` beside `**Verifies:**` (`Task.scenarios` / `Task.scenarios_error`). A new small module `scenarios.py` owns the whole coverage question — the token pattern, which files a group names, reading a file at a commit, and the refusal. `execution._run_verify_first_phase` asks it once, right after the entry run's verdict and before it returns to the caller, so the caller's existing terminal-refusal path (record attempt, `TERMINAL_REFUSAL`, no retry) does the rest. `validate._validate_verify_first_declarations` asks the same module against the working tree and only warns.

**Tech Stack:** Python 3.11+, git CLI, pytest (`uv run pytest`), ruff (line length 100), mypy.

**Spec:** `docs/superpowers/specs/2026-09-23-verify-scenario-coverage-design.md` — read it before starting; section numbers below refer to it.

## Global Constraints

- Field line: `**Scenarios:** BEH-09, BEH-10, BEH-09a` — one line, comma-separated; each id must fullmatch `[A-Z]+-\d+[a-z]?`. Stored in declared order on `Task.scenarios: list[str] | None`; `None` when the line is absent.
- Present-but-empty line, an empty item, or an id of another shape → `validate` **error** quoting the line (never guessed, never partially accepted).
- Declared on a task whose resolved mode is not `verify_first` → `validate` **warning** ("not checked outside verify_first").
- Coverage token: `(?<![A-Za-z0-9_])<re.escape(id)>(?![A-Za-z0-9_])`. `BEH-09` must **not** match `BEH-091`, `BEH-09a`, `BEH-09A`, `BEH-09_extra`, `xBEH-09`, `TestBEH09`.
- Coverage is per **file** of the declared group (`**Verifies:**` elements → their file paths), every declared scenario must be covered by at least one file.
- Enforcement: in `_run_verify_first_phase`, after the entry run reached `green` or `test_failure`, before the group freeze and before any paid call (implementation or RED authoring). Files are read with `git show <sha>:./<path>` at the entry run's commit — never the working tree.
- Uncovered → `Refusal(kind=RefusalKind.POLICY, terminal=True)` naming each uncovered scenario and the files searched. A file unreadable at the commit → `Refusal(kind=RefusalKind.INSTRUMENT)` naming the path.
- A task without `**Scenarios:**` (`scenarios is None`) → byte-for-byte today's behaviour; every existing verify-first test stays green unmodified.
- A missing file (file target **or** node id's file) in the working tree stays a `validate` **warning** (FR-07/BEH-09). `validate` never invokes a runner.
- No state-DB, `--json-result` or `schemas/` change. CHANGELOG under Unreleased, **minor**.
- Repo rules: TDD (failing test first), `uv run ruff format . && uv run ruff check .`, `uv run mypy src`, full `uv run pytest tests/ -q -m "not slow"` before the PR. Tests never reach a real agent (the conftest guard; spy `tdd._run_agent` and mock `execution._run_agent_process`).

## Review Focus

1. **The same scenario declared twice** (`BEH-09, BEH-09`) — accepted, and a refusal names it once, not twice. Test in Task 2.
2. **Two node ids in the same file** — the file is searched (and listed in the refusal) once. Test in Task 2.
3. **A group file with non-UTF-8 bytes** — decoded with replacement, never a `UnicodeDecodeError` crashing the run. Test in Task 2.
4. **A `test_failure` entry run with an uncovered scenario** — refused before RED authoring (the first paid call on that path), not only on the green path. Test in Task 3.
5. **`**Scenarios:**` on a verify_first task whose `**Verifies:**` line is itself unparseable** — `validate` reports the `Verifies` error and does not crash or add a coverage warning built on a group it could not read. Test in Task 4.

---

## File Structure

- `src/spec_runner/task.py` — `SCENARIOS` regex, `SCENARIO_ID` regex, `Task.scenarios`, `Task.scenarios_raw`, `Task.scenarios_error`, parse branch.
- `src/spec_runner/validate.py` — `validate_task_fields` reports `scenarios_error`; `_validate_verify_first_declarations` adds the three warnings.
- `src/spec_runner/scenarios.py` (create) — `scenario_pattern`, `group_files`, `uncovered_scenarios`, `read_at_commit`, `coverage_refusal`.
- `src/spec_runner/execution.py` — one call in `_run_verify_first_phase`.
- Tests: `tests/test_scenario_coverage.py` (create; Tasks 1, 2, 4), `tests/test_scenario_coverage_at_entry.py` (create; Task 3).
- Docs: `spec/FORMAT.md` (field + contract limits), `CHANGELOG.md`, `CLAUDE.md` (module row + test list).

---

### Task 1: Parse `**Scenarios:**` and refuse a malformed line

**Files:**
- Modify: `src/spec_runner/task.py` (regexes near line 104; `Task` fields near line 282; parse loop near line 476)
- Modify: `src/spec_runner/validate.py:240-247` (`validate_task_fields`)
- Test: `tests/test_scenario_coverage.py` (create)

**Interfaces:**
- Produces:
  - `task.SCENARIO_ID = re.compile(r"[A-Z]+-\d+[a-z]?")` (used with `fullmatch`)
  - `Task.scenarios: list[str] | None = None` — declared order, `None` when absent
  - `Task.scenarios_raw: str | None = None` — the line as written
  - `Task.scenarios_error: str | None = None` — named refusal; `scenarios` stays `None` when set

- [ ] **Step 1: Write the failing tests**

```python
"""#402: `**Scenarios:**` — parsing, the coverage core and `validate`.

Spec: docs/superpowers/specs/2026-09-23-verify-scenario-coverage-design.md
"""

from __future__ import annotations

from pathlib import Path

from spec_runner.task import parse_tasks
from spec_runner.validate import validate_task_fields

HEADER = "### TASK-001: t\n\U0001f7e0 P1 | ⬜ TODO\nEst: 1d\n"


def _tasks(tmp_path: Path, body: str):
    path = tmp_path / "tasks.md"
    path.write_text(HEADER + body)
    return parse_tasks(path)


class TestParsing:
    def test_absent_line_is_none(self, tmp_path):
        (task,) = _tasks(tmp_path, "**Mode:** verify_first\n")
        assert task.scenarios is None
        assert task.scenarios_error is None

    def test_ids_kept_in_declared_order(self, tmp_path):
        (task,) = _tasks(tmp_path, "**Scenarios:** BEH-10, BEH-09, BEH-09a\n")
        assert task.scenarios == ["BEH-10", "BEH-09", "BEH-09a"]
        assert task.scenarios_error is None

    def test_line_after_a_verifies_block_is_still_read(self, tmp_path):
        (task,) = _tasks(
            tmp_path,
            "**Verifies:**\n- tests/test_a.py::test_x\n**Scenarios:** BEH-09\n",
        )
        assert task.verifies == ["tests/test_a.py::test_x"]
        assert task.scenarios == ["BEH-09"]


class TestMalformedLineIsAValidateError:
    def _errors(self, tmp_path, line: str) -> str:
        tasks = _tasks(tmp_path, line + "\n")
        assert tasks[0].scenarios is None
        return "\n".join(validate_task_fields(tasks).errors)

    def test_empty_line(self, tmp_path):
        joined = self._errors(tmp_path, "**Scenarios:**")
        assert "TASK-001" in joined and "**Scenarios:**" in joined and "empty" in joined

    def test_empty_item(self, tmp_path):
        joined = self._errors(tmp_path, "**Scenarios:** BEH-09,")
        assert "BEH-09," in joined  # the line is quoted back

    def test_wrong_shape(self, tmp_path):
        for bad in ("beh-09", "BEH09", "BEH-09A", "BEH-09-a", "BEH-09; BEH-10", "—"):
            joined = self._errors(tmp_path, f"**Scenarios:** {bad}")
            assert "TASK-001" in joined, bad
            assert bad in joined, bad
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scenario_coverage.py -q`
Expected: FAIL — `AttributeError: 'Task' object has no attribute 'scenarios'`.

- [ ] **Step 3: Implement the parser**

In `task.py`, beside `VERIFIES`:

```python
# #402: the scenarios a verify_first group must carry. One line,
# comma-separated, stored as written and judged by `validate` — never
# guessed. The id shape is BEH-style with the one suffix seen in practice
# (`BEH-09a`); qualified ids (`<ws>#BEH-09`) are deliberately not accepted.
SCENARIOS = re.compile(r"\*\*Scenarios:\*\*(.*)$")
SCENARIO_ID = re.compile(r"[A-Z]+-\d+[a-z]?")


def _parse_scenarios(line: str, declared: str) -> tuple[list[str] | None, str | None]:
    """(ids, None) for a valid line, (None, refusal) otherwise."""
    items = [item.strip() for item in declared.split(",")]
    if items == [""]:
        return None, f"**Scenarios:** line is empty. Declared line: {line!r}"
    bad = [item for item in items if not SCENARIO_ID.fullmatch(item)]
    if bad:
        return None, (
            f"**Scenarios:** {bad!r} not of the form BEH-09 / BEH-09a "
            f"(comma-separated [A-Z]+-<digits>[a-z]). Declared line: {line!r}"
        )
    return items, None
```

`Task` fields, after `negative_control_error`:

```python
    #: Scenario ids a verify_first group must carry (#402), in declared
    #: order; `None` when no `**Scenarios:**` line is present.
    scenarios: list[str] | None = None
    #: The `**Scenarios:**` line as written, for quoting.
    scenarios_raw: str | None = None
    #: Named refusal when the line is present but unusable; `scenarios` stays
    #: `None` then, the same split `verifies_error` makes.
    scenarios_error: str | None = None
```

Parse loop, immediately before `verifies_match = VERIFIES.search(line)`:

```python
        scenarios_match = SCENARIOS.search(line)
        if scenarios_match:
            current_task.scenarios_raw = line
            current_task.scenarios, current_task.scenarios_error = _parse_scenarios(
                line, scenarios_match.group(1).strip()
            )
            continue
```

In `validate.validate_task_fields`, right after the `verifies_error` block:

```python
        # #402: same contract as `verifies_error` — marked by `parse_tasks`,
        # named here, quoted, no traceback.
        if task.scenarios_error:
            result.errors.append(f"{task.id}: {task.scenarios_error}")
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_scenario_coverage.py tests/test_task.py tests/test_verify_first_declaration.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/task.py src/spec_runner/validate.py tests/test_scenario_coverage.py
git commit -m "task: parse **Scenarios:** and refuse a malformed line (#402)"
```

---

### Task 2: The coverage core — `scenarios.py`

**Files:**
- Create: `src/spec_runner/scenarios.py`
- Test: `tests/test_scenario_coverage.py` (append)

**Interfaces:**
- Consumes: `Task.scenarios`, `Task.verifies` (Task 1); `phases.Refusal`, `phases.RefusalKind`; `tdd_runners.normalise_path`.
- Produces:
  - `scenario_pattern(scenario: str) -> re.Pattern[str]`
  - `group_files(verifies: Sequence[str]) -> list[PurePosixPath]` — each element's file, first-seen order, deduplicated
  - `uncovered_scenarios(scenarios: Sequence[str], texts: Iterable[str]) -> list[str]` — declared order, deduplicated
  - `read_at_commit(root: Path, sha: str, path: PurePosixPath) -> str | None` — `None` when git cannot show it
  - `coverage_refusal(task: Task, root: Path, sha: str) -> Refusal | None`

- [ ] **Step 1: Write the failing tests** (append to `tests/test_scenario_coverage.py`)

```python
import subprocess
from pathlib import PurePosixPath

import pytest

from spec_runner.phases import RefusalKind
from spec_runner.scenarios import (
    coverage_refusal,
    group_files,
    read_at_commit,
    uncovered_scenarios,
)
from spec_runner.task import Task


class TestTokenBoundaries:
    @pytest.mark.parametrize(
        "text", ["BEH-09:", "(BEH-09)", "BEH-09,", "kind: e2e — BEH-09", "BEH-09"]
    )
    def test_covers(self, text):
        assert uncovered_scenarios(["BEH-09"], [text]) == []

    @pytest.mark.parametrize(
        "text", ["BEH-091", "BEH-09a", "BEH-09A", "BEH-09_extra", "xBEH-09", "TestBEH09"]
    )
    def test_does_not_cover(self, text):
        assert uncovered_scenarios(["BEH-09"], [text]) == ["BEH-09"]

    def test_suffixed_id_is_its_own_token(self):
        assert uncovered_scenarios(["BEH-09a"], ["BEH-09a"]) == []
        assert uncovered_scenarios(["BEH-09a"], ["BEH-09"]) == ["BEH-09a"]

    def test_any_file_covers_and_order_is_declared(self):
        assert uncovered_scenarios(
            ["BEH-10", "BEH-09", "BEH-11"], ["BEH-09", "nothing"]
        ) == ["BEH-10", "BEH-11"]

    def test_duplicate_declaration_named_once(self):  # Review Focus 1
        assert uncovered_scenarios(["BEH-09", "BEH-09"], ["x"]) == ["BEH-09"]


class TestGroupFiles:
    def test_node_ids_and_file_targets(self):
        assert group_files(
            ["tests/a.py::T::test_x", "./tests/b.py", "tests/a.py::test_y[1,2]"]
        ) == [PurePosixPath("tests/a.py"), PurePosixPath("tests/b.py")]  # Review Focus 2

    def test_exunit_path_line(self):
        assert group_files(["test/x_test.exs:12"]) == [PurePosixPath("test/x_test.exs")]


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests" / "test_a.py").write_text('"""kind: e2e — BEH-09"""\n')
    (root / "tests" / "test_bin.py").write_bytes(b"# \xff\xfe BEH-10\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _task(verifies, scenarios):
    return Task(
        id="TASK-001", name="t", priority="p1", status="todo", estimate="1h",
        execution_mode="verify_first", verifies=verifies, scenarios=scenarios,
    )


class TestAtCommit:
    def test_read_at_commit(self, repo):
        sha = _git(repo, "rev-parse", "HEAD")
        assert "BEH-09" in (read_at_commit(repo, sha, PurePosixPath("tests/test_a.py")) or "")
        assert read_at_commit(repo, sha, PurePosixPath("tests/missing.py")) is None

    def test_non_utf8_file_does_not_crash(self, repo):  # Review Focus 3
        sha = _git(repo, "rev-parse", "HEAD")
        task = _task(["tests/test_bin.py::test_x"], ["BEH-10"])
        assert coverage_refusal(task, repo, sha) is None

    def test_no_scenarios_is_no_check(self, repo):
        sha = _git(repo, "rev-parse", "HEAD")
        assert coverage_refusal(_task(["tests/missing.py::t"], None), repo, sha) is None

    def test_uncovered_is_terminal_policy_naming_scenario_and_files(self, repo):
        sha = _git(repo, "rev-parse", "HEAD")
        refusal = coverage_refusal(
            _task(["tests/test_a.py::test_x"], ["BEH-09", "BEH-10"]), repo, sha
        )
        assert refusal is not None
        assert refusal.kind is RefusalKind.POLICY and refusal.terminal
        assert "BEH-10" in refusal and "BEH-09" not in refusal.split("uncovered")[1]
        assert "tests/test_a.py" in refusal and sha[:12] in refusal

    def test_working_tree_label_does_not_count(self, repo):
        sha = _git(repo, "rev-parse", "HEAD")
        (repo / "tests" / "test_a.py").write_text('"""BEH-09 BEH-10"""\n')
        refusal = coverage_refusal(
            _task(["tests/test_a.py::test_x"], ["BEH-10"]), repo, sha
        )
        assert refusal is not None and "BEH-10" in refusal

    def test_unreadable_file_is_instrument(self, repo):
        sha = _git(repo, "rev-parse", "HEAD")
        refusal = coverage_refusal(_task(["tests/missing.py::t"], ["BEH-09"]), repo, sha)
        assert refusal is not None
        assert refusal.kind is RefusalKind.INSTRUMENT
        assert "tests/missing.py" in refusal
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scenario_coverage.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'spec_runner.scenarios'`.

- [ ] **Step 3: Implement `scenarios.py`**

```python
"""Verify-first scenario coverage (#402).

A `verify_first` task that declares `**Scenarios:**` must run a group whose
files carry every declared id as a whole token. The contract is deliberately
narrow (design §4):

- coverage is per **file**, not per test function;
- ids are unqualified and repeat across workstreams, so a foreign file that
  carries its own `BEH-09` satisfies the check. It catches a group that
  claims **nothing** about the task's scenarios, not one that claims them
  falsely;
- a name-mangled form (`TestBEH09`) is not a label: accepting it would be a
  guess, and a guessed match is the failure this check exists to stop.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from .phases import Refusal, RefusalKind
from .tdd_runners import normalise_path

if TYPE_CHECKING:
    from .task import Task

_LINE_SUFFIX = re.compile(r":\d+$")


def scenario_pattern(scenario: str) -> re.Pattern[str]:
    """`scenario` as a whole token: no letter, digit or `_` on either side."""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(scenario)}(?![A-Za-z0-9_])")


def group_files(verifies: Sequence[str]) -> list[PurePosixPath]:
    """The file each declared group element names, first-seen order, once.

    Textual, not an adapter parse: a file target is judged at a commit, and
    the adapter's own parser checks the working tree. `path::node` (pytest)
    and `path:line` (ExUnit) both reduce to `path`.
    """
    files: list[PurePosixPath] = []
    for raw in verifies:
        path = normalise_path(_LINE_SUFFIX.sub("", raw.strip().split("::", 1)[0]))
        if str(path) and path not in files:
            files.append(path)
    return files


def uncovered_scenarios(scenarios: Sequence[str], texts: Iterable[str]) -> list[str]:
    """Declared scenarios no text carries, in declared order, each once."""
    corpus = list(texts)
    missing: list[str] = []
    for scenario in dict.fromkeys(scenarios):
        pattern = scenario_pattern(scenario)
        if not any(pattern.search(text) for text in corpus):
            missing.append(scenario)
    return missing


def read_at_commit(root: Path, sha: str, path: PurePosixPath) -> str | None:
    """`path` (relative to `root`) as committed in `sha`; None if git cannot show it."""
    shown = subprocess.run(
        ["git", "show", f"{sha}:./{path}"], cwd=root, capture_output=True
    )
    if shown.returncode != 0:
        return None
    return shown.stdout.decode("utf-8", errors="replace")


def coverage_refusal(task: Task, root: Path, sha: str) -> Refusal | None:
    """Refuse a declared scenario the group does not carry at `sha` (§5).

    Terminal POLICY: the same commit gives the same answer, so a retry would
    only repeat it. A file git cannot show at `sha` is an INSTRUMENT refusal —
    the entry run should already have failed on it.
    """
    if task.scenarios is None:
        return None
    files = group_files(task.verifies or [])
    texts: list[str] = []
    for path in files:
        text = read_at_commit(root, sha, path)
        if text is None:
            return Refusal(
                f"scenario coverage: {path} cannot be read at {sha[:12]}",
                RefusalKind.INSTRUMENT,
            )
        texts.append(text)
    missing = uncovered_scenarios(task.scenarios, texts)
    if not missing:
        return None
    searched = ", ".join(str(path) for path in files)
    return Refusal(
        f"verify-first group at {sha[:12]} leaves declared scenarios uncovered: "
        f"{', '.join(missing)} (searched: {searched}); a label is the id as a "
        "whole token, e.g. in a docstring",
        RefusalKind.POLICY,
        terminal=True,
    )
```

(If `Refusal`'s constructor differs from `Refusal(message, kind, terminal=...)`, match `phases.py` and ledger a ruling.)

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_scenario_coverage.py -q && uv run mypy src/spec_runner/scenarios.py`
Expected: PASS; mypy `Success`.

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/scenarios.py tests/test_scenario_coverage.py
git commit -m "scenarios: per-file coverage of declared scenario ids at a commit (#402)"
```

---

### Task 3: Enforce at the live entry run, before any paid call

**Files:**
- Modify: `src/spec_runner/execution.py:423-535` (`_run_verify_first_phase`)
- Test: `tests/test_scenario_coverage_at_entry.py` (create)

**Interfaces:**
- Consumes: `scenarios.coverage_refusal(task, root, sha) -> Refusal | None` (Task 2); `Task.scenarios` (Task 1).
- Produces: nothing new; `_run_verify_first_phase` may now return `(Refusal(POLICY, terminal=True), result)` on a green or test_failure run. The caller at `execution.py:769-788` already records the attempt and returns `"TERMINAL_REFUSAL"` for a terminal refusal, and `run_with_retries` stops on it.

- [ ] **Step 1: Write the failing tests**

```python
"""#402 §5/§8: an uncovered declared scenario refuses at the live entry run —
terminal, before any paid call, on the commit and not the working tree.

Spec: docs/superpowers/specs/2026-09-23-verify-scenario-coverage-design.md
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from spec_runner import tdd
from spec_runner.config import ExecutorConfig
from spec_runner.execution import run_with_retries
from spec_runner.executor import execute_task
from spec_runner.runner import CliInvocation
from spec_runner.state import ExecutorState
from spec_runner.task import Task

FOREIGN = 'def test_it():\n    """kind: e2e — BEH-03 (another workstream)"""\n    assert True\n'
OWN = 'def test_it():\n    """kind: e2e — BEH-09"""\n    assert True\n'
FAILING = 'def test_it():\n    """kind: e2e — BEH-07"""\n    assert False\n'


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests" / "test_group.py").write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _cfg(root: Path, max_retries: int = 1) -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        test_command="python -m pytest",
        max_retries=max_retries,
        retry_delay_seconds=0,
        create_git_branch=False,
        run_tests_on_done=False,
        auto_commit=True,
        run_review=False,
        callback_url="",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(scenarios: list[str] | None) -> Task:
    return Task(
        id="TASK-001", name="t", priority="p1", status="todo", estimate="1h",
        execution_mode="verify_first", verifies=["tests/test_group.py::test_it"],
        scenarios=scenarios,
    )


@pytest.fixture
def paid(monkeypatch):
    """Both paid seams, spied: the implementation call and RED authoring."""
    red_calls: list[str] = []

    def _spy(config, prompt, **kwargs):
        red_calls.append(prompt)
        return tdd.AgentCall(text="TDD_SELECTOR: tests/test_group.py::test_it")

    monkeypatch.setattr(tdd, "_run_agent", _spy)
    impl = MagicMock(
        return_value=MagicMock(stdout="TASK_COMPLETE", stderr="", returncode=0)
    )
    with (
        patch("spec_runner.execution._run_agent_process", impl),
        patch("spec_runner.execution.update_task_status"),
        patch("spec_runner.execution.log_progress"),
        patch(
            "spec_runner.execution.build_cli_invocation",
            return_value=CliInvocation(["echo", "hi"], "text"),
        ),
        patch("spec_runner.execution.build_task_prompt", return_value="p"),
        patch(
            "spec_runner.execution.post_done_hook",
            return_value=(True, None, "skipped", "", False),
        ),
        patch("spec_runner.execution.pre_start_hook", return_value=True),
    ):
        yield impl, red_calls


class TestForeignGreenGroupIsRefused:
    """kind: e2e — the issue's shape: an existing, green, foreign file."""

    def test_refused_terminally_before_any_paid_call(self, tmp_path, paid):
        impl, red_calls = paid
        cfg = _cfg(_repo(tmp_path, FOREIGN))
        state = ExecutorState(cfg)

        assert execute_task(_task(["BEH-09"]), cfg, state) == "TERMINAL_REFUSAL"

        impl.assert_not_called()
        assert red_calls == []
        error = state.get_task_state("TASK-001").last_error or ""
        assert "BEH-09" in error and "tests/test_group.py" in error
        assert state.get_task_state("TASK-001").attempts[-1].cost_usd == 0.0

    def test_not_retried(self, tmp_path, paid):
        cfg = _cfg(_repo(tmp_path, FOREIGN), max_retries=3)
        state = ExecutorState(cfg)

        assert run_with_retries(_task(["BEH-09"]), cfg, state) is False
        assert state.get_task_state("TASK-001").attempt_count == 1


class TestCoveredGroupProceeds:
    def test_label_in_docstring_proceeds(self, tmp_path, paid):
        impl, _ = paid
        cfg = _cfg(_repo(tmp_path, OWN))
        assert execute_task(_task(["BEH-09"]), cfg, ExecutorState(cfg)) is not False
        impl.assert_called()

    def test_partial_coverage_names_only_the_missing(self, tmp_path, paid):
        cfg = _cfg(_repo(tmp_path, OWN))
        state = ExecutorState(cfg)
        assert execute_task(_task(["BEH-09", "BEH-10"]), cfg, state) == "TERMINAL_REFUSAL"
        error = state.get_task_state("TASK-001").last_error or ""
        assert "BEH-10" in error
        assert "BEH-09" not in error.split("uncovered")[1].split("(searched")[0]

    def test_no_scenarios_line_is_todays_path(self, tmp_path, paid):
        impl, _ = paid
        cfg = _cfg(_repo(tmp_path, FOREIGN))
        assert execute_task(_task(None), cfg, ExecutorState(cfg)) is not False
        impl.assert_called()


class TestCommitNotWorkingTree:
    def test_uncommitted_label_does_not_count(self, tmp_path, paid):
        impl, _ = paid
        root = _repo(tmp_path, FOREIGN)
        (root / "tests" / "test_group.py").write_text(OWN)  # not committed
        cfg = _cfg(root)
        assert execute_task(_task(["BEH-09"]), cfg, ExecutorState(cfg)) == "TERMINAL_REFUSAL"
        impl.assert_not_called()


class TestRedEntryRunIsCheckedToo:  # Review Focus 4
    def test_test_failure_entry_refused_before_red_authoring(self, tmp_path, paid):
        impl, red_calls = paid
        cfg = _cfg(_repo(tmp_path, FAILING))
        state = ExecutorState(cfg)
        assert execute_task(_task(["BEH-09"]), cfg, state) == "TERMINAL_REFUSAL"
        assert red_calls == []
        impl.assert_not_called()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scenario_coverage_at_entry.py -q`
Expected: the refusal tests FAIL (`execute_task` returns a truthy result / `impl` was called); `test_label_in_docstring_proceeds` and `test_no_scenarios_line_is_todays_path` PASS already. If `test_uncommitted_label_does_not_count` fails at setup because the dirty tree is refused earlier, ledger it and commit the foreign file under a second path instead.

- [ ] **Step 3: Implement the check**

In `_run_verify_first_phase`, replace the two verdict branches:

```python
    if result.passed:
        reporter.record(PhaseOutcome.PASS, detail)
        return None, result
    if result.ran:
        reporter.record(PhaseOutcome.UNEXPECTED_FAIL, detail)
        log_progress(f"\U0001f7e5 verify-first: {detail}", task.id)
        return None, result
```

with:

```python
    if result.ran:
        if result.passed:
            reporter.record(PhaseOutcome.PASS, detail)
        else:
            reporter.record(PhaseOutcome.UNEXPECTED_FAIL, detail)
            log_progress(f"\U0001f7e5 verify-first: {detail}", task.id)
        # #402 §5: a run that reached a verdict must also be ABOUT the task's
        # declared scenarios. Asked here — after the verdict, before the
        # freeze and before the first paid call on either path (the
        # implementation pass on green, RED authoring on test_failure) —
        # against the judged commit, never the working tree.
        return coverage_refusal(task, config.project_root, result.sha), result
```

and add `from .scenarios import coverage_refusal` to the module imports (top-level, alphabetical per ruff).

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_scenario_coverage_at_entry.py tests/test_verify_first_cost.py tests/test_verify_first_mode.py tests/test_verify_branching.py tests/test_verify_run_order.py -q`
Expected: PASS (existing verify-first tests unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/execution.py tests/test_scenario_coverage_at_entry.py
git commit -m "execution: refuse uncovered declared scenarios at the verify entry run (#402)"
```

---

### Task 4: `validate` warnings — outside verify_first, uncovered in the tree, missing node-id file

**Files:**
- Modify: `src/spec_runner/validate.py:784-925` (`_validate_verify_first_declarations`)
- Test: `tests/test_scenario_coverage.py` (append)

**Interfaces:**
- Consumes: `scenarios.group_files`, `scenarios.uncovered_scenarios` (Task 2); `Task.scenarios` (Task 1).
- Produces: warnings only; no new errors beyond Task 1.

- [ ] **Step 1: Write the failing tests** (append)

```python
from spec_runner.validate import validate_all


def _validate(tmp_path, body: str, files: dict[str, str] | None = None):
    for rel, text in (files or {}).items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    tasks = tmp_path / "tasks.md"
    tasks.write_text(HEADER + body)
    return validate_all(tasks_file=tasks, config_file=None, project_root=tmp_path)


class TestValidateWarnings:
    def test_outside_verify_first_is_a_warning(self, tmp_path):
        result = _validate(tmp_path, "**Scenarios:** BEH-09\n")
        assert result.ok
        assert any("TASK-001" in w and "not checked outside verify_first" in w
                   for w in result.warnings)

    def test_uncovered_in_the_tree_is_a_warning(self, tmp_path):
        result = _validate(
            tmp_path,
            "**Mode:** verify_first\n**Verifies:** tests/test_a.py::test_x\n"
            "**Scenarios:** BEH-09, BEH-10\n",
            {"tests/test_a.py": '"""BEH-09"""\ndef test_x():\n    pass\n'},
        )
        assert result.ok
        joined = "\n".join(result.warnings)
        assert "BEH-10" in joined and "uncovered" in joined
        assert "BEH-09," not in joined

    def test_covered_in_the_tree_is_silent(self, tmp_path):
        result = _validate(
            tmp_path,
            "**Mode:** verify_first\n**Verifies:** tests/test_a.py::test_x\n"
            "**Scenarios:** BEH-09\n",
            {"tests/test_a.py": '"""BEH-09"""\ndef test_x():\n    pass\n'},
        )
        assert not any("uncovered" in w for w in result.warnings)

    def test_missing_node_id_file_is_a_warning(self, tmp_path):
        result = _validate(
            tmp_path, "**Mode:** verify_first\n**Verifies:** tests/nope.py::test_x\n"
        )
        assert result.ok
        assert any("tests/nope.py" in w and "does not exist" in w for w in result.warnings)

    def test_unparseable_verifies_does_not_add_coverage_noise(self, tmp_path):  # RF 5
        result = _validate(
            tmp_path,
            "**Mode:** verify_first\n**Verifies:** tests/a.py::t[a,b\n**Scenarios:** BEH-09\n",
        )
        assert any("unclosed" in e for e in result.errors)
        assert not any("uncovered" in w for w in result.warnings)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scenario_coverage.py::TestValidateWarnings -q`
Expected: 3 FAIL (outside-mode, uncovered, missing node-id file); covered-silent and unparseable PASS.

- [ ] **Step 3: Implement**

In `_validate_verify_first_declarations`, inside the `if mode != "verify_first":` branch, before its `continue`:

```python
            if task.scenarios is not None:
                result.warnings.append(
                    f"{task.id}: **Scenarios:** {task.scenarios!r} declared but the "
                    f"resolved execution mode is {mode!r} — not checked outside verify_first"
                )
```

In the per-element loop, the `not isinstance(parsed, SelectorRefusal)` branch becomes:

```python
            if not isinstance(parsed, SelectorRefusal):
                # #402 §6: a node id's file gets the same working-tree warning
                # a missing file target already gets (FR-07) — never an error.
                if not (config.project_root / str(parsed.path)).is_file():
                    result.warnings.append(
                        f"{task.id}: mode is verify_first, declared group "
                        f"{task.verifies!r} — file {str(parsed.path)!r} of {raw!r} "
                        "does not exist in the working tree yet"
                    )
                continue
```

After the loop (still inside the per-task body, at the end):

```python
        result.warnings.extend(_scenario_warnings(task, config.project_root))
```

and a module-level helper after `_validate_verify_first_declarations`:

```python
def _scenario_warnings(task: Task, root: Path) -> list[str]:
    """#402 §6: early notice of a declared scenario the group's files in the
    WORKING TREE do not carry. A warning only — the live entry run judges the
    commit and is the one that refuses. Missing files are skipped: they carry
    their own warning above."""
    from spec_runner.scenarios import group_files, uncovered_scenarios

    if not task.scenarios:
        return []
    files = [root / str(path) for path in group_files(task.verifies or [])]
    texts = [f.read_text(errors="replace") for f in files if f.is_file()]
    missing = uncovered_scenarios(task.scenarios, texts)
    if not missing:
        return []
    return [
        f"{task.id}: **Scenarios:** {', '.join(missing)} uncovered by the declared "
        f"group's files in the working tree — the live entry run will refuse "
        "unless the committed files carry them"
    ]
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_scenario_coverage.py tests/test_verify_first_validate.py tests/test_validate.py tests/test_verify_file_target_declaration.py -q`
Expected: PASS. If an existing test asserted `warnings == []` for a verify-first task with a non-existent node-id file, that test encoded the gap §6 closes: ledger a ruling and update its expectation to the new warning.

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/validate.py tests/test_scenario_coverage.py
git commit -m "validate: warn on scenarios outside verify_first, uncovered in the tree, missing node-id file (#402)"
```

---

### Task 5: Docs and the full gate

**Files:**
- Modify: `spec/FORMAT.md` (Execution Declarations example + bullets, ~line 88-125)
- Modify: `CHANGELOG.md` (Unreleased → Added)
- Modify: `CLAUDE.md` (module table row for `scenarios.py`; test list entries)

- [ ] **Step 1: FORMAT.md** — add `**Scenarios:** BEH-09, BEH-10` to the TASK-007 example and this bullet after `Verifies`:

```markdown
- `Scenarios` — optional, `verify_first` only: comma-separated scenario ids
  (`BEH-09`, `BEH-09a`) the `Verifies` group must carry. At the live entry
  run, before any paid call, every id must appear as a whole token
  (`BEH-09` does not match `BEH-091`, `BEH-09a` or `TestBEH09`) in at least
  one of the group's files **as committed**; otherwise the task is refused
  terminally, naming the uncovered ids. Coverage is per file, not per test,
  and ids are unqualified: a foreign file carrying its own `BEH-09`
  satisfies it — the check catches a group that claims nothing about the
  task's scenarios, not one that claims them falsely. `validate` only
  warns (the working tree is not the commit); an empty or malformed line is
  an error. Without the line nothing changes.
```

- [ ] **Step 2: CHANGELOG.md** — under `## [Unreleased]` → `### Added`, after the #338 entry:

```markdown
- **Verify-first scenario coverage (#402).** A `verify_first` task may
  declare `**Scenarios:** BEH-09, BEH-10`. After the live entry run reaches a
  verdict and before any paid call, every declared id must appear as a whole
  token in at least one file of the `**Verifies:**` group at the judged
  commit; otherwise the task is refused terminally (exit 1, no retry),
  naming the uncovered ids and the files searched. `validate` warns about the
  same gap in the working tree, about the field outside `verify_first`, and
  now also about a node id whose file is missing; a malformed line is an
  error. Tasks without the line are unchanged.
```

- [ ] **Step 3: CLAUDE.md** — module table row after `tdd.py`:

```markdown
| `scenarios.py` | ~110 | Verify-first scenario coverage (#402): `scenario_pattern` (whole-token, symmetric `[A-Za-z0-9_]` boundaries), `group_files` (textual `path::…`/`path:line` → path), `uncovered_scenarios`, `read_at_commit` (`git show <sha>:./<path>`, bytes decoded with replacement), `coverage_refusal` — terminal POLICY when a declared `**Scenarios:**` id is carried by no group file at the entry run's commit, INSTRUMENT when a file cannot be read there; `None` for a task without the line. Called once from `execution._run_verify_first_phase` after the verdict, before the freeze and any paid call |
```

and in the Testing paragraph add: `test_scenario_coverage.py` + `test_scenario_coverage_at_entry.py` (#402: parsing and malformed-line errors, the token-boundary table, per-file coverage at a commit and not the tree, the issue's foreign-green group refused terminally with no paid call and no retry, a red entry run refused before RED authoring, and the three `validate` warnings).

- [ ] **Step 4: Full gate**

Run: `uv run ruff format . && uv run ruff check . && uv run mypy src && uv run pytest tests/ -q -m "not slow" > .superpowers/sdd/2026-09-24-verify-scenario-coverage/suite.log 2>&1; tail -3 .superpowers/sdd/2026-09-24-verify-scenario-coverage/suite.log`
Expected: ruff clean, mypy `Success`, pytest all passed (no failures).

- [ ] **Step 5: Commit**

```bash
git add spec/FORMAT.md CHANGELOG.md CLAUDE.md
git commit -m "docs: **Scenarios:** field and its coverage contract (#402)"
```
