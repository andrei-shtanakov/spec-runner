---
spec_stage: design
status: draft
owner_role: architects
traces_to: [requirements, behaviour-spec]
upstream_hashes: {requirements: "4527197fc3891bfef202da34172fd7b683878866", behaviour-spec: "a0320956d9a3ca4d0deba66454ab32126fcc92aa"}
---

# Design — MCP launch scope (spec-runner#485)

Стадия `design` governance-бандла
`workstreams/mcp-launch-scope-spec-runner-485-20260914/`. Даёт механику тому,
что requirements (`10-requirements.md`, blob `7d2e095d…`) и behaviour-spec
(`15-behaviour-spec.md`, blob `eb6f3a72…`) намеренно оставили открытым: где
именно живёт holder, как serializer формирует команду child, каким файлом child
объявляет ready и что родитель делает с child, который ready не объявил.
Продуктовые решения upstream'а — holder + serializer + handshake (CON-04),
отказ на противоречащем prefix (FR-02), `started` только после lock и ready
(FR-05), отказ до `Popen` на невоспроизводимом config (FR-04) — здесь не
пересматриваются; каждое решение ниже ссылается на них как на границу.

Термины — в значении §3 требований: **launch scope**, **плоский запуск**,
**противоречащий `spec_prefix`**, **effective config**, **представимое /
непредставимое поле**, **ready**, **stop-marker**. Ссылки на код — на
состояние `master` после #489 (`0159103`).

## Резолюции открытых вопросов

Входной набор архитектурных вопросов requirements §10: Q-02, Q-03. Q-01
принадлежит product (`owner_role: product`) и здесь не резолвится; дизайн
принимает его рабочее допущение («при плоском запуске непустой tool-level
prefix — уточнение, не противоречие») и изолирует его в одной ветке
предиката (§ Механика 1.3), чтобы иное решение владельца стоило одного условия
и одного теста, как обещано в требованиях.

#### Q-02 · owner_role: architects · resolution: resolved

**Канал ready — отдельный файл `<spec_dir>/.executor-ready`, не поле lock.
Child, не опубликовавший ready до таймаута, завершается родителем.**

Файл, а не lock, по трём причинам, каждая из которых лежит в существующем коде.
(1) Lock-файл `ExecutorLock` — не JSON, как предполагал upstream, а две
текстовые строки `PID:`/`Started:`, которые держатель пишет в открытый под
`flock` дескриптор (`config.py:188–192`); добавить туда «поле ready» — значит
переписывать файл под блокировкой из процесса-держателя и учить родителя
парсить файл, который держатель вправе усечь. (2) Lock берётся в `cmd_run`
(`cli.py:214`) **до** governance-гейта, dirty-spec-гейта, гейта tracked-state и
до `clear_stop_file` (`cli.py:831`) — «lock существует» не означает «старый
marker стёрт», а ready по определению §3 означает именно это; отдельный файл,
записанный последним шагом старта, несёт ровно нужный смысл. (3) Файл лежит
рядом со `.executor-stop`, наблюдается теми же средствами (BEH-18, третий And),
уже покрыт правилом `spec/` корневого `.gitignore` репо и glob-ом
`.executor-*` из `RUNTIME_GITIGNORE_ENTRIES` (`git_ops.py:193`), которым
spec-runner дополняет ignore-файлы spec-директорий (tracked `spec/.gitignore`
в дереве нет) и входит в тот же инвентарь runtime-файлов (NFR-01). Это
«существующий механизм», а не новый IPC — граница OUT (requirements §7).

Судьба child при таймауте — **завершение родителем** (SIGTERM, ограниченное
ожидание, затем SIGKILL), с `pid` и `terminated: true` в ответе. Требования
допускали оба варианта (FR-06, BEH-22); выбор диктует G-03: tool, ответивший
`status: error`, но оставивший живой executor тратить бюджет в namespace —
тот же класс дефекта, что потерянный stop. SIGTERM уже означает для executor-а
graceful shutdown (`executor._signal_handler`), так что завершение не вводит
новой семантики остановки (OUT-01). Побочная выгода — lock освобождается, и
следующий `run_task` не упирается в призрака.

#### Q-03 · owner_role: architects · resolution: resolved

**Обратной сверки effective config child родителем в runtime нет; её место
занимает проверка воспроизводимости до `Popen`, а доказательство совпадения —
E2E BEH-09.**

FR-04 требует отказа **до** запуска child, если parent-config невоспроизводим.
Единственный способ ответить на этот вопрос честно — построить в процессе
родителя тот config, который child получит из тех же входов (YAML по
`project_root` + argv serializer-а через тот же `_build_parser()` и
`build_config`), и сравнить с родительским по всем полям (§ Механика 2.2).
После такой симуляции runtime-эхо от child проверяло бы уже проверенное: те же
YAML, тот же парсер, тот же интерпретатор (CON-05). Что симуляция *не* видит —
изменение YAML между проверкой и стартом child и расхождение окружений — E2E
BEH-09 покрывает против настоящего child, а первое к тому же лежит в поле
допущений любого CLI-запуска. Цена эха — ready-файл превращается в дамп config
с собственным форматом сравнения (Path, dict, personas), плюс ещё одна
error-ветка FR-06 «config mismatch», которую требования вводили условно. Scope
при этом доказывается позиционно: ready-файл читается по пути `spec_dir`
launch namespace и принимается только с `PID`, равным `proc.pid` — child,
взявший lock в чужом namespace, ready в нужном месте не оставит.

## Механика

### 1. Holder и один helper для восьми tools (FR-01, FR-02, FR-07, FR-09)

**1.1 Модуль.** Новый модуль `src/spec_runner/mcp_launch.py` — без импорта
`mcp` SDK: в нём живут `LaunchScope`, предикат противоречия, serializer,
проверка воспроизводимости и ожидание ready. `mcp_server.py` остаётся тонким
слоем `@mcp_app.tool()`-обёрток над `_handle_*` и держит только holder. Так
контракт-тесты BEH-10, BEH-13, BEH-14, BEH-18 не зависят от установленного
`mcp` (опциональная зависимость, FR-07), а `tests/test_lazy_mcp_import.py`
остаётся нетронутым: lazy `__getattr__` в `__init__.py` продолжает отдавать
`mcp_server.run_server`.

**1.2 Holder.** `LaunchScope` — frozen dataclass над resolved `ExecutorConfig`
с производными: `project_root` (resolved absolute), `namespace` в форме
FR-09 (§1.5), `config_path` (какой YAML родитель прочитал — нужен
воспроизводимости, §2.2). Точка врезки — `mcp_server.run_server(config)`:
вместо `_launch_stop_config = config` публикует `_scope = LaunchScope.of(config)`
(module-level, единственный; восстанавливается в `finally`, как сегодня).
`run_server(None)` — programmatic-вход `mcp_run_server()` и плоский
`spec-runner mcp` — строит config из CWD **один раз**, той же `_build_config("")`,
что и сегодня, и кладёт его в holder (BEH-23). Имя `_launch_stop_config`
исчезает; единственная строка существующих тестов, читающая его
(`TestMCPStop::test_change_scoped_server_preserves_launch_stop_namespace`,
`assert server._launch_stop_config is None`), меняется на проверку опустевшего
holder-а — прямое следствие FR-01, не побочный эффект (BEH-28).

**1.3 Helper и предикат.** Все восемь tools получают config через один вызов
`_tool_config(spec_prefix)` в `mcp_server.py`, который делегирует
`mcp_launch.resolve_tool_config(scope, spec_prefix)`:

- пустой prefix → `scope.config`;
- scope с `change_id` и непустой prefix → `ScopeContradiction`;
- scope с `spec_prefix` и prefix, равный ему → `scope.config`; иной → `ScopeContradiction`;
- плоский scope и непустой prefix → **уточнение** (рабочее допущение Q-01):
  config пересобирается «как child» — argv serializer-а (§2.1) плюс
  `--spec-prefix <p>` прогоняются через `_build_parser()` и `build_config` с YAML
  по `scope.project_root`. Одна ветка, один тест (BEH-07, последний And); если
  владелец решит Q-01 иначе — ветка становится четвёртым случаем
  `ScopeContradiction`.

`ScopeContradiction` несёт обе стороны (launch namespace в форме §1.5 и
запрошенный prefix); каждая tool-обёртка ловит его и возвращает JSON
`{"status": "error", "error": "<текст, называющий обе стороны>",
"launch_namespace": {...}, "requested_spec_prefix": "..."}` — в том числе
`logs`, который сегодня отвечает plain-text. Текст — из одного места
(`ScopeContradiction.__str__`), чтобы ни у одного tool не было своей редакции
(BEH-06).

Holder пуст (tools вызваны без `run_server` — только тесты): helper строит
scope из `_build_config("")` **на каждый вызов и не сохраняет его** — иначе
module-level состояние протекало бы между тестами. Это единственная оставшаяся
дорожка к `_build_config` помимо `run_server(None)`; в tool-обработчиках прямых
вызовов не остаётся (M-02: 0/8; BEH-04 проверяет поведением, поднимающим
исключение из двойника `_build_config` при заполненном holder-е).

**1.4 YAML по `project_root`.** `config._resolve_config_path()` сегодня
проверяет относительные `CONFIG_FILE`/`LEGACY_CONFIG_FILE` — то есть CWD
процесса (`config.py:241–242`, `:836`). Точка врезки: `_resolve_config_path`
получает параметр `base: Path` (дефолт — CWD, поведение без флага не меняется),
а `cli.main()` (`cli.py:2439`) передаёт `Path(args.project_root)` когда флаг
задан. Это общее правило для всех команд, не особая ветка `mcp`: смысл
`--project-root` — «проект здесь», и YAML проекта лежит там же. Child тогда
находит тот же файл двумя путями сразу — по флагу и по `cwd = project_root`
(§2.3); `config.config_found` вычисляется от того же пути. `mcp_server._build_config`
для `run_server(None)` продолжает читать CWD — по контракту FR-07 это и есть
project root вызывающего.

**1.5 Форма `namespace` (FR-09).** Фиксируется:
`{"kind": "change", "id": "<change_id>"}` / `{"kind": "prefix", "prefix": "<p>"}` /
`{"kind": "flat"}`. `_handle_status` и `_handle_task_detail` добавляют ключи
`project_root` (str, resolved) и `namespace` к существующим — аддитивно, из
`LaunchScope`, а не из config повторно, чтобы ответ и предикат §1.3 читали одно
и то же описание scope. Те же два ключа входят в JSON `ScopeContradiction`
(`launch_namespace`), так что отказ и статус говорят на одном языке.

### 2. Serializer и воспроизводимость (FR-03, FR-04)

**2.1 Таблица представимости.** В `mcp_launch.py` — одна декларативная таблица
`REPRESENTABLE`: строка = (поле `ExecutorConfig`, флаг `common`, вид: value /
store-true-negation / namespace). Serializer `child_argv(config, task_id)`
перечисляет её и ничего больше:

| Поле config | Флаг | Правило эмиссии |
|---|---|---|
| `project_root` | `--project-root` | всегда, resolved absolute |
| `change_id` \| `spec_prefix` | `--change` \| `--spec-prefix` | ровно один или ни одного (плоский) — одно namespace-поле, два ключа `_COMMON_DEFAULTS` |
| `max_retries` | `--max-retries` | всегда |
| `task_timeout_minutes` | `--timeout` | всегда |
| `run_tests_on_done` | `--no-tests` | эмитить при `False` |
| `create_git_branch` | `--no-branch` | эмитить при `False` |
| `auto_commit` | `--no-commit` | эмитить при `False` |
| `run_review` | `--no-review` | эмитить при `False` |
| `integration_pr` | `--integration-pr` | эмитить при `True` |
| `hitl_review` | `--hitl-review` | эмитить при `True` |
| `budget_usd` | `--budget` | при не-`None` |
| `task_budget_usd` | `--task-budget` | при не-`None` |
| `callback_url` | `--callback-url` | при непустом |
| `log_level` | `--log-level` | всегда |
| `spec_governance` | `--strict` \| `--no-strict` (**run-only**, subparser `run`, `cli.py:1934`–`:1942`) | всегда: `"strict"` → `--strict`, иначе `--no-strict` — child не зависит от своего YAML в этом поле (FR-03 «governance strictness») |

Ключи `_COMMON_DEFAULTS`, которых в таблице нет, перечислены в ней же как
**намеренно непередаваемые с причиной** (BEH-10, второй And): `log_json` — не
поле `ExecutorConfig`, а параметр рендерера логов процесса (`cli.py:2491`);
на effective config не влияет, а вывод child и так уходит в файл (§3.4).
Run-only строка таблицы (`spec_governance`) невидима для `_COMMON_DEFAULTS`:
её флаги живут у subparser-а `run` (`--profile` у `run` нет — `profile_parent`
подключён только к `plan`/`spec`, `cli.py:1898`, `:1984`, поэтому
`spec_profile` непредставим). В `mcp_launch.py` она лежит отдельным перечнем
`REPRESENTABLE_RUN` и сверяется с `dest`-ами действий `run` из
`_build_parser()` тем же тестом паритета (BEH-10, ревью #490).
Паритет — тест с двумя множествами: `common`-флаги таблицы ⊆ ключи
`_COMMON_DEFAULTS`, и `REPRESENTABLE ∪ NOT_FORWARDED == set(_COMMON_DEFAULTS)`;
дополнительно
`_build_parser()` в `cli.py:1860` уже роняет сборку парсера при расхождении
`common` ↔ `_COMMON_DEFAULTS`, так что цепочка serializer → `_COMMON_DEFAULTS`
→ `common` замкнута с обоих концов (RK-05).

Односторонние флаги (`--no-tests` и т.п.) — источник невоспроизводимости:
`run_tests_on_done=True` при YAML `false` флагом не выразить. Это не особый
случай serializer-а, а обычная находка проверки §2.2.

**2.2 Проверка воспроизводимости — симуляция child в процессе родителя.**
`simulate_child_config(scope, argv)`: `load_config_from_yaml(scope.config_path)`
→ `_build_parser().parse_args(argv)` → `build_config(yaml, args)`; результат
сравнивается с `scope.config` по всем полям dataclass (`dataclasses.fields`,
сравнение значений; `Path` — resolved), кроме явного множества
`PARENT_ONLY_FIELDS = {"config_found", "mcp_ready_timeout_seconds"}` —
поля, которые child не потребляет. Непустой дифф → `Irreproducible(fields)`,
и `run_task` отвечает `status: error`, называя каждое поле, его значение у
родителя, значение, которое получил бы child, и почему (нет CLI-флага; YAML по
`project_root` не найден — `scope.config_path` больше не существует). Это
**одна** проверка на оба исхода BEH-13/BEH-14: нет второго места, где решается
«можно ли передать». Она же обслуживает уточнение §1.3 (тот же argv, тот же
парсер).

Порядок в `run_task`: helper §1.3 → governance-гейт `spec_run_gate_ok` (как
сегодня) → `child_argv` → симуляция → только затем §3. До этой точки ни один
файл в namespace не создан (FR-04, третий критерий).

**2.3 Форма инвокации child.** Argv:
`[sys.executable, "-m", "spec_runner", "run", "--task", <id>, *child_argv]`;
`cwd = scope.project_root`; `env` — родительский без добавлений. Для `-m`
добавляется `src/spec_runner/__main__.py`, делегирующий `executor.main()`
(entry point pyproject тот же). `sys.executable` — единственный способ
гарантировать «тот же venv» без поиска по `PATH` (CON-05, BEH-11); argv[0]
child указывает внутрь интерпретатора родителя, что и проверяет тест. Сборка
argv[0..2] вынесена в `child_entry()` — seam, который E2E BEH-09 подменяет на
тестовый entry point, записывающий resolved config через тот же
`_build_parser()`/`build_config` (§ Рамки red-дизайна).

### 3. Startup-handshake (FR-05, FR-06)

**3.1 Сторона child.** `ExecutorConfig.ready_file` — property рядом со
`stop_file` (`config.py:564`): `spec_dir / ".executor-ready"`; путь входит в
`git_ops.runtime_state_paths` (`git_ops.py:24`), чтобы stash-rescue, staging и
`.gitignore`-инвентарь видели его как runtime. Публикация — в
`cli._run_tasks_inner` **сразу после** `clear_stop_file(config)` (`cli.py:831`)
и до `parse_tasks`: атомарная запись (temp + `os.replace`) двух строк
`PID: <os.getpid()>` / `Started: <iso>` — тот же формат, что у lock, тем же
кодом чтения. Это единственная точка: после неё ни одна стартовая ветка
`clear_stop_file` не вызывает (три точки потребления `:831`, `:1055`, `:1274`
не трогаются, OUT-01). Снятие — в `cmd_run` `finally` рядом с `lock.release()`
(`cli.py:258`): обычный CLI-прогон файла не оставляет. Публикация идёт при
каждом `run`, не только под MCP — child не знает, кто его запустил, и
не должен; знак «меня ждут» в argv не передаётся.

**3.2 Сторона родителя.** `mcp_launch.wait_for_ready(proc, ready_file, timeout)`
— цикл с коротким сном: (а) файл существует и `PID == proc.pid` → unlink →
`Ready`; (б) `proc.poll()` не `None` → `Exited(returncode, log_tail)`;
(в) прошло больше `timeout` → `proc.terminate()`, ожидание ограничено,
`proc.kill()` при необходимости → `TimedOut(pid, terminated=True)`; в ветках
(б)/(в) файл с нашим `PID`, если успел появиться, снимается. Ready-файл с
чужим `PID` игнорируется: живой — чей-то executor, мёртвый — мусор после
падения, который родитель снимает до `Popen` (BEH-22, третий And). Таймаут —
`ExecutorConfig.mcp_ready_timeout_seconds` (дефолт 60 с; добавляется в
`load_config_from_yaml` и тем самым в `KNOWN_EXECUTOR_KEYS`), поле только
родителя (§2.2).

**3.3 Popen.** `subprocess.Popen(argv, cwd=project_root, stdin=DEVNULL,
stdout=<log>, stderr=STDOUT)`, где `<log>` — файл под `config.logs_dir` launch
namespace с именем, начинающимся с `task_id` (чтобы существующий glob
`_handle_logs` `f"{task_id}*"` его находил, BEH-12 второй And). `stdin=DEVNULL`
обязателен: stdin сервера — это MCP-транспорт, и child, унаследовавший его,
съел бы сообщения протокола; сегодня он наследуется. `PIPE` не используется
(RK-01).

**3.4 Формы ответов `run_task`.** Успех:
`{"status": "started", "task_id", "pid", "lock_file", "log_file"}` —
`lock_file` = `config.state_file.with_suffix(".lock")`, тот, что E2E BEH-15
читает. Ошибки — `{"status": "error", "error": <причина>, ...}` с
дополнительными ключами по ветке: `Exited` → `exit_code`, `log_tail`
(последние N строк `log_file`), `log_file`; `TimedOut` → `pid`, `terminated`,
`log_file`; `Irreproducible` → `fields: [...]`; `ScopeContradiction` — §1.3.
Существующие ключи ответов (`status`, `task_id`, `pid`, `error`) сохранены.

**3.5 Наблюдаемое stop в single-task режиме.** Child — `run --task <id>`:
`tasks_to_run = [task]` (`cli.py:945`–`:951`), исполняется fixed-list ветка
(`cli.py:1269`–`:1279`), где единственная проверка marker-а стоит **до**
`run_with_retries` (`:1273`); цикл с проверкой «между задачами» (`:1017`,
`:1054`) живёт только под `--all`. Handshake это не меняет (OUT-01), поэтому
E2E FR-05 наблюдает не «следующая задача не начата» (для `--task` вакуумно),
а «marker после `started` не стёрт»: детерминированно — fake command сигналит
о вызове и ждёт освобождения, `stop()` пишется после сигнала, после выхода
child файл на месте; для немедленного `stop()` — инвариант двух легальных
исходов (потреблён до задачи ∨ пережил задачу) с запрещённым «файла нет ∧
попытка есть» (BEH-16, BEH-19).

### 4. Формы коммитов и эвиденции

Репо под governance (CON-02): `execution_mode: tdd`, `review_policy: required`,
integration PR. Отсюда формы, которые decomposition-стадия должна принять как
данность:

- **Коммиты.** Каждая задача — ветка `task/TASK-###-<slug>` на integration
  branch; первый коммит задачи — red: один падающий тест, названный по BEH-id,
  с `TDD_SELECTOR`; далее green и, при необходимости, отдельный коммит с
  документацией (README/CHANGELOG — FR-08) — документация не входит в red.
  `spec-runner.config.yaml` и `spec/.tdd-evidence/*` под `harness_guard: strict`
  задачей не редактируются; новый ключ `mcp_ready_timeout_seconds` — это код
  loader-а, не правка YAML репо.
- **Эвиденция в PR.** (1) Ссылка на CI-прогон с длительностью базового E2E
  (NFR-02 — замер, не оценка); (2) вывод ручного soak `-m slow` ×20 (NFR-03),
  вставленный в описание PR; (3) вывод `grep -n _build_config
  src/spec_runner/mcp_server.py` до/после как предъявление M-02 (7/8 → 0/8);
  (4) диффы README §MCP Server и CHANGELOG; (5) ссылка на issue-handoff
  соседям (ADR-ECO-006).
- **Закрытие #485** — ссылкой на PR и на тест BEH-16 (`test_stop_after_started_is_honoured`
  или его переименование по §9 требований).

## Рамки red-дизайна

Задачи делятся на три класса; для каждого — что red обязан предъявить живьём и
чего утверждать не должен, чтобы не окаменить структуру вместо поведения.

**Задачи-измерения** (M-02 «0/8», NFR-02 «< 60 с», NFR-03 «×20», BEH-10
паритет).
- MUST verify live: BEH-04 — заполнить holder, подменить `_build_config`
  двойником, поднимающим исключение, вызвать все восемь tools и получить восемь
  ответов без исключения; BEH-10 — два множества, вычисленные из живых
  `REPRESENTABLE`/`NOT_FORWARDED` и `_COMMON_DEFAULTS`, равны; soak — цикл ×20
  под `slow` с очисткой namespace между итерациями.
- MUST NOT assert: результат `grep` по исходнику (это эвиденция PR, не тест —
  grep красен от комментария); wall-clock «< 60 с» внутри теста (флаки; замер
  — CI); имена приватных функций helper-а; число строк в `mcp_server.py`.

**Задачи-артефакты** (`__main__.py`, README/CHANGELOG, `ready_file` property,
ключ `mcp_ready_timeout_seconds`).
- MUST verify live: `-m spec_runner` — запуск `[sys.executable, "-m",
  "spec_runner", "--version"]` (или `validate` во временном проекте) с кодом 0;
  `ready_file` — что после `clear_stop_file` в `_run_tasks_inner` файл
  существует с `PID` процесса и исчезает после `cmd_run` (через настоящий
  вызов, не через чтение property); таймаут — что переопределённое малое
  значение действительно обрывает ожидание (BEH-18) и что ключ из YAML доезжает
  до `ExecutorConfig` через `load_config_from_yaml`.
- MUST NOT assert: существование `src/spec_runner/__main__.py` как файла;
  наличие строк в README/CHANGELOG (BEH-24 — `kind: manual`, ревью PR);
  литерал пути `.executor-ready` вместо `config.ready_file`; вхождение ключа в
  `KNOWN_EXECUTOR_KEYS` напрямую (это следствие, проверяемое через loader).

**Задачи-границы** (NFR-01/BEH-26, BEH-03, BEH-02, BEH-13, BEH-20…22).
- MUST verify live: `rglob(".executor-*")` по плоскому `<external>/spec/` и по
  CWD сервера после **настоящих** вызовов, включая отказные ветки; для BEH-02 —
  два YAML с противоположными `spec_governance` и наблюдение, какой применён,
  через ответ `run_task` (не через чтение `config_path`); для BEH-13 —
  programmatic-override и подсчёт вызовов двойника `Popen` (ноль); для
  handshake — хотя бы один red на настоящем child-процессе (`sys.executable`),
  остальные могут использовать двойник `Popen`, который сам создаёт ready-файл
  с нужным `PID` или выходит с кодом; порядок «`started` не раньше lock и
  ready» — через двойник, фиксирующий момент публикации, с щедрым запасом.
- MUST NOT assert: отсутствие символа `_launch_stop_config` или наличие класса
  `LaunchScope` по имени (структура); точный текст ошибки сверх называния обеих
  сторон конфликта / имени поля / слова `timeout`; интервал опроса
  `wait_for_ready`; что child **не** был убит на успешном пути (проверяется
  завершением после задачи, а не сигналами); тайминги с узким запасом
  («ровно 2 с»).

Общее для всех классов: red не вызывает платного агента (conftest-пояс
`PaidBinaryReached`, NFR-02) — child в E2E работает на `tests/fixtures/fake_claude.sh`
с `FAKE_EXIT_CODE`/`FAKE_RESPONSE_FILE`, многословный вывод (BEH-12) — response-файл
≥ 1 MiB.

## Карта затрагиваемых модулей

| Файл / подсистема | Что меняется | Сценарии |
|---|---|---|
| `src/spec_runner/mcp_launch.py` (новый) | `LaunchScope`, `ScopeContradiction`, `resolve_tool_config`, `REPRESENTABLE`/`REPRESENTABLE_RUN`/`NOT_FORWARDED`, `child_argv`, `simulate_child_config`/`Irreproducible`, `child_entry`, `wait_for_ready` (+ `Ready`/`Exited`/`TimedOut`); без импорта `mcp` | BEH-05…07, 10, 13, 14, 18, 20…22 |
| `src/spec_runner/mcp_server.py` | holder `_scope` вместо `_launch_stop_config`; `_tool_config`; все 8 обёрток через него; `run_task`: гейт → argv → симуляция → `Popen` (cwd, DEVNULL, лог) → `wait_for_ready` → формы ответов; `status`/`task_detail` + `project_root`/`namespace`; `_build_config` только для `run_server(None)` и пустого holder-а | BEH-01, 03, 04, 08, 09, 11, 12, 15…17, 23, 25, 26 |
| `src/spec_runner/__main__.py` (новый) | `python -m spec_runner` → `executor.main()` | BEH-11 |
| `src/spec_runner/config.py` | `ready_file` property; `mcp_ready_timeout_seconds` (поле + loader); `_resolve_config_path(base)` | BEH-02, 15, 17, 18 |
| `src/spec_runner/cli.py` | `main()` передаёт `project_root` в `_resolve_config_path`; `_run_tasks_inner` публикует ready после `clear_stop_file`; `cmd_run` снимает ready в `finally` | BEH-02, 15, 17 |
| `src/spec_runner/git_ops.py` | `runtime_state_paths` + `ready_file` | BEH-26 (инвентарь), rescue/staging |
| `tests/test_mcp.py`, `tests/test_mcp_v2_wire.py` | двойники `Popen` в `TestMCPRunTask` имитируют handshake; одна assert-строка holder-а | BEH-28 |
| `tests/test_mcp_launch_scope.py`, `tests/test_mcp_serializer.py`, `tests/test_mcp_e2e_485.py` (новые; имена — ожидание §9 требований) | контракт / интеграция / E2E + soak | по матрице behaviour-spec |
| `tests/fixtures/fake_claude.sh` | без правок, если хватает `FAKE_*`; иначе — специализированный двойник рядом | BEH-12, 21, 27 |
| `README.md` §MCP Server, `CHANGELOG.md` | привязка к launch scope, отказ на противоречащем prefix, значение `started`, `mcp_ready_timeout_seconds`, `.executor-ready` в инвентаре | BEH-24 |
| `docs/state-schema.md` / `schemas/*` | **не меняются**: ни schema state DB, ни `--json-result` не затронуты; `.executor-ready` — не state | — |
| Соседи (devtools, spec-runner-vscode) | только issue/handoff | BEH-24 |

## Вне объёма

Дизайн намеренно оставляет реализатору (под TDD, в границах разделов выше):

- Точные имена приватных функций, сигнатуры dataclass-ов и порядок полей;
  где именно внутри `mcp_launch.py` живут части — лишь бы модуль не
  импортировал `mcp`.
- Интервал опроса `wait_for_ready`, длительность ожидания между SIGTERM и
  SIGKILL, число строк `log_tail`, формат имени лог-файла сверх префикса `task_id`.
- Формулировки текстов ошибок сверх обязательного: обе стороны конфликта
  (FR-02), имена полей и причина (FR-04), код выхода / `timeout` (FR-06).
- Нужен ли `start_new_session` для child — сегодня его нет, требования не
  просят; менять только по находке E2E.
- Порядок и нарезка задач — стадия decomposition; карта модулей — её вход, не
  предписание «одна строка = одна задача».
- Судьба `fake_claude.sh`: расширять или писать двойник рядом — по мере
  того, что потребуют BEH-12/BEH-21.
- Дефолт `mcp_ready_timeout_seconds` (60 с) — уточняется по замеру CI, если
  BEH-27 покажет, что старт child до `clear_stop_file` на CI дольше ожидаемого.
- Вырезание FR-09 при сокращении объёма — решение владельца; здесь
  зафиксирована только форма поля на случай реализации.
