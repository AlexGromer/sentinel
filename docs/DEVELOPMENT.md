# Development Guide — Sentinel

> 🌐 **Русский** (основная версия) · [English](DEVELOPMENT.en.md)

Руководство уровня handoff, позволяющее любому разработчику собрать, запустить и **расширить** Sentinel.
Читайте его вместе с [`../ARCHITECTURE.md`](../ARCHITECTURE.md) (канонический дизайн + ADR) и
контрактами вех в этой папке (`M0_CONTRACT.md`, `M1_CONTRACT.md`, …).

## 0. Принципы работы (не подлежат обсуждению)
1. **Сначала документация.** Перед написанием кода для вехи необходимо зафиксировать спецификацию/контракт (`docs/M*_CONTRACT.md`).
2. **Всё документируется.** Каждый модуль и публичная функция имеют docstring; форматы проводки живут в документе-контракте.
3. **Строим, не покупаем** (ADR-001). Используем OSS-*библиотеки* (Playwright, LangGraph, Anthropic SDK); никогда не принимаем готовые продукты/серверы — компоненты пишем сами.
4. **Детерминизм и доверие** (ADR-006/010). Explore-once → replay-many; сходимость — это измеримая цель покрытия, а не флаг LLM «готово».

## 1. Предпосылки
| Инструмент | Используемая версия | Примечания |
|------|--------------|-------|
| Go | 1.26.x | control-plane |
| Node | 24.x + npm 11.x | pw-executor |
| Python | 3.12+ | brain (LangGraph) |
| uv | 0.10.x | менеджер окружения/зависимостей Python |
| Playwright browser | chromium-headless-shell (совпадает с закреплённым playwright) | однократная загрузка |

## 2. Сборка компонентов
```bash
# TypeScript — pw-executor (our Playwright server)
cd pw-executor
npm install
npm run build                              # tsc → dist/server.js
npx playwright install chromium-headless-shell   # one-time; matches pinned playwright version
cd ..

# Go — control-plane (if /tmp is full: `go env -w GOTMPDIR=/opt/go/tmp` first — Go build scratch)
go build -o bin/agentctl ./cmd/agentctl
go build -o bin/store-gateway ./cmd/store-gateway   # M2b-1: gRPC persistence; agentctl auto-spawns it

# Python — brain (LangGraph)
uv venv                                    # creates .venv
uv pip install langgraph langgraph-checkpoint-sqlite anthropic
```
`agentctl` автоматически использует `./.venv/bin/python` для запуска brain (переопределяется через `BRAIN_PYTHON`).

## 3. Запуск
```bash
# M0 — single perceive, prints a11y tree + trace.zip
./bin/agentctl run --target "file://$PWD/testdata/m0.html"

# M1 — autonomous walk over a multi-page fixture → plan.json
./bin/agentctl run --target "file://$PWD/testdata/site/index.html" --planner heuristic
#   flags: --planner heuristic|llm   --coverage-target 0.85   --max-steps 40

# M2 — replay a frozen plan against a drifted DOM, self-healing broken locators
./bin/agentctl run --replay --plan runs/<id>/plan.json --target "file://$PWD/testdata/site-v2/index.html"
#   flags: --heal-llm   (Sonnet fallback when L1-L6 miss; needs ANTHROPIC_API_KEY)
```
Артефакты сохраняются в `runs/<run_id>/` (`plan.json`, `llm-transcript.jsonl`, `trace.zip`, `checkpoint.db`; при replay добавляется `heal-report.json`) — `runs/` исключён из git. Исцелённые локаторы и аудит сохраняются в `state/locators.db` (временное локальное хранилище, M2 → store-gateway в M2b; исключён из git).

> **Примечание об ограничениях (это окружение):** запуск свежесобранных бинарей и внешняя сеть ограничены.
> Шаги сборки (`npm`, `go build`, `uv pip`) работают нормально; запускайте `agentctl` самостоятельно (например, через префикс `!`)
> и предпочитайте локальные `file://` фикстуры внешним целям.

## 4. Гейты вех (приёмка)
- **M0** (`M0_CONTRACT.md`): дерево a11y выведено + `runs/<id>/trace.zip` размер>0 + exit 0.
- **M1** (`M1_CONTRACT.md`): `plan.json` с **≥5 шагами**, `coverage_achieved` записан, `plan_hash` присутствует, `trace.zip` присутствует; второй идентичный запуск выдаёт **тот же `plan_hash`** (детерминизм — heuristic planner).

```bash
# determinism check (M1)
A=$(./bin/agentctl run --target "file://$PWD/testdata/site/index.html" >/dev/null; jq -r .plan_hash runs/*/plan.json | tail -1)
# run again, compare plan_hash — must match
```

## 5. Проводные контракты (где определены границы)
| Граница | Документ |
|----------|-----|
| agentctl ↔ brain (subprocess + env) | `M0_CONTRACT.md` §Boundary A |
| brain ↔ pw-executor (JSON-RPC 2.0 / stdio) | `M0_CONTRACT.md` §Boundary B + `M1_CONTRACT.md` (new tools) |
| LangGraph nodes / RunState | `STATE_MACHINE.md`, `M1_CONTRACT.md` |
| (M2) Go ↔ Python gRPC, MCP-SDK transport | `ARCHITECTURE.md` §2, GAP-VERIFY-002 |

## 6. Рецепты расширения
### Добавить инструмент браузера pw-executor (TypeScript)
1. Добавить `case 'browser.<x>':` в `handle()` файла `pw-executor/src/server.ts` (сначала вызвать `await ensureBrowser()`; вернуть JSON-safe объект; **логи только в stderr**).
2. Добавить имя метода в массив `capabilities` в `initialize`.
3. Задокументировать в таблице инструментов соответствующего `M*_CONTRACT.md`.
4. `npm run build`; вызывать из brain через `ex.call("browser.<x>", ...)`.

### Добавить плановщик (Python)
1. Реализовать протокол `Planner` в `brain/planner.py`: `propose(state, candidates) -> {action, done, reason, tokens}` с атрибутами `name` и `model`.
2. Подключить выбор в `brain/__main__.py` (ключ `--planner` / env-переменная `PLANNER`).
3. Сохранять детерминизм heuristic-планировщика; LLM-планировщики должны откатываться на heuristic при ошибке/отсутствии ключа (graceful degradation, ADR-011) и логировать использование токенов в транскрипт.

### Добавить / изменить узел LangGraph (Python)
1. Объявить любое новое поле состояния как канал в `RunState` (`brain/state.py`) — **незадекларированные ключи отбрасываются между узлами**.
2. Добавить функцию узла и зарегистрировать её в `brain/graph.py` `build_graph()` (`add_node`), затем соединить рёбрами (`add_edge` / `add_conditional_edges`).
3. Следить за циклами: увеличивать `recursion_limit` в конфиге `invoke`, если добавляются дополнительные суперщаги на цикл.
4. Обновить `STATE_MACHINE.md` и контракт вехи.

### Добавить стратегию исцеления (Python)
1. Добавить ключ стратегии и её prior в `PRIORS` в `brain/healing.py`.
2. Сформировать соответствующую запись `alternatives` при explore в `brain/graph.py` `_buttons_from_interactives`, убедившись, что `pw-executor` `buildLocator` умеет строить и проверять этот тип локатора.
3. `HealingEngine.heal` перебирает alternatives в записанном порядке; verify-before-accept перепроверяет каждого кандидата в живом DOM. Задокументировать в `docs/SELF_HEALING.md` + `docs/M2_CONTRACT.md`.

### Начать новую веху
Сначала напишите `docs/M<N>_CONTRACT.md` (scope, контракты, гейт приёмки Given/When/Then), добавьте ADR в `ARCHITECTURE.md` если это архитектурное решение, добавьте задачи в `BACKLOG.md`, *затем* реализуйте.

## 7. Стандарты кодирования
- Docstrings на каждом модуле и публичной функции; комментарии объясняют *почему*, а не *что*.
- Conventional commits (`feat(m1): …`); в конце сообщений — трейлер `Co-Authored-By`.
- `gitleaks detect` перед коммитом; никогда не коммитить `.claude/`, секреты, `runs/`, `node_modules/`, `dist/`, `bin/`.
- Отслеживать неизвестное в `GAPS.md` (`GAP-[CAT]-[NUM]`); задачи — в `BACKLOG.md` через backlog MCP.
