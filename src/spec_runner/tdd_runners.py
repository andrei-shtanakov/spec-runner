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
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
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
class FileTarget:
    """A declared-group element that names a whole test file, not one test
    (DT-01). Carries no pointer to a single test — the file itself is
    already `Selector.path` — so this locator is deliberately empty; it only
    marks that the selector is a file target rather than a node id or an
    ExUnit definition line.
    """


@dataclass(frozen=True)
class Selector:
    """A parsed, runner-specific pointer at exactly one test."""

    runner: str
    #: Project-relative and normalised, so `./test/x.exs` and `test/x.exs` are
    #: one selector and comparisons against runner output never turn on a `./`.
    path: PurePosixPath
    locator: PytestNodeId | ExUnitDefinitionLine | FileTarget


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


#: The declared limit on the readable half of a namespace segment (#341,
#: BEH-22/NFR-06) — long enough to stay recognisable, short enough that the
#: full path never approaches filesystem limits.
NAMESPACE_SLUG_MAX_LEN = 24

#: Hex digits of the namespace segment's digest half. Short on purpose: it is
#: a differentiator, not an identity — `NAMESPACE_SLUG_MAX_LEN` carries the
#: readability, this carries the guarantee that two namespaces never collapse
#: into one segment (#341 FR-14/BEH-21).
NAMESPACE_DIGEST_LEN = 8

_NAMESPACE_SEP_RE = re.compile(r"[^a-z0-9]+")


def task_slug(task_id: str) -> str:
    """The one formula every adapter's `evidential_file` builds its task
    segment from (#341 BEH-14). A bare `.replace("-", "_")` left every other
    non-alphanumeric character untouched — a task-id carrying a `/` (an
    external tracker's `owner/repo#N`-shaped id) then reached
    `PurePosixPath` unescaped and split into extra path segments, so the
    resulting file's *name* no longer started with `test_` and pytest's own
    discovery would not collect it. Folding every such run to a single `_`,
    the same way `namespace_segment` folds the namespace, keeps the slug to
    one path component for any adapter's join.
    """
    slug = _NAMESPACE_SEP_RE.sub("_", (task_id or "").strip().lower()).strip("_")
    return slug or "task"


def namespace_segment(namespace: str) -> str:
    """The one formula every adapter's `evidential_file` builds its
    namespace segment from (#341 Q-06) — so FR-11 (the adapter names the
    file) and FR-12 (the prompt and the harness agree on one path) hold
    without the rule being duplicated per adapter.

    Always two parts: a lower-cased, punctuation-collapsed slug of the raw
    namespace (readable, and recognisably derived from a *declared*
    `tdd_namespace`; already digest-shaped when the namespace itself is
    `resolve_namespace`'s computed fallback), and a short digest of the raw,
    un-folded namespace string. The slug alone is not enough — two namespaces
    differing only by case, a separator, or a truncated tail collapse to the
    same slug (BEH-21) — so the digest is taken over the value *before*
    case-folding or truncation, and rides along as a lower-case hex string
    that survives a case-fold of the whole path.

    Pure and deterministic: the same namespace string always yields the same
    segment, in any process, on any machine (BEH-20/NFR-03).
    """
    raw = (namespace or "").strip()
    digest = hashlib.sha256(raw.encode()).hexdigest()[:NAMESPACE_DIGEST_LEN]
    slug = _NAMESPACE_SEP_RE.sub("_", raw.lower()).strip("_")
    slug = slug[:NAMESPACE_SLUG_MAX_LEN].strip("_") or "ns"
    return f"{slug}_{digest}"


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

    def evidential_file(self, task_id: str, *, namespace: str) -> PurePosixPath:
        """Where this task's RED should write its failing test (#252).

        The **adapter** names it, never a shared heuristic: a Python-shaped
        guess is how an Elixir suite was told to write `tests/test_x_red.py`,
        and the same class of mistake as #198 and #220. The name it returns
        must be one this runner's ordinary discovery picks up — a test nothing
        collects is a red that cannot be replayed.

        `namespace` (#341) is always folded into the path via
        `namespace_segment` — a `TASK-001` from two workstreams must not name
        the same file. Callers pass `tdd.resolve_namespace(config)`, which
        never returns empty, so the segment is never absent.
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

    def build_scoped_command(self, test_command: str, selector: Selector) -> list[str]:
        """argv narrowed to ``selector`` alone (#367 FR-06, #375 review).

        Unlike `build_command`, which only *appends* the selector and is
        frozen for the red-replay path (FR-04/AC-9), this REPLACES the
        command's own positional test-path argument — a default
        `test_command` naming a directory (`pytest tests/…`, `mix test
        test/…`) must not also run that directory's whole contents. A
        separate method, not a parameter on `build_command`, precisely so the
        red-replay path never picks up this behaviour by accident.
        """
        ...

    def classify(self, result: subprocess.CompletedProcess) -> RunOutcome: ...

    def prove_selected(
        self, selector: Selector, result: subprocess.CompletedProcess
    ) -> SelectionProof: ...

    def execution_proven(self, selector: Selector, result: subprocess.CompletedProcess) -> bool:
        """Whether ``selector`` was PROVEN to actually execute (#367 FR-08).

        Distinct from `prove_selected`: on pytest, a `SKIPPED`/`XFAIL` line
        still carries the requested node id verbatim, so `prove_selected` —
        frozen for the red-replay path, FR-04/AC-9 — reads it as `PROVEN`. The
        identity claim is true; the test simply never ran. verify-first's
        green path needs the narrower claim, in its OWN strict word class
        (`passed`/`failed`/`error`, never `xfailed`/`xpassed` — those count as
        executed in the frozen `_EXECUTED_WORDS` and must not here, per FR-08's
        explicit exclusion).
        """
        ...


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

#: Characters that make a declared-group element a glob/pattern rather than
#: one literal path (BEH-03/FR-02).
_GROUP_ELEMENT_GLOB_CHARS = frozenset("*?[]")


def command_tokens(test_command: str) -> list[str]:
    """`test_command` split for inspection, or [] when it will not split."""
    try:
        return shlex.split(test_command or "")
    except ValueError:  # unbalanced quotes — not something to guess about
        return []


def _flag_takes_separate_value(token: str, value_flags: frozenset[str]) -> bool:
    """Whether ``token`` (already known to start with ``-``) needs the NEXT
    token as its value.

    TERMINAL policy (#375 review rounds 2-4 — three rounds of guessing a
    flag's arity from its bare shape is enough; this is the final rule, not
    another interim one):

    1. **Attached value, unambiguous either way.** A long form carrying its
       value glued on (``--tb=short``, ``--maxfail=1``, ``--color=yes``) or a
       short-flag cluster longer than a bare `-x` (``-ra``, ``-n4`` — a short
       option that accepts an argument always accepts it either attached or
       separate, so a token already longer than one letter received it
       attached) is self-contained: it consumes nothing that follows,
       regardless of ``value_flags``.
    2. **Everything else is BOOLEAN BY DEFAULT.** Round 3's policy defaulted
       the other way (assume value-taking unless positively known boolean)
       on the theory that mis-guessing would only under-narrow. It does not:
       round 4 found an ordinary bare long boolean (`--verbose`, `--quiet`,
       `--cover`) protecting the suite directory right after it, running the
       WHOLE suite silently — the one outcome this module exists to prevent.
       Flipping the default makes the *opposite* mistake instead: an
       unenumerated value-taking flag (some plugin's `--custom-report PATH`)
       has its value mis-stripped as a stray path. That mistake is caught
       downstream, not silent — `prove_selected`/`execution_proven` cannot
       attribute a run whose command lost an argument to the declared
       selector, and the group is refused as an instrument-error rather than
       silently running (or mis-scoping) anything. Between "loud instrument-
       error on a rare unenumerated value flag" and "silent full-suite run on
       an ordinary bare boolean", the former is the acceptable trade-off —
       hence ``value_flags`` is now a curated ALLOWLIST of flags positively
       known to take a separate argument, per adapter, sourced from that
       runner's own `--help` (`PytestAdapter._VALUE_FLAGS`,
       `ExUnitAdapter._VALUE_FLAGS`) — not the inverse.
    """
    if "=" in token:
        return False
    if not token.startswith("--") and len(token) > 2:
        return False
    return token in value_flags


def strip_positional_paths(
    tokens: list[str],
    *,
    keep_exact: frozenset[str],
    executable_names: frozenset[str],
    value_flags: frozenset[str] = frozenset(),
    keep_once: frozenset[str] = frozenset(),
) -> list[str]:
    """Drop bare positional path arguments from a tokenised command (#375).

    Shared by every adapter's `build_scoped_command`: verify-first replaces a
    default `test_command`'s path argument with the declared selector rather
    than appending to it (FR-06's "replace, not append" — the same lesson
    `git_ops.build_scoped_test_command` already applies), and the previous
    approach hardcoded a single literal directory name (`{"tests"}`) outside
    any adapter, so a project naming its suite `test/` or `suite/` ran the
    whole thing plus the selector. This is runner-agnostic: it drops every
    positional token that is not one of the interpreter/wrapper/subcommand
    literals in ``keep_exact`` or the runner's own executable — whatever the
    test directory is called.

    Several different questions, on purpose, across rounds 2-4 of review:

    - **Is this token the runner executable?** Answered by *basename*
      (``executable_names``), the same rule `executable_of` already uses —
      `./venv/bin/pytest tests/` is a supported `test_command` shape
      (`infer_adapter`/`validate_command` both accept it), and a token match
      that requires the literal string `"pytest"` drops the executable itself
      as a stray positional, leaving argv with the node id in position 0.
    - **Is this token a wrapper literal** (`uv`, `run`, `-m`)? Those never
      appear path-prefixed in practice, so an *exact*, repeatable match in
      ``keep_exact`` is enough.
    - **Is this token a subcommand literal that appears exactly ONCE**
      (ExUnit's `test` in `mix test`)? ``keep_once`` matches it only the
      first time it is seen — round 3's finding: matching it every time also
      spared a positional suite directory spelled with the SAME word and no
      trailing slash (`mix test test`), which a trailing-slash spelling
      (`mix test test/`) never collided with, so the bug was invisible to a
      test suite that only tried the slashed form.
    - **Does this flag consume the next token as its value?** Answered by
      `_flag_takes_separate_value` — see its docstring for the TERMINAL
      policy (rounds 2-4): a flag is BOOLEAN BY DEFAULT (its next token is
      free to be read as a stray path and dropped) unless it is in the
      curated ``value_flags`` allowlist for this adapter, or its value is
      visibly attached already.
    """
    kept: list[str] = []
    protect_next = False
    once_remaining = set(keep_once)
    for token in tokens:
        if protect_next:
            kept.append(token)
            protect_next = False
            continue
        if token.startswith("-"):
            kept.append(token)
            protect_next = _flag_takes_separate_value(token, value_flags)
            continue
        if (
            token in keep_exact
            or PurePosixPath(token).name in executable_names
            or _PYTHONS.match(PurePosixPath(token).name)
        ):
            kept.append(token)
            continue
        if token in once_remaining:
            once_remaining.discard(token)
            kept.append(token)
            continue
        # A bare positional argument that is none of the above is the
        # command's own test-path argument — dropped, not kept, so the
        # adapter's append lands on the declared selector alone.
    return kept


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


#: The env var a file-target replay's reporter plugin reads to learn where to
#: write its manifest (DT-02, design "Точка врезки 2"). Set per selector by
#: `live_verify` — the manifest is its own per declared element, never
#: shared across a group, or a mixed group would overwrite itself.
FILE_TARGET_MANIFEST_ENV = "SPEC_RUNNER_VERIFY_MANIFEST"

#: The module name the reporter plugin is loaded under (`-p <name>`).
#: Deliberately not `conftest.py`: connected by the invocation's own flag, so
#: it is never picked up by a project's own collection (design: "не мешает
#: conftest.py проекта и не попадает в его коллекцию").
_REPORTER_PLUGIN_MODULE = "_spec_runner_verify_reporter"

#: The reporter sidecar itself (Q-01): a pytest plugin, written to its own
#: temp file per replay so a project under verification need not depend on
#: spec_runner being importable in its own environment. Two phases, written
#: as they happen rather than reconstructed afterwards: a "collected" record
#: names every member the collection phase gathered, BEFORE any of them
#: runs; an "outcome" record is appended per member as its call phase (or a
#: failing setup/teardown) finishes. The closing "done" record is what tells
#: a reader the run finished rather than crashing mid-way — its absence means
#: an incomplete manifest, read as `instrument_error`, never as a pass.
#: A failure inside the plugin itself is swallowed: it must only ever leave
#: the manifest unreadable or incomplete, never turn a green run red.
_VERIFY_REPORTER_PLUGIN = f"""
import json
import os

_MANIFEST_PATH = os.environ.get({FILE_TARGET_MANIFEST_ENV!r})


def _append(record):
    if not _MANIFEST_PATH:
        return
    try:
        with open(_MANIFEST_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\\n")
    except OSError:
        pass


def pytest_collection_modifyitems(items):
    try:
        members = [item.nodeid for item in items]
    except Exception:
        return
    _append({{"phase": "collected", "members": members}})


def pytest_runtest_logreport(report):
    try:
        if report.when == "call":
            _append({{"phase": "outcome", "nodeid": report.nodeid, "outcome": report.outcome}})
        elif report.when in ("setup", "teardown") and report.outcome != "passed":
            _append({{"phase": "outcome", "nodeid": report.nodeid, "outcome": report.outcome}})
    except Exception:
        pass


def pytest_sessionfinish():
    _append({{"phase": "done"}})
"""


@dataclass(frozen=True)
class FileComposition:
    """What a file target's reporter manifest said about one run (DT-02).

    `members` is the collection phase, in collection order — the composition
    resolved against whatever tree the replay actually ran in (BEH-07).
    `outcomes` maps each reported member's node id to its outcome word.
    `complete` is whether the closing "done" record was seen; its absence
    means the run broke before finishing and the manifest must not be read
    as a final answer.
    """

    members: tuple[str, ...]
    outcomes: Mapping[str, str]
    complete: bool


def read_file_composition(path: Path) -> FileComposition | None:
    """Parse a file target's reporter manifest, or None if it is missing or
    malformed — a broken reporter gives an unreadable manifest, never a
    false verdict either way (design: "не превращает зелёное в красное").
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    members: list[str] = []
    outcomes: dict[str, str] = {}
    complete = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            return None
        phase = record.get("phase")
        if phase == "collected":
            members = [str(member) for member in record.get("members", [])]
        elif phase == "outcome":
            nodeid = record.get("nodeid")
            if isinstance(nodeid, str):
                outcomes[nodeid] = str(record.get("outcome", ""))
        elif phase == "done":
            complete = True
    return FileComposition(members=tuple(members), outcomes=outcomes, complete=complete)


def parse_group_element(
    adapter: TddRunnerAdapter, raw: str, root: Path
) -> Selector | SelectorRefusal:
    """Dispatch to `adapter`'s own declared-group-element vocabulary if it
    has one (pytest). An adapter that never declared one (ExUnit,
    `supports_file_targets = False`) has nothing new to add — its existing
    `parse_selector` already refuses every non-node-id form, including a
    file target, under its own stable code. Not a second dictionary: only
    where a caller that accepts both node ids and file targets (`live_verify`)
    reaches whichever vocabulary applies.
    """
    method = getattr(adapter, "parse_group_element", None)
    if method is None:
        return adapter.parse_selector(raw)
    result: Selector | SelectorRefusal = method(raw, root)
    return result


def pytest_summary_counts(output: str) -> list[tuple[str, str]]:
    """`(count, word)` pairs from pytest's LAST summary line, or [].

    The whole summary is read, not its first count: `1 failed, 97 passed` is a
    98-test run whose first number is 1, and reading that as proof would let a
    full-suite run stand in for the named test (#201). Shared by every reader
    that needs the summary's full word class — `_exactly_one_test_executed`
    below (frozen `_EXECUTED_WORDS`, red-path, FR-04/AC-9) and verify-first's
    own, stricter execution proof (`live_verify.py`, #367 FR-08), which reads
    the same line against a different word set rather than duplicating the
    regex.
    """
    matches = _PYTEST_SUMMARY.findall(output)
    if not matches:
        return []
    return _PYTEST_COUNT.findall(matches[-1])


def _exactly_one_test_executed(output: str) -> bool:
    """True when pytest's summary accounts for exactly one executed test.

    The single count must also be an *executed* outcome — one skipped test is
    one test that did not run.
    """
    counts = pytest_summary_counts(output)
    if len(counts) != 1:
        return False
    number, word = counts[0]
    return number == "1" and word in _EXECUTED_WORDS


class PytestAdapter:
    """pytest, whose exit codes were measured on pytest 8."""

    name = "pytest"
    #: Declared capability (Q-05): pytest accepts file targets in a declared
    #: group; ExUnit does not, and refuses by name rather than accepting one
    #: silently and judging it by a return code (FR-03).
    supports_file_targets = True
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

    def parse_group_element(self, raw: str, root: Path) -> Selector | SelectorRefusal:
        """The declared-group-element vocabulary (BEH-03, DT-01): parses one
        element of a declared verify-first group, which may be either a node
        id or a file target. This is a second, wider entry point — not an
        extension of `parse_selector`, which stays a RED-checkpoint
        vocabulary about exactly one test (the charter forbids widening it).

        Every defective form refuses with its own stable
        `SelectorRefusal.code` (FR-02): a dictionary that collapses two
        different defects onto the same code, or onto a shared catch-all,
        fails BEH-03 even if every input is correctly refused.
        """
        value = (raw or "").strip()
        if not value:
            return SelectorRefusal("empty_element", "declared group element is empty")
        if "::" in value:
            return self.parse_selector(value)
        if value.startswith("-k"):
            return SelectorRefusal(
                "dash_k_expression", f"{raw!r} is a `-k` expression, not a file target"
            )
        if value.startswith("-m"):
            return SelectorRefusal(
                "marker_expression", f"{raw!r} is a marker expression, not a file target"
            )
        if any(ch in value for ch in _GROUP_ELEMENT_GLOB_CHARS):
            return SelectorRefusal("glob_pattern", f"{raw!r} is a glob pattern, not one file")

        root_resolved = root.resolve()
        raw_path = Path(value)
        joined = raw_path if raw_path.is_absolute() else root_resolved / raw_path
        # `os.path.normpath` collapses `..` lexically without touching the
        # filesystem — used only for the symlink check below, which must see
        # the path as written (a resolved path never looks like a symlink).
        unresolved = Path(os.path.normpath(str(joined)))
        # Containment must be checked against the *fully* resolved path: an
        # intermediate symlinked directory is invisible to `normpath` (unlike
        # a `..` segment), so a lexical-only check can be walked outside the
        # repository by a symlink one directory up from the named file.
        candidate = joined.resolve()
        try:
            rel = candidate.relative_to(root_resolved)
        except ValueError:
            return SelectorRefusal("outside_repository", f"{raw!r} resolves outside the repository")
        if unresolved.is_symlink():
            return SelectorRefusal("symlink", f"{raw!r} is a symlink, not a regular file")
        if candidate.is_dir():
            return SelectorRefusal("directory", f"{raw!r} is a directory, not one file")
        if not candidate.is_file():
            return SelectorRefusal("not_a_regular_file", f"{raw!r} is not a regular file")
        rel_posix = PurePosixPath(rel.as_posix())
        if not self.is_discoverable(rel_posix):
            return SelectorRefusal(
                "not_discoverable",
                f"{raw!r} is not a file this adapter's own discovery would collect as tests",
            )
        return Selector(runner=self.name, path=rel_posix, locator=FileTarget())

    def validate_command(self, test_command: str) -> str | None:
        if executable_of(test_command) != "pytest":
            return (
                f"test_command {test_command!r} does not run pytest; "
                "set tdd_runner to the runner this project uses"
            )
        return None

    def preflight(self, root: Path, selector: Selector) -> SelectorRefusal | None:
        """None for a node id, and the absence is the point.

        A pytest node id that names nothing **cannot** be mistaken for a red: it
        exits 4, never 1. That is the property ExUnit lacks and the reason
        ExUnit needs its definition line proven before the runner is invoked.

        A file target is different (DT-02, design "Точка врезки 3"): it names
        no test, only a path, so a missing or non-test file must be refused
        BY NAME here — against ``root`` (the judged commit's worktree when
        called from `live_verify`, BEH-07), never the live working tree —
        rather than surfacing later as an uninformative exit code.
        """
        if isinstance(selector.locator, FileTarget):
            target = Path(root) / str(selector.path)
            if not target.is_file():
                return SelectorRefusal(
                    "missing_test_file",
                    f"{selector.path} does not exist in the tree being replayed",
                )
            if not self.is_discoverable(selector.path):
                return SelectorRefusal(
                    "not_discoverable",
                    f"{selector.path} is not a file this adapter's own discovery "
                    "would collect as tests",
                )
        return None

    def claim_paths(self, selector: Selector) -> tuple[PurePosixPath, ...]:
        """One node id names one file.

        **Documented limitation** (§1.3): a test depending on a fixture in
        `conftest.py` does not claim that conftest.
        """
        return (selector.path,)

    def evidential_file(self, task_id: str, *, namespace: str) -> PurePosixPath:
        """`tests/test_<task>_<namespace>_red.py` — collected by pytest's
        default `test_*.py`, and under `tests/`, which is where a pytest
        project's discovery is rooted by convention."""
        slug = task_slug(task_id)
        ns = namespace_segment(namespace)
        return PurePosixPath("tests") / f"test_{slug}_{ns}_red.py"

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
        """Passthrough for a node id: a Python environment lives outside the
        checkout. That is exactly why pytest never met #207 — a bare
        worktree can run tests because site-packages is somewhere else
        entirely.

        For a file target (DT-02), the reporter sidecar's own module is
        deployed here — once for the whole group, like everything else this
        method prepares — into its own temp directory, added to
        `PYTHONPATH` so `build_scoped_command`'s `-p` flag can import it
        without the target project depending on spec_runner. That directory
        is also where each selector's manifest is written (design: "Директория
        манифеста берётся из ReplayEnvironment.cleanup_paths"), so it is
        returned in `cleanup_paths` for teardown like any other replay
        artefact.
        """
        if not isinstance(selector.locator, FileTarget):
            return ReplayEnvironment(env={}, environment_id=lockfile_identity(canonical_root))
        plugin_dir = Path(tempfile.mkdtemp(prefix="spec-runner-verify-reporter-"))
        (plugin_dir / f"{_REPORTER_PLUGIN_MODULE}.py").write_text(_VERIFY_REPORTER_PLUGIN)
        existing_pythonpath = os.environ.get("PYTHONPATH", "")
        pythonpath = (
            f"{plugin_dir}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else str(plugin_dir)
        )
        return ReplayEnvironment(
            env={"PYTHONPATH": pythonpath},
            environment_id=lockfile_identity(canonical_root),
            cleanup_paths=(plugin_dir,),
        )

    def build_command(self, test_command: str, selector: Selector) -> list[str]:
        assert isinstance(selector.locator, PytestNodeId)
        return [*command_tokens(test_command), selector.locator.value]

    #: pytest flags KNOWN to take a separate argument — TERMINAL allowlist
    #: (#375 review round 4): the default flipped from round 3's "assume
    #: value-taking" to "assume boolean" (see `_flag_takes_separate_value`'s
    #: docstring for why), so this is now an affirmative list of value-taking
    #: flags, sourced from `pytest --help`, not the flags known to take
    #: nothing. Bare booleans (`-v`/`--verbose`, `-q`/`--quiet`, `-x`/
    #: `--exitfirst`, `-s`, `-l`, `--lf`, `--ff`, `--nf`, `--cache-clear`,
    #: `--full-trace`, `--strict[-markers]`, …) need no entry here at all —
    #: they are boolean by the default itself.
    _VALUE_FLAGS = frozenset(
        {
            "-k",
            "-m",
            "-p",
            "-o",
            "-W",
            "-c",
            "-n",
            "--cov",
            "--cov-report",
            "--cov-config",
            "--rootdir",
            "--confcutdir",
            "--junitxml",
            "--junit-xml",
            "--ignore",
            "--ignore-glob",
            "--deselect",
            "--basetemp",
            "--last-failed-no-failures",
            "--maxfail",
            "--tb",
            "--durations",
            "--durations-min",
            "--dist",
            "--capture",
            "--pdbcls",
            "--assert",
            "--doctest-glob",
            "--override-ini",
        }
    )

    def build_scoped_command(self, test_command: str, selector: Selector) -> list[str]:
        kept = strip_positional_paths(
            command_tokens(test_command),
            keep_exact=_RUNNER_WRAPPERS,
            executable_names=frozenset({"pytest"}),
            value_flags=self._VALUE_FLAGS,
        )
        if isinstance(selector.locator, FileTarget):
            # The reporter connects by invocation flag, never autoload
            # (design: "не мешает conftest.py проекта и не попадает в его
            # коллекцию") — and the whole file runs, not one node id, since
            # a file target's composition IS the file (DT-02).
            return [*kept, "-p", _REPORTER_PLUGIN_MODULE, str(selector.path)]
        assert isinstance(selector.locator, PytestNodeId)
        return [*kept, selector.locator.value]

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

    #: verify-first's OWN class of proven-execution words (#367 FR-08) —
    #: narrower than `_EXECUTED_WORDS` on purpose: `xfailed`/`xpassed` count as
    #: executed there (frozen for the red-replay path, FR-04/AC-9) and must
    #: NOT here, and `skipped`/`deselected` never counted as executed either
    #: way.
    _PROVEN_EXECUTION_WORDS = frozenset({"passed", "failed", "error", "errors"})

    #: Summary categories that say nothing about whether the requested test
    #: executed, filtered out before judging the rest (#375 review round 2,
    #: finding 1): `1 passed, 1 warning in 0.05s` is an ordinary passing run
    #: whose dependency happens to emit a `DeprecationWarning`, and reading
    #: "more than one category" as "inconclusive" refused every such project
    #: from using verify_first at all — with a message that falsely claimed
    #: the selector was skipped.
    _NEUTRAL_CATEGORIES = frozenset({"warning", "warnings"})

    def execution_proven(self, selector: Selector, result: subprocess.CompletedProcess) -> bool:
        output = f"{result.stdout or ''}\n{result.stderr or ''}"
        counts = [
            (number, word)
            for number, word in pytest_summary_counts(output)
            if word not in self._NEUTRAL_CATEGORIES
        ]
        if len(counts) != 1:
            return False
        number, word = counts[0]
        return number == "1" and word in self._PROVEN_EXECUTION_WORDS


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
    #: Declared capability (Q-05): ExUnit's exit codes are inverted relative
    #: to pytest's, and per-member accounting would need its own measured
    #: matrix (#198) that this workstream does not build. ExUnit refuses a
    #: file target by name (FR-03) rather than accepting one silently.
    supports_file_targets = False
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

    def evidential_file(self, task_id: str, *, namespace: str) -> PurePosixPath:
        """`test/<task>_<namespace>_red_test.exs` — under `test/`, ending in
        `_test.exs`, which is what `mix test` collects by default. A file
        named any other way is simply never run, and a red nothing runs
        cannot be replayed."""
        slug = task_slug(task_id)
        ns = namespace_segment(namespace)
        return PurePosixPath("test") / f"{slug}_{ns}_red_test.exs"

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

    #: `mix test` flags KNOWN to take a separate argument — TERMINAL
    #: allowlist, same policy flip as `PytestAdapter._VALUE_FLAGS` (#375
    #: review round 4): bare booleans (`--trace`, `--cover`, `--force`,
    #: `--no-start`, `--stale`, `--listen-on-stdin`, `--slowest`) need no
    #: entry — boolean is the default.
    _VALUE_FLAGS = frozenset(
        {"--only", "--exclude", "--include", "--seed", "--max-failures", "--timeout"}
    )

    def build_scoped_command(self, test_command: str, selector: Selector) -> list[str]:
        assert isinstance(selector.locator, ExUnitDefinitionLine)
        kept = strip_positional_paths(
            command_tokens(test_command),
            keep_exact=_RUNNER_WRAPPERS,
            executable_names=frozenset({"mix"}),
            value_flags=self._VALUE_FLAGS,
            # `test` is `mix test`'s subcommand literal, kept only the FIRST
            # time it is seen — never path-prefixed, so an exact match is
            # enough, but matching it on every occurrence also spared a
            # positional suite directory spelled the same way with no
            # trailing slash (`mix test test`, #375 review round 3, finding
            # 2): the trailing-slash spelling (`mix test test/`) never
            # collided because `test/` and `test` are different tokens, so
            # the bug was invisible until this spelling was tried.
            keep_once=frozenset({"test"}),
        )
        if TRACE_FLAG not in kept:
            kept.append(TRACE_FLAG)
        return [*kept, f"{selector.path}:{selector.locator.line}"]

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

    def execution_proven(self, selector: Selector, result: subprocess.CompletedProcess) -> bool:
        """ExUnit has no separate gap here: `prove_selected`'s `PROVEN` is
        already "the requested line's timed trace entry matched" — an
        excluded or skipped test never produces a timing (#375 review,
        `TestTheProofIsTheTrace`), so there is nothing pytest's node-id
        shortcut would leak through that this needs to catch separately.
        """
        return self.prove_selected(selector, result) is SelectionProof.PROVEN


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
    "FILE_TARGET_MANIFEST_ENV",
    "FileComposition",
    "FileTarget",
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
    "namespace_segment",
    "parse_group_element",
    "pytest_summary_counts",
    "read_file_composition",
    "strip_positional_paths",
    "task_slug",
]
