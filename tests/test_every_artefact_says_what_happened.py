"""#295 and #296: a prompt artefact that does not say what became of the call.

Both found in the artefacts of the third evidence run (kapelle m2 TASK-105),
in observability shipped four days earlier — which is the part worth keeping in
mind: reading a real run's log directory found in one afternoon what the
rehearsal of that release had asserted its way past.

What the run left on disk:

```
TASK-105-red-…log     3494 B   prompt only
TASK-105-green-…log   5092 B   prompt + OUTPUT + RETURN CODE + COST: 1.4699667
TASK-105-review-…log  6881 B   prompt only
```

Two different bugs wearing the same shape.

**#295** — the RED path called `log_prompt` and never `append_output`. Its
cause was not a forgotten line but a lossy seam: `AgentCall` carried the text
and the cost and dropped the process's stderr and return code, so the call site
had nothing to append. That was the most expensive call of the task ($2.6944,
27 520 output tokens) and the one whose answer decides which file is frozen for
the rest of the task.

**#296** — the review was refused by the budget guard *after* its prompt was
written, so the artefact held a complete prompt and nothing else: byte-shape
identical to a call that launched and died. An operator listing the directory
saw a review prompt for a review that was never bought.

Fixing them separately would leave the ambiguity: the absence of a terminal
section has to mean exactly one thing. So the invariant these tests pin is

    every artefact ends with a terminal section — an answer, or a statement
    that no call was made

and an artefact without one means the runner itself died mid-call.

A third finding came out of reading both paths side by side: on a **timeout**
the single path returned before appending while the per-role path appended. A
timed-out call ran, and was billed for as long as it ran. The same
two-paths-disagree shape as #270, in the artefact instead of the verdict.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spec_runner.config import ExecutorConfig
from spec_runner.review import ReviewCall
from spec_runner.state import ExecutorState
from spec_runner.tdd import run_red_phase

FAILING = "def test_new_behaviour():\n    assert False\n"
SELECTOR = "tests/test_task_104_red.py::test_new_behaviour"

#: Every section that closes an artefact. The invariant is about this set, not
#: about any one of them.
TERMINAL_SECTIONS = ("=== OUTPUT ===", "=== NOT STARTED:")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> ExecutorConfig:
    root = tmp_path / "repo"
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
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _cfg(tmp_path: Path, **overrides) -> ExecutorConfig:
    defaults: dict = {
        "project_root": tmp_path,
        "state_file": tmp_path / "spec" / ".executor-state.db",
        "logs_dir": tmp_path / "spec" / ".executor-logs",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task():
    from spec_runner.task import Task

    return Task(id="TASK-104", name="t", priority="p1", status="todo", estimate="1h")


def _red_agent(monkeypatch, **call_kwargs):
    """The RED authoring pass, answering as a real agent does — through the
    seam the call site actually uses."""
    from spec_runner import tdd

    def _red(config, prompt, **kwargs):
        target = Path(config.project_root) / "tests" / "test_task_104_red.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(FAILING)
        defaults: dict = {
            "text": f"wrote the failing test\nTDD_SELECTOR: {SELECTOR}",
            "cost_usd": 2.6944,
            "output_tokens": 27_520,
        }
        defaults.update(call_kwargs)
        return tdd.AgentCall(**defaults)

    monkeypatch.setattr(tdd, "_run_agent", _red)


def _refused_call(reason: str) -> ReviewCall:
    return ReviewCall(
        text="", stderr="", returncode=-1, cost_usd=None, budget_refusal=reason, timed_out=False
    )


def _timed_out_call() -> ReviewCall:
    return ReviewCall(text="", stderr="", returncode=-1, cost_usd=None, timed_out=True)


def _logs(cfg: ExecutorConfig, pattern: str) -> list[Path]:
    return sorted(cfg.logs_dir.glob(pattern))


def _raiser(exc: BaseException):
    """A call that fails instead of returning — the case the artefact could not
    survive."""

    def _boom(*args, **kwargs):
        raise exc

    return _boom


REFUSAL = (
    "Task budget reached before the review call ($4.16 >= $4.00) — not starting it. "
    "The limit is authorization #10"
)


@pytest.mark.slow
class TestTheRedArtefactHoldsTheAnswer:
    """#295, from the call site. A test that drove `append_output` directly
    would have passed against the broken code — that is exactly how the 2.33.1
    rehearsal missed this."""

    def test_a_red_pass_records_what_the_money_bought(self, tmp_path, monkeypatch):
        cfg = _repo(tmp_path)
        _red_agent(monkeypatch)

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)

        body = _logs(cfg, "*-red-*.log")[0].read_text()
        assert "=== RED PROMPT ===" in body, "the half that already worked"
        assert "=== OUTPUT ===" in body
        assert f"TDD_SELECTOR: {SELECTOR}" in body, "the answer that decides the frozen file"
        assert "=== RETURN CODE: 0 ===" in body
        assert "=== COST: 2.6944 ===" in body

    def test_an_unreported_cost_is_unknown_and_never_zero(self, tmp_path, monkeypatch):
        """The rule the ledger follows (#213). A zero here would be
        indistinguishable from a cheap call in every later reading."""
        cfg = _repo(tmp_path)
        _red_agent(monkeypatch, cost_usd=None)

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)

        body = _logs(cfg, "*-red-*.log")[0].read_text()
        assert "=== COST: unknown ===" in body
        assert "COST: 0" not in body

    def test_the_call_site_appends_the_stderr_it_was_given(self, tmp_path, monkeypatch):
        cfg = _repo(tmp_path)
        _red_agent(monkeypatch, stderr="warning: deprecated flag\n", returncode=0)

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)

        body = _logs(cfg, "*-red-*.log")[0].read_text()
        assert "=== STDERR ===" in body
        assert "warning: deprecated flag" in body

    def test_the_seam_itself_carries_them_off_the_process(self, tmp_path):
        """The cause, not the symptom: `AgentCall` used to carry neither, so no
        call site could have appended them however carefully it was written.

        Every other test here stubs `_run_agent`, which means none of them can
        see that. Measured: with `stderr=`/`returncode=` deleted from
        `_run_agent`'s return, the stubbed tests all still passed — the same
        vacuum that let #295 ship. So this one runs a real process through the
        real seam, with a script standing in for the CLI: no agent, no money.
        """
        from spec_runner import tdd

        cfg = _repo(tmp_path)
        fake = tmp_path / "fake-cli"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            "printf 'the answer\\n'\n"
            "printf 'a warning from the process\\n' >&2\n"
            "exit 3\n"
        )
        fake.chmod(0o755)
        cfg.claude_command = str(fake)

        call = tdd._run_agent(cfg, "anything")

        assert "the answer" in call.text
        assert "a warning from the process" in call.stderr
        assert call.returncode == 3

    def test_a_reused_red_writes_no_artefact_at_all(self, tmp_path, monkeypatch):
        """It makes no call, so there is nothing to record — and an artefact
        would assert a call that was not made. Guards the fix against being
        applied one level too high."""
        cfg = _repo(tmp_path)
        _red_agent(monkeypatch)

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)
            first = len(_logs(cfg, "*-red-*.log"))
            run_red_phase(_task(), cfg, state)

        assert first == 1
        assert len(_logs(cfg, "*-red-*.log")) == 1, "the second pass reused the confirmed red"


class TestARefusedCallSaysSo:
    """#296, both review paths."""

    def test_the_single_path_records_the_refusal(self, tmp_path, monkeypatch):
        from spec_runner import review

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: "REVIEW THIS")
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: _refused_call(REFUSAL))

        verdict, detail, _ = review.run_code_review(_task(), cfg)

        body = _logs(cfg, "*-review-*.log")[0].read_text()
        assert "=== NOT STARTED:" in body
        assert "$4.16 >= $4.00" in body, "the artefact names the ceiling that refused it"
        assert "=== OUTPUT ===" not in body, "no call, so no answer may be claimed"
        assert verdict.value == "not_run", "the verdict is unchanged by the logging"
        assert detail == REFUSAL

    def test_the_per_role_path_records_it_too(self, tmp_path, monkeypatch):
        """The two paths disagreeing about the same event is the recurring bug
        of this module (#270)."""
        from spec_runner import review

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: _refused_call(REFUSAL))

        review._run_single_role_review(
            "quality", "look for bugs", "base", "cli", "", "", cfg, "TASK-104"
        )

        body = _logs(cfg, "*review-quality*")[0].read_text()
        assert "=== NOT STARTED:" in body
        assert "$4.16 >= $4.00" in body
        assert "=== OUTPUT ===" not in body

    def test_the_prompt_is_kept_beside_the_refusal(self, tmp_path, monkeypatch):
        """Deliberate: the prompt is what an operator reads when deciding
        whether to raise the ceiling that refused it. Deleting the artefact
        would also have removed the ambiguity, and would have removed that."""
        from spec_runner import review

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: "REVIEW THIS")
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: _refused_call(REFUSAL))

        review.run_code_review(_task(), cfg)

        body = _logs(cfg, "*-review-*.log")[0].read_text()
        assert "REVIEW THIS" in body
        assert body.index("REVIEW THIS") < body.index("=== NOT STARTED:")


class TestATimedOutCallIsStillACall:
    """It ran, and it was billed for as long as it ran."""

    def test_the_single_path_records_a_timeout(self, tmp_path, monkeypatch):
        """This is the one that was silent: it returned before the append."""
        from spec_runner import review

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: "REVIEW THIS")
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: _timed_out_call())

        verdict, detail, _ = review.run_code_review(_task(), cfg)

        body = _logs(cfg, "*-review-*.log")[0].read_text()
        assert "=== OUTPUT ===" in body, "a call happened; the artefact must close"
        assert "=== COST: unknown ===" in body, "billed, amount unreported"
        assert verdict.value == "not_run"
        assert detail == "Review timed out"

    def test_the_per_role_path_agrees(self, tmp_path, monkeypatch):
        from spec_runner import review

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: _timed_out_call())

        review._run_single_role_review(
            "quality", "look for bugs", "base", "cli", "", "", cfg, "TASK-104"
        )

        body = _logs(cfg, "*review-quality*")[0].read_text()
        assert "=== OUTPUT ===" in body
        assert "=== COST: unknown ===" in body


class TestACallThatNeverReturnsClosesItsArtefactToo:
    """Copilot, PR #298 — and the hole was wider than the site it named.

    Measured before fixing, by making the call raise at each site:

    | site | what happened | artefact |
    |---|---|---|
    | RED, binary missing | raised `FileNotFoundError` | **open** |
    | RED, timeout | raised `TimeoutExpired` | **open** |
    | review, binary missing | caught; verdict `error` | **open** |
    | review role, binary missing | caught; verdict `error` | **open** |

    The two review rows are the ones that make the invariant false rather than
    merely incomplete: the exception is swallowed, the runner carries on, and
    the file it left behind claims — under the rule this PR was written to
    establish — that the runner died.
    """

    @pytest.mark.slow
    def test_a_red_agent_that_does_not_launch(self, tmp_path, monkeypatch):
        from spec_runner import tdd

        cfg = _repo(tmp_path)
        monkeypatch.setattr(
            tdd,
            "_run_agent",
            _raiser(FileNotFoundError(2, "No such file or directory: 'claude'")),
        )

        with ExecutorState(cfg) as state, pytest.raises(OSError):
            run_red_phase(_task(), cfg, state)

        body = _logs(cfg, "*-red-*.log")[0].read_text()
        assert "=== NOT STARTED:" in body
        assert "did not launch" in body
        assert "=== OUTPUT ===" not in body, "no subprocess, so no answer and no spend"

    @pytest.mark.slow
    def test_a_red_agent_that_times_out(self, tmp_path, monkeypatch):
        """It ran. The artefact says so, and says why there is nothing in it."""
        from spec_runner import tdd

        cfg = _repo(tmp_path)
        monkeypatch.setattr(
            tdd, "_run_agent", _raiser(subprocess.TimeoutExpired(cmd="agent", timeout=1))
        )

        with ExecutorState(cfg) as state, pytest.raises(subprocess.TimeoutExpired):
            run_red_phase(_task(), cfg, state)

        body = _logs(cfg, "*-red-*.log")[0].read_text()
        assert "=== NO RESULT: timed out after" in body
        assert "=== NOT STARTED:" not in body, "it started; it just produced nothing"

    def test_a_reviewer_that_does_not_launch(self, tmp_path, monkeypatch):
        from spec_runner import review

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: "REVIEW THIS")
        monkeypatch.setattr(review, "_run_reviewer", _raiser(FileNotFoundError(2, "nope")))

        verdict, _detail, _ = review.run_code_review(_task(), cfg)

        body = _logs(cfg, "*-review-*.log")[0].read_text()
        assert "=== NOT STARTED:" in body
        assert "did not launch" in body
        assert verdict.value == "error", "the existing control flow is unchanged"

    def test_a_role_reviewer_that_does_not_launch(self, tmp_path, monkeypatch):
        from spec_runner import review

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "_run_reviewer", _raiser(FileNotFoundError(2, "nope")))

        _role, verdict, _detail = review._run_single_role_review(
            "quality", "look for bugs", "base", "cli", "", "", cfg, "TASK-104"
        )

        body = _logs(cfg, "*review-quality*")[0].read_text()
        assert "=== NOT STARTED:" in body
        assert verdict.value == "error"

    def test_a_timed_out_review_says_why_it_is_empty(self, tmp_path, monkeypatch):
        """An empty output block is true and unreadable. Both paths."""
        from spec_runner import review

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: "REVIEW THIS")
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: _timed_out_call())

        review.run_code_review(_task(), cfg)
        review._run_single_role_review(
            "quality", "look for bugs", "base", "cli", "", "", cfg, "TASK-104"
        )

        for pattern in ("*-review-2*.log", "*review-quality*"):
            body = _logs(cfg, pattern)[0].read_text()
            assert "=== NO RESULT: timed out after" in body, pattern


class TestTheInvariant:
    """What the two fixes buy together: the absence of a terminal section has
    exactly one meaning."""

    @pytest.mark.slow
    def test_no_artefact_of_a_real_run_is_left_open(self, tmp_path, monkeypatch):
        cfg = _repo(tmp_path)
        _red_agent(monkeypatch)

        with ExecutorState(cfg) as state:
            run_red_phase(_task(), cfg, state)

        written = _logs(cfg, "*.log")
        assert written, "the run wrote something to check"
        for path in written:
            body = path.read_text()
            assert any(section in body for section in TERMINAL_SECTIONS), (
                f"{path.name} ends without saying what became of the call"
            )

    def test_a_refused_review_is_not_left_open_either(self, tmp_path, monkeypatch):
        from spec_runner import review

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: "REVIEW THIS")
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: _refused_call(REFUSAL))

        review.run_code_review(_task(), cfg)

        for path in _logs(cfg, "*.log"):
            body = path.read_text()
            assert any(section in body for section in TERMINAL_SECTIONS), path.name

    def test_a_logging_failure_still_costs_no_verdict(self, tmp_path, monkeypatch):
        """The rule the writer has always followed, extended to the new
        section: bookkeeping that runs beside work must not decide it."""
        from spec_runner import review
        from spec_runner.prompts_log import append_not_started

        cfg = _cfg(tmp_path)
        monkeypatch.setattr(review, "build_review_prompt", lambda *a, **k: "REVIEW THIS")
        monkeypatch.setattr(review, "_run_reviewer", lambda *a, **k: _refused_call(REFUSAL))
        cfg.logs_dir.chmod(0o500)
        try:
            verdict, detail, _ = review.run_code_review(_task(), cfg)
        finally:
            cfg.logs_dir.chmod(0o700)

        assert verdict.value == "not_run"
        assert detail == REFUSAL
        # And directly: an unwritable path warns rather than raising.
        append_not_started(tmp_path / "no" / "such" / "dir" / "x.log", "reason")
        append_not_started(None, "reason")
