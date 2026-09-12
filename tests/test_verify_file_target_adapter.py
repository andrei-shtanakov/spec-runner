"""BEH-05 (TASK-011, DT-11).

Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/15-behaviour-spec.md#BEH-05
Source: workstreams/verify-first-file-scope-group-targets-20260908/spec/30-decomposition.md#DT-11
Traces: FR-03

TDD-waiver (class: characterisation, sanction: batch-approve-2026-09-09).
All five conditions of the class, confirmed:

- the behaviour is already delivered by this task's own dependencies:
  `PytestAdapter.parse_group_element` (TASK-002/DT-02) accepts a bare file
  path as a `FileTarget`; `ExUnitAdapter` declares no such method, so
  `parse_group_element`'s dispatch (`tdd_runners.py`) falls back to its
  ordinary `parse_selector`, which refuses anything that is not `path:line`
  — including a bare file path — under its own stable
  `SelectorRefusal.code`; and `validate._validate_verify_first_declarations`
  (TASK-004/DT-04) already turns any `parse_group_element` refusal into a
  named `validate` error quoting the adapter and the declared value. None of
  `tdd_runners.py`, `validate.py` or `live_verify.py` is modified by this
  task;
- this task adds the missing characterisation coverage: that ONE declaration
  driven through BOTH adapters, from `validate` through to `run_live_verify`,
  diverges exactly as an adapter-owned property — pytest carries it to a real
  green, ExUnit refuses it by name before `validate` lets it through and
  before a single byte of replay-environment preparation happens. No test in
  the tree drives the same declaration through both halves of this path
  today (DT-11's own boundary: the pieces exist separately, not stitched);
- an honest baseline RED is not possible: `parse_group_element`'s dispatch,
  `ExUnitAdapter.parse_selector`'s refusal, and `validate`'s wrapping of a
  refusal into a named error all already exist and already behave this way —
  writing this file against unmodified `main` cannot fail without first
  reverting delivered code, which is not this task's job;
- every claim below carries a negative control that flips the observed
  result under a deliberately violated property, proving the assertion
  actually discriminates rather than passing vacuously. Per claim:
  - `validate` boundary — `ExUnitAdapter` is monkeypatched with a
    `parse_group_element` that silently accepts the bare file path as though
    it named a valid selector (the exact defect FR-03 forbids: accepting a
    file target silently to judge it later by a return code), and the
    `validate` refusal disappears;
  - execution boundary — the same regressed `parse_group_element` is applied
    to `run_live_verify`, and the call that must never be reached
    (`ExUnitAdapter.prepare_replay`) is reached;
- baseline commit at the start of this task: `2560113fbf6301a31f0c065c6850c7deae7a6687`.

Negative control for the one condition with no machine gate (a real
mutation-kill on "the new test discriminates") is, per the class, left to
task review — not asserted by any test here.

kind: contract — one declared value, read through the real dictionaries of
two real adapters (`PytestAdapter`, `ExUnitAdapter`), from `validate` to a
real `run_live_verify` replay; no mocked verdict, no synthetic third
adapter.
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath
from unittest.mock import MagicMock

from spec_runner.config import ExecutorConfig
from spec_runner.live_verify import run_live_verify
from spec_runner.task import Task
from spec_runner.tdd_runners import ExUnitAdapter, ExUnitDefinitionLine, Selector
from spec_runner.validate import _validate_verify_first_declarations

#: The one declared value driven through both adapters — a bare file path,
#: legal pytest vocabulary (a `FileTarget`) and, under ExUnit, neither a node
#: id nor a `path:line` selector.
DECLARED = "tests/test_verify_target.py"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests").mkdir()
    (root / "tests" / "test_verify_target.py").write_text("def test_it():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _pytest_cfg(root: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": root,
        "state_file": root / ".state.db",
        "logs_dir": root / ".logs",
        "test_command": "python -m pytest",
        "max_retries": 1,
        "retry_delay_seconds": 0,
        "create_git_branch": False,
        "run_tests_on_done": False,
        "auto_commit": False,
        "run_review": False,
        "callback_url": "",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _exunit_cfg(root: Path, **overrides) -> ExecutorConfig:
    return _pytest_cfg(root, tdd_runner="exunit", test_command="mix test", **overrides)


def _task(**overrides) -> Task:
    defaults: dict = {
        "id": "TASK-501",
        "name": "verify-first file target, two adapters",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
        "execution_mode": "verify_first",
        "verifies": [DECLARED],
    }
    defaults.update(overrides)
    return Task(**defaults)


def _accept_as_line_one(self, raw: str, root: Path) -> Selector:
    """A regressed `ExUnitAdapter.parse_group_element`: silently accepts a
    bare file path as though it named a valid `path:line` selector — exactly
    the defect FR-03 forbids ("никогда не принимает файловую цель молча,
    чтобы затем судить по коду возврата")."""
    return Selector(runner="exunit", path=PurePosixPath(raw), locator=ExUnitDefinitionLine(1))


class TestValidateRefusesByNameUnderTheAdapterWithoutSupport:
    """kind: contract — BEH-05, `validate` half: the same declaration is
    accepted under the adapter that declared support and refused, by name,
    under the one that did not."""

    def test_pytest_adapter_accepts_the_declaration_at_validate(self, tmp_path):
        root = _repo(tmp_path)
        result = _validate_verify_first_declarations([_task()], _pytest_cfg(root))

        assert result.ok, f"unexpected: errors={result.errors} warnings={result.warnings}"

    def test_exunit_adapter_refuses_the_same_declaration_by_name(self, tmp_path):
        root = _repo(tmp_path)
        result = _validate_verify_first_declarations([_task()], _exunit_cfg(root))

        assert not result.ok
        joined = "\n".join(result.errors)
        assert "TASK-501" in joined
        assert "exunit" in joined, "the refusal must name the adapter"
        assert DECLARED in joined, "the refusal must quote the declared value verbatim"
        assert "path:line" in joined, "the refusal must name the form this adapter expects"

    def test_negative_control_a_regressed_adapter_would_be_accepted_silently(
        self, tmp_path, monkeypatch
    ):
        """Mutation-kill: give `ExUnitAdapter` a `parse_group_element` that
        silently accepts the file path, and confirm `validate` no longer
        refuses it — proving the refusal above actually depends on ExUnit
        having no such method, not on something else."""
        root = _repo(tmp_path)
        cfg = _exunit_cfg(root)

        baseline = _validate_verify_first_declarations([_task()], cfg)
        assert not baseline.ok, (
            "control baseline: with the real adapter the declaration must be "
            "refused — if this half passes vacuously, the flip below proves "
            "nothing"
        )

        monkeypatch.setattr(
            ExUnitAdapter, "parse_group_element", _accept_as_line_one, raising=False
        )

        regressed = _validate_verify_first_declarations([_task()], cfg)

        assert regressed.ok, (
            "negative control: a `parse_group_element` that accepts the file "
            "path silently lets `validate` pass it through — proving the real "
            "refusal is produced by ExUnit having no such method, not by "
            "something that would have refused anyway"
        )


class TestExecutionNeverAcceptsAFileTargetSilentlyUnderTheAdapterWithoutSupport:
    """kind: contract — BEH-05, execution half: the same declaration, driven
    through `run_live_verify`, reaches a real green under pytest and is
    refused before a byte of replay-environment preparation under ExUnit —
    no observable `green` exists for this input under either failure mode a
    return code could produce, because the run never starts."""

    def test_pytest_executes_the_declaration_to_a_real_green(self, tmp_path):
        root = _repo(tmp_path)
        result = run_live_verify(_task(), _pytest_cfg(root))

        assert result.ran and result.passed, result.detail
        assert result.adapter == "pytest"

    def test_exunit_refuses_before_the_environment_is_ever_prepared(self, tmp_path, monkeypatch):
        root = _repo(tmp_path)
        prepare_spy = MagicMock(
            side_effect=AssertionError(
                "prepare_replay must never be reached for a declaration ExUnit refuses"
            )
        )
        monkeypatch.setattr(ExUnitAdapter, "prepare_replay", prepare_spy)

        result = run_live_verify(_task(), _exunit_cfg(root))

        assert result.ran is False
        assert result.passed is False, "no `green` outcome exists for this input (FR-03)"
        assert result.adapter == "exunit"
        assert "path:line" in result.detail
        prepare_spy.assert_not_called()

    def test_negative_control_a_regressed_adapter_reaches_environment_preparation(
        self, tmp_path, monkeypatch
    ):
        """Mutation-kill for the execution boundary: the same regressed
        `parse_group_element` as the `validate` control above, applied here
        to `run_live_verify`. The call that BEH-05 says must never be
        reached (`prepare_replay`, the first step towards actually running
        something) is reached once the named refusal is gone — proving the
        refusal above, not something downstream, is what stops it today.

        `preflight` is also stubbed to accept: it is ExUnit's *own*, later
        per-selector guard (proving the requested line defines a test) and
        is not what BEH-05 is about — left in place it would simply refuse
        the regressed input for an unrelated reason (this fixture's file is
        not real Elixir source) and mask what this control is checking."""
        root = _repo(tmp_path)
        cfg = _exunit_cfg(root)
        prepare_spy = MagicMock(side_effect=RuntimeError("prepare_replay reached"))
        monkeypatch.setattr(ExUnitAdapter, "prepare_replay", prepare_spy)

        baseline = run_live_verify(_task(), cfg)
        assert baseline.ran is False, (
            "control baseline: with the real adapter the run must never "
            "start — if this half passes vacuously, the flip below proves "
            "nothing"
        )
        # #448 finding 2: `ran is False` alone is true of every refusal,
        # including ones this control never arranged. A dispatcher that
        # accepted the bare path and left `preflight` to reject it as "does
        # not parse as Elixir" would satisfy the line above while the named
        # static refusal — the one BEH-05 is about — was already gone. The
        # same pin the positive test carries says which refusal happened.
        assert "path:line" in baseline.detail, (
            "control baseline: the refusal must be the named static one "
            f"(the adapter has no such method), got {baseline.detail!r}"
        )
        prepare_spy.assert_not_called()

        monkeypatch.setattr(
            ExUnitAdapter, "parse_group_element", _accept_as_line_one, raising=False
        )
        monkeypatch.setattr(ExUnitAdapter, "preflight", lambda self, root, selector: None)

        run_live_verify(_task(), cfg)

        # negative control: with the named refusal gone, the run reaches
        # environment preparation instead of refusing statically.
        prepare_spy.assert_called_once()
