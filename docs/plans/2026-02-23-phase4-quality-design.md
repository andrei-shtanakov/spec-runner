# Design: Phase 4 — Quality

**Date:** 2026-02-23
**Status:** Approved
**Goal:** Enriched review prompts, review verdict tracking in state DB, interactive HITL approval gate.
**Constraint:** Same CLI interface (only new flags added). Execution logic untouched.

## 1. Enriched Review Prompt

### Current state

`build_review_prompt()` in `hooks.py` sends: task ID, task name, changed file names, diff stat summary. The actual code diff, checklist, test results, and retry context are missing.

### New context

The review prompt will include:

| Context | Source | Truncation |
|---------|--------|------------|
| Task checklist | `parse_tasks()` → `task.checklist` | Full (short) |
| Full git diff | `git diff -p HEAD~1` | 30KB max |
| Test results | Captured from test run stdout/stderr | 2KB summary |
| Lint status | "clean" or auto-fix summary | 200 chars |
| Previous errors | `state.tasks[id].attempts[-1].error` | 1KB |

### Changes to `build_review_prompt()`

New parameters:

```python
def build_review_prompt(
    task: Task,
    config: ExecutorConfig,
    cli_name: str = "",
    test_output: str | None = None,    # NEW
    lint_output: str | None = None,     # NEW
    previous_error: str | None = None,  # NEW
) -> str:
```

The built-in fallback prompt template gains new sections:

```
## Task Checklist
- [ ] Item 1
- [x] Item 2

## Code Changes (git diff)
<actual diff, truncated to 30KB>

## Test Results
15 passed, 0 failed

## Lint Status
Clean (or: auto-fixed 2 issues)

## Previous Errors (if retry)
<error context from last attempt>
```

### Changes to `post_done_hook()`

`post_done_hook()` currently runs tests, lint, then review — but discards test/lint output. It will now capture and forward:

```python
# Capture test output
test_result = subprocess.run(test_cmd, capture_output=True, text=True, ...)
test_output = test_result.stdout + test_result.stderr

# Capture lint output
lint_result = subprocess.run(lint_cmd, capture_output=True, text=True, ...)
lint_output = lint_result.stdout

# Pass to review
review_ok, review_error, review_output = run_code_review(
    task, config,
    test_output=test_output[:2048],
    lint_output=lint_output[:200],
    previous_error=last_error,
)
```

## 2. Review Verdict Tracking

### New fields in `TaskAttempt`

```python
@dataclass
class TaskAttempt:
    # ... existing fields ...
    review_status: str | None = None     # passed, fixed, failed, skipped, rejected
    review_findings: str | None = None   # Truncated review output (2KB max)
```

### Schema migration

```sql
ALTER TABLE attempts ADD COLUMN review_status TEXT;
ALTER TABLE attempts ADD COLUMN review_findings TEXT;
```

### New `ReviewVerdict` enum

```python
class ReviewVerdict(str, Enum):
    PASSED = "passed"
    FIXED = "fixed"
    FAILED = "failed"
    SKIPPED = "skipped"
    REJECTED = "rejected"  # HITL rejected
```

### Return value change for `run_code_review()`

Currently returns `(bool, str | None)`. Changes to:

```python
def run_code_review(...) -> tuple[ReviewVerdict, str | None, str | None]:
    """Returns (verdict, error_message, review_output)."""
```

### Status display

`cmd_status()` shows review verdict per task:

```
TASK-001: success (review: passed)
TASK-002: success (review: fixed, 2 issues)
TASK-003: failed  (review: rejected)
```

## 3. HITL Approval Gate

### CLI flag

```bash
spec-runner run --all --hitl-review     # Enable interactive approval
spec-runner run --all                    # Default: auto-approve (existing behavior)
```

### Config

```yaml
executor:
  hitl_review: false    # Default off, CLI flag overrides
```

### Flow

```
Task succeeds → tests pass → lint passes → review runs → HITL gate
```

At the HITL gate:

1. Display review findings in terminal (formatted, colored)
2. Show interactive prompt:

```
═══════════════════════════════════════
📋 Review: TASK-003 — Add API endpoints
═══════════════════════════════════════

2 issues found:
  MAJOR: No error handling in API call (api.py:45)
  MINOR: Unused import (utils.py:1)

Verdict: REVIEW_FIXED (auto-fixed 1 issue)

  [a]pprove  [r]eject  [f]ix-and-retry  [s]kip
> _
```

3. Actions:

| Key | Action | Effect |
|-----|--------|--------|
| `a` | Approve | Proceed to commit/merge. `review_status = "passed"` |
| `r` | Reject | Mark task failed. `review_status = "rejected"`. New error code `REVIEW_REJECTED` |
| `f` | Fix and retry | Re-run task with review findings as error context. Uses existing retry mechanism |
| `s` | Skip | Proceed without review verdict. `review_status = "skipped"` |

### Implementation in `post_done_hook()`

```python
if config.hitl_review:
    # Display findings
    print(format_review_findings(task, review_output))
    # Interactive prompt
    choice = prompt_hitl_verdict()
    if choice == "reject":
        return False  # Task fails
    elif choice == "fix-and-retry":
        # Store review findings as error context for retry
        raise ReviewRetryRequested(review_output)
```

### Parallel mode

In `--parallel` mode, `--hitl-review` is incompatible (can't do interactive prompts for multiple concurrent tasks). If both flags are set, print a warning and ignore `--hitl-review`.

### TUI mode

In `--tui` mode, `--hitl-review` is also incompatible (TUI owns the screen). Same behavior — warn and ignore.

## 4. New Error Code

```python
class ErrorCode(str, Enum):
    # ... existing codes ...
    REVIEW_REJECTED = "REVIEW_REJECTED"
```

`REVIEW_REJECTED` is a **permanent** error — no automatic retries (unless user explicitly chooses fix-and-retry at the HITL gate).

## 5. New CLI Flags

```bash
spec-runner run --all --hitl-review    # Interactive approval gate
```

## 6. Config Additions

```yaml
executor:
  hitl_review: false     # Enable HITL gate (default: off)
```

## 7. Files Changed

| File | Change |
|------|--------|
| `hooks.py` | Enrich `build_review_prompt()`, capture test/lint output, add HITL gate logic, `run_code_review()` returns 3-tuple |
| `state.py` | Add `ReviewVerdict` enum, `review_status`/`review_findings` to TaskAttempt, schema migration |
| `config.py` | Add `hitl_review` field |
| `executor.py` | Add `--hitl-review` flag, pass to config, update `cmd_status()` for review display |
| `prompt.py` | No changes |
| `runner.py` | No changes |
| `task.py` | No changes |
| `logging.py` | No changes |
| `tui.py` | No changes |

## 8. What Does NOT Change

- Sequential/parallel execution path logic
- Task parsing, dependency resolution
- Prompt building for task execution
- Structured logging, TUI dashboard
- Review template system (claude/codex/ollama/llama templates)
- `--no-review` flag behavior
- Budget enforcement, token tracking
