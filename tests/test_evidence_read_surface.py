"""`spec-runner evidence <run_id>` (#480 BEH-36, BEH-37): one `collect()`, store only."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
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


# --- PR #665 acceptance findings ---------------------------------------------


@contextlib.contextmanager
def _live_run(tmp_path: Path, run_id: str = RUN, **config_over: Any) -> Iterator[tuple[Any, Any]]:
    """A started run whose publisher writes into a local store under tmp_path."""
    from spec_runner import run_context
    from spec_runner.config import ExecutorConfig
    from spec_runner.evidence import Publisher

    store = LocalVolumeStore(tmp_path / "store")
    ctx = run_context.RunContext(run_id, None, "run", "2026-10-05T10:00:00Z")
    ctx.publisher = Publisher(store)
    ctx.started = True
    config = ExecutorConfig(project_root=tmp_path, **config_over)
    run_context.install(ctx)
    try:
        yield store, config
    finally:
        run_context.install(None)


def _export_lines(store: LocalVolumeStore, key: str) -> list[dict[str, Any]]:
    lines = [json.loads(x) for x in (store.get(key) or b"").decode().splitlines()]
    for line in lines:
        jsonschema.validate(line, _schema("evidence-record.schema.json"))
    return lines


def _attempt_keys(store: LocalVolumeStore) -> list[str]:
    return [k for k in store.list("") if "/attempts/" in k]


class TestExportCarriesEveryListedTable:
    """FR-06 / AC-21: the export lists every table the requirement names."""

    def test_claim_waivers_authorization_and_remedy_are_exported(self, tmp_path: Path) -> None:
        from spec_runner.claims import Claim
        from spec_runner.remedy import RemedyOperation, RemedyRecord
        from spec_runner.state import ExecutorState, PhaseOutcome
        from spec_runner.tdd import resolve_namespace

        with _live_run(tmp_path) as (store, config):
            ns = resolve_namespace(config)
            with ExecutorState(config) as state:
                for task in ("TASK-001", "TASK-002"):
                    state.record_claim(
                        Claim(ns, task, "cp1", "sha1", f"tests/{task}.py", "blob", "t0")
                    )
                    state.record_waiver(task, "tests", PhaseOutcome.SKIPPED, "why", "op")
                    state.record_waiver_applied(task, ns, "class", "sanction", "base")
                    state.record_budget_authorization(
                        scope="task",
                        task_id=task,
                        namespace=ns,
                        new_limit_usd=5.0,
                        recorded_spend_usd=1.0,
                        unmeasured_calls=0,
                        actor="op",
                        reason="raise",
                    )
                    state.record_remedy(
                        RemedyRecord(ns, task, "cp1", RemedyOperation.ABANDON, "r", "op", "t1")
                    )
                state.record_attempt("TASK-001", success=True, duration=1.0, cost_usd=0.5)

        (key,) = _attempt_keys(store)
        lines = _export_lines(store, key)
        tables = {line["table"] for line in lines}
        for table in (
            "attempts",
            "tdd_claims",
            "phase_waivers",
            "waivers_applied",
            "budget_authorizations",
            "tdd_remedies",
        ):
            assert table in tables, table
        assert {line["row"]["task_id"] for line in lines} == {"TASK-001"}

    def test_schema_enumerates_the_requirement_tables(self) -> None:
        enum = set(_schema("evidence-record.schema.json")["properties"]["table"]["enum"])
        required = {
            "attempts",
            "red_checkpoints",
            "tdd_claims",
            "tdd_phases",
            "tdd_remedies",
            "phase_waivers",
            "waivers_applied",
            "budget_authorizations",
            "gate_verdicts",
            "verify_evidence",
            "agent_calls",
        }
        assert required <= enum


class TestCallsStitchToTheirAttempt:
    """BEH-36: an attempt's view links its call records, also after a plain `retry`.

    Invocation A records a failed attempt; invocation B (a `retry` keeps A's
    attempts) succeeds. B's export is `TASK-001-1` (the ordinal within B, the
    numbering of closure `attempt_ids`), so B's call-starts -- GREEN and review
    -- must carry 1 too, not the lifetime count 2.
    """

    def test_retry_in_a_second_invocation_links_green_and_review(self, tmp_path: Path) -> None:
        from spec_runner import execution
        from spec_runner.state import ExecutorState
        from spec_runner.task import Task
        from tests.call_doubles import (
            Journal,
            RecordingStore,
            completed,
            make_run,
            project,
            spawn_double,
        )

        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1d")
        cfg = project(tmp_path, run_review=True)
        (cfg.project_root / "spec" / "tasks.md").write_text(
            "# Tasks\n\n### TASK-001: t\n🟠 P1 | ⬜ TODO | Est: 1d\n\n**Checklist:**\n- [ ] x\n"
        )
        journal = Journal()
        store = RecordingStore(journal)

        def priced(text: str) -> str:
            return json.dumps({"result": text, "total_cost_usd": 0.01, "usage": {}})

        def answer(green: str) -> Any:
            def _answer(argv: list[str]) -> Any:
                if "Code Review Request" in " ".join(argv):
                    return completed(argv, priced("REVIEW_PASSED"))
                return completed(argv, priced(green))

            return _answer

        from spec_runner import run_context

        try:
            make_run(journal, store)
            with spawn_double(journal, answer("TASK_FAILED: not yet")), ExecutorState(cfg) as st:
                assert execution.execute_task(task, cfg, st) is not True
            ctx_b = make_run(journal, store)
            with spawn_double(journal, answer("TASK_COMPLETE")), ExecutorState(cfg) as st:
                assert execution.execute_task(task, cfg, st) is True
            assert ctx_b.publisher is not None
            assert ctx_b.publisher.drain(1.0)
        finally:
            run_context.install(None)

        prefix = f"runs/{ctx_b.run_id}/calls/"
        b_starts = {
            json.loads(store.objects[k])["call_id"]: json.loads(store.objects[k])
            for k in store.list(prefix)
            if k.endswith("/start.json")
        }
        assert {s["provenance"] for s in b_starts.values()} >= {"green", "review"}
        assert {s["attempt"] for s in b_starts.values()} == {1}

        store.objects[run_start_key(ctx_b.run_id)] = json.dumps(
            _start(run_id=ctx_b.run_id)
        ).encode()
        view = evidence_cmd.collect(store, ctx_b.run_id)  # type: ignore[arg-type]
        (attempt,) = view.attempts
        assert (attempt.task_id, attempt.attempt, attempt.outcome) == ("TASK-001", 1, "success")
        assert sorted(attempt.calls) == sorted(b_starts)


class TestEveryTerminalPathExportsOnce:
    """DEL-25 / AC-21: a task that ends this invocation done, failed or blocked
    leaves exactly one attempt export -- also where `record_attempt` alone does
    not make the task `failed` (terminal refusal, budget, a fatal error code,
    a pre-start hook failure, `retry`). An interrupted attempt is not terminal:
    the task is resumable, and nothing is exported for it.
    """

    TASKS_MD = "# Tasks\n\n### TASK-001: t\n🟠 P1 | ⬜ TODO | Est: 1d\n\n**Checklist:**\n- [ ] x\n"

    def _config_over(self, tmp_path: Path, **over: Any) -> dict[str, Any]:
        (tmp_path / "spec").mkdir(exist_ok=True)
        (tmp_path / "spec" / "tasks.md").write_text(self.TASKS_MD)
        return {
            "state_file": tmp_path / "spec" / ".executor-state.db",
            "logs_dir": tmp_path / "spec" / ".executor-logs",
            "create_git_branch": False,
            "auto_commit": False,
            "run_tests_on_done": False,
            "run_review": False,
            "callback_url": "",
            "retry_delay_seconds": 0,
            "max_retries": 3,
            **over,
        }

    @staticmethod
    def _fake(result: Any, **attempt: Any) -> Any:
        def _execute(task: Any, config: Any, state: Any, **_: Any) -> Any:
            state.record_attempt(task.id, False, 0.1, error="x", **attempt)
            return result

        return _execute

    @staticmethod
    def _count_exports(monkeypatch: pytest.MonkeyPatch) -> list[str]:
        from spec_runner.evidence import AttemptExport, Publisher

        published: list[str] = []
        original = Publisher.publish_or_queue

        def spy(self: Any, record: Any) -> Any:
            if isinstance(record, AttemptExport):
                published.append(record.key)
            return original(self, record)

        monkeypatch.setattr(Publisher, "publish_or_queue", spy)
        return published

    def _run(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake: Any,
        before: Any = None,
        **over: Any,
    ) -> tuple[LocalVolumeStore, list[str], list[dict[str, Any]]]:
        from spec_runner import execution
        from spec_runner.state import ExecutorState
        from spec_runner.task import Task

        task = Task(id="TASK-001", name="t", priority="p1", status="todo", estimate="1d")
        published = self._count_exports(monkeypatch)
        monkeypatch.setattr(execution, "execute_task", fake)
        with (
            _live_run(tmp_path, **self._config_over(tmp_path, **over)) as (store, config),
            ExecutorState(config) as state,
        ):
            if before is not None:
                before(state)
            assert execution.run_with_retries(task, config, state) is not True
        keys = _attempt_keys(store)
        rows = [r["row"] for k in keys for r in _export_lines(store, k) if r["table"] == "attempts"]
        return store, published, rows

    def test_terminal_refusal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from spec_runner.state import ErrorCode

        fake = self._fake(
            "TERMINAL_REFUSAL", error_code=ErrorCode.HOOK_FAILURE, error_kind="policy"
        )
        store, published, rows = self._run(tmp_path, monkeypatch, fake)

        assert published == [attempt_key(RUN, "TASK-001", 1)]
        assert [r["error_kind"] for r in rows] == ["policy"]

    def test_fatal_error_code(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from spec_runner.state import ErrorCode

        fake = self._fake(False, error_code=ErrorCode.REVIEW_REJECTED, error_kind="review")
        _, published, rows = self._run(tmp_path, monkeypatch, fake)

        assert published == [attempt_key(RUN, "TASK-001", 1)]
        assert [r["error_kind"] for r in rows] == ["review"]

    def test_pre_start_hook_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from spec_runner.state import ErrorCode

        fake = self._fake(
            "HOOK_ERROR", error_code=ErrorCode.HOOK_FAILURE, error_kind="hook_failure"
        )
        _, published, rows = self._run(tmp_path, monkeypatch, fake)

        assert published == [attempt_key(RUN, "TASK-001", 1)]
        assert [r["error_kind"] for r in rows] == ["hook_failure"]

    def test_budget_after_an_attempt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from spec_runner.state import ErrorCode

        fake = self._fake(False, error_code=ErrorCode.TEST_FAILURE, cost_usd=2.0)
        _, published, rows = self._run(tmp_path, monkeypatch, fake, task_budget_usd=1.0)

        # The budget refusal is the attempt the task ended with: the second one.
        assert published == [attempt_key(RUN, "TASK-001", 2)]
        assert [r["error_kind"] for r in rows] == ["budget"]

    def test_budget_before_any_attempt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def spent(state: Any) -> None:
            state.record_agent_call("TASK-001", "green", cost_usd=2.0)

        fake = self._fake(True)  # never reached
        _, published, rows = self._run(
            tmp_path, monkeypatch, fake, before=spent, task_budget_usd=1.0
        )

        assert published == [attempt_key(RUN, "TASK-001", 1)]
        assert [r["error_kind"] for r in rows] == ["budget"]

    def test_exhausted_retries_export_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spec_runner.state import ErrorCode

        fake = self._fake(False, error_code=ErrorCode.TEST_FAILURE, error_kind="test_failure")
        _, published, rows = self._run(tmp_path, monkeypatch, fake, max_retries=2)

        assert published == [attempt_key(RUN, "TASK-001", 2)]
        assert len(rows) == 1

    def test_interrupted_is_not_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spec_runner.state import ErrorCode

        fake = self._fake(False, error_code=ErrorCode.INTERRUPTED, error_kind="interrupted")
        store, published, _ = self._run(tmp_path, monkeypatch, fake)

        assert published == []
        assert _attempt_keys(store) == []

    def test_mark_failed_is_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spec_runner.state import ExecutorState

        published = self._count_exports(monkeypatch)
        with (
            _live_run(tmp_path, **self._config_over(tmp_path)) as (store, config),
            ExecutorState(config) as state,
        ):
            state.record_attempt("TASK-001", False, 0.1, error="x", error_kind="policy")
            assert published == []
            state.mark_failed("TASK-001")
            state.mark_failed("TASK-001")

        assert published == [attempt_key(RUN, "TASK-001", 1)]

    def test_retry_that_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import argparse

        from spec_runner.state import ErrorCode

        published = self._count_exports(monkeypatch)
        fake = self._fake(False, error_code=ErrorCode.TEST_FAILURE, error_kind="test_failure")
        monkeypatch.setattr(cli, "execute_task", fake)
        monkeypatch.setattr(cli, "_maybe_start_integration", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_enforce_spec_governance", lambda *a, **k: None)
        monkeypatch.setattr(cli, "_enforce_clean_spec", lambda *a, **k: None)
        with _live_run(tmp_path, **self._config_over(tmp_path)) as (store, config):
            cli.cmd_retry(argparse.Namespace(task_id="TASK-001", fresh=False), config)

        assert published == [attempt_key(RUN, "TASK-001", 1)]
        assert len(_attempt_keys(store)) == 1


class TestNextStepNamesOnlyWhatExists:
    """BEH-37: the next step is a recommendation -- and a command it prints must run.

    `restore` is DT-06 and not in this version; a recommendation that prints
    `spec-runner restore ...` hands the operator an `invalid choice`.
    """

    @staticmethod
    def _views(root: Path) -> list[evidence_cmd.EvidenceView]:
        _completed(root / "closed")
        crash = LocalVolumeStore(root / "crash")
        _put(crash, run_start_key(RUN), _start())
        open_call = LocalVolumeStore(root / "open")
        _put(open_call, run_start_key(RUN), _start())
        _call(open_call, "c9", result=False, cost=None)
        legacy = LocalVolumeStore(root / "legacy")
        _put(legacy, f"runs/{RUN}/task-history.log", b"x")
        return [
            evidence_cmd.collect(open_store_readonly("local_volume", {"root": str(root / d)}), RUN)
            for d in ("closed", "crash", "open", "legacy")
        ]

    def test_every_printed_command_parses(self, tmp_path: Path) -> None:
        import re
        import shlex

        parser = cli._build_parser()
        for view in self._views(tmp_path):
            assert view.next_step is not None
            text = view.next_step.recommendation
            for command in re.findall(r"spec-runner ([^`;,()\n]+)", text):
                argv = [a for a in shlex.split(command) if not a.startswith("<")]
                try:
                    parser.parse_args(argv)
                except SystemExit as exc:  # argparse refuses: the command would fail
                    raise AssertionError(f"{view.status}: `spec-runner {command}`") from exc
