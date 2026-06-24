# FILEMAP — agent_development (Sentinel)

<!-- Check this before Glob/Grep. Update on file create/delete/major refactor. -->

## Quick Reference — docs
| Path | Purpose | Key contents |
|------|---------|--------------|
| README.md | Project overview + quickstart | what/why, status, architecture, build/run |
| ARCHITECTURE.md | Canonical architecture + ADRs | context, components, boundaries, 16 ADRs, §0 BUILD-ONLY, change log |
| GAPS.md | Open questions / VERIFY / risks | GAP-[CAT]-[NUM] tracking |
| BACKLOG.md | Task tracking | M0–M4 + M2b-1 done; M2b-2 + M4b + M5 pending |
| docs/DEVELOPMENT.md | Contributor guide | setup, build/run, milestone gates, extension recipes |
| docs/M0..M4_CONTRACT.md, M2b_CONTRACT.md | Frozen milestone contracts | per-milestone scope/wire/gate |
| docs/STATE_MACHINE / SELF_HEALING / DETERMINISM / MEMORY_PERSISTENCE / OBSERVABILITY / OUTPUTS .md | mechanics deep-dives | reference |
| docs/ROADMAP.md, DESIGN_RECORD.md | delivery plan / design provenance | M0–M5 gates / 4 proposals + 3 verdicts |
| memory/MEMORY.md, memory/session_summary.md | Project memory + session narrative | stack, decisions, in-progress |

## Quick Reference — source
| Path | Lang | Purpose |
|------|------|---------|
| cmd/agentctl/main.go | Go | CLI subcommands: run / baseline update / locators clear-quarantine / export-spec / report / calibrate; spawns store-gateway (`runWithStore`); exit 0/1/2/3 |
| cmd/store-gateway/main.go | Go | M2b-1: gRPC PersistenceService over a Unix socket (agentctl-spawned) |
| internal/store/server.go | Go | SQLite-backed PersistenceService (sole writer, ADR-007/015); WAL checkpoint on close |
| internal/store/server_test.go | Go | gateway unit tests (golden/locator/quarantine round-trips) |
| internal/store/pb/ | Go | generated gRPC stubs (from proto/persistence.proto) |
| proto/persistence.proto | proto3 | PersistenceService contract (mirrors store.py 1:1) |
| go.mod, go.sum | Go | module + deps (grpc, protobuf, modernc.org/sqlite) |
| brain/__main__.py | Python | entrypoint; dispatch explore/replay/baseline/clear-quarantine/export-spec/report/calibrate; `make_store` |
| brain/graph.py | Python | LangGraph StateGraph (9 nodes); explore captures L1–L6 alternatives |
| brain/planner.py | Python | HeuristicPlanner (default) + LLMPlanner (Opus 4.8, ADR-011) |
| brain/healing.py | Python | HealingEngine (cache→L1–L6→verify→gate→audit) — store-agnostic |
| brain/replay.py | Python | replay + M3 trust layer (plan_hash, golden-diff, quarantine, exit codes) — store-agnostic |
| brain/store.py | Python | LocalStore (SQLite, tests/fallback) + GrpcStore (gRPC client, prod) + `make_store` (ADR-015) |
| brain/exporter.py / report.py / calibrate.py | Python | M4 generators (.spec.ts / HTML+JSON+Prom / heal histogram) |
| brain/state.py, brain/executor.py | Python | RunState + hashing helpers; pw-executor JSON-RPC client |
| brain/pb/ | Python | generated gRPC stubs (PersistenceService) |
| brain/pyproject.toml | Python | deps: langgraph, langgraph-checkpoint-sqlite, anthropic, grpcio, grpcio-tools |
| pw-executor/src/server.ts | TS | OUR Playwright server: navigate/snapshot/click/links/currentUrl/probe/interactives/screenshotHash/traceStop |
| tests/test_m3_offline.py / test_m4_offline.py | Python | offline trust/heal + M4 generator tests (fake executor; 5 + 3) |
| .github/workflows/ci.yml | CI | build → replay matrix |
| testdata/m0.html · site/*.html · site-v2/*.html | fixtures | M0 page · M1 clean · M2/M3 drifted |

| docs/M5_CONTRACT.md | Documentation | — |
| Dockerfile | Container definition | — |
| .dockerignore | Project file | — |
| deploy/sentinel/Chart.yaml | Configuration | — |
| deploy/sentinel/values.yaml | Configuration | — |
| deploy/sentinel/values-dev.yaml | Configuration | — |
| deploy/sentinel/values-staging.yaml | Configuration | — |
| deploy/sentinel/values-prod.yaml | Configuration | — |
| deploy/sentinel/templates/_helpers.tpl | Project file | — |
| deploy/sentinel/templates/cronjob.yaml | Configuration | — |
| deploy/sentinel/templates/configmap.yaml | Configuration | — |
| deploy/sentinel/templates/serviceaccount.yaml | Configuration | — |
| deploy/sentinel/templates/pvc.yaml | Configuration | — |
| deploy/sentinel/.helmignore | Project file | — |
| deploy/argocd/sentinel-app.yaml | Configuration | — |
| tests/test_m5_offline.py | Tests | — |
| docs/M4b_CONTRACT.md | Documentation | — |
| brain/otel.py | Python source | — |
| tests/test_m4b_offline.py | Tests | — |
## Directory Structure
```
agent_development/
├── README.md ARCHITECTURE.md GAPS.md BACKLOG.md FILEMAP.md
├── docs/        memory/        testdata/       tests/        .github/workflows/
├── cmd/agentctl/   cmd/store-gateway/        # Go binaries
├── internal/store/  internal/store/pb/       # Go store-gateway + gRPC stubs
├── proto/                                    # protobuf3 contract
├── brain/  brain/pb/                         # Python brain + gRPC stubs
├── pw-executor/                              # TS Playwright server (node_modules/ dist/ ignored)
└── runs/ state/ bin/ .venv/ .claude/         # all git-ignored
```

## Module Dependency Map
```
agentctl ──spawn──▶ store-gateway (Go, gRPC/UDS) ◀──gRPC── brain.store.GrpcStore   [M2b-1]
agentctl ──spawn+env──▶ brain (.venv) ──JSON-RPC/stdio──▶ pw-executor ──▶ Chromium
explore:  brain.graph (LangGraph) → SqliteSaver → runs/<id>/checkpoint.db
replay:   brain.replay (trust) → brain.healing → store (GrpcStore | LocalStore fallback)
M4:       brain.exporter / report / calibrate (pure generators)
[M2b-2] brain↔pw-executor transport → MCP SDK (pending)
```

## Build / run
- gateway-aware: `go build -o bin/agentctl ./cmd/agentctl && go build -o bin/store-gateway ./cmd/store-gateway` (if /tmp full: `go env -w GOTMPDIR=/opt/go/tmp`)
- TS: `cd pw-executor && npm install && npm run build` (`npx playwright install chromium-headless-shell`)
- Py: `uv venv && uv pip install langgraph langgraph-checkpoint-sqlite anthropic grpcio grpcio-tools`
- gRPC stubs (regen): `.venv/bin/python -m grpc_tools.protoc -I proto --python_out=brain/pb --grpc_python_out=brain/pb proto/persistence.proto` (+ go plugins for internal/store/pb)
- tests: `go test ./internal/store/ && .venv/bin/python tests/test_m3_offline.py && .venv/bin/python tests/test_m4_offline.py`
- full contributor guide: docs/DEVELOPMENT.md

## Metadata
- Last updated: 2026-06-24
- Phase: **M0–M4 + M2b-1 done — gates green**. Next: M2b-2 (MCP transport), then M4b / M5.
