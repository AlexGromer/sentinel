# FILEMAP — agent_development (Sentinel)

<!-- Check this before Glob/Grep. Update on file create/delete/major refactor. -->

## Quick Reference — docs
| Path | Purpose | Key contents |
|------|---------|--------------|
| README.md | Project overview + quickstart | what/why, status, architecture, build/run |
| ARCHITECTURE.md | Canonical architecture + ADRs | context, components, boundaries, 11 ADRs, §0 BUILD-ONLY, change log |
| GAPS.md | Open questions / VERIFY / risks | GAP-[CAT]-[NUM] tracking |
| BACKLOG.md | Task tracking | M0–M5 waves (M0, M1 done) |
| docs/DEVELOPMENT.md | Contributor guide | setup, build/run, milestone gates, **extension recipes** |
| docs/M0_CONTRACT.md | M0 frozen contract | env vars, JSON-RPC methods, gate |
| docs/M1_CONTRACT.md | M1 frozen contract | RunState, 9 nodes/edges, planner, plan_hash, gate |
| docs/STATE_MACHINE.md | LangGraph detail | 9 nodes, edges, RunState schema |
| docs/SELF_HEALING.md | Healing detail (M2 target) | 10-step algorithm, L1–L6 priors, confidence gate |
| docs/DETERMINISM.md | CI determinism | explore-once/replay-many, plan_hash, golden baselines, exit codes |
| docs/MEMORY_PERSISTENCE.md | Storage | short/long-term memory, 8 SQLite tables |
| docs/OBSERVABILITY.md | Telemetry | OTel, LLM transcript, token budget, Prometheus metrics |
| docs/OUTPUTS.md | Artifacts | the 10 emitted artifacts |
| docs/ROADMAP.md | Delivery plan | M0–M5 with Given/When/Then gates |
| docs/DESIGN_RECORD.md | Design provenance | 4 proposals + 3 judge verdicts + synthesis trail |
| memory/MEMORY.md, memory/session_summary.md | Project memory + session narrative | stack, decisions, in-progress |

## Quick Reference — source
| Path | Lang | Purpose |
|------|------|---------|
| cmd/agentctl/main.go | Go | CLI; `run` spawns brain (venv python) via subprocess+env; flags --planner/--coverage-target/--max-steps |
| go.mod | Go | module github.com/AlexGromer/sentinel (go 1.26) |
| brain/__main__.py | Python | M1 entrypoint: wires executor+planner+graph, initial nav, runs explore loop, writes plan.json |
| brain/graph.py | Python | LangGraph StateGraph: 9 nodes (perceive/ground/plan/act/verify/heal-stub/checkpoint/report) + edges |
| brain/planner.py | Python | Planner protocol + HeuristicPlanner (default) + LLMPlanner (Opus 4.8, ADR-011) |
| brain/state.py | Python | RunState TypedDict + normalize_url + semantic_id + canonical_plan_hash |
| brain/executor.py | Python | JSON-RPC client over the pw-executor subprocess (stdio) |
| brain/pyproject.toml | Python | deps: langgraph, langgraph-checkpoint-sqlite, anthropic (uv) |
| pw-executor/src/server.ts | TS | OUR Playwright server: navigate/snapshot/click/links/currentUrl/traceStop over JSON-RPC/stdio (ADR-001) |
| pw-executor/package.json, tsconfig.json | TS | deps (playwright) + build config (tsc → dist/) |
| testdata/m0.html | fixture | M0 single-page file:// fixture |
| testdata/site/*.html | fixture | M1 multi-page fixture (index, page-a/b/c) |

## Directory Structure
```
agent_development/
├── README.md ARCHITECTURE.md GAPS.md BACKLOG.md FILEMAP.md
├── docs/         # contributor guide + 9 deep-dives + design record + M0/M1 contracts
├── memory/       # project memory + session summaries
├── cmd/agentctl/ # Go control-plane CLI
├── brain/        # Python LangGraph brain (state/executor/planner/graph/__main__)
├── pw-executor/  # TS Playwright server — node_modules/ dist/ git-ignored
├── testdata/     # m0.html + site/ fixtures
├── runs/ bin/ .venv/ .claude/   # all git-ignored
└── .claude-ver .gitignore
```

## Source layout — planned (not yet created; see docs/ROADMAP.md)
```
internal/orchestrator/ # Go — run FSM, gRPC server, supervisor, budget ceiling (M2+)
internal/store/        # Go — store-gateway, sole SQLite writer (M2)
internal/report/       # Go — report-service (M4)
proto/                 # shared — protobuf3 contracts (M2)
```

## Module Dependency Map
```
agentctl → brain (subprocess+env; .venv python) → pw-executor (subprocess, JSON-RPC/stdio) → Chromium
brain → LangGraph StateGraph → SqliteSaver checkpointer → runs/<id>/checkpoint.db (SEPARATE file)
[M2+] brain/orchestrator → store-gateway (gRPC) → SQLite (main, sole writer)
```

## Build / run
- TS:  `cd pw-executor && npm install && npm run build` (one-time `npx playwright install chromium-headless-shell`)
- Go:  `go build -o bin/agentctl ./cmd/agentctl`
- Py:  `uv venv && uv pip install langgraph langgraph-checkpoint-sqlite anthropic`
- M0:  `./bin/agentctl run --target "file://$PWD/testdata/m0.html"`
- M1:  `./bin/agentctl run --target "file://$PWD/testdata/site/index.html" --planner heuristic`
- Full contributor guide: `docs/DEVELOPMENT.md`

## Metadata
- Last updated: 2026-06-23
- Phase: **M0 + M1 done — gates green**. Next: M2 (self-repairing walker + gRPC/store + MCP-SDK).
