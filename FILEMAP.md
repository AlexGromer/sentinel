# FILEMAP — agent_development (Sentinel)

<!-- Check this before Glob/Grep. Update on file create/delete/major refactor. -->

## Quick Reference — docs
| Path | Purpose | Key contents |
|------|---------|--------------|
| README.md | Project overview + quickstart | what/why, status, architecture, build/run |
| ARCHITECTURE.md | Canonical architecture + ADRs | context, components, boundaries, 14 ADRs, §0 BUILD-ONLY, change log |
| GAPS.md | Open questions / VERIFY / risks | GAP-[CAT]-[NUM] tracking |
| BACKLOG.md | Task tracking | M0–M4 done; M2b + M4b + M5 pending |
| docs/DEVELOPMENT.md | Contributor guide | setup, build/run, milestone gates, extension recipes |
| docs/M0..M4_CONTRACT.md | Frozen milestone contracts | per-milestone scope, wire, algorithm, Given/When/Then gate |
| docs/STATE_MACHINE.md / SELF_HEALING.md / DETERMINISM.md | mechanics deep-dives | LangGraph / heal / replay-trust |
| docs/MEMORY_PERSISTENCE.md / OBSERVABILITY.md / OUTPUTS.md | storage / telemetry / artifacts | reference |
| docs/ROADMAP.md / DESIGN_RECORD.md | delivery plan / design provenance | M0–M5 gates / 4 proposals + 3 verdicts |
| memory/MEMORY.md, memory/session_summary.md | Project memory + session narrative | stack, decisions, in-progress |

## Quick Reference — source
| Path | Lang | Purpose |
|------|------|---------|
| cmd/agentctl/main.go | Go | CLI subcommands: run · baseline update · locators clear-quarantine · export-spec · report · calibrate; exit 0/1/2/3 |
| go.mod | Go | module github.com/AlexGromer/sentinel (go 1.26) |
| brain/__main__.py | Python | entrypoint; dispatch explore/replay/baseline/clear-quarantine/export-spec/report/calibrate |
| brain/graph.py | Python | LangGraph StateGraph (9 nodes); explore captures L1–L6 alternatives |
| brain/planner.py | Python | HeuristicPlanner (default) + LLMPlanner (Opus 4.8, ADR-011) |
| brain/healing.py | Python | HealingEngine: cache→L1–L6→verify-before-accept→confidence gate→audit |
| brain/replay.py | Python | replay runner + M3 trust layer (plan_hash abort, golden-diff, quarantine, exit codes) |
| brain/store.py | Python | interim SQLite: healed_locators/healing_audit/golden_snapshots/step_failures (→ M2b) |
| brain/exporter.py | Python | M4: plan → Playwright `.spec.ts` (deterministic) |
| brain/report.py | Python | M4: heal-report → report.html + report.json + metrics.prom (Prometheus textfile) |
| brain/calibrate.py | Python | M4: healing_audit → confidence histogram + outcome counts |
| brain/state.py | Python | RunState + normalize_url + semantic_id + canonical_plan_hash |
| brain/executor.py | Python | JSON-RPC client over pw-executor subprocess (stdio) |
| brain/pyproject.toml | Python | deps: langgraph, langgraph-checkpoint-sqlite, anthropic (uv) |
| pw-executor/src/server.ts | TS | OUR Playwright server: navigate/snapshot/click/links/currentUrl/probe/interactives/screenshotHash/traceStop |
| pw-executor/package.json, tsconfig.json | TS | deps (playwright) + build (tsc → dist/) |
| tests/test_m3_offline.py | Python | trust-layer + heal tests (fake executor) — 5 |
| tests/test_m4_offline.py | Python | export/report/metrics/calibrate tests — 3 |
| .github/workflows/ci.yml | CI | build → replay matrix (site→0 / site-v2→2) + explore on dispatch |
| testdata/m0.html · site/*.html · site-v2/*.html | fixtures | M0 page · M1 clean · M2/M3 drifted |

## Directory Structure
```
agent_development/
├── README.md ARCHITECTURE.md GAPS.md BACKLOG.md FILEMAP.md
├── docs/         # contributor guide + deep-dives + design record + M0–M4 contracts
├── memory/       # project memory + session summaries
├── cmd/agentctl/ # Go control-plane CLI
├── brain/        # Python brain (state/executor/planner/graph/healing/replay/store/exporter/report/calibrate/__main__)
├── pw-executor/  # TS Playwright server — node_modules/ dist/ git-ignored
├── tests/        # offline test suites (M3, M4)
├── .github/workflows/   # CI
├── testdata/     # m0.html + site/ + site-v2/ fixtures
├── runs/ state/ bin/ .venv/ .claude/   # all git-ignored
└── .claude-ver .gitignore
```

## Source layout — planned (not yet created; see docs/ROADMAP.md)
```
internal/store/        # Go — store-gateway, sole SQLite writer (M2b — replaces brain/store.py)
internal/orchestrator/ # Go — run FSM, gRPC server (M2b/later)
internal/report/       # Go — report-service (M4b)
proto/                 # shared — protobuf3 contracts (M2b)
```

## Module Dependency Map
```
agentctl → brain (subprocess+env; .venv python) → pw-executor (subprocess, JSON-RPC/stdio) → Chromium
explore:  brain.graph (LangGraph) → SqliteSaver → runs/<id>/checkpoint.db
replay:   brain.replay (trust) → brain.healing → brain.store (state/locators.db, interim)
M4:       brain.exporter / report / calibrate (pure generators over plan/heal-report/healing_audit)
[M2b] brain → Go store-gateway (gRPC) → SQLite (sole writer)  |  transport → MCP SDK
```

## Build / run
- TS:  `cd pw-executor && npm install && npm run build` (one-time `npx playwright install chromium-headless-shell`)
- Go:  `go build -o bin/agentctl ./cmd/agentctl`
- Py:  `uv venv && uv pip install langgraph langgraph-checkpoint-sqlite anthropic`
- tests: `.venv/bin/python tests/test_m3_offline.py && .venv/bin/python tests/test_m4_offline.py`
- explore→baseline→replay (M1/M3) and `export-spec` / `report --run <dir>` / `calibrate` (M4) — see docs/DEVELOPMENT.md

## Metadata
- Last updated: 2026-06-24
- Phase: **M0–M4 done — gates green / offline-verified**. Next: M2b (Go store-gateway/gRPC + MCP-SDK).
