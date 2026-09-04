"""Acceptance tests for BEH-02 and BEH-25 (spec-runner#341, TASK-002).

BEH-02 (contract): a lint fix that rewrites the red-file after the authored
diff was committed as the candidate must not leave the checkpoint pointing at
stale bytes. The checkpoint commit's own bytes, the bytes the replay ran
against, and the bytes the claim byte-locks are the *same* bytes — checked
machine-side, by reading the checkpoint SHA and the file content at that SHA,
never by watching the log.

BEH-25 (integration): an operator reading `git log` around the checkpoint can
tell which bytes the agent authored and which bytes the fix added — the
amend (Q-04) is subject-preserving, so the diff lives in the message body,
not only in the merged end state.

Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-02
Source: workstreams/WS-spec-runner-341/spec/15-behaviour-spec.md#BEH-25
`checked_by`: kind=contract, owner=qa, target=tests/test_frozen_bytes_are_the_replayed_bytes.py (BEH-02)
`checked_by`: kind=integration, owner=qa, target=tests/test_frozen_bytes_are_the_replayed_bytes.py (BEH-25)
"""

import shlex
import subprocess
import sys
from pathlib import Path

from spec_runner.claims import check_claims, claim_blob_sha
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import RedOutcome, resolve_namespace, run_red_phase

_CHECK_SCRIPT = """
import sys
from pathlib import Path

bad = any("BADWORD" in Path(p).read_text() for p in sys.argv[1:])
sys.exit(1 if bad else 0)
"""

_FIX_SCRIPT = """
import sys
from pathlib import Path

for p in sys.argv[1:]:
    path = Path(p)
    path.write_text(path.read_text().replace("BADWORD", ""))
"""


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "README.md").write_text("x\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _shell_command(script_path: Path) -> str:
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(script_path))}"


def _cfg(root: Path, lint_command: str, lint_fix_command: str) -> ExecutorConfig:
    cfg = ExecutorConfig(
        project_root=root,
        state_file=root / ".state.db",
        logs_dir=root / ".logs",
        execution_mode="tdd",
        test_command="python -m pytest",
        lint_command=lint_command,
        lint_command_declared=True,
        lint_fix_command=lint_fix_command,
        lint_fix_command_declared=True,
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task() -> Task:
    return Task(id="TASK-002", name="t", priority="p1", status="todo", estimate="1h")


def _agent_writing_a_fixable_red(monkeypatch) -> None:
    from spec_runner import tdd

    def fake(config, prompt, **kwargs):
        path = Path(config.project_root) / "tests/test_x.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_y():  # BADWORD\n    assert False\n")
        return tdd.AgentCall(text="TDD_SELECTOR: tests/test_x.py::test_y")

    monkeypatch.setattr(tdd, "_run_agent", fake)


def _run(tmp_path_factory, monkeypatch):
    root = _repo(tmp_path_factory.mktemp("proj"))
    scripts = tmp_path_factory.mktemp("scripts")
    check_script = scripts / "check_lint.py"
    check_script.write_text(_CHECK_SCRIPT)
    fix_script = scripts / "fix_lint.py"
    fix_script.write_text(_FIX_SCRIPT)

    cfg = _cfg(
        root,
        lint_command=_shell_command(check_script),
        lint_fix_command=_shell_command(fix_script),
    )
    _agent_writing_a_fixable_red(monkeypatch)

    with ExecutorState(cfg) as state:
        result = run_red_phase(_task(), cfg, state)
        claims = state.active_claims(resolve_namespace(cfg))

    return root, cfg, result, claims


class TestFrozenBytesAreTheReplayedBytes:
    """BEH-02 (contract): checkpoint bytes, replayed bytes and byte-locked
    bytes are the same bytes — proven by reading the checkpoint SHA, not by
    observing the log."""

    def test_checkpoint_replay_and_claim_agree_on_the_fixed_bytes(
        self, tmp_path_factory, monkeypatch
    ):
        root, cfg, result, claims = _run(tmp_path_factory, monkeypatch)

        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
        assert result.checkpoint is not None
        checkpoint = result.checkpoint

        # The checkpoint SHA is HEAD: the replay judged this exact commit, not
        # a later or earlier one.
        assert checkpoint.commit_sha == _git(root, "rev-parse", "HEAD").stdout.strip()

        # The bytes the checkpoint commit holds are the fixed bytes, not the
        # authored (BADWORD-carrying) ones.
        committed = subprocess.run(
            ["git", "show", f"{checkpoint.commit_sha}:tests/test_x.py"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "BADWORD" not in committed
        assert committed == (root / "tests/test_x.py").read_text()

        # The claim byte-locks exactly the checkpoint commit's blob — read via
        # `git hash-object`, the same authority the claims gate uses, not a
        # string comparison.
        assert [c.path for c in claims] == ["tests/test_x.py"]
        assert claims[0].blob_sha == claim_blob_sha(root, "tests/test_x.py")

        # And the claims gate, replaying that authority against the
        # checkpoint SHA, finds no mismatch — the replayed tree is the
        # byte-locked tree.
        with ExecutorState(cfg) as state:
            violations = check_claims(cfg, state, resolve_namespace(cfg), checkpoint.commit_sha)
        assert violations == []


class TestFixDiffIsPresentableInHistory:
    """BEH-25 (integration): the fix's own bytes are legible in the checkpoint
    commit's history, distinct from what the agent authored."""

    def test_the_diff_names_what_the_fix_added_and_adoptability_survives(
        self, tmp_path_factory, monkeypatch
    ):
        root, cfg, result, claims = _run(tmp_path_factory, monkeypatch)

        assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
        checkpoint = result.checkpoint
        assert checkpoint is not None

        message = _git(root, "log", "-1", "--format=%B", checkpoint.commit_sha).stdout
        subject = message.splitlines()[0]
        # Subject-preserving amend (Q-04): the checkpoint is still findable by
        # the exact string `_unregistered_red` matches against.
        assert subject == "TASK-002: red for tests/test_x.py::test_y"

        body = message[len(subject) :]
        assert "-def test_y():  # BADWORD" in body
        assert "+def test_y():  #" in body

        # And: the subject `_unregistered_red` looks for is exactly what
        # `git log --format=%s` reports — the trailer body does not leak into
        # it and does not break adoption of a future rejected remainder.
        subject_only = _git(root, "log", "-1", "--format=%s", checkpoint.commit_sha).stdout.strip()
        assert subject_only == subject
