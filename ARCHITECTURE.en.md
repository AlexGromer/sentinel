# Architecture — Sentinel

> 🌐 [Русский](ARCHITECTURE.md) (основная версия) · **English**

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
| The agent itself | Autonomous LLM explorer | Opus 4.8 (plan) / Sonnet 4.6 (heal) — **defaults**; planner/heal are provider-agnostic per-role via `LLM_BACKEND*` (Anthropic or any OpenAI-compatible), ADR-019 |

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
| **brain** | Python | LangGraph StateGraph (9 nodes). Owns ALL LLM calls through the provider-neutral `LLMBackend` (`brain/llm.py`: `AnthropicBackend` \| `OpenAICompatBackend`, per-role `make_backend`; defaults Opus 4.8 plan / Sonnet 4.6 heal), falls back to heuristic / L1–L6 when no key/SDK. Spawns `pw-executor` + binds its MCP tools. gRPC client to orchestrator + store-gateway. Owns explore/replay switching, coverage-based convergence, `plan_hash`. | LangGraph StateGraph + checkpointer, MCP client (VERIFY pkg), Anthropic / OpenAI SDK |
| **healing-engine** | Python | The heal node: bounded re-grounding hierarchy (cache → L1–L6 no-LLM rotation → LLM a11y → gated set-of-marks), grounded confidence model with verify-before-accept, append-only `healing_audit`. Hosts `agentctl calibrate` logic. | Playwright locator strategies via MCP, structured-output LLM |
| **perception** | Python | Parses a11y snapshot → typed `PageModel`, computes `completeness_ratio` to pick modality, computes a11y-hash + subtree-scoped `dom_hash`. | a11y normalization, SHA-256 hashing |
| **brain/llm.py** | Python | Provider-agnostic `LLMBackend` abstraction (Protocol): `AnthropicBackend` (native) \| `OpenAICompatBackend` (ChatGPT/DeepSeek/Qwen/Gemini-compat/OpenRouter/Ollama/vLLM); `make_backend(role)` picks a backend per role via env (`LLM_BACKEND[_PLANNER\|_HEAL]`, `_MODEL`, `_BASE_URL`, `_API_KEY`, `_VISION`) → `None` when key/SDK absent ⇒ heuristic / L1–L6 fallback. The LLM path is best-effort; vision gated by `supports_vision`; defaults Opus 4.8 (planner) / Sonnet 4.6 (heal). (ADR-019) | Anthropic / OpenAI SDK, `typing.Protocol` |
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
| ADR-014 | 2026-06-24 | M4 report / `.spec.ts` export / metrics / calibrate implemented as **brain (Python) generators** reading run artifacts + the interim store; the Go `report-service` (§2) and OTel→Tempo / Prometheus HTTP endpoint deferred until M2b consolidates persistence | Accepted | The user-facing value (readable reports + exported tests) is pure generation, testable offline now; a Go service reading the brain-local SQLite before M2b would be rework. **Rejected:** building report-service in Go pre-M2b (duplicates persistence wiring that M2b restructures) |
| ADR-015 | 2026-06-24 | M2b-1: `store-gateway` = a Go gRPC service (sole SQLite writer) spawned by agentctl over a Unix-domain socket; `brain/store.py` is reimplemented as a thin gRPC client preserving its exact method interface (drop-in) so healing/replay/calibrate are unchanged. Restores ADR-007 | Accepted | The clean Store interface lets us swap SQLite→gRPC with near-zero churn to call sites; agentctl-as-supervisor avoids a separate daemon for local/CI. **Rejected:** Python keeps writing SQLite (perpetuates the ADR-012 deviation); a standalone always-on daemon (ops burden for local runs) |
| ADR-016 | 2026-06-24 | M2b-2: pw-executor migrates to the MCP SDK (`@modelcontextprotocol/sdk` server); brain wraps an MCP stdio client behind the existing `Executor.call` interface; the hand-rolled JSON-RPC is retained as a documented fallback | Accepted | Realizes ADR-002 (native LangGraph MCP tool binding) and closes GAP-VERIFY-002; the wrapper keeps graph/healing/replay unchanged and de-risks SDK-API surprises. **Rejected:** staying on bespoke JSON-RPC forever (diverges from the MCP architecture target) |
| ADR-017 | 2026-06-24 | M5: ship as a containerized **K8s CronJob** via a Helm chart + ArgoCD Application (home-lab GitOps); set-of-marks visual heal is a **Tier-7 scaffold gated off** until a PoC measures ≥70% accuracy on 20 real broken-selector scenarios (ADR-005); Postgres checkpointer is an opt-in (`CHECKPOINT_DSN`) | Accepted | A CronJob matches the explore-once/replay-many model (scheduled CI-style replays) and fits ArgoCD GitOps on the existing K3s; visual heal is costly + non-deterministic, so it must prove its worth before shipping on. **Rejected:** always-on Deployment (the agent is batch, not a service); enabling visual heal unmeasured (token cost + flakiness) |
| ADR-018 | 2026-06-24 | M4b: observability = brain OTel tracing (prompt_HASH not content; OTLP export gated by `OTEL_EXPORTER_OTLP_ENDPOINT`, no-op default) + Prometheus **Pushgateway** for batch metrics. The always-on Go report-service HTTP `/metrics` is **dropped** in favor of push, because the agent is an ephemeral CronJob, not a scrapeable service | Accepted | Distributed traces are the real observability win and work with zero overhead when no collector is set; a batch job can't be HTTP-scraped, so push/textfile is the correct Prometheus integration. **Rejected:** an HTTP `/metrics` server in a job that exits in seconds (nothing to scrape); putting prompt content in spans (leaks secrets) |
| ADR-019 | 2026-06-25 | M6: **provider-agnostic LLM backend.** The planner + heal nodes call `brain/llm.LLMBackend` (`AnthropicBackend` native \| `OpenAICompatBackend` for ChatGPT/DeepSeek/Qwen/Gemini-compat/OpenRouter/Ollama/vLLM); selected per role via env (`LLM_BACKEND[_PLANNER\|_HEAL]`, `_MODEL`, `_BASE_URL`, `_API_KEY`, `_VISION`); `make_backend()` → `None` when key/SDK absent ⇒ the offline fallback (heuristic / L1–L6) is kept. The LLM path is **best-effort, no `plan_hash` guarantee**; `HeuristicPlanner` stays the deterministic anchor and golden baselines stay heuristic-only | Accepted | Removes the single-provider lock-in (user ask: Qwen/Deepseek/Gemini/ChatGPT/routers) without breaking determinism — the model only affects the explore artifact, replay is LLM-free. The per-role split preserves ADR-009 (Opus explore / Sonnet heal as the zero-env defaults). Vision is gated by `supports_vision` (a text-only provider skips Tier-7). **Rejected:** one global backend (breaks ADR-009 per-role split); a hard LiteLLM dependency in the hot path (foreign abstraction; kept as an option) |
| ADR-020 | 2026-06-25 | M7: expose the brain as an **MCP server** (`brain/server.py`, FastMCP; tools explore/heal/replay/report), distinct from the pw-executor MCP server; a host (OpenCode/Kilocode/Claude Desktop) drives it and supplies the model via MCP `sampling/createMessage` — implemented as `SamplingBackend(LLMBackend)` on top of the ADR-019 abstraction | Accepted | Closes the second direction of the user ask ("be driven by host agents"). The B1 abstraction (ADR-019) is sampling-compatible: `SamplingBackend.supports_vision=False`, tokens 0, `LLMResult.model` carries the host's real model, sync↔async bridge like `McpExecutor`, sync graph in a worker thread (loop stays free for the reverse sampling). **Delivered offline-verified (test_m7); live MCP host is user-run (GAP-VERIFY-006).** **Rejected:** doing B2 before B1 (sampling is a special case of a backend, needs the abstraction first) |
| ADR-021 | 2026-06-26 | M8 (Full GAP-OBS-001): (1) distributed W3C tracing across Go/Python/TS (gated OTLP); (2) hard budget ceiling — Python `BudgetTracker` (graceful degradation→heuristic/L1–L6) + a long-lived Go `orchestrator` (gRPC `RunControl`, token reconcile, SIGTERM-kill); (3) Go `report-service` (HTTP `/report`+`/metrics`); new `proto/runcontrol.proto` | Accepted (amends ADR-018) | **Amends, does not contradict ADR-018:** Pushgateway stays for the ephemeral CronJob (batch); the HTTP report-service is only for the long-lived orchestrator/service mode (scrapeable). Introduces the orchestrator promised in §2 but absent from code. Python budget + W3C + per-node spans are offline-verified; Go/TS + live OTLP + the real kill are user-run. **Rejected:** an HTTP `/metrics` in the ephemeral job (ADR-018 — nothing to scrape); a budget ceiling without the Go backstop (a model-cooperative kill is unreliable) |
| ADR-022 | 2026-06-26 | M9: goal-directed / NL test authoring via **explore-first grounding** — a new `GoalPlanner` (NL goal + live element map → steps) in the Planner seam (ADR-011); `--mode explore\|goal\|describe` + auto-default (no goal → pure explore) | Proposed | Explore-first stops the LLM hallucinating selectors; covers business processes on top of coverage-explore. **Rejected:** describe-first as the default (LLM invents nonexistent elements) |
| ADR-023 | 2026-06-26 | M9: dual chat access — **MCP** (brain-as-MCP-server, M7) AND a **non-MCP** thin HTTP/gRPC control API; chat-UI branch now (OSS front in DH/Docker), browser extension later | Proposed | "Both MCP and non-MCP" per the user; non-MCP is needed for CI/scripts and non-MCP chat fronts. **Rejected:** MCP-only (would lock out integrations) |
| ADR-024 | 2026-06-26 | M9: browser execution modes — own-headless (now) → headed → **CDP-attach to the user's browser** (`connectOverCDP`) → **co-pilot takeover/return** (human-in-the-loop) | Proposed | Supports "drive the user's browser" + take/return control (the extension branch). **Rejected:** own-headless forever (no live authoring) |
| ADR-025 | 2026-06-26 | M9: universality (beyond DH) via **pluggable adapters** — auth (none\|basic\|OIDC/Keycloak\|storageState) · deploy (CronJob\|Docker\|CLI) · model (cloud\|local Ollama/vLLM) · trace/metrics (any OTLP/Prom); DH specifics isolated to Helm values | Proposed | The core is already agnostic (target=URL); edge adapters keep the product portable. **Rejected:** baking DH/Keycloak into the core |
| ADR-026 | 2026-06-26 | M9.1: pw-executor interaction/auth/assert primitives — `fill`/`type`(`pressSequentially`)/`press`/`select`/`expect`(non-throwing, base waits)/`saveStorageState`, in **both** transports; secrets via env `secretRef` (resolved only inside pw-executor, **never** in plan/transcript/heal-report/rec/trace); auth runs disable Playwright tracing (`PW_NO_TRACE`); storageState load (`STORAGE_STATE`)/save (`STORAGE_STATE_SAVE`); new step kinds executed in replay/graph/exporter (step read-only → plan_hash stable) | Accepted | Blocker #1 (forms/login/negative testing, M9_CONTRACT §A1). The tracing gate is the only sound `trace.zip` protection (the secret leaks via the submit POST body + DOM snapshot; Playwright has no pause/mask API). **Rejected:** `@playwright/test` just for `expect` (GAP-ARCH-001 — keep pw-executor thin; base `waitFor`/`waitForURL` suffice); pausing tracing around `fill` (doesn't cover the submit POST) |
| ADR-027 | 2026-06-26 | M9.2a GoalPlanner: a goal-directed grounded planner in the `Planner` seam (ADR-011) — the LLM picks an **index** from the real live-map candidates, `propose` returns only `candidates[idx]`/`done` (OOB → done) ⇒ a selector hallucination is impossible (ADR-022); authoring mode by `--goal` presence (auto-default §C) + `PLANNER=goal`/RunConfig `mode` — **not** via `--mode` (= `RUN_MODE`); a minimal RunConfig YAML (mode/goal/planner/budgets; precedence flag>file>default); a `make_planner(env)` factory; goal-mode is best-effort (not `plan_hash`-stable, like ADR-019; replay stays deterministic) | Accepted | "Describe a goal in words → a grounded plan"; the heuristic stays the deterministic anchor + degradation path. **Rejected:** `--mode goal` (collides with `RUN_MODE`); complexity auto-detection (the signal is goal presence); the two-phase explore-then-scenario (§L) + describe-first (§B) — deferred to M9.2b |
| ADR-028 | 2026-06-27 | M9.2b two-phase authoring (§L/§B): goal/describe → a full deterministic heuristic explore (a site map generalized beyond buttons to input/select/link) → a **one-shot grounded phase-2 head** (`GoalPlanner.build_scenario` / `DescribePlanner.draft`+a deterministic `reconcile` in the new `brain/scenario.py`); cross-page navigates synthesized in code; authored steps carry the full grounded `locator`+`alternatives` (replay is LLM-free, deterministic); `plan.json`(walk+scenario)+`scenario.json`+`reconcile-report.json`; a rich RunConfig (declarative auth/scenarios + `--scenario`) | Accepted (supersedes the ADR-027 wiring) | Completes M9 conversational authoring. The LLM never drives the walk (phase 1 is deterministic); the per-step `propose` is retained for M9.4 live/co-pilot. describe-unmatched→exit 1; `GOAL`⊕`DESCRIBE`→exit 3. **Rejected:** a per-step goal planner over an all-pages menu (ad-hoc navigate synthesis); two sub-graphs (one scenario node is simpler); auth as a new adapter (M9.7 — here it's declarative env) |
| ADR-029 | 2026-06-27 | **Local models = a config decision (no new code).** planner/heal/vision run on any local OpenAI-compatible endpoint (Ollama/vLLM/llama.cpp/LM Studio) via the **existing per-role env** (ADR-019: `LLM_BACKEND[_PLANNER\|_HEAL]`/`_MODEL`/`_BASE_URL`/`_API_KEY`/`_VISION`) — **no new "profile" knob** (provider profiles are documented, not coded). The selection methodology is platform-agnostic: VRAM sizing (`params·bytes(quant)+KV-cache+overhead`) + token-cost-per-phase (from the verified `max_tokens`: explore 200/scenario 800/heal-text 200/heal-vision 100; budgets PLAN 50k/HEAL 20k; replay LLM-free) + a model/runtime catalog — in `docs/LOCAL_MODELS.md` + 3 interactive calculators (Pages). In-code default model ids stay `claude-*` (offline=FakeBackend; real local is opt-in via the documented env profiles) | **Accepted (supersedes the local-model deferral in ADR-009; builds on the ADR-019 mechanism)** | User request: both local and cloud. The mechanism already exists (M6/ADR-019) — what was missing was a platform-agnostic **methodology** (ADR-009 deferred local behind "VERIFY home-lab GPU sufficiency"; the RTX 2060 12 GB is now ONE example among the 8/12/16/24 GB tiers, not the basis). **Rejected:** a new `profile` knob (per-role env is enough — extra surface); binding defaults to local (breaks offline determinism/CI/golden) |
| ADR-030 | 2026-06-27 | **Distribution & packaging strategy** — a sequenced epic (contract `docs/DISTRIBUTION.md`): docker-compose one-command quickstart (this cycle) → GitHub Releases (multi-OS/arch binaries agentctl/store-gateway/orchestrator/report-service + Docker publish + checksums + **Cosign/GPG** signing, **M11.1**) → setup-WebUI (**M11.2**) → Helm/Flux/Argo expansion + **Secret plumbing** (**M11.3**, closes GAP-SEC-001) → air-gapped bundle (**M11.4**) → zero-level onboarding/installer (**M11.5**). This cycle closes the hardening prerequisite (§1 CI SCA gates + threat model) | Accepted | A release without hardening (SCA/SBOM/lockfile/signing + a threat model) is not credible → foundation first, the rest docs-first frozen. **Rejected:** everything in one release (4–5 milestones across release-eng/containers/GitOps/frontend — high integration risk) |
| ADR-031 | 2026-06-27 | **setup-UI: static-now / control-API-later.** Phase 1 — a static client-side config generator (vanilla JS, no backend, air-gapped — emits a RunConfig YAML/env block; kin to the Pages calculators); phase 2 — a backed control-API (**brain HTTP control-API, M9.3**) to change mode/API-keys/goals without DevOps. **M11.2** | Accepted | The static generator delivers value immediately and is air-gapped-friendly (zero-external-dep, like the calculators); a live WebUI needs a control-API that does not exist yet (→ M9.3). **Rejected:** a live WebUI now (needs an unbuilt backend + secret handling in the browser); no UI at all (a zero-level user can't edit config) |

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
| 2026-06-24 | M4 started: .spec.ts export, HTML+JSON report, Prometheus textfile metrics, agentctl calibrate (brain generators) | ADR-014 | @AlexGromer |
| 2026-06-24 | M4 core delivered: .spec.ts export + HTML/JSON/Prometheus report + calibrate (offline-verified, 8 tests); OTel/Prometheus-HTTP/Go-report-service → M4b | ADR-014 | @AlexGromer |
| 2026-06-24 | M2b started: spec for Go store-gateway+gRPC+proto (M2b-1) and MCP-SDK transport (M2b-2); split, store.py interface preserved | ADR-015, ADR-016 | @AlexGromer |
| 2026-06-24 | M2b-1 delivered: Go store-gateway + gRPC + proto, live-verified (gate 0/2/3 over gRPC); store.py drop-in LocalStore/GrpcStore; socket→/opt + GOTMPDIR fixes; prod path no sqlite handle | ADR-015 | @AlexGromer |
| 2026-06-24 | M2b-2 delivered: pw-executor dual transport (JSON-RPC default + MCP SDK opt-in), brain McpExecutor behind Executor.call; offline-verified, JSON-RPC unchanged (closes GAP-VERIFY-002) | ADR-016 | @AlexGromer |
| 2026-06-24 | M5 started: spec — deployment (Dockerfile + Helm CronJob + ArgoCD, M5-1), set-of-marks visual heal Tier-7 scaffold behind a ≥70% PoC gate (M5-2), Postgres checkpointer option (M5-3) | ADR-017 | @AlexGromer |
| 2026-06-24 | M5-1 delivered: Dockerfile (multi-stage) + Helm chart (CronJob + per-env values + optional Ceph PVC) + ArgoCD Application; helm lint clean, renders ConfigMap/CronJob/PVC/SA | ADR-017 | @AlexGromer |
| 2026-06-24 | M5-2 delivered: set-of-marks browser tool + HealingEngine Tier-7 visual heal (gated HEAL_VISUAL, mark→real locator, FLAGGED band); offline-tested (mock vision); real Sonnet-vision PoC gated/user-run | ADR-017 | @AlexGromer |
| 2026-06-24 | M5-3 delivered: Postgres checkpointer opt-in (CHECKPOINT_DSN → PostgresSaver else SQLite, near drop-in); default SQLite unchanged, offline-verified | ADR-017 | @AlexGromer |
| 2026-06-24 | M4b started: OTel brain tracing (prompt_HASH, OTLP-gated no-op default) + Prometheus Pushgateway for batch metrics; Go report-service HTTP / TS+Go spans / budget-ceiling deferred (GAP-OBS-001) | ADR-018 | @AlexGromer |
| 2026-06-24 | M4b delivered: brain OTel (sentinel.run + heal.llm spans, prompt_HASH, OTLP-gated no-op default) + Prometheus Pushgateway; offline-verified, suites green | ADR-018 | @AlexGromer |
| 2026-06-25 | M6 delivered: provider-agnostic LLM backend (`brain/llm.py`: AnthropicBackend + OpenAICompatBackend + make_backend per-role); planner/heal go through `LLMBackend`; default path (Anthropic/heuristic) unchanged, vision gated by `supports_vision`; offline-verified (test_b1 8 + test_m5 4, regress m3/m4/m4b green); real-provider smoke is user-run (network blocked) | ADR-019 | @AlexGromer |
| 2026-06-25 | M7 contract frozen (Proposed): MCP-server exposure + `SamplingBackend` on top of ADR-019; implementation next session (needs a live MCP host) | ADR-020 | @AlexGromer |
| 2026-06-26 | M7 delivered: brain MCP server (`brain/server.py`, FastMCP — tools explore/heal/replay/report) + `SamplingBackend` (host supplies the model via sampling; sync graph in a worker thread); `mcp` added to deps; offline-verified (test_m7 5 + regression green); live MCP host is user-run (GAP-VERIFY-006) | ADR-020 | @AlexGromer |
| 2026-06-26 | M8 started (Full GAP-OBS-001): contract + ADR-021 (amends ADR-018); Python budget accumulator + W3C propagation + per-node spans — offline; Go orchestrator/report-service + TS spans + proto/runcontrol — user-run build | ADR-021 | @AlexGromer |
| 2026-06-26 | M8 delivered (Full GAP-OBS-001): distributed tracing (W3C brain→pw-executor→store-gateway: executor `_meta` + store.py gRPC interceptor + pw-executor `otel.ts` + store-gateway `otelgrpc.StatsHandler` + per-node spans) + budget ceiling (Python `BudgetTracker` + Go `orchestrator` RunControl + SIGTERM backstop) + Go `report-service` (HTTP). All three languages instrumented and **compile/test-verified** (Python 36 offline + go build/vet/test + tsc clean). Remaining to observe end-to-end: a live OTLP trace + the real budget-kill | ADR-021 | @AlexGromer |
| 2026-06-26 | M9 design frozen (Proposed): conversational & goal-directed testing — `fill`/`type` + auth, `GoalPlanner` (NL authoring, explore-first), chat-UI (MCP + non-MCP), in-app/browser tabs, backend trace correlation, browser modes (headed/CDP-attach/co-pilot), pluggable adapters (universality beyond DH); `docs/M9_CONTRACT.md`, GAP-M9-01..08 | ADR-022..025 | @AlexGromer |
| 2026-06-26 | M9.1 delivered (offline): pw-executor `fill/type/press/select/expect/saveStorageState` (both transports) + storageState auth (`STORAGE_STATE`/`STORAGE_STATE_SAVE`) + tracing gate (`PW_NO_TRACE`) + secrets via `secretRef`; brain replay/graph/exporter execute the new step kinds (step read-only); `brain/validation.py` (invalid-input generator — sketch); `tests/test_m9_offline.py` (19). Adversarial-review hardening: fail-closed secret-while-tracing (throw + brain exit 3), corrupt-storageState fallback, `setDefaultTimeout`. Gates: `tsc` + offline suite m3..m9 + `go build` + gitleaks. `docs/M9.1_CONTRACT.md`. Live UI run (forms/Keycloak login) — separate, on "go" | ADR-026 | @AlexGromer |
| 2026-06-26 | M9.2a delivered (offline): `GoalPlanner` (a grounded goal-directed planner in the `Planner` seam, ADR-027) + `make_planner` auto-default by `--goal` + `brain/runconfig.py` (a minimal RunConfig YAML, precedence flag>file>default) + agentctl `--goal`/`--run-config`; goal-mode best-effort (replay stays deterministic); `pyyaml` in deps; `tests/test_m9_2_offline.py`. Gates: offline m3..m9_2 + `go build`/`vet` + `tsc` + gitleaks. `docs/M9.2_CONTRACT.md`. Deferred to M9.2b: describe-first, the two-phase explore-then-scenario, auth/scenarios in RunConfig. Live goal run — on "go" | ADR-027 | @AlexGromer |
| 2026-06-27 | M9.2b delivered (offline): two-phase goal (§L) + describe-first (§B) + a rich RunConfig (ADR-028). Site map generalized to input/select/link; `brain/scenario.py` (ground_scenario/reconcile + cross-page navigate synthesis); `GoalPlanner.build_scenario` + `DescribePlanner`; a graph scenario node; `scenario.json`/`reconcile-report.json`; agentctl `--describe`/`--scenario`; declarative auth/scenarios in RunConfig. Terminology: «грауденный»→`grounding`/"grounded in real elements". `tests/test_m9_2b_offline.py`. Gates: offline m3..m9_2b + `go build`/`vet` + `tsc` + gitleaks. `docs/M9.2b_CONTRACT.md` | ADR-028 | @AlexGromer |
| 2026-06-27 | **Foundation cycle**: security CI gates (gitleaks/govulncheck/pip-audit/npm audit + `go vet`/`go test` + the offline suite m3..m9_2b in CI — closes docs-vs-reality + GAP-SEC-002 partially); Dockerfile dep-fix (`openai`+`pyyaml`); `docker-compose.yml` (sentinel + ollama + demo profiles); GitHub Pages (`pages.yml`+`docs/index.md`+`_config.yml`) + 3 calculators (VRAM · token-cost · model-selector, vanilla JS, air-gapped); `docs/{LOCAL_MODELS,THREAT_MODEL,TESTING,DISTRIBUTION}.md` (+en); L1–L5 fixtures; GAP-OPS-001/002 + GAP-SEC-001/002; BACKLOG M11.1–M11.5 + M9-LIVE | ADR-029, ADR-030, ADR-031 | @AlexGromer |
| 2026-06-27 | Post-Foundation: **setup-WebUI** (static config generator, vanilla JS, ADR-031 phase-1) + Docker **`webui` bundle** (air-gapped, `python http.server` on :8088, assets under `/app/docs`); security hardening — **GAP-OPS-002 DONE** (`PW_IGNORE_HTTPS_ERRORS` opt-in + cert classification in `pw-executor`) + **GAP-SEC-001 PARTIAL** (opt-in env allowlist in `agentctl`, `SENTINEL_ENV_ALLOWLIST`, default OFF) | ADR-031 | @AlexGromer |

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
| `docs/ROADMAP.md` | M0→M7 milestones with Given/When/Then acceptance gates + build-only deltas |
| `docs/M6_CONTRACT.md` | M6 contract: provider-agnostic LLM backend (`brain/llm.py`, ADR-019) |
| `docs/M7_CONTRACT.md` | M7 contract (Proposed): expose the brain as an MCP server + `SamplingBackend` (ADR-020) |
| `docs/DESIGN_RECORD.md` | Full design provenance: 4 architect proposals + 3 judge verdicts + synthesis decision trail |
| `GAPS.md` | Open questions, VERIFY items, risks, build-only consequences |

## 8. Top risks (summary — full list in GAPS.md)
1. **`pw-executor` is now critical-path** (build-only): largest single build + ongoing Playwright-API-churn maintenance. *Mitigation:* thin tool layer over Playwright's stable Locator/accessibility API; contract tests; pin version.
2. **Confidence-model cold start** — no human-verified outcomes early. *Mitigation:* default threshold 0.90 until N labeled; verify-before-accept + post-heal verify are model-independent gates.
3. **Token-cost blowout on large SPAs.** *Mitigation:* coverage convergence, per-page budget, graceful degradation, incremental explore.
4. **Heal-storm latency in the deterministic replay hot path.** *Mitigation:* hard 2-attempt cap + per-step deadline + cached-locator amortization (`dom_subtree_hash`).
5. **`dom_hash` fragility.** *Mitigation:* hash the target SUBTREE, not the page; CSS ignore-list.
6. **Full env inheritance + plaintext secrets in Helm** (GAP-SEC-001): `agentctl` passes `os.Environ()` with no allowlist (main.go:68); the Helm CronJob injects env as plaintext `value:` (cronjob.yaml:34–46). *Mitigation (partial / planned):* AUT secrets already go through `secretRef`+`PW_NO_TRACE` (GAP-RISK-010, MITIGATED); env allowlist + `secretKeyRef` plumbing — **M11.3**. Full threat model — `docs/THREAT_MODEL.md`.
7. **Supply chain** (GAP-SEC-002): historically no SCA in CI, Python deps with no lockfile, releases without signing/SBOM. *Mitigation:* **this cycle** added gitleaks/govulncheck/pip-audit/npm audit gates (§1); a committed lockfile + Cosign/GPG-signed releases + SBOM — **M11.1**.

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
