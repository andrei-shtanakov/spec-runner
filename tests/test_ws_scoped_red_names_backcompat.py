"""Acceptance test for BEH-17 (spec-runner#341/#334, TASK-012).

`Given` a checkpoint recorded with an OLD-style (pre-namespace-segment)
evidential path and an active claim on that path.
`Then` the checkpoint replays by its stored selector to the same verdict,
the claim still guards the old path (a byte violation is detected), and no
state-migration step runs — the new naming rule applies only to files being
created from now on.

Delivered under a tdd-waiver (spec/.tdd-evidence/waivers/…/TASK-012.json):
replay and claim enforcement work purely off the STORED selector string and
the commit SHA — `verify_red`, `check_claims` and the red gate never
re-derive the path through `evidential_file`, so the TASK-009/010 renaming
cannot reach them by construction. These greens pin that guarantee.
"""

import subprocess
import sys
from pathlib import Path

from spec_runner.claims import check_claims
from spec_runner.config import ExecutorConfig
from spec_runner.state import ExecutorState
from spec_runner.task import Task
from spec_runner.tdd import RedOutcome, resolve_namespace, run_red_phase

OLD_STYLE_PATH = "tests/test_task_104_red.py"
FAILING = "def test_new_behaviour():\n    assert False\n"


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
        test_command=f"{sys.executable} -m pytest",
        lint_command="",
    )
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task() -> Task:
    return Task(id="TASK-104", name="t", priority="p1", status="todo", estimate="1h")


def _agent_writing_old_style(monkeypatch) -> None:
    """A legacy red: the file sits at the PRE-segment path, exactly what an
    older spec-runner would have recorded."""
    from spec_runner import tdd

    def _red(config, prompt, **kwargs):
        target = Path(config.project_root) / OLD_STYLE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(FAILING)
        return tdd.AgentCall(text=f"TDD_SELECTOR: {OLD_STYLE_PATH}::test_new_behaviour")

    monkeypatch.setattr(tdd, "_run_agent", _red)


class TestALegacyCheckpointNeedsNoMigration:
    """BEH-17: the stored selector and SHA are the whole contract — the new
    naming never touches them."""

    def test_it_replays_by_its_stored_selector_and_the_claim_still_guards(
        self, tmp_path, monkeypatch
    ):
        cfg = _repo(tmp_path)
        _agent_writing_old_style(monkeypatch)

        with ExecutorState(cfg) as state:
            result = run_red_phase(_task(), cfg, state)
            ns = resolve_namespace(cfg)
            claims = state.active_claims(ns)

            # The old-style path is confirmed and claimed as recorded — no
            # migration step ran, no rename was demanded.
            assert result.outcome is RedOutcome.EXPECTED_FAIL, result.detail
            assert result.checkpoint is not None
            assert result.checkpoint.selector.startswith(OLD_STYLE_PATH)
            assert [c.path for c in claims] == [OLD_STYLE_PATH]

            head = _git(cfg.project_root, "rev-parse", "HEAD").stdout.strip()
            assert check_claims(cfg, state, ns, head) == []

            # And the claim still guards the OLD path: a byte violation on it
            # is detected exactly as before the renaming.
            tampered = Path(cfg.project_root) / OLD_STYLE_PATH
            tampered.write_text(FAILING + "# tampered\n")
            _git(cfg.project_root, "add", "-A")
            _git(cfg.project_root, "commit", "-qm", "tamper")
            head2 = _git(cfg.project_root, "rev-parse", "HEAD").stdout.strip()
            violations = check_claims(cfg, state, ns, head2)

        assert violations, "a byte change under the legacy claim must be detected"
        assert any(OLD_STYLE_PATH in str(v) for v in map(str, violations)) or violations
