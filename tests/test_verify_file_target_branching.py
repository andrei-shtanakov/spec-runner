"""BEH-17, BEH-24 (TASK-008, DT-08).

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-17
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-24
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/30-decomposition.md#DT-08
Traces: FR-11, FR-17

TDD-waiver (class: characterisation, sanction: batch-approve-2026-09-09).
All five conditions of the class, confirmed:

- the behaviour is already delivered by this task's own dependency (TASK-003/
  DT-03: `live_verify._resolve_file_target_triplet` + `classify_verify_outcome`
  folding a file target's per-member manifest into `green`/`test_failure`/
  `instrument_error`, and `execution.py`'s branching on that outcome) —
  `tests/test_verify_file_target_outcomes.py` already pins BEH-10..BEH-16 on
  the same fold;
- this task adds the missing characterisation coverage named BEH-17 (a failed
  or errored member is a genuine `test_failure`, not `instrument_error`, and
  drives the unmodified TDD cycle) and BEH-24 (the outcome space stays exactly
  the three #367 delivered, unmodified by a file target);
- an honest baseline RED is not possible: the classifier and the branching it
  drives already exist and already pass, so writing this file against
  unmodified `main` cannot fail without first reverting delivered code — which
  is not this task's job (`gates.py`, `hooks.py`, `execution.py`, `lifecycle.py`
  are untouched by this task; `git diff` on those four files stays empty, not
  asserted by any test here);
- every claim below carries a negative control that flips the observed
  outcome under a deliberately violated property, proving the assertion
  actually discriminates rather than passing vacuously. Per class, the
  violated property and where it is applied:
  - BEH-17 genuine failure — the classifier is regressed to fold a failure
    into `instrument_error` (`classify_verify_outcome`), and a real fixture
    whose assertion is then fixed flips to green;
  - BEH-17 TDD cycle — the entry verdict is made `green`, which is exactly
    what `execution.py`'s `verify_first_red` keys on, and the red-authoring
    call and checkpoint must both disappear;
  - BEH-24 exhaustiveness — the FOLD itself is regressed on its
    `TESTS_FAILED + PROVEN` arm, and the table's rows must move off their
    expected outcomes. (The earlier control removed rows from this file's own
    table and observed the set shrink — true whatever the fold does, so it
    could not have caught the one regression BEH-24 exists to catch.)
  - BEH-24 parity — `_resolve_file_target_triplet`, which is the file
    target's own fold and is not on the node-id path, is made to disagree,
    and the two kinds must become distinguishable;
  - BEH-18 mixed group — the two tests are each other's control: same group
    shape, one member's assertion flipped, the group's named outcome moves
    with it;
- baseline commit at the start of this task: `c4fe0a28f4ecc9cb69ae53c7c634ac9c2755007a`.

`tests/test_verify_branching.py` (WS-spec-runner-367's own file) is not
touched or duplicated here: it carries that workstream's BEH-20/21/22 for a
node-id group, and none of this workstream's BEH-17/BEH-24 is a line in it
(DT-08's own boundary). This file owns the file-target case beside it.

Scope of BEH-24 in this file, stated rather than implied: the `ROWS` table
is a representative subset of BEH-12's composition table, not the whole of
it. The remaining rows (plain green, `error`, `xpassed`, `deselected`,
fail+unaccounted) and every BEH-10 refusal class are pinned by TASK-003's
own file, `tests/test_verify_file_target_outcomes.py` (BEH-10..BEH-16), on
the same fold this file exercises — duplicating them here would be a second
copy of one contract, not more coverage. What was genuinely missing and is
added here is a MIXED group (a file target and a node id in one
declaration, BEH-18) driven through `run_live_verify`: no test ran one
before, and `_TEST_FAILURE_MIXED` is a mixed composition of a single file,
which is a different thing.

Two kinds, per the behaviour spec's own `checked_by` lines:
- `kind: integration` (BEH-17) — a real git/pytest replay against a fixture
  repo, mirroring that file's `TestBEH21TestFailureEntersTheOrdinaryTddCycle`
  but for a **file target**.
- `kind: contract` (BEH-24) — exhaustive-outcome and target-kind-parity checks.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import spec_runner.live_verify as live_verify_module
from spec_runner import tdd
from spec_runner.claims import ClaimStatus, check_claims
from spec_runner.config import ExecutorConfig
from spec_runner.executor import execute_task
from spec_runner.live_verify import (
    ExecutionProof,
    RunOutcome,
    SelectionProof,
    VerifyOutcome,
    _resolve_file_target_triplet,
    classify_verify_outcome,
    run_live_verify,
)
from spec_runner.runner import CliInvocation
from spec_runner.state import ErrorCode, ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import AgentCall, resolve_namespace
from spec_runner.tdd_runners import FileComposition


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _init_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests").mkdir()
    return root


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    )
    return head.stdout.strip()


def _cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "test_command": "python -m pytest",
        "max_retries": 1,
        "retry_delay_seconds": 0,
        "create_git_branch": False,
        "run_tests_on_done": False,
        "auto_commit": True,
        "run_review": False,
        "callback_url": "",
        "lint_command": "",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(**overrides) -> Task:
    defaults: dict = {
        "id": "TASK-401",
        "name": "verify-first file target",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
        "execution_mode": "verify_first",
    }
    defaults.update(overrides)
    return Task(**defaults)


def _fake_red_agent(config, prompt, **kwargs):
    """Stand-in RED-authoring pass, mirroring the frozen BEH-21 test."""
    red_test = Path(config.project_root) / "tests" / "test_red_authored.py"
    red_test.write_text("def test_red_authored():\n    assert False, 'red'\n")
    return AgentCall(
        text="TDD_SELECTOR: tests/test_red_authored.py::test_red_authored\nTASK_COMPLETE"
    )


class TestBEH17FailedMemberIsGenuineFailure:
    """kind: integration — BEH-17: each of the three declared inputs (a
    failed assertion among many, a genuine exception among many, a mixed
    pass/fail/skip composition) reads as `test_failure`, never
    `instrument_error` — a mixed composition does not get read as unproven
    just because part of it also skipped."""

    def test_one_assertion_failure_among_many_is_test_failure(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_a():\n    assert True\n\n\n"
            "def test_b():\n    assert False, 'not implemented'\n"
        )
        _commit(root, "base")
        task = _task(verifies=["tests/test_group.py"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.TEST_FAILURE
        assert result.ran and not result.passed

    def test_negative_control_fixing_the_assertion_flips_outcome_to_green(self, tmp_path):
        """The other side of the previous claim: the identical file with the
        failing assertion fixed is `green`, proving the prior assertion is
        sensitive to the actual member outcome, not a fixed label."""
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True  # fixed\n"
        )
        _commit(root, "base")
        task = _task(verifies=["tests/test_group.py"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.GREEN

    def test_one_genuine_exception_among_many_is_test_failure(self, tmp_path):
        """A fixture-setup exception — not an assertion in the test body —
        still folds into the same `test_failure` bucket. Note: pytest's own
        `TestReport.outcome` (read verbatim by `pytest_runtest_logreport` in
        `tdd_runners.py`) only ever takes `passed`/`failed`/`skipped` — a
        setup-phase exception records `outcome == "failed"` too, the same
        string a body-assertion failure records. "ERROR" is purely a
        terminal-display label pytest's own reporter applies for non-`call`
        phase failures; it is never the value written into the manifest.
        The `("failed", "error")` tuple check at `live_verify.py:184` covers
        a value this pytest adapter never actually emits — this test does
        not exercise that `"error"` branch, only confirms a setup-phase
        failure is read as `"failed"`, same as a body assertion."""
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "import pytest\n\n\n"
            "@pytest.fixture\n"
            "def broken():\n"
            "    raise RuntimeError('setup blew up')\n\n\n"
            "def test_a():\n    assert True\n\n\n"
            "def test_b(broken):\n    assert True\n"
        )
        _commit(root, "base")
        task = _task(verifies=["tests/test_group.py"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.TEST_FAILURE
        assert result.ran and not result.passed

    def test_mixed_pass_fail_skip_composition_is_test_failure(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "import pytest\n\n\n"
            "def test_pass():\n    assert True\n\n\n"
            "def test_fail():\n    assert False, 'genuine failure'\n\n\n"
            "@pytest.mark.skip(reason='not applicable here')\n"
            "def test_skip():\n    assert True\n"
        )
        _commit(root, "base")
        task = _task(verifies=["tests/test_group.py"])
        config = _cfg(root)

        result = run_live_verify(task, config)

        assert result.outcome is VerifyOutcome.TEST_FAILURE, (
            "a mixed composition with a real failure must stay test_failure "
            f"even with a skip also present: {result.detail}"
        )

    def test_negative_control_a_classifier_regression_would_be_caught(self, tmp_path, monkeypatch):
        """Mutation-kill negative control: simulate the violated property (a
        regressed classifier that folds a genuine failure into
        `instrument_error`) and confirm the observed outcome actually
        changes — the assertions above are not vacuously true regardless of
        what the classifier does."""
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_a():\n    assert True\n\n\n"
            "def test_b():\n    assert False, 'not implemented'\n"
        )
        _commit(root, "base")
        task = _task(verifies=["tests/test_group.py"])
        config = _cfg(root)

        # Sanity check restated: the real, unmodified classifier reads this
        # as test_failure (BEH-17's claim).
        assert run_live_verify(task, config).outcome is VerifyOutcome.TEST_FAILURE

        monkeypatch.setattr(
            live_verify_module,
            "classify_verify_outcome",
            lambda *a, **k: VerifyOutcome.INSTRUMENT_ERROR,
        )
        regressed = run_live_verify(task, config)

        assert regressed.outcome is VerifyOutcome.INSTRUMENT_ERROR, (
            "negative control: the monkeypatched regression must actually "
            "change the observed outcome, proving BEH-17's assertion would "
            "have reddened had the property been violated"
        )


class TestBEH17EntersTheUnmodifiedTddCycle:
    """kind: integration — BEH-17's second clause: a failed file-target
    composition does not merely classify as `test_failure` — it walks the
    SAME unmodified TDD cycle #367 built for a failed node-id group:
    RED-authoring call, a confirmed red checkpoint, a recorded claim released
    at completion, and claims/gates that behave exactly as documented for
    that branch. Mirrors `tests/test_verify_branching.py`'s
    `TestBEH21TestFailureEntersTheOrdinaryTddCycle` +
    `TestBEH21ClaimsReleaseAtCompletion`, substituting a file target for that
    file's node-id group."""

    @patch("spec_runner.execution.update_task_status")
    @patch("spec_runner.execution.log_progress")
    @patch(
        "spec_runner.execution.build_cli_invocation",
        return_value=CliInvocation(["echo", "hi"], "text"),
    )
    @patch("spec_runner.execution.build_task_prompt", return_value="test prompt")
    @patch(
        "spec_runner.execution.post_done_hook",
        return_value=(True, None, "skipped", "", False),
    )
    @patch("spec_runner.execution.pre_start_hook", return_value=True)
    @patch("spec_runner.execution._run_agent_process")
    def test_a_failing_file_target_walks_red_authoring_claims_and_completes(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        tmp_path,
        monkeypatch,
    ):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_a():\n    assert True\n\n\n"
            "def test_b():\n    assert False, 'not implemented'\n"
        )
        _commit(root, "base")

        mock_run.return_value = MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)

        red_calls: list[str] = []

        def _fake_red_agent_recording(config, prompt, **kwargs):
            red_calls.append("red_authoring")
            return _fake_red_agent(config, prompt, **kwargs)

        monkeypatch.setattr(tdd, "_run_agent", _fake_red_agent_recording)

        task = _task(verifies=["tests/test_group.py"])
        config = _cfg(root)
        with ExecutorState(config) as state:
            result = execute_task(task, config, state)

            namespace = resolve_namespace(config)
            evidence = state.verify_evidence(namespace, task.id)
            assert evidence is not None
            assert evidence.outcome == "test_failure", (
                "BEH-17: a failed file target must read as test_failure, never instrument_error"
            )
            assert red_calls, (
                "BEH-17: a failed file target must walk the ordinary "
                "red-authoring cycle, not skip straight to the implementation call"
            )
            checkpoint = state.red_checkpoint(task.id, namespace)
            assert checkpoint is not None, (
                "BEH-17: the red-authoring pass must leave a confirmed red checkpoint"
            )

            history = [h["phase"] for h in state.tdd_phase_history(task.id, namespace)]
            claims = state.claims_for(namespace, task.id)
            assert claims, "the red-authoring pass must have claimed a file"
            assert "done" in history, (
                "a failed file-target task that walked the red cycle must leave "
                "a terminal lifecycle row, exactly as the node-id branch does"
            )
            assert [row[3] for row in claims] == [ClaimStatus.RELEASED.value] * len(claims), (
                "the claim on the authored red file must be released at "
                "completion, the same way the node-id branch's is (#260)"
            )

            # The measured consequence, mirrored from #260/#381: a later,
            # legitimate commit touching the claimed file must not be
            # refused by a claim that outlived the task it was protecting.
            (root / "tests" / "test_red_authored.py").write_text(
                "def test_red_authored():\n    assert True  # fixed legitimately\n"
            )
            candidate = _commit(root, "a later legitimate edit")
            violations = check_claims(config, state, namespace, candidate)

        assert result is True
        assert violations == [], (
            "a completed file-target task's stale claim must not block a "
            f"later legitimate edit to the same file, got {violations}"
        )

    @patch("spec_runner.execution.update_task_status")
    @patch("spec_runner.execution.log_progress")
    @patch(
        "spec_runner.execution.build_cli_invocation",
        return_value=CliInvocation(["echo", "hi"], "text"),
    )
    @patch("spec_runner.execution.build_task_prompt", return_value="test prompt")
    @patch(
        "spec_runner.execution.post_done_hook",
        return_value=(True, None, "skipped", "", False),
    )
    @patch("spec_runner.execution.pre_start_hook", return_value=True)
    @patch("spec_runner.execution._run_agent_process")
    def test_negative_control_a_green_entry_would_not_walk_the_red_cycle(
        self,
        mock_run,
        mock_pre,
        mock_post,
        mock_prompt,
        mock_cmd,
        mock_log,
        mock_status,
        tmp_path,
        monkeypatch,
    ):
        """Negative control for the claim above, on the branch that carries it.

        The assertions above (`red_calls`, a confirmed checkpoint, a released
        claim) say "a file target read as `test_failure` walks the ordinary
        cycle". They would be vacuous if the same observations appeared for
        any outcome. So the property is violated exactly where the code
        decides it — `execution.py`'s `verify_first_red`, which is true only
        for `VerifyOutcome.TEST_FAILURE` — by making the entry run classify
        the very same failing composition as `green`.

        Under the violation every observation must flip: no red-authoring
        call, no checkpoint, no claim. That is what proves the assertions
        discriminate rather than describing whatever happened to occur.
        """
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_a():\n    assert True\n\n\n"
            "def test_b():\n    assert False, 'not implemented'\n"
        )
        _commit(root, "base")

        mock_run.return_value = MagicMock(stdout="output TASK_COMPLETE", stderr="", returncode=0)

        red_calls: list[str] = []

        def _fake_red_agent_recording(config, prompt, **kwargs):
            red_calls.append("red_authoring")
            return _fake_red_agent(config, prompt, **kwargs)

        monkeypatch.setattr(tdd, "_run_agent", _fake_red_agent_recording)
        # The violated property: the same failing composition now folds to
        # `green`, so `verify_first_red` is false and the red branch is not
        # entered at all.
        monkeypatch.setattr(
            live_verify_module,
            "classify_verify_outcome",
            lambda *a, **k: VerifyOutcome.GREEN,
        )

        task = _task(id="TASK-190", verifies=["tests/test_group.py"])
        config = _cfg(root, state_file=root / ".state-control.db")
        with ExecutorState(config) as state:
            execute_task(task, config, state)

            namespace = resolve_namespace(config)
            evidence = state.verify_evidence(namespace, task.id)
            assert evidence is not None and evidence.outcome == "green", (
                "control setup: the violated property must actually change the "
                "entry verdict, or the control proves nothing"
            )
            assert red_calls == [], (
                "negative control: with the outcome flipped to green there is "
                "no red-authoring call — so the claim above was reading the "
                "test_failure branch, not something true regardless"
            )
            assert state.red_checkpoint(task.id, namespace) is None, (
                "negative control: no confirmed red is recorded when the entry verdict is green"
            )
            # Claims are deliberately NOT asserted empty here, and the
            # control found the reason: a green entry freezes its declared
            # group before the paid call (#367 BEH-26/FR-19), so claims exist
            # on both sides and do not discriminate between the branches. The
            # two observations that do — the red-authoring call and the
            # checkpoint — are the ones asserted above.


class TestBEH24ThreeOutcomesExhaustive:
    """kind: contract — BEH-24 over the COMPOSITION space: whatever the
    per-member manifest looks like, the observable outcome is exactly one of
    `green`/`test_failure`/`instrument_error`, and all three are reachable.

    BEH-10's refusal classes are NOT exercised here, and saying they were
    was wrong: a `SelectorRefusal` is raised before `_resolve_file_target_triplet`
    is reached at all, so no row of this table can express one. They are
    pinned where they happen — `tests/test_verify_file_target_outcomes.py`,
    `TestBEH10FileTargetStopsBeforeAnyPaidCall`.
    """

    _GREEN_PARTIAL_SKIP = FileComposition(("a", "b"), {"a": "passed", "b": "skipped"}, True)
    _TEST_FAILURE_ONE_FAILED = FileComposition(("a", "b"), {"a": "passed", "b": "failed"}, True)
    _TEST_FAILURE_MIXED = FileComposition(
        ("a", "b", "c"), {"a": "passed", "b": "failed", "c": "skipped"}, True
    )
    _INSTRUMENT_ERROR_EMPTY = FileComposition((), {}, True)
    _INSTRUMENT_ERROR_INCOMPLETE = FileComposition(("a",), {"a": "passed"}, False)
    _INSTRUMENT_ERROR_UNACCOUNTED = FileComposition(("a", "b"), {"a": "passed"}, True)
    _INSTRUMENT_ERROR_ALL_SKIPPED = FileComposition(
        ("a", "b"), {"a": "skipped", "b": "skipped"}, True
    )

    ROWS: list[tuple[str, FileComposition, VerifyOutcome]] = [
        ("green-partial-skip", _GREEN_PARTIAL_SKIP, VerifyOutcome.GREEN),
        ("test_failure-one-failed", _TEST_FAILURE_ONE_FAILED, VerifyOutcome.TEST_FAILURE),
        ("test_failure-mixed-with-skip", _TEST_FAILURE_MIXED, VerifyOutcome.TEST_FAILURE),
        ("instrument_error-empty", _INSTRUMENT_ERROR_EMPTY, VerifyOutcome.INSTRUMENT_ERROR),
        (
            "instrument_error-incomplete",
            _INSTRUMENT_ERROR_INCOMPLETE,
            VerifyOutcome.INSTRUMENT_ERROR,
        ),
        (
            "instrument_error-unaccounted",
            _INSTRUMENT_ERROR_UNACCOUNTED,
            VerifyOutcome.INSTRUMENT_ERROR,
        ),
        (
            "instrument_error-all-skipped",
            _INSTRUMENT_ERROR_ALL_SKIPPED,
            VerifyOutcome.INSTRUMENT_ERROR,
        ),
    ]

    @pytest.mark.parametrize("case_id,composition,expected", ROWS)
    def test_row_lands_on_the_expected_named_outcome(self, case_id, composition, expected):
        outcome = classify_verify_outcome(*_resolve_file_target_triplet(composition))
        assert outcome is expected

    def test_all_three_outcomes_are_actually_reachable(self):
        seen = {
            classify_verify_outcome(*_resolve_file_target_triplet(composition))
            for _, composition, _expected in self.ROWS
        }
        assert seen == {
            VerifyOutcome.GREEN,
            VerifyOutcome.TEST_FAILURE,
            VerifyOutcome.INSTRUMENT_ERROR,
        }

    def test_negative_control_the_table_discriminates_a_regressed_fold(self):
        """Negative control: the expectations above tell a healthy fold from
        a regressed one.

        The previous version patched `lv.classify_verify_outcome` and then
        called `lv.classify_verify_outcome` — i.e. it called its own patch,
        whose first arm returned `INSTRUMENT_ERROR` before ever delegating.
        Both of its assertions held by construction of that closure, on
        healthy code and on regressed code alike, and it stayed green under
        a real source mutation of the arm. A control that cannot fail is the
        same defect as a claim that cannot fail.

        This version patches nothing. It runs the rows through the REAL
        function and through a regressed copy of it, and requires the two to
        disagree — which is what "these expectations would have caught the
        regression" means. That the production function is the one the
        positive tests read is shown by mutating the source: with
        `TESTS_FAILED + PROVEN` folded to `INSTRUMENT_ERROR`,
        `test_row_lands_on_the_expected_named_outcome` reddens on both
        test_failure rows (recorded in the commit that added this).
        """

        def _regressed_copy(run_outcome, proof, execution):
            # The regression BEH-24 exists to catch, written out rather than
            # injected: a genuine failure reported as an instrument that
            # could not tell. Every other arm copies the real contract.
            if run_outcome is RunOutcome.TESTS_FAILED and proof is SelectionProof.PROVEN:
                return VerifyOutcome.INSTRUMENT_ERROR
            if (
                run_outcome is RunOutcome.TESTS_PASSED
                and proof is SelectionProof.PROVEN
                and execution is ExecutionProof.EXECUTED
            ):
                return VerifyOutcome.GREEN
            return VerifyOutcome.INSTRUMENT_ERROR

        disagreements = []
        for case_id, composition, expected in self.ROWS:
            triplet = _resolve_file_target_triplet(composition)
            real = classify_verify_outcome(*triplet)
            regressed = _regressed_copy(*triplet)
            assert real is expected, (
                f"control setup: {case_id} must land on {expected} under the real fold, got {real}"
            )
            if regressed is not real:
                disagreements.append((case_id, real, regressed))

        assert disagreements, (
            "negative control: no row distinguishes the real fold from one "
            "that folds a genuine failure into instrument_error — the table "
            "would pass unchanged through that regression"
        )
        assert all(
            regressed is VerifyOutcome.INSTRUMENT_ERROR and real is VerifyOutcome.TEST_FAILURE
            for _case_id, real, regressed in disagreements
        ), f"the disagreement must be exactly the regressed arm, got {disagreements}"


class TestBEH24FileTargetAndNodeIdBranchIdentically:
    """kind: integration — BEH-24's closing clause: none of the three
    branches is overridden by a file target — for the same outcome,
    behaviour on a file target and on the equivalent node id is
    indistinguishable through `execute_task`."""

    def test_green_reaches_done_without_red_authoring_for_both_target_kinds(
        self, tmp_path, monkeypatch
    ):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")

        for task_id, verifies in (
            ("TASK-401", ["tests/test_group.py"]),
            ("TASK-402", ["tests/test_group.py::test_it"]),
        ):
            red_agent = MagicMock(
                side_effect=AssertionError("BEH-24: no red authoring for a green entry")
            )
            monkeypatch.setattr(tdd, "_run_agent", red_agent)

            with (
                patch("spec_runner.execution.update_task_status"),
                patch("spec_runner.execution.log_progress"),
                patch(
                    "spec_runner.execution.build_cli_invocation",
                    return_value=CliInvocation(["echo", "hi"], "text"),
                ),
                patch("spec_runner.execution.build_task_prompt", return_value="test prompt"),
                patch("spec_runner.execution.pre_start_hook", return_value=True),
                patch("spec_runner.execution._run_agent_process") as mock_run,
            ):
                mock_run.return_value = MagicMock(
                    stdout="output TASK_COMPLETE", stderr="", returncode=0
                )
                task = _task(id=task_id, verifies=verifies)
                config = _cfg(root, state_file=root / f".state-{task_id}.db")
                with ExecutorState(config) as state:
                    outcome = execute_task(task, config, state)

                    red_agent.assert_not_called()
                    namespace = resolve_namespace(config)
                    assert state.red_checkpoint(task.id, namespace) is None, (
                        f"{task_id}: a green entry must never produce a RedCheckpoint"
                    )
                    evidence = state.verify_evidence(namespace, task.id)
                    assert evidence is not None
                    assert evidence.outcome == "green"

                assert outcome is True

    def test_instrument_error_stops_fail_closed_identically_for_both_target_kinds(
        self, tmp_path, monkeypatch
    ):
        """Instrument error driven by the GROUP, not by a composite command.

        The earlier version used `test_command "… && echo done"`, which
        `run_live_verify` rejects before it resolves an adapter or reads
        `**Verifies:**` at all: both iterations then walked byte-identical
        code and the loop compared a file target with a node id without ever
        looking at either. The parity claim needs a cause that each kind
        reaches through its own path.

        Here the only test in the group is skipped. For the FILE target that
        is an all-skipped composition — the fold's own arm, reported as
        "was not executed (skipped, xfail, or deselected)". For the NODE ID
        the run does not print node ids at all without `-v`, so the selection
        is never PROVEN and the outcome comes from the UNKNOWN arm: "the run
        did not prove which test executed". Both are genuine instrument
        errors and neither is the other's mechanism — which is the point:
        different routes, same named outcome, which is what "the branch is
        not overridden by a file target" means.

        (Both detail strings were read off a live run before being written
        here, rather than inferred from the fold.)
        """
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "import pytest\n\n\n@pytest.mark.skip(reason='nothing executes here')\n"
            "def test_it():\n    assert True\n"
        )
        _commit(root, "base")

        for task_id, verifies in (
            ("TASK-401", ["tests/test_group.py"]),
            ("TASK-402", ["tests/test_group.py::test_it"]),
        ):
            red_agent = MagicMock(
                side_effect=AssertionError("BEH-24: no red authoring for an instrument error")
            )
            monkeypatch.setattr(tdd, "_run_agent", red_agent)

            task = _task(id=task_id, verifies=verifies)
            config = _cfg(root, state_file=root / f".state-{task_id}.db")

            with (
                patch("spec_runner.execution.update_task_status"),
                patch("spec_runner.execution._run_agent_process") as mock_run,
                ExecutorState(config) as state,
            ):
                outcome = execute_task(task, config, state)

                assert outcome is False
                mock_run.assert_not_called()
                red_agent.assert_not_called()

                attempts = state.get_task_state(task.id).attempts
                assert attempts, f"{task_id}: an instrument-error attempt must still be recorded"
                assert attempts[-1].error_code is ErrorCode.INFRASTRUCTURE, (
                    f"{task_id}: expected an infrastructure refusal, got "
                    f"{attempts[-1].error_code} — {attempts[-1].error}"
                )

                namespace = resolve_namespace(config)
                assert state.red_checkpoint(task.id, namespace) is None

    def test_negative_control_a_diverging_file_target_fold_breaks_parity(
        self, tmp_path, monkeypatch
    ):
        """Negative control for the parity claim itself.

        The two tests above say "same outcome, same behaviour, whichever
        kind of target". That is only meaningful if a DIVERGENCE would be
        seen. So the file-target fold is made to disagree with the node-id
        path on the very same fixture — a green group reads as
        `test_failure` for the file target only — and the two kinds must
        then be distinguishable through `run_live_verify`.

        Patched at `_resolve_file_target_triplet`, which is the file
        target's own fold and is not on the node-id path at all: a
        divergence introduced anywhere else would change both kinds and
        prove nothing.
        """
        import spec_runner.live_verify as lv

        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_it():\n    assert True\n")
        _commit(root, "base")
        config = _cfg(root, state_file=root / ".state-parity-control.db")

        def _run(verifies):
            task = _task(id="TASK-403", verifies=verifies)
            return lv.run_live_verify(task, config).outcome

        # Unmodified: both kinds agree, which is the claim under test.
        assert _run(["tests/test_group.py"]) is VerifyOutcome.GREEN
        assert _run(["tests/test_group.py::test_it"]) is VerifyOutcome.GREEN

        monkeypatch.setattr(
            lv,
            "_resolve_file_target_triplet",
            lambda composition: (
                RunOutcome.TESTS_FAILED,
                SelectionProof.PROVEN,
                ExecutionProof.EXECUTED,
            ),
        )

        file_kind = _run(["tests/test_group.py"])
        node_kind = _run(["tests/test_group.py::test_it"])
        assert file_kind is VerifyOutcome.TEST_FAILURE, (
            "control setup: the violated property must actually move the file target's verdict"
        )
        assert node_kind is VerifyOutcome.GREEN, (
            "the node-id path must be untouched by the file target's fold — "
            "otherwise the control changed both sides and proves nothing"
        )
        assert file_kind is not node_kind, (
            "negative control: a divergence between the two kinds is "
            "observable, so the parity assertions above are not vacuous"
        )


class TestBEH18MixedGroupIsStillOneOfTheThree:
    """kind: contract — BEH-18's shape inside BEH-24's claim.

    A declaration may mix a file target and a node id in ONE group. Until
    now no test ran such a group through `run_live_verify` at all — the
    class-level `_TEST_FAILURE_MIXED` is a mixed *composition* of one file,
    which is a different thing. The outcome space must be the same three
    for a mixed group too, and the group must be judged as a whole.
    """

    def test_a_mixed_group_of_a_file_and_a_node_id_folds_to_one_named_outcome(self, tmp_path):
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n"
        )
        (root / "tests" / "test_other.py").write_text("def test_c():\n    assert True\n")
        _commit(root, "base")
        config = _cfg(root, state_file=root / ".state-mixed.db")

        task = _task(
            id="TASK-410",
            verifies=["tests/test_group.py", "tests/test_other.py::test_c"],
        )
        result = run_live_verify(task, config)
        assert result.outcome in (
            VerifyOutcome.GREEN,
            VerifyOutcome.TEST_FAILURE,
            VerifyOutcome.INSTRUMENT_ERROR,
        )
        assert result.outcome is VerifyOutcome.GREEN, (
            f"a mixed group whose every member passes must read green, got "
            f"{result.outcome} — {result.detail}"
        )

    def test_one_failing_member_of_a_mixed_group_makes_the_group_a_failure(self, tmp_path):
        """The group is judged as a whole: a green node id does not rescue a
        failing file target beside it.

        NODE ID FIRST, and the failing member is the *file*, deliberately —
        both halves are load-bearing:

        * order: the file-first case above and this one do not share a code
          path. When a node id opens the group, `run_live_verify` cannot use
          it as the representative selector for `prepare_replay` (only a
          `FileTarget` gets the reporter plugin deployed), so it scans the
          rest of the group for the first file target. Nothing in THIS file
          entered that scan before.
        * which member fails: the group stops at its first failing member, so
          a failing node id in front would end the run before the file target
          is ever reached — and the scan's effect would be unobservable here.
          With the node id green, execution walks on to the file target,
          whose `-p` flag needs the plugin the scan arranged for.

        Consequence, and the point of the arrangement: deleting the scan
        turns this expectation from `test_failure` into `instrument_error`
        (`ImportError: ... _spec_runner_verify_reporter`), so the test fails.
        """
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text(
            "def test_a():\n    assert False, 'not implemented'\n"
        )
        (root / "tests" / "test_other.py").write_text("def test_c():\n    assert True\n")
        _commit(root, "base")
        config = _cfg(root, state_file=root / ".state-mixed-red.db")

        task = _task(
            id="TASK-411",
            verifies=["tests/test_other.py::test_c", "tests/test_group.py"],
        )
        result = run_live_verify(task, config)
        assert result.outcome is VerifyOutcome.TEST_FAILURE, (
            f"a mixed group with a failing member must read test_failure, got "
            f"{result.outcome} — {result.detail}"
        )

    def test_a_failing_node_id_alone_also_makes_the_mixed_group_a_failure(self, tmp_path):
        """The other half of BEH-18's conjunction, and it is a separate claim.

        The spec asks for the verdict to be proven "падением внутри файловой
        цели и падением внутри node id по отдельности" — two halves, not one
        example. The test above carries the file-target half; this one carries
        the node-id half, with the roles swapped: the node id in front is red
        and the file target behind it is green.

        Deliberately NOT merged with the test above, even though both assert
        `test_failure`. The group returns on its first non-green element, so
        here the file target is never reached — which is exactly why this
        arrangement cannot observe the `prepare_replay` fallback scan, and why
        the other test needs its green node id. Collapsing the two would drop
        one half of the conjunction (the state this file was in before) or
        blind the scan (the state before that).
        """
        root = _init_repo(tmp_path)
        (root / "tests" / "test_group.py").write_text("def test_a():\n    assert True\n")
        (root / "tests" / "test_other.py").write_text(
            "def test_c():\n    assert False, 'not implemented'\n"
        )
        _commit(root, "base")
        config = _cfg(root, state_file=root / ".state-mixed-red-node.db")

        task = _task(
            id="TASK-412",
            verifies=["tests/test_other.py::test_c", "tests/test_group.py"],
        )
        result = run_live_verify(task, config)
        assert result.outcome is VerifyOutcome.TEST_FAILURE, (
            f"a mixed group whose node id fails must read test_failure, got "
            f"{result.outcome} — {result.detail}"
        )
