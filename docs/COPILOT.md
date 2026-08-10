# Sentinel — Co-pilot: видение, статус, roadmap

> 🌐 **Русский** (основная версия) · [English](COPILOT.en.md)

> **ADR-046** · **Дата**: 2026-07-12 · **Статус**: видение + roadmap (M13–M15 доставлены; авторитетный статус — `../ARCHITECTURE.md` §6 + `../BACKLOG.md`)

Этот документ сводит **полное co-pilot-видение** Sentinel с **фактическим состоянием** и **планом по волнам**
(мои + контрибьютора @0xCoDSnet). Он разрешает накопившийся рассинхрон «ожидание ↔ реализация»
(мульти-тёрн чат, запуск внутри инструмента, перехват управления). Статусы здесь — честные:
**DONE / scaffold / design-only / not-built**. Авторитетные детали: `ARCHITECTURE.md` §3/§6, `BACKLOG.md`, `GAPS.md`.

## 1. Конечная цель и слои

**Цель:** инструмент, в котором тест **описывается словами**, Sentinel **сам исследует приложение**
(можно вживую в браузере), **собирает воспроизводимый сценарий**, **тут же запускает/перепрогоняет** его,
позволяет **корректировать в диалоге**, а на сложных местах — **передавать управление человеку и забирать
обратно** (co-pilot). Экспорт в CI — **вторичен** (бонус), первично — **работа внутри инструмента**.

| Слой | Что даёт | Реализация |
|------|----------|------------|
| **Chat-авторинг** | опиши/goal → грунтованный `scenario.json` | brain `GoalPlanner`/`DescribePlanner` + vanilla-чат + шим |
| **In-tool run-console** | ▶ запуск / 🔁 перепрогон / 📌 baseline **внутри UI** + вердикт | control-API replay/baseline (R1) + vanilla-кнопки |
| **Multi-turn диалог + коррекция** | контекст между сообщениями, правка по ходу | **✅ DONE** — R2a backend (checkpointer-resume) + R2b UI (vanilla, ADR-048); live=M9-LIVE |
| **Co-pilot takeover/return (F4)** | агент ↔ человек на одной живой сессии | MV3-расширение + `chrome.debugger` (0xCoDSnet) + brain interrupt/resume (R3) |
| **MV3-рекордер** | запись действий человека → сценарий | MV3 content-script → `/v1/stream` → `reconcile` (0xCoDSnet) |
| **Rich AG-UI co-pilot** | стриминг/HITL/generative-UI/timeline | **in-house vanilla** в `docs/*` (M14, ADR-055) — суверенный единый UI, air-gapped; CopilotKit `frontend/` заморожен (deprecated) |

## 2. Две оси эволюции

**§F — режимы драйва браузера** (`M9_CONTRACT.md §F`):
F1 **own-headless** (с M0, всегда) → F2 **headed/видимый** (`PW_HEADLESS=0`) → F3 **CDP-attach** к Chrome
пользователя (`PW_CDP_ENDPOINT`) → F4 **co-pilot takeover/return**.
**Статус:** F1 ✅ · **F2 ✅ / F3 ✅** (M9.6/ADR-036/037, offline; live-verify pending) · **F4 ✅** (M9.8) — brain interrupt/resume + RunControl `takeover`/`return` + WS-форвард (R3, **ADR-054**) и extension-side `chrome.debugger` (#47); живой e2e на настоящей сессии = M9-LIVE.

**Эволюция авторинга:** one-shot (одна NL-строка → один прогон → `scenario.json`) → **multi-turn** (диалог
с контекстом + коррекция). **Статус: multi-turn ✅ DONE** (M9.10, ADR-048) — **R2a backend** (checkpointer-resume `conversation_id`→`thread_id` + `messages` add_messages-канал + chat-mode conditional-entry refine) + **R2b UI** (vanilla `docs/chat`+`docs/index#chat`: mint `conversation_id`/разговор, кумулятивный тред, 🆕 «Новый разговор»); offline-verified, live=M9-LIVE. (`explore` остаётся one-shot.)

## 3. Feature inventory (честный статус)

| Фича | Дом (milestone/ADR) | Статус |
|------|---------------------|--------|
| Детерминированный explore→`plan.json` (coverage, `plan_hash`) | M1/M3 | ✅ DONE |
| Goal/describe → `scenario.json` (NL-авторинг, **one-shot**, грунтованный) | M9.2a/b, ADR-027/028 | ✅ DONE offline (live pending) |
| Replay (исполнение/вердикт 0/1/2/3, LLM-free, self-heal) | M3 | ✅ DONE (CLI/CI) |
| Golden-diff (a11y+screenshot) / self-heal (L1–L6 + a11y re-ground) | M2/M3; visual-heal M5 | ✅ DONE; visual set-of-marks heal = PoC-gated |
| Opt-in visual-authoritative flip | ADR-042 | ⚙️ flag (default-off; default-on → M9-LIVE proof) |
| Vanilla chat-front (air-gapped, **one-shot**) | M9.3-tail/ADR-040 | ✅ DONE (`docs/chat/`, `docs/index.html#chat`) |
| OpenAI-compat шим (Open WebUI/SDK = **клиент**, «как модель») | M12/ADR-041 | ✅ DONE (`/v1/chat/completions`) |
| **Replay/baseline ВНУТРИ UI** | M9.3 «вне scope» → **R1/M9.9** | ✅ DONE — R1a backend (ADR-047) + R1b UI ▶/🔁/📌 в `#build`/`#chat`/`chat/`/`setup/` (GAP-M9-16) |
| **Multi-turn чат / контекст / коррекция по ходу** | «brain-extension» → **R2/M9.10** | ✅ DONE — R2a backend + R2b UI (ADR-048, offline; GAP-M9-17 closed) |
| Headed / видимый браузер (F2) | M9.6/ADR-037 | ✅ DONE offline (live pending) |
| CDP-attach к Chrome пользователя (F3) | M9.6/ADR-036/037 | ✅ DONE offline (live pending) |
| **Co-pilot takeover/return (F4)** | M9.8/ADR-039 | ✅ DONE — extension-side (`extension/`, #47: `chrome.debugger` attach/return, баннер) + brain-side interrupt/resume (R3, ADR-054); живой e2e = M9-LIVE |
| WS-транспорт client→server (`/v1/stream`) | M9.8-prep/ADR-043 | ✅ DONE |
| SSE server→client + artifact-fetch | M9.3-tail/ADR-040 | ✅ DONE |
| Rich AG-UI co-pilot (vanilla) | **M14/ADR-055** | ✅ in-house в `docs/index.html` (Settings\|Tests · library/promote · live AG-UI-timeline · auto-HITL-баннер); CopilotKit `frontend/` заморожен (reference) |
| **MV3-рекордер-расширение** | M9.8/ADR-038 (GAP-M9-13) | ✅ DONE (`extension/`, #42-47: рекордер+redaction, SW-WS, DevTools-панель, record→scenario, takeover/return CDP; dev-only, не в CI). Live record→replay = M9-LIVE |
| LiteLLM opt-router · MCP-Inspector | ADR-045 | ✅ DONE (config/docs) |
| In-app tabs + multi-tab (M9.4) · traceparent (M9.5) | ADR | ✅ DONE offline (live pending) |
| Pluggable adapters (auth/deploy/model/backend) | M9.7/ADR-025 | ⚙️ model/backend ✅ (ADR-045); **auth** частично ✅ (storageState/login-as-test, M9.1/ADR-026); OIDC/Keycloak + **deploy-адаптер not-built** |
| Security-модуль (XSS/CSRF/IDOR…, authz-gated) | M10/GAP-M9-11 | ❌ design-only |
| **Rich-UI + persistence + metrics-in-UI** (two-tier service) | M13-15 / ADR-049..053 | ✅ **все доставлены**: M13 5-domain store-gateway SQLite-first (#64, Postgres/service→M13-service); M14 rich AG-UI + Settings\|Tests (#66) + хвосты `run.finished` (#86) и replay-AG-UI/auto-HITL-сигнал (#87); M15 metrics-in-UI (#70) + M15.1 token-cost (#72) |
| Точность (Langfuse/DSPy) | roadmap | ❌ not-built (после user-тестов) |

## 4. Договорённости (принципы)

1. **In-tool-first.** Запуск/перепрогон/baseline **внутри инструмента** — первичны. CI-экспорт (Jenkins/GitLab — `docs/ci-templates/`, уже есть) — вторичен/бонус.
2. **Vanilla `docs/*` = ЕДИНСТВЕННЫЙ суверенный UI** (air-gapped, zero-build, `file://`-safe). **CopilotKit `frontend/` — deprecated (ADR-055), заморожен как reference** (npm/build/Node = не air-gapped → не путь поставки; убирает GAP-SEC-002). **Эволюция (эпик M13-15, ADR-049..053; уточнено ADR-055):** rich AG-UI co-pilot пишем САМИ на vanilla поверх R3-WS (`@@AGUI`-события, M14) — не через kit; профили = топология-не-фичи; метрики **self-contained** (ADR-051, M15).
3. **Open WebUI = совместимый клиент** OpenAI-compat-шима (опц., сам поднимаешь), **НЕ co-pilot**. Перехват/co-pilot даёт **расширение (`chrome.debugger`) + brain**, не чат-UI.
4. **Multi-turn — ✅ DONE** (M9.10, ADR-048): R2a backend (checkpointer-resume) + R2b UI (vanilla); offline-verified, live=M9-LIVE.
5. **F4 — совместная веха:** расширение/CDP/panel — @0xCoDSnet (#47); brain interrupt/resume + WS-сигналы — мои (R3).
6. **Детерминизм-граница:** golden-replay — только headless (ADR-037); headed/CDP — режимы наблюдения.

## 5. Roadmap по волнам

### Мои волны (`control-API` / `brain` / vanilla-UI) — порядок R1 → R2 → R3
| # | Веха | Содержание | Закрывает |
|---|------|-----------|-----------|
| **R1** | **M9.9 In-tool run-console** | control-API `mode=replay\|baseline` + `from_run:<run_id>` (whitelist+traversal-guard; `--replay --plan`/`baseline`) + `config-schema.modes`; ▶/🔁/📌 + вердикт в vanilla-UI (`#build`/`#chat`/`chat/`/`setup/`); httptest — **✅ DONE (R1a backend + R1b UI)** | GAP-M9-16 |
| **R2** | **M9.10 Multi-turn авторинг** ✅ | brain `chat` `RUN_MODE` checkpointer-resume `conversation_id`→`thread_id` + `messages`-канал + conditional-entry refine + agentctl/control-API `conversation_id` (**R2a**) · vanilla-UI `conversation_id`/кумулятивный тред/🆕 новый-разговор (**R2b**) — **✅ DONE (ADR-048)** | GAP-M9-17 ✓ |
| **R3** | **M9.8 F4 takeover (brain-side)** ✅ | brain interrupt-on-takeover / resume-on-return (LangGraph interrupt+checkpoint); WS-сигналы `takeover/return/state-sync` поверх `/v1/stream` — **✅ DONE (ADR-054)**; живой e2e = M9-LIVE | GAP-M9-18 ✓ (+½ GAP-M9-15) |

### Эпик: Rich-UI + Persistence + Metrics (M13–M15, ADR-049..053) — после R3
**Two-tier:** профили = **ТОПОЛОГИЯ, не фичи** — оба несут весь функционал (chat/copilot/UI/replay/library/metrics) и оба **air-gapped**-устанавливаемые. **Control-plane** (always-on: control-API+store-gateway+БД) **vs run-unit** (ephemeral: brain+pw-executor, спавн на 1 прогон → exit). CronJob (ADR-017) = триггер планового run-unit, **не** деплой сервиса. Профили: **standalone** (1 хост/compose/SQLite) · **service** (K8s/Postgres/HA) — оба air-gapped (ADR-053).

| # | Веха | Содержание | ADR |
|---|------|-----------|-----|
| **M13** | Persistence / Service layer | store-gateway N-доменов (hybrid SQLite/Postgres) + control-API CRUD + persist runs + full=service-режим | ADR-049/050 |
| **M14** | Rich AG-UI (full) + split setup-UI | SPA на AG-UI-событиях поверх R3-WS (не one-shot-шим); Settings \| Tests (библиотека·запуск·история·просмотр·чаты·метрики) | ADR-052 |
| **M15** | Metrics & dashboards-in-UI | метрики прогонов → БД (M13) → **native-графики** в SPA; Prom/Grafana = опц. экспорт | ADR-051 |

**Модель данных (5 доменов, владелец = store-gateway, hybrid):**
| Домен | Содержимое | Связь с текущим |
|---|---|---|
| scenarios/tests | scenario_id, name, target, steps, plan_hash, tags · «test» = scenario + golden + расписание | `scenario.json`/`plan.json` в `runs/` → индексируем |
| runs | run_id, conversation_id, mode, target, exit_code, времена, verdict | in-memory map control-API → персистим |
| chats | conversation_id, тёрны, messages | проекция R2a `state/conversations.db` (не дубль) |
| results | heal-report.json / report.json | файлы → индекс+просмотр |
| metrics | pass/heal/fail/regression, coverage, duration, cost, flake-тренды | из results → time-series под native-графики |

**Почему после R3:** rich AG-UI без R3-WS = снова one-shot-шим; M14/M15 ⊃ хранилище M13; chats-домен частично готов (R2a).

### Волны @0xCoDSnet
| Трек | Issues | Зависимости |
|------|--------|-------------|
| Security | #36 (trace-retention #34-pt3 + #33/#35) · #37 (prompt-sanitization) · #38 (lockfile+SBOM, GAP-SEC-002) | — |
| MV3-расширение | #42 skeleton → #43 SW-WS → #44 recorder → #45 panel → #46 record→scenario → #47 takeover | WS `/v1/stream` ✅ + `frontend/` ✅; **#47 ↔ моя R3** |

### Дальше (существующее)
M9.7-remainder (auth/deploy adapters) · M10 security-модуль · M11.1 release (lockfile/SBOM/signing — пересекается с #38) · M11.2/4/5 · **M9-LIVE** (живые прогоны — «go»+ключ+браузер) · Langfuse/DSPy.

## 6. Как закрываются desync-точки
- «Запуск/перепрогон внутри инструмента» → **R1** ✅ DONE (GAP-M9-16).
- «Контекст, не one-shot, коррекция по ходу» → **✅ DONE** — R2a backend + R2b UI (ADR-048, GAP-M9-17 closed; live=M9-LIVE).
- «Перехват/co-pilot/партнёрство» → **✅ DONE** — R3 brain (ADR-054) + #47 extension = F4 (GAP-M9-15/18 закрыты); живой e2e = M9-LIVE.
- «Показывал в браузере, что делает» → **уже есть** (F2 headed / F3 CDP-attach, M9.6); live-verify = M9-LIVE.

## См. также
[`M9_CONTRACT.md`](M9_CONTRACT.md) (§F эволюция, §B/§L авторинг) · [`M9.8_CONTRACT.md`](M9.8_CONTRACT.md) (расширение+F4) ·
[`M9.6_CONTRACT.md`](M9.6_CONTRACT.md) (F2/F3) · [`M12_CONTRACT.md`](M12_CONTRACT.md) (шим, one-shot) ·
[`ADAPTERS.md`](ADAPTERS.md) (LiteLLM/MCP-Inspector) · [`ARCHITECTURE.md`](../ARCHITECTURE.md) · [`GAPS.md`](../GAPS.md) · [`THREAT_MODEL.md`](THREAT_MODEL.md).
