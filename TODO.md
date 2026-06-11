# TODO — spec-runner (план от 2026-04-16, обновлено 2026-05-23)

> Роль в экосистеме: единственная **работающая** кросс-проектная связка Maestro→spec-runner.
> Стратегический контекст: `../_cowork_output/roadmap/ecosystem-roadmap.md`
> Актуальный статус: `../_cowork_output/status/2026-05-22-status.md`

## Текущее состояние
- ✅ v2.2.2 зарелижен 2026-05-29 (console-прогресс в stderr для non-TUI run/watch)
- ✅ v2.2.1 зарелижен 2026-05-28 (CI off Node 20 → Node 24, obs contract test skip-guard)
- ✅ v2.2.0 зарелижен 2026-05-28 (auto-detect OpenCode/Pi CLI, architecture diagrams, green CI)
- ✅ v2.1.0 зарелижен 2026-05-23 (observability reference impl + Dependabot patches)
- ✅ v2.0.0 зарелижен 2026-04-17 (PIPE-0…5, POLISH-1…5, `spec-runner task`, webhook, crash resilience)
- ✅ CI/CD работает (`.github/workflows/ci.yml`) — единственный проект помимо ATP с CI
- ✅ `--json-result` флаг для Maestro interop
- ✅ R-04 (контракт с Maestro) заморожен 2026-04-17 — см. `docs/state-schema.md`, `schemas/`, `tests/test_json_result_contract.py`
- ✅ **Cross-project observability v1 shipped** — spec-runner reference + Maestro M1/M2 + arbiter Rust + ATP (см. `_cowork_output/status/2026-05-22-status.md`)
- ⏸️ **Статус по weekly: `frozen by design`** — нет открытых задач на спринт, ждём Maestro M4

## Правила ведения
- После каждой выполненной задачи проставь `[x]` и добавь хеш коммита
- **Semver-дисциплина**: любое изменение формата `.executor-state.json` или `--json-result` — это **breaking change**, обязательно major-bump и нотис в CHANGELOG

---

## Активные задачи

### Observability (`spec_runner.obs`) — reference-имплементация ecosystem-контракта

Контракт: `_cowork_output/observability-contract/log-schema.json` (OTel Logs Data Model JSONL).
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
- [x] **Вендорить `obs.py` в Maestro / arbiter / ATP** — выполнено на стороне потребителей (Maestro M1+M2, arbiter Rust `arbiter-core::obs`, log-schema.json @ `be29b16`). Подтверждено в `_cowork_output/status/2026-05-22-status.md`.
- [x] **CHANGELOG + версия следующего релиза** — `v2.1.0` тегнут 2026-05-23
- [ ] Расширить `obs.py` метриками runtime (сейчас только logs/spans) — **only-if** контракт `log-schema.json` будет расширен; неблокирующее

### R-04 (spec-runner side): стабилизация контракта с Maestro

Maestro-сторона формализации описана в `../Maestro/TODO.md` (создаёт `ExecutorState` Pydantic-модель). Наша задача — дать Maestro **стабильный контракт, к которому можно прицепиться**.

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

### `plan --from-file` — читать описание из файла (2026-06-11)

Сейчас `spec-runner plan [--full] "<описание>"` принимает описание **только** как
позиционную строку (`cli.py`: `plan_parser.add_argument("description", ...)`;
`cmd_plan` использует `args.description` как есть). Обходной путь —
`plan --full "$(cat file.md)"`. Нужна честная поддержка файла.

- [ ] Добавить optional-флаг `--from-file PATH` к `plan`-сабпарсеру в `cli.py`.
- [ ] Сделать позиционный `description` необязательным (`nargs="?"`); в `cmd_plan`
      (`cli_plan.py`) ветка: если задан `--from-file` — читать `Path(args.from_file).read_text()`
      как `description`; иначе использовать позиционный; ошибка, если не задано ни то
      ни другое (и определить приоритет, если заданы оба).
- [ ] Тест в `tests/test_plan_full.py`: `--from-file` читает содержимое и оно попадает
      в `build_generation_prompt`; ошибки при отсутствии файла / пустом вводе.
- [ ] README + CLAUDE.md: задокументировать флаг.
- Отдельным PR (не блокирует v2.4.0). Запрошено пользователем 2026-06-11.

### Release v2.4.0 (doctor) — см. память `project_pending_v240_release`

doctor влит в master 2026-06-11 (PR #14, `79d4607`), но версия в pyproject всё ещё
`2.3.1`, на PyPI doctor нет. После теста — bump → `v2.4.0`, CHANGELOG, тег, publish.

### Cost tracking сломан для современного claude CLI (2026-06-11)

`spec-runner doctor --cli=claude` на реальном claude **2.1.173** дал
`cost_tracking=warn` → DEGRADED. `runner.parse_token_usage()` ищет в **stderr**
паттерны `input_tokens: …` / `cost: $…`, но текущий `claude -p` их так не отдаёт.
Следствие: `spec-runner costs`, `--budget`, `--task-budget` для claude **молча не
работают** (cost=None, бюджет не enforce-ится). doctor это и поймал — ровно тот
кейс «ложной уверенности».

- [ ] claude CLI имеет `--output-format json` (single result) с полями usage /
      `total_cost_usd`. Перевести получение cost на JSON вместо stderr-regex.
- ⚠️ Это **не просто regex-твик**: при `--output-format json` весь ответ обёрнут в
      JSON (текст в поле `result`), значит меняется и детект маркера `TASK_COMPLETE`,
      и обработка вывода в `runner`/`execution`. Нужно: либо парсить JSON и извлекать
      и `result` (для маркера/контента), и `usage`/`total_cost_usd` (для cost); либо
      добавить отдельный лёгкий usage-запрос. Оценить объём перед началом.
- [ ] После фикса `doctor --cli=claude` должен давать `cost_tracking=ok` и READY.
- [ ] Аналогично проверить doctor'ом codex/pi/ollama — у них cost, вероятно, тоже не
      парсится (та же причина). См. память `project_cost_tracking_broken`.

---

## Ждём от других проектов

- **Maestro → R-04**: создание `ExecutorState` Pydantic-модели; pin версии spec-runner в `Maestro/pyproject.toml`
- **Maestro → R-03**: когда Maestro начнёт вызывать arbiter, spec-runner потенциально получит информацию о маршрутизации через конфиг — сейчас не блокирует

---

## НЕ делаем здесь

- ❌ Интеграция с arbiter напрямую — spec-runner работает через Maestro, не через arbiter
- ❌ Shared type library (R-14) — ждём стабилизации R-01..R-03
