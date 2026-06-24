# BACKLOG

## Active

- [ ] [W5] M5 Visual Heal PoC + K3s/ArgoCD: set-of-marks visual heal built into pw-executor, BUILT ONLY IF PoC measures >70% accuracy on 20 real broken-selector scenarios; Postgres + AsyncPostgresSaver IF concurrency trigger hit; Helm chart + ArgoCD Application for home-lab GitOps; per-namespace dev/staging/prod config. (P2) @k8s-engineer — 2026-06-22
- [ ] [W4] M4b: full observability — Go report-service (HTML/JSON + Prometheus HTTP /metrics endpoint), OpenTelemetry spans across all 3 layers (prompt_HASH not content) -> Tempo, and the Go-side hard budget ceiling. Deferred from M4 (ADR-014); needs the Go service layer (M2b). (P3) @observability-engineer — 2026-06-23
- [ ] [W2] M2b-2: migrate brain<->pw-executor transport to the MCP SDK (ADR-016, GAP-VERIFY-002). pw-executor -> @modelcontextprotocol/sdk McpServer (stdio) exposing the 7 tools; brain -> MCP stdio client wrapped behind Executor.call (graph/healing/replay unchanged); keep JSON-RPC as documented fallback. Verify SDK API before coding. (P2) @web-developer — 2026-06-23
- [ ] [W2] M2b-2: migrate brain<->pw-executor transport to the MCP SDK (ADR-016, GAP-VERIFY-002). pw-executor -> @modelcontextprotocol/sdk McpServer (StdioServerTransport) exposing the same 8 tools; brain -> MCP stdio client wrapped behind Executor.call so graph/healing/replay are unchanged; retain hand-rolled JSON-RPC as a documented fallback. VERIFY MCP SDK + python mcp pkg API before coding. (P2) @api-developer — 2026-06-23

## Completed Archive

- [x] [W2] M2b: extract Go store-gateway (sole SQLite writer, WAL) + gRPC + proto v1 (PersistenceService) to replace the interim brain-local store (ADR-012) and restore ADR-007 single-writer; migrate brain<->pw-executor transport to the official MCP SDK (GAP-VERIFY-002, @modelcontextprotocol/sdk server + python mcp client). (P1) @api-developer — 2026-06-22 ✓ 2026-06-23
- [x] [W4] M4 Production-Observable v1.0: report-service (JSON+HTML, Prometheus /metrics); OTel spans across all 3 layers (prompt_HASH not content) to Tempo; .spec.ts export generated from RunState (no codegen dependency); agentctl calibrate + healing_confidence_histogram; Go-side hard budget ceiling reconciliation. (P2) @observability-engineer — 2026-06-22 ✓ 2026-06-23
- [x] [W3] M3 CI-Ready Replay: replay/ci mode (skip plan node, LLM-free happy path); plan_hash HARD-ABORT (exit 3); dual a11y+screenshot golden baselines; AUT-SHA-gated flake quarantine; structured exit codes 0/1/2/3; orchestrator as proper gRPC server (RunControl, supervision, per-step deadline); per-job SQLite; GitHub Actions (explore conditional + replay matrix). (P1) @ci-cd-engineer — 2026-06-22 ✓ 2026-06-22
- [x] [W2] M2 Self-Repairing Walker: heal node (cache lookup + L1-L6 rotation + Sonnet a11y re-grounding + verify-before-accept probe + confidence gate + post-heal verification + append-only healing_audit + dom_subtree_hash amortization); introduce gRPC + proto v1 (PersistenceService) + Go store-gateway (SQLite WAL: runs, healed_locators, healing_audit). (P1) @ml-engineer — 2026-06-22 ✓ 2026-06-22
- [x] [W1] M1 Autonomous Walk: all 9 LangGraph nodes (heal stubbed); SqliteSaver checkpointer in a SEPARATE DB file; Opus 4.8 plan node (T=0); coverage-based convergence (coverage_target + nav_frontier, not an LLM flag); emit plan.json + plan_hash + llm-transcript.jsonl + trace. (P1) @ml-engineer — 2026-06-22 ✓ 2026-06-22
- [x] [W0] M0 Hello Browser: agentctl (Go) spawns Python brain via subprocess+env (no gRPC yet); single perceive node; BUILD minimal pw-executor (TS: navigate + accessibility_snapshot + trace start/stop); print a11y tree + drop trace.zip. Proves the wire, not intelligence. (P1) @general-purpose — 2026-06-22 ✓ 2026-06-22
- [x] GAP-DECISION-001 (BLOCKER): confirm BUILD-ONLY interpretation with user — are OSS libraries (Playwright lib, LangGraph, Anthropic SDK) allowed as "writing", or is even OSS off-limits (pure from-scratch incl. raw CDP)? Resolution gates the entire stack (ADR-002/004/005). (P1) @system-architect — 2026-06-22 ✓ 2026-06-22

## Deferred

_No deferred tasks._
