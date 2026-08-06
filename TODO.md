# TODO — spec-runner (план от 2026-04-16, обновлено 2026-07-26)

> Роль в экосистеме: единственная **работающая** кросс-проектная связка Maestro→spec-runner.
> Стратегический контекст: `../prograph-vault/authored/notes/ecosystem-roadmap.md`
> Актуальный статус: `../prograph-vault/authored/notes/status/2026-07-08-status.md`
>
> Открытые пункты размечены инлайн-тегами `@owner:` / `@blocked_by:` / `@trigger:` по
> формату из `../_cowork_output/2026-07-26-plan-fields-and-todo-coverage-handoff.md` §3.
> Теги опциональны и исключены из ключа идентичности пункта в Robin (robin-runtime#27),
> отсутствие тега значит «неизвестно» — придумывать значение не надо.

## Текущее состояние
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

### Battle-testing round 4 — находки с v2.16.0 (issues от 2026-08-06, run d4d33ad0) — ✅ 3/4 отгружено в v2.17.0 (#102 ждёт решения владельца)

Четыре находки прогона TASK-007 на kapelle (F-21…F-24).

- [x] **#103 commit-provenance** — весь код фичи уезжал в коммит «code review fixes»,
      таск-коммит получал только флип чекбокса в tasks.md (история лжёт о происхождении
      кода). Фикс: exec-работа коммитится под таск-лейблом ДО review; review-коммит несёт
      только свою дельту; no_op-детекция (#97) учитывает pre-review коммит.
      Тесты: `test_commit_provenance.py` (PR #105) @owner:andrei
- [x] **#104 run-summary-delta** — `Execution summary completed=2` на однозадачном ране:
      summary печатал кумулятивные счётчики executor_meta как итог рана (то же в
      run_complete-нотификации, audit-записи и failed_attempts). Фикс: снапшот до рана,
      в summary — дельта. Тесты: `test_run_summary.py` (PR #106) @owner:andrei
- [x] **#101 pr-opened-notification** — human-merge-гейт зависел от смотрящего в терминал.
      Наша сторона: событие `pr_opened` в существующем notify-механизме
      (Telegram/webhook, в дефолтном `notify_on`); диспетчер может съедать webhook.
      Вопрос агрегации (dispatcher-консоль поверх maestro+spec-runner) остаётся
      за экосистемой (PR #107) @owner:andrei
- [ ] **#102 review-bot-loop** — **решение владельца принято 2026-08-06**: реализуем в
      spec-runner как отдельную resumable-команду `spec-runner review-pr <url-or-number>`
      + опциональную post-PR стадию (НЕ maestro-only hook и НЕ inline в `run`); Maestro
      позже получит тонкий hook `PR_CREATED → post_pr_command → PR_REVIEWED/NEEDS_REVIEW`
      и вызовет ту же команду. Граница: transport+verify/fix/reply loop → spec-runner;
      когда/для какого PR → владелец lifecycle; approval policy → maestro approver_cmd
      (maestro#137, не смешивать approval с mutation). Дизайн-док с state machine,
      нормативными ограничениями (opt-in, allowed bots, verdict valid/refuted/uncertain,
      uncertain→human, TDD-фиксы отдельными коммитами с provenance, гейты до push, ответ
      только после push с SHA, лимиты на всё, fail-closed, no auto-merge/approve) и фазами
      M1 read-only → M2 fix+reply → M3 wiring:
      `docs/superpowers/specs/2026-08-06-review-pr-loop-design.md` @owner:andrei
  - [x] **M1 (read-only)**: команда `review-pr` — collect (gh CLI, allowed-bots
        фильтр) → verify (агент, fail-closed к uncertain, вердикт аннулируется
        при мутации дерева верификатором) → отчёт text/`--json`; durable cursor
        в таблице `pr_review_comments`; exit-контракт 0/1/2. Тесты:
        `test_review_pr.py` (26) (PR #110) @owner:andrei
  - [x] **M2 (fix + reply)**: `_apply_phase` — TDD-фиксы отдельными коммитами
        с трейлером `Review-Comment-Id`, гейты после каждой мутации (красный
        гейт откатывает фикс), один push, ответы в тредах только после
        успешного push (fix SHA / evidence опровержения), `uncertain` — никогда
        не отвечаем; лимиты rounds/comments/lines/cost/wall → NEEDS_HUMAN;
        fail-closed на dirty tree/head mismatch/force-push/push failure;
        `[no-op]`-стиль индикация в `status` (needs_human_rows). Тесты:
        `TestApplyPhase` (12) + `TestStatusSurfacing` (PR #112) @owner:andrei
  - [ ] **M3 (wiring)**: опциональная post-PR стадия после integration_pr,
        документированный exit-code контракт для maestro-хука @owner:andrei

### Battle-testing S2 round 3 — новые находки (issues от 2026-08-05) — ✅ отгружено в v2.16.0

Две находки из kapelle S2 round 3 (maestro-оркестрация), заведены владельцем
как issues #96/#97; maestro-стороны — maestro#122/maestro#123 (наша сторона
откомментирована там же). PR #98 и #99 влиты 2026-08-05, релиз **v2.16.0**.
Maestro может дропнуть per-workstream workaround со `spec/.gitignore` в scope
после пина spec-runner >= 2.16.

- [x] **#96 harness-gitignore-out-of-autocommit** — `spec/.gitignore` (запись #62)
      попадал в первый auto-commit сабтаска; maestro ex-post scope gate валил
      зелёные workstream'ы в NEEDS_REVIEW. Фикс: `stage_all_except_runtime`
      исключает файл из commit set, когда он не трекается в HEAD
      (harness-created); юзерский трекаемый файл ведёт себя по-старому и
      никогда не удаляется. Регресс: `TestHarnessGitignoreNotCommitted` (PR #98) @owner:andrei
- [x] **#97 noop-completion-marker** — no-op задача (работа поглощена соседними
      сабтасками, «No changes to commit») выглядела как недоделанная: maestro
      показывал «DONE 4/5», оператор шёл в git-археологию. Проверено репро
      (обычный и worktree-сценарий): в state DB задача ФИКСИРУЕТСЯ success —
      «not-done» был гонкой чтения на стороне maestro (maestro#122, финальный
      опрос). Наша часть: явный маркер — колонка `attempts.no_op`, `"no_op": true`
      в `--json-result` (аддитивно, только когда true), `[no-op]` в `status`.
      Тесты: `test_noop_marker.py` + golden `json-result-single-noop.json` (PR #99) @owner:andrei

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
- [ ] Отправить steward handoff `../prograph-vault/authored/notes/2026-07-26-steward-specmeta-v2-shipped.md` — написан, блокер снят (v2.11.0 на PyPI) @owner:andrei

#### Follow-up: форма `owner_role` устарела по DEC-007 (найдено 2026-07-26, после релиза)

Пока шла реализация, владелец steward принял **DEC-007**: каноническая форма `owner_role` —
**одна роль-slug без `@`** (`owner_role: product`), а не `"@role[,@role]"`. Решение отменяет
форму значения из июльского ask'а, по которому мы и делали C2. Заметка:
`../prograph-vault/authored/notes/2026-07-26-steward-owner-role-singular-handoff.md`,
решение — `steward/spec/20-design.md` (DEC-007), каталог ролей — `steward/profiles/roles.yaml`.

**Кода это не касается.** Тип поля не меняется, spec-runner значение не разбирает и не
валидирует — «мы только носитель, семантика у steward» здесь сработало ровно как задумано.
`reviewer_roles`/`allowed_approver_roles` из DEC-007 тоже ничего не требуют: они проходят
через `extra` наравне с `upstream_hashes`.

**Но v2.11.0 уехал с отменённой формой в документации**, и golden-фикстура шипуется как
package data именно для сверки потребителями — то есть вводит в заблуждение активно.

- [ ] `docs/CONTRACTS.md:104` — переписать описание `owner_role` под singular slug без `@`; сослаться на DEC-007 и `steward/profiles/roles.yaml` как SSOT формы @owner:andrei
- [ ] `src/spec_runner/contract_fixtures/spec_meta_contract_v2.md:15` — `owner_role: '@platform,@sre'` → одиночный slug @owner:andrei
- [ ] `src/spec_runner/spec.py:198` — инлайн-комментарий `# CODEOWNERS role(s), "@role[,@role]"` под ту же форму @owner:andrei
- [ ] Решить, резать ли 2.11.1 ради того, чтобы исправленная фикстура доехала до потребителей, или ждать следующего релиза @owner:andrei @trigger:"фикстура — package data, потребители сверяются с ней"
- [ ] Обновить заметку vault'а после фикса — сейчас она велит steward игнорировать примеры из фикстуры v2.11.0 @owner:andrei @blocked_by:spec-runner#dec007-doc-fix

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

- [x] Починить проглатывание флага перед субкомандой (2026-08-05: SUPPRESS-вариант) @owner:andrei
- [x] Дать `--spec-prefix` семейству `spec status/approve/reject/adopt/check` (2026-08-05) @owner:andrei
- [x] Регресс-тесты в `tests/test_spec_prefix.py`: обе позиции флага + `spec`-семейство (2026-08-05, `TestSpecPrefixFlagPositions`, 10 тестов) @owner:andrei
- [x] Handoff vscode не нужен как обход: флаг починен на нашей стороне, их порядок argv
      (флаг перед субкомандой) теперь работает как есть; ре-вендор схем шёл отдельно
      (spec-runner-vscode#16, влит 2026-08-05) @owner:andrei

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
- [ ] Расширить `obs.py` метриками runtime (сейчас только logs/spans) — **only-if** контракт `log-schema.json` будет расширен; неблокирующее @owner:andrei @blocked_by:maestro#log-schema-metrics @trigger:"в log-schema.json появилась секция метрик"

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
- [ ] Решить: при `auto_commit=False` не чистить рабочее дерево / не создавать ветку, @owner:andrei
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
