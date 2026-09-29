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
