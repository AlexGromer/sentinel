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
| **M9.2a — GoalPlanner (NL→plan)** | ✅ готово (offline) — goal-directed грауденный планировщик (explore-first, не галлюцинирует селекторы) + `--goal` авто-режим + минимальный RunConfig YAML, ADR-027 |

Подробности по вехам: [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Архитектура вкратце (polyglot — каждый язык там, где он сильнее)
```
agentctl (Go)  ── spawn + env ──▶  brain (Python, LangGraph)  ── JSON-RPC/stdio ──▶  pw-executor (TS, Playwright)
control-plane / CLI                perceive→plan→act→verify→heal               our own browser server  ── Chromium
```
- **Go** — позвоночник control-plane: CLI, жизненный цикл запуска, (M2+) orchestrator, store-gateway, отчёты.
- **Python** — мозг: state machine на LangGraph + логика планирования и healing.
- **TypeScript** — `pw-executor`: наш собственный Playwright-сервер (мы **строим** его сами, а не берём готовый продукт — см. ADR-001).

Полный дизайн: [`ARCHITECTURE.md`](ARCHITECTURE.md) (27 ADR) · детальные разборы в [`docs/`](docs/) · история проектных решений в [`docs/DESIGN_RECORD.md`](docs/DESIGN_RECORD.md).

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
