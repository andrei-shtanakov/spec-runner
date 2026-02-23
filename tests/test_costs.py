"""Tests for cmd_costs() — cost reporting command (red phase).

Tests are written against the expected interface of cmd_costs() which
will be added to spec_runner.executor. Import will fail until the
function is implemented.
"""

import json
from argparse import Namespace
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.executor import cmd_costs
from spec_runner.state import ExecutorState

# --- Helpers ---


def _make_config(tmp_path: Path, **overrides) -> ExecutorConfig:
    """Create an ExecutorConfig with minimal fields for cost tests."""
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    defaults: dict = {
        "project_root": tmp_path,
        "spec_dir": spec_dir,
        "state_file": spec_dir / ".executor-state.db",
        "budget_usd": 5.0,
    }
    defaults.update(overrides)
    # ExecutorConfig doesn't have spec_dir param; remove it before constructing
    defaults.pop("spec_dir", None)
    return ExecutorConfig(**defaults)


def _write_tasks(
    tasks_file: Path,
    tasks: list[tuple[str, str, str, str]],
) -> None:
    """Write tasks.md from a list of (id, name, priority, status) tuples.

    Generates the full markdown format expected by parse_tasks().
    """
    priority_emoji = {
        "p0": "\U0001f534",
        "p1": "\U0001f7e0",
        "p2": "\U0001f7e1",
        "p3": "\U0001f7e2",
    }
    status_emoji = {
        "todo": "\u2b1c",
        "in_progress": "\U0001f504",
        "done": "\u2705",
        "blocked": "\u23f8\ufe0f",
    }

    lines = ["# Tasks\n"]
    for task_id, name, priority, status in tasks:
        p_emoji = priority_emoji.get(priority, "\U0001f534")
        s_emoji = status_emoji.get(status, "\u2b1c")
        lines.append(f"### {task_id}: {name}")
        lines.append(f"{p_emoji} {priority.upper()} | {s_emoji} {status.upper()} | Est: 1d")
        lines.append("")

    tasks_file.parent.mkdir(parents=True, exist_ok=True)
    tasks_file.write_text("\n".join(lines))


def _seed_state(
    config: ExecutorConfig,
    task_data: dict[str, list[tuple[bool, float, int, int]]],
) -> None:
    """Populate state.db with task attempts.

    task_data maps task_id -> list of (success, cost_usd, input_tokens, output_tokens).
    """
    with ExecutorState(config) as state:
        for task_id, attempts in task_data.items():
            for success, cost, inp_tok, out_tok in attempts:
                state.record_attempt(
                    task_id,
                    success=success,
                    duration=10.0,
                    error=None if success else "test error",
                    input_tokens=inp_tok,
                    output_tokens=out_tok,
                    cost_usd=cost,
                )


# --- Tests ---


class TestCmdCosts:
    """Tests for the cmd_costs command."""

    def test_no_tasks(self, tmp_path: Path, capsys) -> None:
        """Empty tasks.md prints 'No tasks found'."""
        config = _make_config(tmp_path)
        # Write an empty tasks file (no task headers)
        _write_tasks(config.tasks_file, [])
        args = Namespace(json=False, sort="id")

        cmd_costs(args, config)

        output = capsys.readouterr().out
        assert "No tasks found" in output

    def test_basic_table(self, tmp_path: Path, capsys) -> None:
        """3 tasks (2 done with costs, 1 todo) — table shows IDs, costs, '--' for todo."""
        config = _make_config(tmp_path)
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Login page", "p0", "done"),
                ("TASK-002", "Signup page", "p1", "done"),
                ("TASK-003", "Dashboard", "p2", "todo"),
            ],
        )
        _seed_state(
            config,
            {
                "TASK-001": [(True, 0.50, 1000, 500)],
                "TASK-002": [(True, 1.20, 2000, 800)],
            },
        )
        args = Namespace(json=False, sort="id")

        cmd_costs(args, config)

        output = capsys.readouterr().out
        assert "TASK-001" in output
        assert "TASK-002" in output
        assert "TASK-003" in output
        # Done tasks show cost values
        assert "0.50" in output
        assert "1.20" in output
        # Todo task shows placeholder
        assert "--" in output

    def test_summary_section(self, tmp_path: Path, capsys) -> None:
        """2 done tasks — shows total cost, budget %, avg per completed."""
        config = _make_config(tmp_path, budget_usd=10.0)
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Login page", "p0", "done"),
                ("TASK-002", "Signup page", "p1", "done"),
            ],
        )
        _seed_state(
            config,
            {
                "TASK-001": [(True, 2.00, 1000, 500)],
                "TASK-002": [(True, 3.00, 2000, 800)],
            },
        )
        args = Namespace(json=False, sort="id")

        cmd_costs(args, config)

        output = capsys.readouterr().out
        # Total cost = 5.00
        assert "5.00" in output
        # Budget percentage: 5.00 / 10.00 = 50%
        assert "50" in output and "%" in output
        # Average cost per completed task = 2.50
        assert "2.50" in output

    def test_json_output(self, tmp_path: Path, capsys) -> None:
        """args.json=True produces valid JSON with 'tasks' array and 'summary' object."""
        config = _make_config(tmp_path)
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Login page", "p0", "done"),
                ("TASK-002", "Dashboard", "p1", "todo"),
            ],
        )
        _seed_state(
            config,
            {
                "TASK-001": [(True, 0.75, 1500, 600)],
            },
        )
        args = Namespace(json=True, sort="id")

        cmd_costs(args, config)

        raw = capsys.readouterr().out
        data = json.loads(raw)
        assert "tasks" in data
        assert isinstance(data["tasks"], list)
        assert len(data["tasks"]) == 2
        assert "summary" in data
        assert isinstance(data["summary"], dict)
        # Summary should include total_cost
        assert "total_cost" in data["summary"]

    def test_sort_by_cost(self, tmp_path: Path, capsys) -> None:
        """args.sort='cost' — expensive task appears first in output."""
        config = _make_config(tmp_path)
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Cheap task", "p0", "done"),
                ("TASK-002", "Expensive task", "p1", "done"),
            ],
        )
        _seed_state(
            config,
            {
                "TASK-001": [(True, 0.10, 100, 50)],
                "TASK-002": [(True, 5.00, 5000, 2000)],
            },
        )
        args = Namespace(json=False, sort="cost")

        cmd_costs(args, config)

        output = capsys.readouterr().out
        # Expensive task (TASK-002) should appear before cheap task (TASK-001)
        pos_002 = output.index("TASK-002")
        pos_001 = output.index("TASK-001")
        assert pos_002 < pos_001, "Expensive task should appear first when sorted by cost"

    def test_no_budget_configured(self, tmp_path: Path, capsys) -> None:
        """config.budget_usd=None — no '%' in output."""
        config = _make_config(tmp_path, budget_usd=None)
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Login page", "p0", "done"),
            ],
        )
        _seed_state(
            config,
            {
                "TASK-001": [(True, 1.00, 1000, 500)],
            },
        )
        args = Namespace(json=False, sort="id")

        cmd_costs(args, config)

        output = capsys.readouterr().out
        assert "%" not in output


class TestCmdCostsSortTokens:
    """Sort-by-tokens tests for cmd_costs."""

    def test_sort_by_tokens(self, tmp_path: Path, capsys) -> None:
        """args.sort='tokens' — high-token task appears first."""
        config = _make_config(tmp_path)
        _write_tasks(
            config.tasks_file,
            [
                ("TASK-001", "Low tokens", "p0", "done"),
                ("TASK-002", "High tokens", "p1", "done"),
            ],
        )
        _seed_state(
            config,
            {
                "TASK-001": [(True, 0.10, 100, 50)],
                "TASK-002": [(True, 0.20, 10000, 5000)],
            },
        )
        args = Namespace(json=False, sort="tokens")

        cmd_costs(args, config)

        output = capsys.readouterr().out
        # High-token task (TASK-002) should appear before low-token task
        pos_002 = output.index("TASK-002")
        pos_001 = output.index("TASK-001")
        assert pos_002 < pos_001, "High-token task should appear first when sorted by tokens"
