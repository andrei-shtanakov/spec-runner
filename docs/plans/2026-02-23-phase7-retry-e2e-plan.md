# Phase 7: Smart Retry & E2E Tests — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add error-aware retry with exponential backoff for rate limits, and E2E integration tests using a fake CLI.

**Architecture:** Two new functions (`classify_retry_strategy`, `compute_retry_delay`) replace the fixed delay in `run_with_retries()`. A bash fake CLI script enables E2E tests that exercise the full pipeline without calling real Claude.

**Tech Stack:** Python 3.10+, pytest, bash

---

### Task 1: Retry strategy classification and delay computation

**Files:**
- Modify: `src/spec_runner/executor.py:376-429`
- Test: `tests/test_execution.py`

**Context:** Currently `run_with_retries()` uses a fixed `time.sleep(config.retry_delay_seconds)` (line 429) for all retries, and exits immediately on RATE_LIMIT (line 392-393). We need error-aware delays.

**Step 1: Write the failing tests**

Add to `tests/test_execution.py`:

```python
from spec_runner.executor import classify_retry_strategy, compute_retry_delay
from spec_runner.state import ErrorCode


class TestRetryStrategy:
    """Tests for error-aware retry delay computation."""

    def test_rate_limit_is_exponential(self):
        assert classify_retry_strategy(ErrorCode.RATE_LIMIT) == "backoff_exponential"

    def test_test_failure_is_linear(self):
        assert classify_retry_strategy(ErrorCode.TEST_FAILURE) == "backoff_linear"

    def test_lint_failure_is_linear(self):
        assert classify_retry_strategy(ErrorCode.LINT_FAILURE) == "backoff_linear"

    def test_task_failed_is_linear(self):
        assert classify_retry_strategy(ErrorCode.TASK_FAILED) == "backoff_linear"

    def test_timeout_is_linear(self):
        assert classify_retry_strategy(ErrorCode.TIMEOUT) == "backoff_linear"

    def test_unknown_is_linear(self):
        assert classify_retry_strategy(ErrorCode.UNKNOWN) == "backoff_linear"

    def test_hook_failure_is_fatal(self):
        assert classify_retry_strategy(ErrorCode.HOOK_FAILURE) == "fatal"

    def test_review_rejected_is_fatal(self):
        assert classify_retry_strategy(ErrorCode.REVIEW_REJECTED) == "fatal"

    def test_budget_exceeded_is_fatal(self):
        assert classify_retry_strategy(ErrorCode.BUDGET_EXCEEDED) == "fatal"

    def test_interrupted_is_fatal(self):
        assert classify_retry_strategy(ErrorCode.INTERRUPTED) == "fatal"


class TestComputeRetryDelay:
    """Tests for compute_retry_delay."""

    def test_exponential_attempt_0(self):
        delay = compute_retry_delay(ErrorCode.RATE_LIMIT, attempt=0)
        assert delay == 30.0

    def test_exponential_attempt_1(self):
        delay = compute_retry_delay(ErrorCode.RATE_LIMIT, attempt=1)
        assert delay == 60.0

    def test_exponential_attempt_2(self):
        delay = compute_retry_delay(ErrorCode.RATE_LIMIT, attempt=2)
        assert delay == 120.0

    def test_exponential_caps_at_300(self):
        delay = compute_retry_delay(ErrorCode.RATE_LIMIT, attempt=10)
        assert delay == 300.0

    def test_linear_attempt_0(self):
        delay = compute_retry_delay(ErrorCode.TEST_FAILURE, attempt=0, base_delay=5)
        assert delay == 5.0

    def test_linear_attempt_1(self):
        delay = compute_retry_delay(ErrorCode.TEST_FAILURE, attempt=1, base_delay=5)
        assert delay == 10.0

    def test_linear_attempt_2(self):
        delay = compute_retry_delay(ErrorCode.TEST_FAILURE, attempt=2, base_delay=5)
        assert delay == 15.0

    def test_fatal_returns_zero(self):
        delay = compute_retry_delay(ErrorCode.HOOK_FAILURE, attempt=0)
        assert delay == 0.0
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_execution.py::TestRetryStrategy -v`
Expected: FAIL with `ImportError: cannot import name 'classify_retry_strategy'`

**Step 3: Write minimal implementation**

Add to `src/spec_runner/executor.py`, right before `run_with_retries()` (before line 376):

```python
# === Retry Strategy ===

_FATAL_ERRORS = frozenset({
    ErrorCode.HOOK_FAILURE,
    ErrorCode.REVIEW_REJECTED,
    ErrorCode.BUDGET_EXCEEDED,
    ErrorCode.INTERRUPTED,
})

_EXPONENTIAL_ERRORS = frozenset({
    ErrorCode.RATE_LIMIT,
})


def classify_retry_strategy(error_code: ErrorCode | str) -> str:
    """Classify error into retry strategy.

    Returns:
        "fatal" — no retry, "backoff_exponential" — long increasing delays,
        "backoff_linear" — short increasing delays.
    """
    code = ErrorCode(error_code) if isinstance(error_code, str) else error_code
    if code in _FATAL_ERRORS:
        return "fatal"
    if code in _EXPONENTIAL_ERRORS:
        return "backoff_exponential"
    return "backoff_linear"


def compute_retry_delay(
    error_code: ErrorCode | str, attempt: int, base_delay: int = 5
) -> float:
    """Compute delay before next retry based on error type and attempt number.

    Args:
        error_code: The error that caused the failure.
        attempt: Zero-based attempt index (0 = first retry).
        base_delay: Base delay in seconds for linear backoff.

    Returns:
        Delay in seconds. 0.0 for fatal errors.
    """
    strategy = classify_retry_strategy(error_code)
    if strategy == "fatal":
        return 0.0
    if strategy == "backoff_exponential":
        return min(30.0 * (2 ** attempt), 300.0)
    # linear
    return float(base_delay * (attempt + 1))
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_execution.py::TestRetryStrategy tests/test_execution.py::TestComputeRetryDelay -v`
Expected: All 18 tests PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v -m "not slow"`
Expected: All tests PASS

**Step 6: Commit**

```bash
git add src/spec_runner/executor.py tests/test_execution.py
git commit -m "feat: add classify_retry_strategy and compute_retry_delay"
```

---

### Task 2: Wire smart retry into run_with_retries

**Files:**
- Modify: `src/spec_runner/executor.py:376-429`
- Test: `tests/test_execution.py`

**Context:** Now we have `classify_retry_strategy` and `compute_retry_delay`. We need to:
1. Remove the immediate `return "API_ERROR"` for RATE_LIMIT (line 392-393)
2. Replace the fixed `time.sleep(config.retry_delay_seconds)` (line 429) with `compute_retry_delay`
3. Keep immediate exit for fatal errors (HOOK_FAILURE already exits at line 396-397, REVIEW_REJECTED at line 417-419)

**Step 1: Write the failing tests**

Add to `tests/test_execution.py`:

```python
class TestSmartRetry:
    """Tests for error-aware retry in run_with_retries."""

    @patch("spec_runner.executor.time.sleep")
    @patch("spec_runner.executor.execute_task")
    def test_rate_limit_retries_with_exponential_backoff(
        self, mock_execute, mock_sleep, tmp_path
    ):
        """RATE_LIMIT should retry with exponential backoff, not exit immediately."""
        config = _make_config(tmp_path, max_retries=3, retry_delay_seconds=5)
        state = _make_state(config)
        task = _make_task()

        # First two: rate limit, third: success
        mock_execute.side_effect = ["API_ERROR", "API_ERROR", True]

        # Need to record RATE_LIMIT attempts for the retry logic to see them
        from spec_runner.state import ErrorCode

        def execute_side_effect(*args, **kwargs):
            call_count = mock_execute.call_count
            if call_count <= 2:
                state.record_attempt(
                    task.id, False, 1.0,
                    error="rate limit", error_code=ErrorCode.RATE_LIMIT,
                )
                return "API_ERROR"
            return True

        mock_execute.side_effect = execute_side_effect

        result = run_with_retries(task, config, state)
        assert result is True
        assert mock_execute.call_count == 3
        # Check exponential delays: 30s, 60s
        assert mock_sleep.call_count == 2

    @patch("spec_runner.executor.time.sleep")
    @patch("spec_runner.executor.execute_task")
    def test_transient_error_uses_linear_backoff(
        self, mock_execute, mock_sleep, tmp_path
    ):
        """TEST_FAILURE should use linear backoff."""
        config = _make_config(tmp_path, max_retries=3, retry_delay_seconds=5)
        state = _make_state(config)
        task = _make_task()

        from spec_runner.state import ErrorCode

        def execute_side_effect(*args, **kwargs):
            call_count = mock_execute.call_count
            if call_count <= 1:
                state.record_attempt(
                    task.id, False, 1.0,
                    error="tests failed", error_code=ErrorCode.TEST_FAILURE,
                )
                return False
            return True

        mock_execute.side_effect = execute_side_effect

        result = run_with_retries(task, config, state)
        assert result is True
        # Linear: 5*(0+1) = 5s
        assert mock_sleep.call_count == 1
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_execution.py::TestSmartRetry -v`
Expected: FAIL (rate limit still returns "API_ERROR" immediately)

**Step 3: Modify `run_with_retries()`**

Replace lines 391-429 of `src/spec_runner/executor.py`. The key changes:

1. Remove the immediate `return "API_ERROR"` for `result == "API_ERROR"`
2. After each failed attempt, get the last error code from state
3. Check if fatal → return immediately
4. Otherwise compute delay and sleep

The new logic after `result = execute_task(...)` (line 389):

```python
        result = execute_task(task, config, state)

        # Hook error — always fatal, stop immediately
        if result == "HOOK_ERROR":
            return False

        # Check per-task budget
        if config.task_budget_usd is not None and state.task_cost(task.id) > config.task_budget_usd:
            log_progress(
                f"Task budget exceeded "
                f"(${state.task_cost(task.id):.2f} > "
                f"${config.task_budget_usd:.2f})",
                task.id,
            )
            update_task_status(config.tasks_file, task.id, "blocked")
            return False

        if result is True:
            return True

        # Determine retry strategy from last attempt's error code
        ts = state.get_task_state(task.id)
        last_error_code = ErrorCode.UNKNOWN
        if ts and ts.attempts:
            last = ts.attempts[-1]
            if last.error_code:
                last_error_code = last.error_code

        # Fatal errors — no retry
        if classify_retry_strategy(last_error_code) == "fatal":
            log_progress(f"Fatal error ({last_error_code.value}) — no retry", task.id)
            return False

        if attempt < config.max_retries - 1:
            delay = compute_retry_delay(last_error_code, attempt, config.retry_delay_seconds)
            logger.info(
                "Waiting before retry",
                task_id=task.id,
                delay_seconds=delay,
                error_code=last_error_code.value,
                strategy=classify_retry_strategy(last_error_code),
            )
            import time

            time.sleep(delay)
```

Note: the `import time` is already there on line 427 — move it to the top of the function or leave inline.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_execution.py::TestSmartRetry -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v -m "not slow"`
Expected: All tests PASS. Existing `TestRunWithRetries` tests must still pass — they used mock `execute_task` returning `"API_ERROR"` and `False`, verify they still work with the new logic.

**Step 6: Commit**

```bash
git add src/spec_runner/executor.py tests/test_execution.py
git commit -m "feat: wire smart retry with exponential backoff into run_with_retries"
```

---

### Task 3: Create fake_claude.sh fixture

**Files:**
- Create: `tests/fixtures/fake_claude.sh`

**Context:** E2E tests need a deterministic fake CLI. This bash script reads env vars and returns canned responses. It supports multi-attempt scenarios via a counter file.

**Step 1: Create the script**

Create `tests/fixtures/fake_claude.sh`:

```bash
#!/usr/bin/env bash
# Fake Claude CLI for E2E testing.
#
# Env vars:
#   FAKE_RESPONSE_FILE  — path to file with response text
#   FAKE_EXIT_CODE      — exit code (default 0)
#   FAKE_DELAY          — sleep seconds before responding (default 0)
#   FAKE_STDERR         — text to write to stderr
#   FAKE_COUNTER_FILE   — if set, appends attempt index to FAKE_RESPONSE_FILE
#                          e.g. response.0, response.1, response.2

set -e

sleep "${FAKE_DELAY:-0}"

RESPONSE_FILE="$FAKE_RESPONSE_FILE"

# Multi-attempt support: read counter, increment, pick response file
if [ -n "$FAKE_COUNTER_FILE" ]; then
    COUNT=$(cat "$FAKE_COUNTER_FILE" 2>/dev/null || echo 0)
    echo $((COUNT + 1)) > "$FAKE_COUNTER_FILE"
    RESPONSE_FILE="${FAKE_RESPONSE_FILE}.${COUNT}"
fi

if [ -n "$RESPONSE_FILE" ] && [ -f "$RESPONSE_FILE" ]; then
    cat "$RESPONSE_FILE"
else
    echo "No response configured"
fi

if [ -n "$FAKE_STDERR" ]; then
    echo -n "$FAKE_STDERR" >&2
fi

exit "${FAKE_EXIT_CODE:-0}"
```

**Step 2: Make it executable**

```bash
chmod +x tests/fixtures/fake_claude.sh
```

**Step 3: Quick manual test**

```bash
FAKE_RESPONSE_FILE=/dev/null FAKE_STDERR="token info" tests/fixtures/fake_claude.sh -p "hello"
echo $?  # Should be 0
```

**Step 4: Commit**

```bash
git add tests/fixtures/fake_claude.sh
git commit -m "test: add fake_claude.sh fixture for E2E tests"
```

---

### Task 4: E2E test — single task success

**Files:**
- Create: `tests/test_e2e.py`

**Context:** First E2E test. Verifies the full pipeline: tasks.md → parse → execute (via fake CLI) → state.db updated → task marked done.

**Step 1: Write the test**

Create `tests/test_e2e.py`:

```python
"""E2E integration tests using fake_claude.sh.

These tests exercise the full execution pipeline without mocking subprocess.
All tests are marked @pytest.mark.slow.
"""

import os
import stat
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task, run_with_retries
from spec_runner.state import ExecutorState
from spec_runner.task import parse_tasks

FAKE_CLI = Path(__file__).parent / "fixtures" / "fake_claude.sh"

MINIMAL_TASKS_MD = """\
# Tasks

## TASK-001: Add login page [p1] [todo]
- Est: 1h
- Checklist:
  - [ ] Create login form
  - [ ] Add validation
"""


def _make_e2e_config(tmp_path: Path, **overrides) -> ExecutorConfig:
    """Create config pointing at fake CLI."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir(exist_ok=True)

    defaults = {
        "project_root": tmp_path,
        "state_file": tmp_path / "state.db",
        "logs_dir": tmp_path / "logs",
        "claude_command": str(FAKE_CLI),
        "command_template": "{cmd} -p {prompt}",
        "skip_permissions": True,
        "max_retries": 3,
        "retry_delay_seconds": 0,
        "task_timeout_minutes": 1,
        "create_git_branch": False,
        "run_tests_on_done": False,
        "run_lint_on_done": False,
        "auto_commit": False,
        "run_review": False,
        "callback_url": "",
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _write_tasks(tmp_path: Path, content: str = MINIMAL_TASKS_MD) -> Path:
    """Write tasks.md and return its path."""
    tasks_file = tmp_path / "spec" / "tasks.md"
    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text(content)
    return tasks_file


def _write_response(tmp_path: Path, filename: str, content: str) -> Path:
    """Write a response file for fake CLI."""
    resp_dir = tmp_path / "responses"
    resp_dir.mkdir(exist_ok=True)
    resp = resp_dir / filename
    resp.write_text(content)
    return resp


@pytest.mark.slow
class TestE2ESingleTask:
    """Single task execution through the full pipeline."""

    def test_single_task_success(self, tmp_path: Path):
        """Full cycle: tasks.md -> parse -> execute -> state.db shows done."""
        config = _make_e2e_config(tmp_path)
        state = ExecutorState(config)

        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)
        assert len(tasks) == 1

        task = tasks[0]
        response_file = _write_response(
            tmp_path, "success.txt", "Implemented login form.\nTASK_COMPLETE"
        )

        # Set env for fake CLI
        os.environ["FAKE_RESPONSE_FILE"] = str(response_file)
        os.environ.pop("FAKE_COUNTER_FILE", None)
        os.environ.pop("FAKE_EXIT_CODE", None)
        os.environ.pop("FAKE_STDERR", None)
        os.environ.pop("FAKE_DELAY", None)

        try:
            result = execute_task(task, config, state)
            assert result is True

            # State should show 1 successful attempt
            ts = state.get_task_state(task.id)
            assert ts is not None
            assert len(ts.attempts) == 1
            assert ts.attempts[0].success is True
        finally:
            os.environ.pop("FAKE_RESPONSE_FILE", None)
```

**Step 2: Run test to verify it passes**

Run: `uv run pytest tests/test_e2e.py::TestE2ESingleTask::test_single_task_success -v -m "slow"`
Expected: PASS

**Step 3: Commit**

```bash
git add tests/test_e2e.py
git commit -m "test: add first E2E test — single task success"
```

---

### Task 5: E2E test — failure, retry, and rate limit backoff

**Files:**
- Modify: `tests/test_e2e.py`

**Context:** Test multi-attempt scenarios. The fake CLI counter file feature lets us return different responses per attempt.

**Step 1: Write the tests**

Add to `tests/test_e2e.py`:

```python
@pytest.mark.slow
class TestE2ERetry:
    """Retry scenarios through the full pipeline."""

    def test_failure_then_success(self, tmp_path: Path):
        """First attempt TASK_FAILED, second succeeds."""
        config = _make_e2e_config(tmp_path, max_retries=3)
        state = ExecutorState(config)
        _write_tasks(tmp_path)
        tasks = parse_tasks(tmp_path / "spec" / "tasks.md")
        task = tasks[0]

        # Response files: attempt 0 = fail, attempt 1 = success
        resp_dir = tmp_path / "responses"
        resp_dir.mkdir(exist_ok=True)
        base = resp_dir / "retry"
        (resp_dir / "retry.0").write_text("Could not complete.\nTASK_FAILED: syntax error")
        (resp_dir / "retry.1").write_text("Fixed and done.\nTASK_COMPLETE")

        counter = tmp_path / "counter.txt"
        os.environ["FAKE_RESPONSE_FILE"] = str(base)
        os.environ["FAKE_COUNTER_FILE"] = str(counter)
        os.environ.pop("FAKE_EXIT_CODE", None)
        os.environ.pop("FAKE_STDERR", None)
        os.environ.pop("FAKE_DELAY", None)

        try:
            result = run_with_retries(task, config, state)
            assert result is True

            ts = state.get_task_state(task.id)
            assert len(ts.attempts) == 2
            assert ts.attempts[0].success is False
            assert ts.attempts[1].success is True
        finally:
            for key in ["FAKE_RESPONSE_FILE", "FAKE_COUNTER_FILE"]:
                os.environ.pop(key, None)

    def test_rate_limit_retries_and_succeeds(self, tmp_path: Path):
        """Rate limit triggers backoff retry, then succeeds."""
        config = _make_e2e_config(tmp_path, max_retries=3)
        state = ExecutorState(config)
        _write_tasks(tmp_path)
        tasks = parse_tasks(tmp_path / "spec" / "tasks.md")
        task = tasks[0]

        resp_dir = tmp_path / "responses"
        resp_dir.mkdir(exist_ok=True)
        base = resp_dir / "ratelimit"
        (resp_dir / "ratelimit.0").write_text("you've hit your limit")
        (resp_dir / "ratelimit.1").write_text("Done!\nTASK_COMPLETE")

        counter = tmp_path / "counter.txt"
        os.environ["FAKE_RESPONSE_FILE"] = str(base)
        os.environ["FAKE_COUNTER_FILE"] = str(counter)
        os.environ.pop("FAKE_EXIT_CODE", None)
        os.environ.pop("FAKE_STDERR", None)
        os.environ.pop("FAKE_DELAY", None)

        try:
            result = run_with_retries(task, config, state)
            assert result is True

            ts = state.get_task_state(task.id)
            assert len(ts.attempts) == 2
            assert ts.attempts[0].success is False
            assert ts.attempts[0].error_code is not None
            assert ts.attempts[0].error_code.value == "RATE_LIMIT"
        finally:
            for key in ["FAKE_RESPONSE_FILE", "FAKE_COUNTER_FILE"]:
                os.environ.pop(key, None)

    def test_all_attempts_fail(self, tmp_path: Path):
        """All 3 attempts fail — task gets blocked."""
        config = _make_e2e_config(tmp_path, max_retries=2)
        state = ExecutorState(config)
        tasks_file = _write_tasks(tmp_path)
        tasks = parse_tasks(tasks_file)
        task = tasks[0]

        response_file = _write_response(
            tmp_path, "fail.txt", "Cannot do this.\nTASK_FAILED: impossible"
        )

        os.environ["FAKE_RESPONSE_FILE"] = str(response_file)
        os.environ.pop("FAKE_COUNTER_FILE", None)
        os.environ.pop("FAKE_EXIT_CODE", None)
        os.environ.pop("FAKE_STDERR", None)
        os.environ.pop("FAKE_DELAY", None)

        try:
            result = run_with_retries(task, config, state)
            assert result == "SKIP"  # default on_task_failure is "skip"

            ts = state.get_task_state(task.id)
            assert len(ts.attempts) == 2
            assert all(not a.success for a in ts.attempts)
        finally:
            os.environ.pop("FAKE_RESPONSE_FILE", None)
```

**Step 2: Run tests to verify they pass**

Run: `uv run pytest tests/test_e2e.py::TestE2ERetry -v -m "slow"`
Expected: All 3 tests PASS

**Step 3: Commit**

```bash
git add tests/test_e2e.py
git commit -m "test: add E2E retry tests — failure recovery and rate limit backoff"
```

---

### Task 6: E2E test — multi-task dependencies and validation

**Files:**
- Modify: `tests/test_e2e.py`

**Context:** Test dependency ordering (TASK-002 depends on TASK-001) and pre-run validation rejecting invalid tasks.md.

**Step 1: Write the tests**

Add to `tests/test_e2e.py`:

```python
from spec_runner.task import get_next_tasks, resolve_dependencies, update_task_status
from spec_runner.validate import validate_tasks


MULTI_TASKS_MD = """\
# Tasks

## TASK-001: Setup database [p0] [todo]
- Est: 1h
- Checklist:
  - [ ] Create schema

## TASK-002: Add API endpoints [p1] [todo]
- depends_on: TASK-001
- Est: 2h
- Checklist:
  - [ ] Create REST endpoints
"""

INVALID_TASKS_MD = """\
# Tasks

## TASK-001: First task [p0] [todo]
- depends_on: TASK-999
- Checklist:
  - [ ] Do something
"""


@pytest.mark.slow
class TestE2EMultiTask:
    """Multi-task and dependency scenarios."""

    def test_dependency_ordering(self, tmp_path: Path):
        """TASK-002 depends on TASK-001 — only TASK-001 is next."""
        tasks_file = _write_tasks(tmp_path, MULTI_TASKS_MD)
        tasks = parse_tasks(tasks_file)
        resolve_dependencies(tasks)

        next_tasks = get_next_tasks(tasks)
        assert len(next_tasks) == 1
        assert next_tasks[0].id == "TASK-001"

        # After TASK-001 done, TASK-002 becomes available
        update_task_status(tasks_file, "TASK-001", "done")
        tasks = parse_tasks(tasks_file)
        resolve_dependencies(tasks)
        next_tasks = get_next_tasks(tasks)
        assert len(next_tasks) == 1
        assert next_tasks[0].id == "TASK-002"

    def test_validation_catches_missing_dependency(self, tmp_path: Path):
        """Invalid tasks.md with missing dependency ref triggers error."""
        tasks_file = _write_tasks(tmp_path, INVALID_TASKS_MD)
        result = validate_tasks(tasks_file)
        assert not result.ok
        assert any("TASK-999" in e for e in result.errors)
```

**Step 2: Run tests**

Run: `uv run pytest tests/test_e2e.py::TestE2EMultiTask -v -m "slow"`
Expected: PASS

**Step 3: Commit**

```bash
git add tests/test_e2e.py
git commit -m "test: add E2E tests for dependency ordering and validation"
```

---

### Task 7: Exports, docs, final cleanup

**Files:**
- Modify: `src/spec_runner/__init__.py`
- Modify: `CLAUDE.md`

**Step 1: Update `__init__.py` exports**

Add `classify_retry_strategy` and `compute_retry_delay` to imports from executor and to `__all__`:

```python
from .executor import (
    classify_retry_strategy,
    compute_retry_delay,
    execute_task,
    run_with_retries,
)
```

And add to `__all__`:
```python
    "classify_retry_strategy",
    "compute_retry_delay",
```

**Step 2: Update CLAUDE.md**

Update the test count and mention the new retry strategy and E2E tests.

**Step 3: Run full suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass (including slow E2E tests)

Run: `uv run ruff check .`
Expected: All checks passed

**Step 4: Commit**

```bash
git add src/spec_runner/__init__.py CLAUDE.md
git commit -m "docs: update exports and CLAUDE.md for Phase 7 retry + E2E"
```
