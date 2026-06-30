# Sentinel — Co-pilot: vision, status, roadmap

> 🌐 [Русский](COPILOT.md) (authoritative) · **English**

> **ADR-046** · **Date**: 2026-06-29 · **Status**: vision + roadmap (single source of truth)

This document reconciles Sentinel's **full co-pilot vision** with the **actual state** and the **wave plan**
(mine + contributor @0xCoDSnet). It resolves the accumulated "expectation ↔ reality" desync (multi-turn chat,
run-inside-the-tool, takeover). Statuses here are honest: **DONE / scaffold / design-only / not-built**.
Authoritative detail: `ARCHITECTURE.md` §3/§6, `BACKLOG.md`, `GAPS.md`.

## 1. End goal and layers

**Goal:** a tool where a test is **described in words**, Sentinel **explores the app itself** (optionally
live in a visible browser), **assembles a replayable scenario**, **runs/re-runs it right there**, lets you
**correct it in dialogue**, and on hard spots **hands control to the human and takes it back** (co-pilot).
CI export is **secondary** (a bonus); **working inside the tool** is primary.

| Layer | What it gives | Implementation |
|-------|---------------|----------------|
| **Chat authoring** | describe/goal → grounded `scenario.json` | brain `GoalPlanner`/`DescribePlanner` + vanilla chat + shim |
| **In-tool run console** | ▶ run / 🔁 re-run / 📌 baseline **in the UI** + verdict | control-API replay/baseline (R1) + vanilla buttons |
| **Multi-turn dialogue + correction** | context across messages, mid-run fixes | brain conversation-state on the checkpointer — **R2a backend ✅ (ADR-048); UI R2b** |
| **Co-pilot takeover/return (F4)** | agent ↔ human on one live session | MV3 extension + `chrome.debugger` (0xCoDSnet) + brain interrupt/resume (R3) |
| **MV3 recorder** | record human actions → scenario | MV3 content-script → `/v1/stream` → `reconcile` (0xCoDSnet) |
| **Rich front (opt.)** | streaming/HITL/generative-UI | AG-UI/CopilotKit `frontend/` (dev) — **vanilla `docs/*` stays primary, air-gapped** |

## 2. Two evolution axes

**§F — browser-driving modes** (`M9_CONTRACT.md §F`):
F1 **own-headless** (since M0, always) → F2 **headed/visible** (`PW_HEADLESS=0`) → F3 **CDP-attach** to the
user's Chrome (`PW_CDP_ENDPOINT`) → F4 **co-pilot takeover/return**.
**Status:** F1 ✅ · **F2 ✅ / F3 ✅** (M9.6/ADR-036/037, offline; live-verify pending) · **F4 = design-only** (M9.8).

**Authoring evolution:** one-shot (one NL string → one run → `scenario.json`) → **multi-turn** (dialogue with
context + correction). **Status:** one-shot ✅ (M9.2a/b/M9.3/M12) · **multi-turn: backend ✅** (M9.10/R2a, ADR-048 — checkpointer-resume `conversation_id`→`thread_id` + a `messages` add_messages channel + chat-mode conditional-entry refine; offline-verified) · **UI panel R2b** (the seam is the LangGraph checkpointer).

## 3. Feature inventory (honest status)

| Feature | Home (milestone/ADR) | Status |
|---------|----------------------|--------|
| Deterministic explore→`plan.json` (coverage, `plan_hash`) | M1/M3 | ✅ DONE |
| Goal/describe → `scenario.json` (NL authoring, **one-shot**, grounded) | M9.2a/b, ADR-027/028 | ✅ DONE offline (live pending) |
| Replay (execution/verdict 0/1/2/3, LLM-free, self-heal) | M3 | ✅ DONE (CLI/CI) |
| Golden-diff (a11y+screenshot) / self-heal (L1–L6 + a11y re-ground) | M2/M3; visual-heal M5 | ✅ DONE; visual set-of-marks heal = PoC-gated |
| Opt-in visual-authoritative flip | ADR-042 | ⚙️ flag (default-off; default-on → M9-LIVE proof) |
| Vanilla chat-front (air-gapped, **one-shot**) | M9.3-tail/ADR-040 | ✅ DONE (`docs/chat/`, `docs/index.html#chat`) |
| OpenAI-compat shim (Open WebUI/SDK = **client**, "as a model") | M12/ADR-041 | ✅ DONE (`/v1/chat/completions`) |
| **Replay/baseline INSIDE the UI** | M9.3 "out of scope" → **R1/M9.9** | ✅ DONE — R1a backend (ADR-047) + R1b UI ▶/🔁/📌 in `#build`/`#chat`/`chat/`/`setup/` (GAP-M9-16) |
| **Multi-turn chat / context / mid-run correction** | "brain-extension" → **R2/M9.10** | ⚙️ backend ✅ R2a (ADR-048, offline) → UI R2b (GAP-M9-17) |
| Headed / visible browser (F2) | M9.6/ADR-037 | ✅ DONE offline (live pending) |
| CDP-attach to the user's Chrome (F3) | M9.6/ADR-036/037 | ✅ DONE offline (live pending) |
| **Co-pilot takeover/return (F4)** | M9.8/ADR-039 | ❌ design-only (extension+brain) |
| WS transport client→server (`/v1/stream`) | M9.8-prep/ADR-043 | ✅ DONE |
| SSE server→client + artifact-fetch | M9.3-tail/ADR-040 | ✅ DONE |
| AG-UI/CopilotKit rich front | M9.8-prep/ADR-044 | 🧪 scaffold (`frontend/`, dev-only, not air-gapped, not in CI) |
| **MV3 recorder extension** | M9.8/ADR-038 (GAP-M9-13) | ❌ not-built → @0xCoDSnet (#42-47) |
| LiteLLM opt-router · MCP-Inspector | ADR-045 | ✅ DONE (config/docs) |
| In-app tabs + multi-tab (M9.4) · traceparent (M9.5) | ADR | ✅ DONE offline (live pending) |
| Pluggable adapters (auth/deploy/model/backend) | M9.7/ADR-025 | ⚙️ model/backend ✅ (ADR-045); **auth** partial ✅ (storageState/login-as-test, M9.1/ADR-026); OIDC/Keycloak + **deploy adapter not-built** |
| Security module (XSS/CSRF/IDOR…, authz-gated) | M10/GAP-M9-11 | ❌ design-only |
| **Rich-UI + persistence + metrics-in-UI** (two-tier service) | M13-15 / ADR-049..053 | 📋 planned (docs-frozen; after R2b→R3) |
| Accuracy (Langfuse/DSPy) | roadmap | ❌ not-built (after user tests) |

## 4. Agreements (principles)

1. **In-tool-first.** Run/re-run/baseline **inside the tool** are primary. CI export (Jenkins/GitLab — `docs/ci-templates/`, already shipped) is secondary/bonus.
2. **Vanilla `docs/*` = primary UI** (air-gapped, zero-build, `file://`-safe). **AG-UI `frontend/` = dev rich-front** (not air-gapped, not in CI) — a parallel option, not a replacement. **Evolution (epic M13-15, ADR-049..053):** profiles = topology-not-features (both full + air-gapped); AG-UI → a **full rich front** (M14) over the R3-WS in BOTH profiles; vanilla `docs/*` = the air-gapped variant of the same set; metrics **self-contained** (ADR-051).
3. **Open WebUI = a compatible client** of the OpenAI-compat shim (optional, you run it), **NOT the co-pilot**. Takeover/co-pilot comes from the **extension (`chrome.debugger`) + brain**, not a chat UI.
4. **Multi-turn is in progress** (M9.10): backend ✅ (R2a, ADR-048 — checkpointer-resume), UI = R2b. The seam exists (checkpointer).
5. **F4 is a joint milestone:** the extension/CDP/panel — @0xCoDSnet (#47); brain interrupt/resume + WS signals — mine (R3).
6. **Determinism boundary:** golden replay is headless-only (ADR-037); headed/CDP are observation modes.

## 5. Wave roadmap

### My waves (`control-API` / `brain` / vanilla-UI) — order R1 → R2 → R3
| # | Milestone | Content | Closes |
|---|-----------|---------|--------|
| **R1** | **M9.9 In-tool run console** | control-API `mode=replay\|baseline` + `from_run:<run_id>` (whitelist+traversal guard; `--replay --plan`/`baseline`) + `config-schema.modes`; ▶/🔁/📌 + verdict in vanilla-UI (`#build`/`#chat`/`chat/`/`setup/`); httptest — **✅ DONE (R1a backend + R1b UI)** | GAP-M9-16 |
| **R2** | **M9.10 Multi-turn authoring** | brain `chat` `RUN_MODE` — resume from the checkpointer by a stable `conversation_id`→`thread_id`; a `messages` add_messages channel in `RunState`; conditional-entry refine; agentctl/control-API `conversation_id` — **R2a backend ✅ DONE (ADR-048)**; multi-turn panel = **R2b** | GAP-M9-17 |
| **R3** | **M9.8 F4 takeover (brain-side)** | brain interrupt-on-takeover / resume-on-return (LangGraph interrupt+checkpoint); WS `takeover/return/state-sync` signals over `/v1/stream` | GAP-M9-18 (+½ GAP-M9-15) |

### Epic: Rich-UI + Persistence + Metrics (M13–M15, ADR-049..053) — after R2b→R3
**Two-tier:** profiles = **TOPOLOGY, not features** — both carry the full feature set (chat/copilot/UI/replay/library/metrics) and both are **air-gapped**-installable. **Control-plane** (always-on: control-API+store-gateway+DB) **vs run-unit** (ephemeral: brain+pw-executor, spawned for 1 run → exit). CronJob (ADR-017) = a scheduled-run-unit trigger, **not** the service deployment. Profiles: **standalone** (1 host/compose/SQLite) · **service** (K8s/Postgres/HA) — both air-gapped (ADR-053).

| # | Milestone | Content | ADR |
|---|-----------|---------|-----|
| **M13** | Persistence / Service layer | store-gateway N domains (hybrid SQLite/Postgres) + control-API CRUD + persist runs + full=service mode | ADR-049/050 |
| **M14** | Rich AG-UI (full) + split setup-UI | SPA on AG-UI events over the R3-WS (not the one-shot shim); Settings \| Tests (library·launch·history·viewing·chats·metrics) | ADR-052 |
| **M15** | Metrics & dashboards-in-UI | run metrics → DB (M13) → **native charts** in the SPA; Prom/Grafana = optional export | ADR-051 |

**Data model (5 domains, owner = store-gateway, hybrid):**
| Domain | Contents | Relation to current |
|---|---|---|
| scenarios/tests | scenario_id, name, target, steps, plan_hash, tags · "test" = scenario + golden + schedule | `scenario.json`/`plan.json` in `runs/` → indexed |
| runs | run_id, conversation_id, mode, target, exit_code, times, verdict | in-memory control-API map → persisted |
| chats | conversation_id, turns, messages | projection of R2a `state/conversations.db` (not a duplicate) |
| results | heal-report.json / report.json | files → index+view |
| metrics | pass/heal/fail/regression, coverage, duration, cost, flake trends | from results → time-series for native charts |

**Why after R2b→R3:** rich AG-UI without R3-WS = the one-shot shim again; M14/M15 ⊃ the M13 store; the chats domain is partly ready (R2a).

### @0xCoDSnet waves
| Track | Issues | Dependencies |
|-------|--------|--------------|
| Security | #36 (trace-retention #34-pt3 + #33/#35) · #37 (prompt-sanitization) · #38 (lockfile+SBOM, GAP-SEC-002) | — |
| MV3 extension | #42 skeleton → #43 SW-WS → #44 recorder → #45 panel → #46 record→scenario → #47 takeover | WS `/v1/stream` ✅ + `frontend/` ✅; **#47 ↔ my R3** |

### Beyond (existing)
M9.7-remainder (auth/deploy adapters) · M10 security module · M11.1 release (lockfile/SBOM/signing — overlaps #38) · M11.2/4/5 · **M9-LIVE** (live runs — needs «go»+key+browser) · Langfuse/DSPy.

## 6. How the desync points close
- "Run/re-run inside the tool" → **R1** ✅ DONE (GAP-M9-16).
- "Context, not one-shot, mid-run correction" → **R2a backend ✅ DONE** (ADR-048); UI = **R2b** (GAP-M9-17).
- "Takeover/co-pilot/partnership" → **R3** (brain) + #47 (extension) = F4 (GAP-M9-15/18).
- "Show in the browser what it's doing" → **already exists** (F2 headed / F3 CDP-attach, M9.6); live-verify = M9-LIVE.

## See also
[`M9_CONTRACT.md`](M9_CONTRACT.md) (§F evolution, §B/§L authoring) · [`M9.8_CONTRACT.md`](M9.8_CONTRACT.md) (extension+F4) ·
[`M9.6_CONTRACT.md`](M9.6_CONTRACT.md) (F2/F3) · [`M12_CONTRACT.md`](M12_CONTRACT.md) (shim, one-shot) ·
[`ADAPTERS.md`](ADAPTERS.md) (LiteLLM/MCP-Inspector) · [`ARCHITECTURE.md`](../ARCHITECTURE.md) · [`GAPS.md`](../GAPS.md) · [`THREAT_MODEL.md`](THREAT_MODEL.md).
