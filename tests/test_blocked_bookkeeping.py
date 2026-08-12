"""#192 (F-8): a gate-blocked task commits its own status flip.

The deadlock this closes: review starts, `tasks.md` flips to `🔍 REVIEW`
(uncommitted, by design — it is written *before* the commit that would carry
it), a pre-terminal gate says no, and the run stops resumably. The next run
then meets the dirty-spec guard and refuses, because `tasks.md` is dirty. Each
piece is right on its own; together they are a recovery deadlock in the new
lifecycle, and the operator's only exits were `--allow-dirty-spec` (which
disarms the guard for *real* spec edits too) or committing a status flip they
did not make.

The fix is a bookkeeping commit on the blocked path: candidate commit → gate
UNSATISFIED → a commit carrying **only** the status flip → resumable stop.

Two invariants the owner asked to hold explicitly, and which most of this file
is about:

1. **The bookkeeping commit is not the new candidate SHA.** The gate judged the
   candidate; a later evaluation happens against a different tree and must be a
   *fresh* evaluation, never the old verdict reapplied to changed code.
2. **Resuming does not grow a chain of identical REVIEW commits.** The second
   pass writes the same status, so there is nothing to commit and nothing is
   committed.

Contract: issue #192.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.bookkeeping import (
    StatusFlip,
    commit_status_flip,
    status_only_transition,
)
from spec_runner.config import ExecutorConfig

TASKS = """\
# Tasks

### TASK-001: first
🟠 P1 | 🔄 IN_PROGRESS
Est: 1d

- [ ] a
- [ ] b

### TASK-002: second
🟢 P2 | ⬜ TODO
Est: 1d

- [ ] c
"""


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "spec").mkdir()
    (root / "spec" / "tasks.md").write_text(TASKS)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "spec")
    return root


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "auto_commit": True,
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _flip(root: Path, task_id: str = "TASK-001", status: str = "review") -> None:
    from spec_runner.task import update_task_status

    assert update_task_status(root / "spec" / "tasks.md", task_id, status)


def _subjects(root: Path) -> list[str]:
    out = _git(root, "log", "--format=%s")
    return out.stdout.strip().splitlines()


class TestTheProofIsStatusOnly:
    """ "Only a proven status-only transition" — the proof is bound to the task
    id, the previous status and the new one. Anything else in the file is a
    content change and must not ride along in a bookkeeping commit."""

    def test_a_status_flip_is_recognised(self):
        after = TASKS.replace("🟠 P1 | 🔄 IN_PROGRESS", "🟠 P1 | 🔍 REVIEW")
        flip = status_only_transition(TASKS, after, "TASK-001")
        assert flip == StatusFlip(task_id="TASK-001", previous="in_progress", new="review")

    def test_an_unchanged_file_is_not_a_transition(self):
        assert status_only_transition(TASKS, TASKS, "TASK-001") is None

    def test_another_task_flipping_too_is_refused(self):
        after = TASKS.replace("🟠 P1 | 🔄 IN_PROGRESS", "🟠 P1 | 🔍 REVIEW").replace(
            "🟢 P2 | ⬜ TODO", "🟢 P2 | 🔄 IN_PROGRESS"
        )
        assert status_only_transition(TASKS, after, "TASK-001") is None

    def test_a_checklist_edit_alongside_the_flip_is_refused(self):
        after = TASKS.replace("🟠 P1 | 🔄 IN_PROGRESS", "🟠 P1 | 🔍 REVIEW").replace(
            "- [ ] a", "- [x] a"
        )
        assert status_only_transition(TASKS, after, "TASK-001") is None

    def test_a_description_edit_alongside_the_flip_is_refused(self):
        after = TASKS.replace("🟠 P1 | 🔄 IN_PROGRESS", "🟠 P1 | 🔍 REVIEW").replace(
            "### TASK-001: first", "### TASK-001: first, renamed"
        )
        assert status_only_transition(TASKS, after, "TASK-001") is None

    def test_a_new_task_alongside_the_flip_is_refused(self):
        after = (
            TASKS.replace("🟠 P1 | 🔄 IN_PROGRESS", "🟠 P1 | 🔍 REVIEW")
            + "\n### TASK-003: third\n🟢 P2 | ⬜ TODO\nEst: 1d\n"
        )
        assert status_only_transition(TASKS, after, "TASK-001") is None

    def test_a_flip_of_the_wrong_task_is_refused(self):
        """The proof names the task the gate judged. Another task's status is
        somebody else's business."""
        after = TASKS.replace("🟢 P2 | ⬜ TODO", "🟢 P2 | 🔄 IN_PROGRESS")
        assert status_only_transition(TASKS, after, "TASK-001") is None

    def test_prose_between_tasks_is_refused(self):
        """Not everything in tasks.md is a parsed field — a line the parser
        ignores is still a spec change."""
        after = TASKS.replace("🟠 P1 | 🔄 IN_PROGRESS", "🟠 P1 | 🔍 REVIEW").replace(
            "# Tasks\n", "# Tasks\n\nSome new note.\n"
        )
        assert status_only_transition(TASKS, after, "TASK-001") is None


class TestTheProofIsPositional:
    """Raised in review of this PR: neutralising the status by replacing the
    word wherever it appears made the proof depend on what else the line says.
    It is now blanked at the span the pattern matched."""

    def test_a_note_containing_a_status_word_does_not_confuse_it(self):
        before = TASKS.replace("🟠 P1 | 🔄 IN_PROGRESS", "🟠 P1 | 🔄 IN_PROGRESS | see TODO below")
        after = before.replace("🔄 IN_PROGRESS |", "🔍 REVIEW |")
        flip = status_only_transition(before, after, "TASK-001")
        assert flip == StatusFlip(task_id="TASK-001", previous="in_progress", new="review")

    def test_a_note_changing_with_the_status_is_still_refused(self):
        before = TASKS.replace("🟠 P1 | 🔄 IN_PROGRESS", "🟠 P1 | 🔄 IN_PROGRESS | see TODO below")
        after = before.replace("🔄 IN_PROGRESS | see TODO below", "🔍 REVIEW | see DONE below")
        assert status_only_transition(before, after, "TASK-001") is None

    def test_a_priority_changing_with_the_status_is_refused(self):
        after = TASKS.replace("🟠 P1 | 🔄 IN_PROGRESS", "🔴 P0 | 🔍 REVIEW")
        assert status_only_transition(TASKS, after, "TASK-001") is None

    def test_the_plain_no_emoji_form_works_too(self):
        before = TASKS.replace("🟠 P1 | 🔄 IN_PROGRESS", "P1 | IN_PROGRESS")
        after = before.replace("P1 | IN_PROGRESS", "P1 | REVIEW")
        flip = status_only_transition(before, after, "TASK-001")
        assert flip == StatusFlip(task_id="TASK-001", previous="in_progress", new="review")

    def test_the_bullet_prefixed_form_works_too(self):
        """#123: agents editing tasks.md mid-run introduce a bullet prefix, and
        this path is reached precisely after an agent has been editing."""
        before = TASKS.replace("🟠 P1 | 🔄 IN_PROGRESS", "- 🟠 P1 | 🔄 IN_PROGRESS")
        after = before.replace("🔄 IN_PROGRESS", "🔍 REVIEW")
        flip = status_only_transition(before, after, "TASK-001")
        assert flip == StatusFlip(task_id="TASK-001", previous="in_progress", new="review")

    def test_it_agrees_with_the_parser_on_every_status(self):
        """The positional pattern is a second copy of `TASK_META`'s shape.
        Drift between them must not silently weaken the proof — the code
        refuses when they disagree, and this is the guard that they don't."""
        from spec_runner.bookkeeping import _split_meta
        from spec_runner.task import STATUS_EMOJI, TASK_META

        for status, emoji in STATUS_EMOJI.items():
            for line in (
                f"🟠 P1 | {emoji} {status.upper()}",
                f"P1 | {status.upper()}",
                f"- 🟠 P1 | {emoji} {status.upper()} | note",
            ):
                assert TASK_META.match(line), line
                parts = _split_meta(line)
                assert parts is not None and parts[0] == status, line


class TestTheCommit:
    def test_it_commits_the_flip_and_leaves_the_spec_clean(self, tmp_path):
        """Asserted through `spec_dirty_paths` — the function the next run's
        guard actually calls — rather than through a clean `git status`, which
        would also be measuring runtime files the guard never looks at."""
        from spec_runner.git_ops import spec_dirty_paths

        root = _repo(tmp_path)
        cfg = _cfg(root)
        _flip(root)
        assert spec_dirty_paths(cfg), "precondition: the flip is dirt until committed"
        assert commit_status_flip(cfg, "TASK-001", candidate_sha="abc1234", reason="gate") is None
        assert spec_dirty_paths(cfg) == []

    def test_the_commit_records_what_it_is_and_what_judged_it(self, tmp_path):
        """Audit: the candidate SHA the gate judged and the verdict that
        blocked, in the commit that records the consequence."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _flip(root)
        commit_status_flip(cfg, "TASK-001", candidate_sha="abc1234def", reason="review: failed")
        body = _git(root, "log", "-1", "--format=%B").stdout
        assert "TASK-001" in body
        assert "abc1234def" in body
        assert "review: failed" in body

    def test_only_tasks_md_is_in_the_commit(self, tmp_path):
        """A bookkeeping commit that swept up a stray file would be exactly the
        provenance muddle the dirty-spec guard exists to prevent."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _flip(root)
        (root / "stray.py").write_text("x = 1\n")
        commit_status_flip(cfg, "TASK-001", candidate_sha="abc", reason="gate")
        files = _git(root, "show", "--name-only", "--format=", "HEAD").stdout.split()
        assert files == ["spec/tasks.md"]
        assert "stray.py" in _git(root, "status", "--porcelain").stdout

    def test_nothing_to_commit_is_not_an_error(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        assert commit_status_flip(cfg, "TASK-001", candidate_sha="abc", reason="gate") is None
        assert _subjects(root) == ["spec"]

    def test_a_content_change_is_refused_and_says_so(self, tmp_path):
        """Acceptance: a spec content change made alongside the status flip
        still blocks. It is left dirty on purpose — the next run's guard is
        then doing its job, not deadlocking."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _flip(root)
        tasks = root / "spec" / "tasks.md"
        tasks.write_text(tasks.read_text().replace("- [ ] a", "- [x] a"))
        problem = commit_status_flip(cfg, "TASK-001", candidate_sha="abc", reason="gate")
        assert problem and "status" in problem.lower()
        assert _git(root, "status", "--porcelain").stdout.strip() != ""

    def test_an_untracked_tasks_file_is_left_alone(self, tmp_path):
        """Orchestrators that keep generated specs untracked (Maestro) never
        meet the guard, so there is nothing here to fix for them."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _git(root, "rm", "-q", "--cached", "spec/tasks.md")
        _git(root, "commit", "-qm", "untrack")
        _flip(root)
        before = _subjects(root)
        assert commit_status_flip(cfg, "TASK-001", candidate_sha="abc", reason="gate") is None
        assert _subjects(root) == before


@pytest.mark.slow
class TestARealBlockedRun:
    """The deadlock is made of three components — the REVIEW flip, the gate,
    the guard — so it can only be shown with all three present."""

    def _run(self, tmp_path, monkeypatch, *, status=None, gate_status=None):
        from spec_runner import gates as gates_mod
        from spec_runner import hooks
        from spec_runner.gates import GateRegistry, GateResult, GateStatus
        from spec_runner.review import ReviewVerdict
        from spec_runner.state import PhaseOutcome
        from spec_runner.task import Task

        root = _repo(tmp_path)
        (root / "spec" / ".gitignore").write_text(".executor-*\n.*task-history.log\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "runtime gitignore")
        cfg = _cfg(
            root,
            state_file=root / "spec" / ".executor-state.db",
            logs_dir=root / "spec" / ".logs",
            create_git_branch=False,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=True,
        )

        seen: list[str] = []
        registry = GateRegistry()
        if gate_status is not None:

            def _evaluate(ctx):
                seen.append(ctx.checkpoint_sha)
                outcome = (
                    PhaseOutcome.PASS
                    if gate_status is GateStatus.SATISFIED
                    else PhaseOutcome.UNEXPECTED_FAIL
                )
                return GateResult(gate_status, outcome, "blocked in a test")

            registry.register("probe", "review", _evaluate)
        monkeypatch.setattr(gates_mod, "REGISTRY", registry)
        monkeypatch.setattr(
            hooks, "run_code_review", lambda *a, **k: (ReviewVerdict.PASSED, None, "ok")
        )

        task = Task(id="TASK-001", name="first", priority="p1", status="in_progress", estimate="1d")
        (root / "widget.py").write_text("x = 1\n")
        result = hooks.post_done_hook(task, cfg, True)
        return root, cfg, result, seen

    def test_the_blocked_task_leaves_the_spec_committed(self, tmp_path, monkeypatch):
        from spec_runner.gates import GateStatus
        from spec_runner.git_ops import spec_dirty_paths

        root, cfg, (ok, error, *_), _ = self._run(
            tmp_path, monkeypatch, gate_status=GateStatus.UNSATISFIED
        )
        assert ok is False and "unsatisfied" in (error or "").lower()
        assert spec_dirty_paths(cfg) == [], "the deadlock: a dirty spec after a blocked stop"
        committed = _git(root, "show", "HEAD:spec/tasks.md").stdout
        assert "🔍 REVIEW" in committed

    def test_the_next_run_starts_without_an_override(self, tmp_path, monkeypatch):
        """Acceptance, in the words it was written in — asserted against the
        guard itself, not a proxy for it."""
        from argparse import Namespace

        from spec_runner.cli import _enforce_clean_spec
        from spec_runner.gates import GateStatus

        _root, cfg, _result, _seen = self._run(
            tmp_path, monkeypatch, gate_status=GateStatus.UNSATISFIED
        )
        _enforce_clean_spec(Namespace(allow_dirty_spec=False), cfg)  # must not SystemExit

    def test_the_blocked_task_is_still_review_and_not_done(self, tmp_path, monkeypatch):
        from spec_runner.gates import GateStatus
        from spec_runner.task import get_task_by_id, parse_tasks

        root, _cfg_, _result, _seen = self._run(
            tmp_path, monkeypatch, gate_status=GateStatus.UNSATISFIED
        )
        task = get_task_by_id(parse_tasks(root / "spec" / "tasks.md"), "TASK-001")
        assert task is not None and task.status == "review"

    def test_the_bookkeeping_commit_carries_only_the_status(self, tmp_path, monkeypatch):
        from spec_runner.gates import GateStatus

        root, _cfg_, _result, _seen = self._run(
            tmp_path, monkeypatch, gate_status=GateStatus.UNSATISFIED
        )
        files = _git(root, "show", "--name-only", "--format=", "HEAD").stdout.split()
        assert files == ["spec/tasks.md"]

    def test_the_gate_judged_the_candidate_not_the_bookkeeping_commit(self, tmp_path, monkeypatch):
        """Invariant 1. The candidate is what carries the work; the
        bookkeeping commit sits on top of it and must not be mistaken for it."""
        from spec_runner.gates import GateStatus

        root, _cfg_, _result, seen = self._run(
            tmp_path, monkeypatch, gate_status=GateStatus.UNSATISFIED
        )
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        assert seen and seen[-1] != head
        judged = _git(root, "ls-tree", "-r", "--name-only", seen[-1]).stdout.split()
        assert "widget.py" in judged, "the gate must judge the tree containing the work"

    def test_a_second_pass_is_judged_afresh(self, tmp_path, monkeypatch):
        """Invariant 1's point: the old verdict must not become applicable to a
        tree that has moved. It cannot, because the next evaluation asks about
        a different SHA — and a different SHA is a different verdict key."""
        from spec_runner import hooks
        from spec_runner.gates import GateStatus
        from spec_runner.task import Task

        root, cfg, _result, seen = self._run(
            tmp_path, monkeypatch, gate_status=GateStatus.UNSATISFIED
        )
        task = Task(id="TASK-001", name="first", priority="p1", status="review", estimate="1d")
        hooks.post_done_hook(task, cfg, True)
        assert len(seen) >= 2 and seen[0] != seen[-1]

    def test_resuming_does_not_grow_a_chain_of_commits(self, tmp_path, monkeypatch):
        """Invariant 2, end to end."""
        from spec_runner import hooks
        from spec_runner.gates import GateStatus
        from spec_runner.task import Task

        root, cfg, _result, _seen = self._run(
            tmp_path, monkeypatch, gate_status=GateStatus.UNSATISFIED
        )
        for _ in range(2):
            task = Task(id="TASK-001", name="first", priority="p1", status="review", estimate="1d")
            hooks.post_done_hook(task, cfg, True)
        flips = [s for s in _subjects(root) if "status" in s and "TASK-001" in s]
        assert len(flips) == 1, _subjects(root)

    def test_no_gate_means_no_extra_commit(self, tmp_path, monkeypatch):
        """The `standard` / `advisory` paths must not acquire a commit they
        never had — the site is only reachable through a registered gate."""
        root, _cfg_, (ok, _e, *_), seen = self._run(tmp_path, monkeypatch, gate_status=None)
        assert ok is True and seen == []
        assert not [s for s in _subjects(root) if "status" in s]

    def test_a_content_change_is_left_dirty_and_reported(self, tmp_path, monkeypatch):
        """Acceptance: a failed bookkeeping commit is visible and is never
        passed off as a clean resumable stop.

        The content change has to arrive **during review** to be in scope here.
        An edit the agent made earlier is already inside the candidate commit —
        `commit_task_work` stages the whole tree — so it is judged by the gate
        rather than left for this to catch. The window this proof guards is the
        one between the candidate commit and the stop, where the reviewer (an
        agent with write access) or a concurrent editor can still move the
        spec. Measured, not assumed: the first version of this test edited the
        file up front and saw it committed as task work."""
        from spec_runner import gates as gates_mod
        from spec_runner import hooks
        from spec_runner.gates import GateRegistry, GateResult, GateStatus
        from spec_runner.git_ops import spec_dirty_paths
        from spec_runner.review import ReviewVerdict
        from spec_runner.state import PhaseOutcome
        from spec_runner.task import Task

        root = _repo(tmp_path)
        cfg = _cfg(
            root,
            state_file=root / "spec" / ".executor-state.db",
            logs_dir=root / "spec" / ".logs",
            create_git_branch=False,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=True,
        )
        registry = GateRegistry()
        registry.register(
            "probe",
            "review",
            lambda ctx: GateResult(GateStatus.UNSATISFIED, PhaseOutcome.UNEXPECTED_FAIL, "blocked"),
        )
        monkeypatch.setattr(gates_mod, "REGISTRY", registry)

        tasks = root / "spec" / "tasks.md"

        def _review_that_edits_the_spec(*_a, **_k):
            tasks.write_text(tasks.read_text().replace("- [ ] a", "- [x] a"))
            return (ReviewVerdict.PASSED, None, "ok")

        monkeypatch.setattr(hooks, "run_code_review", _review_that_edits_the_spec)

        task = Task(id="TASK-001", name="first", priority="p1", status="in_progress", estimate="1d")
        ok, error, *_ = hooks.post_done_hook(task, cfg, True)
        assert ok is False
        assert "more than" in (error or ""), error
        assert spec_dirty_paths(cfg), "a real spec change must stay dirty and block"

    def test_the_instrument_error_prefix_survives_a_failed_bookkeeping(self, tmp_path, monkeypatch):
        """The prefix is a contract, not prose: `execution` reads it to record
        INFRASTRUCTURE (exit 2). Appending must never break `startswith`."""
        from spec_runner import gates as gates_mod
        from spec_runner import hooks
        from spec_runner.gates import GateRegistry, GateResult, GateStatus
        from spec_runner.hooks import GATE_INSTRUMENT_ERROR_PREFIX
        from spec_runner.review import ReviewVerdict
        from spec_runner.state import PhaseOutcome
        from spec_runner.task import Task

        root = _repo(tmp_path)
        cfg = _cfg(
            root,
            state_file=root / "spec" / ".executor-state.db",
            logs_dir=root / "spec" / ".logs",
            create_git_branch=False,
            run_tests_on_done=False,
            run_lint_on_done=False,
            run_review=True,
            gate_recovery_attempts=1,
        )
        registry = GateRegistry()
        registry.register(
            "probe",
            "review",
            lambda ctx: GateResult(
                GateStatus.INSTRUMENT_ERROR, PhaseOutcome.NOT_RUN, "instrument down"
            ),
        )
        monkeypatch.setattr(gates_mod, "REGISTRY", registry)
        tasks = root / "spec" / "tasks.md"

        def _review_that_edits_the_spec(*_a, **_k):
            tasks.write_text(tasks.read_text().replace("- [ ] a", "- [x] a"))
            return (ReviewVerdict.PASSED, None, "ok")

        monkeypatch.setattr(hooks, "run_code_review", _review_that_edits_the_spec)

        task = Task(id="TASK-001", name="first", priority="p1", status="in_progress", estimate="1d")
        _ok, error, *_ = hooks.post_done_hook(task, cfg, True)
        assert (error or "").startswith(GATE_INSTRUMENT_ERROR_PREFIX), error
        assert "more than" in (error or ""), "the failed bookkeeping must still be visible"


class TestTheOtherHarnessStatus:
    """Found by the battle test of the first version of this fix, on a build
    from master: cleaning up the `review` flip left the *terminal failure*
    flip — `⏸️ BLOCKED`, written by `run_with_retries` when a task stops — and
    the deadlock came back one status later. Measured, three runs deep:

        run 1  blocked → REVIEW committed → tree clean
        run 2  writes BLOCKED, uncommitted  → tree dirty
        run 3  ⛔ Refusing to run: spec/config files have uncommitted changes

    Both flips are the harness recording its own process, so both are
    bookkeeping and both get committed.
    """

    def test_blocked_is_a_process_record_too(self, tmp_path):
        from spec_runner.git_ops import spec_dirty_paths

        root = _repo(tmp_path)
        cfg = _cfg(root)
        _flip(root, status="blocked")
        assert commit_status_flip(cfg, "TASK-001", reason="task failed") is None
        assert spec_dirty_paths(cfg) == []

    def test_done_is_still_refused(self, tmp_path):
        """`done` is a claim about the work, carried by the task's own commit.
        Letting it through here would make bookkeeping a way to finish a task."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _flip(root, status="done")
        problem = commit_status_flip(cfg, "TASK-001", reason="whatever")
        assert problem and "done" in problem

    def test_the_reason_is_recorded(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _flip(root, status="blocked")
        commit_status_flip(cfg, "TASK-001", reason="Pre-terminal gate unsatisfied: review")
        assert "Pre-terminal gate unsatisfied" in _git(root, "log", "-1", "--format=%B").stdout

    def test_the_reason_stays_one_trailer_line(self, tmp_path):
        """Raised in review: the reason arrives from `last_error` or a gate
        detail, so it can be multi-line and long — and a trailer is one line to
        `git interpret-trailers` and `--format=%(trailers)` alike."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _flip(root, status="blocked")
        commit_status_flip(cfg, "TASK-001", reason="line one\nline two\n\n  padded   " + "x" * 400)
        body = _git(root, "log", "-1", "--format=%B").stdout
        trailer = [ln for ln in body.splitlines() if ln.startswith("Status-Reason:")]
        assert len(trailer) == 1
        assert "line one line two padded" in trailer[0]
        assert len(trailer[0]) < 250, trailer[0]
        # And git itself agrees it is a trailer.
        parsed = _git(root, "log", "-1", "--format=%(trailers:key=Status-Reason)").stdout
        assert parsed.strip().startswith("Status-Reason:")

    def test_an_empty_reason_still_yields_a_trailer(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        _flip(root, status="blocked")
        commit_status_flip(cfg, "TASK-001", reason="   \n  ")
        assert "Status-Reason: unspecified" in _git(root, "log", "-1", "--format=%B").stdout

    def test_the_failure_path_commits_it(self, tmp_path, monkeypatch):
        """Through the real terminal-failure helper, not just the module."""
        from spec_runner.execution import _record_blocked
        from spec_runner.git_ops import spec_dirty_paths
        from spec_runner.task import Task, get_task_by_id, parse_tasks

        root = _repo(tmp_path)
        cfg = _cfg(root, state_file=root / "spec" / ".executor-state.db")
        task = Task(id="TASK-001", name="first", priority="p1", status="review", estimate="1d")

        _record_blocked(task, cfg)

        assert spec_dirty_paths(cfg) == []
        parsed = get_task_by_id(parse_tasks(root / "spec" / "tasks.md"), "TASK-001")
        assert parsed is not None and parsed.status == "blocked"

    def test_a_bookkeeping_problem_never_takes_the_run_down(self, tmp_path, monkeypatch):
        """A task is already failing when this runs. #127's lesson: the tail of
        a failing run is the worst place to raise."""
        import spec_runner.bookkeeping as bk
        from spec_runner.execution import _record_blocked
        from spec_runner.task import Task

        root = _repo(tmp_path)
        cfg = _cfg(root, state_file=root / "spec" / ".executor-state.db")
        monkeypatch.setattr(
            bk,
            "commit_status_flip",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git ate it")),
        )
        task = Task(id="TASK-001", name="first", priority="p1", status="review", estimate="1d")
        _record_blocked(task, cfg)  # must not raise

    def test_auto_commit_off_commits_nothing(self, tmp_path):
        from spec_runner.bookkeeping import commit_status_flip_quietly

        root = _repo(tmp_path)
        cfg = _cfg(root, auto_commit=False)
        _flip(root, status="blocked")
        commit_status_flip_quietly(cfg, "TASK-001", reason="task failed")
        assert _subjects(root) == ["spec"]


class TestRecoveryAfterACrash:
    """Measured on a build from master: `SIGKILL` during review leaves the
    REVIEW flip uncommitted, and the next run refuses. The stop path commits
    its own flip; a crash has no stop path, so recovery happens where the guard
    would otherwise refuse — under the same proof."""

    def test_an_interrupted_flip_is_committed_and_the_task_inferred(self, tmp_path):
        from spec_runner.bookkeeping import recover_interrupted_flip
        from spec_runner.git_ops import spec_dirty_paths

        root = _repo(tmp_path)
        cfg = _cfg(root)
        _flip(root, status="review")  # …and the process was killed here

        recovered = recover_interrupted_flip(cfg)
        assert recovered is not None
        assert recovered.task_id == "TASK-001" and recovered.new == "review"
        assert spec_dirty_paths(cfg) == []

    def test_in_progress_counts_too(self, tmp_path):
        """A crash right after the task started is the earliest form of it."""
        from spec_runner.bookkeeping import recover_interrupted_flip

        root = _repo(tmp_path)
        cfg = _cfg(root)
        _flip(root, task_id="TASK-002", status="in_progress")  # TASK-001 is already there
        recovered = recover_interrupted_flip(cfg)
        assert recovered is not None and recovered.task_id == "TASK-002"

    def test_a_left_behind_done_is_not_recovered(self, tmp_path):
        """`done` is a claim about the work, carried by the task's own commit.
        Committing one found lying in the tree would complete a task nobody
        finished."""
        from spec_runner.bookkeeping import recover_interrupted_flip
        from spec_runner.git_ops import spec_dirty_paths

        root = _repo(tmp_path)
        cfg = _cfg(root)
        _flip(root, status="done")
        assert recover_interrupted_flip(cfg) is None
        assert spec_dirty_paths(cfg), "it must stay dirt, for the guard to refuse"

    def test_a_real_spec_edit_is_not_recovered(self, tmp_path):
        from spec_runner.bookkeeping import recover_interrupted_flip

        root = _repo(tmp_path)
        cfg = _cfg(root)
        tasks = root / "spec" / "tasks.md"
        _flip(root, status="review")
        tasks.write_text(tasks.read_text().replace("- [ ] a", "- [x] a"))
        assert recover_interrupted_flip(cfg) is None

    def test_the_guard_lets_the_run_through_afterwards(self, tmp_path, capsys):
        """The acceptance criterion, through the guard itself."""
        from argparse import Namespace

        from spec_runner.cli import _enforce_clean_spec

        root = _repo(tmp_path)
        cfg = _cfg(root)
        _flip(root, status="review")
        _enforce_clean_spec(Namespace(allow_dirty_spec=False), cfg)  # must not SystemExit
        assert "Recovered an interrupted run" in capsys.readouterr().out

    def test_the_guard_still_refuses_a_real_spec_change(self, tmp_path):
        from argparse import Namespace

        import pytest as _pytest

        from spec_runner.cli import _enforce_clean_spec

        root = _repo(tmp_path)
        cfg = _cfg(root)
        tasks = root / "spec" / "tasks.md"
        tasks.write_text(tasks.read_text().replace("- [ ] a", "- [x] a"))
        with _pytest.raises(SystemExit):
            _enforce_clean_spec(Namespace(allow_dirty_spec=False), cfg)


class TestTheTwoInvariants:
    def test_resuming_does_not_grow_a_chain_of_review_commits(self, tmp_path):
        """Invariant 2. The second pass writes the same status, so there is
        nothing to commit — not a second identical commit."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        for _ in range(3):
            _flip(root)  # what the run does on every pass through review
            commit_status_flip(cfg, "TASK-001", candidate_sha="abc", reason="gate")
        flips = [s for s in _subjects(root) if "TASK-001" in s]
        assert len(flips) == 1, _subjects(root)

    def test_an_edit_landing_between_the_proof_and_the_commit_is_refused(self, tmp_path):
        """Also from review: the file was read more than once while deciding.
        It is read once now, and what was proven is verified to be what got
        staged — a write landing in between refuses instead of riding along."""
        import spec_runner.bookkeeping as bk

        root = _repo(tmp_path)
        cfg = _cfg(root)
        tasks = root / "spec" / "tasks.md"
        _flip(root)

        real_git = bk._git

        def _edit_then_git(config, *args):
            if args and args[0] == "add":
                result = real_git(config, *args)
                tasks.write_text(tasks.read_text().replace("- [ ] a", "- [x] a"))
                return result
            return real_git(config, *args)

        bk._git = _edit_then_git
        try:
            problem = commit_status_flip(cfg, "TASK-001", candidate_sha="abc", reason="gate")
        finally:
            bk._git = real_git
        assert problem and "changed while" in problem
        assert _subjects(root) == ["spec"], "nothing may be committed after a refusal"

    def test_a_crash_between_the_flip_and_the_commit_recovers(self, tmp_path):
        """The window the whole design has to survive: the process dies after
        `tasks.md` was flipped and before anything committed it. The next pass
        finds a dirty status-only file, which is exactly the case this commits
        — so recovery is the ordinary path, not a special one."""
        from spec_runner.git_ops import spec_dirty_paths

        root = _repo(tmp_path)
        cfg = _cfg(root)
        _flip(root)  # …and the process dies here
        assert spec_dirty_paths(cfg)

        assert commit_status_flip(cfg, "TASK-001", candidate_sha="abc", reason="gate") is None
        assert spec_dirty_paths(cfg) == []
        assert len([s for s in _subjects(root) if "TASK-001" in s]) == 1

    def test_the_bookkeeping_commit_is_not_the_candidate(self, tmp_path):
        """Invariant 1, at its root: the commit the gate judged and the commit
        recording the block are different objects, and the recorded candidate
        stays the former."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        candidate = _git(root, "rev-parse", "HEAD").stdout.strip()
        _flip(root)
        commit_status_flip(cfg, "TASK-001", candidate_sha=candidate, reason="gate")
        head = _git(root, "rev-parse", "HEAD").stdout.strip()
        assert head != candidate
        assert candidate in _git(root, "log", "-1", "--format=%B").stdout
        # …and the candidate is still an ancestor, so nothing was rewritten.
        _git(root, "merge-base", "--is-ancestor", candidate, head)
