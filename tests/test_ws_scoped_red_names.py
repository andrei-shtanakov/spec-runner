"""BEH-12, BEH-13, BEH-20, BEH-21, BEH-22 (spec-runner#341, TASK-009).

Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md. `checked_by`
targets all five behaviours at this file — BEH-12 as `kind: integration`,
the rest as `kind: contract`:

- **BEH-12**: two workstreams sharing a task-id get non-overlapping
  evidential paths, and freeze/claim independently of one another.
- **BEH-13**: the namespace segment is present always — a readable slug when
  `tdd_namespace` is declared, a short digest when it is computed.
- **BEH-20**: the path is a pure, deterministic function of
  (adapter, task-id, namespace) — stable between attempts and processes.
- **BEH-21**: normalisation never collapses two distinct namespaces, and the
  distinction survives a case-fold of the whole path.
- **BEH-22**: the namespace segment stays within its declared length limit
  and stays human-readable for a declared namespace.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import RedOutcome, resolve_namespace, run_red_phase
from spec_runner.tdd_runners import (
    ADAPTERS,
    NAMESPACE_SLUG_MAX_LEN,
    PytestAdapter,
    namespace_segment,
)

FAILING = "def test_new_behaviour():\n    assert False\n"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path, *, namespace: str = "", name: str = "repo") -> ExecutorConfig:
    root = tmp_path / name
    (root / "tests").mkdir(parents=True, exist_ok=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "o@e.c")
    _git(root, "config", "user.name", "O")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        execution_mode="tdd",
        test_command="python -m pytest",
        lint_command="",
        tdd_namespace=namespace,
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(task_id: str = "TASK-001") -> Task:
    return Task(id=task_id, name="t", priority="p1", status="todo", estimate="1h")


def _agent_writing_to(monkeypatch, path: PurePosixPath):
    """A RED pass that writes exactly the path its own workstream was named —
    the way a real agent complies with the prompt's `_evidential_file`."""
    from spec_runner import tdd

    def _red(config, prompt, **kwargs):
        target = Path(config.project_root) / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(FAILING)
        return tdd.AgentCall(text=f"TDD_SELECTOR: {path}::test_new_behaviour")

    monkeypatch.setattr(tdd, "_run_agent", _red)


class TestBEH12TwoWorkstreamsNonOverlappingPaths:
    """BEH-12 (integration): two workstreams, same task-id, must not collide."""

    def test_two_namespaces_get_non_overlapping_paths_for_every_adapter(self):
        for adapter in ADAPTERS.values():
            path_alpha = adapter.evidential_file("TASK-001", namespace="ws-alpha")
            path_beta = adapter.evidential_file("TASK-001", namespace="ws-beta")

            assert path_alpha != path_beta

    @pytest.mark.slow
    def test_two_workstreams_freeze_and_claim_independently(self, tmp_path, monkeypatch):
        """The full loop, one shared checkout: each workstream's RED pass
        writes its own file, confirms its own checkpoint, and claims only its
        own path — a claim recorded for one namespace is invisible to the
        other, and does not freeze the other's file."""
        adapter = PytestAdapter()
        cfg_alpha = _repo(tmp_path, namespace="ws-alpha")
        # A second workstream sharing the same checkout and state.
        cfg_beta = ExecutorConfig(
            project_root=cfg_alpha.project_root,
            state_file=cfg_alpha.state_file,
            logs_dir=cfg_alpha.logs_dir,
            execution_mode="tdd",
            test_command="python -m pytest",
            lint_command="",
            tdd_namespace="ws-beta",
        )

        ns_alpha = resolve_namespace(cfg_alpha)
        ns_beta = resolve_namespace(cfg_beta)
        path_alpha = adapter.evidential_file("TASK-001", namespace=ns_alpha)
        path_beta = adapter.evidential_file("TASK-001", namespace=ns_beta)
        assert path_alpha != path_beta

        with ExecutorState(cfg_alpha) as state:
            _agent_writing_to(monkeypatch, path_alpha)
            result_alpha = run_red_phase(_task(), cfg_alpha, state)

        with ExecutorState(cfg_beta) as state:
            _agent_writing_to(monkeypatch, path_beta)
            result_beta = run_red_phase(_task(), cfg_beta, state)

            assert result_alpha.outcome is RedOutcome.EXPECTED_FAIL
            assert result_beta.outcome is RedOutcome.EXPECTED_FAIL
            assert result_alpha.checkpoint is not None
            assert result_beta.checkpoint is not None
            assert result_alpha.checkpoint.namespace != result_beta.checkpoint.namespace

            alpha_claims = {c.path for c in state.active_claims(ns_alpha)}
            beta_claims = {c.path for c in state.active_claims(ns_beta)}

            assert alpha_claims == {str(path_alpha)}
            assert beta_claims == {str(path_beta)}
            # Neither workstream's lock reaches into the other's file.
            assert alpha_claims.isdisjoint(beta_claims)


class TestBEH13NamespaceSegmentAlwaysPresent:
    """BEH-13 (contract): a slug when declared, a digest when computed —
    but never absent."""

    def test_declared_namespace_produces_a_recognisable_slug(self):
        adapter = PytestAdapter()

        path = adapter.evidential_file("TASK-001", namespace="ws-alpha")

        assert "ws_alpha" in path.name

    def test_computed_namespace_still_yields_a_segment(self, tmp_path):
        """No `tdd_namespace` declared: `resolve_namespace` computes a digest
        from the project root, and the adapter still names a distinguishing
        segment from it — the guarantee does not depend on declaration."""
        cfg = _repo(tmp_path, namespace="")
        adapter = PytestAdapter()

        computed = resolve_namespace(cfg)
        assert computed  # never empty — resolve_namespace always answers
        path = adapter.evidential_file("TASK-001", namespace=computed)

        segment = namespace_segment(computed)
        assert segment in path.name
        assert segment  # the segment itself is never empty

    def test_the_segment_is_never_absent_across_both_forms(self):
        for namespace in ("ws-alpha", ""):
            for adapter in ADAPTERS.values():
                path = adapter.evidential_file("TASK-001", namespace=namespace)

                assert namespace_segment(namespace) in path.name


class TestBEH20PathIsDeterministicAndStable:
    """BEH-20 (contract): a pure function of (adapter, task-id, namespace)."""

    def test_repeated_calls_for_the_same_task_agree(self):
        adapter = PytestAdapter()

        first = adapter.evidential_file("TASK-001", namespace="ws-alpha")
        second = adapter.evidential_file("TASK-001", namespace="ws-alpha")

        assert first == second

    def test_independent_of_environment_variables(self, monkeypatch):
        adapter = PytestAdapter()
        before = adapter.evidential_file("TASK-001", namespace="ws-alpha")

        monkeypatch.setenv("SPEC_RUNNER_TEST_NOISE", "whatever-changes-per-run")
        monkeypatch.setenv("HOSTNAME", "some-other-machine")
        after = adapter.evidential_file("TASK-001", namespace="ws-alpha")

        assert before == after

    def test_stable_across_a_fresh_process(self):
        """Not just "the same object answers twice" — a brand-new
        interpreter, given the same inputs, must compute the same path."""
        adapter = PytestAdapter()
        in_process = adapter.evidential_file("TASK-001", namespace="ws-alpha")

        script = (
            "from spec_runner.tdd_runners import PytestAdapter; "
            "print(PytestAdapter().evidential_file('TASK-001', namespace='ws-alpha'))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, check=True
        )

        assert completed.stdout.strip() == str(in_process)


class TestBEH21NormalisationPreservesDistinctness:
    """BEH-21 (contract): case, separator, and long-tail differences must
    not collapse two namespaces into one segment — including after a
    case-fold of the whole path, since APFS/NTFS treat case-only-different
    paths as the same file."""

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            pytest.param("ws-Alpha", "ws-alpha", id="case"),
            pytest.param("ws-alpha", "ws.alpha", id="separator"),
            pytest.param(
                "ws-shared-prefix-" + "a" * 40,
                "ws-shared-prefix-" + "b" * 40,
                id="long-tail",
            ),
        ],
    )
    def test_close_namespaces_produce_different_paths(self, left, right):
        adapter = PytestAdapter()

        path_left = adapter.evidential_file("TASK-001", namespace=left)
        path_right = adapter.evidential_file("TASK-001", namespace=right)

        assert path_left != path_right
        # The distinction survives a case-fold of the whole path — the
        # guarantee a case-insensitive filesystem actually enforces.
        assert str(path_left).lower() != str(path_right).lower()


class TestBEH22SegmentStaysReadableAndBounded:
    """BEH-22 (contract): bounded against the declared limit, not a literal
    baked into this test."""

    def test_declared_namespace_of_normal_length_is_readable(self):
        segment = namespace_segment("ws-alpha")

        assert "ws_alpha" in segment
        assert len(segment) <= NAMESPACE_SLUG_MAX_LEN + 1 + 8  # slug + "_" + digest

    def test_an_intentionally_long_namespace_is_capped(self):
        long_namespace = "ws-" + "x" * 200

        segment = namespace_segment(long_namespace)
        slug, _, digest = segment.rpartition("_")

        assert len(slug) <= NAMESPACE_SLUG_MAX_LEN
        assert len(digest) == 8

    def test_the_final_path_stays_well_under_filesystem_limits(self):
        adapter = PytestAdapter()
        long_namespace = "ws-" + "x" * 200

        path = adapter.evidential_file("TASK-001", namespace=long_namespace)

        # 255 bytes is the common filename-component limit (ext4, APFS, NTFS).
        assert len(path.name.encode()) < 255
