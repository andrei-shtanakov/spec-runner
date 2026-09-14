---
spec_stage: requirements
status: draft
owner_role: product
traces_to:
- charter
upstream_hashes:
  charter: "3c29f5150cfca9a49122329f2314e7d3490b16db"
---

# Requirements — MCP launch scope (spec-runner#485)

Стадия `requirements` governance-бандла
`workstreams/mcp-launch-scope-spec-runner-485-20260914/`. Уточняет требования
чартера до проверяемых утверждений: каждое FR/NFR несёт приоритет, описание,
критерии приёмки и трассировку к целям, персонам, jobs и метрикам customer-брифа.
Идентификаторы FR-01…FR-09 и NFR-01…NFR-03 взяты из
`00-discovery/discovery-brief-customer.md` (approved 2026-09-14) без изменений;
уточнения (S-/IF-/AP-/RK-) — из engineer-брифа `00-discovery/brief.md`.

## 1. Постановка

`spec-runner mcp` запускается для одного scope — `project_root` плюс ровно один
namespace (`--change <id>` либо `--spec-prefix <p>`) — и обязан читать, запускать
и останавливать только его. Сегодня launch-контекст доступен одному tool из
восьми (`stop`, через `_launch_stop_config` после #481/#484); остальные семь
пересобирают плоский config через `_build_config(spec_prefix)` относительно CWD
сервера, а `run_task` спавнит child без `cwd`, `--project-root` и `--change` и
отвечает `started` в момент `Popen` — до lock и до `clear_stop_file`.

Наблюдаемый дефект (#485): сервер с `--change add-x` → `run_task("TASK-001")`
запускает executor в default namespace → `stop()` пишет per-change marker →
executor его не видит. Немедленный `stop` после `started` теряется и в
правильном namespace: стартовый `clear_stop_file` (`cli.py:831`) стирает marker,
записанный между `Popen` и стартом child.

Форма решения задана владельцем (customer CON-04): **один типизированный
launch-scope holder для всех восьми tools, один serializer effective config в
команду child, startup-handshake до ответа `started`**.

## 2. Цели, персоны, jobs (из customer-брифа)

### Цели

- **G-01** MCP-сервер, запущенный для одного scope, читает, запускает и
  останавливает исключительно этот scope; ни один tool не пересобирает плоский
  config из CWD.
- **G-02** Запрос stop, поданный после ответа `started`, никогда не теряется.
- **G-03** MCP как канал управления executor-ом заслуживает доверия: ни один
  tool не сообщает `started` или `stop_requested` без наблюдаемого эффекта в
  заявленном scope.

### Персоны

- **P-01** Оператор spec-runner в Claude Code (владелец репо) — primary.
- **P-02** Программный вызывающий `mcp_run_server` (fleet-агент devtools,
  spec-runner-vscode) — неизменный programmatic-контракт и lazy import.
- **P-03** Соседний репозиторий или namespace на той же машине — страдающая
  сторона неверно адресованного executor-а.

### Jobs-to-be-done

- **J-01** Запустив MCP-сервер для внешнего проекта с `--change`, вызывать
  status/tasks/next_tasks/task_detail/costs/logs и видеть именно этот change.
  traces: [G-01]
- **J-02** Вызвав `run_task` на этом scope, получить child в том же project root
  и namespace с теми же настройками безопасности — бюджет и правки ложатся туда,
  где запущен сервер. traces: [G-01, G-03]
- **J-03** Вызвав `stop` сразу после `started`, быть уверенным, что работающий
  executor завершит текущую задачу и выйдет. traces: [G-02, G-03]

## 3. Термины

- **Launch scope** — resolved `ExecutorConfig`, собранный `cmd_mcp` из YAML +
  CLI-флагов `spec-runner mcp`: `project_root` и ровно один из `change_id` /
  `spec_prefix` (взаимоисключение проверяется дважды — `SystemExit` в CLI и
  `ConfigError` в `__post_init__`, engineer CON-04).
- **Плоский запуск** — сервер без `--change` и без `--spec-prefix`: namespace =
  плоский `spec/`.
- **Противоречащий `spec_prefix`** — tool-level аргумент `spec_prefix`, который
  (а) непуст при запуске сервера с `--change`, либо (б) непуст и не равен
  launch `spec_prefix` при запуске с `--spec-prefix`. Пустой tool-level prefix
  противоречием не является. Случай «плоский запуск + непустой tool-level
  prefix» — см. Q-01.
- **Effective config** — resolved `ExecutorConfig` после приоритета
  CLI-флаг > YAML > дефолт dataclass (engineer IF-05).
- **Представимое поле** — поле effective config, для которого у парсера `common`
  (`cli.py`, `_COMMON_DEFAULTS`) есть CLI-флаг: `--project-root`,
  `--change` | `--spec-prefix`, `--max-retries`, `--timeout`, `--no-tests`,
  `--no-branch`, `--no-commit`, `--no-review`, `--integration-pr`,
  `--hitl-review`, `--budget`, `--task-budget`, `--callback-url`, `--log-level`,
  `--log-json` (engineer S-04, AP-02).
- **Непредставимое поле** — поле effective config без CLI-флага
  (`spec_governance`, `review_policy`, `execution_mode`, `harness_guard`,
  `commands`, `personas`, …): child получает его только из того же YAML,
  прочитанного с `cwd = project_root`.
- **Ready** — сигнал child, опубликованный после `_acquire_run_lock` и
  `clear_stop_file` (`cli.py:179`, `:831`), означающий «lock взят в заявленном
  namespace, старый stop-marker стёрт, новый marker стёрт не будет». Канал —
  см. Q-02.
- **Stop-marker** — `<spec_dir>/.executor-stop` (engineer IF-03); контракт не
  меняется (OUT-01).

## 4. Функциональные требования

#### FR-01: Все MCP tools берут config из launch scope сервера

**Priority**: Must

Все восемь tools (`status`, `tasks`, `costs`, `logs`, `next_tasks`,
`task_detail`, `run_task`, `stop`) получают `ExecutorConfig` из launch scope
сервера (`project_root` + ровно один namespace: `change_id` либо `spec_prefix`),
переданного `spec-runner mcp`. Ни один tool не пересобирает config из CWD
процесса.

Уточнения:

- Один типизированный holder (engineer AP-01) заменяет модульную глобаль
  `_launch_stop_config`; все восемь tools проходят через один helper. Прямых
  вызовов `_build_config` из tool-обработчиков не остаётся (M-02: 7/8 → 0/8).
- Поиск YAML родителем ведётся по `project_root` launch scope, а не по CWD
  процесса (engineer CON-03, S-03): сервер, запущенный из чужой директории с
  `--project-root <external>`, читает `<external>/spec-runner.config.yaml`
  (или legacy `<external>/spec/executor.config.yaml`), а не YAML своей CWD и
  не дефолты. Это условие доказуемости FR-03 («child читает тот же YAML»).
- Read-tools открывают state через `ExecutorState.for_read` (#337) — чтение не
  создаёт state DB в scope и тем более вне его.

**Acceptance**:

- Сервер запущен с `--project-root <external> --change add-x` из директории,
  не являющейся `<external>`: `status`/`tasks`/`next_tasks`/`task_detail`
  читают `<external>/spec/changes/add-x/tasks.md`.
- После вызова всех read-tools в плоском `<external>/spec/` и в CWD сервера не
  появляется ни одного `.executor-*` (state, lock, stop, ready, log).
- `grep _build_config` по tool-обработчикам `mcp_server.py` даёт 0 вызовов,
  минующих holder; статический или тестовый факт, что все 8 tools проходят
  через один helper.
- Родитель с `--project-root <external>`, где `<external>` объявляет
  `spec_governance: strict`, применяет strict-гейт в `run_task` даже если в
  CWD сервера YAML отсутствует или объявляет `off` (engineer RK-02).

traces: [G-01, J-01, P-01, P-03, M-02, AP-01, CON-03(engineer)]

#### FR-02: Противоречащий tool-level `spec_prefix` — отказ, не переключение

**Priority**: Must

Tool-level `spec_prefix`, противоречащий launch scope (сервер запущен с
`--change`, либо с другим `--spec-prefix`), отклоняется явной ошибкой, называющей
конфликт (launch namespace и запрошенный prefix). Namespace никогда не
переключается молча. Предикат противоречия — один, в том же helper, что и
FR-01; применяется ко всем восьми tools, не только к `run_task`.

**Acceptance**:

- `run_task("TASK-001", spec_prefix="other-")` на сервере с `--change add-x` →
  ответ `status: error` с текстом, называющим `add-x` и `other-`; `Popen` не
  вызван (проверяется monkeypatch-двойником `Popen`, как в `TestMCPRunTask`).
- `status(spec_prefix="other-")` на том же сервере → тот же класс ошибки; state
  DB `other-` не создаётся.
- `status(spec_prefix="p-")` на сервере с `--spec-prefix p-` → успех
  (совпадающий prefix — не противоречие).
- Пустой tool-level `spec_prefix` на любом сервере → успех в launch namespace.
- Существующие тесты `TestMCPStop` (#484) проходят без изменения ожиданий,
  кроме тех, что прямо следуют из этого требования.

traces: [G-01, J-01, P-01, IF-01, AP-01]

#### FR-03: `run_task` передаёт child точный scope и effective config

**Priority**: Must

`run_task` запускает child так, что его effective config совпадает с
родительским:

- **Scope.** `--project-root <project_root>` и ровно один namespace
  (`--change <id>` либо `--spec-prefix <p>`); `cwd = project_root`.
- **Safety-настройки.** governance strictness, branch/commit/review, tests/lint,
  integration PR, HITL — представимые через `--no-tests`, `--no-branch`,
  `--no-commit`, `--no-review`, `--integration-pr`, `--hitl-review`;
  непредставимые (`spec_governance`, `review_policy`, `execution_mode`,
  `harness_guard`, `commands`) — через тот же YAML, прочитанный child с
  `cwd = project_root` (FR-01 гарантирует, что родитель читал тот же файл).
- **Лимиты.** `--max-retries`, `--timeout`, `--budget`, `--task-budget`, а также
  `--callback-url`, `--log-level`, `--log-json`.
- **Serializer.** Один типизированный serializer `ExecutorConfig → argv`
  (engineer AP-02) перечисляет представимые поля явно и строится от того же
  перечня, что `_COMMON_DEFAULTS`; паритет перечней охраняется тестом
  (engineer RK-05).
- **Entry point.** Child запускается через entry point текущего окружения
  (`sys.executable -m spec_runner` либо console-script того же venv), а не
  через `spec-runner` из PATH оператора (engineer CON-05).
- **Вывод child.** `stdout`/`stderr` child направляются в лог-файл под
  `config.logs_dir` launch namespace (или `DEVNULL`), не в `PIPE`, который
  никто не читает (engineer AP-04, RK-01).

**Acceptance**:

- E2E: сервер `--project-root <external> --change add-x` → `run_task` → child
  (fake command) записывает свой resolved `ExecutorConfig`; сравнение с
  родительским по всем перечисленным полям (scope, safety, лимиты) — равенство.
  Сравниваются resolved конфиги, не строка команды (engineer RK-03).
- State, lock и log child лежат под `<external>/spec/changes/add-x/`.
- Тест паритета: множество флагов serializer-а ⊆ множество ключей
  `_COMMON_DEFAULTS`, и каждое представимое поле `ExecutorConfig`, объявленное
  serializer-ом, имеет флаг в `common`.
- E2E с многословным fake (объём вывода > размера pipe-буфера) завершается без
  зависания child.
- Child запущен из тестового окружения: тест зелёный при отсутствии
  `spec-runner` в PATH.

traces: [G-01, J-02, P-01, P-03, M-01, AP-02, AP-04, CON-03(engineer),
  CON-05(engineer), RK-03(engineer)]

#### FR-04: Невоспроизводимый parent-config — отказ до платного вызова

**Priority**: Must

Если resolved parent-config невозможно воспроизвести для child — у родителя
переопределено (CLI-флагом или programmatic-config) непредставимое поле, либо
YAML по `project_root` не найден там, где родитель его прочитал — `run_task`
возвращает `status: error` до `Popen`. Молчаливых defaults нет (engineer AP-05;
прецеденты #64, #129, #337, #484).

**Acceptance**:

- Тест-двойник: родительский config с override непредставимого поля
  относительно YAML → `run_task` → `status: error`, называющая поле; ни одного
  запущенного subprocess (`Popen` не вызван).
- Родительский config, полностью воспроизводимый (все override — представимые
  поля), → serializer формирует argv, `Popen` вызван ровно один раз.
- Отказ не создаёт state, lock, stop или ready-файлов.

traces: [G-01, G-03, J-02, AP-02, AP-05, RK-02(customer)]

#### FR-05: `started` — только после lock, очистки старого marker и ready

**Priority**: Must

`run_task` возвращает `started` только после того, как child (а) получил
executor lock в заявленном namespace (`_acquire_run_lock`), (б) очистил старый
stop-marker (`clear_stop_file`, `cli.py:831`), (в) опубликовал ready. Родитель
ждёт ready с таймаутом, параллельно наблюдая `proc.poll()` (engineer AP-03).
Stop, вызванный после ответа `started`, не может быть стёрт startup-кодом child:
после ready ни одна стартовая ветка child не вызывает `clear_stop_file`.

Уточнения:

- Handshake использует существующие механизмы (файл в namespace или поле lock —
  Q-02), не новый IPC (сокет, pipe-протокол).
- Три точки потребления marker-а между задачами (`cli.py:831`, `:1055`,
  `:1274`) и семантика stop (файл-маркер, executor не убивается) не меняются
  (OUT-01, engineer RK-04).
- Таймаут ожидания ready конфигурируем (дефолт — значение, достаточное для
  медленного CI; customer RK-01).

**Acceptance**:

- E2E на настоящем CLI entry point с fake command: `run_task("TASK-001")` →
  `started` → немедленный `stop()` → child видит
  `<external>/spec/changes/add-x/.executor-stop`, завершает текущую задачу и
  выходит, не начиная следующую (в tasks.md — минимум две ready-задачи).
- Ответ `started` приходит не раньше, чем существует lock-файл
  `<external>/spec/changes/add-x/.executor-<…>state.db.lock` с pid child и
  опубликован ready.
- Stop-marker, записанный **до** `run_task`, стёрт стартовым `clear_stop_file`
  (старое поведение сохранено); stop-marker, записанный **после** `started`, на
  месте до момента потребления между задачами.
- `test_mcp_v2_wire` и `TestMCPRunTask` проходят без изменения ожиданий, кроме
  прямо следующих из этого требования (ответ `started` теперь ждёт ready).

traces: [G-02, G-03, J-03, P-01, M-01, NFR-03, AP-03, RK-01(customer)]

#### FR-06: Child без lock или без ready — error, не ложный `started`

**Priority**: Must

Если child завершился до ready (в т.ч. exit 1 на занятом lock, отказ
governance-гейта, `ConfigError`), либо ready не опубликован до истечения
таймаута, `run_task` возвращает `status: error` с причиной (код выхода, хвост
лога child из `logs_dir`, либо `timeout`) — не `started`. Занятый lock → ошибка
запуска; второй executor не запускается.

**Acceptance**:

- Lock namespace удерживается другим процессом (живой pid): `run_task` →
  `status: error`, называющая занятый lock; второй executor не запущен
  (по lock-файлу — pid прежний); `started` не возвращён.
- Child, умирающий до ready (fake command с exit 1 на старте): `run_task` →
  `status: error` детерминированно, без ожидания полного таймаута
  (`proc.poll()` наблюдается параллельно).
- Child, не публикующий ready (двойник с зависанием): `run_task` →
  `status: error: timeout` по истечении таймаута; процесс child при этом
  завершается родителем либо явно остаётся с указанием pid в ответе — выбор
  фиксируется на стадии design (см. Q-02).
- Ошибочный ответ не оставляет ready-файла в namespace.

traces: [G-02, G-03, J-03, P-01, AP-03, AP-05]

#### FR-07: Programmatic-контракт `mcp_run_server()` неизменен

**Priority**: Should

`spec_runner.mcp_run_server(config=None)` без аргументов работает по прежнему
контракту: config из текущей директории (`run_server(None)` строит его как
прежде, engineer AP-01); lazy import `mcp_server` через `__getattr__` пакета
сохраняется; `mcp` остаётся опциональной зависимостью (engineer S-08, IF-06).
Плоский запуск из project root без namespace обслуживает его как сегодня
(инвариант 8 чартера).

**Acceptance**:

- `tests/test_lazy_mcp_import.py` проходит без правок.
- Programmatic-вызов `mcp_run_server()` без аргументов из project root
  проходит без правок теста: holder заполнен config-ом из CWD, все tools
  работают в плоском namespace.
- `import spec_runner` без установленного `mcp` не падает.

traces: [G-01, J-02, P-02, CON-01(customer), IF-06]

#### FR-08: Контракт синхронизирован — README, CHANGELOG, #485

**Priority**: Should

README §MCP Server описывает привязку сервера к launch scope, требования к
запуску (`--project-root` + один namespace), отказ на противоречащем tool-level
`spec_prefix` (видимое изменение контракта, engineer IF-01) и семантику
`started` (child держит lock, ready опубликован). CHANGELOG содержит запись.
Issue #485 закрыт ссылкой на PR с evidence. Соседям (devtools,
spec-runner-vscode) новый контракт объявлен issue/handoff-ом без правки их
файлов.

**Acceptance**:

- В PR — раздел README §MCP Server (привязка к launch scope; отказ на
  противоречащем prefix; значение `started`) и строка CHANGELOG под
  Unreleased.
- #485 закрыт ссылкой на PR; в закрывающем комментарии — ссылка на E2E-тест.
- Handoff соседям: issue в devtools / spec-runner-vscode с `slug:` + `from:`
  (ADR-ECO-006) либо запись в `../prograph-vault/authored/notes/`.

traces: [G-03, J-01, P-02, IF-01]

#### FR-09: `status` и `task_detail` показывают launch scope

**Priority**: Should

Ответы `status` и `task_detail` содержат поля `project_root` (абсолютный путь)
и `namespace` (`{"kind": "change", "id": "add-x"}` /
`{"kind": "prefix", "prefix": "p-"}` / `{"kind": "flat"}` — точная форма
фиксируется на стадии design), совпадающие с параметрами запуска сервера, чтобы
оператор мог проверить, какой scope обслуживается.

Приоритет в источнике — **Could**; здесь несётся как Should, поскольку схема
стадии допускает только Must/Should. Остаётся последним кандидатом на вырезание
при сокращении объёма.

**Acceptance**:

- JSON-ответ `status` на сервере `--project-root <external> --change add-x`
  содержит `project_root == <external>` (resolved) и namespace `add-x`.
- Тот же сервер, `task_detail("TASK-001")` — те же поля.
- Плоский запуск — `namespace` сообщает плоский `spec/`.
- Существующие ключи ответов `status`/`task_detail` сохранены (аддитивное
  изменение).

traces: [G-03, J-01, P-01, AP-04]

## 5. Нефункциональные требования

#### NFR-01: Ни один tool не пишет вне spec-директории launch scope

**Priority**: Must

Категория: safety. Ни один tool (включая ошибочные ветки FR-02, FR-04, FR-06)
не пишет state, lock, stop, ready или log-файлы вне spec-директории launch
scope (`config.spec_dir` / `logs_dir` namespace). Это единственная защита P-03.

**Acceptance**:

- После полного E2E (FR-01 … FR-06, включая отказные ветки) в плоском
  `<external>/spec/` и в CWD сервера нет ни одного файла `.executor-*`;
  проверяется `rglob` по обеим директориям.
- Все runtime-файлы child перечислены под `<external>/spec/changes/add-x/`.

traces: [G-01, P-03, FR-01, FR-03, FR-04]

#### NFR-02: Тесты не вызывают платного агента и укладываются в CI-бюджет

**Priority**: Must

Категория: cost. E2E и unit-тесты не исполняют платного агента (conftest-пояс
`PaidBinaryReached`, engineer CON-01); child в тестах использует
детерминированный fake command (`tests/fixtures/fake_claude.sh` или
специализированный двойник); базовый E2E завершается быстрее 60 секунд и входит
в `-m "not slow"`.

**Acceptance**:

- Прогон `uv run pytest tests/ -q -m "not slow"` — без `PaidBinaryReached`;
  базовый E2E #485 < 60 с на CI.
- Fake command E2E умеет: успешно завершить задачу; писать многословный вывод
  (FR-03); выйти с exit 1 на старте (FR-06).

traces: [G-03, CON-01(engineer), FR-05, FR-06]

#### NFR-03: Stop после `started` не теряется даже при немедленном вызове

**Priority**: Must

Категория: reliability. Гарантия FR-05 подтверждается статистически: отдельный
soak-тест под маркером `slow` повторяет E2E `run_task → started → stop` 20 раз
подряд без единого потерянного stop-запроса; базовый E2E в CI — одна итерация.

**Acceptance**:

- `@pytest.mark.slow` soak ×20: в каждой итерации child завершается после
  текущей задачи, не начав следующую; ноль потерянных stop.
- Soak не входит в обязательный CI-прогон; запускается вручную и в ночном
  профиле (если таковой есть).

traces: [G-02, J-03, FR-05, RK-01(customer)]

## 6. Ограничения

- **CON-01 (customer)** Публичный `mcp_run_server` и lazy import `mcp_server`
  из `spec_runner` сохраняются (→ FR-07).
- **CON-02 (customer)** Доставка через governance репо: `spec_governance:
  strict`, `execution_mode: tdd`, `review_policy: required`, integration PR;
  `harness_guard: strict` запрещает задаче трогать `spec-runner.config.yaml` и
  `spec/.tdd-evidence/*` (engineer CON-02, S-09).
- **CON-03 (customer)** `--change` и `--spec-prefix` взаимоисключающие; child
  получает ровно один namespace (→ FR-03).
- **CON-04 (customer)** Форма решения задана владельцем: holder + serializer +
  handshake (→ FR-01/02, FR-03/04, FR-05/06).
- **CON-03 (engineer)** Поиск YAML по `project_root` входит в объём как условие
  FR-03 (→ FR-01).
- **CON-05 (engineer)** E2E запускает настоящий CLI entry point тестового
  окружения, не `spec-runner` из PATH (→ FR-03).
- Тулинг: Python 3.11+, ruff (line length 100), mypy strict, pyrefly;
  тесты `uv run pytest`.

## 7. Вне объёма

- **OUT-01** Семантика остановки за пределами стартового handshake: stop —
  файл-маркер, executor не убивается; три точки потребления в
  `run`/`watch`/`retry` не переписываются.
- **OUT-02** Сетевой транспорт и аутентификация MCP; stdio-only.
- **OUT-03** Argv-контракт spec-runner-vscode и жизненный цикл команды `change`.
- **OUT-04** Новые write-tools MCP помимо `run_task` и `stop`.
- Новый IPC для ready (сокет, pipe-протокол).
- Правка соседних репо (devtools, spec-runner-vscode) — только issue/handoff.

## 8. Метрики успеха

- **M-01** E2E #485 (`run_task → started → stop`, внешний project root,
  `--change`) — baseline: stop теряется / executor в чужом namespace → target:
  зелёный E2E в CI на каждом PR. traces: [FR-01, FR-03, FR-05, NFR-01]
- **M-02** Число MCP tools, пересобирающих config из CWD — baseline 7/8 →
  target 0/8. traces: [FR-01]

## 9. Трассировка

### FR/NFR → цели, jobs, персоны, метрики

| ID | Priority | Goals | Jobs | Personas | Metrics | Engineer AP |
|---|---|---|---|---|---|---|
| FR-01 | Must | G-01 | J-01 | P-01, P-03 | M-02 | AP-01 |
| FR-02 | Must | G-01 | J-01 | P-01 | — | AP-01 |
| FR-03 | Must | G-01 | J-02 | P-01, P-03 | M-01 | AP-02, AP-04 |
| FR-04 | Must | G-01, G-03 | J-02 | P-01 | — | AP-02, AP-05 |
| FR-05 | Must | G-02, G-03 | J-03 | P-01 | M-01 | AP-03 |
| FR-06 | Must | G-02, G-03 | J-03 | P-01 | — | AP-03, AP-05 |
| FR-07 | Should | G-01 | J-02 | P-02 | — | AP-01 |
| FR-08 | Should | G-03 | J-01 | P-02 | — | — |
| FR-09 | Should (source: Could) | G-03 | J-01 | P-01 | — | AP-04 |
| NFR-01 | Must | G-01 | — | P-03 | M-01 | AP-05 |
| NFR-02 | Must | G-03 | — | — | — | — |
| NFR-03 | Must | G-02 | J-03 | P-01 | M-01 | AP-03 |

### FR/NFR → критерии приёмки чартера → тестовые артефакты

| ID | Критерий приёмки чартера | Тест (ожидаемый артефакт) |
|---|---|---|
| FR-01 | read-tools читают change; ноль `.executor-*` вне scope; `_build_config` 0/8 | `test_mcp_launch_scope.py::TestHolder` (все 8 tools через helper; YAML по `project_root`) |
| FR-02 | `spec_prefix="other-"` при `--change add-x` → error, `Popen` не вызван | `test_mcp_launch_scope.py::TestContradiction` |
| FR-03 | effective config child == parent; state/lock/log под change | `test_mcp_e2e_485.py` (resolved-config compare, многословный fake), `test_mcp_serializer.py::test_parity_with_common_defaults` |
| FR-04 | непредставимое поле → error, ни одного subprocess | `test_mcp_serializer.py::TestRefusal` |
| FR-05 | started → stop → child видит marker, не начинает следующую | `test_mcp_e2e_485.py::test_stop_after_started_is_honoured` |
| FR-06 | занятый lock → error; child без ready → error | `test_mcp_launch_scope.py::TestHandshake` (busy lock, early exit, timeout) |
| FR-07 | `test_lazy_mcp_import` и `mcp_run_server()` без правок | `tests/test_lazy_mcp_import.py`, `tests/test_mcp.py` (существующие) |
| FR-08 | README §MCP Server, CHANGELOG, #485 закрыт | ревью PR; ссылка в закрытии #485 |
| FR-09 | `status` содержит `project_root` и `namespace` | `test_mcp_launch_scope.py::TestScopeVisibility` |
| NFR-01 | ноль `.executor-*` в плоском `spec/` и CWD после E2E | `rglob`-проверка в `test_mcp_e2e_485.py` |
| NFR-02 | базовый E2E < 60 с, без `PaidBinaryReached` | тайминг CI; conftest-пояс |
| NFR-03 | soak ×20 под `slow`, ноль потерянных stop | `test_mcp_e2e_485.py::test_soak_stop_after_started` (`@pytest.mark.slow`) |

Имена тестовых файлов — ожидание требований, не предписание; стадия design
вправе переименовать при сохранении покрытия.

### Риски → требования-меры

| Риск | Мера | Требование |
|---|---|---|
| RK-01 (customer): флаки окна ready на медленном CI | ready **и** `proc.poll()` одновременно; таймаут конфигурируем; soak — `slow` | FR-05, FR-06, NFR-03 |
| RK-02 (customer) / RK-03 (engineer): неполная сериализация | явный перечень + паритет с `_COMMON_DEFAULTS`; сравнение resolved configs; отказ на непредставимом override | FR-03, FR-04 |
| RK-02 (engineer): родитель читает чужой YAML | поиск YAML по `project_root` | FR-01 |
| RK-01 (engineer): недренируемый PIPE | stdout/stderr в `logs_dir`; многословный fake в E2E | FR-03 |
| RK-04 (engineer): расползание на семантику stop | handshake после существующего `clear_stop_file`; OUT-01 | FR-05 |
| RK-05 (engineer): рассинхрон serializer ↔ `common` | тест паритета перечней | FR-03 |
| IF-01: видимое изменение контракта | README до мержа; плоский запуск как прежде | FR-08, FR-07 |

## 10. Открытые вопросы

- **Q-01 · owner_role: product · blocking: false.** Сервер запущен без
  namespace (плоский `spec/`): остаётся ли непустой tool-level `spec_prefix`
  разрешённым выбором prefix-namespace внутри launch `project_root`
  (противоречия нет), или отклоняется тоже? Рабочее допущение требований
  (чартер Q-A, engineer Q-01): **разрешён**, через тот же holder — это
  уточнение scope, а не противоречие; FR-02 сформулировано под это допущение
  («противоречащий» определён в §3). Если владелец решит иначе, меняется только
  предикат противоречия и один тест.
- **Q-02 · owner_role: architects · blocking: false.** Канал ready
  (чартер Q-B; customer Q-01, engineer Q-02): файл `<spec_dir>/.executor-ready`
  или поле в JSON lock через `ExecutorLock`? Предложение — файл в namespace:
  наблюдаем в E2E, не меняет формат lock. Сюда же — судьба child при таймауте
  ready (FR-06): завершать родителем или оставлять с pid в ответе.
- **Q-03 · owner_role: architects · blocking: false.** Обратная сверка effective
  config child родителем (чартер Q-C; customer Q-02, engineer Q-03): child
  записывает resolved поля в ready-marker, родитель сравнивает перечисленные
  serializer-ом поля и отказывает при расхождении — или доверия сформированной
  команде достаточно? Предложение — сверка: это тест FR-03, вынесенный в
  runtime; если принимается, FR-06 получает ещё одну error-ветку («config
  mismatch»).

## 11. Условие завершения стадии

Требования считаются выполненными, когда все FR с приоритетом Must и все NFR
подтверждены перечисленными в §9 тестами, зелёными в CI на каждом PR
(`-m "not slow"`), soak NFR-03 пройден вручную хотя бы один раз, FR-07/FR-08
подтверждены ревью PR, а FR-09 — либо реализован, либо явно вырезан решением
владельца с записью в CHANGELOG-заметке PR.

## Источники

- `00-charter.md` (blob `3c29f515…`) — цель, объём, инварианты 1–8, критерии
  приёмки, риски, Q-A/Q-B/Q-C.
- `00-discovery/discovery-brief-customer.md` (approved 2026-09-14, blob
  `4d7900d7…`) — G/P/J/FR/NFR/CON/M/OUT/RK/Q; источник идентификаторов FR/NFR.
- `00-discovery/brief.md` (engineer, blob `39e8c625…`) — S/IF/CON/AP/RK/Q.
- `src/spec_runner/mcp_server.py:19`, `:22`, `:206`, `:230`, `:284`;
  `src/spec_runner/config.py:241`–`:242`, `:836`; `src/spec_runner/cli.py:179`,
  `:831`, `:1055`, `:1274`.
- Issue #485; прецеденты #481/#484 (узкое исправление `stop`), #64, #129, #337
  (fail-closed).
