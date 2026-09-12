"""Shared pytest configuration for spec-runner tests."""

from __future__ import annotations

import asyncio
import shlex
import subprocess
from pathlib import PurePath

import pytest

from spec_runner.spec import StageDef, StageProfile

#: A name that exists in no PATH, reserved for tests that must drive a seam
#: all the way to its subprocess call. Pointing `claude_command` (or the
#: review command) at this instead of a real CLI means the worst case of a
#: broken belt is `FileNotFoundError`, never a bill.
BELT_PROBE_COMMAND = "spec-runner-belt-probe"


class PaidBinaryReached(BaseException):
    """The belt fired: a paid agent binary was about to be executed.

    Deliberately **not** an `Exception`. `execute_task` and several other
    call paths catch `Exception` and turn it into a failed attempt, so a belt
    raising one would be swallowed: the test would fail later, for another
    reason, and the belt's own verdict would be lost. Nothing in the product
    catches and *swallows* `BaseException` — the four handlers that name it
    (`spec.py`, `obs.py`, `change_commands.py`) re-raise after cleanup.
    """


#: The belt's OWN list of CLI names, deliberately a literal rather than a
#: reference to `PAID_AGENT_COMMANDS` below.
#:
#: A belt keyed on the constant whose guard is under test is not a belt:
#: weakening that constant is both how the guard fails in reality and what a
#: reviewer does to check it, so a shared list switches the guard and the belt
#: off in the same stroke. Measured the hard way (spec-runner#455,
#: 2026-09-12): dropping "claude" from `PAID_AGENT_COMMANDS` while the belt
#: read that same constant executed the real agent — seven sessions across
#: three runs, including one through the then-unguarded review seam.
#:
#: Kept deliberately broad, and a superset of `PAID_AGENT_COMMANDS`: an extra
#: name here costs nothing, a missing one costs money. `test_harness_guards`
#: pins that relationship statically, without executing anything.
_NEVER_EXECUTE = frozenset(
    {
        "claude",
        "claude-code",
        "codex",
        "opencode",
        "pi",
        "ollama",
        "llama-cli",
        # Listed for completeness, and unreachable by name: that branch issues
        # `curl http://localhost:8080/completion`, so no argv element carries
        # this basename. Harmless to keep, misleading to rely on.
        "llama-server",
        "qwen",
        "copilot",
        "gemini",
        "aider",
        "cursor-agent",
        BELT_PROBE_COMMAND,
    }
)


def _argv_names(argv) -> set[str]:
    """Every basename visible in `argv`, including inside composite elements.

    Reading `PurePath(part).name` per element is not enough, and the two cases
    it misses are the ones that matter (spec-runner#459 review):

    * a wrapped template (`command_template: bash -lc '{cmd} -p {prompt}'`)
      goes through `shlex.split` in `build_cli_invocation`, so the agent name
      ends up INSIDE one element: `["bash", "-lc", "claude -p '…'"]`. The
      basename of that third element is the whole string;
    * `subprocess.run("<cmd> --flag", shell=True)` passes one string.

    So each element is also split as a shell word vector and every token's
    basename is taken. An element that does not lex (an unbalanced quote in a
    prompt, say) falls back to a plain whitespace split: a malformed element
    must not become a way to hide a name, which is what returning the raw
    string alone would have made it.

    Not every paid path is an argv with a name in it: the llama-server branch
    sends `["curl", "-s", "http://localhost:8080/completion", …]`, where no
    element has an agent basename. That request is not caught here, and the
    list below does not pretend otherwise — it is an HTTP call to a local
    server, not the execution of a paid CLI binary.
    """
    parts = argv if isinstance(argv, (list, tuple)) else [argv]
    names: set[str] = set()
    for part in parts:
        text = str(part)
        names.add(PurePath(text).name)
        try:
            tokens = shlex.split(text)
        except ValueError:
            tokens = text.split()
        names.update(PurePath(token).name for token in tokens)
    return names


@pytest.fixture(autouse=True)
def _belt_never_executes_a_paid_binary(monkeypatch):
    """Run-wide belt: no test may execute a paid agent CLI, ever.

    `_no_real_agent_calls` below guards the two *seams* by name, which is the
    first line and the one that produces a readable message. This is the
    second line, underneath it, at the point where a process would actually
    be created — `subprocess.run`, `subprocess.Popen` and
    `asyncio.create_subprocess_exec` (runner.py's streaming path). It exists
    because the first line has failure modes of its own: a seam it does not
    cover (the review seam had none until spec-runner#455), a test that
    replaces the patched seam itself, or `PAID_AGENT_COMMANDS` losing a name.

    Everything else passes through untouched: git, pytest, mix, fake scripts
    under tmp_path — only a known agent name is refused.
    """
    real_run = subprocess.run
    real_popen = subprocess.Popen
    real_exec = asyncio.create_subprocess_exec

    def _refuse(argv) -> None:
        hit = _argv_names(argv) & _NEVER_EXECUTE
        if hit:
            raise PaidBinaryReached(
                f"belt: a test was about to execute a paid agent binary "
                f"({sorted(hit)}) — argv={argv!r}. Point `claude_command` at a "
                f"fake script under tmp_path, or stub the seam."
            )

    def _belted_run(argv, *args, **kwargs):
        _refuse(argv)
        return real_run(argv, *args, **kwargs)

    def _belted_popen(argv, *args, **kwargs):
        _refuse(argv)
        return real_popen(argv, *args, **kwargs)

    async def _belted_exec(program, *argv, **kwargs):
        _refuse([program, *argv])
        return await real_exec(program, *argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", _belted_run)
    monkeypatch.setattr(subprocess, "Popen", _belted_popen)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _belted_exec)


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


def _real_agent_refusal(cmd: str, cls: type[AssertionError] = AssertionError) -> AssertionError:
    return cls(
        f"this test would call the real agent ({cmd!r}) and be billed for it. "
        "Stub `spec_runner.tdd._run_agent` / `spec_runner.execution._run_agent_process`, "
        "or point `claude_command` at a fake script under tmp_path."
    )


@pytest.fixture(autouse=True)
def _no_real_agent_calls(monkeypatch):
    """Fail a test that would invoke a real agent, instead of billing for it.

    Written after this suite spent $0.55: a new test drove the RED phase with
    the default `claude_command`, the checkpoint it planted turned out not to
    be reusable, and the phase did exactly what it is supposed to do — it
    called `claude`. Nothing was wrong with the product; the test was missing
    one `monkeypatch.setattr`, and the only signal was a minute of silence.

    The guard covers TWO of the paid seams by name: `tdd._run_agent` for the
    TDD red/fix passes and `execution._run_agent_process` for the standard
    execution path (#341/#334 BEH-24: the second seam that used to have no
    guard at all). It still does NOT cover the review seam
    (`review._run_reviewer`, also reached from `post_done_hook` with
    `run_review=True`) or the plan/review-pr seams — but since
    spec-runner#455 those are no longer unprotected: they are caught one
    level down by `_belt_never_executes_a_paid_binary` above, at process
    creation, which is exactly the case that seam-by-seam guarding keeps
    missing. The guard fires only on a **known agent name**: a fake script
    (an absolute path under `tmp_path`) runs as before, and a test that stubs
    either seam itself replaces this patch and never sees it — the belt
    underneath still applies.
    """
    from spec_runner import execution, tdd

    def _refuse_tdd(config, prompt, **kwargs):
        cmd = getattr(config, "claude_command", "")
        if cmd in PAID_AGENT_COMMANDS:
            raise _real_agent_refusal(cmd)
        return _real_run_agent(config, prompt, **kwargs)

    def _refuse_execution(config, invocation, **kwargs):
        # Keyed on the invocation's own argv[0], not `config.claude_command`:
        # `build_cli_invocation` is a separate, commonly-stubbed seam, and a
        # test that points it at a harmless real binary (`true`, `echo`) while
        # `claude_command` stays at its default must still be allowed through —
        # what would actually run is what decides, not the config value that
        # produced it.
        argv = getattr(invocation, "argv", None) or []
        # A wrapped template (`bash -lc '{cmd} …'`) or the llama-server
        # branch (`curl …`) hides the agent name deeper in argv, and argv[0]
        # alone would wave the paid call through (#363 review). Refuse when
        # a known agent name is visible ANYWHERE in argv, by basename.
        from pathlib import PurePath

        cmd = argv[0] if argv else ""
        visible = {PurePath(str(part)).name for part in argv}
        if cmd in PAID_AGENT_COMMANDS or visible & PAID_AGENT_COMMANDS:
            # `execution.RealAgentCallRefused`, not a bare `AssertionError`:
            # `execute_task`'s `try` block also reaches genuine internal
            # asserts (harness/stage invariants), and only this guard's own
            # exception may propagate as an uncaught test failure.
            raise _real_agent_refusal(cmd, cls=execution.RealAgentCallRefused)
        return _real_run_agent_process(config, invocation, **kwargs)

    _real_run_agent = tdd._run_agent
    monkeypatch.setattr(tdd, "_run_agent", _refuse_tdd)

    _real_run_agent_process = execution._run_agent_process
    monkeypatch.setattr(execution, "_run_agent_process", _refuse_execution)


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
