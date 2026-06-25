# Sentinel — Observability and Cost Controls

> 🌐 [Русский](OBSERVABILITY.md) (основная версия) · **English**

Derived from the design synthesis 2026-06-23; canonical summary in ../ARCHITECTURE.md.

---

## Overview

Observability in Sentinel is layered across four orthogonal concerns: distributed tracing
(added at M4), an immutable per-run LLM decision transcript, token budget enforcement with
graceful degradation, and Playwright browser traces. All four are first-class artifacts,
not retrofits.

---

## 1. Distributed Tracing (OpenTelemetry)

OTel spans are introduced at **M4** — not day 1. This is a deliberate anti-over-engineering
choice: the framework is stable before the telemetry layer is added.

**Scope:** Every LangGraph node emits one OTel span. MCP tool calls (pw-executor) and Go
gRPC calls are child spans of their enclosing node span, forming a complete parent-child
hierarchy per run.

**Span attributes per node span:**

| Attribute | Description |
|---|---|
| `run_id` | UUID of the current run |
| `node_name` | LangGraph node (perceive, ground, plan, act, verify, heal, checkpoint, report) |
| `step_index` | Index of the plan step being executed |
| `run_mode` | `explore` / `replay` / `ci` |
| `model` | Model identifier used in this node (if LLM call present) |

**Additional attributes on LLM-call spans:**

| Attribute | Description |
|---|---|
| `prompt_tokens` | Prompt token count |
| `completion_tokens` | Completion token count |
| `latency_ms` | End-to-end LLM call latency in milliseconds |
| `cost_usd` | Computed cost from config-driven price table |
| `decision_type` | Classification of the LLM decision (e.g., `plan_action`, `heal_locator`) |
| `confidence` | Confidence score emitted by the node |
| `prompt_HASH` | SHA-256 of the prompt text — **never the prompt content itself** |

Storing the hash, not the content, prevents secrets embedded in page state from appearing
in trace backends.

**Context propagation:** W3C Trace Context is propagated in gRPC metadata (Go↔Python
boundary) and in MCP call metadata (Python↔pw-executor boundary), so a single trace ID
flows across all three runtime layers.

**Export path:** OTLP → Grafana Alloy → **Tempo** (home lab) / **Jaeger** (dev).

**Sampling:** 100% for both `explore` and `ci` runs. Every run must be auditable for
trust; no head-based dropping.

---

## 2. Immutable LLM Transcript

**Location:** `/runs/{run_id}/llm-transcript.jsonl`

Every LLM call appends exactly one JSON line. The file is `fsync`-ed at run end and is
**never overwritten or mutated** after that point. It is emitted as a CI artifact alongside
`run_report.json`.

**Record schema:**

| Field | Type | Description |
|---|---|---|
| `ts` | ISO-8601 | Timestamp of the call |
| `run_id` | string | Run identifier |
| `step_id` | string | Plan step this call belongs to |
| `node` | string | LangGraph node name |
| `model` | string | Model identifier |
| `prompt_tokens` | int | Prompt token count |
| `completion_tokens` | int | Completion token count |
| `latency_ms` | int | Wall-clock latency |
| `cost_usd` | float | Cost for this call |
| `decision_summary` | string | Human-readable summary of the decision (not the full output) |
| `temperature` | float | Temperature used |

**Use cases:** offline decision debugging; per-node cost attribution; prompt iteration
without re-hitting the API; compliance audit ("what did the agent decide and why").

---

## 3. Token Budget, In-Process Counter, and Go-Side Hard Ceiling

Token budget enforcement uses a three-layer design. Each layer is independent; the outer
layer enforces even if the inner one fails.

### Layer 1 — In-process counter (Python brain)

The brain maintains a `token_usage` dict keyed by `model_id → {prompt, completion, cost_usd}`
and a `token_budget` dict with per-model limits. **Before every LLM call**, the brain checks
the remaining budget. No per-call gRPC round-trip is made — this was an over-engineering
pattern discarded from earlier proposals.

**Config defaults:**

| Budget | Model | Default |
|---|---|---|
| `plan_token_limit` | Opus 4.8 | 50 000 tokens/run |
| `heal_token_limit` | Sonnet 4.6 | 20 000 tokens/run |

### Layer 2 — Go-side hard ceiling (orchestrator)

The Go orchestrator independently enforces a hard ceiling by reconciling the brain's token
counter, received on each `RunEvent` stream message. If the brain ever overruns, Go flags
the overrun and can terminate the brain subprocess. The Go ceiling operates **without** a
per-LLM-call gRPC round-trip — it reconciles at event granularity.

### Layer 3 — Graceful degradation (not abort)

Budget exhaustion does **not** hard-abort the run. Instead:

- **Plan node:** stops issuing new exploration actions; the current plan is frozen as a
  partial plan (plan_hash still computed over available steps).
- **Heal node:** falls back to L1–L6 deterministic strategy rotation only; no Sonnet or
  Opus calls are made.

At **80% utilisation** the brain emits a `BUDGET_WARNING` event (visible in the
orchestrator log and surfaced in the run report).

---

## 4. Playwright Traces

**Source:** `pw-executor` (our TypeScript Playwright execution server) starts/stops a
trace per run. One `trace.zip` is written to the shared artifact directory configured at
server launch.

**Relay path:** `pw-executor` → path returned in MCP tool response → Python brain →
gRPC `RunEvent` → Go orchestrator → report-service serves it at `/runs/{run_id}/trace`.

**Viewing:** `playwright show-trace trace.zip`

No custom trace infrastructure is required; Playwright's built-in trace format (network,
console, DOM snapshots, screenshots, action timeline) is the primary CI-failure debugging
artifact.

---

## 5. Prometheus Metrics

Exposed by `report-service` at `/metrics` (standard Prometheus scrape endpoint).
A Grafana dashboard template is shipped in the repository.

| Metric | Labels | Description |
|---|---|---|
| `agent_run_total` | `mode`, `status` | Counter of completed runs by mode and exit status |
| `agent_run_duration_seconds` | — | Histogram of total run wall-clock time |
| `agent_tokens_total` | `model`, `node` | Counter of tokens consumed, by model and LangGraph node |
| `agent_cost_usd_total` | `model` | Counter of cost in USD, by model |
| `agent_heal_attempts_total` | `strategy`, `outcome` | Counter of heal attempts by strategy (L1–L6, llm_a11y, llm_visual, cache) and outcome |
| `agent_heal_success_rate` | — | Gauge: rolling ratio of successful heals to total attempts |
| `healing_confidence_histogram` | `strategy` | Histogram of final confidence scores per healing strategy — the calibration signal |
| `agent_flake_quarantine_count` | — | Gauge: number of currently quarantined steps |
| `agent_a11y_completeness_ratio` | `url` | Histogram of per-page completeness_ratio (canvas-heavy-app early warning) |
| `agent_budget_remaining_ratio` | — | Gauge: fraction of token budget remaining (plan + heal combined) |

---

## 6. Alertmanager Rules

| Alert name | Condition | Severity | Action |
|---|---|---|---|
| `DOM_INSTABILITY` | `agent_heal_attempts_total` rate > 0.20 per run | warning | Investigate AUT DOM churn; review strategy_degradation events |
| `BUDGET_WARNING` | `agent_budget_remaining_ratio` < 0.20 (i.e., budget > 80% consumed) | warning | Review explore scope; consider raising limits or scoping AUT surface |
| `CI_QUARANTINE_THRESHOLD` | `agent_flake_quarantine_count` > 5 | critical | **Blocks CI pipeline**; review quarantined steps with `agentctl locators list` |
