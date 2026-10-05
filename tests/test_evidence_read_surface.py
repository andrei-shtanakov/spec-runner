"""`spec-runner evidence <run_id>` (#480 BEH-36, BEH-37): one `collect()`, store only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from spec_runner import cli, evidence_cmd
from spec_runner.artifact_store import (
    LocalVolumeStore,
    attempt_key,
    call_result_key,
    call_start_key,
    checkpoint_key,
    closure_key,
    open_store_readonly,
    run_start_key,
)

RUN = "run-0001"
SCHEMAS = Path(__file__).resolve().parent.parent / "schemas"


def _schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / name).read_text())


def _put(store: LocalVolumeStore, key: str, body: Any) -> None:
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    store.put(key, data, metadata={})


def _start(**over: Any) -> dict[str, Any]:
    return {
        "kind": "run-start",
        "run_id": RUN,
        "subcommand": "run",
        "contract_version": 1,
        "ack_channel": "store",
        "policy": {"config_hash": "abc", "namespace": "ns"},
        "repository": {"remote": "git@example:x/y.git"},
        "started_at": "2026-10-05T10:00:00Z",
        **over,
    }


def _closure() -> dict[str, Any]:
    return {
        "kind": "closure",
        "run_id": RUN,
        "subcommand": "run",
        "closure_kind": "completed",
        "reason": "",
        "exit_code": 0,
        "last_checkpoint_id": "cp2",
        "last_call_ids": ["c1"],
        "attempt_ids": ["TASK-001-1"],
        "open_calls": 0,
        "degraded": False,
        "started_at": "2026-10-05T10:00:00Z",
        "ended_at": "2026-10-05T10:05:00Z",
    }


def _call(store: LocalVolumeStore, call_id: str, *, result: bool, cost: float | None) -> None:
    _put(
        store,
        call_start_key(RUN, call_id),
        {
            "call_id": call_id,
            "provenance": "green",
            "task_id": "TASK-001",
            "attempt": 1,
            "timestamp": "2026-10-05T10:01:00Z",
        },
    )
    if result:
        _put(store, call_result_key(RUN, call_id), {"call_id": call_id, "cost_usd": cost})


def _checkpoints(store: LocalVolumeStore) -> None:
    for seq, cp in ((1, "cp1"), (2, "cp2")):
        manifest = {"checkpoint_id": cp, "sequence": seq, "identity": {"namespace": "ns"}}
        _put(store, checkpoint_key(RUN, seq, cp, "manifest.json"), manifest)


def _completed(root: Path) -> None:
    store = LocalVolumeStore(root)
    _put(store, run_start_key(RUN), _start())
    _put(store, closure_key(RUN), _closure())
    _checkpoints(store)
    _call(store, "c1", result=True, cost=0.25)
    row = {"table": "attempts", "task_id": "TASK-001", "attempt": 1}
    row["row"] = {"success": 1, "cost_usd": 0.25, "error_kind": None}
    _put(store, attempt_key(RUN, "TASK-001", 1), (json.dumps(row) + "\n").encode())


class _SpyStore:
    """Wraps the read-only door and records every call that would write."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.writes: list[str] = []

    def put(self, key: str, data: bytes, *, metadata: dict[str, str]) -> Any:
        self.writes.append(key)
        return self.inner.put(key, data, metadata=metadata)

    def delete(self, key: str) -> None:
        self.writes.append(key)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _run_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], root: Path, *argv: str
) -> tuple[int, str]:
    spy = _SpyStore(open_store_readonly("local_volume", {"root": str(root)}))
    monkeypatch.setattr(evidence_cmd, "open_configured_store", lambda config: spy)
    with pytest.raises(SystemExit) as exc:
        cli.main(["evidence", RUN, *argv])
    assert spy.writes == []
    return int(exc.value.code or 0), capsys.readouterr().out


class TestBeh36:
    def test_human_and_json_come_from_one_collect(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _completed(tmp_path / "store")
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)

        code, text = _run_cli(monkeypatch, capsys, tmp_path / "store")
        code_json, raw = _run_cli(monkeypatch, capsys, tmp_path / "store", "--json")

        assert code == code_json == 0
        data = json.loads(raw)
        jsonschema.validate(data, _schema("evidence-view.schema.json"))
        assert data["status"] == "closed:completed"
        assert data["last_checkpoint"]["checkpoint_id"] == "cp2"
        assert data["last_checkpoint"]["sequence"] == 2
        assert data["last_checkpoint"]["namespace"] == "ns"
        assert data["open_calls"] == []
        assert data["attempts"][0]["cost_usd"] == 0.25
        assert data["attempts"][0]["calls"] == ["c1"]
        assert data["total_cost_usd"] == 0.25
        assert list(empty.iterdir()) == []
        for shown in ("closed:completed", "cp2", "seq=2", "ns=ns", "TASK-001#1", "$0.2500"):
            assert shown in text
        assert data["run_start"]["subcommand"] == "run"
        assert "git@example:x/y.git" in text

    def test_null_cost_is_unknown_and_counted(self, tmp_path: Path) -> None:
        store = LocalVolumeStore(tmp_path)
        _put(store, run_start_key(RUN), _start())
        _call(store, "c1", result=True, cost=None)
        row = {"table": "attempts", "task_id": "TASK-001", "attempt": 1}
        row["row"] = {"success": 0, "cost_usd": None, "error_kind": "policy"}
        _put(store, attempt_key(RUN, "TASK-001", 1), (json.dumps(row) + "\n").encode())

        view = evidence_cmd.collect(
            open_store_readonly("local_volume", {"root": str(tmp_path)}), RUN
        )

        assert view.attempts[0].cost_usd == "unknown"
        assert view.attempts[0].outcome == "policy"
        assert view.unpriced_calls == 1


class TestBeh37:
    def _view(self, root: Path) -> evidence_cmd.EvidenceView:
        return evidence_cmd.collect(open_store_readonly("local_volume", {"root": str(root)}), RUN)

    def test_no_closure_is_crash_unknown_and_not_provable(self, tmp_path: Path) -> None:
        _put(LocalVolumeStore(tmp_path), run_start_key(RUN), _start())

        view = self._view(tmp_path)

        assert view.status == "crash/unknown"
        assert view.next_step is not None
        assert view.next_step.unprovable is not None
        assert "не доказуемо" in view.next_step.unprovable

    def test_open_call_is_named(self, tmp_path: Path) -> None:
        store = LocalVolumeStore(tmp_path)
        _put(store, run_start_key(RUN), _start())
        _call(store, "c9", result=False, cost=None)

        (call,) = self._view(tmp_path).open_calls

        assert (call.call_id, call.provenance, call.task_id) == ("c9", "green", "TASK-001")
        assert call.timestamp == "2026-10-05T10:01:00Z"

    @pytest.mark.parametrize(
        "start", [None, _start(contract_version=0), _start(ack_channel="local")]
    )
    def test_legacy_lists_what_is_missing(self, tmp_path: Path, start: Any) -> None:
        store = LocalVolumeStore(tmp_path)
        _put(store, f"runs/{RUN}/task-history.log", b"x")
        if start is not None:
            _put(store, run_start_key(RUN), start)

        view = self._view(tmp_path)

        assert view.status == "legacy"
        assert view.status_note == "нет evidence-контракта"
        assert view.missing

    def test_unreachable_store_exits_2_with_a_reason(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        class Broken:
            def get(self, key: str) -> bytes:
                raise OSError("connection refused")

        monkeypatch.setattr(evidence_cmd, "open_configured_store", lambda config: Broken())

        with pytest.raises(SystemExit) as exc:
            cli.main(["evidence", RUN])

        assert exc.value.code == 2
        assert "connection refused" in capsys.readouterr().out

    def test_answer_is_limited_to_one_run(self, tmp_path: Path) -> None:
        store = LocalVolumeStore(tmp_path)
        _put(store, run_start_key(RUN), _start())
        _put(store, "runs/other/run-start.json", _start(run_id="other"))
        _put(store, "runs/other/closure.json", {**_closure(), "run_id": "other"})

        assert self._view(tmp_path).status == "crash/unknown"


class TestExportAttempt:
    def test_probe_publishes_nothing(self, tmp_path: Path) -> None:
        from spec_runner import run_context
        from spec_runner.config import ExecutorConfig
        from spec_runner.evidence import Publisher, export_attempt
        from spec_runner.state import ExecutorState

        store = LocalVolumeStore(tmp_path / "store")
        ctx = run_context.RunContext(RUN, None, "doctor", "2026-10-05T10:00:00Z")
        ctx.publisher = Publisher(store)
        ctx.started = True
        config = ExecutorConfig(project_root=tmp_path, probe_provenance="doctor")
        run_context.install(ctx)
        try:
            with ExecutorState(config) as state:
                assert export_attempt(state, "TASK-001", 1) is False
        finally:
            run_context.install(None)

        assert store.list("") == []

    def test_terminal_attempt_is_published_once(self, tmp_path: Path) -> None:
        from spec_runner import run_context
        from spec_runner.config import ExecutorConfig
        from spec_runner.evidence import Publisher
        from spec_runner.state import ExecutorState

        store = LocalVolumeStore(tmp_path / "store")
        ctx = run_context.RunContext(RUN, None, "run", "2026-10-05T10:00:00Z")
        ctx.publisher = Publisher(store)
        ctx.started = True
        config = ExecutorConfig(project_root=tmp_path)
        run_context.install(ctx)
        try:
            with ExecutorState(config) as state:
                state.record_attempt("TASK-001", success=True, duration=1.0, cost_usd=0.5)
        finally:
            run_context.install(None)

        (key,) = [k for k in store.list("") if "/attempts/" in k]
        assert key == attempt_key(RUN, "TASK-001", 1)
        lines = [json.loads(x) for x in (store.get(key) or b"").decode().splitlines()]
        for line in lines:
            jsonschema.validate(line, _schema("evidence-record.schema.json"))
        attempts = [x for x in lines if x["table"] == "attempts"]
        assert attempts[0]["row"]["cost_usd"] == 0.5
