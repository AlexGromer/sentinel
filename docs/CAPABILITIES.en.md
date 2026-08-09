# What this tool can do

> 🌐 [Русский](CAPABILITIES.md) (primary) · **English**

A catalogue of features that work but are hard to reach because nothing named them (the LiteLLM class: code present, cannot be found). The source of truth is [`capabilities.json`](capabilities.json); the gate `tests/test_capabilities_offline.py` verifies that every access path below actually resolves in the code, so this page cannot promise a feature the product does not have.

## Integrations

| Capability | How to reach it |
|---|---|
| **OpenAI-compatible endpoint ⭐** | POST /v1/chat/completions — point any OpenAI client (Open WebUI, SDK, curl) at the product. |

## CLI (`agentctl`)

| Capability | How to reach it |
|---|---|
| **Export to @playwright/test ⭐** | agentctl export-spec --plan <plan.json> — a frozen plan -> .spec.ts; the migration path to Playwright. |
| **Reports (html/json/junit/prom)** | agentctl report --run <run-dir> — the sole source of report.html, report.json, junit.xml, metrics.prom. |
| **Healing calibration** | agentctl calibrate — heal precision by strategy + identity verdicts. |
| **Purge foreign text from the DB** | agentctl purge-store --tables <..> --yes — explicit cleanup of accumulated foreign text (ADR-100). |
| **Redact a trace** | agentctl redact-trace --trace <trace.zip> — strip typed values and secrets from a trace (ADR-098). |

## HTTP API

| Capability | How to reach it |
|---|---|
| **Scenario library** | GET/DELETE /v1/scenarios — saved scenarios as reusable assets. |
| **Promote a scenario to a test** | POST /v1/tests/promote — turn a scenario into a named test. |
| **Metrics and trends** | GET /v1/results, GET /v1/trends — run outcomes and their trend, natively in the UI (ADR-051). |
| **Server-side log filtering** | GET /v1/runs/{id}/logs — filter by level/source/category/module/code. |
| **Config over the API + schema** | GET/PUT /v1/config, GET /v1/config-schema — service config and its schema (ADR-060/062). |

## Observability

| Capability | How to reach it |
|---|---|
| **Service journal ⭐** | GET /v1/service-log — what the tool itself did: sign-ins and failed sign-ins, accounts, configuration changes, refusals, services starting and stopping. An account sees its own events; an admin or the machine token sees the deployment's (HEALTH-005). |
| **The service journal in the browser** | A "Service journal" view beside Logs — the same renderer and the same message catalogue; a partial view says it is partial rather than looking complete. |
| **The service journal from a terminal** | agentctl service-log [--lvl warn --svc control-api --actor alice] — a thin client over the same route. |
| **Destroying journal records** | agentctl purge-service --yes [--older-than 720h] — counts, never content; nothing is swept automatically, and the purge records itself (service.log_purged). |
| **Aggregate Prometheus scrape across runs** | GET /metrics — an aggregate over the caller's own runs (the machine token / an accounts-less deployment sees every run), a run label on every series, # HELP/# TYPE per family, honest runs_included/runs_omitted; agentctl metrics is the same scrape from a terminal (ADR-119, replaces the removed cmd/report-service). |

## Runtime & modes

| Capability | How to reach it |
|---|---|
| **Attach to your own Chrome (CDP) ⭐** | PW_CDP_ENDPOINT=http://localhost:9222 — the tool drives YOUR already-open Chrome (ADR-037). |
| **Take over a live run by hand ⭐** | GET /v1/stream (WS) — take over a running run, drive by hand, hand control back. |
| **The brain as an MCP server ⭐** | RUN_MODE=mcp-server — run the brain as an MCP server (tools/list + sampling). |
| **Log in once (login-as-test) ⭐** | STORAGE_STATE / STORAGE_STATE_SAVE — log in once, later runs start authenticated; password via secretRef. |
| **Multi-turn chat authoring** | RUN_MODE=chat (agentctl run --mode chat --conversation-id <id>) — author a scenario by conversation. |
| **Tab control** | browser.tabs / switchTab — multi-tab and multi-page scenarios (M9.4). |
| **Download a run's trace** | GET /v1/runs/{id}/artifact?name=trace.zip — a failed run's trace is reachable without server access (ADR-099). |
| **Environment allowlist** | SENTINEL_ENV_ALLOWLIST — which host env reaches the brain; on by default (ADR-035). |
| **Token budgets** | PLAN_TOKEN_LIMIT / HEAL_TOKEN_LIMIT / TOTAL_TOKEN_LIMIT — spend ceilings; running out degrades gracefully, not fails (ADR-021). |
| **Disable trace screenshots** | SENTINEL_TRACE_SCREENSHOTS=0 — do not record frames (ADR-098 redacts text, not pixels). |
| **Ready fixtures to try now** | testdata/fixtures/l1..l7 — run against a built-in page with no network (file://). |

## Deployment

| Capability | How to reach it |
|---|---|
| **A working Helm chart ⭐** | deploy/sentinel/ — deploy to Kubernetes; templates, values and secrets are written. |
| **Install without a checkout ⭐** | install.sh — installs the agent by checksum + cosign, no repository clone. |
| **Air-gapped bundle with the whole UI ⭐** | scripts/build-airgap-bundle.sh — the bundle ships the whole browser UI, runs with no network. |
| **Windows as a client** | install.ps1 — installs `agentctl.exe` natively, no admin. A run also needs Python/uv + Node + browsers on the host, so the supported path is `agentctl` against a control-API in a container or on another machine (ADR-110). |
| **Local model (ollama)** | docker compose --profile ollama up -d ollama — a local OpenAI-compatible model endpoint. |
| **Model router (LiteLLM)** | docker compose --profile litellm up -d litellm — a router over many providers behind LLM_BASE_URL (ADR-045). |
| **Browser UI (webui)** | docker compose up — the hub and wizard are served locally on :8088, no flags. |
| **Persistent store** | docker compose up — the store-gateway comes up and is wired in; control-api already points at it (ADR-050). |

---

⭐ — the big features most often looked for and not found. The full machine-readable list with access paths is [`capabilities.json`](capabilities.json).
