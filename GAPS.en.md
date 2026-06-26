# GAPS — Sentinel

> 🌐 [Русский](GAPS.md) (основная версия) · **English**

Tracking of open questions, items to verify, and known risks. Format: `GAP-[CAT]-[NUM]`.
Categories: ARCH (architecture), VERIFY (needs fact-check), RISK, AGENT (missing tooling), DECISION (awaiting user).

> Source: design workflow synthesis (2026-06-23) + BUILD-ONLY constraint reconciliation.

---

## Decision / constraint gaps

| ID | Priority | Gap | Status |
|----|----------|-----|--------|
| GAP-DECISION-001 | P1 | **BUILD-ONLY interpretation.** OSS libraries (Playwright, LangGraph, Anthropic SDK, MCP SDK) = allowed ("writing"); turnkey servers/SaaS products = not allowed. | **RESOLVED 2026-06-23** (confirmed by user). GAP-ARCH-002 unblocked → closed. |
| GAP-ARCH-001 | P1 | **`pw-executor` is now critical-path.** Building + maintaining our own TS Playwright execution server is the largest single component and the highest ongoing-maintenance risk (tracks Playwright lib API churn). Mitigation: thin tool layer over Playwright's stable `Locator`/`accessibility` API; contract tests asserting tool names+schemas; pin Playwright version. | OPEN |
| GAP-ARCH-002 | P2 | If GAP-DECISION-001 resolves to "no OSS either", re-evaluate the entire stack (LangGraph → bespoke loop, Playwright → raw CDP). Would invalidate ADR-002/004/005. | BLOCKED on GAP-DECISION-001 |

## VERIFY (fact-checks — anti-hallucination; do not assume)

| ID | Priority | Item | Resolve by |
|----|----------|------|------------|
| GAP-VERIFY-001 | P1 | Underlying **Playwright library API** capabilities at the pinned version: `accessibility` snapshot surface, `tracing.start/stop`, locator engines (role/text/label/testid), `screenshot`. We DEFINE our own tool surface in `pw-executor`, but it sits on these primitives. | M0 |
| GAP-VERIFY-002 | P1 | **Python↔MCP binding** package + maturity (e.g. `langchain-mcp-adapters`): stdio transport stability, tool-schema validation strictness, subprocess error propagation. Both ends are ours now (lowers risk). Keep a thin swappable adapter so a ~300-line custom JSON-RPC client is a low-risk fallback. | M1 |
| GAP-VERIFY-003 | P2 | **LangGraph `SqliteSaver` / `AsyncPostgresSaver`** checkpointer API + separate-DB-file usage; `interrupt`/pause semantics for the human gate. | M1 |
| GAP-VERIFY-004 | P3 | **Anthropic SDK** structured-output / tool-use call shape for plan + heal nodes (model IDs `claude-opus-4-8`, `claude-sonnet-4-6`). | M1 |
| GAP-VERIFY-005 | P2 | **Real-provider smoke for the provider-agnostic backend (M6/ADR-019).** The environment blocks network → offline is covered by `FakeBackend`; real OpenAI-compat behaviour (at least one router: OpenRouter/DeepSeek/Qwen/Gemini-compat) is **user-run**: is `temperature=0` accepted?; `max_tokens` vs `max_completion_tokens` (o-series); missing `usage` (Ollama/vLLM); vision `image_url` data-URI. Instructions in `docs/M6_CONTRACT.md`. | M6 (user-run) |
| GAP-VERIFY-006 | P2 | **MCP `sampling/createMessage` support across hosts** for the M7 `SamplingBackend` (ADR-020): M7 implemented, offline-verified; the server `create_message` API is confirmed on the installed `mcp`. Remaining is **user-run** — a real host (Claude Desktop — yes; **OpenCode/Kilocode — confirm capability before production use**). No sampling → the backend is unavailable → fallback to heuristic/L1–L6. | M7 (user-run) |

## Design open questions (from synthesis)

| ID | Priority | Question |
|----|----------|----------|
| GAP-ARCH-003 | P1 | Precise **coverage metric** for ADR-010: "interactive element exercised" counted per `semantic_id` / per `(page,role,name)` / per distinct flow? Must not reward trivial clicks nor punish small apps. |
| GAP-ARCH-004 | P1 | How to reliably derive the scenario's target **SUBTREE** for `dom_subtree_hash` scoping (nearest landmark/role container?) without over/under-scoping. Validate against real DOM drift. |
| GAP-ARCH-005 | P2 | **Calibration bootstrap volume:** how many human-verified outcomes before lowering auto-accept threshold from the 0.90 default, and over what window? |
| GAP-ARCH-006 | P2 | Is explore-mode **soft verification on Sonnet** worth its cost, or does a deterministic post-action state check (URL change, expected element present) suffice? Measure @ M3. |
| GAP-ARCH-007 | P2 | **Cross-browser scope:** Chromium-only @ MVP assumed; confirm Firefox/WebKit near-term need (affects golden-baseline portability — hashes differ per engine). |
| GAP-ARCH-008 | P1 | **Auth/secret handling** for the AUT (storage-state/cookie injection): where do credentials live (home-lab Vault?) and how are they referenced in `RunConfig` without landing in traces/transcripts? |

## Risks (full list; summary in ARCHITECTURE §8)

| ID | Priority | Risk | Mitigation |
|----|----------|------|------------|
| GAP-RISK-001 | P1 | `pw-executor` maintenance burden (build-only) — see GAP-ARCH-001 | thin layer + contract tests + pinned version |
| GAP-RISK-002 | P1 | Confidence-model cold start (no human-verified data when the store is being seeded with the most consequential records) | threshold 0.90 until N labeled; verify-before-accept + post-heal verify = model-independent gate; M2/M3 budget human review to bootstrap calibration set |
| GAP-RISK-003 | P2 | Token-cost blowout on large SPAs (50+ pages, Opus pricing) | coverage convergence (ADR-010), depth cap, per-page budget, graceful degrade to partial frozen plan, Go hard ceiling, incremental explore (skip unchanged-a11y-hash pages) |
| GAP-RISK-004 | P2 | Heal-storm latency/cost variance in deterministic replay hot path on a churning AUT | hard 2-attempt cap + per-step deadline + auto-skip; `dom_subtree_hash` amortization (recurrent change = 0 LLM after first heal); quarantine caps blast radius |
| GAP-RISK-005 | P2 | a11y-tree blind spots (shadow DOM, canvas, custom web components, cross-origin iframes) | `completeness_ratio` first-class metric (Grafana histogram) triggers visual fallback + surfaced in report; recommend AUT team add data-testid/ARIA where chronically low |
| GAP-RISK-006 | P2 | `dom_hash` fragility — whole-page hash invalidates all locators on unrelated change (ads/A-B/analytics) | hash target SUBTREE not page; configurable scope; CSS ignore-list for volatile widgets |
| GAP-RISK-007 | P2 | SQLite write contention under parallel CI / multi-runner K3s | per-job SQLite for CI (no shared writer); single Go-writer + WAL for service; documented Postgres trigger (>50 concurrent shared-DB writers / distributed workers) |
| GAP-RISK-008 | P3 | Proto/gRPC versioning friction as the brain evolves | CI-generated stubs from one `.proto`; proto-hash assertion (mismatch=build failure); optional fields + 1-major backward compat; boundary phased in only @ M2 |
| GAP-RISK-009 | P2 | Screenshot-hash not byte-stable across separate browser launches (baseline run vs replay run) → flaky visual golden-diff. M3 mitigations: capture once per page at first landing (no focus/caret), and make **visual regression advisory** (a11y-hash drives exit 2). Full fix deferred: fixed viewport + font set + `animations:'disabled'`/`caret:'hide'`, or capture goldens in the same process. **PARTIAL (M8):** determinism options implemented in pw-executor (`animations:'disabled'` + `caret:'hide'` + `scale:'css'` + fixed viewport 1280×720/DSR=1), tsc-verified; visual stays **advisory** until byte-stability is confirmed (golden twice in separate processes = equal). Flip to authoritative is a follow-up | PARTIAL |
| GAP-OBS-001 | P3 | M4b deferrals: Go `report-service` HTTP `/metrics` (dropped — batch CronJob uses Pushgateway/textfile, ADR-018); TS (`pw-executor`) + Go (`store-gateway`) OTel spans with W3C context propagation; Go-side hard budget ceiling (needs a long-running Go orchestrator + brain→Go token reporting; default heuristic path uses no LLM). **RESOLVED (M8/ADR-021):** Go `report-service` (HTTP) + TS/Go OTel spans + W3C propagation (executor `_meta` + store.py gRPC interceptor + otelgrpc StatsHandler + per-node spans) + Go `orchestrator` budget-ceiling — implemented and **compile/test-verified** (Python 36 offline + `go build`/`vet`/`test` + `tsc`, all clean). Remaining to observe: a live OTLP trace + the real budget-kill end-to-end | RESOLVED |
