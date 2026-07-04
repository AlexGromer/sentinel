# Contract M13 — Persistence / service layer (store-gateway → 5 domains)

> 🌐 [Русский](M13_CONTRACT.md) (authoritative) · **English**

> **Status**: **Design frozen (ADR-049/050)** → **as-built contract (in progress)** · **Date**: 2026-07-04
> **Covers**: M13 = extend the Go `store-gateway` (sole writer, ADR-007) to **5 domains** on top of today's heal/trust domain. First step of the Rich-UI/Persistence/Metrics epic (ADR-049..053); the foundation for M14 (rich UI) and M15 (metrics-in-UI).
> **Scope decision**: **SQLite-first**. The Postgres-hybrid backend + `golang-migrate` + TCP/mTLS transport (needed by the service profile, K8s/HA, ADR-049) are **deferred to M13-service (M11/ADR-053 track)**; M13 only lays the DSN-switch scaffold so Postgres slots in with no schema churn.

---

## 1. Why

Today `store-gateway` (`internal/store/server.go`) is the sole SQLite writer (`state/locators.db`, ADR-007) but holds ONLY the heal/trust domain (4 tables: `healed_locators`, `healing_audit`, `golden_snapshots`, `step_failures`). Everything else is ephemeral or scattered across files:
- **runs** live in control-API's in-memory `map[string]*run` (`cmd/control-api/main.go`) → **lost on restart**;
- **scenarios/tests** — only `runs/<id>/{plan.json,scenario.json}` artifacts + golden in store-gateway, no index/entity;
- **chats** — the LangGraph checkpointer `state/conversations.db` (R2a, ADR-048), not browsable;
- **results** — `runs/<id>/{heal-report.json,report.json}`, served by `report-service`, no index;
- **metrics** — per-run `runs/<id>/metrics.prom`, no cross-run time-series/aggregation.

Rich UI (M14) and metrics-in-UI (M15) need **persistent, indexable** state. M13 provides it while preserving the single-writer invariant (ADR-007) — control-API/brain do **not** open the DB directly; everything goes through gRPC to store-gateway.

## 2. The five domains (ADR-050)

store-gateway gains a new service (`StoreService` in `proto/store.proto`, separate from the legacy `PersistenceService` so the heal-domain stub hash-assert isn't broken). All tables share the same `state/locators.db` (single-writer, one `sync.Mutex`; per-domain locks only if a bottleneck is profiled).

| Domain | Today's source | Schema (SQLite; portable SQL `ON CONFLICT`) |
|---|---|---|
| **runs** | control-API in-memory `run{}` | `runs(run_id PK, conversation_id, mode, target, planner, state, exit_code, artifact_dir, error, started_at, finished_at)` — **add `conversation_id`** (today only in argv → the runs↔chats join doesn't survive restart). |
| **scenarios/tests** | `plan.json`/`scenario.json` + `golden_snapshots` | `scenarios(scenario_id PK, name, target, run_mode, plan_hash, steps_json, unmatched, tags, created_at, source_run_id)` + `tests(test_id PK, scenario_id FK, plan_hash, name, schedule, enabled, last_status, last_run_id, created_at)`. `plan_hash` = `brain/state.py:canonical_plan_hash`. **`schedule` is a reserved column only — no scheduler is built** (0 impl today = scope creep). `test` = a promoted scenario (frozen plan_hash + golden + optional schedule + pass/fail history). |
| **chats** | `state/conversations.db` (LangGraph) | `chats(conversation_id PK, last_target, turn_count, last_active, last_goal, summary, updated_at)` — a **browsable projection, NOT a duplicate** of the checkpointer. Written by the brain: at the end of a chat turn the brain sends a lightweight projection row via the gateway (decoupled from LangGraph's opaque schema). |
| **results** | `heal-report.json`/`report.json` | `results(run_id PK, plan_id, mode, verdict, exit_code, healed, failed, regressions_json, steps_json, coverage, duration_ms, created_at)` — close to `cmd/report-service/main.go:report`. |
| **metrics** | per-run `metrics.prom` | `metrics(run_id, ts, name, value, labels_json)` (time-series; ingest parses `metrics.prom`/`report.json`). Trends (pass/heal/flake/cost/coverage) = aggregate queries for M15. **Entirely new** — nothing aggregates across runs today. |

### gRPC surface (StoreService, in addition to PersistenceService)
- **runs**: `UpsertRun`, `GetRun`, `ListRuns`.
- **scenarios/tests**: `SaveScenario`, `ListScenarios`, `PromoteTest`, `ListTests`, `GetTest`.
- **chats**: `UpsertChat`, `ListChats`, `GetChat`.
- **results**: `SaveResult`, `GetResult`, `ListResults`.
- **metrics**: `IngestMetrics`, `QueryMetrics`, `Trends`.

All methods inherit the existing `TokenAuthInterceptor` (`internal/store/auth.go`) — auth for free.

## 3. control-API integration

`cmd/control-api` stops being the sole owner of `runs` state:
- `spawnRun`/finish goroutine → `UpsertRun` to the gateway on **state transitions** (running/done/failed), NOT per stdout line (chattiness). `run.ConversationID` is now stored.
- `handleListRuns`/`handleGetRun` → read from the gateway (`ListRuns`/`GetRun`); the in-memory `runs` map stays a write-through cache for the live `runStream` (SSE is ephemeral — not persisted).
- **Fallback (fail-open, actual behavior)**: gateway unreachable → warn-log at startup (`newStoreClient` fail-fast), run persistence is silently skipped, reads (list/get) transparently fall back to the in-memory map (**200 OK, NOT 503**); the control-API does NOT crash. A live run (spawn+SSE) works without the gateway.
- `results`: control-API (or report-mode brain) sends `SaveResult` on completion; `metrics`: `IngestMetrics` from `report.json`/`metrics.prom`.

## 4. GAP-M9-20 / GAP-M9-19 (in M13 scope per BACKLOG)

- **GAP-M9-20** (chat history growth): cap N user turns + rolling summary + retention in the checkpointer state (`brain/state.py`/`graph.py`), plus `summary` in the chats projection.
- **GAP-M9-19** (stale site_map on refine): a11y-hash staleness-detect on the warm-refine path; on drift — flag / optional re-explore.

## 5. R3 hardening (from #58 notes, server-side)
- `/v1/stream` Origin check is bearer-only when `corsAllow` is empty → tighten (require Origin on a non-loopback bind).
- reconnect mints a new `record-<session>` → recording fragmentation; optional server-side session-resume. Both are defense-in-depth (bearer is fail-closed).

## 6. Deferred (M13-service, M11/ADR-053 track)
Postgres driver (`pgx`) + dialect abstraction · `golang-migrate` (today only `CREATE TABLE IF NOT EXISTS` + ad-hoc ALTER) · TCP/mTLS listener (today UDS+SO_PEERCRED, single-host). M13 lays only the `STORE_DSN` env branch in `store.New()` (mirroring `CHECKPOINT_DSN` in `brain/__main__.py:_checkpointer`) + portable SQL (`ON CONFLICT DO UPDATE`, no `INSERT OR REPLACE`/pragma dependence in the new domains).

## 7. Acceptance criteria
- [x] `proto/store.proto` + regenerated Go+Python stubs (same toolchain).
- [x] 5-domain gateway ready (SQLite), single-writer (ADR-007) preserved, `STORE_DSN` scaffold (refuses Postgres). **Wired from real callers: `runs` + `chats`**; `results`/`metrics`/`scenarios`/`tests` — RPCs + schema + unit tests exist, but NOT yet wired to a production writer (await wiring in M14/M15).
- [x] control-API `runs` survive restart (via the gateway, fail-open); `conversation_id` stored; fallback when gateway unreachable.
- [x] chats projection browsable (`ChatProjector`), NOT a duplicate of conversations.db.
- [~] **GAP-M9-20** cap+summary ✅ (`_capped_history`); `conversations.db` retention → M13-service. **GAP-M9-19** reverify flag ✅ (`SENTINEL_REFINE_REVERIFY`); auto-detect (a11y probe) → **M9-LIVE**.
- [x] Gates green (go build/vet/race/gofmt · pytest 16 · bilingual · gitleaks); **adversarial-verify (sonnet)** — wave 7.
- [x] Docs sync: this contract + `MEMORY_PERSISTENCE` (reconciled) + ARCHITECTURE §6 + FILEMAP + GAPS + BACKLOG + COPILOT — bilingual (wave 7).

> **Anti-hallucination:** `MEMORY_PERSISTENCE.md` today claims golang-migrate + Postgres compatibility — this is **aspirational**; the code only has `CREATE TABLE IF NOT EXISTS` + one `ALTER` (`ensureGoldenMacColumn`). M13 reconciles the doc: SQLite-first now, Postgres = M13-service.
