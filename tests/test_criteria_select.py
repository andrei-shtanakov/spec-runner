"""#603 B2a: BEH selectors from collected items, and the §6.1 digest."""

from __future__ import annotations

import hashlib
import json

import pytest

from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_inventory import Excluded, TestItem
from spec_runner.criteria_select import content_sha256, digest_paths, select

SOURCE = (
    "import pytest\n\n\n"
    "@pytest.mark.parametrize('x', [1, 2])\n"
    "def test_p(x):\n"
    '    """ENC:BEH-01"""\n\n\n'
    "class TestK:\n"
    '    """ENC:BEH-02"""\n\n'
    "    def test_m(self):\n"
    "        pass\n"
)
BLOBS = {"tests/test_a.py": SOURCE.encode()}


def _item(node: str, qualname: str, line: int, file: str = "tests/test_a.py") -> TestItem:
    return TestItem(node, file, qualname, line)


ITEMS = [
    _item("tests/test_a.py::test_p[1]", "test_p", 4),
    _item("tests/test_a.py::test_p[2]", "test_p", 4),
    _item("tests/test_a.py::TestK::test_m", "TestK.test_m", 12),
]


class TestSelect:
    def test_every_parametrized_item_and_class_region(self):
        chosen = select(ITEMS, ["ENC:BEH-01", "ENC:BEH-02", "ENC:BEH-03"], BLOBS)
        assert [i.node_id for i in chosen["ENC:BEH-01"]] == [
            "tests/test_a.py::test_p[1]",
            "tests/test_a.py::test_p[2]",
        ]
        assert [i.node_id for i in chosen["ENC:BEH-02"]] == ["tests/test_a.py::TestK::test_m"]
        assert chosen["ENC:BEH-03"] == []

    def test_a_line_the_ast_does_not_hold_is_unresolved(self):
        with pytest.raises(CriteriaError) as raised:
            select([_item("tests/test_a.py::test_p[1]", "test_p", 5)], ["ENC:BEH-01"], BLOBS)
        assert raised.value.kind is ErrorKind.DEFINITION_UNRESOLVED

    def test_an_unparseable_definition_file_is_unresolved(self):
        item = _item("tests/t.py::test_x", "test_x", 1, "tests/t.py")
        with pytest.raises(CriteriaError) as raised:
            select([item], ["ENC:BEH-01"], {"tests/t.py": b"def (:\n"})
        assert raised.value.kind is ErrorKind.DEFINITION_UNRESOLVED

    def test_a_non_utf8_definition_file_is_unresolved(self):
        item = _item("tests/t.py::test_x", "test_x", 1, "tests/t.py")
        with pytest.raises(CriteriaError) as raised:
            select([item], ["ENC:BEH-01"], {"tests/t.py": b"\xff\xfe\x00"})
        assert raised.value.kind is ErrorKind.DEFINITION_UNRESOLVED

    def test_a_file_with_no_blob_is_unresolved(self):
        item = _item("tests/t.py::test_x", "test_x", 1, "tests/t.py")
        with pytest.raises(CriteriaError) as raised:
            select([item], ["ENC:BEH-01"], {})
        assert raised.value.kind is ErrorKind.DEFINITION_UNRESOLVED


def _ex(how: str, path: str | None = None, node_id: str | None = None) -> Excluded:
    return Excluded(how, path, node_id, None, None)


class TestDigestPaths:
    def test_pyproject_is_always_present_and_never_duplicated(self):
        assert digest_paths(["pkg/a.py"], ["tests/t.py"], (), []) == [
            "pkg/a.py",
            "pyproject.toml",
            "tests/t.py",
        ]
        got = digest_paths(["pkg/a.py"], ["pyproject.toml"], (), [])
        assert got.count("pyproject.toml") == 1

    def test_a_skipped_file_is_included(self):
        got = digest_paths([], ["tests/t.py"], [_ex("skipped", "tests/s.py")], ["tests/s.py"])
        assert "tests/s.py" in got

    def test_an_ignored_directory_brings_its_tracked_py_but_not_a_prefix_sibling(self):
        tracked = ["tests/legacy/x.py", "tests/legacy/sub/y.py", "tests/legacy2/x.py"]
        got = digest_paths([], [], [_ex("ignored", "tests/legacy")], tracked)
        assert "tests/legacy/x.py" in got
        assert "tests/legacy/sub/y.py" in got
        assert "tests/legacy2/x.py" not in got

    def test_an_ignored_file_path_equal_matches(self):
        got = digest_paths([], [], [_ex("ignored", "tests/i.py")], ["tests/i.py", "tests/j.py"])
        assert got == ["pyproject.toml", "tests/i.py"]

    def test_deselected_contributes_nothing(self):
        got = digest_paths([], [], [_ex("deselected", node_id="tests/t.py::a")], ["tests/t.py"])
        assert got == ["pyproject.toml"]

    def test_each_path_once_sorted_by_utf8_bytes(self):
        got = digest_paths(["z.py", "é.py"], ["z.py", "B.py"], (), [])
        assert got == ["B.py", "pyproject.toml", "z.py", "é.py"]


class TestDigest:
    def test_matches_the_design_definition(self):
        files = {"pkg/mod.py": b"x = 1\n", "tests/test_a.py": b"def test(): pass\n"}
        expected_obj = {
            "v": 1,
            "product_roots": ["pkg"],
            "lock": "c" * 64,
            "environment": {"groups": ["a", "b"], "extras": ["e"]},
            "files": [[p, hashlib.sha256(files[p]).hexdigest()] for p in sorted(files)],
        }
        expected = hashlib.sha256(
            json.dumps(
                expected_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
        ).hexdigest()
        assert content_sha256(["pkg"], "c" * 64, ("b", "a"), ("e",), files) == expected

    def test_undeclared_groups_are_null_and_differ_from_empty(self):
        files = {"a.py": b""}
        obj = {
            "v": 1,
            "product_roots": [],
            "lock": "c" * 64,
            "environment": {"groups": None, "extras": []},
            "files": [["a.py", hashlib.sha256(b"").hexdigest()]],
        }
        expected = hashlib.sha256(
            json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).hexdigest()
        assert content_sha256([], "c" * 64, None, [], files) == expected
        assert content_sha256([], "c" * 64, (), [], files) != expected

    # Golden vectors: the exact digests devtools must reproduce (design §6.1). Computed
    # once, independently of content_sha256, from the canonical expression
    #   hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
    #                             ensure_ascii=True).encode("ascii")).hexdigest()
    # with
    #   obj = {"v": 1, "product_roots": ["pkg", "tool.py"], "lock": "a" * 64,
    #          "environment": {"groups": <None | [] | ["dev", "test"]>,
    #                          "extras": ["alpha", "zeta"]},
    #          "files": [["pkg/z.py", sha256(b"")],
    #                    ["pkg/\u00fc.py", sha256("x = '\u00fc'\n".encode())],
    #                    ["pyproject.toml", sha256(b'[project]\nname = "p"\n')]]}
    # ("pkg/z.py" < "pkg/ü.py" by UTF-8 bytes: 0x7a < 0xc3; ü is escaped as \u00fc).
    GOLDEN_FILES = {
        "pyproject.toml": b'[project]\nname = "p"\n',
        "pkg/\u00fc.py": "x = '\u00fc'\n".encode(),
        "pkg/z.py": b"",
    }

    @pytest.mark.parametrize(
        ("groups", "digest"),
        [
            (None, "1a4b403fe57573449c53f5c343a6b6da391eb3065a4bec5feffbbc5d475c91a8"),
            ([], "6931968399538f6487ec85a8ee96184e802975e0250d512fdd0b76bca6123a42"),
            (
                ["test", "dev"],
                "50ff65866b463040cc43f5ac835c133f514b31f67120e212b0bbfecbd4af65c5",
            ),
        ],
    )
    def test_golden_vector(self, groups, digest):
        got = content_sha256(
            ["tool.py", "pkg"], "a" * 64, groups, ["zeta", "alpha"], self.GOLDEN_FILES
        )
        assert got == digest

    def test_each_input_moves_the_digest(self):
        files = {"pkg/mod.py": b"x = 1\n"}
        base = content_sha256(["pkg"], "c" * 64, ["g"], ["e"], files)
        assert content_sha256(["pkg", "tool.py"], "c" * 64, ["g"], ["e"], files) != base
        assert content_sha256(["pkg"], "d" * 64, ["g"], ["e"], files) != base
        assert content_sha256(["pkg"], "c" * 64, ["h"], ["e"], files) != base
        assert content_sha256(["pkg"], "c" * 64, ["g"], ["f"], files) != base
        assert content_sha256(["pkg"], "c" * 64, ["g"], ["e"], {"pkg/mod.py": b"x = 2\n"}) != base

    def test_crlf_and_lf_differ(self):
        lf = content_sha256([], "c" * 64, None, [], {"a.py": b"x = 1\n"})
        crlf = content_sha256([], "c" * 64, None, [], {"a.py": b"x = 1\r\n"})
        assert lf != crlf
