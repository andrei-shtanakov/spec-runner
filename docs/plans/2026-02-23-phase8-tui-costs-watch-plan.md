# Phase 8: TUI Dashboard, Cost Reporting, Watch Mode — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add `spec-runner costs` command, `spec-runner watch` continuous execution mode, and enhance the TUI dashboard with a log panel.

**Architecture:** Cost reporting reads state.db via existing `ExecutorState` methods. Watch mode is a polling loop reusing `run_with_retries()`. TUI enhancements add a `RichLog` panel tailing the progress file.

**Tech Stack:** Python 3.10+, Textual (already a dependency), SQLite via `ExecutorState`

---

### Task 1: Cost Reporting — Tests

**Files:**
- Create: `tests/test_costs.py`

**Context:**
- `cmd_costs()` will live in `executor.py` alongside `cmd_status()` (line 1068)
- It reads state via `ExecutorState.task_cost()` (state.py:462), `ExecutorState.total_cost()` (state.py:456), `ExecutorState.total_tokens()` (state.py:469)
- Tasks are parsed from `tasks.md` via `parse_tasks()` (task.py)
- Config has `budget_usd` field for the budget display

**Step 1: Write failing tests for `cmd_costs()`**

```python
"""Tests for spec-runner costs command."""

import argparse
import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.executor import cmd_costs
from spec_runner.state import ExecutorState, TaskAttempt, TaskState


def _make_config(tmp_path: Path) -> ExecutorConfig:
    """Create minimal config for cost tests."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    return ExecutorConfig(
        project_root=tmp_path,
        spec_dir=spec_dir,
        tasks_file=spec_dir / "tasks.md",
        state_file=spec_dir / ".executor-state.db",
        budget_usd=5.0,
    )


def _write_tasks(tasks_file: Path, tasks: list[tuple[str, str, str, str]]) -> None:
    """Write tasks.md with given (id, name, priority, status) tuples."""
    lines = []
    for tid, name, prio, status in tasks:
        lines.append(f"## {tid}: {name} [{prio}] ({status})")
        lines.append("")
    tasks_file.write_text("\n".join(lines))


def _seed_state(config: ExecutorConfig, task_data: dict) -> None:
    """Seed state.db with task attempts.

    task_data: {task_id: [(success, cost, input_tok, output_tok), ...]}
    """
    with ExecutorState(config) as state:
        for task_id, attempts in task_data.items():
            for i, (success, cost, inp, out) in enumerate(attempts):
                ts = state.get_task_state(task_id)
                if not ts:
                    state.set_task_state(
                        task_id,
                        TaskState(
                            task_id=task_id,
                            status="running",
                            started_at="2026-01-01T00:00:00",
                        ),
                    )
                attempt = TaskAttempt(
                    attempt_number=i + 1,
                    success=success,
                    cost_usd=cost,
                    input_tokens=inp,
                    output_tokens=out,
                    duration_seconds=10.0,
                    started_at="2026-01-01T00:00:00",
                )
                state.add_attempt(task_id, attempt)
                if success:
                    ts_updated = state.get_task_state(task_id)
                    if ts_updated:
                        ts_updated.status = "success"
                        ts_updated.completed_at = "2026-01-01T00:01:00"
                        state.set_task_state(task_id, ts_updated)
                elif i == len(attempts) - 1 and not success:
                    ts_updated = state.get_task_state(task_id)
                    if ts_updated:
                        ts_updated.status = "failed"
                        state.set_task_state(task_id, ts_updated)


class TestCmdCosts:
    """Tests for cmd_costs() output."""

    def test_no_tasks(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Empty tasks.md produces empty output."""
        config = _make_config(tmp_path)
        config.tasks_file.write_text("")
        args = argparse.Namespace(json=False, sort="id")
        cmd_costs(args, config)
        out = capsys.readouterr().out
        assert "No tasks found" in out

    def test_basic_table(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Tasks with cost data render a table."""
        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [
            ("TASK-001", "Login page", "p0", "done"),
            ("TASK-002", "DB schema", "p1", "done"),
            ("TASK-003", "Tests", "p2", "todo"),
        ])
        _seed_state(config, {
            "TASK-001": [(True, 0.45, 12500, 3200)],
            "TASK-002": [(False, 0.10, 4000, 1000), (True, 0.23, 8200, 2100)],
        })
        args = argparse.Namespace(json=False, sort="id")
        cmd_costs(args, config)
        out = capsys.readouterr().out
        assert "TASK-001" in out
        assert "$0.45" in out
        assert "TASK-002" in out
        assert "$0.33" in out  # 0.10 + 0.23
        assert "TASK-003" in out
        assert "--" in out  # no cost for todo task

    def test_summary_section(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """Summary shows total cost, budget percentage, avg per task."""
        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [
            ("TASK-001", "Task A", "p0", "done"),
            ("TASK-002", "Task B", "p1", "done"),
        ])
        _seed_state(config, {
            "TASK-001": [(True, 0.50, 10000, 2000)],
            "TASK-002": [(True, 0.30, 8000, 1500)],
        })
        args = argparse.Namespace(json=False, sort="id")
        cmd_costs(args, config)
        out = capsys.readouterr().out
        assert "$0.80" in out  # total
        assert "$5.00" in out  # budget
        assert "16.0%" in out  # 0.80 / 5.00
        assert "$0.40" in out  # avg per completed

    def test_json_output(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """--json flag produces valid JSON."""
        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [
            ("TASK-001", "Login", "p0", "done"),
        ])
        _seed_state(config, {
            "TASK-001": [(True, 0.45, 12500, 3200)],
        })
        args = argparse.Namespace(json=True, sort="id")
        cmd_costs(args, config)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "tasks" in data
        assert "summary" in data
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["task_id"] == "TASK-001"
        assert data["tasks"][0]["cost"] == 0.45
        assert data["summary"]["total_cost"] == 0.45

    def test_sort_by_cost(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """--sort=cost orders tasks by descending cost."""
        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [
            ("TASK-001", "Cheap", "p0", "done"),
            ("TASK-002", "Expensive", "p1", "done"),
        ])
        _seed_state(config, {
            "TASK-001": [(True, 0.10, 5000, 1000)],
            "TASK-002": [(True, 0.90, 20000, 5000)],
        })
        args = argparse.Namespace(json=False, sort="cost")
        cmd_costs(args, config)
        out = capsys.readouterr().out
        # TASK-002 should appear before TASK-001
        pos_002 = out.index("TASK-002")
        pos_001 = out.index("TASK-001")
        assert pos_002 < pos_001

    def test_no_budget_configured(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """No budget_usd in config omits budget percentage."""
        config = _make_config(tmp_path)
        config.budget_usd = None
        _write_tasks(config.tasks_file, [
            ("TASK-001", "Task", "p0", "done"),
        ])
        _seed_state(config, {
            "TASK-001": [(True, 0.50, 10000, 2000)],
        })
        args = argparse.Namespace(json=False, sort="id")
        cmd_costs(args, config)
        out = capsys.readouterr().out
        assert "$0.50" in out
        assert "%" not in out  # no budget percentage


class TestCmdCostsSortTokens:
    """Test --sort=tokens ordering."""

    def test_sort_by_tokens(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        """--sort=tokens orders by total tokens descending."""
        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [
            ("TASK-001", "Few tokens", "p0", "done"),
            ("TASK-002", "Many tokens", "p1", "done"),
        ])
        _seed_state(config, {
            "TASK-001": [(True, 0.10, 1000, 500)],
            "TASK-002": [(True, 0.05, 50000, 10000)],
        })
        args = argparse.Namespace(json=False, sort="tokens")
        cmd_costs(args, config)
        out = capsys.readouterr().out
        pos_002 = out.index("TASK-002")
        pos_001 = out.index("TASK-001")
        assert pos_002 < pos_001
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_costs.py -v`
Expected: FAIL — `cmd_costs` not found in `spec_runner.executor`

**Step 3: Commit test file**

```bash
git add tests/test_costs.py
git commit -m "test: add cost reporting tests (red phase)"
```

---

### Task 2: Cost Reporting — Implementation

**Files:**
- Modify: `src/spec_runner/executor.py` (add `cmd_costs()` ~80 lines, add `costs` subparser)

**Context:**
- Add `cmd_costs()` near `cmd_status()` (after line ~1148)
- Add `costs` subparser in `main()` near line 1661 (after `tui` parser)
- Register `"costs": cmd_costs` in the `commands` dict at line 1686
- The function reads tasks via `parse_tasks()`, state via `ExecutorState`, formats a table

**Step 1: Implement `cmd_costs()`**

Add this function after `cmd_status()` in `executor.py`:

```python
def cmd_costs(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Display cost breakdown per task."""
    tasks = parse_tasks(config.tasks_file) if config.tasks_file.exists() else []
    if not tasks:
        print("No tasks found.")
        return

    with ExecutorState(config) as state:
        # Build per-task cost data
        rows = []
        for task in tasks:
            ts = state.tasks.get(task.id)
            cost = state.task_cost(task.id)
            attempt_count = ts.attempt_count if ts else 0

            # Per-task tokens
            inp = sum(
                a.input_tokens for a in ts.attempts if a.input_tokens is not None
            ) if ts else 0
            out = sum(
                a.output_tokens for a in ts.attempts if a.output_tokens is not None
            ) if ts else 0

            status = ts.status if ts else task.status
            rows.append({
                "task_id": task.id,
                "name": task.name,
                "status": status,
                "cost": cost,
                "attempts": attempt_count,
                "input_tokens": inp,
                "output_tokens": out,
            })

        # Sort
        sort_key = getattr(args, "sort", "id")
        if sort_key == "cost":
            rows.sort(key=lambda r: r["cost"], reverse=True)
        elif sort_key == "tokens":
            rows.sort(key=lambda r: r["input_tokens"] + r["output_tokens"], reverse=True)
        elif sort_key == "name":
            rows.sort(key=lambda r: r["name"].lower())
        # default: task id order (already sorted from parse_tasks)

        # Summary
        total_cost = sum(r["cost"] for r in rows)
        total_inp = sum(r["input_tokens"] for r in rows)
        total_out = sum(r["output_tokens"] for r in rows)
        completed = [r for r in rows if r["status"] == "success"]
        avg_cost = total_cost / len(completed) if completed else 0.0
        most_expensive = max(rows, key=lambda r: r["cost"]) if rows else None

        if getattr(args, "json", False):
            import json as json_mod

            output = {
                "tasks": [
                    {
                        "task_id": r["task_id"],
                        "name": r["name"],
                        "status": r["status"],
                        "cost": r["cost"],
                        "attempts": r["attempts"],
                        "input_tokens": r["input_tokens"],
                        "output_tokens": r["output_tokens"],
                    }
                    for r in rows
                ],
                "summary": {
                    "total_cost": total_cost,
                    "budget_usd": config.budget_usd,
                    "total_input_tokens": total_inp,
                    "total_output_tokens": total_out,
                    "avg_cost_per_completed": round(avg_cost, 4),
                    "most_expensive_task": most_expensive["task_id"] if most_expensive and most_expensive["cost"] > 0 else None,
                },
            }
            print(json_mod.dumps(output, indent=2))
            return

        # Text table
        def _fmt_tokens(n: int) -> str:
            if n >= 1000:
                return f"{n / 1000:.1f}K"
            return str(n)

        print("\nTask Costs:")
        for r in rows:
            if r["attempts"] > 0:
                attempts_str = f"{r['attempts']} attempt" + ("s" if r['attempts'] != 1 else "")
                tokens_str = f"{_fmt_tokens(r['input_tokens'] + r['output_tokens'])} tokens"
                print(
                    f"  {r['task_id']:12s} {r['name'][:24]:<24s} {r['status']:<8s} "
                    f"${r['cost']:<7.2f} {attempts_str:<12s} {tokens_str}"
                )
            else:
                print(
                    f"  {r['task_id']:12s} {r['name'][:24]:<24s} {r['status']:<8s} "
                    f"{'--':<8s} {'--':<12s} {'--'}"
                )

        print(f"\nSummary:")
        if config.budget_usd is not None:
            pct = (total_cost / config.budget_usd * 100) if config.budget_usd > 0 else 0
            print(f"  Total cost:     ${total_cost:.2f} / ${config.budget_usd:.2f} budget ({pct:.1f}%)")
        else:
            print(f"  Total cost:     ${total_cost:.2f}")
        print(f"  Total tokens:   {_fmt_tokens(total_inp)} input, {_fmt_tokens(total_out)} output")
        if completed:
            print(f"  Avg per task:   ${avg_cost:.2f} (completed only)")
        if most_expensive and most_expensive["cost"] > 0:
            print(f"  Most expensive: {most_expensive['task_id']} (${most_expensive['cost']:.2f})")
```

**Step 2: Add `costs` subparser in `main()`**

After the `tui` subparser (line ~1661), add:

```python
    # costs
    costs_parser = subparsers.add_parser("costs", parents=[common], help="Show cost breakdown per task")
    costs_parser.add_argument("--json", action="store_true", help="Output as JSON")
    costs_parser.add_argument(
        "--sort",
        choices=["id", "cost", "tokens", "name"],
        default="id",
        help="Sort order (default: task id)",
    )
```

Add `"costs": cmd_costs` to the `commands` dict.

**Step 3: Run tests**

Run: `uv run pytest tests/test_costs.py -v`
Expected: All PASS

**Step 4: Run full test suite**

Run: `uv run pytest tests/ -v -m "not slow"`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/spec_runner/executor.py tests/test_costs.py
git commit -m "feat: add spec-runner costs command with table and JSON output"
```

---

### Task 3: Watch Mode — Tests

**Files:**
- Create: `tests/test_watch.py`

**Context:**
- `cmd_watch()` will live in `executor.py`
- It loops: parse tasks → get next → run_with_retries → sleep 5s
- Stops on: stop file, Ctrl+C, consecutive failure limit, no tasks (keeps polling)
- Uses `check_stop_requested()` from state.py:499, `validate_all()` for first iteration
- Calls `run_with_retries(task, config, state)` from executor.py:425

**Step 1: Write failing tests**

```python
"""Tests for spec-runner watch command."""

import argparse
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.executor import cmd_watch


def _make_config(tmp_path: Path) -> ExecutorConfig:
    """Create minimal config for watch tests."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    return ExecutorConfig(
        project_root=tmp_path,
        spec_dir=spec_dir,
        tasks_file=spec_dir / "tasks.md",
        state_file=spec_dir / ".executor-state.db",
        stop_file=spec_dir / ".executor-stop",
        max_consecutive_failures=2,
    )


def _write_tasks(tasks_file: Path, tasks: list[tuple[str, str, str, str]]) -> None:
    """Write tasks.md with given (id, name, priority, status) tuples."""
    lines = []
    for tid, name, prio, status in tasks:
        lines.append(f"## {tid}: {name} [{prio}] ({status})")
        lines.append("")
    tasks_file.write_text("\n".join(lines))


class TestCmdWatch:
    """Tests for watch mode loop logic."""

    @patch("spec_runner.executor.run_with_retries")
    @patch("spec_runner.executor.validate_all")
    @patch("spec_runner.executor.time")
    def test_executes_ready_task_then_stops(
        self, mock_time, mock_validate, mock_run, tmp_path: Path
    ) -> None:
        """Watch picks up a ready task, executes it, then stops on stop file."""
        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [
            ("TASK-001", "First task", "p0", "todo"),
        ])

        mock_validate.return_value = MagicMock(ok=True)
        mock_run.return_value = True

        # After first execution, create stop file to break loop
        call_count = 0
        def side_effect(*a, **kw):
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                config.stop_file.write_text("stop")
            return True

        mock_run.side_effect = side_effect
        mock_time.sleep = MagicMock()

        args = argparse.Namespace(tui=False)
        cmd_watch(args, config)

        assert mock_run.call_count == 1

    @patch("spec_runner.executor.run_with_retries")
    @patch("spec_runner.executor.validate_all")
    @patch("spec_runner.executor.time")
    def test_stops_on_stop_file(
        self, mock_time, mock_validate, mock_run, tmp_path: Path
    ) -> None:
        """Watch exits when stop file exists before any execution."""
        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [
            ("TASK-001", "First task", "p0", "todo"),
        ])
        config.stop_file.write_text("stop")

        mock_validate.return_value = MagicMock(ok=True)

        args = argparse.Namespace(tui=False)
        cmd_watch(args, config)

        mock_run.assert_not_called()

    @patch("spec_runner.executor.run_with_retries")
    @patch("spec_runner.executor.validate_all")
    @patch("spec_runner.executor.time")
    def test_stops_on_consecutive_failures(
        self, mock_time, mock_validate, mock_run, tmp_path: Path
    ) -> None:
        """Watch stops after max_consecutive_failures."""
        config = _make_config(tmp_path)
        config.max_consecutive_failures = 2
        _write_tasks(config.tasks_file, [
            ("TASK-001", "Failing task", "p0", "todo"),
        ])

        mock_validate.return_value = MagicMock(ok=True)
        mock_run.return_value = False
        mock_time.sleep = MagicMock()

        args = argparse.Namespace(tui=False)
        cmd_watch(args, config)

        assert mock_run.call_count == 2

    @patch("spec_runner.executor.run_with_retries")
    @patch("spec_runner.executor.validate_all")
    @patch("spec_runner.executor.time")
    def test_resets_failures_on_success(
        self, mock_time, mock_validate, mock_run, tmp_path: Path
    ) -> None:
        """Consecutive failure count resets on success."""
        config = _make_config(tmp_path)
        config.max_consecutive_failures = 2
        _write_tasks(config.tasks_file, [
            ("TASK-001", "Task A", "p0", "todo"),
            ("TASK-002", "Task B", "p1", "todo"),
            ("TASK-003", "Task C", "p2", "todo"),
        ])

        mock_validate.return_value = MagicMock(ok=True)
        results = [False, True, False, False]  # fail, success, fail, fail -> stop
        result_idx = 0

        def run_side_effect(*a, **kw):
            nonlocal result_idx
            r = results[result_idx] if result_idx < len(results) else False
            result_idx += 1
            return r

        mock_run.side_effect = run_side_effect
        mock_time.sleep = MagicMock()

        args = argparse.Namespace(tui=False)
        cmd_watch(args, config)

        # Should have run 4 times: fail, success (reset), fail, fail (stop)
        assert mock_run.call_count == 4

    @patch("spec_runner.executor.validate_all")
    def test_validation_failure_stops(
        self, mock_validate, tmp_path: Path
    ) -> None:
        """Watch exits if pre-run validation fails."""
        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [
            ("TASK-001", "Task", "p0", "todo"),
        ])

        mock_validate.return_value = MagicMock(ok=False)

        args = argparse.Namespace(tui=False)
        cmd_watch(args, config)

        # Should not have proceeded past validation

    @patch("spec_runner.executor.run_with_retries")
    @patch("spec_runner.executor.validate_all")
    @patch("spec_runner.executor.time")
    def test_polls_when_no_tasks_ready(
        self, mock_time, mock_validate, mock_run, tmp_path: Path
    ) -> None:
        """Watch sleeps when no tasks are ready, then stops on stop file."""
        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [
            ("TASK-001", "Done task", "p0", "done"),
        ])

        mock_validate.return_value = MagicMock(ok=True)

        sleep_count = 0
        def sleep_side_effect(secs):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                config.stop_file.write_text("stop")

        mock_time.sleep = MagicMock(side_effect=sleep_side_effect)

        args = argparse.Namespace(tui=False)
        cmd_watch(args, config)

        mock_run.assert_not_called()
        assert sleep_count >= 2
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_watch.py -v`
Expected: FAIL — `cmd_watch` not found in `spec_runner.executor`

**Step 3: Commit test file**

```bash
git add tests/test_watch.py
git commit -m "test: add watch mode tests (red phase)"
```

---

### Task 4: Watch Mode — Implementation

**Files:**
- Modify: `src/spec_runner/executor.py` (add `cmd_watch()` ~80 lines, add `watch` subparser)

**Context:**
- Add `cmd_watch()` near `cmd_tui()` (before line 1522)
- Add `watch` subparser in `main()` near the `tui` parser
- Register `"watch": cmd_watch` in the `commands` dict
- The loop: check stop → validate once → parse tasks → get next → execute → track failures
- Uses `check_stop_requested()` from state.py, `clear_stop_file()` from state.py
- Uses `validate_all()` from validate.py
- Uses `run_with_retries()` from executor.py
- Uses `parse_tasks()`, `resolve_dependencies()`, `get_next_tasks()` from task.py

**Step 1: Implement `cmd_watch()`**

Add before `cmd_tui()` in `executor.py`:

```python
def cmd_watch(args: argparse.Namespace, config: ExecutorConfig) -> None:
    """Continuously watch tasks.md and execute ready tasks."""
    from .state import check_stop_requested, clear_stop_file
    from .validate import format_results, validate_all

    clear_stop_file(config)

    # Pre-run validation (once)
    pre_result = validate_all(
        tasks_file=config.tasks_file,
        config_file=config.project_root / CONFIG_FILE,
    )
    if not pre_result.ok:
        logger.error("Validation failed before watch")
        print(format_results(pre_result))
        return

    print(f"Watching {config.tasks_file} for changes...")
    print(f"Polling every 5s | Stop: Ctrl+C or touch {config.stop_file}")

    consecutive_failures = 0

    while True:
        # Check stop conditions
        if check_stop_requested(config):
            logger.info("Stop requested, exiting watch mode")
            break

        if consecutive_failures >= config.max_consecutive_failures:
            logger.error(
                "Watch stopped: too many consecutive failures",
                consecutive_failures=consecutive_failures,
            )
            break

        # Parse tasks and find next ready
        tasks = parse_tasks(config.tasks_file)
        tasks = resolve_dependencies(tasks)
        from .task import get_next_tasks

        ready = get_next_tasks(tasks)
        if not ready:
            time.sleep(5)
            continue

        task = ready[0]
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] Starting {task.id}: {task.name}")

        with ExecutorState(config) as state:
            result = run_with_retries(task, config, state)

        if result is True:
            consecutive_failures = 0
            cost = 0.0
            with ExecutorState(config) as state:
                cost = state.task_cost(task.id)
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] {task.id} completed (${cost:.2f})")
        else:
            consecutive_failures += 1
            timestamp = datetime.now().strftime("%H:%M:%S")
            print(f"[{timestamp}] {task.id} failed ({consecutive_failures}/{config.max_consecutive_failures})")

        # Brief pause between tasks
        time.sleep(1)
```

**Step 2: Add `watch` subparser in `main()`**

After the `tui` subparser, add:

```python
    # watch
    watch_parser = subparsers.add_parser("watch", parents=[common], help="Continuously execute ready tasks")
    watch_parser.add_argument(
        "--tui",
        action="store_true",
        help="Show TUI dashboard during watch",
    )
```

Add `"watch": cmd_watch` to the `commands` dict.

**Step 3: Run watch tests**

Run: `uv run pytest tests/test_watch.py -v`
Expected: All PASS

**Step 4: Run full test suite**

Run: `uv run pytest tests/ -v -m "not slow"`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/spec_runner/executor.py tests/test_watch.py
git commit -m "feat: add spec-runner watch command for continuous task execution"
```

---

### Task 5: TUI Log Panel — Tests

**Files:**
- Modify: `tests/test_tui.py` (add log panel tests)

**Context:**
- The existing `tui.py` has a full kanban dashboard (394 lines) with `SpecRunnerApp`, `TaskCard`, `StatsBar`, `KanbanColumn`
- The design calls for a `LogPanel(RichLog)` that tails `spec/.executor-progress.txt`
- The progress file is written by `log_progress()` in executor.py
- The log panel should show below the kanban columns and above the stats bar
- Keyboard: `j/k` scroll log panel, `r` force refresh

**Step 1: Check existing test file**

Read `tests/test_tui.py` to understand existing tests.

**Step 2: Write failing tests for log panel**

Add tests for:
- `LogPanel` widget renders and reads from progress file
- `r` key triggers refresh
- Log panel shows new lines when progress file grows

```python
# Add to tests/test_tui.py

class TestLogPanel:
    """Tests for the log panel widget."""

    def test_log_panel_format(self) -> None:
        """LogPanel.format_line strips timestamps and truncates."""
        from spec_runner.tui import LogPanel
        line = "[14:23:01] TASK-003 Attempt 1/3 started"
        formatted = LogPanel.format_line(line)
        assert "TASK-003" in formatted

    def test_log_panel_reads_file(self, tmp_path: Path) -> None:
        """LogPanel reads lines from progress file."""
        from spec_runner.tui import LogPanel
        progress = tmp_path / "progress.txt"
        progress.write_text("[14:23] Line 1\n[14:24] Line 2\n")
        panel = LogPanel()
        lines = panel.read_new_lines(progress)
        assert len(lines) == 2
```

**Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_tui.py::TestLogPanel -v`
Expected: FAIL — `LogPanel` not found

**Step 4: Commit**

```bash
git add tests/test_tui.py
git commit -m "test: add TUI log panel tests (red phase)"
```

---

### Task 6: TUI Log Panel — Implementation

**Files:**
- Modify: `src/spec_runner/tui.py` (add `LogPanel` widget, integrate into layout, add `r` key binding)

**Context:**
- Add `LogPanel` class using `textual.widgets.RichLog`
- Track file position to only read new lines each tick
- Add to `compose()` between the `#board` and `#stats-bar`
- Add `r` key binding for force refresh
- The progress file is at `config.spec_dir / ".executor-progress.txt"` (same dir as tasks)

**Step 1: Implement `LogPanel`**

```python
class LogPanel(Static):
    """Panel showing execution progress log."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._file_pos: int = 0
        self._lines: list[str] = []

    @staticmethod
    def format_line(line: str) -> str:
        """Format a progress log line for display."""
        return line.rstrip()

    def read_new_lines(self, path: Path) -> list[str]:
        """Read new lines from progress file since last read."""
        if not path.exists():
            return []
        try:
            with open(path) as f:
                f.seek(self._file_pos)
                new_data = f.read()
                self._file_pos = f.tell()
            if new_data:
                new_lines = [self.format_line(ln) for ln in new_data.splitlines() if ln.strip()]
                self._lines.extend(new_lines)
                # Keep last 100 lines
                if len(self._lines) > 100:
                    self._lines = self._lines[-100:]
                return new_lines
        except OSError:
            pass
        return []

    def render_log(self) -> str:
        """Render last N lines as Rich markup."""
        visible = self._lines[-10:]  # Show last 10 in the panel
        if not visible:
            return "[dim]No log entries yet[/]"
        return "\n".join(visible)
```

**Step 2: Integrate into `SpecRunnerApp`**

- Add `LogPanel` to `compose()` between `Horizontal(id="board")` and `StatsBar`
- In `_do_refresh()`, call `log_panel.read_new_lines()` and update
- Add `Binding("r", "refresh", "Refresh")` and `action_refresh()` method
- Add CSS for `#log-panel` with fixed height

**Step 3: Run TUI tests**

Run: `uv run pytest tests/test_tui.py -v`
Expected: All PASS

**Step 4: Run full test suite**

Run: `uv run pytest tests/ -v -m "not slow"`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/spec_runner/tui.py tests/test_tui.py
git commit -m "feat: add log panel to TUI dashboard with file tailing"
```

---

### Task 7: Watch + TUI Integration

**Files:**
- Modify: `src/spec_runner/executor.py` (add `--tui` flag handling to `cmd_watch()`)

**Context:**
- `spec-runner watch --tui` should launch TUI as main thread, watch loop as background thread
- Pattern already exists in `cmd_run()` at lines 828-856 — reuse the same threading approach
- The watch loop runs in a daemon thread, TUI polls state.db every 2s

**Step 1: Write failing test**

Add to `tests/test_watch.py`:

```python
class TestWatchTui:
    """Tests for watch --tui integration."""

    @patch("spec_runner.executor.SpecRunnerApp")
    @patch("spec_runner.executor.validate_all")
    def test_tui_flag_launches_app(self, mock_validate, mock_app_cls, tmp_path: Path) -> None:
        """--tui flag launches SpecRunnerApp with watch loop in background."""
        config = _make_config(tmp_path)
        _write_tasks(config.tasks_file, [
            ("TASK-001", "Task", "p0", "todo"),
        ])

        mock_validate.return_value = MagicMock(ok=True)
        mock_app = MagicMock()
        mock_app_cls.return_value = mock_app

        args = argparse.Namespace(tui=True)
        cmd_watch(args, config)

        mock_app.run.assert_called_once()
```

**Step 2: Implement watch + TUI**

At the top of `cmd_watch()`, after validation, add TUI branch:

```python
    if getattr(args, "tui", False):
        import threading

        from .logging import setup_logging
        from .tui import SpecRunnerApp

        log_file = config.logs_dir / f"watch-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        setup_logging(level=config.log_level, tui_mode=True, log_file=log_file)

        app = SpecRunnerApp(config=config)

        def _start_watch() -> None:
            def watch_loop() -> None:
                # Same loop as non-TUI but without print statements
                consecutive_failures = 0
                while True:
                    if check_stop_requested(config):
                        break
                    if consecutive_failures >= config.max_consecutive_failures:
                        break
                    tasks = parse_tasks(config.tasks_file)
                    tasks = resolve_dependencies(tasks)
                    ready = get_next_tasks(tasks)
                    if not ready:
                        time.sleep(5)
                        continue
                    task = ready[0]
                    with ExecutorState(config) as state:
                        result = run_with_retries(task, config, state)
                    if result is True:
                        consecutive_failures = 0
                    else:
                        consecutive_failures += 1
                    time.sleep(1)

            t = threading.Thread(target=watch_loop, daemon=True)
            t.start()

        app.call_later(_start_watch)
        app.run()
        return
```

**Step 3: Run tests**

Run: `uv run pytest tests/test_watch.py -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add src/spec_runner/executor.py tests/test_watch.py
git commit -m "feat: add --tui flag to watch mode for live dashboard"
```

---

### Task 8: Exports and Lint

**Files:**
- Modify: `src/spec_runner/__init__.py` (add `cmd_costs`, `cmd_watch` exports)
- Run: `uv run ruff check . --fix && uv run ruff format .`

**Step 1: Update `__init__.py`**

Add to imports from `.executor`:
```python
from .executor import (
    classify_retry_strategy,
    cmd_costs,
    cmd_watch,
    compute_retry_delay,
    execute_task,
    run_with_retries,
)
```

Add to `__all__`:
```python
    "cmd_costs",
    "cmd_watch",
```

**Step 2: Run linter and formatter**

```bash
uv run ruff check . --fix
uv run ruff format .
```

**Step 3: Run full test suite**

Run: `uv run pytest tests/ -v`
Expected: All PASS (including slow tests)

**Step 4: Commit**

```bash
git add src/spec_runner/__init__.py
git commit -m "chore: export cmd_costs and cmd_watch, lint cleanup"
```

---

### Task 9: Documentation Update

**Files:**
- Modify: `CLAUDE.md` (add new CLI commands to documentation)

**Step 1: Update CLAUDE.md**

Add to the CLI entry points section:
```
spec-runner costs                          # Cost breakdown per task
spec-runner costs --json                   # JSON output for automation
spec-runner costs --sort=cost              # Sort by cost descending
spec-runner watch                          # Continuously execute ready tasks
spec-runner watch --tui                    # Watch with live TUI dashboard
```

Update the module table to reflect new line counts.

**Step 2: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add costs and watch commands to CLAUDE.md"
```

---

## Summary

| Task | Feature | New files | Modified files | ~Lines |
|------|---------|-----------|----------------|--------|
| 1-2 | Cost Reporting | `tests/test_costs.py` | `executor.py` | ~180 |
| 3-4 | Watch Mode | `tests/test_watch.py` | `executor.py` | ~180 |
| 5-6 | TUI Log Panel | — | `tui.py`, `tests/test_tui.py` | ~80 |
| 7 | Watch + TUI | — | `executor.py`, `tests/test_watch.py` | ~50 |
| 8-9 | Exports + Docs | — | `__init__.py`, `CLAUDE.md` | ~20 |
| **Total** | | **2 new** | **5 modified** | **~510** |

### Implementation order

1. **Cost Reporting** (Tasks 1-2) — standalone, no new dependencies
2. **Watch Mode** (Tasks 3-4) — reuses run_with_retries, no TUI dependency
3. **TUI Log Panel** (Tasks 5-6) — enhances existing TUI
4. **Watch + TUI** (Task 7) — combines watch + TUI
5. **Exports + Docs** (Tasks 8-9) — cleanup
