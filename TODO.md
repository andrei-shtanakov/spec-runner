# TODO — spec-runner (план от 2026-04-16)

> Роль в экосистеме: единственная **работающая** кросс-проектная связка Maestro→spec-runner.
> Стратегический контекст: `../_cowork_output/roadmap/ecosystem-roadmap.md`
> Актуальный статус: `../_cowork_output/status/2026-04-10-status.md`

## Текущее состояние
- ✅ v2.0.0 зарелижен (PIPE-0…5, POLISH-1…5, `spec-runner task`, webhook, crash resilience)
- ✅ CI/CD работает (`.github/workflows/ci.yml`) — единственный проект помимо ATP с CI
- ✅ `--json-result` флаг для Maestro interop
- ⚠️ Контракт с Maestro держится на неформальном парсинге `.executor-state.json`

## Правила ведения
- После каждой выполненной задачи проставь `[x]` и добавь хеш коммита
- **Semver-дисциплина**: любое изменение формата `.executor-state.json` или `--json-result` — это **breaking change**, обязательно major-bump и нотис в CHANGELOG

---

## Активные задачи

### R-04 (spec-runner side): стабилизация контракта с Maestro

Maestro-сторона формализации описана в `../Maestro/TODO.md` (создаёт `ExecutorState` Pydantic-модель). Наша задача — дать Maestro **стабильный контракт, к которому можно прицепиться**.

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

## Ждём от других проектов

- **Maestro → R-04**: создание `ExecutorState` Pydantic-модели; pin версии spec-runner в `Maestro/pyproject.toml`
- **Maestro → R-03**: когда Maestro начнёт вызывать arbiter, spec-runner потенциально получит информацию о маршрутизации через конфиг — сейчас не блокирует

---

## НЕ делаем здесь

- ❌ Интеграция с arbiter напрямую — spec-runner работает через Maestro, не через arbiter
- ❌ Shared type library (R-14) — ждём стабилизации R-01..R-03
