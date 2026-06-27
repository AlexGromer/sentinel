# Architecture — Sentinel

> 🌐 **Русский** (основная версия) · [English](ARCHITECTURE.en.md)

> Автономный self-healing Playwright агент для UI-тестирования.
> Полиглот: **Go** spine / **Python** LangGraph brain / **TypeScript** Playwright executor.
> Создан в рамках трёхфазного процесса проектирования (4 независимых архитектора → 3 adversarial judge → синтез lead-архитектора), 2026-06-23.
> Детальная механика — в `docs/` (см. §7). История проектирования — в `docs/DESIGN_RECORD.md`.

---

## 0. CONSTRAINT OVERRIDE — BUILD-ONLY (2026-06-23, hard)

**Директива пользователя:** *«Мы не можем покупать или принимать ничего готового; мы можем только писать всё сами.»*

**Интерпретация (допущение — требует подтверждения):** open-source **библиотеки**, против которых мы пишем код (Playwright library, LangGraph, Anthropic SDK), считаются *«написанными нами»* и разрешены. Использование готового стороннего **сервера / SaaS-продукта** *не* разрешено. Если даже OSS-библиотеки запрещены (чисто с нуля, включая browser CDP), scope меняется кардинально — см. GAP-ARCH-002 / открытый вопрос.

**Следствие:** самое высоко оценённое решение синтеза — *КУПИТЬ официальный Microsoft-сервер `@playwright/mcp`* — **ОТМЕНЕНО**. Мы **СОЗДАЁМ** собственный TypeScript-сервер выполнения Playwright (`pw-executor`). Все три судьи-проектировщика указали на hand-built Playwright-сервер как на самый большой «language-tourism» cost; это предупреждение **принято** как неизбежная цена build-only суверенитета. Транспорт MCP-over-stdio (ADR-002) **остаётся** — MCP — открытый протокол, который мы реализуем сами; мы строим *сервер*, а не покупаем его.

---

## 1. Контекст

### Назначение
Production-grade автономный standalone агент UI-тестирования, который (1) самостоятельно исследует незнакомое веб-приложение, (2) решает, какие потоки тестировать, (3) фиксирует детерминированный, воспроизводимый план тестирования, (4) исправляет сломанные locators при изменении DOM и (5) генерирует артефакты для инженеров (отчёты, traces, экспортированные Playwright specs, regression baselines). Это отличие от существующего subagent `qa-automation-engineer`, который лишь *пишет* тесты — Sentinel *обнаруживает и поддерживает* их.

### Участники
| Актор | Роль | Интерфейс |
|-------|------|-----------|
| CI pipeline | Запускает детерминированный replay; потребляет exit codes 0/1/2/3 + JSON/JUnit-отчёты | `agentctl run --ci` |
| QA / dev-инженер | Запускает explore-прогоны, просматривает помеченные heals + human gates, утверждает baselines, потребляет `.spec.ts` | `agentctl` (интерактивный) |
| Home-lab оператор | Запускает долгоживущий сервис на K3s/ArgoCD, смотрит Grafana cost/health | Helm + ArgoCD (M5) |
| Сам агент | Автономный LLM-исследователь | Opus 4.8 (plan) / Sonnet 4.6 (heal) — **дефолты**; planner/heal провайдер-агностичны per-role через `LLM_BACKEND*` (Anthropic или любой OpenAI-совместимый), ADR-019 |

### Область применения
- **В scope:** автономное exploratory testing; locator self-healing с confidence gating + human-in-loop; explore-once / replay-many CI-детерминизм; короткая и долгосрочная память; per-run token/cost budgets + tracing; генерация артефактов; headless CI + долгоживущий сервис; цель — home-lab K3s/ArgoCD.
- **Вне scope (v1):** роль `.md` subagent Claude Code (явно исключено); multi-tenant SaaS; автоматическое слияние healed plans в защищённые ветки без ревью; load/perf testing; mobile-native (не web); cross-browser за пределами Chromium на MVP (Firefox/WebKit отложены); утверждения о корректности бизнес-логики за пределами observable UI state.

---

## 2. Компоненты / Области

### Обзор
```
                  ┌──────────── Go (control-plane / spine) ────────────┐
  CI / engineer ─►│ agentctl (CLI) → orchestrator (run FSM, gRPC srv,   │
                  │   budget ceiling, subprocess supervision)          │
                  │ store-gateway (SOLE writer, main SQLite-WAL)        │
                  │ report-service (JSON+HTML, /metrics, .spec.ts gen)  │
                  └───────────────────────┬────────────────────────────┘
                              gRPC proto3 (UDS/TCP)  ◄── phased in @ M2
                  ┌───────────────────────┴────────────────────────────┐
                  │            Python (brain — LangGraph)               │
                  │ StateGraph(9 nodes) · perception · healing-engine   │
                  │ checkpointer → SEPARATE SQLite file (not main DB)   │
                  └───────────────────────┬────────────────────────────┘
                              MCP / JSON-RPC 2.0 over stdio
                  ┌───────────────────────┴────────────────────────────┐
                  │  TypeScript (hands — BUILD)  pw-executor (our own)   │
                  │  Playwright Chromium · a11y snapshot · trace        │
                  └─────────────────────────────────────────────────────┘
```

### Таблица компонентов
| Компонент | Язык | Ответственность | Ключевые технологии |
|-----------|------|-----------------|---------------------|
| **agentctl** | Go | Единственный CLI/CI бинарник: `run` (`--explore`/`--replay`/`--ci`), `gate`, `report`, `baseline update`, `locators`, `calibrate`. Exit codes: **0** — успех / **1** — step-fail / **2** — golden-diff regression / **3** — plan-integrity-or-budget. Работает в non-TTY + interactive режимах. | cobra/urfave-cli, gRPC client, Viper (YAML) |
| **orchestrator** | Go | FSM жизненного цикла прогона (PENDING→RUNNING→HEALING→PAUSED→PARTIAL→DONE\|FAILED\|ABORTED), gRPC server (RunControl + EventStream), управляет subprocess Python brain (5s health-ping, restart-on-crash, SIGTERM при per-step deadline). Применяет **Go-side hard budget ceiling** (согласовано со счётчиком brain — НЕ per-call round trip). SQLite не трогает. | gRPC, goroutine supervisor, context deadlines |
| **store-gateway** | Go | **Единственный writer** основного SQLite (WAL). Всё долгосрочное состояние — через PersistenceService gRPC; управляет миграциями; предоставляет read RPCs. (LangGraph checkpointer использует ОТДЕЛЬНЫЙ DB-файл — single-writer *действительно* соблюдается.) | SQLite WAL, golang-migrate, gRPC |
| **report-service** | Go | Собирает `run_report.json` + HTML (аналог Playwright HTML-reporter), раздаёт trace/spec/cost endpoints, предоставляет Prometheus `/metrics`. Генерирует `.spec.ts` из `RunState.executed_actions` по шаблону (без зависимости от codegen-инструментов). Введён в M4. | Go html/template, client_golang |
| **brain** | Python | LangGraph StateGraph (9 узлов). Владеет ВСЕМИ LLM-вызовами через провайдер-нейтральный `LLMBackend` (`brain/llm.py`: `AnthropicBackend` \| `OpenAICompatBackend`, per-role `make_backend`; дефолты Opus 4.8 plan / Sonnet 4.6 heal), откатывается на heuristic / L1–L6 при отсутствии ключа/SDK. Порождает `pw-executor` + привязывает его MCP tools. gRPC-клиент к orchestrator + store-gateway. Управляет переключением explore/replay, coverage-based convergence, `plan_hash`. | LangGraph StateGraph + checkpointer, MCP client (VERIFY pkg), Anthropic / OpenAI SDK |
| **healing-engine** | Python | Heal-узел: ограниченная иерархия re-grounding (cache → L1–L6 no-LLM rotation → LLM a11y → gated set-of-marks), grounded confidence model с verify-before-accept, append-only `healing_audit`. Содержит логику `agentctl calibrate`. | Playwright locator strategies via MCP, structured-output LLM |
| **perception** | Python | Разбирает a11y snapshot → типизированный `PageModel`, вычисляет `completeness_ratio` для выбора модальности, вычисляет a11y-hash + subtree-scoped `dom_hash`. | a11y normalization, SHA-256 hashing |
| **brain/llm.py** | Python | Провайдер-агностичная абстракция `LLMBackend` (Protocol): `AnthropicBackend` (нативно) \| `OpenAICompatBackend` (ChatGPT/DeepSeek/Qwen/Gemini-compat/OpenRouter/Ollama/vLLM); `make_backend(role)` выбирает backend per-role через env (`LLM_BACKEND[_PLANNER\|_HEAL]`, `_MODEL`, `_BASE_URL`, `_API_KEY`, `_VISION`) → `None` при отсутствии ключа/SDK ⇒ fallback heuristic / L1–L6. LLM-путь best-effort; vision гейтится `supports_vision`; дефолты Opus 4.8 (planner) / Sonnet 4.6 (heal). (ADR-019) | Anthropic / OpenAI SDK, `typing.Protocol` |
| **pw-executor** | **TS (BUILD)** | **НАШ СОБСТВЕННЫЙ** Node-сервис, предоставляющий brain Playwright-примитивы (navigate, accessibility snapshot, click/type/и др., screenshot, trace control, locator resolve/probe, set-of-marks overlay) через MCP (JSON-RPC 2.0) stdio-интерфейс **нашей реализации**. Запускается как дочерний subprocess brain. | Playwright (lib, pinned), MCP server impl (ours), stdio JSON-RPC |
| **proto** | shared | protobuf3 — единый источник истины для Go↔Python. Services: RunControl, PersistenceService, EventStream. Stubs генерируются в CI для Go+Python; hash `.proto` проверяется против зафиксированных stubs (несовпадение = build failure). Введён в M2. | buf/protoc, CI codegen + hash assertion |

### Ключевые взаимодействия / границы
**Два активных протокола взаимодействия; третья граница намеренно устранена.**

1. **Go ↔ Python — gRPC proto3** (bidi streaming, UDS single-host / TCP для K3s). *Почему:* типизированные контракты на этапе компиляции (drift = build failure), server-push событий budget/gate без polling, propagation дедлайнов = per-step timeout. **Поэтапно вводится с M2** — M0/M1 используют обычный subprocess + env vars (`TARGET_URL`, `RUN_ID`, `RUN_MODE`, `ARTIFACT_DIR`). Отклонено: REST/JSON (нет compile-time schema, нет чистого streaming/cancel).
2. **Python ↔ TS — MCP (JSON-RPC 2.0) over stdio**, к `pw-executor`, привязан через MCP tool integration LangGraph. *Почему:* нативный протокол LLM tool-call (zero adapter), stdio избегает нестабильности выделения портов в CI, жизненный цикл subprocess управляется Python-родителем (SIGTERM cascade), EOF — чистый сигнал об ошибке. **Примечание BUILD-only:** мы реализуем этот сервер сами (ADR-001).
3. **TS → Go (artifacts) — УСТРАНЕНО.** Playwright traces записываются в общий artifact dir; brain получает путь в MCP-ответе и передаёт его Go по существующему gRPC-каналу; `report-service` читает файлы напрямую. `.spec.ts` генерируется Go из `RunState`, а не передаётся из TS. Меньше связей = меньше точек отказа.

**Изоляция отказов (реальная):** сбой TS/MCP → brain обнаруживает EOF, однократно перезапускает subprocess, переходит к checkpoint `page_model.url`, повторно входит в узел (работа не теряется — checkpoint предшествует действию). Сбой Python → orchestrator обнаруживает завершение gRPC-потока, помечает FAILED, сохраняет частичное состояние, checkpoint остаётся нетронутым (`agentctl run --resume`). Сбой Go → brain переподключается с backoff; основная DB устойчива; budget безопасно деградирует до in-process counter.

---

## 3. Решения (журнал ADR)

| ID | Дата | Решение | Статус | Контекст / отклонённая альтернатива |
|----|------|---------|--------|--------------------------------------|
| ADR-001 | 2026-06-23 | **СОЗДАТЬ** собственный TS-сервер выполнения Playwright (`pw-executor`) на базе MCP stdio-интерфейса нашей реализации | **Accepted (продиктовано ограничением; supersedes synthesis)** | Директива build-only (§0). Мы владеем схемой инструментов (стабильной) ценой поддержки Playwright-API-churn. **Отклонено:** КУПИТЬ официальный `@playwright/mcp` (запрещено ограничением — был лучшим выбором synthesis) |
| ADR-002 | 2026-06-23 | MCP over stdio для границы Python↔TS | Accepted | Нативный протокол LLM tool-call; LangGraph привязывает без adapter; stdio избегает нестабильности портов в CI. **Отклонено:** gRPC TS-сервер; Python-Playwright in-process (нарушает polyglot lock, теряет Node-native trace) |
| ADR-003 | 2026-06-23 | gRPC proto3 для Go↔Python, **поэтапно с M2** (M0/M1 = subprocess+env) | Accepted | Типизированные контракты на этапе компиляции, server-push, propagation дедлайнов. **Отклонено:** REST/JSON; gRPC с первого дня (преждевременно) |
| ADR-004 | 2026-06-23 | LangGraph StateGraph backbone; checkpointer в **ОТДЕЛЬНОМ** DB-файле от store-gateway | Accepted | Бесплатный checkpoint/resume/conditional-heal-edges/human-pause; отдельный файл делает «Go — единственный writer основной DB» истинным. **Отклонено:** bespoke asyncio loop; shared checkpoint+store DB (два writer) |
| ADR-005 | 2026-06-23 | a11y tree = первичное восприятие; set-of-marks visual = fallback, активируемый при `completeness_ratio<0.30` И измеренном PoC | Accepted | ARIA roles/names семантичны, устойчивы к ресайзу, дёшевы, дают пригодные selectors. **Отклонено:** screenshot-primary (стоимость, хрупкость); full-DOM snapshot (token blowout) |
| ADR-006 | 2026-06-23 | Explore-once / replay-many с `plan_hash` HARD-ABORT при replay + неизменяемые golden baselines (обновление только командой оператора) | Accepted | Замороженный план — единственная надёжная гарантия воспроизводимости (нет детерминизма провайдера даже при T=0). **Отклонено:** seeded/T=0 LLM как механизм детерминизма; auto-regenerate plan при устаревании (фатальный изъян P2); HAR replay |
| ADR-007 | 2026-06-23 | Go store-gateway = единственный writer одного основного SQLite (WAL); Postgres + AsyncPostgresSaver отложены до M5 за явным триггером | Accepted | Zero-ops, backupable через cp, single-writer + concurrent-readers соответствует паттерну доступа; per-job SQLite для параллелизма CI. Postgres-compatible schema. **Отклонено:** Postgres с первого дня; прямой доступ Python к DB |
| ADR-008 | 2026-06-23 | Grounded, calibrated confidence: per-strategy priors + empirical discounts + ОБЯЗАТЕЛЬНЫЙ live-DOM probe verify-before-accept + post-heal verification + scheduled calibration | Accepted | Каждый LLM/visual кандидат повторно проверяется live (confidence обнуляется при отсутствии); `calibrate` пересчитывает precision/recall относительно human-verified; cold-start threshold поднят до 0.90. **Отклонено:** raw LLM self-report против magic thresholds без пути к калибровке |
| ADR-009 | 2026-06-23 | Разделение моделей: Opus 4.8 для explore/plan, Sonnet 4.6 для healing | Accepted | Качество планирования — ключевой дифференциатор (выполняется один раз за explore); healing ограничен, структурирован, в replay hot path (latency/cost). **Отклонено:** uniform Opus (5–8× стоимость); local model (VERIFY достаточность home-lab GPU при пересмотре) |
| ADR-010 | 2026-06-23 | Исследование завершается при ИЗМЕРИМОЙ цели охвата (доля обнаруженных интерактивных элементов, задействованных + пустой nav frontier); бюджет = подстраховка | Accepted | Закрывает сквозную неопределённость: флаг LLM «done» ограничивает, но не конвергирует. **Отклонено:** только флаг `exploration_complete` LLM; только cap фиксированной глубины |
| ADR-011 | 2026-06-23 | Pluggable planner: `HeuristicPlanner` (по умолчанию, offline, детерминированный, zero-cost) + `LLMPlanner` (Opus 4.8, опционально через `--planner llm`, fallback на heuristic) | Accepted | Позволяет верифицировать M1 explore-gate offline / в CI без сети или LLM-расходов, а также служит путём graceful-degradation при исчерпании бюджета (согласовано с §8). LLM остаётся основным «умным» исследователем при наличии ключа. **Отклонено:** план только на Opus (нетестируемый offline, токены за каждый smoke-прогон, блокирует CI) |
| ADR-012 | 2026-06-23 | M2 доставлен heal-engine-first: детерминированная L1–L6 rotation + verify-before-accept + confidence gate + minimal replay, с **промежуточным brain-local SQLite store**; Go store-gateway+gRPC+proto (M2b) и MCP-SDK transport отложены | Accepted | Self-healing — ценность M2, тестируется offline; gRPC/store-gateway — инфраструктура, лучше вводить отдельно. Heal требует триггера stale-locator → minimal replay вытащен вперёд (без M3 trust layer). Промежуточный local store — **задокументированное временное отклонение** от ADR-007 (single-writer), восстанавливается в M2b. **Отклонено:** полный пакет M2 сразу (высокий integration risk, нетестируемо в gated/offline среде) |
| ADR-013 | 2026-06-23 | Heal и golden-diff **сосуществуют**: исправленный шаг всё равно выполняется И его страница всё равно проходит golden-diff, поэтому drift, исправленный через testid, также вызывает a11y golden regression (exit 2). Golden baselines **привязаны по URL basename** (cross-base comparison). M3 gRPC orchestrator остаётся в M2b | Accepted | Healing = robustness теста (продолжать выполнение); golden-diff = обнаружение изменений (сигнализировать об изменении). Они отвечают на разные вопросы и должны срабатывать оба. Page-basename keying позволяет диффить план, исследованный на site/, против site-v2/. **Отклонено:** считать heal подавляющим сигнал regression (скрыло бы реальные изменения приложения) |
| ADR-014 | 2026-06-24 | M4 report / `.spec.ts` export / metrics / calibrate реализованы как **brain (Python) generators**, читающие run artifacts + interim store; Go `report-service` (§2) и OTel→Tempo / Prometheus HTTP endpoint отложены до консолидации persistence в M2b | Accepted | Ценность для пользователя (читаемые отчёты + экспортированные тесты) — чистая генерация, тестируемая offline сейчас; Go-сервис, читающий brain-local SQLite до M2b, потребует переработки. **Отклонено:** сборка report-service на Go до M2b (дублирует wiring persistence, который M2b реструктурирует) |
| ADR-015 | 2026-06-24 | M2b-1: `store-gateway` = Go gRPC-сервис (единственный SQLite writer), порождаемый agentctl через Unix-domain socket; `brain/store.py` переписан как тонкий gRPC-клиент с сохранением точного метода-интерфейса (drop-in), чтобы healing/replay/calibrate оставались неизменными. Восстанавливает ADR-007 | Accepted | Чистый интерфейс Store позволяет заменить SQLite→gRPC с минимальными изменениями call sites; agentctl-as-supervisor избегает отдельного daemon для local/CI. **Отклонено:** Python продолжает писать SQLite (сохраняет отклонение ADR-012); standalone always-on daemon (операционная нагрузка для local-прогонов) |
| ADR-016 | 2026-06-24 | M2b-2: pw-executor мигрирует на MCP SDK (`@modelcontextprotocol/sdk` server); brain оборачивает MCP stdio-клиент за существующим интерфейсом `Executor.call`; hand-rolled JSON-RPC сохраняется как задокументированный fallback | Accepted | Реализует ADR-002 (нативный LangGraph MCP tool binding) и закрывает GAP-VERIFY-002; wrapper сохраняет graph/healing/replay неизменными и снижает риски сюрпризов SDK-API. **Отклонено:** остаться на bespoke JSON-RPC навсегда (расходится с архитектурной целью MCP) |
| ADR-017 | 2026-06-24 | M5: поставить как containerized **K8s CronJob** через Helm chart + ArgoCD Application (home-lab GitOps); set-of-marks visual heal — **Tier-7 scaffold, отключён** до PoC, измеряющего ≥70% точности на 20 реальных сценариях сломанных selectors (ADR-005); Postgres checkpointer — opt-in (`CHECKPOINT_DSN`) | Accepted | CronJob соответствует модели explore-once/replay-many (scheduled CI-style replays) и вписывается в ArgoCD GitOps на существующем K3s; visual heal дорог и недетерминирован, поэтому должен доказать ценность перед выкаткой. **Отклонено:** always-on Deployment (агент — batch, а не сервис); включение visual heal без измерений (токен cost + нестабильность) |
| ADR-018 | 2026-06-24 | M4b: observability = brain OTel tracing (prompt_HASH, не содержимое; OTLP export через `OTEL_EXPORTER_OTLP_ENDPOINT`, no-op по умолчанию) + Prometheus **Pushgateway** для batch metrics. Always-on Go report-service HTTP `/metrics` **удалён** в пользу push, поскольку агент — ephemeral CronJob, а не scrapeable сервис | Accepted | Distributed traces — реальный выигрыш в observability и работают с нулевыми накладными расходами при отсутствии collector; batch job нельзя опрашивать по HTTP, поэтому push/textfile — правильная интеграция с Prometheus. **Отклонено:** HTTP `/metrics`-сервер в job, завершающемся за секунды (нечего scrape); помещение содержимого prompt в spans (утечка секретов) |
| ADR-019 | 2026-06-25 | M6: **провайдер-агностичный LLM-backend.** Узлы planner + heal вызывают `brain/llm.LLMBackend` (`AnthropicBackend` нативно \| `OpenAICompatBackend` для ChatGPT/DeepSeek/Qwen/Gemini-compat/OpenRouter/Ollama/vLLM); выбор per-role через env (`LLM_BACKEND[_PLANNER\|_HEAL]`, `_MODEL`, `_BASE_URL`, `_API_KEY`, `_VISION`); `make_backend()` → `None` при отсутствии ключа/SDK ⇒ сохраняется fallback (heuristic / L1–L6). LLM-путь **best-effort, без гарантии `plan_hash`**; `HeuristicPlanner` — детерминированный якорь, golden baselines — heuristic-only | Accepted | Снимает привязку к одному провайдеру (запрос пользователя: Qwen/Deepseek/Gemini/ChatGPT/роутеры) без слома детерминизма — модель влияет только на explore-артефакт, replay LLM-free. Per-role split сохраняет ADR-009 (Opus explore / Sonnet heal как дефолты при нуле env). Vision гейтится `supports_vision` (text-only провайдер пропускает Tier-7). **Отклонено:** один глобальный backend (ломает per-role split ADR-009); жёсткая зависимость от LiteLLM в hot path (чужая абстракция; оставлена как опция) |
| ADR-020 | 2026-06-25 | M7: экспонировать brain как **MCP-сервер** (`brain/server.py`, FastMCP; tools explore/heal/replay/report), отдельный от MCP-сервера pw-executor; host (OpenCode/Kilocode/Claude Desktop) драйвит его и через MCP `sampling/createMessage` поставляет модель — реализовано как `SamplingBackend(LLMBackend)` поверх абстракции ADR-019 | Accepted | Закрывает второе направление запроса пользователя («работать из агентов-хостов»). Абстракция B1 (ADR-019) sampling-совместима: `SamplingBackend.supports_vision=False`, токены 0, `LLMResult.model` несёт реальную модель хоста, sync↔async мост как `McpExecutor`, sync-граф в worker-thread (loop свободен для встречного sampling). **Доставлен offline-verified (test_m7); живой MCP-host — user-run (GAP-VERIFY-006).** **Отклонено:** делать B2 до B1 (sampling — частный случай backend, требует абстракции первой) |
| ADR-021 | 2026-06-26 | M8 (Full GAP-OBS-001): (1) distributed tracing W3C через Go/Python/TS (gated OTLP); (2) hard budget ceiling — Python `BudgetTracker` (graceful degradation→heuristic/L1–L6) + долгоживущий Go `orchestrator` (gRPC `RunControl`, token-reconcile, SIGTERM-kill); (3) Go `report-service` (HTTP `/report`+`/metrics`); новый `proto/runcontrol.proto` | Accepted (дополняет ADR-018) | **Дополняет, не противоречит ADR-018:** Pushgateway остаётся для ephemeral CronJob (batch); HTTP report-service — только для долгоживущего orchestrator/service-режима (scrapeable). Вводит orchestrator, обещанный в §2, но отсутствовавший в коде. Python budget + W3C + per-node spans — offline-verified; Go/TS + live OTLP + реальный kill — user-run. **Отклонено:** HTTP `/metrics` в ephemeral job (ADR-018 — нечего scrape); budget-ceiling без Go-backstop (model-cooperative kill ненадёжен) |
| ADR-022 | 2026-06-26 | M9: goal-directed / NL-авторинг тестов через **explore-first grounding** — новый `GoalPlanner` (NL-цель + живая карта элементов → шаги) в шве Planner (ADR-011); `--mode explore\|goal\|describe` + авто-дефолт (нет цели → чистый explore) | Proposed | Explore-first не даёт LLM галлюцинировать селекторы; покрывает бизнес-процессы поверх coverage-explore. **Отклонено:** describe-first дефолтом (LLM выдумывает несуществующие элементы) |
| ADR-023 | 2026-06-26 | M9: доступ к чату двумя путями — **MCP** (brain-as-MCP-server, M7) И **не-MCP** тонкий HTTP/gRPC control-API; ветка чат-UI сейчас (OSS-фронт в DH/Docker), браузерное расширение — позже | Proposed | «И MCP, и не-MCP» по требованию пользователя; не-MCP нужен для CI/скриптов и чат-фронтов без MCP. **Отклонено:** только-MCP (заперло бы интеграции) |
| ADR-024 | 2026-06-26 | M9: режимы выполнения браузера — own-headless (сейчас) → headed → **CDP-attach к браузеру пользователя** (`connectOverCDP`) → **co-pilot takeover/return** (human-in-the-loop) | Proposed | Поддерживает «работать в браузере пользователя» + перехват/возврат управления (ветка расширения). **Отклонено:** только own-headless навсегда (нет live-авторинга) |
| ADR-025 | 2026-06-26 | M9: универсальность (не только DH) через **pluggable adapters** — auth (none\|basic\|OIDC/Keycloak\|storageState) · deploy (CronJob\|Docker\|CLI) · model (cloud\|local Ollama/vLLM) · trace/metrics (любой OTLP/Prom); DH-специфика изолирована в Helm values | Proposed | Ядро уже агностично (target=URL); адаптеры по краям держат продукт переносимым. **Отклонено:** вшивать DH/Keycloak в ядро |
| ADR-026 | 2026-06-26 | M9.1: pw-executor interaction/auth/assert примитивы — `fill`/`type`(`pressSequentially`)/`press`/`select`/`expect`(non-throwing, base-waits)/`saveStorageState`, в **обоих** транспортах; секреты через env-`secretRef` (резолв только внутри pw-executor, **никогда** в plan/transcript/heal-report/rec/trace); auth-прогоны отключают Playwright-tracing (`PW_NO_TRACE`); storageState load (`STORAGE_STATE`)/save (`STORAGE_STATE_SAVE`); новые виды шагов исполняются в replay/graph/exporter (шаг read-only → plan_hash стабилен) | Accepted | Блокер №1 (формы/логин/негативное тестирование, M9_CONTRACT §A1). Tracing-gate — единственная корректная защита `trace.zip` (секрет утекает телом submit-POST + DOM-снапшотом; pause/mask API у Playwright нет). **Отклонено:** `@playwright/test` ради `expect` (GAP-ARCH-001 — держим pw-executor тонким; base-`waitFor`/`waitForURL` достаточно); пауза tracing вокруг `fill` (не покрывает submit-POST) |
| ADR-027 | 2026-06-26 | M9.2a GoalPlanner: goal-directed планировщик с `grounding` в шве `Planner` (ADR-011) — LLM выбирает **индекс** из реальных кандидатов живой карты, `propose` возвращает только `candidates[idx]`/`done` (OOB → done) ⇒ галлюцинация селектора невозможна (ADR-022); режим авторинга по наличию `--goal` (авто-дефолт §C) + `PLANNER=goal`/RunConfig `mode` — **не** через `--mode` (= `RUN_MODE`); минимальный RunConfig YAML (mode/goal/planner/budgets; приоритет флаг>файл>дефолт); `make_planner(env)` фабрика; goal-режим best-effort (не `plan_hash`-стабилен, как ADR-019; replay детерминирован) | Accepted | «Описать цель словами → план, привязанный к реальным элементам»; heuristic остаётся детерминированным якорем + путём деградации. **Отклонено:** `--mode goal` (коллизия с `RUN_MODE`); авто-определение сложности (сигнал = наличие цели); двухфазный explore-then-scenario (§L) + describe-first (§B) — отложены в M9.2b |
| ADR-028 | 2026-06-27 | M9.2b двухфазный авторинг (§L/§B): goal/describe → полный детерминированный heuristic-explore (карта сайта, обобщённая за пределы кнопок на input/select/link) → **one-shot привязанная голова фазы-2** (`GoalPlanner.build_scenario` / `DescribePlanner.draft`+детерминированный `reconcile` в новом `brain/scenario.py`); кросс-страничные navigate синтезируются в коде; авторские шаги несут полный привязанный `locator`+`alternatives` (replay LLM-free детерминирован); `plan.json`(walk+scenario)+`scenario.json`+`reconcile-report.json`; богатый RunConfig (декларативные auth/scenarios + `--scenario`) | Accepted (заменяет проводку ADR-027) | Завершает conversational-авторинг M9. LLM не ведёт walk (фаза-1 детерминирована); per-step `propose` сохранён для M9.4 live/co-pilot. describe-unmatched→exit 1; `GOAL`⊕`DESCRIBE`→exit 3. **Отклонено:** per-step goal-планировщик над all-pages меню (ad-hoc navigate-синтез); two-sub-graph (один scenario-узел проще); auth как новый adapter (M9.7 — здесь декларативно в env) |
| ADR-029 | 2026-06-27 | **Локальные модели = config-решение (не новый код).** planner/heal/vision работают на любом локальном OpenAI-compatible эндпоинте (Ollama/vLLM/llama.cpp/LM Studio) через **существующие per-role env** (ADR-019: `LLM_BACKEND[_PLANNER\|_HEAL]`/`_MODEL`/`_BASE_URL`/`_API_KEY`/`_VISION`) — **без нового «profile»-knob** (provider-профили документируются, не кодируются). Методика выбора платформо-агностична: VRAM-sizing (`params·bytes(quant)+KV-cache+overhead`) + token-cost-per-phase (из верифицированных `max_tokens`: explore 200/scenario 800/heal-text 200/heal-vision 100; бюджеты PLAN 50k/HEAL 20k; replay LLM-free) + каталог моделей/runtime — в `docs/LOCAL_MODELS.md` + 3 интерактивных калькулятора (Pages). In-code дефолтные model-ids остаются `claude-*` (offline=FakeBackend; реальный local — opt-in через документированные env-профили) | **Accepted (supersedes отложенность local-моделей в ADR-009; опирается на механизм ADR-019)** | Запрос пользователя: local+cloud оба. Механизм уже есть (M6/ADR-019) — недоставало платформо-агностичной **методики** (ADR-009 откладывал local за «VERIFY достаточности home-lab GPU»; RTX 2060 12GB — теперь ОДИН пример среди тиров 8/12/16/24 ГБ, не основа). **Отклонено:** новый `profile`-knob (per-role env достаточно — лишняя поверхность); привязка дефолтов к local (ломает offline-детерминизм/CI/golden) |
| ADR-030 | 2026-06-27 | **Стратегия дистрибуции и упаковки** — секвенированный эпик (контракт `docs/DISTRIBUTION.md`): docker-compose one-command quickstart (этот цикл) → GitHub Releases (мульти-OS/arch бинарники agentctl/store-gateway/orchestrator/report-service + Docker publish + checksums + **Cosign/GPG** подпись, **M11.1**) → setup-WebUI (**M11.2**) → Helm/Flux/Argo расширение + **Secret-плумбинг** (**M11.3**, закрывает GAP-SEC-001) → air-gapped bundle (**M11.4**) → zero-level onboarding/installer (**M11.5**). Этот цикл закрывает hardening-предпосылку (SCA-гейты §1 CI + threat-model) | Accepted | Release без hardening (SCA/SBOM/lockfile/подпись + модель угроз) не заслуживает доверия → foundation сначала, остальное — docs-first freeze. **Отклонено:** всё-сразу одним релизом (4–5 milestone'ов across release-eng/containers/GitOps/frontend — высокий integration-risk) |
| ADR-031 | 2026-06-27 | **setup-UI: static-now / control-API-later.** Фаза-1 — статический клиентский генератор конфигурации (vanilla JS, без бэкенда, air-gapped — генерит RunConfig YAML/env-блок; родственно Pages-калькуляторам); фаза-2 — backed control-API (**brain HTTP control-API, M9.3**) для смены mode/API-keys/целей без DevOps. **M11.2** | Accepted | Статический генератор даёт ценность сразу и air-gapped-friendly (zero-external-dep, как калькуляторы); live-WebUI требует control-API, которого ещё нет (→ M9.3). **Отклонено:** сразу live-WebUI (нужен непостроенный бэкенд + секрет-handling в браузере); вообще без UI (zero-level-user не может править конфиг) |

> Шаблон ADR для новых решений:
> ```
> ### ADR-NNN: Title
> - Date / Status (Proposed/Accepted/Deprecated/Superseded) / Context / Decision / Consequences
> ```

---

## 4. Ограничения

| Ограничение | Тип | Влияние | Смягчение |
|-------------|-----|---------|-----------|
| **BUILD-ONLY — никаких готовых продуктов** (§0) | Бизнес/стратегический | Необходимо написать TS Playwright executor самостоятельно; нельзя использовать `@playwright/mcp` | ADR-001; тонкий инструментальный слой над стабильным Playwright lib API; contract tests; pin version |
| LLM non-determinism | Технический | Автономный исследователь не bit-воспроизводим | Explore-once/replay-many + `plan_hash` hard-abort (ADR-006) |
| LLM token cost | Бизнес | Исследование крупных SPA дорогостояще (Opus) | Coverage convergence (ADR-010), per-run budgets, graceful degradation, Go-side hard ceiling |
| Слепые пятна a11y-tree | Технический | Shadow DOM / canvas / custom elements / cross-origin iframes дают частичное восприятие | Метрика `completeness_ratio` → gated visual fallback; рекомендуется добавить в AUT `data-testid`/ARIA |
| Цель — home-lab (K3s/ArgoCD/Proxmox/Ceph) | Технический | Разворачивание как GitOps-сервиса в M5 | Helm chart + ArgoCD Application; замена на Postgres за триггером |
| Только Chromium на MVP | Технический | Golden a11y/screenshot hashes различаются для разных движков | Firefox/WebKit отложены; переносимость baseline — открытый вопрос |

---

## 5. Принципы
1. **Доверие — это продукт** — недетерминированный LLM-исследователь должен быть *структурно* неспособен молча переписать свой baseline или запустить подменённый план.
2. **Строить, владеть, контролировать** — никаких готовых сторонних продуктов; мы владеем каждой границей (build-only суверенитет).
3. **Ничего не покупать, но переиспользовать OSS-библиотеки** — писать код против Playwright/LangGraph/Anthropic SDK; строить *компоненты*, а не *примитивы*.
4. **Откладывать инфраструктуру за именованными триггерами** — gRPC в M2, Postgres в M5, OTel в M4 — никакого спекулятивного gold-plating.
5. **Верифицировать до доверия** — каждый исправленный locator повторно проверяется против live DOM до принятия; каждый confidence threshold откалиброван, никогда не является magic constant.
6. **Измерять конвергенцию, не утверждать её** — метрика охвата вместо флага LLM «done».

---

## 6. История изменений
| Дата | Изменение | ADR | Автор |
|------|-----------|-----|-------|
| 2026-06-23 | Исходная архитектура из процесса проектирования (4 архитектора → 3 judge → synthesis) | ADR-001..010 | @AlexGromer / Claude |
| 2026-06-23 | BUILD-ONLY override: ADR-001 перевёл BUY→BUILD (`pw-executor` написан in-house) | ADR-001 | @AlexGromer |
| 2026-06-23 | M0 (Hello Browser) доставлен: Go→Python→TS wire + trace.zip (commit e6844ba) | — | @AlexGromer |
| 2026-06-23 | M1 начат: LangGraph StateGraph + pluggable planner (heuristic по умолчанию + Opus опционально) | ADR-011 | @AlexGromer |
| 2026-06-23 | M1 доставлен: LangGraph 9-node explore, детерминированный plan.json (8 шагов, coverage 1.0); добавлено docs-first руководство разработчика | ADR-011 | @AlexGromer |
| 2026-06-23 | M2 heal-core доставлен: детерминированный L1–L6 self-heal + verify-before-accept + minimal replay (healed=2/0 на drifted fixture); gRPC/store-gateway вынесен в M2b | ADR-012 | @AlexGromer |
| 2026-06-23 | M3 начат: replay trust layer — plan_hash hard-abort, exit codes 0/1/2/3, dual golden baselines, AUT-SHA flake quarantine, GitHub Actions | ADR-006, ADR-013 | @AlexGromer |
| 2026-06-23 | M3 доставлен: trust layer live-green (CLEAN 0 / DRIFT heal+a11y-regression 2 / tampered 3); first-landing golden symmetry + visual-advisory (GAP-RISK-009); offline test suite + CI workflow | ADR-006, ADR-013 | @AlexGromer |
| 2026-06-24 | M4 начат: .spec.ts export, HTML+JSON report, Prometheus textfile metrics, agentctl calibrate (brain generators) | ADR-014 | @AlexGromer |
| 2026-06-24 | M4 core доставлен: .spec.ts export + HTML/JSON/Prometheus report + calibrate (offline-verified, 8 tests); OTel/Prometheus-HTTP/Go-report-service → M4b | ADR-014 | @AlexGromer |
| 2026-06-24 | M2b начат: spec для Go store-gateway+gRPC+proto (M2b-1) и MCP-SDK transport (M2b-2); split, интерфейс store.py сохранён | ADR-015, ADR-016 | @AlexGromer |
| 2026-06-24 | M2b-1 доставлен: Go store-gateway + gRPC + proto, live-verified (gate 0/2/3 over gRPC); store.py drop-in LocalStore/GrpcStore; socket→/opt + GOTMPDIR fixes; prod path no sqlite handle | ADR-015 | @AlexGromer |
| 2026-06-24 | M2b-2 доставлен: pw-executor dual transport (JSON-RPC по умолчанию + MCP SDK opt-in), brain McpExecutor за Executor.call; offline-verified, JSON-RPC неизменён (закрывает GAP-VERIFY-002) | ADR-016 | @AlexGromer |
| 2026-06-24 | M5 начат: spec — deployment (Dockerfile + Helm CronJob + ArgoCD, M5-1), set-of-marks visual heal Tier-7 scaffold за PoC-gate ≥70% (M5-2), опция Postgres checkpointer (M5-3) | ADR-017 | @AlexGromer |
| 2026-06-24 | M5-1 доставлен: Dockerfile (multi-stage) + Helm chart (CronJob + per-env values + optional Ceph PVC) + ArgoCD Application; helm lint clean, renders ConfigMap/CronJob/PVC/SA | ADR-017 | @AlexGromer |
| 2026-06-24 | M5-2 доставлен: set-of-marks browser tool + HealingEngine Tier-7 visual heal (gated HEAL_VISUAL, mark→real locator, FLAGGED band); offline-tested (mock vision); реальный Sonnet-vision PoC gated/user-run | ADR-017 | @AlexGromer |
| 2026-06-24 | M5-3 доставлен: Postgres checkpointer opt-in (CHECKPOINT_DSN → PostgresSaver else SQLite, near drop-in); SQLite по умолчанию неизменён, offline-verified | ADR-017 | @AlexGromer |
| 2026-06-24 | M4b начат: OTel brain tracing (prompt_HASH, OTLP-gated no-op default) + Prometheus Pushgateway для batch metrics; Go report-service HTTP / TS+Go spans / budget-ceiling отложены (GAP-OBS-001) | ADR-018 | @AlexGromer |
| 2026-06-24 | M4b доставлен: brain OTel (sentinel.run + heal.llm spans, prompt_HASH, OTLP-gated no-op default) + Prometheus Pushgateway; offline-verified, suites green | ADR-018 | @AlexGromer |
| 2026-06-25 | M6 доставлен: провайдер-агностичный LLM-backend (`brain/llm.py`: AnthropicBackend + OpenAICompatBackend + make_backend per-role); planner/heal через `LLMBackend`; default-path (Anthropic/heuristic) неизменён, vision гейтится `supports_vision`; offline-verified (test_b1 8 + test_m5 4, регресс m3/m4/m4b зелёный); реальный smoke к провайдерам — user-run (сеть заблокирована) | ADR-019 | @AlexGromer |
| 2026-06-25 | M7 контракт заморожен (Proposed): MCP-server exposure + `SamplingBackend` поверх ADR-019; имплементация — следующая сессия (нужен живой MCP-host) | ADR-020 | @AlexGromer |
| 2026-06-26 | M7 доставлен: brain MCP-сервер (`brain/server.py`, FastMCP — tools explore/heal/replay/report) + `SamplingBackend` (host поставляет модель через sampling; sync-граф в worker-thread); `mcp` в deps; offline-verified (test_m7 5 + регресс зелёный); живой MCP-host — user-run (GAP-VERIFY-006) | ADR-020 | @AlexGromer |
| 2026-06-26 | M8 начат (Full GAP-OBS-001): contract + ADR-021 (amends ADR-018); Python budget-аккумулятор + W3C propagation + per-node spans — offline; Go orchestrator/report-service + TS spans + proto/runcontrol — user-run build | ADR-021 | @AlexGromer |
| 2026-06-26 | M8 доставлен (Full GAP-OBS-001): distributed tracing (W3C brain→pw-executor→store-gateway: executor `_meta` + store.py gRPC interceptor + pw-executor `otel.ts` + store-gateway `otelgrpc.StatsHandler` + per-node spans) + budget ceiling (Python `BudgetTracker` + Go `orchestrator` RunControl + SIGTERM backstop) + Go `report-service` (HTTP). Все 3 языка инструментированы и **compile/test-verified** (Python 36 offline + go build/vet/test + tsc clean). Остаётся observe end-to-end: live OTLP-trace + реальный budget-kill | ADR-021 | @AlexGromer |
| 2026-06-26 | M9 дизайн заморожен (Proposed): conversational & goal-directed testing — `fill`/`type` + auth, `GoalPlanner` (NL-авторинг, explore-first), чат-UI (MCP + не-MCP), in-app/browser tabs, backend trace-корреляция, browser-режимы (headed/CDP-attach/co-pilot), pluggable adapters (универсальность не-только-DH); `docs/M9_CONTRACT.md`, GAP-M9-01..08 | ADR-022..025 | @AlexGromer |
| 2026-06-26 | M9.1 доставлен (offline): pw-executor `fill/type/press/select/expect/saveStorageState` (оба транспорта) + storageState auth (`STORAGE_STATE`/`STORAGE_STATE_SAVE`) + tracing-gate (`PW_NO_TRACE`) + секреты через `secretRef`; brain replay/graph/exporter исполняют новые виды шагов (шаг read-only); `brain/validation.py` (генератор невалидных вводов — набросок); `tests/test_m9_offline.py` (19). Adversarial-review hardening: fail-closed секрет-при-трейсе (throw + brain exit 3), corrupt-storageState fallback, `setDefaultTimeout`. Gates: `tsc` + offline-сьют m3..m9 + `go build` + gitleaks. `docs/M9.1_CONTRACT.md`. Живой UI-прогон (формы/Keycloak-логин) — отдельно по «go» | ADR-026 | @AlexGromer |
| 2026-06-26 | M9.2a доставлен (offline): `GoalPlanner` (goal-directed планировщик с `grounding` в шве `Planner`, ADR-027) + `make_planner` авто-дефолт по `--goal` + `brain/runconfig.py` (минимальный RunConfig YAML, приоритет флаг>файл>дефолт) + agentctl `--goal`/`--run-config`; goal-режим best-effort (replay детерминирован); `pyyaml` в deps; `tests/test_m9_2_offline.py`. Gates: offline m3..m9_2 + `go build`/`vet` + `tsc` + gitleaks. `docs/M9.2_CONTRACT.md`. Отложено в M9.2b: describe-first, двухфазный explore-then-scenario, auth/scenarios в RunConfig. Живой goal-прогон — по «go» | ADR-027 | @AlexGromer |
| 2026-06-27 | M9.2b доставлен (offline): двухфазный goal (§L) + describe-first (§B) + богатый RunConfig (ADR-028). Карта сайта обобщена на input/select/link; `brain/scenario.py` (ground_scenario/reconcile + кросс-страничный navigate-синтез); `GoalPlanner.build_scenario` + `DescribePlanner`; scenario-узел графа; `scenario.json`/`reconcile-report.json`; agentctl `--describe`/`--scenario`; декларативные auth/scenarios в RunConfig. Терминология: «грауденный»→`grounding`/«привязка к реальным элементам». `tests/test_m9_2b_offline.py`. Gates: offline m3..m9_2b + `go build`/`vet` + `tsc` + gitleaks. `docs/M9.2b_CONTRACT.md` | ADR-028 | @AlexGromer |
| 2026-06-27 | **Foundation cycle**: security CI-гейты (gitleaks/govulncheck/pip-audit/npm audit + `go vet`/`go test` + offline-suite m3..m9_2b в CI — закрывает docs-vs-reality + GAP-SEC-002 частично); Dockerfile dep-fix (`openai`+`pyyaml`); `docker-compose.yml` (sentinel + ollama + demo profiles); GitHub Pages (`pages.yml`+`docs/index.md`+`_config.yml`) + 3 калькулятора (VRAM · token-cost · model-selector, vanilla JS, air-gapped); `docs/{LOCAL_MODELS,THREAT_MODEL,TESTING,DISTRIBUTION}.md` (+en); L1–L5 fixtures; GAP-OPS-001/002 + GAP-SEC-001/002; BACKLOG M11.1–M11.5 + M9-LIVE | ADR-029, ADR-030, ADR-031 | @AlexGromer |
| 2026-06-27 | Post-Foundation: **setup-WebUI** (статический генератор конфигурации, vanilla JS, ADR-031 фаза-1) + Docker **`webui`-бандл** (air-gapped, `python http.server` на :8088, ассеты в `/app/docs`); security-hardening — **GAP-OPS-002 DONE** (`PW_IGNORE_HTTPS_ERRORS` opt-in + cert-классификация в `pw-executor`) + **GAP-SEC-001 PARTIAL** (opt-in env-allowlist в `agentctl`, `SENTINEL_ENV_ALLOWLIST`, default OFF) | ADR-031 | @AlexGromer |
| 2026-06-27 | Волна A (M9.4+M9.5, offline): **M9.4** in-app tabs perception (`[role=tab]` в interactives/setOfMarks, A5) + browser multi-page (`browser.tabs`/`browser.switchTab` + `context.on('page')`, A6); **M9.5** `traceparent`-инъекция во все запросы браузера (`context.route`, gated на OTLP, §I backend-корреляция). `pw-executor` tsc clean; fixture `l6-newtab.html`; live-verify по «go». `docs/M9.4_CONTRACT.md` | ADR-022/024 | @AlexGromer |

---

## 7. Где живут детали (`docs/`)
| Файл | Содержимое |
|------|------------|
| `docs/STATE_MACHINE.md` | Полный LangGraph: 9 узлов, все рёбра (в т.ч. conditional/heal), схема shared объекта `RunState` |
| `docs/SELF_HEALING.md` | 10-шаговый алгоритм self-healing, L1–L6 strategy priors, confidence gate, калибровка |
| `docs/DETERMINISM.md` | Explore-once/replay-many, `plan_hash` hard-abort, иммутабельные golden baselines, flake quarantine, exit codes |
| `docs/MEMORY_PERSISTENCE.md` | Краткосрочная/долгосрочная память, SQLite schema (все таблицы), checkpoint GC |
| `docs/OBSERVABILITY.md` | OTel tracing, LLM transcript, token budget + hard caps, Prometheus metrics |
| `docs/OUTPUTS.md` | 10 генерируемых артефактов |
| `docs/ROADMAP.md` | Вехи M0→M7 с acceptance gates в формате Given/When/Then + build-only deltas |
| `docs/M6_CONTRACT.md` | Контракт M6: провайдер-агностичный LLM-backend (`brain/llm.py`, ADR-019) |
| `docs/M7_CONTRACT.md` | Контракт M7 (Proposed): экспонирование brain как MCP-сервер + `SamplingBackend` (ADR-020) |
| `docs/DESIGN_RECORD.md` | Полная история проектирования: 4 архитекторских предложения + 3 вердикта judge + история решений synthesis |
| `GAPS.md` | Открытые вопросы, VERIFY items, риски, последствия build-only |

## 8. Топ-риски (сводка — полный список в GAPS.md)
1. **`pw-executor` теперь на критическом пути** (build-only): наибольший единичный объём сборки + постоянная поддержка Playwright-API-churn. *Смягчение:* тонкий инструментальный слой над стабильным Playwright Locator/accessibility API; contract tests; pin version.
2. **Cold start модели confidence** — отсутствие human-verified результатов на начальном этапе. *Смягчение:* порог по умолчанию 0.90 до N размеченных примеров; verify-before-accept + post-heal verify — model-independent gates.
3. **Token-cost blowout на крупных SPA.** *Смягчение:* coverage convergence, per-page budget, graceful degradation, incremental explore.
4. **Задержки heal-storm в детерминированном replay hot path.** *Смягчение:* hard cap в 2 попытки + per-step deadline + amortization кешированных locators (`dom_subtree_hash`).
5. **Хрупкость `dom_hash`.** *Смягчение:* хешировать целевой SUBTREE, не страницу; CSS ignore-list.
6. **Полное наследование env + plaintext-секреты в Helm** (GAP-SEC-001): `agentctl` передаёт `os.Environ()` без allowlist (main.go:68); Helm CronJob инжектит env как plaintext `value:` (cronjob.yaml:34–46). *Смягчение (частичное / планируемое):* секреты AUT уже идут через `secretRef`+`PW_NO_TRACE` (GAP-RISK-010, MITIGATED); env-allowlist + `secretKeyRef`-плумбинг — **M11.3**. Полная модель угроз — `docs/THREAT_MODEL.md`.
7. **Supply-chain** (GAP-SEC-002): исторически без SCA в CI, Python-deps без lockfile, релизы без подписи/SBOM. *Смягчение:* **этот цикл** добавил gitleaks/govulncheck/pip-audit/npm audit-гейты (§1); lockfile + Cosign/GPG-подписанные релизы + SBOM — **M11.1**.

---

## Расширения по типу — Разработка

### Сборка и CI
- **Языки/инструменты:** Go (control-plane), Python 3.x (brain, LangGraph), TypeScript/Node (pw-executor, Playwright).
- **CI:** GitHub Actions — задача `explore` (conditional/manual) + матрица `replay`; proto codegen + утверждение `.proto`-hash (M2); gitleaks secrets scan; per-job SQLite для параллельного replay.
- **Pre-commit:** gitleaks; `.claude/` git-ignored (никогда не коммитится).

### Стратегия тестирования
- **Разбивка self-test:** Go unit (orchestrator FSM, budget reconciliation), Python unit (логика узлов, confidence model), TS unit (инструментальный слой pw-executor), contract tests (proto stubs, MCP tool schema), e2e (агент против fixture app).
- **Acceptance = milestone gates** (`docs/ROADMAP.md`), выраженные в формате Given/When/Then с пороговыми значениями.

### Зависимости (OSS-библиотеки — разрешены по build-only)
Playwright (pinned), LangGraph + checkpointer, Anthropic SDK, gRPC/protobuf (buf/protoc), SQLite (WAL), Prometheus client, OpenTelemetry SDK. **Никаких готовых сторонних серверов/SaaS.**
