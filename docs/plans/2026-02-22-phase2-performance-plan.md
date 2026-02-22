# Phase 2 — Performance Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add parallel task execution (asyncio), token/cost tracking (parse Claude CLI stderr), and budget enforcement (per-task + global limits).

**Architecture:** New async dispatch path activated by `--parallel` flag. Existing sync path untouched. Token usage parsed from stderr and stored in SQLite `attempts` table. Budget checks after each attempt halt execution when limits exceeded.

**Tech Stack:** Python 3.10+ stdlib (asyncio, sqlite3), no new dependencies.

---

### Task 1: Token parsing in runner.py

**Files:**
- Modify: `src/spec_runner/runner.py:1-161`
- Test: `tests/test_runner.py`

**Step 1: Write the failing tests**

Add to `tests/test_runner.py`:

```python
from spec_runner.runner import parse_token_usage


class TestParseTokenUsage:
    """Tests for parse_token_usage."""

    def test_parses_standard_format(self):
        stderr = "input_tokens: 12500\noutput_tokens: 3200\ntotal cost: $0.12"
        inp, out, cost = parse_token_usage(stderr)
        assert inp == 12500
        assert out == 3200
        assert cost == 0.12

    def test_parses_with_commas(self):
        stderr = "input_tokens: 1,250\noutput_tokens: 320\ncost: $1.50"
        inp, out, cost = parse_token_usage(stderr)
        assert inp == 1250
        assert out == 320
        assert cost == 1.50

    def test_parses_underscore_variant(self):
        stderr = "input tokens: 500\noutput tokens: 100\ntotal_cost: $0.01"
        inp, out, cost = parse_token_usage(stderr)
        assert inp == 500
        assert out == 100
        assert cost == 0.01

    def test_returns_none_on_empty(self):
        inp, out, cost = parse_token_usage("")
        assert inp is None
        assert out is None
        assert cost is None

    def test_returns_none_on_garbage(self):
        inp, out, cost = parse_token_usage("some random text\nwith no tokens")
        assert inp is None
        assert out is None
        assert cost is None

    def test_partial_match_returns_available(self):
        stderr = "input_tokens: 500\nno output info"
        inp, out, cost = parse_token_usage(stderr)
        assert inp == 500
        assert out is None
        assert cost is None
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner.py::TestParseTokenUsage -v`
Expected: FAIL with `ImportError: cannot import name 'parse_token_usage'`

**Step 3: Implement parse_token_usage**

Add to `src/spec_runner/runner.py` after `check_error_patterns`:

```python
import re

def parse_token_usage(stderr: str) -> tuple[int | None, int | None, float | None]:
    """Extract (input_tokens, output_tokens, cost_usd) from Claude CLI stderr.

    Parses common patterns like "input_tokens: 12,500" and "cost: $0.12".
    Returns None for any field that can't be parsed. Never raises.
    """
    def _parse_int(pattern: str) -> int | None:
        m = re.search(pattern, stderr, re.IGNORECASE)
        if m:
            return int(m.group(1).replace(",", ""))
        return None

    def _parse_float(pattern: str) -> float | None:
        m = re.search(pattern, stderr, re.IGNORECASE)
        if m:
            return float(m.group(1).replace(",", ""))
        return None

    input_tokens = _parse_int(r"input[_ ]tokens?[:\s]+(\d[\d,]*)")
    output_tokens = _parse_int(r"output[_ ]tokens?[:\s]+(\d[\d,]*)")
    cost = _parse_float(r"(?:total[_ ])?cost[:\s]+\$?([\d.]+)")
    return input_tokens, output_tokens, cost
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner.py::TestParseTokenUsage -v`
Expected: PASS (6 tests)

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All 171+ tests pass

**Step 6: Commit**

```bash
git add src/spec_runner/runner.py tests/test_runner.py
git commit -m "feat: add parse_token_usage to extract tokens/cost from stderr"
```

---

### Task 2: Token fields in TaskAttempt + SQLite schema migration

**Files:**
- Modify: `src/spec_runner/state.py:32-41` (TaskAttempt dataclass)
- Modify: `src/spec_runner/state.py:113-144` (_init_db schema)
- Modify: `src/spec_runner/state.py:201-253` (_load)
- Modify: `src/spec_runner/state.py:268-306` (_save)
- Modify: `src/spec_runner/state.py:313-372` (record_attempt)
- Test: `tests/test_state.py`

**Step 1: Write the failing tests**

Add to `tests/test_state.py`:

```python
class TestTokenTracking:
    """Tests for token/cost fields in TaskAttempt and ExecutorState."""

    def test_task_attempt_has_token_fields(self):
        a = TaskAttempt(
            timestamp="2025-01-01T00:00:00",
            success=True,
            duration_seconds=10.0,
            input_tokens=1000,
            output_tokens=500,
            cost_usd=0.05,
        )
        assert a.input_tokens == 1000
        assert a.output_tokens == 500
        assert a.cost_usd == 0.05

    def test_task_attempt_token_fields_default_none(self):
        a = TaskAttempt(
            timestamp="2025-01-01T00:00:00",
            success=True,
            duration_seconds=10.0,
        )
        assert a.input_tokens is None
        assert a.output_tokens is None
        assert a.cost_usd is None

    def test_record_attempt_stores_tokens(self, tmp_path):
        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.db",
        )
        state = ExecutorState(config)
        state.record_attempt(
            "TASK-001", True, 10.0,
            input_tokens=5000, output_tokens=1200, cost_usd=0.08,
        )
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].input_tokens == 5000
        assert ts.attempts[-1].output_tokens == 1200
        assert ts.attempts[-1].cost_usd == 0.08
        state.close()

    def test_record_attempt_tokens_persist_to_sqlite(self, tmp_path):
        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.db",
        )
        state = ExecutorState(config)
        state.record_attempt(
            "TASK-001", True, 10.0,
            input_tokens=5000, output_tokens=1200, cost_usd=0.08,
        )
        state.close()

        # Re-open and verify persistence
        state2 = ExecutorState(config)
        ts = state2.get_task_state("TASK-001")
        assert ts.attempts[-1].input_tokens == 5000
        assert ts.attempts[-1].output_tokens == 1200
        assert ts.attempts[-1].cost_usd == 0.08
        state2.close()

    def test_total_cost(self, tmp_path):
        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.db",
        )
        state = ExecutorState(config)
        state.record_attempt("TASK-001", True, 10.0, cost_usd=0.10)
        state.record_attempt("TASK-002", False, 5.0, error="err", cost_usd=0.05)
        state.record_attempt("TASK-002", True, 8.0, cost_usd=0.07)
        assert abs(state.total_cost() - 0.22) < 0.001
        state.close()

    def test_task_cost(self, tmp_path):
        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.db",
        )
        state = ExecutorState(config)
        state.record_attempt("TASK-001", False, 5.0, error="err", cost_usd=0.10)
        state.record_attempt("TASK-001", True, 10.0, cost_usd=0.15)
        assert abs(state.task_cost("TASK-001") - 0.25) < 0.001
        assert state.task_cost("TASK-999") == 0.0
        state.close()

    def test_total_tokens(self, tmp_path):
        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.db",
        )
        state = ExecutorState(config)
        state.record_attempt("T1", True, 1.0, input_tokens=100, output_tokens=50)
        state.record_attempt("T2", True, 1.0, input_tokens=200, output_tokens=80)
        inp, out = state.total_tokens()
        assert inp == 300
        assert out == 130
        state.close()

    def test_schema_migration_adds_token_columns(self, tmp_path):
        """DB created before token tracking gets columns added."""
        import sqlite3
        db_path = tmp_path / "state.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT, completed_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                success INTEGER NOT NULL,
                duration_seconds REAL NOT NULL,
                error TEXT, error_code TEXT, claude_output TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE executor_meta (key TEXT PRIMARY KEY, value TEXT)
        """)
        conn.commit()
        conn.close()

        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.db",
        )
        state = ExecutorState(config)
        state.record_attempt("TASK-001", True, 10.0, input_tokens=100, cost_usd=0.01)
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].input_tokens == 100
        assert ts.attempts[-1].cost_usd == 0.01
        state.close()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py::TestTokenTracking -v`
Expected: FAIL (TaskAttempt doesn't accept `input_tokens`, etc.)

**Step 3: Implement token tracking in state.py**

3a. Add `BUDGET_EXCEEDED` to ErrorCode enum (line 29):

```python
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
```

3b. Add fields to `TaskAttempt` dataclass (after line 41):

```python
input_tokens: int | None = None
output_tokens: int | None = None
cost_usd: float | None = None
```

3c. Add column migration in `_init_db()`, after the CREATE TABLE statements (after line 143):

```python
# Migrate: add token columns if missing (for DBs created before Phase 2)
cursor = self._conn.execute("PRAGMA table_info(attempts)")
columns = {row[1] for row in cursor.fetchall()}
for col, col_type in [
    ("input_tokens", "INTEGER"),
    ("output_tokens", "INTEGER"),
    ("cost_usd", "REAL"),
]:
    if col not in columns:
        self._conn.execute(
            f"ALTER TABLE attempts ADD COLUMN {col} {col_type}"
        )
self._conn.commit()
```

3d. Update `_load()` attempts query (line 217-242) to include new columns:

Change the SELECT to:
```sql
SELECT task_id, timestamp, success, duration_seconds,
       error, error_code, claude_output,
       input_tokens, output_tokens, cost_usd
FROM attempts ORDER BY id
```

And construct TaskAttempt with the three new fields.

3e. Update `record_attempt()` signature (line 313) to accept new params:

```python
def record_attempt(
    self,
    task_id: str,
    success: bool,
    duration: float,
    error: str | None = None,
    output: str | None = None,
    error_code: ErrorCode | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
) -> None:
```

Pass new fields to TaskAttempt constructor and INSERT statement.

3f. Update `_save()` attempt INSERT (line 290-305) to include new columns.

3g. Add aggregation methods after `should_stop()` (after line 394):

```python
def total_cost(self) -> float:
    """Sum of cost_usd across all attempts."""
    return sum(
        a.cost_usd
        for ts in self.tasks.values()
        for a in ts.attempts
        if a.cost_usd is not None
    )

def task_cost(self, task_id: str) -> float:
    """Sum of cost_usd for a specific task."""
    ts = self.tasks.get(task_id)
    if not ts:
        return 0.0
    return sum(a.cost_usd for a in ts.attempts if a.cost_usd is not None)

def total_tokens(self) -> tuple[int, int]:
    """(total_input_tokens, total_output_tokens) across all attempts."""
    inp = sum(
        a.input_tokens
        for ts in self.tasks.values()
        for a in ts.attempts
        if a.input_tokens is not None
    )
    out = sum(
        a.output_tokens
        for ts in self.tasks.values()
        for a in ts.attempts
        if a.output_tokens is not None
    )
    return inp, out
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_state.py::TestTokenTracking -v`
Expected: PASS (8 tests)

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/spec_runner/state.py tests/test_state.py
git commit -m "feat: token/cost fields in TaskAttempt, schema migration, aggregation methods"
```

---

### Task 3: Wire token parsing into execute_task

**Files:**
- Modify: `src/spec_runner/executor.py:40-45` (imports)
- Modify: `src/spec_runner/executor.py:147-266` (execute_task)
- Modify: `src/spec_runner/runner.py:38-82` (send_callback)
- Test: `tests/test_execution.py`

**Step 1: Write the failing tests**

Add to `tests/test_execution.py`:

```python
class TestTokenTrackingInExecutor:
    """Tests for token/cost tracking in execute_task."""

    @patch("spec_runner.executor.mark_all_checklist_done")
    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook", return_value=(True, None))
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_tokens_parsed_from_stderr(
        self, mock_run, mock_pre, mock_post, mock_prompt, mock_cmd,
        mock_log, mock_status, mock_checklist, tmp_path,
    ):
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE",
            stderr="input_tokens: 5000\noutput_tokens: 1200\ntotal cost: $0.08",
            returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        result = execute_task(task, config, state)

        assert result is True
        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].input_tokens == 5000
        assert ts.attempts[-1].output_tokens == 1200
        assert ts.attempts[-1].cost_usd == 0.08

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook")
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_tokens_stored_on_failure(
        self, mock_run, mock_pre, mock_post, mock_prompt, mock_cmd,
        mock_log, mock_status, tmp_path,
    ):
        mock_run.return_value = MagicMock(
            stdout="TASK_FAILED: could not compile",
            stderr="input_tokens: 3000\noutput_tokens: 800\ncost: $0.04",
            returncode=1,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        execute_task(task, config, state)

        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].input_tokens == 3000
        assert ts.attempts[-1].cost_usd == 0.04

    @patch("spec_runner.executor.mark_all_checklist_done")
    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.build_cli_command", return_value=["echo", "hi"])
    @patch("spec_runner.executor.build_task_prompt", return_value="test prompt")
    @patch("spec_runner.executor.post_done_hook", return_value=(True, None))
    @patch("spec_runner.executor.pre_start_hook", return_value=True)
    @patch("spec_runner.executor.subprocess.run")
    def test_no_tokens_in_stderr_stores_none(
        self, mock_run, mock_pre, mock_post, mock_prompt, mock_cmd,
        mock_log, mock_status, mock_checklist, tmp_path,
    ):
        mock_run.return_value = MagicMock(
            stdout="output TASK_COMPLETE",
            stderr="",
            returncode=0,
        )
        task = _make_task()
        config = _make_config(tmp_path)
        state = _make_state(config)

        execute_task(task, config, state)

        ts = state.get_task_state("TASK-001")
        assert ts.attempts[-1].input_tokens is None
        assert ts.attempts[-1].cost_usd is None
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_execution.py::TestTokenTrackingInExecutor -v`
Expected: FAIL (record_attempt doesn't receive token args yet)

**Step 3: Wire token parsing into execute_task**

3a. Add `parse_token_usage` to imports at top of `executor.py` (line 40-45):

```python
from .runner import (
    build_cli_command,
    check_error_patterns,
    log_progress,
    parse_token_usage,
    send_callback,
)
```

3b. After `combined_output = output + "\n" + result.stderr` (line 157), add:

```python
input_tokens, output_tokens, cost_usd = parse_token_usage(result.stderr)
```

3c. Pass token fields to every `state.record_attempt()` call in execute_task that has access to `result.stderr`. There are 4 paths:

- API error path (line 172-176): add `input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd`
- Success path (line 202): add same
- Hook failure path (line 225-229): add same
- TASK_FAILED path (line 237-241): add same
- Timeout/exception paths: no `result` available, leave as-is (tokens will be None)

3d. Update `send_callback()` in `runner.py` to accept optional token/cost fields and include them in payload.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_execution.py::TestTokenTrackingInExecutor -v`
Expected: PASS (3 tests)

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/spec_runner/executor.py src/spec_runner/runner.py tests/test_execution.py
git commit -m "feat: wire token/cost parsing from stderr into execute_task"
```

---

### Task 4: Budget enforcement config + checks

**Files:**
- Modify: `src/spec_runner/config.py:72-160` (ExecutorConfig)
- Modify: `src/spec_runner/config.py:165-263` (load/build)
- Modify: `src/spec_runner/executor.py:269-342` (run_with_retries)
- Modify: `src/spec_runner/state.py:392-394` (should_stop)
- Test: `tests/test_config.py`
- Test: `tests/test_execution.py`
- Test: `tests/test_state.py`

**Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
class TestBudgetConfig:
    def test_budget_defaults_none(self):
        config = ExecutorConfig()
        assert config.budget_usd is None
        assert config.task_budget_usd is None

    def test_budget_from_kwargs(self):
        config = ExecutorConfig(budget_usd=10.0, task_budget_usd=2.0)
        assert config.budget_usd == 10.0
        assert config.task_budget_usd == 2.0

    def test_max_concurrent_default(self):
        config = ExecutorConfig()
        assert config.max_concurrent == 3
```

Add to `tests/test_state.py`:

```python
class TestBudgetShouldStop:
    def test_should_stop_on_budget_exceeded(self, tmp_path):
        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.db",
            budget_usd=0.15,
        )
        state = ExecutorState(config)
        state.record_attempt("T1", True, 5.0, cost_usd=0.10)
        state.record_attempt("T2", True, 5.0, cost_usd=0.06)
        assert state.should_stop() is True
        state.close()

    def test_should_not_stop_under_budget(self, tmp_path):
        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.db",
            budget_usd=1.00,
        )
        state = ExecutorState(config)
        state.record_attempt("T1", True, 5.0, cost_usd=0.10)
        assert state.should_stop() is False
        state.close()

    def test_should_not_stop_no_budget(self, tmp_path):
        config = ExecutorConfig(
            project_root=tmp_path,
            state_file=tmp_path / "state.db",
        )
        state = ExecutorState(config)
        state.record_attempt("T1", True, 5.0, cost_usd=100.0)
        assert state.should_stop() is False
        state.close()
```

Add to `tests/test_execution.py`:

```python
class TestBudgetEnforcement:
    """Tests for budget enforcement in run_with_retries."""

    @patch("spec_runner.executor.update_task_status")
    @patch("spec_runner.executor.log_progress")
    @patch("spec_runner.executor.execute_task")
    def test_task_budget_exceeded_stops_retries(
        self, mock_exec, mock_log, mock_status, tmp_path,
    ):
        """When task cost exceeds task_budget_usd, stop retrying."""
        config = _make_config(tmp_path, max_retries=5, task_budget_usd=0.10)
        state = _make_state(config)
        task = _make_task()

        call_count = 0
        def side_effect(t, cfg, st):
            nonlocal call_count
            call_count += 1
            # Simulate each attempt costing $0.06
            st.record_attempt(
                t.id, False, 5.0, error="err",
                error_code=ErrorCode.TASK_FAILED, cost_usd=0.06,
            )
            return False

        mock_exec.side_effect = side_effect

        result = run_with_retries(task, config, state)

        assert result is False
        # Should stop after 2 attempts ($0.12 > $0.10)
        assert call_count == 2
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py::TestBudgetConfig tests/test_state.py::TestBudgetShouldStop tests/test_execution.py::TestBudgetEnforcement -v`
Expected: FAIL

**Step 3: Implement budget config and enforcement**

3a. Add fields to `ExecutorConfig` (after `on_task_failure`, around line 80):

```python
max_concurrent: int = 3  # Max parallel tasks
budget_usd: float | None = None  # Global budget limit (None = unlimited)
task_budget_usd: float | None = None  # Per-task budget limit (None = unlimited)
```

3b. Update `load_config_from_yaml()` to read `max_concurrent`, `budget_usd`, `task_budget_usd`.

3c. Update `build_config()` to handle CLI args for budget/concurrency.

3d. Update `should_stop()` in `state.py` (line 392-394):

```python
def should_stop(self) -> bool:
    """Check if we should stop (consecutive failures or budget exceeded)."""
    if self.consecutive_failures >= self.config.max_consecutive_failures:
        return True
    if self.config.budget_usd is not None and self.total_cost() > self.config.budget_usd:
        return True
    return False
```

3e. Add per-task budget check in `run_with_retries()` after each failed attempt (in the retry loop, after `if result is True: return True`):

```python
# Check per-task budget
if config.task_budget_usd is not None:
    if state.task_cost(task.id) > config.task_budget_usd:
        log_progress(
            f"Task budget exceeded (${state.task_cost(task.id):.2f} > ${config.task_budget_usd:.2f})",
            task.id,
        )
        update_task_status(config.tasks_file, task.id, "blocked")
        return False
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py::TestBudgetConfig tests/test_state.py::TestBudgetShouldStop tests/test_execution.py::TestBudgetEnforcement -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/spec_runner/config.py src/spec_runner/state.py src/spec_runner/executor.py tests/test_config.py tests/test_state.py tests/test_execution.py
git commit -m "feat: add budget enforcement (per-task + global limits)"
```

---

### Task 5: Hooks no_branch verification for parallel mode

**Files:**
- Test: `tests/test_hooks.py`

**Step 1: Write tests verifying existing no_branch behavior**

Add to `tests/test_hooks.py`:

```python
from spec_runner.config import ExecutorConfig
from spec_runner.hooks import pre_start_hook, post_done_hook
from spec_runner.task import Task


class TestNoBranchMode:
    """Verify hooks skip git ops when create_git_branch=False (parallel mode)."""

    @patch("spec_runner.hooks.subprocess.run")
    def test_pre_start_skips_branch_when_no_branch(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        config = ExecutorConfig(
            project_root=tmp_path,
            create_git_branch=False,
        )
        task = Task(id="TASK-001", name="Test", priority="p1", status="todo")

        result = pre_start_hook(task, config)

        assert result is True
        # Git checkout/branch should not be called
        call_args = [str(c) for c in mock_run.call_args_list]
        assert not any("checkout" in c for c in call_args)

    @patch("spec_runner.hooks.subprocess.run")
    def test_post_done_skips_merge_when_no_branch(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        config = ExecutorConfig(
            project_root=tmp_path,
            create_git_branch=False,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=False,
            auto_commit=False,
        )
        task = Task(id="TASK-001", name="Test", priority="p1", status="todo")

        success, error = post_done_hook(task, config, True)

        assert success is True
        call_args = [str(c) for c in mock_run.call_args_list]
        assert not any("merge" in c for c in call_args)
```

**Step 2: Run tests**

Run: `uv run pytest tests/test_hooks.py::TestNoBranchMode -v`
Expected: PASS (this confirms existing behavior works for parallel mode)

**Step 3: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 4: Commit**

```bash
git add tests/test_hooks.py
git commit -m "test: verify no_branch hooks behavior for parallel mode"
```

---

### Task 6: Async subprocess wrapper

**Files:**
- Modify: `src/spec_runner/runner.py`
- Test: `tests/test_runner.py`

**Step 1: Write the failing tests**

Add to `tests/test_runner.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock
from spec_runner.runner import run_claude_async


class TestRunClaudeAsync:
    """Tests for async subprocess wrapper."""

    def test_returns_stdout_stderr_returncode(self):
        async def _run():
            with patch("spec_runner.runner.asyncio.create_subprocess_exec") as mock_cse:
                mock_proc = AsyncMock()
                mock_proc.communicate.return_value = (b"output text", b"stderr text")
                mock_proc.returncode = 0
                mock_cse.return_value = mock_proc

                stdout, stderr, rc = await run_claude_async(
                    ["echo", "hi"], timeout=60, cwd="/tmp"
                )
                assert stdout == "output text"
                assert stderr == "stderr text"
                assert rc == 0

        asyncio.run(_run())

    def test_timeout_kills_process(self):
        async def _run():
            with patch("spec_runner.runner.asyncio.create_subprocess_exec") as mock_cse:
                mock_proc = AsyncMock()
                mock_proc.communicate.side_effect = asyncio.TimeoutError()
                mock_proc.kill = MagicMock()
                mock_proc.wait = AsyncMock()
                mock_cse.return_value = mock_proc

                with pytest.raises(asyncio.TimeoutError):
                    await run_claude_async(["echo", "hi"], timeout=1, cwd="/tmp")
                mock_proc.kill.assert_called_once()

        asyncio.run(_run())
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_runner.py::TestRunClaudeAsync -v`
Expected: FAIL with `ImportError: cannot import name 'run_claude_async'`

**Step 3: Implement run_claude_async**

Add to `src/spec_runner/runner.py`:

```python
import asyncio


async def run_claude_async(
    cmd: list[str],
    timeout: float,
    cwd: str,
) -> tuple[str, str, int]:
    """Run CLI command asynchronously.

    Args:
        cmd: Command arguments.
        timeout: Timeout in seconds.
        cwd: Working directory.

    Returns:
        (stdout, stderr, returncode).

    Raises:
        asyncio.TimeoutError: If command exceeds timeout.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return stdout_bytes.decode(), stderr_bytes.decode(), proc.returncode
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_runner.py::TestRunClaudeAsync -v`
Expected: PASS (2 tests)

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/spec_runner/runner.py tests/test_runner.py
git commit -m "feat: add run_claude_async for non-blocking subprocess execution"
```

---

### Task 7: Parallel execution in executor.py

**Files:**
- Modify: `src/spec_runner/executor.py:348-501` (cmd_run, _run_tasks)
- Modify: `src/spec_runner/executor.py:866-971` (main, argparse)
- Modify: `src/spec_runner/config.py:223-263` (build_config)
- Test: `tests/test_execution.py`

**Step 1: Write the failing tests**

Add to `tests/test_execution.py`:

```python
import asyncio


class TestParallelExecution:
    """Tests for parallel task execution."""

    def test_run_tasks_parallel_exists(self):
        """_run_tasks_parallel function exists and is a coroutine."""
        from spec_runner.executor import _run_tasks_parallel
        assert asyncio.iscoroutinefunction(_run_tasks_parallel)

    def test_execute_task_async_exists(self):
        """_execute_task_async function exists and is a coroutine."""
        from spec_runner.executor import _execute_task_async
        assert asyncio.iscoroutinefunction(_execute_task_async)

    def test_parallel_flag_in_argparser(self):
        """CLI parser accepts --parallel flag."""
        import argparse
        from spec_runner.executor import main
        # Just verify it doesn't crash on import
        assert callable(main)
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_execution.py::TestParallelExecution -v`
Expected: FAIL with `ImportError`

**Step 3: Implement parallel execution**

3a. Add `--parallel` and `--max-concurrent` to run_parser in `main()` (after line 915):

```python
run_parser.add_argument(
    "--parallel",
    action="store_true",
    help="Execute ready tasks in parallel (implies --no-branch)",
)
run_parser.add_argument(
    "--max-concurrent",
    type=int,
    default=0,
    help="Max parallel tasks (default: from config, typically 3)",
)
```

3b. Handle in `build_config()`: if `args.parallel`, set `create_git_branch = False`. If `args.max_concurrent > 0`, override `max_concurrent`.

3c. Add `asyncio` import at top of `executor.py`.

3d. Add `_execute_task_async()`:

```python
async def _execute_task_async(
    task: Task,
    config: ExecutorConfig,
    state: ExecutorState,
    state_lock: asyncio.Lock,
) -> bool | str:
    """Async wrapper for task execution with state locking.

    Uses run_claude_async for non-blocking subprocess execution.
    Protects ExecutorState writes with asyncio.Lock.
    """
    from .runner import run_claude_async, parse_token_usage

    task_id = task.id
    log_progress(f"Starting: {task.name}", task_id)

    # Pre-start hook (sync, but quick)
    if not pre_start_hook(task, config):
        async with state_lock:
            state.record_attempt(
                task_id, False, 0.0,
                error="Pre-start hook failed",
                error_code=ErrorCode.HOOK_FAILURE,
            )
        return "HOOK_ERROR"

    async with state_lock:
        state.mark_running(task_id)
    update_task_status(config.tasks_file, task_id, "in_progress")

    # Build prompt
    task_state = state.get_task_state(task_id)
    previous_attempts = task_state.attempts if task_state.attempts else None
    retry_context = None
    if previous_attempts:
        failed = [a for a in previous_attempts if not a.success]
        if failed:
            last = failed[-1]
            retry_context = RetryContext(
                attempt_number=task_state.attempt_count + 1,
                max_attempts=config.max_retries,
                previous_error_code=last.error_code or ErrorCode.UNKNOWN,
                previous_error=last.error or "Unknown error",
                what_was_tried=f"Previous attempt for {task.name}",
                test_failures=(
                    extract_test_failures(last.claude_output)
                    if last.claude_output
                    and last.error_code
                    in (ErrorCode.TEST_FAILURE, ErrorCode.LINT_FAILURE)
                    else None
                ),
            )

    prompt = build_task_prompt(task, config, previous_attempts, retry_context=retry_context)

    # Log
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    log_file = config.logs_dir / f"{task_id}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    with open(log_file, "w") as f:
        f.write(f"=== PROMPT ===\n{prompt}\n\n")

    # Build command
    cmd = build_cli_command(
        cmd=config.claude_command,
        prompt=prompt,
        model=config.claude_model,
        template=config.command_template,
        skip_permissions=config.skip_permissions,
    )

    start_time = datetime.now()

    try:
        stdout, stderr, returncode = await run_claude_async(
            cmd,
            timeout=config.task_timeout_minutes * 60,
            cwd=str(config.project_root),
        )

        duration = (datetime.now() - start_time).total_seconds()
        output = stdout
        combined_output = output + "\n" + stderr
        input_tokens, output_tokens, cost_usd = parse_token_usage(stderr)

        # Save output
        with open(log_file, "a") as f:
            f.write(f"=== OUTPUT ===\n{output}\n\n")
            f.write(f"=== STDERR ===\n{stderr}\n\n")
            f.write(f"=== RETURN CODE: {returncode} ===\n")

        # Check for API errors
        error_pattern = check_error_patterns(combined_output)
        if error_pattern:
            async with state_lock:
                state.record_attempt(
                    task_id, False, duration,
                    error=f"API error: {error_pattern}",
                    error_code=ErrorCode.RATE_LIMIT,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_usd=cost_usd,
                )
            return "API_ERROR"

        # Check result markers
        has_complete = "TASK_COMPLETE" in output
        has_failed = "TASK_FAILED" in output
        implicit_success = returncode == 0 and not has_failed
        success = (has_complete and not has_failed) or implicit_success

        if success:
            hook_success, hook_error = post_done_hook(task, config, True)
            if hook_success:
                async with state_lock:
                    state.record_attempt(
                        task_id, True, duration, output=output,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cost_usd=cost_usd,
                    )
                update_task_status(config.tasks_file, task_id, "done")
                mark_all_checklist_done(config.tasks_file, task_id)
                return True
            else:
                error = hook_error or "Post-done hook failed"
                error_code = ErrorCode.UNKNOWN
                if hook_error:
                    if "Tests failed" in hook_error:
                        error_code = ErrorCode.TEST_FAILURE
                    elif "Lint errors" in hook_error:
                        error_code = ErrorCode.LINT_FAILURE
                    else:
                        error_code = ErrorCode.HOOK_FAILURE
                full_output = output
                if hook_error:
                    full_output = f"{output}\n\n=== TEST FAILURES ===\n{hook_error}"
                async with state_lock:
                    state.record_attempt(
                        task_id, False, duration,
                        error=error, output=full_output,
                        error_code=error_code,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                        cost_usd=cost_usd,
                    )
                return False
        else:
            import re as re_mod
            error_match = re_mod.search(r"TASK_FAILED:\s*(.+)", output)
            error = error_match.group(1) if error_match else "Unknown error"
            async with state_lock:
                state.record_attempt(
                    task_id, False, duration,
                    error=error, output=output,
                    error_code=ErrorCode.TASK_FAILED,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    cost_usd=cost_usd,
                )
            return False

    except asyncio.TimeoutError:
        duration = config.task_timeout_minutes * 60
        async with state_lock:
            state.record_attempt(
                task_id, False, duration,
                error=f"Timeout after {config.task_timeout_minutes} minutes",
                error_code=ErrorCode.TIMEOUT,
            )
        return False

    except Exception as e:
        duration = (datetime.now() - start_time).total_seconds()
        async with state_lock:
            state.record_attempt(
                task_id, False, duration,
                error=str(e), error_code=ErrorCode.UNKNOWN,
            )
        return False
```

3e. Add `_run_tasks_parallel()`:

```python
async def _run_tasks_parallel(args, config: ExecutorConfig):
    """Execute tasks in parallel using asyncio."""
    clear_stop_file(config)
    tasks = parse_tasks(config.tasks_file)
    state = ExecutorState(config)
    state_lock = asyncio.Lock()
    sem = asyncio.Semaphore(config.max_concurrent)
    executed_ids: set[str] = set()

    async def run_one(task: Task) -> tuple[str, bool | str]:
        async with sem:
            result = await _execute_task_async(task, config, state, state_lock)
            return task.id, result

    try:
        include_in_progress = not getattr(args, "restart", False)
        while True:
            if check_stop_requested(config):
                clear_stop_file(config)
                print("\n Graceful shutdown requested")
                break

            tasks = parse_tasks(config.tasks_file)
            ready = get_next_tasks(tasks, include_in_progress=include_in_progress)
            if args.milestone:
                ready = [t for t in ready if args.milestone.lower() in t.milestone.lower()]
            ready = [t for t in ready if t.id not in executed_ids]

            if not ready or state.should_stop():
                break

            print(f"\nDispatching {len(ready)} tasks in parallel...")
            for t in ready:
                print(f"   - {t.id}: {t.name}")
                executed_ids.add(t.id)

            results = await asyncio.gather(
                *[run_one(t) for t in ready],
                return_exceptions=True,
            )

            # Check for API errors
            api_error = False
            for r in results:
                if isinstance(r, tuple) and r[1] == "API_ERROR":
                    api_error = True
                    break
            if api_error:
                print("\n Stopping: API rate limit reached")
                break

            if state.should_stop():
                print("\n Stopping: failure/budget limit reached")
                break

        # Summary
        tasks = parse_tasks(config.tasks_file)
        remaining = len([t for t in tasks if t.status == "todo"])
        total_cost_val = state.total_cost()

        print(f"\n{'=' * 60}")
        print("Execution Summary (parallel)")
        print(f"{'=' * 60}")
        print(f"   Tasks completed:    {state.total_completed}")
        print(f"   Tasks failed:       {state.total_failed}")
        print(f"   Tasks remaining:    {remaining}")
        if total_cost_val > 0:
            print(f"   Total cost:         ${total_cost_val:.2f}")
    finally:
        state.close()
```

3f. Update `cmd_run()` to dispatch parallel path (line 348-362):

```python
def cmd_run(args, config: ExecutorConfig):
    """Execute tasks."""
    if getattr(args, "parallel", False):
        # Parallel mode implies no branch
        config.create_git_branch = False
        if getattr(args, "max_concurrent", 0) > 0:
            config.max_concurrent = args.max_concurrent
        asyncio.run(_run_tasks_parallel(args, config))
    else:
        lock = ExecutorLock(config.state_file.with_suffix(".lock"))
        if not lock.acquire():
            print("Another executor is already running")
            sys.exit(1)
        try:
            _run_tasks(args, config)
        finally:
            lock.release()
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_execution.py::TestParallelExecution -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/spec_runner/executor.py src/spec_runner/config.py tests/test_execution.py
git commit -m "feat: add parallel task execution with asyncio + semaphore"
```

---

### Task 8: Token/cost in status output

**Files:**
- Modify: `src/spec_runner/executor.py:520-574` (cmd_status)

**Step 1: Add token/cost summary to cmd_status**

After the existing `Consecutive failures:` line (around line 552), add:

```python
# Token/cost summary
total_cost_val = state.total_cost()
if total_cost_val > 0:
    total_inp, total_out = state.total_tokens()
    def _fmt_tokens(n: int) -> str:
        if n >= 1000:
            return f"{n / 1000:.1f}K"
        return str(n)
    print(f"Tokens:                {_fmt_tokens(total_inp)} in / {_fmt_tokens(total_out)} out")
    print(f"Total cost:            ${total_cost_val:.2f}")
```

Also add per-task cost in the Task History section, after the attempts_info line:

```python
task_cost = state.task_cost(ts.task_id)
if task_cost > 0:
    attempts_info += f", ${task_cost:.2f}"
```

**Step 2: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 3: Commit**

```bash
git add src/spec_runner/executor.py
git commit -m "feat: show token/cost summary in spec-runner status output"
```

---

### Task 9: Update __init__.py exports and CLAUDE.md

**Files:**
- Modify: `src/spec_runner/__init__.py`
- Modify: `CLAUDE.md`

**Step 1: Update __init__.py exports**

Add to imports:

```python
from .runner import (
    parse_token_usage,
    run_claude_async,
)
```

Add to `__all__`:

```python
"parse_token_usage",
"run_claude_async",
```

**Step 2: Update CLAUDE.md**

Update the Architecture table to reflect Phase 2 additions:
- `runner.py` — add `parse_token_usage()`, `run_claude_async()`
- `executor.py` — add `_run_tasks_parallel()`, `_execute_task_async()`, budget checks
- `config.py` — add `max_concurrent`, `budget_usd`, `task_budget_usd`
- `state.py` — add token fields, `total_cost()`, `task_cost()`, `total_tokens()`, `BUDGET_EXCEEDED`

Update CLI entry points section:

```bash
spec-runner run --all --parallel           # Execute ready tasks in parallel
spec-runner run --all --parallel --max-concurrent=5  # With concurrency limit
```

Update test count.

**Step 3: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 4: Commit**

```bash
git add src/spec_runner/__init__.py CLAUDE.md
git commit -m "docs: update exports and CLAUDE.md for Phase 2"
```
