---
spec_stage: decomposition
status: draft
owner_role: tech-lead
traces_to:
- design
- acceptance
upstream_hashes:
  design: 4f3a2257ec1109bd8e82162d9f05d9b5f17b8032
  acceptance: 35a79dacd4e648a68435689f2349a7503b966b37
---

# Decomposition — Durable continuation checkpoint и evidence для run/call/attempt (spec-runner#480)

Стадия `decomposition` governance-бандла
`workstreams/durable-continuation-checkpoint-evidence-20260915/`. Режет доставку
на задачи и объявляет граф их зависимостей поверх design (`20-design.md`) и
acceptance (`25-acceptance.md`); их ревизии пинованы в frontmatter
`upstream_hashes` этого узла. Резолюции
design — Q-02 (ack = возврат `put` store-адаптера до `Popen`, spool ack-ом не
является), Q-03 (WIP — tar с `git bundle` и байтами dirty/untracked), Q-05
(локальный snapshot синхронно, один упорядоченный publisher, drain перед
call-start и перед closure, manifest последним), Q-06 (seam в `paid_call.py`,
`_spawn` — единственный spawn провайдера), Q-07 (состав `PolicyIdentity`), Q-08
(движок redaction), Q-11 (retention считает spec-runner, удаляет адаптер,
audit-запись в store), Q-12 (open calls на старте `run` — из `open`-строк DB с
targeted `get` в store; `restore` читает только store) — здесь входные условия
и не переоткрываются. Термины — в значении §3 требований и «Области
поведения» behaviour-спеки.

**Нарезка продиктована владением тестовыми файлами.** Единственный владелец на
файл считается исключительно по `checked_by` сценариев (через `scenarios`), и
behaviour-спека раздала 46 сценариев по 23 целям: 21 тестовый файл (из них
четыре существующих — `tests/test_config.py`, `tests/test_state.py`,
`tests/test_cli_info.py`, `tests/test_harness_guards.py`) и две цели `kind:
manual` (`scripts/bench_durability.py`, `docs/architecture.md`). Два сценария
одного файла не могут лежать на разных задачах — иначе у файла два владельца, и
«зелёное» перестаёт быть про одну из них. Отсюда четырнадцать задач, и их
границы проходят там, где проходят границы файлов, а не там, где их провела бы
карта модулей design'а: `tests/test_call_start_before_spawn.py` требует seam
на **всех** сайтах разом (BEH-05), `tests/test_restore_drill.py` несёт и
Git-материал, и сам restore, и sweep по продуктовому Git (BEH-16…19, BEH-43),
`tests/test_closure_every_exit.py` наблюдает closure у команд, которых до
`restore` и `evidence` не существует (BEH-04). Более тонкая нарезка потребовала
бы развести `checked_by` behaviour-спеки по большему числу файлов — это правка
upstream'а, не этого документа (см. «Вне объёма»). Карта модулей design'а —
вход, не предписание «одна строка = одна задача»; design сам называет свою
последовательность (store + publisher + redactor → `RunContext`/run-start/
closure → seam + столбцы → checkpoint + spool → WIP → restore → evidence
read-surface → retention) разумной, но изменяемой — здесь она изменена в двух
местах, и оба названы: read-surface `evidence` поднят раньше полного
checkpoint-а (BEH-13 наблюдает, что `evidence <run_id>` ничего не публикует, — команда
должна существовать), а spool разрезан по чтению и записи (restore обязан
проверить и доиграть spool по BEH-19/BEH-20 (7) раньше, чем degraded mode
начнёт его писать по BEH-33).

**Условие на `verify`: baseline-эвиденция.** Тип `verify` допустим только там,
где носитель существует в дереве и поведение уже доставлено задачами из
`delivered_by`; мост исполняет такую задачу как verify_first — живой прогон
объявленной группы стоит первым действием, и зелёное означает, что красный не
покупается. В этом графе таких задач **нет**: ни один сценарий BEH-01…BEH-46
сегодня не утверждён никаким файлом дерева без правок — четыре существующих
файла-носителя получают новые утверждения (миграция столбцов, `run_id` в
`status`, `durability:` в loader-е, пояс на `paid_call._spawn`), и правящая их
задача — `implement`. Все четырнадцать задач — `implement`, у каждой честный
baseline-RED достижим в её собственных границах, и `tdd_waiver` не объявлен ни
разу. Задачи-измерения design'а (BEH-05, BEH-13, BEH-22, BEH-39) остаются
`implement`, потому что каждая вносит в seam то, чего там нет, — общий журнал
двойников, точки инъекции, единственность пути, — и красный у них честный:
измерять до задачи нечем.

**Документация — не отдельная задача.** Под `execution_mode: tdd` задача без
исполняемого сценария не имеет честного red; единственный санкционированный
класс waiver-а документацию не покрывает. По design §8 схемы, `docs/
state-schema.md`, `docs/architecture.md` и CHANGELOG идут отдельными коммитами
**той задачи, которая вводит контракт** (столбцы — с миграцией, manifest-схема —
с checkpoint-ом, `restore-result` — с restore), а сводный manual-критерий
BEH-45 лежит на последней задаче графа (DT-14) — там, где документ может
описывать доставленное целиком. Человеческие действия BEH-45 (закрытие #480,
пункт `TODO.md`, issue соседям по ADR-ECO-006) названы в теле DT-14 как
приёмочные пункты, не как предмет автотеста. Бенчмарк BEH-41 (`kind: manual`,
цель — сам скрипт) лежит на DT-06 вместе с его CI-половиной
`test_validation_under_60s`, которая живёт в файле той же задачи.

## Задачи

#### DT-01: Store-контракт, `LocalVolumeStore`, блок `durability:` в config и validate · type: implement · owner: dev
scenarios: [BEH-28]
depends_on: []
parallel_group: core

Предмет — design §1.1–1.3 и половина §1 карты модулей: новый
`src/spec_runner/artifact_store.py` с протоколом `ArtifactStore` (`put` с
семантикой `if_none_match` всегда → `AlreadyExists` на существующем ключе,
`get`, `list`, `delete`), декларацией `StoreCapabilities(tls,
encryption_at_rest, immutable_put, lifecycle)`, функцией ключей §1.3
(`runs/<run_id>/…` и индекс workstream-а `workstreams/<workstream_key>/runs/…`
парой `.json`/`.closed` — каждый ключ пишется один раз; `workstream_key`
считается от repository identity и `spec_prefix`/`change_id`, абсолютный путь
в него не входит) и первым адаптером
`LocalVolumeStore(root)` (временный файл → `fsync` → `link`/`rename` с
`O_EXCL`-семантикой; `tls: n/a`, `encryption_at_rest` — по декларации оператора
в options, `lifecycle: none`); `open_store_readonly` — единственный
`Publisher`-less вход, разрешённый design §7.1 по имени. В `config.py` —
блок `durability:` (`store: {adapter, options…}`, `ack`, `ack_timeout_seconds`,
`checkpoint_ack_timeout_seconds`, `retention_days`) в поля `ExecutorConfig` и
тем самым в `KNOWN_EXECUTOR_KEYS`; **на загрузке** `ConfigError`, если адаптер
объявил `tls: false`, не объявил `encryption_at_rest` или `immutable_put`, или
`retention_days` вне 7–365; `validate.py` повторяет те же проверки в отчёте.
Путеподобные `store.options` (у первого адаптера — `root`) резолвятся **на
загрузке** в абсолютные против `project_root`, а не лениво при первом `put`
и не против CWD (design §1.1): процесс вправе сменить рабочий каталог внутри
себя, и относительный адрес, разрешённый после `os.chdir` пробы `doctor`
(`doctor.py:338`), указал бы внутрь каталога, который тут же удаляется
(`:408`) — платный вызов без единой durable-записи. Наблюдаемое —
последний And BEH-47 (ключи на месте после `doctor` при относительном
`root` в config-е), красный до этой строки.
Свойства путей `.executor-checkpoints`/`.executor-spool.jsonl` (с
`spec_prefix`/`change_id` как у `state_file`) и их включение в
`git_ops.runtime_state_paths` — здесь же, чтобы каждая следующая задача брала
путь из config, а не из литерала.

Границы: `Publisher`, redactor и `bound_evidence` — DT-02 (их наблюдают
BEH-05/BEH-26, файлы DT-02); второй адаптер — отдельное одобрение владельца
(CON-08), не задача графа; `evidence purge` и retention-вычисление — DT-12,
здесь только диапазон в loader-е (последний And BEH-28). Владеет существующим
`tests/test_config.py` (правка по предмету BEH-28). Red-рамки по design:
`load_config_from_yaml` с плохим адаптером → `ConfigError` с именем адаптера и
недостающего свойства, `spec-runner validate` — та же ошибка, `run` с таким
config-ом не доходит до run-start (двойник store пуст); не утверждать вхождение
ключей `durability.*` в `KNOWN_EXECUTOR_KEYS` напрямую, имена приватных
функций адаптера, литералы путей.

#### DT-02: Run identity, run-start/closure, publisher с redactor-ом и единый seam платного вызова на всех сайтах · type: implement · owner: dev
scenarios: [BEH-03, BEH-05, BEH-06, BEH-07, BEH-24, BEH-26, BEH-38, BEH-44]
depends_on: [DT-01]
parallel_group: core

Предмет — design §1.4, §2 и §6.1–6.3 в их первой, «рабочей» форме, и
столбцы §2.3. Три новых модуля: `run_context.py` (`RunContext` — full UUIDv4
`run_id`, `pipeline_id` из structlog contextvars после `setup_logging`,
`subcommand`, `started_at`, `Publisher`, `policy`; создаётся в `cli.main()`
вместо `bind_contextvars(run_id=uuid4().hex[:8])`, `cli.py:2504`; `start()` —
**одна** точка: диспетчер `main()` перед вызовом handler-а по множеству
`PAYING_SUBCOMMANDS` (рядом с ним — `BLOCKING`/`NON_BLOCKING` шага 5 § 7.2 и тест их полноты, предмет DT-06), так что `retry`, `watch` и `run --force`, не берущие
executor lock, получают run-start наравне с обычным `run`; `_acquire_run_lock`
run-start **не** пишет и вообще не правится — его `sys.exit(1)` доходит до
`finally` диспетчера; `close(exit_code)` из `try/except SystemExit/except
BaseException/finally` вокруг dispatch; meta
`last_run_id`/`last_pipeline_id` — и только их: маркер `continuation_index`
`start()` **не** пишет, его писатель и читатель один и тот же и он в DT-09,
иначе запись из диспетчера до handler-а перекрыла бы `restored` и отсутствие
маркера прежде их чтения — design § 6.2, § 7.4, Q-12), `closure.py` (`CLOSURE_KINDS` — пять
значений `completed`/`refused`/`failed`/`interrupted`/`crashed`;
`run-closure.schema.json` с пинованным словарём kinds) и `evidence.py` (`Publisher` — единственный владелец экземпляра
`ArtifactStore` и единственный импортёр `_open_store`; `publish(record)` через
`redaction.redact()` по каждому текстовому полю, SHA-256, `put`; записи
`RunStart`/`CallStart`/`CallResult`/`Closure`; `bound_evidence(text, limit)`
над `prompts_log.bound` с head **и** tail, marker, `full_sha256`, `full_size`,
пределы 1 MiB / 4 MiB), плюс `redaction.py` (denylist из окружения + паттерны,
placeholder `[REDACTED:kind:hash8]`, общий словарь имён с
`obs._DEFAULT_REDACT_KEYS`). Seam — `paid_call.py`: `PaidCall`, `CallOutcome`,
`execute(config, state, call)` в порядке §2.2 шаги 2–8 (шаг 1 — drain
checkpoint-ов — врезает DT-03, когда checkpoint-ы появятся), `_spawn` —
единственная функция репо, передающая argv провайдера в `subprocess.run`;
autouse-guard `_no_real_agent_calls` переключается на **одно** имя
`paid_call._spawn`, а два его патча швов (`tdd._run_agent`,
`execution._run_agent_process`) снимаются — тем же коммитом, что переводит
последний сайт на seam, и тем же, что переписывает
`tests/test_harness_guards.py` на новое имя: окна, где швы уже не патчатся,
а `_spawn` ещё не проверен, быть не должно. Иначе рецепт BEH-05 неисполним
— `_refuse_tdd` (`tests/conftest.py:296-299`) и `_refuse_execution`
(`:316-323`) поднимают отказ на настоящем имени `claude` раньше, чем
управление дошло бы до `_spawn` (design Q-06). Ключ гварда — argv, как уже
у `_refuse_execution`; сообщение отказа называет сайт по `provenance` из
`PaidCall`; тип отказа — **от `BaseException`** (design Q-06,
пункт (4) цены): `RealAgentCallRefused` не годится, это `AssertionError`
(`execution.py:504`), и `except Exception` у `run_code_review`
(`review.py:776`), интерактивного `plan` (`cli_plan.py:892`) и post-PR
стадии, зовущей `review-pr` (`cli.py:440`), проглотили бы его — verdict
`error` и exit 0 вместо красноты. Тем же коммитом: `RealAgentCallRefused` и
ветка `except RealAgentCallRefused: raise` удаляются (поднимать их больше
некому), а `tests/test_task_015_c560727b864370c7_1eb83bfd_red.py:79`, чей
`pytest.raises(AssertionError)` ключуется на старом типе, переписывается на
новый. Пояс `_belt_never_executes_a_paid_binary` не трогается и страховкой
здесь не работает: процесс в этом отказе не создаётся. Тест пинует свойство
**вне** `execute_task`. `call_id` чеканит **сайт** — до `log_prompt` и
до `execute`, — и передаёт его полем `PaidCall`; `execute` его не создаёт, а
проверяет (шаг 2 §2.2 — сборка `CallStart` из пришедшего id, не чеканка), так
что заголовок prompt-артефакта и call-start несут одно значение и сигнатура
`PaidCall` не переписывается в DT-07. Второй, асинхронный путь к бинарю
провайдера снимается здесь же: `runner.run_claude_async` удаляется вместе с
публичным экспортом (`__init__.py:58`, `__all__:156`) — в дереве у него нет
продуктовых вызывающих, — CHANGELOG под Unreleased называет удаление.
Диаграммы `docs/architecture.md` и `CLAUDE.md` правит **DT-14**: файл —
её `checked_by`-цель (BEH-45), и владение файлом есть право его править;
между DT-02 и DT-14 диаграммы называют удалённую функцию — промежуточное
состояние integration-ветки, названное в «Порядке и параллельности». После
удаления
«`_spawn` — единственный spawn» истинно по построению дерева, и статический
тест BEH-44 утверждает, что `asyncio.create_subprocess_exec` не вызывается ни
из одного модуля `src/spec_runner/`. Все сайты §2.4 переводятся на `execute`:
`tdd._run_agent` (`red`, `red:fix`), `execution._run_agent_process`
(`green`), `review._run_reviewer` (`_record_call` — шаг close),
`review_pr.verify_comment`/`run_fix_agent` (`_record_pr_call` — шаг close,
`CostGuard` до `execute`), **все три** платных сайта `cli_plan.py` —
`:170` (`_generate_stage_draft`, gated; параметр `invoke=subprocess.run`
`:94` снимается), `:660` (`--full`, каждая стадия) и `:797`
(интерактивный цикл `cmd_plan`, каждый круг; `cmd = [claude_command, "-p",
prompt]` `:791` → `build_cli_invocation`) — с `build_cli_command` →
`build_cli_invocation` + `parse_cli_result`, provenance `plan:<stage>` для
первых двух и `plan:interactive` для третьего, ledger — **новая**
`plan_agent_calls` (design §2.3: третья таблица семьи по образцу
`pr_agent_calls`/#218, без столбца `task_id` вовсе — `agent_calls.task_id`
остаётся `NOT NULL`, и смены типа столбца задача не делает); `costs` —
строка «planning» по образцу `pr_cost_rows`, `repo_total_cost` включает её;
`doctor` — через `execute_task`, и **с правкой** (design §2.7,
spec-runner#525): `doctor.build_scratch` объявляет область пробы на своём
scratch-конфиге — одно поле `probe_provenance: "doctor"` (его наличие и
есть объявление «это проба», оно же даёт префикс), пин
`execution_mode = "standard"`, обнуление `review_parallel`/`review_roles`, —
а seam отображает provenance сайта по закрытой карте
`{green: "<probe>:execute", review: "<probe>:review"}`, подставляя значение
поля вместо `<probe>` (имени `doctor` в seam-е нет), и публикует её записи с
`task_id = null`. Карта — рядом с
`PaidCall`, provenance сайта вне карты при заполненном поле —
`Refusal(kind="instrument")` до записи call-start. Без этой правки значения словаря
`doctor:execute`/`doctor:review` не производил бы ни один сайт (их требует
FR-02 и пинует BEH-05), проба под проектом `tdd` делала бы третий платный
вызов вопреки своему cost gate, а опубликованный `task_id` канонной
`TASK-001` блокировал бы **одноимённую реальную** задачу проекта после
ветки (2) Q-12. Поле — в `config.py` (DT-01 его не знает), читатели —
`after_mutation` (DT-03) и
`export_attempt` (DT-04); полный знак области — BEH-47 в DT-05. Третий сайт
`cli_plan.py` назван здесь
поимённо, потому что он достижим любым `spec-runner plan "<описание>"` без
флагов и, оставшись непереведённым, дал бы платный вызов без call-start в
обход FR-02.
Budget guard (#213) и `log_prompt` (#282) остаются на сайтах **до**
`execute` — «a refusal is no row» буквально (BEH-07). Столбцы — аддитивно в
`_migrate` по образцу #218: `agent_calls`/`pr_agent_calls` — `run_id`,
`call_id`, `status` (`open`/`closed`/`not_started`), `started_at`; `attempts`
— `run_id`; плюс `CREATE TABLE IF NOT EXISTS plan_agent_calls` там же; строка `open` пишется до store-ack и закрывается после
call-result (§2.3, Q-12). `--json-result` (`build_task_json_result`) и
`status --json` — аддитивные `run_id`/`pipeline_id`; `status` — строка с
`run_id` последнего run-start namespace-а из `executor_meta`, `null`/
отсутствие для namespace без прогонов по новому контракту; `docs/
state-schema.md` и `schemas/executor-state.schema.json`, `json-result`,
`status` — minor bump, golden-фикстуры `tests/fixtures/maestro-interop/` —
только добавление ключей (отдельные коммиты задачи, design §8).

Границы. Здесь closure пишется на штатном выходе платящей подкоманды и
знает пять kind-ов; само правило вывода `closure.derive(outcome)` — его
чтение исхода работы из DB, обработка сигналов и неперехваченных исключений,
drain перед closure и отказные режимы записи — DT-10 (файл
`tests/test_closure_every_exit.py` его). Сайты выхода подкоманд ни здесь, ни
там не правятся: kind выводится в диспетчере (design § 6.3). Детекция `open`-строк на
старте `run` (процедура Q-12) и дверь `evidence close-call` — DT-09: между
DT-02 и DT-09 `open`-строка после crash-а на старте следующего `run` **не**
распознаётся — известное промежуточное состояние integration-ветки, названное
в «Порядке и параллельности». Каналы OTel/audit-log/prompt-заголовок — DT-07;
здесь `AuditLogger` и `obs` не трогаются, `log_prompt` не получает `call_id`
(BEH-01 наблюдает это в файле DT-07). Экспорт attempt-а — DT-04. Владеет
`tests/test_call_start_before_spawn.py` (новый), `tests/
test_planning_has_ledger_identity.py` (новый), `tests/
test_bounded_evidence_logs.py` (новый), `tests/test_state.py` (существующий,
правка по предмету BEH-03), `tests/test_cli_info.py` (существующий, по
предмету BEH-38), `tests/test_harness_guards.py` (существующий, по предмету
BEH-44 и BEH-05 — гвард переписан на `paid_call._spawn`, пояс без
изменений). `tests/conftest.py` правится по предмету
guard-а и общих фикстур (двойники store/`_spawn`, `RunContext`), а
`tests/test_runner.py` и `tests/test_events.py` — по предмету удаления
`run_claude_async` (снятие осиротевших тестов стримингового пути); ни один из
трёх носителем сценария не является и никем другим не заявлен; `tests/
test_json_result_contract.py`, `tests/test_costs.py`, schema-тесты — зелёные
без изменения ожиданий, кроме аддитивных ключей (BEH-44), любое иное — находка
ревью.

Red-рамки по design («задачи-измерения» и «задачи-границы»): BEH-05 — общий
журнал двойников store и `_spawn`, где перед каждым `spawn` непосредственно
стоит `call_start` с тем же `call_id`, по **настоящему** прогону каждого сайта
с fake CLI, число `spawn` == число acknowledged call-start, а не вызов seam-а
напрямую; BEH-06 — двойник store, отказывающий в `put` call-start → 0 вызовов
`_spawn`, attempt `error_code=INFRASTRUCTURE`, exit 2, closure
kind `failed` с exit 2 — через
`spec-runner run --task` целиком; BEH-03 — старая DB-фикстура открывается,
старые строки читаются с `run_id IS NULL`, `costs` даёт прежнюю сумму; BEH-26
— `full_sha256` равен digest-у исходника при усечённом теле. Красная половина рецепта BEH-05 — матрица сайтов с настоящим именем
`claude` в `claude_command` и двойником `_spawn`: двойник теста заменяет
патч гварда (документированное свойство гварда), пояс остаётся под ним, и
единственный наблюдаемый признак — журнал `call_start`/`spawn`. Не
утверждать: `grep` по исходнику на `subprocess.run` (пояс `PaidBinaryReached`
ловит обход живьём, grep красен от комментария), имена приватных функций
seam-а, порядок
внутренних шагов сверх «call-start acked → spawn → result», число строк
`state.py`, конкретные regex redactor-а.

#### DT-03: Checkpoint: seam «после mutation», backup-snapshot, `sequence`, `PolicyIdentity`, manifest и очередь publisher-а · type: implement · owner: dev
scenarios: [BEH-12]
depends_on: [DT-02]
parallel_group: core

Предмет — design §3.1–3.2, §3.4–3.5 в объёме одного сайта записи. Новый
`checkpoint.py`: `after_mutation(config, *, table, task_id=None, conn=None)` —
единственная точка публикации; snapshot через `sqlite3.Connection.backup()`
с живого соединения в `<state_dir>/.executor-checkpoints/<seq:06d>-<id>/
state.db` (backup API читает через pager и включает WAL-only страницы — то,
что BEH-12 отличает от `shutil.copy`); `sequence` — монотонный счётчик в
`executor_meta` под `checkpoint_seq:<run_id>`; ротация локальных копий
(последняя + предыдущая, старшие — после ack преемника); `PolicyIdentity`
(`contract_version`, `config_hash` над `POLICY_KEYS`, `namespace`,
`namespace_source`, `spec_prefix`, `change_id`) — одна dataclass для
run-start, call-start и manifest-а, `facts` рядом, не в identity; manifest по
`schemas/checkpoint-manifest.schema.json` (схема — полный состав §3.3, вводится
здесь отдельным коммитом) с полями identity, `sequence`, `supersedes`,
`join_keys`, `digests`, `degraded: false`, `wip: none`, `spool: none`,
`manifest_sha256` — repository/workstream/`refs[]`/`excluded` и WIP заполняет
DT-05. `Publisher` получает очередь по `sequence`, `drain(timeout)` и
`last_acknowledged()`; manifest кладётся последним, ack manifest-а = ack
checkpoint-а; **три** точки drain (Q-05): перед call-start — шаг 1 `execute`
(таймаут `checkpoint_ack_timeout_seconds` → `Refusal(kind="instrument")`,
вызова нет), перед closure (`last_checkpoint_id = last_acknowledged()`) и —
третья, spec-runner#527 — **сразу после mutation** у подкоманд, у которых
платного вызова нет вовсе и первая точка потому не наступает никогда
(`budget authorize`, `tdd abandon|repair|resume|release`): решение принимает
сама `after_mutation` по `run_context.current().subcommand`, а не сайт
подкоманды, — иначе у каждой был бы свой сайт ожидания и подкоманда,
добавленная после бандла, тихо осталась бы без него (тот же довод, по
которому kind closure выводит диспетчер). Таймаут здесь → `Refusal(kind=
"instrument")` наружу, то есть exit 2 и closure `failed` по общему правилу:
успех такая подкоманда не печатает. Здесь же — `checkpoint_outbox`
(таблица, резервирование `sequence` и **доставка незакрытых строк** в
порядке `sequence`, идемпотентно по `checkpoint_id`:
`AlreadyExists` от store читается как «доставлено»; ротация локальных копий
не удаляет копию, на которую ссылается строка outbox-а). Доставляет всякий
invocation, который открывает DB каталога **и** входит в
`PAYING_SUBCOMMANDS` (design §3.5): `run`/`retry`/`watch` — на рубеже
`_run_start_gate`, подкоманды точки (в) — перед своей mutation, `evidence
close-call`/`purge`, `plan`, `review-pr` — на входе; read-only команды не
доставляют (иначе BEH-04/BEH-13 «двойник не получил ничего» становятся
ложными), а `restore` исходную DB не открывает вовсе и потому назван входом
остатка. В этой задаче сайт один — `record_attempt` и старт `run`;
`close-call`/`purge` получают свой вызов в DT-09/DT-12 той же строкой, что
добавляет их в `PAYING_SUBCOMMANDS`. Вставку строки
outbox-а **в транзакцию** каждой mutation делает DT-05 вместе с остальными
сайтами; здесь она есть у одного — `record_attempt`.
Ещё одно решение `after_mutation` — выход без публикации при поднятом
`config.probe_provenance` (design §2.7, поле ставит DT-02): mutation эфемерной
пробы `doctor` не continuation-relevant, её DB удаляется вместе с каталогом.
Сайт записи в этой задаче один — `record_attempt`; остальные сайты §3.1
подключает DT-05.

Границы: WIP (`wip.py`), `repository_identity`, `refs[]` с reachability и
`excluded` — DT-05; spool в checkpoint-е и `degraded: true` — DT-08; отказные
режимы drain перед closure (таймаут → closure `failed` с предыдущим id,
BEH-31) — DT-10; целостность при чтении — DT-11; наблюдаемое поведение
третьей точки drain и outbox-а на настоящих подкомандах (BEH-48) и области
пробы (BEH-47) — DT-05, где есть все их сайты записи. Владеет `tests/
test_checkpoint_is_a_snapshot.py` (новый). Red-рамки: attempt под `PRAGMA
wal_autocheckpoint=0`, seam вызван через `record_attempt`, snapshot открыт в
новом каталоге и видит attempt, контрольный вариант с `shutil.copy` — не
видит; не утверждать имя ключа `checkpoint_seq:<run_id>`, литерал
`.executor-checkpoints`, число локальных копий сверх «последняя + предыдущая»,
`Path.exists()` файла схемы.

#### DT-04: Read-surface `evidence <run_id>`: один `collect()`, `--json`, статусы, экспорт terminal attempt · type: implement · owner: dev
scenarios: [BEH-36, BEH-37]
depends_on: [DT-03]
parallel_group: core

Предмет — design §7.1 (в части `evidence`), §7.4 и §6.4. Новый
`evidence_cmd.py`: subparser `evidence <run_id> [--json]` (подкоманды
`close-call` и `purge` — DT-09 и DT-12 на том же subparser-е);
`collect(store, run_id) → EvidenceView` — один сбор для человека и `--json`
по образцу `tdd_status.py`, схема `schemas/evidence-view.schema.json`;
читает **только store** через `open_store_readonly`, без `project_root`, DB
и Git; статус `closed:<kind>` либо — при run-start без closure — всегда
`crash/unknown` с пометкой «не доказуемо: процесс мог быть жив» (по lock-PID
read-surface не гадает и за оператора не решает); последний acknowledged
checkpoint (id, sequence, время, namespace); open calls (call-start без
call-result, с `call_id`, provenance, задачей, временем); attempts с исходом,
стоимостью (`unknown` при `null`) и ссылками на call records; суммарная
стоимость; `deletions[]`; стоимость хранения, если адаптер отдаёт
(`StoreCapabilities.storage_cost`), иначе поле отсутствует; store недоступен
→ exit 2 с причиной; legacy (нет `run-start.json`, `contract_version` < 1 или
`ack_channel: local`) → «нет evidence-контракта» с перечнем недостающего —
классификация, которую DT-06 переиспользует для отказа restore. Следующий
безопасный шаг — как **рекомендация** с пометкой «не доказуемо: <причина>»
там, где данных нет; ответ ограничен одним `run_id` (OUT-08). Экспорт: при
terminal `record_attempt` (`success`/`failed`/`blocked`)
`evidence.export_attempt(state, task_id, n)` собирает строки этой задачи/
attempt-а из таблиц §6.4 в JSONL по `schemas/evidence-record.schema.json` и
публикует `attempts/<task>-<n>.jsonl` через ту же очередь publisher-а — это и
есть источник «attempts» для `collect()`. Второй читатель
`config.probe_provenance` (design §2.7, поле ставит DT-02) — здесь: при
поднятом флаге `export_attempt` не публикует ничего, потому что экспорт
назвал бы задачу, которой в workstream-е нет, и корроборировал бы
checkpoint, которого нет; стоимость пробы при этом остаётся в её
call-result. Знак — BEH-47 (пустой префикс `attempts/` под `run_id`
прогона `doctor`), предъявляется в DT-05.

Границы: экспорт `pr_*` при завершении раунда `review-pr`, доказательство
«только свои строки» и матрица outcome × site — DT-13 (файл `tests/
test_evidence_every_outcome.py`); пометка superseded в выводе — там же
(BEH-25); `close-call` — DT-09; `purge` — DT-12. Owned by `tests/
test_evidence_read_surface.py` (новый). Прогоны для BEH-37 производятся
фикстурами через настоящие команды с fake CLI и двойниками DT-02: `os._exit` в
seam-е для crash/unknown и open call (двойник, а не `sleep`-гонка), legacy —
каталог с DB и логами без run-start. Red-рамки: `collect()` один — тест
сравнивает значения полей человеческого и JSON-вывода; двойник store не
получил ни одного `put`; каталог остался пустым; не утверждать точный текст
рекомендации сверх пометки «не доказуемо», порядок полей записи, имена
приватных функций.

#### DT-05: Checkpoint после каждой mutation: все сайты записи, полный manifest, repository identity, WIP collect, область пробы и синхронный ack · type: implement · owner: dev
scenarios: [BEH-13, BEH-14, BEH-47, BEH-48]
depends_on: [DT-04]
parallel_group: core

Предмет — design §3.1 в полном перечне сайтов, §3.3 целиком и §4.1–4.2.
`after_mutation` вызывают все `record_*` в `state.py` (`record_red_checkpoint`,
`record_claim`, `supersede_claims`, `reinstate_checkpoint_with_claims`,
`record_tdd_phase`, `record_verify_evidence`, `record_gate_verdict`,
`record_waiver`, `record_waiver_applied`, `record_remedy`,
`record_budget_authorization`, `record_agent_call` — шаг close),
`claims.release_claims`, `lifecycle.advance`, `ReviewPrState` при завершении
раунда (`review_pr.py:282`, своё соединение — передаётся в `conn`),
`bookkeeping.commit_status_flip` (`conn=None` — checkpointer сам открывает
соединение к `state_file`); `mark_running`, `set_meta` и `phase_results` seam
не вызывают (§3 требований). Строку `checkpoint_outbox` (таблица и
резервирование `sequence` — DT-03) вставляет **та же транзакция**, что сама
mutation, у каждого из этих сайтов: `after_mutation` получает уже
зафиксированное обязательство и лишь исполняет его, ack строку удаляет
(design §3.1, Q-05). Отсюда — оба сценария этой задачи сверх BEH-13/14.
BEH-48 (spec-runner#527) наблюдает третью точку drain и outbox на настоящих
подкомандах: у `budget authorize` и `tdd release` ack mutation-checkpoint-а
стоит раньше строки успеха; при отклонённом ack — exit 2, stderr с
недоставленным `sequence`/`checkpoint_id`, mutation в DB и строка outbox-а
рядом с ней; после `kill -9` в окне между commit-ом и ack следующий
invocation в каталоге доставляет checkpoint прежде любой другой работы, с
тем же `checkpoint_id` и идемпотентно, и после этого шаг 5 restore (DT-06)
на нём отказывает — то есть правка, терявшаяся в окне, предъявлена, а
предикат шага 5 не менялся. BEH-47 (spec-runner#525) наблюдает область
пробы целиком, потому что только здесь собраны все её половины: поля и
provenance-карта из DT-02, выход `after_mutation` из DT-03, молчание
`export_attempt` из DT-04. Manifest дополняется до §3.3: `repository`
(`git_ops.repository_identity` — remote URL, нормализованный до
`host/owner/repo` без учётных данных и `.git`, плюс root commit по `git
rev-list --max-parents=0 HEAD`, все корни перечислены), `workstream`
(`namespace`, `namespace_source: declared|computed` — **значение** effective
namespace, не seed), `head`, `refs[]` (имя, SHA, `published`, `published_ref`),
`join_keys`, `excluded[]` (lock, `.<prefix>spec.lock`, `.executor-stop`,
`.executor-ready`, worktrees `spec-runner-*`, `.executor-progress.txt`),
`digests` для каждого файла включая `wip.tar`; ни абсолютных путей, ни PID —
все пути project-relative. Новый `wip.py`: `collect(config) → WipArtifact |
None` из `after_mutation`, переупаковка только при изменении `git status
--porcelain` + HEAD + `refs/stash` с прошлого checkpoint-а (сравнение по
digest-у входов); `wip.tar` = `bundle.git` (`git bundle create` для каждого
in-flight ref в диапазоне `<published-base>..<ref>`, published-base —
merge-base с `origin/<base>`; целиком опубликованный ref — в manifest с
`published: true`, в bundle не входит), stash-commit-ы с меткой `spec-runner
rescue:` под `refs/spec-runner/wip/stash/<n>`, `dirty.tar` по
`git_ops.uncommitted_work_paths` (runtime-state исключён по построению),
`index.json` с per-file SHA-256; байты pushed-объектов не копируются (OUT-04);
пустой WIP — `wip: none` в manifest, файла нет.

Границы: применение WIP (`wip.apply`) и восстановление — DT-06; наблюдаемое
«байт в байт» (BEH-16) — там же; spool в checkpoint-е — DT-08. Владеет `tests/
test_checkpoint_after_every_mutation.py` (новый). Red-рамки: двойник store
получает ровно один checkpoint с большим `sequence` после каждой mutation,
вызванной через её **штатный сайт** (`tdd abandon`, `budget authorize`
отдельным invocation без attempt, раунд `review-pr` с fake gh,
`commit_status_flip`…), и ноль — после `status`/`costs`/`validate`/`report`/
`evidence <run_id>`; статический тест по образцу `run_plugin_hooks_for`
находит ровно один seam; manifest после настоящей mutation валиден по схеме, а `grep` по
байтам всех файлов checkpoint-а не находит `str(project_root)`, `os.getpid()`
и имени активного `spec-runner-red-*` worktree; повторный прогон с объявленным
`tdd_namespace` даёт `namespace_source: declared`. Для BEH-47 — один
настоящий прогон `spec-runner doctor --with-review --yes` с fake CLI: у
двойника store run-start, две пары call-start/call-result с provenance
`doctor:execute`/`doctor:review`, одна closure и **пустые** префиксы
`checkpoints/` и `attempts/` под тем же `run_id`; ни одна опубликованная
запись не несёт `task_id`; следующий `run --all` доходит до платного вызова
собственной `TASK-001` проекта — в том числе после `reset` и в свежем
клоне; ключи на месте при относительном `root` в config-е; два платных
вызова и под проектом `execution_mode: tdd`; тот же набор при verdict
`broken`. Для BEH-48 — порядок в общем журнале, exit 2 на отклонённом ack и
поздняя доставка после `kill -9` — на канонной последовательности
«`evidence close-call` → `restore`», а не только через `run`, — плюс
контроль на `run --task`: при пустом
на старте outbox-е число
синхронных ожиданий ack равно числу точек drain перед платными вызовами
плюс одна перед closure (ожидание на каждую mutation — красный тест,
RK-01), а непустой даёт ровно одно ожидание-ремонт до выбора задачи и только
один раз на строку. Владеет ещё `tests/test_doctor_probe_scope.py` и `tests/
test_mutation_checkpoint_ack.py` (оба новые). Не утверждать число
вызовов `after_mutation` в `state.py`, формат `wip.tar` внутри, нормализацию
URL за пределами `host/owner/repo`, имена полей области пробы
(`probe_provenance`), имя и схему таблицы
`checkpoint_outbox`, wall-clock цену третьей точки drain внутри CI-теста
(её меряет бенчмарк BEH-41).

#### DT-06: Restore по `run_id`: `plan`/`apply`, порядок проверок, WIP apply, чтение и replay spool, следующий шаг, `--experimental`, `--json`, бенчмарк · type: implement · owner: dev
scenarios: [BEH-16, BEH-17, BEH-18, BEH-19, BEH-20, BEH-21, BEH-41, BEH-43]
depends_on: [DT-05]
parallel_group: core

Предмет — design §7.1 (в части `restore`), §7.2–7.3, §4.3 и **читающая
половина** §5. Новый `restore_cmd.py`: subparser `restore <run_id> --into
<dir> [--experimental] [--json]`; непустой `--into` — отказ до всего;
`restore.plan(run_id) → RestorePlan | RestoreRefusal` — до записи в каталог,
в объявленном порядке: `contract_version` → digests всех файлов последнего
acknowledged checkpoint-а + `manifest_sha256` → repository identity (root
commit клона против manifest) → policy identity (`config_hash` активного
config, ключ и оба значения в сообщении) → namespace (оба значения и оба
источника `declared`/`computed`) → open calls **всего workstream-а** →
spool (sha256 каждой строки); первое несовпадение — отказ,
instrument-класс (1), (2), (7) — exit 2, остальные — `needs-human`, exit 1;
legacy — fail-closed с перечнем недостающего (классификация DT-04) и ссылкой
на «operational minimum» `docs/architecture.md`; без `--experimental` — отказ
с текстом CON-01, статус — одна константа `RESTORE_EXPERIMENTAL = True`,
которую снимает CHANGELOG-запись (тест параметризует оба значения).
`restore.apply(plan)`: `git clone <remote> <into>` (или `--from-clone` для
bare-репо в тесте), `wip.apply` — `git bundle verify` → `git fetch <bundle>
'refs/*:refs/*'` → `git switch <task-branch>` → распаковка `dirty.tar` с
проверкой per-file SHA-256 → `git stash store -m <label> <sha>`; ref,
объявленный опубликованным, которого forge не отдаёт → `needs-human` с именем
и SHA до распаковки, реконструкции нет (OUT-09); `state.db` из snapshot-а на
место `config.state_file` **и сразу правка одного ключа meta в нём** —
`continuation_index` = `restored` (`set_meta`, не `record_*`, так что
`after_mutation` не вызывается): без неё восстановленный snapshot нёс бы
`last_run_id` и первый `run` после restore пошёл бы по ветке (1) Q-12, мимо
индекса; replay spool; lock/stop/ready/worktrees просто не создаются. Namespace по Q-09: config не объявляет `tdd_namespace` →
`tdd_namespace: <value>` дописывается shape-preserving merge-ом по образцу
`preset_cmd.apply_to_config --apply` с `.bak`, diff печатается, файл виден в
`git status`. Следующий шаг — из DB: open call → `needs-human` (уже отказано
в plan); confirmed red без green → `run --task <id>`; после green с не-DONE
lifecycle → `tdd resume <id> …`; иначе `run --all`. `--json` —
`schemas/restore-result.schema.json` (`status`, `next_step`|`reason`,
`checks[]`, `wip`, `namespace`; exit 0/1/2 ↔ `ok`/`needs-human`/`instrument`).
Spool, читающая половина: `spool.py` с `Spool(path)`, `read` (единственные
читатели — `replay` и checkpoint), проверкой sha256 строки (отказ называет
`seq` и оба digest-а), `replay(state)` в порядке `seq` с таблицей
`spool_replays` `(run_id, seq)` для идемпотентности; фикстура для (7) и
replay — spool-файл, положенный в checkpoint тестом, потому что писать его
начнёт DT-08. Здесь же — `scripts/bench_durability.py` (`kind: manual`:
call-start ack p95/p99 и время доступности checkpoint-а вне машины на
reference workload, сравнение с NFR-02 в отчёте, не в assert) и CI-половина
BEH-41 — `test_validation_under_60s` на reference-наборе ≈ 10 MiB (NFR-03,
щедрый запас). BEH-43 — autouse-фикстура в `tests/conftest.py`: после каждого
E2E под git automation `git status --porcelain` продуктового репо не содержит
путей под `.executor-*` и `tracked_state_paths` пуст — так E2E, авторуемые
DT-08…DT-14, попадают под тот же sweep без правки этого файла; `spec/
.gitignore` untracked — не находка.

Проверка (6) — namespace-wide, и это предмет именно этой задачи (design
§ 7.2, перечень путей § 2.6): `workstream_key` берётся из run-start
восстанавливаемого прогона, один `list` индекса
`workstreams/<workstream_key>/runs/` DT-01, разбираются прогоны без парного
`.closed` **и** все, начатые позже восстанавливаемого; любой call-start без
call-result где угодно в workstream-е — `needs-human` с `run_id` того
прогона, `call_id`, provenance и `task_id`; более поздний прогон
блокирует восстановление, когда сошлись два условия: его `subcommand` из
run-start в блокирующей половине закрытого перечня — `run`/`retry`/`watch`,
`plan`, `review-pr`, `budget authorize`, `tdd abandon|repair|resume|release`
— **и** под его `run_id` есть хотя бы один **acknowledged**
checkpoint — не просто ключ под `runs/<run_id>/checkpoints/…`: manifest
кладётся последним, и checkpoint без него для читателей не существует
(design § 1.1, § 3.5). Проверка — по acknowledged-признаку checkpoint-а, а
не `list` префикса. Отказ — `needs-human`, и выход в нём
исполним: печатается последний прогон workstream-а с acknowledged
checkpoint-ом, **после которого нет ни одного блокирующего прогона**
(собственная запись текущего invocation не в счёт; прогон с checkpoint-ом,
но заблокированный более поздним, — петля, не выход). Кандидат существует
всегда, когда шаг 5 сработал, — блокирующий прогон сам несёт acknowledged checkpoint, а
самый поздний из блокирующих не имеет блокирующих после себя; ветки
«восстановимого прогона нет» поэтому не существует, она была бы
недостижимым кодом. Отказ называет и
то, чего checkpoint выхода может не нести: материал, записанный после его
последнего checkpoint-а (у `plan` — `tasks.md`).
Холостой прогон из той же половины (нечего делать; старт отказан гвардом или
занятым lock-ом после run-start) checkpoint-а не публикует и восстановлению
не мешает — иначе оператор остаётся без пути, потому что восстановить его
самого нечем. Известный пробел, принятый владельцем: прогон, убитый в окне
между mutation и доставкой checkpoint-а, acknowledged checkpoint-а не имеет
и тоже не блокирует — его правка теряется молча (design § 7.2 шаг 5, абзац
про пробел); fail-closed здесь невозможен, он сделал бы недостижимым путь
«дверь `close-call` → restore». Предикат этого шага поэтому и не менялся, а
окно сузили третья точка drain и `checkpoint_outbox` (DT-03, DT-05): на
живой машине потеря превращается в позднюю доставку, которую этот шаг
видит как всякий другой checkpoint, и знак этого — последние And BEH-48,
предъявляемые **через** отказ этого шага.
Неблокирующая половина — `evidence close-call`, `evidence purge`, `doctor`,
`restore` — не блокирует независимо от checkpoint-ов (причина у каждого
своя, design § 7.2 шаг 5; у двери checkpoint есть, и он ничего не меняет; у
`doctor` его нет вовсе — design §2.7, так что его строка здесь фиксирует
свойство, а не решает за него, и кандидатом на выход он не бывает по тому
же основанию). Обе половины объявляются рядом с
`PAYING_SUBCOMMANDS` в `run_context.py` (DT-02), и полнота держится тестом
`set(PAYING_SUBCOMMANDS) == BLOCKING | NON_BLOCKING` при пустом пересечении
(предмет BEH-20, последние And; файл — `tests/test_restore_refusals.py`
этой задачи). Половины объявляются над тем, что лежит в
`PAYING_SUBCOMMANDS` на момент этой задачи; растящие множество DT-09 и DT-12
дописывают свою подкоманду в `NON_BLOCKING` той же строкой, поэтому
равенство держится на каждой границе, а не только в конце.
Приёмка самого теста — подсадка неклассифицированной подкоманды, на которой
он обязан покраснеть. Недоступный store или индекс — instrument, exit 2. `--json` несёт
исход полем `workstream` (`later_runs[]`, `open_calls[]`). Проверка по
ключам одного `runs/<run_id>/calls/` красна: конфигурация «прогон A закрыт,
более поздний C оставил open call, восстанавливается A» — та, на которой
`run_id`-scope и namespace-scope расходятся (BEH-09, BEH-20).

Границы: детекция open calls на старте `run` и дверь `close-call` — DT-09
(BEH-09 наблюдает `restore` → `needs-human` вместе с `run`); degraded mode и
replay на старте `run` — DT-08; целостность для каждого файла × {байт
изменён, файл удалён} на `run` и `evidence` — DT-11 (здесь — проверка (2)
над digests последнего checkpoint-а в форме BEH-20). Владеет `tests/
test_restore_drill.py` (новый), `tests/test_restore_refusals.py` (новый),
`scripts/bench_durability.py` (новый, `kind: manual`). Red-рамки по design:
`--json` отчёт с `checks[]`, `git log`/`sha256sum` восстановленного каталога
(эвиденция PR); каждое условие BEH-20 своей параметризацией и одно с двумя
подменами сразу, `--into` пуст после отказа (`listdir`), двойник
`check_claims` вызван 0 раз, двойник `Popen` — 0 раз за время restore;
BEH-19 — `tdd status` с тем же blob SHA и RED checkpoint, `costs` с
authorization, `budget.effective_limits` с поднятым потолком, следующий `run`
начинает с GREEN; BEH-17 — `git worktree list` только основной, `status` не
видит stale lock чужого PID. Не утверждать точный текст отказов сверх
обязательного (оба root commit, ключ и оба значения policy, оба namespace и
источники, имя ref и SHA), формат `wip.tar`, wall-clock p95/p99 внутри
CI-теста, что 1 GiB drill выполнен (ручной, отчёт в условии завершения M-01).

#### DT-07: Один `run_id` во всех каналах: OTel, audit-log, заголовок prompt-артефакта, `watch` · type: implement · owner: dev
scenarios: [BEH-01, BEH-02]
depends_on: [DT-03]
parallel_group: identity

Предмет — остаток design §6.1 за пределами DT-02: `AuditLogger` принимает
`run_id` обязательным параметром из контекста в `build_audit_logger`
(`audit_log.py:195`), собственный `uuid.uuid4()` в `__init__` удаляется;
`run_id` в structlog contextvars рядом с `pipeline_id`, так что OTel-записи
`obs.py` несут его без правки формата; `prompts_log.log_prompt` пишет
`run_id`/`call_id` в заголовочную строку `=== <SLUG> PROMPT ===` — `call_id`
чеканит сайт до `execute` и передаёт и `PaidCall`, и `log_prompt`; seam
проверяет, что ему передан тот же; тело между заголовком и терминальной
секцией остаётся «prompt как отправлен» (#282), в него ни один id не вписан.
`watch` — один `start()` на invocation, одна closure, строки обеих задач под
одним `run_id`. Статические тесты BEH-02: в `cli.py` нет `uuid4().hex[:8]`
как источника `run_id`, в `audit_log.py` нет собственного `uuid4()`.

Границы: manifest и closure как каналы `run_id` уже существуют (DT-03,
DT-02) — эта задача их не меняет, а наблюдает вместе с остальными; `status`
и ledger-столбцы — DT-02. Задача независима от `restore`/`evidence` и потому
стартует сразу после DT-03, параллельно DT-04…DT-06. Владеет `tests/
test_run_identity.py` (новый). Red-рамки: одно и то же 36-символьное значение
в OTel JSONL, audit-log, `agent_calls` GREEN и review, заголовках
prompt-артефактов, manifest и closure после настоящего `run --task` с
`ORCHESTRA_PIPELINE_ID`; `pipeline_id == <pid>` отдельным полем в каждом
канале, и ни в одном — подстановка одного вместо другого; без
`ORCHESTRA_PIPELINE_ID` `pipeline_id` там, где есть, один и никогда не равен
`run_id`; тело prompt-артефакта байт в байт равно prompt-у, переданному
двойнику `Popen`; два invocation → `A != B`. Не утверждать наличие класса
`RunContext` по имени, формат заголовочной строки сверх присутствия обоих id.

#### DT-08: Emergency spool: degraded mode пишет spool, replay на старте, ротация, checkpoint `degraded: true` · type: implement · owner: dev
scenarios: [BEH-15, BEH-33, BEH-34, BEH-35]
depends_on: [DT-06]
parallel_group: spool

Предмет — **пишущая половина** design §5 и её след в §3.2–3.3.
`state._enter_degraded_mode` (`state.py:2352`) перестаёт быть «уведомить и жить
в памяти»: каждый `record_*`, поймавший `OperationalError`, вызывает
`Spool.append(table, payload)` (append-only, `fsync` на строку, строка =
`{seq, run_id, namespace, task_id, attempt, table, payload, sha256}`) и только
при успехе возвращается как записанный; `_save()` (`state.py:948`) в degraded
mode — тем же путём; отказ spool-а → `Refusal(kind="instrument")` наверх:
следующий платный вызов не начинается, exit 2, closure `failed`
с причиной, называющей обе неудачи, а при недоступном ещё и store — stderr с
причиной и тот же exit 2; `state_degraded`-уведомление — один раз, как
сегодня. `spool.replay(state)` (DT-06) вызывается на старте `run`/`retry`/
`watch` после run-start и гардов старта, до выбора задачи, из
`tdd`/`budget`-команд и из `restore`
(там уже стоит); повреждённая строка на старте — отказ с `seq` и обоими
digest-ами, прогон не стартует, 0 `Popen`, closure `failed`;
после replay файл ротируется в `.executor-spool.<ts>.jsonl.done`, ссылка —
в следующем manifest (`spool_replayed`). Checkpoint в degraded mode: DB
snapshot — из последнего успешного состояния файла, `spool.jsonl` — копия
активного spool-а (чтение байтов, не разбор строк), manifest `degraded: true`,
`sequence` — из spool. Статический тест единственного читателя: `Spool.read`
вызывается только из `replay` и checkpoint-а; `get_next_tasks`, claims gate и
budget guard spool не читают (RK-03).

Границы: формат строки и `spool_replays` заведены DT-06 и здесь не
переопределяются; последний And BEH-15 («restore из этого checkpoint-а
доигрывает spool и показывает attempt») наблюдается через `restore` DT-06 —
потому задача и стоит после него. Владеет `tests/test_emergency_spool.py`
(новый). Red-рамки: двойник `os.fsync` считает вызовы; replay дважды без
дублей; повреждённый байт → отказ с `seq`; BEH-33 параметризован по каждой
mutation из перечня, новый процесс в том же namespace — `status`/`tdd
status`/`costs` показывают mutation, следующий checkpoint несёт её в snapshot;
граница replay предъявляется как в AC-30 — на `retry`, `watch` и `run --all
--force`, то есть на трёх путях, executor lock не берущих: доказательство
replay вызовом `_acquire_run_lock` красный не гасит;
BEH-35 — двойник `Popen` не вызван после отказа, TASK-001 в следующем процессе
не `done`. Не утверждать имя таблицы `spool_replays`, литерал
`.executor-spool.jsonl`, интервал ожидания publisher-а.

#### DT-09: Open call: детекция на старте `run` (Q-12), операторская дверь `evidence close-call` и её сайт доставки outbox-а · type: implement · owner: dev
scenarios: [BEH-09, BEH-10, BEH-11]
depends_on: [DT-06]
parallel_group: door

Предмет — design §2.4 (абзац про старт `run`) и §2.5.
`paid_call.open_calls(config, state) → list[OpenCall]` — процедура Q-12.
Эта задача заводит общий рубеж старта `cli._run_start_gate(args, config,
state)` и вызывает его из **трёх** handler-ов сразу после гардов старта:
`_run_tasks_inner` там же, где `recover_stale_tasks` (`cli.py:861`),
`cmd_retry` (в его `with ExecutorState`, `:1480`, до `execute_task`) и
`cmd_watch` (один раз на invocation, под собственный `with ExecutorState`, до
первого круга цикла). Ни `cmd_retry`, ни `cmd_watch` через `_run_tasks_inner`
не проходят, поэтому единственный сайт оставил бы оба пути без детекции —
красный BEH-09. DT-08 подключает replay spool в тот же рубеж перед
процедурой; до DT-08 рубеж состоит из одной процедуры. Веток две, и
различает их meta `continuation_index`: значение `restored` пишет
`restore.apply` (DT-06), отсутствие маркера — свежая DB, значение `local` —
сама эта процедура, успешно завершив ветку (2) (`RunContext.start()` маркера
не пишет, DT-02). **(1) маркер `local`:** для каждой `open`-строки namespace-а — targeted
`get` двух ключей в store, не листинг; (а) есть call-result → строка
закрывается им, задача свободна; (б) есть call-start, нет call-result → open
call: `run --all` пропускает задачу с причиной, называющей `call_id`,
provenance и «open call», и выполняет остальные ready; `run --task` отказывает
той же причиной, exit 1; (в) нет call-start → строка закрывается outcome
`not_started`, задача свободна. **(2) маркера нет или он `restored`** — DB создана заново
(`spec-runner reset` `cli_info.py:571` удаляет файл без аудита и без
`--reason`; ручное удаление; свежий клон; другая машина) либо приехала из
snapshot-а: один `list` индекса
workstream-а DT-01, прогоны без парного `.closed` разбираются по своим
`calls/`-ключам, каждый call-start без call-result, **несущий `task_id`**,
восстанавливается как
`open`-строка свежей DB (`task_id`, attempt, `call_id`, provenance, `run_id` —
из call-start), дальше работает ветка (б); call-start без `task_id` (сайты
планирования, `review-pr`, проба `doctor` — design §2.4, §2.7) строкой не
становится: адресовать ею нечего, `agent_calls.task_id` — `NOT NULL`, а
истиной остаётся пара ключей в store, которую читают `evidence <run_id>` и
проверка (6) `restore`; пустой индекс — прогон идёт как
обычно; успешно пройденная ветка последним шагом ставит `set_meta
continuation_index=local` (в том числе на пустом индексе); недоступный store
на этом пути — `Refusal(kind="instrument")`, exit 2, маркер не ставится.
`reset` сам по себе задачей не правится и платящей подкомандой не становится —
инвариант держит ветка (2), а не гвард на одной команде. Здесь же — **сайт
доставки `checkpoint_outbox` у двери и у `evidence purge`**: обе входят в
`PAYING_SUBCOMMANDS` этой задачей (и DT-12), обе открывают DB, поэтому по
design §3.5 доставляют незакрытые строки до своей работы. Без этого сайта
канонная последовательность FR-05 «дверь → restore» недоставленного
checkpoint-а не доставляла бы вовсе, хотя оператор в каталоге как раз
работал (последний And BEH-11). Ни один путь не
создаёт второй call-start для того же attempt. Дверь — `spec-runner evidence
close-call <run_id> --call <call_id> --reason …` по образцу `remedy.cmd_tdd`:
обязательный `--reason`, записанный actor, `SPEC_RUNNER_AGENT` guardrail,
отказ под PID-checked lock; идемпотентность — по наличию `result.json` в
store, проверенному **до** записи; пишет
`CallResult(outcome="resolved_unknown", supersedes=<start key>)` в store и
закрывает строку DB того namespace-а, если DB доступна (иначе — процедура Q-12
закроет её на следующем старте по store); платного вызова не делает и не
решает, повторять ли его. Платящей подкомандой при этом является по второй
половине критерия FR-01 — она меняет continuation-state: `evidence
close-call` входит в `PAYING_SUBCOMMANDS` (DT-02 заводит множество, эта
задача добавляет в него строку) и пишет свою пару run-start + closure. Той
же строкой она относит подкоманду к `NON_BLOCKING` шага 5 § 7.2 (дверь
восстановлению не мешает — design там же): множество и половины растут
одним изменением, иначе тест полноты
`set(PAYING_SUBCOMMANDS) == BLOCKING | NON_BLOCKING`, доставленный DT-06,
краснеет на границе этой задачи. Kind
выводит диспетчер по коду выхода (design § 6.3): `completed` на закрытии и
на идемпотентном повторе (код 0), `failed` под guardrail-ом или занятым
lock-ом (код 1) и при недоступном store (код 2). Своих сайтов closure у
команды нет, причину контексту она не сообщает, и ребра на DT-10 отсюда не
возникает. Закрытие строки идёт шагом «close»
`record_agent_call`, то есть через `after_mutation`, и checkpoint публикуется
под `run_id`, у которого run-start есть. Контракт `call_id` (BEH-10): второй call-result и
второй call-start под тем же id отвергнуты с именем `call_id`, первая запись
неизменна, отказ не «улучшает» исход задачи — семантика `AlreadyExists` DT-01
и seam-а DT-02, предъявляемая здесь живьём вместе с дверью.

Границы: `restore` → `needs-human: open call <call_id> (<provenance>,
TASK-001)` до любого `Popen` — проверка (6) DT-06, здесь наблюдается в новом
процессе и новом каталоге; `evidence` показывает 0 open calls и запись
`resolved_unknown` с actor — `collect()` DT-04. Владеет `tests/
test_open_call_door.py` (новый). Red-рамки по design («задачи-границы»): open
call, оставленный `os._exit` двойника seam-а после ack (обе границы: до spawn
и после spawn до результата), → `run --all` пропускает с `call_id` в причине,
`run --task` exit 1, `restore` `needs-human` — в новом процессе и каталоге,
0 `_spawn`; тот же open call после `spec-runner reset`, после удаления файла
DB и в клоне на другом пути — те же три исхода и 0 `_spawn` (пустой
`agent_calls`, принятый за «open call нет», — красный тест), а в каталоге без
прошлых прогонов `run` стартует обычным порядком; четыре вызова `close-call` по очереди (без `--reason`, с ним,
повторно, под `SPEC_RUNNER_AGENT=1`), у каждого из которых двойник store
получает ровно один run-start и ровно одну closure своего исхода, а после
второго `run --task TASK-001` доходит до **нового** call-start. Не утверждать точный текст отказов сверх
`call_id` и provenance, имена приватных функций.

#### DT-10: Правило вывода kind closure в диспетчере, drain перед closure, отказные режимы, `kill -9` · type: implement · owner: dev
scenarios: [BEH-04, BEH-29, BEH-30, BEH-31, BEH-32, BEH-46]
depends_on: [DT-06]
parallel_group: closure

Предмет — правило вывода kind closure из design § 6.3 и остаток § 6.2 за
пределами рабочей формы DT-02. Всё, что задача делает с kind-ами, живёт в
одной функции `closure.derive(outcome)` и в диспетчере `cli.main()`; ни один
сайт выхода ни одной подкоманды этой задачей не правится.

`derive` читает два факта. Первый — как handler ушёл: неперехваченное
исключение (не `SystemExit`), сигнал — поднятый флаг
`executor._shutdown_requested`, который диспетчер читает перед closure
(`_signal_handler` не правится; stop-marker флаг не поднимает) — или
`KeyboardInterrupt`, код выхода.
Второй — исход работы: есть ли задача, по которой этот invocation записал
attempt (столбец `run_id`, DT-02) и которая не в статусе `success`
(`state.py:2211-2217`), либо остался open call. Строки применяются сверху
вниз: исключение → `crashed`; сигнал → `interrupted`; код ≠ 0 при `error_kind`
последнего неуспешного attempt-а из отказного подмножества `ERROR_KINDS`
(`policy`, `budget`, `blocked`, `hook_failure`, `harness_guard`,
`errors.py:101-121`) → `refused`; прочий код ≠ 0 → `failed`; код 0 с
невыполненной работой → `failed`; код 0 без неё → `completed`. Подмножество
отказных `error_kind` берётся из словаря дерева, второй словарь не заводится
(#230).

`CLOSURE_KINDS` — закрытые пять значений, заведённые DT-02; эта задача их не
расширяет. `RUN_STOP_REASONS` (`cli.py:536-541`) остаётся семизначным:
closure его не читает, и ни одного нового значения в него не добавляется.
`set_meta("last_run_stop_reason", …)` на `cli.py:1374` не трогается, включая
его дефолт `completed` — тест наблюдает прямо, что closure gate-отказа под
`review_policy: required` есть `refused` при `status`, показывающем
`completed`.

`reason` — свободная строка: исключение, флаг сигнала, `error_kind`/`error`
последнего неуспешного attempt-а, строковый аргумент `SystemExit` или
персистированный `last_run_stop_reason`, что из этого есть; пустое значение
допустимо и валидно по схеме. Словаря reason-ов нет, отказа сериализации по
reason нет.

Drain перед closure: таймаут ack последнего checkpoint-а → closure `failed`
с `last_checkpoint_id` предыдущего acknowledged и exit 2; отказ записи
closure → stderr с причиной, exit code не улучшается (0 при незаписанной
closure невозможен); повторная closure → `AlreadyExists`, первая неизменна;
closure по времени позже последнего checkpoint-ack (журнал двойника). Closure
несёт `run_id`, `pipeline_id`, подкоманду, kind, reason, exit code, число open
calls, `degraded`/spool status, timestamps start/end,
`last_call_ids`/`attempt_ids`.

Read-only команды (`status`, `costs`, `validate`, `report`,
`evidence <run_id>`) получают `run_id` для логов, но
run-start/checkpoint/closure не пишут; `PAYING_SUBCOMMANDS` — одиннадцать
позиций (`run`, `retry`, `watch`, `plan`, `review-pr`, `doctor`, `tdd
abandon/repair/resume/release`, `budget authorize`, `restore`, `evidence
close-call`, `evidence purge`): ровно один run-start и ровно одна closure на
invocation, и три пути, executor lock не берущие (`retry`, `watch`, `run
--all --force`), предъявляются отдельно — доказывать их run-start вызовом
`_acquire_run_lock` нельзя, они его не проходят. `run --force` остаётся
обычным платящим прогоном контракта. `kill -9`/`os._exit` после run-start и
первого acknowledged checkpoint-а — closure нет по построению; `evidence` —
`crash/unknown`, `restore` восстанавливает с последнего acknowledged
checkpoint-а и печатает следующий шаг только без open calls; `status` в
исходном каталоге не считает прогон завершённым и показывает его `run_id`.

Границы: `CLOSURE_KINDS` и closure на штатном выходе заведены DT-02 и здесь
дополняются правилом, не переписываются; `evidence`/`restore` — DT-04/DT-06,
здесь только наблюдаются. Обязанностей «сообщить причину» у DT-06, DT-09 и
DT-12 эта задача не создаёт и потому не ждёт их: closure их подкоманд пишет
тот же диспетчер по коду выхода, и порядок слияния лент остаётся
произвольным. Владеет `tests/test_closure_every_exit.py` (новый). Red-рамки
по design: двойник handler-а, завершающийся каждым из шести способов
правила, — kind выведен на всех шести, `completed` только на последнем;
gate-отказ под `review_policy: required` → `refused` при дефолтном
`last_run_stop_reason = completed` (closure `completed` — красный тест);
`retry` с задачей `blocked` и `watch` с `max_consecutive_failures` — код 0 и
kind `failed` (красный тест на `completed`); одиночный run-start без closure
для «lock занят» — красный тест; exit code closure равен фактическому.
Статический тест: в `src/spec_runner/` нет `note_stop`, нет таблиц причин по
подкомандам, `RUN_STOP_REASONS` не вырос. Не утверждать интервал/порядок
drain publisher-а сверх «closure позже последнего ack», тайминги с узким
запасом, точный текст reason (он свободная строка), различение отказа
правила и поломки инструмента у подкоманд без attempt-ов.

#### DT-11: Целостность fail-closed: изменённый байт или отсутствующий файл останавливают `run`, `restore` и `evidence` до `Popen` и claims gate · type: implement · owner: dev
scenarios: [BEH-40]
depends_on: [DT-06]
parallel_group: integrity

Предмет — NFR-04 на трёх читателях bundle-а. `restore` — проверка (2) DT-06
расширяется до параметризации BEH-40: каждый файл bundle-а (DB snapshot,
manifest, WIP artifact, spool, каждый call record, экспорт attempt-а, closure)
× {один байт изменён; обязательный файл удалён — DB snapshot, manifest, WIP
при `wip != none`, closure при `closed`}; изменённый manifest отвергается по
`manifest_sha256`, даже если все перечисленные в нём digests сходятся. `run`
(и `retry`/`watch`) на старте — после run-start и гардов старта, до `Popen` и
до `claims.check_claims` — проверяет целостность bundle-а, от которого namespace
продолжается (источник его `run_id` — реализатору: последний run-start
namespace-а в `executor_meta`, снапшот которого лежит в DB), и отказывает
`Refusal(kind="instrument")`, exit 2, closure `failed`.
`evidence` — exit 2 с `reason`, называющим файл и оба digest-а; у `restore
--json` и `evidence --json` — то же в `reason`. Авто-«лечения» нет (OUT-09):
ни один вариант не заканчивается успешным restore с предупреждением.

Границы: сами digests и `manifest_sha256` считает DT-03/DT-05, `restore.plan`
— DT-06; здесь — полнота параметризации и два новых читателя (`run`,
`evidence`). Владеет `tests/test_integrity_fail_closed.py` (новый).
Red-рамки по design: параметризация по каждому файлу × режиму, все три
команды отказывают с именем файла и обоими digest-ами, `run` — двойники
`_spawn` и `check_claims` подтверждают 0 вызовов; следующий `run` выполняется
в каталоге, восстановленном из нетронутой копии, а подмена — в store. Не
утверждать точный текст отказа сверх имени файла и обоих digest-ов, порядок
проверки файлов внутри bundle-а.

#### DT-12: Retention: диапазон, `evidence purge`, audit-запись удаления без payload, отказ store не обходится · type: implement · owner: dev
scenarios: [BEH-42]
depends_on: [DT-06]
parallel_group: retention

Предмет — design Q-11 и `retention.py`. Каждый `put` несёт метаданные
(`run_id`, `kind`, `closure_at`, `retention_until`), которые cloud-адаптер
вправе отдать своему lifecycle; `spec-runner evidence purge <run_id> --reason
…` (подкоманда на subparser-е DT-04; `--reason`, actor, `SPEC_RUNNER_AGENT`
guardrail — по образцу `close-call` DT-09) считает по тем же метаданным, что
истекло, и какие промежуточные checkpoint-ы удаляемы (только с acknowledged
преемником — RK-05), вызывает `delete` адаптера и **только после** успешного
`delete` пишет `deletions/<ts>.json` (`run_id`, ids, actor, reason, время —
без payload); отказ store (legal hold, OUT-10) оставляет всё как есть, включая
локальную копию, и audit-записи не пишет; `AuditLogger`, когда включён,
получает копию записи. `evidence purge` — платящая подкоманда по критерию
FR-01 (меняет continuation-state, удаляя опубликованные объекты): эта задача
добавляет её в `PAYING_SUBCOMMANDS` и той же строкой — в `NON_BLOCKING`
шага 5 § 7.2 (open call он не закрывает, а `deletions/<ts>.json` — аудит
собственного удаления; множество и половины растут одним изменением, иначе
тест полноты из DT-06 краснеет на границе задачи), а kind её closure выводит диспетчер по
коду выхода (design § 6.3) — `completed` при коде 0 (истёкших объектов нет;
удалено и записано), `failed` при коде 1 (store отказал в `delete`) и коде 2
(store недоступен). Своих сайтов closure у команды нет, словаря причин она не
пополняет, ребра на DT-10 отсюда не возникает. Open call она при этом
не закрывает по построению: удаляются только истёкшие checkpoint-ы с
acknowledged преемником, call records и run-start живут до своего retention
(перечень путей — design § 2.6). `retention_days` вне 7–365 — `ConfigError` при
загрузке и ошибка `validate` (loader DT-01, здесь наблюдается вместе с
удалением).

Границы: lifecycle-политики второго адаптера — вне графа (CON-08); `evidence
<run_id>` уже показывает `deletions[]` (DT-04). Владеет `tests/
test_retention_policy.py` (новый). Red-рамки: три checkpoint-а (sequence 1–3)
и closure; после удаления 1–2 restore из 3 проходит (DT-06); удалённые записи
заменены audit-записью без payload; двойник store, отказывающий в `delete`, →
команда сообщает отказ, локальная копия на месте, `deletions/` пуст. Не
утверждать механизм удаления у store, точный формат ключа `deletions/<ts>`.

#### DT-13: Evidence на каждой клетке outcome × site, экспорт только своих строк, immutability и корпус секретов · type: implement · owner: dev
scenarios: [BEH-08, BEH-22, BEH-23, BEH-25, BEH-27]
depends_on: [DT-08, DT-09]
parallel_group: matrix

Предмет — FR-06 как измерение по всей поверхности, которую DT-02…DT-09
построили, и три вещи, которых там ещё нет. (1) Матрица outcome {success,
`TASK_FAILED`, blocked, timeout, infrastructure error} × site {GREEN, review,
`review:<role>`, `plan --full`, `plan --gated`, интерактивный `plan`,
`review-pr fix`, `doctor`}:
каждая клетка — call record, адресуемый `run_id/call_id`, с provenance,
outcome, стоимостью (число или `null`, никогда `0.0` за неизвестную),
bounded/redacted prompt и result с SHA-256 и размером; клетки `TASK_BLOCKED`,
timeout, `INFRASTRUCTURE`, `is_error` при exit 0 — так же, как success
(`post_review` не канал evidence); число records == число `spawn` двойника;
что из этого seam DT-02 уже делает, а что теряет на нештатных ветках сайтов —
и есть предмет измерения, найденные дыры закрываются здесь. Четыре режима fake
CLI BEH-08 — call-result с outcome по `classify_agent_answer` (`timeout`,
`failed`×3), не open call: `evidence` показывает 0 open calls, следующий `run`
не ставит задачу в `needs-human` (процедура DT-09). (2) Экспорт `pr_*` при
завершении раунда `review-pr`, не на каждый comment; доказательство «только
свои строки» для трёх задач с исходами `done`/`failed`/`blocked` по
`evidence-record.schema.json` (экспорт — DT-04); здесь же — две
корроборирующие записи bundle-а из design §6.5, которых требует инвентарь
`docs/architecture.md:251-252`: срез task change history за прогон (хвост
`.task-history.log` от отметки `RunContext.start()`) и срез audit-trail по
своему `run_id`, обе через тот же redactor и `bound_evidence`, обе
необязательные — нет источника, нет ключа и нет отказа. (3) Immutability на живом
адаптере: повторный `put` с другим содержимым отвергнут, чтение возвращает
исходник байт в байт, «исправление» — новая запись с `supersedes`, и
`evidence <run_id>` показывает обе, помечая первую superseded, не удаляя
(пометка — правка `collect()` DT-04 по предмету BEH-25). (4) Корпус
`tests/fixtures/secrets-corpus/` из 100 синтетических секретов известной
формы; паттерны `redaction.py` дополняются до полного покрытия корпуса; ни одно
из 100 значений — ни в одном файле, полученном двойником store (bundle,
checkpoint, WIP artifact, spool), при `full_sha256`/`full_size` от
**исходного** содержимого; статический пояс по образцу `PaidBinaryReached`:
`ArtifactStore.put` подменяется двойником, поднимающим исключение при вызове
не из `Publisher.publish` (по стеку), и гоняется E2E; локальный
prompt-артефакт по-прежнему полный.

Границы: seam, сайты, `Publisher`, `bound_evidence` — DT-02; `collect()` и
`export_attempt` — DT-04; эта задача правит их только по предмету своих
сценариев (пометка superseded, экспорт раунда, паттерны и пояс). Владеет
`tests/test_evidence_every_outcome.py` (новый), `tests/
test_redaction_corpus.py` (новый). Red-рамки по design: record в двойнике
store на каждую клетку с `null` там, где fake CLI стоимости не сообщил; все
100 значений отсутствуют в **каждом** файле — grep по байтам полученных файлов,
не по полям; `LocalVolumeStore.put` дважды под одним ключом — второй
`AlreadyExists`; срезы task-history и audit-log не содержат строк соседнего
`run_id`, а при выключенном аудите и отсутствующей истории прогон зелёный без
них. Не утверждать конкретные regex redactor-а (корпус, не
паттерны), способ генерации `corpus.json`, порядок полей записи, формат
строки `.task-history.log`.

#### DT-14: Fault-injection ×1000 на каждой границе, документация контракта, статус experimental, handoff соседям · type: implement · owner: dev
scenarios: [BEH-39, BEH-45]
depends_on: [DT-07, DT-10, DT-11, DT-12, DT-13]
parallel_group: gate

Предмет — сквозная гарантия NFR-01 и сводный manual-критерий. Seam-ы
(`paid_call.execute`, `checkpoint.after_mutation`, `Spool.append`,
`RunContext.start`) получают точки инъекции, через которые двойник теста
вызывает `os._exit` на границах: после run-start; после call-start до spawn;
после spawn до результата; после результата до attempt; после attempt до
checkpoint — инъекция двойниками, не `sleep`-гонками. Тест под
`@pytest.mark.slow`, параметризован по границе, 1000 повторений на границу,
детерминирован по seed: каждая итерация запускает прогон, обрывает его и
запускает новый процесс в том же namespace; суммарно 0 mutation,
подтверждённых DB/spool/store и отсутствующих после рестарта («подтверждено ∧
отсутствует после рестарта == ∅»), 0 `spawn` без предшествующего acknowledged
call-start, 0 автоматических повторов open call (каждый виден как
`needs-human`). Тест входит в `-m slow`; прогон хотя бы раз перед снятием
experimental — условие завершения, ссылка на прогон прилагается к PR (AC-36).
Найденные инъекцией дыры закрываются здесь же, в границах seam-ов.

Документация — отдельные коммиты задачи после green (design §8): `docs/
architecture.md` (раздел «Runtime-state inventory and delivery policy
(#478)» — контракт checkpoint/evidence/closure и «operational minimum» для
legacy; сюда же — снятие `runner.run_claude_async` с диаграмм и та же правка
в `CLAUDE.md`: функцию удаляет DT-02, но `docs/architecture.md` —
`checked_by`-цель этой задачи, а владение файлом есть право его править),
сводка `docs/state-schema.md` (аддитивные столбцы, minor bump —
введена DT-02, здесь сверяется с итогом), `schemas/` (пять версионированных
схем, введённых DT-02…DT-06, — проверка, что список полон), CHANGELOG под
Unreleased — статус `experimental` для `restore` до приёмки FR-01–FR-08 (CON-01)
со ссылкой на #480, README — блок `durability:` и команды `restore`/`evidence`.
Человеческие действия при приёмке PR (AC-43, AC-44), автотестом не
покрываемые намеренно: контракт `run_id`/`pipeline_id` объявлен соседям
(devtools, Maestro) issue с `slug:` + `from:` по ADR-ECO-006 либо записью в
`../prograph-vault/authored/notes/` — без правки их файлов; #480 закрыт
ссылкой на PR с restore-drill (M-01) и матрицей open call (M-02); пункт
`runtime-state-artifact-export` в `TODO.md` закрыт; 1 GiB drill выполнен и
время записано в отчёте. Снятие `RESTORE_EXPERIMENTAL` — отдельное событие
после приёмки, не этой задачи (порог приёмки).

Границы: задача не вводит новых модулей; поведение, которое она измеряет,
доставлено DT-02…DT-13 — честный red здесь в том, что точек инъекции в seam-ах
нет и измерять нечем. Владеет `tests/test_write_ahead_fault_injection.py`
(новый), `docs/architecture.md` (существующий, `kind: manual`) и `CLAUDE.md`
(существующий, носителем сценария не является). Red-рамки по
design: не утверждать наличие строк README/CHANGELOG/`docs/architecture.md`
(BEH-45 — ревью PR), wall-clock внутри теста, имена приватных точек инъекции;
базовый E2E остаётся в `-m "not slow"` у своих задач, здесь — только `slow`.

## Инварианты графа

**Покрытие сценариев.** Каждый сценарий BEH-01…BEH-48 назван ровно одной
задачей; пропусков и дублей нет. Распределение: DT-01 → 28; DT-02 → 03, 05,
06, 07, 24, 26, 38, 44; DT-03 → 12; DT-04 → 36, 37; DT-05 → 13, 14, 47, 48;
DT-06 →
16, 17, 18, 19, 20, 21, 41, 43; DT-07 → 01, 02; DT-08 → 15, 33, 34, 35;
DT-09 → 09, 10, 11; DT-10 → 04, 29, 30, 31, 32, 46; DT-11 → 40; DT-12 → 42;
DT-13 → 08, 22, 23, 25, 27; DT-14 → 39, 45. Итого 1 + 8 + 1 + 2 + 4 + 8 + 2 +
4 + 3 + 6 + 1 + 1 + 5 + 2 = 48 сценариев в четырнадцати задачах.

**Владелец сценария — та задача, на чьей поверхности он наблюдаем.**
`implement`-задача без `delivered_by` обязана погасить свой красный в
собственных границах. Отсюда: BEH-08 (timeout/пустой ответ — не open call)
лежит не на seam-е DT-02, а на DT-13, потому что его And «следующий `run` не
ставит задачу в `needs-human`» наблюдаем только после процедуры DT-09; BEH-04
(read-only команды не пишут run-start) — на DT-10, потому что перечень
команд включает `restore` и `evidence`; BEH-15 — на DT-08, потому что его
последний And наблюдает `restore` DT-06; BEH-41 — на DT-06, потому что его
CI-половина живёт в `tests/test_restore_drill.py`; BEH-43 — на DT-06 по
`checked_by`, а сам sweep — autouse-фикстура conftest, под которую попадают и
E2E последующих задач; BEH-47 и BEH-48 — на DT-05, а не на DT-02/DT-03,
которые кладут их половины (поля области пробы и provenance-карта; выход
`after_mutation`, третья точка drain и таблица outbox-а): наблюдаемы они
только там, где у mutation есть все штатные сайты записи, а у пробы — и
`export_attempt` DT-04. Обе задачи-донора называют это своей границей.

**Один владелец на файл.** Владение = право править файл; цели задач попарно
не пересекаются.

| Задача | Целевой файл (`checked_by`) | Статус файла |
|---|---|---|
| DT-01 | `tests/test_config.py` | существующий, правка по предмету BEH-28 |
| DT-02 | `tests/test_call_start_before_spawn.py`; `tests/test_planning_has_ledger_identity.py`; `tests/test_bounded_evidence_logs.py`; `tests/test_state.py`; `tests/test_cli_info.py`; `tests/test_harness_guards.py` | три новых; три существующих, правка по предмету BEH-03 / BEH-38 / BEH-44 |
| DT-03 | `tests/test_checkpoint_is_a_snapshot.py` | новый |
| DT-04 | `tests/test_evidence_read_surface.py` | новый |
| DT-05 | `tests/test_checkpoint_after_every_mutation.py`; `tests/test_doctor_probe_scope.py`; `tests/test_mutation_checkpoint_ack.py` | новые |
| DT-06 | `tests/test_restore_drill.py`; `tests/test_restore_refusals.py`; `scripts/bench_durability.py` (`kind: manual`) | новые |
| DT-07 | `tests/test_run_identity.py` | новый |
| DT-08 | `tests/test_emergency_spool.py` | новый |
| DT-09 | `tests/test_open_call_door.py` | новый |
| DT-10 | `tests/test_closure_every_exit.py` | новый |
| DT-11 | `tests/test_integrity_fail_closed.py` | новый |
| DT-12 | `tests/test_retention_policy.py` | новый |
| DT-13 | `tests/test_evidence_every_outcome.py`; `tests/test_redaction_corpus.py` | новые |
| DT-14 | `tests/test_write_ahead_fault_injection.py`; `docs/architecture.md` (`kind: manual`) | новый; существующий (включая снятие `run_claude_async` с диаграмм, функцию удаляет DT-02) |

Файлы, которые правятся, но носителем сценария не являются и никем не
заявлены: `tests/conftest.py` (DT-02 — guard и общие двойники; DT-06 —
sweep BEH-43), `tests/fixtures/maestro-interop/` (DT-02, только добавление
ключей), `tests/fixtures/secrets-corpus/` (DT-13, новый), схемы `schemas/*`,
`docs/state-schema.md`, CHANGELOG, README, `CLAUDE.md` (DT-14) — по правилу
design §8, каждая задача правит их в границах своего контракта. Конфликта
владения нет: ни один из них не назван в `checked_by`, и ни один
`checked_by`-файл не правит задача, его не владеющая — правка диаграмм
`docs/architecture.md` по `run_claude_async` поэтому и перенесена в DT-14.

**`delivered_by` ⊆ замыкание `depends_on`.** Задач `type: verify` в графе
нет; поле `delivered_by` не встречается. Поле `verifies` не встречается —
оно рекомендовано только для `verify`-задач и запрещено для `implement`;
двухуровневая проверка его содержимого (фатальная — владелец файла вне
замыкания `depends_on`; нефатальная — файл без владельца) здесь не имеет
предмета. Single-owner считается только по `checked_by`.

**Ацикличность и порядок объявления.** Рёбра: DT-02 → DT-01; DT-03 → DT-02;
DT-04 → DT-03; DT-05 → DT-04; DT-06 → DT-05; DT-07 → DT-03; DT-08 → DT-06;
DT-09 → DT-06; DT-10 → DT-06; DT-11 → DT-06; DT-12 → DT-06; DT-13 → DT-08,
DT-09; DT-14 → DT-07, DT-10, DT-11, DT-12, DT-13. Циклов нет; каждая задача
объявлена после всех, от кого зависит. Искусственной линейной цепи нет:
DT-07 не ждёт DT-04…DT-06 (каналы `run_id` не зависят от restore), пять
задач после DT-06 не зависят друг от друга, а DT-13 и DT-14 зависят ровно от
тех, чью поверхность измеряют. Каждое ребро цепи `core` реальное: seam
пишет в store DT-01; checkpoint drain врезается в `execute` DT-02;
`collect()` читает manifest DT-03; BEH-13 требует существования `evidence`
DT-04; restore применяет WIP и полный manifest DT-05.

**Правило слияния групп.** Задачи, зависящие от членов двух и более чужих
`parallel_group`: DT-13 (`spool` и `door`) и DT-14 (`identity`, `closure`,
`integrity`, `retention`, `matrix`). У каждой из этих групп ровно одна задача,
и она же — sink; DT-13 и DT-14 зависят от всех sink-ов каждой группы, которую
сливают. Точечные рёбра в одну чужую группу — DT-07, DT-08…DT-12 в `core` —
дополнительных рёбер не требуют.

**`tdd_waiver`.** Не объявлен ни у одной задачи: DT-01 открывает граф; у
каждой следующей `implement`-задачи есть наблюдаемое, которого до неё нет
(перечислено в телах как «честный red»); у DT-13 и DT-14 — измерение, которое
до задачи невозможно провести. Санкционировать нечего.

**Соответствие acceptance.** AC-26 — DT-01; AC-02, AC-04, AC-05, AC-06,
AC-22, AC-24, AC-35, AC-40 — DT-02; AC-10 — DT-03; AC-33, AC-34 — DT-04;
AC-11, AC-12 — DT-05; AC-14, AC-15, AC-16, AC-17, AC-18, AC-19, AC-39 —
DT-06; AC-01 — DT-07; AC-13, AC-30, AC-31, AC-32 — DT-08; AC-08, AC-09 —
DT-09; AC-03, AC-27, AC-28, AC-29 — DT-10; AC-37 — DT-11; AC-38 — DT-12;
AC-07, AC-20, AC-21, AC-23, AC-25 — DT-13; AC-36 — DT-14. `verification:
metric` (AC-41 — отчёт бенчмарка DT-06; AC-42 — CI-лог `test_validation_under_60s`
DT-06 и отчёт drill-а; AC-44 — число прогонов drill-матриц DT-06 и open-call
матрицы DT-09) и `verification: manual` (AC-43 — DT-14) закрываются при
приёмке integration PR, а не зелёным задачи. Порог приёмки не предусматривает
частичной приёмки — потому граф не имеет «достаточной» промежуточной точки:
FR-09 (`evidence`, DT-04) стоит в цепи `core`, и его вырезание владельцем
(порог, CHANGELOG) означало бы перенос его сценариев из графа, а не пропуск
задачи.

## Порядок и параллельность

**Критический путь — группа `core`: DT-01 → DT-02 → DT-03 → DT-04 → DT-05 →
DT-06.** Шесть задач строго последовательно, и они определяют длину доставки;
параллелить внутри группы нечего. DT-02 и DT-06 — самые крупные (по восемь
сценариев), и обе на критическом пути: это следствие того, что BEH-05 требует
seam на всех сайтах разом, а `tests/test_restore_drill.py` несёт Git-материал,
restore и sweep вместе. DT-05 выросла до четырёх сценариев по той же
причине, по которой выросли те две: BEH-47 и BEH-48 наблюдаемы только при
полном наборе сайтов записи, и разнести их по задачам-донорам значило бы
предъявить половину знака.

**DT-07 (`identity`) стартует сразу после DT-03** и идёт параллельно
DT-04 → DT-05 → DT-06: каналам `run_id` не нужны ни `evidence`, ни `restore`.

**После DT-06 — пять независимых лент одновременно:** DT-08 (`spool`), DT-09
(`door`), DT-10 (`closure`), DT-11 (`integrity`), DT-12 (`retention`). Каждая
правит свою поверхность (`state.py`/`spool.py`; `paid_call.open_calls` и
`evidence close-call`; `run_context`/`closure`; читатели bundle-а;
`retention.py`/`evidence purge`), общие точки — subparser `evidence` DT-04 и
рубеж старта `_run_start_gate` — его вместе с тремя сайтами (`_run_tasks_inner`,
`cmd_retry`, `cmd_watch`) заводит DT-09, а замену в него кладут три задачи в
объявленном порядке: replay spool DT-08 → целостность DT-11 → процедура open
calls DT-09; каждая задача вставляет свою, не переставляя чужие, и `retry`/
`watch` получают все три разом, потому что рубеж один. Конфликты
слияния между ними — по строкам одного файла, не по семантике; порядок
слияния в integration branch произвольный.

**DT-13 (`matrix`) — после DT-08 и DT-09;** DT-14 (`gate`) — после всех
остальных sink-ов. Максимальная ширина графа — шесть задач одновременно (DT-07
и пять лент после DT-06, если DT-07 ещё не закрыта); без DT-07 — пять.

**Промежуточные состояния integration-ветки, названные честно.** Между DT-02
и DT-09 `open`-строка, оставленная crash-ом, на старте следующего `run` не
распознаётся — молчаливый повтор возможен; между DT-02 и DT-10 closure
пишется без правила вывода, поэтому сигналы и неперехваченные исключения
kind-а не получают, а отказ ack последнего checkpoint-а в closure не
отражается; между DT-03 и DT-05 синхронную точку drain (Q-05 (в)) и строку
outbox-а имеет один сайт — `record_attempt`, — а `budget authorize` и `tdd
release` своего `after_mutation` ещё не зовут вовсе, поэтому окно потери у
них остаётся прежним, добандловым, и правило шага 5 restore на них не
опирается; область пробы `doctor` промежуточного состояния, наоборот, не
создаёт: каждая её половина ложится в ту же задачу, что заводит
соответствующий путь публикации (checkpoint — DT-03, attempt-экспорт —
DT-04, provenance и `task_id` — DT-02), так что окна, в котором проба уже
может публиковать и ещё не погашена, в цепи нет; между DT-02 и DT-14 диаграммы `docs/architecture.md` и `CLAUDE.md`
называют уже удалённый `runner.run_claude_async`; между DT-03 и DT-08 degraded mode DB
по-прежнему «живёт в памяти»; между DT-06 и DT-11 `run` не проверяет
целостность bundle-а. Ни одно из этих состояний не годится для реального
прогона оператора — цепь исполняется внутри одного integration PR (design §8),
и порог приёмки частичной доставки не допускает.

**Что даёт каждая задача наружу.** DT-01 — контракт store и config, на который
ложится всё остальное; DT-02 — первый прогон, где ни один доллар не тратится
без acknowledged call-start (G-01 наполовину); DT-03 — первый durable
boundary; DT-04 — первый ответ по `run_id` без каталога; DT-05 — checkpoint
после каждой mutation с Git-материалом; DT-06 — восстановление в новом каталоге
(M-01, вторая половина G-01); дальше — замыкание отказных ветвей и измерения.

## Вне объёма

Намеренно не декомпозируется здесь:

- **Перераспределение `checked_by` behaviour-спеки.** Крупность DT-02 и DT-06
  — следствие того, что BEH-05 и BEH-16…19/43 лежат в одном файле каждая.
  Развести их по файлам — правка `15-behaviour-spec.md`, не этого документа;
  граф принимает раздачу как есть.
- **Второй адаптер store** (объектное хранилище / CI artifact service) и его
  расходы — отдельное одобрение владельца (CON-08); граф содержит только
  контракт и `LocalVolumeStore`.
- **Вырезание FR-09** при сокращении объёма — решение владельца с записью в
  CHANGELOG (порог приёмки). Здесь FR-09 в объёме; при вырезании DT-04
  сужается до `export_attempt` и legacy-классификации, BEH-36…38 снимаются,
  `evidence close-call` (DT-09) остаётся — это дверь FR-02, не read-surface.
- **Снятие статуса experimental** с `restore` — событие после приёмки
  (CHANGELOG-запись, условие AC-36/AC-44), не задача графа; DT-06
  параметризует оба значения `RESTORE_EXPERIMENTAL`.
- **Ручной 1 GiB drill, issue соседям, закрытие #480 и пункта `TODO.md`** —
  человеческие действия при приёмке PR (AC-43, AC-44), названы в DT-14 как
  приёмочные пункты; автотест на них завёл бы сетевую зависимость и правку
  чужих файлов.
- **Отдельный CI job для `-m slow`** (BEH-39 ×1000) — инфраструктура репо;
  условие завершения требует одного зафиксированного прогона, не гейта.
- **Дефолты `ack_timeout_seconds` (3 s) и `checkpoint_ack_timeout_seconds`
  (60 s)** — уточняются по бенчмарку DT-06, не отдельной задачей.
- **Точные имена приватных функций, сигнатуры dataclass-ов, формат `wip.tar`
  внутри, конкретные regex redactor-а, интервал drain и размер очереди** —
  реализатору в границах design («Вне объёма» design'а).
- **Правка `spec-runner.config.yaml` репо** — задачами не выполняется (design
  §8); `durability:` попадает в config проектов операторов, не этого репо.
- **Изменение правил claims / TDD / waiver / remedy / budget / review**
  (OUT-06) и **автоматическое продолжение после restore** (CON-05) — вне
  workstream-а целиком.
