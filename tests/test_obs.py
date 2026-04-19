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
    import importlib

    import spec_runner.obs as mod

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

    import importlib

    import spec_runner.obs as mod

    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info("root.started")

    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert len(rec["TraceId"]) == 32
    assert "parent_span_id" not in rec["Attributes"]


def test_traceparent_malformed_warns_and_roots(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("TRACEPARENT", "garbage")

    import importlib

    import spec_runner.obs as mod

    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info("recovered")

    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert len(rec["TraceId"]) == 32
    assert "parent_span_id" not in rec["Attributes"]


def test_timestamp_formats(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import importlib

    import spec_runner.obs as mod

    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info("ts.check")

    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    # Timestamp: ns since epoch, string, ~19 digits in 2026
    assert rec["Timestamp"].isdigit() and 18 <= len(rec["Timestamp"]) <= 20
    # ts_iso: microseconds, Z suffix
    assert rec["ts_iso"].endswith("Z")
    assert len(rec["ts_iso"].split(".")[1]) == 7  # "NNNNNNZ" = 6 digits + Z


def test_span_nesting_linkage(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import importlib

    import spec_runner.obs as mod

    importlib.reload(mod)
    mod.init_logging("spec-runner")
    log = mod.get_logger()

    with mod.span("outer.op") as outer:
        log.info("inside.outer")
        with mod.span("inner.op") as inner:
            log.info("inside.inner")
            assert inner.parent_span_id == outer.span_id

    lines = [json.loads(line) for line in list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()]
    inner_records = [r for r in lines if r["Body"] in ("inside.inner", "inner.op.started")]
    for r in inner_records:
        assert r["Attributes"].get("parent_span_id") == outer.span_id


def test_span_emits_started_and_ended(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import importlib

    import spec_runner.obs as mod

    importlib.reload(mod)
    mod.init_logging("spec-runner")
    with mod.span("op.do", x=1):
        pass
    events = [
        json.loads(line)["Attributes"]["event"]
        for line in list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()
    ]
    assert "op.do.started" in events
    assert "op.do.ended" in events


def test_span_failure_emits_failed_and_reraises(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import importlib

    import spec_runner.obs as mod

    importlib.reload(mod)
    mod.init_logging("spec-runner")
    with pytest.raises(RuntimeError), mod.span("op.do"):
        raise RuntimeError("boom")
    lines = [json.loads(line) for line in list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()]
    failed = [r for r in lines if r["Attributes"]["event"] == "op.do.failed"]
    assert len(failed) == 1
    assert failed[0]["Attributes"]["error"]["type"] == "RuntimeError"
    assert failed[0]["Attributes"]["error"]["message"] == "boom"


def test_redaction_default_blocklist(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import importlib

    import spec_runner.obs as mod

    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info("http.request", api_key="sk-secret", password="p", url="https://x")
    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert rec["Attributes"]["api_key"] == "<redacted>"
    assert rec["Attributes"]["password"] == "<redacted>"
    assert rec["Attributes"]["url"] == "https://x"


def test_redaction_nested(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import importlib

    import spec_runner.obs as mod

    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info("ctx", headers={"Authorization": "Bearer t", "X-Req": "1"})
    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert rec["Attributes"]["headers"]["Authorization"] == "<redacted>"
    assert rec["Attributes"]["headers"]["X-Req"] == "1"


def test_redaction_extended_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    monkeypatch.setenv("ORCHESTRA_REDACT_KEYS", "ssn,pin")
    import importlib

    import spec_runner.obs as mod

    importlib.reload(mod)
    mod.init_logging("spec-runner")
    mod.get_logger().info("pii", ssn="123", pin="1234", name="Alice")
    rec = json.loads(list(tmp_path.glob("*.jsonl"))[0].read_text().splitlines()[0])
    assert rec["Attributes"]["ssn"] == "<redacted>"
    assert rec["Attributes"]["pin"] == "<redacted>"
    assert rec["Attributes"]["name"] == "Alice"


def test_child_env_contains_traceparent(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_LOG_DIR", str(tmp_path))
    monkeypatch.delenv("TRACEPARENT", raising=False)
    import importlib

    import spec_runner.obs as mod

    importlib.reload(mod)
    mod.init_logging("spec-runner")
    with mod.span("outer") as s:
        env = mod.child_env()
    tp = env["TRACEPARENT"]
    assert tp.startswith("00-")
    parts = tp.split("-")
    assert len(parts[1]) == 32  # trace_id
    assert parts[2] == s.span_id  # parent for the child = current span
    assert parts[3] == "01"
    assert "ORCHESTRA_PIPELINE_ID" in env
    assert env["ORCHESTRA_LOG_DIR"] == str(tmp_path)
