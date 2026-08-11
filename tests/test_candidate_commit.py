"""F-1: the pre-terminal gate must judge a commit containing the work.

The battle test on published v2.25.0 found a task rewriting its own claimed
test and reaching DONE. Both gate evaluations judged the *red* commit; the
mutation landed in the task commit created **after** the gate.

The cause was my own fix in #170. Moving the gate before the DONE write was
right — a blocked task must not be labelled done first — but it also moved the
gate before the commit that contains the work. The `#103` pre-review commit
happened to cover the review-on path, so the byte-lock held exactly when an
unrelated feature was enabled.

Target order (owner, 2026-08-11):

    agent → deterministic checks → candidate commit (no DONE in it)
    → pre-terminal gates against the candidate SHA
       ├─ blocked   → resumable, no merge, no DONE
       └─ satisfied → bookkeeping/status commit → merge → DONE

Report: `docs/superpowers/specs/2026-08-11-tdd-battle-report.md`, F-1.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.gates import GateRegistry, GateResult, GateStatus
from spec_runner.state import PhaseOutcome
from spec_runner.task import Task

pytestmark = pytest.mark.slow


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "spec").mkdir(parents=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("x\n")
    (root / "spec" / "tasks.md").write_text(
        "# Tasks\n\n### TASK-001: t\n🟠 P1 | 🔄 IN_PROGRESS\nEst: 1d\n\n- [ ] x\n"
    )
    # `pre_start_hook` writes this in a real run (#62). Without it the runtime
    # state DB is staged into the task commit, which both puts runtime state in
    # git and breaks no-op detection — an artefact of the fixture, not of the
    # code under test.
    (root / "spec" / ".gitignore").write_text(".executor-*\n.*task-history.log\n.*spec.lock\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / "spec" / ".executor-state.db",
        "logs_dir": root / "spec" / ".logs",
        "auto_commit": True,
        "create_git_branch": False,
        "run_tests_on_done": False,
        "run_lint_on_done": False,
        "run_review": False,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task() -> Task:
    return Task(id="TASK-001", name="t", priority="p1", status="review", estimate="1h")


def _recording_gate(monkeypatch, status=GateStatus.SATISFIED):
    """A gate that records the SHA it was asked about."""
    from spec_runner import gates as gates_mod

    seen: list[str] = []
    registry = GateRegistry()

    def _evaluate(ctx):
        seen.append(ctx.checkpoint_sha)
        outcome = PhaseOutcome.PASS if status is GateStatus.SATISFIED else PhaseOutcome.NOT_RUN
        return GateResult(status, outcome, "battle gate")

    registry.register("probe", "review", _evaluate)
    monkeypatch.setattr(gates_mod, "REGISTRY", registry)
    return seen


class TestTheGateJudgesTheWork:
    """The heart of F-1."""

    def test_the_gate_sees_the_commit_containing_the_green_work(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _repo(tmp_path)
        cfg = _cfg(root)
        seen = _recording_gate(monkeypatch)
        # The agent's work, uncommitted — exactly the state post_done_hook meets.
        (root / "widget.py").write_text("def make():\n    return 'thing'\n")

        hooks.post_done_hook(_task(), cfg, True)

        assert seen, "the gate never ran"
        judged = seen[-1]
        tree = _git(root, "ls-tree", "-r", "--name-only", judged).stdout.split()
        assert "widget.py" in tree, (
            "the gate judged a tree without the work — the byte-lock and every "
            "other policy would be deciding about the wrong commit"
        )

    def test_it_works_with_review_off(self, tmp_path, monkeypatch):
        """The defect's exact shape: correct with review on, silently wrong
        with review off."""
        from spec_runner import hooks

        root = _repo(tmp_path)
        cfg = _cfg(root, run_review=False)
        seen = _recording_gate(monkeypatch)
        (root / "widget.py").write_text("x = 1\n")

        hooks.post_done_hook(_task(), cfg, True)
        tree = _git(root, "ls-tree", "-r", "--name-only", seen[-1]).stdout.split()
        assert "widget.py" in tree

    def test_done_is_not_inside_the_judged_candidate(self, tmp_path, monkeypatch):
        """A tree that already claims the task is done is a circular thing to
        judge when doneness is what is being decided."""
        from spec_runner import hooks

        root = _repo(tmp_path)
        cfg = _cfg(root)
        seen = _recording_gate(monkeypatch)
        (root / "widget.py").write_text("x = 1\n")

        hooks.post_done_hook(_task(), cfg, True)
        tasks_md = _git(root, "show", f"{seen[-1]}:spec/tasks.md").stdout
        assert "DONE" not in tasks_md


class TestABlockedTaskGoesNowhere:
    def test_it_is_not_marked_done(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _repo(tmp_path)
        cfg = _cfg(root)
        _recording_gate(monkeypatch, GateStatus.UNSATISFIED)
        (root / "widget.py").write_text("x = 1\n")

        success, error, *_ = hooks.post_done_hook(_task(), cfg, True)

        assert success is False
        assert "DONE" not in (root / "spec" / "tasks.md").read_text()

    def test_the_work_is_still_committed_so_the_task_is_resumable(self, tmp_path, monkeypatch):
        """Blocked is not "lose the work": the candidate commit stands, which
        is what makes the refusal resumable and the evidence replayable."""
        from spec_runner import hooks

        root = _repo(tmp_path)
        cfg = _cfg(root)
        _recording_gate(monkeypatch, GateStatus.UNSATISFIED)
        (root / "widget.py").write_text("x = 1\n")

        hooks.post_done_hook(_task(), cfg, True)

        dirty = [
            line for line in _git(root, "status", "--porcelain").stdout.splitlines() if line.strip()
        ]
        assert dirty == [], f"the work should be committed, not left dirty: {dirty}"
        assert "widget.py" in _git(root, "ls-tree", "-r", "--name-only", "HEAD").stdout


class TestNoOpHasACandidate:
    def test_a_task_that_changed_nothing_still_gives_the_gate_a_sha(self, tmp_path, monkeypatch):
        """Otherwise the gate is asked about nothing in particular."""
        from spec_runner import hooks

        root = _repo(tmp_path)
        cfg = _cfg(root)
        seen = _recording_gate(monkeypatch)

        base = _git(root, "rev-parse", "HEAD").stdout.strip()
        success, _error, _verdict, _findings, no_op = hooks.post_done_hook(_task(), cfg, True)

        assert success is True
        assert no_op is True
        # The candidate is the tree as it stood: a real commit, and the one the
        # bookkeeping commit was later built on. (Not HEAD *after* the run —
        # that is the bookkeeping commit, which the gate never saw.)
        assert seen and seen[-1] == base
        _git(root, "merge-base", "--is-ancestor", seen[-1], "HEAD")


class TestAnExternalCommitBetweenGateAndMergeIsCaught:
    """The verdict is about a tree. If the tree moves under us before the
    merge, the verdict no longer describes what would be merged."""

    def test_a_foreign_commit_after_the_gate_refuses_the_merge(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _repo(tmp_path)
        cfg = _cfg(root, create_git_branch=True)
        _recording_gate(monkeypatch)
        (root / "widget.py").write_text("x = 1\n")

        real_gates = hooks._run_pre_terminal_gates

        def _gate_then_interfere(*args, **kwargs):
            verdict = real_gates(*args, **kwargs)
            # Someone else lands a commit after the verdict and before the
            # merge — the injection point that matters, since anything earlier
            # is simply part of what the gate judged.
            (root / "intruder.py").write_text("y = 2\n")
            _git(root, "add", "-A")
            _git(root, "commit", "-qm", "another process")
            return verdict

        monkeypatch.setattr(hooks, "_run_pre_terminal_gates", _gate_then_interfere)
        success, error, *_ = hooks.post_done_hook(_task(), cfg, True)

        assert success is False
        assert "moved" in (error or "").lower() or "changed" in (error or "").lower()


class TestReviewFixesProduceANewCandidate:
    def test_the_gate_judges_the_tree_after_the_fixes(self, tmp_path, monkeypatch):
        """A verdict on the pre-fix tree says nothing about what merges."""
        from spec_runner import hooks
        from spec_runner.state import ReviewVerdict

        root = _repo(tmp_path)
        cfg = _cfg(root, run_review=True)
        seen = _recording_gate(monkeypatch)
        (root / "widget.py").write_text("x = 1\n")

        def _review_that_fixes(task, config, **kwargs):
            (Path(config.project_root) / "fixed.py").write_text("z = 3\n")
            _git(Path(config.project_root), "add", "-A")
            _git(Path(config.project_root), "commit", "-qm", f"{task.id}: code review fixes")
            return ReviewVerdict.FIXED, None, "REVIEW_FIXED"

        monkeypatch.setattr(hooks, "run_code_review", _review_that_fixes)
        hooks.post_done_hook(_task(), cfg, True)

        tree = _git(root, "ls-tree", "-r", "--name-only", seen[-1]).stdout.split()
        assert "fixed.py" in tree, "the gate judged the tree from before the review fixes"

    def test_a_diverged_branch_is_caught_even_though_git_log_succeeds(self, tmp_path):
        """Copilot's finding: `git log A..B` exits 0 when A is not an ancestor
        of B — it just lists what B has — so ancestry has to be asked
        explicitly or a rewritten branch passes as "no foreign commits"."""
        from spec_runner import hooks

        root = _repo(tmp_path)
        cfg = _cfg(root)
        gated = _git(root, "rev-parse", "HEAD").stdout.strip()

        # Move the branch somewhere that does not descend from `gated`.
        _git(root, "checkout", "-q", "--orphan", "elsewhere")
        (root / "other.py").write_text("y = 2\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "TASK-001: looks like ours")

        drift = hooks._detect_candidate_drift(cfg, gated, "TASK-001")
        assert drift is not None, "a branch that does not descend from the verdict must refuse"
        assert "descends" in drift

    def test_our_own_bookkeeping_commit_is_not_drift(self, tmp_path):
        from spec_runner import hooks

        root = _repo(tmp_path)
        cfg = _cfg(root)
        gated = _git(root, "rev-parse", "HEAD").stdout.strip()
        (root / "note.txt").write_text("bookkeeping\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "TASK-001: t")

        assert hooks._detect_candidate_drift(cfg, gated, "TASK-001") is None
