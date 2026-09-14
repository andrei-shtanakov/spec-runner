---
spec_stage: decomposition
status: draft
owner_role: tech-lead
traces_to: [design, acceptance]
upstream_hashes: {design: "361d4da37c1d685ddb03ba769a3d3b23e24b36f2", acceptance: "88ab19d8293d6049cfaeba97ba53264cbe806bb0"}
---

# Decomposition — MCP launch scope (spec-runner#485)

Стадия `decomposition` governance-бандла
`workstreams/mcp-launch-scope-spec-runner-485-20260914/`. Режет доставку на
задачи и объявляет граф их зависимостей поверх design (`20-design.md`, blob
`05ebfe08…`) и acceptance (`25-acceptance.md`, blob `ae154d88…`). Резолюции
design (Q-02 — ready как файл `<spec_dir>/.executor-ready`, child при таймауте
завершается родителем; Q-03 — обратной сверки в runtime нет, воспроизводимость
решается симуляцией до `Popen`) и продуктовые решения upstream'а — holder +
serializer + handshake (CON-04), отказ на противоречащем prefix (FR-02),
`started` только после lock и ready (FR-05), отказ до `Popen` на
невоспроизводимом config (FR-04) — здесь входные условия и не переоткрываются.

**Нарезка продиктована владением тестовыми файлами.** Единственный владелец на
файл считается исключительно по `checked_by` сценариев (через `scenarios`), и
behaviour-спека раздала 28 сценариев по шести целям:
`tests/test_mcp_launch_scope.py` (12 сценариев), `tests/test_mcp_e2e_485.py`
(10), `tests/test_mcp_serializer.py` (3), `tests/test_lazy_mcp_import.py` (1),
`tests/test_mcp.py` (1), `README.md` (1, `kind: manual`). Два сценария одного
файла не могут лежать на разных задачах — иначе у файла два владельца, и
«зелёное» перестаёт быть про одну из них. Поэтому задач здесь четыре, а не
десять, и две из них крупные; более тонкая нарезка потребовала бы развести
`checked_by` behaviour-спеки по большему числу файлов — это правка upstream'а,
а не этого документа (см. «Вне объёма»). Карта модулей design'а — вход, не
предписание «одна строка = одна задача»: она читается по осям
«как child запускается и ожидается» (§1 + §3 design) → «что child-у передаётся»
(§2) → «что child публикует и как это доказано живьём» (§3.1 + E2E).

**Условие на `verify`: baseline-эвиденция.** Тип `verify` допустим только там,
где цель существует в дереве и поведение уже доставлено задачами из
`delivered_by`; мост исполняет такую задачу как verify_first — живой прогон
объявленной группы стоит первым действием, и зелёное означает, что красный не
покупается. В этом графе одна такая задача — DT-03 (`tests/test_lazy_mcp_import.py`
проходит без правок по букве BEH-23; наблюдаемое «holder заполнен из CWD»
утверждается в файле DT-01, который DT-03 объявляет группой наблюдения через
`verifies`). Три остальные задачи — `implement`: их сценарии сегодня не
утверждены нигде, и честный baseline-RED у каждой достижим в её собственных
границах — `tdd_waiver` в этом графе не объявлен ни разу.

**Документация — не отдельная задача.** Под `execution_mode: tdd` задача без
исполняемого сценария не имеет честного red: единственный тест на README —
grep по строкам — design запрещает явно («Рамки red-дизайна», задачи-артефакты),
а единственный санкционированный класс waiver-а (`characterisation`) документацию
не покрывает. Поэтому BEH-24 лежит на последней кодовой задаче (DT-04) в форме
design §4: red и green — поведение, документация — отдельный коммит той же
задачи после green. Человеческие действия BEH-24 (закрытие #485, issue соседям
по ADR-ECO-006) названы в теле DT-04 как приёмочные пункты, не как предмет
автотеста.

## Задачи

#### DT-01: Launch scope: holder, предикат, YAML по `project_root`, родительская сторона handshake · type: implement · owner: dev
scenarios: [BEH-01, BEH-02, BEH-03, BEH-04, BEH-05, BEH-06, BEH-07, BEH-18, BEH-20, BEH-21, BEH-22, BEH-25, BEH-28]
depends_on: []
parallel_group: core

Предмет — design §1 целиком и §3.2–3.4: новый модуль `src/spec_runner/mcp_launch.py`
без импорта `mcp` (`LaunchScope` с `project_root`/`namespace`/`config_path`,
`ScopeContradiction` с обеими сторонами конфликта и единственным текстом,
`resolve_tool_config` с четырьмя ветками §1.3 — включая ветку-уточнение Q-01 при
плоском запуске, `wait_for_ready` с исходами `Ready`/`Exited`/`TimedOut`,
проверкой `PID == proc.pid`, снятием чужого мёртвого ready-файла до `Popen` и
завершением child SIGTERM→SIGKILL по таймауту); в `mcp_server.py` — holder
`_scope` вместо `_launch_stop_config`, один `_tool_config(spec_prefix)`, через
который проходят все восемь обёрток (включая `logs`, отвечающий на противоречие
JSON, а не plain-text), `run_task` в порядке helper → governance-гейт → `Popen`
(`cwd = project_root`, `stdin=DEVNULL`, `stdout` в файл под `config.logs_dir`
с префиксом `task_id`, `stderr=STDOUT`, без `PIPE`) → `wait_for_ready` → формы
ответов §3.4, `status`/`task_detail` с аддитивными `project_root`/`namespace`
в форме §1.5; в `config.py` — property `ready_file`, поле
`mcp_ready_timeout_seconds` в loader (код loader-а, не правка YAML репо под
`harness_guard: strict`), `_resolve_config_path(base)`; в `cli.main()` —
передача `project_root` в `_resolve_config_path`; `src/spec_runner/__main__.py`
и seam `child_entry()` (`[sys.executable, "-m", "spec_runner"]`) — форма
инвокации, которую живьём предъявит BEH-11 у DT-04. Holder пуст только у
тестов, вызывающих tools без `run_server`: helper строит scope из
`_build_config("")` на каждый вызов и не сохраняет.

Границы. Хвост argv child после `run --task <id>` в этой задаче остаётся
сегодняшним (namespace-флаг по launch scope); таблица представимости,
`child_argv` и симуляция воспроизводимости — DT-02, и ветка-уточнение Q-01
здесь пересобирает config через `_build_parser()`/`build_config` по
namespace-argv (`--project-root`, `--spec-prefix <p>`), а DT-02 переводит её
на `child_argv`, не меняя наблюдаемого BEH-07(в) (namespace `p-` внутри
`<external>`, `project_root` сохранён). Child-сторона handshake — публикация
ready в `_run_tasks_inner` и снятие в `finally` `cmd_run` — DT-04: все сценарии
этой задачи, где нужен child, обходятся двойниками (`Popen`-двойник, создающий
`config.ready_file` с нужным `PID` или выходящий с кодом; процесс-держатель lock
для BEH-20; процесс, выходящий с exit 1 с диагностикой в stderr, для BEH-21;
двойник, берущий lock и молчащий, для BEH-22). Следствие названо честно: между
DT-01 и DT-04 настоящий `run_task` на живом child упирается в таймаут ready —
промежуточное состояние integration-ветки известно и неработоспособно для
оператора, что и делает цепь DT-01 → DT-02 → DT-04 обязательной внутри одного
integration PR.

BEH-28 лежит здесь, а не отдельной задачей, потому что именно эта задача ломает
существующие ожидания и обязана привести их в порядок в своих границах: двойники
`Popen` в `TestMCPRunTask` (`tests/test_mcp.py`) и в `tests/test_mcp_v2_wire.py`
начинают имитировать handshake, единственная строка
`assert server._launch_stop_config is None` в
`TestMCPStop::test_change_scoped_server_preserves_launch_stop_namespace`
становится проверкой опустевшего holder-а — ровно два следствия контракта,
любое иное изменённое ожидание — находка ревью. Владеет
`tests/test_mcp_launch_scope.py` (новый) и `tests/test_mcp.py` (существующий,
правка по предмету BEH-28); `tests/test_mcp_v2_wire.py` правится по тому же
предмету, но носителем сценария не является и никем другим не заявлен.

Red-рамки по design: BEH-04 — заполненный holder, двойник `_build_config`,
поднимающий исключение, восемь ответов без исключения (счётчик двойника — и
есть измерение M-02; `grep` по исходнику — эвиденция PR, не тест); BEH-02 —
два YAML с противоположными `spec_governance`, применённый — через ответ
`run_task`; BEH-18 — переопределённый малый таймаут обрывает ожидание, ключ
из YAML доезжает до `ExecutorConfig` через `load_config_from_yaml`. Не
утверждать: отсутствие символа `_launch_stop_config`, имя класса `LaunchScope`,
интервал опроса, точный текст ошибок сверх обеих сторон конфликта / слова
`timeout`, тайминги с узким запасом.

#### DT-02: Serializer effective config и проверка воспроизводимости до `Popen` · type: implement · owner: dev
scenarios: [BEH-10, BEH-13, BEH-14]
depends_on: [DT-01]
parallel_group: core

Предмет — design §2 в `mcp_launch.py`: декларативная таблица `REPRESENTABLE`
(поле `ExecutorConfig` → флаг `common` → правило эмиссии, namespace-поле как
одна строка на два ключа `_COMMON_DEFAULTS`) и `NOT_FORWARDED` с причиной
(`log_json` — параметр рендерера логов процесса, не поле config);
`child_argv(config, task_id)`, перечисляющий таблицу и ничего больше;
`simulate_child_config(scope, argv)` — `load_config_from_yaml(scope.config_path)`
→ `_build_parser().parse_args(argv)` → `build_config`, сравнение по всем полям
dataclass (`Path` — resolved) кроме `PARENT_ONLY_FIELDS`; `Irreproducible(fields)`
с именем поля, значением родителя, значением child и причиной (нет CLI-флага;
YAML по `project_root` больше не существует). Врезка в `run_task` DT-01: гейт →
`child_argv` → симуляция → только затем `Popen`; до этой точки в namespace не
создан ни один файл. Та же пара `child_argv` + парсер обслуживает
ветку-уточнение Q-01 в `resolve_tool_config` — второй проход по одной ветке,
названный в DT-01.

Границы: `_COMMON_DEFAULTS` и `_build_parser()` в `cli.py` не расширяются —
паритет доказывается против них как есть (BEH-10: флаги таблицы ⊆ ключи
`_COMMON_DEFAULTS`, `REPRESENTABLE ∪ NOT_FORWARDED == set(_COMMON_DEFAULTS)`,
оба множества вычислены из живых объектов); односторонние флаги (`--no-tests`
при YAML `false`) — обычная находка симуляции, не особый случай serializer-а;
живой child здесь не запускается — BEH-13 считает вызовы двойника `Popen`
(ноль), BEH-14 — ровно один с двойником, имитирующим handshake через
`config.ready_file` DT-01. Владеет `tests/test_mcp_serializer.py` (новый).
Red-рамки: одна проверка воспроизводимости на оба исхода BEH-13/BEH-14 — тест
не должен знать, каким флагом какое поле доехало; не утверждать имена
приватных функций и порядок полей таблицы.

#### DT-03: Programmatic-контракт `mcp_run_server()` и lazy import не сдвинулись · type: verify · owner: qa
scenarios: [BEH-23]
depends_on: [DT-01]
delivered_by: [DT-01]
parallel_group: solo
verifies: [tests/test_mcp_launch_scope.py, tests/test_mcp.py]

Предмет — предъявить, что FR-07 держится после переезда на holder: `import
spec_runner` не импортирует `mcp`, `spec_runner.mcp_run_server` резолвится через
lazy `__getattr__` пакета — оба утверждения уже несёт существующий
`tests/test_lazy_mcp_import.py`, и по букве BEH-23 он проходит **без правок**;
«holder заполнен config-ом из CWD, все восемь tools работают в плоском
namespace» наблюдаемо на пути `run_server(None)`, который утверждают плоские
случаи BEH-07(в) и BEH-25 в `tests/test_mcp_launch_scope.py` — файл владеет
DT-01, он лежит в замыкании `depends_on` этой задачи и объявлен группой
наблюдения; `tests/test_mcp.py` входит в `verifies` ради BEH-28 (существующие
ожидания после переезда на holder), а не как свидетель плоского запуска —
`TestMCPStop::test_explicit_prefix_keeps_launch_project_root` закрепляет случай
(а) BEH-07 (launch `--spec-prefix`, совпадающий prefix) и `run_server(config)`,
не `run_server(None)`. Сам
`mcp_run_server()` без аргументов живьём не вызывается — он поднимает stdio-сервер
и не возвращается; наблюдаемое — путь `run_server(None)` под ним.

Baseline-эвиденция проверена по содержанию, не по имени файла: носитель
существует в дереве, утверждает первую и вторую из четырёх And-частей сценария,
остальные две утверждены файлами DT-01 в `verifies`. Задача ничего не правит:
зелёный живой прогон группы — предъявленный факт, красный в нём — регрессия
DT-01, а не работа этой задачи. Носитель `tests/test_lazy_mcp_import.py`
заявлен только здесь и никем не правится.

#### DT-04: Child публикует ready; E2E scope, effective config, handshake и стоп на живом child; soak; документация · type: implement · owner: dev
scenarios: [BEH-08, BEH-09, BEH-11, BEH-12, BEH-15, BEH-16, BEH-17, BEH-19, BEH-24, BEH-26, BEH-27]
depends_on: [DT-02]
parallel_group: core

Предмет — child-сторона handshake (design §3.1) и её живое доказательство.
В `cli._run_tasks_inner` сразу после `clear_stop_file` и до `parse_tasks` —
атомарная запись `config.ready_file` (`PID`/`Started`, тот же формат и код
чтения, что у lock); снятие — в `finally` `cmd_run` рядом с `lock.release()`;
`ready_file` входит в `git_ops.runtime_state_paths`, чтобы stash-rescue,
staging и инвентарь `.executor-*` видели его как runtime. Три точки потребления
marker-а (`run`/`watch`/`retry`) не трогаются (OUT-01), и ни одна стартовая
ветка после ready не вызывает `clear_stop_file`. Далее — E2E на настоящем
child (`sys.executable -m spec_runner` через `child_entry()` DT-01) и
детерминированном `tests/fixtures/fake_claude.sh` (`FAKE_EXIT_CODE`,
`FAKE_RESPONSE_FILE` ≥ 1 MiB для BEH-12; если трёх режимов BEH-27 фикстуре не
хватает — специализированный двойник рядом, `fake_claude.sh` не переписывать):
argv и `cwd` child, runtime-файлы под `<external>/spec/changes/add-x/` и
пустой `rglob(".executor-*")` по плоскому `spec/` и по CWD сервера после
полного прогона, включая отказные ветки (BEH-26); равенство двух resolved
`ExecutorConfig` через подмену `child_entry()` тестовым entry point, пишущим
свой config тем же `_build_parser()`/`build_config` (BEH-09); `PATH` без
`spec-runner` (BEH-11); lock с pid child и опубликованный ready в момент
`started`, двойник child с задержкой lock задерживает `started` (BEH-15);
`run_task` → `started` → немедленный `stop()` — `TASK-001` успешна, вторая
задача не начата, child вышел сам (BEH-16, M-01); marker до `run_task` стёрт,
marker после `started` цел (BEH-17); soak ×20 под `@pytest.mark.slow` с
очисткой namespace между итерациями (BEH-19); ни одного `PaidBinaryReached`,
базовый E2E BEH-15/16/17 в `-m "not slow"` (BEH-27).

Документация — отдельный коммит задачи после green (design §4): README §MCP
Server — привязка сервера к launch scope и требование запуска
(`--project-root` + один namespace), отказ на противоречащем tool-level
`spec_prefix` как видимое изменение контракта, значение `started`, ключ
`mcp_ready_timeout_seconds`, `.executor-ready` в инвентаре runtime-файлов;
CHANGELOG — запись под Unreleased со ссылкой на #485. Закрытие #485 ссылкой на
PR и на тест BEH-16, issue соседям (devtools, spec-runner-vscode) с `slug:` +
`from:` по ADR-ECO-006 — действия человека при приёмке PR (AC-22), автотестом не
покрываются намеренно: тест на наличие issue завёл бы сетевую зависимость, а
файлы соседей не правятся.

Границы: `docs/state-schema.md` и `schemas/*` не меняются (ready — не state);
`start_new_session` для child не вводится, если E2E не потребует; дефолт
таймаута 60 с уточняется только по замеру CI. Владеет
`tests/test_mcp_e2e_485.py` (новый) и `README.md` (существующий, `kind: manual`).
Red-рамки: `ready_file` — через настоящий вызов (после `clear_stop_file` файл
существует с `PID` процесса и исчезает после `cmd_run`), не через чтение
property; хотя бы один red на настоящем child-процессе; не утверждать
wall-clock «< 60 с» внутри теста (замер — `pytest --durations` в CI, AC-17),
наличие строк README/CHANGELOG (AC-22 — ревью PR), литерал `.executor-ready`
вместо `config.ready_file`, что child не был убит на успешном пути (проверяется
завершением после задачи), тайминги «ровно 2 с».

## Инварианты графа

**Покрытие сценариев.** Каждый сценарий BEH-01…BEH-28 назван ровно одной
задачей; пропусков и дублей нет. Распределение: DT-01 → 01, 02, 03, 04, 05,
06, 07, 18, 20, 21, 22, 25, 28; DT-02 → 10, 13, 14; DT-03 → 23; DT-04 → 08,
09, 11, 12, 15, 16, 17, 19, 24, 26, 27. Итого 13 + 3 + 1 + 11 = 28 сценариев
в четырёх задачах.

**Владелец сценария — та задача, на чьей поверхности он наблюдаем.**
`implement`-задача без `delivered_by` обязана погасить свой красный в
собственных границах. Отсюда: сценарии на двойниках child (BEH-18, 20, 21, 22)
лежат на DT-01, где живёт `wait_for_ready`; сценарии на живом child (BEH-15,
16, 17) — на DT-04, где child впервые публикует ready; BEH-11 и BEH-12
наблюдают форму инвокации и лог, созданные DT-01, но наблюдаемы только живым
child — и потому лежат на DT-04; BEH-28 — на DT-01, потому что ломает
ожидания `tests/test_mcp.py` именно она.

**Один владелец на файл.** Владение = право править файл; цели задач попарно не
пересекаются. Единственная цель, которую объявившая задача не правит, —
носитель `verify`-задачи DT-03; владение там читается как «эту цель не заявляет
никто другой».

| Задача | Целевой файл (`checked_by`) | Статус файла |
|---|---|---|
| DT-01 | `tests/test_mcp_launch_scope.py`; `tests/test_mcp.py` (носитель BEH-28) | новый; существующий, правка по предмету BEH-28 |
| DT-02 | `tests/test_mcp_serializer.py` | новый |
| DT-03 | `tests/test_lazy_mcp_import.py` — носитель BEH-23; группа наблюдения — поле `verifies` | существующий: заявлен носителем, **не правится** |
| DT-04 | `tests/test_mcp_e2e_485.py`; `README.md` (носитель BEH-24, `kind: manual`) | новый; существующий |

`tests/test_mcp_v2_wire.py` правится DT-01 по предмету BEH-28, но носителем
сценария не является и ни одной задачей не заявлен — конфликта владения нет.

**`delivered_by` ⊆ замыкание `depends_on`.** DT-03: `delivered_by: [DT-01]`,
замыкание `depends_on` = {DT-01}. У `implement`-задач поля нет.

**`verifies` — двухуровневая проверка.** (1) Фатальный инвариант: каждый файл
в `verifies` DT-03 — `tests/test_mcp_launch_scope.py` и `tests/test_mcp.py` —
владеется DT-01 через `checked_by`, и DT-01 лежит в замыкании `depends_on`
DT-03 напрямую. Собственный носитель DT-03 в поле не входит — свою цель задача
не наблюдает, в живой прогон она войдёт первой по построению `**Verifies:**`.
(2) Нефатальная форма: файлов без владельца в `verifies` нет. У
`implement`-задач поле отсутствует. `verifies` не даёт освобождения от
single-owner: владение считается только по `checked_by`.

**Ацикличность и порядок объявления.** Рёбра: DT-02 → DT-01, DT-03 → DT-01,
DT-04 → DT-02. Циклов нет; каждая задача объявлена после всех, от кого зависит.
Искусственной линейной цепи нет: DT-03 не зависит от DT-02 и DT-04, потому что
FR-07 доставлен DT-01 целиком.

**Правило слияния групп.** Ни одна задача не зависит от членов двух и более
чужих `parallel_group`: DT-02 и DT-04 зависят внутри `core`, DT-03 — на одну
задачу `core`. Дополнительных рёбер на sink-и не требуется.

**`tdd_waiver`.** Не объявлен ни у одной задачи: DT-01 открывает граф, DT-02 и
DT-04 утверждают новое поведение, у которого до них нет носителя, DT-03 —
`verify`. Санкционировать нечего.

**Соответствие acceptance.** AC-01, 02, 03, 04, 12, 14, 18, 20, 21 — DT-01;
AC-06, 09 — DT-02; AC-19 — DT-03; AC-05, 07, 08, 10, 11, 13, 15, 16, 17, 22 —
DT-04. `verification: metric` (AC-17 длительность из CI, AC-18 счётчик
двойника + `grep` в описании PR) и `verification: manual` (AC-22) закрываются
при приёмке integration PR, а не зелёным задачи.

## Порядок и параллельность

**Критический путь — группа `core`: DT-01 → DT-02 → DT-04.** Каждое ребро
реальное: serializer врезается в `run_task`, который создаёт DT-01; живой child
DT-04 наблюдает argv, который строит DT-02. Три задачи исполняются строго
последовательно и определяют длину доставки; параллелить внутри группы нечего.

**DT-03 (`solo`) стартует сразу после DT-01** и идёт параллельно с DT-02 и
DT-04: verify_first-прогон группы `tests/test_lazy_mcp_import.py` +
`tests/test_mcp_launch_scope.py` + `tests/test_mcp.py` не зависит ни от
serializer-а, ни от child-стороны. Это единственная параллельная дорожка
графа.

**Промежуточные состояния integration-ветки.** После DT-01 и до DT-04 живой
`run_task` на настоящем child не получает `started`: родитель ждёт ready,
который child ещё не публикует. Состояние названо в DT-01 и допустимо только
потому, что доставка — один integration PR (CON-02, `integration_pr: true`);
частичная приёмка (read-tools без handshake) порогом acceptance не
предусмотрена. Оператору не следует пробовать сервер на живом проекте между
DT-01 и DT-04.

**Практическая рекомендация оператору.** Две дорожки: (1) `core` как ведущая;
(2) DT-03 подхватить сразу после DT-01 — она короткая и закрывает AC-19 раньше
конца цепи. Блокирующие пункты приёмки вне зелёного задач: soak ×20 (AC-13) —
ручной прогон `-m slow` после DT-04, вывод в описание PR; длительность базового
E2E (AC-17) — из `pytest --durations` CI-прогона PR; `grep -n _build_config
src/spec_runner/mcp_server.py` до/после (AC-18) — в описание PR; README/CHANGELOG,
закрытие #485 и issue соседям (AC-22) — ревью PR и человек.

## Вне объёма

Сознательно **не** декомпозировано:

- **Более тонкая нарезка DT-01 и DT-04.** Тринадцать и одиннадцать сценариев на
  задачу — следствие того, что behaviour-спека сложила все contract/integration
  сценарии в один файл, а все e2e — в другой. Разделить (например,
  `tests/test_mcp_handshake.py` под BEH-18/20/21/22 или отдельный файл под
  FR-09) можно только правкой `checked_by` в `15-behaviour-spec.md` с
  перепиновкой upstream_hashes design/acceptance; этот документ upstream не
  правит и от единственного владельца на файл не отступает.
- **Внутренние формы, оставленные design'ом реализатору под TDD:** имена
  приватных функций и порядок полей dataclass-ов; интервал опроса
  `wait_for_ready`; пауза между SIGTERM и SIGKILL; число строк `log_tail`; имя
  лог-файла сверх префикса `task_id`; формулировки ошибок сверх обязательного.
  Отдельных задач под них нет — это свобода внутри DT-01/DT-02/DT-04.
- **Иное решение Q-01** (плоский запуск + непустой prefix = противоречие).
  Ветка изолирована в `resolve_tool_config` (DT-01) и стоит одного условия и
  одного теста; задача «на случай иного решения» не заводится.
- **Вырезание FR-09.** Решение владельца; при вырезании BEH-25 снимается с DT-01
  целиком с записью в CHANGELOG-заметке PR (AC-20), граф не меняется — ни одна
  задача на BEH-25 не опирается.
- **Обратная сверка effective config child родителем в runtime** («config
  mismatch», Q-03 — нет). Совпадение доказывается симуляцией DT-02 и E2E
  BEH-09 у DT-04; error-ветки под неё нет.
- **Новый IPC для ready, сетевой транспорт MCP, новые write-tools, семантика
  стопа за пределами стартового handshake** (OUT-01…OUT-04): задач нет, DT-04
  предъявляет лишь неизменность трёх точек потребления marker-а.
- **Правка соседних репозиториев** (devtools, spec-runner-vscode) — только
  issue/handoff по ADR-ECO-006, действие человека в рамках приёмки DT-04.
- **Правка `tests/fixtures/fake_claude.sh`** сверх того, что потребуют
  BEH-12/BEH-21/BEH-27: если `FAKE_*` хватает — без правок; иначе двойник рядом,
  не переписывание общей фикстуры.
- **`docs/state-schema.md`, `schemas/*`, контракт `--json-result`** — не
  затронуты; `.executor-ready` — не state, major bump не требуется.
- **Оптимизации и переписывание `mcp_server.py` «заодно».** Меняются только
  места, названные картой модулей design; задач «переписать» нет.
