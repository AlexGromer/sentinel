# Sentinel

> 🌐 **Русский** (основная версия) · [English](README.en.md)

**Автономный self-healing агент для UI-тестирования.** Sentinel самостоятельно исследует веб-приложение,
решает, что тестировать, замораживает детерминированный и воспроизводимый план тестирования и восстанавливает
сломанные локаторы при дрейфе DOM — генерируя артефакты для инженеров (отчёты, трассировки,
экспортированные Playwright-спеки, regression baselines).

Это ключевое отличие от обычного test-writer: Sentinel **обнаруживает и поддерживает**
тесты, а не только пишет их.

## Язык / Language

Русский — основная и авторитетная версия документации. Английские копии находятся в файлах с суффиксом `*.en.md`.

## Статус
| Milestone | Состояние |
|-----------|-----------|
| **M0 — Hello Browser** | ✅ готово — цепочка Go→Python→TS формирует a11y tree + `trace.zip` |
| **M1 — Autonomous Walk** | ✅ готово — LangGraph StateGraph, convergence по покрытию, `plan.json` + `plan_hash` |
| **M2 + M2b — Self-Healing + Service Layer** | ✅ готово — heal-движок (L1–L6 + LLM); Go store-gateway (gRPC) + MCP-SDK транспорт |
| **M3 — CI-Ready Replay** | ✅ готово — trust layer, exit codes 0/1/2/3, golden baselines, flake quarantine |
| **M4 + M4b — Reports + Observability** | ✅ готово — HTML/JSON/Prometheus отчёты, `.spec.ts` экспорт; brain OTel + Pushgateway |
| **M5 — Deploy + Visual Heal** | ✅ готово — Dockerfile + Helm CronJob + ArgoCD; set-of-marks Tier-7 (gated) |
| **M6 — Provider-Agnostic Brain** | ✅ готово — planner/heal на любом провайдере (Anthropic / OpenAI-compat), ADR-019 |
| **M7 — MCP-Server Exposure** | ✅ готово — brain как MCP-сервер (FastMCP) + `SamplingBackend` (host поставляет модель), ADR-020 |
| **M8 — Distributed Observability + Budget Ceiling** | ✅ готово — W3C-трейсинг Go/Python/TS + Go orchestrator (бюджет-потолок, SIGTERM) + report-service, ADR-021 |
| **M9 — Conversational & Goal-Directed Testing** | 📝 дизайн заморожен (Proposed, ADR-022..025) — см. [`docs/M9_CONTRACT.md`](docs/M9_CONTRACT.md) |
| **M9.1 — Form/Login/Validation primitives** | ✅ готово (offline) — pw-executor `fill`/`type`/`press`/`select` + storageState-auth (login-as-test) + assert/негативный слой, ADR-026 |
| **M9.2a — GoalPlanner (NL→plan)** | ✅ готово (offline) — goal-directed планировщик с `grounding` (выбор только из реальных элементов карты — не галлюцинирует селекторы) + `--goal` авто-режим + минимальный RunConfig YAML, ADR-027 |
| **M9.2b — Two-phase + describe-first** | ✅ готово (offline) — полный explore→карта сайта→one-shot сценарий по цели/описанию (кросс-страничный, привязан к реальным элементам); `--describe` + богатый RunConfig (auth/scenarios), ADR-028 |
| **M9.3 — Control-API (non-MCP)** | ✅ готово (Wave B) — Go `cmd/control-api` (localhost-bind + bearer-token + CORS); чат-фронт (`docs/chat/`) + CI-шаблоны (`docs/ci-templates/`) — ✅ (M9.3-tail, GAP-M9-03 закрыт), ADR-023/032/040 |
| **M9.4 + M9.5 — Tabs + backend correlation** | ✅ готово (offline, Wave A) — in-app вкладки (`[role=tab]`) + браузерные вкладки (multi-page) + `traceparent`-инъекция в запросы, ADR-022/024 |
| **M9.6 — Browser modes** | ✅ готово (offline, Wave D) — headed + CDP-attach (env-тумблер `PW_HEADLESS=0` / `PW_CDP_ENDPOINT`); **Chromium-only by design**, ADR-036/037 |
| **M9.7 — Pluggable adapters** | 🔶 частично — model/backend через LiteLLM-роутер (ADR-045); остаток — auth/deploy-адаптеры (GAP-M9-08) |
| **M9.8-R3 — Co-pilot takeover (brain-side)** | ✅ готово — takeover/return поверх RunControl gRPC (`interrupt()`/`Command(resume)`, abort>takeover), ADR-054; MV3-расширение → @0xCoDSnet |
| **M9.9 — Replay-in-UI (R1)** | ✅ готово — ▶/🔁/📌 run/replay/baseline + вердикт в vanilla-консолях (`mode=replay\|baseline`, `from_run`), ADR-047 |
| **M9.10 — Multi-turn authoring (R2)** | ✅ готово — многотёрновый диалог (`conversation_id` → checkpointer-resume, `messages`-канал), ADR-048 |
| **M11.x — Дистрибуция/установка** | ✅ готово — release-pipeline + Cosign-keyless + syft SBOM (M11.1, ADR-030) · Helm/Flux + Secret-плумбинг (M11.3, ADR-035) · air-gapped bundle + offline-verifier (M11.4) · installer (`install.sh`/`install.ps1`/Homebrew) + schema-driven визард + config-домен + `/readyz` (M11.5, ADR-059..062) · Pages-хаб + калькуляторы (M11.6/b). Хвост: подписанный релиз/bundle на первом `v*`-теге. Подробно — [`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md) |
| **M12 — OpenAI-compat shim + единая консоль** | ✅ готово — `POST /v1/chat/completions` (1 тёрн→1 прогон) + единая `docs/index.html` (#connect/#build/#chat), ADR-041 |
| **M13 — Persistence / 6-домен store-gateway** | ✅ готово — store-gateway на 6 доменов (`runs`·`scenarios`/`tests`·`chats`·`results`·`metrics` + `config`), SQLite-first, ADR-049/050/062 |
| **M14 — Rich AG-UI co-pilot (in-house vanilla)** | ✅ готово — server→client AG-UI поверх WS `/v1/stream`; split Settings\|Tests + live-timeline + auto-HITL; CopilotKit убран, `frontend/` заморожен (ADR-052/055). Хвосты: терминальный `run.finished` (#86) + AG-UI/auto-HITL-сигнал в replay (#87) |
| **M-STRUCTURED-OUT — Strict structured output** | ✅ готово — строгий `tool_use`/`json_schema` + `extract_json` для authoring/heal (ADR-057) |
| **M15 — Metrics-in-UI + token-cost** | ✅ готово — нативные SVG-панели результатов/метрик; **M15.1** — 8-я метрика token-cost (`tokens`-блок → `cost_usd`), ADR-051 |
| **M9-LIVE-prep — Подготовка к живому прогону** | ✅ готово — исполнимый `docs/M9_LIVE_PLAN.md` (8 факт-ошибок) + `scripts/collect-live-run.sh` (редактирующий коллектор артефактов), #88 |

Подробности по вехам: [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Архитектура вкратце (polyglot — каждый язык там, где он сильнее)
```
agentctl (Go)  ── spawn + env ──▶  brain (Python, LangGraph)  ── JSON-RPC/stdio ──▶  pw-executor (TS, Playwright)
control-plane / CLI                perceive→plan→act→verify→heal               our own browser server  ── Chromium
```
- **Go** — позвоночник control-plane: CLI, жизненный цикл запуска, (M2+) orchestrator, store-gateway, отчёты.
- **Python** — мозг: state machine на LangGraph + логика планирования и healing.
- **TypeScript** — `pw-executor`: наш собственный Playwright-сервер (мы **строим** его сами, а не берём готовый продукт — см. ADR-001).

Полный дизайн: [`ARCHITECTURE.md`](ARCHITECTURE.md) (62 ADR) · детальные разборы в [`docs/`](docs/) · история проектных решений в [`docs/DESIGN_RECORD.md`](docs/DESIGN_RECORD.md).

> **Режимы браузера (M9.6):** по умолчанию own-headless; `PW_HEADLESS=0` — headed (видимый), `PW_CDP_ENDPOINT` — CDP-attach к существующему Chrome пользователя. Движок — **только Chromium by design** (ADR-036); детерминированный голден-replay — только в headless (см. [`docs/DETERMINISM.md`](docs/DETERMINISM.md)).

## Быстрый старт (M0)
```bash
# 1. build the TS browser server
cd pw-executor && npm install && npm run build && npx playwright install chromium-headless-shell && cd ..
# 2. build the Go CLI
go build -o bin/agentctl ./cmd/agentctl
# 3. run against a local fixture (no network)
./bin/agentctl run --target "file://$PWD/testdata/m0.html"
# → prints the accessibility tree and writes runs/<id>/trace.zip
```

## Быстрый старт через Docker (one-command)
```bash
docker compose build
# zero-dependency demo: эвристический планировщик + встроенная file://-фикстура, без сети и API-ключа
docker compose --profile demo up
# …или против своей цели (goal-режим, нужен ключ или локальная модель):
docker compose run --rm sentinel run --target "https://your-app.example" --goal "залогиниться и открыть биллинг"
```
**Живой UI одним сервисом (рекомендуемый путь, ADR-064):**
```bash
CONTROL_API_SERVE_UI=1 CONTROL_API_CORS_ORIGINS= docker compose --profile control-api up control-api
```
→ открой ссылку `?bootstrap=…`, которую control-API печатает при старте: один порт (`:8090`), никакого CORS,
токен подставляется в UI сам (одноразово). Токен генерируется автоматически и хранится в
`state/control-api.token` — придумывать его заранее больше не нужно.

**Setup-WebUI отдельным сервисом (статика, air-gapped, в составе бандла):** `docker compose --profile webui up` →
открой `http://localhost:8088/setup/` (и `/calculators/`) — генератор конфигурации и калькуляторы в браузере, без сети.

**Локальная модель** (без облака): раскомментируйте блок `LLM_*` в [`docker-compose.yml`](docker-compose.yml) и
поднимите endpoint — `docker compose --profile ollama up -d ollama` (или мульти-провайдер роутер LiteLLM — `docker compose --profile litellm up -d litellm`, см. [`docs/ADAPTERS.md`](docs/ADAPTERS.md)). Подбор модели/железа — в
[`docs/LOCAL_MODELS.md`](docs/LOCAL_MODELS.md) и интерактивных калькуляторах на
[GitHub Pages](https://alexgromer.github.io/sentinel/). Полное руководство по запуску и проверке —
[`docs/TESTING.md`](docs/TESTING.md).

## Документация
| Документ | О чём |
|----------|-------|
| [`docs/TESTING.md`](docs/TESTING.md) | offline-гейты, локальные модели, live-прогон, zero-level docker-compose |
| [`docs/LOCAL_MODELS.md`](docs/LOCAL_MODELS.md) | VRAM-методика + token-cost-методика + каталог моделей и runtime (verified) |
| [`docs/ADAPTERS.md`](docs/ADAPTERS.md) | подключаемые адаптеры: опц. LiteLLM-роутер (за `LLM_BASE_URL`) + MCP-Inspector отладка M7 |
| [`docs/COPILOT.md`](docs/COPILOT.md) | co-pilot: видение · статус (честный feature-inventory) · договорённости · roadmap по волнам [me]/[@0xCoDSnet] |
| [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) | STRIDE-lite по границам доверия (→ [`SECURITY.md`](SECURITY.md)) |
| [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) | гайд контрибьютора: сборка, milestone-гейты, рецепты расширения, Secret-плумбинг |
| [`docs/DETERMINISM.md`](docs/DETERMINISM.md) | детерминизм, plan_hash, golden baselines, граница headless-only |
| [`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md) | эпик дистрибуции/онбординга: Release · compose · Helm/Flux · setup-WebUI · air-gapped |
| [GitHub Pages](https://alexgromer.github.io/sentinel/) | хаб документации + 3 калькулятора (VRAM · token-cost · model-selector) |

## Карта проекта
| Путь | Назначение |
|------|------------|
| `ARCHITECTURE.md`, `GAPS.md`, `BACKLOG.md`, `FILEMAP.md` | канонический дизайн, открытые вопросы, задачи, индекс файлов |
| `docs/` | спецификации по областям + контракты milestone (`M*_CONTRACT.md`) + история дизайна |
| `cmd/agentctl/` | Go CLI |
| `brain/` | Python LangGraph brain |
| `pw-executor/` | TypeScript Playwright server |
| `testdata/` | тестовые фикстуры |

## Участие в разработке / расширение
Прочитайте **[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)** — настройка toolchain, сборка по компонентам, запуск
milestone gates, и пошаговые рецепты расширения (добавить инструмент pw-executor, добавить planner,
добавить узел LangGraph). **Сначала документация:** каждый milestone имеет контракт в `docs/`, написанный до кода;
весь код снабжён docstring; нет недокументированных модулей.

## Лицензия
[Apache-2.0](LICENSE) (+ [`NOTICE`](NOTICE)). Контрибьюция: [`CONTRIBUTING.md`](CONTRIBUTING.md) · безопасность: [`SECURITY.md`](SECURITY.md) · кодекс: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). Ветка `main` защищена (PR + ревью + зелёный CI).
