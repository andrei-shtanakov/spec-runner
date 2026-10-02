"""#480 BEH-24: planning gets a ledger identity and its own line in ``costs``.

Run through ``cli.main`` -- the whole path from the argument parser to the cost
table -- with a fake CLI that reports a cost. Planning used to write no ledger
row at all; now every planning call is a call record with a ``run_id`` and a
``call_id``, in a ledger of its own (``plan_agent_calls``): ``agent_calls`` has
``task_id NOT NULL`` and ``costs`` groups it by task, so a row without a task
does not belong there.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path

import pytest

from spec_runner import cli
from spec_runner.state import ExecutorState
from tests.call_doubles import Journal, completed, spawn_double

PLAN_COST = 0.25
TASK_COST = 0.5

_BODIES = {
    "requirements": "# Requirements\n\n## Out of Scope\n- none\n\n"
    "#### REQ-001: X\n**Acceptance Criteria:**\nGIVEN a WHEN b THEN c\n",
    "design": "# Design\n\n### DESIGN-001: Y trace [REQ-001]\n",
    "tasks": "# Tasks\n\n### TASK-001: Do X [REQ-001] [DESIGN-001]\nP0 | TODO\n- [ ] do it\n",
}
_MARK = {"requirements": "SPEC_REQUIREMENTS", "design": "SPEC_DESIGN", "tasks": "SPEC_TASKS"}


def _priced(text: str) -> str:
    return json.dumps(
        {
            "result": text,
            "total_cost_usd": PLAN_COST,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )


def _stage_text(stage: str) -> str:
    return f"{_MARK[stage]}_READY\n{_BODIES[stage]}\n{_MARK[stage]}_END\n"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A project with one completed task of known cost."""
    (tmp_path / "spec").mkdir()
    (tmp_path / "spec-runner.config.yaml").write_text("executor:\n  claude_command: claude\n")
    (tmp_path / "spec" / "tasks.md").write_text(
        "# Tasks\n\n### TASK-009: Done already\n🟠 P1 | ✅ DONE | Est: 1d\n\n**Checklist:**\n- [x] x\n"
    )
    from spec_runner.config import ExecutorConfig

    cfg = ExecutorConfig(project_root=tmp_path)
    with ExecutorState(cfg) as state:
        state.record_attempt("TASK-009", True, 1.0, cost_usd=TASK_COST)
    return tmp_path


def _run(root: Path, argv: list[str], answers) -> int:
    """``spec-runner <argv>`` with ``_spawn`` answering from ``answers``."""
    journal = Journal()
    queue = list(answers)

    def answer(args):
        return completed(args, _priced(queue.pop(0)))

    with spawn_double(journal, answer):
        try:
            cli.main([*argv, "--project-root", str(root)])
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 1
    return 0


def _rows(root: Path, table: str) -> list[tuple]:
    conn = sqlite3.connect(root / "spec" / ".executor-state.db")
    try:
        return conn.execute(
            f"SELECT provenance, run_id, call_id, status, cost_usd FROM {table} ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _costs_json(root: Path, capsys) -> dict:
    capsys.readouterr()
    cli.main(["costs", "--json", "--project-root", str(root)])
    return json.loads(capsys.readouterr().out)


class TestEachPlanningPathLeavesCallRecords:
    def test_plan_full_leaves_three_records_without_a_task(self, root):
        rc = _run(
            root,
            ["plan", "--full", "build x"],
            [_stage_text("requirements"), _stage_text("design"), _stage_text("tasks")],
        )

        assert rc == 0
        rows = _rows(root, "plan_agent_calls")
        assert [r[0] for r in rows] == ["plan:requirements", "plan:design", "plan:tasks"]
        assert len({r[1] for r in rows}) == 1, "one invocation, one run_id"
        assert len({r[2] for r in rows}) == 3, "each call has its own call_id"
        assert all(r[3] == "closed" and r[4] == PLAN_COST for r in rows)

    def test_plan_gated_leaves_one_record(self, root):
        rc = _run(
            root,
            ["plan", "--gated", "--stage", "requirements", "build x"],
            [_stage_text("requirements")],
        )

        assert rc == 0
        rows = _rows(root, "plan_agent_calls")
        assert [r[0] for r in rows] == ["plan:requirements"]
        assert rows[0][1] and rows[0][2]

    def test_two_invocations_have_two_run_ids(self, root):
        _run(
            root, ["plan", "--gated", "--stage", "requirements", "x"], [_stage_text("requirements")]
        )
        (root / "spec" / "requirements.md").unlink()
        _run(
            root, ["plan", "--gated", "--stage", "requirements", "x"], [_stage_text("requirements")]
        )

        rows = _rows(root, "plan_agent_calls")
        assert len(rows) == 2 and rows[0][1] != rows[1][1]

    def test_interactive_plan_leaves_its_own_record(self, root, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda *_a: "n")

        rc = _run(root, ["plan", "build x"], ["PLAN_READY\n"])

        assert rc == 0
        rows = _rows(root, "plan_agent_calls")
        assert [r[0] for r in rows] == ["plan:interactive"]


class TestTheyAreNotInTheTaskLedger:
    def test_agent_calls_gained_no_row_and_no_task_changed_cost(self, root, capsys):
        before = _costs_json(root, capsys)

        _run(
            root,
            ["plan", "--full", "build x"],
            [_stage_text("requirements"), _stage_text("design"), _stage_text("tasks")],
        )

        after = _costs_json(root, capsys)
        assert _rows(root, "agent_calls") == []
        # `plan --full` rewrote tasks.md, so ask the ledger, not the table.
        from spec_runner.config import ExecutorConfig

        with ExecutorState(ExecutorConfig(project_root=root)) as state:
            assert state.task_cost("TASK-009") == TASK_COST
        assert after["summary"]["total_cost"] == before["summary"]["total_cost"] == TASK_COST


class TestCostsShowsPlanningAsItsOwnLine:
    def test_json_has_a_separate_planning_row_and_the_repo_total_includes_it(self, root, capsys):
        _run(
            root,
            ["plan", "--full", "build x"],
            [_stage_text("requirements"), _stage_text("design"), _stage_text("tasks")],
        )

        payload = _costs_json(root, capsys)

        assert payload["planning"]["calls"] == 3
        assert payload["planning"]["cost"] == pytest.approx(3 * PLAN_COST)
        assert payload["planning"]["unmeasured_calls"] == 0
        assert payload["summary"]["planning_cost"] == pytest.approx(3 * PLAN_COST, abs=0.01)
        assert payload["summary"]["repo_total_cost"] == pytest.approx(
            TASK_COST + 3 * PLAN_COST, abs=0.01
        )

    def test_the_text_table_names_the_planning_line(self, root, capsys):
        _run(
            root, ["plan", "--gated", "--stage", "requirements", "x"], [_stage_text("requirements")]
        )

        capsys.readouterr()
        cli.main(["costs", "--project-root", str(root)])
        out = capsys.readouterr().out

        assert "planning" in out and "Repo total" in out

    def test_a_project_that_never_planned_sees_no_new_keys(self, root, capsys):
        payload = _costs_json(root, capsys)

        assert "planning" not in payload
        assert "planning_cost" not in payload["summary"]
        assert "repo_total_cost" not in payload["summary"]

    def test_the_payload_validates_against_the_schema(self, root, capsys):
        jsonschema = pytest.importorskip("jsonschema")
        _run(
            root, ["plan", "--gated", "--stage", "requirements", "x"], [_stage_text("requirements")]
        )

        payload = _costs_json(root, capsys)

        schema = json.loads(
            (Path(__file__).parent.parent / "schemas" / "costs.schema.json").read_text()
        )
        jsonschema.validate(payload, schema)


class TestAnUnpricedPlanningCallIsAFloor:
    def test_unknown_cost_is_null_and_counted_not_zeroed(self, root, capsys):
        journal = Journal()
        with (
            spawn_double(journal, lambda argv: completed(argv, _stage_text("requirements"))),
            contextlib.suppress(SystemExit),
        ):
            cli.main(
                ["plan", "--gated", "--stage", "requirements", "x", "--project-root", str(root)]
            )

        rows = _rows(root, "plan_agent_calls")
        assert rows[0][4] is None
        payload = _costs_json(root, capsys)
        assert payload["planning"]["unmeasured_calls"] == 1
        assert payload["summary"]["planning_unmeasured_calls"] == 1
