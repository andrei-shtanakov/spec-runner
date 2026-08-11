# TODO — spec-runner (план от 2026-04-16, обновлено 2026-08-10)

> Роль в экосистеме: единственная **работающая** кросс-проектная связка Maestro→spec-runner.
> Стратегический контекст: `../prograph-vault/authored/notes/ecosystem-roadmap.md`
> Актуальный статус: `../prograph-vault/authored/notes/status/2026-07-08-status.md`
>
> Пункты могут быть размечены опциональными тегами на строке чекбокса:
> `@owner:<principal>` /
> `@blocked_by:<reference>` / `@trigger:"…"` / `@id:<node-id>`. Канонические
> владельцы: `github:<login>`, `github-team:<org>/<team>`,
> `repo:<manifest-key>` или `TBD`; отсутствующий `@owner` (`missing`) отличается
> от явно отложенного `@owner:TBD`. Канонический блокер —
> `todo://<repo>/<id>`, legacy `<repo>#<slug>` поддерживается переходно. Теги
> исключены из ключа идентичности пункта в Robin (robin-runtime#27).
> Отсутствующий тег означает «неизвестно» — придумывать значение не надо.

## Текущее состояние
- 🚀 **v2.23.0 собран, ждёт мержа и тега** (release PR). `pyproject` = 2.23.0,
  секция CHANGELOG датирована 2026-08-10. Содержимое: весь класс ложно-зелёного
  выхода (#127/#129/#130/#131/#132/#134-п.1/#136), harness guard (#137), строгость
  мета-строки (#128/#133), scoped tests (#139), `TASK_BLOCKED` (#140), authoring
  contract (#135), наследование описания в `plan --gated` (#134-п.2).
  **Minor, не major:** exit-коды изменились и добавилось значение `ErrorCode`, но
  формат `--json-result` и схема state DB не тронуты, а прецедент 2.22.0 («Minor,
  not patch: `run --all` now exits non-zero on a state/spec mismatch») ровно про
  такой случай. После мержа: тег `v2.23.0` → `publish.yml` → **GitHub Release
  создать руками** (автоматически не создаётся — грабли v2.11.1/v2.12.0/v2.22.0).
- ✅ **v2.22.0 дотегнут и выпущен 2026-08-10** (`release-v2.22.0-tag` закрыт). Тег
  поставлен на релизный коммит `de9a31c`, а не на tip: там версия и датированная
  секция, а более поздние PR принадлежат следующему релизу. PyPI 200, GitHub Release
  оформлен руками, `release-tag-guard` на master зелёный. Висел нетегнутым с 08-08 —
  гард честно краснел два дня, и это не поймали: **прибор работает, петля обратной
  связи нет** (третий раз после v2.4.0 и v2.10.0).
- 📌 **Сводка релизов ниже обрывается на v2.11.0** и не покрывает v2.12…v2.22 — при чтении
  триажа номера версий не с чем соотнести. Не восстанавливаю задним числом (CHANGELOG —
  SSOT, и он полон); отмечаю как известный разрыв ведения.
- ⚠️ **Триаж 17 issues 2026-08-10** (см. «Активные задачи → Триаж 2026-08-10»). Главное:
  класс **ложно-зелёного выхода** пойман вживую (#136 — false-DONE при 1/11 уехал в merge
  и PR), и рекомендация из release notes 2.22.0 против него не помогает, потому что
  `on_task_failure: stop` не останавливает ран. Рядом — #137 (Critical): `harness_guard:
  strict` разоружается обычным ретраем. Отказов по ADR-ECO-006 нет: все находки
  перепроверены по коду и подтвердились.
- ✅ **v2.11.0 зарелижен 2026-07-26** (PyPI + GitHub Release, тег `v2.11.0`, release commit
  `7be192c`): SpecMeta contract v2 + профильная осведомлённость spec-поверхности. Собрал три
  PR: #53 (обход governance-гейта + падение `plan --gated` на кастомных профилях), #54 (C2:
  lossless frontmatter, `owner_role`, валидация, `SPEC_META_CONTRACT = 2`, замороженная
  поверхность, `docs/CONTRACTS.md`), #55 (`requires-python` → `>=3.11`). Сьют 1129 → 1197.
  Maestro-контракт не тронут.
- ✅ **v2.10.0 дотегнут и опубликован 2026-07-26** — висел собранным, но без тега с 07-14
  (третий раз подряд после v2.4.0 такого не будет: заведён CI-гард, см. ниже).
- ✅ **Governance gate в CI** (`c2d59a6`, PR #51, 2026-07-16): `.github/workflows/governance.yml`
  дёргает переиспользуемый воркфлоу умбреллы, пиненый тегом `governance-v1`. На master
  включён ruleset: обязательный чек `governance / gate` + 1 approving review от code owner
  (`.github/CODEOWNERS` = `* @andrei-shtanakov`, `48934c1`). Практическое следствие:
  PR, созданные **до** #51, висят `BLOCKED` навсегда, пока их не ребейзнут.
- ✅ **v2.9.0 зарелижен 2026-07-07** (PyPI + GitHub Release, тег `v2.9.0`, release commit
  `538d570`): C1 loadable stage profiles. Additive/minor, дефолт `lite` = поведение 2.8.x.
- ✅ **C1 «STAGES → загружаемый профиль» реализован 2026-07-06** (вошёл в v2.9.0):
  захардкоженная цепочка стадий вынесена в данные — `StageDef`/`StageProfile` +
  бандленый `profiles/lite.yaml` (= прежние req→design→tasks 1:1), `spec.py`/`prompt.py`/
  `validate.py` читают из профиля, `config.spec_profile` + флаг `--profile`, неизвестный профиль →
  внятная ошибка. Zero behaviour change: сьют **976 passed** (+50 тестов, ни одной правки старых),
  ruff/mypy чистые. Keystone steward Phase 1 — разблокирует governance-профили (steward G1/G2).
  Спека-бандл: `docs/plans/spec-runner-c1-stages-profile/`. Исполнено самим spec-runner (claude
  preset, 7/7 задач с первой попытки, $15.91) — dogfood gated+run пайплайна.
- ✅ **Оба бага `--spec-prefix` починены 2026-08-05** (slug `spec-prefix-swallow`):
  SUPPRESS-дефолты в `common` + `_CommonDefaultsParser` (мерж дефолтов после парса) —
  флаг перед субкомандой больше не проглатывается (это чинит и все остальные common-флаги,
  включая `--budget`), а семейство `spec status/approve/reject/adopt/check` получило
  common-флаги. `spec-runner.specPrefix` в VSCode-расширении заработает без правок с их
  стороны. Регресс-тесты: `tests/test_spec_prefix.py::TestSpecPrefixFlagPositions`.
- ✅ **v2.8.1 зарелижен 2026-07-05** (PyPI + GitHub Release, тег `v2.8.1`): два фикса
  machine-JSON поверхностей для `spec-runner-vscode` — `costs --json` без `tasks.md`
  отдаёт валидный пустой payload (не прозу/hard-exit), и pre-init structlog default
  в `obs.py` уводит логи в stderr (subdir-warning больше не ломает `JSON.parse`
  на `status --json`). Фиксы: `31410ab`, релиз: `85278a7`.
- ✅ **v2.8.0 зарелижен 2026-07-02** (PyPI + GitHub Release, тег `v2.8.0`): VSCode
  read-surface контракты (`schemas/status|costs|spec-frontmatter.schema.json` +
  `spec-runner --version` + `tests/test_vscode_contract.py`, PR #30) **вместе с**
  gated spec generation (#28, была `[Unreleased]`). Version-pin в
  `spec-runner-vscode` (`>=2.8.0`) теперь честный.
- ✅ **Gated spec generation** влито в master 2026-07-01 (`592528f`, PR #28): `plan --gated`,
  `spec status/approve/reject/adopt/check`, `config.spec_governance: off|strict` +
  `run`/`watch --strict`. Вышло в составе v2.8.0.
- ✅ **spec-runner-vscode** — новый sibling-репозиторий (TS/npm), тонкое расширение над
  CLI/JSON-контрактами: три TreeView, gated-действия, run/stop, unit (vitest) +
  integration (`@vscode/test-electron`) тесты. Первый JS-тулчейн в монорепе.
- ✅ v2.7.0 зарелижен 2026-06-14 (`--model` для qwen/copilot presets)
- ✅ v2.2.2 зарелижен 2026-05-29 (console-прогресс в stderr для non-TUI run/watch)
- ✅ v2.2.1 зарелижен 2026-05-28 (CI off Node 20 → Node 24, obs contract test skip-guard)
- ✅ v2.2.0 зарелижен 2026-05-28 (auto-detect OpenCode/Pi CLI, architecture diagrams, green CI)
- ✅ v2.1.0 зарелижен 2026-05-23 (observability reference impl + Dependabot patches)
- ✅ v2.0.0 зарелижен 2026-04-17 (PIPE-0…5, POLISH-1…5, `spec-runner task`, webhook, crash resilience)
- ✅ CI/CD работает (`.github/workflows/ci.yml`) — единственный проект помимо ATP с CI
- ✅ `--json-result` флаг для Maestro interop
- ✅ R-04 (контракт с Maestro) заморожен 2026-04-17 — см. `docs/state-schema.md`, `schemas/`, `tests/test_json_result_contract.py`
- ✅ **Cross-project observability v1 shipped** — spec-runner reference + Maestro M1/M2 + arbiter Rust + ATP (см. `../prograph-vault/authored/notes/status/2026-05-22-status.md`)
- ✅ **Dependabot** — `mcp 1.26.0 → 1.28.1` влит 2026-07-26 (`5126476`, PR #50; в 1.27–1.28
  задепрекейчены WebSocket-транспорт и experimental tasks API — `mcp_server.py` использует
  только `FastMCP` поверх stdio, ни то ни другое не задето). Открытых Dependabot-PR нет.

## Правила ведения
- После каждой выполненной задачи проставь `[x]` и добавь хеш коммита
- **Semver-дисциплина**: любое изменение формата `.executor-state.json` или `--json-result` — это **breaking change**, обязательно major-bump и нотис в CHANGELOG

---

## Активные задачи

### Входящие (ADR-ECO-006)

**Исходящее 2026-08-10:** заведён **maestro#169** — смена exit-кодов (класс A) едет
к потребителю, который читает ровно этот сигнал: их критерий `DONE = exit 0 + merge ok`
(maestro#164, DONE при 1/9 subtask-ов) до сих пор был правдой о процессе, а не о работе.
Там же второе, независимое от бампа: их `_load_meta` читает `executor_meta` как int'ы и
молча выбрасывает строковые `last_run_stop_reason`/`last_run_stop_detail` — проверено по
их коду. Действие целиком на их стороне; мы ничего не ждём.

**Закрыто на GitHub 2026-08-10:** #125 (уже отгружено v2.15.0), #134/#135 (по мержу
#147/#148), #136 (PR #144), #137 (PR #145, с явным перечислением четырёх несделанных
сопутствующих пунктов в теле ответа). Открытыми остаются #138, #139, #140, #141, #142.

- [x] **review-pr-json-purity** (inbox spec-runner#116, from maestro#post-pr-command): @owner:github:andrei-shtanakov @id:review-pr-json-purity
      при `--json` stdout несёт ровно один JSON-документ на всех путях выхода (0/1/2),
      диагностика — только в stderr. Было: пути лимитов (`_apply_phase`) и fail-closed
      печатали текст в stdout, а на exit 1 JSON не эмитился вовсе. Добавлен `exit_code`
      в payload (self-describing отчёт для audit-таблицы maestro). Приёмка проверена
      вживую: `review-pr 99 --json > out.json` на закрытом PR → `json.loads` ок.
      Тесты: `TestJsonStdoutPurity` (6) (PR #117)

### Триаж 2026-08-10 — 17 открытых issues (10 inbox + 7 собственных)

Три источника: пилот **disputatio** (боевые прогоны 08-09/08-10, 26 задач),
**steward** (живой V1-прогон gated-цикла на чистом клоне тега v2.21.0) и финальное
ревью PR #126. Каждое фактическое утверждение перепроверено по коду на master
`de9a31c` — **ни одно не оказалось неточным**, поэтому отказов (ADR-ECO-006,
«закрыть как not planned») в этом триаже нет. Разбивка по классам, не по номерам:
A/B — дефекты подтверждённого поведения, C — решения о политике, D — контракт
авторинга, E — крупные спеки, которым нужен scope от владельца.

#### A. Ложно-зелёный выход: exit 0 там, где ран отказал (боевой класс) — ✅ отгружено PR #144 (`8da6155`)

Шесть входов в один класс: потребитель (maestro, CI) читает успех там, где
исполнения не было или оно не доделано. Не гипотеза — пойман вживую: workstream
`w-adapters` закрылся DONE при 1/11 задач и уехал в merge + PR (#136).

Закрыто всё; остаток #134 (п.2 + документация) добит отдельной веткой,
п.4 отдан #138 как вопрос политики, а не дефект exit-контракта.
Тесты: `tests/test_exit_contract.py` — 15 тест-функций, 17 кейсов (governance-отказ
параметризован по `run`/`watch`/`retry`). Попутно выровнены два входа того же
класса, которых в issues не было: остановка по `max_consecutive_failures`/бюджету
в середине рана (предрановый отказ по той же причине уже давал ненулевой, #67) и
закрытие audit-пары на ветке «нет готовых задач» (находка ревью Copilot).
**Ранний возврат «нечего исполнять» сознательно оставлен тихим exit 0**: `--task`/
`--milestone` штатно оставляют blocked-работу вне выборки, и вердикт там был бы
ложной тревогой — он принадлежит циклу, который видит, что реально произошло.

- [x] **#136 on-task-failure-stop-does-not-stop** (inbox, from disputatio) @owner:github:andrei-shtanakov @id:on-task-failure-stop-does-not-stop
      `on_task_failure: stop` помечает задачу `blocked` и возвращает `False`
      (`execution.py:571`), но выход из цикла висит на `state.should_stop()`
      (`cli.py:888`) = «≥ max_consecutive_failures ИЛИ бюджет». Одна упавшая
      задача стопа не даёт → следующая итерация видит пустой ready при непустом
      todo → ветка «No more ready tasks» → `break` → exit 0, `stop_reason=completed`.
      Подтверждено чтением кода. **Рекомендация из release notes 2.22.0 («для
      orchestrator-managed запусков ставьте `on_task_failure: stop`») не работает** —
      пилот её выполнил и всё равно получил ложный DONE.
- [x] **#134 gated-run-v1-findings** (inbox, from steward) — п.1-3 + мелочь закрыты; @owner:github:andrei-shtanakov @id:gated-run-v1-findings
      п.4 живёт своей жизнью как #138 (там же и решение о политике).
      - **п.1 (главный) — PR #144**: `run --strict` при неаппрувнутом `tasks.md`
        печатал `⛔ spec governance: …` в stdout и делал `return` → exit 0.
        Теперь диагностика в stderr, выход ненулевой; тот же паттерн вычищен в
        `cmd_retry` и `cmd_watch`.
      - **п.2 — эта ветка**: стадии после первой требовали description, хотя
        README показывает `plan --gated --stage design` голым. Стадия с
        аппрувнутым upstream наследует описание (`_gated_description`), первая
        по-прежнему требует и говорит об этом по имени.
      - **п.3 — документация**: одиночный `run` исполняет РОВНО ОДНУ задачу
        (не волну), очередь дренирует только `--all`/`watch`. Поведение
        сознательное — единица планирования для оркестратора; в README теперь
        сказано прямо, рядом с командами.
      - **мелочь**: `--no-interactive` назван в README рядом с checkpoint-меню.
- [x] **#129 tui-thread-exit-swallow** — `cmd_run --tui` крутит `_run_tasks` @owner:github:andrei-shtanakov @id:tui-thread-exit-swallow
      в daemon-треде (`cli.py:191-198`); `sys.exit(1)` в треде гаснет молча →
      все fail-closed гейты в TUI-режиме дают процессный exit 0.
- [x] **#130 notify-on-mismatch-exit** — `_exit_on_state_spec_mismatch` @owner:github:andrei-shtanakov @id:notify-on-mismatch-exit
      (`cli.py:452-481`) завершает ран до `notify_run_complete` → владельцы
      Telegram/webhook не получают уведомление о самой тяжёлой остановке.
- [x] **#132 orphaned-success-warning-scope** — warning об осиротевших success-строках @owner:github:andrei-shtanakov @id:orphaned-success-warning-scope
      живёт внутри `if nonterminal_tasks` (`cli.py:766`); кейс «всё done + осиротевшая
      строка» проходит молча. Поднять выше `if`.
- [x] **#127 status-emoji-failed-keyerror** — `_fail_for_budget` (`execution.py:488`) @owner:github:andrei-shtanakov @id:status-emoji-failed-keyerror
      зовёт `update_task_status(..., "failed")`, а `STATUS_EMOJI` (`task.py:48`) ключа
      `failed` не имеет → `KeyError` на `task.py:278`. Найдено ревью, эмпирически
      не прогонялось; нужен тест budget-пути.
- [x] **#131 blocked-after-skip-exit-policy** — политика exit для blocked-after-skip. @owner:github:andrei-shtanakov @id:blocked-after-skip-exit-policy
      В 2.22.0 сознательно оставлен exit 0 + честный `stop_reason`; #136 показывает,
      что откладывать «до когда-нибудь» нельзя — решается вместе с ним.
      Отдельный вход: maestro не читает `stop_reason` (`_load_meta` отбрасывает не-int),
      т.е. одного честного reason'а потребителю недостаточно (maestro#124).

#### B. Producer/parser: спека проходит валидацию и падает в рантайме

- [ ] **#133 plan-meta-normalizer** — третья наблюдённая форма meta-строки от @owner:github:andrei-shtanakov @id:plan-meta-normalizer
      `plan --full` за один пилот (`- TASK-023 | 🔄 IN_PROGRESS | P0 | …`, ID и статус
      перед приоритетом). `TASK_META` её не узнаёт → статусы дефолтятся в TODO →
      `update_task_status` fail-closed возвращает False → гейт 2.22.0 честно валит ран.
      Цепочка отработала; корень — producer. Два предложения: канонический
      нормализатор на выходе генерации + validate-правило «задача без распознанной
      TASK_META = error» (сейчас такой файл валидацию проходит).
- [ ] **#128 task-meta-status-whitelist** — `TASK_META` парсит статус как `(\w+)`: @owner:github:andrei-shtanakov @id:task-meta-status-whitelist
      `- P0 | high priority stuff` даст статус `high`. Bullet-допуск 2.22.0 расширил
      поверхность. Сузить до альтернации известных статусов, осторожно с обратной
      совместимостью (см. также #133 — тот же шов с другой стороны).
- [ ] **#139 scoped-test-command** — `build_scoped_test_command` (`git_ops.py:286`) @owner:github:andrei-shtanakov @id:scoped-test-command
      дописывает пути тестов в конец **всей** shell-цепочки: при
      `pytest -q && pyrefly check` пути уедут в `pyrefly`. Плюс деградация полного
      suite до выборочного не видна в evidence. Оговорка автора: найдено чтением
      кода, на 5 прогонах scoping не активировался ни разу.

#### C. Целостность оракула и политика ретраев (решения владельца по составу)

- [x] **#137 harness-guard-retry-bypass** (inbox, Critical, from disputatio) @owner:github:andrei-shtanakov @id:harness-guard-retry-bypass
      `harness_guard: strict` снимает snapshot **внутри каждой попытки**
      (`execution.py:133`, вызывается из `run_with_retries` по попытке) → запрещённая
      правка, пережившая упавшую попытку, попадает в baseline следующей и
      легализуется. Боевое наблюдение: TASK-022, attempt 1 FAIL → правка осталась в
      дереве → attempt 2 PASS → правка в истории. Гейт блокирует ровно один раз и
      разоружается ретраем; при дефолтном `max_retries: 3` заявленная гарантия
      «оракульная поверхность неизменна» не выполняется.
      **Исправлено PR #145 (`9a2267a`)**: `harness.HarnessBaseline` — снимок привязан
      к lifecycle задачи и переиспользуется всеми попытками, включая рекурсию
      операторского `retry`. Захват остался ленивым, ПОСЛЕ `pre_start_hook`, иначе
      `uv sync` стал бы нарушением на каждом ране. Тесты:
      `tests/test_harness_guard_retry.py` (6); проверено, что два ключевых краснеют
      без фикса.
- [ ] **harness-guard-companions** — четыре сопутствующих пункта из #137, @owner:github:andrei-shtanakov @id:harness-guard-companions
      каждый самостоятельный (issue закрыт по главному дефекту, эти — нет):
      1. **Control-plane не защищён** — `spec-runner.config.yaml` не входит ни в
         `HARNESS_CANDIDATES`, ни в дефолтный `harness_files`: агент, работающий в
         worktree, может изменить саму политику, которой его проверяют.
      2. **Текст гейта предлагает себя обойти** — «...or, if the change is
         intentional, exempt it via `harness_allow`» уходит в retry-промпт
         author-агенту. Самый дешёвый из четырёх и, пожалуй, самый неприятный: мы
         буквально подсказываем, как снять барьер.
      3. **`harness_allow` глобален** — task-scoped исключений нет, разрешив файл
         ради одной задачи, оператор открывает его всем последующим.
      4. **Нет preflight'а** на пересечение declared scope задачи с оракульными
         файлами. TASK-022 была невыполнимым контрактом с самого начала — это
         выявляется статически, за секунды, до запуска агента.
- [ ] **#138 review-stage-fail-open** (inbox, from disputatio) — стадия `review` @owner:github:andrei-shtanakov @id:review-stage-fail-open
      не может провалить задачу ни при каком исходе, но в логе выглядит как гейт.
      Три пути: таймаут → `FAILED`, но `hooks.py:415` явным комментарием делает его
      advisory; **нет маркера в выводе → `PASSED`** (`review.py:343` — пустой вывод
      агента засчитывается как успешное ревью); порядок стадий
      `tests → lint → commit → review` — ревью застаёт код уже закоммиченным.
      Замеренная цена: 6 таймаутов по 15 минут на 26 задачах — полтора часа стены
      за советы, которых не было, все шесть задач закрыты DONE.
      Решить: различать в evidence «прошло / не состоялось / нашло проблемы»,
      блокирующий режим вне HITL, порядок ревью относительно commit.
- [ ] **#140 terminal-refusal-no-retry** (inbox, from disputatio) — ретраи не отличают @owner:github:andrei-shtanakov @id:terminal-refusal-no-retry
      переходный сбой от осознанной эскалации к оператору: агент, честно
      остановившийся по конституции проекта, получает попытки 2-3 с припиской
      «Do not repeat the same mistake», хотя единственный неошибочный путь ему
      запрещён самим харнессом. На TASK-025 попытка 2 из-за этого перешла границу
      чужого scope, которую попытка 1 корректно соблюла — **барьер выдержал один раз
      и был снят ретраем** (тот же механизм, что в #137). Предложение: маркер
      `TASK_BLOCKED: <причина>` рядом с `TASK_FAILED`, такие не ретраить.

#### Найдено при триаже, не из issues

- [x] **release-v2.22.0-tag** — тегнут и опубликован 2026-08-10; остаётся открытым вопрос, почему красный @owner:github:andrei-shtanakov @id:release-v2.22.0-tag
      `release-tag-guard` на master не превратился в действие (гард работает —
      не работает петля обратной связи). Кандидат: сделать провал гарда шумным
      там, где его увидят, а не только в списке ран-ов.
      При выпуске следующей ступени (класс A + #137 + #134 + #135 → minor, v2.23.0)
      дописать номер тега в **maestro#169**: их предупредили о смене exit-кодов
      заранее, но бампать пин (`SPEC_RUNNER_REQUIRED_VERSION = "2.16.0"`) им
      не по чему, пока тега нет.

#### D. Контракт авторинга SpecMeta (steward)

- [x] **#125 specmeta-owner-role-canonical** (inbox, from steward) — **ask уже удовлетворён @owner:github:andrei-shtanakov @id:specmeta-owner-role-canonical
      отгруженным кодом**, проверено по артефактам 2026-08-10:
      `docs/CONTRACTS.md` («exactly one role, no `@`, no comma-list», SSOT формы —
      steward `profiles/roles.yaml`), фикстура (`owner_role: platform`) и
      инлайн-комментарий `spec.py` — все три под DEC-007 с PR #94, вышли в **v2.15.0**;
      триаж 08-10 переоткрыл пункты по устаревшему снимку TODO, а не по коду.
      **Писать нам нечего**: spec-runner `owner_role` не генерирует — только носит из
      уже существующего frontmatter, так что legacy-форму мы не производим. Грамматику
      (`^[a-z][a-z0-9-]{1,31}$`) сознательно НЕ валидируем: данные steward не
      мигрированы, а носитель обязан round-trip'ить legacy `"@a,@b"` дословно
      (пинится `test_legacy_owner_role_form_carried_verbatim`). `reviewer_roles[]` /
      `allowed_approver_roles[]` уже проходят через `extra` (их же vendor-тест это
      подтверждает). `SPEC_META_CONTRACT` остаётся 2.
      Остаток — не наш код: заметка в vault всё ещё велит steward игнорировать примеры
      из фикстуры v2.11.0 (см. `dec007-vault-note-update` ниже; после #135 фикстура
      меняется снова, так что обновлять её стоит один раз, после релиза).
- [x] **#135 authoring-contract-traces-pins** (inbox, from steward, DEC-008) @owner:github:andrei-shtanakov @id:authoring-contract-traces-pins
      Канонические имена стадий признаны нашими (steward переименовал `task` → `tasks`
      у себя, без alias). Остаток шва — за нами как владельцем формата, сделано:
      `spec.stamp_authoring_links` (+ `derive_traces_to` / `upstream_pins` /
      `git_blob_hash`) зовётся из `plan --gated`, `spec approve` и `spec adopt`.
      (1) `traces_to` — **список**: сначала прямой upstream по цепочке профиля,
      затем реальные id из тела, которые действительно резолвятся в upstream-тексте
      (нерезолвящийся id у steward — `GC-TRACE` **error**, хуже исходного warn'а).
      (2) `upstream_hashes` — `{прямой upstream: git blob hash}` при approve;
      считаем локально (`sha1("blob <len>\0"+bytes)`), воспроизводится
      `git hash-object`. Пин НЕ обновляется при re-approve upstream — расхождение
      и есть сигнал stale-cascade.
      Контракт не бампается: оба ключа — extras (pass-through), не canonical.
      Попутно: golden-фикстура шипилась с **невалидным** для steward примером
      (`traces_to` скаляром и пином транзитивного предка `requirements` на стадии
      `tasks` → `GC-STALE-KEY`) — исправлена; формы описаны в `docs/CONTRACTS.md`.
      Тесты: `tests/test_authoring_links.py` (15).

#### E. Крупные спеки D7 — нужен scope-решение владельца, не «сделать»

Обе поданы disputatio как inbox-issues и намеренно разделены: связать в один PR
значило бы, что при провале непонятно, что сломалось. Объём каждой — уровень
minor-релиза, а не багфикса.

- [ ] **#141 D7-A tdd-execution-mode** — `execution_mode: standard | tdd` @owner:TBD @id:tdd-execution-mode
      как контракт исполнителя: фазы `RED_AUTHORING → RED_VERIFYING → GREEN_* →
      REFACTORING`, переход в green запрещён без **подтверждённого** red (селектор
      реально прогонялся и упал), типизированные per-phase вердикты
      (`PASS|EXPECTED_FAIL|UNEXPECTED_FAIL|ERROR|WAIVED`), durable checkpoints
      `(commit SHA, полный pytest node-id, baseline SHA, namespace)`, evidence в state
      с append-only историей. `standard` обязан остаться byte-identical.
      Вход достоверный: пилот прогнал 100 leaf-задач через TDD-дисциплину,
      построенную **вне** spec-runner (плагин + 1500 строк скрипта), и все упоры
      однотипны — у задачи нет фаз. Их же выводы, без которых контракт неполон:
      байт-замок на все заклеймленные файлы (не только текущий), операторские remedy
      `abandon`/`repair` записью а не прозой в коммите, lint фиксируемого файла в `red`,
      типовая проверка в пер-тасковом гейте, и #140.
      Известный блокирующий вход: `post_done` срабатывает **после** commit/merge,
      т.е. фазовая проверка не может стоять до коммита (пересекается с #138 п.3).
- [ ] **#142 D7-B greenfield-preflight-bootstrap** — нулевого этапа у spec-runner нет. @owner:TBD @id:greenfield-preflight-bootstrap
      Две раздельные команды: `preflight` (только диагностика, JSON-вывод для
      оркестратора) и `bootstrap --check|--plan|--apply` (создание, стековые detectors
      + **явные** presets, не эвристика). Главное из пилота: bootstrap обязан оставить
      **сертифицированный** оракул — baseline-тест (пустой suite даёт `0 passed` и
      exit 0, т.е. зелёный гейт на пустом проекте не доказывает ничего) и mutation
      probe. Отдельное правило оттуда же: exit code берётся у проверяемой команды,
      а не у последнего элемента пайпа (`... | tail -1` сделал ветку ERROR
      недостижимой). Границы: preflight Maestro не смешивать с нашим, авторинг-гейты
      steward не при чём. Пересекается с #133 (fail-closed валидация сгенерированной
      спеки как часть стартового контура).

#### F. Дизайн-треки (решения владельца приняты, код не начат)

- [ ] **tdd-lifecycle-design** — #141 принят как **дизайн-трек**, не minor-релиз: @owner:github:andrei-shtanakov @id:tdd-lifecycle-design
      `execution_mode: tdd` добавляет state machine, durable-чекпоинты, модель
      evidence, replay, операторские remedy, миграцию состояния и новую
      терминальную семантику — каждый пункт со своим радиусом поражения.
      Дизайн-документ: `docs/superpowers/specs/2026-08-11-tdd-lifecycle-design.md`.
      Порядок: (0) общий контракт результата фазы, независимый от TDD и
      полезный сам по себе → (1) RED-чекпоинт → (2) байт-замок на все
      заклеймленные файлы → (3) операторские remedy → (4) GREEN/REFACTOR.
      **Жёсткая зависимость:** нельзя начинать раньше решения по #157 —
      `post_done` срабатывает после commit/merge, а RED_VERIFYING обязан
      гейтить коммит, а не следовать за ним. @blocked_by:todo://spec-runner/review-policy-and-lifecycle
- [ ] **review-policy-and-lifecycle** (#157) — может ли review блокировать @owner:TBD @id:review-policy-and-lifecycle
      задачу вне HITL и где стадия стоит относительно commit. Не переносить
      «просто до commit»: checkpoint-коммит существует ради стабильного SHA и
      provenance. Закрывает попутно п.4 из #134.
- [ ] **bootstrap-product-boundary** (#159) — берём ли мы на себя scaffolding. @owner:TBD @id:bootstrap-product-boundary
      Плюс открытый вопрос: где живёт mutation probe, если bootstrap не берём.

### Battle-testing round 4 — находки с v2.16.0 (issues от 2026-08-06, run d4d33ad0) — ✅ 4/4 отгружено
(#101/#103/#104 — в v2.17.0; #102 — цикл `review-pr` M1/M2/M3 в v2.18.0–v2.20.0)

Четыре находки прогона TASK-007 на kapelle (F-21…F-24).

- [x] **#103 commit-provenance** — весь код фичи уезжал в коммит «code review fixes», @owner:github:andrei-shtanakov @id:commit-provenance
      таск-коммит получал только флип чекбокса в tasks.md (история лжёт о происхождении
      кода). Фикс: exec-работа коммитится под таск-лейблом ДО review; review-коммит несёт
      только свою дельту; no_op-детекция (#97) учитывает pre-review коммит.
      Тесты: `test_commit_provenance.py` (PR #105)
- [x] **#104 run-summary-delta** — `Execution summary completed=2` на однозадачном ране: @owner:github:andrei-shtanakov @id:run-summary-delta
      summary печатал кумулятивные счётчики executor_meta как итог рана (то же в
      run_complete-нотификации, audit-записи и failed_attempts). Фикс: снапшот до рана,
      в summary — дельта. Тесты: `test_run_summary.py` (PR #106)
- [x] **#101 pr-opened-notification** — human-merge-гейт зависел от смотрящего в терминал. @owner:github:andrei-shtanakov @id:pr-opened-notification
      Наша сторона: событие `pr_opened` в существующем notify-механизме
      (Telegram/webhook, в дефолтном `notify_on`); диспетчер может съедать webhook.
      Вопрос агрегации (dispatcher-консоль поверх maestro+spec-runner) остаётся
      за экосистемой (PR #107)
- [x] **#102 review-bot-loop** — **решение владельца принято 2026-08-06**: @owner:github:andrei-shtanakov @id:review-bot-loop
      реализуем в spec-runner как отдельную resumable-команду `spec-runner review-pr <url-or-number>`
      + опциональную post-PR стадию (НЕ maestro-only hook и НЕ inline в `run`); внешний
      оркестратор вызывает ту же команду вместо своей реализации цикла. Потребление на
      стороне Maestro отслеживается снаружи: maestro#147 / todo://maestro/post-pr-command
      (форма, lifecycle и маппинг исхода — их канон, не наш).
      Граница: transport+verify/fix/reply loop → spec-runner;
      когда/для какого PR → владелец lifecycle; approval policy → maestro approver_cmd
      (maestro#137, не смешивать approval с mutation). Дизайн-док с state machine,
      нормативными ограничениями (opt-in, allowed bots, verdict valid/refuted/uncertain,
      uncertain→human, TDD-фиксы отдельными коммитами с provenance, гейты до push, ответ
      только после push с SHA, лимиты на всё, fail-closed, no auto-merge/approve) и фазами
      M1 read-only → M2 fix+reply → M3 wiring:
      `docs/superpowers/specs/2026-08-06-review-pr-loop-design.md`
      **Закрыт 2026-08-06**: M1/M2/M3 отгружены в v2.18.0/v2.19.0/v2.20.0, issue #102
      закрыт мержем PR #114; follow-up по границе caller-контракта — PR #119;
      блокер потребителя снят в v2.21.0 (inbox #116).
  - [x] **M1 (read-only)**: команда `review-pr` — collect (gh CLI, allowed-bots фильтр) @owner:github:andrei-shtanakov @id:review-pr-m1
        → verify (агент, fail-closed к uncertain, вердикт аннулируется
        при мутации дерева верификатором) → отчёт text/`--json`; durable cursor
        в таблице `pr_review_comments`; exit-контракт 0/1/2. Тесты:
        `test_review_pr.py` (26) (PR #110)
  - [x] **M2 (fix + reply)**: `_apply_phase` — TDD-фиксы отдельными коммитами @owner:github:andrei-shtanakov @id:review-pr-m2
        с трейлером `Review-Comment-Id`, гейты после каждой мутации (красный
        гейт откатывает фикс), один push, ответы в тредах только после
        успешного push (fix SHA / evidence опровержения), `uncertain` — никогда
        не отвечаем; лимиты rounds/comments/lines/cost/wall → NEEDS_HUMAN;
        fail-closed на dirty tree/head mismatch/force-push/push failure;
        `[no-op]`-стиль индикация в `status` (needs_human_rows). Тесты:
        `TestApplyPhase` (12) + `TestStatusSurfacing` (PR #112)
  - [x] **M3 (wiring)**: `review_pr.post_pr: off|verify|full` (дефолт off — integration_pr без конфигурации байт-в-байт прежний) @owner:github:andrei-shtanakov @id:review-pr-m3
        Режим verify = read-only
        триаж, full = checkout run-ветки → полный цикл → всегда возврат на base;
        `post_pr_wait_seconds` даёт боту время откомментировать; стадия не меняет
        exit-статус рана. Контракт внешнего вызова (0/1/2 + --json + предусловия
        mutating-режима) задокументирован в дизайн-доке; потребление отслеживается
        снаружи как maestro#147 / todo://maestro/post-pr-command.
        Тесты: `test_post_pr_stage.py` (8). **#102 закрыт** (PR #114)

### Battle-testing S2 round 3 — новые находки (issues от 2026-08-05) — ✅ отгружено в v2.16.0

Две находки из kapelle S2 round 3 (maestro-оркестрация), заведены владельцем
как issues #96/#97; maestro-стороны — maestro#122/maestro#123 (наша сторона
откомментирована там же). PR #98 и #99 влиты 2026-08-05, релиз **v2.16.0**.
Maestro может дропнуть per-workstream workaround со `spec/.gitignore` в scope
после пина spec-runner >= 2.16.

- [x] **#96 harness-gitignore-out-of-autocommit** — `spec/.gitignore` (запись #62) @owner:github:andrei-shtanakov @id:harness-gitignore-out-of-autocommit
      попадал в первый auto-commit сабтаска; maestro ex-post scope gate валил
      зелёные workstream'ы в NEEDS_REVIEW. Фикс: `stage_all_except_runtime`
      исключает файл из commit set, когда он не трекается в HEAD
      (harness-created); юзерский трекаемый файл ведёт себя по-старому и
      никогда не удаляется. Регресс: `TestHarnessGitignoreNotCommitted` (PR #98)
- [x] **#97 noop-completion-marker** — no-op задача (работа поглощена соседними сабтасками, «No changes to commit») выглядела как недоделанная: @owner:github:andrei-shtanakov @id:noop-completion-marker
      maestro показывал «DONE 4/5», оператор шёл в git-археологию. Проверено репро
      (обычный и worktree-сценарий): в state DB задача ФИКСИРУЕТСЯ success —
      «not-done» был гонкой чтения на стороне maestro (maestro#122, финальный
      опрос). Наша часть: явный маркер — колонка `attempts.no_op`, `"no_op": true`
      в `--json-result` (аддитивно, только когда true), `[no-op]` в `status`.
      Тесты: `test_noop_marker.py` + golden `json-result-single-noop.json` (PR #99)

### C2: SpecMeta contract v2 (`owner_role` + `SPEC_META_CONTRACT`) — ✅ отгружен в v2.11.0

**Был единственным пунктом, где кто-то реально ждал spec-runner.** steward закрыл свою половину C2
2026-07-15 (stale-cascade check REQ-206 на `upstream_hashes`) и держит вендоренную копию
`steward/src/steward/_vendor/spec_meta.py` на v1 с временным обходом «читаем `owner_role` из
сырого frontmatter-dict» (`steward/meta.py`). Формат принадлежит нам (DEC-003), поэтому
разблокировать может только spec-runner. Ask: `../prograph-vault/authored/notes/2026-07-15-spec-runner-specmeta-v2-handoff.md`.
Готовый спек-бандл (draft от 2026-07-05): `../_cowork_output/spec-runner-c2-specmeta-contract/`.

⚠️ **Бандл и handoff расходятся в семантике approver — при переносе бандла в репо чинить.**
`REQ-402` бандла исходит из того, что `approved_by` несёт agent-id, и предлагает добавить
отдельное поле `approver` для человека. По факту (проверено 2026-07-26) это уже не так:
`spec_commands.py:31 _approver()` возвращает `git config user.name`, а agent-id живёт в
`generated_by` (`cli_plan.py:141`, `<harness>@<model>`). То есть текущий код **уже** совпадает
с тем, что просит steward, и REQ-402 сводится к «задокументировать», а не «добавить поле».

- [x] Спека C2 перенесена в репо как `docs/superpowers/specs/2026-07-26-specmeta-contract-v2-design.md`; REQ-402 переписан под факт (`aa44e9f`, PR #54)
- [x] `SpecMeta.owner_role` — вышел как `str | None = None`, не `str = ""` (лучше ложится на `parse_owner_roles` у steward) (`35f47ff`)
- [x] `SPEC_META_CONTRACT = 2` объявлен апстримом впервые + заморожена публичная поверхность (`29d27a9`)
- [x] `docs/CONTRACTS.md` создан: матрица полей, семантика `approved_by`/`generated_by`, политика бампа (`bede398`)
- [x] Golden-фикстура в package data (`spec_runner.contract_fixtures`) + round-trip тест (`bede398`)
- [x] `upstream_hashes` и любые чужие ключи сохраняются через `SpecMeta.extra` losslessly — шире, чем просили (`d3626c5`, `b1346d2`)
- [ ] Отправить steward handoff `../prograph-vault/authored/notes/2026-07-26-steward-specmeta-v2-shipped.md` — написан, блокер снят (v2.11.0 на PyPI) @owner:github:andrei-shtanakov @id:steward-specmeta-v2-handoff

#### Follow-up: форма `owner_role` устарела по DEC-007 (найдено 2026-07-26, после релиза)

Пока шла реализация, владелец steward принял **DEC-007**: каноническая форма `owner_role` —
**одна роль-slug без `@`** (`owner_role: product`), а не `"@role[,@role]"`. Решение отменяет
форму значения из июльского ask'а, по которому мы и делали C2. Заметка:
`../prograph-vault/authored/notes/2026-07-26-steward-owner-role-singular-handoff.md`,
решение — `steward/spec/20-design.md` (DEC-007), каталог ролей — `steward/profiles/roles.yaml`.

**Подтверждено issue #125** (inbox, 2026-08-08, `slug: specmeta-owner-role-canonical`):
грамматика слага — `^[a-z][a-z0-9-]{1,31}$`, ровно одна роль, `@` в значение не входит;
множественность выражается будущими `reviewer_roles[]` / `allowed_approver_roles[]`
(добавлять в v2 только по согласованию — пока достаточно pass-through через `extra`).
Триггер steward на убийство legacy-пути `"@a,@b"` — `SPEC_META_CONTRACT = 2` в master,
он уже выполнен, так что legacy держится только нашей документацией.

**Кода это не касается.** Тип поля не меняется, spec-runner значение не разбирает и не
валидирует — «мы только носитель, семантика у steward» здесь сработало ровно как задумано.
`reviewer_roles`/`allowed_approver_roles` из DEC-007 тоже ничего не требуют: они проходят
через `extra` наравне с `upstream_hashes`.

**Но v2.11.0 уехал с отменённой формой в документации**, и golden-фикстура шипуется как
package data именно для сверки потребителями — то есть вводит в заблуждение активно.

Все три правки отгружены **PR #94 → v2.15.0 (2026-08-05)**; проверено по файлам
2026-08-10 при разборе #125. Триаж 08-10 переоткрыл их по устаревшему снимку TODO.

- [x] `docs/CONTRACTS.md` — описание `owner_role` под singular slug без `@`, DEC-007 и `steward/profiles/roles.yaml` названы SSOT формы (`46e5b0e`) @owner:github:andrei-shtanakov @id:dec007-doc-fix
- [x] `src/spec_runner/contract_fixtures/spec_meta_contract_v2.md` — `owner_role: platform` (`46e5b0e`) @owner:github:andrei-shtanakov @id:dec007-fixture
- [x] `src/spec_runner/spec.py` — инлайн-комментарий переписан под DEC-007 (`46e5b0e`) @owner:github:andrei-shtanakov @id:dec007-inline-comment
- [x] Вопрос патч-релиза снят: правка доехала до потребителей в **v2.15.0** (`1adec1f`), отдельная 2.11.1 не понадобилась @owner:github:andrei-shtanakov @id:dec007-patch-release-decision
- [ ] Обновить заметку vault'а — она всё ещё велит steward игнорировать примеры из фикстуры v2.11.0. Ждать релиза с #135: фикстура там меняется снова (`traces_to` списком, пин только прямого upstream), и переписывать заметку дважды смысла нет @owner:github:andrei-shtanakov @blocked_by:todo://spec-runner/authoring-contract-traces-pins @id:dec007-vault-note-update

### Дотегать и опубликовать v2.10.0 (2026-07-26)

Код, версия и CHANGELOG уже в master (`a24aba5`), не хватает только тега — а `publish.yml`
триггерится исключительно по `on.push.tags: ["v*"]`. Пока тега нет, `pip install spec-runner`
даёт 2.9.0 без M1…M3, и version-pin у потребителей (`spec-runner-vscode`, Maestro) нечестен.

- [x] `v2.10.0` тегнут и опубликован 2026-07-26 (тег на `58b4002`, PyPI 2.10.0)
      — на `a24aba5` либо на текущем HEAD, если решим включить `#48`/`#50`/`#51`
- [x] `publish.yml` отработал, PyPI 2.10.0, GitHub Release оформлен
- [x] CI-гард `.github/workflows/release-tag-guard.yml` — падает на master, если версия в pyproject не имеет одноимённого тега

### Баги `--spec-prefix` (найдены при dogfood C1, перепроверены 2026-07-26)

Оба воспроизводятся на master `5126476` через `_build_parser().parse_args(...)`:

| Вызов | Ожидание | Факт |
|---|---|---|
| `spec-runner --spec-prefix=phase2- run` | `spec_prefix='phase2-'` | `''` — молча проглочен |
| `spec-runner run --spec-prefix=phase2-` | `spec_prefix='phase2-'` | `'phase2-'` ✅ |
| `spec-runner spec status --spec-prefix=phase2-` | работает | `SystemExit 2`, unrecognized |

Причина (1): `--spec-prefix` объявлен и на top-level парсере, и в parent-парсере `common`
(`cli.py:896`), которым отнаследованы субкоманды. Субпарсер применяет свой `default=""`
**после** top-level и затирает значение. **Важно:** `spec-runner-vscode` ставит флаг именно
перед субкомандой, то есть настройка `spec-runner.specPrefix` сейчас не работает вообще.
Причина (2): семейство `spec` (`cli.py:1235`+) не отнаследовано от `common` и флага не имеет.

- [x] Починить проглатывание флага перед субкомандой (2026-08-05: SUPPRESS-вариант) @owner:github:andrei-shtanakov @id:spec-prefix-swallow
- [x] Дать `--spec-prefix` семейству `spec status/approve/reject/adopt/check` (2026-08-05) @owner:github:andrei-shtanakov @id:spec-prefix-spec-family
- [x] Регресс-тесты в `tests/test_spec_prefix.py`: обе позиции флага + `spec`-семейство (2026-08-05, `TestSpecPrefixFlagPositions`, 10 тестов) @owner:github:andrei-shtanakov @id:spec-prefix-regression-tests
- [x] Handoff vscode не нужен как обход: флаг починен на нашей стороне, их порядок argv @owner:github:andrei-shtanakov @id:spec-prefix-vscode-handoff
      (флаг перед субкомандой) теперь работает как есть; ре-вендор схем шёл отдельно
      (spec-runner-vscode#16, влит 2026-08-05)

### Observability (`spec_runner.obs`) — reference-имплементация ecosystem-контракта

Контракт: `maestro/contracts/observability/log-schema.json` (OTel Logs Data Model JSONL).
`spec-runner` — reference, файл `obs.py` затем вендорится в другие проекты.

- [x] **`init_logging` + `get_logger` скелет** (`ead7070`)
- [x] **Парсинг `TRACEPARENT` с graceful fallback** (`788b77f`)
- [x] **Формат timestamps: ns-string + ISO micros** (`208938c`)
- [x] **Span context manager с error chains** (`31e4cdd`)
- [x] **Redaction processor (default + env-extended blocklist)** (`b07153b`)
- [x] **`child_env()` для пропагации трейсов в subprocess** (`1cd18f9`)
- [x] **Contract-тесты против shared schema/fixtures** (`1bcf9eb`)
- [x] **Cutover `logging.py` → back-compat shim над `obs.py`** (`641b9b8`)
- [x] **Использовать `TRACEPARENT` parent span_id как initial `_span_id`** (`fa6b106`)

Дальнейшие шаги:
- [x] **Вендорить `obs.py` в Maestro / arbiter / ATP** — выполнено на стороне потребителей (Maestro M1+M2, arbiter Rust `arbiter-core::obs`, log-schema.json @ `be29b16`). Подтверждено в `../prograph-vault/authored/notes/status/2026-05-22-status.md`.
- [x] **CHANGELOG + версия следующего релиза** — `v2.1.0` тегнут 2026-05-23
- [ ] Расширить `obs.py` метриками runtime (сейчас только logs/spans) — **only-if** контракт `log-schema.json` будет расширен; неблокирующее @owner:github:andrei-shtanakov @trigger:"в log-schema.json появилась секция метрик" @id:obs-runtime-metrics
  - `maestro#log-schema-metrics` снят как blocker: принятого узла/issue с таким slug
    нет; готовность полностью определяется изменением контракта из trigger выше

### R-04 (spec-runner side): стабилизация контракта с Maestro

Maestro-сторона формализации описана в `../maestro/TODO.md` (создаёт `ExecutorState` Pydantic-модель). Наша задача — дать Maestro **стабильный контракт, к которому можно прицепиться**.

> Коммит: `273ef00`

- [x] **Документировать схему `.executor-state.json`** (2026-04-17)
  - Текущий источник истины: `src/spec_runner/state.py` (`ExecutorState`, `TaskState`, `TaskAttempt`)
  - `docs/state-schema.md` — покрыты SQLite (canonical), legacy JSON, `--json-result`, `status --json`
  - Поля помечены stable / experimental / deprecated

- [x] **Экспортировать JSON Schema для `.executor-state.json`** (2026-04-17)
  - `schemas/executor-state.schema.json` (Draft-07, матчится с `ExecutorState`/`TaskState`/`TaskAttempt`)
  - `schemas/json-result.schema.json` (Draft-07, для `--json-result` stdout)
  - Well-formedness проверяется в `tests/test_json_result_contract.py::TestSchemaWellFormed`

- [x] **Стабилизировать формат `--json-result`** (2026-04-17)
  - Описан в `docs/state-schema.md#3-spec-runner-run---json-result-stdout`
  - Эмиттер вынесен в `spec_runner.cli.build_task_json_result()`
  - Golden-тесты в `tests/test_json_result_contract.py`:
    - `TestJsonResultGolden` (4 сценария: single-success / single-failure / multi / empty)
    - `TestErrorTruncation` (200-char cap)
  - Обновление фикстур: `uv run pytest tests/test_json_result_contract.py --update-golden`
  - Любое изменение формата → обновить golden + CHANGELOG с пометкой BREAKING

- [x] **Добавить contract test-пару с Maestro** (2026-04-17)
  - `tests/fixtures/maestro-interop/` содержит:
    - `json-result-single-success.json`, `json-result-single-failure.json`, `json-result-multi.json`, `json-result-empty.json` (генерятся из golden-тестов)
    - `json-result-legacy-json-state.json` (pre-2.0 JSON state для Maestro fallback)
    - `README.md` с инструкциями
  - Maestro может копировать эти файлы и валидировать свой Pydantic-парс против них

---

## Backlog (запланировано, не начато)

### `plan --from-file` — читать описание из файла (2026-06-11) — ✅ ИСПРАВЛЕНО (PR #17)

- [x] Optional-флаг `--from-file PATH` + позиционный `description` → `nargs="?"`.
- [x] `resolve_plan_description(description, from_file)` в `cli_plan.py` (from-file
      приоритетнее; ошибки при отсутствии файла / пустом / ни-то-ни-другое).
- [x] Тесты в `tests/test_plan_full.py` (resolve + парсер).
- [x] README + CLAUDE.md задокументированы.

### Release v2.4.0 (doctor) — ✅ ЗАКРЫТО (тег `v2.4.0`, CHANGELOG `## [2.4.0] — 2026-06-12`)

doctor влит в master 2026-06-11 (PR #14, `79d4607`) и опубликован в v2.4.0. Тот же промах
«release-commit без тега» повторился на v2.10.0 — см. «Активные задачи».

### Cost tracking сломан для современного claude CLI (2026-06-11) — ✅ ИСПРАВЛЕНО (PR #16)

`spec-runner doctor --cli=claude` на реальном claude **2.1.173** дал
`cost_tracking=warn` → DEGRADED. `runner.parse_token_usage()` ищет в **stderr**
паттерны `input_tokens: …` / `cost: $…`, но текущий `claude -p` их так не отдаёт.
Следствие: `spec-runner costs`, `--budget`, `--task-budget` для claude **молча не
работают** (cost=None, бюджет не enforce-ится). doctor это и поймал — ровно тот
кейс «ложной уверенности».

- [x] Перевод на `--output-format json` через per-CLI seam (`build_cli_invocation`/
      `CliInvocation`/`parse_cli_result`/`_parse_claude_json`); JSON-режим строго для
      явного claude (`claude`/`claude-code`), остальные CLI/template/wrapper — text.
- [x] `doctor --cli=claude` → **READY**, `cost_tracking=ok` (реальный cost $0.32).
- [x] `is_error`-payload форсит неуспех; defensive fallback при невалидном JSON.
- Отложено: нативный `--max-budget-usd` cap (поддержан builder'ом, но не включён в
      runs — хард-фейлит при малом overage, ломал doctor); review-stage cost; cost для
      codex/pi/ollama (та же seam — добавить ветку). См. память `project_cost_tracking_broken`.

### BUG: DONE-статус задачи не персистится в git при auto-commit (2026-06-11) — ✅ ИСПРАВЛЕНО (`9f62ab1`, PR #15)

Найдено на тестовом прогоне `run --all --tui` (репо textkit, 19 задач): 4 задачи
выполнены и влиты в `main`, но в `tasks.md` все остались 🔄 IN_PROGRESS (БД-учёт
верный). Причина — порядок в `execution.py`:
- `execution.py:195` `post_done_hook(...)` делает `git commit` (hooks.py:388) +
  `merge` (hooks.py:465);
- `execution.py:211-212` `update_task_status(..., "done")` + `mark_all_checklist_done`
  пишут DONE в `tasks.md` **после** commit/merge → DONE не коммитится, остаётся в
  рабочей копии и затирается при создании ветки следующей задачи от `main`.
- На старте `execution.py:65` пишет IN_PROGRESS — и именно он попадает в коммит.

Следствие: `tasks.md` (читается `task next`/`status`-история/`resolve_dependencies`)
рассинхронен с `state.db`; next-task-resolution сбивается, выглядит как «зависло».

- [x] Перенесён `update_task_status("done")` + `mark_all_checklist_done` в
      `post_done_hook` **до** commit-шага (guard `tasks_file.exists()`).
- [x] Регресс-тест `TestDoneStatusPersistence` на реальном git-репо (HEAD:`tasks.md` = DONE).
- [x] Обновлены 13 execute_task-тестов под переезд функции.
- См. память `project_done_status_not_committed`.

### Minor follow-up: `--no-commit` теряет работу между задачами (2026-06-11)

Не stash (как предполагал Copilot — checkout main не падает, когда ветка == HEAD main),
а `pre_start` следующей задачи: `git checkout -- .` + `git clean -fd --exclude=spec/`
(hooks.py ~108-117) затирает незакоммиченную работу предыдущей задачи при
`create_git_branch=True` + `auto_commit=False`. Низкий приоритет (ниша `--no-commit`).
- [ ] Решить: при `auto_commit=False` не чистить рабочее дерево / не создавать ветку, @owner:github:andrei-shtanakov @id:no-commit-work-loss
      либо документировать, что `--no-commit` подразумевает `--no-branch`.

---

## Ждём от других проектов

- **Maestro → R-04**: создание `ExecutorState` Pydantic-модели; pin версии spec-runner в `maestro/pyproject.toml`
- **Maestro → R-03**: когда Maestro начнёт вызывать arbiter, spec-runner потенциально получит информацию о маршрутизации через конфиг — сейчас не блокирует
- **Maestro → `SpecRunnerConfig` gaps**: `to_executor_config()` (`maestro/models.py:1152,1184`)
  прокидывает лишь узкое подмножество `ExecutorConfig` — модели (`claude_model`,
  `review_command`, `review_model`), `personas`, `review_parallel`/`review_roles`, notify- и
  budget-ключи в Maestro-запусках всегда падают на дефолты. Находка наша, правка на стороне
  Maestro. Handoff: `../prograph-vault/authored/notes/2026-07-17-maestro-specrunnerconfig-gaps-handoff.md`
- **steward → C2**: ждёт от нас `owner_role` + `SPEC_META_CONTRACT` v2, после чего ре-вендорит
  контракт (см. «Активные задачи → C2»). Это мы блокируем steward, не наоборот.

---

## НЕ делаем здесь

- ❌ Интеграция с arbiter напрямую — spec-runner работает через Maestro, не через arbiter
- ❌ Shared type library (R-14) — ждём стабилизации R-01..R-03
