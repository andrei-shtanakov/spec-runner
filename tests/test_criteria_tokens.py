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
    'class TestA:\n    def test_old(self):\n        """BEH-01"""\n\n\ndef TestA():\n    pass\n'
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
        with pytest.raises(Exception) as raised:
            select_tests(source)
        assert isinstance(raised.value, SyntaxError | ValueError)


class TestTokenPattern:
    @pytest.mark.parametrize("text", ["ENC:BEH-03", "(ENC:BEH-03)", "ENC:BEH-03,"])
    def test_matches(self, text):
        assert token_pattern("ENC:BEH-03").search(text)

    @pytest.mark.parametrize("text", ["XENC:BEH-03", "ENC:BEH-030", "ENC:BEH-03a", "_ENC:BEH-03"])
    def test_does_not_match(self, text):
        assert not token_pattern("ENC:BEH-03").search(text)
