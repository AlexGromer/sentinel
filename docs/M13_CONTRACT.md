# Контракт M13 — Persistence / service layer (store-gateway → 5 доменов)

> 🌐 **Русский** (основная версия) · [English](M13_CONTRACT.en.md)

> **Статус**: **Design frozen (ADR-049/050)** → **as-built контракт (в разработке)** · **Дата**: 2026-07-04
> **Покрывает**: M13 = расширение Go `store-gateway` (единственный writer, ADR-007) на **5 доменов** поверх сегодняшнего heal/trust-домена. Первый шаг эпика Rich-UI/Persistence/Metrics (ADR-049..053); фундамент для M14 (rich-UI) и M15 (metrics-in-UI).
> **Scope-решение**: **SQLite-first**. Postgres-hybrid backend + `golang-migrate` + TCP/mTLS-транспорт (нужны service-профилю K8s/HA, ADR-049) — **отложены в M13-service (трек M11/ADR-053)**; в M13 закладывается только DSN-switch-скаффолд, чтобы Postgres встал без миграции схемы.

---

## 1. Зачем

Сегодня `store-gateway` (`internal/store/server.go`) — единственный SQLite-writer (`state/locators.db`, ADR-007), но хранит ТОЛЬКО heal/trust-домен (4 таблицы: `healed_locators`, `healing_audit`, `golden_snapshots`, `step_failures`). Всё остальное состояние — эфемерно или размазано по файлам:
- **runs** control-API живут в in-memory `map[string]*run` (`cmd/control-api/main.go`) → **теряются при рестарте**;
- **scenarios/tests** — только артефакты `runs/<id>/{plan.json,scenario.json}` + golden в store-gateway, без индекса/сущности;
- **chats** — LangGraph-checkpointer `state/conversations.db` (R2a, ADR-048), не browsable;
- **results** — `runs/<id>/{heal-report.json,report.json}`, отдаёт `report-service`, без индекса;
- **metrics** — per-run `runs/<id>/metrics.prom`, никакой time-series/агрегации между прогонами.

Rich-UI (M14) и metrics-in-UI (M15) требуют **персистентного, индексируемого** состояния. M13 даёт его, сохраняя инвариант единственного writer'а (ADR-007) — control-API/brain **не** открывают БД напрямую, всё через gRPC к store-gateway.

## 2. Пять доменов (ADR-050)

Store-gateway расширяется новым сервисом (`StoreService` в `proto/store.proto`, отдельно от legacy `PersistenceService`, чтобы не ломать hash-assert стабов heal-домена). Все таблицы — в том же `state/locators.db` (single-writer, один `sync.Mutex`; per-домен-локи — только если профилируется bottleneck).

| Домен | Источник данных сегодня | Схема (SQLite; портируемый SQL `ON CONFLICT`) |
|---|---|---|
| **runs** | in-memory `run{}` control-API | `runs(run_id PK, conversation_id, mode, target, planner, state, exit_code, artifact_dir, error, started_at, finished_at)` — **добавляем `conversation_id`** (сегодня только в argv → join runs↔chats не переживает рестарт). |
| **scenarios/tests** | `plan.json`/`scenario.json` + `golden_snapshots` | `scenarios(scenario_id PK, name, target, run_mode, plan_hash, steps_json, unmatched, tags, created_at, source_run_id)` + `tests(test_id PK, scenario_id FK, plan_hash, name, schedule, enabled, last_status, last_run_id, created_at)`. `plan_hash` = `brain/state.py:canonical_plan_hash`. **`schedule` — только колонка-резерв, планировщик НЕ строим** (0 impl сегодня = scope creep). `test` = promoted scenario (frozen plan_hash + golden + опц. schedule + pass/fail-история). |
| **chats** | `state/conversations.db` (LangGraph) | `chats(conversation_id PK, last_target, turn_count, last_active, last_goal, summary, updated_at)` — **browsable-проекция, НЕ дубль** checkpointer'а. Заполняется brain'ом: на конце chat-тёрна brain шлёт лёгкую projection-строку через gateway (развязка от opaque LangGraph-схемы). |
| **results** | `heal-report.json`/`report.json` | `results(run_id PK, plan_id, mode, verdict, exit_code, healed, failed, regressions_json, steps_json, coverage, duration_ms, created_at)` — близко к `cmd/report-service/main.go:report`. |
| **metrics** | per-run `metrics.prom` | `metrics(run_id, ts, name, value, labels_json)` (time-series; ingest парсит `metrics.prom`/`report.json`). Тренды (pass/heal/flake/cost/coverage) — агрегирующие запросы для M15. **Новое целиком** — сегодня ничего не агрегирует между прогонами. |

### gRPC-поверхность (StoreService, дополнительно к PersistenceService)
- **runs**: `UpsertRun(RunRecord) → Empty`, `GetRun(RunId) → RunRecord`, `ListRuns(ListRunsReq{limit,offset,state?}) → RunList`.
- **scenarios/tests**: `SaveScenario(Scenario) → Empty`, `ListScenarios(…) → …`, `PromoteTest(PromoteReq) → TestRecord`, `ListTests`, `GetTest`.
- **chats**: `UpsertChat(ChatProjection) → Empty`, `ListChats(…) → ChatList`, `GetChat`.
- **results**: `SaveResult(ResultRecord) → Empty`, `GetResult(RunId)`, `ListResults(…)`.
- **metrics**: `IngestMetrics(MetricsBatch) → Empty`, `QueryMetrics(MetricsQuery) → MetricsSeries` (за окно/по имени), `Trends(TrendReq) → TrendReply`.

Все методы наследуют существующий `TokenAuthInterceptor` (`internal/store/auth.go`) — auth бесплатно.

## 3. Интеграция control-API

`cmd/control-api` перестаёт быть единственным владельцем `runs`-состояния:
- `spawnRun`/finish-горутина → `UpsertRun` в gateway на **сменах состояния** (running/done/failed), НЕ на каждую stdout-строку (chattiness). `run.ConversationID` теперь хранится.
- `handleListRuns`/`handleGetRun` → читают из gateway (`ListRuns`/`GetRun`); in-memory `runs`-map остаётся как write-through кэш для live `runStream` (SSE эфемерен — не персистим).
- **Fallback**: gateway недоступен → control-API деградирует (лог + 503 на persistence-зависимых ручках), НЕ падает. Live-прогон (spawn+SSE) работает и без gateway.
- `results`: control-API (или report-mode brain) шлёт `SaveResult` по завершении; `metrics`: `IngestMetrics` из `report.json`/`metrics.prom`.

## 4. GAP-M9-20 / GAP-M9-19 (в scope M13 по BACKLOG)

- **GAP-M9-20** (рост истории chat): cap N user-тёрнов + rolling summary + retention в checkpointer-стейте (`brain/state.py`/`graph.py`), плюс `summary` в chats-проекции.
- **GAP-M9-19** (устаревание site_map на refine): a11y-hash staleness-detect на warm-refine-пути; при дрейфе — пометка/опц. re-explore.

## 5. R3-hardening (из заметок #58, server-side)
- `/v1/stream` Origin-check сейчас bearer-only при пустом `corsAllow` → усилить (требовать Origin при не-loopback bind).
- reconnect минтит новый `record-<session>` → фрагментация записи; опц. server-side session-resume. Оба — defense-in-depth (bearer fail-closed).

## 6. Отложено (M13-service, трек M11/ADR-053)
Postgres-драйвер (`pgx`) + dialect-абстракция · `golang-migrate` (сегодня только `CREATE TABLE IF NOT EXISTS` + ad-hoc ALTER) · TCP/mTLS-listener (сегодня UDS+SO_PEERCRED, single-host). В M13 — только `STORE_DSN`-env-ветка в `store.New()` (по образцу `CHECKPOINT_DSN` в `brain/__main__.py:_checkpointer`) + портируемый SQL (`ON CONFLICT DO UPDATE`, без `INSERT OR REPLACE`/pragma-зависимостей в новых доменах).

## 7. Критерии приёмки
- [ ] `proto/store.proto` + регенерированные Go+Python стабы (тот же тулчейн; hash-assert расширен).
- [ ] 5 доменов персистятся через gateway (SQLite), single-writer (ADR-007) сохранён; `STORE_DSN`-скаффолд на месте.
- [ ] control-API `runs` переживают рестарт (через gateway); `conversation_id` хранится; fallback при недоступном gateway.
- [ ] chats-проекция browsable, НЕ дубль conversations.db.
- [ ] GAP-M9-20 (cap+summary+retention) + GAP-M9-19 (staleness-detect) закрыты.
- [ ] Гейты зелёные (go build/vet/race/gofmt · pytest · bilingual · gitleaks); adversarial-verify пройден.
- [ ] Docs sync: этот контракт + `MEMORY_PERSISTENCE.md` (сверить с реальностью: golang-migrate/Postgres пока **аспирационны**) + ARCHITECTURE §6 + FILEMAP + GAPS + BACKLOG + COPILOT — bilingual.

> **Анти-галлюцинации:** `MEMORY_PERSISTENCE.md` сегодня утверждает про golang-migrate + Postgres-совместимость — это **аспирация**, в коде только `CREATE TABLE IF NOT EXISTS` + один `ALTER` (`ensureGoldenMacColumn`). M13 приводит доку в соответствие: SQLite-first сейчас, Postgres — M13-service.
