# Предложения по доработке проекта executor

## 1. Текущие узкие места

### 1.1. Последовательное выполнение (главный bottleneck)

DAG-зависимости парсятся корректно, но выполнение **строго последовательное** — `task = ready_tasks[0]`. Если 10 независимых P0-задач по 30 мин каждая — это 300 мин вместо 30.

- `executor.py:1801` — всегда берёт первую ready-задачу
- `task.py:369` — фильтрация готовых задач есть, но parallel dispatch нет

### 1.2. "Тупой" retry без обучения на ошибках

- Фиксированная задержка 5 сек (нет exponential backoff)
- Нет классификации ошибок (transient vs permanent vs rate-limit)
- Контекст предыдущих попыток обрезается до 30KB (`executor.py:810`)
- Claude видит усечённый текст ошибки, но не структурированный анализ причины

### 1.3. Отсутствие трекинга токенов и стоимости

- Нет подсчёта input/output tokens
- Нет аккумуляции стоимости по задачам/попыткам
- HTTP callback (`executor.py:90-98`) не включает cost info

### 1.4. Хрупкость state файла

- Полная перезапись JSON на каждую попытку (`executor.py:495-521`)
- Нет WAL/журналирования — crash mid-write = потеря данных
- Вся история attempts хранится в памяти без pruning

### 1.5. Review — автоматический, без HITL

- Маркер-based (`REVIEW_PASSED`/`REVIEW_FIXED`/`REVIEW_FAILED`)
- Нет human approval step
- Review prompt не включает контекст задачи, чеклист, предыдущие ошибки

### 1.6. Тестирование — покрыто ~10%

- 41 тест — только config path resolution
- ZERO тестов на: retry, DAG, state recovery, hooks, review, prompt rendering, git ops

---

## 2. Заимствования из других проектов монорепы

### 2.1. Параллельное выполнение из Maestro (scope conflict prevention)

- **Проблема:** sequential execution при наличии DAG
- **Взять из Maestro:**
  - Topological sort + level assignment для определения параллельных задач
  - Scope conflict detection (glob-pattern matching) — предотвращение конфликтов файлов
  - Concurrency limit (`max_concurrent`) — контролируемый параллелизм
- **Реализация:** asyncio + semaphore для concurrent subprocess execution
- **Объём:** ~150-200 строк в новом модуле `parallel.py`
- **Не брать:** multi-process worktree decomposition — overkill для executor

### 2.2. Intelligent retry из hive (reflexion loop)

- **Проблема:** retry повторяет то же самое, Claude не учится на ошибках
- **Взять из hive:**
  - Structured feedback: 4 вердикта вместо binary (ACCEPT / RETRY / REPLAN / ESCALATE)
  - Error categorization: transient → backoff, permanent → fail fast, rate-limit → exponential backoff
  - Контекст между попытками: structured JSON (что пробовали, что не сработало, гипотеза)
- **Реализация:** заменить `range(attempts)` loop на state machine с вердиктами
- **Объём:** ~100 строк в `retry_strategy.py`
- **Не брать:** полный reflexion с LLM-judge — executor ориентирован на тесты как критерий успеха

### 2.3. Cost tracking из Maestro (per-task USD)

- **Проблема:** нулевая видимость стоимости
- **Взять из Maestro:**
  - Token counting через anthropic SDK (input_tokens, output_tokens из response)
  - Cost accumulator per task + per attempt в state
  - Budget enforcement: прекращение задачи при превышении порога
- **Реализация:** обёртка вокруг subprocess output parsing (Claude CLI выводит token usage)
- **Объём:** ~50 строк в существующем `record_attempt()`
- **Не брать:** полную SSE cost dashboard — достаточно summary в progress log

### 2.4. SQLite state из arbiter (crash-safe persistence)

- **Проблема:** JSON state file хрупок, нет WAL, нет incremental updates
- **Взять из arbiter:**
  - SQLite с WAL mode для атомарных записей
  - Таблицы: tasks, attempts, outcomes (вместо монолитного JSON)
  - Retry-on-lock backoff (50ms, 100ms, 200ms)
- **Реализация:** заменить `ExecutorState` JSON на SQLite через `aiosqlite`
- **Объём:** ~200 строк в `state_db.py`
- **Не брать:** полную schema (decisions, agent_stats) — executor проще

### 2.5. Plan review из plannotator (HITL для критических задач)

- **Проблема:** review автоматический, нет human-in-the-loop
- **Взять из plannotator:**
  - Опциональный HITL gate для P0/critical задач
  - Annotation UI для review результатов (approve/deny/comment)
  - Блокирующий hook с timeout
- **Реализация:** optional `--hitl-review` flag → запуск plannotator перед commit
- **Объём:** ~30 строк интеграции (plannotator уже standalone)
- **Не брать:** полный annotation workflow — executor ориентирован на автоматизацию

### 2.6. Structured logging из atp-platform

- **Проблема:** text-based logs, нет structured events
- **Взять из atp-platform:**
  - structlog с contextual fields (task_id, attempt, duration, status)
  - JSON log format для machine parsing
  - Per-task metrics: tokens, cost, duration, retries
- **Объём:** ~40 строк замены print → structlog
- **Не брать:** OpenTelemetry tracing — overkill для CLI tool

---

## 3. Quick wins (высокий импакт, низкие усилия)

| # | Что сделать | Усилия | Импакт |
|---|------------|--------|--------|
| 1 | Структурированные коды ошибок (SYNTAX, TIMEOUT, DEPENDENCY, RATE_LIMIT) | 2ч | Умный retry, лучшая диагностика |
| 2 | Улучшить retry context — показать Claude что именно пробовали и почему не сработало | 3ч | Меньше повторных ошибок, экономия токенов |
| 3 | Token counting из stdout Claude CLI | 2ч | Видимость стоимости |
| 4 | Включить task description + checklist в review prompt | 1ч | Качество review |
| 5 | 10 базовых тестов на retry/DAG/state | 4ч | Защита от регрессий |

---

## 4. Что НЕ брать

| Паттерн | Источник | Причина отказа |
|---------|----------|---------------|
| Multi-process worktrees | Maestro | executor — single-directory tool, worktrees усложнят git flow |
| Goal-driven graph generation | hive | executor работает с готовыми spec-файлами, не генерирует планы |
| MCP tools ecosystem | hive, klaw.sh | executor вызывает CLI-агентов, не управляет tools напрямую |
| Container isolation | nanoclaw | executor доверяет local environment, sandbox не нужен |
| Multi-channel gateway | openclaw | executor — CLI tool, не messaging platform |
| Policy engine routing | arbiter | executor знает какой CLI использовать из config |
| vtable extensibility | nullclaw | Python + config-driven подход проще и достаточен |

---

## 5. TUI Kanбан-дашборд (оригинальная идея)

### Концепция

Kanban-доска в терминале — колонки соответствуют жизненному циклу задач executor'а. Нигде в монорепе такого нет (hive — graph view, klaw.sh — status cards, manbot — web dashboard). TUI-канбан для spec-runner — оригинальный подход.

### Макет

```
┌─ BLOCKED (2) ──┬─ TODO (5) ─────┬─ IN PROGRESS ──┬─ DONE (8) ─────┬─ FAILED (1) ──┐
│                │                │                │                │               │
│ TASK-012 🔴P0  │ TASK-003 🔴P0  │ TASK-007 ⬜P1  │ TASK-001 ✅    │ TASK-009 ❌   │
│ Auth service   │ API endpoints  │ ▓▓▓▓░░ 67%     │ 2m31s · $0.12  │ 3 attempts    │
│ ← TASK-007     │                │ attempt 2/3    │                │ TIMEOUT       │
│                │ TASK-004 ⬜P1  │                │ TASK-002 ✅    │               │
│ TASK-015 ⬜P2  │ DB migrations  │                │ 1m05s · $0.04  │               │
│ Tests          │                │                │                │               │
│ ← TASK-012     │ TASK-006 🟡P2  │                │ ...            │               │
│                │ Docs update    │                │                │               │
├────────────────┴────────────────┴────────────────┴────────────────┴───────────────┤
│ Total: 16 tasks │ Tokens: 45.2K in / 12.8K out │ Cost: $0.84 │ Elapsed: 14m32s  │
└──────────────────────────────────────────────────────────────────────────────────┘
```

### Карточка задачи показывает

- **BLOCKED**: task ID, приоритет, название, кто блокирует (← TASK-XXX)
- **TODO**: task ID, приоритет, название
- **IN PROGRESS**: progress bar, текущая попытка (attempt 2/3), elapsed time
- **DONE**: время выполнения, стоимость
- **FAILED**: количество попыток, тип ошибки (TIMEOUT / SYNTAX / DEPENDENCY)

### Footer — агрегированные метрики

- Total tasks + breakdown по статусам
- Token usage (input/output)
- Accumulated cost ($)
- Elapsed wall-clock time

### Технический стек

- **Textual** (Python) — тот же стек что hive (TextualUI); rich-виджеты, async, mouse support
- **Интеграция**: читает `ExecutorState` (JSON/SQLite) + подписка на события через file watcher или event bus
- **Объём**: ~300-400 строк в новом модуле `tui_dashboard.py`
- **Запуск**: `spec-runner dashboard` или `spec-runner run --all --tui`

### Возможные расширения (не для MVP)

- Клик на карточку → детали задачи (prompt, output, errors)
- Горячие клавиши: `r` retry failed, `s` skip, `p` pause
- DAG-граф зависимостей (как `spec-task graph`, но live)
- Фильтр по приоритету / тегам

### Почему это хорошо для executor

1. executor — CLI tool, пользователь уже в терминале
2. Задачи имеют естественные Kanban-состояния
3. При parallel execution (Phase 2) визуализация прогресса критична
4. Textual даёт mouse + keyboard, не требует браузера
5. Дифференцирует от конкурентов (все делают web dashboards)

---

## 6. Приоритетный roadmap (revised)

### Phase 0: Foundation — декомпозиция и тесты (2 дня)

**Почему это первое:** executor.py — 2313 строк монолит. Добавлять модули
(parallel.py, retry_strategy.py, state_db.py) к монолиту без тестов — путь к
"работает, но боюсь трогать". Декомпозиция + тесты делают все последующие фазы
безопасными и быстрыми.

**Декомпозиция executor.py → модули:**

| Модуль | Содержимое | ~строк |
|--------|-----------|--------|
| `config.py` | `ExecutorConfig`, загрузка YAML, path resolution | 250 |
| `state.py` | `ExecutorState`, `TaskState`, `TaskAttempt`, persistence | 200 |
| `prompt.py` | `build_task_prompt()`, template rendering, context formatting | 300 |
| `hooks.py` | pre/post hooks, git operations (branch, commit, merge) | 400 |
| `runner.py` | subprocess execution, output parsing, TASK_COMPLETE/FAILED detection | 300 |
| `executor.py` | CLI + оркестрация (main loop, retry coordination) | ~500 |

**Тесты (40-50 штук, параллельно с декомпозицией):**
- config: path resolution, YAML merge, CLI override precedence
- state: save/load/recovery, attempt recording, consecutive failure tracking
- prompt: template rendering, error context truncation, checklist formatting
- hooks: execution sequence, git branch naming, failure handling
- runner: TASK_COMPLETE/FAILED detection, timeout, output parsing
- task: DAG resolution, dependency promotion, priority ordering
- retry: attempt counting, error forwarding, API error detection

### Phase 1: Reliability (1-2 дня)
- SQLite state вместо JSON (теперь безопасно — state.py выделен, тесты есть)
- Structured error codes (SYNTAX, TIMEOUT, DEPENDENCY, RATE_LIMIT)
- Улучшенный retry context (structured JSON: что пробовали, почему не сработало)

### Phase 2: Performance (2-3 дня)
- Parallel execution independent задач (asyncio + semaphore в runner.py)
- Token/cost tracking (парсинг stdout Claude CLI)
- Budget enforcement (per-task и глобальный лимиты)

### Phase 3: Visibility (по необходимости)
- Structured logging (structlog, ~40 строк — quick win, делать сразу)
- TUI Kanban dashboard — **только после Phase 2**, когда параллельное выполнение
  делает визуализацию прогресса реально нужной

### Phase 4: Quality (1-2 дня)
- HITL review integration (из plannotator)
- Review prompt с контекстом задачи, чеклистом, предыдущими ошибками

---

## 7. Что НЕ менять в Phase 0

Декомпозиция — **строго рефакторинг**, без изменения поведения:
- Не менять формат state JSON
- Не менять CLI интерфейс и аргументы
- Не менять формат spec/tasks.md
- Не менять exit codes и stdout output
- Все существующие тесты должны проходить
