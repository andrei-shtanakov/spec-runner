"""Shared pytest configuration for spec-runner tests."""

from __future__ import annotations

import pytest

from spec_runner.spec import StageDef, StageProfile


@pytest.fixture
def acceptance_profile() -> StageProfile:
    """A non-lite profile whose final stage is absent from the lite chain.

    Mirrors lite's marker prefixes and validator keys so no new bundled
    template is needed; only the final stage name differs.
    """
    return StageProfile(
        name="acceptance",
        stages=(
            StageDef(
                name="requirements",
                template="requirements.template.md",
                marker_prefix="SPEC_REQUIREMENTS",
                validator_key="requirements",
            ),
            StageDef(
                name="design",
                template="design.template.md",
                marker_prefix="SPEC_DESIGN",
                validator_key="design",
                upstream=("requirements",),
            ),
            StageDef(
                name="acceptance",
                template="tasks.template.md",
                marker_prefix="SPEC_TASKS",
                validator_key="tasks",
                upstream=("design",),
            ),
        ),
    )


@pytest.fixture(autouse=True)
def _isolate_gate_registry():
    """Restore the process-wide gate registry after every test.

    `gates.REGISTRY` is global by design, and `execute_task` attaches the TDD
    gates to it through `ensure_red_gate` — so any test that runs a `tdd` task
    leaves them registered for whatever runs next. `has_gates()` then reads
    True in an unrelated test, which changes real behaviour: `post_done_hook`
    commits a pre-review candidate only when a gate exists to judge it.

    Found by `test_no_pre_review_commit_when_review_off`, which passed alone
    and failed after the new budget-guard tests — the failure mode gates.py's
    own comment names: "a global that tests must mutate is how order-dependent
    suites are born". Restoring here fixes the whole class rather than the one
    test that happened to sit downstream of it.
    """
    from spec_runner.gates import REGISTRY

    saved = {phase: list(gates) for phase, gates in REGISTRY._gates.items()}
    yield
    REGISTRY._gates.clear()
    REGISTRY._gates.update(saved)


#: CLI names that mean a real, paid agent. A test that reaches one of these is
#: spending money; a test that points `claude_command` at a script under
#: `tmp_path` is not, and neither is one that stubs the seam.
PAID_AGENT_COMMANDS = frozenset(
    {"claude", "claude-code", "codex", "opencode", "pi", "ollama", "llama-cli", "qwen", "copilot"}
)


@pytest.fixture(autouse=True)
def _no_real_agent_calls(monkeypatch):
    """Fail a test that would invoke a real agent, instead of billing for it.

    Written after this suite spent $0.55: a new test drove the RED phase with
    the default `claude_command`, the checkpoint it planted turned out not to
    be reusable, and the phase did exactly what it is supposed to do — it
    called `claude`. Nothing was wrong with the product; the test was missing
    one `monkeypatch.setattr`, and the only signal was a minute of silence.

    The guard sits on the seam every paid call goes through, and it fires only
    on a **bare known-agent name**: a fake script (an absolute path under
    `tmp_path`) runs as before, and a test that stubs `_run_agent` itself
    replaces this patch and never sees it.
    """
    from spec_runner import tdd

    def _refuse(config, prompt, **kwargs):
        cmd = getattr(config, "claude_command", "")
        if cmd in PAID_AGENT_COMMANDS:
            raise AssertionError(
                f"this test would call the real agent ({cmd!r}) and be billed for it. "
                "Stub `spec_runner.tdd._run_agent`, or point `claude_command` at a fake "
                "script under tmp_path."
            )
        return _real_run_agent(config, prompt, **kwargs)

    _real_run_agent = tdd._run_agent
    monkeypatch.setattr(tdd, "_run_agent", _refuse)


@pytest.fixture
def anyio_backend() -> str:
    """Restrict anyio-marked async tests to the asyncio backend (no trio)."""
    return "asyncio"


def pytest_addoption(parser) -> None:
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Regenerate golden fixtures under tests/fixtures/",
    )
