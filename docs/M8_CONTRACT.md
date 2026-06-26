# M8 Contract — "Distributed Observability + Budget Ceiling" (frozen 2026-06-26)

> 🌐 **Русский** (основная версия) · [English](M8_CONTRACT.en.md)

Цель: закрыть **GAP-OBS-001** полностью (решение пользователя — full scope). Три части:
**(1) distributed tracing** через все три языка (Go/Python/TS) с W3C trace-context propagation;
**(2) hard budget ceiling** — Python-аккумулятор токенов + Go-оркестратор, убивающий subprocess при
превышении; **(3) Go `report-service`** (HTTP). Вводит **ADR-021**, дополняющий ADR-018 (который
намеренно убрал always-on HTTP `/metrics` для ephemeral CronJob — ADR-021 их примиряет, см. ниже).

## Разделение по верифицируемости (exec-gating)
| Часть | Язык | Verify |
|---|---|---|
| C2 budget-аккумулятор + W3C inject/extract + per-node spans | Python | **offline (me)** — `tests/test_m8_offline.py` |
| C1 proto + Python-стабы | proto/Python | offline regen; Go-стабы user-run |
| C3 orchestrator + report-service + store-gateway spans | Go | **user-run** (`go build`) |
| C4 pw-executor spans | TS | **user-run** (`npm run build`) |
| live OTLP→Tempo/Jaeger, реальный budget-kill | — | **user-run** |

## ADR-021 — примирение с ADR-018 (не противоречие)
- **Ephemeral CronJob (batch)** → остаётся **Pushgateway/textfile** (ADR-018 неизменён для этого пути).
- **Долгоживущий orchestrator/service mode** → **report-service HTTP** `/report` + `/metrics` (scrapeable,
  т.к. процесс долгоживущий). Это НЕ возврат HTTP-сервера в ephemeral job — это отдельный режим.
- Вводит долгоживущий **Go orchestrator** (был обещан в ARCHITECTURE §2, но в коде отсутствовал).

## (1) Distributed tracing — W3C context propagation
Всё gated на `OTEL_EXPORTER_OTLP_ENDPOINT` (no-op по умолчанию, нулевой overhead — паттерн `brain/otel.py`).
- **brain (Python):** новые `otel.inject_context(carrier)` / `extract_context(carrier)` (`TraceContextTextMapPropagator`).
  Per-node spans в `brain/graph.py` (по одному на узел LangGraph — обещано в OBSERVABILITY.md, ещё не сделано).
- **brain → pw-executor:** `executor.py` кладёт `traceparent` в `params._meta` (JSON-RPC) и в MCP `_meta`.
- **brain → store-gateway:** `store.py` gRPC client-interceptor инъецирует W3C в metadata.
- **pw-executor (TS):** новый `src/otel.ts` (gated tracer, зеркало brain); `dispatch()` извлекает `traceparent`
  из `_meta`, стартует child-span на каждый tool-вызов; `RpcRequest` расширен полем `_meta`.
- **store-gateway (Go):** `otelgrpc.StatsHandler` на `grpc.NewServer()` (авто-извлечение W3C из metadata +
  server-спаны); tracer-init gated. Опц. ручные child-спаны в методах `internal/store/server.go`.

## (2) Hard budget ceiling
- **`brain/budget.py` (новый):** `BudgetTracker` — аккумулирует `prompt+completion` токены per-role
  (`plan`/`heal`) и суммарно; лимиты из env `PLAN_TOKEN_LIMIT` (деф. 50000), `HEAL_TOKEN_LIMIT` (20000),
  `TOTAL_TOKEN_LIMIT` (опц.). `add(role, result)` после каждого `backend.complete`; `exceeded(role)` —
  pre-call гард.
- **Graceful degradation:** при превышении planner-лимита `LLMPlanner.propose` → heuristic; heal-лимита →
  L1–L6 без LLM. Дефолтный heuristic-путь токены не тратит → ceiling не влияет (обоснование low-value отсрочки M4b).
- **Go-side hard ceiling:** orchestrator получает токен-дельты brain через `RunControl.ReportEvent` (см.
  proto), сверяет с конфигом, и при превышении возвращает `Control{abort=true}`; brain деградирует; если не
  подчинился за grace-period — orchestrator **SIGTERM** subprocess (model-independent backstop).

## (3) proto/runcontrol.proto (proto3) — новый сервис
```
service RunControl {
  rpc StartRun (StartRunRequest) returns (StartRunReply);   // регистрация прогона (run_id, limits)
  rpc ReportEvent (RunEvent) returns (Control);             // brain → orch per node-step; reply может abort
  rpc Abort (AbortRequest) returns (AbortReply);            // внешний abort
}
message RunEvent { string run_id; string node; int64 prompt_tokens; int64 completion_tokens; string status; }
message Control  { bool abort; string reason; }
```
Стабы: Python (`grpc_tools.protoc`, offline) + Go (plugin, user-run); **proto-hash assert** (GAP-RISK-008).
brain — gRPC-client к orchestrator (gated на `ORCH_ADDR`; не задан → standalone, budget только Python-side).

## (3b) Go компоненты (user-run build)
- **`cmd/orchestrator/main.go`:** gRPC `RunControl` server; спавнит brain-subprocess (env `ORCH_ADDR`,
  `RUN_ID`, limits); FSM PENDING→RUNNING→…→DONE|ABORTED; health-ping; per-step deadline; budget-kill.
- **`cmd/report-service/main.go`:** HTTP — `/report/<run_id>` (HTML+JSON из `runs/<id>/heal-report.json`,
  Go `html/template`) + `/metrics` (`client_golang`).
- **`cmd/store-gateway/main.go` + `internal/store/server.go`:** otelgrpc StatsHandler + tracer-init.
- **`go.mod`:** + `go.opentelemetry.io/otel*`, `otelgrpc`, `client_golang`.

## Гейт M8
- **Offline (me):** `tests/test_m8_offline.py` — `BudgetTracker` (accumulate, exceeded, per-role degradation);
  `inject_context`/`extract_context` round-trip + no-op без endpoint; per-node span no-op; Python runcontrol-стаб
  импортируется. Регресс всех `test_*_offline` зелёный.
- **User-run:** `go build ./cmd/orchestrator ./cmd/report-service ./cmd/store-gateway`; `npm run build` (pw-executor +
  otel deps); запуск с реальным OTLP-collector → один trace с кросс-язык spans (brain→TS→Go); реальный budget-kill
  (planner-лимит занижен → orchestrator SIGTERM).

## Вне scope
- D (screenshot determinism) — отдельная фаза. M6 real-provider smoke (GAP-VERIFY-005).
- Полная инструментация всех store-методов ручными спанами (StatsHandler покрывает server-спаны; ручные — опц.).
