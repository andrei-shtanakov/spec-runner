"""Store-контракт, ключи публикуемых артефактов и первый адаптер.

Design § 1.1–1.3 бандла `durable-continuation-checkpoint-evidence-20260915`
(spec-runner#480). Здесь живёт ровно граница «наружу»: протокол, которым
пользуются publisher и read-surface, форма ключей и один адаптер поверх
управляемого тома.

Три свойства, ради которых модуль отдельный:

1. **Каждый ключ пишется один раз.** `put` всегда идёт с семантикой
   `if_none_match`: существующий ключ — отказ `AlreadyExists`, а не
   перезапись. Это и есть immutability (BEH-25): «исправление» — новый ключ
   с полем `supersedes`, а первая запись остаётся читаемой байт в байт.
2. **Возможности адаптера объявлены, а не выведены.** `StoreCapabilities`
   читает загрузчик config и отказывает небезопасному адаптеру ДО первого
   платного вызова (BEH-28). spec-runner проверяет декларацию, а не
   реализацию (OUT-03): доказать шифрование в покое он всё равно не может.
3. **Ключ workstream-а переживает переезд каталога.** Абсолютный путь в него
   не входит намеренно — в отличие от `tdd.resolve_namespace`, который
   хеширует именно путь. Найти workstream надо как раз после переезда, ради
   которого весь механизм и существует.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4


class AlreadyExists(Exception):
    """Ключ уже опубликован — вторая запись отвергнута (BEH-25).

    Не ошибка вызывающего в обычном смысле: публикация идемпотентна по
    построению, и повтор означает либо ретрай доставки, либо попытку
    переписать историю. Различать их — дело вызывающего, но молча заменять
    содержимое store не вправе.
    """


@dataclass(frozen=True)
class StoreCapabilities:
    """Что адаптер ОБЪЯВЛЯЕТ о себе (§ 1.1).

    `lifecycle` — строка, а не флаг: у адаптеров она разная («none» у тома,
    имя политики у объектного хранилища), и загрузчику важно её предъявить
    оператору, а не свести к «да/нет».
    """

    tls: bool
    encryption_at_rest: bool
    immutable_put: bool
    lifecycle: str


@dataclass(frozen=True)
class Ack:
    """Подтверждение публикации: то, на что вправе сослаться closure.

    Несёт ключ и размер — ровно то, что известно store-у и проверяемо
    читателем. Времени здесь нет намеренно: часы адаптера и часы прогона —
    разные часы, а порядок задаёт `sequence` в ключе.
    """

    key: str
    size: int


@runtime_checkable
class ArtifactStore(Protocol):
    """Граница «наружу» для всего, что публикуется (§ 1.1)."""

    def put(self, key: str, data: bytes, *, metadata: dict[str, str]) -> Ack:
        """Опубликовать ключ. Существующий — `AlreadyExists`, всегда."""
        ...

    def get(self, key: str) -> bytes | None:
        """Содержимое ключа либо None, если его нет."""
        ...

    def list(self, prefix: str) -> list[str]:
        """Ключи под префиксом, отсортированные."""
        ...

    def delete(self, key: str) -> None:
        """Удалить ключ; отсутствующий — не ошибка."""
        ...

    def capabilities(self) -> StoreCapabilities:
        """Объявленные свойства адаптера."""
        ...


# --- ключи § 1.3 -----------------------------------------------------------
#
# Одно место на весь репозиторий: читатель и писатель обязаны считать ключ
# одинаково, а «почти та же строка» в двух местах — это способ разойтись
# молча.


def run_start_key(run_id: str) -> str:
    return f"runs/{run_id}/run-start.json"


def call_start_key(run_id: str, call_id: str) -> str:
    return f"runs/{run_id}/calls/{call_id}/start.json"


def call_result_key(run_id: str, call_id: str) -> str:
    return f"runs/{run_id}/calls/{call_id}/result.json"


def closure_key(run_id: str) -> str:
    return f"runs/{run_id}/closure.json"


def attempt_key(run_id: str, task_id: str, attempt: int) -> str:
    return f"runs/{run_id}/attempts/{task_id}-{attempt}.jsonl"


def checkpoint_key(run_id: str, sequence: int, checkpoint_id: str, filename: str) -> str:
    """Файл checkpoint-а. `sequence` дополняется до шести цифр.

    Ширина не косметика: читатели берут checkpoint-ы через `list(prefix)`, а
    он отдаёт ключи лексикографически. Без дополнения `10` встало бы раньше
    `9`, и «последний acknowledged» оказался бы не последним.
    """
    return f"runs/{run_id}/checkpoints/{sequence:06d}-{checkpoint_id}/{filename}"


def deletions_key(run_id: str, timestamp: str) -> str:
    return f"runs/{run_id}/deletions/{timestamp}.json"


def task_history_key(run_id: str) -> str:
    return f"runs/{run_id}/task-history.log"


def audit_log_key(run_id: str) -> str:
    return f"runs/{run_id}/audit-log.jsonl"


def workstream_index_key(
    workstream: str, started_at: str, run_id: str, *, closed: bool = False
) -> str:
    """Запись прогона в индексе workstream-а — единственное, что не адресуется
    `run_id`-ом (§ 1.3).

    `started_at` стоит перед `run_id` затем же, зачем `sequence` дополняется:
    порядок ключей обязан совпадать с порядком прогонов, потому что решение
    про open calls namespace-wide, а `run_id` масштаба workstream-а не имеет.
    """
    suffix = "closed" if closed else "json"
    return f"workstreams/{workstream}/runs/{started_at}-{run_id}.{suffix}"


def workstream_key(
    repository_remote: str, root_commit: str, *, spec_prefix: str, change_id: str
) -> str:
    """SHA-256 от repository identity (§ 4.2) и namespace-полей.

    Абсолютного пути здесь нет намеренно: `tdd.resolve_namespace` хеширует
    именно путь, и на другой машине дал бы другое имя — а найти надо тот же
    workstream ровно после переезда.
    """
    payload = "\n".join((repository_remote, root_commit, spec_prefix, change_id))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- адаптер: управляемый том (§ 1.2) --------------------------------------


class LocalVolumeStore:
    """Первый адаптер: каталог на диске.

    `put` — временный файл рядом с целью, `fsync`, затем `os.link`: у него
    ровно `O_EXCL`-семантика, то есть отказ на существующем ключе выполняет
    сам примитив, а не проверка «а есть ли файл» перед записью (между такой
    проверкой и записью помещается чужая публикация).
    """

    def __init__(self, root: Path | str, *, encryption_at_rest: bool = False) -> None:
        self.root = Path(root)
        self._encryption_at_rest = encryption_at_rest

    def capabilities(self) -> StoreCapabilities:
        """`tls` неприменим к локальному пути, шифрование объявляет оператор."""
        return StoreCapabilities(
            tls=False,
            encryption_at_rest=self._encryption_at_rest,
            immutable_put=True,
            lifecycle="none",
        )

    def _path(self, key: str) -> Path:
        return self.root / key

    def put(self, key: str, data: bytes, *, metadata: dict[str, str]) -> Ack:
        """Опубликовать ключ одним неделимым шагом.

        `metadata` адаптером тома не хранится: держать его в отдельном файле
        значило бы завести второй ключ на ту же запись и потерять
        одноразовость. Облачный адаптер отдаст его своему lifecycle — за тем
        поле и есть в протоколе.
        """
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Уникальность имени — по публикации, не по процессу: у двух `put`
        # одного ключа в одном процессе PID совпадает, и общий временный файл
        # сделал бы их гонкой. `O_EXCL` ниже доказывает, что файл наш.
        tmp = target.parent / f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        # `finally` начинается ЗДЕСЬ, а не выше: до успешного `os.open` файла
        # с этим именем не существует либо он чужой, и снимать его нельзя —
        # это удалило бы чужую незавершённую публикацию.
        try:
            try:
                # `os.write` вправе записать МЕНЬШЕ запрошенного, и молча: без
                # цикла короткая запись опубликовала бы усечённый ключ — а он
                # неизменяем, то есть исправить его нельзя по построению, и
                # `Ack` при этом сообщал бы полный размер.
                written = 0
                view = memoryview(data)
                while written < len(data):
                    chunk = os.write(fd, view[written:])
                    if chunk <= 0:
                        raise OSError(f"короткая запись в {tmp}: {written} из {len(data)} байт")
                    written += chunk
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(tmp, target)
            except FileExistsError as exc:
                raise AlreadyExists(key) from exc
            self._fsync_dir(target.parent)
        finally:
            tmp.unlink(missing_ok=True)
        return Ack(key=key, size=len(data))

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        """Имя тоже надо закрепить: fsync файла не обещает, что ссылка на него
        переживёт падение машины."""
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return path.read_bytes()

    def list(self, prefix: str) -> list[str]:
        if not self.root.is_dir():
            return []
        keys = [
            p.relative_to(self.root).as_posix()
            for p in self.root.rglob("*")
            if p.is_file() and not p.name.endswith(".tmp")
        ]
        return sorted(k for k in keys if k.startswith(prefix))

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class _ReadOnlyStore:
    """Обёртка двери § 7.1: читает, но писать не даёт.

    Отказ здесь — не вежливость, а граница: `evidence` и `restore` обязаны
    быть безопасны для чужого прогона, и единственный вход в store без
    `Publisher` не должен уметь в него добавить.
    """

    def __init__(self, inner: ArtifactStore) -> None:
        self._inner = inner

    def capabilities(self) -> StoreCapabilities:
        return self._inner.capabilities()

    def get(self, key: str) -> bytes | None:
        return self._inner.get(key)

    def list(self, prefix: str) -> list[str]:
        return self._inner.list(prefix)

    def put(self, key: str, data: bytes, *, metadata: dict[str, str]) -> Ack:
        raise PermissionError(f"read-only store: {key} не может быть записан этой дверью")

    def delete(self, key: str) -> None:
        raise PermissionError(f"read-only store: {key} не может быть удалён этой дверью")


#: Адаптеры, известные по имени в `durability.store.adapter`.
ADAPTERS = ("local_volume",)


def _build_store(adapter: str, options: dict[str, str]) -> ArtifactStore:
    """Собрать адаптер по объявлению config-а — ПРИВАТНО.

    Имя с подчёркиванием — не стиль, а половина пояса § 1.4: правило «в store
    пишет только publisher» проверяется поиском публичных входов, и пишущая
    фабрика с публичным именем была бы вторым `Publisher`-less входом рядом с
    объявленным единственным (`open_store_readonly`). Publisher инстанцирует
    адаптер через неё же, изнутри модуля.

    Путеподобные options к этому моменту уже абсолютны — их разрешает
    загрузчик (§ 1.1), и повторять резолв здесь нельзя: он пришёлся бы на
    CWD процесса, который вправе её сменить.
    """
    if adapter != "local_volume":
        raise ValueError(f"неизвестный адаптер store: {adapter!r}; известны: {', '.join(ADAPTERS)}")
    root = options.get("root")
    if not root:
        raise ValueError("адаптер local_volume требует options.root")
    declared = str(options.get("encryption_at_rest", "")).lower() in {"1", "true", "yes"}
    return LocalVolumeStore(Path(root), encryption_at_rest=declared)


def open_store_readonly(adapter: str, options: dict[str, str]) -> ArtifactStore:
    """Единственный `Publisher`-less вход в store (design § 7.1).

    Назван так, чтобы статический пояс § 1.4 мог допустить его **по имени**:
    правило «в store пишет только publisher» иначе пришлось бы проверять
    рассуждением, а не поиском.
    """
    return _ReadOnlyStore(_build_store(adapter, options))
