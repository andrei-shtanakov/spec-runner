"""RED for BEH-10: an unresolvable adapter and a composite `test_command`
must each surface as `instrument-error` — a third value distinct from
`green` and `test-failure` — not as a generic boolean failure that reads
the same as a genuine test failure.

Source: workstreams/WS-spec-runner-367/spec/15-behaviour-spec.md#BEH-10
Traces: FR-07, FR-15

`run_live_verify` already refuses both cases today (composite `test_command`,
no adapter resolvable) — but only via `ran=False, passed=False`, the exact
same shape a genuine `TESTS_FAILED` replay never produces either (`ran=True,
passed=False`) NOR distinguishes from the third outcome this behaviour
requires. Nothing on `VerifyRunResult` names *which* of the three outcomes
(green / test-failure / instrument-error) a result is — so this fails on a
plain missing-attribute assertion, not an import error, not a crash.
"""

import subprocess
from pathlib import Path

from spec_runner.config import ExecutorConfig
from spec_runner.live_verify import run_live_verify
from spec_runner.task import Task


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_group.py").write_text("def test_it():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


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
        "auto_commit": False,
        "run_review": False,
        "callback_url": "",
    }
    defaults.update(overrides)
    cfg = ExecutorConfig(**defaults)
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    return cfg


def _task(**overrides) -> Task:
    defaults: dict = {
        "id": "TASK-101",
        "name": "verify-first task",
        "priority": "p1",
        "status": "todo",
        "estimate": "1h",
        "execution_mode": "verify_first",
        "verifies": ["tests/test_group.py::test_it"],
    }
    defaults.update(overrides)
    return Task(**defaults)


class TestUnresolvableAdapterAndCompositeCommandAreInstrumentErrors:
    """kind: integration — BEH-10: both pre-run refusals must be reported
    through the same named `instrument-error` outcome, not through a
    boolean shape that a real test failure could also produce."""

    def test_both_cases_report_the_instrument_error_outcome(self, tmp_path):
        root = _repo(tmp_path)
        task = _task()

        composite_config = _cfg(root, test_command="python -m pytest tests/ && echo done")
        composite_result = run_live_verify(task, composite_config)

        unresolvable_config = _cfg(root, test_command="some-unrecognizable-runner --frobnicate")
        unresolvable_result = run_live_verify(task, unresolvable_config)

        assert getattr(composite_result, "outcome", None) == "instrument_error", (
            "a composite test_command must classify explicitly as the named "
            "'instrument_error' outcome (FR-15) — narrowing 'a && b && c' to a "
            "selector cannot be guessed, and a caller reading `ran`/`passed` "
            f"alone cannot tell this apart from a real failure: {composite_result!r}"
        )
        assert getattr(unresolvable_result, "outcome", None) == "instrument_error", (
            "an unresolvable adapter must classify explicitly as the named "
            "'instrument_error' outcome (FR-07) rather than the same "
            f"`ran`/`passed` shape a real failure would also produce: "
            f"{unresolvable_result!r}"
        )
