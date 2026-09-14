---
spec_stage: acceptance
status: draft
owner_role: qa
traces_to: [requirements, behaviour-spec]
upstream_hashes: {requirements: "7d2e095dda2274fe8abd8e47acf58f9f4789c76d", behaviour-spec: "eb6f3a72f3e72a69f13189ab6ea354b318407d3f"}
---

# Acceptance — MCP launch scope (spec-runner#485)

Стадия `acceptance` governance-бандла
`workstreams/mcp-launch-scope-spec-runner-485-20260914/`. Единственный источник
критериев приёмки workstream-а: критерии составлены заново от требований
(`10-requirements.md`, FR-01…FR-09, NFR-01…NFR-03) и сценариев поведения
(`15-behaviour-spec.md`, BEH-01…BEH-28); нумерация критериев чартера сюда не
переносится. Термины — в значении upstream'а (§3 требований): **launch scope**,
**плоский запуск**, **противоречащий `spec_prefix`**, **effective config**,
**представимое / непредставимое поле**, **ready**, **stop-marker**.

Входной набор Must-требований: FR-01, FR-02, FR-03, FR-04, FR-05, FR-06,
NFR-01, NFR-02, NFR-03. Should-требования FR-07, FR-08, FR-09 покрыты по
усмотрению qa (FR-09 — с оговоркой о вырезании, см. AC-20).

Правило чтения критерия: `verification: test` — критерий доказан зелёными
сценариями из `scenarios`; `verification: manual` — прозой названо, что
наблюдает человек; `verification: metric` — прозой назван источник числа,
само число в критерии не зашито.

## Критерии приёмки

### A. Один launch scope для всех восьми tools

#### AC-01: Все восемь tools обслуживают launch scope, а не CWD сервера · verification: test
traces: [FR-01]
scenarios: [BEH-01, BEH-02, BEH-04]

Наблюдаемый знак: сервер, запущенный из чужой директории с
`--project-root <external> --change add-x`, отвечает на `status`, `tasks`,
`next_tasks`, `task_detail`, `costs`, `logs` содержимым
`<external>/spec/changes/add-x/tasks.md` (ровно две задачи change; задача
плоского `spec/` не находится), `stop` пишет marker под тот же change, а
governance-гейт `run_task` берётся из `<external>/spec-runner.config.yaml`
(или legacy `<external>/spec/executor.config.yaml`), а не из YAML CWD
сервера — зеркальные пары `strict`/`off` дают отказ и запуск соответственно.
Двойник `_build_config`, поднимающий исключение при любом вызове, не
срабатывает ни на одном из восьми tools с пустым tool-level `spec_prefix`.

#### AC-02: Чтение не оставляет следов ни в scope, ни вне его · verification: test
traces: [FR-01, NFR-01]
scenarios: [BEH-03]

Наблюдаемый знак: после вызова всех шести read-tools (включая вызовы по
несуществующей задаче) на сервере без существующей state DB `rglob(".executor-*")`
по плоскому `<external>/spec/` и по CWD сервера пуст, и в
`<external>/spec/changes/add-x/` не появилось ни state DB, ни lock-, stop- или
ready-файла.

### B. Противоречащий tool-level `spec_prefix`

#### AC-03: Противоречащий prefix отклоняется по имени и ничего не запускает · verification: test
traces: [FR-02]
scenarios: [BEH-05, BEH-06]

Наблюдаемый знак: `run_task("TASK-001", spec_prefix="other-")` на сервере с
`--change add-x` (и на сервере с `--spec-prefix p-`) отвечает JSON
`status: error`, текст которого называет обе стороны конфликта (`add-x` или
`p-` — и `other-`), без трейсбека; считающий двойник `Popen` не вызван ни разу;
следующий `status()` без prefix по-прежнему отвечает за launch namespace. Семь
остальных tools с `spec_prefix="other-"` отвечают тем же классом ошибки с тем
же называнием обеих сторон; state DB `other-` нигде не создана, `stop` не
написал marker ни в `other-`, ни в `add-x`.

#### AC-04: Совпадающий, пустой и уточняющий prefix принимаются · verification: test
traces: [FR-02]
scenarios: [BEH-07]

Наблюдаемый знак: `status(spec_prefix="p-")` на сервере `--spec-prefix p-`
и `status(spec_prefix="")` на любом сервере отвечают успехом в launch
namespace; при плоском запуске из `<external>` `status(spec_prefix="p-")`
отвечает успехом в namespace `p-` внутри `<external>` (рабочее допущение Q-01;
`project_root` остаётся `<external>`). Иное решение владельца по Q-01 меняет
ровно последний пункт этого критерия.

### C. `run_task` передаёт child точный scope и effective config

#### AC-05: Child живёт в `project_root`, в одном namespace, с тем же effective config · verification: test
traces: [FR-03]
scenarios: [BEH-08, BEH-09]

Наблюдаемый знак: argv child содержит `--project-root <external>` и ровно один
namespace-флаг (`--change add-x` без `--spec-prefix`, либо `--spec-prefix p-`
без `--change` — тот, с которым запущен сервер); `cwd` child равен
`<external>`; state DB, lock и лог child лежат под
`<external>/spec/changes/add-x/`. Resolved `ExecutorConfig`, записанный child
(тестовый entry point), равен родительскому launch scope по каждому
представимому полю (`max_retries`, `task_timeout_minutes`, tests/branch/
commit/review, integration PR, HITL, budget/task budget, callback URL,
log level/json) и по каждому непредставимому полю (`review_policy`,
`execution_mode`, `harness_guard`, `commands`), объявленному YAML
`<external>` не по дефолту. Сравниваются два resolved config-а, не строка
команды.

#### AC-06: Перечень serializer-а и парсера `common` не расходятся · verification: test
traces: [FR-03]
scenarios: [BEH-10]

Наблюдаемый знак: контрактный тест паритета зелёный — каждое представимое поле
serializer-а имеет ключ в `_COMMON_DEFAULTS`, а каждый ключ `_COMMON_DEFAULTS`
либо объявлен представимым, либо явно перечислен как намеренно непередаваемый
с причиной; добавление флага в `common` без правки serializer-а (или наоборот)
делает этот тест красным.

#### AC-07: Child запускается entry point-ом текущего окружения · verification: test
traces: [FR-03]
scenarios: [BEH-11]

Наблюдаемый знак: при `PATH` без исполняемого `spec-runner` `run_task` всё
равно возвращает `started`, lock содержит pid child, и argv[0] child указывает
внутрь текущего интерпретатора/venv (`sys.executable -m spec_runner` либо
console-script того же venv); версия и модули child совпадают с родительскими.

#### AC-08: Вывод child уходит в лог namespace, многословный child не зависает · verification: test
traces: [FR-03]
scenarios: [BEH-12]

Наблюдаемый знак: с fake command, печатающим объём, заведомо больший
pipe-буфера ОС (объём — из AP-04/RK-01 требований), child завершается в
пределах таймаута теста, хотя родитель после `started` ничего у него не
читает; лог child лежит под `<external>/spec/changes/add-x/` в
`config.logs_dir` и показывается tool-ом `logs`; вне namespace лога нет;
`subprocess.PIPE` для stdout/stderr не используется.

### D. Невоспроизводимый parent-config

#### AC-09: Невоспроизводимый config — отказ до `Popen`, воспроизводимый — ровно один запуск · verification: test
traces: [FR-04]
scenarios: [BEH-13, BEH-14]

Наблюдаемый знак: programmatic-config с override непредставимого поля
относительно YAML `<external>` (например `review_policy`, `execution_mode`)
или с удалённым между запуском сервера и `run_task` YAML даёт `status: error`,
называющую поле и причину (нет CLI-флага / YAML по `project_root` его не
объявляет); двойник `Popen` не вызван; в namespace нет state, lock, stop- и
ready-файлов. Config, все override которого представимы, при том же YAML даёт
`Popen` ровно один раз и ответ `started`; обе ветки решаются одной проверкой
воспроизводимости.

### E. Startup-handshake

#### AC-10: `started` приходит не раньше lock и ready; старый marker стёрт, новый — нет · verification: test
traces: [FR-05]
scenarios: [BEH-15, BEH-17]

Наблюдаемый знак: в момент получения ответа `started` lock-файл
`<external>/spec/changes/add-x/.executor-<…>state.db.lock` существует и
содержит pid, равный `pid` из ответа, ready уже опубликован (через интерфейс,
зафиксированный design), а двойник child, берущий lock с задержкой, задерживает
`started` на ту же величину. Marker, записанный `stop()` **до** `run_task`,
стёрт стартовым `clear_stop_file`, задача выполнена как обычно; marker,
записанный **после** `started`, остаётся на месте до потребления между
задачами; существующие тесты stop-семантики `run`/`watch`/`retry` проходят без
правок.

#### AC-11: Stop сразу после `started` не теряется · verification: test
traces: [FR-05, NFR-03]
scenarios: [BEH-16]

Наблюдаемый знак: на `tasks.md` change с двумя ready-задачами
`run_task("TASK-001")` → `started` → немедленный `stop()` дают state, в котором
`TASK-001` успешна, у второй задачи ни одной попытки и ни одного вызова fake
command; ответ `stop` — `stop_requested` со `stop_file` под
`<external>/spec/changes/add-x/`; child не убит, вышел сам после потребления
marker-а. Это одна итерация E2E, входящая в `-m "not slow"` (M-01).

#### AC-12: Таймаут ожидания ready объявлен и конфигурируем · verification: test
traces: [FR-05]
scenarios: [BEH-18]

Наблюдаемый знак: при таймауте, переопределённом на малое значение, двойник
child, публикующий ready позже него, получает `status: error` с причиной
`timeout` по истечении именно переопределённого значения; с дефолтом тот же
двойник, публикующий ready быстро, получает `started`; канал ready — файл в
namespace, наблюдаемый теми же средствами, что и остальные `.executor-*`
(нового IPC нет).

#### AC-13: Гарантия «stop после started» держится статистически · verification: test
traces: [NFR-03, FR-05]
scenarios: [BEH-19]

Наблюдаемый знак: soak под `@pytest.mark.slow` с числом итераций, объявленным
NFR-03, зелёный — в каждой итерации child завершился после текущей задачи, не
начав следующую, ноль потерянных stop; тест не входит в `-m "not slow"` и
запускается вручную (и в ночном профиле, если он есть). Evidence — ссылка на
прогон в PR.

### F. Child без lock или без ready

#### AC-14: Занятый lock, ранний выход и молчащий child дают `status: error`, не `started` · verification: test
traces: [FR-06]
scenarios: [BEH-20, BEH-21, BEH-22]

Наблюдаемый знак: (а) lock namespace, удерживаемый живым pid, → `status: error`,
называющая занятый lock (pid или путь), lock после ответа содержит прежний pid,
ответ раньше полного таймаута ready; (б) child с exit 1 на старте → `status:
error` с кодом выхода и хвостом лога из `logs_dir` namespace, без ожидания
таймаута, тот же класс ответа для governance-отказа и `ConfigError` внутри
child; (в) child, взявший lock и не опубликовавший ready, → по истечении
переопределённого таймаута `status: error` с причиной `timeout`, child
завершён родителем и lock освобождён либо ответ явно содержит его `pid`
(третьего варианта — живой child, о котором ответ молчит — нет). После любой
из веток в namespace нет ready-файла.

### G. Сквозные гарантии

#### AC-15: Ни один tool не пишет вне spec-директории launch scope, включая отказные ветки · verification: test
traces: [NFR-01, FR-01, FR-03, FR-04, FR-06]
scenarios: [BEH-26, BEH-06, BEH-13, BEH-22]

Наблюдаемый знак: после полного прогона сценариев BEH-01…BEH-22 на одном
`<external>` с `--change add-x`, включая противоречащий prefix,
невоспроизводимый config, занятый lock, ранний выход и таймаут ready,
`rglob(".executor-*")` по плоскому `<external>/spec/` и по CWD сервера
возвращает пустые списки, а все runtime-файлы child и сервера (state, lock,
stop, ready, лог) перечислены под `<external>/spec/changes/add-x/`.

#### AC-16: Тесты не вызывают платного агента и базовый E2E входит в `not slow` · verification: test
traces: [NFR-02]
scenarios: [BEH-27]

Наблюдаемый знак: прогон `uv run pytest tests/ -q -m "not slow"` при активном
conftest-поясе `PaidBinaryReached` не поднимает его ни в одном тесте; базовый
E2E #485 (BEH-15, BEH-16, BEH-17) не помечен `slow` и входит в этот прогон;
fake command child умеет три режима — успешное завершение задачи, многословный
вывод, exit 1 на старте.

#### AC-17: Базовый E2E укладывается в порог NFR-02 · verification: metric
traces: [NFR-02]
scenarios: [BEH-27]

Источник порога — NFR-02 требований (`10-requirements.md` §5); источник
измерения — длительность базового E2E #485 (BEH-15, BEH-16, BEH-17) из
отчёта `pytest --durations` в логе CI-прогона PR (не локальная оценка).
Критерий выполнен, когда измеренная длительность ниже порога, объявленного
NFR-02.

#### AC-18: Число tools, пересобирающих config из CWD, достигло target M-02 · verification: metric
traces: [FR-01]
scenarios: [BEH-04]

Источник baseline и target — метрика M-02 требований (`10-requirements.md`
§8); источник измерения — счётчик срабатываний двойника `_build_config` в
контрактном тесте BEH-04 и статический `grep _build_config` по
tool-обработчикам `mcp_server.py` (вызовы, минующие holder). Критерий выполнен,
когда измеренное число равно target M-02 и модульная глобаль
`_launch_stop_config` отсутствует.

### H. Programmatic-контракт и синхронизация контракта

#### AC-19: `mcp_run_server()` без аргументов и lazy import работают как прежде · verification: test
traces: [FR-07]
scenarios: [BEH-23]

Наблюдаемый знак: `tests/test_lazy_mcp_import.py` зелёный без правок;
`spec_runner.mcp_run_server()` без аргументов из project root плоского
проекта заполняет holder config-ом из CWD, и все восемь tools работают в
плоском namespace; `import spec_runner` без установленного `mcp` не падает;
плоский запуск `spec-runner mcp` обслуживается тем же holder-ом.

#### AC-20: `status` и `task_detail` называют обслуживаемый scope · verification: test
traces: [FR-09]
scenarios: [BEH-25]

Наблюдаемый знак: на серверах `--change add-x`, `--spec-prefix p-` и при
плоском запуске оба ответа содержат `project_root`, равный resolved
абсолютному пути `<external>`, и `namespace`, различающий три случая (точная
форма — по design); все существующие ключи ответов сохранены с прежними
значениями (`TestMCPStatus`, `TestMCPTaskDetail` без правок). FR-09 — Should
(в источнике Could): при вырезании владельцем критерий снимается целиком с
записью в CHANGELOG-заметке PR; ни один другой AC на него не опирается.

#### AC-21: Существующие MCP-тесты меняют ожидания только по контракту · verification: test
traces: [FR-02, FR-05]
scenarios: [BEH-28]

Наблюдаемый знак: `tests/test_mcp.py` (все классы `TestMCP*`) и
`tests/test_mcp_v2_wire.py` зелёные; diff ожиданий существующих тестов
ограничен двумя следствиями контракта — `started` только после ready (двойники
`Popen` в `TestMCPRunTask` имитируют handshake) и отказ на противоречащем
prefix; любое иное изменённое ожидание — находка ревью PR.

#### AC-22: README, CHANGELOG и #485 говорят то же, что код · verification: manual
traces: [FR-08]
scenarios: [BEH-24]

Что наблюдает ревьюер PR: в README §MCP Server — привязка сервера к launch
scope и требование запуска (`--project-root` + один namespace), отказ на
противоречащем tool-level `spec_prefix` как видимое изменение контракта,
значение `started` (child держит lock, ready опубликован, stop после него не
теряется), новый ключ таймаута ready и `.executor-ready` в инвентаре
runtime-файлов; в CHANGELOG — запись под Unreleased со ссылкой на #485; #485
закрыт ссылкой на PR, в закрывающем комментарии — ссылка на E2E BEH-16;
соседям (devtools, spec-runner-vscode) контракт объявлен issue с
`slug:` + `from:` (ADR-ECO-006) либо записью в
`../prograph-vault/authored/notes/`, файлы соседей не тронуты.

## Инварианты покрытия

Инварианты, которые qa проверяет над этим документом (нарушение любого —
документ не готов к approve):

1. **Каждое Must-требование покрыто ≥ 1 AC.** FR-01 → AC-01, AC-02, AC-15,
   AC-18; FR-02 → AC-03, AC-04, AC-21; FR-03 → AC-05, AC-06, AC-07, AC-08,
   AC-15; FR-04 → AC-09, AC-15; FR-05 → AC-10, AC-11, AC-12, AC-13, AC-21;
   FR-06 → AC-14, AC-15; NFR-01 → AC-02, AC-15; NFR-02 → AC-16, AC-17;
   NFR-03 → AC-11, AC-13.
2. **Should-требования покрыты по усмотрению qa, все три.** FR-07 → AC-19;
   FR-08 → AC-22; FR-09 → AC-20 (условный, см. порог).
3. **Каждый AC трассирует ≥ 1 требование**, идентификаторы — только из
   `10-requirements.md`; новых FR/NFR здесь не вводится.
4. **Каждый `verification: test` несёт `scenarios`** с идентификаторами только
   из `15-behaviour-spec.md`; новых BEH здесь не вводится. Обратно — каждый
   сценарий BEH-01…BEH-28 упомянут хотя бы одним AC: BEH-01/02/04 → AC-01
   (и BEH-04 → AC-18); BEH-03 → AC-02; BEH-05/06 → AC-03 (BEH-06 → AC-15);
   BEH-07 → AC-04; BEH-08/09 → AC-05; BEH-10 → AC-06; BEH-11 → AC-07;
   BEH-12 → AC-08; BEH-13/14 → AC-09 (BEH-13 → AC-15); BEH-15/17 → AC-10;
   BEH-16 → AC-11; BEH-18 → AC-12; BEH-19 → AC-13; BEH-20/21/22 → AC-14
   (BEH-22 → AC-15); BEH-23 → AC-19; BEH-24 → AC-22; BEH-25 → AC-20;
   BEH-26 → AC-15; BEH-27 → AC-16, AC-17; BEH-28 → AC-21.
5. **Каждый AC называет наблюдаемый знак**, а не пересказывает требование:
   ответ tool, вызов/невызов `Popen`, файл в namespace, pid в lock, число
   начатых задач, код выхода, длительность прогона — то, что перечислено в
   «Области поведения» behaviour-спеки как наблюдаемое.
6. **Числа не зашиты в metric-критерии.** AC-17 берёт порог из NFR-02, AC-18 —
   baseline/target из M-02; изменение числа в upstream'е не требует правки
   этого документа.
7. **Метрики требований предъявлены.** M-01 — AC-11 (зелёный E2E
   `run_task → started → stop` на внешнем project root с `--change`);
   M-02 — AC-18.
8. **Резолюции design не противоречат критериям.** Q-02 (ready — файл
   `<spec_dir>/.executor-ready`; child при таймауте завершается родителем) —
   AC-10, AC-12, AC-14 сформулированы через факт, допускающий эту резолюцию;
   Q-03 (обратной сверки в runtime нет, доказательство — E2E BEH-09) — AC-05
   есть эта проверка, отдельного AC «config mismatch» нет намеренно.

## Порог приёмки

Workstream объявляется delivered, когда одновременно:

- **Все `verification: test`, кроме AC-13 и AC-20,** зелёные в CI на PR в
  прогоне `uv run pytest tests/ -q -m "not slow"`: AC-01, AC-02, AC-03, AC-04,
  AC-05, AC-06, AC-07, AC-08, AC-09, AC-10, AC-11, AC-12, AC-14, AC-15, AC-16,
  AC-19, AC-21.
- **AC-13** (soak, `slow`) пройден вручную хотя бы один раз; ссылка на прогон
  приложена к PR (условие §11 требований).
- **AC-20** (FR-09, Should/Could): либо зелёный в том же CI-прогоне, либо FR-09
  явно вырезан решением владельца с записью в CHANGELOG-заметке PR — третьего
  состояния («не реализован и не вырезан») порог не допускает.
- **`verification: metric` — по перечислению:** AC-17 (длительность базового
  E2E из CI ниже порога NFR-02), AC-18 (число tools, пересобирающих config из
  CWD, равно target M-02).
- **`verification: manual` — по перечислению:** AC-22 подтверждён ревью PR
  (терминальный прогон от ai-prosto по правилу репо), #485 закрыт.

Любой красный критерий из перечисленных, красный обязательный CI-чек или
неприбывшее ревью держат workstream в состоянии «не delivered»; частичная
приёмка (например, только read-tools без handshake) не предусмотрена — G-03
требует доверия к каналу целиком.

## Вне объёма

Намеренно не является критерием приёмки:

- **Семантика остановки за пределами стартового handshake** (OUT-01): stop
  остаётся файл-маркером, executor не убивается, три точки потребления в
  `run`/`watch`/`retry` не переписываются — AC-10 проверяет лишь, что они не
  изменились, а не их поведение.
- **Сетевой транспорт и аутентификация MCP** (OUT-02); stdio-only.
- **Argv-контракт spec-runner-vscode и жизненный цикл команды `change`**
  (OUT-03).
- **Новые write-tools MCP** помимо `run_task` и `stop` (OUT-04).
- **Новый IPC для ready** (сокет, pipe-протокол) — AC-12 требует, напротив,
  его отсутствия.
- **Правка соседних репозиториев** (devtools, spec-runner-vscode) — только
  issue/handoff (часть AC-22).
- **Обратная сверка effective config child родителем в runtime** («config
  mismatch», Q-03 резолвлен design как «нет»); совпадение доказывается
  до-`Popen` симуляцией и E2E в AC-05.
- **Внутренние параметры handshake**: интервал опроса `wait_for_ready`,
  длительность ожидания между SIGTERM и SIGKILL, дефолтное значение таймаута
  ready — AC-12 проверяет объявленность и конфигурируемость, не величину.
- **Точная форма поля `namespace`** в ответах `status`/`task_detail` — AC-20
  требует лишь различимости трёх случаев.
- **Точные формулировки текстов ошибок** — AC-03, AC-09, AC-14 требуют
  называния сторон конфликта / имени поля / кода выхода / слова `timeout`, не
  дословного текста.
- **Имена тестовых файлов и классов** — `checked_by` behaviour-спеки и §9
  требований являются ожиданием; design вправе переименовать при сохранении
  покрытия сценариев.
- **Производительность самого MCP-сервера** (латентность ответов tools) — не
  измеряется; единственная временная метрика — AC-17.
- **Работа под несколькими namespace одним сервером** — по определению launch
  scope сервер обслуживает ровно один; уточняющий prefix при плоском запуске
  (AC-04) — рабочее допущение Q-01, а не мульти-scope.
