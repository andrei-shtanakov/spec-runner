"""Store-контракт, ключи § 1.3 и первый адаптер (DT-01 бандла #480).

Предмет — design § 1.1–1.3: протокол `ArtifactStore`, декларация
`StoreCapabilities`, функции ключей (включая индекс workstream-а и
`workstream_key`), адаптер `LocalVolumeStore` и `open_store_readonly`.

Почему отдельным заходом руками. Исполняемая проекция задачи несла только
`scenarios: [BEH-28]` — отказ config на небезопасном адаптере, — и платный
прогон закрыл ровно его, оставив store-слой непоставленным (devtools#282).
Здесь он дописывается: DT-02 и DT-03 опираются на этот модуль.
"""

from __future__ import annotations

import inspect
import os
import stat
from pathlib import Path

import pytest

from spec_runner.artifact_store import (
    AlreadyExists,
    ArtifactStore,
    LocalVolumeStore,
    StoreCapabilities,
    attempt_key,
    call_result_key,
    call_start_key,
    checkpoint_key,
    closure_key,
    open_store_readonly,
    run_start_key,
    workstream_index_key,
    workstream_key,
)


class TestKeys:
    """§ 1.3: каждый ключ имеет одну форму, и она не зависит от адаптера."""

    def test_keys_have_the_declared_shape(self):
        assert run_start_key("R1") == "runs/R1/run-start.json"
        assert call_start_key("R1", "C7") == "runs/R1/calls/C7/start.json"
        assert call_result_key("R1", "C7") == "runs/R1/calls/C7/result.json"
        assert closure_key("R1") == "runs/R1/closure.json"
        assert attempt_key("R1", "TASK-001", 2) == "runs/R1/attempts/TASK-001-2.jsonl"

    def test_checkpoint_key_pads_the_sequence_to_six_digits(self):
        """`<seq:06d>` — не украшение: лексикографический порядок ключей обязан
        совпадать с числовым, иначе `list(prefix)` отдаёт 10 раньше 9."""
        assert checkpoint_key("R1", 9, "abc", "manifest.json") == (
            "runs/R1/checkpoints/000009-abc/manifest.json"
        )
        assert checkpoint_key("R1", 10, "abc", "state.db") < checkpoint_key(
            "R1", 100, "abc", "state.db"
        )

    def test_workstream_index_key_carries_started_at_before_run_id(self):
        key = workstream_index_key("WS", "2026-09-21T10:00:00Z", "R1")
        assert key == "workstreams/WS/runs/2026-09-21T10:00:00Z-R1.json"
        assert workstream_index_key("WS", "2026-09-21T10:00:00Z", "R1", closed=True) == (
            "workstreams/WS/runs/2026-09-21T10:00:00Z-R1.closed"
        )

    def test_workstream_key_ignores_the_absolute_path(self):
        """Ключ обязан пережить переезд каталога — иначе после restore на другой
        машине workstream не найти (§ 1.3, в отличие от `tdd.resolve_namespace`,
        который хеширует абсолютный путь намеренно)."""
        a = workstream_key("github.com/o/r", "root-sha", spec_prefix="p-", change_id="")
        b = workstream_key("github.com/o/r", "root-sha", spec_prefix="p-", change_id="")
        assert a == b
        assert workstream_key("github.com/o/r", "root-sha", spec_prefix="q-", change_id="") != a
        assert workstream_key("github.com/o/other", "root-sha", spec_prefix="p-", change_id="") != a

    def test_workstream_key_is_a_hex_digest_without_separators(self):
        key = workstream_key("github.com/o/r", "root-sha", spec_prefix="", change_id="")
        assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)

    def test_workstream_key_takes_no_path_argument_at_all(self):
        """Свойство «путь не входит» держит СИГНАТУРА, а не вычисление.

        Поймано мутационной проверкой: подмешать в payload константу тест по
        значениям не различает, потому что различать нечего — вход один и тот
        же. Отличить можно только появление входа-пути, что и утверждается:
        добавит его будущий рефактор — тест покраснеет, и это ровно тот
        случай, ради которого § 1.3 отделяет ключ workstream-а от
        `tdd.resolve_namespace` (тот хеширует абсолютный путь намеренно).
        """
        params = inspect.signature(workstream_key).parameters
        assert set(params) == {"repository_remote", "root_commit", "spec_prefix", "change_id"}
        for name, param in params.items():
            annotation = str(param.annotation)
            assert "Path" not in annotation, f"{name}: путь не может быть входом ключа workstream-а"


class TestLocalVolumeStoreCapabilities:
    def test_declares_what_the_config_gate_reads(self):
        """BEH-28 судит адаптер по декларации, а не по поведению (OUT-03):
        локальный том не даёт TLS, шифрование объявляет оператор, lifecycle нет."""
        caps = LocalVolumeStore(Path("/tmp/x")).capabilities()
        assert isinstance(caps, StoreCapabilities)
        assert caps.tls is False
        assert caps.immutable_put is True
        assert caps.lifecycle == "none"

    def test_encryption_at_rest_comes_from_operator_options(self):
        assert LocalVolumeStore(Path("/tmp/x")).capabilities().encryption_at_rest is False
        declared = LocalVolumeStore(Path("/tmp/x"), encryption_at_rest=True)
        assert declared.capabilities().encryption_at_rest is True


class TestLocalVolumeStorePutIsImmutable:
    def test_put_then_get_returns_the_bytes(self, tmp_path: Path):
        store = LocalVolumeStore(tmp_path)
        store.put("runs/R1/run-start.json", b'{"a":1}', metadata={"run_id": "R1"})
        assert store.get("runs/R1/run-start.json") == b'{"a":1}'

    def test_second_put_on_the_same_key_is_refused_and_the_first_survives(self, tmp_path: Path):
        """BEH-25: immutability — это отказ на существующем ключе, а не версия."""
        store = LocalVolumeStore(tmp_path)
        store.put("runs/R1/closure.json", b"first", metadata={})

        with pytest.raises(AlreadyExists):
            store.put("runs/R1/closure.json", b"second", metadata={})

        assert store.get("runs/R1/closure.json") == b"first"

    def test_refused_put_leaves_no_temporary_file_behind(self, tmp_path: Path):
        store = LocalVolumeStore(tmp_path)
        store.put("runs/R1/closure.json", b"first", metadata={})
        with pytest.raises(AlreadyExists):
            store.put("runs/R1/closure.json", b"second", metadata={})

        leftovers = [
            p.name for p in (tmp_path / "runs" / "R1").iterdir() if p.name != "closure.json"
        ]
        assert leftovers == [], f"временные файлы остались: {leftovers}"

    def test_get_of_an_absent_key_is_none_not_an_error(self, tmp_path: Path):
        assert LocalVolumeStore(tmp_path).get("runs/R1/nope.json") is None

    def test_put_fsyncs_both_the_data_and_the_name(self, tmp_path: Path, monkeypatch):
        """`fsync` до публикации ключа — иначе ack врёт: процесс, убитый сразу
        после `put`, оставил бы имя без содержимого.

        Синхронизаций обязано быть ДВЕ, и тест их различает: по файлу (иначе
        переживёт имя без данных) и по каталогу (иначе fsync данных ничего не
        обещает про саму ссылку). Прежняя редакция считала любой `fsync` и
        оставалась зелёной, когда синхронизацию данных убирали, — мутация это
        и показала.
        """
        synced_modes: list[int] = []
        real_fsync = os.fsync

        def _recording_fsync(fd: int) -> None:
            synced_modes.append(os.fstat(fd).st_mode)
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", _recording_fsync)

        LocalVolumeStore(tmp_path).put("runs/R1/run-start.json", b"x", metadata={})

        assert any(stat.S_ISREG(m) for m in synced_modes), (
            "данные не синхронизированы: ключ мог бы пережить падение пустым"
        )
        assert any(stat.S_ISDIR(m) for m in synced_modes), (
            "каталог не синхронизирован: имя ключа могло бы не пережить падение"
        )


class TestLocalVolumeStoreListAndDelete:
    def test_list_returns_keys_under_the_prefix_only(self, tmp_path: Path):
        store = LocalVolumeStore(tmp_path)
        store.put("runs/R1/run-start.json", b"a", metadata={})
        store.put("runs/R1/calls/C1/start.json", b"b", metadata={})
        store.put("runs/R2/run-start.json", b"c", metadata={})

        assert sorted(store.list("runs/R1/")) == [
            "runs/R1/calls/C1/start.json",
            "runs/R1/run-start.json",
        ]

    def test_list_is_sorted_so_sequence_order_is_the_key_order(self, tmp_path: Path):
        store = LocalVolumeStore(tmp_path)
        for seq in (100, 9, 10):
            store.put(checkpoint_key("R1", seq, "id", "manifest.json"), b"m", metadata={})

        listed = store.list("runs/R1/checkpoints/")
        assert listed == sorted(listed)
        assert listed[0].endswith("000009-id/manifest.json")

    def test_delete_removes_the_key_and_is_quiet_on_a_missing_one(self, tmp_path: Path):
        store = LocalVolumeStore(tmp_path)
        store.put("runs/R1/x.json", b"a", metadata={})
        store.delete("runs/R1/x.json")
        assert store.get("runs/R1/x.json") is None
        store.delete("runs/R1/x.json")


class TestReadOnlyDoor:
    def test_open_store_readonly_gives_a_store_that_refuses_to_write(self, tmp_path: Path):
        """§ 7.1: единственный Publisher-less вход, и он именно read-only —
        `evidence`/`restore` читают store, ничего в него не добавляя."""
        LocalVolumeStore(tmp_path).put("runs/R1/run-start.json", b"a", metadata={})

        ro = open_store_readonly("local_volume", {"root": str(tmp_path)})
        assert ro.get("runs/R1/run-start.json") == b"a"
        assert ro.list("runs/") == ["runs/R1/run-start.json"]

        with pytest.raises(PermissionError):
            ro.put("runs/R1/other.json", b"b", metadata={})
        with pytest.raises(PermissionError):
            ro.delete("runs/R1/run-start.json")

    def test_unknown_adapter_is_named_in_the_refusal(self, tmp_path: Path):
        with pytest.raises(ValueError, match="nosuch"):
            open_store_readonly("nosuch", {"root": str(tmp_path)})


def test_local_volume_store_satisfies_the_protocol():
    """Статически и в рантайме: адаптер подставим под протокол."""
    store: ArtifactStore = LocalVolumeStore(Path("/tmp/x"))
    assert isinstance(store, ArtifactStore)


class TestPutDoesNotTouchAnotherPublication:
    """Находка ревью: `finally`, начинавшийся до `os.open`, снимал чужой
    временный файл, если открытие своего не удалось."""

    def test_a_failing_open_leaves_a_concurrent_temporary_alone(self, tmp_path: Path, monkeypatch):
        store = LocalVolumeStore(tmp_path)
        target_dir = tmp_path / "runs" / "R1"
        target_dir.mkdir(parents=True)
        foreign = target_dir / ".closure.json.999.deadbeef.tmp"
        foreign.write_bytes(b"foreign unfinished publication")

        def _boom(*args, **kwargs):
            raise OSError("no fds")

        monkeypatch.setattr(os, "open", _boom)
        with pytest.raises(OSError):
            store.put("runs/R1/closure.json", b"mine", metadata={})

        assert foreign.exists(), "снят чужой временный файл при неудачном open"

    def test_two_puts_of_one_key_do_not_share_a_temporary_name(self, tmp_path: Path):
        """Один процесс, один ключ, два вызова: имена обязаны различаться,
        иначе второй `put` удалит временный файл первого."""
        store = LocalVolumeStore(tmp_path)
        seen: list[str] = []
        real_open = os.open

        def _recording_open(path, flags, mode=0o777, **kw):
            seen.append(str(path))
            return real_open(path, flags, mode, **kw)

        import unittest.mock as mock

        with mock.patch.object(os, "open", _recording_open):
            store.put("runs/R1/a.json", b"1", metadata={})
            with pytest.raises(AlreadyExists):
                store.put("runs/R1/a.json", b"2", metadata={})

        tmps = [s for s in seen if s.endswith(".tmp")]
        assert len(tmps) == 2 and tmps[0] != tmps[1], f"временные имена совпали: {tmps}"


class TestRuntimeStatePathsCoverTheStoreLocals:
    """DT-01 требует свойства путей «здесь же, чтобы каждая следующая задача
    брала путь из config, а не из литерала»."""

    def test_checkpoints_and_spool_follow_the_state_db_namespace(self, tmp_path: Path):
        from spec_runner.config import ExecutorConfig

        cfg = ExecutorConfig(project_root=tmp_path, spec_prefix="ws-")
        assert cfg.checkpoints_dir.name == ".executor-ws-checkpoints"
        assert cfg.spool_file.name == ".executor-ws-spool.jsonl"
        assert cfg.checkpoints_dir.parent == cfg.state_file.parent

    def test_both_are_runtime_state_and_never_committed(self, tmp_path: Path):
        from spec_runner.config import ExecutorConfig
        from spec_runner.git_ops import runtime_state_paths

        cfg = ExecutorConfig(project_root=tmp_path)
        paths = runtime_state_paths(cfg)
        assert cfg.checkpoints_dir in paths, "локальные копии checkpoint-ов попали бы в коммит"
        assert cfg.spool_file in paths, "аварийный spool попал бы в коммит"
