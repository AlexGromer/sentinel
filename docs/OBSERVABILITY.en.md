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

### Layer 4 — The supervision no delivery form had until 2026-08-16 (ADR-126)

The three layers above live **in the brain** and always work. Layer 4 — the model-INDEPENDENT stop
that ADR-021 introduced `orchestrator` for — worked **nowhere**, and that is worth saying plainly:
the binary was built by every release and installed by the `.deb`, while no `docker-compose*.yml`, no
`Dockerfile` and no `install.sh` launched it, and no systemd unit existed. Dead alongside it were
**takeover and return** (ADR-054) and the **map gate** (ADR-108c): `control-api` answered
`"no orchestrator wired"`, and `brain/runcontrol.py::_Noop.wired` was `False`.

It is now a **long-lived service**, and the division of labour is the whole design:

* **The orchestrator DECIDES.** `orchestrator --serve --addr <socket>` keeps a per-run ledger and
  declares a breach. One socket per deployment, not one per run: `CONTROL_API_ORCH_ADDR` is read
  ONCE at control-api startup, so the per-run path `state/sentinel-orch-<id>.sock` could never be
  named at all — which is also why the old form could not support two concurrent runs.
* **control-api ENFORCES.** It registers the run (`StartRun`) under ITS OWN id — the same one the hub
  shows, the store persists and `Takeover(run_id)` carries; the orchestrator used to mint its own, so
  a takeover would have addressed a run it had never heard of. The poll is a **zero-delta**
  `ReportEvent` (an idiom that already existed: `brain/runcontrol.py::poll()` is exactly that), and a
  declared breach calls `cancel.go`.

⚠ **Why control-api enforces and not the orchestrator itself.** The old backstop was
`cmd.Process.Signal(SIGTERM)`, possible only because that process was the brain's parent. A service
cannot do it **physically**: under compose each service has its own PID namespace. control-api's
mechanism is strictly **better** than the old one: the run has its own process group, and the signal
goes to the GROUP — SIGTERM, then SIGKILL — reaching the brain, the executor and Chromium where the
old backstop reached a single process.

⚠ **Supervision is an addition, not a precondition.** No orchestrator, an unreachable socket, a
service that dies mid-run — the run proceeds as before. Each failure is said ONCE rather than per
poll: a supervisor that floods the journal with a line every two seconds after losing its peer is one
people turn off.

Turning it on takes two actions, and the second is the easier to forget:

```bash
docker compose up -d                       # the orchestrator service is already in the set
# .deb:
sudo systemctl enable --now sentinel-orchestrator
# and, necessarily, the address in /etc/sentinel/control-api.env:
CONTROL_API_ORCH_ADDR=unix:/var/lib/sentinel/orch.sock
```

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
answers **403**, not 401: the guard's refusal is always `403` (`denyCredential` in
`cmd/control-api/access.go`). The only route that answers `401` is `POST /v1/login` on an invalid
name/password pair (`cmd/control-api/session.go`). The route is declared `accessAuthed` with no
`legacyOpen`, so a credential is required even in a deployment without accounts.

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

**One file for every service, distinguished by `svc`.** Four writers: `control-api`, `agentctl`
(`service.log_purged` and `service.vnc_password_source`), `browser` and — as of LIVE-VNC —
`browser-vnc`. ⚠ The last two run THE SAME binary, and only `SENTINEL_SVC_NAME` tells them apart:
without that variable both would write `svc: "browser"`, and the field would stop answering the
question it exists for. Four files would mean answering "what happened at 14:32" by
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

## 9. The live view: about the RUN, not the service (LIVE-PER-RUN, ADR-121)

Sections 1–8 did not distinguish two runs going at once in the same browser: `/live/status`,
`/live/frame.jpg` and `/live/mjpeg` (ADR-111) answered about the **service** — whichever page happened
to be open last. With two runs in flight at once, that meant the viewer of the first run saw the
second. Showing another run's picture is worse than showing none — absence is visible, and a wrong
picture is indistinguishable from a right one.

**The routes accept `run_id`, and it is OPTIONAL.**

| Route | CLI | Without `run_id` | With `run_id` |
|---|---|---|---|
| `GET /v1/live/status` | `agentctl live status [--run-id <id>]` | answers about the newest open page, `scoped:false` | answers about this run's own page, or names the refusal reason, `scoped:true` |
| `GET /v1/live/frame.jpg` | `agentctl live frame [--run-id <id>]` | same, plus frame headers (below) | same |
| `GET /v1/live/mjpeg` | `agentctl live stream [--run-id <id>]` | same | same |

An unnamed request — what `agentctl live frame` with no flag sends, and every piece of code that
predates ADR-121 — stays legal: it is not refused, it answers about the newest page, as before. What
changed is that the answer now **says so explicitly** — the `scoped:false` field in the `/live/status`
JSON, and `X-Sentinel-Run`/`X-Sentinel-Scoped` headers on the frame itself (a JPEG cannot say this in
its body). Before ADR-121, "this is the newest page" and "this is your run" were indistinguishable in
the response.

**The claim (`POST /live/claim`).** As soon as the executor (`pw-executor/src/server.ts::claimLivePage`)
has a page, it reads that page's Chromium `targetId` (`Target.getTargetInfo`) and announces it to the
browser service: `{run_id, target_id}`. This works even when the executor does NOT own the page (CDP-
attach mode, ADR-037): `targetId` is the one identifier CDP hands out identically to the owner and to a
client that only adopted someone else's page — established by a direct measurement, not an assumption,
before any code was written. The page is NOT labelled by arrival order, URL or creation time — all
three break exactly when two runs overlap.

The claim is **fail-open**: a failure (the service unreachable, `targetId` could not be read, a
timeout) does not fail the run — observation must not be allowed to kill work. But it is not silent
either: the reason is logged on the executor side (`log('live claim failed …')`).

The claim address is **derived** from `PW_CDP_ENDPOINT`, already set for control-api in compose (the
CDP-relay port is swapped for `CDP_LIVE_PORT`, the path for `/live/claim`; the derivation is announced
in the log as `live claim endpoint derived from PW_CDP_ENDPOINT: …`), overridable via an explicit
`PW_LIVE_CLAIM`. No second address was added to compose — that is a deliberate decision, not an
oversight: `docker-compose.yml` did not need to change, and a second address would have been a second
way to drift, exactly as already happened once with a YAML merge key that swallowed an entire
`environment:` block.

**Refusal instead of substitution.** A named run that cannot be resolved gets a `503` with a plain-text
reason — three cases are distinguished, not collapsed into one generic "no picture":

| Reason | When |
|---|---|
| "run \<id\> has not claimed a page — …" | the run has not claimed a page yet (its browser has not started, or this deployment has no browser service at all) |
| "run \<id\> claimed a page that is no longer open — …" | a claim exists but the page is closed (the run finished) |
| "the browser has no page yet — start a run first" | (unnamed request only) the browser has no page open at all |
| "run \<id\> shares one browser page with …" | ⚠ **version skew, not ordinary work.** Two runs claimed ONE page. Before ADR-128 this was the topology (the executor adopted `pages()[0]`, so the second run drove the same tab); a run now opens its own, so the only remaining way here is an executor older than ADR-128 against a newer browser service. The refusal says exactly that, and the case itself is written to the service journal as `service.live_claim_conflict` |

A claim outlives its page on the `CDP_LIVE_PORT` service for `CLAIM_TTL_MS` (12 hours by default) —
that is what makes "the run finished, its page closed" distinguishable from "the run never claimed a
page": the claim does not vanish the moment the page closes.

**ADR-128: every run gets its own page.** The mechanism above was built while runs shared one tab: in CDP-attach the executor adopted `pages()[0]`, so two concurrent runs announced ONE `targetId` (measured live: both `84DC6185`) and `resolve` honestly refused BOTH — a picture cannot be attributed to one of two. A run now opens its own page (`context.newPage()`) in the same adopted context: the user's session is reused, their open tab is not. Measured after the change: two runs on different fixtures give different `url`s in `/live/status` and frames that differ byte-for-byte (9 882 vs 10 105 bytes), each carrying its own `X-Sentinel-Run`. The whole claim machinery — `targetId`, `SENTINEL_RUN_ID`, `liveTargetURL`, `scoped:false` for an unnamed request — is exactly as it was; what changed is that "two runs" stopped being grounds for a refusal and became ordinary work.

⚠ **A run closes its pages, and that is visible from here.** Its own and the popups it opened, on every exit path including being killed by a signal. So once a run ends, `?run_id=` answers with the table's second row ("the page is closed") instead of the neighbouring tab's picture — not a new reason, the same one become ordinary. ⚠ Its own PAGE does not make video recording possible over CDP: `recordVideo` is a property of the CONTEXT at creation and the context stays adopted, so ADR-125's refusal at the door stands.

**`/live/status` carries three new fields**, without renaming the old ones (`streaming`/`has_page`/
`url`/`last_frame_ts`/`ack_errors`/`error` are unchanged):

| field | meaning |
|---|---|
| `run_id` | echoes the requested `run_id`, or `null` |
| `scoped` | `true` if the answer is about the CLAIMED page of exactly this run; `false` — about the newest page in general |
| `reason` | the refusal reason (see table above), or `null` |

**`SENTINEL_RUN_ID`.** Before ADR-121, `agentctl run` minted a fresh random `runID` with no link to the
name control-api had already given the run's directory (`runs/control-<id>` at `spawnRun`) — the claim
would have gone out under a DIFFERENT name, and the hub, which asks about control-api's `<id>`, would
never have met it. `cmd/control-api/main.go::spawnRun` passes the child process `SENTINEL_RUN_ID=<id>`;
`cmd/agentctl/main.go::cmdRun` uses it in place of a fresh `runID` when it is set. The variable name is
deliberately NOT `RUN_ID` — a common shell variable whose accidental inheritance would give two
unrelated runs the same claimed identity.

**One URL builder in control-api.** `cmd/control-api/live.go::liveTargetURL` is the single function
both proxying handlers (`handleLiveStatus`, `proxyLive`) go through. Before it, they built the browser-
service URL independently, and that drifted unnoticed: `?run_id=` was appended to `proxyLive`'s
request but dropped on the floor in `handleLiveStatus` — nothing failed, `/live/status` simply answered
about a different page than `/live/frame.jpg` did. Only `run_id` is forwarded, never the whole query
string, so this proxy cannot become a way to hand the browser service arbitrary parameters.

**Frame headers.** `GET /v1/live/frame.jpg` and `GET /v1/live/mjpeg` carry `X-Sentinel-Run` (echoes the
requested `run_id`, empty for an unnamed request) and `X-Sentinel-Scoped` (`"true"`/`"false"`) —
control-api forwards them from the browser service's response (`proxyLive`). This is the only way to
learn a frame's ownership WITHOUT reading JSON: the body is a plain JPEG or a multipart stream.

---

## 10. Choosing an observation mode (LIVE-MATRIX, ADR-120)

Sections 1–9 described infrastructural observability and one service's live view. This section is
about HOW MUCH a run shows and to whom, and that is a USER choice, not derived state.

Before ADR-120, observation was governed by four unrelated switches, read in four places across
three processes and two languages — `PW_HEADED`/`PW_HEADLESS` (`pw-executor/src/launch.ts:26`),
`SENTINEL_TRACE_SCREENSHOTS` (`pw-executor/src/server.ts:489`), `SENTINEL_LIVE_FRAMES`
(`brain/graph.py:45`), `PW_NO_TRACE` (not a mode — see below) — and no single place knew all four.
Worse: ONE picture — the per-step frame — was gated by TWO variables in TWO languages, so switching
one off produced a half-observed run that said nothing about the missing half. The resolver
`brain/observe.py` (`resolve()`/`apply()`) collapses the choice into one place and writes both frame
variables TOGETHER — it is the only writer (a gate walks all of `brain/` to hold that). A third rides
the same expansion for the same reason — `SENTINEL_DECORATE` (LIVE-HUMAN): not because it is a third
frame switch, but because one decision must not acquire a second author.

### Five modes and their cost

| Mode | What is captured | Cost |
|---|---|---|
| `off` | nothing | fastest; there will be nothing to look at |
| `frames` (default) | one frame per step, rendered by the hub | slows a run slightly |
| `stream` | `frames` + the live screencast, undecorated | usable by a person as-is, and by a machine |
| `human` | `stream` + synthetic cursor + `slowMo` + highlight | **CHANGES TIMING** — not for response times, races, timeouts; does not mix with golden mode |
| `record` ⚠ currently refused | a video file as an artifact after the run | does not affect the live view |

The cost text lives in ONE place — `brain/observe.py::COST` (ru+en per mode) — and flows from there
into the control-api schema (`cost` on the `observe` field), which the hub form reads. Hard-coding
it a second time would mean a second copy destined to drift from the first.

### Where it is chosen — three surfaces, one name

1. **Deployment settings** — the default (`frames`), declared in the schema
   (`cmd/control-api/main.go`, field `observe.default`), never implied silently.
2. **The hub's run form** (`docs/index.html`, the `b-observe` selector) — filled from
   `GET /v1/config-schema` (`lvFillObserve`); the first list item NAMES the inherited default rather
   than leaving the field blank with no explanation; selecting a mode shows its cost (the
   `b-observe-cost` box).
3. **`agentctl run --observe off|frames|stream|human|record`** — the same name, the same set of
   values, from a terminal.

An empty value is NOT `off` — it is "no choice was made", and the resolver
(`brain/observe.py::resolve()`) expands it into `DEFAULT`, naming that in the log
(`run.observation`, whose `why` reads "by default, nothing was asked for"). That keeps "I did not
choose" and "I chose exactly `frames`" different facts rather than the same act.

### `human` — shipped (LIVE-HUMAN)

The mode was DECLARED and refused with the task named; LIVE-HUMAN brought the machinery, and `human`
left `NOT_YET` IN THE SAME CHANGE as the switch that performs it. Apart, those two halves are exactly
the state the refusal existed to prevent: a resolver that decides, a variable nobody exports, and an
executor reading something nobody sets.

One switch, two ends, nothing else:

* **`SENTINEL_DECORATE`** = `1`/`0`. It is written ONLY by `brain/observe.py::apply()` — out of
  `plan.decorations`, by the same `setdefault` move as the two frame switches — and it is reported by
  `overrides()`, so a switch set BY HAND survives the resolver and is NAMED in the same log line. It
  is read ONLY by `pw-executor`.
* Decoration is part of the USER's mode, not derived state: `decorations` is true for `human` and
  false for everything else, and nothing inside a run may turn it on by itself.
* On the frame axis `human` behaves exactly like `stream`: the live screencast is not gated by this
  plan at all (the executor starts it), so "like stream" reduces here to "frames stay on", which
  `mode != off` already yields. There is no special case for `human` on that axis — and there must
  not be one until the screencast becomes a switch this resolver owns, at which point `stream` and
  `human` acquire it TOGETHER, in one line.

**A frame for a MODEL or for a GOLDEN must be CLEAN.** The overlay is lifted AROUND such a capture
rather than cancelled for the run: a person always sees the cursor, while the picture a model reads
or a baseline is compared against contains nothing we drew. Otherwise the decoration would enter the
input a decision is made from — and "the UI changed" would become indistinguishable from "we drew a
cursor on it".

`human` is still refused for a golden run (`baseline=True`), and refused at the door: the slowdown
and the overlay do not degrade the reference, they make it WRONG, and that only surfaces later, on
somebody else's replay. Capture the baseline with `frames`; watch a later replay with `human`.

### `record` — shipped (LIVE-RECORD, ADR-125), and `NOT_YET` is now empty

`record` left `NOT_YET` in the same change as `SENTINEL_RECORD`, exactly as `human` did before it.
**The refusal set is now empty: every mode the product declares, it performs.** An empty `NOT_YET` is
a state rather than a list somebody forgot to fill, and the gate in
`tests/test_observation_modes_offline.py` was rewritten FOR it deliberately: the floor "at least one
unbuilt mode exists" was replaced by the assertion "every declared mode PRODUCES a plan".

What the mode does:

* **`SENTINEL_RECORD`** = `1`/`0`, the set's fourth switch. Written ONLY by
  `brain/observe.py::apply()` from `plan.video`, read ONLY by `pw-executor` (`src/record.ts`).
* `recordVideo` is set on OUR `newContext`, sized to the determinism viewport: a recording of a
  differently sized window would show a layout the run never saw.
* The artifact is `runs/<id>/video.webm`, fetched through the same route as everything else
  (`GET /v1/runs/{id}/artifact?name=video.webm`) and played in the hub's artifact panel.

**The recording carries the CURSOR** — Alex's requirement of 2026-08-02: without one it is as
unreadable as a bare screencast. So `record` draws into the page, and drawing means `slowMo` and an
overlay, i.e. ALL of `human`'s consequences: **timing is changed, and such a run cannot be a golden**.
Both facts are derived from one tuple, `observe.DECORATED`, so they cannot be set apart; the golden
refusal walks the same tuple and picked `record` up with no separate line. The frame a model and a
golden read stays CLEAN — the "lift the overlay around the capture and restore it" mechanism was built
by ADR-120 and is reused as-is.

⚠ **Over CDP-attach the mode is IMPOSSIBLE, and that is a refusal, not a degradation.** `recordVideo`
is a property of a context at the moment it is CREATED, and in CDP-attach the context is adopted.
`slowMo` has the same shape of problem and DEGRADES: the executor pays the pacing with its own pause.
Video has nothing to pay with — the run would simply end with no file, and a run that did not do the
one thing it was asked for is not "slightly worse". So the combination is declined BEFORE the start
(`exit 3`, `fatal.observe_refused`, and the reason names CDP), while the executor carries a second,
louder `videoUnavailable` guard in case the switch arrives by another route. A blank `PW_CDP_ENDPOINT`
is NOT an attachment: that is how compose spells "not configured", and refusing on it would be a
refusal nobody can act on.

⚠ **The decision to write is taken AT THE START — and that is a price, not a detail.** ADR-084 decides
about the trace at the END, so a green run's bytes never touch the disk. Not possible here: the file
is written as the run goes, and only a deletion afterwards remains. Per Alex's decision the recording
is **dropped on green**, and `SENTINEL_VIDEO_ALWAYS=1` keeps it always. The discard is ANNOUNCED along
with the name of that lever: otherwise a person who asked to record a passing run reads the absence as
the mode being broken.

`collect-live-run.sh` does **not** collect the video and has no `--with-video` counterpart. Same reason
as the trace: pixels are not redactable. The structural and textual cleanup can blank a `value` in a
step and mask a token in a log line, and can do nothing at all about a recording in which a filled
login form was on screen for as long as it was on screen.

### A run performed FOR A PERSON is marked where its result is read

A mode's price is printed beside the choice — but the choice is made once, and the result is read a
week later. A run performed in `human` carries the mark where its outcome is read: on the verdict card
right after the run, and as a pinned line in the Logs view — "⚠ This run was drawn for a PERSON,
observation mode `human`; the cursor, the slowdown and the highlight CHANGE TIMING: it says nothing
trustworthy about response times, races or timeouts, and it cannot serve as a golden".

The mark has ONE source: the `run.observation` event the brain prints before the run starts, carrying
both the mode and `decorations`. Live it arrives as a log line; afterwards it sits in
`runs/control-<id>/logs/run.jsonl` and comes back from `GET /v1/runs/{id}/logs?code=run.observation`,
so the answer to "what was this drawn with" outlives both the tab and a control-API restart. There is
deliberately no second field, no second artifact and no separate flag: two records of one fact drift,
and then neither can be trusted. The values are recovered from the line with the CATALOGUE's own
template — the same mechanism that lets the Logs view show Russian with real values — rather than
with a private regex that would drift from the catalogue the first time the event's text is edited.

### `PW_NO_TRACE` is not a mode

`PW_NO_TRACE` is not in the enum and is never written by the observation resolver. It is a
fail-closed SECRET guard with two independent enforcement points: `pw-executor/src/server.ts:610`
throws when a secret is entered while tracing is active, and `brain/__main__.py` exits 3 if a
secret would reach the trace (`fatal.secret_would_leak_to_trace`). An `off` that cleared it would
silently remove that guard; a mode that set it would break every login-as-test run. Both directions
are wrong, so `PW_NO_TRACE` stays out of the observation choice and is not shown beside it in the
interface.

### The VLM layer: a consequence, not a mode

A model able to read frames (vision-heal) does not decide whether frames are captured — that is the
person's call. But when `heal_llm` and vision are configured (`LLM_VISION`/`LLM_VISION_HEAL`) while
capture is off (`observe=off`), the resolver appends a line to `why`: "vision heal is configured but
observe=off, so it will receive no frame and cannot look at anything". Without that line, a heal
with nothing to look at is indistinguishable from a heal that silently never ran — exactly the class
of silence LIVE-MATRIX exists to remove.

### Verified

The gate `tests/test_observation_modes_offline.py` holds: both frame gates are written TOGETHER and
only by the resolver; `PW_NO_TRACE` is never written by this file, and the guard is alive on both
sides; the control-api schema, the CLI flag and the resolver's own set agree BOTH on the enumeration
and on the "not in this build" list (otherwise the hub would promise the opposite of what a run does);
a refusal resolves BEFORE the executor is spawned; decoration is true ONLY for `human`; and the switch
list is DERIVED — what `apply()` writes is what `overrides()` reports, with a floor on the count.

The gate `tests/test_live_human_mode_offline.py` (LIVE-HUMAN) checks what the resolver cannot say
about itself: it runs the SHIPPED entry point `python -m brain` against a stub executor that writes
its own environment to a file, then reads what the child RECEIVED. `observe=human` →
`SENTINEL_DECORATE=1` in the child; any other mode → `0`; `human` + golden → `exit 3` with the stub
never spawned AT ALL; and the mode and `decorations` are recovered from the `run.observation` line
with the catalogue template. Mutations: `decorations` hard-wired to `False`, the switch removed from
`SWITCHES`, `overrides()` hiding it, `human` returned to `NOT_YET`, the schema marking `human`
unavailable again, and the event dropping `decorations` — all six COMPILED, and all six went red.

The interface was verified live, in a headless Chromium against the shipped page: choosing `human`
prints "⚠ CHANGES TIMING …" in the form (screenshot taken and looked at), the option is no longer
labelled "not in this build", and the Logs view of a run with `decorations True` shows the pinned
mark — and does not show it for an ordinary run. Both checks were added to the DOM gate
`scripts/hub-dom-check.mjs`; two page mutations (the reader never sees decoration; the Logs view never
fills the pinned slot) went red.

---

---
