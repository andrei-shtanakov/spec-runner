"""#603 B2a: criteria-closure/v1 schemas, the error-kind table, request parsing.

Spec: docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md §3.2, §3.5, §4
(rev 4 — confirmed by devtools on 1d69fc3, spec-runner#623).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft7Validator

from spec_runner.criteria_contract import (
    CriteriaError,
    ErrorKind,
    normalise_remote,
    owner_matches,
    parse_request,
)

ROOT = Path(__file__).parent.parent
SCHEMAS = ROOT / "schemas" / "criteria-closure" / "v1"
GOLDEN = Path(__file__).parent / "fixtures" / "criteria-closure" / "v1" / "responses"

REQUEST = {
    "protocol": 1,
    "owner_repo": "devtools",
    "workstream": "ws-20260929",
    "code": "ENC",
    "bundle_pin": "a" * 40,
    "product_sha": "b" * 40,
    "test_criteria": [
        {"id": "ENC:BEH-01", "verify_task": False},
        {"id": "ENC:BEH-02a", "verify_task": True},
    ],
}


def _schema(name: str) -> Draft7Validator:
    schema = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
    Draft7Validator.check_schema(schema)
    return Draft7Validator(schema)


def _golden(name: str) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((GOLDEN / f"{name}.json").read_text())
    return doc


class TestKindTable:
    EXIT_2 = {
        "request-invalid",
        "owner-repo-mismatch",
        "product-sha-absent",
        "clone-failed",
        "environment-sync-failed",
        "unsupported-runtime",
        "collection-config-outside-checkout",
        "collection-failed",
        "timeout",
    }

    def test_the_twenty_one_kinds(self):
        assert len(ErrorKind) == 21

    def test_rev4_kinds_are_blocking(self):
        for kind in (
            ErrorKind.ENVIRONMENT_SELECTION_INVALID,
            ErrorKind.COLLECTION_MUTATED_CHECKOUT,
        ):
            assert not kind.retryable
            assert kind.exit_code == 3

    @pytest.mark.parametrize("kind", list(ErrorKind), ids=lambda k: k.value)
    def test_retryable_and_exit_follow_the_table(self, kind):
        assert kind.retryable is (kind.value in self.EXIT_2)
        assert kind.exit_code == (2 if kind.value in self.EXIT_2 else 3)

    def test_schema_enum_is_the_python_enum(self):
        schema = json.loads((SCHEMAS / "response.schema.json").read_text())
        assert set(schema["definitions"]["error_kind"]["enum"]) == {k.value for k in ErrorKind}
        assert set(schema["definitions"]["retryable_kinds"]["enum"]) == self.EXIT_2


class TestRequest:
    def test_valid_request_parses_and_keeps_the_raw_echo(self):
        request = parse_request(copy.deepcopy(REQUEST))
        assert request.raw == REQUEST
        assert request.beh_ids == ("ENC:BEH-01", "ENC:BEH-02a")
        assert request.code == "ENC" and request.product_sha == "b" * 40
        assert not list(_schema("request").iter_errors(REQUEST))

    def test_bundle_pin_is_echoed_verbatim(self):
        assert parse_request(copy.deepcopy(REQUEST)).raw["bundle_pin"] == "a" * 40

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            (("protocol",), 2),
            (("protocol",), True),
            (("code",), "enc"),
            (("code",), "ENCODES"),
            (("bundle_pin",), "a" * 39),
            (("product_sha",), "B" * 40),
            (("owner_repo",), "a/b/c"),
            (("workstream",), ""),
            (("test_criteria", 0, "id"), "ENC:AC-01"),  # only BEH ids are test criteria
            (("test_criteria", 0, "verify_task"), "no"),
        ],
    )
    def test_invalid_field_is_request_invalid_and_schema_invalid(self, path, value):
        data = copy.deepcopy(REQUEST)
        target: Any = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(CriteriaError) as raised:
            parse_request(data)
        assert raised.value.kind is ErrorKind.REQUEST_INVALID
        assert list(_schema("request").iter_errors(data)), path

    def test_extra_key_and_exact_duplicate_refused(self):
        extra = {**copy.deepcopy(REQUEST), "extra": 1}
        dup = copy.deepcopy(REQUEST)
        dup["test_criteria"].append({"id": "ENC:BEH-01", "verify_task": False})
        for data in (extra, dup):
            with pytest.raises(CriteriaError):
                parse_request(data)
            assert list(_schema("request").iter_errors(data))

    def test_not_an_object(self):
        with pytest.raises(CriteriaError) as raised:
            parse_request(["not", "an", "object"])
        assert raised.value.kind is ErrorKind.REQUEST_INVALID


class TestSemanticRules:
    """Rules the schema cannot express — parse_request enforces them, the schema accepts."""

    def test_foreign_code_is_schema_valid_but_refused(self):
        data = copy.deepcopy(REQUEST)
        data["test_criteria"][0]["id"] = "XYZ:BEH-01"
        assert not list(_schema("request").iter_errors(data))
        with pytest.raises(CriteriaError) as raised:
            parse_request(data)
        assert raised.value.kind is ErrorKind.REQUEST_INVALID

    def test_same_id_different_verify_task_is_schema_valid_but_refused(self):
        data = copy.deepcopy(REQUEST)
        data["test_criteria"].append({"id": "ENC:BEH-01", "verify_task": True})
        assert not list(_schema("request").iter_errors(data))
        with pytest.raises(CriteriaError) as raised:
            parse_request(data)
        assert raised.value.kind is ErrorKind.REQUEST_INVALID


class TestOwner:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("git@github.com:andrei-shtanakov/devtools.git", ("andrei-shtanakov", "devtools")),
            ("https://github.com/andrei-shtanakov/devtools", ("andrei-shtanakov", "devtools")),
            ("https://github.com/Andrei-Shtanakov/DevTools.git/", ("andrei-shtanakov", "devtools")),
            (
                "ssh://git@github.com/andrei-shtanakov/devtools.git",
                ("andrei-shtanakov", "devtools"),
            ),
            ("/local/path/devtools", None),
        ],
    )
    def test_normalise_remote(self, url, expected):
        assert normalise_remote(url) == expected

    def test_full_name_checks_the_owner(self):
        origin = "git@github.com:andrei-shtanakov/devtools.git"
        assert owner_matches("andrei-shtanakov/devtools", origin)
        assert not owner_matches("other/devtools", origin)
        assert not owner_matches("spec-runner", origin)
        assert not owner_matches("devtools", "/local/path/devtools")

    def test_bare_name_checks_no_owner(self):
        """The named boundary (design §4): a bare name matches that name under any owner."""
        assert owner_matches("devtools", "git@github.com:andrei-shtanakov/devtools.git")
        assert owner_matches("devtools", "git@github.com:someone-else/devtools.git")


class TestResponseSchema:
    @pytest.mark.parametrize(
        "name", ["answer", "error-retryable", "error-blocked", "not-applicable"]
    )
    def test_golden_responses_validate(self, name):
        assert not list(_schema("response").iter_errors(_golden(name))), name

    def test_retryable_contradicting_kind_is_invalid(self):
        doc = _golden("error-blocked")
        doc["error"]["retryable"] = True
        assert list(_schema("response").iter_errors(doc))

    @pytest.mark.parametrize(
        "kind", ["environment-selection-invalid", "collection-mutated-checkout"]
    )
    def test_rev4_kinds_validate_only_as_blocking(self, kind):
        doc = _golden("error-blocked")
        doc["error"]["kind"] = kind
        assert not list(_schema("response").iter_errors(doc))
        doc["error"]["retryable"] = True
        assert list(_schema("response").iter_errors(doc))

    def test_unknown_kind_is_invalid(self):
        doc = _golden("error-blocked")
        doc["error"]["kind"] = "something-new"
        assert list(_schema("response").iter_errors(doc))

    def test_child_process_field_is_invalid(self):
        doc = _golden("answer")
        doc["beh"][0]["selectors"][0]["runs"][0]["child_process"] = False
        assert list(_schema("response").iter_errors(doc))

    def test_one_run_is_invalid(self):
        doc = _golden("answer")
        doc["beh"][0]["selectors"][0]["runs"].pop()
        assert list(_schema("response").iter_errors(doc))

    def test_traced_with_reason_and_unconfirmed_without_are_invalid(self):
        doc = _golden("answer")
        doc["beh"][0]["reason"] = "no-test"
        assert list(_schema("response").iter_errors(doc))
        doc = _golden("answer")
        doc["beh"][1].pop("reason")
        assert list(_schema("response").iter_errors(doc))

    def test_error_with_beh_is_invalid(self):
        doc = _golden("error-blocked")
        doc["beh"] = []
        assert list(_schema("response").iter_errors(doc))

    def test_skipped_teardown_validates(self):
        doc = _golden("answer")
        run = doc["beh"][0]["selectors"][0]["runs"][0]
        run["phases"]["teardown"] = "skipped"
        run["outcome"] = "skipped"
        assert not list(_schema("response").iter_errors(doc))


class TestEnvironmentSelection:
    """Design §3.3: groups null (undeclared) vs a list; extras a list; names normalised."""

    @pytest.mark.parametrize(
        ("groups", "extras"),
        [(None, []), ([], []), (["gov-x", "test"], ["cli"])],
    )
    def test_valid_selections(self, groups, extras):
        doc = _golden("answer")
        doc["environment"]["groups"] = groups
        doc["environment"]["extras"] = extras
        assert not list(_schema("response").iter_errors(doc))

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("groups", ["Gov_X"]),  # not normalised
            ("groups", ["gov.x"]),
            ("groups", ["a", "a"]),
            ("extras", None),  # only groups may be null
            ("extras", ["-cli"]),
        ],
    )
    def test_invalid_selections(self, field, value):
        doc = _golden("answer")
        doc["environment"][field] = value
        assert list(_schema("response").iter_errors(doc))

    @pytest.mark.parametrize("field", ["groups", "extras"])
    def test_selection_is_required(self, field):
        doc = _golden("answer")
        del doc["environment"][field]
        assert list(_schema("response").iter_errors(doc))


class TestCollectionExcluded:
    """Design §3.5/§4: what collection left out, in three forms."""

    DEFINITION = {"file": "tests/test_mod.py", "qualname": "test_slow", "line": 9}

    def test_required_in_the_answer(self):
        doc = _golden("answer")
        del doc["collection_excluded"]
        assert list(_schema("response").iter_errors(doc))

    def test_optional_in_the_error_branch(self):
        doc = _golden("error-blocked")
        assert "collection_excluded" not in doc
        doc["collection_excluded"] = [{"how": "ignored", "path": "tests/legacy"}]
        assert not list(_schema("response").iter_errors(doc))

    @pytest.mark.parametrize(
        "entry",
        [
            {"how": "skipped", "path": "tests/test_opt.py", "reason": "could not import 'x'"},
            {"how": "ignored", "path": "tests/legacy"},
            {
                "how": "deselected",
                "node_id": "tests/test_mod.py::test_slow[1]",
                "definition": DEFINITION,
            },
            {"how": "deselected", "node_id": "tests/doc.txt::doc", "definition": None},
        ],
    )
    def test_each_form_validates(self, entry):
        doc = _golden("answer")
        doc["collection_excluded"] = [entry]
        assert not list(_schema("response").iter_errors(doc))

    @pytest.mark.parametrize(
        "entry",
        [
            {"how": "skipped", "path": "tests/test_opt.py"},  # reason required
            {"how": "ignored", "path": "tests/legacy", "reason": "x"},
            {"how": "deselected", "node_id": "tests/test_mod.py::test_slow"},  # definition
            {"how": "deselected", "path": "tests/test_mod.py", "definition": None},
            {"how": "missing", "path": "tests/x.py"},
            {"how": "ignored", "path": "/abs/tests"},
        ],
    )
    def test_malformed_entries_are_invalid(self, entry):
        doc = _golden("answer")
        doc["collection_excluded"] = [entry]
        assert list(_schema("response").iter_errors(doc))
