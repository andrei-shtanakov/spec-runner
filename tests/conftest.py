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
