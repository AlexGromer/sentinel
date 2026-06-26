# M8 Contract — "Distributed Observability + Budget Ceiling" (frozen 2026-06-26)

> 🌐 [Русский](M8_CONTRACT.md) (основная версия) · **English**

Goal: close **GAP-OBS-001** in full (user decision — full scope). Three parts:
**(1) distributed tracing** across all three languages (Go/Python/TS) with W3C trace-context
propagation; **(2) a hard budget ceiling** — a Python token accumulator + a Go orchestrator that
kills the subprocess on breach; **(3) a Go `report-service`** (HTTP). Introduces **ADR-021**, which
amends ADR-018 (which deliberately dropped the always-on HTTP `/metrics` for the ephemeral CronJob —
ADR-021 reconciles them, see below).

## Split by verifiability (exec-gating)
| Part | Lang | Verify |
|---|---|---|
| C2 budget accumulator + W3C inject/extract + per-node spans | Python | **offline (me)** — `tests/test_m8_offline.py` |
| C1 proto + Python stubs | proto/Python | offline regen; Go stubs user-run |
| C3 orchestrator + report-service + store-gateway spans | Go | **user-run** (`go build`) |
| C4 pw-executor spans | TS | **user-run** (`npm run build`) |
| live OTLP→Tempo/Jaeger, real budget-kill | — | **user-run** |

## ADR-021 — reconciliation with ADR-018 (not a contradiction)
- **Ephemeral CronJob (batch)** → stays on **Pushgateway/textfile** (ADR-018 unchanged for this path).
- **Long-lived orchestrator/service mode** → **report-service HTTP** `/report` + `/metrics` (scrapeable,
  because the process is long-lived). This is NOT bringing the HTTP server back into the ephemeral job —
  it is a distinct mode.
- Introduces the long-lived **Go orchestrator** (promised in ARCHITECTURE §2 but absent from code).

## (1) Distributed tracing — W3C context propagation
All gated on `OTEL_EXPORTER_OTLP_ENDPOINT` (no-op default, zero overhead — the `brain/otel.py` pattern).
- **brain (Python):** new `otel.inject_context(carrier)` / `extract_context(carrier)` (`TraceContextTextMapPropagator`).
  Per-node spans in `brain/graph.py` (one per LangGraph node — promised in OBSERVABILITY.md, not yet done).
- **brain → pw-executor:** `executor.py` puts `traceparent` into `params._meta` (JSON-RPC) and the MCP `_meta`.
- **brain → store-gateway:** `store.py` gRPC client-interceptor injects W3C into metadata.
- **pw-executor (TS):** new `src/otel.ts` (gated tracer, mirrors brain); `dispatch()` extracts `traceparent`
  from `_meta`, starts a child span per tool call; `RpcRequest` extended with a `_meta` field.
- **store-gateway (Go):** `otelgrpc.StatsHandler` on `grpc.NewServer()` (auto-extracts W3C from metadata +
  server spans); tracer init gated. Optional manual child spans in `internal/store/server.go` methods.

## (2) Hard budget ceiling
- **`brain/budget.py` (new):** `BudgetTracker` — accumulates `prompt+completion` tokens per role
  (`plan`/`heal`) and total; limits from env `PLAN_TOKEN_LIMIT` (default 50000), `HEAL_TOKEN_LIMIT` (20000),
  `TOTAL_TOKEN_LIMIT` (optional). `add(role, result)` after each `backend.complete`; `exceeded(role)` is the
  pre-call guard.
- **Graceful degradation:** on planner-limit breach `LLMPlanner.propose` → heuristic; on heal-limit → L1–L6
  without LLM. The default heuristic path spends no tokens → the ceiling is inert there (the low-value
  rationale for deferring it from M4b).
- **Go-side hard ceiling:** the orchestrator receives the brain's token deltas via `RunControl.ReportEvent`
  (see proto), reconciles against config, and on breach returns `Control{abort=true}`; the brain degrades;
  if it does not comply within a grace period the orchestrator **SIGTERM**s the subprocess (a
  model-independent backstop).

## (3) proto/runcontrol.proto (proto3) — new service
```
service RunControl {
  rpc StartRun (StartRunRequest) returns (StartRunReply);   // register a run (run_id, limits)
  rpc ReportEvent (RunEvent) returns (Control);             // brain → orch per node-step; reply may abort
  rpc Abort (AbortRequest) returns (AbortReply);            // external abort
}
message RunEvent { string run_id; string node; int64 prompt_tokens; int64 completion_tokens; string status; }
message Control  { bool abort; string reason; }
```
Stubs: Python (`grpc_tools.protoc`, offline) + Go (plugin, user-run); **proto-hash assert** (GAP-RISK-008).
The brain is a gRPC client of the orchestrator (gated on `ORCH_ADDR`; unset → standalone, budget Python-side only).

## (3b) Go components (user-run build)
- **`cmd/orchestrator/main.go`:** gRPC `RunControl` server; spawns the brain subprocess (env `ORCH_ADDR`,
  `RUN_ID`, limits); FSM PENDING→RUNNING→…→DONE|ABORTED; health-ping; per-step deadline; budget-kill.
- **`cmd/report-service/main.go`:** HTTP — `/report/<run_id>` (HTML+JSON from `runs/<id>/heal-report.json`,
  Go `html/template`) + `/metrics` (`client_golang`).
- **`cmd/store-gateway/main.go` + `internal/store/server.go`:** otelgrpc StatsHandler + tracer init.
- **`go.mod`:** + `go.opentelemetry.io/otel*`, `otelgrpc`, `client_golang`.

## M8 gate
- **Offline (me):** `tests/test_m8_offline.py` — `BudgetTracker` (accumulate, exceeded, per-role degradation);
  `inject_context`/`extract_context` round-trip + no-op without an endpoint; per-node span no-op; the Python
  runcontrol stub imports. All `test_*_offline` regression green.
- **User-run:** `go build ./cmd/orchestrator ./cmd/report-service ./cmd/store-gateway`; `npm run build`
  (pw-executor + otel deps); run with a real OTLP collector → one trace with cross-language spans
  (brain→TS→Go); real budget-kill (lower the planner limit → orchestrator SIGTERM).

## Out of scope
- D (screenshot determinism) — separate phase. M6 real-provider smoke (GAP-VERIFY-005).
- Full manual span instrumentation of every store method (StatsHandler covers server spans; manual is optional).
