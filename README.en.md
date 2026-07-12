# Sentinel

> 🌐 [Русский](README.md) (основная версия) · **English**

**Autonomous, self-healing UI-testing agent.** Sentinel explores a web app on its own,
decides what to test, freezes a deterministic & replayable test plan, and repairs broken
locators when the DOM drifts — emitting engineer-consumable artifacts (reports, traces,
exported Playwright specs, regression baselines).

It is the differentiator over a plain test-writer: Sentinel **discovers and maintains**
tests rather than only writing them.

## Language

Russian is the primary, authoritative documentation; English copies live in `*.en.md` files.

## Status
| Milestone | State |
|-----------|-------|
| **M0 — Hello Browser** | ✅ done — Go→Python→TS wire produces a11y tree + `trace.zip` |
| **M1 — Autonomous Walk** | ✅ done — LangGraph StateGraph, coverage-converged explore, `plan.json` + `plan_hash` |
| **M2 + M2b — Self-Healing + Service Layer** | ✅ done — heal engine (L1–L6 + LLM); Go store-gateway (gRPC) + MCP-SDK transport |
| **M3 — CI-Ready Replay** | ✅ done — trust layer, exit codes 0/1/2/3, golden baselines, flake quarantine |
| **M4 + M4b — Reports + Observability** | ✅ done — HTML/JSON/Prometheus reports, `.spec.ts` export; brain OTel + Pushgateway |
| **M5 — Deploy + Visual Heal** | ✅ done — Dockerfile + Helm CronJob + ArgoCD; set-of-marks Tier-7 (gated) |
| **M6 — Provider-Agnostic Brain** | ✅ done — planner/heal on any provider (Anthropic / OpenAI-compat), ADR-019 |
| **M7 — MCP-Server Exposure** | ✅ done — brain as an MCP server (FastMCP) + `SamplingBackend` (host supplies the model), ADR-020 |
| **M8 — Distributed Observability + Budget Ceiling** | ✅ done — W3C tracing across Go/Python/TS + Go orchestrator (budget ceiling, SIGTERM) + report-service, ADR-021 |
| **M9 — Conversational & Goal-Directed Testing** | 📝 design frozen (Proposed, ADR-022..025) — see [`docs/M9_CONTRACT.md`](docs/M9_CONTRACT.md) |
| **M9.1 — Form/Login/Validation primitives** | ✅ done (offline) — pw-executor `fill`/`type`/`press`/`select` + storageState auth (login-as-test) + assert/negative layer, ADR-026 |
| **M9.2a — GoalPlanner (NL→plan)** | ✅ done (offline) — a goal-directed grounded planner (explore-first, never hallucinates selectors) + `--goal` auto-mode + a minimal RunConfig YAML, ADR-027 |
| **M9.2b — Two-phase + describe-first** | ✅ done (offline) — full explore→site map→one-shot scenario from a goal/description (cross-page, grounded in real elements); `--describe` + a rich RunConfig (auth/scenarios), ADR-028 |
| **M9.3 — Control-API (non-MCP)** | ✅ done (Wave B) — Go `cmd/control-api` (localhost-bind + bearer-token + CORS); chat front (`docs/chat/`) + CI templates (`docs/ci-templates/`) — ✅ (M9.3-tail, GAP-M9-03 closed), ADR-023/032/040 |
| **M9.4 + M9.5 — Tabs + backend correlation** | ✅ done (offline, Wave A) — in-app tabs (`[role=tab]`) + browser tabs (multi-page) + `traceparent` injection into requests, ADR-022/024 |
| **M9.6 — Browser modes** | ✅ done (offline, Wave D) — headed + CDP-attach (env toggle `PW_HEADLESS=0` / `PW_CDP_ENDPOINT`); **Chromium-only by design**, ADR-036/037 |
| **M9.7 — Pluggable adapters** | 🔶 partial — model/backend via the LiteLLM router (ADR-045); auth/deploy adapters remain (GAP-M9-08) |
| **M9.8-R3 — Co-pilot takeover (brain-side)** | ✅ done — takeover/return over RunControl gRPC (`interrupt()`/`Command(resume)`, abort>takeover), ADR-054; MV3 extension → @0xCoDSnet |
| **M9.9 — Replay-in-UI (R1)** | ✅ done — ▶/🔁/📌 run/replay/baseline + verdict across the vanilla consoles (`mode=replay\|baseline`, `from_run`), ADR-047 |
| **M9.10 — Multi-turn authoring (R2)** | ✅ done — multi-turn dialogue (`conversation_id` → checkpointer resume, `messages` channel), ADR-048 |
| **M11.x — Distribution/Pages** | ✅ partial — docker-compose · Helm/Flux + Secret plumbing (M11.3) · GitHub Pages hub + calculators (M11.6/b). Details — [`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md) |
| **M12 — OpenAI-compat shim + unified console** | ✅ done — `POST /v1/chat/completions` (1 turn→1 run) + unified `docs/index.html` (#connect/#build/#chat), ADR-041 |
| **M13 — Persistence / 5-domain store-gateway** | ✅ done — store-gateway across 5 domains (SQLite-first), `runs`+`chats` persisted; `scenarios`/`tests`/`results`/`metrics` = schema+RPC (wiring → M14/M15), ADR-049/050 |

Milestone details: [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Architecture at a glance (polyglot — each language where it is strongest)
```
agentctl (Go)  ── spawn + env ──▶  brain (Python, LangGraph)  ── JSON-RPC/stdio ──▶  pw-executor (TS, Playwright)
control-plane / CLI                perceive→plan→act→verify→heal               our own browser server  ── Chromium
```
- **Go** — control-plane spine: CLI, run lifecycle, (M2+) orchestrator, store-gateway, reports.
- **Python** — the brain: LangGraph state machine + planning/healing logic.
- **TypeScript** — `pw-executor`: our own Playwright server (we **build**, never adopt a turnkey product — see ADR-001).

Full design: [`ARCHITECTURE.md`](ARCHITECTURE.md) (62 ADRs) · deep-dives in [`docs/`](docs/) · design provenance in [`docs/DESIGN_RECORD.md`](docs/DESIGN_RECORD.md).

> **Browser modes (M9.6):** own-headless by default; `PW_HEADLESS=0` — headed (visible), `PW_CDP_ENDPOINT` — CDP-attach to the user's existing Chrome. The engine is **Chromium-only by design** (ADR-036); deterministic golden replay is headless-only (see [`docs/DETERMINISM.md`](docs/DETERMINISM.md)).

## Quickstart (M0)
```bash
# 1. build the TS browser server
cd pw-executor && npm install && npm run build && npx playwright install chromium-headless-shell && cd ..
# 2. build the Go CLI
go build -o bin/agentctl ./cmd/agentctl
# 3. run against a local fixture (no network)
./bin/agentctl run --target "file://$PWD/testdata/m0.html"
# → prints the accessibility tree and writes runs/<id>/trace.zip
```

## Quickstart via Docker (one command)
```bash
docker compose build
# zero-dependency demo: heuristic planner + bundled file:// fixture, no network, no API key
docker compose --profile demo up
# …or against your own target (goal mode; needs a key or a local model):
docker compose run --rm sentinel run --target "https://your-app.example" --goal "log in and open billing"
```
**setup-WebUI locally (air-gapped, part of the bundle):** `docker compose --profile webui up` → open
`http://localhost:8088/setup/` (and `/calculators/`) — the config generator + calculators run in your browser, no network.

**Local model** (no cloud): uncomment the `LLM_*` block in [`docker-compose.yml`](docker-compose.yml) and
start an endpoint — `docker compose --profile ollama up -d ollama` (or the multi-provider LiteLLM router — `docker compose --profile litellm up -d litellm`, see [`docs/ADAPTERS.md`](docs/ADAPTERS.md)). Model/hardware sizing lives in
[`docs/LOCAL_MODELS.md`](docs/LOCAL_MODELS.md) and the interactive calculators on
[GitHub Pages](https://alexgromer.github.io/sentinel/). Full run & verification guide:
[`docs/TESTING.md`](docs/TESTING.md).

## Documentation
| Doc | About |
|-----|-------|
| [`docs/TESTING.md`](docs/TESTING.md) | offline gates, local models, live run, zero-level docker-compose |
| [`docs/LOCAL_MODELS.md`](docs/LOCAL_MODELS.md) | VRAM methodology + token-cost methodology + verified model & runtime catalog |
| [`docs/ADAPTERS.md`](docs/ADAPTERS.md) | pluggable adapters: optional LiteLLM router (behind `LLM_BASE_URL`) + MCP-Inspector M7 debugging |
| [`docs/COPILOT.md`](docs/COPILOT.md) | co-pilot: vision · status (honest feature inventory) · agreements · wave roadmap [me]/[@0xCoDSnet] |
| [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) | STRIDE-lite over the trust boundaries (→ [`SECURITY.md`](SECURITY.md)) |
| [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) | contributor guide: build, milestone gates, extension recipes, Secret plumbing |
| [`docs/DETERMINISM.md`](docs/DETERMINISM.md) | determinism, plan_hash, golden baselines, the headless-only boundary |
| [`docs/DISTRIBUTION.md`](docs/DISTRIBUTION.md) | distribution/onboarding epic: Release · compose · Helm/Flux · setup-WebUI · air-gapped |
| [GitHub Pages](https://alexgromer.github.io/sentinel/) | docs hub + 3 calculators (VRAM · token-cost · model-selector) |

## Project map
| Path | What |
|------|------|
| `ARCHITECTURE.md`, `GAPS.md`, `BACKLOG.md`, `FILEMAP.md` | canonical design, open questions, tasks, file index |
| `docs/` | per-area specs + milestone contracts (`M*_CONTRACT.md`) + design record |
| `cmd/agentctl/` | Go CLI |
| `brain/` | Python LangGraph brain |
| `pw-executor/` | TypeScript Playwright server |
| `testdata/` | test fixtures |

## Contributing / extending
Read **[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)** — toolchain setup, per-component build, how to run
the milestone gates, and step-by-step recipes for extending (add a pw-executor tool, add a planner,
add a LangGraph node). **Docs-first:** every milestone has a contract in `docs/` written before code;
all code carries docstrings; no undocumented modules.

## License
[Apache-2.0](LICENSE) (+ [`NOTICE`](NOTICE)). Contributing: [`CONTRIBUTING.md`](CONTRIBUTING.md) · security: [`SECURITY.md`](SECURITY.md) · conduct: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). The `main` branch is protected (PR + review + green CI).
