# Criteria closure — B1 (shared ownership fixtures) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the shared token-ownership fixtures agreed in devtools#491 (design §6.3) and the `criteria_tokens.owned_definitions` function they describe, so devtools can vendor them and reach parser parity before slice B2.

**Architecture:** `criteria_tokens.py` gains `QUALIFIED_TOKEN` and `owned_definitions(source)`: every function and method the index holds (not classes, not functions nested in functions), with its start line and the qualified tokens it owns, using the same regions as the gate. Nineteen byte-exact case files plus hand-checked `expected.json` files live under `tests/fixtures/criteria-closure/v1/ownership/`, protected by `-text` and excluded from ruff. A contract test pins every case against the code.

**Tech Stack:** Python ≥ 3.11 stdlib (`ast`, `re`, `json`), pytest, ruff, mypy.

**Spec:** `docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md` §2.2 (parser rules agreed with devtools) and §6.3 (fixture form).

## Global Constraints

- Fixture path: `tests/fixtures/criteria-closure/v1/ownership/<NN>_<slug>.py` + `<NN>_<slug>.expected.json`.
- `expected.json`: `{"case", "owned": [{"qualname", "line", "tokens"}], "selection": {"file_only": [...]}}`, `owned` sorted by `(line, qualname)`, `tokens` sorted unique; an unparseable case is `{"case", "error": "syntax"}` instead.
- `owned` lists every function and method `criteria_tokens._index` holds — including non-`test*` ones; no classes; no `def` nested in a function. `line` = first decorator, else `def`. `tokens` = qualified tokens (`[A-Z]{2,6}:[A-Z]+-\d+[a-z]?`, §1.3 boundaries) on the definition's carrier lines (own region + every enclosing class region).
- Fixture bytes are the cases: `.gitattributes` `tests/fixtures/criteria-closure/** -text`; ruff `extend-exclude` covers the directory (a syntax-error and a NUL case are deliberate).
- Fixture file names must not match pytest's `test_*.py`/`*_test.py` (they are data, not tests).
- Ruff line length 100; mypy strict; commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Branch `feat/603-b1-ownership-fixtures`.

## Review Focus

1. A fixture file silently normalised by git or an editor (CRLF → LF, BOM stripped) — the byte guard test must fail, not the expectation test pass by accident. Pinned in Task 2 (`TestBytesAreTheCases`).
2. A token of a *different* code in the same file (`XENC:BEH-01` next to `ENC:BEH-01`) — both are qualified tokens and both are reported; §1.3 boundaries still apply. Pinned in Task 1.
3. A bare id (`BEH-01`) in a region — not a qualified token, not in `tokens`. Pinned in Task 1.
4. pytest collecting a fixture as a test module — names start with a digit, never `test_`. Pinned in Task 2 (`test_no_fixture_is_a_pytest_module`).
5. `ruff format .` rewriting a fixture — excluded in `pyproject.toml`; `uv run ruff format --check .` clean in Task 2.

---

### Task 1: `owned_definitions` in `criteria_tokens.py`

**Files:**
- Modify: `src/spec_runner/criteria_tokens.py`
- Test: `tests/test_criteria_tokens.py`

**Interfaces:**
- Consumes: `_index`, `_carriers`, `_span`, `_without_bom`, `_LINE_BREAK`, `_FUNCTIONS` (existing, slice A).
- Produces:
  - `QUALIFIED_TOKEN: re.Pattern[str]`
  - `@dataclass(frozen=True) class OwnedDefinition: qualname: str; line: int; tokens: tuple[str, ...]`
  - `owned_definitions(source: str) -> list[OwnedDefinition]` — raises `SyntaxError`/`ValueError` on unparseable source.

- [ ] **Step 1: Create the branch**

```bash
git switch master && git pull --ff-only && git switch -c feat/603-b1-ownership-fixtures
```

- [ ] **Step 2: Write the failing tests** — append to `tests/test_criteria_tokens.py`:

```python
from spec_runner.criteria_tokens import OwnedDefinition, owned_definitions  # noqa: E402


class TestOwnedDefinitions:
    """B1 (design §6.3): every indexed function/method with its qualified tokens."""

    def test_every_function_and_method_with_line_and_tokens(self):
        assert owned_definitions(SOURCE) == [
            OwnedDefinition("helper", 7, ("ENC:BEH-03",)),
            OwnedDefinition("test_top", 11, ("ENC:BEH-04", "ENC:BEH-05")),
            OwnedDefinition("TestGroup.test_a", 24, ("ENC:BEH-07", "ENC:BEH-08")),
            OwnedDefinition("TestGroup.test_b", 27, ("ENC:BEH-07",)),
            OwnedDefinition("TestGroup.helper", 30, ("ENC:BEH-07", "ENC:BEH-09")),
            OwnedDefinition("TestGroup.TestInner.test_c", 34, ("ENC:BEH-07", "ENC:BEH-10")),
            OwnedDefinition("Helper.test_d", 39, ("ENC:BEH-11",)),
            OwnedDefinition("test_async", 43, ("ENC:BEH-12",)),
        ]

    def test_bare_ids_are_not_qualified_tokens(self):  # Review Focus 3
        source = 'def test_a():\n    """BEH-01 ENC:BEH-02"""\n'
        assert owned_definitions(source) == [OwnedDefinition("test_a", 1, ("ENC:BEH-02",))]

    def test_other_codes_are_reported_with_boundaries(self):  # Review Focus 2
        source = 'def test_a():\n    """XENC:BEH-01 ENC:BEH-01 ENC:BEH-01xy _AB:BEH-02"""\n'
        assert owned_definitions(source) == [
            OwnedDefinition("test_a", 1, ("ENC:BEH-01", "XENC:BEH-01"))
        ]

    def test_unparseable_source_raises(self):
        with pytest.raises(Exception) as raised:
            owned_definitions("def (:\n")
        assert isinstance(raised.value, SyntaxError | ValueError)
```

(Move the new import to the file's import block; the `# noqa` is only for pasting mid-file.)

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/test_criteria_tokens.py -q`
Expected: collection ERROR — `ImportError: cannot import name 'OwnedDefinition'`.

- [ ] **Step 4: Implement** — in `src/spec_runner/criteria_tokens.py`, after `token_pattern`:

```python
#: A qualified scenario token (§1.3): a workstream code of 2-6 capitals, `:`,
#: then the id — with the same whole-token boundaries as `token_pattern`.
QUALIFIED_TOKEN = re.compile(r"(?<![A-Za-z0-9_])[A-Z]{2,6}:[A-Z]+-\d+[a-z]?(?![A-Za-z0-9_])")
```

after `TestDefinition`:

```python
@dataclass(frozen=True)
class OwnedDefinition:
    """One function or method and the qualified tokens it owns (design §6.3)."""

    qualname: str
    line: int
    tokens: tuple[str, ...]
```

and after `carried_ids`:

```python
def owned_definitions(source: str) -> list[OwnedDefinition]:
    """Every function and method the index holds, with the qualified tokens it owns.

    The shared-fixture view of §1.4 (devtools#491): not only tests — whether a
    definition is a test is pytest's collection's call — and no classes or
    functions nested in functions. `line` is the first decorator's, else the
    `def`'s, which is what `co_firstlineno` reports for the collected function.
    `tokens` are the qualified tokens on the definition's carrier lines (its own
    region and every enclosing class's), sorted, each once. Sorted by
    `(line, qualname)`. Raises `SyntaxError`/`ValueError` on unparseable source.
    """
    text = _without_bom(source)
    lines = _LINE_BREAK.split(text)
    owned: list[OwnedDefinition] = []
    for name, (node, enclosing) in _index(ast.parse(text)).items():
        if not isinstance(node, _FUNCTIONS):
            continue
        carrier = sorted(_carriers(node, enclosing))
        tokens = {
            match.group(0)
            for number in carrier
            if 0 < number <= len(lines)
            for match in QUALIFIED_TOKEN.finditer(lines[number - 1])
        }
        owned.append(OwnedDefinition(name, _span(node).start, tuple(sorted(tokens))))
    return sorted(owned, key=lambda d: (d.line, d.qualname))
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_criteria_tokens.py -q && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src`
Expected: all pass; no lint, format or type errors.

- [ ] **Step 6: Commit**

```bash
git add src/spec_runner/criteria_tokens.py tests/test_criteria_tokens.py
git commit -m "feat(#603): criteria_tokens.owned_definitions — the shared-fixture view of §1.4

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: The fixtures, their protection, and the contract test

**Files:**
- Create: `tests/fixtures/criteria-closure/v1/ownership/*.py` (19) and `*.expected.json` (19), `tests/fixtures/criteria-closure/v1/ownership/README.md`
- Modify: `.gitattributes`, `pyproject.toml` (`[tool.ruff] extend-exclude`)
- Create: `tests/test_ownership_fixtures.py`
- Modify: `CHANGELOG.md` (`[Unreleased]` › Added), `CLAUDE.md` (the `criteria_tokens.py` row; Testing list), `TODO.md` (B sub-items)

**Interfaces:**
- Consumes: `owned_definitions`, `select_tests` (Task 1, slice A).
- Produces: the fixture directory devtools vendors (`PIN` + their `manifest.json`).

- [ ] **Step 1: Protect the bytes before writing them**

Append to `.gitattributes`:

```
# criteria-closure/v1 shared ownership fixtures (#603 design §6.3): the bytes
# ARE the cases (CRLF, lone CR, BOM, NUL, form feed) — never normalise them.
tests/fixtures/criteria-closure/** -text
```

In `pyproject.toml`, under `[tool.ruff]` (after `target-version`):

```toml
# Shared ownership fixtures: deliberate syntax errors, NUL and CR-only line
# endings — data for criteria_tokens, not code to lint or format.
extend-exclude = ["tests/fixtures/criteria-closure"]
```

- [ ] **Step 2: Write the contract test first**

Create `tests/test_ownership_fixtures.py`:

```python
"""#603 B1: the shared token-ownership fixtures (design §6.3, devtools#491).

devtools vendors `tests/fixtures/criteria-closure/v1/ownership/` under PIN and
runs its own parser against the same files; this test is our half of parity.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from spec_runner.criteria_tokens import owned_definitions, select_tests

ROOT = Path(__file__).parent / "fixtures" / "criteria-closure" / "v1" / "ownership"
CASES = sorted(p for p in ROOT.glob("*.py"))


def _expected(case: Path) -> dict:
    return json.loads(case.with_suffix(".expected.json").read_text(encoding="utf-8"))


def test_the_agreed_case_set_is_present():
    assert [c.stem for c in CASES] == [
        "01_form_feed",
        "02_redefined_class",
        "03_conditional_definition",
        "04_nested_class_outer_token",
        "05_bom",
        "06_test_method_of_non_test_class",
        "07_test_nested_in_test_function",
        "08_decorator_line",
        "09_nested_helper",
        "10_module_header",
        "11_class_docstring",
        "12_async_def",
        "13_parametrize",
        "14_crlf",
        "15_lone_cr",
        "16_syntax_error",
        "17_nul",
        "18_if_else_same_name",
        "19_module_helper_with_token",
    ]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.stem)
def test_owned_matches_expected(case: Path):
    expected = _expected(case)
    assert expected["case"] == case.stem
    source = case.read_bytes().decode("utf-8")
    if "error" in expected:
        assert expected == {"case": case.stem, "error": "syntax"}
        with pytest.raises(Exception) as raised:
            owned_definitions(source)
        assert isinstance(raised.value, SyntaxError | ValueError)
        return
    owned = [
        {"qualname": d.qualname, "line": d.line, "tokens": list(d.tokens)}
        for d in owned_definitions(source)
    ]
    assert owned == expected["owned"]
    selected = [d.qualname for d in select_tests(source)]
    assert selected == expected["selection"]["file_only"]


class TestBytesAreTheCases:  # Review Focus 1
    @pytest.mark.parametrize(
        ("stem", "check"),
        [
            ("01_form_feed", lambda b: b"\x0c" in b),
            ("05_bom", lambda b: b.startswith(b"\xef\xbb\xbf")),
            ("14_crlf", lambda b: b.count(b"\r\n") == 6 and b.count(b"\n") == 6),
            ("15_lone_cr", lambda b: b"\n" not in b and b.count(b"\r") == 6),
            ("17_nul", lambda b: b"\x00" in b),
        ],
    )
    def test_special_bytes_survive(self, stem, check):
        assert check((ROOT / f"{stem}.py").read_bytes()), stem

    def test_git_does_not_normalise_them(self):
        out = subprocess.run(
            ["git", "check-attr", "text", "--", str(ROOT / "14_crlf.py")],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert out.strip().endswith(": text: unset"), out


def test_no_fixture_is_a_pytest_module():  # Review Focus 4
    assert not [c for c in CASES if c.name.startswith("test_") or c.stem.endswith("_test")]
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/test_ownership_fixtures.py -q`
Expected: FAIL — `test_the_agreed_case_set_is_present` (empty directory), the byte checks (`FileNotFoundError`), `test_git_does_not_normalise_them` passes only once Step 1 is in place.

- [ ] **Step 4: Write the case files, byte-exact**

Run once (the bytes are the contract; this snippet is not committed):

```bash
uv run python - <<'EOF'
from pathlib import Path
root = Path("tests/fixtures/criteria-closure/v1/ownership")
root.mkdir(parents=True, exist_ok=True)
CASES = {
    "01_form_feed": b"# page\x0cbreak\ndef test_a():\n    '''ENC:BEH-01'''\n",
    "02_redefined_class": b"class TestA:\n    def test_old(self):\n        '''ENC:BEH-01'''\n\n\nclass TestA:\n    def test_new(self):\n        '''ENC:BEH-02'''\n",
    "03_conditional_definition": b"import sys\n\nif sys.platform:\n    def test_a():\n        '''ENC:BEH-01'''\n\ntry:\n    import json\nexcept ImportError:  # ENC:BEH-02\n    pass\nelse:\n    def test_b():\n        '''ENC:BEH-03'''\n",
    "04_nested_class_outer_token": b"class TestOuter:\n    '''ENC:BEH-01'''\n\n    class TestInner:\n        def test_c(self):\n            '''ENC:BEH-02'''\n",
    "05_bom": b"\xef\xbb\xbfdef test_a():\n    '''ENC:BEH-01'''\n",
    "06_test_method_of_non_test_class": b"class Helper:\n    def test_d(self):\n        '''ENC:BEH-01'''\n",
    "07_test_nested_in_test_function": b"def test_x():\n    '''ENC:BEH-01'''\n\n    def test_inner():\n        '''ENC:BEH-02'''\n\n    assert test_inner\n",
    "08_decorator_line": b"import pytest\n\n\n@pytest.mark.slow  # ENC:BEH-01\ndef test_a():\n    pass\n",
    "09_nested_helper": b"def test_a():\n    def helper():\n        '''ENC:BEH-01'''\n\n    '''ENC:BEH-02'''\n    assert helper\n",
    "10_module_header": b"'''Module header ENC:BEH-01'''\n\nNOTE = 'ENC:BEH-02'\n\n\ndef test_a():\n    pass\n",
    "11_class_docstring": b"class TestK:\n    '''ENC:BEH-01'''\n\n    def test_m(self):\n        '''ENC:BEH-02'''\n\n    def test_n(self):\n        pass\n",
    "12_async_def": b"async def test_a():\n    '''ENC:BEH-01'''\n",
    "13_parametrize": b"import pytest\n\n\n@pytest.mark.parametrize('x', ['ENC:BEH-01', 'b'])\ndef test_p(x):\n    '''ENC:BEH-02'''\n",
    "14_crlf": b"def test_a():\r\n    '''ENC:BEH-01'''\r\n\r\n\r\ndef test_b():\r\n    '''ENC:BEH-02'''\r\n",
    "15_lone_cr": b"def test_a():\r    '''ENC:BEH-01'''\r\r\rdef test_b():\r    '''ENC:BEH-02'''\r",
    "16_syntax_error": b"def test_a(:\n    '''ENC:BEH-01'''\n",
    "17_nul": b"def test_a():\n    '''ENC:BEH-01\x00'''\n",
    "18_if_else_same_name": b"import sys\n\nif sys.platform == 'win32':\n    def test_a():\n        '''ENC:BEH-01'''\nelse:\n    def test_a():\n        '''ENC:BEH-02'''\n",
    "19_module_helper_with_token": b"def make_x():  # ENC:BEH-01\n    return 1\n\n\ndef test_a():\n    assert make_x()\n",
}
for stem, data in CASES.items():
    (root / f"{stem}.py").write_bytes(data)
EOF
```

- [ ] **Step 5: Write the expectations by hand** — one file per case, `<stem>.expected.json`, exactly (UTF-8, LF, two-space indent, trailing newline). Each was checked against the §2.2 rules, not generated:

```json
{"case": "01_form_feed", "owned": [{"qualname": "test_a", "line": 2, "tokens": ["ENC:BEH-01"]}], "selection": {"file_only": ["test_a"]}}
{"case": "02_redefined_class", "owned": [{"qualname": "TestA.test_new", "line": 7, "tokens": ["ENC:BEH-02"]}], "selection": {"file_only": ["TestA.test_new"]}}
{"case": "03_conditional_definition", "owned": [{"qualname": "test_a", "line": 4, "tokens": ["ENC:BEH-01"]}, {"qualname": "test_b", "line": 12, "tokens": ["ENC:BEH-03"]}], "selection": {"file_only": ["test_a", "test_b"]}}
{"case": "04_nested_class_outer_token", "owned": [{"qualname": "TestOuter.TestInner.test_c", "line": 5, "tokens": ["ENC:BEH-01", "ENC:BEH-02"]}], "selection": {"file_only": ["TestOuter.TestInner.test_c"]}}
{"case": "05_bom", "owned": [{"qualname": "test_a", "line": 1, "tokens": ["ENC:BEH-01"]}], "selection": {"file_only": ["test_a"]}}
{"case": "06_test_method_of_non_test_class", "owned": [{"qualname": "Helper.test_d", "line": 2, "tokens": ["ENC:BEH-01"]}], "selection": {"file_only": []}}
{"case": "07_test_nested_in_test_function", "owned": [{"qualname": "test_x", "line": 1, "tokens": ["ENC:BEH-01"]}], "selection": {"file_only": ["test_x"]}}
{"case": "08_decorator_line", "owned": [{"qualname": "test_a", "line": 4, "tokens": ["ENC:BEH-01"]}], "selection": {"file_only": ["test_a"]}}
{"case": "09_nested_helper", "owned": [{"qualname": "test_a", "line": 1, "tokens": ["ENC:BEH-02"]}], "selection": {"file_only": ["test_a"]}}
{"case": "10_module_header", "owned": [{"qualname": "test_a", "line": 6, "tokens": []}], "selection": {"file_only": ["test_a"]}}
{"case": "11_class_docstring", "owned": [{"qualname": "TestK.test_m", "line": 4, "tokens": ["ENC:BEH-01", "ENC:BEH-02"]}, {"qualname": "TestK.test_n", "line": 7, "tokens": ["ENC:BEH-01"]}], "selection": {"file_only": ["TestK.test_m", "TestK.test_n"]}}
{"case": "12_async_def", "owned": [{"qualname": "test_a", "line": 1, "tokens": ["ENC:BEH-01"]}], "selection": {"file_only": ["test_a"]}}
{"case": "13_parametrize", "owned": [{"qualname": "test_p", "line": 4, "tokens": ["ENC:BEH-01", "ENC:BEH-02"]}], "selection": {"file_only": ["test_p"]}}
{"case": "14_crlf", "owned": [{"qualname": "test_a", "line": 1, "tokens": ["ENC:BEH-01"]}, {"qualname": "test_b", "line": 5, "tokens": ["ENC:BEH-02"]}], "selection": {"file_only": ["test_a", "test_b"]}}
{"case": "15_lone_cr", "owned": [{"qualname": "test_a", "line": 1, "tokens": ["ENC:BEH-01"]}, {"qualname": "test_b", "line": 5, "tokens": ["ENC:BEH-02"]}], "selection": {"file_only": ["test_a", "test_b"]}}
{"case": "16_syntax_error", "error": "syntax"}
{"case": "17_nul", "error": "syntax"}
{"case": "18_if_else_same_name", "owned": [{"qualname": "test_a", "line": 7, "tokens": ["ENC:BEH-02"]}], "selection": {"file_only": ["test_a"]}}
{"case": "19_module_helper_with_token", "owned": [{"qualname": "make_x", "line": 1, "tokens": ["ENC:BEH-01"]}, {"qualname": "test_a", "line": 5, "tokens": []}], "selection": {"file_only": ["test_a"]}}
```

Write each line to its own file pretty-printed:

```bash
uv run python - <<'EOF'
import json
from pathlib import Path
root = Path("tests/fixtures/criteria-closure/v1/ownership")
LINES = Path(".superpowers/b1-expected.jsonl").read_text().splitlines()
for line in LINES:
    doc = json.loads(line)
    (root / f"{doc['case']}.expected.json").write_text(json.dumps(doc, indent=2) + "\n")
EOF
```

(Save the 19 JSON lines above to `.superpowers/b1-expected.jsonl` first; `.superpowers/` is git-ignored.)

Create `tests/fixtures/criteria-closure/v1/ownership/README.md`:

```markdown
# criteria-closure/v1 — shared token-ownership fixtures

Produced by spec-runner, vendored by devtools under `PIN` with its
`manifest.json` (spec-runner#603 design §6.3, devtools#491). Both sides run
their own parser against every `<case>.py` and must produce `<case>.expected.json`.

- `owned`: every function and method the index holds — including non-`test*`
  ones; no classes; no `def` nested in a function — sorted by `(line, qualname)`.
  `line` is the first decorator's, else the `def`'s. `tokens` are the qualified
  tokens (`CODE:ID`, whole-token boundaries) on the definition's own region and
  every enclosing class's region.
- `{"error": "syntax"}`: the file does not parse.
- `selection.file_only`: spec-runner's task-gate approximation of default pytest
  collection; devtools does not read it.

The bytes are the cases (CRLF, lone CR, BOM, NUL, form feed): `-text` in
`.gitattributes`, excluded from ruff. Never re-save these files in an editor.
```

- [ ] **Step 6: Run the contract test**

Run: `uv run pytest tests/test_ownership_fixtures.py -q`
Expected: PASS (all). A mismatch means the expectation or the code is wrong — re-check the case against §2.2 by hand; never regenerate the JSON from the code.

- [ ] **Step 7: Docs and TODO**

`CHANGELOG.md`, under `## [Unreleased]` › `### Added` (append):

```markdown
- **Shared token-ownership fixtures** (#603, design §6.3):
  `tests/fixtures/criteria-closure/v1/ownership/` — 19 byte-exact cases with
  hand-checked expectations, agreed with devtools in devtools#491, and
  `criteria_tokens.owned_definitions` — every function and method with the
  qualified tokens it owns. devtools vendors them for parser parity before
  `verify --criteria` (slice B2).
```

`CLAUDE.md`: in the `criteria_tokens.py` row, after `carried_ids`, add `` `owned_definitions` (every indexed function/method → `line` + qualified tokens; the shared-fixture view, #603 §6.3), `QUALIFIED_TOKEN` ``; in the Testing paragraph, after the `test_scenario_ownership.py` entry, add `` `test_ownership_fixtures.py` (#603 B1: the 19 shared cases' `owned`/`selection`, the special bytes surviving, `-text` in effect, no fixture collectable by pytest), ``.

`TODO.md`, in the `criteria-closure-verify` item, replace the slice B sub-item with:

```markdown
      - [x] slice B1 — shared ownership fixtures + `owned_definitions`
            (plan `docs/superpowers/plans/2026-09-29-criteria-closure-b1-ownership-fixtures.md`)
      - [ ] slice B2 — `verify --criteria` + schemas `criteria-closure/v1`; release = X
            Контракт согласован в devtools#491 (дизайн rev 3, #614).
            Предусловие: devtools сверил свой парсер с фикстурами B1.
            Релиз A (minor) минимальную версию `criteria-closure/v1` НЕ задаёт.
```

(the old "Предусловие (решение владельца 2026-09-29) …" lines of the B sub-item go; the parity prerequisite now names the concrete fixtures.)

- [ ] **Step 8: Full verification**

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src
uv run python scripts/check_changelog_links.py
uv run pytest tests/ -q -m "not slow"
git check-attr text -- tests/fixtures/criteria-closure/v1/ownership/14_crlf.py
```

Expected: clean; all tests pass; `text: unset`.

- [ ] **Step 9: Commit, PR, notify devtools**

```bash
git add .gitattributes pyproject.toml tests/fixtures/criteria-closure tests/test_ownership_fixtures.py CHANGELOG.md CLAUDE.md TODO.md
git commit -m "feat(#603): B1 — shared token-ownership fixtures (devtools#491)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

Then the repo's gate (`sh scripts/review/local-claude.sh` one round → push → PR →
`sh ../devtools/review-pr.sh spec-runner <pr>` → merge on approve with green
checks via `GH_CONFIG_DIR=~/.config/review gh pr merge --match-head-commit`),
and a comment in devtools#491 with the merge SHA and the fixture path, as
promised there.
