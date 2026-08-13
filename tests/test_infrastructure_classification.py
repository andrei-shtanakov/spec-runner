"""#230 part 1: a broken instrument and a refused verdict are different exits.

`run_exit_code` has said since v2.25.0 that exit 1 means "the work did not
finish" and exit 2 means "the instrument broke, so I cannot tell you whether
the work is good". The RED site could not reach 2. Its instrument errors were
classified by a **prefix match on the message**, and it wrote a different
sentence — so a replay that failed for environment reasons was recorded
`HOOK_FAILURE` and reported to CI as a failed task. From the pilot's own state
DB, attempt 1 of kapelle TASK-101:

    HOOK_FAILURE | RED could not be verified (infrastructure): ...

The word is right there in the message and nothing read it.

The fix is typed, not another sentence to match: `gates.refusal_for` turns the
gate's own `GateStatus` into a `Refusal` that carries its kind, and
`_refusal_error_code` reads the kind. A new refusal site cannot inherit the
wrong exit code by phrasing itself differently, because phrasing is no longer
what decides.

These run through the real CLI entrypoint, because the defect was in the
wiring between a verdict and `sys.exit` — the layer where every part in
isolation looked right.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from spec_runner.gates import GateStatus, refusal_for
from spec_runner.phases import Refusal, RefusalKind
from spec_runner.state import ErrorCode

# --- the vocabulary itself ------------------------------------------------


class TestTheKindTravelsWithTheMessage:
    def test_an_instrument_error_maps_to_infrastructure(self):
        r = refusal_for(GateStatus.INSTRUMENT_ERROR, "the replay environment is broken")
        assert r.kind is RefusalKind.INSTRUMENT
        assert r.error_code is ErrorCode.INFRASTRUCTURE

    def test_an_unsatisfied_gate_maps_to_a_hook_failure(self):
        r = refusal_for(GateStatus.UNSATISFIED, "the claimed red did not fail on replay")
        assert r.kind is RefusalKind.POLICY
        assert r.error_code is ErrorCode.HOOK_FAILURE

    def test_a_satisfied_gate_is_not_a_refusal(self):
        with pytest.raises(ValueError):
            refusal_for(GateStatus.SATISFIED, "all good")

    def test_it_is_still_a_string_everywhere_it_used_to_be(self):
        """The type exists so the classifier stops reading words — not so that
        every logger, f-string and DB column has to learn a new type."""
        r = refusal_for(GateStatus.INSTRUMENT_ERROR, "boom")
        assert isinstance(r, str)
        assert f"⛔ {r}" == "⛔ boom"
        assert r.startswith("boom")

    def test_a_note_keeps_the_kind(self):
        """Refusals collect context on the way out (the bookkeeping commit that
        failed, work stranded in the tree). Plain concatenation would return an
        ordinary `str` and the answer would be gone."""
        noted = refusal_for(GateStatus.INSTRUMENT_ERROR, "boom").with_note("and the flip failed")
        assert noted.kind is RefusalKind.INSTRUMENT
        assert noted.error_code is ErrorCode.INFRASTRUCTURE
        assert "and the flip failed" in noted

    def test_the_classifier_prefers_the_kind_over_the_words(self):
        """A message that looks like a policy refusal but is typed as an
        instrument error is an instrument error. The old code could only have
        got this wrong."""
        from spec_runner.execution import _refusal_error_code

        disguised = Refusal("Pre-terminal gate unsatisfied: 2 findings", RefusalKind.INSTRUMENT)
        assert _refusal_error_code(disguised) is ErrorCode.INFRASTRUCTURE

    def test_an_untyped_string_still_classifies_by_prefix(self):
        """The legacy path stays for refusals this classifier does not own."""
        from spec_runner.execution import _refusal_error_code
        from spec_runner.hooks import GATE_INSTRUMENT_ERROR_PREFIX

        assert _refusal_error_code(f"{GATE_INSTRUMENT_ERROR_PREFIX}: x") is ErrorCode.INFRASTRUCTURE
        assert _refusal_error_code("Tests failed: 3 of 40") is ErrorCode.HOOK_FAILURE


# --- end to end, through the CLI -----------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


#: Writes a failing test and reports its selector — a RED authoring pass.
RED_AGENT = """#!/bin/bash
mkdir -p tests
cat > tests/test_x.py <<'EOF'
def test_y():
    assert {assertion}
EOF
echo "TDD_SELECTOR: tests/test_x.py::test_y"
"""

FAILING_AGENT = '#!/bin/bash\necho "TASK_FAILED: no"\nexit 1\n'


def _tdd_project(
    tmp_path: Path, *, assertion: str, test_command: str, name: str = "proj", tasks: int = 1
) -> Path:
    root = tmp_path / name
    (root / "spec").mkdir(parents=True)
    _git(root.parent, "init", "-q", str(root))
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")

    agent = root / "agent.sh"
    agent.write_text(RED_AGENT.format(assertion=assertion))
    agent.chmod(0o755)

    body = "# Tasks\n"
    for i in range(1, tasks + 1):
        body += f"\n### TASK-00{i}: task {i}\n🟠 P1 | ⬜ TODO\nEst: 1d\n\n- [ ] do it\n"
    (root / "spec" / "tasks.md").write_text(body)

    (root / "spec-runner.config.yaml").write_text(
        f"""claude_command: {agent}
command_template: "{{cmd}} -p {{prompt}}"
max_retries: 1
execution_mode: tdd
tdd_runner: pytest
commands:
  test: "{test_command}"
hooks:
  pre_start:
    create_git_branch: false
    sync_deps: false
  post_done:
    run_tests: false
    run_lint: false
    run_review: false
    auto_commit: false
"""
    )
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


def _run_cli(cwd: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", "from spec_runner.cli import main; main()", *argv],
        cwd=cwd,
        capture_output=True,
        text=True,
    )


def _last_attempt(root: Path) -> tuple[str, str]:
    import sqlite3

    db = root / "spec" / ".executor-state.db"
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT error_code, error FROM attempts ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return (row[0] or ""), (row[1] or "")


@pytest.mark.slow
class TestABrokenInstrumentExitsTwo:
    def test_an_unusable_replay_environment_is_infrastructure(self, tmp_path):
        """A composite `test_command` cannot be narrowed to one test, so the
        replay refuses **without running** — nothing was ever learned about the
        work. That is the shape of the pilot's attempt 1."""
        root = _tdd_project(
            tmp_path, assertion="False", test_command=f"{sys.executable} -m pytest && true"
        )

        result = _run_cli(root, "run", "--task=TASK-001")

        code, error = _last_attempt(root)
        assert code == "INFRASTRUCTURE", f"recorded {code}: {error}"
        assert result.returncode == 2, f"exit {result.returncode}\n{result.stdout}{result.stderr}"


@pytest.mark.slow
class TestARefusedVerdictExitsOne:
    def test_a_red_that_does_not_fail_is_the_works_problem(self, tmp_path):
        """The instrument worked perfectly and answered: this test passes, so
        it is not a red. Nothing is broken — the work is not ready."""
        root = _tdd_project(
            tmp_path, assertion="True", test_command=f"{sys.executable} -m pytest"
        )

        result = _run_cli(root, "run", "--task=TASK-001")

        code, error = _last_attempt(root)
        assert code == "HOOK_FAILURE", f"recorded {code}: {error}"
        assert "did not fail on replay" in error
        assert result.returncode == 1, f"exit {result.returncode}\n{result.stdout}{result.stderr}"


@pytest.mark.slow
class TestAMixedRun:
    def test_a_product_failure_outranks_a_broken_instrument(self, tmp_path):
        """Already the declared semantics of `run_exit_code` — pinned here
        because it is now reachable: before this fix no RED-site instrument
        error could ever reach the infrastructure counter, so the mixed case
        could not occur in a TDD run at all."""
        from spec_runner.cli import run_exit_code

        assert run_exit_code(failed=1, infrastructure=1, prior=0) == 1
        assert run_exit_code(failed=0, infrastructure=1, prior=0) == 2
