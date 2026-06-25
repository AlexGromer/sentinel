# M4b Contract — "Observability" (frozen 2026-06-24)

> 🌐 [Русский](M4b_CONTRACT.md) (основная версия) · **English**

Goal: distributed tracing + push metrics for runs, now that the Go service layer (M2b) exists.

## Scope decision (ADR-018) — honest
**In M4b (offline-authorable, gated export):**
- **OTel tracing in the brain** — a run span + LLM spans carrying **prompt_HASH, never prompt content**;
  exports to an OTLP collector (→ Tempo) IFF `OTEL_EXPORTER_OTLP_ENDPOINT` is set, else **no-op** (zero overhead).
- **Prometheus Pushgateway** for the batch metrics (`PROM_PUSHGATEWAY`), since the agent is a **CronJob**.

**Deferred (with rationale, → GAP-OBS-001):**
- Always-on Go `report-service` HTTP `/metrics` endpoint — the agent is an **ephemeral batch job**, not a
  scrapeable service; Pushgateway / node_exporter textfile fit the model. (HTML/JSON report already ships in M4.)
- TS (`pw-executor`) + Go (`store-gateway`) OTel spans — extension points (W3C context propagation in gRPC/MCP metadata).
- Go-side hard budget ceiling — needs a long-running Go orchestrator + brain→Go token reporting; the default
  heuristic path uses no LLM, so low value now.

## OTel (`brain/otel.py`)
- `setup_tracing()` — if `OTEL_EXPORTER_OTLP_ENDPOINT` is set, configure a `TracerProvider` +
  `OTLPSpanExporter` (gRPC) + `BatchSpanProcessor` + `Resource(service.name="sentinel-brain")`; else a no-op.
  Robust to OTel not being installed (try/except → no-op).
- `span(name, **attrs)` contextmanager; `prompt_hash(text)` = `sha256`.
- Spans: `sentinel.run` (run_id, mode, transport, store); `heal.llm` / `plan.llm` (model, **prompt_hash**,
  prompt_tokens, completion_tokens). **Span attributes NEVER carry prompt or page content.** Sampling 100%.

## Prometheus Pushgateway (`brain/report.py`)
`push_metrics(report, gateway, job="sentinel", grouping)` builds a `CollectorRegistry` with the same
`sentinel_*` series and `push_to_gateway(...)`. Called from the `report` mode when `PROM_PUSHGATEWAY` is set;
the `metrics.prom` textfile still ships.

## Acceptance gate (Given/When/Then)
- **Unset:** `OTEL_EXPORTER_OTLP_ENDPOINT` / `PROM_PUSHGATEWAY` absent → spans no-op, no push, suites green, zero new failure modes.
- **Set (user, with a collector/gateway):** run traces appear in Tempo; metrics appear in the Pushgateway.
- **Offline test:** `otel.setup_tracing()` no-op path + `span()` work without a collector; `push_metrics` import + no-op guard.

## Out of scope
Go report-service HTTP, TS/Go spans, Go budget ceiling (GAP-OBS-001); SLO dashboards.
