# Контракт M14 — Rich AG-UI co-pilot (in-house vanilla) + split Settings|Tests + wiring scenarios/tests/chats + full auto-HITL

> 🌐 **Русский** (основная версия) · [English](M14_CONTRACT.en.md)

> **Статус**: **Design frozen (ADR-052 + новый ADR-055)** → **as-built контракт (в разработке)** · **Дата**: 2026-07-04
> **Покрывает**: M14 = второй шаг эпика Rich-UI/Persistence/Metrics (ADR-049..053). Поверх персистентности M13 строит **суверенный vanilla AG-UI co-pilot**, разбивает setup-UI на **Settings | Tests**, подключает домены `scenarios`/`tests`/`chats` к реальным вызывающим и добавляет полный **auto-escalate-to-HITL**.
> **Ключевое решение (ADR-055)**: co-pilot пишем **сами (in-house vanilla JS)** как единственный суверенный UI — **CopilotKit убран из delivery-пути** (§0 BUILD-ONLY + air-gapped), `frontend/` заморожен. Это уточняет ADR-052 («frontend/ → SPA») и снимает delivery-роль ADR-044.
> **Scope-граница**: панели `результаты`/`метрики` в Tests-view = **заглушки** → наполняет **M15** (ADR-051, native-charts). Postgres → M13-service (M11). `strict structured-outputs` → отдельная mini-веха M-STRUCTURED-OUT.

---

## 1. Зачем

M13 дал персистентное индексируемое состояние (5 доменов store-gateway), но:
- **`scenarios`/`tests`/`results`** и обратное чтение **`chats`** (`GetChat`/`ListChats`) — RPC+схема+тесты есть, но **нет production-вызывающих и HTTP-поверхности**: UI нечего звать сверх `runs`;
- **UI** (`docs/index.html`) не имеет **библиотеки/истории** — только последний run (`bLastRunId`/`chLastRunId`);
- **WS `/v1/stream`** несёт только recorder-ingest (client→server) + takeover/return-control-фреймы — **server→client AG-UI-событий нет**;
- **takeover** сегодня инициирует только человек/оркестратор — нет авто-эскалации на серию неудач.

M14 закрывает это: HTTP-поверхность доменов, живой AG-UI-timeline поверх R3-WS, split Settings|Tests, entity scenario→test promotion, авто-HITL. Требование пользователя: **функционально не меньше Open WebUI + CopilotKit** (parity-ядро в M14; широта — по вехам).

## 2. AG-UI event schema + WS-транспорт (замораживается здесь)

**Конверт**: `{"type":<event>, "run_id":<id>, "seq":<int>, "ts":<iso8601>, "data":{…}}`

| type | data | эмитент |
|---|---|---|
| `run.started` | `{mode,target,planner}` | brain (perceive) |
| `state.transition` | `{from,to}` | brain (perceive, verify) |
| `step.progress` | `{n,total,desc}` | brain (act) |
| `tool.call` | `{name,args_summary}` | brain (act) |
| `heal` | `{step,strategy(L1–L6),ok}` | brain (heal) |
| `hitl_needed` | `{reason,count}` | brain (checkpoint auto-arm) |
| `verdict` | `{verdict,exit_code,healed,failed}` | brain (`outcome.announce`) — **ADR-139**: печатается ИЗ ТОГО ЖЕ значения, которое возвращается как код выхода процесса, поэтому кадр не может ему противоречить. Раньше эмитился узлом `report` графа из СВОИХ полей и расходился в обе стороны. `verdict` — слово из таблицы `outcome.VERDICT_WORD` (`pass`/`problem`/`regression`/`integrity`/`tool_failure`/…), общей с путём `replay`; прежние `ok`/`failed` пути `explore` заменены на `pass`/`problem` (потребителей значения в репозитории замерено ноль) |
| `run.finished` | `{exit_code,state}` | control-API — **эмитится** (finish-горутина инжектит `@@AGUI`-строку после stdout brain, до `finish()`; `seq` опущен — отдельное un-ordered-пространство; failed-spawn → `exit_code:-1` (различается по `state`: signal-kill=`done`, spawn-fail=`failed`); typed для WS, сырая строка в `log` для SSE) |
| `log` | `{line}` | passthrough сырого stdout |

**Транспорт (надстройка R3, НЕ one-shot-шим ADR-041)**: поверх существующего WS `GET /v1/stream`. Клиент коннектится с `?run_id=<id>` (charset `validRunID` — тот же charset-guard, что у `?session=`; `run_id` используется как ключ `s.runs`-map/JSON, в путь НЕ идёт, потому `filepath.Base` не нужен) → сервер **подписывает** сокет на `runStream` этого run → пушит конверты как `wsOpText`-фреймы. Client→server takeover/return без изменений — **дуплекс на одном сокете**.

> **Слайс, не полнота**: M14 тянет вперёд из M9-LIVE только **подписку по `run_id`** (одна вкладка ↔ один run). **Полная cross-run ownership-авторизация** (сокет может адресовать только свои run) остаётся **M9-LIVE** — сегодня любой authed-клиент может адресовать любой run_id (комментарий в `ws.go`), для single-user-localhost приемлемо.

**Источник событий (in-band, reuse существующего захвата)**: brain печатает строки с префиксом `@@AGUI <json>` в stdout на нодах графа; consumer control-API распознаёт префикс → форвардит `data` как типизированное событие; прочие строки → `log`-событие. Переиспользует весь путь `lineWriter`→`runStream` (main.go:138) — **новый транспорт не нужен**, и `step/tool/heal/hitl_needed` идут единообразно. control-API дополнительно инжектит события, что знает сам (`run.started`/`run.finished`/`verdict`). *Fallback если in-band хрупок:* отдельный NDJSON side-channel из brain (как recorder-ingest, инвертированный).

## 3. HTTP-поверхность доменов + scenario-персист (control-API)

Новые token-gated + CORS-роуты через **существующий fail-open store-gateway-клиент** (`cmd/control-api/store.go`):
- `GET /v1/scenarios[/{id}]` → `ListScenarios`/`GetScenario`
- `GET /v1/tests[/{id}]` → `ListTests`/`GetTest`
- `POST /v1/tests/promote` `{scenario_id,name,schedule?}` → `PromoteTest` (фризит `plan_hash`)
- `GET /v1/chats[/{id}]` → `ListChats`/`GetChat`
- `DELETE /v1/{scenarios|tests|chats}/{id}` → новые RPC `Delete*` (под single-writer `s.mu`) — library/conversation management

**Scenario-персист (wire `SaveScenario`)**: на finish-горутине (`main.go:397`, рядом с `UpsertRun`), если в `artifactDir` есть `scenario.json` → читаем → `SaveScenario` (**`plan_hash` берём из артефакта**, `brain/__main__.py:46` — не пересчитываем). Это подключает домен `scenarios` к реальному вызывающему.

**Fallback**: как в M13 — gateway недоступен → чтения падают на пусто/in-memory, персист молча пропускается, control-API НЕ падает (fail-open).

## 4. brain: AG-UI-эмиссия + full auto-HITL

- **`brain/state.py`**: RunState += `consecutive_heal_failures:int`, `failed_steps:int`, `agui_seq:int` (default 0).
- **`brain/agui.py`** (новый, чистый/offline): `emit(type, run_id, seq, **data)` → `print("@@AGUI "+json.dumps(...))` со строгим экранированием.
- **`brain/graph.py`**: эмит на нодах (perceive→`run.started`/`state.transition`; act→`tool.call`/`step.progress`; verify→`state.transition`; heal→`heal`; report→`verdict`).
- **auto-HITL**: инкремент `consecutive_heal_failures` в heal-ноде (L1–L6 miss) + `failed_steps` в verify-ноде (`_verify_ok=False`); сброс consecutive на любом успехе. В `route_checkpoint` (`graph.py:357`): `if consecutive_heal_failures >= SENTINEL_AUTO_HITL_THRESHOLD (env, 0=off): arm _takeover_armed` + `emit hitl_needed{reason,count}`. **Переиспользует существующий latch `_takeover_armed`** (state.py:55 / graph.py:362) — это новая *причина* взвести takeover; никакой новой pause-машины. Сигнал доходит до UI по AG-UI-каналу (§2).

Счётчики — это ещё и **субстрат для M15-метрик** (peak `consecutive_heal_failures`, `auto_hitl_triggered`-флаг → домен `metrics`).

## 5. Frontend: vanilla AG-UI co-pilot (parity-ядро) + Settings|Tests

Единственный суверенный UI — `docs/index.html` (air-gapped, zero-dep, `file://`-safe, bilingual через `data-lang`+`bi(ru,en)`). `frontend/` (CopilotKit) заморожен как non-maintained reference.

- **Tab-механизм** Settings|Tests (малый JS/CSS show/hide — сегодня nav только якорный).
- **Settings** = re-parent `#connect`+`#build` + редактирование **model-per-role / planner / mode / budgets** (temperature=0 **read-only** + пометка «детерминизм»).
- **Tests**:
  - **Library**: список scenarios/tests · **promote** · pass/fail-история · delete/rename · search;
  - **Run history**: список runs + лаунчер ▶/🔁/📌 (reuse `from_run`);
  - **Chats-mgmt**: список бесед · rename · delete · search;
  - **Live AG-UI timeline**: типизированные события · state-чипы · **`hitl_needed`-баннер + кнопка «взять управление»** (шлёт takeover-фрейм) · co-pilot запускает runs/promote;
  - **Rich chat render**: минимальный hand-rolled markdown+code (no-CDN);
  - панели `результаты`/`метрики` = **заглушки** («M15»).

### Parity-матрица (≥ OpenWebUI + CopilotKit)
| Возможность | Наш аналог | Веха |
|---|---|---|
| Generative-UI (рендер tool-calls) | AG-UI timeline | **M14** |
| Agent-state (useCoAgent) | state-чипы | **M14** |
| Human-in-the-loop | takeover/return + auto-HITL | **M14** |
| Frontend-actions | co-pilot запускает runs/promote | **M14** |
| Streaming chat + markdown/code | #chat + SSE + hand-rolled render | **M14** |
| Conversation mgmt (list/rename/delete/search) | chats-домен + delete-RPC | **M14** |
| Prompt library / presets | scenarios/tests-библиотека | **M14** |
| Model switch + params в UI | Settings (per-role/budgets) | **M14** |
| Model management / pull моделей | — | → M-AUTOPILOT-LOCAL |
| Multi-user / RBAC | bearer-token (single) | → M9.7 |
| RAG / doc-upload / multimodal-input | N/A по домену (исследуем живой app) | — |

## 6. ADR-055 — почему свой vanilla co-pilot, а не CopilotKit

CopilotKit (npm/React + Node-runtime) может быть только **dev-удобством**: он требует build-toolchain + registry → противоречит air-gapped-суверенитету (ADR-049/053, «скачал релиз → запустил оффлайн»). Vanilla `docs/*` **обязан** нести весь функционал независимо. Значит поддержка CopilotKit — чистый налог (версионный дрейф + parity-двух-UI). ADR-055: **in-house vanilla AG-UI co-pilot = суверенный единый UI; CopilotKit убран из пути поставки; `frontend/` заморожен**. Бонус: **убирает GAP-SEC-002** (npm supply-chain исчезает вместе с CopilotKit). AG-UI-протокол (схема событий §2) определяем сами — просто потребляем в vanilla, а не через kit.

## 7. Отложено
- **M15**: wiring доменов `results`/`metrics` + native-charts в Tests-панели (сегодня — заглушки).
- **M9-LIVE**: live e2e AG-UI (реальный браузер) · полная ownership-авторизация `run_id`↔session · живая проверка auto-HITL.
- **replay/baseline AG-UI + auto-HITL — ✅ эмиссия+сигнал (M14 tail 2):** `run_replay` теперь эмитит `run.started`/`step.progress`/`heal`(реальные L1-L6 strategy/confidence)/`verdict`(реальный exit 0/1/2/3) — богатый timeline вместо `log`-view; счётчик **consecutive real-failures** (любой не-quarantined провал, как graph-mode) + `hitl_needed` на `SENTINEL_AUTO_HITL_THRESHOLD` (0=off). В mcp-server-режиме `@@AGUI`/принты уводятся в stderr (`server.py` `_drive`), чтобы не корраптить MCP JSON-RPC на stdout (лечит и graph-explore). **Живая auto-ПАУЗА (человек берёт управление посреди replay) = M9-LIVE** — у replay нет interrupt/resume-механизма, а co-pilot-takeover-in-replay отложен туда же (как и живая проверка graph-mode-паузы). `tests/test_m14_replay_agui_offline.py`.
- **M-STRUCTURED-OUT** (сразу после M14): strict `tool_use`/`json_schema` вместо `find('{')`-парса.
- **M-INSTALL / M-AUTOPILOT-LOCAL** (после эпика): self-installer · hw-probe→sizing→ollama-deploy + UI model-management.
- **M13-service** (M11): Postgres/migrations/TCP.

## 8. Критерии приёмки
- [ ] AG-UI event schema заморожена (этот контракт) + **ADR-055** в ARCHITECTURE §3/§6.
- [ ] WS `/v1/stream` server→client-канал: mutex-guarded writer-goroutine, `?run_id=`-подписка, `@@AGUI`-consumer; recorder-обратная-совместимость; httptest (`-race`).
- [ ] HTTP-поверхность `scenarios`/`tests`/`chats` (+promote, +delete) через fail-open store-client; scenario-персист на finish (`SaveScenario`); httptest.
- [ ] brain: AG-UI-эмиссия на нодах + full auto-HITL (счётчики + auto-arm `_takeover_armed` + `hitl_needed`); offline-тест (`test_m14_agui_offline.py`); порог=0 → байт-идентично.
- [ ] vanilla co-pilot: Settings|Tests + library/promote/history/chats-mgmt + live AG-UI-timeline + `hitl_needed`-баннер; bilingual; `frontend/` заморожен (ADR-055).
- [ ] Гейты зелёные (go build/vet/race/gofmt · pytest offline+m14_agui · bilingual · gitleaks); **adversarial-verify (sonnet)**.
- [ ] Docs sync: контракт(+en) · ADR-055 · COPILOT(+en) · FILEMAP · GAPS (новый auto-HITL gap-id) · BACKLOG — bilingual.
- [ ] **Живое e2e — вне M14** (M9-LIVE): реальный браузер, ownership-auth, живой auto-HITL-пауз.

> **Анти-галлюцинации:** M14 реализует frozen ADR-052; **ADR-055** введён только из-за осознанного отклонения (drop CopilotKit) — не «новая фича», а уточнение delivery-пути. `results`/`metrics`-панели помечены заглушками, чтобы downstream (M15) не принял их за готовые. `?run_id=`-подписка — это НЕ полная ownership-авторизация (та = M9-LIVE).
