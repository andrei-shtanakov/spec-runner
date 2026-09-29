"""Verify-first scenario coverage (#402), with token ownership (#603).

A `verify_first` task that declares `**Scenarios:**` must run a group whose
entries carry every declared id as a whole token. The rule is chosen by the
entry's file type (design §2.3,
`docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md`):

- a `.py` file — the id counts only inside the test definition the entry
  names, or the region of a class containing it (`criteria_tokens`, norm
  §1.4); a file-only entry uses a static approximation of default pytest
  collection. The module header, module constants and helpers own nothing.
  Bare and qualified (`ENC:BEH-09`) ids follow the same rule;
- any other file (ExUnit `path:line`) — today's per-file whole-token match
  (#402 design §4).

A name-mangled form (`TestBEH09`) is never a label: accepting it would be a
guess, and a guessed match is the failure this check exists to stop.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from .criteria_tokens import UnresolvedQualname, carried_ids, select_tests, token_pattern
from .phases import Refusal, RefusalKind
from .tdd_runners import normalise_path

if TYPE_CHECKING:
    from .task import Task

_LINE_SUFFIX = re.compile(r":\d+$")


def scenario_pattern(scenario: str) -> re.Pattern[str]:
    """`scenario` as a whole token: no letter, digit or `_` on either side."""
    return token_pattern(scenario)


def split_entry(raw: str) -> tuple[PurePosixPath, str | None]:
    """A group element as (file, dotted qualname or None).

    `path::Class::test[param]` → (path, "Class.test"). The parametrize suffix is
    cut at its first `[` before the name is split, since an id may itself
    contain `::`; identifiers never contain `[`. `path:line` (ExUnit) and a
    bare path → (path, None). Textual, not an adapter parse: coverage is judged
    at a commit, and the adapter's own parser checks the working tree.
    """
    head, separator, rest = raw.strip().partition("::")
    path = normalise_path(_LINE_SUFFIX.sub("", head))
    if not separator:
        return path, None
    name = ".".join(part for part in rest.split("[", 1)[0].split("::") if part)
    return path, name or None


def group_files(verifies: Sequence[str]) -> list[PurePosixPath]:
    """The file each declared group element names, first-seen order, once."""
    files: list[PurePosixPath] = []
    for raw in verifies:
        path, _ = split_entry(raw)
        if str(path) not in ("", ".") and path not in files:
            files.append(path)
    return files


def uncovered_scenarios(scenarios: Sequence[str], texts: Iterable[str]) -> list[str]:
    """Declared scenarios no text carries, in declared order, each once."""
    corpus = list(texts)
    missing: list[str] = []
    for scenario in dict.fromkeys(scenarios):
        pattern = scenario_pattern(scenario)
        if not any(pattern.search(text) for text in corpus):
            missing.append(scenario)
    return missing


@dataclass(frozen=True)
class GroupCoverage:
    """What a group carries: ids no entry carries, and entries not judgeable."""

    missing: list[str]
    problems: list[str]


def group_coverage(
    scenarios: Sequence[str], entries: Sequence[str], texts: Mapping[PurePosixPath, str]
) -> GroupCoverage:
    """Judge every entry whose file text is in `texts` by the rule for its type.

    An entry whose file is absent from `texts` is skipped — the caller decides
    whether that is a refusal (the gate) or a separate warning (`validate`).
    """
    carried: set[str] = set()
    problems: list[str] = []
    for raw in entries:
        path, qualname = split_entry(raw)
        text = texts.get(path)
        if text is None:
            continue
        if path.suffix != ".py":
            missing_here = uncovered_scenarios(scenarios, [text])
            carried.update(s for s in scenarios if s not in missing_here)
            continue
        try:
            definitions = select_tests(text, qualname)
        except UnresolvedQualname as exc:
            problems.append(
                f"{str(exc.args[0]).replace('.', '::')} is not defined in {path} "
                "(a generated or inherited test cannot be read statically)"
            )
            continue
        except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
            # RecursionError/MemoryError: `ast.parse` on a pathologically deep
            # file — "does not parse" for our purposes, never a traceback.
            problems.append(f"{path} cannot be parsed as Python ({type(exc).__name__}: {exc})")
            continue
        carried.update(carried_ids(text, definitions, scenarios))
    missing = [s for s in dict.fromkeys(scenarios) if s not in carried]
    return GroupCoverage(missing, list(dict.fromkeys(problems)))


def _show(root: Path, sha: str, path: PurePosixPath) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "show", f"{sha}:./{path}"], cwd=root, capture_output=True)


def read_at_commit(root: Path, sha: str, path: PurePosixPath) -> str | None:
    """`path` (relative to `root`) as committed in `sha`; None if git cannot show it."""
    shown = _show(root, sha, path)
    if shown.returncode != 0:
        return None
    return shown.stdout.decode("utf-8", errors="replace")


def coverage_refusal(task: Task, root: Path, sha: str) -> Refusal | None:
    """Refuse a declared scenario the group does not carry at `sha`.

    Terminal POLICY: the same commit gives the same answer, so a retry would
    only repeat it. A `.py` file that does not parse, or a qualname the file
    does not define, is a terminal INSTRUMENT refusal — coverage cannot be
    judged, and the commit will not change. A file git cannot show at `sha` is
    a retryable INSTRUMENT refusal. A task without `**Scenarios:**` is not
    checked at all (#402 decision 3).
    """
    if task.scenarios is None:
        return None
    entries = task.verifies or []
    files = group_files(entries)
    texts: dict[PurePosixPath, str] = {}
    for path in files:
        shown = _show(root, sha, path)
        if shown.returncode != 0:
            said = shown.stderr.decode("utf-8", errors="replace").strip()
            return Refusal(
                f"scenario coverage: {path} cannot be read at {sha[:12]}"
                + (f" (git: {said})" if said else ""),
                RefusalKind.INSTRUMENT,
            )
        texts[path] = shown.stdout.decode("utf-8", errors="replace")
    coverage = group_coverage(task.scenarios, entries, texts)
    if coverage.problems:
        # Terminal, kind unchanged: a `.py` file that does not parse, or a
        # qualname its AST does not define, is a fact about this commit — a
        # retry would re-run the group for the same answer. A git read failing
        # (above) stays retryable: its causes can be transient.
        return Refusal(
            f"scenario coverage at {sha[:12]} cannot be judged: {'; '.join(coverage.problems)}",
            RefusalKind.INSTRUMENT,
            terminal=True,
        )
    if not coverage.missing:
        return None
    searched = ", ".join(str(path) for path in files)
    return Refusal(
        f"verify-first group at {sha[:12]} leaves declared scenarios uncovered: "
        f"{', '.join(coverage.missing)} (searched: {searched}); in a Python test file "
        "a label counts only inside the test definition the group names or its "
        "containing test class — not in the module header or a helper",
        RefusalKind.POLICY,
        terminal=True,
    )
