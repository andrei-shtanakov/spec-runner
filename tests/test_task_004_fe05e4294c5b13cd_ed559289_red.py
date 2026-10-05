"""RED for TASK-004 (BEH-36): `evidence_cmd.collect(store, run_id)` reads only the store."""

from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import json
from pathlib import Path

from spec_runner.artifact_store import (
    LocalVolumeStore,
    closure_key,
    open_store_readonly,
    run_start_key,
)

RUN_ID = "run-20261005-0001"


def _seed(root: Path) -> None:
    store = LocalVolumeStore(root)
    start = {
        "kind": "run-start",
        "run_id": RUN_ID,
        "subcommand": "run",
        "contract_version": 1,
        "ack_channel": "store",
        "policy": {"config_hash": "abc", "namespace": "ns"},
        "repository": {"remote": "git@example:x/y.git"},
        "started_at": "2026-10-05T10:00:00Z",
    }
    closure = {
        "kind": "closure",
        "run_id": RUN_ID,
        "subcommand": "run",
        "closure_kind": "completed",
        "reason": "",
        "exit_code": 0,
        "last_checkpoint_id": None,
        "last_call_ids": [],
        "attempt_ids": [],
        "open_calls": 0,
        "degraded": False,
        "started_at": "2026-10-05T10:00:00Z",
        "ended_at": "2026-10-05T10:05:00Z",
    }
    store.put(run_start_key(RUN_ID), json.dumps(start).encode(), metadata={})
    store.put(closure_key(RUN_ID), json.dumps(closure).encode(), metadata={})


class TestEvidenceCollect:
    def test_collect_reports_closed_completed_from_the_store_alone(self, tmp_path: Path):
        spec = importlib.util.find_spec("spec_runner.evidence_cmd")
        assert spec is not None, "spec_runner.evidence_cmd does not exist yet"
        evidence_cmd = importlib.import_module("spec_runner.evidence_cmd")

        root = tmp_path / "store"
        _seed(root)
        ro = open_store_readonly("local_volume", {"root": str(root)})
        before = sorted(ro.list(""))

        view = evidence_cmd.collect(ro, RUN_ID)

        flat = json.dumps(dataclasses.asdict(view), default=str)
        assert "closed:completed" in flat
        assert RUN_ID in flat
        assert sorted(ro.list("")) == before
