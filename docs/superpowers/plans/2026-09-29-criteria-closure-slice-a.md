# Criteria closure — slice A (qualified ids, token ownership) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `**Scenarios:**` accepts qualified `CODE:BEH-NN` ids, and in Python group files a declared id counts only inside the test definition the group names (or its containing test class), in the `verify_first` gate and in `validate`.

**Architecture:** A new pure-AST module `criteria_tokens.py` owns the §1.4 ownership rule (regions, default-naming approximation, carried ids) so slice B can reuse it. `scenarios.py` gains one rule-by-entry function, `group_coverage`, used by both the gate (`coverage_refusal`, text at the commit) and `validate` (`_scenario_warnings`, working tree). The parser in `task.py` accepts the qualified form and refuses a mixed line.

**Tech Stack:** Python ≥ 3.11 stdlib (`ast`, `re`), pytest, uv, ruff, mypy, pyrefly.

**Spec:** `docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md` §2 (slice A), norm devtools `b7edcca` §1.3–1.4, §2.2.

## Global Constraints

- Python floor stays `>=3.11`; no new dependency.
- Ruff line length **100**; ruff rules E, F, W, I, UP, B, C4, SIM; mypy strict; `pyrefly check` clean.
- Token boundaries exactly `(?<![A-Za-z0-9_])<id>(?![A-Za-z0-9_])` (norm §1.3).
- `CODE` is `^[A-Z]{2,6}$` (norm §1.1); `ID` keeps today's shape `[A-Z]+-\d+[a-z]?`.
- Rule by file type: `.py` → AST ownership for bare **and** qualified ids; any other suffix → today's per-file whole-token match, unchanged.
- The gate stays `verify_first`-only; `tdd`/`standard` get a non-failing `validate` warning.
- Refusal kinds: uncovered → terminal POLICY; unreadable file, unparseable `.py`, unresolved qualname → INSTRUMENT.
- Release: **minor**, with a prominent **Changed** CHANGELOG entry.
- Branch `feat/603-slice-a-token-ownership`; commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A node id naming a **class** (`tests/t.py::TestLogin`) — its default-named test methods count, plus the class region; a helper method does not. Pinned in Task 2 (`TestClassEntry`) and Task 3.
2. A parametrize id containing `::` or `[` (`tests/t.py::test_x[a::b]`) — resolves to `test_x`, never to an unresolved `a` qualname. Pinned in Task 3 (`TestSplitEntry`).
3. A `.py` group file that does not parse at the commit (syntax error, NUL byte) — INSTRUMENT naming the file, no traceback. Pinned in Task 3.
4. A label in a decorator argument (`@pytest.mark.parametrize("x", ["ENC:BEH-04"])`) — counts for the decorated test. Pinned in Task 2.
5. A bare declared `BEH-03` when the test carries `ENC:BEH-03` — covered (`:` is not a token character; bare ids are unqualified by definition). Pinned in Task 3 (`TestBoundaries`).

---

### Task 1: Qualified ids in `**Scenarios:**`, one form per line

**Files:**
- Modify: `src/spec_runner/task.py:106-111` (comment + `SCENARIO_ID`), `src/spec_runner/task.py:213-224` (`_parse_scenarios`)
- Test: `tests/test_scenario_coverage.py` (classes `TestParsing`, `TestMalformedLineIsAValidateError`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `Task.scenarios` may now hold `"ENC:BEH-03"`-style strings; a line is homogeneous (all qualified or all bare).

- [ ] **Step 1: Create the branch**

```bash
git switch master && git pull --ff-only && git switch -c feat/603-slice-a-token-ownership
```

- [ ] **Step 2: Write the failing tests**

Append to `class TestParsing` in `tests/test_scenario_coverage.py`:

```python
    def test_qualified_ids_accepted(self, tmp_path):
        (task,) = _tasks(tmp_path, "**Scenarios:** ENC:BEH-03, ENC:BEH-04a, AB:X-1\n")
        assert task.scenarios == ["ENC:BEH-03", "ENC:BEH-04a", "AB:X-1"]
        assert task.scenarios_error is None
```

Append to `class TestMalformedLineIsAValidateError`:

```python
    def test_wrong_qualified_shape(self, tmp_path):
        for bad in (
            "E:BEH-09",  # code too short
            "ENCODES:BEH-09",  # code too long
            "enc:BEH-09",
            "ENC:beh-09",
            "ENC::BEH-09",
            "ENC:BEH-09:x",
            "ENC#BEH-09",
        ):
            joined = self._errors(tmp_path, f"**Scenarios:** {bad}")
            assert "TASK-001" in joined, bad
            assert bad in joined, bad

    def test_mixed_line_is_refused_naming_both_kinds(self, tmp_path):
        joined = self._errors(tmp_path, "**Scenarios:** ENC:BEH-03, BEH-04")
        assert "TASK-001" in joined
        assert "mixes" in joined
        assert "ENC:BEH-03" in joined and "BEH-04" in joined
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/test_scenario_coverage.py -k "qualified or mixed" -v`
Expected: FAIL — `test_qualified_ids_accepted` gets `scenarios is None`; `test_mixed_line_is_refused_naming_both_kinds` fails on `"mixes"`.

- [ ] **Step 4: Implement**

In `src/spec_runner/task.py`, replace the comment and regex at lines 106-111:

```python
# #402: the scenarios a verify_first group must carry. One line,
# comma-separated, stored as written and judged by `validate` — never
# guessed. The id shape is BEH-style with the one suffix seen in practice
# (`BEH-09a`), optionally qualified by a workstream code (#603, criteria
# norm §1.1/§1.3: `ENC:BEH-09`, code of 2-6 capitals). A line is all
# qualified or all bare: a mixed line has no single matching rule.
SCENARIOS = re.compile(r"\*\*Scenarios:\*\*(.*)$")
SCENARIO_ID = re.compile(r"(?:[A-Z]{2,6}:)?[A-Z]+-\d+[a-z]?")
```

Replace `_parse_scenarios` (lines 213-224):

```python
def _parse_scenarios(line: str, declared: str) -> tuple[list[str] | None, str | None]:
    """(ids, None) for a valid `**Scenarios:**` line, (None, refusal) otherwise."""
    items = [item.strip() for item in declared.split(",")]
    if items == [""]:
        return None, f"**Scenarios:** line is empty. Declared line: {line!r}"
    bad = [item for item in items if not SCENARIO_ID.fullmatch(item)]
    if bad:
        return None, (
            f"**Scenarios:** {bad!r} not of the form BEH-09 / BEH-09a / ENC:BEH-09 "
            f"(comma-separated [A-Z]+-<digits>[a-z], optionally prefixed by a "
            f"2-6 capital workstream code and ':'). Declared line: {line!r}"
        )
    qualified = [item for item in items if ":" in item]
    if qualified and len(qualified) != len(items):
        bare = [item for item in items if ":" not in item]
        return None, (
            f"**Scenarios:** mixes qualified ids ({', '.join(qualified)}) with bare "
            f"ones ({', '.join(bare)}) — one line is all CODE:ID or all bare. "
            f"Declared line: {line!r}"
        )
    return items, None
```

- [ ] **Step 5: Run the file**

Run: `uv run pytest tests/test_scenario_coverage.py -v`
Expected: PASS (all, including the pre-existing `test_wrong_shape`).

- [ ] **Step 6: Commit**

```bash
git add src/spec_runner/task.py tests/test_scenario_coverage.py
git commit -m "feat(#603): **Scenarios:** accepts qualified CODE:ID, one form per line

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: `criteria_tokens.py` — the §1.4 ownership rule

**Files:**
- Create: `src/spec_runner/criteria_tokens.py`
- Test: `tests/test_criteria_tokens.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `token_pattern(token: str) -> re.Pattern[str]`
  - `class TestDefinition` (frozen dataclass): `qualname: str`, `carrier_lines: frozenset[int]`
  - `class UnresolvedQualname(LookupError)` — `args[0]` is the dotted qualname
  - `select_tests(source: str, qualname: str | None = None) -> list[TestDefinition]` — raises `SyntaxError` / `ValueError` on unparseable source, `UnresolvedQualname` when `qualname` is absent
  - `carried_ids(source: str, definitions: Sequence[TestDefinition], ids: Sequence[str]) -> set[str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_criteria_tokens.py`:

```python
"""#603 slice A: token ownership by AST (criteria norm §1.4).

Spec: docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md §2.2
"""

from __future__ import annotations

import pytest

from spec_runner.criteria_tokens import (
    UnresolvedQualname,
    carried_ids,
    select_tests,
    token_pattern,
)

SOURCE = '''\
"""Module header ENC:BEH-01"""
import pytest

HELPER_NOTE = "ENC:BEH-02"


def helper():
    """ENC:BEH-03"""


@pytest.mark.parametrize("x", ["ENC:BEH-04"])
def test_top(x):
    """ENC:BEH-05"""

    def nested():
        """ENC:BEH-06"""

    assert nested


class TestGroup:
    """ENC:BEH-07"""

    def test_a(self):
        """ENC:BEH-08"""

    def test_b(self):
        pass

    def helper(self):
        """ENC:BEH-09"""

    class TestInner:
        def test_c(self):
            """ENC:BEH-10"""


class Helper:
    def test_d(self):
        """ENC:BEH-11"""


async def test_async():
    """ENC:BEH-12"""
'''

ALL = [f"ENC:BEH-{i:02d}" for i in range(1, 13)]


def _carried(qualname: str | None, source: str = SOURCE) -> list[str]:
    ids = carried_ids(source, select_tests(source, qualname), ALL)
    return sorted(i[-2:] for i in ids)


class TestFunctionEntry:
    def test_decorator_and_body_count_nested_helper_does_not(self):
        assert _carried("test_top") == ["04", "05"]

    def test_method_gets_its_class_region(self):
        assert _carried("TestGroup.test_a") == ["07", "08"]

    def test_method_token_does_not_reach_its_sibling(self):
        assert _carried("TestGroup.test_b") == ["07"]

    def test_nested_class_method_gets_every_enclosing_class_region(self):
        assert _carried("TestGroup.TestInner.test_c") == ["07", "10"]

    def test_a_named_function_is_taken_as_given(self):
        assert _carried("Helper.test_d") == ["11"]


class TestClassEntry:  # Review Focus 1
    def test_default_named_methods_and_class_region_not_helpers(self):
        assert _carried("TestGroup") == ["07", "08", "10"]


class TestFileEntry:
    def test_static_approximation_of_default_collection(self):
        # Module header, module constant, helper function, nested helper,
        # helper method and a non-Test* class all own nothing.
        assert _carried(None) == ["04", "05", "07", "08", "10", "12"]

    def test_redefinition_keeps_the_last_definition(self):
        source = 'def test_x():\n    """ENC:BEH-01"""\n\n\ndef test_x():\n    pass\n'
        assert carried_ids(source, select_tests(source, "test_x"), ["ENC:BEH-01"]) == set()


REDEFINED_CLASS = (
    "class TestA:\n"
    "    def test_old(self):\n"
    '        """BEH-01"""\n'
    "\n"
    "\n"
    "class TestA:\n"
    "    def test_new(self):\n"
    "        pass\n"
)

CLASS_REPLACED_BY_FUNCTION = (
    "class TestA:\n"
    "    def test_old(self):\n"
    '        """BEH-01"""\n'
    "\n"
    "\n"
    "def TestA():\n"
    "    pass\n"
)


class TestRedefinedClass:
    """A replaced class takes its methods with it (plan review, P1)."""

    @pytest.mark.parametrize("source", [REDEFINED_CLASS, CLASS_REPLACED_BY_FUNCTION])
    def test_file_selection_excludes_obsolete_methods(self, source):
        assert carried_ids(source, select_tests(source), ["BEH-01"]) == set()
        assert all(d.qualname != "TestA.test_old" for d in select_tests(source))

    def test_class_selection_excludes_obsolete_methods(self):
        selected = select_tests(REDEFINED_CLASS, "TestA")
        assert [d.qualname for d in selected] == ["TestA.test_new"]
        assert carried_ids(REDEFINED_CLASS, selected, ["BEH-01"]) == set()

    @pytest.mark.parametrize("source", [REDEFINED_CLASS, CLASS_REPLACED_BY_FUNCTION])
    def test_explicit_obsolete_method_is_unresolved(self, source):
        with pytest.raises(UnresolvedQualname):
            select_tests(source, "TestA.test_old")


class TestLineAlignment:
    def test_form_feed_does_not_shift_lines(self):
        # str.splitlines() would split at \x0c and read the label from the
        # wrong line; ast numbers lines by \n, \r\n and \r only.
        source = "# page\x0cbreak\ndef test_x():\n    '''ENC:BEH-01'''\n"
        assert carried_ids(source, select_tests(source), ["ENC:BEH-01"]) == {"ENC:BEH-01"}

    def test_crlf_source(self):
        source = "def test_x():\r\n    '''ENC:BEH-01'''\r\n"
        assert carried_ids(source, select_tests(source), ["ENC:BEH-01"]) == {"ENC:BEH-01"}


class TestRefusals:
    def test_unknown_qualname(self):
        with pytest.raises(UnresolvedQualname) as raised:
            select_tests(SOURCE, "TestGroup.test_nope")
        assert raised.value.args[0] == "TestGroup.test_nope"

    @pytest.mark.parametrize("source", ["def (:\n", "x = '\x00'\n"])
    def test_unparseable_source_raises(self, source):
        with pytest.raises((SyntaxError, ValueError)):
            select_tests(source)


class TestTokenPattern:
    @pytest.mark.parametrize("text", ["ENC:BEH-03", "(ENC:BEH-03)", "ENC:BEH-03,"])
    def test_matches(self, text):
        assert token_pattern("ENC:BEH-03").search(text)

    @pytest.mark.parametrize("text", ["XENC:BEH-03", "ENC:BEH-030", "ENC:BEH-03a", "_ENC:BEH-03"])
    def test_does_not_match(self, text):
        assert not token_pattern("ENC:BEH-03").search(text)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_criteria_tokens.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'spec_runner.criteria_tokens'`.

- [ ] **Step 3: Implement**

Create `src/spec_runner/criteria_tokens.py`:

```python
"""Token ownership by AST (#603, criteria-closure norm §1.4).

A scenario token counts for a test only when it stands in that test's
*region*. A function or method region runs from its first decorator to its
last line, minus the ranges of nested definitions — a nested helper's token
does not count for the enclosing test. A class region is its decorators,
header, docstring and body outside nested definitions; it counts for every
test method of the class (and of classes nested in it). The module header,
module constants and neighbouring definitions own nothing.

Regions are computed on source lines because comments are invisible to
`ast`. One module, so the `verify_first` gate and the `verify --criteria`
measurement give the same answer to "which tests carry this id".

Design: docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md §2.2
"""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from dataclasses import dataclass

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_Definition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
_Indexed = tuple[_Definition, tuple[ast.ClassDef, ...]]

# ast numbers lines by \n, \r\n and \r only; str.splitlines() also splits at
# \x0c, \x1c,   and others, which would read a label from the wrong line.
_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def token_pattern(token: str) -> re.Pattern[str]:
    """`token` as a whole token: no letter, digit or `_` on either side (§1.3)."""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])")


@dataclass(frozen=True)
class TestDefinition:
    """One test and the source lines whose tokens count for it."""

    __test__ = False  # a data class, not a pytest test class

    qualname: str
    carrier_lines: frozenset[int]


class UnresolvedQualname(LookupError):
    """The group entry names a definition the module does not contain."""


def select_tests(source: str, qualname: str | None = None) -> list[TestDefinition]:
    """The tests a group entry selects in `source`, each with its carrier lines.

    `qualname` is dotted (`TestLogin.test_ok`). A function is taken as given;
    a class selects its default-named test methods. `None` (a file-only entry)
    is a **static approximation of default pytest collection**: module-level
    `test*` functions and `test*` methods of `Test*` classes (module-level or
    nested in a `Test*` class). Custom `python_functions`/`python_classes`,
    collection hooks and runtime behaviour are not read.

    Raises `SyntaxError`/`ValueError` when `source` does not parse and
    `UnresolvedQualname` when `qualname` is not defined in it. A redefined
    name keeps its last definition, as Python and pytest do.
    """
    index = _index(ast.parse(source))
    if qualname is None:
        chosen = [
            (name, node, enclosing)
            for name, (node, enclosing) in index.items()
            if _is_test_function(node) and all(map(_is_test_class, enclosing))
        ]
    elif qualname not in index:
        raise UnresolvedQualname(qualname)
    else:
        node, enclosing = index[qualname]
        if isinstance(node, ast.ClassDef):
            depth = len(enclosing) + 1
            chosen = [
                (name, inner, inner_enclosing)
                for name, (inner, inner_enclosing) in index.items()
                if name.startswith(f"{qualname}.")
                and _is_test_function(inner)
                and all(map(_is_test_class, inner_enclosing[depth:]))
            ]
        else:
            chosen = [(qualname, node, enclosing)]
    return [TestDefinition(name, _carriers(node, enclosing)) for name, node, enclosing in chosen]


def carried_ids(
    source: str, definitions: Sequence[TestDefinition], ids: Sequence[str]
) -> set[str]:
    """The `ids` that stand, as whole tokens, on a carrier line of any definition."""
    lines = _LINE_BREAK.split(source)
    carrier: set[int] = set()
    for definition in definitions:
        carrier |= definition.carrier_lines
    texts = [lines[number - 1] for number in sorted(carrier) if 0 < number <= len(lines)]
    return {token for token in ids if any(token_pattern(token).search(t) for t in texts)}


def _index(tree: ast.Module) -> dict[str, _Indexed]:
    """Every module-level definition and every definition nested in a class,
    by dotted qualname, with the classes that enclose it (outermost first)."""
    found: dict[str, _Indexed] = {}

    def visit(body: list[ast.stmt], prefix: str, enclosing: tuple[ast.ClassDef, ...]) -> None:
        for node in body:
            if isinstance(node, _DEFINITIONS):
                name = f"{prefix}{node.name}"
                # The last definition wins, as in Python: the replaced one and
                # everything nested in it are gone — a redefined class's old
                # methods must not keep carrying labels.
                for stale in [k for k in found if k == name or k.startswith(f"{name}.")]:
                    del found[stale]
                found[name] = (node, enclosing)
                if isinstance(node, ast.ClassDef):
                    visit(node.body, f"{name}.", (*enclosing, node))

    visit(tree.body, "", ())
    return found


def _span(node: _Definition) -> range:
    start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
    return range(start, (node.end_lineno or node.lineno) + 1)


def _carriers(node: _Definition, enclosing: tuple[ast.ClassDef, ...]) -> frozenset[int]:
    """The test's own region plus the region of every class enclosing it."""
    lines = set(_region(node))
    for cls in enclosing:
        lines |= _region(cls)
    return frozenset(lines)


def _region(node: _Definition) -> frozenset[int]:
    """`node`'s own lines: first decorator to last line, minus nested definitions."""
    lines = set(_span(node))
    for inner in ast.walk(node):
        if inner is not node and isinstance(inner, _DEFINITIONS):
            lines.difference_update(_span(inner))
    return frozenset(lines)


def _is_test_function(node: _Definition) -> bool:
    return isinstance(node, _FUNCTIONS) and node.name.startswith("test")


def _is_test_class(node: ast.ClassDef) -> bool:
    return node.name.startswith("Test")
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_criteria_tokens.py -v`
Expected: PASS (all).

- [ ] **Step 5: Type-check and lint the new module**

Run: `uv run ruff check src/spec_runner/criteria_tokens.py tests/test_criteria_tokens.py && uv run ruff format --check src/spec_runner/criteria_tokens.py tests/test_criteria_tokens.py && uv run mypy src/spec_runner/criteria_tokens.py && pyrefly check`
Expected: no errors.

Note the name: the selector is `select_tests`, not `test_*` — a module-level
name starting with `test` imported into a test module would be collected by
pytest as a test function.

- [ ] **Step 6: Commit**

```bash
git add src/spec_runner/criteria_tokens.py tests/test_criteria_tokens.py
git commit -m "feat(#603): criteria_tokens — token ownership by AST (norm §1.4)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The gate — one rule per entry, at the commit

**Files:**
- Modify: `src/spec_runner/scenarios.py` (module docstring; `scenario_pattern`, `group_files`; add `split_entry`, `GroupCoverage`, `group_coverage`; rewrite `coverage_refusal`)
- Modify: `tests/test_scenario_coverage.py` (the `repo` fixture's module-level labels)
- Create: `tests/test_scenario_ownership.py`
- Modify: `tests/test_scenario_coverage_at_entry.py` (one new class)

**Interfaces:**
- Consumes: `token_pattern`, `select_tests`, `carried_ids`, `UnresolvedQualname` (Task 2).
- Produces:
  - `split_entry(raw: str) -> tuple[PurePosixPath, str | None]` — (file, dotted qualname or `None`)
  - `@dataclass(frozen=True) class GroupCoverage: missing: list[str]; problems: list[str]`
  - `group_coverage(scenarios: Sequence[str], entries: Sequence[str], texts: Mapping[PurePosixPath, str]) -> GroupCoverage` — entries whose file is not in `texts` are skipped
  - `coverage_refusal(task, root, sha)` — signature unchanged

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scenario_ownership.py`:

```python
"""#603 slice A: the verify_first gate judges ownership per group entry.

Spec: docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md §2.3
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import pytest

from spec_runner.phases import RefusalKind
from spec_runner.scenarios import coverage_refusal, group_coverage, split_entry
from spec_runner.task import Task

F = PurePosixPath("tests/test_x.py")

TWO_TESTS = (
    '"""Module header BEH-01 ENC:BEH-01"""\n'
    "\n"
    "\n"
    "def helper():\n"
    '    """BEH-02"""\n'
    "\n"
    "\n"
    "def test_a():\n"
    '    """BEH-03 ENC:BEH-03"""\n'
    "\n"
    "\n"
    "def test_b():\n"
    '    """BEH-04"""\n'
    "\n"
    "\n"
    "class TestK:\n"
    '    """BEH-05"""\n'
    "\n"
    "    def test_m(self):\n"
    "        pass\n"
)


def _missing(scenarios, entries, text=TWO_TESTS):
    return group_coverage(scenarios, entries, {F: text}).missing


class TestSplitEntry:  # Review Focus 2
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("tests/test_x.py::test_a", (F, "test_a")),
            ("./tests/test_x.py::TestK::test_m", (F, "TestK.test_m")),
            ("tests/test_x.py::test_a[a::b]", (F, "test_a")),
            ("tests/test_x.py::test_a[x[1]]", (F, "test_a")),
            ("tests/test_x.py", (F, None)),
            ("test/x_test.exs:12", (PurePosixPath("test/x_test.exs"), None)),
        ],
    )
    def test_split(self, raw, expected):
        assert split_entry(raw) == expected


class TestOwnership:
    def test_node_id_entry_counts_only_its_own_definition(self):
        assert _missing(["BEH-03", "BEH-04"], ["tests/test_x.py::test_a"]) == ["BEH-04"]

    def test_module_header_and_helper_do_not_count_bare_or_qualified(self):
        entries = ["tests/test_x.py::test_a"]
        assert _missing(["BEH-01", "BEH-02"], entries) == ["BEH-01", "BEH-02"]
        assert _missing(["ENC:BEH-01"], entries) == ["ENC:BEH-01"]

    def test_class_region_counts_for_its_method(self):
        assert _missing(["BEH-05"], ["tests/test_x.py::TestK::test_m"]) == []

    def test_class_entry(self):  # Review Focus 1
        assert _missing(["BEH-05", "BEH-03"], ["tests/test_x.py::TestK"]) == ["BEH-03"]

    def test_file_entry_uses_default_naming(self):
        assert _missing(["BEH-02", "BEH-03", "BEH-04", "BEH-05"], ["tests/test_x.py"]) == [
            "BEH-02"
        ]

    def test_parametrize_suffix_resolves(self):  # Review Focus 2
        assert _missing(["ENC:BEH-03"], ["tests/test_x.py::test_a[a::b]"]) == []

    def test_any_entry_may_carry_an_id(self):
        entries = ["tests/test_x.py::test_a", "tests/test_x.py::test_b"]
        assert _missing(["BEH-03", "BEH-04"], entries) == []

    def test_non_python_file_keeps_the_per_file_match(self):
        exs = PurePosixPath("test/x_test.exs")
        text = "# BEH-09 at the top of the file\ntest \"x\" do\nend\n"
        coverage = group_coverage(["BEH-09"], ["test/x_test.exs:2"], {exs: text})
        assert coverage.missing == [] and coverage.problems == []

    def test_file_absent_from_texts_is_skipped(self):
        coverage = group_coverage(["BEH-03"], ["tests/other.py::test_a"], {F: TWO_TESTS})
        assert coverage.missing == ["BEH-03"] and coverage.problems == []


class TestBoundaries:
    def test_bare_id_is_carried_by_its_qualified_spelling(self):  # Review Focus 5
        assert _missing(["BEH-03"], ["tests/test_x.py::test_a"]) == []
        text = 'def test_a():\n    """ENC:BEH-03"""\n'
        assert _missing(["BEH-03"], ["tests/test_x.py::test_a"], text) == []

    @pytest.mark.parametrize("label", ["XENC:BEH-03", "ENC:BEH-030", "ENC:BEH-03a"])
    def test_qualified_boundaries(self, label):
        text = f'def test_a():\n    """{label}"""\n'
        assert _missing(["ENC:BEH-03"], ["tests/test_x.py::test_a"], text) == ["ENC:BEH-03"]


class TestProblems:
    def test_unresolved_qualname(self):
        coverage = group_coverage(["BEH-03"], ["tests/test_x.py::test_nope"], {F: TWO_TESTS})
        assert coverage.problems and "test_nope is not defined in tests/test_x.py" in (
            coverage.problems[0]
        )

    @pytest.mark.parametrize("text", ["def (:\n", "x = '\x00'\n"])  # Review Focus 3
    def test_unparseable_file(self, text):
        coverage = group_coverage(["BEH-03"], ["tests/test_x.py::test_a"], {F: text})
        assert coverage.problems and "tests/test_x.py cannot be parsed" in coverage.problems[0]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(tmp_path: Path, text: str) -> tuple[Path, str]:
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tests" / "test_x.py").write_text(text)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root, _git(root, "rev-parse", "HEAD")


def _task(verifies, scenarios):
    return Task(
        id="TASK-001",
        name="t",
        priority="p1",
        status="todo",
        estimate="1h",
        execution_mode="verify_first",
        verifies=verifies,
        scenarios=scenarios,
    )


class TestCoverageRefusal:
    def test_module_header_label_is_terminal_policy_naming_the_place(self, tmp_path):
        root, sha = _commit(tmp_path, TWO_TESTS)
        refusal = coverage_refusal(_task(["tests/test_x.py::test_a"], ["BEH-01"]), root, sha)
        assert refusal is not None
        assert refusal.kind is RefusalKind.POLICY and refusal.terminal
        assert "BEH-01" in refusal and "test definition" in refusal
        assert "docstring" not in refusal

    def test_unresolved_qualname_is_instrument(self, tmp_path):
        root, sha = _commit(tmp_path, TWO_TESTS)
        refusal = coverage_refusal(_task(["tests/test_x.py::test_nope"], ["BEH-03"]), root, sha)
        assert refusal is not None and refusal.kind is RefusalKind.INSTRUMENT
        assert "test_nope" in refusal and sha[:12] in refusal

    def test_unparseable_file_is_instrument(self, tmp_path):  # Review Focus 3
        root, sha = _commit(tmp_path, "def (:\n")
        refusal = coverage_refusal(_task(["tests/test_x.py::test_a"], ["BEH-03"]), root, sha)
        assert refusal is not None and refusal.kind is RefusalKind.INSTRUMENT
        assert "cannot be parsed" in refusal

    def test_owned_label_passes(self, tmp_path):
        root, sha = _commit(tmp_path, TWO_TESTS)
        task = _task(["tests/test_x.py::test_a"], ["ENC:BEH-03"])
        assert coverage_refusal(task, root, sha) is None
```

In `tests/test_scenario_coverage.py`, the `repo` fixture's labels sit at module level, which no longer counts. Move them into a test (these two files are the only change to existing expectations):

```python
    (root / "tests" / "test_a.py").write_text(
        'def test_x():\n    """kind: e2e — BEH-09"""\n'
    )
    (root / "tests" / "test_bin.py").write_bytes(
        b"def test_x():\n    # \xff\xfe BEH-10\n    pass\n"
    )
```

and in `TestAtCommit.test_working_tree_label_does_not_count` replace the uncommitted write with an owned label, so the test still proves "commit, not tree" rather than "module header":

```python
        (repo / "tests" / "test_a.py").write_text('def test_x():\n    """BEH-09 BEH-10"""\n')
```

Append to `tests/test_scenario_coverage_at_entry.py`:

```python
MODULE_LABEL = '"""kind: e2e — BEH-09"""\n\n\ndef test_it():\n    assert True\n'


class TestModuleLabelNoLongerCounts:
    """#603 Changed: a label in the module header is refused at the entry run."""

    def test_refused_terminally_before_any_paid_call(self, tmp_path, paid):
        impl, red_calls = paid
        cfg = _cfg(_repo(tmp_path, MODULE_LABEL))
        state = ExecutorState(cfg)

        assert execute_task(_task(["BEH-09"]), cfg, state) == "TERMINAL_REFUSAL"

        impl.assert_not_called()
        assert red_calls == []
        error = state.get_task_state("TASK-001").last_error or ""
        assert "BEH-09" in error and "test definition" in error
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scenario_ownership.py tests/test_scenario_coverage_at_entry.py tests/test_scenario_coverage.py -v`
Expected: FAIL — `ImportError: cannot import name 'group_coverage'`; the entry test proceeds instead of refusing.

- [ ] **Step 3: Implement**

Replace `src/spec_runner/scenarios.py` with:

```python
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
        except (SyntaxError, ValueError) as exc:
            problems.append(f"{path} cannot be parsed as Python ({exc})")
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
    only repeat it. A file git cannot show at `sha`, a `.py` file that does not
    parse, or a qualname the file does not define is an INSTRUMENT refusal —
    coverage could not be judged. A task without `**Scenarios:**` is not
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
        return Refusal(
            f"scenario coverage at {sha[:12]} cannot be judged: {'; '.join(coverage.problems)}",
            RefusalKind.INSTRUMENT,
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
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_scenario_ownership.py tests/test_scenario_coverage.py tests/test_scenario_coverage_at_entry.py tests/test_criteria_tokens.py -v`
Expected: PASS (all). If a pre-existing `TestValidateWarnings` test fails on a module-docstring label, leave it for Task 4 — it is `validate`'s copy of the rule.

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/scenarios.py tests/test_scenario_ownership.py tests/test_scenario_coverage.py tests/test_scenario_coverage_at_entry.py
git commit -m "feat(#603): verify_first gate judges token ownership per group entry

A declared id in a .py group file counts only inside the test definition
the entry names, or its containing test class; non-.py files keep the
per-file match. Unparseable files and unresolved qualnames are
INSTRUMENT refusals.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `validate` — the same rule on the working tree, reworded mode warning

**Files:**
- Modify: `src/spec_runner/validate.py:874-879` (mode warning), `src/spec_runner/validate.py:943-975` (`_scenario_warnings`)
- Test: `tests/test_scenario_coverage.py` (`TestValidateWarnings`, `TestValidateWarningMinors`)

**Interfaces:**
- Consumes: `group_coverage`, `group_files` (Task 3).
- Produces: nothing new for later tasks.

- [ ] **Step 1: Write / adjust the tests**

In `tests/test_scenario_coverage.py`, every `'"""BEH-09"""\ndef test_x():\n    pass\n'` fixture text (in `TestValidateWarnings` and `TestValidateWarningMinors`) becomes an owned label:

```python
OWNED = 'def test_x():\n    """BEH-09"""\n'
```

(define `OWNED` once at module level next to `HEADER`, and use it in `test_uncovered_in_the_tree_is_a_warning`, `test_covered_in_the_tree_is_silent`, `test_a_group_file_outside_the_root_is_not_read` and `test_a_partly_missing_group_is_still_judged`). Then append to `class TestValidateWarnings`:

```python
    def test_module_header_label_now_warns(self, tmp_path):  # #603 Changed
        result = _validate(
            tmp_path,
            "**Mode:** verify_first\n**Verifies:** tests/test_a.py::test_x\n"
            "**Scenarios:** BEH-09\n",
            {"tests/test_a.py": '"""BEH-09"""\ndef test_x():\n    pass\n'},
        )
        assert result.ok
        joined = "\n".join(result.warnings)
        assert "BEH-09" in joined and "uncovered" in joined and "test definition" in joined

    def test_unresolved_qualname_warns(self, tmp_path):
        result = _validate(
            tmp_path,
            "**Mode:** verify_first\n**Verifies:** tests/test_a.py::test_nope\n"
            "**Scenarios:** BEH-09\n",
            {"tests/test_a.py": OWNED},
        )
        assert result.ok
        assert any("test_nope is not defined" in w for w in result.warnings)

    def test_qualified_ids_outside_verify_first_warn_without_failing(self, tmp_path):
        # default mode (standard), as in test_outside_verify_first_is_a_warning
        result = _validate(tmp_path, "**Scenarios:** ENC:BEH-09, ENC:BEH-10\n")
        assert result.ok
        assert any(
            "TASK-001" in w
            and "task-level token ownership" in w
            and "not checked outside verify_first" in w
            for w in result.warnings
        )
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_scenario_coverage.py -k "Validate" -v`
Expected: FAIL — `test_module_header_label_now_warns` (still silent), `test_unresolved_qualname_warns`, `test_qualified_ids_outside_verify_first_warn_without_failing` (old wording).

- [ ] **Step 3: Implement**

In `src/spec_runner/validate.py`, the mode warning (lines 874-879) becomes:

```python
        if mode != "verify_first":
            if task.scenarios is not None:
                result.warnings.append(
                    f"{task.id}: **Scenarios:** {task.scenarios!r} declared but the "
                    f"resolved execution mode is {mode!r} — task-level token ownership "
                    "is not checked outside verify_first"
                )
```

Replace `_scenario_warnings` (lines 943-975) with:

```python
def _scenario_warnings(task: Task, root: Path) -> list[str]:
    """#402 §6 / #603: early notice of a declared scenario the group's files in
    the WORKING TREE do not carry, by the gate's own rule (`group_coverage`).
    A warning only — the live entry run judges the commit and is the one that
    refuses.

    Missing files are skipped — they carry their own warning above — and when
    no group file is present at all nothing is judged: "uncovered" over zero
    files only restates that warning (PR #590 minor). A file past the root (a
    node id may spell `..`) is named, never read."""
    from spec_runner.scenarios import group_coverage, group_files

    if not task.scenarios:
        return []
    base = root.resolve()
    files = [(path, root / str(path)) for path in group_files(task.verifies or [])]
    outside = [path for path, f in files if not f.resolve().is_relative_to(base)]
    if outside:
        return [
            f"{task.id}: **Scenarios:** group file {str(path)!r} is outside the "
            "project root — not read for coverage"
            for path in outside
        ]
    texts = {path: f.read_text(errors="replace") for path, f in files if f.is_file()}
    if not texts:
        return []
    coverage = group_coverage(task.scenarios, task.verifies or [], texts)
    warnings = [
        f"{task.id}: **Scenarios:** coverage cannot be judged in the working tree: "
        f"{problem} — the live entry run will refuse it as an instrument error"
        for problem in coverage.problems
    ]
    if coverage.missing:
        warnings.append(
            f"{task.id}: **Scenarios:** {', '.join(coverage.missing)} uncovered by the "
            "declared group's files in the working tree (in a Python test file only the "
            "test definition the group names or its containing test class counts) — the "
            "live entry run will refuse unless the committed files carry them"
        )
    return warnings
```

- [ ] **Step 4: Run the scenario suites**

Run: `uv run pytest tests/test_scenario_coverage.py tests/test_scenario_ownership.py tests/test_scenario_coverage_at_entry.py tests/test_criteria_tokens.py tests/test_validate.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/validate.py tests/test_scenario_coverage.py
git commit -m "feat(#603): validate judges scenarios by the gate's ownership rule

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Documentation, CHANGELOG, TODO, and the full gate

**Files:**
- Modify: `spec/FORMAT.md:121-131` (the `Scenarios` bullet)
- Modify: `CHANGELOG.md:11` (`## [Unreleased]`)
- Modify: `CLAUDE.md` (module table: `scenarios.py` row, new `criteria_tokens.py` row; Testing list: `test_criteria_tokens.py`, `test_scenario_ownership.py`)
- Modify: `TODO.md` (the `criteria-closure-verify` item)

**Interfaces:**
- Consumes: the behaviour of Tasks 1-4.
- Produces: nothing for code.

- [ ] **Step 1: `spec/FORMAT.md`** — replace the `Scenarios` bullet with:

```markdown
- `Scenarios` — optional, `verify_first` only: comma-separated scenario ids
  the `Verifies` group must carry — bare (`BEH-09`, `BEH-09a`) or qualified
  by a workstream code of 2-6 capitals (`ENC:BEH-09`). One line is all bare
  or all qualified; a mixed line is an error. At the live entry run, before
  any paid call, every id must appear as a whole token (`BEH-09` does not
  match `BEH-091`, `BEH-09a` or `TestBEH09`; `ENC:BEH-09` does not match
  `XENC:BEH-09`) in the group **as committed**; otherwise the task is refused
  terminally, naming the uncovered ids. In a Python (`.py`) group file the
  id counts only inside the test definition the entry names — from its first
  decorator to its end, not in nested helpers — or in the body of a class
  containing it (outside its methods); the module header, module constants
  and helper functions do not count. A file-only entry uses a **static
  approximation of default pytest collection** (module-level `test*`
  functions, `test*` methods of `Test*` classes); custom `python_functions`
  / `python_classes`, collection hooks and runtime behaviour are not read.
  A `.py` file that does not parse, or a node id naming a definition the
  file does not contain, is an instrument refusal. In any other file
  (ExUnit `path:line`) the id may stand anywhere in the file. A bare id
  also matches its qualified spelling (`BEH-09` in `ENC:BEH-09`). `validate`
  only warns (the working tree is not the commit), and warns without
  failing when the line appears on a `tdd`/`standard` task; an empty or
  malformed line is an error. Without the line nothing changes.
```

- [ ] **Step 2: `CHANGELOG.md`** — under `## [Unreleased]` insert:

```markdown
### Changed

- **`**Scenarios:**` labels in a Python group file must sit inside the test
  definition** (#603). A declared id used to count anywhere in a group file;
  in a `.py` file it now counts only inside the test definition the
  `**Verifies:**` entry names (from its first decorator to its end, minus
  nested helpers) or in the body of a class containing it. **Labels in a
  module docstring, module header, module constant or helper function no
  longer satisfy the gate — move them into the test definition or its test
  class.** A file-only entry uses a static approximation of default pytest
  collection. Non-Python group files (ExUnit `path:line`) keep the per-file
  match. A `.py` group file that does not parse, or a node id naming an
  absent definition, is an instrument refusal. `validate` applies the same
  rule to the working tree as warnings.

### Added

- **Qualified scenario ids** (#603): `**Scenarios:** ENC:BEH-03, ENC:BEH-04`
  — a workstream code of 2-6 capitals and `:` before the id (criteria-closure
  norm §1.1/§1.3). A line is all qualified or all bare; a mixed line is a
  named error. Qualified ids are accepted on `tdd`/`standard` tasks with a
  non-failing warning that task-level token ownership is not checked there.
- `criteria_tokens.py`: the token-ownership rule (norm §1.4) as one module,
  shared by the gate and, in slice B, `verify --criteria`.
```

- [ ] **Step 3: `CLAUDE.md`** — replace the `scenarios.py` row of the module table with:

```markdown
| `scenarios.py` | ~170 | Verify-first scenario coverage (#402, #603): `split_entry` (`path::Class::test[param]` → file + dotted qualname, the parametrize suffix cut first), `group_coverage` (the rule per entry: `.py` → `criteria_tokens` ownership for bare and qualified ids, any other file → per-file whole token; returns `missing` + `problems`), `coverage_refusal` — terminal POLICY when a declared id is carried by no entry at the entry run's commit, INSTRUMENT when a file cannot be read, a `.py` file does not parse or a qualname is not defined; `None` for a task without the line. Called once from `execution._run_verify_first_phase`; `validate._scenario_warnings` runs the same `group_coverage` on the working tree |
```

and add below it:

```markdown
| `criteria_tokens.py` | ~130 | Token ownership by AST (#603, criteria norm §1.4): `token_pattern` (§1.3 boundaries), `TestDefinition(qualname, carrier_lines)`, `select_tests(source, qualname=None)` (a function region = first decorator to end minus nested definitions; a class region counts for its test methods; `None` = static approximation of default pytest collection; `UnresolvedQualname`), `carried_ids`. Lines split by `\r\n|\r|\n` as `ast` numbers them. Shared by the gate and slice B's `verify --criteria` |
```

In the Testing paragraph, after `test_scenario_coverage.py + test_scenario_coverage_at_entry.py (#402: …)`, add: `` `test_criteria_tokens.py` (#603: function/method/class regions, decorator lines, nested helpers, the default-naming approximation, redefinition, form-feed and CRLF line alignment, §1.3 boundaries) and `test_scenario_ownership.py` (#603: entry splitting incl. `[a::b]`, the node-id entry owning only its definition, class and file entries, bare vs qualified, non-Python files per file, unparseable files and unresolved qualnames as INSTRUMENT), ``.

- [ ] **Step 4: `TODO.md`** — under the `criteria-closure-verify` item add:

```markdown
      - [x] slice A — qualified ids + AST token ownership in the verify_first gate
            (design §2; plan `docs/superpowers/plans/2026-09-29-criteria-closure-slice-a.md`)
      - [ ] slice B — `verify --criteria` + schemas `criteria-closure/v1`; release = X
            @blocked_by:devtools#491
```

- [ ] **Step 5: Full verification**

Run, in order (formatting → types → lint → tests):

```bash
uv run ruff format .
uv run mypy src
pyrefly check
uv run ruff check .
uv run pytest tests/ -v -m "not slow"
uv run pytest tests/ -v -m slow
```

Expected: no formatting diff left, no type or lint errors, every test passes. If an unrelated test fails, re-run it alone on `master` before touching anything — do not edit it to pass.

- [ ] **Step 6: Commit**

```bash
git add spec/FORMAT.md CHANGELOG.md CLAUDE.md TODO.md
git commit -m "docs(#603): slice A — FORMAT, CHANGELOG (Changed), CLAUDE.md, TODO

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

- [ ] **Step 7: Review loop and PR** (repo rule, `CLAUDE.md` › Git workflow)

```bash
sh scripts/review/local-claude.sh      # one round; stop when it yields no blocking finding
git push -u origin feat/603-slice-a-token-ownership
gh pr create --draft --title "feat(#603): slice A — qualified scenario ids, AST token ownership" --body "…"
sh ../devtools/review-pr.sh spec-runner <pr>   # publish directly — no --dry-run
```

Merge only on approve with green checks: `gh pr checks <pr> --watch && GH_CONFIG_DIR=~/.config/review gh pr merge <pr> --merge --match-head-commit <sha>`, then `git switch master && git pull --ff-only`, delete the branch locally and on origin, `git fetch --prune`.
