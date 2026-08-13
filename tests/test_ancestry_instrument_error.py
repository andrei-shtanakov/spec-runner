"""#245: "not an ancestor" and "git could not look" are different answers.

`_descends_from` returned `returncode == 0`, so exit 1 ("no") and exit 128 ("a
bad object, a missing repo, an unreadable one") became the same `False`, and
`_red_gate` reported:

    UNSATISFIED — the confirmed red is on a different tree

for a commit that is simply not in this clone. That is a **verdict about the
work** standing in for a broken instrument — the class #230 part 1 fixed one
layer down, where an infrastructure failure was recorded as a task failure and
reached CI as exit 1 instead of 2.

The fix is the same fail-closed contract, split three ways:

| git says | means | gate |
|---|---|---|
| exit 0 | the red covers this tree | `SATISFIED` |
| exit 1 | it does not | `UNSATISFIED` — an honest verdict |
| anything else, or git will not run | nothing is known | `INSTRUMENT_ERROR` |

It protects `tdd resume` directly: a resume reinstates a red whose commit the
gate must then locate, and a clone missing that history should say so rather
than declare the evidence irrelevant.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.claims import record_claims
from spec_runner.config import ExecutorConfig
from spec_runner.gates import AncestryUnknown, GateContext, GateStatus, _descends_from, _red_gate
from spec_runner.state import ExecutorState
from spec_runner.tdd import RedCheckpoint, RedOutcome, _config_hash, resolve_namespace


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _cfg(root: Path) -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root.parent / ".state.db",
        logs_dir=root.parent / ".logs",
        execution_mode="tdd",
        test_command="python -m pytest",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _repo_with_red(tmp_path: Path, *, commit_sha: str | None = None):
    """A confirmed red and a green on top of it, as a real run leaves them."""
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "o@e.c")
    _git(root, "config", "user.name", "O")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_x.py").write_text("def test_y():\n    assert False\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "red")
    red_sha = _git(root, "rev-parse", "HEAD").stdout.strip()
    (root / "src.py").write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "green")

    cfg = _cfg(root)
    checkpoint = RedCheckpoint(
        task_id="TASK-001",
        namespace=resolve_namespace(cfg),
        commit_sha=commit_sha or red_sha,
        baseline_sha=red_sha,
        selector="tests/test_x.py::test_y",
        environment_id="unpinned",
        execution_mode="tdd",
        config_hash=_config_hash(cfg),
        outcome=RedOutcome.EXPECTED_FAIL,
        timestamp="2026-08-13T00:00:00",
    )
    with ExecutorState(cfg) as state:
        state.record_red_checkpoint(checkpoint)
        record_claims(cfg, state, checkpoint)
    return root, cfg, checkpoint


def _evaluate(cfg: ExecutorConfig, head: str):
    with ExecutorState(cfg) as state:
        return _red_gate(
            GateContext(
                task_id="TASK-001",
                checkpoint_sha=head,
                config=cfg,
                state=state,
                facts={"execution_mode": "tdd"},
            )
        )


class TestTheHelper:
    def test_an_ancestor_is_true(self, tmp_path):
        root, cfg, cp = _repo_with_red(tmp_path)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        assert _descends_from(cfg, cp.commit_sha, head) is True

    def test_a_non_ancestor_is_false(self, tmp_path):
        root, cfg, cp = _repo_with_red(tmp_path)
        base = _git(root, "rev-list", "--max-parents=0", "HEAD").stdout.strip()
        assert _descends_from(cfg, cp.commit_sha, base) is False

    def test_a_commit_that_is_not_here_raises(self, tmp_path):
        """Exit 128, not exit 1 — and the difference is the whole issue."""
        root, cfg, _cp = _repo_with_red(tmp_path)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        with pytest.raises(AncestryUnknown, match="could not compare"):
            _descends_from(cfg, "0" * 40, head)

    def test_a_git_that_cannot_run_raises(self, tmp_path):
        """One step earlier, same answer: nothing was learned."""
        cfg = _cfg(tmp_path / "nowhere")
        with pytest.raises(AncestryUnknown, match="could not be run"):
            _descends_from(cfg, "a" * 40, "b" * 40)


class TestTheGate:
    def test_a_covered_tree_is_satisfied(self, tmp_path):
        root, cfg, _cp = _repo_with_red(tmp_path)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        assert _evaluate(cfg, head).status is GateStatus.SATISFIED

    def test_a_tree_the_red_does_not_cover_is_an_honest_verdict(self, tmp_path):
        """Still `UNSATISFIED`. The fix must not turn a real "no" into an
        instrument error — that would make the gate unfailable."""
        root, cfg, _cp = _repo_with_red(tmp_path)
        base = _git(root, "rev-list", "--max-parents=0", "HEAD").stdout.strip()

        result = _evaluate(cfg, base)

        assert result.status is GateStatus.UNSATISFIED
        assert "different tree" in (result.detail or "")

    def test_a_missing_commit_is_an_instrument_error(self, tmp_path):
        """The defect: this used to read as "the red is on a different tree",
        sending an operator to look at branches instead of fetching."""
        root, cfg, _cp = _repo_with_red(tmp_path, commit_sha="0" * 40)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()

        result = _evaluate(cfg, head)

        assert result.status is GateStatus.INSTRUMENT_ERROR
        assert "different tree" not in (result.detail or "")

    def test_the_detail_carries_gits_own_words(self, tmp_path):
        """An operator needs the cause, not our paraphrase of it."""
        root, cfg, _cp = _repo_with_red(tmp_path, commit_sha="0" * 40)
        head = _git(root, "rev-parse", "HEAD").stdout.strip()

        detail = _evaluate(cfg, head).detail or ""

        assert "0000000" in detail
        assert "Not a valid" in detail or "malformed" in detail or "not a valid" in detail


@pytest.mark.slow
class TestItReachesTheExitCode:
    def test_the_run_reports_infrastructure_not_a_failed_task(self, tmp_path, monkeypatch):
        """End of the wire: an instrument error at the RED site is exit 2 —
        "I cannot tell you whether the work is good" — not exit 1."""
        from spec_runner import tdd
        from spec_runner.execution import _refusal_error_code, _run_red_phase_gate
        from spec_runner.stages import StageReporter
        from spec_runner.state import ErrorCode
        from spec_runner.task import Task

        root, cfg, _cp = _repo_with_red(tmp_path, commit_sha="0" * 40)
        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1h")
        # A checkpoint whose commit is missing is not reusable, so the phase
        # would author a new red — which is a paid call. Stubbed to produce
        # nothing: this test is about what the *gate* then says.
        monkeypatch.setattr(tdd, "_run_agent", lambda *a, **k: tdd.AgentCall(text=""))

        with ExecutorState(cfg) as state:
            reporter = StageReporter(task.id, lambda _line: None)
            refusal = _run_red_phase_gate(task, cfg, state, reporter)

        assert refusal is not None
        assert _refusal_error_code(refusal) is ErrorCode.INFRASTRUCTURE
        assert "could not compare" in str(refusal)
