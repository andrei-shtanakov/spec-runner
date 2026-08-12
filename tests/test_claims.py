"""#141 slice 2: the claim contract — a byte-lock on the files a red depends on.

The pilot's first version checked only the current selector, so neighbouring
tests were protected by a sentence in the agent's prompt rather than by the
instrument. This is the instrument.

Contract: `docs/superpowers/specs/2026-08-11-claim-and-remedy-contracts.md` §1
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.claims import (
    Claim,
    ClaimStatus,
    ViolationKind,
    check_claims,
    claim_paths_for,
    record_claims,
    validate_claim_path,
)
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.tdd import RedCheckpoint, RedOutcome, resolve_namespace


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "execution_mode": "tdd",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _commit(root: Path, files: dict[str, str], message: str = "c") -> str:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD").stdout.strip()


def _checkpoint(cfg, sha, *, task="TASK-001", selector="tests/test_x.py::test_y") -> RedCheckpoint:
    return RedCheckpoint(
        task_id=task,
        namespace=resolve_namespace(cfg),
        commit_sha=sha,
        baseline_sha=sha,
        selector=selector,
        environment_id="unpinned",
        execution_mode="tdd",
        config_hash="h",
        outcome=RedOutcome.EXPECTED_FAIL,
        timestamp="2026-08-11T00:00:00",
    )


class TestTheCheckpointHasAStableId:
    """`--checkpoint <id>` needs something a person can copy and that survives
    a state rebuild; an autoincrement rowid is neither."""

    def test_the_id_is_derived_not_a_rowid(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))
        assert _checkpoint(cfg, "a" * 40).checkpoint_id.isalnum()

    def test_the_same_checkpoint_yields_the_same_id(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))
        assert _checkpoint(cfg, "a" * 40).checkpoint_id == _checkpoint(cfg, "a" * 40).checkpoint_id

    def test_a_different_commit_yields_a_different_id(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))
        assert _checkpoint(cfg, "a" * 40).checkpoint_id != _checkpoint(cfg, "b" * 40).checkpoint_id

    def test_a_different_selector_yields_a_different_id(self, tmp_path):
        cfg = _cfg(_repo(tmp_path))
        a = _checkpoint(cfg, "a" * 40, selector="tests/t.py::one")
        b = _checkpoint(cfg, "a" * 40, selector="tests/t.py::two")
        assert a.checkpoint_id != b.checkpoint_id

    def test_it_survives_a_round_trip_through_the_state_db(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        cp = _checkpoint(cfg, "a" * 40)
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(cp)
            stored = state.red_checkpoint("TASK-001", resolve_namespace(cfg))
        assert stored.checkpoint_id == cp.checkpoint_id


class TestTheClaimSetComesFromTheSelector:
    def test_a_node_id_claims_its_file(self):
        assert claim_paths_for(_sel("tests/test_x.py::TestY::test_z")) == ["tests/test_x.py"]

    def test_a_plain_file_node_id_claims_the_file(self):
        assert claim_paths_for(_sel("tests/test_x.py::test_y")) == ["tests/test_x.py"]

    def test_a_selector_without_a_node_id_claims_nothing(self):
        """Refused upstream by `verify_red`; claiming nothing here rather than
        guessing keeps the two from disagreeing."""
        assert _parse("test_x") is None, "an unparseable selector never becomes a Selector"

    def test_a_conftest_fixture_is_not_claimed(self):
        """The documented limitation, pinned so it is a known gap rather than a
        surprise: a selector names one file, so a fixture it depends on is
        reachable and unlocked (§1.3)."""
        assert "tests/conftest.py" not in claim_paths_for(_sel("tests/test_x.py::test_y"))


class TestPathValidation:
    def test_an_ordinary_file_is_accepted(self, tmp_path):
        root = _repo(tmp_path)
        _commit(root, {"tests/test_x.py": "x\n"})
        assert validate_claim_path(root, "tests/test_x.py") is None

    def test_a_symlink_is_rejected(self, tmp_path):
        """A symlink's bytes are the target's, so hashing it freezes something
        the claim does not name."""
        root = _repo(tmp_path)
        _commit(root, {"tests/real.py": "x\n"})
        (root / "tests" / "link.py").symlink_to(root / "tests" / "real.py")
        assert "symlink" in (validate_claim_path(root, "tests/link.py") or "")

    def test_a_path_outside_the_repo_is_rejected(self, tmp_path):
        root = _repo(tmp_path)
        assert validate_claim_path(root, "../elsewhere.py") is not None

    def test_a_non_canonical_path_is_rejected(self, tmp_path):
        """`git ls-tree` keys are canonical, so `tests/./test_x.py` would never
        match its own entry and would read as DELETED on a tree where the file
        is untouched. A false violation blocks work for a reason that is not
        true, which is worse than a refusal."""
        root = _repo(tmp_path)
        _commit(root, {"tests/test_x.py": "x\n"})
        assert "canonical" in (validate_claim_path(root, "tests/./test_x.py") or "")
        assert "canonical" in (validate_claim_path(root, "tests/../tests/test_x.py") or "")

    def test_an_absolute_path_inside_the_repo_is_still_rejected(self, tmp_path):
        """Being inside the tree does not make it comparable to a ls-tree key."""
        root = _repo(tmp_path)
        _commit(root, {"tests/test_x.py": "x\n"})
        assert "project-relative" in (
            validate_claim_path(root, str(root / "tests" / "test_x.py")) or ""
        )

    def test_an_absolute_path_escaping_the_repo_is_rejected(self, tmp_path):
        root = _repo(tmp_path)
        assert validate_claim_path(root, "/etc/passwd") is not None

    def test_a_directory_is_rejected(self, tmp_path):
        root = _repo(tmp_path)
        _commit(root, {"tests/test_x.py": "x\n"})
        assert "regular file" in (validate_claim_path(root, "tests") or "")

    def test_a_missing_file_is_rejected(self, tmp_path):
        root = _repo(tmp_path)
        assert validate_claim_path(root, "tests/absent.py") is not None


class TestHashingIsOverRawBytes:
    def test_a_line_ending_flip_changes_the_hash(self, tmp_path):
        """A claim that tolerates a CRLF flip is not a byte-lock."""
        from spec_runner.claims import claim_blob_sha

        root = _repo(tmp_path)
        (root / "a.py").write_bytes(b"x = 1\n")
        lf = claim_blob_sha(root, "a.py")
        (root / "a.py").write_bytes(b"x = 1\r\n")
        assert claim_blob_sha(root, "a.py") != lf

    def test_it_matches_git_hash_object(self, tmp_path):
        from spec_runner.claims import claim_blob_sha

        root = _repo(tmp_path)
        (root / "a.py").write_bytes(b"def f():\n    return 1\n")
        expected = _git(root, "hash-object", "a.py").stdout.strip()
        assert claim_blob_sha(root, "a.py") == expected


class TestRecording:
    def test_a_claim_round_trips(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        sha = _commit(root, {"tests/test_x.py": "def test_y():\n    assert False\n"})
        with ExecutorState(cfg) as state:
            record_claims(cfg, state, _checkpoint(cfg, sha))
            claims = state.active_claims(resolve_namespace(cfg))
        assert [c.path for c in claims] == ["tests/test_x.py"]
        assert claims[0].status is ClaimStatus.ACTIVE

    def test_reclaiming_the_same_bytes_is_idempotent(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        sha = _commit(root, {"tests/test_x.py": "def test_y():\n    assert False\n"})
        with ExecutorState(cfg) as state:
            record_claims(cfg, state, _checkpoint(cfg, sha))
            record_claims(cfg, state, _checkpoint(cfg, sha))
            claims = state.active_claims(resolve_namespace(cfg))
        assert len(claims) == 1, "a re-run must not stack duplicate rows"

    def test_another_workstream_is_a_separate_ledger(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        sha = _commit(root, {"tests/test_x.py": "x\n"})
        with ExecutorState(cfg) as state:
            record_claims(cfg, state, _checkpoint(cfg, sha))
            assert state.active_claims("someone-else") == []


class TestEnforcement:
    """§1.5 — against the candidate commit, over every active claim in the
    namespace, not only the current task's."""

    def _claimed(self, tmp_path):
        root = _repo(tmp_path)
        cfg = _cfg(root)
        sha = _commit(root, {"tests/test_x.py": "def test_y():\n    assert False\n"})
        with ExecutorState(cfg) as state:
            record_claims(cfg, state, _checkpoint(cfg, sha))
        return root, cfg, sha

    def test_an_untouched_claim_is_not_a_violation(self, tmp_path):
        root, cfg, sha = self._claimed(tmp_path)
        candidate = _commit(root, {"impl.py": "x = 1\n"})
        with ExecutorState(cfg) as state:
            assert check_claims(cfg, state, resolve_namespace(cfg), candidate) == []

    def test_a_modified_claim_is_a_violation(self, tmp_path):
        root, cfg, _ = self._claimed(tmp_path)
        candidate = _commit(root, {"tests/test_x.py": "def test_y():\n    assert True\n"})
        with ExecutorState(cfg) as state:
            violations = check_claims(cfg, state, resolve_namespace(cfg), candidate)
        assert [v.kind for v in violations] == [ViolationKind.MODIFIED]

    def test_a_deleted_claim_is_a_violation_and_is_named_as_one(self, tmp_path):
        root, cfg, _ = self._claimed(tmp_path)
        _git(root, "rm", "-q", "tests/test_x.py")
        _git(root, "commit", "-qm", "delete")
        candidate = _git(root, "rev-parse", "HEAD").stdout.strip()
        with ExecutorState(cfg) as state:
            violations = check_claims(cfg, state, resolve_namespace(cfg), candidate)
        assert [v.kind for v in violations] == [ViolationKind.DELETED]

    def test_a_renamed_claim_is_distinguished_from_a_delete(self, tmp_path):
        """The bytes survive at another path. Blocking either way, but calling
        a rename a deletion sends the operator looking for the wrong thing."""
        root, cfg, _ = self._claimed(tmp_path)
        _git(root, "mv", "tests/test_x.py", "tests/test_renamed.py")
        _git(root, "commit", "-qm", "rename")
        candidate = _git(root, "rev-parse", "HEAD").stdout.strip()
        with ExecutorState(cfg) as state:
            violations = check_claims(cfg, state, resolve_namespace(cfg), candidate)
        assert [v.kind for v in violations] == [ViolationKind.RENAMED]
        assert "tests/test_renamed.py" in (violations[0].detail or "")

    def test_the_working_tree_cannot_change_the_answer(self, tmp_path):
        """Authoritative against the candidate commit, never the mutable tree
        — the same reason the RED replay judges a commit."""
        root, cfg, sha = self._claimed(tmp_path)
        candidate = _commit(root, {"impl.py": "x = 1\n"})
        (root / "tests" / "test_x.py").write_text("def test_y():\n    assert True\n")
        with ExecutorState(cfg) as state:
            assert check_claims(cfg, state, resolve_namespace(cfg), candidate) == [], (
                "an uncommitted edit is not what will be merged"
            )

    def test_another_tasks_claim_is_enforced_too(self, tmp_path):
        """The pilot's finding: checking only the current task's claim left
        neighbouring tests guarded by prompt text."""
        root = _repo(tmp_path)
        cfg = _cfg(root)
        sha = _commit(root, {"tests/test_a.py": "def test_a():\n    assert False\n"})
        with ExecutorState(cfg) as state:
            record_claims(
                cfg,
                state,
                _checkpoint(cfg, sha, task="TASK-OTHER", selector="tests/test_a.py::test_a"),
            )
        candidate = _commit(root, {"tests/test_a.py": "def test_a():\n    assert True\n"})
        with ExecutorState(cfg) as state:
            violations = check_claims(cfg, state, resolve_namespace(cfg), candidate)
        assert [v.task_id for v in violations] == ["TASK-OTHER"]

    def test_a_superseded_claim_is_not_enforced(self, tmp_path):
        root, cfg, _ = self._claimed(tmp_path)
        candidate = _commit(root, {"tests/test_x.py": "def test_y():\n    assert True\n"})
        with ExecutorState(cfg) as state:
            state.supersede_claims(resolve_namespace(cfg), "TASK-001", ClaimStatus.SUPERSEDED)
            assert check_claims(cfg, state, resolve_namespace(cfg), candidate) == []

    def test_claims_of_another_namespace_are_not_enforced(self, tmp_path):
        root, cfg, _ = self._claimed(tmp_path)
        candidate = _commit(root, {"tests/test_x.py": "def test_y():\n    assert True\n"})
        with ExecutorState(cfg) as state:
            assert check_claims(cfg, state, "someone-else", candidate) == []


class TestTheGate:
    """Registered alongside the RED gate for the `tests` phase, so it is
    evaluated at both existing points: before GREEN and before merge."""

    def _evaluate(self, cfg, state, candidate, mode="tdd"):
        from spec_runner.gates import (
            GateContext,
            GateRegistry,
            evaluate_gates,
            register_builtin_gates,
        )

        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)
        ctx = GateContext(
            task_id="TASK-001",
            checkpoint_sha=candidate,
            config=cfg,
            state=state,
            facts={"execution_mode": mode},
        )
        return evaluate_gates("tests", ctx, registry=registry)

    def test_a_violated_claim_is_unsatisfied(self, tmp_path):
        from spec_runner.gates import GateStatus

        root = _repo(tmp_path)
        cfg = _cfg(root)
        sha = _commit(root, {"tests/test_x.py": "def test_y():\n    assert False\n"})
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(_checkpoint(cfg, sha))
            record_claims(cfg, state, _checkpoint(cfg, sha))
        candidate = _commit(root, {"tests/test_x.py": "def test_y():\n    assert True\n"})
        with ExecutorState(cfg) as state:
            outcome = self._evaluate(cfg, state, candidate)
        assert outcome.status is GateStatus.UNSATISFIED
        assert any("claim" in (r.detail or "").lower() for r in outcome.results)

    def test_untouched_claims_do_not_block(self, tmp_path):
        from spec_runner.gates import GateStatus

        root = _repo(tmp_path)
        cfg = _cfg(root)
        sha = _commit(root, {"tests/test_x.py": "def test_y():\n    assert False\n"})
        with ExecutorState(cfg) as state:
            state.record_red_checkpoint(_checkpoint(cfg, sha))
            record_claims(cfg, state, _checkpoint(cfg, sha))
        candidate = _commit(root, {"impl.py": "x = 1\n"})
        with ExecutorState(cfg) as state:
            outcome = self._evaluate(cfg, state, candidate)
        assert outcome.status is GateStatus.SATISFIED

    def test_standard_mode_is_not_gated_on_claims(self, tmp_path):
        from spec_runner.gates import GateStatus

        root = _repo(tmp_path)
        cfg = _cfg(root)
        sha = _commit(root, {"tests/test_x.py": "def test_y():\n    assert False\n"})
        with ExecutorState(cfg) as state:
            record_claims(cfg, state, _checkpoint(cfg, sha))
        candidate = _commit(root, {"tests/test_x.py": "changed\n"})
        with ExecutorState(cfg) as state:
            outcome = self._evaluate(cfg, state, candidate, mode="standard")
        assert outcome.status is GateStatus.SATISFIED


@pytest.mark.parametrize(
    "status,enforced",
    [(ClaimStatus.ACTIVE, True), (ClaimStatus.SUPERSEDED, False), (ClaimStatus.ABANDONED, False)],
)
def test_only_active_claims_are_enforced(tmp_path, status, enforced):
    root = _repo(tmp_path)
    cfg = _cfg(root)
    sha = _commit(root, {"tests/test_x.py": "a\n"})
    with ExecutorState(cfg) as state:
        state.record_claim(
            Claim(
                namespace=resolve_namespace(cfg),
                task_id="TASK-001",
                checkpoint_id="cp1",
                checkpoint_sha=sha,
                path="tests/test_x.py",
                blob_sha="0" * 40,
                created_at="2026-08-11T00:00:00",
                status=status,
            )
        )
        violations = check_claims(cfg, state, resolve_namespace(cfg), sha)
    assert bool(violations) is enforced


@pytest.mark.slow
class TestTheRedPhaseFreezesAndRefuses:
    """§1.5–1.6 wired into the phase that produces claims."""

    def _agent(self, monkeypatch, *, output, writes):
        from spec_runner import tdd

        def _fake(config, prompt, **kwargs):
            for name, body in (writes or {}).items():
                path = Path(config.project_root) / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(body)
            return tdd.AgentCall(text=output)

        monkeypatch.setattr(tdd, "_run_agent", _fake)

    def _task(self, task_id="TASK-001"):
        from spec_runner.task import Task

        return Task(id=task_id, name="t", priority="p1", status="todo", estimate="1h")

    def test_a_confirmed_red_freezes_its_file(self, tmp_path, monkeypatch):
        from spec_runner.tdd import run_red_phase

        root = _repo(tmp_path)
        cfg = _cfg(root, test_command="python -m pytest", lint_command="")
        self._agent(
            monkeypatch,
            output="TDD_SELECTOR: tests/test_x.py::test_y",
            writes={"tests/test_x.py": "def test_y():\n    assert False\n"},
        )
        with ExecutorState(cfg) as state:
            run_red_phase(self._task(), cfg, state)
            claims = state.active_claims(resolve_namespace(cfg))
        assert [c.path for c in claims] == ["tests/test_x.py"]

    def test_a_refuted_red_freezes_nothing(self, tmp_path, monkeypatch):
        """Only a confirmed red earns a lock. Freezing on an unproven claim
        would let a wrong test hold the suite hostage."""
        from spec_runner.tdd import run_red_phase

        root = _repo(tmp_path)
        cfg = _cfg(root, test_command="python -m pytest", lint_command="")
        self._agent(
            monkeypatch,
            output="TDD_SELECTOR: tests/test_x.py::test_y",
            writes={"tests/test_x.py": "def test_y():\n    assert True\n"},
        )
        with ExecutorState(cfg) as state:
            run_red_phase(self._task(), cfg, state)
            assert state.active_claims(resolve_namespace(cfg)) == []

    def test_a_red_that_edits_another_tasks_claim_is_refused(self, tmp_path, monkeypatch):
        from spec_runner.tdd import RedOutcome as RO
        from spec_runner.tdd import run_red_phase

        root = _repo(tmp_path)
        cfg = _cfg(root, test_command="python -m pytest", lint_command="")
        sha = _commit(root, {"tests/test_a.py": "def test_a():\n    assert False\n"})
        with ExecutorState(cfg) as state:
            record_claims(
                cfg,
                state,
                _checkpoint(cfg, sha, task="TASK-OTHER", selector="tests/test_a.py::test_a"),
            )

        self._agent(
            monkeypatch,
            output="TDD_SELECTOR: tests/test_b.py::test_b",
            writes={
                "tests/test_b.py": "def test_b():\n    assert False\n",
                "tests/test_a.py": "def test_a():\n    assert True\n",
            },
        )
        with ExecutorState(cfg) as state:
            result = run_red_phase(self._task("TASK-002"), cfg, state)
            claims = state.active_claims(resolve_namespace(cfg))

        assert result.outcome is RO.UNVERIFIABLE
        assert "violates an active claim" in (result.detail or "")
        assert [c.task_id for c in claims] == ["TASK-OTHER"], (
            "a violating red must not add claims of its own"
        )

    def test_lint_failure_stops_the_file_being_frozen(self, tmp_path, monkeypatch):
        """After a checkpoint the file is byte-immutable, so lint debt that got
        in is uncurable without an operator."""
        from spec_runner.tdd import RedOutcome as RO
        from spec_runner.tdd import run_red_phase

        root = _repo(tmp_path)
        cfg = _cfg(root, test_command="python -m pytest", lint_command="false")
        self._agent(
            monkeypatch,
            output="TDD_SELECTOR: tests/test_x.py::test_y",
            writes={"tests/test_x.py": "def test_y():\n    assert False\n"},
        )
        with ExecutorState(cfg) as state:
            result = run_red_phase(self._task(), cfg, state)
            assert state.active_claims(resolve_namespace(cfg)) == []
            assert state.red_checkpoint("TASK-001", resolve_namespace(cfg)) is None
        assert result.outcome is RO.UNVERIFIABLE
        assert "byte-immutable" in (result.detail or "")

    def test_a_passing_lint_does_not_block(self, tmp_path, monkeypatch):
        from spec_runner.tdd import RedOutcome as RO
        from spec_runner.tdd import run_red_phase

        root = _repo(tmp_path)
        cfg = _cfg(root, test_command="python -m pytest", lint_command="true")
        self._agent(
            monkeypatch,
            output="TDD_SELECTOR: tests/test_x.py::test_y",
            writes={"tests/test_x.py": "def test_y():\n    assert False\n"},
        )
        with ExecutorState(cfg) as state:
            result = run_red_phase(self._task(), cfg, state)
        assert result.outcome is RO.EXPECTED_FAIL


class TestFailingClosed:
    """Three places where "we could not find out" must not read as "all clear".
    A byte-lock that silently does not exist is worse than no byte-lock, because
    the run believes it is there."""

    def test_a_claim_that_cannot_be_persisted_raises(self, tmp_path):
        import sqlite3

        root = _repo(tmp_path)
        cfg = _cfg(root)
        with ExecutorState(cfg) as state:
            state._conn.execute("DROP TABLE tdd_claims")
            with pytest.raises(sqlite3.Error):
                state.record_claim(
                    Claim(
                        namespace="ns",
                        task_id="TASK-001",
                        checkpoint_id="cp",
                        checkpoint_sha="a" * 40,
                        path="tests/test_x.py",
                        blob_sha="b" * 40,
                        created_at="2026-08-11T00:00:00",
                    )
                )

    def test_an_unreadable_candidate_commit_is_not_all_clear(self, tmp_path):
        from spec_runner.claims import ClaimCheckError

        root = _repo(tmp_path)
        cfg = _cfg(root)
        sha = _commit(root, {"tests/test_x.py": "a\n"})
        with ExecutorState(cfg) as state:
            record_claims(cfg, state, _checkpoint(cfg, sha))
            with pytest.raises(ClaimCheckError):
                check_claims(cfg, state, resolve_namespace(cfg), "0" * 40)

    def test_the_gate_reports_that_as_an_instrument_error(self, tmp_path):
        from spec_runner.gates import (
            GateContext,
            GateRegistry,
            GateStatus,
            evaluate_gates,
            register_builtin_gates,
        )

        root = _repo(tmp_path)
        cfg = _cfg(root)
        sha = _commit(root, {"tests/test_x.py": "a\n"})
        registry = GateRegistry()
        register_builtin_gates(cfg, registry=registry)
        with ExecutorState(cfg) as state:
            record_claims(cfg, state, _checkpoint(cfg, sha))
            state.record_red_checkpoint(_checkpoint(cfg, sha))
            ctx = GateContext(
                task_id="TASK-001",
                checkpoint_sha="0" * 40,
                config=cfg,
                state=state,
                facts={"execution_mode": "tdd"},
            )
            outcome = evaluate_gates("tests", ctx, registry=registry)
        assert outcome.status is not GateStatus.SATISFIED
        assert any(r.status is GateStatus.INSTRUMENT_ERROR for r in outcome.results)


def _parse(raw: str, runner: str = "pytest"):
    """Parse a selector the way the pipeline does — through an adapter."""
    from spec_runner.tdd_runners import Selector, adapter_for

    adapter = adapter_for(runner)
    assert adapter is not None
    parsed = adapter.parse_selector(raw)
    return parsed if isinstance(parsed, Selector) else None


def _sel(raw: str = "tests/test_x.py::test_y", runner: str = "pytest"):
    parsed = _parse(raw, runner)
    assert parsed is not None, raw
    return parsed


class TestClaimPathsAreTheAdaptersBusiness:
    """#210, found by the second paid pilot run. `claim_paths_for` split the
    raw string on `::` and returned `[]` for anything else, so a valid ExUnit
    `path:line` claimed nothing and a **confirmed** red was discarded.

    It now takes the parsed `Selector` — the object the config's adapter
    produced — so the shape is read once, by one authority. Passing the raw
    string and re-deriving the adapter here is what the owner's review of
    PR #211 refused: today `::` and `:line` do not overlap, but a third adapter
    could make one string parse under two, and the order of `ADAPTERS` would
    quietly become semantics of the byte-lock.
    """

    def test_a_pytest_node_id_claims_its_file(self):
        from spec_runner.claims import claim_paths_for

        assert claim_paths_for(_sel("tests/test_x.py::TestY::test_z")) == ["tests/test_x.py"]

    def test_an_exunit_line_selector_claims_its_file(self):
        from spec_runner.claims import claim_paths_for

        selector = _sel("test/kapelle/providers/catalog_test.exs:85", "exunit")
        assert claim_paths_for(selector) == ["test/kapelle/providers/catalog_test.exs"]

    def test_the_selector_carries_its_own_runner(self):
        """No inference at the claim site: the object says which adapter made
        it, and that adapter answers."""
        assert _sel("test/x_test.exs:12", "exunit").runner == "exunit"
        assert _parse("test/x_test.exs:12", "pytest") is None

    def test_a_selector_from_an_unregistered_runner_claims_nothing(self):
        """The fail-closed half: a red with nothing locked would pass the gate
        over an open file."""
        from dataclasses import replace

        from spec_runner.claims import claim_paths_for

        assert claim_paths_for(replace(_sel(), runner="rspec")) == []

    @pytest.mark.parametrize("raw", ["garbage", "tests/test_x.py", "", "::test_y"])
    def test_nonsense_never_becomes_a_selector_at_all(self, raw):
        assert _parse(raw) is None


class TestTheAdapterContract:
    """The machine contract, kept apart from the prompt text (owner's review of
    PR #211): rewording an instruction must not be able to change what is
    guaranteed, and a guarantee must not depend on a sentence being greppable.
    """

    def test_every_adapters_canonical_selectors_parse_and_claim(self):
        from spec_runner.claims import claim_paths_for
        from spec_runner.tdd_runners import ADAPTERS, Selector

        for name, adapter in ADAPTERS.items():
            assert adapter.contract_selectors(), f"{name} declares no contract selectors"
            for raw in adapter.contract_selectors():
                parsed = adapter.parse_selector(raw)
                assert isinstance(parsed, Selector), f"{name}: {raw!r} does not parse"
                assert claim_paths_for(parsed), f"{name}: {raw!r} claims nothing"

    def test_no_adapter_accepts_anothers_canonical_selector(self):
        """Not required by the contract, but true today — and if it ever stops
        being true, the claim site must be reading `selector.runner` rather
        than guessing, which is exactly what #210 was about."""
        from spec_runner.tdd_runners import ADAPTERS, Selector

        for name, adapter in ADAPTERS.items():
            for other, foreign in ADAPTERS.items():
                if other == name:
                    continue
                for raw in foreign.contract_selectors():
                    assert not isinstance(adapter.parse_selector(raw), Selector), (
                        f"{name} accepts {other}'s selector {raw!r}"
                    )
