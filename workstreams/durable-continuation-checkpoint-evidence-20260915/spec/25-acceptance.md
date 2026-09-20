---
spec_stage: acceptance
status: draft
owner_role: qa
traces_to:
- requirements
- behaviour-spec
upstream_hashes:
  requirements: ffbd991ff6f3fa6d2fe0297307c7ee1cbb8af041
  behaviour-spec: c53b871d785d4837b8c73199451f1a98c3c2b5a2
---

# Acceptance — Durable continuation checkpoint и evidence для run/call/attempt (spec-runner#480)

Стадия `acceptance` governance-бандла
`workstreams/durable-continuation-checkpoint-evidence-20260915/`. Единственный
источник критериев приёмки workstream-а: критерии составлены заново от
требований (`10-requirements.md`, FR-01…FR-09, NFR-01…NFR-07) и сценариев
поведения (`15-behaviour-spec.md`, BEH-01…BEH-48); список критериев чартера
сюда не переносится. Термины — в значении upstream'а (§3 требований):
**`run_id`**, **`pipeline_id`**, **`call_id`**, **платный subprocess**,
**provenance**, **policy identity**, **call-start** / **call-result** /
**open call**, **ack**, **run-start** / **run-closure**,
**continuation-relevant mutation**, **continuation checkpoint**,
**manifest**, **WIP artifact**, **evidence bundle**, **emergency spool**,
**artifact store**, **durable boundary**, **legacy run**, **restore**.
«Двойник store» и «двойник `Popen`» — в значении «Области поведения»
behaviour-спеки. Сайт RED agent round (#220) называется здесь только так —
без ярлыка в форме BEH-id.

Входной набор Must-требований: FR-01, FR-02, FR-03, FR-04, FR-05, FR-06,
FR-07, FR-08, NFR-01, NFR-04, NFR-05, NFR-06, NFR-07. Should-требования
FR-09, NFR-02, NFR-03 покрыты по усмотрению qa: FR-09 — с оговоркой о
вырезании владельцем (AC-33…AC-35), NFR-02/NFR-03 — критериями `metric`
(бенчмарк и drill, не CI-гейт, как задаёт Q-10).

Правило чтения критерия: `verification: test` — критерий доказан зелёными
сценариями из `scenarios`; `verification: manual` — прозой названо, что
наблюдает человек; `verification: metric` — прозой назван источник числа,
само число в критерии не зашито.

## Критерии приёмки

### A. Один `run_id` во всех каналах

#### AC-01: Один full UUIDv4 `run_id` на invocation во всех каналах, `pipeline_id` — отдельным полем · verification: test
traces: [FR-01]
scenarios: [BEH-01, BEH-02]

Наблюдаемый знак: после `run --task TASK-001` с `ORCHESTRA_PIPELINE_ID`
OTel JSONL, audit-log, строки `agent_calls` GREEN и review, заголовки
prompt-артефактов, checkpoint manifest и run-closure несут одно и то же
36-символьное значение `run_id` и `pipeline_id == <pid>` отдельным полем; ни
в одном канале одно не подставлено вместо другого, а без `ORCHESTRA_PIPELINE_ID`
`pipeline_id` там, где есть, один и никогда не равен `run_id`. Два
последовательных invocation дают `A != B` при одном `pipeline_id`; один
`watch` на две задачи оставляет один run-start, одну closure и строки обеих
задач под одним `run_id`. Тело prompt-артефакта между заголовком и
терминальной секцией байт в байт равно prompt-у, переданному двойнику
`Popen`. Статический тест не находит `uuid4().hex[:8]` в `cli.py` и
собственного `uuid.uuid4()` для `run_id` в `audit_log.py`; `AuditLogger`
принимает `run_id` параметром.

#### AC-02: Старая state DB и пинованные JSON-контракты меняются только аддитивно · verification: test
traces: [FR-01]
scenarios: [BEH-03]

Наблюдаемый знак: DB, созданная до контракта, открывается `ExecutorState`,
получает столбцы `run_id`/`call_id` без удаления или переписывания строк,
старые строки читаются с `run_id IS NULL` (и `call_id IS NULL` в
call-ledger-ах), `spec-runner costs` показывает прежнюю сумму;
`schemas/executor-state.schema.json` и `docs/state-schema.md` описывают
новые столбцы под minor bump; `--json-result` и `status --json` несут
аддитивные `run_id` (и `pipeline_id`, когда он есть) при сохранённых прежних
ключах, `tests/test_json_result_contract.py` зелёный, golden-фикстуры
изменены только добавлением ключей.

#### AC-03: Run-start и closure пишут только платящие или меняющие continuation-state подкоманды · verification: test
traces: [FR-01, FR-07]
scenarios: [BEH-04]

Наблюдаемый знак: после `status`, `costs`, `validate`, `report`,
`evidence <run_id>` structlog каждой команды содержит свой full UUIDv4
`run_id`, а двойник store не получил от них ни run-start, ни checkpoint-а, ни
closure; каждая из `run`, `retry`, `watch`, `plan`, `review-pr`, `doctor`,
`tdd abandon/repair/resume/release`, `budget authorize`, `restore` оставляет
у двойника ровно один run-start и ровно одну closure. Read-only считается
только форма `evidence <run_id>`: формы `close-call` и `purge` платного
вызова не делают, но меняют continuation-state, поэтому пишут ту же пару, и
их знак предъявлен в AC-08 и AC-38 — там, где живут сами команды. Три пути, не берущие
executor lock, — `retry`, `watch` и `run --all --force` — предъявляются
отдельно и дают ту же пару: run-start приходит от диспетчера `main()` по
перечню платящих подкоманд, а не от `_acquire_run_lock`, которого эти пути не
проходят; отсутствие run-start у любого из трёх — невыполненный критерий.

### B. Запись раньше траты

#### AC-04: На каждом сайте платного вызова acknowledged call-start предшествует `Popen`, через один seam · verification: test
traces: [FR-02]
scenarios: [BEH-05]

Наблюдаемый знак: в общем журнале двойников для каждого `spawn` (RED
authoring, RED agent round (#220), GREEN, review, `review:<role>` в
параллельном и последовательном режиме, три стадии `plan --full`,
`plan --gated`, интерактивный `plan "<описание>"` на один круг цикла,
`review-pr` verify и fix, оба сайта пробы `doctor --with-review`)
непосредственно раньше
стоит `call_start` с ack и тем же `call_id`, число `spawn` равно числу
acknowledged call-start-ов; call-start несёт `run_id`, `call_id`, provenance
из одного словаря (`red`, `red:fix`, `green`, `review`, `review:<role>`,
`plan:<stage>`, `plan:interactive`, `review-pr:verify`, `review-pr:fix`,
`doctor:execute`, `doctor:review`), policy
identity, digest redacted prompt-а, timestamp и для task-сайтов
`task_id`/номер attempt — **кроме** двух сайтов эфемерной пробы, чьи записи
задачи не несут (AC-45); значение словаря, которого не производит ни один
сайт журнала, — невыполненный критерий; тот же `call_id` стоит рядом с
`provenance` в строке ledger-а своего семейства (`agent_calls` — сайты
задачи, `pr_agent_calls` — `review-pr`, `plan_agent_calls` — планирование;
строка планирования в `agent_calls` критерий не выполняет, см. AC-22). Все три платных пути
`cli_plan.py` — gated, `--full` и интерактивный — предъявлены в журнале:
критерий не считается выполненным, если seam доказан на двух из трёх.
Статический тест по образцу `PaidBinaryReached` красный на любом
`subprocess.run`/`Popen` с argv провайдера в обход seam-а. Отказ
autouse-гварда на `_spawn` наблюдается как падение теста с вердиктом гварда
на сайте **вне** `execute_task` — на review и на интерактивном `plan`, с
настоящим именем `claude`: прогон, получивший вместо этого
`ReviewVerdict.ERROR` или exit 0, — невыполненный критерий (отказ проглочен
их собственным `except Exception`; пояс не срабатывает, потому что процесс не
создавался, — значит тип отказа обязан быть от `BaseException`).

#### AC-05: Без acknowledgement процесс не стартует — отказ до траты, exit 2, closure с причиной · verification: test
traces: [FR-02, FR-07, NFR-01]
scenarios: [BEH-06]

Наблюдаемый знак: с двойником store, отказывающим в ack (ошибкой или
превышением объявленного таймаута), `run --task TASK-001` под
`execution_mode: tdd` завершается exit 2 при 0 вызовах двойника `Popen`;
статус задачи не `done`, attempt несёт `error_code = INFRASTRUCTURE`; stderr
называет `Refusal(kind="instrument")` — не «tests/lint check» и не трейсбек;
closure записана с kind `failed` и exit code 2 (`error_kind` attempt-а —
`instrument`, вне отказного подмножества `ERROR_KINDS`), либо stderr называет
отказ её записи.
Таймаут ack — конфигурируемый ключ, превышение ведёт к тому же исходу, а не к
ожиданию без предела.

#### AC-06: Budget-отказ происходит до call-start и не оставляет строки · verification: test
traces: [FR-02]
scenarios: [BEH-07]

Наблюдаемый знак: при исчерпанном `task_budget` и отказе budget guard перед
GREEN двойник store не получил call-start, двойник `Popen` не вызван, строки в
`agent_calls` нет, prompt-артефакт GREEN заканчивается
`=== NOT STARTED: … ===`, attempt несёт `error_code = BUDGET_EXCEEDED` и
`error_kind = budget`, closure прогона — kind `refused`.

#### AC-07: Timeout, пустой ответ, `is_error` при exit 0 и crash провайдера — call-result, не open call · verification: test
traces: [FR-02, FR-06]
scenarios: [BEH-08]

Наблюдаемый знак: у каждого из четырёх режимов fake CLI есть call-start и
ровно один call-result с outcome по `runner.classify_agent_answer`
(`timeout`, `failed`×3) и стоимостью-числом либо `null`; `evidence <run_id>`
показывает 0 open calls, следующий `run` в том же namespace не ставит задачу в
`needs-human` по этой причине.

#### AC-08: Open call блокирует продолжение до аудируемого закрытия оператором · verification: test
traces: [FR-02, FR-05]
scenarios: [BEH-09, BEH-11]

Наблюдаемый знак: после `os._exit` между ack call-start и записью
call-result (на обеих границах — до spawn и после него) новый процесс в новом
каталоге видит open call по evidence bundle/checkpoint: `run --all`
пропускает задачу с причиной, называющей `call_id`, provenance и «open
call», и выполняет остальные; `run --task TASK-001` отказывает с той же
причиной, exit 1; `restore` печатает `needs-human: open call <call_id>
(<provenance>, TASK-001)` и завершается exit 1 — двойник `Popen` для задачи
вызван 0 раз во всех трёх шагах, второй call-start для того же attempt не
создан. Граница детекции — гарды старта, а не executor lock, и предъявлена
она на всех путях, которые lock не берут (как AC-03 для run-start и AC-30
для replay): `retry TASK-001` отказывает той же причиной с exit 1, `watch`, остановленный stop-файлом после первого круга,
предъявляет тот же open call на старте invocation и задачу не берёт, `run --all --force` её пропускает — 0 вызовов двойника `Popen` во
всех трёх. Детекция, доказанная вызовом `_acquire_run_lock`, критерий не
выполняет; `retry` или `watch`, дошедший до нового call-start для того же
attempt, — невыполненный критерий. `evidence close-call` без `--reason` отказана до записи; с `--reason`
пишет call-result `resolved_unknown` со ссылкой на open call, actor и
reason; повтор отвечает «уже закрыт» без второй записи; с
`SPEC_RUNNER_AGENT=1` отказана guardrail-ом; после закрытия
`run --task TASK-001` доходит до нового call-start с новым `call_id`, а сама
команда закрытия платный вызов не запускает. У каждого из четырёх её
invocation-ов двойник store получает ровно один run-start и ровно одну
closure, kind которой выводит диспетчер по коду выхода (`completed` на
закрытии и идемпотентном повторе, `failed` под guardrail-ом и при недоступном
store с exit 2); попытка без `--reason` отказана argparse-ом до `start()` и
потому не имеет ни run-start, ни closure, а
checkpoint закрытия строки опубликован под тем же `run_id`: checkpoint под
`run_id` без run-start — невыполненный критерий. Тот же знак предъявлен, когда
локальной state DB между crash-ом и следующим `run` нет: после `spec-runner
reset`, после ручного удаления файла DB и в клоне репозитория на другом пути
`run --all` по-прежнему пропускает задачу, `run --task TASK-001` отказывает
exit 1, двойник `Popen` не вызван, — а в каталоге без единого прошлого
прогона `run` стартует обычным порядком. Пустой `agent_calls` свежей DB,
принятый за «open call нет», — невыполненный критерий. Тот же знак
предъявлен там, где «по `run_id`» и «namespace-wide» расходятся: прогон A
закрыт, более поздний прогон C того же namespace оставил open call X,
`restore <run_id-A>` отказывает `needs-human` с `run_id` прогона C,
`call_id` X и его provenance, `--into` остаётся пустым и `Popen` не вызван;
после закрытия X дверью restore применяется — и условие этого названо, а не
подразумевается: под `run_id` прогона C нет ни одного acknowledged
checkpoint-а, это факт его конфигурации и проверяется у двойника store, а
не выводится из границы краша (достижимость самой конфигурации зависит от
открытого вопроса «публикует ли checkpoint флип `in_progress`» — §3 против
§3.1, см. BEH-09; критерий написан против узкого прочтения), — поэтому второе условие ключа шага 5 на
нём не выполняется (прогон, успевший закрыть задачу до краша, несёт строку
`attempts` с доставленным checkpoint-ом и блокирует — это соседняя
конфигурация, не эта) (дверь закрывает **call**, не
прогон, и сама в блокирующую половину не входит); первый `run` в
восстановленном каталоге предъявляет open call прогона D, начатого уже
**после** применения snapshot-а — начнись он раньше, его собственный open
call отказал бы восстановлению шагом 4, который стоит до шага 5, — и
предъявляет его, обратившись к индексу workstream-а, а не к `open`-строкам
snapshot-а. Та же последовательность команд при C, успевшем доставить
acknowledged checkpoint, обязана дать противоположный ответ:
`restore <run_id-A>` отказывает `needs-human` шагом 5, называя выходом C;
применённый snapshot A здесь — невыполненный критерий;
`restore` более раннего `run_id` при более позднем закрытом прогоне того же
workstream-а **из блокирующей половины перечня, несущем хотя бы один
acknowledged checkpoint**, — предъявлено как минимум на `review-pr` и на `plan` —
отказывает `needs-human`, называя выходом последний прогон workstream-а с
acknowledged checkpoint-ом, после которого нет ни одного блокирующего
прогона. Знак берётся исполнением: `restore` напечатанного `run_id` в новый
пустой `--into` применяет snapshot; его отказ — невыполненный критерий. В
конфигурации A → B (`plan`, checkpoint) → C (`review-pr`, checkpoint)
выходом обязан быть C; напечатанные здесь B или сам восстанавливаемый
`run_id` — невыполненный критерий. Более поздний
**холостой** прогон из той же половины отказа не даёт: `run --all`, которому
нечего делать, и `run --all`, чей старт отказан занятым executor lock-ом
после run-start, — оба закрылись, оба позже A, acknowledged checkpoint-ов нет
ни у одного,
snapshot A применяется; отказ здесь — невыполненный критерий, потому что
восстановить названный выходом холостой прогон нечем. Более поздний прогон
из неблокирующей половины отказа не даёт независимо от checkpoint-ов, и это
предъявлено на трёх, каждый из которых оставляет свою строку в индексе
workstream-а: аудируемая дверь `evidence close-call` (в обеих
конфигурациях — где вызов жил в `agent_calls` и где в `pr_agent_calls`; свой
checkpoint она публикует, и он не должен ничего менять), `evidence purge`,
отработавший между A и восстановлением, и чужой, более ранний `restore` —
наблюдается при восстановлении A второй раз в новый пустой `--into`
(повторный запуск в занятый каталог отказывает раньше, на проверке
каталога). Отказ на любом из трёх — невыполненный критерий. Четвёртый —
`doctor`, и знак у него другой: не строка перечня, а отсутствие второго
условия ключа вовсе, потому что acknowledged checkpoint-ов у него нет ни
одного (AC-45); `doctor --with-review --yes`, отработавший между A и
восстановлением, отказа не даёт, и он же не назван выходом ни в одной
конфигурации — напечатанный выходом `doctor` невыполненный критерий, его
собственный `restore` упёрся бы в отсутствие digests. Применённый
snapshot A в конфигурациях с open call и с блокирующим более поздним
прогоном, несущим acknowledged checkpoint, — невыполненный критерий.

#### AC-09: Один `call_id` — ровно один call-start и не более одного call-result · verification: test
traces: [FR-02, FR-06]
scenarios: [BEH-10]

Наблюдаемый знак: второй call-result для того же `call_id` отвергнут ошибкой,
называющей `call_id`, чтение возвращает первый результат неизменным; второй
call-start с тем же `call_id` отвергнут; `call_id` разных вызовов в одном и в
разных прогонах различны (full UUIDv4); отказ записи не меняет исход задачи
и не удаляет первую запись.

### C. Checkpoint после каждой continuation-relevant mutation

#### AC-10: Checkpoint — транзакционный snapshot с WAL-only страницами, а не копия файла · verification: test
traces: [FR-03]
scenarios: [BEH-12]

Наблюдаемый знак: при `PRAGMA wal_autocheckpoint=0` и открытом соединении
attempt, лежащий только в `-wal`, виден в DB, восстановленной из snapshot-а
checkpoint-а в новом каталоге; контрольный вариант с `shutil.copy`
main-файла `.db` attempt **не** видит — тест различает backup API и
копирование.

#### AC-11: Каждая mutation из перечня публикует новый checkpoint с большим `sequence`; read-only команды — ни одного · verification: test
traces: [FR-03]
scenarios: [BEH-13]

Наблюдаемый знак: после каждой из mutation — attempt, RED checkpoint,
запись и release claims, `tdd_phases`, `verify_evidence`, gate verdict,
waiver, remedy, `budget authorize` отдельным invocation, строка `pr_*`,
harness status flip — двойник store получил ровно один новый checkpoint с
UUIDv4 `checkpoint_id`, строго возрастающим `sequence` внутри `run_id` и
`supersedes` на предыдущий; после `status`, `costs`, `validate`, `report`,
`evidence <run_id>` — ничего (read-only здесь именует форму `evidence
<run_id>`, а не подкоманду целиком: checkpoint формы `close-call` —
знак AC-08); checkpoint после `budget authorize` содержит
authorization в DB snapshot без attempt; статический тест находит ровно один
seam «после mutation», через который проходят все перечисленные сайты записи;
mutation эфемерной пробы через тот же seam checkpoint-а не даёт — полный знак
области пробы — AC-45.

#### AC-12: Manifest валиден по схеме, полон и не содержит локальных фактов · verification: test
traces: [FR-03, FR-04, NFR-04]
scenarios: [BEH-14]

Наблюдаемый знак: manifest любого checkpoint-а проходит
`schemas/checkpoint-manifest.schema.json` и содержит `contract_version`,
`run_id`, `pipeline_id`, repository identity (нормализованный remote URL,
root commit), workstream (namespace, `spec_prefix`/`change_id`), `head`,
`published_ref` с reachability, `config_hash`, effective TDD namespace как
значение с `namespace_source: computed` (и `declared` при объявленном
`tdd_namespace`), join keys, `digests` каждого файла, `excluded` с lock,
`.<prefix>spec.lock`, `.executor-stop`, временными worktrees,
`.executor-progress.txt`, и digest верхнего уровня над собой; `grep` по
manifest и всем файлам checkpoint-а не находит абсолютного пути исходного
каталога, PID и имени временного worktree.

#### AC-13: Checkpoint в degraded mode несёт spool и `degraded: true` · verification: test
traces: [FR-03, FR-08]
scenarios: [BEH-15]

Наблюдаемый знак: при двойнике SQLite, уронившем запись attempt, двойник
store получает checkpoint с `degraded: true` в manifest, spool-файлом с этой
mutation и DB snapshot-ом последнего успешного состояния; `state_degraded`
уведомлён один раз; restore из этого checkpoint-а доигрывает spool и
показывает attempt.

#### AC-45: Проба `doctor` не оставляет следа в учёте проекта, а её платные вызовы — оставляют полный · verification: test
traces: [FR-02, FR-03, FR-06]
scenarios: [BEH-47]

Наблюдаемый знак: после `spec-runner doctor --with-review --yes` с fake CLI
двойник store имеет от этого invocation run-start, две пары
call-start/call-result с provenance `doctor:execute` и `doctor:review` и одну
closure — и ни одного checkpoint-а, ни одного attempt-экспорта; любой
checkpoint под этим `run_id` — невыполненный критерий. Ни одна
опубликованная запись пробы не несёт `task_id`, а столбец
`agent_calls.task_id` остаётся `NOT NULL` и миграции не требует. Следующий
`run --all` в том же каталоге берёт собственную `TASK-001` проекта и доходит
до её платного вызова — в том числе после `spec-runner reset` и в свежем
клоне, где `open`-строки восстанавливаются из store: заблокированная
одноимённой канонной задачей пробы задача проекта — невыполненный критерий.
Все ключи лежат в store вызывающего при **относительном** пути в настройках
адаптера: путеподобная настройка разрешена в абсолютную до смены рабочего
каталога пробой, и 0 ключей после `doctor` (адрес уехал в scratch и удалён с
ним) — невыполненный критерий, потому что вызов при этом состоялся и деньги
потрачены. Платных вызовов ровно два и у проекта под `execution_mode: tdd`
(проба идёт под `standard`, третьего вызова нет), и тот же набор ключей
предъявлен при verdict `broken`.

#### AC-46: Подкоманда без платного вызова не завершается успешно, пока её checkpoint не acknowledged · verification: test
traces: [FR-03, FR-05, FR-07]
scenarios: [BEH-48]

Наблюдаемый знак: в `spec-runner budget authorize … --reason …` (одна
mutation), `spec-runner tdd release … --reason …` (две) и `spec-runner tdd
abandon … --reason …` (многошаговая) с исправным двойником store ack
mutation-checkpoint-а стоит в журнале раньше выхода с
кодом 0; с двойником, отклоняющим ack, все три завершаются exit 2, stderr
называет недоставленный `sequence` и `checkpoint_id` и говорит, что решение
записано локально, но не доставлено, — нулевой код у любой из трёх
невыполненный критерий, а напечатанная handler-ом строка успеха успехом не
считается. Отказ ack при этом **не рвёт** многошаговую подкоманду: у `tdd
abandon` в DB есть и retirement claims, и строка `tdd_remedies` с actor и
reason; состояние «claims сняты, audit-строки нет» — невыполненный
критерий. Mutation в DB есть (закоммичены раньше). Что критерий **не**
требует и почему: транзакционного обязательства публикации бандл не вводит
(усиление #527 вынесено в spec-runner#528), поэтому убитый в окне между
записью mutation и ack прогон правку теряет — это принятый пробел FR-05, и
его отсутствие критерием не является. Горячий путь `run` при этом не
получает ни одного нового синхронного ожидания: `run --task` ведёт себя как
до бандла по числу и месту обращений к двойнику store, и ожидание на каждую
mutation задачи — невыполненный критерий (RK-01): ожиданий у invocation-а
столько, сколько точек drain, и число mutation подкоманды его не
увеличивает. Цену гейта перед closure меряет AC-41 (бенчмарк), не этот
критерий.

### D. Git-материал и незавершённая работа

#### AC-14: Local-only commit, dirty, untracked и rescue stash восстанавливаются байт в байт · verification: test
traces: [FR-04]
scenarios: [BEH-16]

Наблюдаемый знак: после удаления исходного каталога и `restore … --into
<other-abs-path> --experimental` `git log` task-ветки содержит local-only
commit с тем же SHA, dirty и untracked файлы имеют записанные заранее
SHA-256, `git stash list` содержит stash с меткой `spec-runner rescue:
TASK-001 …` и тем же содержимым; manifest несёт для base, task-ветки и
integration branch имя, SHA и reachability, а размер WIP artifact не зависит
от объёма published-истории; `.executor-*` в WIP отсутствует.

#### AC-15: Lock, stop-marker, временные worktrees и process identity не восстанавливаются · verification: test
traces: [FR-04, FR-05]
scenarios: [BEH-17]

Наблюдаемый знак: в восстановленном каталоге нет `.executor-state.lock`
(в т.ч. с `--spec-prefix`), `.executor-stop`, `.<prefix>spec.lock`,
`.executor-progress.txt` и worktree `spec-runner-red-*`; `git worktree list`
показывает только основной; `spec-runner status` не сообщает о stale lock
чужого PID, следующий `run` создаёт lock живым процессом; virtualenv и tool
caches не восстановлены и не числятся в manifest обязательными.

#### AC-16: Пустой WIP — явная запись; недостижимый published ref — `needs-human` с именем ref и SHA · verification: test
traces: [FR-04]
scenarios: [BEH-18]

Наблюдаемый знак: для прогона с чистым деревом manifest несёт `wip: none`,
restore проходит с exit 0 и `--json` отмечает `wip: none`; для manifest-а с
удалённым на fake forge published ref restore останавливается `needs-human`,
exit 1, называя имя ref и ожидаемый SHA, ничего не записав в каталог и не
реконструируя ref; digest WIP artifact присутствует в `digests` manifest-а.

### E. Restore по `run_id` в новом каталоге

#### AC-17: Restore возвращает claims, authority, waiver, namespace и печатает следующий шаг без оплаты · verification: test
traces: [FR-05, FR-04]
scenarios: [BEH-19]

Наблюдаемый знак: restore в другой абсолютный путь завершается exit 0,
печатает `spec-runner run --task TASK-001`, двойник `Popen` за время restore
вызван 0 раз; `tdd status` показывает active claim с тем же blob SHA и тот
же RED checkpoint, `costs` — authorization, `budget.effective_limits` —
поднятый потолок, effective namespace равен значению manifest-а; изменение
замороженного файла до `run` даёт отказ claims gate; следующий `run`
начинается с GREEN без нового call-start для RED; строки `tdd_claims`,
`red_checkpoints`, `budget_authorizations`, `phase_waivers` совпадают с
экспортом terminal attempt-а исходного прогона.

#### AC-18: Все проверки restore — до записи в каталог, в объявленном порядке, любое несовпадение — отказ · verification: test
traces: [FR-05, NFR-04]
scenarios: [BEH-20]

Наблюдаемый знак: для семи подменённых условий (contract version из
будущего; байт в DB snapshot; другой root commit; другой `review_policy`;
другой `tdd_namespace`; open call; повреждённая строка spool) `--into`
остаётся пустым, exit 1 для repository/policy/namespace/open call и exit 2
для contract/digest/spool, `Popen` 0 раз, `claims.check_claims` не вызван;
сообщение namespace-несовпадения содержит оба значения и оба
`namespace_source`, policy — ключ и оба значения, repository — оба root
commit; при двух подменах сразу отказ называет первую по порядку contract
version → digests → repository identity → policy identity → namespace → open
calls → spool; при необъявленном `tdd_namespace` restore дописывает его в
config нового каталога, печатает diff, и файл виден в `git status`. Полнота перечня,
по которому шаг 5 решает, проверена подсадкой, а не чтением: подкоманда,
добавленная в `PAYING_SUBCOMMANDS` и не отнесённая ни к одной половине,
красит тест `set(PAYING_SUBCOMMANDS) == BLOCKING | NON_BLOCKING`; зелёный
тест на такой подсадке — невыполненный критерий. Задачи, растящие множество,
дописывают подкоманду в половину тем же изменением, поэтому красноты на
границе задачи быть не должно.

#### AC-19: Legacy run, отсутствие `--experimental` и непустой `--into` — отказы с точной причиной · verification: test
traces: [FR-05]
scenarios: [BEH-21]

Наблюдаемый знак: legacy `run_id` → exit 1 с перечнем «нет run-start», «нет
manifest», «нет call records» и ссылкой на «operational minimum» в
`docs/architecture.md`, без реконструкции; без `--experimental` до снятия
статуса — отказ с текстом CON-01 и нетронутым каталогом, после снятия флаг не
требуется (оба состояния — параметр теста); непустой `--into` — отказ с байт
в байт прежним содержимым; `restore --json` во всех исходах валиден по
`schemas/restore-result.schema.json`, несёт `next_step` либо `reason`, exit
0/1/2 однозначно соответствует `ok`/`needs-human`/`instrument`.

### F. Evidence на каждом платном вызове и terminal attempt

#### AC-20: Каждая клетка матрицы outcome × site оставляет адресуемый call record · verification: test
traces: [FR-06, FR-02]
scenarios: [BEH-22]

Наблюдаемый знак: для {success, `TASK_FAILED`, blocked, timeout,
infrastructure error} × {GREEN, review, `review:<role>`, `plan --full`,
`plan --gated`, интерактивный `plan`, `review-pr fix`, `doctor`} в store есть
record по
`run_id/call_id` с provenance сайта, outcome клетки, стоимостью-числом или
`null` (никогда `0.0` за неизвестную), bounded/redacted prompt и result с
SHA-256 и размером полного содержимого; клетки `TASK_BLOCKED`, timeout,
`INFRASTRUCTURE`, `is_error` при exit 0 оставляют record наравне с success;
число records равно числу `spawn` двойника во всей матрице.

#### AC-21: Terminal attempt экспортирует JSONL своих строк, и только своих · verification: test
traces: [FR-06]
scenarios: [BEH-23]

Наблюдаемый знак: для задач, завершившихся `done`, `failed`, `blocked`,
опубликован JSONL, валидный по `schemas/evidence-record.schema.json`, со
строками этой задачи/attempt-а из `attempts`, `red_checkpoints`,
`tdd_claims`, `tdd_phases`, `tdd_remedies`, `phase_waivers`/`waivers_applied`,
`budget_authorizations`, `gate_verdicts`, `verify_evidence`, `agent_calls`,
каждая с namespace, task, attempt, `config_hash`, actor, reason, referenced
SHA/blob, timestamp, supersession status; в экспорте TASK-001 нет строк
TASK-002/TASK-003; `pr_*` экспортируются при завершении раунда `review-pr`.
С bundle того же `run_id` опубликованы срез task change history за этот
прогон и срез compliance audit-trail со строками этого `run_id` — когда
источники есть; при выключенном аудите и отсутствующей истории их нет, и
прогон не отказывает. Ни в одном из двух срезов нет строк чужого прогона,
оба redacted и ограничены по размеру, и ни `evidence`, ни `restore` не
выводят из них статус задачи.

#### AC-22: Планирование получает ledger-identity и отдельную строку `planning` в `costs` · verification: test
traces: [FR-06, FR-01]
scenarios: [BEH-24]

Наблюдаемый знак: `plan --full` оставляет три call records
(`plan:requirements`, `plan:design`, `plan:tasks`) **без задачи**,
`plan --gated --stage requirements` — один, каждый с `run_id` своего
invocation и своим `call_id`; `costs` и `costs --json` показывают их суммой
строкой «planning», `task_cost` выполненной задачи не изменился,
`repo_total_cost` включает planning; интерактивный `plan "<описание>"`,
прогнанный на один круг, оставляет свой record с provenance
`plan:interactive` и тоже без задачи; все три пути планирования проходят
через тот же seam, что task-сайты. «Без задачи» предъявляется как
отсутствие задачи у строки, а не как `NULL` в `agent_calls`: число строк
`agent_calls` после этих прогонов не изменилось, а записи планирования
читаются своим ledger-ом семьи. Критерий, выполненный `NULL`-ом в
`agent_calls.task_id`, не выполнен: столбец `NOT NULL`, вставка исчезает
warning-ом, и планирование остаётся без записи вовсе.

#### AC-23: Опубликованная запись не переписывается; исправление — новая запись со ссылкой · verification: test
traces: [FR-06]
scenarios: [BEH-25]

Наблюдаемый знак: публикация с тем же ключом и другим содержимым отвергнута,
чтение по ключу возвращает исходную запись байт в байт; «исправление» с
`supersedes` принято под новым ключом, `evidence <run_id>` показывает обе,
помечая первую superseded; контракт адаптера объявляет запрет overwrite, и
адаптер без него отклонён при загрузке config.

#### AC-24: Bounded prompt и result сохраняют доказательство исходных байтов · verification: test
traces: [FR-06, NFR-06]
scenarios: [BEH-26]

Наблюдаемый знак: для prompt-а и result-а сверх бюджетов NFR-06 bounded
копии не превышают объявленных пределов, содержат head **и** tail исходника
(в tail — frozen-files block), marker `truncated`, `full_sha256` и
`full_size`, равные SHA-256 и размеру исходника; для контрольного вызова в
бюджете marker-а нет, `full_sha256` равен digest bounded-копии, `full_size`
— её размеру; полный текст остаётся в локальном prompt-артефакте.

#### AC-25: Секреты не публикуются; единственный путь публикации проходит через redactor · verification: test
traces: [FR-06, NFR-05]
scenarios: [BEH-27]

Наблюдаемый знак: ни одно значение корпуса `tests/fixtures/secrets-corpus/`
(половина в prompt, половина в result и stderr fake CLI) не встречается ни в
одном файле, полученном двойником store — bundle, checkpoint, WIP artifact,
spool; `full_sha256`/`full_size` соответствуют нередактированному
содержимому; статический тест по образцу `PaidBinaryReached` красный на
прямом `put` адаптера в обход redactor-а; `evidence <run_id>` показывает
redacted копии, локальный prompt-артефакт — полный текст.

#### AC-26: Адаптер store без TLS или шифрования в покое отклонён при загрузке config · verification: test
traces: [NFR-05]
scenarios: [BEH-28]

Наблюдаемый знак: config с адаптером `tls: false` или без объявленного
шифрования в покое даёт `ConfigError` при `build_config` и ошибку `validate`
с именем адаптера и недостающего свойства, `run` с ним не доходит до
run-start; config с `tls: true` и managed encryption загружается.

### G. Closure на каждом штатном завершении

#### AC-27: Каждый orderly exit любой платящей подкоманды оставляет ровно одну closure, kind которой выведен одним правилом · verification: test
traces: [FR-07]
scenarios: [BEH-29, BEH-32, BEH-46]

Наблюдаемый знак: у каждой конфигурации `run` — все задачи `done`, нет
ready-задач, `--dry-run`, несуществующий `--task`, красный `validate`,
governance-гейт, dirty-spec guard, tracked-state-DB guard, занятый lock,
budget guard перед вызовом, неудовлетворённый gate под `review_policy:
required`, отказ ack, истёкший session-таймер при невыполненных задачах,
SIGTERM и неперехваченное исключение — ровно одна closure с kind из пяти
словаря `schemas/run-closure.schema.json`, `last_checkpoint_id` последнего
acknowledged checkpoint-а (или `null`), записанным фактическим exit code,
`run_id`, `pipeline_id`, подкомандой, числом open calls, `degraded`/spool
status, timestamps и `last_call_ids`/`attempt_ids`. Kind отвечает правилу:
`completed` — только при коде 0 и отсутствии невыполненной работы; `refused`
— при ненулевом коде с отказным `error_kind` последнего неуспешного
attempt-а (`policy` у gate-отказа — типизированный `Refusal` гейта, `budget`
у budget guard-а); `failed`
— на прочих ненулевых кодах и на коде 0 с невыполненной работой;
`interrupted` — на SIGTERM, который диспетчер видит по флагу
`executor._shutdown_requested` (design § 6.3), при штатном коде выхода
цикла; `crashed` — на неперехваченном исключении.
Closure с невыполненными задачами и kind `completed` — невыполненный
критерий, и это предъявляется прямо на конфигурации «неудовлетворённый gate»,
где персистированный `last_run_stop_reason` остался дефолтным `completed`, а
`run` вышел с кодом 1: closure `last_run_stop_reason` не читает.
Четыре гарда старта дают `failed` с exit 1, для «lock занят» двойник
получает пару run-start + closure под одним `run_id`, и равенство reason
какому-либо словарю не требуется — reason свободная строка и может быть
пустым. `run --all --force` предъявляется отдельной конфигурацией: executor
lock не берётся, run-start и closure есть.

Тот же знак предъявлен у восьми платящих подкоманд вне `run` — `retry`,
`watch`, `doctor`, `plan`, `review-pr`, `tdd
abandon/repair/resume/release`, `budget authorize`, `restore`, — и критерий
считается выполненным только вместе с ними: у каждой по одному invocation на
каждую достижимую строку правила, ровно один run-start и ровно одна closure
с выведенным kind и фактическим exit code. Две конфигурации, где код процесса
лжёт об исходе, предъявлены прямо: `retry` с задачей `blocked` и `watch`,
остановленный `max_consecutive_failures`, выходят с кодом 0 и дают `failed`,
потому что задача, по которой invocation записал attempt, не в статусе
`success`; closure `completed` на любой из двух — невыполненный критерий.
Третья конфигурация того же класса предъявлена паритетом, а не отдельным
правилом: `watch` с красной pre-run validation выходит с кодом 1 и даёт
`failed` — тот же ответ, что конфигурация «красный `validate`» у `run`
выше, на той же спеке. Код 0 или kind `completed` здесь — невыполненный
критерий; знак берётся исполнением обеих подкоманд, а не чтением правила.
Правило предъявлено отдельно двойником handler-а, завершающимся каждым из
шести способов (исключение, сигнал, ненулевой код с отказным attempt-ом,
прочий ненулевой код, код 0 с невыполненной работой, код 0 без неё): kind
выведен на всех шести, `completed` — только на последнем, run-start без
closure не остаётся ни на одном. Статический тест подтверждает, что ни один
сайт выхода kind не выбирает: `note_stop`, таблиц причин по подкомандам и
новых значений `RUN_STOP_REASONS` в дереве нет. Различение «отказ правила» и
«поломка инструмента» у подкоманд без attempt-ов не требуется и требованием
не является: `plan --gated` с неодобренным upstream-ом даёт `failed`, и
требование `refused` на нём — невыполненный критерий. Две оставшиеся платящие
формы — `evidence close-call` и `evidence purge` — в этот критерий не входят:
их исходы предъявлены в AC-08 и AC-38, и все одиннадцать позиций перечня
FR-01 закрыты только тремя критериями вместе.

#### AC-28: `kill -9` не оставляет closure; читатели классифицируют прогон как crash/unknown · verification: test
traces: [FR-07, FR-09]
scenarios: [BEH-30]

Наблюдаемый знак: после SIGKILL за run-start и первым acknowledged
checkpoint-ом closure для `run_id` отсутствует, `evidence` показывает
`crash/unknown` (не `completed`), `restore` восстанавливает с последнего
acknowledged checkpoint-а и печатает следующий шаг только при 0 open calls,
иначе `needs-human`; `status` в живом исходном каталоге не считает прогон
завершённым и показывает его `run_id`.

#### AC-29: Closure — последняя запись; отказ ack последнего checkpoint-а и повторная closure — fail-closed · verification: test
traces: [FR-07, FR-03]
scenarios: [BEH-31]

Наблюдаемый знак: при неподтверждённом последнем checkpoint-е closure несёт
kind `failed`, `last_checkpoint_id` предыдущего acknowledged и exit 2; при отказе записи
closure stderr называет причину и exit code не становится 0; повторная
closure отвергнута, первая неизменна; журнал двойника не содержит
публикации checkpoint-а после closure.

### H. Аварийный spool при отказе DB

#### AC-30: Отказ SQLite сохраняет mutation в spool, новый процесс доигрывает её до любой работы · verification: test
traces: [FR-08, NFR-01]
scenarios: [BEH-33]

Наблюдаемый знак: для каждой mutation (`record_attempt`, claims, budget
authorization, gate verdict, `record_verify_evidence`, RED checkpoint, remedy,
waiver) spool в `.executor-*` поясе содержит строку с `seq`, `run_id`,
`namespace`, `task_id`/attempt, `table`, payload и SHA-256 строки, записанную
с `fsync`; новый процесс после run-start и гардов старта, до выбора задачи
доигрывает её в DB в порядке `seq`, `status`/`tdd status`/`costs` показывают
mutation, следующий checkpoint содержит её в snapshot-е, spool ротирован в
архив с пометкой в manifest; mutation, не подтверждённую ни DB, ни spool,
никакой путь не считает записанной. Граница replay — run-start и гарды старта,
а не executor lock: как AC-03 предъявляет три пути без lock-а для run-start,
так и здесь replay предъявлен на `spec-runner retry`, `spec-runner watch` и
`run --all --force` — ни один из трёх executor lock не берёт (`--force` даёт
`lock = None`), и доказательство replay вызовом `_acquire_run_lock` критерий
не выполняет.

#### AC-31: Replay идемпотентен, повреждённая строка spool останавливает старт · verification: test
traces: [FR-08]
scenarios: [BEH-34]

Наблюдаемый знак: после двух replay число строк в DB равно числу после
первого; с изменённым байтом payload replay отказывает, называя `seq` и
ожидаемый/фактический SHA-256, прогон не стартует — `Refusal(kind=
"instrument")`, exit 2, 0 `Popen`, closure `failed`;
статический тест находит единственного читателя spool — replay.

#### AC-32: Одновременный отказ DB и spool останавливает исполнение до следующего платного вызова · verification: test
traces: [FR-08, FR-07, NFR-01]
scenarios: [BEH-35]

Наблюдаемый знак: после двойного отказа на записи attempt TASK-001 двойник
`Popen` не вызван для TASK-002, exit 2, closure `failed` с причиной,
называющей обе неудачи; при недоступном store stderr называет
причину и exit остаётся 2; `status` в следующем процессе показывает TASK-001
незакрытой, а не `done`.

### I. Read-surface по `run_id`

#### AC-33: `evidence <run_id>` отвечает без клона, DB и Git, из одного `collect()` · verification: test
traces: [FR-09]
scenarios: [BEH-36]

Наблюдаемый знак: из пустого каталога с доступом только к двойнику store
`evidence <run_id>` и `--json` содержат run-start, статус
`closed:completed`, последний acknowledged checkpoint, пустые open calls,
attempts с исходами, стоимостью или `unknown` и ссылками на call records,
суммарную стоимость и (при данных store) стоимость хранения; `--json`
валиден по `schemas/evidence-view.schema.json`, значения полей человеческого
и JSON-вывода совпадают; двойник не получил ни одного `put`, каталог остался
пустым.

#### AC-34: Read-surface называет crash/unknown, open call, legacy и недоступный store, не решая за оператора · verification: test
traces: [FR-09, FR-07]
scenarios: [BEH-37]

Наблюдаемый знак: прогон после `kill -9` показан `crash/unknown`; прогон с
open call перечисляет `call_id`, provenance, задачу и время call-start;
legacy отвечает «нет evidence-контракта» с перечнем недостающего; store с
ошибкой даёт exit 2 с причиной, не пустой отчёт; следующий шаг показан как
рекомендация restore-а с пометкой «не доказуемо: <причина>» там, где данных
нет; ответ ограничен одним `run_id`.

#### AC-35: Локальный `status` показывает `run_id` последнего прогона namespace-а · verification: test
traces: [FR-09, FR-01]
scenarios: [BEH-38]

Наблюдаемый знак: после прогонов A и B человеческий `status` содержит строку
с `run_id` B, `status --json` — ключ `run_id` = B и `pipeline_id`, если был;
существующие ключи сохранены, `schemas/status.schema.json` дополнена
аддитивно; для namespace без прогонов по контракту поле отсутствует либо
`null`.

### J. Сквозные гарантии

#### AC-36: Ни одна подтверждённая mutation не теряется ни на одной границе (fault-injection) · verification: test
traces: [NFR-01, FR-02, FR-03, FR-08]
scenarios: [BEH-39]

Наблюдаемый знак: `slow`-тест, параметризованный по пяти границам (после
run-start; после call-start до spawn; после spawn до результата; после
результата до attempt; после attempt до checkpoint) с числом повторений из
NFR-01 требований, суммарно даёт 0 mutation, подтверждённых DB/spool/store и
отсутствующих после рестарта, 0 `spawn` без acknowledged call-start, 0
автоматических повторов open call (каждый виден как `needs-human`);
инъекция — двойники и `os._exit` в seam-ах, детерминирована по seed.

#### AC-37: Любой изменённый байт или отсутствующий обязательный файл — fail-closed до `Popen` и claims gate · verification: test
traces: [NFR-04, FR-03, FR-04, FR-05, FR-06, FR-07]
scenarios: [BEH-40]

Наблюдаемый знак: для каждого файла (DB snapshot, manifest, WIP artifact,
spool, каждый call record, attempt export, closure) в режимах «байт изменён»
и «обязательный файл удалён» `restore`, следующий `run` в namespace и
`evidence` отказывают с именем файла и ожидаемым/фактическим digest, exit 2
у всех трёх; restore — до записи в каталог, `run` — до `Popen` и
`claims.check_claims` (двойники подтверждают 0 вызовов) с closure `failed`; изменённый manifest отвергнут по digest-у верхнего
уровня при сходящихся внутренних digests; ни один вариант не заканчивается
успешным restore с предупреждением.

#### AC-38: Retention вне объявленного диапазона отклоняется; удаление оставляет audit-запись без содержимого · verification: test
traces: [NFR-07, FR-03, FR-06, FR-07]
scenarios: [BEH-42]

Наблюдаемый знак: `retention_days` ниже и выше диапазона NFR-07 даёт
`ConfigError` при загрузке и ошибку `validate`, значение в диапазоне принято;
после удаления промежуточных checkpoint-ов 1–2 restore из checkpoint-а 3
проходит, удалённые записи заменены audit-записью с `run_id`, ids, actor,
reason и временем без payload; при отказе store в удалении команда сообщает
отказ, не удаляет локальную копию и не пишет audit-запись об удалении. У
каждого invocation `evidence purge` двойник store получает ровно один
run-start и ровно одну closure, kind которой выводит диспетчер по коду
выхода: `completed` без истёкших объектов и после удаления, `failed` при
отказе store в `delete` и при недоступном store, — и open call прежнего
прогона после неё предъявляется как раньше.

#### AC-39: Ни байта checkpoint/evidence/spool в продуктовом Git после всех E2E · verification: test
traces: [FR-03, FR-04, FR-06]
scenarios: [BEH-43]

Наблюдаемый знак: после каждого E2E `git status --porcelain` продуктового
репо не содержит файлов под `.executor-*` (checkpoint, evidence, spool, WIP
artifact); единственные tracked-изменения — сегодняшние harness-коммиты
(status flips `tasks.md`, коммиты задач) и `tdd_namespace`, записанный
restore-ом; untracked `spec/.gitignore` в проекте, где его не трекают, —
не находка; `git_ops.tracked_state_paths` не находит tracked `.executor-*`
ни в одном проекте.

#### AC-40: Тесты не вызывают платного агента; существующие контракты меняют ожидания только аддитивно · verification: test
traces: [FR-01, FR-02]
scenarios: [BEH-44]

Наблюдаемый знак: `uv run pytest tests/ -q -m "not slow"` и `-m slow` не
поднимают ни одного `PaidBinaryReached` и не требуют сети к реальному store;
`tests/test_json_result_contract.py`, schema-тесты `.executor-state.db`,
`test_state.py`, `test_costs.py` зелёные без изменения ожиданий, кроме
аддитивных `run_id`/`call_id`/`pipeline_id` — любое иное изменённое
ожидание существующего теста является находкой ревью PR; пояс
`PaidBinaryReached` продолжает ловить каждый сайт через seam. Второй путь к
бинарю провайдера снят, а не только обойдён: `asyncio.create_subprocess_exec`
не вызывается ни из одного модуля `src/spec_runner/`, `run_claude_async`
отсутствует в `runner.py` и в `__all__` пакета, CHANGELOG под Unreleased
называет удаление публичного экспорта, а пояс продолжает перечислять
`asyncio.create_subprocess_exec` как перехватываемую точку.

#### AC-41: Acknowledgement call-start и доступность checkpoint-а укладываются в бюджет NFR-02 · verification: metric
traces: [NFR-02, FR-02]
scenarios: [BEH-41]

Источник порогов — NFR-02 требований (`10-requirements.md` §5: p95/p99 ack,
p95 доступности checkpoint-а вне машины); источник измерения — отчёт
`scripts/bench_durability.py` на reference workload (одна задача с review,
fake CLI, локальный адаптер store), приложенный к PR, а не assert CI.
Критерий выполнен, когда измеренные p95/p99 ack и p95 доступности
checkpoint-а не превышают порогов NFR-02, а таймаут ack — конфигурируемый
ключ, превышение которого даёт исход AC-05.

#### AC-42: Валидация restore и полное восстановление укладываются в бюджет NFR-03 · verification: metric
traces: [NFR-03, FR-05]
scenarios: [BEH-41]

Источник порогов — NFR-03 требований (`10-requirements.md` §5: время
валидации + вычисления шага на reference-наборе; время полного
восстановления набора объявленного объёма при объявленной сети); источник
измерения — длительность `test_restore_drill.py::test_validation_under_60s`
из `pytest --durations` в CI-логе PR и отчёт ручного drill-а на большом
наборе, приложенный к условию завершения (M-01). Критерий выполнен, когда обе
измеренные величины ниже порогов NFR-03.

#### AC-43: Документация, схемы, CHANGELOG и статус experimental говорят то же, что код; соседям объявлен контракт · verification: manual
traces: [FR-01, FR-05, FR-09]
scenarios: [BEH-45]

Что наблюдает ревьюер PR: `docs/architecture.md` («Runtime-state inventory
and delivery policy (#478)») описывает контракт checkpoint/evidence/closure
и «operational minimum» для legacy; `schemas/` содержит версионированные
`checkpoint-manifest`, `evidence-record`, `run-closure`, `restore-result`,
`evidence-view`; `docs/state-schema.md` описывает аддитивные столбцы и minor
bump; CHANGELOG под Unreleased называет `experimental` для `restore` до
приёмки FR-01–FR-08 со ссылкой на #480 (после приёмки — запись о снятии);
контракт `run_id`/`pipeline_id` объявлен devtools и Maestro issue с
`slug:` + `from:` (ADR-ECO-006) либо записью в
`../prograph-vault/authored/notes/`, файлы соседей не тронуты; #480 закрыт
ссылкой на PR с evidence, пункт `runtime-state-artifact-export` в `TODO.md`
закрыт; при вырезании FR-09 владельцем в CHANGELOG есть запись, и BEH-36…38
сняты.

#### AC-44: Restore-drill и open-call матрица выполнены в объёме условия завершения · verification: metric
traces: [FR-05, FR-02]
scenarios: [BEH-19, BEH-09]

Источник чисел — §11 требований (`10-requirements.md` «Условие завершения
стадии»: число прогонов restore-drill для M-01, число прогонов open-call
матрицы для M-02, число перенесённых реальных прогонов для M-05); источник
измерения — отчёт drill-а, приложенный к закрывающему PR или к #480, с
перечнем `run_id`, исходов и числом `Popen` при восстановлении. Критерий
выполнен, когда число зелёных прогонов каждого вида не ниже объявленного в
§11 и ни один прогон не повторил оплаченный вызов.

## Инварианты покрытия

Инварианты, которые qa проверяет над этим документом (нарушение любого —
документ не готов к approve):

1. **Каждое Must-требование покрыто ≥ 1 AC.** FR-01 → AC-01, AC-02, AC-03,
   AC-22, AC-35, AC-40, AC-43; FR-02 → AC-04, AC-05, AC-06, AC-07, AC-08,
   AC-09, AC-20, AC-36, AC-40, AC-41, AC-44, AC-45; FR-03 → AC-10, AC-11,
   AC-12, AC-13, AC-29, AC-36, AC-37, AC-38, AC-39, AC-45, AC-46;
   FR-04 → AC-12, AC-14, AC-15,
   AC-16, AC-17, AC-37, AC-39; FR-05 → AC-08, AC-15, AC-17, AC-18, AC-19,
   AC-37, AC-42, AC-43, AC-44, AC-46; FR-06 → AC-07, AC-09, AC-20, AC-21,
   AC-22, AC-23, AC-24, AC-25, AC-37, AC-38, AC-39, AC-45;
   FR-07 → AC-03, AC-05, AC-27,
   AC-28, AC-29, AC-32, AC-34, AC-37, AC-38, AC-46; FR-08 → AC-13, AC-30, AC-31,
   AC-32, AC-36; NFR-01 → AC-05, AC-30, AC-32, AC-36; NFR-04 → AC-12, AC-18,
   AC-37; NFR-05 → AC-25, AC-26; NFR-06 → AC-24; NFR-07 → AC-38.
2. **Should-требования покрыты по усмотрению qa, все три.** FR-09 → AC-28,
   AC-33, AC-34, AC-35, AC-43 (условно, см. порог); NFR-02 → AC-41;
   NFR-03 → AC-42.
3. **Каждый AC трассирует ≥ 1 требование**, идентификаторы — только из
   `10-requirements.md`; новых FR/NFR здесь не вводится.
4. **Каждый `verification: test` несёт `scenarios`** с идентификаторами только
   из `15-behaviour-spec.md`; новых BEH здесь не вводится. Обратно — каждый
   сценарий BEH-01…BEH-48 упомянут хотя бы одним AC: BEH-01/02 → AC-01;
   BEH-03 → AC-02; BEH-04 → AC-03; BEH-05 → AC-04; BEH-06 → AC-05;
   BEH-07 → AC-06; BEH-08 → AC-07; BEH-09 → AC-08, AC-44; BEH-10 → AC-09;
   BEH-11 → AC-08; BEH-12 → AC-10; BEH-13 → AC-11; BEH-14 → AC-12;
   BEH-15 → AC-13; BEH-16 → AC-14; BEH-17 → AC-15; BEH-18 → AC-16;
   BEH-19 → AC-17, AC-44; BEH-20 → AC-18; BEH-21 → AC-19; BEH-22 → AC-20;
   BEH-23 → AC-21; BEH-24 → AC-22; BEH-25 → AC-23; BEH-26 → AC-24;
   BEH-27 → AC-25; BEH-28 → AC-26; BEH-29 → AC-27; BEH-30 → AC-28;
   BEH-31 → AC-29; BEH-32 → AC-27; BEH-33 → AC-30; BEH-34 → AC-31;
   BEH-35 → AC-32; BEH-36 → AC-33; BEH-37 → AC-34; BEH-38 → AC-35;
   BEH-39 → AC-36; BEH-40 → AC-37; BEH-41 → AC-41, AC-42; BEH-42 → AC-38;
   BEH-43 → AC-39; BEH-44 → AC-40; BEH-45 → AC-43; BEH-46 → AC-27;
   BEH-47 → AC-45; BEH-48 → AC-46.
   Сценарии `kind: manual`
   (BEH-41, BEH-45) стоят только за критериями `metric`/`manual`.
5. **Каждый AC называет наблюдаемый знак**, а не пересказывает требование:
   значение id в канале, число вызовов `Popen` и их порядок относительно
   call-start в журнале двойников, число строк на `call_id`, `sequence`
   checkpoint-а у двойника, содержимое manifest-а, SHA-256 файлов в
   восстановленном каталоге, kind/reason/`last_checkpoint_id` closure,
   текст и exit code отказа, `git status --porcelain`, число
   `PaidBinaryReached` — то, что перечислено в «Области поведения»
   behaviour-спеки.
6. **Числа не зашиты в критерии.** AC-41 берёт пороги из NFR-02, AC-42 — из
   NFR-03, AC-44 — из §11 требований, AC-36 — число повторений из NFR-01,
   AC-24 — пределы из NFR-06, AC-38 — диапазон из NFR-07; изменение числа в
   upstream'е не требует правки этого документа.
7. **Метрики требований предъявлены.** M-01/M-05 — AC-14, AC-17, AC-36,
   AC-44; M-02 — AC-08, AC-44; M-03 — AC-04, AC-09, AC-20; M-04 — AC-27,
   AC-28; M-06 — AC-33, AC-34; M-07 — AC-18, AC-25, AC-37, AC-39.
8. **Резолюции design не противоречат критериям.** Q-02 (ack = возврат
   `put` store-адаптера до `Popen`; spool ack-ом не является; режим
   `durability.ack: local` помечен в run-start/closure и читается
   restore/evidence fail-closed как неполный контракт) — AC-04, AC-05 и
   AC-30 сформулированы через факт «запись подтверждена раньше `spawn`» и
   «spool доигрывается в DB», допускающий эту резолюцию; отдельного AC на
   local-режим нет намеренно — его отказная форма та же, что у legacy в
   AC-19 и AC-34. Q-03 (WIP — tar с `git bundle` и байтами dirty/untracked)
   — AC-14 проверяет восстановление через `git`, не формат. Q-05
   (локальный snapshot синхронно, publisher с drain перед call-start и перед
   closure, вторая точка — гейт успешного завершения,
   manifest кладётся последним) — AC-11 считает checkpoint
   полученным по ack двойника, AC-29 фиксирует лишь acknowledged id в
   closure, AC-46 — что успешного завершения без ack не бывает.
   Q-06/Q-08 (место seam-а, движок redaction) — AC-04, AC-25
   проверяют единственность пути статически, где бы он ни жил. Q-07
   (состав policy identity) — AC-18 подменяет `review_policy` как заведомо
   policy-relevant ключ. Q-11/Q-12 (исполнитель retention, источник open
   calls при старте) — AC-38 и AC-08 не называют механизм.
9. **Рабочие имена Q-04 и допущение Q-09** (`restore`/`evidence`/`evidence
   close-call`, `--into`, `--experimental`; запись `tdd_namespace`
   restore-ом с diff-ом) употребляются как в behaviour-спеке; переименование
   владельцем при сохранении семантики exit-кодов и отказов не меняет
   критерии, кроме подстановки имени.

## Порог приёмки

Workstream объявляется delivered, когда одновременно:

- **Все `verification: test`, кроме AC-36 и AC-33…AC-35,** зелёные в CI на
  PR в прогоне `uv run pytest tests/ -q -m "not slow"`: AC-01, AC-02, AC-03,
  AC-04, AC-05, AC-06, AC-07, AC-08, AC-09, AC-10, AC-11, AC-12, AC-13,
  AC-14, AC-15, AC-16, AC-17, AC-18, AC-19, AC-20, AC-21, AC-22, AC-23,
  AC-24, AC-25, AC-26, AC-27, AC-28, AC-29, AC-30, AC-31, AC-32, AC-37,
  AC-38, AC-39, AC-40, AC-45, AC-46.
- **AC-36** (fault-injection, `slow`) пройден хотя бы один раз до снятия
  статуса experimental; ссылка на прогон приложена к PR (условие §11
  требований).
- **AC-33, AC-34, AC-35** (FR-09, Should): либо зелёные в том же CI-прогоне,
  либо FR-09 явно вырезан решением владельца с записью в CHANGELOG — третьего
  состояния («не реализован и не вырезан») порог не допускает; при вырезании
  AC-28 теряет только ветку `evidence`, restore-ветка остаётся обязательной,
  а AC-43 проверяет запись о вырезании.
- **`verification: metric` — по перечислению:** AC-41 (ack и доступность
  checkpoint-а из отчёта бенчмарка не выше порогов NFR-02), AC-42 (валидация
  из CI-лога и полное восстановление из отчёта drill-а не выше порогов
  NFR-03), AC-44 (число прогонов drill-матриц и перенесённых реальных
  прогонов не ниже §11 требований, 0 повторных оплаченных вызовов).
- **`verification: manual` — по перечислению:** AC-43 подтверждён ревью PR
  (терминальный прогон от ai-prosto по правилу репо), #480 закрыт, пункт
  `TODO.md` закрыт, issue соседям заведены.

Снятие статуса experimental с `restore` (CON-01) — отдельное от «delivered»
событие: оно допустимо только когда все критерии, трассирующие FR-01–FR-08,
зелёные, AC-36 прогнан, AC-44 выполнен, и CHANGELOG несёт запись о снятии
(AC-19 фиксирует оба состояния флага как параметр, AC-43 — запись).
**AC-46 входит в этот набор особо** (решение владельца 2026-09-18): пока
подкоманда без платного вызова вправе завершиться успешно до ack своего
checkpoint-а, restore штатно применяет snapshot старше чужого потерянного
решения — и делает это молча. Красный AC-46 держит статус experimental даже
при всех остальных зелёных.

Любой красный критерий из перечисленных, красный обязательный CI-чек или
неприбывшее ревью держат workstream в состоянии «не delivered»; частичная
приёмка (например, identity и closure без write-ahead и restore) не
предусмотрена — G-01 требует, чтобы прерванный прогон можно было продолжить
без повторной оплаты, а это доказывают только AC-04…AC-09 и AC-17…AC-19
вместе.

## Вне объёма

Намеренно не является критерием приёмки:

- **Автоматическое продолжение после restore и `run --resume`** (CON-05,
  Q-04): продолжение — всегда отдельный `run`, инициированный оператором;
  AC-17 проверяет напечатанный следующий шаг, а не его исполнение.
- **Retry open call без человека** (OUT-02): AC-08 требует, напротив, его
  отсутствия; решение «повторять или нет» остаётся за оператором и не
  проверяется.
- **Собственное хранилище, IAM, KMS, retention-service и первый облачный
  адаптер** (OUT-03, CON-08, Q-01): AC-26 проверяет только объявление
  контракта адаптером; расходы на store и облачный адаптер — отдельное
  одобрение владельца, не критерий.
- **Копии pushed-объектов, virtualenv, кэшей, образов ОС** (OUT-04, OUT-05):
  AC-14/AC-15 требуют, напротив, их отсутствия в checkpoint-е.
- **Изменение правил claims / TDD / waiver / remedy / budget / review**
  (OUT-06): AC-17 проверяет лишь, что старый claim после restore виден
  существующему gate, не поведение gate.
- **Миграция legacy-прогонов** (OUT-07 брифа, CON-07): AC-19 и AC-34
  требуют fail-closed диагностики, реконструкция не проверяется.
- **Межпроектные отчёты, тренды, биллинг, список прогонов namespace-а**
  (OUT-08): AC-33/AC-34 ограничены одним `run_id`.
- **Авто-«лечение» повреждённого артефакта** (OUT-09): AC-37 требует его
  отсутствия.
- **Обход retention и legal hold store** (OUT-10): AC-38 проверяет
  диапазон и audit-запись, не механизм удаления у store.
- **Механизмы, резолвленные design** (Q-02, Q-03, Q-05, Q-06, Q-07, Q-08,
  Q-11, Q-12): канал ack, формат WIP tar, publisher и точки drain, модуль
  seam-а, состав `PolicyIdentity`, паттерны redaction, исполнитель
  retention, источник open calls при старте — критерии проверяют
  предъявленный факт, не реализацию; отдельного критерия для
  `durability.ack: local` нет (см. инвариант 8).
- **Латентность сетевого адаптера store как CI-гейт** (Q-10, RK-01): AC-41
  измеряется бенчмарком на локальном адаптере; сетевой адаптер — тот же
  бенчмарк при его появлении, не этот workstream.
- **Правка соседних репозиториев** (devtools, Maestro): только issue/handoff,
  часть AC-43.
