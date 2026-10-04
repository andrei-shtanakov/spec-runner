"""WIP never feeds a gate; red adoption sees through a WIP chain (spec §4)."""

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.task import Task
from spec_runner.wip import WIP_ATTEMPT_TRAILER, WIP_TRAILER

SEL = "tests/test_x.py::test_x"


def _git(root, *a):
    return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=True).stdout


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@e.c")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "a.py").write_text("1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-qb", "task/task-080-w")
    return tmp_path


def _commit(root, name, msg):
    (root / name).write_text(name)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", msg)


def _wip(root, name, task="TASK-080", attempt=1):
    _commit(
        root, name, f"wip({task}): x\n\n{WIP_TRAILER}: {task}\n{WIP_ATTEMPT_TRAILER}: {attempt}"
    )


def _task():
    return Task(id="TASK-080", name="w", priority="p0", status="todo", estimate="1d")


class _St:
    def checkpoint_exists_for_commit(self, ns, sha):
        return False


def test_red_found_through_a_wip_chain(tmp_path):
    from spec_runner.tdd import _unregistered_red

    root = _repo(tmp_path)
    _commit(root, "t.py", f"TASK-080: red for {SEL}")
    red = _git(root, "rev-parse", "HEAD").strip()
    _wip(root, "w1.py")
    _wip(root, "w2.py", attempt=2)
    cfg = ExecutorConfig(project_root=root, create_git_branch=True)
    assert _unregistered_red(cfg, _St(), _task(), SEL) == red


def test_chain_stops_at_another_commit(tmp_path):
    from spec_runner.tdd import _unregistered_red

    root = _repo(tmp_path)
    _commit(root, "t.py", f"TASK-080: red for {SEL}")
    _commit(root, "o.py", "someone else")
    _wip(root, "w1.py")
    cfg = ExecutorConfig(project_root=root, create_git_branch=True)
    assert _unregistered_red(cfg, _St(), _task(), SEL) == ""


def test_other_tasks_wip_is_not_skipped(tmp_path):
    from spec_runner.tdd import _unregistered_red

    root = _repo(tmp_path)
    _commit(root, "t.py", f"TASK-080: red for {SEL}")
    _wip(root, "w1.py", task="TASK-999")
    cfg = ExecutorConfig(project_root=root, create_git_branch=True)
    assert _unregistered_red(cfg, _St(), _task(), SEL) == ""


def test_candidate_over_wip_is_explicit(tmp_path):
    from spec_runner.hooks import commit_candidate_over_wip

    root = _repo(tmp_path)
    _wip(root, "w1.py")
    cfg = ExecutorConfig(project_root=root, create_git_branch=True, auto_commit=True)
    commit_candidate_over_wip(_task(), cfg)
    assert _git(root, "log", "-1", "--format=%s").strip() == "TASK-080: candidate"


def test_noop_is_cumulative(tmp_path):
    from spec_runner.hooks import task_changed_since_base

    root = _repo(tmp_path)
    cfg = ExecutorConfig(project_root=root, create_git_branch=True)
    _wip(root, "w1.py")
    assert task_changed_since_base(cfg) is True
    (root / "w1.py").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "undo")
    assert task_changed_since_base(cfg) is False


def test_wip_read_failure_fails_closed(tmp_path, monkeypatch):
    import spec_runner.wip as wipmod
    from spec_runner.hooks import post_done_hook
    from spec_runner.phases import Refusal, RefusalKind

    root = _repo(tmp_path)
    _commit(root, "own.py", "TASK-080: own commit")
    cfg = ExecutorConfig(
        project_root=root,
        create_git_branch=True,
        auto_commit=True,
        run_tests_on_done=False,
        run_lint_on_done=False,
        run_review=False,
    )

    def boom(*a, **k):
        raise wipmod.WipReadError("rev-list failed")

    monkeypatch.setattr(wipmod, "wip_commits", boom)
    ok, err, _v, _f, _n = post_done_hook(_task(), cfg, True)
    assert ok is False
    assert isinstance(err, Refusal)
    assert err.kind == RefusalKind.INSTRUMENT


# --- fix round 1 -----------------------------------------------------------

import pytest  # noqa: E402

from tests.test_candidate_commit import _cfg as cc_cfg  # noqa: E402
from tests.test_candidate_commit import _git as cc_git  # noqa: E402
from tests.test_candidate_commit import _recording_gate  # noqa: E402
from tests.test_candidate_commit import _repo as cc_repo  # noqa: E402
from tests.test_candidate_commit import _task as cc_task  # noqa: E402


def _cc_branch_with_wip(tmp_path, wip_file="w1.py"):
    root = cc_repo(tmp_path)
    cc_git(root, "checkout", "-qb", "task/task-001-t")
    (root / wip_file).write_text("w\n")
    cc_git(root, "add", "-A")
    cc_git(
        root,
        "commit",
        "-qm",
        f"wip(TASK-001): x\n\n{WIP_TRAILER}: TASK-001\n{WIP_ATTEMPT_TRAILER}: 1",
    )
    return root


def _subjects(root, rev="--all"):
    return cc_git(root, "log", rev, "--format=%s").stdout.splitlines()


def _is_wip(root, sha):
    return WIP_TRAILER in cc_git(root, "log", "-1", "--format=%B", sha).stdout


@pytest.mark.slow
class TestPostDoneHookOverWip:
    def test_gate_judges_a_candidate_not_the_wip(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _cc_branch_with_wip(tmp_path)
        cfg = cc_cfg(root, create_git_branch=True)
        seen = _recording_gate(monkeypatch)
        ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)
        assert ok is True, err
        assert seen and not _is_wip(root, seen[-1])
        assert cc_git(root, "log", "-1", "--format=%s", seen[-1]).stdout.strip() == (
            "TASK-001: candidate"
        )
        assert _subjects(root).count("TASK-001: candidate") == 1

    def test_review_checkpoint_never_names_wip(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _cc_branch_with_wip(tmp_path)
        cfg = cc_cfg(root, create_git_branch=True, run_review=True)
        cfg.review_policy = "advisory"
        seen = _recording_gate(monkeypatch)
        from spec_runner.state import ReviewVerdict

        monkeypatch.setattr(
            hooks,
            "run_code_review",
            lambda *a, **k: (ReviewVerdict.PASSED, None, "REVIEW_PASSED"),
        )
        reviewed: list[str] = []
        real_head = hooks._head_sha

        def _spy(config):
            sha = real_head(config)
            reviewed.append(sha)
            return sha

        monkeypatch.setattr(hooks, "_head_sha", _spy)
        hooks.post_done_hook(cc_task(), cfg, True)
        assert seen, "the gate never ran"
        assert not any(_is_wip(root, sha) for sha in seen)
        assert reviewed and not any(_is_wip(root, sha) for sha in reviewed[-2:])

    def test_no_candidate_without_auto_commit(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _cc_branch_with_wip(tmp_path)
        cfg = cc_cfg(root, create_git_branch=True, auto_commit=False)
        _recording_gate(monkeypatch)
        hooks.post_done_hook(cc_task(), cfg, True)
        assert "TASK-001: candidate" not in _subjects(root)

    def test_a_retry_makes_no_second_candidate(self, tmp_path, monkeypatch):
        from spec_runner import hooks
        from spec_runner.gates import GateStatus

        root = _cc_branch_with_wip(tmp_path)
        cfg = cc_cfg(root, create_git_branch=True)
        _recording_gate(monkeypatch, GateStatus.UNSATISFIED)
        hooks.post_done_hook(cc_task(), cfg, True)
        hooks.post_done_hook(cc_task(), cfg, True)
        assert _subjects(root).count("TASK-001: candidate") == 1

    def test_a_failed_candidate_commit_is_an_instrument_refusal(self, tmp_path, monkeypatch):
        from spec_runner import hooks
        from spec_runner.phases import Refusal, RefusalKind

        root = _cc_branch_with_wip(tmp_path)
        hook = root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        cfg = cc_cfg(root, create_git_branch=True)
        seen = _recording_gate(monkeypatch)
        ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)
        assert ok is False
        assert isinstance(err, Refusal) and err.kind == RefusalKind.INSTRUMENT
        assert not seen, "no gate may run when the candidate could not be made"

    def test_uncomputable_merge_base_fails_closed(self, tmp_path, monkeypatch):
        from spec_runner import git_ops, hooks
        from spec_runner.phases import Refusal, RefusalKind

        root = _cc_branch_with_wip(tmp_path)
        cfg = cc_cfg(root, create_git_branch=True)
        monkeypatch.setattr(git_ops, "get_main_branch", lambda c: "no-such-branch")
        ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)
        assert ok is False
        assert isinstance(err, Refusal) and err.kind == RefusalKind.INSTRUMENT

    def test_noop_when_wip_is_undone_in_the_tree(self, tmp_path, monkeypatch):
        from spec_runner import hooks

        root = _cc_branch_with_wip(tmp_path)
        (root / "w1.py").unlink()
        cfg = cc_cfg(root, create_git_branch=True)
        ok, err, _v, _f, no_op = hooks.post_done_hook(cc_task(), cfg, True)
        assert ok is True, err
        assert no_op is True

    def test_not_noop_when_wip_has_content(self, tmp_path):
        from spec_runner import hooks

        root = _cc_branch_with_wip(tmp_path)
        cfg = cc_cfg(root, create_git_branch=True)
        ok, err, _v, _f, no_op = hooks.post_done_hook(cc_task(), cfg, True)
        assert ok is True, err
        assert no_op is False


@pytest.mark.slow
def test_tracked_tasks_md_is_not_the_tasks_work(tmp_path):
    from spec_runner.hooks import task_changed_since_base

    root = _cc_branch_with_wip(tmp_path)
    cfg = cc_cfg(root, create_git_branch=True)
    tasks = root / "spec" / "tasks.md"
    tasks.write_text(tasks.read_text() + "\nDONE flip\n")
    cc_git(root, "add", "-A")
    cc_git(root, "commit", "-qm", "status flip")
    assert task_changed_since_base(cfg) is True  # w1.py still there
    (root / "w1.py").unlink()
    cc_git(root, "add", "-A")
    cc_git(root, "commit", "-qm", "undo")
    assert task_changed_since_base(cfg) is False  # only tasks.md differs


def test_unreadable_diff_is_not_unchanged(tmp_path, monkeypatch):
    from spec_runner import review
    from spec_runner.hooks import task_changed_since_base
    from spec_runner.wip import WipReadError

    root = _repo(tmp_path)
    cfg = ExecutorConfig(project_root=root, create_git_branch=True)
    monkeypatch.setattr(review, "task_base", lambda c: "no-such-ref")
    with pytest.raises(WipReadError):
        task_changed_since_base(cfg)


def test_walk_reaching_a_root_commit_adopts_nothing(tmp_path):
    from spec_runner.tdd import _unregistered_red

    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@e.c")
    _git(tmp_path, "config", "user.name", "T")
    _wip(tmp_path, "w1.py")
    cfg = ExecutorConfig(project_root=tmp_path, create_git_branch=True)
    assert _unregistered_red(cfg, _St(), _task(), SEL) == ""


# --- a red below WIP is adopted only if it needs no repair ------------------


def _scenario(tmp_path, body):
    import shlex
    import sys

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@e.c")
    _git(root, "config", "user.name", "T")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "check.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "sys.exit(1 if any('AGENTWORD' in Path(p).read_text() for p in sys.argv[1:]) else 0)\n"
    )
    (scripts / "fix.py").write_text(
        "import sys\nfrom pathlib import Path\n"
        "for p in sys.argv[1:]:\n"
        "    f = Path(p)\n    f.write_text(f.read_text().replace('AGENTWORD', ''))\n"
    )
    q = shlex.quote
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        execution_mode="tdd",
        test_command="python -m pytest",
        lint_command=f"{q(sys.executable)} {q(str(scripts / 'check.py'))}",
        lint_command_declared=True,
        lint_fix_command=f"{q(sys.executable)} {q(str(scripts / 'fix.py'))}",
        lint_fix_command_declared=True,
        create_git_branch=True,
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    from spec_runner.tdd import resolve_namespace
    from spec_runner.tdd_runners import ADAPTERS

    evid = str(ADAPTERS["pytest"].evidential_file("TASK-080", namespace=resolve_namespace(cfg)))
    red = root / evid
    red.parent.mkdir(parents=True, exist_ok=True)
    red.write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", f"TASK-080: red for {evid}::test_thing")
    red_sha = _git(root, "rev-parse", "HEAD").strip()
    _wip(root, "w1.py")
    return root, cfg, red_sha, evid


@pytest.mark.slow
class TestRedBelowWip:
    def test_a_repair_that_would_rewrite_the_red_is_refused(self, tmp_path):
        from spec_runner.state import ExecutorState
        from spec_runner.tdd import RedOutcome, run_red_phase

        root, cfg, red_sha, evid = _scenario(
            tmp_path, "def test_thing():  # AGENTWORD\n    assert False\n"
        )
        wip_sha = _git(root, "rev-parse", "HEAD").strip()
        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert result.checkpoint is None
        assert red_sha[:12] in result.detail and wip_sha[:12] in result.detail
        assert _git(root, "rev-parse", "HEAD").strip() == wip_sha
        body = _git(root, "log", "-1", "--format=%B")
        assert f"{WIP_TRAILER}: TASK-080" in body and f"{WIP_ATTEMPT_TRAILER}: 1" in body
        assert (root / evid).read_text().count("AGENTWORD") == 1
        assert _git(root, "status", "--porcelain", "--", evid).strip() == ""

    def test_a_clean_red_is_adopted_at_its_own_sha(self, tmp_path):
        from spec_runner.state import ExecutorState
        from spec_runner.tdd import RedOutcome, run_red_phase

        root, cfg, red_sha, _ = _scenario(tmp_path, "def test_thing():\n    assert False\n")
        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
        assert result.checkpoint is not None
        assert result.checkpoint.commit_sha == red_sha


def _spy_agent(monkeypatch, reply=""):
    from spec_runner import tdd

    calls: list[str] = []

    def agent(config, prompt, **kw):
        calls.append(prompt)
        return tdd.AgentCall(text=reply)

    monkeypatch.setattr(tdd, "_run_agent", agent)
    return calls


@pytest.mark.slow
class TestBelowWipIsCheckOnly:
    def test_no_paid_round_when_findings_survive(self, tmp_path, monkeypatch):
        """The fix cures nothing; below WIP the agent round must not run."""
        import shlex
        import sys

        from spec_runner.state import ExecutorState
        from spec_runner.tdd import RedOutcome, run_red_phase

        root, cfg, red_sha, evid = _scenario(
            tmp_path, "def test_thing():  # AGENTWORD\n    assert False\n"
        )
        noop = tmp_path / "scripts" / "noop.py"
        noop.write_text("import sys\nsys.exit(0)\n")
        cfg.lint_fix_command = f"{shlex.quote(sys.executable)} {shlex.quote(str(noop))}"
        calls = _spy_agent(monkeypatch)
        wip_sha = _git(root, "rev-parse", "HEAD").strip()
        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
        assert calls == [], "a paid call was made below WIP"
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert red_sha[:12] in result.detail and wip_sha[:12] in result.detail
        assert "git reset --hard" in result.detail
        assert _git(root, "rev-parse", "HEAD").strip() == wip_sha

    def test_post_authoring_path_refuses_too(self, tmp_path, monkeypatch):
        """No declared fix: adoption goes through _unregistered_red."""
        from spec_runner.state import ExecutorState
        from spec_runner.tdd import RedOutcome, run_red_phase

        root, cfg, red_sha, evid = _scenario(
            tmp_path, "def test_thing():  # AGENTWORD\n    assert False\n"
        )
        cfg.lint_fix_command_declared = False
        calls = _spy_agent(monkeypatch, reply=f"TDD_SELECTOR: {evid}::test_thing")
        wip_sha = _git(root, "rev-parse", "HEAD").strip()
        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
        assert len(calls) == 1, "only the authoring call"
        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert result.checkpoint is None
        assert red_sha[:12] in result.detail and wip_sha[:12] in result.detail
        assert _git(root, "rev-parse", "HEAD").strip() == wip_sha


@pytest.mark.slow
def test_failed_candidate_stage_commit_over_wip_is_refused(tmp_path, monkeypatch):
    from spec_runner import hooks
    from spec_runner.phases import Refusal, RefusalKind

    root = _cc_branch_with_wip(tmp_path)
    cfg = cc_cfg(root, create_git_branch=True)
    seen = _recording_gate(monkeypatch)
    monkeypatch.setattr(hooks, "commit_task_work", lambda t, c: "failed")
    ok, err, *_ = hooks.post_done_hook(cc_task(), cfg, True)
    assert ok is False
    assert isinstance(err, Refusal) and err.kind == RefusalKind.INSTRUMENT
    assert not seen
