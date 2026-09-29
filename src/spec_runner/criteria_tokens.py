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


def carried_ids(source: str, definitions: Sequence[TestDefinition], ids: Sequence[str]) -> set[str]:
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
