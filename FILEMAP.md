# FILEMAP — agent_development (Sentinel)

<!-- Check this before Glob/Grep. Update on file create/delete/major refactor. -->

## Quick Reference

| Path | Purpose | Key contents |
|------|---------|--------------|
| ARCHITECTURE.md | Canonical architecture + ADRs | Context, components, boundaries, 10 ADRs, constraints (§0 BUILD-ONLY), change log |
| GAPS.md | Open questions / VERIFY / risks | GAP-[CAT]-[NUM] tracking |
| BACKLOG.md | Task tracking | M0–M5 waves (via backlog-mcp) |
| FILEMAP.md | This file | — |
| docs/STATE_MACHINE.md | LangGraph detail | 9 nodes, edges, RunState schema |
| docs/SELF_HEALING.md | Healing detail | 10-step algorithm, L1–L6 priors, confidence gate |
| docs/DETERMINISM.md | CI determinism | explore-once/replay-many, plan_hash, golden baselines, exit codes, e2e walkthrough |
| docs/MEMORY_PERSISTENCE.md | Storage | short/long-term memory, 8 SQLite tables, checkpoint GC |
| docs/OBSERVABILITY.md | Telemetry | OTel, LLM transcript, token budget, Prometheus metrics + alerts |
| docs/OUTPUTS.md | Artifacts | the 10 emitted artifacts |
| docs/ROADMAP.md | Delivery plan | M0–M5 with Given/When/Then gates (BUILD-only deltas) |
| docs/DESIGN_RECORD.md | Design provenance | 4 proposals + 3 judge verdicts + synthesis trail |
| memory/MEMORY.md | Project memory | stack, decisions, in-progress |
| memory/session_summary.md | Session narrative | design session + build-only pivot |
| .claude/settings.local.json | Project permissions (git-ignored) | allow/ask/deny |
| .claude-ver | Init version marker | config-version 9.1.0 |
| .gitignore | Ignore patterns | security-focused; `.claude/` ignored |

## Directory Structure
```
agent_development/
├── ARCHITECTURE.md        # canonical architecture + ADRs
├── GAPS.md                # open questions / risks
├── BACKLOG.md             # M0–M5 tasks
├── FILEMAP.md             # this file
├── docs/                  # 8 architecture deep-dives + design record
├── memory/                # project memory + session summaries
├── .claude/               # project-local Claude Code config (git-ignored)
├── .claude-ver
└── .gitignore
```

## Planned source layout (not yet created — see docs/ROADMAP.md)
```
cmd/agentctl/          # Go — CLI entrypoint
internal/orchestrator/ # Go — run FSM, gRPC server, supervisor, budget ceiling
internal/store/        # Go — store-gateway (sole SQLite writer)
internal/report/       # Go — report-service (M4)
brain/                 # Python — LangGraph StateGraph, perception, healing-engine
pw-executor/           # TypeScript — OUR OWN Playwright MCP server (BUILD, ADR-001)
proto/                 # shared — protobuf3 contracts (M2)
```

## Module Dependency Map
```
agentctl → orchestrator → brain (subprocess) → pw-executor (subprocess)
brain/orchestrator → store-gateway (gRPC, M2+) → SQLite (main, sole writer)
brain → LangGraph checkpointer → SQLite (SEPARATE file)
```

## File Categories
### Entry Points
- (planned) `cmd/agentctl/` (Go CLI), `brain/` (Python LangGraph entry), `pw-executor/` (TS server entry)

### Core Logic
- (planned) `internal/orchestrator/`, `brain/` nodes, `internal/store/`

### Configuration
- `.claude/settings.local.json` — project permissions
- `.claude-ver` — version marker

### Tests
- (planned) Go unit, Python unit, TS unit, proto/MCP contract tests, e2e against a fixture app

## Metadata
- Last updated: 2026-06-23
- Phase: architecture complete + documented; source not yet started (M0 next)
