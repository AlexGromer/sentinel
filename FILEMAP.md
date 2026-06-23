# FILEMAP — agent_development (Sentinel)

<!-- Check this before Glob/Grep. Update on file create/delete/major refactor. -->

## Quick Reference — docs
| Path | Purpose | Key contents |
|------|---------|--------------|
| ARCHITECTURE.md | Canonical architecture + ADRs | Context, components, boundaries, 10 ADRs, constraints (§0 BUILD-ONLY), change log |
| GAPS.md | Open questions / VERIFY / risks | GAP-[CAT]-[NUM] tracking |
| BACKLOG.md | Task tracking | M0–M5 waves (via backlog-mcp); M0 done |
| docs/M0_CONTRACT.md | M0 frozen contract | env vars, JSON-RPC methods, acceptance gate |
| docs/STATE_MACHINE.md | LangGraph detail | 9 nodes, edges, RunState schema |
| docs/SELF_HEALING.md | Healing detail | 10-step algorithm, L1–L6 priors, confidence gate |
| docs/DETERMINISM.md | CI determinism | explore-once/replay-many, plan_hash, golden baselines, exit codes |
| docs/MEMORY_PERSISTENCE.md | Storage | short/long-term memory, 8 SQLite tables |
| docs/OBSERVABILITY.md | Telemetry | OTel, LLM transcript, token budget, Prometheus metrics |
| docs/OUTPUTS.md | Artifacts | the 10 emitted artifacts |
| docs/ROADMAP.md | Delivery plan | M0–M5 with Given/When/Then gates |
| docs/DESIGN_RECORD.md | Design provenance | 4 proposals + 3 judge verdicts + synthesis trail |
| memory/MEMORY.md | Project memory | stack, decisions, in-progress |
| memory/session_summary.md | Session narrative | design session + build-only pivot |

## Quick Reference — source (M0 live)
| Path | Lang | Purpose |
|------|------|---------|
| cmd/agentctl/main.go | Go | CLI; `run` spawns brain via subprocess+env, streams output, propagates exit code |
| go.mod | Go | module github.com/AlexGromer/sentinel (go 1.26) |
| brain/__main__.py | Python | M0 "perceive": drives pw-executor over JSON-RPC, prints a11y tree, ensures trace.zip |
| brain/__init__.py | Python | package marker |
| pw-executor/src/server.ts | TypeScript | OUR Playwright server (navigate/snapshot/traceStop) over newline JSON-RPC/stdio (ADR-001 BUILD) |
| pw-executor/package.json, tsconfig.json | TS | deps (playwright) + build config (tsc → dist/) |
| testdata/m0.html | fixture | local file:// page for M0 smoke test (no network) |

## Directory Structure
```
agent_development/
├── ARCHITECTURE.md GAPS.md BACKLOG.md FILEMAP.md
├── docs/         # 9 architecture deep-dives + design record + M0 contract
├── memory/       # project memory + session summaries
├── cmd/agentctl/ # Go control-plane CLI (M0 live)
├── brain/        # Python agent layer (M0: perceive; M1: LangGraph)
├── pw-executor/  # TS Playwright server (M0 live) — node_modules/ dist/ git-ignored
├── testdata/     # test fixtures
├── runs/         # run artifacts (git-ignored)
├── bin/          # Go build output (git-ignored)
├── .claude/      # project-local config (git-ignored)
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
agentctl → brain (subprocess+env) → pw-executor (subprocess, JSON-RPC/stdio) → Chromium
[M2+] brain/orchestrator → store-gateway (gRPC) → SQLite (main, sole writer)
[M1]  brain → LangGraph checkpointer → SQLite (SEPARATE file)
```

## Build / run (M0)
- `cd pw-executor && npm install && npm run build`  (one-time: `npx playwright install chromium-headless-shell`)
- `go build -o bin/agentctl ./cmd/agentctl`
- `./bin/agentctl run --target "file:///.../testdata/m0.html"`

## Metadata
- Last updated: 2026-06-23
- Phase: **M0 (Hello Browser) done — gate green**. Next: M1 (LangGraph autonomous walk).
