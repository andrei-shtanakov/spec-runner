"""#252 variant D: the evidential test lives in a file of its own.

The wall the owner hit: a claim freezes the **whole file**, the green
legitimately appended tests to that same file, and after `tdd resume` no tree
containing the implementation could satisfy the reinstated claim. TASK-101 was
unfinishable through the loop.

Variant D, signed off: the red is written to a new file, the byte-lock stays
exactly what it is — a whole-file hash, the cheapest instrument that cannot be
fooled by formatting, moved lines, or an AST that parses differently in two
versions of a runner — and the conflict becomes impossible instead of being
adjudicated.

The five conditions attached to that sign-off, each pinned below:

1. the file name is formed by the **adapter**, not a shared Python-shaped
   heuristic (the mistake of #198's selector and #220's linter);
2. the file must land in the runner's **ordinary discovery** — a test nothing
   collects is a red that cannot be replayed;
3. existence is checked against **`baseline_sha`**, and a git failure is an
   **INSTRUMENT_ERROR** rather than a verdict (#245's rule);
4. **no exemption** for a file the task itself created earlier: the invariant
   stays "did it exist at the baseline", which is provable, rather than a story
   about who wrote what when;
5. legacy checkpoints are neither migrated nor reinterpreted.

Variant B (the claim ends at green) stays forbidden and C (re-claim at green)
deferred, per the same sign-off.
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.tdd import RedOutcome, run_red_phase
from spec_runner.tdd_runners import ADAPTERS

FAILING = "def test_new_behaviour():\n    assert False\n"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path, *, with_existing_test: bool = False) -> ExecutorConfig:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True, exist_ok=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "o@e.c")
    _git(root, "config", "user.name", "O")
    (root / "README.md").write_text("x\n")
    if with_existing_test:
        (root / "tests" / "test_catalog.py").write_text("def test_old():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")

    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        execution_mode="tdd",
        test_command="python -m pytest",
        lint_command="",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task():
    from spec_runner.task import Task

    return Task(id="TASK-104", name="t", priority="p1", status="todo", estimate="1h")


def _agent_writing(monkeypatch, path: str, body: str = FAILING, selector: str | None = None):
    """A RED pass that writes one file and reports its selector."""
    from spec_runner import tdd

    def _red(config, prompt, **kwargs):
        target = Path(config.project_root) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        node = selector or f"{path}::test_new_behaviour"
        return tdd.AgentCall(text=f"TDD_SELECTOR: {node}")

    monkeypatch.setattr(tdd, "_run_agent", _red)


class TestTheAdapterNamesTheFile:
    def test_each_adapter_answers_for_itself(self):
        """A shared guess is the defect this avoids: `tests/test_x_red.py` in
        an Elixir project is a file `mix test` never collects."""
        assert ADAPTERS["pytest"].evidential_file("TASK-104") == PurePosixPath(
            "tests/test_task_104_red.py"
        )
        assert ADAPTERS["exunit"].evidential_file("TASK-104") == PurePosixPath(
            "test/task_104_red_test.exs"
        )

    @pytest.mark.parametrize("name", list(ADAPTERS))
    def test_what_it_names_is_what_it_collects(self, name):
        """Condition 2, asked of every adapter rather than of the two I
        happened to think about."""
        adapter = ADAPTERS[name]

        assert adapter.is_discoverable(adapter.evidential_file("TASK-104"))

    @pytest.mark.parametrize(
        ("name", "path"),
        [
            ("pytest", "src/thing.py"),
            ("pytest", "tests/helpers.py"),
            ("exunit", "lib/kapelle/catalog.ex"),
            ("exunit", "test/support/fixtures.ex"),
        ],
    )
    def test_a_file_the_runner_never_collects_is_not_discoverable(self, name, path):
        assert ADAPTERS[name].is_discoverable(PurePosixPath(path)) is False

    def test_the_prompt_asks_for_the_adapter_s_path(self, tmp_path):
        """Condition 5 of the sign-off: the prompt and the later validation
        must demand the same thing, so the prompt states the invariant *and*
        names a file that satisfies it."""
        from spec_runner.prompt import build_red_prompt

        cfg = _repo(tmp_path)
        prompt = build_red_prompt(_task(), cfg)

        assert "new file that does not exist yet" in prompt
        assert "tests/test_task_104_red.py" in prompt

    def test_with_no_adapter_the_rule_is_stated_without_an_invented_path(self, tmp_path):
        """Naming a file for a runner nothing knows about would be exactly the
        heuristic this replaces."""
        from spec_runner.prompt import build_red_prompt

        cfg = _repo(tmp_path)
        cfg.test_command = "make check"
        prompt = build_red_prompt(_task(), cfg)

        assert "new file that does not exist yet" in prompt
        assert "test_task_104_red" not in prompt


@pytest.mark.slow
class TestTheRedPhaseEnforcesIt:
    def test_a_new_file_is_accepted(self, tmp_path, monkeypatch):
        cfg = _repo(tmp_path)
        _agent_writing(monkeypatch, "tests/test_task_104_red.py")

        from spec_runner.state import ExecutorState

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.EXPECTED_FAIL
        assert result.checkpoint is not None

    def test_a_pre_existing_file_is_refused(self, tmp_path, monkeypatch):
        """The pilot's exact shape: the red written into the module's own test
        file, which the green then legitimately needs to extend."""
        cfg = _repo(tmp_path, with_existing_test=True)
        _agent_writing(monkeypatch, "tests/test_catalog.py", body=FAILING)

        from spec_runner.state import ExecutorState

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "already existed" in (result.detail or "")
        assert result.checkpoint is None

    def test_a_refused_red_leaves_no_lock(self, tmp_path, monkeypatch):
        """Checked before anything is claimed: a refusal that froze the file
        anyway would be the wedge it exists to prevent."""
        cfg = _repo(tmp_path, with_existing_test=True)
        _agent_writing(monkeypatch, "tests/test_catalog.py")

        from spec_runner.state import ExecutorState
        from spec_runner.tdd import resolve_namespace

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)
            claims = state.active_claims(resolve_namespace(cfg))

        assert claims == []

    def test_it_is_a_policy_refusal_not_an_instrument_error(self, tmp_path, monkeypatch):
        """A file that existed is something the tool *looked at and found*.
        The task fails (exit 1); the environment is not blamed (exit 2)."""
        cfg = _repo(tmp_path, with_existing_test=True)
        _agent_writing(monkeypatch, "tests/test_catalog.py")

        from spec_runner.state import ExecutorState

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.instrument_error is False

    def test_a_file_the_runner_would_not_collect_is_refused_early(self, tmp_path, monkeypatch):
        """Cheaper than replaying it and reading `unverifiable` — and the
        message names the shape that would work."""
        cfg = _repo(tmp_path)
        _agent_writing(
            monkeypatch,
            "tests/helpers_red.py",
            selector="tests/helpers_red.py::test_new_behaviour",
        )

        from spec_runner.state import ExecutorState

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "does not collect" in (result.detail or "")

    def test_an_unreadable_baseline_is_an_instrument_error(self, tmp_path, monkeypatch):
        """Condition 3, and #245's rule: "we could not look" is not "the file
        is new". Measured rather than mocked — the baseline handed to git is a
        real object name that resolves to nothing, which is what a corrupted
        state file or a garbage-collected commit produces.

        The tool matters here (Copilot, PR #280): `git cat-file -e` answers
        **128 for everything** — absent path, invalid revision, not a
        repository — so a returncode read there let a bad baseline pass as "the
        file is new". `ls-tree` separates the questions by construction.
        """
        from spec_runner import tdd
        from spec_runner.state import ExecutorState

        cfg = _repo(tmp_path)
        _agent_writing(monkeypatch, "tests/test_task_104_red.py")
        monkeypatch.setattr(tdd, "_head", lambda config: "0" * 40)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.instrument_error is True
        assert "could not say" in (result.detail or "")
        assert result.checkpoint is None

    def test_the_two_git_answers_are_told_apart(self, tmp_path, monkeypatch):
        """The distinction the previous version could not make: a path absent
        from a good tree is *new* (proceed), a tree git cannot read is *unknown*
        (stop). Both are exercised through the same code path."""
        from spec_runner.state import ExecutorState

        cfg = _repo(tmp_path)
        _agent_writing(monkeypatch, "tests/test_task_104_red.py")

        with ExecutorState(cfg) as state:
            good = run_red_phase(_task(), cfg, state)

        assert good.outcome is RedOutcome.EXPECTED_FAIL, "absent from a good tree means new"
        assert good.instrument_error is False


class TestTheKindReachesTheOperator:
    """Found by mutation: every test above passed while the escalation at the
    RED site was removed, so "git could not answer" would have been reported to
    CI as a failed task — exit 1, *the work is bad* — about work nothing had
    judged. That is #230 exactly, and it needs its own assertion."""

    def _refusal(self, tmp_path, monkeypatch, *, instrument: bool):
        from spec_runner import execution
        from spec_runner.stages import StageReporter
        from spec_runner.state import ExecutorState
        from spec_runner.tdd import RedPhaseResult

        cfg = _repo(tmp_path)
        monkeypatch.setattr(
            "spec_runner.tdd.run_red_phase",
            lambda *a, **k: RedPhaseResult(
                RedOutcome.UNVERIFIABLE,
                "could not say whether x existed",
                instrument_error=instrument,
            ),
        )
        with ExecutorState(cfg) as state:
            reporter = StageReporter("TASK-104", lambda _line: None)
            return execution._run_red_phase_gate(_task(), cfg, state, reporter)

    def test_an_unreadable_index_is_infrastructure(self, tmp_path, monkeypatch):
        from spec_runner.state import ErrorCode

        refusal = self._refusal(tmp_path, monkeypatch, instrument=True)

        assert refusal is not None
        assert refusal.error_code is ErrorCode.INFRASTRUCTURE, "exit 2 — fix the environment"

    def test_a_genuine_missing_red_stays_a_failed_task(self, tmp_path, monkeypatch):
        """The other side, or the escalation would swallow every ordinary
        refusal into "the environment is broken"."""
        from spec_runner.state import ErrorCode

        refusal = self._refusal(tmp_path, monkeypatch, instrument=False)

        assert refusal is not None
        assert refusal.error_code is ErrorCode.HOOK_FAILURE, "exit 1 — fix the work"


@pytest.mark.slow
class TestWhatIsDeliberatelyNotDone:
    def test_no_exemption_for_a_file_this_task_created_earlier(self, tmp_path, monkeypatch):
        """Condition 4. An earlier attempt of the *same* task committed the
        file; it existed at this attempt's baseline, so it is refused. The
        invariant stays provable rather than becoming a story about authorship.
        """
        cfg = _repo(tmp_path)
        root = cfg.project_root
        (root / "tests" / "test_task_104_red.py").write_text(
            "def test_earlier():\n    assert True\n"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "TASK-104: an earlier attempt's file")
        _agent_writing(monkeypatch, "tests/test_task_104_red.py")

        from spec_runner.state import ExecutorState

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)

        assert result.outcome is RedOutcome.UNVERIFIABLE
        assert "already existed" in (result.detail or "")

    def test_a_legacy_checkpoint_is_not_reinterpreted(self, tmp_path):
        """Condition 5. The check lives in the authoring path, so a red that
        was recorded before this rule — over a shared file — keeps standing and
        keeps being reusable. Migrating it would retroactively invalidate
        evidence that was admissible when it was taken."""
        from spec_runner.claims import record_claims
        from spec_runner.state import ExecutorState
        from spec_runner.tdd import (
            RedCheckpoint,
            _config_hash,
            _reusable_checkpoint,
            resolve_namespace,
        )

        cfg = _repo(tmp_path, with_existing_test=True)
        sha = _git(cfg.project_root, "rev-parse", "HEAD").stdout.strip()
        legacy = RedCheckpoint(
            task_id="TASK-104",
            namespace=resolve_namespace(cfg),
            commit_sha=sha,
            baseline_sha=sha,
            selector="tests/test_catalog.py::test_old",
            environment_id="unpinned",
            execution_mode="tdd",
            config_hash=_config_hash(cfg),
            outcome=RedOutcome.EXPECTED_FAIL,
            timestamp="2026-08-01T00:00:00",
        )
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(legacy)
            record_claims(cfg, state, legacy)
            reusable, ambiguity = _reusable_checkpoint(cfg, state, _task())

        assert ambiguity is None
        assert reusable is not None and reusable.checkpoint_id == legacy.checkpoint_id
