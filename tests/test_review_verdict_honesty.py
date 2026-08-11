"""A review that did not happen must not report as one that passed (#138).

The stage could not fail a task by any route, but the log read like a quality
gate. Three independent paths reached "fine":

1. a timeout became `FAILED`, which non-HITL mode logs and ignores;
2. **no recognizable marker in the output became `PASSED`** — an agent that
   produced nothing was recorded as a successful review;
3. the stage runs after commit, so even `REVIEW_FAILED` arrives too late.

Measured cost on a 26-task pilot run: six 15-minute timeouts — an hour and a
half of wall time for advice that was never given — every one of those tasks
closed DONE with `⏰ Review timeout after 15m` and not a single finding.

This module covers the correctness half only: the verdict must say what
actually happened. Whether a failed review *blocks* a task outside HITL, and
whether review should move relative to the commit, are policy decisions tracked
separately — nothing here changes blocking behaviour.
"""

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.review import run_code_review
from spec_runner.state import ReviewVerdict
from spec_runner.task import Task


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "spec").mkdir()
    (tmp_path / "logs").mkdir()
    return tmp_path


def _cfg(project: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": project,
        "state_file": project / "state.db",
        "logs_dir": project / "logs",
        "create_git_branch": False,
        "auto_commit": False,
        "review_timeout_minutes": 1,
    }
    defaults.update(overrides)
    return ExecutorConfig(**defaults)


def _task() -> Task:
    return Task(id="TASK-001", name="Demo", priority="p0", status="todo", estimate="1d")


def _cli(monkeypatch, *, stdout="", stderr="", returncode=0, raises=None):
    from spec_runner import review as review_mod

    def _run(*a, **k):
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(
            args=["review"], returncode=returncode, stdout=stdout, stderr=stderr
        )

    monkeypatch.setattr(review_mod.subprocess, "run", _run)


class TestSilenceIsNotApproval:
    def test_output_without_a_marker_is_not_passed(self, project, monkeypatch):
        """The core defect: an agent that said nothing recognizable counted as
        a clean review."""
        _cli(monkeypatch, stdout="I looked at some files and had some thoughts.\n")
        verdict, _, _ = run_code_review(_task(), _cfg(project))
        assert verdict is not ReviewVerdict.PASSED
        assert verdict is ReviewVerdict.NOT_RUN

    def test_empty_output_is_not_run(self, project, monkeypatch):
        _cli(monkeypatch, stdout="   \n")
        verdict, error, _ = run_code_review(_task(), _cfg(project))
        assert verdict is ReviewVerdict.NOT_RUN
        assert error and "empty" in error.lower()

    def test_timeout_is_not_run_not_failure(self, project, monkeypatch):
        """A timeout is "no verdict", not "found issues" — conflating them made
        the six pilot timeouts indistinguishable from reviewed-and-fine."""
        _cli(monkeypatch, raises=subprocess.TimeoutExpired(cmd="review", timeout=60))
        verdict, error, _ = run_code_review(_task(), _cfg(project))
        assert verdict is ReviewVerdict.NOT_RUN
        assert error and "timed out" in error.lower()


class TestMachineryFailureIsAnError:
    def test_nonzero_exit_without_output_is_error(self, project, monkeypatch):
        _cli(monkeypatch, returncode=2, stderr="command not found\n")
        verdict, error, _ = run_code_review(_task(), _cfg(project))
        assert verdict is ReviewVerdict.ERROR
        assert error and "2" in error

    def test_unexpected_exception_is_error(self, project, monkeypatch):
        _cli(monkeypatch, raises=OSError("no such binary"))
        verdict, error, _ = run_code_review(_task(), _cfg(project))
        assert verdict is ReviewVerdict.ERROR
        assert error and "no such binary" in error

    def test_api_error_is_error(self, project, monkeypatch):
        _cli(monkeypatch, stdout="rate limit exceeded\n")
        verdict, error, _ = run_code_review(_task(), _cfg(project))
        assert verdict is ReviewVerdict.ERROR
        assert error and "rate limit" in error.lower()


class TestRealVerdictsAreUnchanged:
    """Guard against over-correction: the three honest outcomes keep working."""

    @pytest.mark.parametrize(
        "marker,expected",
        [
            ("REVIEW_PASSED", ReviewVerdict.PASSED),
            ("REVIEW_FAILED: nulls unchecked", ReviewVerdict.FAILED),
        ],
    )
    def test_explicit_markers(self, project, monkeypatch, marker, expected):
        _cli(monkeypatch, stdout=f"some prose\n{marker}\n")
        verdict, _, _ = run_code_review(_task(), _cfg(project))
        assert verdict is expected

    def test_findings_carry_a_reason(self, project, monkeypatch):
        _cli(monkeypatch, stdout="REVIEW_FAILED: nulls unchecked\n")
        _, error, _ = run_code_review(_task(), _cfg(project))
        assert error


class TestOutcomesAreDistinguishable:
    """`passed`, `findings`, `not_run` and `error` must be four different
    things in the record — an operator reading it decides whether to act."""

    def test_the_four_outcomes_have_distinct_verdicts(self, project, monkeypatch):
        seen = {}
        for label, kwargs in {
            "passed": {"stdout": "REVIEW_PASSED\n"},
            "findings": {"stdout": "REVIEW_FAILED: x\n"},
            "not_run": {"stdout": "nothing useful\n"},
            "error": {"returncode": 3, "stderr": "boom\n"},
        }.items():
            _cli(monkeypatch, **kwargs)
            seen[label] = run_code_review(_task(), _cfg(project))[0]
        assert len(set(seen.values())) == 4, seen

    @pytest.mark.parametrize(
        "kwargs,needle",
        [
            ({"stdout": "REVIEW_PASSED\n"}, "passed"),
            ({"stdout": "nothing useful\n"}, "no verdict"),
            ({"returncode": 3, "stderr": "boom\n"}, "error"),
        ],
    )
    def test_progress_line_names_the_outcome(self, project, monkeypatch, kwargs, needle):
        from spec_runner import review as review_mod

        lines: list[str] = []
        monkeypatch.setattr(review_mod, "log_progress", lambda msg, *a, **k: lines.append(msg))
        _cli(monkeypatch, **kwargs)
        run_code_review(_task(), _cfg(project))
        assert any(needle in ln.lower() for ln in lines), lines

    def test_not_run_is_not_described_as_completed(self, project, monkeypatch):
        """The old line read "✅ Code review completed (no explicit status
        marker)" — the tick and the word "completed" are exactly what made a
        non-review look like a review."""
        from spec_runner import review as review_mod

        lines: list[str] = []
        monkeypatch.setattr(review_mod, "log_progress", lambda msg, *a, **k: lines.append(msg))
        _cli(monkeypatch, stdout="nothing useful\n")
        run_code_review(_task(), _cfg(project))
        assert not any("✅" in ln for ln in lines), lines


class TestVerdictVocabulary:
    def test_new_verdicts_exist_with_stable_values(self):
        assert ReviewVerdict.NOT_RUN.value == "not_run"
        assert ReviewVerdict.ERROR.value == "error"

    def test_existing_values_unchanged(self):
        assert [v.value for v in ReviewVerdict][:5] == [
            "passed",
            "fixed",
            "failed",
            "skipped",
            "rejected",
        ]


class TestNonZeroExitIsNeverAVerdict:
    """A crashed reviewer's stdout is not evidence (Copilot, PR #156).

    The guard only covered "non-zero **and** empty output", so a process that
    printed something before dying still had that output parsed for a marker —
    including `REVIEW_PASSED`. That is the same class this PR exists to close:
    output that was never a considered verdict being read as approval.
    """

    @pytest.mark.parametrize("marker", ["REVIEW_PASSED", "REVIEW_FIXED", "REVIEW_FAILED"])
    def test_any_marker_after_a_crash_is_an_error(self, project, monkeypatch, marker):
        _cli(monkeypatch, stdout=f"{marker}\n", returncode=1, stderr="segfault\n")
        verdict, _, _ = run_code_review(_task(), _cfg(project))
        assert verdict is ReviewVerdict.ERROR

    def test_the_output_is_still_reported(self, project, monkeypatch):
        """Discarding the verdict must not discard what was said — an operator
        reading the record still needs the reviewer's last words."""
        _cli(monkeypatch, stdout="REVIEW_FAILED: nulls unchecked\n", returncode=1)
        _, error, output = run_code_review(_task(), _cfg(project))
        assert error and "1" in error
        assert output and "nulls unchecked" in output


class TestParallelFixesAreAlwaysCommittedAndGated:
    """A role that fixed code changed the working tree; that must not depend on
    what the *other* roles returned (Copilot, PR #156).

    With `fixed` + `not_run` the aggregate became `not_run`, which skipped the
    commit-and-rerun-gates path — and the general auto-commit later picked the
    same changes up without re-running any gate. Un-gated review edits landing
    silently is exactly the failure mode this issue is about, one level down.
    """

    def _run(self, project, monkeypatch, role_verdicts: dict[str, ReviewVerdict]):
        from spec_runner import review as review_mod

        committed: list[list[str]] = []
        monkeypatch.setattr(review_mod, "stage_all_except_runtime", lambda cfg: True)
        monkeypatch.setattr(
            review_mod.subprocess,
            "run",
            lambda cmd, *a, **k: (
                committed.append(cmd),
                subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr=""),
            )[1],
        )
        monkeypatch.setattr(
            review_mod,
            "_run_single_role_review",
            lambda role, *a, **k: (role, role_verdicts[role], f"{role} output"),
        )
        cfg = _cfg(project, review_parallel=True, review_roles=list(role_verdicts))
        return review_mod.run_parallel_review(_task(), cfg), committed

    def test_fixes_commit_even_when_another_role_did_not_answer(self, project, monkeypatch):
        (verdict, _, _), committed = self._run(
            project,
            monkeypatch,
            {"quality": ReviewVerdict.FIXED, "testing": ReviewVerdict.NOT_RUN},
        )
        assert any("commit" in c for c in committed), "review fixes were left uncommitted"
        assert verdict is ReviewVerdict.FIXED, (
            "gates rerun on FIXED, so a run that changed the tree must report it"
        )

    def test_fixes_commit_even_when_another_role_errored(self, project, monkeypatch):
        (verdict, _, _), committed = self._run(
            project,
            monkeypatch,
            {"quality": ReviewVerdict.FIXED, "testing": ReviewVerdict.ERROR},
        )
        assert any("commit" in c for c in committed)
        assert verdict is ReviewVerdict.FIXED

    def test_roles_that_did_not_answer_are_still_reported(self, project, monkeypatch):
        (_, error, _), _ = self._run(
            project,
            monkeypatch,
            {"quality": ReviewVerdict.FIXED, "testing": ReviewVerdict.NOT_RUN},
        )
        assert error and "testing" in error, (
            "the silent role vanished from the record once FIXED won"
        )

    def test_findings_still_outrank_fixes(self, project, monkeypatch):
        (verdict, _, _), _ = self._run(
            project,
            monkeypatch,
            {"quality": ReviewVerdict.FAILED, "testing": ReviewVerdict.FIXED},
        )
        assert verdict is ReviewVerdict.FAILED

    def test_all_silent_still_aggregates_to_not_run(self, project, monkeypatch):
        (verdict, _, _), _ = self._run(
            project,
            monkeypatch,
            {"quality": ReviewVerdict.NOT_RUN, "testing": ReviewVerdict.NOT_RUN},
        )
        assert verdict is ReviewVerdict.NOT_RUN


def test_state_schema_enumerates_every_verdict():
    """`schemas/executor-state.schema.json` is the frozen interop contract
    (Copilot, PR #156): a value the code writes but the schema rejects makes
    validating consumers fail on healthy state."""
    import json

    schema = json.loads(Path("schemas/executor-state.schema.json").read_text())
    text = json.dumps(schema)
    for verdict in ReviewVerdict:
        assert f'"{verdict.value}"' in text, f"{verdict.value} missing from the schema"
