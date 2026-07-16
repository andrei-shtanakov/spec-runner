"""Validates obs.py output against maestro/contracts/observability/."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import jsonschema
import pytest

_SPEC_RUNNER_ROOT = Path(__file__).resolve().parents[1]
_UMBRELLA = _SPEC_RUNNER_ROOT.parent  # all_ai_orchestrators/
_CONTRACT = _UMBRELLA / "Maestro" / "contracts" / "observability"
_SCHEMA_PATH = _CONTRACT / "log-schema.json"
if not _SCHEMA_PATH.exists():
    pytest.skip(
        "observability contract unavailable (sibling Maestro checkout "
        "not present in standalone CI)",
        allow_module_level=True,
    )
_SCHEMA = json.loads(_SCHEMA_PATH.read_text())


@pytest.fixture
def obs_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv(
        "TRACEPARENT",
        "00-3f2e8c1a9b7d450f6e2c8a1b9f4d730e-9f2e4a1b6c0d3387-01",
    )
    monkeypatch.setenv("ORCHESTRA_PIPELINE_ID", "01HZKX3P9M7Q2VFGR8BNDAW5YT")
    import spec_runner.obs as mod

    importlib.reload(mod)
    mod.init_logging("spec-runner")
    return mod, tmp_path


def test_emits_schema_valid_records(obs_env):
    mod, tmp_path = obs_env
    with mod.span("spec.verify.task", task_id="T-042"):
        mod.get_logger("execution").info("check.started", check_type="syntax")
    for line in list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines():
        jsonschema.validate(json.loads(line), _SCHEMA)


def test_fixture_root_span_is_schema_valid():
    for line in (_CONTRACT / "fixtures" / "root-span.jsonl").read_text().splitlines():
        jsonschema.validate(json.loads(line), _SCHEMA)


def test_fixture_nested_span_is_schema_valid():
    for line in (_CONTRACT / "fixtures" / "nested-span.jsonl").read_text().splitlines():
        jsonschema.validate(json.loads(line), _SCHEMA)
