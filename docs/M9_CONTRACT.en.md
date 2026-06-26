# M9 Contract — "Conversational & Goal-Directed Testing" (PROPOSED — design freeze 2026-06-26)

> 🌐 [Русский](M9_CONTRACT.md) (основная версия) · **English**

Status: **Proposed** (design freeze; a roadmap epic with sub-milestones M9.1…M9.8). Source — the
2026-06-26 design session. Implementation lands as separate sub-milestones, each docs-first.

Goal: evolve Sentinel from "coverage-explore + CLI" into a tool that (a) tests real **multi-step
business processes** including forms and authentication; (b) supports **natural-language (NL) test
authoring** grounded in the live element map; (c) works in **MCP and non-MCP** modes, with **local or
cloud** models; (d) stays **universal** (not Deckhouse-specific). Two delivery branches: **chat-UI
now**, **browser extension later**.

## A. Capability gaps (with session decisions)
| # | Gap | Decision / direction |
|---|-----|----------------------|
| A1 | **No `fill`/`type` in pw-executor** | Add `browser.fill`/`type`/`press`/`select` tools — without them no forms, search, or **login**. Blocker #1. |
| A2 | Explore is **coverage-driven**, not a business process | `GoalPlanner` (see B) — NL goal + element map → steps. |
| A3 | No NL authoring | Chat UX (see G) on top of M7 + a non-MCP API. |
| A4 | Auth (Keycloak) | storageState precondition + **login as a test target** (needs A1). See H. |
| A5 | **In-app tabs** (`role=tab`/`tabpanel` within one page) | Perception adds the `tab` role; tab-switch = in-page navigation. Mostly works. |
| A6 | **Browser tabs/windows** (multi-page) | pw-executor → multi-context/page; the plan references the tab. Real gap. |
| A7 | Backend correlation (microservices + Kafka) | Inject `traceparent` into all browser requests (see I). |
| A8 | Universality beyond DH | Pluggable adapters (see J). |
| A9 | Browser modes (own / user's / co-pilot) | own-headless → headed → CDP-attach → takeover (see F). |

## B. Authoring model — explore-first (default) vs describe-first
- **explore-first (default, recommended):** explore builds the map of **real** elements
  (`semantic_id`/role/name/testid) → the LLM **proposes scenarios** from the map OR accepts an NL
  description but **grounds it in real elements** → you edit/approve → freeze `plan.json` + `.spec.ts`.
  Why: the LLM cannot hallucinate selectors that don't exist.
- **describe-first (optional):** NL description → LLM draft plan → explore **reconciles** with reality
  → edits. For users who know the flow up front.

## C. Modes & switches
- **`--mode explore | goal | describe`** (explicit) **+ an auto-default:** no goal supplied → pure
  `explore` (for simple pages without business processes); an NL goal supplied → `goal`. **Not** a
  "magic complexity auto-detector".
- The pluggable **`GoalPlanner`** slots into the existing `Planner` seam (ADR-011) beside Heuristic/LLM.
- **Config surfaces (the mode is chosen per-run, not a live toggle):** (1) `agentctl` flags → env
  (DH ConfigMap/Secret); (2) a **RunConfig YAML** for rich runs (goal text, auth, budgets, scenarios — too
  much for flags); (3) **interactively via the chat-UI** (M9.3) — describe the goal in words, the mode is
  implied. There is no live switch mid-run: a run commits to a mode. (GAP-M9-09)

## D. Tabs: in-app vs browser (explicitly distinguished)
- **In-app tabs** (DOM tab widgets) — interactive elements; explore clicks them, the `tabpanel`
  content swaps → re-perceive. Needs: perception captures `role=tab`, coverage counts tabs. **A5.**
- **Browser tabs/windows** — pw-executor must hold several `page`/`context`, the plan stores a tab id,
  the executor routes calls per tab. **A6 (new code).**

## E. Models: cloud + local
- **Local models already work (M6):** `LLM_BACKEND=openai` + `LLM_BASE_URL=<ollama/vllm/lmstudio>`.
  On DH — Ollama/vLLM in-cluster → sovereign / air-gapped. Vision-heal: a vision model (Qwen-VL/LLaVA),
  gated by `supports_vision`. Per-role: a local model for heal, cloud for plan, or vice versa.

## F. Browser execution modes (evolution)
1. **own-headless (now):** pw-executor launches its own headless Chromium; the user doesn't see it.
2. **headed (visible):** `chromium.launch({headless:false})` — a trivial toggle; the user watches.
3. **CDP-attach to the user's browser:** `chromium.connectOverCDP` to a Chrome with `--remote-debugging-port`
   — Sentinel drives the user's **existing** browser/session. Foundation for the extension branch.
4. **co-pilot takeover/return:** the agent drives → hands control to the human → takes it back.
   Human-in-the-loop live authoring/editing. The second branch (after chat-UI).

## G. Access surfaces — MCP AND non-MCP (both)
- **MCP mode:** the brain as an MCP server (M7, ADR-020) — any MCP host (Open WebUI/Claude Desktop/…) drives it.
- **Non-MCP mode:** a thin **HTTP/gRPC control API** to the brain (reuse/extend RunControl) — for a chat-UI
  that doesn't want MCP, and for CI/scripts. **Chat is NOT MCP-only.**
- **Delivery branches:** **(1) chat-UI now** — an OSS front (Open WebUI / custom) in DH/Docker, talking to
  the brain via MCP **or** the control API; **(2) browser extension later** — live record/describe + CDP takeover (F3/F4).

## H. Auth (user decision: add it + test the login itself)
- **Precondition:** Playwright `storageState` (log in once → reuse cookie/token); creds from **Vault**,
  never in traces (`GAP-ARCH-008`).
- **Test target:** the login flow (Keycloak: form→submit→redirect→token) — a business process for explore/replay.
  **Requires A1 (`fill`/`type`).**
- **Pluggable auth adapter:** none | basic | OIDC/Keycloak | storageState file (for universality, J).

## I. Backend correlation (microservices + Kafka)
- Sentinel tests the UI black-box; it doesn't touch the backend directly. **BUT** injecting `traceparent`
  into ALL outgoing browser requests (`context.setExtraHTTPHeaders`/`route`) → **each test action maps to a
  full backend trace** (UI→frontend→service A→Kafka→service B→DB) in Tempo — provided the services are
  OTel-instrumented (the user's side). This is the value of Tempo for a microservices estate.

## J. Universality (beyond DH)
- **The core (Go/Python/TS) is already agnostic:** target = a URL; OTLP/Prometheus are standard; build-only, no lock-in.
- **Principle:** a universal core + **swappable adapters at the edges:** auth-provider · deploy-target
  (CronJob/Docker/standalone CLI) · model-backend (cloud/local) · trace/metrics-backend (any OTLP/Prom).
  DH specifics are isolated to Helm values + a Keycloak adapter. The core stays untouched.

## K. Clarification: code + agent, NOT an md-agent
Sentinel = an autonomous agent **implemented in code** (polyglot), not a Claude Code `.md` subagent
(explicitly out of scope, ARCHITECTURE §1). The md docs are the specification (docs-first), not the implementation.

## L. Session-2 clarifications (2026-06-26)
- **Models anywhere, not just in DH:** a model endpoint = `LLM_BASE_URL` + `LLM_API_KEY`. Cloud
  (Anthropic/OpenAI/OpenRouter) or self-hosted elsewhere — just an endpoint. **The vision model is per-role**
  (`LLM_*_HEAL`): it may be a **separate** endpoint+key (or the same). Nothing must be deployed in DH.
- **Connecting to an already-deployed UI:** yes — `target` is just a URL (in-cluster Service, Ingress, external,
  staging). Sentinel connects to any reachable URL; nothing needs re-deploying.
- **goal-mode = a FULL explore first, then the goal:** explore can't itself "decide a goal is needed" (that's
  semantics). So goal-mode first runs a **full explore** (map of all pages/elements + problem discovery), and the
  `GoalPlanner` builds the scenario from that map → both working elements AND problems are found. Pure explore (no
  goal) is for simple pages.
- **Metrics: both push and pull.** The ephemeral CronJob **pushes** to a Prometheus **Pushgateway** (we emit). The
  long-lived `report-service` serves `/metrics` which Prometheus **scrapes** (it pulls). Chosen by deploy mode.
- **What/how we deploy:** (a) **DH/k8s** — Helm CronJob + ArgoCD (GitOps), Ollama/vLLM optionally in-cluster;
  (b) **Docker** — a single container (Dockerfile exists), `agentctl` in a container/compose; (c) **bare CLI** —
  the `agentctl`/`store-gateway`/`orchestrator` binaries + venv + node, no k8s. The core is identical; only the wrapper changes.
- **Field validation (format/chars/bounds) — IN scope (M9.x):** negative testing — enter invalid input →
  **assert the UI rejected it** (error/blocked submit). Needs `fill`/`type` (A1) + an assertion layer + an
  invalid-input generator per field type/mask. This catches "validation doesn't work".
- **UI security checks — a SEPARATE module, not the core.** Functional + validation testing is the core; security
  (XSS/CSRF/IDOR/auth-bypass/sensitive-data-in-DOM) is a **pluggable security module (M10/extension)** consuming the
  element map + traces. Reasons: a different discipline, a **different authorization model** (active security needs
  explicit authorization — per your own rules), and to keep the core lean. The substrate (explore map) is ready.
- **CI/commits:** Sentinel = a CLI + exit codes 0/1/2/3 → **any CI** (Jenkins/GitLab/Drone/GH Actions) calls
  `agentctl run --replay --ci`. On commit → CI hook → build/deploy a preview → run → gate on the exit code. GitHub
  Actions already exists; Jenkinsfile/.gitlab-ci templates are M9.x.
- **Connecting CI and running WITHOUT CI (runner-agnostic):** Sentinel depends on no CI — it is a CLI + exit codes
  invoked by **any runner.** (a) **CI** (Jenkins/GitLab/GH Actions/Drone): a step calls `agentctl run --replay --ci`,
  triggered on-commit/PR/schedule, gates on the exit code, publishes the report as an artifact; connecting = build/deploy
  a preview URL → `--replay --ci` → publish the HTML/JSON report. (b) **Without CI:** a k8s **CronJob** (M5, scheduled —
  this *is* the "no-CI" automation), **cron / systemd-timer** (bare metal), a **git-hook** (pre-push), or simply
  **manually** `agentctl run …`. The trigger/runner is a swappable adapter (GAP-M9-08); CI is just one option, not required.
- **Monorepo is correct — don't split.** A polyglot product (Go+Python+TS) with shared contracts (proto/MCP) must
  version **atomically**; separate repos → version skew across gRPC/MCP, a triple-release nightmare. One proto source,
  one CI, one release. Extract a component only if it becomes an independent product; not now.

## Sub-milestones (proposed sequencing by value/risk)
- **M9.1** `fill`/`type`/`press`/`select` + auth (storageState + login-as-test) — *minimum for a live test.*
- **M9.2** `GoalPlanner` (NL→plan, explore-first grounding) + the `--mode` switch.
- **M9.3** Chat UX: non-MCP HTTP/gRPC control API + an OSS front in DH (plus the MCP path via M7).
- **M9.4** In-app tabs (perception) + browser multi-tab/context.
- **M9.5** Browser trace-header injection (backend correlation).
- **M9.6** headed + CDP-attach modes (foundation for the extension).
- **M9.7** Pluggable adapters (auth/deploy/model) — universality.
- **M9.8 (branch 2)** Browser extension + co-pilot takeover/return.

## ADRs (of this contract)
- **ADR-022** Goal-directed / NL authoring via explore-first grounding (new `GoalPlanner`).
- **ADR-023** Dual chat access: MCP (M7) + a non-MCP HTTP/gRPC control API.
- **ADR-024** Browser execution modes: own-headless → headed → CDP-attach → co-pilot.
- **ADR-025** Universality via pluggable adapters (auth/deploy/model/backend).

## Out of scope (this contract = design freeze)
Implementation (the M9.x sub-milestones). Testing backend APIs without a UI (Sentinel is a UI agent).
The concrete OSS chat choice (Open WebUI vs custom) — decided in M9.3.
