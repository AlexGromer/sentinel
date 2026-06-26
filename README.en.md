# Sentinel

> 🌐 [Русский](README.md) (основная версия) · **English**

**Autonomous, self-healing UI-testing agent.** Sentinel explores a web app on its own,
decides what to test, freezes a deterministic & replayable test plan, and repairs broken
locators when the DOM drifts — emitting engineer-consumable artifacts (reports, traces,
exported Playwright specs, regression baselines).

It is the differentiator over a plain test-writer: Sentinel **discovers and maintains**
tests rather than only writing them.

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
| **M7 — MCP-Server Exposure** | 📝 contract frozen (Proposed, ADR-020) — see [`docs/M7_CONTRACT.md`](docs/M7_CONTRACT.md) |

Milestone details: [`docs/ROADMAP.md`](docs/ROADMAP.md).

## Architecture at a glance (polyglot — each language where it is strongest)
```
agentctl (Go)  ── spawn + env ──▶  brain (Python, LangGraph)  ── JSON-RPC/stdio ──▶  pw-executor (TS, Playwright)
control-plane / CLI                perceive→plan→act→verify→heal               our own browser server  ── Chromium
```
- **Go** — control-plane spine: CLI, run lifecycle, (M2+) orchestrator, store-gateway, reports.
- **Python** — the brain: LangGraph state machine + planning/healing logic.
- **TypeScript** — `pw-executor`: our own Playwright server (we **build**, never adopt a turnkey product — see ADR-001).

Full design: [`ARCHITECTURE.md`](ARCHITECTURE.md) (20 ADRs) · deep-dives in [`docs/`](docs/) · design provenance in [`docs/DESIGN_RECORD.md`](docs/DESIGN_RECORD.md).

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
