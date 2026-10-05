"""`tdd abandon` drops the trusted baseline; the way back is `harness trust` (owner, PR #661).

Owner decision: abandon keeps deleting workspace, baseline and file rows
atomically with its own records (the trust audit is kept). It does not
confirm the current harness: the next start under `strict` on the surviving
branch is refused until the operator restores and checks the harness and
runs `harness trust --bind-branch`. A new `initial` is never auto-captured.
"""

import argparse
import subprocess

import pytest

from spec_runner import paid_call
from spec_runner.claims import record_claims
from spec_runner.remedy import abandon, cmd_tdd
from spec_runner.state import ExecutorState
from spec_runner.tdd import RedCheckpoint, RedOutcome, resolve_namespace
from tests.test_retry_continues_from_wip import BRANCH, _cfg, _fail_once, _git, _task, repo

__all__ = ["repo"]

pytestmark = pytest.mark.slow


def _red_for_task_070(cfg) -> RedCheckpoint:
    """A confirmed red for TASK-070 at HEAD, as a tdd attempt would leave it."""
    root = cfg.project_root
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_x.py").write_text("def test_y():\n    assert False\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "TASK-070: red for tests/test_x.py::test_y")
    sha = _git(root, "rev-parse", "HEAD").strip()
    cp = RedCheckpoint(
        task_id="TASK-070",
        namespace=resolve_namespace(cfg),
        commit_sha=sha,
        baseline_sha=sha,
        selector="tests/test_x.py::test_y",
        environment_id="unpinned",
        execution_mode="tdd",
        config_hash="h",
        outcome=RedOutcome.EXPECTED_FAIL,
        timestamp="2026-10-05T00:00:00",
    )
    with ExecutorState(cfg) as st:
        st.record_red_checkpoint(cp)
        record_claims(cfg, st, cp)
    return cp


def _spawn_spy(monkeypatch) -> list[int]:
    calls: list[int] = []

    def _spawn(invocation, *, timeout, cwd, env):
        calls.append(1)
        return subprocess.CompletedProcess(invocation.argv, 0, "TASK_COMPLETE\n", "")

    monkeypatch.setattr(paid_call, "_spawn", _spawn)
    return calls


def _abandoned(repo, monkeypatch):
    _fail_once(repo, monkeypatch)  # strict, branch, workspace row, `initial` baseline
    cfg = _cfg(repo, max_retries=1, run_lint_on_done=False)
    (repo / "feature.py").unlink()  # the failed attempt's dirt is not the subject here
    ns = resolve_namespace(cfg)
    with ExecutorState(cfg) as st:
        assert st.get_harness_baseline(ns, "TASK-070").provenance == "initial"
    cp = _red_for_task_070(cfg)
    with ExecutorState(cfg) as st:
        abandon(cfg, st, "TASK-070", cp.checkpoint_id, reason="the red was wrong")
        assert st.get_workspace(ns, "TASK-070") is None
        assert st.get_harness_baseline(ns, "TASK-070") is None
    return cfg, ns, cp


def test_abandon_retry_trust_retry(repo, monkeypatch):
    from spec_runner.execution import execute_task
    from spec_runner.harness_cmd import trust

    cfg, ns, _cp = _abandoned(repo, monkeypatch)
    calls = _spawn_spy(monkeypatch)

    with ExecutorState(cfg) as st:
        execute_task(_task(), cfg, st)
        last = st.get_task_state("TASK-070").attempts[-1]
        assert st.get_harness_baseline(ns, "TASK-070") is None, "a baseline was auto-captured"
    assert calls == [], "the agent ran without a trusted harness"
    assert last.error_kind == "policy"
    assert f"harness trust TASK-070 --bind-branch {BRANCH}" in (last.error or "")
    assert "restore" in (last.error or "")

    with ExecutorState(cfg) as st:
        trust(cfg, st, "TASK-070", reason="restored from main and checked", bind_branch=BRANCH)
        assert st.get_harness_baseline(ns, "TASK-070").provenance == "operator"

    with ExecutorState(cfg) as st:
        result = execute_task(_task(), cfg, st)
        # It ran to DONE, which drops the operator baseline with the task (§2).
        assert st.get_task_state("TASK-070").status == "success"
    assert result is True
    assert calls, "the retry did not proceed after harness trust"


@pytest.mark.parametrize("guard", ["strict", "warn"])
def test_abandon_never_yields_a_new_initial(repo, monkeypatch, guard):
    """Under `warn` the start proceeds with a `recaptured` (untrusted) snapshot."""
    from spec_runner.execution import execute_task

    cfg, ns, _cp = _abandoned(repo, monkeypatch)
    cfg.harness_guard = guard
    _spawn_spy(monkeypatch)
    with ExecutorState(cfg) as st:
        execute_task(_task(), cfg, st)
        stored = st.get_harness_baseline(ns, "TASK-070")
    assert stored is None or stored.provenance == "recaptured"


def test_abandon_output_names_the_way_back(repo, monkeypatch, capsys):
    _fail_once(repo, monkeypatch)
    cfg = _cfg(repo, max_retries=1)
    cp = _red_for_task_070(cfg)
    code = cmd_tdd(
        argparse.Namespace(
            tdd_command="abandon",
            task_id="TASK-070",
            checkpoint=cp.checkpoint_id,
            reason="the red was wrong",
            actor=None,
        ),
        cfg,
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "Abandoned" in out
    assert "restore the harness" in out
    assert f'spec-runner harness trust TASK-070 --bind-branch {BRANCH} --reason "…"' in out
