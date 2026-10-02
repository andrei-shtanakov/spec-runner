"""#480 BEH-05/06/07: a paid call's intent is durable before its process exists.

The store double writes ``call_start(call_id)`` into a journal when it
acknowledges a call-start; the ``_spawn`` double writes ``spawn`` into the same
journal. Every spawn must be *immediately* preceded by a call-start with an
ack, and the pair must share one ``call_id``. The real agent name (``claude``)
is in ``claude_command`` throughout: the double of ``_spawn`` replaces the
guard (a documented property of the guard) and the belt stays underneath.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from spec_runner import cli_plan, execution, paid_call, review, review_pr, run_context, tdd
from spec_runner.errors import ERROR_KINDS
from spec_runner.runner import build_cli_invocation
from spec_runner.state import ErrorCode, ExecutorState
from spec_runner.task import Task
from tests.call_doubles import (
    Journal,
    RecordingStore,
    adjacent_pairs,
    completed,
    make_run,
    project,
    spawn_double,
)

TASK = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1d")

#: The dictionary of provenances call-start carries (FR-02). A value that no
#: site of the matrix produces is a red result of the last test of this file.
PROVENANCE_DICTIONARY = {
    "red",
    "red:fix",
    "green",
    "review",
    "review:quality",
    "plan:requirements",
    "plan:design",
    "plan:tasks",
    "plan:interactive",
    "review-pr:verify",
    "review-pr:fix",
    "doctor:execute",
    "doctor:review",
}


@pytest.fixture
def journal() -> Journal:
    return Journal()


# --- the sites ---------------------------------------------------------------


def _invocation(cfg, prompt="do the work"):
    return build_cli_invocation(
        cmd=cfg.claude_command,
        prompt=prompt,
        model=None,
        template=None,
        skip_permissions=False,
        json_output=True,
    )


def site_red(cfg):
    with (
        ExecutorState(cfg) as state,
        tdd._paid_scope(TASK, "red", tdd.RED_AUTHORING, state, "p", None),
    ):
        tdd._run_agent(cfg, "write the failing test")


def site_red_fix(cfg):
    with (
        ExecutorState(cfg) as state,
        tdd._paid_scope(TASK, "red:fix", tdd.RED_AUTOFIX_AGENT_ROUND, state, "p", None),
    ):
        tdd._run_agent(cfg, "fix the findings")


def site_green(cfg):
    with (
        ExecutorState(cfg) as state,
        paid_call.scope(
            task_id=TASK.id, provenance="green", attempt=1, state=state, price_on_attempt=True
        ),
    ):
        execution._run_agent_process(cfg, _invocation(cfg))


def site_review(cfg):
    review._run_reviewer(cfg, TASK.id, "review", "review it", "claude", "", "")


def site_review_role(cfg):
    review._run_reviewer(cfg, TASK.id, "review:quality", "review it", "claude", "", "")


def site_review_pr_verify(cfg):
    with review_pr.ReviewPrState(cfg) as ledger:
        comment = review_pr.BotComment(
            comment_id=1, author="bot", path="a.py", line=1, body="b", diff_hunk="", url=""
        )
        review_pr.verify_comment(comment, "o/r", 6, cfg, ledger=ledger, head_sha="abc")


def site_review_pr_fix(cfg):
    with review_pr.ReviewPrState(cfg) as ledger:
        comment = review_pr.BotComment(
            comment_id=1, author="bot", path="a.py", line=1, body="b", diff_hunk="", url=""
        )
        review_pr.run_fix_agent(comment, "evidence", "o/r", 6, cfg, ledger=ledger, head_sha="abc")


SITES = {
    "red": site_red,
    "red:fix": site_red_fix,
    "green": site_green,
    "review": site_review,
    "review:quality": site_review_role,
    "review-pr:verify": site_review_pr_verify,
    "review-pr:fix": site_review_pr_fix,
}


@pytest.mark.parametrize("provenance", sorted(SITES))
def test_every_task_site_acknowledges_the_call_start_before_it_spawns(
    provenance, tmp_path, journal
):
    cfg = project(tmp_path)
    make_run(journal)

    with spawn_double(journal):
        SITES[provenance](cfg)

    assert adjacent_pairs(journal) == [(journal.call_ids_in_order()[0], provenance)]
    assert len(journal.of("spawn")) == len(journal.of("call_start")) == 1


@pytest.mark.parametrize("provenance", sorted(SITES))
def test_the_start_record_carries_the_identity_and_the_ledger_shares_the_call_id(
    provenance, tmp_path, journal
):
    cfg = project(tmp_path)
    ctx = make_run(journal)

    with spawn_double(journal):
        SITES[provenance](cfg)

    (start,) = ctx.test_store.records("call-start")
    assert start["run_id"] == ctx.run_id
    assert start["provenance"] == provenance
    assert start["call_id"] == journal.call_ids_in_order()[0]
    assert start["policy"]["contract_version"] == 1
    assert len(start["prompt_sha256"]) == 64 and start["timestamp"]
    if provenance.startswith("review-pr"):
        assert start["task_id"] is None
        table = "pr_agent_calls"
    else:
        assert start["task_id"] == TASK.id
        assert start["attempt"] == 1
        table = "agent_calls"
    conn = sqlite3.connect(cfg.state_file)
    try:
        rows = conn.execute(f"SELECT call_id, run_id, status FROM {table}").fetchall()
    finally:
        conn.close()
    assert rows == [(start["call_id"], ctx.run_id, "closed")]


# --- planning ----------------------------------------------------------------

_BODIES = {
    "requirements": "# Requirements\n\n## Out of Scope\n- none\n\n"
    "#### REQ-001: X\n**Acceptance Criteria:**\nGIVEN a WHEN b THEN c\n",
    "design": "# Design\n\n### DESIGN-001: Y trace [REQ-001]\n",
    "tasks": "# Tasks\n\n### TASK-001: Do X [REQ-001] [DESIGN-001]\nP0 | TODO\n- [ ] do it\n",
}
_MARK = {"requirements": "SPEC_REQUIREMENTS", "design": "SPEC_DESIGN", "tasks": "SPEC_TASKS"}


def _plan_answer(argv):
    prompt = " ".join(argv)
    for stage in ("requirements", "design", "tasks"):
        if f"{_MARK[stage]}_READY" in prompt:
            body = _BODIES[stage]
            return completed(argv, f"{_MARK[stage]}_READY\n{body}\n{_MARK[stage]}_END\n")
    return completed(argv, "PLAN_READY\n")


def _stage_answer(stage):
    body = _BODIES[stage]
    return lambda argv: completed(argv, f"{_MARK[stage]}_READY\n{body}\n{_MARK[stage]}_END\n")


def test_plan_full_acknowledges_each_of_its_three_stages(tmp_path, journal):
    cfg = project(tmp_path)
    make_run(journal)
    stages = iter(["requirements", "design", "tasks"])

    def answer(argv):
        return _stage_answer(next(stages))(argv)

    args = SimpleNamespace(
        full=True, gated=False, description="build x", from_file=None, stage=None
    )
    with spawn_double(journal, answer):
        cli_plan.cmd_plan(args, cfg)

    assert [p for _, p in adjacent_pairs(journal)] == [
        "plan:requirements",
        "plan:design",
        "plan:tasks",
    ]


def test_plan_gated_acknowledges_its_stage(tmp_path, journal):
    cfg = project(tmp_path)
    make_run(journal)

    with spawn_double(journal, _stage_answer("requirements")):
        rc = cli_plan.run_gated_stage("requirements", "Build X", cfg)

    assert rc == 0
    assert [p for _, p in adjacent_pairs(journal)] == ["plan:requirements"]


def test_interactive_plan_acknowledges_its_round(tmp_path, journal, monkeypatch):
    cfg = project(tmp_path)
    make_run(journal)
    monkeypatch.setattr("builtins.input", lambda *_a: "n")
    args = SimpleNamespace(
        full=False, gated=False, description="build x", from_file=None, stage=None
    )

    with spawn_double(journal, lambda argv: completed(argv, "PLAN_READY\n")):
        cli_plan.cmd_plan(args, cfg)

    assert [p for _, p in adjacent_pairs(journal)] == ["plan:interactive"]


def test_interactive_plan_refused_start_exits_2(tmp_path, journal, monkeypatch):
    """Review of #653: the interactive loop's broad `except` swallowed the
    refusal, so `plan` exited 0 and its closure said `completed`."""
    cfg = project(tmp_path)
    make_run(journal, RecordingStore(journal, refuse_starts=True), ack_timeout=0.1)
    monkeypatch.setattr("builtins.input", lambda *_a: "n")
    args = SimpleNamespace(
        full=False, gated=False, description="build x", from_file=None, stage=None
    )

    with spawn_double(journal), pytest.raises(SystemExit) as exit_info:
        cli_plan.cmd_plan(args, cfg)

    assert exit_info.value.code == 2


def test_plan_calls_land_in_the_plan_ledger_and_not_in_agent_calls(tmp_path, journal):
    cfg = project(tmp_path)
    ctx = make_run(journal)

    with spawn_double(journal, _stage_answer("requirements")):
        cli_plan.run_gated_stage("requirements", "Build X", cfg)

    (start,) = ctx.test_store.records("call-start")
    conn = sqlite3.connect(cfg.state_file)
    try:
        plan_rows = conn.execute(
            "SELECT provenance, call_id, run_id FROM plan_agent_calls"
        ).fetchall()
        task_rows = conn.execute("SELECT COUNT(*) FROM agent_calls").fetchone()[0]
    finally:
        conn.close()
    assert plan_rows == [("plan:requirements", start["call_id"], ctx.run_id)]
    assert task_rows == 0
    assert start["task_id"] is None


# --- the probe -----------------------------------------------------------------


def test_the_probe_publishes_its_two_calls_without_a_task(tmp_path, journal):
    from spec_runner import doctor

    base = project(tmp_path / "caller", run_review=True)
    ctx = make_run(journal)
    scratch, root = doctor.build_scratch(base, with_review=True, budget=1.0, timeout_min=1)

    def priced(text):
        return json.dumps(
            {
                "result": text,
                "total_cost_usd": 0.01,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        )

    def answer(argv):
        text = " ".join(argv)
        if "Code Review Request" in text:
            return completed(argv, priced("REVIEW_PASSED"))
        return completed(argv, priced("TASK_COMPLETE"))

    try:
        with spawn_double(journal, answer):
            doctor.run_probe(scratch)
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)

    starts = ctx.test_store.records("call-start")
    assert sorted(s["provenance"] for s in starts) == ["doctor:execute", "doctor:review"]
    assert all(s["task_id"] is None for s in starts)
    assert [p for _, p in adjacent_pairs(journal)] == ["doctor:execute", "doctor:review"]


def test_a_provenance_outside_the_probe_map_is_refused_before_call_start(tmp_path, journal):
    cfg = project(tmp_path, probe_provenance="doctor")
    ctx = make_run(journal)

    with (
        spawn_double(journal),
        pytest.raises(paid_call.CallRefused) as refused,
        paid_call.scope(task_id="TASK-001", provenance="red", attempt=1),
    ):
        tdd._run_agent(cfg, "p")

    assert refused.value.refusal.kind.value == "instrument"
    assert journal.events == []
    assert ctx.test_store.objects == {}


# --- BEH-06: no ack, no process ---------------------------------------------


def _tdd_task_project(tmp_path):
    cfg = project(
        tmp_path,
        execution_mode="tdd",
        test_command="pytest",
        tdd_runner="pytest",
        auto_commit=True,
    )
    (cfg.project_root / "spec" / "tasks.md").write_text(
        "# Tasks\n\n### TASK-001: t\n🟠 P1 | ⬜ TODO | Est: 1d\n\n**Checklist:**\n- [ ] x\n"
    )
    for args in (
        ("init", "-q"),
        ("config", "user.email", "t@example.com"),
        ("config", "user.name", "t"),
        ("add", "-A"),
        ("commit", "-qm", "base"),
    ):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)
    return cfg


@pytest.mark.parametrize("failure", ["refuses", "stalls"])
def test_without_an_ack_the_process_never_starts(tmp_path, journal, failure):
    cfg = _tdd_task_project(tmp_path)
    store = RecordingStore(journal, refuse_starts=True, stall=0.5 if failure == "stalls" else 0.0)
    make_run(journal, store, ack_timeout=0.1)

    with spawn_double(journal) as argvs, ExecutorState(cfg) as state:
        result = execution.execute_task(TASK, cfg, state)

    assert result is False
    assert argvs == [], "the process was started without an acknowledged call-start"
    assert journal.of("spawn") == []
    with ExecutorState(cfg) as state:
        attempt = state.get_task_state(TASK.id).attempts[-1]
        assert state.get_task_state(TASK.id).status != "success"
    assert attempt.error_code is ErrorCode.INFRASTRUCTURE
    assert attempt.error_kind == "instrument"
    assert "call_start_not_acknowledged" in (attempt.error or "")


def test_the_refusal_is_a_typed_instrument_refusal(tmp_path, journal):
    cfg = project(tmp_path)
    make_run(journal, RecordingStore(journal, refuse_starts=True), ack_timeout=0.1)

    with spawn_double(journal), pytest.raises(paid_call.CallRefused) as refused:
        tdd._run_agent(cfg, "p")

    assert refused.value.refusal.error_code is ErrorCode.INFRASTRUCTURE
    assert "call_start_not_acknowledged" in str(refused.value.refusal)


def test_a_refused_start_closes_the_ledger_row_as_not_started(tmp_path, journal):
    cfg = project(tmp_path)
    make_run(journal, RecordingStore(journal, refuse_starts=True), ack_timeout=0.1)

    with spawn_double(journal), pytest.raises(paid_call.CallRefused):
        site_review(cfg)

    conn = sqlite3.connect(cfg.state_file)
    try:
        assert conn.execute("SELECT status FROM agent_calls").fetchall() == [("not_started",)]
    finally:
        conn.close()
    with ExecutorState(cfg) as state:
        assert state.agent_calls() == [], "a call that never started is not a call"


def test_the_prompt_artefact_of_a_refused_start_says_so(tmp_path, journal):
    cfg = project(tmp_path)
    make_run(journal, RecordingStore(journal, refuse_starts=True), ack_timeout=0.1)
    artefact = tmp_path / "prompt.log"
    artefact.write_text("=== RED PROMPT ===\nthe prompt\n")

    with (
        spawn_double(journal),
        pytest.raises(paid_call.CallRefused),
        paid_call.scope(task_id="TASK-001", provenance="red", prompt_log=artefact),
    ):
        tdd._run_agent(cfg, "p")

    assert artefact.read_text().rstrip().endswith("===") and "NOT STARTED" in artefact.read_text()


def test_the_closure_of_a_refused_run_is_failed_with_exit_2(tmp_path, journal):
    """Through ``cli.main``: the typed refusal becomes exit 2 and a closure."""
    from spec_runner import cli

    cfg = _tdd_task_project(tmp_path)
    store = RecordingStore(journal, refuse_starts=True)
    seen = {}

    def fake_start(self, config):
        from spec_runner.evidence import Publisher

        self.paying = True
        self.publisher = Publisher(store, ack_timeout=0.1)
        self.started = True
        seen["ctx"] = self

    def refuse_in_handler(args, config):
        with spawn_double(journal), ExecutorState(config) as state:
            ok = execution.execute_task(TASK, config, state)
        raise SystemExit(0 if ok else 2)

    with (
        patch.object(run_context.RunContext, "start", fake_start),
        patch.object(cli, "_dispatch", refuse_in_handler),
        patch.object(cli, "load_config_from_yaml", return_value={}),
        patch.object(cli, "build_config", return_value=cfg),
        pytest.raises(SystemExit) as exit_info,
    ):
        cli.main(["run", "--task", "TASK-001", "--project-root", str(tmp_path)])

    assert exit_info.value.code == 2
    # The store that refused every call-start still takes the closure.
    (closure,) = store.records("closure")
    assert closure["closure_kind"] == "failed" and closure["exit_code"] == 2
    assert journal.of("spawn") == []


# --- BEH-07: a budget refusal happens before call-start --------------------------


def test_a_budget_refusal_leaves_no_start_no_spawn_and_no_row(tmp_path, journal):
    from spec_runner.budget import BudgetRefused

    cfg = project(tmp_path, task_budget_usd=1.0)
    make_run(journal)
    with ExecutorState(cfg) as state:
        state.record_agent_call(TASK.id, "red_authoring", cost_usd=1.5)

    with spawn_double(journal), ExecutorState(cfg) as state, pytest.raises(BudgetRefused):
        from spec_runner.budget import check_before_call

        refusal = check_before_call(cfg, state, TASK.id, "green")
        assert refusal is not None
        raise BudgetRefused(refusal)

    assert journal.events == []
    with ExecutorState(cfg) as state:
        assert [c["provenance"] for c in state.agent_calls()] == ["red_authoring"], (
            "a refusal is no row"
        )


def test_a_budget_stop_is_refused_not_failed_in_the_closure():
    from spec_runner import closure

    assert "budget" in ERROR_KINDS
    outcome = closure.Outcome(exit_code=1, last_error_kind="budget")
    assert closure.derive(outcome) == "refused"


# --- the matrix covers the dictionary ---------------------------------------


def test_the_dictionary_has_no_value_without_a_site():
    covered = set(SITES) | {
        "plan:requirements",
        "plan:design",
        "plan:tasks",
        "plan:interactive",
        "doctor:execute",
        "doctor:review",
    }
    assert covered == PROVENANCE_DICTIONARY


def test_json_in_the_journal_is_what_the_store_took(tmp_path, journal):
    """The record the store acknowledged is the one that names the call."""
    cfg = project(tmp_path)
    ctx = make_run(journal)

    with spawn_double(journal):
        site_review(cfg)

    key = next(k for k in ctx.test_store.list("") if k.endswith("/start.json"))
    assert json.loads(ctx.test_store.objects[key])["call_id"] in key


def test_a_timeout_is_a_result_not_an_open_call(tmp_path, journal):
    cfg = project(tmp_path)
    ctx = make_run(journal)

    def timeout(argv):
        raise subprocess.TimeoutExpired(argv, 1)

    with spawn_double(journal, timeout):
        call = review._run_reviewer(cfg, TASK.id, "review", "p", "claude", "", "")

    assert call.timed_out
    (result,) = ctx.test_store.records("call-result")
    assert result["outcome"] == "timeout" and result["cost_usd"] is None
    assert ctx.open_call_ids == set()
