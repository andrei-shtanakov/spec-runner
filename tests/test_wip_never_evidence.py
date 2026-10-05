"""A WIP SHA is never a verdict's or evidence's SHA (PR #661 blocker 1).

Spec 2026-10-04 §4: "the carried work is not a candidate … and is never the
SHA a gate verdict is bound to". Two holes the owner set before approve:

- `auto_commit: false` + a registered gate + HEAD on this task's WIP commit:
  no candidate can be made, so the gates judged the WIP SHA. Now an explicit
  POLICY refusal, terminal, before any gate runs.
- R7 (no candidate before `gated_sha` in a project with no review and no
  gates) left `gated_sha` naming WIP; the post_review-plugin refusal wrote it
  as `Gate-Candidate:` evidence into the bookkeeping commit.
"""

import pytest

from spec_runner.phases import Refusal, RefusalKind
from tests.test_candidate_commit import _cfg as cc_cfg
from tests.test_candidate_commit import _git as cc_git
from tests.test_candidate_commit import _recording_gate
from tests.test_candidate_commit import _task as cc_task
from tests.test_wip_and_gates import _cc_branch_with_wip

pytestmark = pytest.mark.slow


def _head(root):
    return cc_git(root, "rev-parse", "HEAD").stdout.strip()


class TestNoCandidateWithoutAutoCommit:
    def test_refused_before_the_gate(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _cc_branch_with_wip(tmp_path)
        cfg = cc_cfg(root, create_git_branch=True, auto_commit=False)
        seen = _recording_gate(monkeypatch)
        ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)
        assert ok is False
        assert seen == [], "a gate judged the WIP commit"
        assert isinstance(err, Refusal)
        assert err.kind == RefusalKind.POLICY
        assert err.terminal is True
        assert "auto_commit" in err and "commit" in err

    def test_review_is_not_paid_for_either(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _cc_branch_with_wip(tmp_path)
        cfg = cc_cfg(root, create_git_branch=True, auto_commit=False, run_review=True)
        _recording_gate(monkeypatch)
        reviewed: list[object] = []
        monkeypatch.setattr(hooks, "run_code_review", lambda *a, **k: reviewed.append(a))
        ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)
        assert ok is False and isinstance(err, Refusal)
        assert reviewed == []

    def test_no_gate_is_refused_too(self, tmp_path, monkeypatch):
        """Round 2: the backstop refuses in every configuration, before the merge."""
        from spec_runner import gates as gates_mod
        from spec_runner import hooks
        from spec_runner.gates import GateRegistry

        root = _cc_branch_with_wip(tmp_path)
        refs_before = cc_git(root, "show-ref", "--heads").stdout
        cfg = cc_cfg(root, create_git_branch=True, auto_commit=False)
        monkeypatch.setattr(gates_mod, "REGISTRY", GateRegistry())
        ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)
        assert ok is False
        assert isinstance(err, Refusal) and err.kind == RefusalKind.POLICY and err.terminal
        assert cc_git(root, "show-ref", "--heads").stdout == refs_before, "a branch moved"

    def test_a_commit_above_the_wip_is_judged_normally(self, tmp_path, monkeypatch):
        """Only a WIP HEAD is refused; the operator's own commit is a candidate."""
        from spec_runner import hooks

        root = _cc_branch_with_wip(tmp_path)
        (root / "mine.py").write_text("x\n")
        cc_git(root, "add", "-A")
        cc_git(root, "commit", "-qm", "operator commit")
        operator_sha = _head(root)
        cfg = cc_cfg(root, create_git_branch=True, auto_commit=False)
        seen = _recording_gate(monkeypatch)
        ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)
        assert ok is True, err
        assert seen == [operator_sha]

    def test_unreadable_head_is_an_instrument_refusal(self, tmp_path, monkeypatch):
        from spec_runner import hooks, wip

        root = _cc_branch_with_wip(tmp_path)
        cfg = cc_cfg(root, create_git_branch=True, auto_commit=False)
        seen = _recording_gate(monkeypatch)

        def boom(config, task_id):
            raise wip.WipReadError("cannot read HEAD")

        monkeypatch.setattr(wip, "head_is_wip_of", boom)
        ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)
        assert ok is False and seen == []
        assert isinstance(err, Refusal) and err.kind == RefusalKind.INSTRUMENT


def _tasks_flip_to_in_progress(root):
    """Committed TODO, working tree IN_PROGRESS — the flip a run writes."""
    tasks = root / "spec" / "tasks.md"
    tasks.write_text(tasks.read_text().replace("🔄 IN_PROGRESS", "⬜ TODO"))
    cc_git(root, "add", "-A")
    cc_git(root, "commit", "-q", "--amend", "--no-edit")
    tasks.write_text(tasks.read_text().replace("⬜ TODO", "🔄 IN_PROGRESS"))


class TestPluginRefusalNamesNoWip:
    def test_bookkeeping_commit_has_no_wip_gate_candidate(self, tmp_path, monkeypatch):
        from spec_runner import gates as gates_mod
        from spec_runner import hooks
        from spec_runner.gates import GateRegistry

        root = _cc_branch_with_wip(tmp_path)
        _tasks_flip_to_in_progress(root)
        wip_sha = _head(root)
        cfg = cc_cfg(root, create_git_branch=True)
        monkeypatch.setattr(gates_mod, "REGISTRY", GateRegistry())
        real = hooks.run_plugin_hooks_for

        def plugin(point, task, config, success):
            if point == "post_review":
                return "exporter refused"
            return real(point, task, config, success=success)

        monkeypatch.setattr(hooks, "run_plugin_hooks_for", plugin)
        ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)
        assert ok is False and "exporter refused" in err
        assert _head(root) != wip_sha, "the bookkeeping commit was not made"
        body = cc_git(root, "log", "-1", "--format=%B").stdout
        assert "Task-Status: TASK-001" in body
        assert wip_sha not in body
        assert "Gate-Candidate" not in body

    def test_a_real_candidate_is_still_named(self, tmp_path, monkeypatch):
        """The filter drops WIP only; an ordinary candidate stays evidence."""
        from spec_runner import hooks
        from spec_runner.state import ReviewVerdict

        root = _cc_branch_with_wip(tmp_path)
        (root / "work.py").write_text("x\n")
        cfg = cc_cfg(root, create_git_branch=True, run_review=True)
        seen = _recording_gate(monkeypatch)
        monkeypatch.setattr(
            hooks, "run_code_review", lambda *a, **k: (ReviewVerdict.PASSED, None, "ok")
        )
        real = hooks.run_plugin_hooks_for

        def plugin(point, task, config, success):
            if point == "post_review":
                return "exporter refused"
            return real(point, task, config, success=success)

        monkeypatch.setattr(hooks, "run_plugin_hooks_for", plugin)
        ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)
        assert ok is False
        body = cc_git(root, "log", "-1", "--format=%B").stdout
        candidate = cc_git(root, "rev-parse", "HEAD~1").stdout.strip()
        assert seen == [candidate]
        assert f"Gate-Candidate: {candidate}" in body


def test_gates_never_evaluate_a_wip_sha(tmp_path, monkeypatch):
    """Invariant at the one gate site: a WIP SHA is refused, not judged."""
    from spec_runner import hooks

    root = _cc_branch_with_wip(tmp_path)
    cfg = cc_cfg(root, create_git_branch=True)
    seen = _recording_gate(monkeypatch)
    blocked = hooks._run_pre_terminal_gates(cc_task(), cfg, candidate_sha=_head(root))
    assert seen == []
    assert isinstance(blocked, Refusal) and blocked.kind == RefusalKind.INSTRUMENT


def test_gates_never_receive_a_wip_review_checkpoint(tmp_path, monkeypatch):
    from spec_runner import hooks

    root = _cc_branch_with_wip(tmp_path)
    wip_sha = _head(root)
    (root / "c.py").write_text("x\n")
    cc_git(root, "add", "-A")
    cc_git(root, "commit", "-qm", "TASK-001: candidate")
    cfg = cc_cfg(root, create_git_branch=True)
    seen = _recording_gate(monkeypatch)
    blocked = hooks._run_pre_terminal_gates(
        cc_task(),
        cfg,
        candidate_sha=_head(root),
        facts={"review_checkpoint_sha": wip_sha},
    )
    assert seen == []
    assert isinstance(blocked, Refusal) and blocked.kind == RefusalKind.INSTRUMENT


def test_a_pre_implementation_verdict_on_wip_is_not_recorded(tmp_path, monkeypatch):
    """A retry's pre-implementation gates judge the tree in hand (HEAD = WIP);
    the answer stands, but no `gate_verdicts` row binds it to the WIP SHA."""
    from spec_runner.gates import GateContext, GateRegistry, GateResult, GateStatus, evaluate_gates
    from spec_runner.state import ExecutorState, PhaseOutcome

    root = _cc_branch_with_wip(tmp_path)
    wip_sha = _head(root)
    cfg = cc_cfg(root, create_git_branch=True)
    registry = GateRegistry()
    registry.register(
        "probe", "tests", lambda ctx: GateResult(GateStatus.SATISFIED, PhaseOutcome.PASS)
    )
    with ExecutorState(cfg) as state:
        ctx = GateContext(task_id="TASK-001", checkpoint_sha=wip_sha, config=cfg, state=state)
        outcome = evaluate_gates("tests", ctx, registry=registry)
        assert outcome.status is GateStatus.SATISFIED
        assert state.gate_verdict("TASK-001", "probe", wip_sha, ctx.config_hash) is None
        rows = state._conn.execute("SELECT checkpoint_sha FROM gate_verdicts").fetchall()
        assert rows == []


def test_a_verdict_on_an_ordinary_commit_is_recorded(tmp_path):
    from spec_runner.gates import GateContext, GateRegistry, GateResult, GateStatus, evaluate_gates
    from spec_runner.state import ExecutorState, PhaseOutcome

    root = _cc_branch_with_wip(tmp_path)
    (root / "c.py").write_text("x\n")
    cc_git(root, "add", "-A")
    cc_git(root, "commit", "-qm", "TASK-001: candidate")
    sha = _head(root)
    cfg = cc_cfg(root, create_git_branch=True)
    registry = GateRegistry()
    registry.register(
        "probe", "tests", lambda ctx: GateResult(GateStatus.SATISFIED, PhaseOutcome.PASS)
    )
    with ExecutorState(cfg) as state:
        ctx = GateContext(task_id="TASK-001", checkpoint_sha=sha, config=cfg, state=state)
        evaluate_gates("tests", ctx, registry=registry)
        assert state.gate_verdict("TASK-001", "probe", sha, ctx.config_hash) is not None
