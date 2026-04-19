"""Unit tests for spec_runner.obs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def test_init_logging_creates_logger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)

    from spec_runner import obs

    obs.init_logging("spec-runner")
    log = obs.get_logger("test")
    log.info("hello.world", x=1)

    files = list(tmp_path.glob("spec-runner-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["Resource"]["service.name"] == "spec-runner"
    assert record["Attributes"]["event"] == "hello.world"
    assert record["Attributes"]["x"] == 1
    assert record["SeverityText"] == "INFO"


def test_traceparent_valid_inherited(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    tid = "3f2e8c1a9b7d450f6e2c8a1b9f4d730e"
    pid_span = "9f2e4a1b6c0d3387"
    monkeypatch.setenv("TRACEPARENT", f"00-{tid}-{pid_span}-01")
    monkeypatch.setenv("ORCHESTRA_PIPELINE_ID", "01HZKX3P9M7Q2VFGR8BNDAW5YT")

    # Re-import fresh to reset module state
    import importlib, spec_runner.obs as mod
    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info("child.started")

    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert rec["TraceId"] == tid
    assert rec["Attributes"]["parent_span_id"] == pid_span
    assert rec["Attributes"]["pipeline_id"] == "01HZKX3P9M7Q2VFGR8BNDAW5YT"
    assert rec["SpanId"] != pid_span  # fresh span for this process


def test_traceparent_empty_means_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("TRACEPARENT", "")

    import importlib, spec_runner.obs as mod
    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info("root.started")

    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert len(rec["TraceId"]) == 32
    assert "parent_span_id" not in rec["Attributes"]


def test_traceparent_malformed_warns_and_roots(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("TRACEPARENT", "garbage")

    import importlib, spec_runner.obs as mod
    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info("recovered")

    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert len(rec["TraceId"]) == 32
    assert "parent_span_id" not in rec["Attributes"]
