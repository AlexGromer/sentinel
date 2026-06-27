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
uv pip install langgraph langgraph-checkpoint-sqlite anthropic openai
#   openai опционален — нужен только для OpenAI-совместимых провайдеров (M6); import-guarded
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

### LLM backend (M6, провайдер-нейтральный)
По умолчанию (ноль env) — Anthropic, как раньше: `claude-opus-4-8` (planner) / `claude-sonnet-4-6`
(heal), ключ `ANTHROPIC_API_KEY`; без ключа planner откатывается на heuristic, heal — на L1–L6.
Чтобы пустить **planner** и/или **heal** на любой OpenAI-совместимый endpoint (ChatGPT, DeepSeek,
Qwen, Gemini-compat, OpenRouter, Ollama, vLLM) — выставь env **per-role**. Precedence:
`LLM_<KEY>_<ROLE>` > `LLM_<KEY>` > дефолт (роли: `PLANNER`, `HEAL`).

| Ключ | Global | Per-role override | Дефолт (как до M6) |
|------|--------|-------------------|--------------------|
| `LLM_BACKEND` | да | `LLM_BACKEND_PLANNER` / `_HEAL` | `anthropic` |
| `LLM_MODEL` | да | `LLM_MODEL_PLANNER` / `_HEAL` | `claude-opus-4-8` / `claude-sonnet-4-6` |
| `LLM_BASE_URL` | да | `LLM_BASE_URL_PLANNER` / `_HEAL` | — |
| `LLM_API_KEY` | да | `LLM_API_KEY_PLANNER` / `_HEAL` | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` |
| `LLM_VISION` | да | `LLM_VISION_HEAL` | provider default (anthropic=on) |

```bash
# planner на OpenRouter/DeepSeek, heal остаётся на Anthropic-дефолте
LLM_BACKEND_PLANNER=openai LLM_BASE_URL_PLANNER=https://openrouter.ai/api/v1 \
  LLM_API_KEY_PLANNER=… LLM_MODEL_PLANNER=deepseek/deepseek-chat \
  ./bin/agentctl run --target "file://$PWD/testdata/site/index.html" --planner llm

# vision-heal на другом провайдере (set-of-marks Tier-7 требует vision-модель)
LLM_BACKEND_HEAL=openai LLM_BASE_URL_HEAL=… LLM_MODEL_HEAL=… LLM_VISION_HEAL=1 \
  ./bin/agentctl run --replay --plan runs/<id>/plan.json --target "file://…" --heal-llm
```
`make_backend(role)` (`brain/llm.py`) собирает backend из env или возвращает `None` ⇒ сохраняется
offline-fallback (heuristic / L1–L6); **никогда не бросает**. Text-only провайдер пропускает Tier-7
(нет vision) → детерминированный L1–L6. Источник истины: `brain/llm.py` + `docs/M6_CONTRACT.md`.

## 4. Гейты вех (приёмка)
- **M0** (`M0_CONTRACT.md`): дерево a11y выведено + `runs/<id>/trace.zip` размер>0 + exit 0.
- **M1** (`M1_CONTRACT.md`): `plan.json` с **≥5 шагами**, `coverage_achieved` записан, `plan_hash` присутствует, `trace.zip` присутствует; второй идентичный запуск выдаёт **тот же `plan_hash`** (детерминизм — heuristic planner).

```bash
# determinism check (M1)
A=$(./bin/agentctl run --target "file://$PWD/testdata/site/index.html" >/dev/null; jq -r .plan_hash runs/*/plan.json | tail -1)
# run again, compare plan_hash — must match
```

- **M2** (`M2_CONTRACT.md`): сломанный locator в replay исцелён с уверенностью **≥ 0.85**, строка `HealedLocator` `status=active` сохранена, exit 0; второй прогон — **ноль LLM-токенов** для того же semantic_id (попадание в кэш).
- **M2b** (`M2b_CONTRACT.md`): со `store-gateway` (Go gRPC через UDS) explore/baseline/replay/calibrate идентичны; `grep -r sqlite3 brain/` пуст; `tools/list` pw-executor возвращает **7 инструментов**; live-гейты M0–M3 проходят через MCP-транспорт (live — user-run).
- **M3** (`M3_CONTRACT.md`): 3 параллельных replay `--ci` **< 2 мин** каждый, exit 0; replay по подменённому `plan_hash` — **exit 3** за < 5 c (оба хэша в stderr).
- **M4** (`M4_CONTRACT.md`): `run_report.html` непустой и рендерится; экспортированный `.spec.ts` проходит `tsc --noEmit`; `trace.zip` открывается; `agent_cost_usd_total` **> 0** в `/metrics`.
- **M4b** (`M4b_CONTRACT.md`): без `OTEL_EXPORTER_OTLP_ENDPOINT` / `PROM_PUSHGATEWAY` — span'ы no-op, push нет, offline-тесты зелёные; с endpoint+gateway (user-run) трейсы в Tempo, метрики в Pushgateway.
- **M5** (`M5_CONTRACT.md`): set-of-marks visual heal — **≥ 15/20** сценариев совпали с human-verified (≥ 75% > 70% порог), иначе функция отложена и overlay удалён из бинаря.
- **M6** (`M6_CONTRACT.md`): offline `test_b1_offline` (8) + `test_m5_offline` (4) зелёные, регресс `test_m3` / `test_m4` / `test_m4b` зелёный, **default-path байт-в-байт**; реальный smoke провайдера — user-run.
- **M7** (`M7_CONTRACT.md`): brain MCP-сервер — `tools/list` возвращает `explore`/`heal`/`replay`/`report`; offline `test_m7` (5) зелёный + `SamplingBackend` через fake sampling-session; живой MCP-host — user-run.
- **M8** (`M8_CONTRACT.md`): W3C-трейс brain→pw-executor→store-gateway (gated, no-op без OTLP) + `BudgetTracker` флипает `exceeded()` на лимите с degradation; offline `test_m8` (9) зелёный + `go build`/`vet`/`test` + `tsc`; live OTLP / реальный budget-kill — user-run.
- **M9.1** (`M9.1_CONTRACT.md`): pw-executor `fill`/`type`/`press`/`select`/`expect`/`saveStorageState` (`tsc --noEmit` clean); offline `test_m9` (19) зелёный — секрет не утекает в артефакты, `plan_hash` стабилен, exit-композиция assert'ов; gitleaks чисто; живой UI-прогон (формы/логин) — по «go».
- **M9.2a** (`M9.2_CONTRACT.md`): `GoalPlanner` с `grounding` (выбор по индексу из реальных кандидатов, OOB→done — никогда не фабрикует селектор) + `make_planner` авто-дефолт по `--goal` + RunConfig YAML (приоритет флаг>файл>дефолт через `SENTINEL_EXPLICIT`); offline `test_m9_2` (20) зелёный + `go build`/`vet`; живой goal-прогон — по «go».

```bash
# offline-набор (без сети/бинарей): весь регресс M3..M9
for t in m3 m4 m4b m5 b1 m7 m8 m9 m9_2; do .venv/bin/python tests/test_${t}_offline.py; done
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
