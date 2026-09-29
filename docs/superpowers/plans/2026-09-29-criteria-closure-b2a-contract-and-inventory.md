# Criteria closure — B2a (contract, workspace, inventory, selection) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Everything `verify --criteria` needs *before* a test is run: the frozen `criteria-closure/v1` schemas, request parsing and the error-kind table, the fresh clone and locked environment, declared product roots, the collection inventory (the probe's collect mode), selection of BEH selectors and `content_sha256`. B2b adds the probe's run mode, the isolated runs, aggregation and the CLI command.

**Architecture:** Flat modules beside the existing ones, each with one job: `criteria_contract.py` (kinds, errors, request), `criteria_workspace.py` (origin, clone, `uv sync`, interpreter, child env), `criteria_roots.py` (declaration → files), `criteria_probe.py` (stdlib-only pytest plugin, deployed by copy), `criteria_inventory.py` (deploy + run the probe's collect mode, read its manifest), `criteria_select.py` (selectors per BEH, `test_items`, the digest). No CLI surface until B2b — nothing user-visible lands in `master` half-built.

**Tech Stack:** Python ≥ 3.11 (spec-runner), CPython ≥ 3.12 (the product environment), pytest, uv, git, PyYAML, jsonschema (dev only).

**Spec:** `docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md` (rev 3) §3.2–3.5, §4, §6.1; shared ownership fixtures `tests/fixtures/criteria-closure/v1/ownership/` (B1).

**Start condition:** execution starts only after devtools posts «паритет подтверждён» with a SHA in devtools#491 (TODO `criteria-closure-b2` is `@blocked_by:todo://devtools/criteria-closure-v1-parity`). Writing and reviewing this plan does not wait.

## Global Constraints

- `jsonschema` is a **dev** dependency: runtime request validation is hand-written and mirrors `request.schema.json`; the schemas are pinned by contract tests.
- Product environment: `uv sync --locked --all-groups --all-extras`, `UV_PROJECT_ENVIRONMENT` outside the checkout, `VIRTUAL_ENV` and every `PYTEST_*` removed from child environments (Task 1 ruling — measured on devtools: 2886 tests collected without groups, 3173 with all groups).
- The product's pytest is launched as `<env>/bin/python -m pytest …` — the orchestrator's **direct child**, never through `uv run` (the probe's owner check reads the parent PID; under `uv run` the parent is `uv`, measured).
- When the product environment can import `xdist`, every probe invocation appends `-n 0 --dist no` (design §3.6; measured to override `addopts = -n 2`).
- Error kinds, `retryable` and exit codes exactly as design §3.2; nothing outside the enum.
- Paths in every artefact: repository-relative POSIX, no leading `./`.
- Ruff line length 100; mypy strict; commits end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; branch `feat/603-b2a-contract-inventory`.

## The orchestrator ↔ probe interface — `probe/1`

Fixed here for both B2a (collect mode) and B2b (run mode). The probe is `src/spec_runner/criteria_probe.py`, **stdlib-only** (it imports `pytest` only inside hooks, from the product's environment), copied by `criteria_inventory.deploy_probe()` into a fresh temp directory as `_spec_runner_criteria_probe.py`.

**Invocation** (cwd = checkout):

```
<env>/bin/python -m pytest -p _spec_runner_criteria_probe [-n 0 --dist no] --collect-only -q      # collect
<env>/bin/python -m pytest -p _spec_runner_criteria_probe [-n 0 --dist no] -q <node_id>          # run (B2b)
```

**Environment** (on top of the parent's, minus `VIRTUAL_ENV` and `PYTEST_*`):

| variable | value |
|---|---|
| `PYTHONPATH` | the probe directory, prepended |
| `SPEC_RUNNER_PROBE_PARENT` | the orchestrator's PID (`os.getpid()`) |
| `SPEC_RUNNER_PROBE_MODE` | `collect` or `run` |
| `SPEC_RUNNER_PROBE_MANIFEST` | absolute path the owner writes its manifest to (fresh per invocation) |
| `SPEC_RUNNER_PROBE_PRODUCT_FILES` | run mode only: absolute path of a JSON list of absolute product-file paths |
| `TMPDIR` | a fresh directory per invocation |

**Ownership:** at import the probe is the *owner* iff `str(os.getppid()) == SPEC_RUNNER_PROBE_PARENT`; it records `OWNER_PID = os.getpid()`. Every hook and the writer act only while `os.getpid() == OWNER_PID`. A distribution worker (parent: the pytest controller) or a fork (parent: the owner) never records or writes.

**Manifest** — written once by the owner at `pytest_sessionfinish` via a temp file + `os.replace`; `"complete": true` last-written field is what marks it finished. A missing, unparsable or incomplete manifest is never evidence.

Collect mode (`"mode": "collect"`):

```json
{"probe": 1, "mode": "collect", "pid": 123,
 "python": {"implementation": "CPython", "version": "3.12.13"},
 "rootpath": "/abs/checkout", "inipath": "/abs/checkout/pyproject.toml",
 "plugins": ["pytest-9.0.2", "pytest-xdist-3.8.0"],
 "conftests": ["/abs/checkout/tests/conftest.py"],
 "items": [{"node_id": "tests/test_a.py::test_x[1]", "function": true,
            "module": "/abs/checkout/tests/test_a.py",
            "definition": {"file": "/abs/checkout/tests/test_a.py", "qualname": "test_x", "line": 8}}],
 "errors": [{"node_id": "tests/test_b.py", "message": "…"}],
 "exitstatus": 0, "complete": true}
```

`definition` is `null` when the item is a `pytest.Function` whose function cannot be unwrapped to a source file; `function` is `false` for non-function items (doctests, plugin items). `inipath` is `null` without a config file.

Run mode (`"mode": "run"`, B2b) — fixed now so B2b cannot drift:

```json
{"probe": 1, "mode": "run", "pid": 123,
 "collected": ["tests/test_a.py::test_x[1]"],
 "phases": {"setup": "passed", "call": "passed", "teardown": "passed"},
 "call_in_owner": true, "distributed": false,
 "product_lines": {"/abs/checkout/pkg/mod.py": [12, 13]},
 "process_operations": ["subprocess.Popen"],
 "exitstatus": 0, "complete": true}
```

`phases.call` is `not-reached` when setup did not pass; `distributed` is true when the owner received a `call` report for an item whose `pytest_runtest_call` did not run in the owner (design §3.6). Line filtering to function bodies by AST happens in the orchestrator, not the probe.

## Review Focus

1. A collection that pytest reports as failed (import error in a test module) vs. an infrastructure failure (timeout, missing manifest) — `collection-error` (3) vs `collection-failed` (2). Pinned in Task 5.
2. An inherited test whose definition lives in a helper module outside `tests/` — the helper file must join `test_files` and the definition must resolve. Pinned in Task 5/6.
3. A collected item whose `(qualname, line)` the AST at `product_sha` does not hold (e.g. pytest imported the other `if/else` branch) — `definition-unresolved`, never a silent non-selection. Pinned in Task 6.
4. A declared product root that is a tracked symlink, or a directory containing one — `product-roots-invalid`. Pinned in Task 4.
5. `owner_repo` given as bare name vs `owner/name`, origin as SSH vs HTTPS — equal when they name the same repo. Pinned in Task 2.

---

### Task 1: Design rev 4 deltas (owner confirms at plan review)

**Files:** Modify `docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md` (§3.3, §4 request line, §9).

- [ ] **Step 1: Branch**

```bash
git switch master && git pull --ff-only && git switch -c feat/603-b2a-contract-inventory
```

- [ ] **Step 2: Edit the design** — three deltas, each measured or read from devtools' merged code:

1. §3.3 step 4: `uv sync --locked` → `uv sync --locked --all-groups --all-extras`, with the sentence: "every group and extra the lock pins — devtools' tests need its `governance` group (measured at devtools `a78c1ac`: 2886 tests collected with the default groups, 3173 with all)."
2. §4 request: `owner_repo` — "the repository's name, alone or as `owner/name`; compared with the source repo's `origin` by name, and by owner too when given" (devtools sends `state.repo`, a bare name); `bundle_pin` — "a 40-hex commit SHA" (devtools sends `runner._verified_result_sha(state)`).
3. §9: a "Rev 4" line listing the two.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md
git commit -m "docs(#603): design rev 4 — all lock groups; owner_repo forms; bundle_pin is a SHA

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Schemas v1, error kinds, request parsing

**Files:**
- Create: `schemas/criteria-closure/v1/request.schema.json`, `schemas/criteria-closure/v1/response.schema.json`
- Create: `tests/fixtures/criteria-closure/v1/responses/{answer,error-retryable,error-blocked,not-applicable}.json`
- Create: `src/spec_runner/criteria_contract.py`
- Test: `tests/test_criteria_contract.py`

**Interfaces — Produces:**
- `PROTOCOL: int = 1`
- `class ErrorKind(str, Enum)` — the 19 values of design §3.2; properties `retryable: bool`, `exit_code: int`
- `class CriteriaError(Exception)`: `kind: ErrorKind`, `detail: str`, `established: dict[str, object]`
- `@dataclass(frozen=True) class Request`: `raw: dict[str, object]`, `owner_repo: str`, `code: str`, `product_sha: str`, `beh_ids: tuple[str, ...]`
- `parse_request(data: object) -> Request` — raises `CriteriaError(REQUEST_INVALID)`
- `normalise_remote(url: str) -> tuple[str, str] | None` — `(owner, name)`, lower-cased, `.git` stripped
- `owner_matches(requested: str, origin_url: str) -> bool`

- [ ] **Step 1: Write the failing tests** — `tests/test_criteria_contract.py`:

```python
"""#603 B2a: criteria-closure/v1 schemas, the error-kind table, request parsing.

Spec: docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md §3.2, §4
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

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


class TestKindTable:
    EXIT_2 = {
        "request-invalid", "owner-repo-mismatch", "product-sha-absent", "clone-failed",
        "environment-sync-failed", "unsupported-runtime",
        "collection-config-outside-checkout", "collection-failed", "timeout",
    }

    def test_the_nineteen_kinds(self):
        assert len(ErrorKind) == 19

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
            (("test_criteria", 0, "id"), "XYZ:BEH-01"),  # another code
            (("test_criteria", 0, "id"), "ENC:AC-01"),  # only BEH ids are test criteria
            (("test_criteria", 0, "verify_task"), "no"),
        ],
    )
    def test_invalid_field_is_request_invalid_and_schema_invalid(self, path, value):
        data = copy.deepcopy(REQUEST)
        target = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
        with pytest.raises(CriteriaError) as raised:
            parse_request(data)
        assert raised.value.kind is ErrorKind.REQUEST_INVALID
        assert list(_schema("request").iter_errors(data)), path

    def test_extra_key_and_duplicate_id_refused(self):
        extra = {**copy.deepcopy(REQUEST), "extra": 1}
        dup = copy.deepcopy(REQUEST)
        dup["test_criteria"].append({"id": "ENC:BEH-01", "verify_task": False})
        for data in (extra, dup):
            with pytest.raises(CriteriaError):
                parse_request(data)
            assert list(_schema("request").iter_errors(data))

    def test_not_an_object(self):
        with pytest.raises(CriteriaError):
            parse_request(["not", "an", "object"])


class TestOwner:  # Review Focus 5
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("git@github.com:andrei-shtanakov/devtools.git", ("andrei-shtanakov", "devtools")),
            ("https://github.com/andrei-shtanakov/devtools", ("andrei-shtanakov", "devtools")),
            ("https://github.com/Andrei-Shtanakov/DevTools.git/", ("andrei-shtanakov", "devtools")),
            ("ssh://git@github.com/andrei-shtanakov/devtools.git", ("andrei-shtanakov", "devtools")),
            ("/local/path/devtools", None),
        ],
    )
    def test_normalise_remote(self, url, expected):
        assert normalise_remote(url) == expected

    def test_bare_name_and_full_name_match(self):
        origin = "git@github.com:andrei-shtanakov/devtools.git"
        assert owner_matches("devtools", origin)
        assert owner_matches("andrei-shtanakov/devtools", origin)
        assert not owner_matches("other/devtools", origin)
        assert not owner_matches("spec-runner", origin)
        assert not owner_matches("devtools", "/local/path/devtools")


class TestResponseSchema:
    @pytest.mark.parametrize("name", ["answer", "error-retryable", "error-blocked", "not-applicable"])
    def test_golden_responses_validate(self, name):
        doc = json.loads((GOLDEN / f"{name}.json").read_text())
        assert not list(_schema("response").iter_errors(doc)), name

    def _answer(self):
        return json.loads((GOLDEN / "answer.json").read_text())

    def test_retryable_contradicting_kind_is_invalid(self):
        doc = json.loads((GOLDEN / "error-blocked.json").read_text())
        doc["error"]["retryable"] = True
        assert list(_schema("response").iter_errors(doc))

    def test_unknown_kind_is_invalid(self):
        doc = json.loads((GOLDEN / "error-blocked.json").read_text())
        doc["error"]["kind"] = "something-new"
        assert list(_schema("response").iter_errors(doc))

    def test_child_process_field_is_invalid(self):
        doc = self._answer()
        doc["beh"][0]["selectors"][0]["runs"][0]["child_process"] = False
        assert list(_schema("response").iter_errors(doc))

    def test_one_run_is_invalid(self):
        doc = self._answer()
        doc["beh"][0]["selectors"][0]["runs"].pop()
        assert list(_schema("response").iter_errors(doc))

    def test_traced_with_reason_and_unconfirmed_without_are_invalid(self):
        doc = self._answer()
        doc["beh"][0]["reason"] = "no-test"
        assert list(_schema("response").iter_errors(doc))
        doc = self._answer()
        doc["beh"][1].pop("reason")
        assert list(_schema("response").iter_errors(doc))

    def test_error_with_beh_is_invalid(self):
        doc = json.loads((GOLDEN / "error-blocked.json").read_text())
        doc["beh"] = []
        assert list(_schema("response").iter_errors(doc))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_criteria_contract.py -q`
Expected: collection ERROR — `ModuleNotFoundError: spec_runner.criteria_contract`.

- [ ] **Step 3: Write `schemas/criteria-closure/v1/request.schema.json`**

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$id": "https://github.com/andrei-shtanakov/spec-runner/schemas/criteria-closure/v1/request.schema.json",
  "title": "criteria-closure/v1 request",
  "description": "The request devtools sends to `spec-runner verify --criteria --request <file> --json` (spec-runner#603 design §4).",
  "type": "object",
  "additionalProperties": false,
  "required": ["protocol", "owner_repo", "workstream", "code", "bundle_pin", "product_sha", "test_criteria"],
  "properties": {
    "protocol": {"const": 1},
    "owner_repo": {
      "type": "string",
      "pattern": "^([A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+$",
      "description": "The repository's name, alone or as owner/name; compared with the source repo's origin by name, and by owner too when given."
    },
    "workstream": {"type": "string", "minLength": 1},
    "code": {"type": "string", "pattern": "^[A-Z]{2,6}$"},
    "bundle_pin": {"type": "string", "pattern": "^[0-9a-f]{40}$", "description": "The bundle's approval commit SHA; echoed, not used."},
    "product_sha": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
    "test_criteria": {
      "type": "array",
      "uniqueItems": true,
      "items": {
        "type": "object",
        "additionalProperties": false,
        "required": ["id", "verify_task"],
        "properties": {
          "id": {"type": "string", "pattern": "^[A-Z]{2,6}:BEH-[0-9]+[a-z]?$", "description": "A qualified BEH id whose code equals `code`."},
          "verify_task": {"type": "boolean", "description": "Echoed; no effect since norm rev 8."}
        }
      }
    }
  }
}
```

(`uniqueItems` on objects does not catch two entries with the same `id` and different `verify_task`; `parse_request` refuses any repeated `id`, and the id-equals-code rule is enforced there too — JSON Schema cannot express either.)

- [ ] **Step 4: Write `schemas/criteria-closure/v1/response.schema.json`**

```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "$id": "https://github.com/andrei-shtanakov/spec-runner/schemas/criteria-closure/v1/response.schema.json",
  "title": "criteria-closure/v1 response",
  "description": "stdout of `spec-runner verify --criteria … --json` on every exit path (spec-runner#603 design §3.2, §4, §6). Exactly one of three branches; `not_applicable` is reserved and never emitted by v1.",
  "oneOf": [
    {"$ref": "#/definitions/answer"},
    {"$ref": "#/definitions/error_response"},
    {"$ref": "#/definitions/not_applicable_response"}
  ],
  "definitions": {
    "sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
    "relpath": {"type": "string", "minLength": 1, "pattern": "^(?!/)(?!\\./)(?!.*(^|/)\\.\\.(/|$)).+$"},
    "definition": {
      "type": "object",
      "additionalProperties": false,
      "required": ["file", "qualname", "line"],
      "description": "Where pytest's collected function is defined. `line` is the line of the first decorator, else of `def` (co_firstlineno of the unwrapped function) — devtools matches definitions by (file, qualname, line).",
      "properties": {
        "file": {"$ref": "#/definitions/relpath"},
        "qualname": {"type": "string", "minLength": 1},
        "line": {"type": "integer", "minimum": 1}
      }
    },
    "test_item": {
      "type": "object",
      "additionalProperties": false,
      "required": ["node_id", "definition"],
      "properties": {"node_id": {"type": "string", "minLength": 1}, "definition": {"$ref": "#/definitions/definition"}}
    },
    "environment": {
      "type": "object",
      "additionalProperties": false,
      "required": ["lock_sha256", "python", "pytest_plugins"],
      "properties": {
        "lock_sha256": {"$ref": "#/definitions/sha256"},
        "python": {"type": "string", "minLength": 1},
        "pytest_plugins": {"type": "array", "items": {"type": "string"}}
      }
    },
    "product_roots": {
      "type": "object",
      "additionalProperties": false,
      "required": ["declared", "files"],
      "properties": {
        "declared": {"type": "array", "minItems": 1, "uniqueItems": true, "items": {"$ref": "#/definitions/relpath"}},
        "files": {"type": "array", "minItems": 1, "uniqueItems": true, "items": {"$ref": "#/definitions/relpath"}}
      }
    },
    "complete_run": {
      "type": "object",
      "additionalProperties": false,
      "required": ["result", "collected", "phases", "outcome", "product_lines", "product_line_count", "process_operations"],
      "properties": {
        "result": {"const": "complete"},
        "collected": {"type": "array", "items": {"type": "string"}},
        "phases": {
          "type": "object",
          "additionalProperties": false,
          "required": ["setup", "call", "teardown"],
          "properties": {
            "setup": {"enum": ["passed", "failed", "skipped"]},
            "call": {"enum": ["passed", "failed", "skipped", "not-reached"]},
            "teardown": {"enum": ["passed", "failed"]}
          }
        },
        "outcome": {"enum": ["passed", "failed", "skipped"], "description": "Over all three phases: passed only if setup, call and teardown all passed."},
        "product_lines": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["file", "lines"],
            "properties": {
              "file": {"$ref": "#/definitions/relpath"},
              "lines": {"type": "array", "minItems": 1, "uniqueItems": true, "items": {"type": "integer", "minimum": 1}}
            }
          }
        },
        "product_line_count": {"type": "integer", "minimum": 0, "description": "The total length of the `lines` lists."},
        "process_operations": {"type": "array", "items": {"type": "string"}, "description": "Audit events observed during call; non-empty is the norm's child-process indicator (§4.3). An attempt, not proof a child ran the product."}
      }
    },
    "error_run": {
      "type": "object",
      "additionalProperties": false,
      "required": ["result", "reason", "detail"],
      "properties": {
        "result": {"const": "error"},
        "reason": {"enum": ["runner", "io"]},
        "detail": {"type": "string"},
        "exit_status": {"type": "integer"},
        "timed_out": {"type": "boolean"},
        "mutated_paths": {"type": "array", "items": {"$ref": "#/definitions/relpath"}}
      }
    },
    "run": {"oneOf": [{"$ref": "#/definitions/complete_run"}, {"$ref": "#/definitions/error_run"}]},
    "status_reason": {
      "oneOf": [
        {"properties": {"status": {"const": "traced"}}, "not": {"required": ["reason"]}},
        {"properties": {"status": {"const": "unconfirmed"}, "reason": {"enum": ["no-test", "no-product-execution", "subprocess-only", "not-passed", "nondeterministic"]}}, "required": ["reason"]},
        {"properties": {"status": {"const": "error"}, "reason": {"enum": ["io", "runner"]}}, "required": ["reason"]}
      ]
    },
    "selector": {
      "allOf": [
        {"$ref": "#/definitions/status_reason"},
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["node_id", "definition", "status", "runs"],
          "properties": {
            "node_id": {"type": "string", "minLength": 1},
            "definition": {"$ref": "#/definitions/definition"},
            "status": {"enum": ["traced", "unconfirmed", "error"]},
            "reason": {"type": "string"},
            "runs": {"type": "array", "minItems": 2, "maxItems": 2, "items": {"$ref": "#/definitions/run"}}
          }
        }
      ]
    },
    "beh": {
      "allOf": [
        {"$ref": "#/definitions/status_reason"},
        {
          "type": "object",
          "additionalProperties": false,
          "required": ["id", "status", "selectors"],
          "properties": {
            "id": {"type": "string", "pattern": "^[A-Z]{2,6}:BEH-[0-9]+[a-z]?$"},
            "status": {"enum": ["traced", "unconfirmed", "error"]},
            "reason": {"type": "string"},
            "selectors": {"type": "array", "items": {"$ref": "#/definitions/selector"}}
          }
        }
      ]
    },
    "error_kind": {
      "enum": [
        "request-invalid", "owner-repo-mismatch", "product-sha-absent", "clone-failed",
        "environment-sync-failed", "unsupported-runtime", "collection-config-outside-checkout",
        "collection-failed", "timeout", "lock-not-current", "collection-error",
        "product-roots-undeclared", "product-roots-empty", "product-roots-invalid",
        "product-roots-no-python", "product-roots-overlap-tests", "definition-unresolved",
        "selector-absent", "distributed-execution"
      ]
    },
    "retryable_kinds": {
      "enum": [
        "request-invalid", "owner-repo-mismatch", "product-sha-absent", "clone-failed",
        "environment-sync-failed", "unsupported-runtime", "collection-config-outside-checkout",
        "collection-failed", "timeout"
      ]
    },
    "error": {
      "type": "object",
      "additionalProperties": false,
      "required": ["kind", "retryable", "detail"],
      "properties": {
        "kind": {"$ref": "#/definitions/error_kind"},
        "retryable": {"type": "boolean"},
        "detail": {"type": "string"}
      },
      "if": {"properties": {"kind": {"$ref": "#/definitions/retryable_kinds"}}},
      "then": {"properties": {"retryable": {"const": true}}},
      "else": {"properties": {"retryable": {"const": false}}},
      "description": "retryable is true exactly for the exit-2 kinds; exit 3 otherwise (design §3.2)."
    },
    "answer": {
      "type": "object",
      "additionalProperties": false,
      "required": ["protocol", "request", "spec_runner_version", "product_roots", "test_files", "test_items", "environment", "content_sha256", "beh"],
      "properties": {
        "protocol": {"const": 1},
        "request": {"type": "object", "description": "The request, verbatim."},
        "spec_runner_version": {"type": "string", "minLength": 1},
        "product_roots": {"$ref": "#/definitions/product_roots"},
        "test_files": {"type": "array", "uniqueItems": true, "items": {"$ref": "#/definitions/relpath"}},
        "test_items": {"type": "array", "items": {"$ref": "#/definitions/test_item"}},
        "environment": {"$ref": "#/definitions/environment"},
        "content_sha256": {"$ref": "#/definitions/sha256"},
        "beh": {"type": "array", "items": {"$ref": "#/definitions/beh"}}
      }
    },
    "error_response": {
      "type": "object",
      "additionalProperties": false,
      "required": ["protocol", "spec_runner_version", "error"],
      "properties": {
        "protocol": {"const": 1},
        "request": {"type": "object"},
        "spec_runner_version": {"type": "string", "minLength": 1},
        "error": {"$ref": "#/definitions/error"},
        "environment": {"$ref": "#/definitions/environment"},
        "product_roots": {"$ref": "#/definitions/product_roots"},
        "test_files": {"type": "array", "uniqueItems": true, "items": {"$ref": "#/definitions/relpath"}},
        "test_items": {"type": "array", "items": {"$ref": "#/definitions/test_item"}},
        "content_sha256": {"$ref": "#/definitions/sha256"}
      }
    },
    "not_applicable_response": {
      "type": "object",
      "additionalProperties": false,
      "required": ["protocol", "request", "spec_runner_version", "not_applicable"],
      "properties": {
        "protocol": {"const": 1},
        "request": {"type": "object"},
        "spec_runner_version": {"type": "string", "minLength": 1},
        "not_applicable": {
          "type": "object",
          "additionalProperties": false,
          "required": ["reason"],
          "properties": {"reason": {"const": "language"}}
        }
      }
    }
  }
}
```

- [ ] **Step 5: Write the golden responses** under `tests/fixtures/criteria-closure/v1/responses/`:

`answer.json`:

```json
{
  "protocol": 1,
  "request": {"protocol": 1, "owner_repo": "devtools", "workstream": "ws-20260929", "code": "ENC",
              "bundle_pin": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "product_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
              "test_criteria": [{"id": "ENC:BEH-01", "verify_task": false}, {"id": "ENC:BEH-02", "verify_task": false}]},
  "spec_runner_version": "4.5.0",
  "product_roots": {"declared": ["pkg"], "files": ["pkg/__init__.py", "pkg/mod.py"]},
  "test_files": ["pyproject.toml", "tests/conftest.py", "tests/test_mod.py"],
  "test_items": [{"node_id": "tests/test_mod.py::test_run", "definition": {"file": "tests/test_mod.py", "qualname": "test_run", "line": 4}}],
  "environment": {"lock_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "python": "CPython 3.12.13", "pytest_plugins": ["pytest-9.0.2"]},
  "content_sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "beh": [
    {"id": "ENC:BEH-01", "status": "traced", "selectors": [
      {"node_id": "tests/test_mod.py::test_run", "definition": {"file": "tests/test_mod.py", "qualname": "test_run", "line": 4},
       "status": "traced",
       "runs": [
         {"result": "complete", "collected": ["tests/test_mod.py::test_run"], "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}, "outcome": "passed", "product_lines": [{"file": "pkg/mod.py", "lines": [3, 4]}], "product_line_count": 2, "process_operations": []},
         {"result": "complete", "collected": ["tests/test_mod.py::test_run"], "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}, "outcome": "passed", "product_lines": [{"file": "pkg/mod.py", "lines": [3, 4]}], "product_line_count": 2, "process_operations": []}
       ]}
    ]},
    {"id": "ENC:BEH-02", "status": "unconfirmed", "reason": "no-test", "selectors": []}
  ]
}
```

`error-retryable.json`:

```json
{"protocol": 1, "spec_runner_version": "4.5.0",
 "request": {"protocol": 1, "owner_repo": "devtools", "workstream": "ws-20260929", "code": "ENC", "bundle_pin": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "product_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "test_criteria": []},
 "error": {"kind": "product-sha-absent", "retryable": true, "detail": "bbbbbbbbbbbb is not in the local object store — fetch and retry"}}
```

`error-blocked.json`:

```json
{"protocol": 1, "spec_runner_version": "4.5.0",
 "request": {"protocol": 1, "owner_repo": "devtools", "workstream": "ws-20260929", "code": "ENC", "bundle_pin": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "product_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "test_criteria": []},
 "error": {"kind": "product-roots-undeclared", "retryable": false, "detail": "no criteria.product_roots in the spec-runner config at bbbbbbbbbbbb"},
 "environment": {"lock_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc", "python": "CPython 3.12.13", "pytest_plugins": ["pytest-9.0.2"]}}
```

`not-applicable.json`:

```json
{"protocol": 1, "spec_runner_version": "4.5.0",
 "request": {"protocol": 1, "owner_repo": "devtools", "workstream": "ws-20260929", "code": "ENC", "bundle_pin": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "product_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "test_criteria": []},
 "not_applicable": {"reason": "language"}}
```

- [ ] **Step 6: Write `src/spec_runner/criteria_contract.py`**

```python
"""criteria-closure/v1 — error kinds, the kind → retryable → exit table, the request (#603).

The schemas under `schemas/criteria-closure/v1/` are the contract devtools
vendors; `jsonschema` is a dev dependency, so the request is validated here by
hand, mirroring `request.schema.json` (pinned by `tests/test_criteria_contract.py`).

Design: docs/superpowers/specs/2026-09-29-criteria-closure-verify-design.md §3.2, §4
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

PROTOCOL = 1

_OWNER_REPO = re.compile(r"^(?:[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+$")
_CODE = re.compile(r"^[A-Z]{2,6}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_BEH_ID = re.compile(r"^([A-Z]{2,6}):BEH-[0-9]+[a-z]?$")
_REQUEST_KEYS = frozenset(
    {"protocol", "owner_repo", "workstream", "code", "bundle_pin", "product_sha", "test_criteria"}
)
_REMOTE = re.compile(
    r"^(?:[a-z+]+://)?(?:[^@/]+@)?[^:/]+[:/](?P<owner>[^/]+)/(?P<name>[^/]+?)(?:\.git)?/?$"
)


class ErrorKind(str, Enum):
    """Response-level error kinds (design §3.2). The kind fixes retryable and exit."""

    REQUEST_INVALID = "request-invalid"
    OWNER_REPO_MISMATCH = "owner-repo-mismatch"
    PRODUCT_SHA_ABSENT = "product-sha-absent"
    CLONE_FAILED = "clone-failed"
    ENVIRONMENT_SYNC_FAILED = "environment-sync-failed"
    UNSUPPORTED_RUNTIME = "unsupported-runtime"
    COLLECTION_CONFIG_OUTSIDE_CHECKOUT = "collection-config-outside-checkout"
    COLLECTION_FAILED = "collection-failed"
    TIMEOUT = "timeout"
    LOCK_NOT_CURRENT = "lock-not-current"
    COLLECTION_ERROR = "collection-error"
    PRODUCT_ROOTS_UNDECLARED = "product-roots-undeclared"
    PRODUCT_ROOTS_EMPTY = "product-roots-empty"
    PRODUCT_ROOTS_INVALID = "product-roots-invalid"
    PRODUCT_ROOTS_NO_PYTHON = "product-roots-no-python"
    PRODUCT_ROOTS_OVERLAP_TESTS = "product-roots-overlap-tests"
    DEFINITION_UNRESOLVED = "definition-unresolved"
    SELECTOR_ABSENT = "selector-absent"
    DISTRIBUTED_EXECUTION = "distributed-execution"

    @property
    def retryable(self) -> bool:
        """True exactly for the machine- or network-dependent kinds (exit 2)."""
        return self in _RETRYABLE

    @property
    def exit_code(self) -> int:
        """2 for a retryable kind, 3 for a property of the product at product_sha."""
        return 2 if self.retryable else 3


_RETRYABLE = frozenset(
    {
        ErrorKind.REQUEST_INVALID,
        ErrorKind.OWNER_REPO_MISMATCH,
        ErrorKind.PRODUCT_SHA_ABSENT,
        ErrorKind.CLONE_FAILED,
        ErrorKind.ENVIRONMENT_SYNC_FAILED,
        ErrorKind.UNSUPPORTED_RUNTIME,
        ErrorKind.COLLECTION_CONFIG_OUTSIDE_CHECKOUT,
        ErrorKind.COLLECTION_FAILED,
        ErrorKind.TIMEOUT,
    }
)


class CriteriaError(Exception):
    """A response-level failure: its kind, a detail, and the fields established so far."""

    def __init__(self, kind: ErrorKind, detail: str, **established: object) -> None:
        super().__init__(f"{kind.value}: {detail}")
        self.kind = kind
        self.detail = detail
        self.established: dict[str, object] = dict(established)


@dataclass(frozen=True)
class Request:
    """A parsed request; `raw` is echoed verbatim in every response."""

    raw: dict[str, object]
    owner_repo: str
    code: str
    product_sha: str
    beh_ids: tuple[str, ...] = field(default=())


def parse_request(data: object) -> Request:
    """Validate `data` against the v1 request contract, or raise REQUEST_INVALID."""

    def refuse(why: str) -> CriteriaError:
        return CriteriaError(ErrorKind.REQUEST_INVALID, why)

    if not isinstance(data, dict):
        raise refuse("the request is not a JSON object")
    keys = set(data)
    if keys != _REQUEST_KEYS:
        missing, extra = sorted(_REQUEST_KEYS - keys), sorted(keys - _REQUEST_KEYS)
        raise refuse(f"request keys: missing {missing}, unexpected {extra}")
    protocol = data["protocol"]
    if isinstance(protocol, bool) or protocol != PROTOCOL:
        raise refuse(f"protocol must be {PROTOCOL}, got {protocol!r}")
    checks = (
        ("owner_repo", _OWNER_REPO),
        ("code", _CODE),
        ("bundle_pin", _SHA),
        ("product_sha", _SHA),
    )
    for key, pattern in checks:
        value = data[key]
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise refuse(f"{key} {value!r} does not match {pattern.pattern}")
    if not isinstance(data["workstream"], str) or not data["workstream"]:
        raise refuse("workstream must be a non-empty string")
    criteria = data["test_criteria"]
    if not isinstance(criteria, list):
        raise refuse("test_criteria must be a list")
    ids: list[str] = []
    for entry in criteria:
        if not isinstance(entry, dict) or set(entry) != {"id", "verify_task"}:
            raise refuse(f"test_criteria entry {entry!r} is not {{id, verify_task}}")
        match = _BEH_ID.fullmatch(entry["id"]) if isinstance(entry["id"], str) else None
        if match is None or match.group(1) != data["code"]:
            raise refuse(f"test_criteria id {entry['id']!r} is not {data['code']}:BEH-NN")
        if not isinstance(entry["verify_task"], bool):
            raise refuse(f"verify_task of {entry['id']} is not a boolean")
        if entry["id"] in ids:
            raise refuse(f"test_criteria repeats {entry['id']}")
        ids.append(entry["id"])
    return Request(
        raw=data,
        owner_repo=data["owner_repo"],
        code=data["code"],
        product_sha=data["product_sha"],
        beh_ids=tuple(ids),
    )


def normalise_remote(url: str) -> tuple[str, str] | None:
    """`(owner, name)` of a hosted remote URL, lower-cased; None for a local path."""
    if url.startswith(("/", ".", "file:")):
        return None
    match = _REMOTE.match(url.strip())
    if match is None:
        return None
    return match.group("owner").lower(), match.group("name").lower()


def owner_matches(requested: str, origin_url: str) -> bool:
    """Whether `owner_repo` names the repo `origin_url` points at (by name, and owner if given)."""
    remote = normalise_remote(origin_url)
    if remote is None:
        return False
    owner, name = remote
    wanted_owner, _, wanted_name = requested.lower().rpartition("/")
    return wanted_name == name and (not wanted_owner or wanted_owner == owner)
```

- [ ] **Step 7: Run the tests**

Run: `uv run pytest tests/test_criteria_contract.py -q && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: PASS; clean. If a schema-rejection test passes the schema (no errors) — the schema is too loose for that case; tighten it, never relax the test.

- [ ] **Step 8: Commit**

```bash
git add schemas/criteria-closure src/spec_runner/criteria_contract.py tests/test_criteria_contract.py tests/fixtures/criteria-closure/v1/responses
git commit -m "feat(#603): criteria-closure/v1 schemas, error-kind table, request parsing

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The workspace — origin, clone, locked environment, child env

**Files:**
- Create: `src/spec_runner/criteria_workspace.py`
- Test: `tests/test_criteria_workspace.py`

**Interfaces:**
- Consumes: `ErrorKind`, `CriteriaError`, `owner_matches` (Task 2).
- Produces:
  - `check_origin(project_root: Path, owner_repo: str) -> None`
  - `clone_at(project_root: Path, sha: str, into: Path) -> Path` — the checkout
  - `@dataclass(frozen=True) class Environment: python: Path; implementation: str; version: str; lock_sha256: str; has_xdist: bool` with `.label -> str` (`"CPython 3.12.13"`)
  - `sync_environment(checkout: Path, env_dir: Path, timeout: float) -> Environment`
  - `child_env(probe_dir: Path, extra: Mapping[str, str]) -> dict[str, str]`
  - `distribution_args(env: Environment) -> list[str]` — `["-n", "0", "--dist", "no"]` or `[]`

- [ ] **Step 1: Write the failing tests** — `tests/test_criteria_workspace.py`:

```python
"""#603 B2a: origin check, fresh clone at product_sha, locked environment."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_workspace import (
    Environment,
    check_origin,
    child_env,
    clone_at,
    distribution_args,
    sync_environment,
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def source(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.py").write_text("x = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "one")
    return root


class TestOrigin:
    def test_matching_origin_passes(self, source):
        _git(source, "remote", "add", "origin", "git@github.com:andrei-shtanakov/devtools.git")
        check_origin(source, "devtools")
        check_origin(source, "andrei-shtanakov/devtools")

    @pytest.mark.parametrize("requested", ["spec-runner", "other/devtools"])
    def test_mismatch_is_retryable(self, source, requested):
        _git(source, "remote", "add", "origin", "https://github.com/andrei-shtanakov/devtools")
        with pytest.raises(CriteriaError) as raised:
            check_origin(source, requested)
        assert raised.value.kind is ErrorKind.OWNER_REPO_MISMATCH

    def test_no_origin_is_a_mismatch(self, source):
        with pytest.raises(CriteriaError) as raised:
            check_origin(source, "devtools")
        assert raised.value.kind is ErrorKind.OWNER_REPO_MISMATCH


class TestClone:
    def test_clone_resolves_to_exactly_the_sha(self, source, tmp_path):
        first = _git(source, "rev-parse", "HEAD")
        (source / "a.py").write_text("x = 2\n")
        _git(source, "commit", "-qam", "two")
        checkout = clone_at(source, first, tmp_path / "ws")
        assert _git(checkout, "rev-parse", "HEAD") == first
        assert (checkout / "a.py").read_text() == "x = 1\n"
        assert _git(checkout, "status", "--porcelain") == ""

    def test_unknown_sha_is_product_sha_absent(self, source, tmp_path):
        with pytest.raises(CriteriaError) as raised:
            clone_at(source, "f" * 40, tmp_path / "ws")
        assert raised.value.kind is ErrorKind.PRODUCT_SHA_ABSENT


class TestChildEnv:
    def test_pytest_vars_and_virtual_env_removed_probe_first(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PYTEST_ADDOPTS", "-x")
        monkeypatch.setenv("PYTEST_PLUGINS", "evil")
        monkeypatch.setenv("VIRTUAL_ENV", "/somewhere")
        monkeypatch.setenv("PYTHONPATH", "/existing")
        env = child_env(tmp_path, {"SPEC_RUNNER_PROBE_MODE": "collect"})
        assert not [k for k in env if k.startswith("PYTEST_")]
        assert "VIRTUAL_ENV" not in env
        assert env["PYTHONPATH"] == f"{tmp_path}{os.pathsep}/existing"
        assert env["SPEC_RUNNER_PROBE_MODE"] == "collect"

    def test_distribution_args(self):
        base = dict(python=Path("/p"), implementation="CPython", version="3.12.13", lock_sha256="0" * 64)
        assert distribution_args(Environment(**base, has_xdist=True)) == ["-n", "0", "--dist", "no"]
        assert distribution_args(Environment(**base, has_xdist=False)) == []


@pytest.mark.slow
class TestSync:
    """Real `uv sync --locked` on a dependency-free project (offline from the cache)."""

    def _project(self, root: Path, python: str = ">=3.12") -> None:
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "p"\nversion = "0"\nrequires-python = "{python}"\n'
            "dependencies = []\n[tool.uv]\npackage = false\n"
        )

    def test_missing_lock_is_lock_not_current(self, tmp_path):
        self._project(tmp_path)
        with pytest.raises(CriteriaError) as raised:
            sync_environment(tmp_path, tmp_path.parent / "env-missing", timeout=120)
        assert raised.value.kind is ErrorKind.LOCK_NOT_CURRENT

    def test_stale_lock_is_lock_not_current(self, tmp_path):
        # a version bump makes a virtual project's lock stale without any network
        self._project(tmp_path)
        subprocess.run(["uv", "lock", "-q"], cwd=tmp_path, check=True)
        text = (tmp_path / "pyproject.toml").read_text()
        (tmp_path / "pyproject.toml").write_text(text.replace('version = "0"', 'version = "1"'))
        with pytest.raises(CriteriaError) as raised:
            sync_environment(tmp_path, tmp_path.parent / "env-stale", timeout=120)
        assert raised.value.kind is ErrorKind.LOCK_NOT_CURRENT

    def test_locked_sync_reports_the_environment(self, tmp_path):
        self._project(tmp_path)
        subprocess.run(["uv", "lock", "-q"], cwd=tmp_path, check=True)
        env_dir = tmp_path.parent / "env-ok"
        env = sync_environment(tmp_path, env_dir, timeout=300)
        assert env.python.is_relative_to(env_dir) and env.implementation == "CPython"
        assert tuple(int(p) for p in env.version.split(".")[:2]) >= (3, 12)
        assert len(env.lock_sha256) == 64 and not env.has_xdist
        assert not (tmp_path / ".venv").exists()  # the environment lives outside the checkout
        shutil.rmtree(env_dir, ignore_errors=True)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_criteria_workspace.py -q`
Expected: collection ERROR — `ModuleNotFoundError: spec_runner.criteria_workspace`.

- [ ] **Step 3: Implement** `src/spec_runner/criteria_workspace.py`:

```python
"""The measurement's workspace: origin, a fresh clone at product_sha, the locked env (#603).

Design §3.3. The clone comes from the local object store, never the network;
the environment is `uv sync --locked --all-groups --all-extras` into a
directory outside the checkout, so the checkout can be reset between runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from .criteria_contract import CriteriaError, ErrorKind, owner_matches

MIN_PRODUCT_PYTHON = (3, 12)
_INTERPRETER_PROBE = (
    "import json, platform, sys, importlib.util; "
    "print(json.dumps([platform.python_implementation(), platform.python_version(), "
    "importlib.util.find_spec('xdist') is not None]))"
)


@dataclass(frozen=True)
class Environment:
    """The provisioned product environment."""

    python: Path
    implementation: str
    version: str
    lock_sha256: str
    has_xdist: bool

    @property
    def label(self) -> str:
        """`environment.python` in the response, e.g. `CPython 3.12.13`."""
        return f"{self.implementation} {self.version}"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def check_origin(project_root: Path, owner_repo: str) -> None:
    """Refuse (retryable) when the source repo's origin is not the requested repo."""
    shown = _git(project_root, "remote", "get-url", "origin")
    url = shown.stdout.strip() if shown.returncode == 0 else ""
    if not url or not owner_matches(owner_repo, url):
        raise CriteriaError(
            ErrorKind.OWNER_REPO_MISMATCH,
            f"owner_repo {owner_repo!r} is not this repository's origin ({url or 'none'})",
        )


def clone_at(project_root: Path, sha: str, into: Path) -> Path:
    """A fresh clone of `project_root` detached at exactly `sha`."""
    if _git(project_root, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        raise CriteriaError(
            ErrorKind.PRODUCT_SHA_ABSENT,
            f"{sha[:12]} is not in the local object store — fetch and retry",
        )
    checkout = into / "src"
    into.mkdir(parents=True, exist_ok=True)
    steps = (
        (into, ("clone", "-q", "--no-local", "--no-checkout", str(project_root), str(checkout))),
        (checkout, ("checkout", "-q", "--detach", sha)),
    )
    for cwd, args in steps:
        done = _git(cwd, *args)
        if done.returncode != 0:
            raise CriteriaError(ErrorKind.CLONE_FAILED, f"git {args[0]}: {done.stderr.strip()}")
    head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
    if head != sha:
        raise CriteriaError(ErrorKind.CLONE_FAILED, f"checkout resolved to {head}, not {sha}")
    return checkout


def sync_environment(checkout: Path, env_dir: Path, timeout: float) -> Environment:
    """`uv sync --locked --all-groups --all-extras` into `env_dir`, then read the interpreter."""
    lock = checkout / "uv.lock"
    if not lock.is_file():
        raise CriteriaError(ErrorKind.LOCK_NOT_CURRENT, "no uv.lock at product_sha")
    lock_sha256 = hashlib.sha256(lock.read_bytes()).hexdigest()
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    env["UV_PROJECT_ENVIRONMENT"] = str(env_dir)
    # No --quiet: measured (uv 0.11.23) to suppress the "`--locked` was provided"
    # line this classification reads, turning a stale lock into a retryable failure.
    argv = ["uv", "sync", "--locked", "--all-groups", "--all-extras"]
    try:
        done = subprocess.run(
            argv, cwd=checkout, env=env, capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise CriteriaError(ErrorKind.ENVIRONMENT_SYNC_FAILED, "uv sync timed out") from None
    if done.returncode != 0:
        said = done.stderr.strip()
        kind = (
            ErrorKind.LOCK_NOT_CURRENT
            if "`--locked` was provided" in said
            else ErrorKind.ENVIRONMENT_SYNC_FAILED
        )
        raise CriteriaError(kind, f"uv sync --locked: {said}")
    python = env_dir / "bin" / "python"
    probe = subprocess.run(
        [str(python), "-c", _INTERPRETER_PROBE], capture_output=True, text=True, timeout=60
    )
    if probe.returncode != 0:
        raise CriteriaError(ErrorKind.ENVIRONMENT_SYNC_FAILED, probe.stderr.strip())
    implementation, version, has_xdist = json.loads(probe.stdout)
    major_minor = tuple(int(part) for part in version.split(".")[:2])
    if implementation != "CPython" or major_minor < MIN_PRODUCT_PYTHON:
        raise CriteriaError(
            ErrorKind.UNSUPPORTED_RUNTIME,
            f"the product environment is {implementation} {version}; CPython >= 3.12 is required",
        )
    return Environment(python, implementation, version, lock_sha256, bool(has_xdist))


def child_env(probe_dir: Path, extra: Mapping[str, str]) -> dict[str, str]:
    """The environment of a probe invocation: no PYTEST_*, no VIRTUAL_ENV, the probe first."""
    env = {
        k: v for k, v in os.environ.items() if not k.startswith("PYTEST_") and k != "VIRTUAL_ENV"
    }
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{probe_dir}{os.pathsep}{existing}" if existing else str(probe_dir)
    env.update(extra)
    return env


def distribution_args(env: Environment) -> list[str]:
    """Keep every test in the probe's own process when xdist is importable (§3.6)."""
    return ["-n", "0", "--dist", "no"] if env.has_xdist else []
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_criteria_workspace.py -q -m "not slow" && uv run pytest tests/test_criteria_workspace.py -q -m slow && uv run mypy src && uv run ruff check .`
Expected: PASS (the slow class needs `uv` and a CPython ≥ 3.12 uv can find — CI has both).

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/criteria_workspace.py tests/test_criteria_workspace.py
git commit -m "feat(#603): criteria workspace — origin, clone at product_sha, locked env

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Declared product roots

**Files:**
- Create: `src/spec_runner/criteria_roots.py`
- Test: `tests/test_criteria_roots.py`

**Interfaces:**
- Consumes: `CriteriaError`, `ErrorKind` (Task 2).
- Produces:
  - `read_declaration(checkout: Path) -> list[str]` — normalised, sorted
  - `resolve_roots(checkout: Path, sha: str, declared: list[str]) -> list[str]` — tracked regular `.py` files, sorted
  - `check_overlap(files: list[str], test_files: list[str]) -> None`

- [ ] **Step 1: Write the failing tests** — `tests/test_criteria_roots.py`:

```python
"""#603 B2a: product roots are declared in the product's config at product_sha (§3.4)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_roots import check_overlap, read_declaration, resolve_roots


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(tmp_path: Path, files: dict[str, str], config: str | None) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    if config is not None:
        (root / "spec-runner.config.yaml").write_text(config)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "c")
    return root, _git(root, "rev-parse", "HEAD")


FILES = {"pkg/__init__.py": "", "pkg/mod.py": "x = 1\n", "pkg/data.txt": "d", "tool.py": "", "tests/test_a.py": ""}


def _kind(fn, *args) -> ErrorKind:
    with pytest.raises(CriteriaError) as raised:
        fn(*args)
    return raised.value.kind


class TestDeclaration:
    def test_flat_and_executor_shapes(self, tmp_path):
        root, _ = _repo(tmp_path, FILES, "criteria:\n  product_roots: [pkg/, ./tool.py]\n")
        assert read_declaration(root) == ["pkg", "tool.py"]
        (root / "spec-runner.config.yaml").write_text("executor:\n  criteria:\n    product_roots: [pkg]\n")
        assert read_declaration(root) == ["pkg"]

    def test_legacy_location(self, tmp_path):
        root, _ = _repo(tmp_path, {**FILES, "spec/executor.config.yaml": "executor:\n  criteria:\n    product_roots: [pkg]\n"}, None)
        assert read_declaration(root) == ["pkg"]

    @pytest.mark.parametrize(
        ("config", "kind"),
        [
            (None, ErrorKind.PRODUCT_ROOTS_UNDECLARED),
            ("claude_model: sonnet\n", ErrorKind.PRODUCT_ROOTS_UNDECLARED),
            ("criteria:\n  product_roots: []\n", ErrorKind.PRODUCT_ROOTS_EMPTY),
            ("criteria:\n  product_roots: pkg\n", ErrorKind.PRODUCT_ROOTS_INVALID),
            ("criteria:\n  product_roots: [/abs]\n", ErrorKind.PRODUCT_ROOTS_INVALID),
            ("criteria:\n  product_roots: [../up]\n", ErrorKind.PRODUCT_ROOTS_INVALID),
            ("criteria:\n  product_roots: [pkg, ./pkg/]\n", ErrorKind.PRODUCT_ROOTS_INVALID),
            ("criteria:\n  product_roots: ['.']\n", ErrorKind.PRODUCT_ROOTS_INVALID),
        ],
    )
    def test_refusals(self, tmp_path, config, kind):
        root, _ = _repo(tmp_path, FILES, config)
        assert _kind(read_declaration, root) is kind


class TestResolve:
    def test_directory_expands_to_tracked_python_files(self, tmp_path):
        root, sha = _repo(tmp_path, FILES, "")
        (root / "pkg" / "untracked.py").write_text("")
        assert resolve_roots(root, sha, ["pkg", "tool.py"]) == ["pkg/__init__.py", "pkg/mod.py", "tool.py"]

    def test_missing_root_is_invalid(self, tmp_path):
        root, sha = _repo(tmp_path, FILES, "")
        assert _kind(resolve_roots, root, sha, ["nope"]) is ErrorKind.PRODUCT_ROOTS_INVALID

    def test_no_python_under_roots(self, tmp_path):
        root, sha = _repo(tmp_path, {**FILES, "docs/readme.md": "x"}, "")
        assert _kind(resolve_roots, root, sha, ["docs"]) is ErrorKind.PRODUCT_ROOTS_NO_PYTHON

    @pytest.mark.parametrize("declared", ["link", "pkg"])  # Review Focus 4
    def test_tracked_symlink_is_invalid(self, tmp_path, declared):
        root, _ = _repo(tmp_path, FILES, "")
        (root / "link").symlink_to("/etc")
        (root / "pkg" / "escape.py").symlink_to("/etc/hosts")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "links")
        sha = _git(root, "rev-parse", "HEAD")
        assert _kind(resolve_roots, root, sha, [declared]) is ErrorKind.PRODUCT_ROOTS_INVALID


class TestOverlap:
    def test_a_test_file_under_a_root_is_refused(self):
        with pytest.raises(CriteriaError) as raised:
            check_overlap(["pkg/mod.py", "pkg/conftest.py"], ["pkg/conftest.py", "tests/test_a.py"])
        assert raised.value.kind is ErrorKind.PRODUCT_ROOTS_OVERLAP_TESTS
        assert "pkg/conftest.py" in raised.value.detail

    def test_disjoint_passes(self):
        check_overlap(["pkg/mod.py"], ["tests/test_a.py"])
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_criteria_roots.py -q`
Expected: collection ERROR — `ModuleNotFoundError: spec_runner.criteria_roots`.

- [ ] **Step 3: Implement** `src/spec_runner/criteria_roots.py`:

```python
"""Declared product roots, read and resolved at product_sha (#603 design §3.4).

The declaration is the product repo's own: `criteria.product_roots` in its
spec-runner config (flat or under `executor:`). It is a trust boundary — the
overlap check catches known test infrastructure, it cannot prove the rest is
product.
"""

from __future__ import annotations

import subprocess
from pathlib import Path, PurePosixPath

import yaml

from .criteria_contract import CriteriaError, ErrorKind

_CONFIG_LOCATIONS = ("spec-runner.config.yaml", "spec/executor.config.yaml")
_REGULAR_MODES = frozenset({"100644", "100755"})
_SYMLINK_MODE = "120000"


def read_declaration(checkout: Path) -> list[str]:
    """`criteria.product_roots` from the checkout's config, normalised and sorted."""
    for location in _CONFIG_LOCATIONS:
        path = checkout / location
        if path.is_file():
            break
    else:
        raise CriteriaError(ErrorKind.PRODUCT_ROOTS_UNDECLARED, "no spec-runner config at product_sha")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CriteriaError(ErrorKind.PRODUCT_ROOTS_INVALID, f"{location}: {exc}") from None
    section = data.get("executor", data) if isinstance(data, dict) else {}
    criteria = section.get("criteria") if isinstance(section, dict) else None
    if not isinstance(criteria, dict) or "product_roots" not in criteria:
        raise CriteriaError(
            ErrorKind.PRODUCT_ROOTS_UNDECLARED, f"no criteria.product_roots in {location}"
        )
    raw = criteria["product_roots"]
    if not isinstance(raw, list):
        raise CriteriaError(ErrorKind.PRODUCT_ROOTS_INVALID, "criteria.product_roots is not a list")
    if not raw:
        raise CriteriaError(ErrorKind.PRODUCT_ROOTS_EMPTY, "criteria.product_roots is empty")
    normalised: list[str] = []
    for entry in raw:
        root = _normalise(entry)
        if root in normalised:
            raise CriteriaError(ErrorKind.PRODUCT_ROOTS_INVALID, f"{entry!r} is declared twice")
        normalised.append(root)
    return sorted(normalised)


def _normalise(entry: object) -> str:
    if not isinstance(entry, str) or not entry.strip():
        raise CriteriaError(ErrorKind.PRODUCT_ROOTS_INVALID, f"{entry!r} is not a path")
    path = PurePosixPath(entry.strip())
    if path.is_absolute() or ".." in path.parts:
        raise CriteriaError(ErrorKind.PRODUCT_ROOTS_INVALID, f"{entry!r} escapes the checkout")
    parts = [part for part in path.parts if part not in (".", "")]
    if not parts:
        raise CriteriaError(ErrorKind.PRODUCT_ROOTS_INVALID, f"{entry!r} is the repository root")
    return "/".join(parts)


def resolve_roots(checkout: Path, sha: str, declared: list[str]) -> list[str]:
    """Every tracked regular `.py` file under the declared roots at `sha`, sorted."""
    files: set[str] = set()
    for root in declared:
        listed = subprocess.run(
            ["git", "ls-tree", "-r", "-z", sha, "--", root],
            cwd=checkout,
            capture_output=True,
            text=True,
        )
        entries = [e for e in listed.stdout.split("\0") if e] if listed.returncode == 0 else []
        if not entries:
            raise CriteriaError(
                ErrorKind.PRODUCT_ROOTS_INVALID, f"{root} is not tracked at {sha[:12]}"
            )
        for entry in entries:
            meta, path = entry.split("\t", 1)
            mode = meta.split(" ", 1)[0]
            if mode == _SYMLINK_MODE:
                raise CriteriaError(
                    ErrorKind.PRODUCT_ROOTS_INVALID, f"{path} is a symlink — a root may not reach outside"
                )
            if mode in _REGULAR_MODES and path.endswith(".py"):
                files.add(path)
    if not files:
        raise CriteriaError(
            ErrorKind.PRODUCT_ROOTS_NO_PYTHON, f"{', '.join(declared)} hold no Python file"
        )
    return sorted(files)


def check_overlap(files: list[str], test_files: list[str]) -> None:
    """Refuse a product file that is also a collected test module or loaded conftest/config."""
    overlap = sorted(set(files) & set(test_files))
    if overlap:
        raise CriteriaError(
            ErrorKind.PRODUCT_ROOTS_OVERLAP_TESTS,
            f"declared product roots include test files: {', '.join(overlap)}",
        )
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_criteria_roots.py -q && uv run mypy src && uv run ruff check .`
Expected: PASS; clean. (If mypy wants `types-PyYAML`, follow how `config.py` imports yaml — same stub situation.)

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/criteria_roots.py tests/test_criteria_roots.py
git commit -m "feat(#603): declared product roots, resolved at product_sha

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The probe's collect mode and the inventory

**Files:**
- Create: `src/spec_runner/criteria_probe.py` (stdlib-only; collect mode now, run-mode hooks in B2b)
- Create: `src/spec_runner/criteria_inventory.py`
- Test: `tests/test_criteria_inventory.py`

**Interfaces:**
- Consumes: `CriteriaError`, `ErrorKind` (Task 2); `Environment`, `child_env`, `distribution_args` (Task 3).
- Produces:
  - `PROBE_MODULE = "_spec_runner_criteria_probe"`; env-var constants `PARENT_ENV`, `MODE_ENV`, `MANIFEST_ENV`, `PRODUCT_FILES_ENV` (defined once in `criteria_probe.py`, imported by `criteria_inventory.py`)
  - `deploy_probe(into: Path) -> Path` — the probe directory
  - `@dataclass(frozen=True) class TestItem: node_id: str; file: str; qualname: str; line: int`
  - `@dataclass(frozen=True) class Inventory: items: tuple[TestItem, ...]; test_files: tuple[str, ...]; inipath: str | None; plugins: tuple[str, ...]`
  - `collect(env: Environment, checkout: Path, probe_dir: Path, work: Path, timeout: float) -> Inventory`

- [ ] **Step 1: Write the failing tests** — `tests/test_criteria_inventory.py` runs the real probe under **this** interpreter (spec-runner's venv has pytest), bypassing `uv sync`:

```python
"""#603 B2a: the probe's collect mode and the inventory it yields (probe/1)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_inventory import collect, deploy_probe
from spec_runner.criteria_workspace import Environment


def _env(has_xdist: bool = False) -> Environment:
    return Environment(Path(sys.executable), "CPython", "3.12.0", "0" * 64, has_xdist)


def _project(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def _collect(tmp_path: Path, files: dict[str, str]):
    checkout = _project(tmp_path / "checkout", files)
    probe = deploy_probe(tmp_path / "probe")
    return collect(_env(), checkout, probe, tmp_path / "work", timeout=120)


BASE = {
    "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
    "tests/conftest.py": "",
    "tests/helpers.py": "class Base:\n    def test_inherited(self):\n        pass\n",
    "tests/test_a.py": (
        "import pytest\n"
        "from helpers import Base\n\n\n"
        "@pytest.mark.parametrize('x', [1, 2])\n"
        "def test_p(x):\n    pass\n\n\n"
        "class TestSub(Base):\n    pass\n"
    ),
}


class TestInventory:
    def test_items_with_real_definitions_and_test_files(self, tmp_path):
        inv = _collect(tmp_path, {**BASE, "tests/conftest.py": "import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n"})
        by_id = {i.node_id: i for i in inv.items}
        assert set(by_id) == {"tests/test_a.py::test_p[1]", "tests/test_a.py::test_p[2]", "tests/test_a.py::TestSub::test_inherited"}
        assert (by_id["tests/test_a.py::test_p[1]"].qualname, by_id["tests/test_a.py::test_p[1]"].line) == ("test_p", 5)
        inherited = by_id["tests/test_a.py::TestSub::test_inherited"]  # Review Focus 2
        assert (inherited.file, inherited.qualname, inherited.line) == ("tests/helpers.py", "Base.test_inherited", 2)
        assert inv.test_files == ("pyproject.toml", "tests/conftest.py", "tests/helpers.py", "tests/test_a.py")
        assert inv.inipath == "pyproject.toml"
        assert any(p.startswith("pytest-") for p in inv.plugins)

    def test_zero_tests_is_a_valid_empty_inventory(self, tmp_path):
        inv = _collect(tmp_path, {"pyproject.toml": "[tool.pytest.ini_options]\n", "tests/test_none.py": "x = 1\n"})
        assert inv.items == ()

    def test_import_error_in_a_test_module_is_collection_error(self, tmp_path):  # Review Focus 1
        with pytest.raises(CriteriaError) as raised:
            _collect(tmp_path, {**BASE, "tests/test_b.py": "import no_such_module\n"})
        assert raised.value.kind is ErrorKind.COLLECTION_ERROR
        assert "tests/test_b.py" in raised.value.detail

    def test_timeout_is_collection_failed(self, tmp_path):  # Review Focus 1
        checkout = _project(tmp_path / "checkout", {"tests/test_slow.py": "import time\ntime.sleep(30)\n"})
        probe = deploy_probe(tmp_path / "probe")
        with pytest.raises(CriteriaError) as raised:
            collect(_env(), checkout, probe, tmp_path / "work", timeout=2)
        assert raised.value.kind is ErrorKind.COLLECTION_FAILED

    def test_config_outside_the_checkout(self, tmp_path):
        (tmp_path / "pytest.ini").write_text("[pytest]\n")
        checkout = _project(tmp_path / "checkout", {"tests/test_a.py": "def test_x():\n    pass\n"})
        probe = deploy_probe(tmp_path / "probe")
        with pytest.raises(CriteriaError) as raised:
            collect(_env(), checkout, probe, tmp_path / "work", timeout=120)
        assert raised.value.kind is ErrorKind.COLLECTION_CONFIG_OUTSIDE_CHECKOUT


class TestProbeIsStdlibOnly:
    def test_no_spec_runner_import(self):
        source = (Path(__file__).parent.parent / "src" / "spec_runner" / "criteria_probe.py").read_text()
        assert "spec_runner" not in source.split('"""', 2)[2]  # docstring aside
```

(`test_config_outside_the_checkout` relies on pytest's rootdir search walking up to `tmp_path/pytest.ini` because the checkout holds no config of its own — which is exactly the case §3.2's kind names.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_criteria_inventory.py -q`
Expected: collection ERROR — `ModuleNotFoundError: spec_runner.criteria_inventory`.

- [ ] **Step 3: Implement the probe** — `src/spec_runner/criteria_probe.py`:

```python
"""spec-runner criteria-closure probe, protocol probe/1 (#603 design §3.5-3.6).

Loaded into the PRODUCT's pytest with `-p _spec_runner_criteria_probe` from a
temp directory on PYTHONPATH; copied there by `criteria_inventory.deploy_probe`.
Standard library only (pytest comes from the product's environment): the
product need not have spec_runner installed. The interface — invocation,
environment variables, ownership, manifest — is fixed in
docs/superpowers/plans/2026-09-29-criteria-closure-b2a-contract-and-inventory.md
and must change only together with the orchestrator.
"""

from __future__ import annotations

import inspect
import json
import os
import platform
from typing import Any

PARENT_ENV = "SPEC_RUNNER_PROBE_PARENT"
MODE_ENV = "SPEC_RUNNER_PROBE_MODE"
MANIFEST_ENV = "SPEC_RUNNER_PROBE_MANIFEST"
PRODUCT_FILES_ENV = "SPEC_RUNNER_PROBE_PRODUCT_FILES"
PROTOCOL = 1

#: Owner iff the orchestrator is our direct parent; fixed at import.
OWNER_PID: int | None = os.getpid() if str(os.getppid()) == os.environ.get(PARENT_ENV) else None

_manifest: dict[str, Any] = {
    "probe": PROTOCOL,
    "mode": os.environ.get(MODE_ENV, ""),
    "pid": os.getpid(),
    "python": {"implementation": platform.python_implementation(), "version": platform.python_version()},
    "conftests": [],
    "items": [],
    "errors": [],
}


def _owner() -> bool:
    return OWNER_PID is not None and os.getpid() == OWNER_PID


def pytest_configure(config: Any) -> None:
    if not _owner():
        return
    inipath = getattr(config, "inipath", None)
    _manifest["inipath"] = str(inipath) if inipath else None
    _manifest["rootpath"] = str(config.rootpath)
    _manifest["plugins"] = sorted(
        f"{dist.project_name}-{dist.version}" for _, dist in config.pluginmanager.list_plugin_distinfo()
    )


def pytest_plugin_registered(plugin: Any, manager: Any) -> None:
    if not _owner():
        return
    path = getattr(plugin, "__file__", None) or ""
    if os.path.basename(path) == "conftest.py" and path not in _manifest["conftests"]:
        _manifest["conftests"].append(os.path.realpath(path))


def pytest_collectreport(report: Any) -> None:
    if _owner() and report.failed:
        _manifest["errors"].append({"node_id": report.nodeid, "message": str(report.longrepr)[-2000:]})


def pytest_collection_finish(session: Any) -> None:
    if not _owner():
        return
    import pytest

    for item in session.items:
        is_function = isinstance(item, pytest.Function)
        _manifest["items"].append(
            {
                "node_id": item.nodeid,
                "function": is_function,
                "module": os.path.realpath(str(item.path)),
                "definition": _definition(item) if is_function else None,
            }
        )


def _definition(item: Any) -> dict[str, Any] | None:
    try:
        function = inspect.unwrap(item.function)
        code = function.__code__
        source = inspect.getsourcefile(function) or code.co_filename
        return {"file": os.path.realpath(source), "qualname": function.__qualname__, "line": code.co_firstlineno}
    except Exception:  # noqa: BLE001 — an unresolvable definition is reported as null
        return None


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    if not _owner():
        return
    path = os.environ.get(MANIFEST_ENV)
    if not path:
        return
    _manifest["exitstatus"] = int(exitstatus)
    _manifest["complete"] = True
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(_manifest, handle)
    os.replace(tmp, path)
```

- [ ] **Step 4: Implement the inventory** — `src/spec_runner/criteria_inventory.py`:

```python
"""Deploy the probe, run its collect mode, and read the inventory (#603 design §3.5).

pytest's own exit codes decide the error kind: 0 and 5 (no tests) are an
inventory; 1-2 with collection errors reported is a product error at
product_sha (exit 3); anything else — a crash, a usage error, a timeout, no
manifest — is an infrastructure failure (exit 2).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import criteria_probe
from .criteria_contract import CriteriaError, ErrorKind
from .criteria_probe import MANIFEST_ENV, MODE_ENV, PARENT_ENV
from .criteria_workspace import Environment, child_env, distribution_args

PROBE_MODULE = "_spec_runner_criteria_probe"
_INVENTORY_EXIT = frozenset({0, 5})


@dataclass(frozen=True)
class TestItem:
    """A collected `pytest.Function` and where it is defined (repository-relative)."""

    __test__ = False

    node_id: str
    file: str
    qualname: str
    line: int


@dataclass(frozen=True)
class Inventory:
    """What pytest collects at product_sha, as the probe saw it."""

    items: tuple[TestItem, ...]
    test_files: tuple[str, ...]
    inipath: str | None
    plugins: tuple[str, ...]


def deploy_probe(into: Path) -> Path:
    """Copy the probe's source into a fresh directory under `into`, named `PROBE_MODULE`."""
    into.mkdir(parents=True, exist_ok=True)
    probe_dir = Path(tempfile.mkdtemp(prefix="probe-", dir=into))
    source = Path(criteria_probe.__file__).read_text(encoding="utf-8")
    (probe_dir / f"{PROBE_MODULE}.py").write_text(source, encoding="utf-8")
    return probe_dir


def collect(env: Environment, checkout: Path, probe_dir: Path, work: Path, timeout: float) -> Inventory:
    """Run the probe's collect mode in `checkout` and turn its manifest into an Inventory."""
    work.mkdir(parents=True, exist_ok=True)
    manifest_path = work / "collect.json"
    tmp = Path(tempfile.mkdtemp(prefix="tmp-", dir=work))
    argv = [str(env.python), "-m", "pytest", "-p", PROBE_MODULE, *distribution_args(env), "--collect-only", "-q"]
    variables = {PARENT_ENV: str(os.getpid()), MODE_ENV: "collect", MANIFEST_ENV: str(manifest_path), "TMPDIR": str(tmp)}
    try:
        done = subprocess.run(
            argv, cwd=checkout, env=child_env(probe_dir, variables), capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        raise CriteriaError(ErrorKind.COLLECTION_FAILED, f"collection exceeded {timeout:.0f}s") from None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    manifest = _read_manifest(manifest_path)
    if manifest is None:
        tail = (done.stdout + done.stderr).strip()[-2000:]
        raise CriteriaError(ErrorKind.COLLECTION_FAILED, f"no collection manifest (exit {done.returncode}): {tail}")
    return _inventory(manifest, checkout, done.returncode)


def _read_manifest(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = isinstance(data, dict) and data.get("complete") is True and data.get("mode") == "collect"
    return data if ok else None


def _relative(path: str, checkout: Path) -> str | None:
    resolved = Path(path).resolve()
    base = checkout.resolve()
    return resolved.relative_to(base).as_posix() if resolved.is_relative_to(base) else None


def _inventory(manifest: dict[str, Any], checkout: Path, returncode: int) -> Inventory:
    errors = manifest.get("errors") or []
    if isinstance(errors, list) and errors:
        where = ", ".join(str(e.get("node_id")) for e in errors if isinstance(e, dict))
        raise CriteriaError(ErrorKind.COLLECTION_ERROR, f"pytest reported collection errors in: {where}")
    if returncode not in _INVENTORY_EXIT:
        raise CriteriaError(ErrorKind.COLLECTION_FAILED, f"pytest --collect-only exited {returncode}")
    inipath_abs = manifest.get("inipath")
    inipath = _relative(str(inipath_abs), checkout) if inipath_abs else None
    if inipath_abs and inipath is None:
        raise CriteriaError(
            ErrorKind.COLLECTION_CONFIG_OUTSIDE_CHECKOUT, f"pytest read its config from {inipath_abs}"
        )
    items: list[TestItem] = []
    files: set[str] = {inipath} if inipath else set()
    for conftest in manifest.get("conftests") or []:
        rel = _relative(str(conftest), checkout)
        if rel is not None:
            files.add(rel)
    for raw in manifest.get("items") or []:
        if not isinstance(raw, dict) or not raw.get("function"):
            continue
        definition = raw.get("definition")
        module = _relative(str(raw.get("module")), checkout)
        target = _relative(str(definition["file"]), checkout) if isinstance(definition, dict) else None
        if target is None or module is None:
            raise CriteriaError(
                ErrorKind.DEFINITION_UNRESOLVED,
                f"{raw.get('node_id')}: its definition does not resolve inside the checkout",
            )
        files.update({module, target})
        items.append(TestItem(str(raw["node_id"]), target, str(definition["qualname"]), int(definition["line"])))
    plugins = tuple(str(p) for p in manifest.get("plugins") or [])
    return Inventory(tuple(items), tuple(sorted(files)), inipath, plugins)
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_criteria_inventory.py -q && uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: PASS; clean. If `test_items_with_real_definitions…` reports a different `line` for `test_p`, check against `co_firstlineno` in a REPL before touching the expectation — line 5 is the decorator.

- [ ] **Step 6: Commit**

```bash
git add src/spec_runner/criteria_probe.py src/spec_runner/criteria_inventory.py tests/test_criteria_inventory.py
git commit -m "feat(#603): the probe's collect mode and the collection inventory (probe/1)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Selection, `test_items`, `content_sha256`

**Files:**
- Create: `src/spec_runner/criteria_select.py`
- Test: `tests/test_criteria_select.py`

**Interfaces:**
- Consumes: `owned_definitions` (B1), `TestItem`, `Inventory` (Task 5), `CriteriaError`, `ErrorKind` (Task 2).
- Produces:
  - `select(items: Sequence[TestItem], beh_ids: Sequence[str], read: Callable[[str], str]) -> dict[str, list[TestItem]]`
  - `content_sha256(declared: Sequence[str], lock_sha256: str, files: Mapping[str, bytes]) -> str`

- [ ] **Step 1: Write the failing tests** — `tests/test_criteria_select.py`:

```python
"""#603 B2a: BEH selectors from collected items, and the §6.1 digest."""

from __future__ import annotations

import hashlib
import json

import pytest

from spec_runner.criteria_contract import CriteriaError, ErrorKind
from spec_runner.criteria_inventory import TestItem
from spec_runner.criteria_select import content_sha256, select

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
FILES = {"tests/test_a.py": SOURCE}


def _item(node: str, qualname: str, line: int, file: str = "tests/test_a.py") -> TestItem:
    return TestItem(node, file, qualname, line)


ITEMS = [
    _item("tests/test_a.py::test_p[1]", "test_p", 4),
    _item("tests/test_a.py::test_p[2]", "test_p", 4),
    _item("tests/test_a.py::TestK::test_m", "TestK.test_m", 12),
]


class TestSelect:
    def test_every_parametrized_item_and_class_region(self):
        chosen = select(ITEMS, ["ENC:BEH-01", "ENC:BEH-02", "ENC:BEH-03"], FILES.__getitem__)
        assert [i.node_id for i in chosen["ENC:BEH-01"]] == ["tests/test_a.py::test_p[1]", "tests/test_a.py::test_p[2]"]
        assert [i.node_id for i in chosen["ENC:BEH-02"]] == ["tests/test_a.py::TestK::test_m"]
        assert chosen["ENC:BEH-03"] == []

    def test_a_line_the_ast_does_not_hold_is_unresolved(self):  # Review Focus 3
        with pytest.raises(CriteriaError) as raised:
            select([_item("tests/test_a.py::test_p[1]", "test_p", 5)], ["ENC:BEH-01"], FILES.__getitem__)
        assert raised.value.kind is ErrorKind.DEFINITION_UNRESOLVED

    def test_an_unparseable_definition_file_is_unresolved(self):
        with pytest.raises(CriteriaError) as raised:
            select([_item("tests/t.py::test_x", "test_x", 1, "tests/t.py")], ["ENC:BEH-01"], {"tests/t.py": "def (:\n"}.__getitem__)
        assert raised.value.kind is ErrorKind.DEFINITION_UNRESOLVED


class TestDigest:
    def test_matches_the_design_definition(self):
        files = {"pkg/mod.py": b"x = 1\n", "tests/test_a.py": b"def test(): pass\n"}
        expected_obj = {
            "v": 1,
            "product_roots": ["pkg"],
            "lock": "c" * 64,
            "files": [[p, hashlib.sha256(files[p]).hexdigest()] for p in sorted(files)],
        }
        expected = hashlib.sha256(
            json.dumps(expected_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        ).hexdigest()
        assert content_sha256(["pkg"], "c" * 64, files) == expected

    def test_each_input_moves_the_digest(self):
        files = {"pkg/mod.py": b"x = 1\n"}
        base = content_sha256(["pkg"], "c" * 64, files)
        assert content_sha256(["pkg", "tool.py"], "c" * 64, files) != base
        assert content_sha256(["pkg"], "d" * 64, files) != base
        assert content_sha256(["pkg"], "c" * 64, {"pkg/mod.py": b"x = 2\n"}) != base
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_criteria_select.py -q`
Expected: collection ERROR — `ModuleNotFoundError: spec_runner.criteria_select`.

- [ ] **Step 3: Implement** `src/spec_runner/criteria_select.py`:

```python
"""BEH selectors from the collection inventory, and content_sha256 (#603 §3.5, §6.1).

A collected item's definition must be one the AST at product_sha holds at the
same (qualname, line): otherwise pytest ran something the static view cannot
name (another if/else branch, generated code) and no selector set could be
claimed complete — `definition-unresolved`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence

from .criteria_contract import CriteriaError, ErrorKind
from .criteria_inventory import TestItem
from .criteria_tokens import owned_definitions


def select(
    items: Sequence[TestItem], beh_ids: Sequence[str], read: Callable[[str], str]
) -> dict[str, list[TestItem]]:
    """Every collected item whose definition owns each BEH's token, in inventory order."""
    owned_by_file: dict[str, dict[tuple[str, int], tuple[str, ...]]] = {}
    chosen: dict[str, list[TestItem]] = {beh: [] for beh in beh_ids}
    for item in items:
        owned = owned_by_file.get(item.file)
        if owned is None:
            try:
                definitions = owned_definitions(read(item.file))
            except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
                raise CriteriaError(
                    ErrorKind.DEFINITION_UNRESOLVED, f"{item.file} cannot be parsed: {exc}"
                ) from None
            owned = {(d.qualname, d.line): d.tokens for d in definitions}
            owned_by_file[item.file] = owned
        tokens = owned.get((item.qualname, item.line))
        if tokens is None:
            raise CriteriaError(
                ErrorKind.DEFINITION_UNRESOLVED,
                f"{item.node_id}: {item.qualname} at {item.file}:{item.line} is not a definition "
                "the file's AST holds at product_sha",
            )
        for beh in beh_ids:
            if beh in tokens:
                chosen[beh].append(item)
    return chosen


def content_sha256(declared: Sequence[str], lock_sha256: str, files: Mapping[str, bytes]) -> str:
    """The G6 key of design §6.1: declaration, lock and file contents, canonically encoded."""
    obj = {
        "v": 1,
        "product_roots": sorted(declared),
        "lock": lock_sha256,
        "files": [
            [path, hashlib.sha256(files[path]).hexdigest()]
            for path in sorted(files, key=lambda p: p.encode("utf-8"))
        ],
    }
    encoded = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_criteria_select.py -q && uv run mypy src && uv run ruff check .`
Expected: PASS; clean.

- [ ] **Step 5: Commit**

```bash
git add src/spec_runner/criteria_select.py tests/test_criteria_select.py
git commit -m "feat(#603): BEH selection by token ownership and the content digest

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Docs and the full gate

**Files:** Modify `CLAUDE.md` (module table rows for the six new modules; Testing list), `CHANGELOG.md` (`[Unreleased]` › Added: "criteria-closure/v1 schemas and the measurement's pre-run stages (no command yet)"), `TODO.md` (B2a sub-checkbox under `criteria-closure-b2`).

- [ ] **Step 1:** Add one `CLAUDE.md` row per module (`criteria_contract.py`, `criteria_workspace.py`, `criteria_roots.py`, `criteria_probe.py`, `criteria_inventory.py`, `criteria_select.py`), each one line naming its job and its Produces; add the six test files to the Testing paragraph.
- [ ] **Step 2:** CHANGELOG entry as above; `TODO.md`: under `criteria-closure-b2` add `- [x] B2a — contract, workspace, inventory, selection (plan …b2a…)` and `- [ ] B2b — probe run mode, isolated runs, aggregation, CLI`.
- [ ] **Step 3: Full gate**

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy src
uv run python scripts/check_changelog_links.py
uv run pytest tests/ -q -m "not slow"
uv run pytest tests/ -q -m slow
```

Expected: clean; all pass.

- [ ] **Step 4:** Commit, then the repo's gate: one local round (`sh scripts/review/local-claude.sh`, raise `--max-diff-files` explicitly if needed), PR, `sh ../devtools/review-pr.sh spec-runner <pr>`, merge on approve with green checks via `GH_CONFIG_DIR=~/.config/review gh pr merge --match-head-commit`.
