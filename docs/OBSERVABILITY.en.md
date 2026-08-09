# Sentinel — Observability and Cost Controls

> 🌐 [Русский](OBSERVABILITY.md) (основная версия) · **English**

Derived from the design synthesis 2026-06-23; canonical summary in ../ARCHITECTURE.md.

> **M13 (2026-07-04):** the store-gateway gained a `metrics` domain (time-series schema + RPC, ADR-050) — today it is schema+RPC only (population from a real writer + native metrics-in-UI charts = **M15**, ADR-051). The Prometheus/Pushgateway export (below) is untouched — it stays optional.

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

**Span attributes (as-built):** individual node spans (`node.<name>`, one per LangGraph node)
carry **no attributes** — `_traced()` opens the span with no kwargs (`brain/graph.py:447-452`).
Attributes are set on only two span kinds:

| Span | Attributes | Source |
|---|---|---|
| `sentinel.run` — chat (`brain/__main__.py:238`) | `run_id`, `mode`, `conversation_id`, `store` | — |
| `sentinel.run` — explore/replay (`brain/__main__.py:509-511`) | `run_id`, `mode`, `transport`, `store` | — |
| `heal.llm` (LLM call in the heal node) | `model`, `prompt_hash`, `llm.prompt_tokens`, `llm.completion_tokens` | `brain/healing.py:129`, `brain/otel.py:53-64` |

`step_index`, per-node `run_mode`, `latency_ms`, `cost_usd`, `decision_type`, `confidence` are
target schema from the early design; they are never set in code.

Storing `prompt_hash` (SHA-256 of the prompt text), not the prompt itself, prevents secrets
embedded in page state from appearing in trace backends.

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
`report.json`/`report.html` (`brain/report.py::generate()`).

**Record schema:**

| Field | Type | Description |
|---|---|---|
The real record (`tx_write`, `brain/graph.py:225-245` → `brain/__main__.py:113-115`) is exactly **7 fields**:

| Field | Type | Description |
|---|---|---|
| `step` | int | Step number |
| `planner` | string | Decision planner (`llm` \| `heuristic`) |
| `model` | string \| null | Model identifier (null for heuristic) |
| `decision` | string | The action taken |
| `reason` | string | Rationale for the decision |
| `prompt_tokens` | int \| null | Prompt tokens |
| `completion_tokens` | int \| null | Completion tokens |

The fields `ts`/`run_id`/`step_id`/`node`/`latency_ms`/`cost_usd`/`decision_summary`/`temperature` are **not** in the record.

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
| `plan_token_limit` | Opus 4.8 (default) | 50 000 tokens/run |
| `heal_token_limit` | Sonnet 4.6 (default) | 20 000 tokens/run |

> **M6 (ADR-019):** the models in this table are **defaults**, routed through the `LLMBackend`
> (per-role `LLM_BACKEND*`). Post-M6 token/cost data may carry a non-Anthropic model id — but
> this isn't a Prometheus label: as of M15.1, `model` is written into the metric points'
> `labels_json` in the store-gateway SQLite `metrics` domain (`cmd/control-api/main.go:660`),
> see §5.

### Layer 2 — Go-side hard ceiling (orchestrator)

The Go orchestrator independently enforces a hard ceiling by reconciling the brain's token
counter, received on each `RunEvent` stream message. If the brain ever overruns, Go flags
the overrun and can terminate the brain subprocess. The Go ceiling operates **without** a
per-LLM-call gRPC round-trip — it reconciles at event granularity.

### Layer 3 — Graceful degradation (not abort)

Budget exhaustion does **not** hard-abort the run. Instead:

- **Plan node:** stops issuing new exploration actions; the current plan is frozen as a
  partial plan (plan_hash still computed over available steps).
- **Heal node:** falls back to L1–L6 deterministic strategy rotation only; no heal/plan
  model calls (Sonnet/Opus by default) are made.

The threshold isn't 80% — it's full exhaustion: `BudgetTracker.exceeded(role)` returns `True`
once that role's counter (or `total_limit`) reaches the limit (`brain/budget.py:63-68`), and the
calling node degrades as described above. There is no `BUDGET_WARNING` event in code — that was
part of the early design and was never implemented. Actual spend (per-role `prompt`/`completion`/
`total`, `summary()`) is written into the report artifacts (`plan.json` / `heal-report.json`);
the control-API converts it into `cost_usd` (§5) — not Prometheus.

---

## 4. Playwright Traces

**Source:** `pw-executor` (our TypeScript Playwright execution server) starts/stops a
trace per run. One `trace.zip` is written to the shared artifact directory configured at
server launch.

**Relay path:** the **brain** computes the trace path itself (`brain/__main__.py:95`) and passes it to
`pw-executor` via `browser.traceStop(path=…)` (`:143`); `pw-executor` writes the file through Playwright
(`pw-executor/src/server.ts:421-428`) and merely **echoes** the path back — it never originates it.
The gRPC `RunEvent` carries no trace field, and no Go code writes `trace.zip` (only `sweepTraces` deletes
old ones). There is no separate HTTP service for traces anymore — `cmd/report-service` has been removed
(ADR-119, 2026-08-09): it was built and signed but never launched by anything. `trace.zip` is read
directly from the run directory (or from the CI artifact), or served through control-api's artifact
whitelist — `GET /v1/runs/{id}/artifact?name=trace.zip` (token-gated, ADR-099).

**Viewing:** `playwright show-trace trace.zip`

No custom trace infrastructure is required; Playwright's built-in trace format (network,
console, DOM snapshots, screenshots, action timeline) is the primary CI-failure debugging
artifact.

---

## 5. Prometheus Metrics

The aggregate scrape is served by **control-api** at the root `GET /metrics` (ADR-119,
`cmd/control-api/metrics_agg.go`). Before 2026-08-09 the route belonged to `report-service` — a
binary that was built and signed but launched by NOTHING: absent from the `Dockerfile`, every
compose file, and `install.sh`. The route is `accessAuthed`: a regular token sees the aggregate over
its OWN runs; the machine token and a deployment with no accounts (`owner == ""`) see every run. No
Grafana dashboard and no Alertmanager rules file ship in the repository.

**The aggregate and the per-run artifact are now DIFFERENT things.** `<run_dir>/metrics.prom`
(`_metrics()` in `brain/report.py:14-35`) is unchanged: a plain node_exporter textfile with no label,
no `# HELP`/`# TYPE`. A byte-for-byte concatenation of these files would have produced N duplicates of
the same series (a broken scrape, not an aggregate), so the merge happens IN CONTROL-API on every
`/metrics` request: each series gets a `run="<id>"` label, series are grouped by family, and each
family gets exactly one `# HELP`/`# TYPE` header (the type is DERIVED from the name — a `_total`
suffix means counter, everything else gauge). Fixing `brain/report.py` alone would not have been
enough: this repository already has 192 run directories with no run label, and nobody is going to
rewrite them.

| Per-run metric | Labels | Description |
|---|---|---|
| `sentinel_run_steps` | `run` | Number of steps in the run |
| `sentinel_run_exit_code` | `run` | Structured exit code of the run |
| `sentinel_heal_total` | `run` | Count of healed steps |
| `sentinel_heal_by_strategy_total` | `run`, `strategy` | Count of heals by strategy — `strategy` ∈ the `PRIORS` keys (`testid`, `role_name`, `label`, `text_role`, `css`, `xpath`, `visual`), or `unknown` (on a cache hit the cached locator's own strategy — a `PRIORS` key — is returned; the literal `cache` is never emitted as a label) |
| `sentinel_regression_total` | `run`, `kind` | Count of regressions by kind — `kind` ∈ {`a11y`, `visual`} |
| `sentinel_quarantined_total` | `run` | Number of currently quarantined steps |
| `sentinel_failed_total` | `run` | Number of failed steps |

The response is capped at the **500 newest runs** (newest first), and what is dropped is STATED, not
silent — three metrics about the aggregator itself:

| Aggregator metric | Labels | Description |
|---|---|---|
| `sentinel_metrics_runs_included` | — | Run directories included in this response, after scoping to the caller |
| `sentinel_metrics_runs_omitted` | `reason` ∈ {`cap`, `unreadable`, `too_large`, `conflict`} | Run directories the aggregator did not include, by reason |
| `sentinel_metrics_lines_dropped` | — | Lines of a source `metrics.prom` that were not recognisable Prometheus textfile syntax |

The walk of `runs/` is cached for 10 s with single-flight (one disk walk serves N concurrent callers);
unreadable directories (this repository has some left root-owned by Docker) are skipped and counted
under `runs_omitted{reason="unreadable"}` rather than failing the request with 5xx.

**Tokens and cost are still not a Prometheus metric.** As of M15.1 they're computed by the control-API
(`cmd/control-api/main.go:666-675`, priced by `costUSD` at `:561`) and written as points into the
store-gateway `metrics` domain (SQLite, ADR-050/051) with `model` in `labels_json`; the UI
renders them natively — not via `/metrics`.

**The scrape requires a credential** — `/metrics` is `accessAuthed`, like every other control-api
service route. A working `scrape_configs` fragment:

```yaml
scrape_configs:
  - job_name: sentinel
    metrics_path: /metrics
    bearer_token_file: /etc/prometheus/secrets/sentinel-token
    static_configs:
      - targets: ["control-api:8090"]
```

The token is the same bearer used by the rest of control-API (`state/control-api.token` in a
docker-compose deployment, or the machine token in a deployment with accounts); without it `/metrics`
answers 401.

**Separate path (optional, unchanged by this edit):** `push_metrics()` (`brain/report.py:70-85`)
pushes 5 gauge values to a Prometheus Pushgateway per run — `sentinel_run_steps`, `sentinel_run_exit_code`,
`sentinel_heal_total`, `sentinel_failed_total`, and `sentinel_regression_a11y_total` (note: this
name differs from `sentinel_regression_total{kind="a11y"}` in the text-file path above — don't
conflate the two).

---

## 6. Alertmanager — design target (not shipped)

Neither an Alertmanager rules file nor a `BUDGET_WARNING` event exists — the latter is absent
from code entirely (see §3). The table below is the design target from the early synthesis,
expressed against the real §5 metrics, not as-built behavior.

| Alert name | Condition (target) | Severity | Action |
|---|---|---|---|
| `DOM_INSTABILITY` | `sentinel_heal_by_strategy_total` rate > 0.20 per run | warning | Investigate AUT DOM churn |
| `CI_QUARANTINE_THRESHOLD` | `sentinel_quarantined_total` > 5 | critical | **Blocks CI pipeline**; review quarantined steps with `agentctl locators list` |

---

## 7. Two event streams and a message catalogue (ADR-065 · 067 · 068 rev.2)

Sections 1–6 describe **infrastructural** observability — traces, metrics, budget. This section is about
observability **of a run as a person sees it**, introduced in M9-LIVE.

**One merged stdout+stderr stream is split by AUDIENCE rather than by severity** (`logsink.go`):

| file | what it is | who reads it |
|---|---|---|
| `runs/<id>/logs/events.jsonl` | AG-UI frames — the run's **narrative**: "step 2 of 40", "click button Sign in", the healing strategy and its outcome | the Live view, the live timeline |
| `runs/<id>/logs/run.jsonl` | structured **diagnostics** with level, category, module, step and source | the Logs view |
| `runs/<id>/logs/run.log` | the raw stream, 1:1 | a person with `grep`, when the two projections above do not answer the question |

Why this is not one "correct" projection: the AG-UI frames are **82% of a run's output** and they are not
noise — they carry the story. Assigning a log level to a story destroys it, so it gets its own file and its
own view, and the diagnostics stop being 82% protocol. The frames stay in the in-memory ring buffer
regardless — the WS `/v1/stream` subscription replays it to drive the live timeline; the sink is a **second**
consumer of the same lines, which is what keeps the split from touching the live path at all.

**Writes are immediate.** Collapsing repeats and nesting stack frames are PRESENTATION concerns and live on
the read side (`handleRunLogs`), not the write side. The first version held a record back to count its
repeats, and a live run proved that wrong twice over: a stuck run emits nothing different, so the held
record stayed out of the file for as long as the loop lasted — the very case collapsing exists to expose.
Also, real repeats arrive about 5 s apart, so any deadline short enough to keep the loop visible was also
too short to collapse anything.

**The message catalogue** (`brain/events.json`) is the single source of truth for every human-facing line.
A `code` travels on the wire, the text comes from the catalogue, and the placeholder VALUES are recovered by
matching the English template against the server-rendered string. Axes:

- **level** — debug/info/warn/error;
- **category** — run · plan · heal · hitl · llm · record · system · browser · app · test;
- **source** derived from the category (`brain/embed.go::SourceOf`): the tool · the application · testing;
- **audience** (ADR-068 rev.2) — a layer above the sources: `business` (application+testing) versus `tool`;
  the gate requires audiences to partition sources **exactly**;
- **`degrades: true`** (34 codes) — the one legitimate crossing from diagnostics into the narrative: a run
  that exits zero with the LLM absent must be able to say so on its verdict rather than hide in the log;
- **fault** (`fault`, ADR-113) — WHOSE problem this is: `none` · `app` · `tool` · `test` · `config`. Only
  codes that can END a run carry it, and that rule is derived rather than listed: an entry declaring
  `exit` terminates a run by definition, so the same set must say whose problem the ending is. The axis
  is deliberately orthogonal to the verdict — `problem` stays the OUTCOME, and "whose" is answered
  separately, because folding them together would need one word per (outcome × domain) pair. A record
  carrying a `fault` stamps it on the `run.jsonl` line too, so the file stays self-describing for
  whoever greps it later.

**Why the axis was needed.** The verdict word fused three different endings into one: `exit 1` ("a step
failed" — a fact about the application), `exit 4` ("our own code threw", ADR-087) and `exit -1` ("we were
killed by a signal") all reached the reader as `problem`, i.e. as an invitation to go and debug their
application. Worse, HEALTH-001 gave refusals-to-start `exit 3`, which already meant `integrity` — so a run
refused because the model endpoint was unreachable rendered as "plan_hash/golden mismatch" when no plan
and no baseline were involved. The precise answer is known not by the exit code but by the code that
ENDED the run, so that is what decides (`cmd/control-api/fault.go`); `exit_codes[N].fault` is only the
fallback for a run whose log could not be read.

The gate `tests/test_event_catalog_offline.py` holds this in both directions: every code a module emits
exists in the catalogue and names that module; every catalogue entry names modules that really emit it.
Anchoring is **per module, not per line** — line anchoring went stale all at once on the first conversion.

**What is missing here.** Application events (`app.*`, seven codes) reach `run.jsonl` and do not reach the
verdict — a run can report `exit 0` while the application throws exceptions. That is `GAP-PROD-001`, analysed
in `docs/REGRESSION_MAP.en.md` §6. There is also no write-side redaction for foreign output —
`GAP-SEC-005`, `docs/THREAT_MODEL.en.md` §4.12.

## 8. Three journal streams, and what is not in them (HEALTH-005 · ADR-116)

Section 7 is about observing a RUN. This one is about observing **the tool itself**: what it did, when,
and on whose instruction. The measurement behind the section: before HEALTH-005 the service plane was
logged nowhere — `session.go` and `configfile.go` had ZERO logging lines of either kind, so creating an
account, changing a password, deleting an account and editing the global config left no trace at all.

| stream | where it lives | what it carries | how it is read |
|---|---|---|---|
| **run** | `runs/<id>/logs/run.jsonl` | diagnostics of ONE run (section 7) | the Logs view · `GET /v1/runs/{id}/logs` · `agentctl logs` |
| **service** | `state/logs/service.jsonl` | what the tool did: sign-ins and FAILED sign-ins, accounts, configuration changes, refusals, services starting and stopping | the Service journal view · `GET /v1/service-log` · `agentctl service-log` |
| **foreign services** | the docker journal | output of `ollama`, `litellm`, `webui` — programs that are not ours | `docker compose logs <service>` |

**Why the service stream is a FILE and not a store table.** `s.store == nil` is a supported tier
(ADR-075), and an audit trail absent from the deployment where people most often work alone is not an
audit trail. And a store that has fallen over must not take down the record of it falling over.

**One file for every service, distinguished by `svc`.** Three writers: `control-api`, `agentctl` (only
`service.log_purged`) and `browser`. Four files would mean answering "what happened at 14:32" by
merging them by hand.

**Levels remove VOLUME, not selection** (the directive was: record everything, and have levels). Reads
are `debug`, mutations `info`, a refusal or a reach for someone else's row `warn`, a 5xx `error`;
`info` by default. The filter is on the WRITE side, because a journal whose noise is only suppressed by
a read filter still pays for it in disk.

**Rotation.** `state/logs/service.jsonl` plus one previous generation `.1`; the threshold is
`SENTINEL_SERVICE_LOG_MAX_MB` (16 by default). Since PR-C the compose services carry `logging:` with
`max-size: 10m` and `max-file: 3` — before it no driver was configured at all, so there was no rotation
and the logs died with the container.

### What is NOT in these streams — stated, not left to be discovered

- **Configuration values.** `service.config_changed` records WHICH SECTIONS changed and never their
  values: those carry endpoints, budgets and paths, and the journal would become a second copy of the
  configuration with none of the protection the first one has.
- **Passwords or request bodies.** A failed sign-in records the name that was TRIED and never the
  password. Request and response bodies are not recorded at all.
- **The content of what was deleted.** `agentctl purge-service` prints counts and never content — the
  same posture as `redact-trace` (ADR-098) and `purge-store` (ADR-100).
- **Browser-service records written during a purge.** It writes from Node, where `flock` needs a native
  module this project does not take. The purge re-reads whatever was appended since its snapshot, so
  the window is one syscall wide — but it is not zero. The Go writers do take the lock and lose nothing.
- **Structure for foreign services.** `ollama`, `litellm` and `webui` write in their own formats and we
  do NOT parse them: turning someone else's output into a structure it does not have is inventing data.
  They get docker's rotation and `docker compose logs`; the boundary is here, and it is deliberate.
- **Who sees what.** A regular account sees events it owns; events with no owner — service start, global
  config, store failures — are visible only to an admin or the machine token. A partial view SAYS it is
  partial (`scoped` and `scope_reason` in the answer), because "no records" and "not your records" are
  different answers and an empty list cannot tell them apart.

---

---
