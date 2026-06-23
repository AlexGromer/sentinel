# FILEMAP — agent_development (Sentinel)

<!-- Check this before Glob/Grep. Update on file create/delete/major refactor. -->

## Quick Reference — docs
| Path | Purpose | Key contents |
|------|---------|--------------|
| README.md | Project overview + quickstart | what/why, status, architecture, build/run |
| ARCHITECTURE.md | Canonical architecture + ADRs | context, components, boundaries, 13 ADRs, §0 BUILD-ONLY, change log |
| GAPS.md | Open questions / VERIFY / risks | GAP-[CAT]-[NUM] tracking |
| BACKLOG.md | Task tracking | M0/M1/M2/M3 done; M2b + M4–M5 pending |
| docs/DEVELOPMENT.md | Contributor guide | setup, build/run, milestone gates, **extension recipes** (tool/planner/node/heal) |
| docs/M0_CONTRACT.md … M3_CONTRACT.md | Frozen milestone contracts | per-milestone scope, wire, algorithm, Given/When/Then gate |
| docs/STATE_MACHINE.md | LangGraph detail | 9 nodes, edges, RunState schema |
| docs/SELF_HEALING.md | Healing detail | 10-step algorithm, L1–L6 priors, confidence gate |
| docs/DETERMINISM.md | CI determinism | explore-once/replay-many, plan_hash, golden baselines, exit codes |
| docs/MEMORY_PERSISTENCE.md / OBSERVABILITY.md / OUTPUTS.md | storage / telemetry / artifacts | reference deep-dives |
| docs/ROADMAP.md | Delivery plan | M0–M5 with Given/When/Then gates |
| docs/DESIGN_RECORD.md | Design provenance | 4 proposals + 3 judge verdicts + synthesis trail |
| memory/MEMORY.md, memory/session_summary.md | Project memory + session narrative | stack, decisions, in-progress |

## Quick Reference — source
| Path | Lang | Purpose |
|------|------|---------|
| cmd/agentctl/main.go | Go | CLI subcommands: `run` (explore/replay) · `baseline update` · `locators clear-quarantine`; propagates exit 0/1/2/3 |
| go.mod | Go | module github.com/AlexGromer/sentinel (go 1.26) |
| brain/__main__.py | Python | entrypoint; dispatch explore / replay / baseline / clear-quarantine; exit-code mapping |
| brain/graph.py | Python | LangGraph StateGraph (9 nodes); explore captures L1–L6 alternatives per element |
| brain/planner.py | Python | Planner protocol + HeuristicPlanner (default) + LLMPlanner (Opus 4.8, ADR-011) |
| brain/healing.py | Python | HealingEngine: cache→L1–L6→verify-before-accept→confidence gate→audit (ADR-008/012) |
| brain/replay.py | Python | replay runner + M3 trust layer (plan_hash abort, golden-diff, quarantine, exit codes) |
| brain/store.py | Python | interim SQLite: healed_locators, healing_audit, golden_snapshots, step_failures (→ M2b) |
| brain/state.py | Python | RunState TypedDict + normalize_url + semantic_id + canonical_plan_hash |
| brain/executor.py | Python | JSON-RPC client over the pw-executor subprocess (stdio) |
| brain/pyproject.toml | Python | deps: langgraph, langgraph-checkpoint-sqlite, anthropic (uv) |
| pw-executor/src/server.ts | TS | OUR Playwright server: navigate/snapshot/click/links/currentUrl/probe/interactives/screenshotHash/traceStop (ADR-001) |
| pw-executor/package.json, tsconfig.json | TS | deps (playwright) + build (tsc → dist/) |
| tests/test_m3_offline.py | Python | offline trust-layer + heal tests (fake executor): plan_hash, golden, quarantine, heal |
| .github/workflows/ci.yml | CI | build → replay matrix (site→0 / site-v2→2) + explore on dispatch |
| testdata/m0.html · site/*.html · site-v2/*.html | fixtures | M0 single page · M1 clean multi-page · M2/M3 drifted |

## Directory Structure
```
agent_development/
├── README.md ARCHITECTURE.md GAPS.md BACKLOG.md FILEMAP.md
├── docs/         # contributor guide + deep-dives + design record + M0–M3 contracts
├── memory/       # project memory + session summaries
├── cmd/agentctl/ # Go control-plane CLI
├── brain/        # Python brain (state/executor/planner/graph/healing/replay/store/__main__)
├── pw-executor/  # TS Playwright server — node_modules/ dist/ git-ignored
├── tests/        # offline test suite
├── .github/workflows/   # CI
├── testdata/     # m0.html + site/ + site-v2/ fixtures
├── runs/ state/ bin/ .venv/ .claude/   # all git-ignored
└── .claude-ver .gitignore
```

## Source layout — planned (not yet created; see docs/ROADMAP.md)
```
internal/store/        # Go — store-gateway, sole SQLite writer (M2b — replaces brain/store.py)
internal/orchestrator/ # Go — run FSM, gRPC server, supervisor (M2b/M3+)
internal/report/       # Go — report-service (M4)
proto/                 # shared — protobuf3 contracts (M2b)
```

## Module Dependency Map
```
agentctl → brain (subprocess+env; .venv python) → pw-executor (subprocess, JSON-RPC/stdio) → Chromium
explore:  brain.graph (LangGraph) → SqliteSaver checkpointer → runs/<id>/checkpoint.db (separate)
replay:   brain.replay (trust layer) → brain.healing (HealingEngine) → brain.store (state/locators.db, interim)
[M2b] brain → Go store-gateway (gRPC) → SQLite (main, sole writer)  |  transport → MCP SDK
```

## Build / run
- TS:  `cd pw-executor && npm install && npm run build` (one-time `npx playwright install chromium-headless-shell`)
- Go:  `go build -o bin/agentctl ./cmd/agentctl`
- Py:  `uv venv && uv pip install langgraph langgraph-checkpoint-sqlite anthropic`
- test: `.venv/bin/python tests/test_m3_offline.py`
- M1 explore: `./bin/agentctl run --target "file://$PWD/testdata/site/index.html" --planner heuristic`
- M3 baseline+replay: `./bin/agentctl baseline update --plan runs/<id>/plan.json` then `./bin/agentctl run --replay --plan runs/<id>/plan.json --target <url> --ci --aut-version <sha>`
- Full contributor guide: `docs/DEVELOPMENT.md`

## Metadata
- Last updated: 2026-06-23
- Phase: **M0 + M1 + M2 + M3 done — gates green**. Next: M2b (Go store-gateway/gRPC + MCP-SDK) or M4.
