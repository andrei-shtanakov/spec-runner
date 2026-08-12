"""Per-runner adapters for RED verification — #198 step 2, build order §1.

A confirmed red is two claims, and only one of them is about an exit code:

```
EXPECTED_FAIL  ⟺  the runner selected the requested test
              AND  that test failed as a test failure
```

The second claim is what `classify` answers, from a table measured per runner.
The first is `prove_selected`, and it exists because on at least one real runner
the exit code cannot carry it: `mix test path:line` selects the *nearest test at
or before* the line, so a line past the end of a file silently runs the last
test and reports an ordinary "1 test, 1 failure". Reading that as a confirmed
red is how a test that never ran became evidence (#198).

Observation and proof are therefore separate types. `RunOutcome` describes the
run as a whole and never names a test; `SelectionProof` answers which test ran.
An adapter cannot assert identity in its easy path and have it checked only in
its hard one.

This module ships with pytest as the only adapter — the same runner step 1
allowed — so behaviour does not change. ExUnit follows in build order §3.

Design: ``docs/superpowers/specs/2026-08-12-tdd-runner-adapter-design.md``
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Protocol

from .logging import get_logger

logger = get_logger("tdd_runners")


class RunOutcome(str, Enum):
    """What the run did, as a whole. Never a claim about *which* test ran."""

    TESTS_PASSED = "tests_passed"
    TESTS_FAILED = "tests_failed"
    #: The runner ran but selected nothing, or the path matched nothing.
    SELECTION_FAILED = "selection_failed"
    COLLECTION_OR_COMPILE_ERROR = "collection_or_compile_error"
    #: The runner itself failed: usage, crash, timeout.
    RUNNER_ERROR = "runner_error"
    #: The adapter does not recognise this output. Deliberately not a fallback
    #: to the exit code — that fallback is the original defect, one level up.
    UNRECOGNIZED = "unrecognized"


class SelectionProof(str, Enum):
    """Whether the *requested* test is what ran."""

    PROVEN = "proven"
    REFUTED = "refuted"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PytestNodeId:
    """pytest's locator: the whole node id, path included."""

    value: str


@dataclass(frozen=True)
class ExUnitDefinitionLine:
    """ExUnit's locator: the line the `test "..." do` is written on.

    Not any line inside the test. A line in the body resolves correctly today
    and identically to a line past the end of the file, so accepting "near
    enough" would accept the case this whole mechanism exists to refuse.
    """

    line: int


@dataclass(frozen=True)
class Selector:
    """A parsed, runner-specific pointer at exactly one test."""

    runner: str
    #: Project-relative and normalised, so `./test/x.exs` and `test/x.exs` are
    #: one selector and comparisons against runner output never turn on a `./`.
    path: PurePosixPath
    locator: PytestNodeId | ExUnitDefinitionLine


@dataclass(frozen=True)
class SelectorRefusal:
    """Why a selector cannot be used. Not an exception: an unusable selector is
    a normal outcome of asking an agent for one, and it belongs in the
    checkpoint record like any other verdict."""

    code: str
    message: str


def normalise_path(raw: str) -> PurePosixPath:
    """A project-relative path with `./` and duplicate separators removed."""
    parts = [p for p in PurePosixPath(raw.strip()).parts if p not in (".", "")]
    return PurePosixPath(*parts) if parts else PurePosixPath("")


class TddRunnerAdapter(Protocol):
    """What a runner must be able to answer for a red to be confirmed."""

    name: str

    def parse_selector(self, raw: str) -> Selector | SelectorRefusal: ...

    def validate_command(self, test_command: str) -> str | None:
        """A refusal, or None when this command can carry this adapter's selector."""
        ...

    def preflight(self, root: Path, selector: Selector) -> SelectorRefusal | None:
        """Check the selector against the source, before anything is executed."""
        ...

    def build_command(self, test_command: str, selector: Selector) -> list[str]:
        """argv — never a shell string, since the selector is agent output."""
        ...

    def classify(self, result: subprocess.CompletedProcess) -> RunOutcome: ...

    def prove_selected(
        self, selector: Selector, result: subprocess.CompletedProcess
    ) -> SelectionProof: ...


# === pytest ===

#: Wrappers that run something else. `uv run pytest` is a pytest run; the
#: runner is the first token that is not one of these.
_RUNNER_WRAPPERS = frozenset(
    {"uv", "run", "poetry", "pipenv", "hatch", "rye", "pdm", "nox", "tox", "-m", "exec"}
)
_PYTHONS = re.compile(r"^python(\d(\.\d+)?)?$")

#: pytest's final line, e.g. `===== 1 failed, 2 passed, 1 skipped in 0.01s =====`.
#: Matched whole, and every count in it is read — an earlier version anchored on
#: the *first* count and so read `1 failed, 97 passed` as "one test ran"
#: (Copilot, PR #201). Measured forms: `1 passed in 0.00s`, `1 failed in 0.01s`,
#: `1 skipped in 0.00s`, `1 xfailed in 0.01s`, and the mixed line above.
_PYTEST_SUMMARY = re.compile(r"^=*\s*((?:\d+ \w+(?:, )?)+) in [\d.]+s", re.MULTILINE)
_PYTEST_COUNT = re.compile(r"(\d+) (\w+)")

#: Words that mean a test **executed**. `skipped` and `deselected` mean it did
#: not, so they cannot prove that the requested test ran — and a claimed red
#: that was skipped must not be retired as `not_red`.
_EXECUTED_WORDS = frozenset({"passed", "failed", "xfailed", "xpassed", "error", "errors"})


def command_tokens(test_command: str) -> list[str]:
    """`test_command` split for inspection, or [] when it will not split."""
    try:
        return shlex.split(test_command or "")
    except ValueError:  # unbalanced quotes — not something to guess about
        return []


def executable_of(test_command: str) -> str | None:
    """The program a command actually runs, past any wrappers."""
    for token in command_tokens(test_command):
        if token.startswith("-") and token != "-m":
            continue
        name = PurePosixPath(token).name
        if name in _RUNNER_WRAPPERS or _PYTHONS.match(name):
            continue
        return name
    return None


def _exactly_one_test_executed(output: str) -> bool:
    """True when pytest's summary accounts for exactly one executed test.

    The whole summary is read, not its first count: `1 failed, 97 passed` is a
    98-test run whose first number is 1, and reading that as proof would let a
    full-suite run stand in for the named test. The single count must also be an
    *executed* outcome — one skipped test is one test that did not run.
    """
    matches = _PYTEST_SUMMARY.findall(output)
    if not matches:
        return False
    counts = _PYTEST_COUNT.findall(matches[-1])
    if len(counts) != 1:
        return False
    number, word = counts[0]
    return number == "1" and word in _EXECUTED_WORDS


class PytestAdapter:
    """pytest, whose exit codes were measured on pytest 8."""

    name = "pytest"

    def parse_selector(self, raw: str) -> Selector | SelectorRefusal:
        value = (raw or "").strip()
        if "::" not in value:
            # `-k`-style names match several tests, and a checkpoint that
            # matches several proves nothing about the one (§3.3).
            return SelectorRefusal(
                "not_a_node_id",
                f"selector {raw!r} is not a node id (expected 'path::test')",
            )
        path = normalise_path(value.split("::", 1)[0])
        if not path.parts:
            return SelectorRefusal("not_a_node_id", f"selector {raw!r} names no file")
        return Selector(runner=self.name, path=path, locator=PytestNodeId(value))

    def validate_command(self, test_command: str) -> str | None:
        if executable_of(test_command) != "pytest":
            return (
                f"test_command {test_command!r} does not run pytest; "
                "set tdd_runner to the runner this project uses"
            )
        return None

    def preflight(self, root: Path, selector: Selector) -> SelectorRefusal | None:
        """None, and the absence is the point.

        A pytest node id that names nothing **cannot** be mistaken for a red: it
        exits 4, never 1. That is the property ExUnit lacks and the reason
        ExUnit needs its definition line proven before the runner is invoked.
        """
        return None

    def build_command(self, test_command: str, selector: Selector) -> list[str]:
        assert isinstance(selector.locator, PytestNodeId)
        return [*command_tokens(test_command), selector.locator.value]

    def classify(self, result: subprocess.CompletedProcess) -> RunOutcome:
        # Measured on pytest 8: an unresolvable node id and a test file with a
        # syntax error both exit 4, not the 5 ("no tests collected") one would
        # guess — 5 is a directory with no tests.
        return {
            0: RunOutcome.TESTS_PASSED,
            1: RunOutcome.TESTS_FAILED,
            4: RunOutcome.COLLECTION_OR_COMPILE_ERROR,
            5: RunOutcome.SELECTION_FAILED,
        }.get(result.returncode, RunOutcome.RUNNER_ERROR)

    def prove_selected(
        self, selector: Selector, result: subprocess.CompletedProcess
    ) -> SelectionProof:
        assert isinstance(selector.locator, PytestNodeId)
        output = f"{result.stdout or ''}\n{result.stderr or ''}"
        if selector.locator.value in output:
            # The failure header carries the node id verbatim — measured:
            # `FAILED tests/test_p.py::test_bad - assert False`.
            return SelectionProof.PROVEN
        if _exactly_one_test_executed(output):
            # A passing run prints `1 passed in 0.00s` and no node id. Exactly
            # one test ran, and pytest cannot silently substitute a different
            # one — a node id that resolves to nothing exits 4.
            return SelectionProof.PROVEN
        return SelectionProof.UNKNOWN


#: Adapters by name. Adding one means measuring a runner, not assuming it
#: behaves like another (#198).
ADAPTERS: dict[str, TddRunnerAdapter] = {PytestAdapter.name: PytestAdapter()}


def adapter_for(name: str) -> TddRunnerAdapter | None:
    return ADAPTERS.get(name)


def infer_adapter(test_command: str) -> TddRunnerAdapter | None:
    """The adapter a command unambiguously implies, or None.

    Inference is allowed only where it cannot be wrong: an executable that *is*
    a known runner's. Everything else must be declared, because guessing is
    what turned a test that never ran into a confirmed red.
    """
    return adapter_for(executable_of(test_command) or "")


__all__ = [
    "ADAPTERS",
    "ExUnitDefinitionLine",
    "PytestAdapter",
    "PytestNodeId",
    "RunOutcome",
    "SelectionProof",
    "Selector",
    "SelectorRefusal",
    "TddRunnerAdapter",
    "adapter_for",
    "command_tokens",
    "executable_of",
    "infer_adapter",
    "normalise_path",
]
