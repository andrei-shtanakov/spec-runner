# Phase 4 — Quality Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Enrich review prompts with full diff/checklist/test results, track review verdicts in state DB, add interactive HITL approval gate.

**Architecture:** Extend `build_review_prompt()` and `run_code_review()` in hooks.py with richer context and structured return values. Add `ReviewVerdict` enum and review fields to `TaskAttempt` in state.py with schema migration. Add `--hitl-review` CLI flag that enables interactive approval prompt after review runs.

**Tech Stack:** Python 3.10+ stdlib, existing structlog logging, SQLite state persistence

---

### Task 1: Add ReviewVerdict enum and review fields to state.py

**Files:**
- Modify: `src/spec_runner/state.py:19-30` (ErrorCode enum), `src/spec_runner/state.py:33-45` (TaskAttempt)
- Test: `tests/test_state.py`

**Step 1: Write the failing tests**

Add to `tests/test_state.py`:

```python
from spec_runner.state import ReviewVerdict


class TestReviewVerdict:
    def test_review_verdict_values(self):
        assert ReviewVerdict.PASSED == "passed"
        assert ReviewVerdict.FIXED == "fixed"
        assert ReviewVerdict.FAILED == "failed"
        assert ReviewVerdict.SKIPPED == "skipped"
        assert ReviewVerdict.REJECTED == "rejected"

    def test_review_verdict_is_string(self):
        assert isinstance(ReviewVerdict.PASSED, str)


class TestReviewFieldsInTaskAttempt:
    def test_review_fields_default_none(self):
        attempt = TaskAttempt(
            timestamp="2026-01-01T00:00:00",
            success=True,
            duration_seconds=10.0,
        )
        assert attempt.review_status is None
        assert attempt.review_findings is None

    def test_review_fields_from_kwargs(self):
        attempt = TaskAttempt(
            timestamp="2026-01-01T00:00:00",
            success=True,
            duration_seconds=10.0,
            review_status="passed",
            review_findings="No issues found",
        )
        assert attempt.review_status == "passed"
        assert attempt.review_findings == "No issues found"

    def test_review_fields_persist_to_sqlite(self, tmp_path):
        from spec_runner.config import ExecutorConfig

        config = ExecutorConfig(
            state_file=tmp_path / "state.db",
            project_root=tmp_path,
        )
        state = ExecutorState(config)
        state.record_attempt(
            task_id="TASK-001",
            success=True,
            duration=10.0,
            review_status="fixed",
            review_findings="Auto-fixed 1 issue",
        )
        state.close()

        # Re-open and verify persistence
        state2 = ExecutorState(config)
        task = state2.tasks["TASK-001"]
        assert task.attempts[0].review_status == "fixed"
        assert task.attempts[0].review_findings == "Auto-fixed 1 issue"
        state2.close()
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_state.py::TestReviewVerdict -v`
Expected: FAIL with `ImportError: cannot import name 'ReviewVerdict'`

**Step 3: Implement changes**

3a. Add `ReviewVerdict` enum to `state.py` (after `ErrorCode` enum, around line 30):

```python
class ReviewVerdict(str, Enum):
    """Review result classification."""

    PASSED = "passed"
    FIXED = "fixed"
    FAILED = "failed"
    SKIPPED = "skipped"
    REJECTED = "rejected"
```

3b. Add `REVIEW_REJECTED` to `ErrorCode` enum:

```python
    REVIEW_REJECTED = "REVIEW_REJECTED"
```

3c. Add review fields to `TaskAttempt` dataclass (after `cost_usd`):

```python
    review_status: str | None = None
    review_findings: str | None = None
```

3d. Update `record_attempt()` to accept and store review fields. Add parameters:

```python
def record_attempt(
    self,
    task_id: str,
    success: bool,
    duration: float,
    error: str | None = None,
    claude_output: str | None = None,
    error_code: ErrorCode | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
    review_status: str | None = None,       # NEW
    review_findings: str | None = None,      # NEW
) -> None:
```

Pass these to `TaskAttempt()` constructor and include in the SQLite INSERT statement.

3e. Add schema migration for the new columns (in `_ensure_schema` or migration method):

```python
# Migrate: add review columns if missing
cursor.execute("PRAGMA table_info(attempts)")
columns = {row[1] for row in cursor.fetchall()}
if "review_status" not in columns:
    cursor.execute("ALTER TABLE attempts ADD COLUMN review_status TEXT")
    cursor.execute("ALTER TABLE attempts ADD COLUMN review_findings TEXT")
```

3f. Update the `_load_attempt` / row-to-TaskAttempt conversion to include the new columns.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_state.py -v`
Expected: All pass

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All 225 tests pass + new tests

**Step 6: Commit**

```bash
git add src/spec_runner/state.py tests/test_state.py
git commit -m "feat: add ReviewVerdict enum and review fields to TaskAttempt"
```

---

### Task 2: Add hitl_review config field and CLI flag

**Files:**
- Modify: `src/spec_runner/config.py:73-130` (ExecutorConfig)
- Modify: `src/spec_runner/config.py:169-232` (load_config_from_yaml)
- Modify: `src/spec_runner/config.py:235-283` (build_config)
- Modify: `src/spec_runner/executor.py` (main argparse)
- Test: `tests/test_config.py`

**Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
class TestHitlReviewConfig:
    def test_hitl_review_default_false(self):
        config = ExecutorConfig()
        assert config.hitl_review is False

    def test_hitl_review_from_kwargs(self):
        config = ExecutorConfig(hitl_review=True)
        assert config.hitl_review is True
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py::TestHitlReviewConfig -v`
Expected: FAIL

**Step 3: Implement changes**

3a. Add `hitl_review` field to `ExecutorConfig` (after `run_review` field):

```python
    hitl_review: bool = False  # Interactive approval gate after review
```

3b. In `load_config_from_yaml()`, add to the return dict:

```python
            "hitl_review": executor_config.get("hitl_review"),
```

3c. In `build_config()`, add CLI override:

```python
    if hasattr(args, "hitl_review") and getattr(args, "hitl_review", False):
        config_kwargs["hitl_review"] = True
```

3d. In `main()` in executor.py, add `--hitl-review` flag to `common` parser (near the `--no-review` flag):

```python
    common.add_argument(
        "--hitl-review",
        action="store_true",
        help="Enable interactive approval gate after code review",
    )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py::TestHitlReviewConfig -v`
Expected: PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/spec_runner/config.py src/spec_runner/executor.py tests/test_config.py
git commit -m "feat: add hitl_review config field and --hitl-review CLI flag"
```

---

### Task 3: Enrich build_review_prompt() with full context

**Files:**
- Modify: `src/spec_runner/hooks.py:204-282` (build_review_prompt)
- Test: `tests/test_hooks.py`

**Step 1: Write the failing tests**

Add to `tests/test_hooks.py`:

```python
class TestBuildReviewPrompt:
    def test_includes_task_checklist(self):
        task = _make_task()
        task.checklist = ["Implement API endpoint", "Add error handling", "Write tests"]
        config = _make_config()
        prompt = build_review_prompt(task, config)
        assert "Implement API endpoint" in prompt
        assert "Add error handling" in prompt

    def test_includes_full_diff(self):
        task = _make_task()
        config = _make_config()
        with patch("spec_runner.hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="diff --git a/foo.py\n+new_line\n",
                stderr="",
                returncode=0,
            )
            prompt = build_review_prompt(task, config)
        assert "diff --git" in prompt or "foo.py" in prompt

    def test_includes_test_output(self):
        task = _make_task()
        config = _make_config()
        prompt = build_review_prompt(
            task, config,
            test_output="15 passed, 0 failed in 2.1s",
        )
        assert "15 passed" in prompt

    def test_includes_previous_error(self):
        task = _make_task()
        config = _make_config()
        prompt = build_review_prompt(
            task, config,
            previous_error="TypeError: expected str, got int",
        )
        assert "TypeError" in prompt

    def test_truncates_long_diff(self):
        task = _make_task()
        config = _make_config()
        with patch("spec_runner.hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="x" * 50000,
                stderr="",
                returncode=0,
            )
            prompt = build_review_prompt(task, config)
        assert len(prompt) < 40000  # Diff truncated to ~30KB
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hooks.py::TestBuildReviewPrompt -v`
Expected: FAIL (signature mismatch or missing parameters)

**Step 3: Implement changes**

Update `build_review_prompt()` signature:

```python
def build_review_prompt(
    task: Task,
    config: ExecutorConfig,
    cli_name: str = "",
    test_output: str | None = None,
    lint_output: str | None = None,
    previous_error: str | None = None,
) -> str:
```

In the function body, after getting changed files and diff stat:

3a. Get full diff (replacing or supplementing the stat-only diff):

```python
    # Full diff for review context (truncated to 30KB)
    diff_result = subprocess.run(
        ["git", "diff", "-p", "HEAD~1"],
        capture_output=True, text=True,
        cwd=config.project_root,
    )
    full_diff = diff_result.stdout[:30_000]
    if len(diff_result.stdout) > 30_000:
        full_diff += "\n... (truncated)"
```

3b. Build checklist section:

```python
    checklist_section = ""
    if task.checklist:
        items = "\n".join(f"- {item}" for item in task.checklist)
        checklist_section = f"\n## Task Checklist\n{items}\n"
```

3c. Build test/lint/error sections:

```python
    test_section = ""
    if test_output:
        test_section = f"\n## Test Results\n{test_output[:2048]}\n"

    lint_section = ""
    if lint_output:
        lint_section = f"\n## Lint Status\n{lint_output[:200]}\n"

    error_section = ""
    if previous_error:
        error_section = f"\n## Previous Errors\n{previous_error[:1024]}\n"
```

3d. Include these sections in the fallback prompt template (after the changed files/diff sections).

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_hooks.py::TestBuildReviewPrompt -v`
Expected: All PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/spec_runner/hooks.py tests/test_hooks.py
git commit -m "feat: enrich review prompt with full diff, checklist, test results"
```

---

### Task 4: Update run_code_review() to return ReviewVerdict

**Files:**
- Modify: `src/spec_runner/hooks.py:284-389` (run_code_review)
- Test: `tests/test_hooks.py`

**Step 1: Write the failing tests**

Add to `tests/test_hooks.py`:

```python
from spec_runner.state import ReviewVerdict


class TestRunCodeReview:
    def test_returns_three_tuple(self):
        task = _make_task()
        config = _make_config(run_review=True)
        with patch("spec_runner.hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="All good. REVIEW_PASSED",
                stderr="",
                returncode=0,
            )
            with patch("spec_runner.hooks.build_review_prompt", return_value="prompt"):
                verdict, error, output = run_code_review(task, config)
        assert verdict == ReviewVerdict.PASSED
        assert error is None
        assert "REVIEW_PASSED" in output

    def test_review_fixed_verdict(self):
        task = _make_task()
        config = _make_config(run_review=True)
        with patch("spec_runner.hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="Fixed issue. REVIEW_FIXED",
                stderr="",
                returncode=0,
            )
            with patch("spec_runner.hooks.build_review_prompt", return_value="prompt"):
                verdict, error, output = run_code_review(task, config)
        assert verdict == ReviewVerdict.FIXED

    def test_review_failed_verdict(self):
        task = _make_task()
        config = _make_config(run_review=True)
        with patch("spec_runner.hooks.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="MAJOR issue found. REVIEW_FAILED",
                stderr="",
                returncode=0,
            )
            with patch("spec_runner.hooks.build_review_prompt", return_value="prompt"):
                verdict, error, output = run_code_review(task, config)
        assert verdict == ReviewVerdict.FAILED
        assert output is not None
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hooks.py::TestRunCodeReview -v`
Expected: FAIL (returns 2-tuple, not 3-tuple)

**Step 3: Implement changes**

Change `run_code_review()` signature and return type:

```python
def run_code_review(
    task: Task,
    config: ExecutorConfig,
    test_output: str | None = None,
    lint_output: str | None = None,
    previous_error: str | None = None,
) -> tuple[ReviewVerdict, str | None, str | None]:
    """Run code review. Returns (verdict, error_message, review_output)."""
```

Update the marker detection section to return `ReviewVerdict` values:

```python
    output_upper = output.upper()
    if "REVIEW_PASSED" in output_upper:
        return ReviewVerdict.PASSED, None, output
    elif "REVIEW_FIXED" in output_upper:
        # Auto-commit fixes
        subprocess.run(["git", "add", "-A"], cwd=config.project_root)
        subprocess.run(["git", "commit", "-m", f"{task.id}: code review fixes"], cwd=config.project_root)
        return ReviewVerdict.FIXED, None, output
    elif "REVIEW_FAILED" in output_upper:
        return ReviewVerdict.FAILED, "Review found issues", output
    else:
        return ReviewVerdict.PASSED, None, output  # No marker = implicit pass
```

Update error/timeout returns:

```python
    except subprocess.TimeoutExpired:
        return ReviewVerdict.FAILED, "Review timed out", None
    except Exception as e:
        return ReviewVerdict.FAILED, str(e), None
```

Pass the new parameters through to `build_review_prompt()`:

```python
    prompt = build_review_prompt(
        task, config, cli_name=review_cmd,
        test_output=test_output,
        lint_output=lint_output,
        previous_error=previous_error,
    )
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_hooks.py::TestRunCodeReview -v`
Expected: All PASS

**Step 5: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 6: Commit**

```bash
git add src/spec_runner/hooks.py tests/test_hooks.py
git commit -m "feat: run_code_review returns ReviewVerdict with review output"
```

---

### Task 5: Wire enriched review into post_done_hook()

**Files:**
- Modify: `src/spec_runner/hooks.py:392-587` (post_done_hook)
- Test: `tests/test_hooks.py`

**Step 1: Implement changes**

In `post_done_hook()`, update the test/lint/review section:

5a. Capture test output (around lines 405-420):

```python
    test_output_str: str | None = None
    if config.run_tests_on_done:
        logger.info("Running tests")
        result = subprocess.run(
            config.test_command, shell=True,
            capture_output=True, text=True,
            cwd=config.project_root,
        )
        test_output_str = (result.stdout + result.stderr)[:2048]
        if result.returncode != 0:
            logger.error("Tests failed")
            logger.error("Test stderr", stderr=result.stderr[:500])
            return False, f"Tests failed:\n{result.stdout + result.stderr}"
        logger.info("Tests passed")
```

5b. Capture lint output (around lines 423-464):

```python
    lint_output_str: str | None = None
    # ... existing lint logic ...
    lint_output_str = lint_result.stdout[:200] if lint_result else "clean"
```

5c. Get previous error for review context:

```python
    previous_error: str | None = None
    # Get last error if this was a retry
    state = ExecutorState(config)
    ts = state.tasks.get(task.id)
    if ts and ts.attempts:
        last = ts.attempts[-1]
        if not last.success and last.error:
            previous_error = last.error[:1024]
    state.close()
```

5d. Update the review call (around lines 467-472):

```python
    review_verdict = ReviewVerdict.SKIPPED
    review_output: str | None = None
    if config.run_review:
        logger.info("Running code review")
        review_verdict, review_error, review_output = run_code_review(
            task, config,
            test_output=test_output_str,
            lint_output=lint_output_str,
            previous_error=previous_error,
        )
        if review_verdict == ReviewVerdict.FAILED:
            logger.warning("Review found issues", error=review_error)
```

5e. Return review info so executor can store it in state. Change return type to include review data:

```python
    return True, None, review_verdict.value, (review_output or "")[:2048]
```

Note: This changes the return type of `post_done_hook()` from `tuple[bool, str | None]` to `tuple[bool, str | None, str, str]`. Update all callers in executor.py accordingly.

**Step 2: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass (fix any broken tests due to return type change)

**Step 3: Commit**

```bash
git add src/spec_runner/hooks.py tests/test_hooks.py
git commit -m "feat: wire enriched review context into post_done_hook"
```

---

### Task 6: Update executor.py to store review results in state

**Files:**
- Modify: `src/spec_runner/executor.py` (execute_task, cmd_status)

**Step 1: Implement changes**

6a. In `execute_task()` and/or `_run_tasks()`, where `post_done_hook()` is called and `record_attempt()` is called, pass review data through:

```python
    # After post_done_hook returns
    success, hook_error, review_status, review_findings = post_done_hook(task, config, task_success)

    # When recording attempt
    state.record_attempt(
        task_id=task.id,
        success=success,
        duration=duration,
        error=hook_error,
        # ... existing fields ...
        review_status=review_status,
        review_findings=review_findings[:2048] if review_findings else None,
    )
```

6b. Update `cmd_status()` to show review verdicts. After showing task status, add review info:

```python
    # In the task history display section
    if attempt.review_status:
        print(f"    Review: {attempt.review_status}")
```

**Step 2: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 3: Commit**

```bash
git add src/spec_runner/executor.py
git commit -m "feat: store review verdicts in state, show in status output"
```

---

### Task 7: Implement HITL approval gate

**Files:**
- Modify: `src/spec_runner/hooks.py` (post_done_hook, new helper functions)
- Test: `tests/test_hooks.py`

**Step 1: Write the failing tests**

Add to `tests/test_hooks.py`:

```python
class TestHitlReviewGate:
    def test_format_review_findings(self):
        from spec_runner.hooks import format_review_findings
        output = format_review_findings("TASK-001", "Add API", "MAJOR: No error handling\nMINOR: Unused import")
        assert "TASK-001" in output
        assert "MAJOR" in output

    def test_prompt_hitl_approve(self):
        from spec_runner.hooks import prompt_hitl_verdict
        with patch("builtins.input", return_value="a"):
            result = prompt_hitl_verdict()
        assert result == "approve"

    def test_prompt_hitl_reject(self):
        from spec_runner.hooks import prompt_hitl_verdict
        with patch("builtins.input", return_value="r"):
            result = prompt_hitl_verdict()
        assert result == "reject"

    def test_prompt_hitl_fix(self):
        from spec_runner.hooks import prompt_hitl_verdict
        with patch("builtins.input", return_value="f"):
            result = prompt_hitl_verdict()
        assert result == "fix"

    def test_prompt_hitl_skip(self):
        from spec_runner.hooks import prompt_hitl_verdict
        with patch("builtins.input", return_value="s"):
            result = prompt_hitl_verdict()
        assert result == "skip"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_hooks.py::TestHitlReviewGate -v`
Expected: FAIL with `ImportError`

**Step 3: Implement helper functions**

Add to `hooks.py`:

```python
def format_review_findings(task_id: str, task_name: str, review_output: str) -> str:
    """Format review findings for HITL display."""
    separator = "=" * 50
    return (
        f"\n{separator}\n"
        f"Review: {task_id} — {task_name}\n"
        f"{separator}\n\n"
        f"{review_output[:3000]}\n"
    )


def prompt_hitl_verdict() -> str:
    """Prompt user for HITL review verdict. Returns: approve, reject, fix, skip."""
    print("\n  [a]pprove  [r]eject  [f]ix-and-retry  [s]kip")
    while True:
        choice = input("> ").strip().lower()
        if choice in ("a", "approve"):
            return "approve"
        elif choice in ("r", "reject"):
            return "reject"
        elif choice in ("f", "fix"):
            return "fix"
        elif choice in ("s", "skip"):
            return "skip"
        print("  Invalid choice. Use: a, r, f, or s")
```

**Step 4: Wire HITL gate into post_done_hook()**

After the review runs and before auto-commit, add the HITL gate:

```python
    # HITL approval gate
    if config.hitl_review and review_output:
        print(format_review_findings(task.id, task.name, review_output))
        choice = prompt_hitl_verdict()
        if choice == "reject":
            logger.info("HITL rejected task", task_id=task.id)
            return False, "Review rejected by human", ReviewVerdict.REJECTED.value, review_output[:2048]
        elif choice == "fix":
            logger.info("HITL requested fix-and-retry", task_id=task.id)
            return False, f"Fix requested. Review findings:\n{review_output[:1024]}", ReviewVerdict.REJECTED.value, review_output[:2048]
        elif choice == "skip":
            review_verdict = ReviewVerdict.SKIPPED
            logger.info("HITL skipped review", task_id=task.id)
        # "approve" falls through to normal commit flow
```

**Step 5: Add parallel/TUI mode guard in executor.py**

In `cmd_run()`, warn if `--hitl-review` is combined with `--parallel` or `--tui`:

```python
    if getattr(args, "hitl_review", False) and getattr(args, "parallel", False):
        logger.warning("--hitl-review ignored in parallel mode (interactive prompts not supported)")
        config.hitl_review = False
    if getattr(args, "hitl_review", False) and getattr(args, "tui", False):
        logger.warning("--hitl-review ignored in TUI mode (TUI owns the screen)")
        config.hitl_review = False
```

**Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_hooks.py::TestHitlReviewGate -v`
Expected: All PASS

**Step 7: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 8: Commit**

```bash
git add src/spec_runner/hooks.py src/spec_runner/executor.py tests/test_hooks.py
git commit -m "feat: add HITL approval gate with interactive prompt"
```

---

### Task 8: Update exports and CLAUDE.md

**Files:**
- Modify: `src/spec_runner/__init__.py`
- Modify: `CLAUDE.md`

**Step 1: Update __init__.py**

Add `ReviewVerdict` to imports and `__all__`:

```python
from .state import (
    ExecutorState,
    ReviewVerdict,
    TaskAttempt,
    TaskState,
)
```

Add `"ReviewVerdict"` to `__all__`.

**Step 2: Update CLAUDE.md**

- Add `REVIEW_REJECTED` to ErrorCode list
- Add `ReviewVerdict` to Key Classes section
- Add `--hitl-review` to CLI flags
- Update test count
- Note enriched review prompt with full diff, checklist, test results

**Step 3: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All tests pass

**Step 4: Commit**

```bash
git add src/spec_runner/__init__.py CLAUDE.md
git commit -m "docs: update exports and CLAUDE.md for Phase 4"
```
