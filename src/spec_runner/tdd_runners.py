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

import hashlib
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Protocol
from uuid import uuid4

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


@dataclass(frozen=True)
class ReplayEnvironment:
    """A proven, isolated environment for one replay (#207).

    `env` is overlaid on the process environment for the test run.
    `environment_id` is what the checkpoint records — richer than a lockfile
    hash, because "the same lock" is not "the same environment".
    `cleanup_paths` are removed when the replay ends, on every path.
    """

    env: Mapping[str, str]
    environment_id: str
    cleanup_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ReplayEnvironmentRefusal:
    """Why a replay cannot be run here.

    A refusal is never a red and never a pass. Preparation **proves and
    isolates** what is installed; it does not download, generate or repair
    anything, so "the environment is not there" stays the operator's problem
    rather than becoming a silent network call inside a gate.
    """

    code: str
    message: str


#: Lockfiles that identify an environment, most specific first. The order is
#: fixed so the answer is deterministic when a repo carries more than one.
LOCKFILES = (
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "requirements.txt",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "go.sum",
    "mix.lock",
)


def lockfile_identity(project_root: Path) -> str:
    """``"<lockfile>:<hash>"``, or ``"unpinned"``.

    The generic identity, used by adapters that have nothing richer to say.
    Saying "unpinned" is honest and keeps TDD mode available to projects that
    pin nothing; inventing an identity would not be.
    """
    for name in LOCKFILES:
        candidate = project_root / name
        if candidate.is_file():
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()[:16]
            return f"{name}:{digest}"
    return "unpinned"


class TddRunnerAdapter(Protocol):
    """What a runner must be able to answer for a red to be confirmed."""

    name: str

    #: How the RED prompt must ask for a selector. The agent writes the test;
    #: the shape it reports has to be the shape this adapter parses, or every
    #: RED is refused before it is replayed.
    selector_instruction: str

    def parse_selector(self, raw: str) -> Selector | SelectorRefusal: ...

    def validate_command(self, test_command: str) -> str | None:
        """A refusal, or None when this command can carry this adapter's selector."""
        ...

    def preflight(self, root: Path, selector: Selector) -> SelectorRefusal | None:
        """Check the selector against the source, before anything is executed."""
        ...

    def prepare_replay(
        self, canonical_root: Path, replay_root: Path, selector: Selector
    ) -> ReplayEnvironment | ReplayEnvironmentRefusal:
        """Prove and isolate the environment the replay will run in."""
        ...

    def claim_paths(self, selector: Selector) -> tuple[PurePosixPath, ...]:
        """The files this selector depends on, for the byte-lock."""
        ...

    def evidential_file(self, task_id: str) -> PurePosixPath:
        """Where this task's RED should write its failing test (#252).

        The **adapter** names it, never a shared heuristic: a Python-shaped
        guess is how an Elixir suite was told to write `tests/test_x_red.py`,
        and the same class of mistake as #198 and #220. The name it returns
        must be one this runner's ordinary discovery picks up — a test nothing
        collects is a red that cannot be replayed.
        """
        ...

    def is_discoverable(self, path: PurePosixPath) -> bool:
        """Whether this runner's ordinary discovery would collect ``path``.

        Asked of the file the red actually claimed, so a red written somewhere
        the runner never looks is refused while it is still cheap — rather than
        replayed, found to select nothing, and recorded `unverifiable`.
        """
        ...

    def contract_selectors(self) -> tuple[str, ...]:
        """Canonical selectors this adapter must parse. The machine contract,
        kept apart from the human-readable `selector_instruction` so that
        rewording the prompt cannot silently change what is guaranteed."""
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
    selector_instruction = (
        "TDD_SELECTOR: path/to/test_file.py::TestClass::test_name\n"
        "\n"
        "   The full pytest node id. Not a `-k` expression and not a bare name:\n"
        "   those match several tests, and a checkpoint that matches several\n"
        "   proves nothing about the one."
    )

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

    def claim_paths(self, selector: Selector) -> tuple[PurePosixPath, ...]:
        """One node id names one file.

        **Documented limitation** (§1.3): a test depending on a fixture in
        `conftest.py` does not claim that conftest.
        """
        return (selector.path,)

    def evidential_file(self, task_id: str) -> PurePosixPath:
        """`tests/test_<task>_red.py` — collected by pytest's default
        `test_*.py`, and under `tests/`, which is where a pytest project's
        discovery is rooted by convention."""
        slug = task_id.strip().lower().replace("-", "_") or "task"
        return PurePosixPath("tests") / f"test_{slug}_red.py"

    def is_discoverable(self, path: PurePosixPath) -> bool:
        """pytest's default patterns: `test_*.py` or `*_test.py`."""
        name = path.name
        return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))

    def contract_selectors(self) -> tuple[str, ...]:
        return (
            "tests/test_thing.py::test_it",
            "tests/test_thing.py::TestGroup::test_it",
        )

    def prepare_replay(
        self, canonical_root: Path, replay_root: Path, selector: Selector
    ) -> ReplayEnvironment | ReplayEnvironmentRefusal:
        """Passthrough: a Python environment lives outside the checkout.

        That is exactly why pytest never met #207 — a bare worktree can run
        tests because site-packages is somewhere else entirely.
        """
        return ReplayEnvironment(env={}, environment_id=lockfile_identity(canonical_root))

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


# === ExUnit ===

#: The whole point of the preflight, in one script: ask **Elixir** where the
#: tests are, rather than teaching Python to read Elixir. The path arrives via
#: `System.argv()` and is never interpolated into source.
_DEFINITION_LINES_SCRIPT = """
[path] = System.argv()
case Code.string_to_quoted(File.read!(path)) do
  {:ok, ast} ->
    {_, lines} = Macro.prewalk(ast, [], fn
      {:test, meta, [_ | _]} = n, acc -> {n, [meta[:line] | acc]}
      n, acc -> {n, acc}
    end)
    lines |> Enum.reverse() |> Enum.join(",") |> IO.puts()
  {:error, _} -> IO.puts("PARSE_ERROR")
end
"""

#: ExUnit's summary, e.g. `1 test, 1 failure (2 excluded)` or `0 tests, 0 failures`.
_EXUNIT_SUMMARY = re.compile(r"^(\d+) (?:doctest|test)s?, (\d+) failures?", re.MULTILINE)

#: The location line under a numbered failure:
#:     1) test calls missing module (ProbeTest)
#:        test/probe_test.exs:9
_EXUNIT_FAILURE_AT = re.compile(r"^\s+\d+\) test .*\n\s+(\S+):(\d+)\s*$", re.MULTILINE)

#: `mix test --trace` prints two lines per test — a start and a result — each
#: carrying the test's **definition line**:
#:
#:     * test fails [L#6]                 <- start, every test gets one
#:     * test fails (excluded) [L#6]      <- result: not run
#:     * test passes [L#3]
#:     * test passes (0.00ms) [L#3]       <- result: ran, in 0.00ms
#:
#: So "executed" is the **timed** result, not "a line without (excluded)" —
#: measured, after the first version of this rule read every start line as an
#: execution and refuted everything. A timing is the only thing that means the
#: test ran; `(excluded)` and `(skipped)` mean it did not.
#:
#: This is direct proof, and version-independent: for `:999` the timed entry
#: reads `[L#9]`, refuting the claim outright rather than leaving it to be
#: inferred from a count — which is what the earlier summary-counting rule did,
#: and it disagreed between Elixir 1.18 and 1.19. CI caught that.
_EXUNIT_TRACE_EXECUTED = re.compile(r"\(\d+(?:\.\d+)?ms\)\s*\[L#(\d+)\]")

#: Appended so the trace above exists. It also serialises the run, which for a
#: single replayed test costs nothing and makes the output deterministic.
TRACE_FLAG = "--trace"

_COMPILE_ERROR = "Compilation error in file"
_NO_SUCH_PATH = 'Paths given to "mix test" did not match'

#: How long the AST preflight may take. It parses one file; a minute is already
#: generous, and hanging here would hang the run before any test is executed.
PREFLIGHT_TIMEOUT_SECONDS = 60


def definition_lines(root: Path, path: PurePosixPath) -> list[int] | str:
    """Lines where ``path`` defines ExUnit tests, or an error code.

    Elixir's own parser is the authority: `Code.string_to_quoted` plus a walk
    for the `test` macro. Measured — a `@tag`ged test reports the `test` line
    rather than the tag line, a test inside `describe` is found, and a bodiless
    `test "not implemented"` is found.
    """
    target = Path(root) / str(path)
    if not target.is_file():
        return "missing_test_file"
    try:
        result = subprocess.run(
            ["elixir", "-e", _DEFINITION_LINES_SCRIPT, str(target)],
            capture_output=True,
            text=True,
            timeout=PREFLIGHT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        return "runner_toolchain_missing"
    except subprocess.SubprocessError:
        # A timeout or a crashed parse is not a missing toolchain, and saying
        # "`elixir` is not on PATH" would send an operator to fix the wrong
        # thing (Copilot, PR #203). Both refuse; they refuse differently.
        return "preflight_failed"
    if result.returncode != 0:
        return "unparseable_test_file"
    out = result.stdout.strip()
    if out == "PARSE_ERROR":
        return "unparseable_test_file"
    if not out:
        return []
    try:
        return [int(part) for part in out.split(",") if part]
    except ValueError:  # pragma: no cover - the script emits integers or nothing
        return "unparseable_test_file"


#: Where a replay's private build lives, under the canonical `_build/`.
#:
#: **Forced, not chosen.** Mix links a dependency's `priv` into the build with a
#: *relative* symlink computed for the standard layout, so a build path outside
#: the project gets no link at all — measured on kapelle, where
#: `phoenix_live_dashboard` then failed to compile deterministically because it
#: reads `phoenix`'s priv asset at compile time. A temp-directory build (both
#: `MIX_BUILD_PATH` and `MIX_BUILD_ROOT`) fails; a sibling of `_build/test`
#: works. `_build` is gitignored, the directory is unique per replay, and it is
#: removed afterwards — so the canonical project's *tracked* content is
#: untouched, which is the invariant that matters.
REPLAY_BUILD_PREFIX = ".spec-runner-replay-"

#: Phrases in `mix deps` output that mean the installed sources do not satisfy
#: the checkpoint's lock. Matched as text because Mix has no machine-readable
#: form for this, and matched **fail-closed**: an unrecognised problem still
#: shows up as a non-zero exit.
_DEPS_PROBLEMS = (
    "the dependency is not available",
    "the dependency is out of date",
    "lock mismatch",
    "lock outdated",
    "does not match the lock",
)


def elixir_toolchain() -> tuple[str, str] | str:
    """`(elixir_version, otp_release)`, or a message explaining why not.

    Recorded in the environment identity because the same lock compiled by a
    different Elixir is a different environment — and because a verdict that
    changed with a toolchain upgrade should be visible as such.
    """
    try:
        result = subprocess.run(["elixir", "--version"], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return "`elixir` is not on PATH"
    except subprocess.SubprocessError as exc:
        return f"`elixir --version` did not complete: {exc}"
    if result.returncode != 0:
        return f"`elixir --version` exited {result.returncode}"
    text = result.stdout
    elixir = re.search(r"^Elixir (\S+)", text, re.MULTILINE)
    otp = re.search(r"Erlang/OTP (\S+)", text)
    if not elixir or not otp:
        return "could not read the Elixir/OTP versions"
    return elixir.group(1), otp.group(1)


class ExUnitAdapter:
    """ExUnit, whose exit codes are **inverted** relative to pytest's.

    Measured on Elixir/OTP 28: `mix test` exits 2 when tests fail and 1 when
    the run never happened. And more importantly, `path:line` selects the
    nearest test *at or before* the line — so a line past the end of a file
    runs the last test in it and reports an ordinary "1 test, 1 failure". That
    is why this adapter proves the line is a definition line **before** running
    anything, and checks the reported location afterwards as well.
    """

    name = "exunit"
    selector_instruction = (
        "TDD_SELECTOR: test/path/to/file_test.exs:LINE\n"
        "\n"
        '   `path:line`, where LINE is the line the `test "..." do` is written\n'
        "   on — the definition line, not a line inside the body. A pytest-style\n"
        "   `path::name` is refused: `mix test` cannot resolve it, and a line\n"
        "   that resolves to nothing silently runs a different test."
    )

    def parse_selector(self, raw: str) -> Selector | SelectorRefusal:
        value = (raw or "").strip()
        if "::" in value:
            # The pytest form. It is what the RED prompt used to ask for, and
            # `mix test 'x.exs::name'` matches no file and exits 1 — which the
            # old code read as a confirmed red (#198).
            return SelectorRefusal(
                "pytest_style_selector",
                f"selector {raw!r} is a pytest node id; ExUnit selectors are "
                "'path:line', where line is the `test \"...\" do` line",
            )
        head, separator, tail = value.rpartition(":")
        if not separator or not tail.isdigit():
            return SelectorRefusal(
                "not_a_line_selector",
                f"selector {raw!r} is not 'path:line'",
            )
        path = normalise_path(head)
        if not path.parts:
            return SelectorRefusal("not_a_line_selector", f"selector {raw!r} names no file")
        return Selector(runner=self.name, path=path, locator=ExUnitDefinitionLine(int(tail)))

    def validate_command(self, test_command: str) -> str | None:
        tokens = command_tokens(test_command)
        if executable_of(test_command) != "mix" or "test" not in tokens:
            return (
                f"test_command {test_command!r} does not run `mix test`; "
                "set tdd_runner to the runner this project uses"
            )
        return None

    def preflight(self, root: Path, selector: Selector) -> SelectorRefusal | None:
        """Prove the requested line *defines* a test, before anything runs.

        Without this, a `not_red` verdict would rest on nothing: a passing run
        prints no location, and `1 test, 0 failures` is what `:999` prints too.
        And `not_red` is not a quiet verdict — it retires a claimed red and
        sends an operator to `repair`.
        """
        assert isinstance(selector.locator, ExUnitDefinitionLine)
        lines = definition_lines(root, selector.path)
        if isinstance(lines, str):
            return SelectorRefusal(lines, _PREFLIGHT_MESSAGES[lines].format(path=selector.path))
        if not lines:
            return SelectorRefusal("no_tests_in_file", f"{selector.path} defines no ExUnit tests")
        wanted = selector.locator.line
        if wanted not in lines:
            nearest = ", ".join(str(line) for line in lines)
            return SelectorRefusal(
                "not_a_definition_line",
                f'{selector.path}:{wanted} is not a `test "..." do` line '
                f"(tests are defined at {nearest}); ExUnit would silently run "
                "the nearest test at or before it",
            )
        return None

    def claim_paths(self, selector: Selector) -> tuple[PurePosixPath, ...]:
        """One `path:line` names one file."""
        return (selector.path,)

    def evidential_file(self, task_id: str) -> PurePosixPath:
        """`test/<task>_red_test.exs` — under `test/`, ending in `_test.exs`,
        which is what `mix test` collects by default. A file named any other
        way is simply never run, and a red nothing runs cannot be replayed."""
        slug = task_id.strip().lower().replace("-", "_") or "task"
        return PurePosixPath("test") / f"{slug}_red_test.exs"

    def is_discoverable(self, path: PurePosixPath) -> bool:
        """`mix test`'s default: `test/**/*_test.exs`."""
        parts = path.parts
        return bool(parts) and parts[0] == "test" and path.name.endswith("_test.exs")

    def contract_selectors(self) -> tuple[str, ...]:
        return ("test/thing_test.exs:12",)

    def prepare_replay(
        self, canonical_root: Path, replay_root: Path, selector: Selector
    ) -> ReplayEnvironment | ReplayEnvironmentRefusal:
        """Share the dependency *sources*, isolate the build (#207).

        A `git worktree` carries tracked files only, and Elixir keeps `deps/`
        and `_build/` inside the project — both gitignored — so a replay tree
        cannot compile anything. Measured on a real project: the first paid
        pilot run died here.

        Dependency **sources** are reusable as a cache and are shared read-only
        through `MIX_DEPS_PATH`. Build **artifacts** carry the compile state of
        one checkout and are never shared: each replay gets its own build path,
        removed afterwards.

        The private build lives under the canonical `_build/`, and that
        placement is forced rather than chosen — see `REPLAY_BUILD_PREFIX`.
        Nothing here downloads, generates or repairs: preparation proves and
        isolates what is installed, or refuses.
        """
        deps = (canonical_root / "deps").resolve()
        # `is_relative_to`, not a string prefix: `/repo-other/deps` starts with
        # `/repo` textually, so a symlinked `deps/` could point outside the
        # project and still pass a prefix check (Copilot, PR #208). The guard
        # exists precisely because that directory is shared into the replay.
        if deps.is_dir() and not deps.is_relative_to(canonical_root.resolve()):
            return ReplayEnvironmentRefusal(
                "environment_unavailable", f"{deps} is outside the project root"
            )
        # A project with no dependencies has no `deps/`, and that is not a
        # problem to report — authority belongs to the checkpoint's lock and to
        # `mix deps`, not to whether a directory exists. If dependencies *are*
        # declared and missing, `mix deps` below says so.

        toolchain = elixir_toolchain()
        if isinstance(toolchain, str):
            return ReplayEnvironmentRefusal("environment_unavailable", toolchain)

        build = canonical_root.resolve() / "_build" / f"{REPLAY_BUILD_PREFIX}{uuid4().hex[:12]}"
        if build.exists():  # pragma: no cover - a uuid4 collision
            return ReplayEnvironmentRefusal("environment_unavailable", f"{build} already exists")
        env = {"MIX_ENV": "test", "MIX_BUILD_PATH": str(build)}
        if deps.is_dir():
            env["MIX_DEPS_PATH"] = str(deps)

        problem = self._check_deps(replay_root, env)
        if problem is not None:
            shutil.rmtree(build, ignore_errors=True)
            return ReplayEnvironmentRefusal("environment_unavailable", problem)

        lock = replay_root / "mix.lock"
        lock_id = (
            hashlib.sha256(lock.read_bytes()).hexdigest()[:16] if lock.is_file() else "unlocked"
        )
        identity = ";".join(
            [
                "runner=exunit",
                f"mix.lock={lock_id}",
                f"elixir={toolchain[0]}",
                f"otp={toolchain[1]}",
                "mix_env=test",
                f"deps_source={hashlib.sha256(str(deps).encode()).hexdigest()[:12]}",
            ]
        )
        return ReplayEnvironment(env=env, environment_id=identity, cleanup_paths=(build,))

    def _check_deps(self, replay_root: Path, env: Mapping[str, str]) -> str | None:
        """`mix deps` in the *checkpoint* tree, so the lock being checked is the
        checkpoint's own. No network: `mix deps` reports status, it does not
        fetch.

        (`mix deps.check` is not a Mix task — measured: "The task
        "deps.check" could not be found". The status listing is the real one.)
        """
        try:
            result = subprocess.run(
                ["mix", "deps"],
                cwd=replay_root,
                capture_output=True,
                text=True,
                timeout=PREFLIGHT_TIMEOUT_SECONDS,
                env={**os.environ, **env},
            )
        except FileNotFoundError:
            return "`mix` is not on PATH"
        except subprocess.SubprocessError as exc:
            return f"`mix deps` did not complete: {exc}"
        output = f"{result.stdout}\n{result.stderr}"
        for phrase in _DEPS_PROBLEMS:
            if phrase in output:
                return (
                    f"the checkpoint's dependencies do not match what is installed ({phrase}); "
                    "the replay will not fetch or repair them"
                )
        if result.returncode != 0:
            return f"`mix deps` exited {result.returncode} in the replay tree"
        return None

    def build_command(self, test_command: str, selector: Selector) -> list[str]:
        assert isinstance(selector.locator, ExUnitDefinitionLine)
        tokens = command_tokens(test_command)
        if TRACE_FLAG not in tokens:
            tokens.append(TRACE_FLAG)
        return [*tokens, f"{selector.path}:{selector.locator.line}"]

    def classify(self, result: subprocess.CompletedProcess) -> RunOutcome:
        output = f"{result.stdout or ''}\n{result.stderr or ''}"
        summary = _EXUNIT_SUMMARY.search(output)
        if summary:
            tests, failures = int(summary.group(1)), int(summary.group(2))
            if tests == 0:
                # Measured: a line before the first test runs nothing and still
                # exits 0. "Nothing ran" is not "your test passed".
                return RunOutcome.SELECTION_FAILED
            if failures > 0 and result.returncode == 2:
                return RunOutcome.TESTS_FAILED
            if failures == 0 and result.returncode == 0:
                return RunOutcome.TESTS_PASSED
            return RunOutcome.UNRECOGNIZED
        if _COMPILE_ERROR in output:
            # No summary at all: the file never reached ExUnit. This is the
            # structural difference between a compile error and a runtime
            # failure, and why classify keys on the summary and not on text.
            return RunOutcome.COLLECTION_OR_COMPILE_ERROR
        if _NO_SUCH_PATH in output:
            return RunOutcome.SELECTION_FAILED
        return RunOutcome.UNRECOGNIZED

    def prove_selected(
        self, selector: Selector, result: subprocess.CompletedProcess
    ) -> SelectionProof:
        assert isinstance(selector.locator, ExUnitDefinitionLine)
        output = f"{result.stdout or ''}\n{result.stderr or ''}"
        wanted = selector.locator.line

        # The trace first: it states which test *executed*, by line, whether it
        # passed or failed. `:999` shows `[L#9]` here — refuted outright rather
        # than inferred from a count.
        executed = {int(m.group(1)) for m in _EXUNIT_TRACE_EXECUTED.finditer(output)}
        if executed:
            return SelectionProof.PROVEN if executed == {wanted} else SelectionProof.REFUTED

        # No trace (an older ExUnit, or a command that suppressed it): a failure
        # block still names a location.
        located = [
            (str(normalise_path(path)), int(line))
            for path, line in _EXUNIT_FAILURE_AT.findall(output)
        ]
        if located:
            return (
                SelectionProof.PROVEN
                if (str(selector.path), wanted) in located
                else SelectionProof.REFUTED
            )
        return SelectionProof.UNKNOWN


_PREFLIGHT_MESSAGES = {
    "missing_test_file": "{path} does not exist in the tree being replayed",
    "unparseable_test_file": "{path} does not parse as Elixir; nothing was run",
    "runner_toolchain_missing": (
        "`elixir` is not on PATH, so {path} cannot be checked before running — "
        "an unchecked ExUnit selector can silently run a different test"
    ),
    "preflight_failed": (
        "checking {path} for the test's definition line did not complete "
        "(timeout or a failed parse run); nothing was run"
    ),
}


#: Adapters by name. Adding one means measuring a runner, not assuming it
#: behaves like another (#198).
ADAPTERS: dict[str, TddRunnerAdapter] = {
    PytestAdapter.name: PytestAdapter(),
    ExUnitAdapter.name: ExUnitAdapter(),
}


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
    "ExUnitAdapter",
    "ExUnitDefinitionLine",
    "PytestAdapter",
    "PytestNodeId",
    "ReplayEnvironment",
    "ReplayEnvironmentRefusal",
    "RunOutcome",
    "SelectionProof",
    "Selector",
    "SelectorRefusal",
    "TddRunnerAdapter",
    "adapter_for",
    "elixir_toolchain",
    "definition_lines",
    "command_tokens",
    "executable_of",
    "lockfile_identity",
    "infer_adapter",
    "normalise_path",
]
