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

Подробности по вехам: [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Архитектура вкратце (polyglot — каждый язык там, где он сильнее)
```
agentctl (Go)  ── spawn + env ──▶  brain (Python, LangGraph)  ── JSON-RPC/stdio ──▶  pw-executor (TS, Playwright)
control-plane / CLI                perceive→plan→act→verify→heal               our own browser server  ── Chromium
```
- **Go** — позвоночник control-plane: CLI, жизненный цикл запуска, (M2+) orchestrator, store-gateway, отчёты.
- **Python** — мозг: state machine на LangGraph + логика планирования и healing.
- **TypeScript** — `pw-executor`: наш собственный Playwright-сервер (мы **строим** его сами, а не берём готовый продукт — см. ADR-001).

Полный дизайн: [`ARCHITECTURE.md`](ARCHITECTURE.md) (31 ADR) · детальные разборы в [`docs/`](docs/) · история проектных решений в [`docs/DESIGN_RECORD.md`](docs/DESIGN_RECORD.md).

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
**Setup-WebUI локально (air-gapped, в составе бандла):** `docker compose --profile webui up` → открой
`http://localhost:8088/setup/` (и `/calculators/`) — генератор конфигурации и калькуляторы в браузере, без сети.

**Локальная модель** (без облака): раскомментируйте блок `LLM_*` в [`docker-compose.yml`](docker-compose.yml) и
поднимите endpoint — `docker compose --profile ollama up -d ollama`. Подбор модели/железа — в
[`docs/LOCAL_MODELS.md`](docs/LOCAL_MODELS.md) и интерактивных калькуляторах на
[GitHub Pages](https://alexgromer.github.io/sentinel/). Полное руководство по запуску и проверке —
[`docs/TESTING.md`](docs/TESTING.md).

## Документация
| Документ | О чём |
|----------|-------|
| [`docs/TESTING.md`](docs/TESTING.md) | offline-гейты, локальные модели, live-прогон, zero-level docker-compose |
| [`docs/LOCAL_MODELS.md`](docs/LOCAL_MODELS.md) | VRAM-методика + token-cost-методика + каталог моделей и runtime (verified) |
| [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) | STRIDE-lite по границам доверия (→ [`SECURITY.md`](SECURITY.md)) |
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
