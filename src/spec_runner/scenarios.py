"""Verify-first scenario coverage (#402).

A `verify_first` task that declares `**Scenarios:**` must run a group whose
files carry every declared id as a whole token. The contract is deliberately
narrow (design §4, `docs/superpowers/specs/2026-09-23-verify-scenario-coverage-design.md`):

- coverage is per **file**, not per test function;
- ids are unqualified and repeat across workstreams, so a foreign file that
  carries its own `BEH-09` satisfies the check. It catches a group that
  claims **nothing** about the task's scenarios, not one that claims them
  falsely;
- a name-mangled form (`TestBEH09`) is not a label: accepting it would be a
  guess, and a guessed match is the failure this check exists to stop.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from .phases import Refusal, RefusalKind
from .tdd_runners import normalise_path

if TYPE_CHECKING:
    from .task import Task

_LINE_SUFFIX = re.compile(r":\d+$")


def scenario_pattern(scenario: str) -> re.Pattern[str]:
    """`scenario` as a whole token: no letter, digit or `_` on either side."""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(scenario)}(?![A-Za-z0-9_])")


def group_files(verifies: Sequence[str]) -> list[PurePosixPath]:
    """The file each declared group element names, first-seen order, once.

    Textual, not an adapter parse: coverage is judged at a commit, and the
    adapter's own parser checks the working tree. `path::node` (pytest) and
    `path:line` (ExUnit) both reduce to `path`.
    """
    files: list[PurePosixPath] = []
    for raw in verifies:
        path = normalise_path(_LINE_SUFFIX.sub("", raw.strip().split("::", 1)[0]))
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


def read_at_commit(root: Path, sha: str, path: PurePosixPath) -> str | None:
    """`path` (relative to `root`) as committed in `sha`; None if git cannot show it."""
    shown = subprocess.run(["git", "show", f"{sha}:./{path}"], cwd=root, capture_output=True)
    if shown.returncode != 0:
        return None
    return shown.stdout.decode("utf-8", errors="replace")


def coverage_refusal(task: Task, root: Path, sha: str) -> Refusal | None:
    """Refuse a declared scenario the group does not carry at `sha` (design §5).

    Terminal POLICY: the same commit gives the same answer, so a retry would
    only repeat it. A file git cannot show at `sha` is an INSTRUMENT refusal —
    the entry run should already have failed on it. A task without
    `**Scenarios:**` is not checked at all (decision 3).
    """
    if task.scenarios is None:
        return None
    files = group_files(task.verifies or [])
    texts: list[str] = []
    for path in files:
        text = read_at_commit(root, sha, path)
        if text is None:
            return Refusal(
                f"scenario coverage: {path} cannot be read at {sha[:12]}",
                RefusalKind.INSTRUMENT,
            )
        texts.append(text)
    missing = uncovered_scenarios(task.scenarios, texts)
    if not missing:
        return None
    searched = ", ".join(str(path) for path in files)
    return Refusal(
        f"verify-first group at {sha[:12]} leaves declared scenarios uncovered: "
        f"{', '.join(missing)} (searched: {searched}); a label is the id as a "
        "whole token, e.g. in a docstring",
        RefusalKind.POLICY,
        terminal=True,
    )
