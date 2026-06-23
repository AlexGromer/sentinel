# Architecture — Sentinel

> Autonomous Self-Healing Playwright UI-Testing Agent.
> Polyglot: **Go** spine / **Python** LangGraph brain / **TypeScript** Playwright executor.
> Generated from a 3-phase design workflow (4 independent architects → 3 adversarial judges → lead synthesis), 2026-06-23.
> Deep mechanics live in `docs/` (see §7). Design provenance in `docs/DESIGN_RECORD.md`.

---

## 0. CONSTRAINT OVERRIDE — BUILD-ONLY (2026-06-23, hard)

**User directive:** *"We cannot buy / adopt anything off-the-shelf; we can only write it ourselves."*

**Interpretation (assumption — to confirm):** open-source **libraries** we write code against (Playwright library, LangGraph, Anthropic SDK) are *"writing"* and allowed. Adopting a turnkey third-party **server / SaaS product** is *not* allowed. If even OSS libraries are off-limits (pure from-scratch incl. browser CDP), scope changes drastically — see GAP-ARCH-002 / open question.

**Consequence:** the synthesis's single most-praised decision — *BUY the official Microsoft `@playwright/mcp` server* — is **REVERSED**. We **BUILD** our own TypeScript Playwright execution server (`pw-executor`). All three design judges flagged a hand-built Playwright server as the biggest "language-tourism" cost; that warning is **acknowledged and accepted** as the unavoidable price of build-only sovereignty. The MCP-over-stdio transport (ADR-002) **stays** — MCP is an open protocol we implement ourselves; we build the *server*, we do not buy it.

---

## 1. Context

### Purpose
A production-grade, standalone autonomous UI-testing agent that (1) explores an unknown web app on its own, (2) decides what flows to test, (3) freezes a deterministic, replayable test plan, (4) repairs broken locators when the DOM changes, and (5) emits engineer-consumable artifacts (reports, traces, exported Playwright specs, regression baselines). It is the differentiator over the existing `qa-automation-engineer` subagent, which only *writes* tests — Sentinel *discovers and maintains* them.

### Actors
| Actor | Role | Interface |
|-------|------|-----------|
| CI pipeline | Runs deterministic replay; consumes exit codes 0/1/2/3 + JSON/JUnit reports | `agentctl run --ci` |
| QA / dev engineer | Triggers explore runs, reviews flagged heals + human gates, approves baselines, consumes `.spec.ts` | `agentctl` (interactive) |
| Home-lab operator | Runs the long-lived service on K3s/ArgoCD, watches Grafana cost/health | Helm + ArgoCD (M5) |
| The agent itself | Autonomous LLM explorer | Opus 4.8 (plan) / Sonnet 4.6 (heal) |

### Scope
- **In scope:** autonomous exploratory testing; locator self-healing with confidence gating + human-in-loop; explore-once / replay-many CI determinism; short- + long-term memory; per-run token/cost budgets + tracing; artifact emission; headless CI + long-running service; home-lab K3s/ArgoCD target.
- **Out of scope (v1):** being a Claude Code `.md` subagent (explicitly excluded); multi-tenant SaaS; auto-merging healed plans into protected branches without review; load/perf testing; mobile-native (non-web); cross-browser beyond Chromium at MVP (Firefox/WebKit deferred); assertions about business correctness beyond observable UI state.

---

## 2. Components / Areas

### Overview
```
                  ┌──────────── Go (control-plane / spine) ────────────┐
  CI / engineer ─►│ agentctl (CLI) → orchestrator (run FSM, gRPC srv,   │
                  │   budget ceiling, subprocess supervision)          │
                  │ store-gateway (SOLE writer, main SQLite-WAL)        │
                  │ report-service (JSON+HTML, /metrics, .spec.ts gen)  │
                  └───────────────────────┬────────────────────────────┘
                              gRPC proto3 (UDS/TCP)  ◄── phased in @ M2
                  ┌───────────────────────┴────────────────────────────┐
                  │            Python (brain — LangGraph)               │
                  │ StateGraph(9 nodes) · perception · healing-engine   │
                  │ checkpointer → SEPARATE SQLite file (not main DB)   │
                  └───────────────────────┬────────────────────────────┘
                              MCP / JSON-RPC 2.0 over stdio
                  ┌───────────────────────┴────────────────────────────┐
                  │  TypeScript (hands — BUILD)  pw-executor (our own)   │
                  │  Playwright Chromium · a11y snapshot · trace        │
                  └─────────────────────────────────────────────────────┘
```

### Component table
| Component | Lang | Responsibility | Key tech |
|-----------|------|----------------|----------|
| **agentctl** | Go | Single CLI/CI binary: `run` (`--explore`/`--replay`/`--ci`), `gate`, `report`, `baseline update`, `locators`, `calibrate`. Exit codes **0** pass / **1** step-fail / **2** golden-diff regression / **3** plan-integrity-or-budget. Works non-TTY + interactive. | cobra/urfave-cli, gRPC client, Viper (YAML) |
| **orchestrator** | Go | Run-lifecycle FSM (PENDING→RUNNING→HEALING→PAUSED→PARTIAL→DONE\|FAILED\|ABORTED), gRPC server (RunControl + EventStream), supervises Python brain subprocess (5s health-ping, restart-on-crash, SIGTERM on per-step deadline). Enforces **Go-side hard budget ceiling** (reconciled vs the brain's in-process counter — NOT a per-call round trip). Does not touch SQLite. | gRPC, goroutine supervisor, context deadlines |
| **store-gateway** | Go | **Sole writer** to the main SQLite (WAL). All long-term state via PersistenceService gRPC; owns migrations; exposes read RPCs. (The LangGraph checkpointer uses a SEPARATE DB file — so single-writer is *actually* true.) | SQLite WAL, golang-migrate, gRPC |
| **report-service** | Go | Assembles `run_report.json` + HTML (mirrors Playwright HTML-reporter), serves trace/spec/cost endpoints, exposes Prometheus `/metrics`. Generates `.spec.ts` from `RunState.executed_actions` via template (no codegen-tool dependency). Introduced @ M4. | Go html/template, client_golang |
| **brain** | Python | LangGraph StateGraph (9 nodes). Owns ALL LLM calls (Opus 4.8 plan; Sonnet 4.6 heal). Spawns `pw-executor` + binds its MCP tools. gRPC client to orchestrator + store-gateway. Owns explore/replay switching, coverage-based convergence, `plan_hash`. | LangGraph StateGraph + checkpointer, MCP client (VERIFY pkg), Anthropic SDK |
| **healing-engine** | Python | The heal node: bounded re-grounding hierarchy (cache → L1–L6 no-LLM rotation → LLM a11y → gated set-of-marks), grounded confidence model with verify-before-accept, append-only `healing_audit`. Hosts `agentctl calibrate` logic. | Playwright locator strategies via MCP, structured-output LLM |
| **perception** | Python | Parses a11y snapshot → typed `PageModel`, computes `completeness_ratio` to pick modality, computes a11y-hash + subtree-scoped `dom_hash`. | a11y normalization, SHA-256 hashing |
| **pw-executor** | **TS (BUILD)** | **OUR OWN** Node service exposing Playwright primitives (navigate, accessibility snapshot, click/type/etc., screenshot, trace control, locator resolve/probe, set-of-marks overlay) to the brain over an MCP (JSON-RPC 2.0) stdio interface **we implement**. Runs as a child subprocess of the brain. | Playwright (lib, pinned), MCP server impl (ours), stdio JSON-RPC |
| **proto** | shared | protobuf3 single source of truth for Go↔Python. Services: RunControl, PersistenceService, EventStream. Stubs generated in CI for Go+Python; `.proto` hash asserted vs checked-in stubs (mismatch = build failure). Introduced @ M2. | buf/protoc, CI codegen + hash assertion |

### Key interactions / boundaries
**Two live wire protocols; a third boundary deliberately eliminated.**

1. **Go ↔ Python — gRPC proto3** (bidi streaming, UDS single-host / TCP for K3s). *Why:* compile-time typed contracts (drift = build failure), server-push of budget/gate events without polling, deadline propagation = per-step timeout. **Phased in @ M2** — M0/M1 use plain subprocess + env vars (`TARGET_URL`, `RUN_ID`, `RUN_MODE`, `ARTIFACT_DIR`). Rejected: REST/JSON (no compile-time schema, no clean streaming/cancel).
2. **Python ↔ TS — MCP (JSON-RPC 2.0) over stdio**, to `pw-executor`, bound via LangGraph's MCP tool integration. *Why:* native LLM tool-call protocol (zero adapter), stdio avoids CI port-allocation flakiness, subprocess lifecycle owned by the Python parent (SIGTERM cascade), EOF is a clean failure signal. **BUILD-only note:** we implement this server ourselves (ADR-001).
3. **TS → Go (artifacts) — ELIMINATED.** Playwright traces written to a shared artifact dir; the brain receives the path in the MCP response and relays it to Go over the existing gRPC channel; `report-service` reads files directly. `.spec.ts` generated by Go from `RunState`, not pushed from TS. Fewer wires = fewer failure modes.

**Failure isolation (real):** TS/MCP crash → brain detects EOF, restarts subprocess once, re-navigates to checkpoint `page_model.url`, re-enters the node (no work lost — checkpoint precedes the action). Python crash → orchestrator detects gRPC stream termination, marks FAILED, persists partial state, leaves checkpoint intact (`agentctl run --resume`). Go crash → brain reconnects with backoff; main DB durable; budget degrades safely to the in-process counter.

---

## 3. Decisions (ADR Log)

| ID | Date | Decision | Status | Context / rejected alternative |
|----|------|----------|--------|--------------------------------|
| ADR-001 | 2026-06-23 | **BUILD** our own TS Playwright execution server (`pw-executor`) over an MCP stdio interface we implement | **Accepted (constraint-driven; supersedes synthesis)** | Build-only directive (§0). We own the tool schema (stable) at the cost of owning Playwright-API-churn maintenance. **Rejected:** BUY official `@playwright/mcp` (disallowed by constraint — was the synthesis's top pick) |
| ADR-002 | 2026-06-23 | MCP over stdio for the Python↔TS boundary | Accepted | Native LLM tool-call protocol; LangGraph binds it w/o adapter; stdio avoids CI port flakiness. **Rejected:** gRPC TS server; Python-Playwright in-process (violates polyglot lock, loses Node-native trace) |
| ADR-003 | 2026-06-23 | gRPC proto3 for Go↔Python, **phased in @ M2** (M0/M1 = subprocess+env) | Accepted | Compile-time contracts, server-push, deadline propagation. **Rejected:** REST/JSON; gRPC-from-day-1 (premature) |
| ADR-004 | 2026-06-23 | LangGraph StateGraph backbone; checkpointer in a **SEPARATE** DB file from store-gateway | Accepted | Free checkpoint/resume/conditional-heal-edges/human-pause; separate file makes "Go sole writer of main DB" true. **Rejected:** bespoke asyncio loop; shared checkpoint+store DB (two writers) |
| ADR-005 | 2026-06-23 | a11y tree = primary perception; set-of-marks visual = fallback gated by `completeness_ratio<0.30` AND a measured PoC | Accepted | ARIA roles/names are semantic, re-skin-stable, cheap, yield usable selectors. **Rejected:** screenshot-primary (cost, fragile); full-DOM snapshot (token blowout) |
| ADR-006 | 2026-06-23 | Explore-once / replay-many with `plan_hash` HARD-ABORT on replay + immutable golden baselines (operator-command update only) | Accepted | The frozen plan is the only trustworthy reproducibility guarantee (no provider determinism even at T=0). **Rejected:** seeded/T=0 LLM as the determinism mechanism; auto-regenerate plan on staleness (P2's fatal flaw); HAR replay |
| ADR-007 | 2026-06-23 | Go store-gateway = single writer of one main SQLite (WAL); Postgres + AsyncPostgresSaver deferred to M5 behind explicit trigger | Accepted | Zero-ops, cp-backupable, single-writer + concurrent-readers matches access; per-job SQLite for CI parallelism. Postgres-compatible schema. **Rejected:** Postgres day-1; Python direct DB access |
| ADR-008 | 2026-06-23 | Grounded, calibrated confidence: per-strategy priors + empirical discounts + MANDATORY verify-before-accept live-DOM probe + post-heal verification + scheduled calibration | Accepted | Every LLM/visual candidate re-probed live (confidence zeroed if absent); `calibrate` recomputes precision/recall vs human-verified; cold-start threshold raised to 0.90. **Rejected:** raw LLM self-report vs magic thresholds with no calibration path |
| ADR-009 | 2026-06-23 | Model split: Opus 4.8 for explore/plan, Sonnet 4.6 for healing | Accepted | Planning quality drives the differentiator (runs once/explore); healing is bounded + structured + in the replay hot path (latency/cost). **Rejected:** uniform Opus (5–8× cost); local model (VERIFY home-lab GPU sufficiency if revisited) |
| ADR-010 | 2026-06-23 | Exploration terminates on a MEASURABLE coverage target (fraction of discovered interactive elements exercised + empty nav frontier); budget = backstop | Accepted | Closes the cross-cutting hand-wave: an LLM "done" flag bounds, it does not converge. **Rejected:** LLM `exploration_complete` flag alone; fixed-depth-only cap |
| ADR-011 | 2026-06-23 | Pluggable planner: `HeuristicPlanner` (default, offline, deterministic, zero-cost) + `LLMPlanner` (Opus 4.8, optional via `--planner llm`, falls back to heuristic) | Accepted | Makes the M1 explore-gate verifiable offline / in CI without network or LLM spend, and doubles as the budget-exhaustion graceful-degradation path (consistent with §8). LLM stays the primary "smart" explorer when a key is present. **Rejected:** Opus-only plan node (untestable offline, costs tokens per smoke run, blocks CI) |
| ADR-012 | 2026-06-23 | M2 delivered heal-engine-first: deterministic L1–L6 rotation + verify-before-accept + confidence gate + minimal replay, with an **interim brain-local SQLite store**; Go store-gateway+gRPC+proto (M2b) and MCP-SDK transport deferred | Accepted | Self-healing is M2's value and is testable offline; gRPC/store-gateway is infra best introduced separately. Heal needs a stale-locator trigger → minimal replay pulled forward (without the M3 trust layer). Interim local store is a **documented temporary deviation** from ADR-007 (single-writer), restored at M2b. **Rejected:** full M2 bundle at once (high integration risk, untestable in the gated/offline env) |
| ADR-013 | 2026-06-23 | Heal and golden-diff **coexist**: a healed step still executes AND its page is still golden-diffed, so a drift that heals via testid also raises an a11y golden regression (exit 2). Golden baselines are **page-keyed by URL basename** (cross-base comparison). M3 gRPC orchestrator stays in M2b | Accepted | Healing = test robustness (keep running); golden-diff = change detection (flag the change). They answer different questions and must both fire. Page-basename keying lets a plan explored on site/ be diffed against site-v2/. **Rejected:** treating a heal as suppressing the regression signal (would hide real app changes) |

> ADR template for new decisions:
> ```
> ### ADR-NNN: Title
> - Date / Status (Proposed/Accepted/Deprecated/Superseded) / Context / Decision / Consequences
> ```

---

## 4. Constraints

| Constraint | Type | Impact | Mitigation |
|------------|------|--------|------------|
| **BUILD-ONLY — no off-the-shelf products** (§0) | Business/strategic | Must write the TS Playwright executor ourselves; cannot adopt `@playwright/mcp` | ADR-001; thin tool layer over Playwright's stable lib API; contract tests; pin version |
| LLM non-determinism | Technical | Autonomous explorer not bit-reproducible | Explore-once/replay-many + `plan_hash` hard-abort (ADR-006) |
| LLM token cost | Business | Explore on large SPAs is expensive (Opus) | Coverage convergence (ADR-010), per-run budgets, graceful degradation, Go-side hard ceiling |
| a11y-tree blind spots | Technical | Shadow DOM / canvas / custom elements / cross-origin iframes give partial perception | `completeness_ratio` metric → gated visual fallback; recommend AUT add `data-testid`/ARIA |
| Home-lab target (K3s/ArgoCD/Proxmox/Ceph) | Technical | Deploy as GitOps service @ M5 | Helm chart + ArgoCD Application; Postgres swap behind trigger |
| Chromium-only @ MVP | Technical | Golden a11y/screenshot hashes differ per engine | Firefox/WebKit deferred; baseline portability is an open question |

---

## 5. Principles
1. **Trust is the product** — a non-deterministic LLM explorer must be *structurally* unable to silently rewrite its own baseline or run a tampered plan.
2. **Build, own, control** — no turnkey third-party products; we own every boundary (build-only sovereignty).
3. **Buy nothing, but reuse OSS libraries** — write code against Playwright/LangGraph/Anthropic SDK; build the *components*, not the *primitives*.
4. **Defer infrastructure behind named triggers** — gRPC @ M2, Postgres @ M5, OTel @ M4 — no speculative gold-plating.
5. **Verify before trust** — every healed locator is re-probed against the live DOM before acceptance; every confidence threshold is calibrated, never a magic constant.
6. **Measure convergence, don't assert it** — coverage metric over LLM "done" flag.

---

## 6. Change Log
| Date | Change | ADR | Author |
|------|--------|-----|--------|
| 2026-06-23 | Initial architecture from design workflow (4 architects → 3 judges → synthesis) | ADR-001..010 | @AlexGromer / Claude |
| 2026-06-23 | BUILD-ONLY override: ADR-001 reversed BUY→BUILD (`pw-executor` written in-house) | ADR-001 | @AlexGromer |
| 2026-06-23 | M0 (Hello Browser) delivered: Go→Python→TS wire + trace.zip (commit e6844ba) | — | @AlexGromer |
| 2026-06-23 | M1 started: LangGraph StateGraph + pluggable planner (heuristic default + Opus optional) | ADR-011 | @AlexGromer |
| 2026-06-23 | M1 delivered: LangGraph 9-node explore, deterministic plan.json (8 steps, coverage 1.0); docs-first dev guide added | ADR-011 | @AlexGromer |
| 2026-06-23 | M2 heal-core delivered: deterministic L1–L6 self-heal + verify-before-accept + minimal replay (healed=2/0 on drifted fixture); gRPC/store-gateway split to M2b | ADR-012 | @AlexGromer |
| 2026-06-23 | M3 started: replay trust layer — plan_hash hard-abort, exit codes 0/1/2/3, dual golden baselines, AUT-SHA flake quarantine, GitHub Actions | ADR-006, ADR-013 | @AlexGromer |
| 2026-06-23 | M3 delivered: trust layer live-green (CLEAN 0 / DRIFT heal+a11y-regression 2 / tampered 3); first-landing golden symmetry + visual-advisory (GAP-RISK-009); offline test suite + CI workflow | ADR-006, ADR-013 | @AlexGromer |

---

## 7. Where the detail lives (`docs/`)
| File | Contents |
|------|----------|
| `docs/STATE_MACHINE.md` | Full LangGraph: 9 nodes, all edges (incl. conditional/heal), the `RunState` shared object schema |
| `docs/SELF_HEALING.md` | The 10-step self-healing algorithm, L1–L6 strategy priors, confidence gate, calibration |
| `docs/DETERMINISM.md` | Explore-once/replay-many, `plan_hash` hard-abort, immutable golden baselines, flake quarantine, exit codes |
| `docs/MEMORY_PERSISTENCE.md` | Short/long-term memory, SQLite schema (all tables), checkpoint GC |
| `docs/OBSERVABILITY.md` | OTel tracing, LLM transcript, token budget + hard caps, Prometheus metrics |
| `docs/OUTPUTS.md` | The 10 emitted artifacts |
| `docs/ROADMAP.md` | M0→M5 milestones with Given/When/Then acceptance gates + build-only deltas |
| `docs/DESIGN_RECORD.md` | Full design provenance: 4 architect proposals + 3 judge verdicts + synthesis decision trail |
| `GAPS.md` | Open questions, VERIFY items, risks, build-only consequences |

## 8. Top risks (summary — full list in GAPS.md)
1. **`pw-executor` is now critical-path** (build-only): largest single build + ongoing Playwright-API-churn maintenance. *Mitigation:* thin tool layer over Playwright's stable Locator/accessibility API; contract tests; pin version.
2. **Confidence-model cold start** — no human-verified outcomes early. *Mitigation:* default threshold 0.90 until N labeled; verify-before-accept + post-heal verify are model-independent gates.
3. **Token-cost blowout on large SPAs.** *Mitigation:* coverage convergence, per-page budget, graceful degradation, incremental explore.
4. **Heal-storm latency in the deterministic replay hot path.** *Mitigation:* hard 2-attempt cap + per-step deadline + cached-locator amortization (`dom_subtree_hash`).
5. **`dom_hash` fragility.** *Mitigation:* hash the target SUBTREE, not the page; CSS ignore-list.

---

## Type-Specific Extensions — Development

### Build & CI
- **Languages/tools:** Go (control-plane), Python 3.x (brain, LangGraph), TypeScript/Node (pw-executor, Playwright).
- **CI:** GitHub Actions — `explore` job (conditional/manual) + `replay` matrix; proto codegen + `.proto`-hash assertion (M2); gitleaks secrets scan; per-job SQLite for parallel replay.
- **Pre-commit:** gitleaks; `.claude/` git-ignored (never committed).

### Testing strategy
- **Self-test split:** Go unit (orchestrator FSM, budget reconciliation), Python unit (node logic, confidence model), TS unit (pw-executor tool layer), contract tests (proto stubs, MCP tool schema), e2e (the agent against a fixture app).
- **Acceptance = milestone gates** (`docs/ROADMAP.md`), expressed Given/When/Then with thresholds.

### Dependencies (OSS libraries — allowed under build-only)
Playwright (pinned), LangGraph + checkpointer, Anthropic SDK, gRPC/protobuf (buf/protoc), SQLite (WAL), Prometheus client, OpenTelemetry SDK. **No turnkey third-party servers/SaaS.**
