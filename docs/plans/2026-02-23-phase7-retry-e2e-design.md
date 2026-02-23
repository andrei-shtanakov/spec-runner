# Phase 7: Smart Retry & E2E Integration Tests

**Goal:** Make retry logic error-aware with exponential backoff for rate limits, and add E2E integration tests using a fake CLI to validate the full execution pipeline.

**Scope:** Two features, ~350 lines of new code, 1 new test file, 1 new fixture.

---

## 1. Smart Retry with Exponential Backoff

### Problem

Current retry has a fixed 5-second delay regardless of error type. Rate limit errors (`RATE_LIMIT`) cause immediate abort with no retry. This wastes task progress when a transient API limit is hit.

### Error Categories

Three categories drive retry behavior:

| Category | ErrorCodes | Strategy | Delays |
|----------|-----------|----------|--------|
| **Rate limit** | RATE_LIMIT | Exponential backoff | 30s, 60s, 120s, 240s (cap 300s) |
| **Transient** | TEST_FAILURE, LINT_FAILURE, TASK_FAILED, TIMEOUT, UNKNOWN | Linear backoff | 5s, 10s, 15s |
| **Fatal** | HOOK_FAILURE, REVIEW_REJECTED, BUDGET_EXCEEDED, INTERRUPTED | Immediate stop | — |

### New functions

**`classify_retry_strategy(error_code: str) -> str`**

Returns one of: `"backoff_exponential"`, `"backoff_linear"`, `"fatal"`.

Mapping is a simple dict lookup with `"backoff_linear"` as default for unknown codes.

**`compute_retry_delay(error_code: str, attempt: int, base_delay: int = 5) -> float`**

```
exponential: min(30.0 * (2 ** attempt), 300.0)
linear:      base_delay * (attempt + 1)
fatal:       0.0
```

### Changes to `run_with_retries()`

Current flow (executor.py:376-476):
1. RATE_LIMIT → `return "API_ERROR"` (immediate exit)
2. HOOK_FAILURE → `return False` (immediate exit)
3. All other failures → `time.sleep(config.retry_delay_seconds)` → next attempt

New flow:
1. After `execute_task()`, classify the error code
2. Fatal → immediate exit (same as now for HOOK_FAILURE, REVIEW_REJECTED)
3. Rate limit → compute exponential delay, sleep, retry
4. Transient → compute linear delay, sleep, retry
5. Log the computed delay before sleeping

### What we don't do

- No jitter (randomized delays) — single-process tool, no thundering herd
- No per-error-type config — YAGNI, the defaults are sensible
- No structured retry context JSON — current RetryContext is sufficient

### New code

- Add `classify_retry_strategy()` and `compute_retry_delay()` to `executor.py` (~25 lines)
- Modify `run_with_retries()` — replace fixed sleep + API_ERROR exit with strategy-based logic (~20 lines changed)

---

## 2. E2E Integration Tests

### Problem

All 348 tests mock `subprocess.run`. No test validates the real pipeline: parse tasks → build prompt → execute subprocess → parse output → update state. Regressions in the glue between components go undetected.

### Fake CLI: `tests/fixtures/fake_claude.sh`

A bash script that acts as a drop-in replacement for `claude`:

```bash
#!/usr/bin/env bash
# Env vars control behavior:
#   FAKE_RESPONSE_FILE — path to response text
#   FAKE_EXIT_CODE — exit code (default 0)
#   FAKE_DELAY — sleep seconds (default 0)
#   FAKE_STDERR — text for stderr (token usage, errors)

sleep "${FAKE_DELAY:-0}"
if [ -n "$FAKE_RESPONSE_FILE" ]; then
    cat "$FAKE_RESPONSE_FILE"
else
    echo "No response configured"
fi
echo -n "${FAKE_STDERR:-}" >&2
exit "${FAKE_EXIT_CODE:-0}"
```

Ignores all CLI arguments (`-p`, `--model`, `--allowedTools`, etc.) — just returns the canned response.

### Test file: `tests/test_e2e.py`

All tests marked `@pytest.mark.slow`. Each test:

1. Creates `tmp_path/spec/tasks.md` with minimal task definitions
2. Builds `ExecutorConfig` pointing `claude_command` at `fake_claude.sh`
3. Prepares response files in `tmp_path/responses/`
4. Calls `execute_task()` or `run_with_retries()` directly (not via CLI `main()`)
5. Asserts on state.db contents and filesystem side effects

### Test scenarios

| Test | What it validates |
|------|-------------------|
| `test_single_task_success` | Full cycle: tasks.md → parse → execute → state.db shows done |
| `test_single_task_failure_and_retry` | First attempt TASK_FAILED, second TASK_COMPLETE, state.db has 2 attempts |
| `test_rate_limit_backoff` | Fake returns "rate limit exceeded", retried with backoff, then success |
| `test_multi_task_dependencies` | TASK-001 → TASK-002 (depends_on), sequential execution in correct order |
| `test_validation_before_run` | Invalid tasks.md → run refuses to start |
| `test_hooks_git_and_commit` | With git hooks enabled — verifies branch created and commit made |

### Fixture design

Response files per scenario live alongside the test. For multi-attempt scenarios, the fake CLI reads a counter file to return different responses on each invocation:

```bash
# In fake_claude.sh, if FAKE_COUNTER_FILE is set:
COUNT=$(cat "$FAKE_COUNTER_FILE" 2>/dev/null || echo 0)
echo $((COUNT + 1)) > "$FAKE_COUNTER_FILE"
FAKE_RESPONSE_FILE="${FAKE_RESPONSE_FILE}.${COUNT}"
```

This allows `response.0` (failure) → `response.1` (success) sequences without complex logic.

### What we don't do

- No testing via `subprocess.run(["spec-runner", "run"])` — too brittle, hard to control config
- No real Claude CLI — cost + flaky
- No YAML scenario files — env vars sufficient for our needs

### New code

- Create `tests/fixtures/fake_claude.sh` (~20 lines)
- Create `tests/test_e2e.py` (~250 lines, 6 test cases)

---

## Summary

| Feature | New files | Modified files | ~Lines |
|---------|-----------|----------------|--------|
| Smart retry | — | `executor.py` | ~45 |
| E2E tests | `tests/fixtures/fake_claude.sh`, `tests/test_e2e.py` | — | ~270 |
| **Total** | **2 new files** | **1 modified** | **~315** |

### Implementation order

1. **Smart retry** — small change to executor.py, enables the rate limit E2E test
2. **E2E tests** — builds on retry changes, validates full pipeline

### Testing strategy

- `test_execution.py` — unit tests for `compute_retry_delay()` and `classify_retry_strategy()`
- `test_e2e.py` — integration tests with fake CLI (`@pytest.mark.slow`)
