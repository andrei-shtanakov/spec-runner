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
