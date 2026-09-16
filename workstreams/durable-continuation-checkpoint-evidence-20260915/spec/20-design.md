---
spec_stage: design
status: draft
owner_role: architects
traces_to:
- requirements
- behaviour-spec
upstream_hashes:
  requirements: e859a9d8130848ad5d1a50071816a8bd828ae9a1
  behaviour-spec: 323e4cbda58853bb1ea378e3c9d35af662395f01
---

# Design — Durable continuation checkpoint и evidence для run/call/attempt (spec-runner#480)

Стадия `design` governance-бандла
`workstreams/durable-continuation-checkpoint-evidence-20260915/`. Даёт
механику тому, что requirements (`10-requirements.md`) и behaviour-spec
(`15-behaviour-spec.md`) — ревизии пинованы в frontmatter `upstream_hashes`
этого узла — намеренно оставили открытым: какой канал вправе подтверждать
call-start, где живёт единый seam платного вызова, как checkpoint снимается и
доставляется, в чём переносится WIP, что входит в policy identity, чем режется
секрет, кто исполняет retention и откуда обычный `run` узнаёт об open call.
Продуктовые решения upstream'а — один `run_id` на invocation (FR-01), «запись
раньше траты» с fail-closed до spawn (FR-02), checkpoint после каждой
continuation-relevant mutation (FR-03), Git-материал и WIP в checkpoint-е
(FR-04), конечный `restore` без платного вызова (FR-05), evidence на каждом
исходе (FR-06), одна closure на run-start (FR-07), spool при отказе DB
(FR-08), read-surface по `run_id` (FR-09) — здесь не пересматриваются; каждое
решение ниже ссылается на них как на границу.

Термины — в значении §3 требований: **`run_id`**, **`pipeline_id`**,
**`call_id`**, **платный subprocess**, **provenance**, **policy identity**,
**call-start** / **call-result** / **open call**, **ack**, **run-start** /
**run-closure**, **continuation-relevant mutation**, **continuation
checkpoint**, **manifest**, **WIP artifact**, **evidence bundle**,
**emergency spool**, **artifact store**, **durable boundary**, **legacy run**,
**restore**. Ссылки на код — на состояние `master` после #521 (`6bd1dc3`).

Четыре вопроса requirements §10 принадлежат product и здесь не резолвятся;
дизайн принимает их рабочие допущения и изолирует каждое в одном месте, чтобы
иное решение владельца стоило одной правки: Q-01 — абстракция store с одним
контрактом и **первым адаптером на управляемом томе** (§ Механика 1);
Q-04 — рабочие имена `restore` / `evidence` / `evidence close-call` и флаги
`--into` / `--experimental` (§ Механика 7, argparse-таблица одна); Q-09 —
`restore` записывает восстановленный effective namespace в config нового
каталога и печатает diff (§ Механика 7.3, одна ветка); Q-10 — NFR-02/NFR-03
как бенчмарк и ручной drill (§ Рамки red-дизайна).

## Резолюции открытых вопросов

Входной набор архитектурных вопросов requirements §10: Q-02, Q-03, Q-05,
Q-06, Q-07, Q-08, Q-11, Q-12. Все `blocking: false`; все резолвятся здесь.

#### Q-02 · owner_role: architects · resolution: resolved

**Acknowledgement call-start приходит от store-адаптера до `Popen`; локальный
spool ack-ом не является. Spool-only режим существует, но как явно
включённый и помеченный в run-start и closure, а прогон в этом режиме restore
читает как неполный контракт.**

Критерий требования — какой канал проходит NFR-01 при потере машины. Spool
лежит на той же машине, что и процесс; после её потери он отвечает ровно на
те вопросы, на которые отвечает потерянная DB — ни на один. Инвариант 5 и
M-02 («open call → `needs-human` до повтора») требуют, чтобы факт «вызов мог
стартовать» пережил машину, а это умеет только запись, которую до `Popen`
подтвердил кто-то вне неё. Поэтому ack = возврат `put` store-адаптера
(§ Механика 1.2), синхронно, с конфигурируемым таймаутом
(`durability.ack_timeout_seconds`, дефолт 3 s = p99 NFR-02); отказ или
таймаут → `Refusal(kind="instrument")` до spawn (BEH-06). Латентность RK-01
для первого адаптера (управляемый том, Q-01) — `open(O_EXCL)` + `fsync` +
`rename`, то есть бюджет p95 ≤ 1 s выполняется с запасом на порядки; для
сетевого адаптера бюджет измеряет бенчмарк BEH-41, а не CI.

Spool при этом **не лишний**: он — write-ahead канал для DB (FR-08), и
call-start пишется в него, когда DB отказала (§ Механика 5). Но роль у него
одна — доиграться в DB, — и ни один читатель не принимает по нему решений
(RK-03). Режим `durability.ack: local` (по умолчанию `store`) допускает
работу без store — для проектов, которым continuation вне машины не нужен, —
и тогда «ack» даёт локальный append-log с `fsync`; run-start и closure несут
`ack_channel: local`, а `restore`/`evidence` для такого `run_id` отвечают
fail-closed с перечнем «нет внешнего call-start», как для legacy (CON-07):
восстановимость не обещана и не имитируется (RK-04).

#### Q-03 · owner_role: architects · resolution: resolved

**WIP artifact — один tar внутри checkpoint-а: `git bundle` для
неопубликованных commits и stash-commit-ов, tar байтов dirty/untracked work по
`git_ops.uncommitted_work_paths`, и индекс с именами ref, SHA и меткой stash.
Access-controlled ref на forge не используется.**

FR-04 требует байт-идентичного восстановления, тех же digests/redaction/
retention, что у остального checkpoint-а, и ни байта в продуктовый Git
(CON-04). Ref на forge нарушает последнее по построению — это push в
продуктовый remote, пусть и под чужим namespace; требует прав push у
runner-а, которых у CI-читателя может не быть; зависит от forge-специфичной
ACL (OUT-03 отдаёт IAM store-у, не forge-у); и не переносит dirty/untracked
байты вовсе. `git bundle` — переносимый файл, который `git bundle verify`
проверяет без сети, `git fetch <bundle>` восстанавливает с теми же SHA (объекты
идентичны по построению — SHA и есть содержимое), и который store шифрует в
покое вместе со всем checkpoint-ом (NFR-05, OUT-03). Rescue stash — это
commit; bundle несёт его под синтетическим ref
`refs/spec-runner/wip/stash/<n>`, а restore воссоздаёт запись `git stash
store -m <label> <sha>` — та же метка `spec-runner rescue: <task> …`
(`hooks.py:132`), тот же SHA (BEH-16). Dirty/untracked — tar с режимами файлов
и per-file SHA-256 в индексе; mtime не переносится и не сравнивается. Состав и
границы — § Механика 4.

#### Q-05 · owner_role: architects · resolution: resolved

**Snapshot — синхронно в точке mutation; публикация — одним упорядоченным
publisher-ом с двумя точками drain: перед каждым call-start и перед closure.
«Доступен вне машины» = store подтвердил `put` manifest-а, который кладётся
последним.**

Синхронная публикация каждого checkpoint-а на сетевой адаптер поставила бы
NFR-02 (60 s) на critical path каждой из ~десяти mutation одной TDD-задачи;
полностью асинхронная — оставила бы closure ссылаться на неподтверждённый id и
превратила бы «durable boundary» в пожелание. Середина, которую выбирает
дизайн: локальный snapshot (`sqlite3.Connection.backup()` — § Механика 3.2)
и manifest пишутся синхронно, до возврата из mutation, под `.executor-*`
пояс; их доставку выполняет один процессный publisher в порядке `sequence`.
Две точки, где процесс ждёт его с таймаутом
(`durability.checkpoint_ack_timeout_seconds`, дефолт 60 s = NFR-02): (а)
перед записью call-start — так между потраченным долларом и последним
acknowledged checkpoint-ом никогда нет неподтверждённой mutation, и durable
boundary совпадает с границей «до платного вызова»; (б) перед closure — так
closure всегда несёт acknowledged id (BEH-31). Таймаут в (а) — отказ до
spawn (`instrument`), в (б) — closure kind `failed`, exit 2, с id
предыдущего acknowledged. Между точками drain процесс делает то, что делал
(тесты, lint, git, сборка prompt-а) — это и есть выигрыш от асинхронности.
Порядок файлов внутри одного checkpoint-а: DB snapshot, spool, WIP, затем
manifest — checkpoint без acknowledged manifest-а для читателей не существует
(NFR-04: неполный набор = отказ).

#### Q-06 · owner_role: architects · resolution: resolved

**Seam — новый модуль `src/spec_runner/paid_call.py` с одной функцией
исполнения платного вызова и одной функцией spawn; все существующие сайты
становятся её вызывающими, `cli_plan.py` переводится с `build_cli_command` на
`build_cli_invocation` + тот же seam на всех трёх своих сайтах, `doctor`
покрыт через `execute_task`, а второй, асинхронный spawn провайдера —
`runner.run_claude_async` — удаляется.**

Ни `runner.py`, ни `prompts_log` не подходят на роль seam-а, но по разным
причинам, и первую нужно назвать точно. `runner.py` — сборщик argv и
разборщик результата, и синхронный spawn действительно живёт на каждом сайте
отдельно (`execution.py:524`, `tdd.py:1935`, `review.py:466`,
`review_pr.py:687`/`:878`, `cli_plan.py:170`/`:660`/`:797`). Но утверждение
«`runner.py` процесс не запускает» ложно: `run_claude_async`
(`runner.py:557-587`) вызывает `asyncio.create_subprocess_exec` с argv
провайдера и экспортирован из пакета (`__init__.py:58`, `__all__:156`) —
второй, публичный путь к бинарю, о котором знает даже пояс conftest
(«runner.py's streaming path», `tests/conftest.py:125`). Сделать его
вызывающим seam-а нельзя дёшево: seam синхронен и fail-closed, а этот путь
асинхронен и стримит stdout в `EventBus`. Решение — **удалить** его: в дереве
у него нет ни одного продуктового вызывающего (единственные ссылки —
`tests/test_runner.py` и `tests/test_events.py`, плюс диаграммы
`docs/architecture.md` и строка `CLAUDE.md`), так что это снятие мёртвого
публичного экспорта, а не изъятие работающей возможности; CHANGELOG под
Unreleased называет удаление `spec_runner.run_claude_async` из публичного API,
осиротевшие тесты снимаются вместе с ним, `docs/architecture.md` и `CLAUDE.md`
правятся в тех же PR. Если стриминг понадобится TUI снова, он возвращается
асинхронным вариантом **внутри** `paid_call`, а не вторым spawn-ом рядом с
ним. `prompts_log` не подходит иначе: это писатель артефакта, у которого
«никогда не проваливать задачу» — принципиальная позиция (#282),
противоположная fail-closed call-start. Нужен модуль, у которого есть право
отказать до spawn и который знает про store, DB и spool разом. Он один и — по
образцу `_belt_never_executes_a_paid_binary` — доказуем поясом:
`paid_call._spawn` становится **единственной** функцией, которой разрешено
передать argv провайдера в `subprocess`, и после удаления `run_claude_async`
это утверждение истинно по построению дерева, а не по умолчанию; тест BEH-05
подменяет `_spawn` двойником и гоняет матрицу сайтов с настоящим именем
`claude` в `claude_command`.

Рецепт BEH-05 исполним только вместе с одной правкой harness-а, и она —
часть этого решения, а не деталь задачи: autouse-гвард
`_no_real_agent_calls` переключается на **одно** имя — `paid_call._spawn`, —
а два его патча швов снимаются. Сегодня они поднимают отказ на внешнем шве
раньше, чем управление дошло бы до `_spawn`: `_refuse_tdd` — по
`config.claude_command` (`tests/conftest.py:296-299`), `_refuse_execution` —
по любому имени в argv (`:316-323`), оба ставятся autouse
(`:326-327`), — то есть ровно на той матрице, которую BEH-05 обязан
прогнать с настоящим именем. Перенос гвард не ослабляет, а расширяет:
сегодня он закрывает по имени **два** шва из пяти, а review, `plan` и
`review-pr` держит только пояс уровня процесса (`conftest.py:121-128`
называет это прямо); после перевода всех сайтов на seam одно имя закрывает
все пять. Цена названа заранее, и вся она — в той же задаче: (1) гвард
начинает ключеваться по argv, как уже делает `_refuse_execution`
(`:305-309`: «решает то, что реально запустится, а не значение config»), и
конфигурация «`claude_command` платный, `build_cli_invocation` подменён на
безобидный бинарь» перестаёт быть отказом; (2) `tests/test_harness_guards.py`,
который пинует гвард на двух швах, переписывается на `_spawn` **тем же
коммитом**, иначе между снятием патчей и новым тестом остаётся окно без
гварда; (3) сообщение отказа называет сайт по `provenance` из `PaidCall`,
чтобы читаемость «какой шов» не потерялась вместе с именем шва. Двойник
`_spawn` самого BEH-05 заменяет собой патч гварда — это документированное
свойство гварда, а не обход («тест, подменивший шов сам, этот патч не
видит», `conftest.py:290-292`); последней линией под ним остаётся пояс
`PaidBinaryReached` (`tests/conftest.py:118`), который ловит любой обходной
путь к бинарю и красит тест. Статический
тест BEH-44 добавляет вторую половину: `asyncio.create_subprocess_exec` не
вызывается ни из одного модуля `src/spec_runner/`. Существующие
имена `execution._run_agent_process` и `tdd._run_agent` **сохраняются** как
вызывающие seam-а: их патчат десятки тестов, и патч на этом уровне
по-прежнему означает «вызова не было» — что тестам и нужно. Автоматический
отказ при настоящем имени переезжает с них на `_spawn` (выше): шов остаётся
точкой подмены, но перестаёт быть точкой отказа. Механика — § Механика 2.

#### Q-07 · owner_role: architects · resolution: resolved

**Policy identity = `contract_version` + `config_hash` над `gates.POLICY_KEYS`
(множество не расширяется) + effective TDD namespace с `namespace_source` +
workstream (`spec_prefix` / `change_id`). Budget ceiling, CLI и модель
провайдера, версия spec-runner — записываются как факты, но не сравниваются.**

Критерий требования: отказать на расхождении, которое меняет допустимость
следующего шага, и не отказывать на косметике. `POLICY_KEYS`
(`gates.py:107`) уже отвечает ровно на этот вопрос для gate-вердиктов —
`review_policy`, `execution_mode`, `gate_recovery_attempts`, `tdd_runner` —
и расширять его здесь нельзя: его второй смысл — staleness сохранённых
вердиктов, и лишний ключ в нём заставил бы переоценивать gates на
несвязанной правке. Namespace — вне `config_hash` по той же причине, что и в
manifest-е (RK-06): сравнивается не способ получения, а значение, и отказ
называет оба значения и оба источника (BEH-20 (5)). Budget ceiling — не
identity: потолок — это **state** (`budget_authorizations` едут в snapshot-е),
а config-значение сравнивать бессмысленно — guard #213 пересчитает его перед
следующим вызовом, и ниже потолок на новой машине даст честный
отказ по бюджету, а не молчаливый повтор. CLI/модель — косметика по критерию:
задачу законно доигрывают другим агентом, и ни claims, ни red, ни waiver от
этого не меняются; они пишутся в run-start/manifest как факты для аудитора
(`evidence` их показывает) и в `costs`. Версия spec-runner — так же факт;
совместимость решает `contract_version` (первая проверка restore).

#### Q-08 · owner_role: architects · resolution: resolved

**Redactor — один модуль с двумя слоями: точный denylist значений из
окружения процесса и библиотека паттернов известной формы. Энтропийная
эвристика не применяется. Корпус — синтетические значения известной формы с
намеренно невалидной контрольной суммой, генерируемые скриптом фикстуры и
закоммиченные рядом с ним.**

Энтропия отвергнута по одной причине: наши артефакты **состоят** из
высокоэнтропийных строк — blob SHA claims, SHA commits, `call_id`,
`checkpoint_id`, digests manifest-а — и это join keys, без которых bundle
бесполезен аудитору (M-06). Эвристика либо режет их, либо настроена так, что
не режет ничего. Два оставшихся слоя закрывают корпус NFR-05 целиком:
(1) denylist — значения переменных окружения, чьё имя содержит `TOKEN`, `KEY`,
`SECRET`, `PASSWORD`, `CREDENTIAL` (тот же словарь, что `obs._DEFAULT_REDACT_KEYS`
плюс `ORCHESTRA_REDACT_KEYS`, одна константа для обоих), длиной ≥ 8, — это
именно те секреты, которые агент реально видит через `agent_env()`;
(2) паттерны — ключи провайдеров (`sk-…`, `sk-ant-…`), токены forge
(`ghp_`/`gho_`/`github_pat_`/`glpat-`), AWS (`AKIA…` + secret рядом),
PEM-блоки, JWT, строки `NAME=value` для имён из (1) в `.env`-форме. Замена —
`[REDACTED:<kind>:<sha256(value)[:8]>]`: один секрет → один placeholder,
аудитор видит «тот же секрет в prompt-е и в result-е», не видя значения.
`full_sha256`/`full_size` берутся **до** redaction (BEH-26/27). Единственный
путь публикации — `evidence.Publisher.publish(record)`, который прогоняет
redactor по каждому текстовому полю и bounded-копии; адаптеры store
инстанцируются только внутри него (BEH-27, статический пояс: `store.put`
недостижим из другого модуля — § Механика 1.3). Корпус
(`tests/fixtures/secrets-corpus/`): `generate.py` + `corpus.json` из 100
значений; формы валидны для паттернов, контрольные суммы (там, где формат её
имеет — GitHub-токены) намеренно неверны, так что push-protection forge их не
считает живыми, а наш redactor, который контрольную сумму не проверяет, —
считает. Это и есть ответ «как корпус не становится утечкой».

#### Q-11 · owner_role: architects · resolution: resolved

**Политику retention вычисляет spec-runner (`retention.py`), удаление
исполняет адаптер через свой `delete`, audit-запись удаления — immutable
запись в store под тем же `run_id`; компл. `AuditLogger` получает её копию,
когда включён. Lifecycle store по метаданным — допустимый ускоритель, не
единственный исполнитель.**

Первый адаптер (управляемый том, Q-01) lifecycle-политик не имеет вовсе;
дизайн, который отдал бы retention store-у, для него означал бы «retention
нет». Один код-путь для обоих случаев: каждый `put` несёт метаданные
(`run_id`, `kind`, `closure_at`, `retention_until`), которые cloud-адаптер
вправе отдать своему lifecycle; `spec-runner evidence purge <run_id>
--reason …` (Q-04 подтверждает имя) считает по тем же метаданным, что
истекло, вызывает `delete`, и **только после** успешного `delete` пишет
запись `deletions/<ts>.json` (ids, actor, reason, время — без payload) — а
отказ store (legal hold, OUT-10) оставляет всё как есть, включая локальную
копию (BEH-42). Запись — в store, а не только в `AuditLogger`, потому что
`AuditLogger` выключен по умолчанию и машинно-локален; аудитор без каталога
должен видеть, что удалено, через `evidence <run_id>` (FR-09). Промежуточные
checkpoint-ы удаляемы: при публикации следующего manifest `supersedes` уже
записан, а retention считает «удаляем» только те, у которых есть acknowledged
преемник (RK-05).

#### Q-12 · owner_role: architects · resolution: resolved

**Обычный `run` читает open calls из локальной DB — строка `agent_calls` со
статусом `open`; строка пишется до store-ack и закрывается после call-result.
DB здесь — индекс, а не второй домен: сама по себе она может сказать только
«открыт», сказать «закрыт» без store она не вправе. Наличие индекса при этом
не предполагается: когда его нет вовсе — свежая DB после `reset`, удаления
файла, клона или переезда на другую машину, — `run` спрашивает store по
индексу workstream-а и восстанавливает `open`-строки оттуда.**

Второй домен (RK-03) возникает, когда два хранилища могут дать **разные
решения**. Здесь решение одно и направлено в одну сторону: строка `open` в DB
— повод спросить store; ответ store — истина. Процедура на старте `run`
(после run-start и гардов старта, до выбора задачи, § Механика 2.4) имеет две
ветки, и различает их одно наблюдение — объявляет ли DB себя индексом
**этого** workstream-а: meta `continuation_index` со значением `local`,
которую `RunContext.start()` пишет рядом с `last_run_id` (§ Механика 6.1).
`local` означает ровно одно проверяемое утверждение: каждый прогон этого
workstream-а, начатый с момента появления этой DB, записан в неё же.
Свежая DB маркера не несёт вовсе, восстановленная `restore`-ом несёт
`restored` (§ 7.3) — и то и другое ведёт в ветку (2).

**(1) DB объявляет себя индексом** (`continuation_index: local`) — обычный
случай, индекс на месте. Для каждой
`open`-строки namespace-а — targeted `get` двух ключей в store (call-start,
call-result), не листинг: (а) есть call-result → строка закрывается им
(процесс умер между `put` результата и записью в DB), задача свободна;
(б) есть call-start, нет call-result → open call, задача `needs-human`
(BEH-09); (в) нет call-start → процесс умер между строкой DB и `put` — spawn
по построению не было, строка закрывается outcome `not_started`, задача
свободна. Сеть — только при наличии `open`-строк, то есть в норме на старте
её нет вовсе.

**(2) Маркера нет или он `restored`** — локальный индекс не покрывает
workstream целиком: `spec-runner reset` (`cli_info.py:571` —
`config.state_file.unlink()` без аудита и без `--reason`), ручное удаление
файла, свежий клон, новая машина, а также DB, приехавшая из snapshot-а
(§ 7.3: `restore` ставит `restored` именно ради этой ветки — иначе первый
`run` после restore пошёл бы по (1) со stale `last_run_id` и прошёл бы мимо
open call более позднего прогона). Пустой
`agent_calls` здесь не доказывает ничего, и ветка (1) прошла бы мимо open
call молча — ровно тот тихий повтор, который запрещают FR-02 и M-02. Поэтому:
один `list` индекса workstream-а (§ Механика 1.3, префикс
`workstreams/<workstream_key>/runs/`); прогоны, у которых в индексе нет
парного маркера `.closed`, разбираются по своим `calls/`-ключам, и каждый
call-start без call-result становится восстановленной `open`-строкой свежей
DB (`task_id`, attempt, `call_id`, provenance, `run_id` — из call-start),
после чего работает ветка (б). Пустой индекс — workstream без истории,
прогон идёт как обычно. Недоступный store на этом пути → `Refusal(kind=
"instrument")`, exit 2: доказать отсутствие open call нечем, а платный вызов
при недоступном store всё равно не стартует (Q-02).

`reset` при этом **не** становится платящей подкомандой и evidence не пишет.
Классифицировать его как «удаление continuation-индекса, требующее аудита»
значило бы поставить гвард на одной команде: `rm`, свежий клон и другая
машина — сценарий, ради которого NFR-01 и существует, — остались бы дырой
того же размера. Инвариант держит ветка (2): после любого способа потерять
DB open call предъявляется из store и закрывается только аудируемой дверью
`evidence close-call --reason` (§ Механика 2.5). Цена — один `list` на первый
`run` в каталоге без истории прогонов.

**Чего ветка (1) не видит, и почему это названо, а не умолчано.** Маркер
`local` — утверждение про одну DB в одном рабочем каталоге. Оно перестаёт
быть истинным в трёх случаях, и каждый из них уводит в ветку (2): DB создана
заново, DB приехала из snapshot-а, DB отсутствует. Остаётся один случай,
который ветка (1) закрыть не может: два одновременно живых рабочих каталога
с одним объявленным `tdd_namespace` и одним `workstream_key` — например,
`restore` выполнен, пока исходная машина жива. Namespace в дереве привязан к
абсолютному пути (`tdd.resolve_namespace`, чартер), поэтому «в том же
namespace» FR-02 на эту конфигурацию по букве не распространяется; закрыть
её означало бы листать индекс на каждом старте и держать над ним lease — ни
того, ни другого этот бандл не вводит. Конфигурация названа ограничением в
FR-02 и отдельной строкой таблицы § 2.6, а не считается покрытой.

`restore` локальную DB не читает — только store (BEH-09: «в новом процессе и
новом каталоге»), потому что snapshot мог быть снят раньше строки; но свой
вопрос про open calls он задаёт индексу workstream-а, а не только ключам
восстанавливаемого `run_id` (§ 7.2, проверка (6)). Если DB отказала
на записи `open`-строки — она уходит в spool (FR-08) и доигрывается на старте
**до** этой процедуры.

## Механика

### 1. Store-контракт, адаптеры, publisher (FR-06, NFR-04, NFR-05, NFR-07)

**1.1 Модуль и контракт.** Новый модуль `src/spec_runner/artifact_store.py`:
протокол `ArtifactStore` с `put(key, data, *, metadata) → Ack`
(`if_none_match` всегда: существующий ключ → `AlreadyExists`, это и есть
immutability BEH-25), `get(key) → bytes | None`, `list(prefix) → keys`,
`delete(key)`, и декларация `StoreCapabilities(tls, encryption_at_rest,
immutable_put, lifecycle)`. Загрузчик config (`config.py`,
`load_config_from_yaml`) читает блок `durability:` (`store: {adapter,
options…}`, `ack`, `ack_timeout_seconds`, `checkpoint_ack_timeout_seconds`,
`retention_days`) в поля `ExecutorConfig` (и тем самым в
`KNOWN_EXECUTOR_KEYS`), и **на загрузке** отказывает `ConfigError`, если
адаптер объявил `tls: false`, не объявил `encryption_at_rest` или
`immutable_put` (BEH-28), или `retention_days` вне 7–365 (BEH-42);
`validate.py` повторяет те же проверки в своём отчёте.

**1.2 Первый адаптер — управляемый том** (рабочее допущение Q-01):
`LocalVolumeStore(root)`. `put` = запись во временный файл рядом,
`fsync`, `os.link`/`rename` с `O_EXCL`-семантикой — существующий ключ даёт
`AlreadyExists`; `get`/`list`/`delete` — файловая система. Объявляет `tls:
n/a` (локальный путь), `encryption_at_rest` — по декларации оператора в
options (spec-runner проверяет только объявление, OUT-03), `lifecycle: none`.
Второй адаптер (объектное хранилище / CI artifact service) — отдельное
одобрение владельца (CON-08), тот же протокол.

**1.3 Ключи.** Одна функция `keys.py`-уровня в том же модуле:
`runs/<run_id>/run-start.json`; `runs/<run_id>/calls/<call_id>/start.json` и
`…/result.json`; `runs/<run_id>/checkpoints/<seq:06d>-<checkpoint_id>/
{state.db, spool.jsonl, wip.tar, manifest.json}`;
`runs/<run_id>/attempts/<task_id>-<n>.jsonl`; `runs/<run_id>/task-history.log`
и `runs/<run_id>/audit-log.jsonl` (§ 6.5, обе — когда источник есть);
`runs/<run_id>/closure.json`; `runs/<run_id>/deletions/<ts>.json`. Каждый ключ
пишется один раз (`AlreadyExists` = отказ, первая запись неизменна — BEH-10,
BEH-25, BEH-31); «исправление» — новый ключ с полем `supersedes`.

Рядом с ними — **индекс workstream-а**, единственное, что не адресуется
`run_id`-ом: `workstreams/<workstream_key>/runs/<started_at>-<run_id>.json`
кладётся вместе с run-start, а `…/<started_at>-<run_id>.closed` — вместе с
closure; обе записи одноразовы, как всё в § 1.3. У него два читателя, и обоим он
нужен по одной причине: решение про open calls обязано быть namespace-wide
(FR-02), а `run_id` такого масштаба не имеет. Первый — ветка (2) процедуры
Q-12, где локальной DB нет и спросить store больше не по чему. Второй —
проверка (6) `restore` (§ 7.2), где восстанавливаемый `run_id` может
оказаться не последним прогоном своего workstream-а. `evidence <run_id>`
индекс **не** читает: он ничего не решает, его ключ по-прежнему `run_id`
(FR-09). Полный перечень путей, читающих или решающих open calls, — § 2.6. `workstream_key` — SHA-256 от repository
identity (§ 4.2) и `spec_prefix`/`change_id`: путь в него не входит
намеренно, потому что `tdd.resolve_namespace` хеширует **абсолютный путь**
(charter, «Namespace привязан к абсолютному пути») и на другой машине дал бы
другое имя, а найти надо тот же workstream именно после переезда. Маркер
`.closed` — указатель, а не решение: его отсутствие заставляет процедуру
прочитать `calls/` прогона (лишняя работа), его наличие — пропустить прогон,
а истиной остаётся пара call-start/call-result. Потеря маркера поэтому
безопасна в ту сторону, в которую ошибаться можно; второй домен (RK-03) из
него не возникает.

**1.4 Publisher и redactor.** `src/spec_runner/evidence.py`: `Publisher`
— единственный владелец экземпляра `ArtifactStore`; `publish(record)`
сериализует dataclass записи, прогоняет `redaction.redact()` по каждому
текстовому полю, считает SHA-256 и кладёт. Статический пояс BEH-27: адаптеры
не экспортируются из `artifact_store` под публичными именами, а
`Publisher` — единственный импортёр `_open_store`; тест подменяет
`ArtifactStore.put` на двойник, поднимающий исключение при вызове не из
`Publisher.publish` (по стеку), и гоняет E2E. `redaction.py` — Q-08.
`bound_evidence(text, limit)` — обёртка над `prompts_log.bound` с параметром
лимита (1 MiB / 4 MiB, NFR-06): head и tail, marker, `full_sha256`,
`full_size` (BEH-26).

### 2. Один seam платного вызова (FR-02, FR-06, FR-01)

**2.1 Модуль.** Новый `src/spec_runner/paid_call.py`:

- `PaidCall` — frozen: `invocation: CliInvocation`, `provenance`,
  `task_id | None`, `attempt: int | None`, `prompt`, `timeout_seconds`,
  `prompt_log: Path | None`.
- `execute(config, state | None, call) → CallOutcome` — весь протокол FR-02
  в одном месте; `CallOutcome` несёт `call_id`, `CompletedProcess`-подобные
  поля (`stdout`, `stderr`, `returncode`), `timed_out`, `parsed: CliResult`,
  `outcome` (словарь call-result: `success` / `failed` / `blocked` /
  `timeout` / `infrastructure_error`, по `runner.classify_agent_answer`,
  BEH-08) и `cost_usd | None`.
- `_spawn(invocation, *, timeout, cwd, env) → CompletedProcess` — единственная
  функция репо, которой разрешено передать argv провайдера в
  `subprocess.run`. Тест BEH-05 подменяет её; conftest-guard
  `_no_real_agent_calls` переключается на это имя и **только** на него —
  два его патча швов (`tdd._run_agent`, `execution._run_agent_process`)
  снимаются тем же коммитом, потому что на настоящем имени они срабатывают
  раньше `_spawn` и делают рецепт BEH-05 неисполнимым (Q-06). Единственность буквальна: асинхронный spawn
  `runner.run_claude_async` удаляется (Q-06), и после этого в
  `src/spec_runner/` нет ни `asyncio.create_subprocess_exec`, ни
  `subprocess.Popen`/`subprocess.run` с argv провайдера вне `_spawn`.

**2.2 Порядок внутри `execute`** — ровно тот, что зафиксирован в FR-02, с
одним уточнением из Q-12 (строка-индекс):

1. `checkpoint.drain(timeout)` — все ранее снятые checkpoint-ы acknowledged
   (Q-05 (а)); таймаут → `Refusal(kind="instrument")`, вызова нет.
2. `CallStart` собирается — **не** чеканится: `call_id` уже создан сайтом и
   пришёл полем `PaidCall` (§ 6.1). `CallStart` = `run_id`, `pipeline_id`,
   `call.call_id`, provenance, policy identity (§ 3.4), `task_id`/attempt,
   `prompt_sha256` (по redacted prompt-у), bounded redacted prompt,
   `timestamp`. Чеканка `call_id` внутри `execute` невозможна по построению
   FR-02: `log_prompt` пишет `call_id` в заголовок prompt-артефакта (BEH-01) и
   стоит на сайте **до** `execute`, так что id, созданный здесь, никогда не
   совпал бы с id в заголовке. `execute` вместо этого **проверяет**, что
   `call.call_id` — валидный UUIDv4 и что тот же id передан `log_prompt`;
   несовпадение — `Refusal(kind="instrument")` до записи call-start.
3. Строка `agent_calls`/`pr_agent_calls` со `status='open'` (§ 2.3) — через
   DB, при отказе — через spool (§ 5); оба отказали → `instrument`, вызова нет.
4. `Publisher.publish(CallStart)` с таймаутом `ack_timeout_seconds`; отказ
   или таймаут → строка закрывается outcome `not_started`, prompt-артефакт
   получает `append_not_started` (#296), возвращается
   `Refusal(kind="instrument")` с reason `call_start_not_acknowledged`
   (BEH-06). Вызова нет.
5. `_spawn`. `TimeoutExpired` — не исключение наружу, а `outcome=timeout`,
   `cost=None`.
6. `parse_cli_result` → `classify_agent_answer` → `CallResult` (`outcome`,
   `cost_usd | None`, `returncode`, bounded redacted result + digests).
7. `Publisher.publish(CallResult)`. Отказ здесь **не** делает вызов open
   (spawn был; деньги потрачены): результат записывается в DB/spool
   (шаг 8), а publisher ставит запись в очередь повторной доставки; drain
   перед следующим call-start и перед closure ждёт и её — так до следующей
   траты и до закрытия прогона result либо в store, либо прогон остановлен
   отказом инструмента.
8. Закрытие строки ledger-а (`record_agent_call` в его сегодняшней
   сигнатуре, дополненной `call_id` — становится шагом «close»), затем
   возврат `CallOutcome` сайту. `record_attempt` и checkpoint — по-прежнему
   дело сайта (FR-02: «→ `record_agent_call` / `record_attempt` → checkpoint»).

Budget guard (#213) и `log_prompt` (#282) остаются **на сайтах, до**
`execute` — так «a refusal is no row» сохраняется буквально (BEH-07): до шага
3 не доходит ничего.

**2.3 Аддитивные столбцы.** `agent_calls`: `run_id TEXT NULL`, `call_id TEXT
NULL`, `status TEXT NULL` (`open` / `closed` / `not_started`), `started_at
TEXT NULL`; `pr_agent_calls` — те же; `attempts`: `run_id TEXT NULL`.
Миграция — `ALTER TABLE … ADD COLUMN` в `_migrate` по образцу #218 stage 2;
старые строки `NULL` (BEH-03). `docs/state-schema.md` и
`schemas/executor-state.schema.json` — minor bump. `--json-result`
(`build_task_json_result`) и `status --json` — аддитивные `run_id`,
`pipeline_id` (BEH-03, BEH-38); golden-фикстуры `tests/fixtures/maestro-interop/`
— только добавление ключей.

**2.4 Сайты.** Каждый строит `CliInvocation` как сегодня и вызывает
`paid_call.execute` вместо `subprocess.run`:

| Сайт | Функция | provenance | Что меняется |
|---|---|---|---|
| RED authoring, RED agent round (#220) | `tdd._run_agent` | `red`, `red:fix` | тело → `execute`; `AgentCall` собирается из `CallOutcome` |
| GREEN | `execution._run_agent_process` | `green` | тело → `execute`; `TimeoutExpired`-ветка сайта читает `timed_out` |
| review, `review:<role>` | `review._run_reviewer` | как сегодня | `_record_call` становится шагом close seam-а; последовательность ролей под бюджетом сохраняется |
| `review-pr` verify / fix | `review_pr.verify_comment`, `run_fix_agent` | `review-pr:verify`, `review-pr:fix` | `_record_pr_call` — шаг close; `CostGuard` остаётся до `execute` |
| `plan --gated` (каждая стадия) | `cli_plan._generate_stage_draft`, `cli_plan.py:170` | `plan:<stage>` | `build_cli_command` → `build_cli_invocation` (+`parse_cli_result` — planning впервые получает стоимость), `task_id=None`; параметр `invoke=subprocess.run` (`:94`) снимается — подмена делается двойником `_spawn`, как у остальных сайтов |
| `plan --full` (каждая стадия) | `cli_plan.py:660` | `plan:<stage>` | то же; `costs` — строка «planning» по образцу `pr_cost_rows` (BEH-24) |
| `plan "<описание>"` (интерактивный цикл, каждый круг) | `cli_plan.cmd_plan`, `cli_plan.py:797` | `plan:interactive` | то же; `cmd = [claude_command, "-p", prompt]` (`:791`) заменяется на `build_cli_invocation`; каждый круг цикла — свой `call_id` и своя пара call-start/call-result |
| `doctor` | через `execute_task` | как у сайта | без правок; run-start/closure — § 6 |

Старт `run`/`retry`/`watch` (после run-start § 6.2 и гардов старта, до
выбора задачи — эта граница одна для всех трёх, и executor lock её не задаёт:
`retry` и `watch` его не берут, `run --force` пропускает): replay spool
(§ 5) → процедура open calls Q-12 → как сегодня. `run --all` пропускает
задачу с open call с причиной, называющей `call_id` и provenance; `run
--task` отказывает exit 1 (BEH-09). Обнаружение живёт в `paid_call.open_calls
(config, state) → list[OpenCall]` и вызывается из `_run_tasks_inner` там же,
где `recover_stale_tasks`. Процедура двуветочная (Q-12): при meta
`continuation_index: local` — targeted `get` по `open`-строкам DB; при её
отсутствии или значении `restored` — `list` индекса workstream-а (§ 1.3) и
восстановление `open`-строк из store, потому что пустой `agent_calls` свежей
или восстановленной DB (после `reset`, удаления файла, клона, переезда на
другую машину, `restore`) не доказывает отсутствия open call. Кто ещё
читает и меняет open calls — § 2.6, одной таблицей.

**2.5 Операторская дверь** — `spec-runner evidence close-call <run_id>
--call <call_id> --reason …` (Q-04): по образцу `remedy.cmd_tdd` — обязательный
`--reason`, actor, `SPEC_RUNNER_AGENT` guardrail, отказ под PID-checked lock;
идемпотентность — по наличию `result.json` в store (проверяется **до**
записи); пишет `CallResult(outcome="resolved_unknown", supersedes=<start
key>)` в store и закрывает строку DB того namespace-а, если DB доступна
(иначе — процедура Q-12 закроет её на следующем старте по store). Платного
вызова не делает (BEH-11), но **платящей подкомандой по критерию FR-01
является**: она меняет continuation-state — задача после неё снова
выбираема. Отсюда три следствия, и все три намеренные: `evidence close-call`
и `evidence purge` входят в `PAYING_SUBCOMMANDS` (§ 6.2) и пишут свою пару
run-start + closure; закрытие строки DB идёт тем же шагом «close»
`record_agent_call`, что и у seam-а, а значит через `after_mutation` (§ 3.1)
и checkpoint — под `run_id`, у которого run-start есть, так что аномалии
«checkpoint под `run_id` без run-start» (§ 6.2) здесь не возникает; read-only
остаётся `evidence <run_id>`, и именно он назван read-only в BEH-04/BEH-13,
а не подкоманда `evidence` целиком.

**2.6 Кто читает и кто меняет open calls — полный перечень путей.** Правило
FR-02 namespace-wide, а `run_id` такого масштаба не имеет, поэтому у каждого
пути должен быть назван механизм, которым правило обеспечено именно
namespace-wide. Таблица — здесь, одним местом; § 7.2, § 6.2, Q-12 и узлы
ниже на неё ссылаются, а не пересказывают.

| Путь | Что он решает про open calls / continuation-state | Чем обеспечено namespace-wide правило |
|---|---|---|
| `run --all` / `run --task` — старт | выбирать ли задачу | процедура Q-12 § 2.4 после run-start и гардов, до выбора задачи |
| `retry <task>` — старт | та же задача | та же процедура, та же точка (`retry` executor lock не берёт) |
| `watch` — старт invocation | задачи всех кругов цикла | та же процедура, один раз на invocation; open calls своих кругов закрывает seam того же процесса |
| `run --force` | то же, что `run` | процедура стоит вне lock-а: `--force` отключает lock, а не её |
| первый `run`/`retry`/`watch` после `restore` | выбирать ли задачу | восстановленная DB несёт `continuation_index: restored` (§ 7.3) ⇒ ветка (2) Q-12: `list` индекса workstream-а, а не `open`-строки snapshot-а |
| `run` после `reset` / удаления файла DB / в свежем клоне / на новой машине | то же | маркера нет ⇒ ветка (2) Q-12 |
| `restore <run_id>` | применять ли snapshot | проверка (6) § 7.2 читает индекс workstream-а: любой прогон того же `workstream_key` с call-start без call-result и любой прогон, начатый позже восстанавливаемого, — отказ `needs-human` |
| `evidence close-call` | закрывает open call | единственная аудируемая дверь (§ 2.5); истина — call-result в store, строка DB лишь следствие; своя пара run-start + closure |
| `evidence purge` | удаляет объекты store | open call не закрывает по построению: удаляются только истёкшие checkpoint-ы с acknowledged преемником (Q-11), call records и run-start живут до своего retention; своя пара run-start + closure и запись `deletions/<ts>.json` |
| `evidence <run_id>` | ничего не решает | read-only: показывает open calls своего `run_id` и не читает индекс (§ 1.3) |
| seam `paid_call.execute` внутри любого прогона | открывает и закрывает call | пара call-start/call-result в store под своим `run_id`; строка DB — индекс, а не второй домен (Q-12) |
| replay spool на старте | доигрывает mutation, не решает про calls | идёт **до** процедуры Q-12 (§ 2.4), чтобы процедура читала уже доигранную DB |
| `plan`, `review-pr`, `doctor` | платят, но задач ledger-а не выбирают | свои calls закрывает seam того же invocation; чужой open call им нечего повторить — задачу они не берут |
| `tdd abandon/repair/resume/release`, `budget authorize` | меняют continuation-state, open calls не читают | mutation ⇒ checkpoint (§ 3.1) и своя пара run-start + closure; open call закрывается только дверью § 2.5 |
| `status`, `costs`, `validate`, `report` | ничего не решают и ничего не меняют | run-start не пишут (BEH-04), store не трогают |
| два одновременно живых каталога с одним `workstream_key` | — | **не покрыто**: ветка (1) видит только свою DB, а lease над индексом бандл не вводит (Q-12, последний абзац); ограничение названо в FR-02 |

### 3. Checkpoint: seam «после mutation», snapshot, manifest (FR-03)

**3.1 Одна функция.** Новый `src/spec_runner/checkpoint.py`:
`after_mutation(config, *, table, task_id=None, conn=None)` — единственная
точка публикации (BEH-13, статический тест по образцу `run_plugin_hooks_for`).
Вызывающие: каждый `record_*` в `state.py` (`record_attempt`,
`record_red_checkpoint`, `record_claim`, `supersede_claims`,
`reinstate_checkpoint_with_claims`, `record_tdd_phase`,
`record_verify_evidence`, `record_gate_verdict`, `record_waiver`,
`record_waiver_applied`, `record_remedy`, `record_budget_authorization`,
`record_agent_call` — шаг close, § 2.2), `claims.release_claims`,
`ReviewPrState` при завершении раунда (`review_pr.py:282`; у него своё
соединение — он передаёт его в `conn`), `bookkeeping.commit_status_flip`
(`conn=None`: checkpointer сам открывает соединение к `state_file` для
backup). `mark_running` и `set_meta` — не continuation-relevant по §3
требований и seam не вызывают; `phase_results` (best-effort, #164) — тоже.

**3.2 Snapshot.** `sqlite3.Connection.backup(target)` с живого соединения в
`<state_dir>/.executor-checkpoints/<seq:06d>-<checkpoint_id>/state.db` —
backup API читает через pager и включает WAL-only страницы (BEH-12: тот же
тест с `shutil.copy` красный). Каталог под `.executor-*` (`git_ops.
RUNTIME_GITIGNORE_ENTRIES` уже покрывает `.executor-*`; `runtime_state_paths`
получает его для stash-rescue/staging — BEH-43). `sequence` — монотонный
счётчик в `executor_meta` под ключом `checkpoint_seq:<run_id>`; в degraded
mode — из spool (§ 5). Локальные копии ротируются: хранятся последние две
(последняя нужна restore-у, предпоследняя — на случай отказа ack последней),
старшие удаляются после ack преемника.

**3.3 Состав checkpoint-а.** `state.db` (snapshot), `spool.jsonl` (копия
активного spool-а, пустой файл при отсутствии — в manifest `spool: none`),
`wip.tar` (§ 4; при отсутствии — `wip: none` в manifest, файла нет),
`manifest.json` последним. Manifest — `schemas/checkpoint-manifest.schema.json`:
`contract_version`, `checkpoint_id`, `sequence`, `supersedes`, `run_id`,
`pipeline_id`, `repository` (`remote_url` нормализованный — без учётных
данных, без `.git`-суффикса, — `root_commit`), `workstream` (`namespace`,
`namespace_source`, `spec_prefix`, `change_id`), `head`, `refs[]` (имя, SHA,
`published: true|false`, `published_ref`), `config_hash`, `policy`
(§ 3.4), `join_keys` (последние `attempt_id`, `call_id`, `closure` — `null`
пока нет), `digests{file: sha256}`, `excluded[]` (lock, `.<prefix>spec.lock`,
`.executor-stop`, `.executor-ready`, worktrees `spec-runner-*`,
`.executor-progress.txt`), `degraded: bool`, `wip`, `spool`,
`manifest_sha256` (digest верхнего уровня над каноническим JSON без этого
поля). Ни одного абсолютного пути и PID (BEH-14): все пути в manifest —
project-relative; тест `grep`-ает `str(project_root)` и `os.getpid()`.

**3.4 Policy identity** (Q-07) — одна dataclass `PolicyIdentity` в
`checkpoint.py`, используемая run-start, call-start и manifest-ом (одна
сериализация): `contract_version`, `config_hash` (`GateContext`-совместимый
хеш над `POLICY_KEYS`), `namespace`, `namespace_source`, `spec_prefix`,
`change_id`. Рядом, **не** в identity: `facts` — `claude_command`,
`review_command`, модели ролей, `spec_runner_version`.

**3.5 Publisher checkpoint-ов** — тот же `Publisher` (§ 1.4), очередь
упорядочена по `sequence`; `drain(timeout)` — две точки (Q-05); manifest
кладётся последним, ack manifest-а = ack checkpoint-а; `last_acknowledged()`
— то, на что ссылается closure. Один поток, одна очередь, никакого пула.

### 4. Git-материал и WIP (FR-04)

**4.1 Сбор** — `src/spec_runner/wip.py`, `collect(config) → WipArtifact |
None`, вызывается из `after_mutation` (3.1), но переупаковывает только при
изменении `git status --porcelain` + HEAD + `refs/stash` с прошлого
checkpoint-а (иначе — тот же файл, тот же digest; сравнение по digest-у
входов, не по mtime). Состав `wip.tar`: `bundle.git` — `git bundle create`
для каждого in-flight ref (task-ветка, integration branch #254): диапазон
`<published-base>..<ref>`, где published-base — merge-base с `origin/<base>`
(если ref целиком опубликован — ref в manifest с `published: true`, в bundle
не входит); stash-commit-ы с меткой `spec-runner rescue:` — по SHA под
`refs/spec-runner/wip/stash/<n>`; `dirty.tar` — файлы по
`git_ops.uncommitted_work_paths` (runtime-state исключён по построению,
BEH-16) с режимами; `index.json` — refs, SHA, stash-метки, per-file SHA-256
dirty/untracked. Байты pushed-объектов не копируются (OUT-04).

**4.2 Repository identity** — `git remote get-url origin` (нормализация:
без credentials, без `.git`, `ssh`/`https` эквиваленты приведены к
`host/owner/repo`) + `git rev-list --max-parents=0 HEAD` (первый по порядку
при нескольких корнях — и все перечислены).

**4.3 Восстановление** (§ 7): `git clone <remote> <into>` (или `--from-clone`
для теста с bare-репо), `git fetch <bundle.git> 'refs/*:refs/*'` после `git
bundle verify`, `git switch <task-branch>`, распаковка `dirty.tar` с проверкой
per-file SHA-256, `git stash store -m <label> <sha>` для каждого stash. Ref,
который manifest называет опубликованным, а forge не отдаёт → `needs-human`
с именем и SHA, до распаковки (BEH-18 (б), OUT-09).

### 5. Emergency spool (FR-08)

`src/spec_runner/spool.py`: `Spool(path)` — `<state_dir>/.executor-spool.jsonl`
(с `spec_prefix`/`change_id` как у `state_file`), append-only, `fsync` на
строку; строка = `{seq, run_id, namespace, task_id, attempt, table, payload,
sha256}` (sha256 — над каноническим JSON остальных полей). `seq` монотонный
внутри файла. **Пишет**: `state._enter_degraded_mode` (`state.py:2352`)
перестаёт быть «уведомить и жить в памяти»: каждый `record_*`, поймавший
`OperationalError`, вызывает `spool.append(table, payload)` и только при
успехе возвращается как записанный; отказ spool-а → `Refusal(kind=
"instrument")` наверх (BEH-35), `state_degraded`-уведомление — как сегодня,
один раз. `_save()` (`state.py:948`) в degraded mode пишет через тот же
путь. **Читает** — только `spool.replay(state)`: на старте `run`/`retry`/
`watch` (после run-start и гардов старта, до Q-12-процедуры и до выбора
задачи), из
`tdd`/`budget`-команд (они пишут authority mutations и обязаны видеть DB
полной) и из `restore`. Replay идемпотентен: `spool_replays` (новая
таблица) хранит `(run_id, seq)` доигранных строк; повреждённый sha256 → отказ
с `seq` и обоими digest-ами, прогон не стартует (BEH-34). После replay файл
ротируется в `.executor-spool.<ts>.jsonl.done` и ссылка на него — в
следующем manifest (`spool_replayed`). Статический тест BEH-34 (единственный
читатель): `Spool.read` вызывается только из `replay` и `checkpoint`
(копирование в checkpoint — чтение байтов, не разбор строк).

### 6. Run identity, run-start, closure (FR-01, FR-07)

**6.1 `RunContext`** — новый `src/spec_runner/run_context.py`: frozen
`run_id` (full UUIDv4, `uuid4()` один раз), `pipeline_id` (из structlog
contextvars после `setup_logging` → `obs.init_logging`, `obs.py:250`),
`subcommand`, `started_at`, `store`/`Publisher`, `policy: PolicyIdentity`.
Создаётся в `cli.main()` там, где сегодня
`bind_contextvars(run_id=uuid4().hex[:8])` (`cli.py:2504`) — эта строка
заменяется на bind полного `run_id` (BEH-02, статический тест). Доступ —
`run_context.current()` (module-level, один на процесс; тесты ставят и
снимают через фикстуру). `AuditLogger` получает `run_id=` из контекста в
`build_audit_logger` (`audit_log.py:195`), собственный `uuid.uuid4()` в
`__init__` удаляется (параметр становится обязательным). OTel-записи несут
`run_id` через contextvars (уже так для `pipeline_id`). `prompts_log.log_prompt`
пишет `run_id`/`call_id` в заголовочную строку `=== <SLUG> PROMPT ===`
(BEH-01, последний And) — `call_id` известен раньше `execute` только если
сайт его чеканит; поэтому `PaidCall` принимает `call_id` от сайта, а
`log_prompt` — параметром; seam проверяет, что ему передан тот же.

**6.2 Run-start** — `RunContext.start()`: пишется в store как
`run-start.json` (`run_id`, `pipeline_id`, subcommand, policy, facts,
repository, `contract_version`, `ack_channel`). Точка вызова — **одна, и она
в диспетчере**: `cli.main()` вызывает `start()` перед вызовом handler-а, если
подкоманда принадлежит множеству `PAYING_SUBCOMMANDS` (`run`, `retry`,
`watch`, `plan`, `review-pr`, `doctor`, `tdd abandon/repair/resume/release`,
`budget authorize`, `restore`, `evidence close-call`, `evidence purge`);
read-only команды его не пишут (BEH-04). Две последние платного вызова не
делают, но входят по второй половине критерия FR-01 — «меняют
continuation-state»: `close-call` возвращает задачу в выбираемые (§ 2.5),
`purge` удаляет объекты store и пишет `deletions/<ts>.json` (Q-11). Тот же
критерий уже привёл сюда `tdd abandon/…` и `budget authorize`, которые тоже
не платят; оставить эти две снаружи значило бы, что операторское решение,
разблокировавшее задачу, — единственное в перечне без своей closure.
Read-only остаётся `evidence <run_id>`, а не подкоманда `evidence` целиком.
`watch` — один `start()` на invocation, потому что invocation один, а не
потому, что lock берётся один раз. Позиций в перечне одиннадцать: `run` и
десять вне его — восемь самостоятельных команд (`retry`, `watch`, `plan`,
`review-pr`, `doctor`, `tdd abandon/repair/resume/release`, `budget
authorize`, `restore`) и две формы `evidence`. Исход каждой закрывает одно
правило § 6.3, одинаковое для всех одиннадцати.

Executor lock носителем run-start быть не может, и это не деталь реализации, а
свойство дерева: `_acquire_run_lock` (`cli.py:181`) вызывается **только** из
`cmd_run` и **только** без `--force` (`cli.py:213-217`: при `--force`
`lock = None`); `cmd_retry` (`cli.py:1467`) и `cmd_watch` (`cli.py:1541`)
executor lock не берут вовсе. Точка в lock-е оставила бы три платящих пути —
`retry`, `watch` и `run --force` — без run-start, а значит (по § 6.3, «если
`start()` был вызван») и без closure: их call-start-ы публиковались бы под
`run_id`, для которого нет `run-start.json`, и `evidence`/`restore` (§ 7.2,
7.4) читали бы такой прогон как legacy «нет evidence-контракта». `run --force`
при этом остаётся обычным платящим прогоном контракта: `--force` отключает
проверку lock-а, а не участие в evidence.

Отказ lock-а («lock занят», `sys.exit(1)`-ветка `_acquire_run_lock`)
наступает **после** run-start и потому даёт обычную пару run-start + closure,
а не одинокий run-start: `sys.exit(1)` доходит до `finally` диспетчера, и
closure пишет общая точка § 6.3 — kind по коду выхода, никакой правки самой
ветки. Так же устроены три других гарда старта — governance-гейт
(`_enforce_spec_governance`, `cli.py:285-299`), dirty-spec
(`_enforce_clean_spec`, `:729`) и tracked-state DB
(`_enforce_untracked_state`, #273, `:779`): все три стоят внутри handler-а,
подряд в начале `_run_tasks_inner` (`:828-830`) и в `cmd_retry` (`:1471`) /
`cmd_watch` (`:1547`), то есть после run-start. Ни один из четырёх не
правится этим дизайном.

**6.3 Closure** — `RunContext.close(exit_code)` — одна точка записи, и она
там же, где run-start: `main()` оборачивает вызов handler-а в `try/except
SystemExit/except BaseException/finally`; на выходе, если `start()` был
вызван и closure ещё нет: `drain(timeout)` publisher-а → closure с
`last_checkpoint_id = last_acknowledged()`. Таймаут drain — единственный
случай, когда `close()` перекрывает выведенный kind: она пишет `failed` с
exit 2 и reason, называющим неподтверждённый checkpoint (BEH-31). Это её
собственное наблюдение о неполноте записи, а не сообщение сайта. Сайты
выхода подкоманд контексту не сообщают ничего: ни `note_stop`, ни обязанности
«поставить причину вот здесь» в дизайне больше нет. Kind выводит одна функция
`closure.derive(outcome)` из **двух** фактов, которые диспетчер наблюдает сам,
одинаково для каждой подкоманды из `PAYING_SUBCOMMANDS`.

**Факт первый — как handler ушёл:** необработанное исключение (не
`SystemExit`), сигнал или `KeyboardInterrupt`, либо код выхода
(`SystemExit.code` или код, возвращённый handler-ом). Сигнал диспетчер
наблюдает не по способу ухода, а по флагу: `main()` вешает
`executor._signal_handler` на SIGINT и SIGTERM до dispatch-а
(`cli.py:2519-2520`), handler лишь поднимает `_shutdown_requested`
(`executor.py:18-21`), процесс не завершается и `KeyboardInterrupt` не
поднимается — циклы `run` и `watch` видят `check_stop_requested` и делают
`break`, выходя штатным кодом (`cli.py:1281-1285`, `:1613-1616`). Поэтому
`close()` читает `executor._shutdown_requested` при сборке `outcome`, и
поднятый флаг есть «сигнал» второй строки правила, какой бы код handler ни
вернул; `_signal_handler` и флаг не правятся, диспетчер их только читает.
Оговорка: stop-marker (`config.stop_file`) флаг не поднимает —
`check_stop_requested` (`state.py:2644-2648`) читает его отдельно, — так
что остановка stop-marker-ом остаётся кодом выхода, не сигналом.
`KeyboardInterrupt` в строке — на случай, если исключение всё же дойдёт до
диспетчера; в этом дереве живой путь сигнала — флаг.

**Факт второй — исход работы:** есть ли задача, по которой **этот**
invocation записал attempt и которая на момент выхода не в статусе `success`
(`state.py:2211-2217`), либо остался open call. Attempt-ы invocation-а
закрепляет столбец `run_id` (§ 2.3), число open calls closure и так несёт. У
подкоманд, attempt-ов не пишущих, множество пусто: для них второй факт всегда
«невыполненной работы нет», и kind решает код выхода.

**Kind — малое закрытое множество из пяти**, и `run-closure.schema.json`
пинует ровно их. Строки читаются сверху вниз, первая подошедшая выигрывает:

| Как ушёл handler | Невыполненная работа | kind |
|---|---|---|
| необработанное исключение (не `SystemExit`) | любая | `crashed` |
| сигнал (флаг `executor._shutdown_requested` поднят) или `KeyboardInterrupt` | любая | `interrupted` |
| код ≠ 0, `error_kind` последнего неуспешного attempt-а — отказ правила | любая | `refused` |
| код ≠ 0 | любая | `failed` |
| код 0 | есть | `failed` |
| код 0 | нет | `completed` |

«Отказ правила» — закрытое подмножество словаря дерева `ERROR_KINDS`
(`errors.py:101-121`): `policy`, `budget`, `blocked`, `hook_failure`,
`harness_guard`. Остальные его значения (`instrument`, `timeout`, `network`,
`rate_limit`, `auth`, `api_error`, `cli_error`, `internal_error`,
`interrupted`, `unknown`) означают «инструмент не смог ответить» и дают
`failed`. Значение `interrupted` словаря `ERROR_KINDS` совпадает по имени с
kind-ом closure, но им не становится: сигнал ловит вторая строка правила
раньше, чем дело доходит до attempt-ов. Подмножество берётся из словаря дерева, а не заводится заново:
второй словарь для тех же состояний — это ровно #230.

**`completed` даёт единственная клетка** — код 0 при отсутствии невыполненной
работы. Ни ненулевой код, ни невыполненная задача не дают его ни на одной
строке; клетки «причина неизвестна → `completed`» в правиле нет, потому что
последняя строка не дефолт, а конъюнкция двух проверенных условий. Этим и
держится инвариант 4 чартера и FR-07.

**Правило не читает `last_run_stop_reason` — и в этом его смысл.** Дефолт
`stop_reason = "completed"` (`cli.py:876`) персистится на `cli.py:1374` и
тогда, когда прогон не выполнил ни одной задачи: цикл single-mode меняет его
лишь на двух сайтах (`:1318`, `:1333`), и gate-отказ под `review_policy:
required` до них не доходит — `HOOK_FAILURE` фатален (`execution.py:1284`,
`:1522`), одна неудача не трипует `max_consecutive_failures`
(`config.py:292`). Closure, ключуемая на этом значении, назвала бы такой
прогон успехом; правило смотрит на код выхода, а он здесь 1. Свой исход `run`
уже считает сам: `considered = touched | tasks_to_run`, и каждая задача
оттуда не в статусе `success` увеличивает счётчик неуспехов —
включая выбранную задачу вообще без attempt-ов (`cli.py:1344-1371`). Поэтому
для `run` «не доделал выбранное» закодировано кодом выхода, а второй факт
нужен там, где код лжёт: `retry`, чья задача закончила `blocked`
(`cli.py:1528`, exit 0), и `watch`, остановленный `max_consecutive_failures`
(`:1623`, exit 0).

Откуда берётся исход работы — по перечню платящих подкоманд FR-01
(одиннадцать позиций):

| Подкоманда | Исход работы |
|---|---|
| `run` | attempt-ы invocation-а и статусы их задач в DB |
| `retry` | то же |
| `watch` | то же |
| `plan` | attempt-ов нет — только код выхода |
| `review-pr` | attempt-ов нет — только код выхода |
| `doctor` | attempt-ов нет — только код выхода |
| `tdd abandon/repair/resume/release` | attempt-ов нет — только код выхода |
| `budget authorize` | attempt-ов нет — только код выхода |
| `restore` | attempt-ов нет — только код выхода |
| `evidence close-call` | attempt-ов нет — только код выхода |
| `evidence purge` | attempt-ов нет — только код выхода |

Инвентаря сайтов выхода в этом дизайне нет — ни по `run`, ни по десяти
остальным. Единая точка делает его ненужным: любая ветка любого handler-а —
существующая сегодня и добавленная после этого бандла — проходит через
`finally` диспетчера и получает kind по тем же двум фактам. Перечень сайтов,
наоборот, обязан был бы догонять дерево.

**`reason` — свободная строка, не словарь.** Схема пинует kinds; reason не
перечисляется и не валидируется, «неизвестного» reason не бывает, и отказа
сериализации по reason нет. Диспетчер собирает его по kind-у: для `crashed` —
тип и сообщение исключения; для `interrupted` — `shutdown_requested` (флаг
имени сигнала не хранит) или `KeyboardInterrupt`; для `refused` и `failed` —
`error_kind` и `error` последнего неуспешного attempt-а, если он есть, иначе
строковый аргумент `SystemExit`, если это строка, иначе пусто; для `completed`
— персистированный `last_run_stop_reason`, если invocation его писал (ровно
то, что показывает `status`), иначе пусто. Исключение — таймаут drain: там
reason называет неподтверждённый checkpoint, потому что это факт самой
`close()`. Пусто — допустимое значение: смысл несёт kind, reason помогает
человеку.

**`RUN_STOP_REASONS` не растёт и ключом closure не является.** Словарь
(`cli.py:536-541`) описывает то, что показывает `status` и читают внешние
потребители (audit-таблица Maestro); closure его не читает и не расширяет.
Следствие названо прямо: `status` по-прежнему покажет `completed` прогону,
оборванному таймером или stop-marker-ом, — это сегодняшнее поведение дерева,
и его исправление не предмет этого workstream-а (§ «Вне объёма»). Closure
такого прогона строится по своим двум фактам, а не по тому, что показывает
`status`.

**Чего правило не различает — названо здесь, а не оставлено читателю.**
(1) Отказ правила от поломки инструмента у подкоманд без attempt-ов:
`plan --gated` с неодобренным upstream-ом (`cli_plan.py:126-128`, exit 2, ни
одного вызова CLI) и `doctor`, у которого оператор отказался на cost gate
(`doctor.py:389`, exit 2), дают `failed` наравне с настоящим отказом
инструмента. (2) Код 1 «работа плоха» от кода 2 «инструмент не смог» — оба
`failed`; различитель остаётся в поле `exit_code`, которое closure несёт.
(3) Прогон, остановленный таймером или stop-marker-ом, от прогона, которому
нечего было делать, — когда обе конфигурации дают код 0 и невыполненной
работы нет; сигнал сюда не входит — его диспетчер видит по флагу (факт
первый). Каждое из трёх восстановимо только своим словарём причин по
сайтам, а он и есть то, что этот дизайн снял; `exit_code`, `attempt_ids` и
число open calls, которые closure несёт, дают читателю ту же информацию, не
требуя, чтобы словарь догонял дерево. По той же причине `completed` с пустым
`attempt_ids` и нулём вызовов — это «делать было нечего», и read-surface
(FR-09) показывает его как есть: отдельный kind для «ничего не делал» был бы
шестым на пустом месте.

**Отказные режимы.** Повторная closure — `AlreadyExists` от store (§ 1.3) →
отказ, первая неизменна. Отказ записи closure — stderr + exit code не
улучшается. `kill -9` — closure нет по построению (BEH-30). Состав записи:
`run_id`, `pipeline_id`, подкоманда, kind, reason, exit code,
`last_checkpoint_id`, `last_call_ids`/`attempt_ids`, число open calls,
`degraded`/spool status, timestamps start/end. Состав kinds идёт в
`schemas/run-closure.schema.json` и в CHANGELOG под Unreleased тем же
коммитом, что схему (§ 8).

**6.4 Attempt evidence** (FR-06) — при terminal `record_attempt`
(`success` / `failed` / `blocked`) `evidence.export_attempt(state, task_id,
n)` собирает строки этой задачи/attempt-а из перечисленных таблиц
(`WHERE task_id = ? [AND attempt = ?]`, namespace-фильтр для TDD-таблиц) в
JSONL по `schemas/evidence-record.schema.json` и публикует
`attempts/<task>-<n>.jsonl`; для `review-pr` — при завершении раунда
(BEH-23). Публикуется через ту же очередь publisher-а; drain перед closure
ждёт и его.

**6.5 Корроборирующие логи прогона** (FR-06, SSOT #478) — инвентарь
`docs/architecture.md:251-252` называет две строки, которые обязаны ехать с
evidence bundle и ledger-ом не являются: task change history
(`spec/.task-history.log` и префиксные варианты, `task.history_file_for`,
`task.py:418`) и compliance audit-trail (`audit_log_path`, выключен по
умолчанию, `audit_log.py:195`). Обе публикуются **срезом этого прогона**, не
файлом целиком, и перед closure, вместе с attempt-экспортом. Срез у них
получается по-разному, потому что по-разному устроены источники: строка
истории задач — `<ts> | <task_id> | <change>` (`task.py:429`) и `run_id` не
несёт, поэтому `RunContext.start()` запоминает размер файла, а publisher
кладёт дописанный за прогон хвост как `runs/<run_id>/task-history.log`; у
audit-log-а `run_id` есть в каждой строке (`audit_log.py:158`, после § 6.1 —
из контекста), поэтому срез — строки своего `run_id`, ключ
`runs/<run_id>/audit-log.jsonl`. Оба проходят тот же redactor и тот же
`bound_evidence` (§ 1.4, NFR-06); обоих может не быть (история не заведена,
аудит выключен) — отсутствие источника даёт отсутствие ключа, не отказ.
Ни `evidence`, ни `restore` по ним решений не принимают: SSOT называет
историю «corroborating history, not the authority for current status», и
здесь она ровно этим и остаётся. Файл целиком не копируется намеренно: он
переживает много прогонов, и копия под каждым `run_id` превратила бы
retention по `run_id` (Q-11) в умножение одного и того же текста. Второй
вариант SSOT для audit-trail («point it at an orchestrator-managed durable
volume») остаётся операторским: он не заменяет срез, а решает ту же задачу
конфигурацией, и спорить с ним дизайну не о чем.

### 7. Restore и read-surface (FR-05, FR-09)

**7.1 Модуль** — `src/spec_runner/restore_cmd.py` + `evidence_cmd.py`
(argparse: `restore <run_id> --into <dir> [--experimental] [--json]`;
`evidence <run_id> [--json]`, `evidence close-call …`, `evidence purge …`).
`restore <run_id>` и `evidence <run_id>` читают **только store**
(`Publisher`-less `_open_store` — единственное исключение из § 1.4,
read-only, статический пояс это допускает по имени функции
`open_store_readonly`); `project_root`, DB и Git не требуются для `evidence`
(BEH-36). `evidence close-call` и `evidence purge` — писатели: они
инстанцируют `Publisher` обычным путём § 1.4, а не read-only дверь, и
пишут свою пару run-start + closure (§ 2.5, § 6.2).

**7.2 Порядок проверок restore** — до записи в `--into` (BEH-20), функция
`restore.plan(run_id) → RestorePlan | RestoreRefusal`:
`contract_version` (manifest ≤ поддерживаемой) → digests всех файлов
последнего acknowledged checkpoint-а + `manifest_sha256` (NFR-04) →
repository identity (root commit `--into`-клона против manifest) → policy
identity (`config_hash` активного config против manifest, ключ и оба
значения в сообщении) → namespace (§ 7.3) → open calls **всего workstream-а** (ниже) →
spool (sha256 каждой строки). Первое несовпадение — отказ;
instrument-класс ((1), (2), (7)) — exit 2, остальные — `needs-human`,
exit 1; единственное исключение — (6) при недоступном store или индексе:
отсутствие open call тогда не доказано, и это instrument, exit 2, а не
`needs-human`.

Проверка (6) — namespace-wide, а не по восстанавливаемому `run_id`, и это
единственный способ выполнить FR-02 на пути restore. `run_id` — ключ
прогона, а правило запрещает повтор open call **в том же namespace**; между
ними разница ровно в одном наблюдаемом сценарии: namespace N, прогон A
закрыт, более поздний прогон C оставил open call X, оператор восстанавливает
A. Проверка по ключам `runs/A/calls/` не находит ничего, snapshot A
применяется, и первый же `run` повторяет X — молча, то есть ровно тот запрет,
который FR-02 и M-02 формулируют буквально. Поэтому порядок проверки (6):

1. `workstream_key` берётся из run-start восстанавливаемого прогона (§ 1.3);
   его отсутствие — уже отказ на проверке legacy, не здесь.
2. Один `list` индекса `workstreams/<workstream_key>/runs/`.
3. Разбираются по своим `calls/`-ключам: каждый прогон без парного маркера
   `.closed` **и** каждый прогон со `started_at` позже восстанавливаемого,
   включая закрытые (маркер `.closed` говорит о закрытии прогона, не о
   закрытии его calls — § 1.3).
4. Любой call-start без call-result, в каком бы прогоне он ни нашёлся, —
   отказ `needs-human`, exit 1; сообщение называет `run_id` этого прогона,
   `call_id`, provenance и `task_id`, то есть не «у A open call нет», а «в
   этом workstream open call есть, и вот где».
5. Более поздний прогон **без** open calls — тоже отказ `needs-human`, но с
   другим сообщением: восстановленное состояние старше его изменений
   (`budget authorize`, `tdd abandon|release|resume` живут под собственными
   `run_id` и в snapshot A не попали), и продолжать с него значило бы
   потерять их молча. Выход оператора назван в самом отказе: восстановить
   последний `run_id` workstream-а (он печатается) либо, если нужен именно
   ранний, решить это явно. Откат на ранний checkpoint workstream-а, у
   которого есть более поздние прогоны, этот бандл как операцию **не
   предлагает**; иное решение владельца — один флаг и одна ветка здесь.
6. Store или индекс недоступны — instrument, exit 2: доказать отсутствие
   open call нечем (та же логика, что в ветке (2) Q-12).

`--json` несёт исход этой проверки полем `workstream` (`later_runs[]`,
`open_calls[]`), чтобы отказ был машинно-читаем, а не только текстом. Legacy (`run-start.json` отсутствует или `contract_version` < 1, или
`ack_channel: local`) — fail-closed с перечнем недостающего и ссылкой на
`docs/architecture.md` «operational minimum» (BEH-21 (а)). Без
`--experimental` до снятия статуса — отказ с текстом CON-01; статус — одна
константа `RESTORE_EXPERIMENTAL = True` в `restore_cmd.py`, которую снимает
CHANGELOG-запись (BEH-21 (б) параметризует оба значения). Непустой `--into`
— отказ до всего.

**7.3 Применение** — `restore.apply(plan)`: клон и Git-материал (§ 4.3),
`state.db` из snapshot-а на место `config.state_file` нового каталога,
replay spool (§ 5), удаление ничего — lock/stop/ready/worktrees просто не
создаются (BEH-17). Сразу после укладки DB `apply` правит в ней один ключ
meta: `continuation_index` = `restored` (§ 6.1 пишет `local` только из
`RunContext.start()`). Без этой строки восстановленный snapshot нёс бы
`last_run_id` прогона A, первый `run` после restore пошёл бы по ветке (1)
Q-12 — по `open`-строкам DB, снятым в момент snapshot-а, — и индекс
workstream-а не прочитал бы никто: проверка § 7.2 осталась бы одноразовой,
а не инвариантом. `last_run_id` при этом сохраняется как факт (чей snapshot
лежит), решение принимает маркер. Правка — `set_meta`, не `record_*`, так
что `after_mutation` она не вызывает и checkpoint-а не производит. Namespace: активный config нового каталога объявляет
`tdd_namespace` == manifest → ок; объявляет другой → отказ выше; не
объявляет → рабочее допущение Q-09: `tdd_namespace: <value>` дописывается в
YAML (`spec-runner.config.yaml`, shape-preserving merge по образцу
`preset_cmd.apply_to_config --apply`, с `.bak`), diff печатается, файл виден в
`git status` (BEH-20, последний And). Иное решение владельца — одна ветка:
отказ с инструкцией. Следующий шаг — из DB: open call → `needs-human`
(уже отказано в 7.2); задача с confirmed red без green → `spec-runner run
--task <id>`; задача после green с не-DONE lifecycle → `spec-runner tdd
resume <id> …`; иначе `spec-runner run --all`. `--json` —
`schemas/restore-result.schema.json`: `status: ok|needs-human|instrument`,
`next_step` либо `reason`, `checks[]` с исходами, `wip`, `namespace`.
`Popen` за время restore — 0 (двойник в тесте).

**7.4 `evidence <run_id>`** — `evidence_cmd.collect(store, run_id) →
EvidenceView` (один `collect()` для человека и `--json`, по образцу
`tdd_status.py`; `schemas/evidence-view.schema.json`): run-start; статус
(`closed:<kind>`, либо — при run-start без closure — всегда `crash/unknown` с
пометкой «не доказуемо: процесс мог быть жив»; read-surface не пытается
отличить живой прогон от упавшего по lock-PID и не решает за оператора,
BEH-37);
последний acknowledged checkpoint; open calls; attempts с исходами и
стоимостью (`unknown` при `null`), ссылки на call records; суммарная
стоимость; `deletions[]`; стоимость хранения, если адаптер отдаёт
(`StoreCapabilities.storage_cost`, иначе поле отсутствует). Store недоступен —
exit 2 (BEH-37). Локальный `status` показывает `run_id`/`pipeline_id`
последнего run-start namespace-а — из `executor_meta`
(`last_run_id`, `last_pipeline_id`, пишутся `RunContext.start()` когда DB
доступна; иначе поле `null`, BEH-38). Тем же `set_meta` и в той же точке
пишется `continuation_index: local` — маркер ветки (1) Q-12; `status` его не
показывает, его единственный читатель — процедура open calls § 2.4.

### 8. Формы коммитов и эвиденции

Репо под governance (`execution_mode: tdd`, `review_policy: required`,
integration PR). Отсюда формы, которые decomposition-стадия принимает как
данность:

- **Коммиты.** Каждая задача — ветка `task/TASK-###-<slug>` на integration
  branch; первый коммит — red: один падающий тест, названный по BEH-id, с
  `TDD_SELECTOR`; далее green; схемы (`schemas/*.schema.json`), `docs/
  state-schema.md`, `docs/architecture.md`, CHANGELOG — отдельными коммитами
  той же задачи, не в red. Миграция DB — в задаче, которая первой пишет
  новый столбец, не «задача-миграция» отдельно: столбец без писателя — дрейф.
  `spec-runner.config.yaml` репо задачей не редактируется.
- **Эвиденция в PR.** (1) Вывод `uv run pytest -m "not slow"` и — один раз
  перед снятием experimental — `-m slow` для BEH-39 с числом итераций;
  (2) для задачи seam-а — вывод BEH-05 с матрицей сайтов и нулём
  `PaidBinaryReached`; (3) для restore-drill — `--json` отчёт restore с
  `checks[]` и `git log`/`sha256sum` восстановленного каталога; (4) для
  NFR-02/03 — вывод `scripts/bench_durability.py` (ручной, вставлен в
  описание PR) и время CI-теста ≤ 60 s; (5) diff `docs/state-schema.md` с
  minor bump и green `test_json_result_contract.py`; (6) ссылка на
  issue-handoff соседям (devtools, Maestro) с контрактом
  `run_id`/`pipeline_id` (ADR-ECO-006).
- **Закрытие #480** — ссылкой на PR с restore-drill (M-01) и матрицей open
  call (M-02); пункт `runtime-state-artifact-export` в `TODO.md` закрывается
  тем же PR.

## Рамки red-дизайна

Задачи делятся на три класса; для каждого — что red обязан предъявить живьём
и чего утверждать не должен, чтобы не окаменить структуру вместо поведения.

**Задачи-измерения** (BEH-05 «call-start раньше spawn на каждом сайте»,
BEH-13 «checkpoint после каждой mutation», BEH-39 fault-injection ×1000,
BEH-41 бенчмарк, BEH-22 матрица outcome × site).
- MUST verify live: общий журнал двойников store и `_spawn`, в котором для
  каждого `spawn` непосредственно раньше стоит `call_start` с тем же
  `call_id` — по **настоящему** прогону каждого сайта с fake CLI, не по
  вызову seam-а напрямую; число `spawn` == число acknowledged call-start;
  для BEH-13 — двойник store получает ровно один checkpoint с большим
  `sequence` после каждой mutation из перечня, вызванной через её штатный
  сайт (`tdd abandon`, `budget authorize`, `commit_status_flip`…), и ноль —
  после `status`/`costs`/`validate`/`report`/`evidence <run_id>`; для BEH-39 —
  `os._exit` из двойника seam-а в параметризованной точке, новый процесс,
  сверка «подтверждено ∧ отсутствует после рестарта == ∅», детерминированный
  seed; для BEH-22 — record в двойнике store на каждую клетку, с `null`
  вместо `0.0` там, где fake CLI стоимости не сообщил.
- MUST NOT assert: результат `grep` по исходнику на `subprocess.run`
  (обход ловит пояс `PaidBinaryReached` при живом прогоне, а grep красен от
  комментария и слеп к `getattr`); wall-clock p95/p99 внутри CI-теста
  (BEH-41 — `manual`; в CI только ≤ 60 s валидации на reference-наборе с
  щедрым запасом); имена приватных функций seam-а; порядок внутренних шагов
  seam-а сверх наблюдаемого «call-start acked → spawn → result»; число строк
  или число `after_mutation`-вызовов в `state.py`.

**Задачи-артефакты** (схемы `checkpoint-manifest` / `evidence-record` /
`run-closure` / `restore-result` / `evidence-view`, столбцы `run_id`/`call_id`,
`RunContext`, `LocalVolumeStore`, spool-файл, корпус секретов, `wip.tar`).
- MUST verify live: manifest, полученный двойником после настоящей mutation,
  валиден по схеме и **не содержит** `str(project_root)`, `str(os.getpid())`,
  имени активного `spec-runner-red-*` worktree (BEH-14) — grep по байтам
  полученных файлов, а не по полям; старая DB-фикстура открывается, старые
  строки читаются с `run_id IS NULL`, `costs` даёт прежнюю сумму (BEH-03);
  `LocalVolumeStore.put` дважды под одним ключом — второй `AlreadyExists`,
  первое содержимое байт в байт (BEH-25); spool-строка с `fsync`
  (двойник `os.fsync` считает вызовы) и sha256, replay дважды без дублей,
  повреждённый байт → отказ с `seq` (BEH-34); все 100 значений корпуса
  отсутствуют в **каждом** файле, полученном двойником store, при том что
  `full_sha256` равен digest-у исходника (BEH-27); WIP — `git log`, `sha256`
  dirty/untracked и `git stash list` в восстановленном каталоге (BEH-16).
- MUST NOT assert: существование файлов схем как файлов (валидация записи по
  схеме — да; `Path.exists()` — нет); имя таблицы `spool_replays` или ключа
  `checkpoint_seq:<run_id>` в `executor_meta`; литерал пути
  `.executor-checkpoints`/`.executor-spool.jsonl` вместо свойства config;
  наличие строк в README/CHANGELOG/`docs/architecture.md` (BEH-45 —
  `manual`); формат `wip.tar` внутри (bundle/tar/index — реализатору);
  конкретные regex redactor-а (BEH-27 проверяет корпус, не паттерны);
  вхождение ключей `durability.*` в `KNOWN_EXECUTOR_KEYS` напрямую (это
  следствие loader-а, проверяемое через `load_config_from_yaml` и
  `ConfigError` на плохом значении).

**Задачи-границы** (BEH-06 отказ ack до spawn, BEH-09 open call блокирует,
BEH-20 порядок проверок restore, BEH-29/31/32 closure, BEH-35 оба отказали,
BEH-40 integrity fail-closed, BEH-43 ни байта в Git, BEH-44 контракты
аддитивны).
- MUST verify live: двойник store, отказывающий в `put` call-start → 0
  вызовов двойника `_spawn`, attempt с `error_code=INFRASTRUCTURE`, exit 2,
  closure kind `failed` — через
  `spec-runner run --task` целиком, не через вызов seam-а; open call,
  оставленный `os._exit` после ack, → `run --all` пропускает с `call_id` в
  причине, `run --task` exit 1, `restore` `needs-human` — в **новом**
  процессе и новом каталоге, 0 `_spawn`; тот же open call после `spec-runner
  reset`, после удаления файла DB и в клоне на другом пути — те же исходы и
  0 `_spawn`; restore-refusals — каждое условие
  своей параметризацией и одно с двумя подменами сразу, `--into` пуст после
  отказа (listdir), `check_claims`-двойник вызван 0 раз; closure — на каждом
  исходе из BEH-29 ровно одна запись в двойнике store с kind из схемы и
  reason == `last_run_stop_reason` там, где stop-reason персистится, включая
  четыре гарда старта, где attempt не создан и reason называет гард; `kill -9`/`os._exit` → closure нет и `evidence` говорит
  `crash/unknown`; integrity — параметризация по каждому файлу × {байт
  изменён, файл удалён}, все три команды отказывают с именем файла и обоими
  digest-ами, `run` — до `_spawn` и до `check_claims`; `git status
  --porcelain` продуктового репо после каждого E2E не содержит путей под
  `.executor-*` (сам `spec/.gitignore` untracked — не находка, BEH-43);
  `test_json_result_contract.py`, schema-тесты, `test_costs.py` — зелёные с
  изменениями только добавлением ключей.
- MUST NOT assert: точный текст отказов сверх обязательного — `call_id` и
  provenance (BEH-09), оба значения и источники namespace (BEH-20 (5)), ключ и
  оба значения policy (BEH-20 (4)), оба root commit (BEH-20 (3)), имя файла и
  оба digest-а (BEH-40); отсутствие символа `_launch`-подобных приватных имён
  или наличие класса `RunContext` по имени; интервал/порядок drain
  publisher-а сверх «closure позже последнего ack» (журнал двойника);
  тайминги с узким запасом (таймаут ack в тесте — маленький, но отказ
  проверяется по факту, а не по секундам); что процесс «не был убит» на
  успешном пути.

Общее для всех классов: red не вызывает платного агента — conftest-пояс
`PaidBinaryReached` и guard `_no_real_agent_calls` (теперь и на
`paid_call._spawn`) остаются активными; сайты в E2E работают на
`tests/fixtures/fake_claude.sh`; store — только `LocalVolumeStore` в `tmp_path`
или двойник контракта; сети нет (BEH-44).

## Карта затрагиваемых модулей

| Файл / подсистема | Что меняется | Сценарии |
|---|---|---|
| `src/spec_runner/paid_call.py` (новый) | `PaidCall`, `CallOutcome`, `execute` (протокол FR-02 §2.2), `_spawn` (единственный spawn провайдера), `open_calls` (процедура Q-12, обе ветки: по `open`-строкам DB при `continuation_index: local` и по индексу workstream-а, когда маркера нет или он `restored`) | BEH-05…11, 22, 39, 44 |
| `src/spec_runner/artifact_store.py` (новый) | протокол `ArtifactStore`, `StoreCapabilities`, ключи § 1.3 (включая индекс workstream-а и `workstream_key`), `LocalVolumeStore`, `open_store_readonly` | BEH-09, 25, 28, 36, 37, 42 |
| `src/spec_runner/evidence.py` (новый) | `Publisher` (очередь по `sequence`, `drain`, `last_acknowledged`), записи `RunStart`/`CallStart`/`CallResult`/`Closure`, `export_attempt`, экспорт срезов task-history и audit-log (§ 6.5), `bound_evidence` | BEH-01, 22, 23, 26, 27, 31 |
| `src/spec_runner/redaction.py` (новый) | denylist из окружения + паттерны, placeholder `[REDACTED:kind:hash8]`; общая константа словаря имён с `obs._DEFAULT_REDACT_KEYS` | BEH-27 |
| `src/spec_runner/checkpoint.py` (новый) | `after_mutation` (один seam), backup-snapshot, manifest + `PolicyIdentity`, `sequence`, ротация локальных копий | BEH-12…15, 40 |
| `src/spec_runner/wip.py` (новый) | `collect` (bundle + dirty tar + index), `apply` (fetch bundle, распаковка, `stash store`) | BEH-16…18 |
| `src/spec_runner/spool.py` (новый) | `Spool.append`/`replay`/ротация, таблица `spool_replays` | BEH-15, 33…35 |
| `src/spec_runner/run_context.py` (новый) + `closure.py` (новый) | `RunContext` (`run_id`, `pipeline_id`, `start`/`close`, отметка размера task-history на старте — § 6.5), `PAYING_SUBCOMMANDS` (включает `evidence close-call` и `evidence purge`, § 6.2), `CLOSURE_KINDS` — пять kind'ов, `derive(outcome)` — правило вывода из кода выхода и исхода работы (§ 6.3) | BEH-01, 02, 04, 23, 29…32, 46 |
| `src/spec_runner/restore_cmd.py`, `evidence_cmd.py` (новые) | `restore` (`plan`/`apply`, порядок проверок, next step, `--experimental`, `--json`), проверка (6) по индексу workstream-а (§ 7.2) и запись meta `continuation_index: restored` при `apply` (§ 7.3), `evidence` (`collect`, `close-call`, `purge`), `retention.py`. Closure этих трёх подкоманд пишет диспетчер по коду их выхода — своих сайтов closure у них нет (§ 6.3) | BEH-09, 11, 19…21, 30, 36…38, 40, 42, 46 |
| `src/spec_runner/cli.py` | `main()`: `RunContext` вместо `uuid4().hex[:8]`, run-start по `PAYING_SUBCOMMANDS` до handler-а, dispatch в `try/except SystemExit/except BaseException/finally` с closure, перед closure читает `executor._shutdown_requested` (§ 6.3; `executor.py` не правится); `_run_tasks_inner`: replay spool + `open_calls` на старте прогона; новые subparsers `restore`/`evidence`. Сайты выхода `run`, `cmd_retry`, `cmd_watch`, `cmd_doctor` и `except SpecMetaError` не правятся вовсе: kind выводится в диспетчере, `RUN_STOP_REASONS` (`:536-541`) не растёт (§ 6.3) | BEH-02, 04, 09, 29, 32, 38, 46 |
| `src/spec_runner/execution.py`, `tdd.py`, `review.py`, `review_pr.py`, `cli_plan.py` | сайты → `paid_call.execute`; `cli_plan` — **все три** сайта (`:170` gated, `:660` full, `:797` интерактивный цикл) на `build_cli_invocation` + `parse_cli_result`, provenance `plan:<stage>` и `plan:interactive`, параметр `invoke=` `_generate_stage_draft` снимается; `ReviewPrState` вызывает `after_mutation` при закрытии раунда; `_record_call`/`_record_pr_call` — шаг close | BEH-05, 07, 08, 22…24, 46 |
| `src/spec_runner/runner.py`, `__init__.py` | `run_claude_async` удаляется вместе с публичным экспортом (Q-06) — второй, асинхронный путь к бинарю провайдера; `build_cli_invocation`, `parse_cli_result`, `classify_agent_answer` остаются и используются seam-ом; осиротевшие `tests/test_runner.py` / `tests/test_events.py` правятся в той же задаче | BEH-05, 44 |
| `src/spec_runner/state.py` | миграция столбцов `run_id`/`call_id`/`status`/`started_at`; `record_agent_call` open/close; вызовы `after_mutation` из каждого `record_*`/`supersede`/`reinstate`; `_enter_degraded_mode` → spool или `Refusal`; `spool_replays`; meta `last_run_id`/`continuation_index`/`checkpoint_seq:<run_id>` | BEH-03, 13, 15, 33, 35, 38 |
| `src/spec_runner/claims.py`, `bookkeeping.py`, `lifecycle.py` | `release_claims`, `commit_status_flip`, `advance` вызывают `after_mutation` | BEH-13 |
| `src/spec_runner/audit_log.py`, `logging.py`/`obs.py` | `run_id` обязательным параметром `AuditLogger`, из контекста; `run_id` в contextvars рядом с `pipeline_id` | BEH-01, 02 |
| `src/spec_runner/prompts_log.py` | `run_id`/`call_id` в заголовке; тело неизменно | BEH-01 |
| `src/spec_runner/config.py`, `validate.py` | блок `durability:` → поля; `ConfigError` на `tls`/шифровании/`retention_days`; свойства путей `.executor-checkpoints`/`.executor-spool.jsonl` | BEH-28, 42 |
| `src/spec_runner/git_ops.py` | `runtime_state_paths` + checkpoint-каталог и spool; `repository_identity`; helpers для bundle/published-base | BEH-14, 16, 43 |
| `src/spec_runner/cli_info.py` | `status`: `run_id`/`pipeline_id`; `costs`: строка «planning», `repo_total_cost` | BEH-24, 38 |
| `src/spec_runner/remedy.py`, `budget_cmd.py`, `doctor.py` | не правятся этим дизайном: их closure пишет диспетчер по коду выхода (§ 6.3) | BEH-46 |
| `schemas/` | новые `checkpoint-manifest`, `evidence-record`, `run-closure`, `restore-result`, `evidence-view`; аддитивно `executor-state`, `json-result`, `status`, `costs` | BEH-03, 14, 21, 23, 29, 36, 38 |
| `docs/state-schema.md`, `docs/architecture.md`, `CHANGELOG.md`, `README.md` | minor bump, контракт checkpoint/evidence/closure, «operational minimum», статус experimental, `durability:` | BEH-45 |
| `tests/conftest.py`, `tests/test_harness_guards.py` | `_no_real_agent_calls` переключается на одно имя `paid_call._spawn`, два патча швов снимаются, ключ — argv (Q-06); `test_harness_guards.py` переписывается тем же коммитом; пояс `_belt_never_executes_a_paid_binary` не меняется; фикстуры двойников store/`_spawn`/spool, `RunContext` | BEH-05, 44 |
| `tests/test_*` из §9 требований, `tests/fixtures/secrets-corpus/`, `scripts/bench_durability.py` | новые тесты по матрице behaviour-spec; имена — ожидание, не предписание | по матрице |
| Соседи (devtools, Maestro) | только issue/handoff с контрактом `run_id`/`pipeline_id` | BEH-45 |

## Вне объёма

Дизайн намеренно оставляет реализатору (под TDD, в границах разделов выше):

- Точные имена приватных функций, сигнатуры dataclass-ов, порядок полей
  записей и разбивка кода между `evidence.py`/`checkpoint.py`/`spool.py` —
  лишь бы seam-ы оставались единственными (§ 2.1, § 3.1, § 1.4) и
  доказуемыми поясом.
- Внутренний формат `wip.tar` (имена файлов bundle/tar/index, сжатие) — при
  сохранении байт-идентичности и per-file digests.
- Конкретный набор regex redactor-а и порог длины denylist сверх
  обязательного покрытия корпуса; способ генерации `corpus.json`.
- Интервал ожидания в `drain`, размер очереди publisher-а, число локально
  хранимых копий checkpoint-а сверх «последняя + предыдущая».
- Формулировки текстов отказов сверх обязательного (перечислено в
  «Рамки red-дизайна»).
- Значения дефолтов `ack_timeout_seconds` (3 s) и
  `checkpoint_ack_timeout_seconds` (60 s) — уточняются по бенчмарку BEH-41.
- Нормализация remote URL за пределами `host/owner/repo` (порты, нестандартные
  форжи) — по находкам E2E.
- Порядок и нарезка задач — стадия decomposition; карта модулей — её вход,
  не предписание «одна строка = одна задача». Разумная последовательность,
  которую decomposition вправе изменить: store + publisher + redactor →
  `RunContext`/run-start/closure → seam + столбцы → checkpoint + spool →
  WIP → restore → evidence read-surface → retention.
- Второй адаптер store и его расходы — отдельное одобрение владельца
  (CON-08); дизайн фиксирует только контракт.
- Правдивость `status` на ранних остановках: `last_run_stop_reason` остаётся
  таким, каким его пишет дерево сегодня (дефолт `completed` на stop-marker,
  таймерах, паузе→`q`, no-ready), `RUN_STOP_REASONS` не растёт. Closure этим
  словарём не ключуется (§ 6.3) и потому от его дефекта не зависит; починка
  самого `status` — отдельная работа.
- Вырезание FR-09 при сокращении объёма — решение владельца; `evidence
  close-call` при этом остаётся (это дверь FR-02, не read-surface).
