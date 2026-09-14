---
schema: discovery-brief
schema_version: 1
spec_stage: discovery
status: draft
generated_by: discovery-runtime
generated_at: '2026-09-14T04:52:30Z'
validation: pass
interview:
  frame: engineer
  sessions:
  - participant_role: engineer
coverage:
  systems: covered
  interfaces: covered
  constraints: covered
  arch_preferences: covered
  risks: covered
  feasibility_review: covered
  gate_passed: true
open_questions: 3
blocking_open_questions: 0
conflicts: 0
traces_to:
- discovery-brief-customer.md
---

# Discovery Brief — andrei-shtanakov/spec-runner (engineer-фрейм)

## Constraints

- **CON-01** Тесты не исполняют платного агента: conftest-«пояс» поднимает `PaidBinaryReached` на argv известного агента; fake CLI — `tests/fixtures/fake_claude.sh` через `claude_command`; CI гоняет `uv run pytest tests/ -q -m "not slow"`.
- **CON-02** Тулинг и доставка: Python 3.11+, ruff (line length 100), `ruff format --check`, mypy strict и pyrefly; изменения идут через governance репо (`spec_governance: strict`, `execution_mode: tdd`, `review_policy: required`, integration PR); `harness_guard: strict` запрещает задаче трогать `spec-runner.config.yaml` и `spec/.tdd-evidence/*`.
- **CON-03** Факт: `_resolve_config_path` ищет YAML относительно CWD, а не `project_root`. Значит, поля без CLI-представления (`spec_governance`, `review_policy`, `execution_mode`, `harness_guard`, `commands`) воспроизводятся у child только если он запущен с `cwd=project_root`, а родитель прочитал тот же файл — сегодня родитель, запущенный с `--project-root` из чужой директории, читает чужой YAML или дефолты.
- **CON-04** `--change` и `--spec-prefix` взаимоисключающие — проверяется дважды (`SystemExit` в CLI и `ConfigError` в `__post_init__`); child получает ровно один namespace.
- **CON-05** E2E обязан запускать настоящий CLI entry point из тестового окружения (`python -m spec_runner` либо console-script uv-venv), а не полагаться на `spec-runner` в PATH оператора; иначе тест зелёный только на одной машине.

## System Assessment

- **S-01** `src/spec_runner/mcp_server.py` — `MCPServer("spec-runner")`, 8 tools. Единственный launch-контекст — модульная глобаль `_launch_stop_config`, которую выставляет только `run_server(config)`; её читает лишь `stop`. Остальные 7 tools вызывают `_build_config(spec_prefix)` = `load_config_from_yaml()` + `argparse.Namespace(project_root="", ...)`. Состояние: хрупко — это и есть дефект #485.
- **S-02** `src/spec_runner/config.py` `ExecutorConfig`: `__post_init__` резолвит `project_root`, namespace-ит `state_file`/`logs_dir` по `spec_prefix` либо `change_id` (взаимоисключающие, `ConfigError`), свойства `spec_dir`, `stop_file = spec_dir/.executor-stop`, `tasks_file`, `spec_lock_file`, `prompts_dir`; `_validate_change_id`. Состояние: стабильно.
- **S-03** `config.py` `load_config_from_yaml(config_path=None)` → `_resolve_config_path()` → модульные константы `CONFIG_FILE = Path("spec-runner.config.yaml")` и `LEGACY_CONFIG_FILE = Path("spec/executor.config.yaml")` — пути относительны CWD процесса, `project_root` в поиске не участвует. Состояние: стабильно, но это tacit-ловушка для запуска из чужой директории.
- **S-04** `src/spec_runner/cli.py`: общий родительский парсер `common` (SUPPRESS-дефолты + `_COMMON_DEFAULTS` + assert-гвард дрейфа) с `--project-root`, `--change`, `--spec-prefix`, `--max-retries`, `--timeout`, `--no-tests/--no-branch/ --no-commit/--no-review`, `--integration-pr`, `--hitl-review`, `--callback-url`, `--log-level`, `--log-json`, `--budget`, `--task-budget`; `build_config(yaml, args)`; `cmd_mcp` в `cli_info.py` вызывает `run_server(config)` с resolved config. Состояние: стабильно.
- **S-05** Путь запуска `cli.py`: `cmd_run` → `_acquire_run_lock` (`ExecutorLock` на `state_file.with_suffix(".lock")`, обход `--force`) → `_run_tasks` → `_run_tasks_inner`: `_enforce_spec_governance`, `_enforce_clean_spec`, `_enforce_untracked_state`, затем `clear_stop_file(config)` (строка 831) и только потом открытие `ExecutorState`; stop потребляется между задачами через `check_stop_requested` + `clear_stop_file` (строки 1055, 1274). Состояние: стабильно, но порядок «lock → clear» и есть гонка stop.
- **S-06** `src/spec_runner/state.py`: `ExecutorLock` (`acquire`/`release`/ `_read_lock_info`: pid, started, alive), `ExecutorState` и `for_read` (#337: чтение не создаёт state DB), `clear_stop_file`, `check_stop_requested`; `cmd_stop` в `cli_info.py` пишет marker с меткой времени. Состояние: стабильно.
- **S-07** Тесты: `tests/test_mcp.py` (`TestMCPStop` с launch-namespace тестами из #484, `TestMCPRunTask` с governance-гейтом и monkeypatch `Popen`), `tests/test_lazy_mcp_import.py`, `tests/test_mcp_v2_wire.py`, `tests/test_e2e.py` + `tests/fixtures/fake_claude.sh`; conftest-«пояс» `PaidBinaryReached`/`BELT_PROBE_COMMAND` против платного агента. Состояние: стабильно.
- **S-08** `src/spec_runner/__init__.py`: ленивый экспорт `mcp_run_server` через модульный `__getattr__`; `mcp` — опциональная зависимость. Состояние: стабильно, охраняется `test_lazy_mcp_import`.
- **S-09** Конфиг самого репо `spec-runner.config.yaml`: `spec_governance: strict`, `execution_mode: tdd`, `tdd_runner: pytest`, `review_policy: required`, `integration_pr: true`, `harness_guard: strict` с `harness_files` (сам yaml + `spec/.tdd-evidence/*`), `on_task_failure: stop`. Состояние: стабильно; harness_files — то, что «боятся трогать» по политике.
- **S-10** Раскладка namespace-ов: плоский `spec/` (tasks.md, `.executor-state.db`), prefix-namespace `spec/<prefix>tasks.md` + `.executor-<prefix>state.db`, change-as-folder `spec/changes/<id>/` со своими tasks/state/logs/stop; lock выводится из пути state, поэтому namespace-ы могут идти параллельно. Состояние: стабильно.

## Interfaces

- **IF-01** MCP stdio tools: каждый принимает `spec_prefix: str = ""`; документированы в README §MCP Server. Потребители: Claude Code через `.mcp.json`, любой MCP-клиент. Версионируемость: нет; отказ при противоречащем prefix — видимое изменение контракта, требует README (FR-08).
  traces: [S-01]
- **IF-02** Spawn child в `run_task`: `subprocess.Popen(["spec-runner", "run", "--task", id] + ["--spec-prefix", p]?, stdout=PIPE, stderr=PIPE)` — без `cwd`, без `--project-root`/`--change`, PIPE никто не читает, `spec-runner` берётся из PATH. Недокументировано, «так исторически».
  traces: [S-01, S-05]
- **IF-03** Stop-marker `<spec_dir>/.executor-stop`: писатели — `cmd_stop`, MCP `stop`, ручной `touch`; читатель — цикл `run` между задачами; очищается на старте прогона и при потреблении. Документирован (CLI help, README).
  traces: [S-05, S-06]
- **IF-04** Lock `<state_file>.lock` с JSON (pid, started); per-namespace, потому что state namespace-ится; обход `--force`; занятый lock → `exit 1` с логом «Another executor is already running». Документирован сообщениями CLI.
  traces: [S-05, S-06]
- **IF-05** Приоритет конфигурации в `build_config`: CLI-флаг > YAML > дефолт dataclass; флаги субтрактивные (`--no-*`) или позитивные-только (`--integration-pr`), обратных нет; YAML ищется относительно CWD (S-03). «Так исторически».
  traces: [S-03, S-04]
- **IF-06** Programmatic `spec_runner.mcp_run_server(config=None)` — ленивый атрибут пакета; потребители — `cmd_mcp` и внешние вызывающие (P-02 customer-брифа); охраняется `test_lazy_mcp_import`. Стабильный публичный контракт.
  traces: [S-08]
- **IF-07** Governance-гейт `spec_run_gate_ok(config)` читает frontmatter tasks.md (status approved при strict); используется `run_task` и CLI `run/watch/retry`. Документирован в docstring `run_task`.
  traces: [S-05, S-09]

## Architecture Preferences

- **AP-01** Единый launch-scope holder (типизированный, например `MCPLaunchScope` с resolved `ExecutorConfig`) вместо `_launch_stop_config`; все 8 tools берут config через один helper, который при tool-level `spec_prefix`, противоречащем scope, отказывает; `run_server(None)` строит config как прежде. Почему: один источник истины и один предикат противоречия вместо восьми пересборок. Вердикт: реализует FR-01, FR-02 и FR-07 — реализуемо на текущих системах.
  traces: [S-01, CON-04]
- **AP-02** Типизированный serializer `ExecutorConfig → argv` для child: перечисляет представимые поля (`--project-root`, `--change` | `--spec-prefix`, `--max-retries`, `--timeout`, `--no-*`, `--integration-pr`, `--hitl-review`, `--budget`, `--task-budget`, `--callback-url`, `--log-level`); поля без представления воспроизводятся тем, что child читает тот же YAML с `cwd=project_root`, а родитель — тот же файл по `project_root`; override непредставимого поля у родителя → отказ до spawn. Почему: зеркало гварда `_COMMON_DEFAULTS` — молчаливый пропуск поля невозможен. Вердикт: реализует FR-03 (при условии CON-03 — правка поиска YAML входит в объём) и FR-04 — реализуемо.
  traces: [S-03, S-04, CON-03]
- **AP-03** Handshake на существующих механизмах: child после `_acquire_run_lock` и `clear_stop_file` публикует ready (предпочтение — файл в том же namespace, например `<spec_dir>/.executor-ready`, либо поле в JSON lock через `ExecutorLock`); MCP ждёт ready с таймаутом, параллельно наблюдая `proc.poll()`, и только тогда отвечает started; при занятом lock child выходит с exit 1 до ready, и MCP возвращает error. Почему: без нового IPC, маркер живёт рядом со stop и наблюдаем в E2E; отвечает на Q-01 customer-брифа. Вердикт: реализует FR-05 и FR-06 — реализуемо.
  traces: [S-05, S-06]
- **AP-04** stdout/stderr child — в лог-файл под `config.logs_dir` (или DEVNULL), а не `PIPE`. Почему: недренируемый PIPE блокирует child после заполнения буфера (RK-01). Попутно закрывает FR-08/FR-09 документацией и полем scope в status — реализуемо.
  traces: [S-01]
- **AP-05** Fail-closed вместо fallback везде, где scope или конфиг не воспроизводим: по прецедентам #64, #129, #337, #484. Почему: молчаливый дефолт в этом репо уже трижды оборачивался тихим неверным поведением.
  traces: [S-05, CON-03]

## Risks

- **RK-01** tacit: `Popen(stdout=PIPE, stderr=PIPE)` в `run_task` никто не читает — разговорчивый child блокируется на записи после заполнения буфера pipe; нигде не зафиксировано, E2E с многословным fake это вскроет.
- **RK-02** tacit: CWD-относительный поиск YAML (CON-03) — родитель, запущенный из чужой директории, молча работает с дефолтами (например `spec_governance: off`), и `spec_run_gate_ok` пропускает там, где целевой репо strict.
- **RK-03** Перенос effective config больше, чем кажется: три источника и односторонние флаги; сравнивать в приёмке FR-03 нужно resolved `ExecutorConfig` child и parent по перечисленным полям, а не строку команды.
- **RK-04** Stop-marker потребляется в трёх точках (старт, две между задачами) и разделяется командами `run`/`watch`/`retry`; handshake не должен регрессить их — риск расползания объёма.
- **RK-05** tacit: добавление CLI-флага для serializer (AP-02) требует синхронной правки `common` и `_COMMON_DEFAULTS`; гвард ловит рассинхрон только при импорте.

## Open Questions

- **Q-01** Сервер запущен без namespace (плоский spec/): остаётся ли tool-level `spec_prefix` разрешённым выбором prefix-namespace внутри launch project_root (противоречия нет) или отклоняется тоже? Предложение: разрешён, через тот же holder.
  owner_role: product
  blocking: false
- **Q-02** Канал ready: файл `<spec_dir>/.executor-ready` или поле в JSON lock (AP-03)? Предложение: файл в namespace — наблюдаем в E2E, не меняет формат lock.
  owner_role: architect
  blocking: false
- **Q-03** Обратная сверка effective config child родителем (Q-02 customer-брифа): предложение — child записывает resolved поля в ready-marker, родитель сравнивает перечисленные serializer-ом поля и отказывает при расхождении.
  owner_role: architect
  blocking: false