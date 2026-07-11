# Contract M14 — Rich AG-UI co-pilot (in-house vanilla) + split Settings|Tests + wiring scenarios/tests/chats + full auto-HITL

> 🌐 [Русский](M14_CONTRACT.md) (primary version) · **English**

> **Status**: **Design frozen (ADR-052 + new ADR-055)** → **as-built contract (in progress)** · **Date**: 2026-07-04
> **Covers**: M14 = the second step of the Rich-UI/Persistence/Metrics epic (ADR-049..053). On top of M13's persistence it builds a **sovereign vanilla AG-UI co-pilot**, splits the setup UI into **Settings | Tests**, wires the `scenarios`/`tests`/`chats` domains to real callers, and adds full **auto-escalate-to-HITL**.
> **Key decision (ADR-055)**: we write the co-pilot **ourselves (in-house vanilla JS)** as the single sovereign UI — **CopilotKit is removed from the delivery path** (§0 BUILD-ONLY + air-gapped), `frontend/` is frozen. This refines ADR-052 ("frontend/ → SPA") and retires the delivery role of ADR-044.
> **Scope boundary**: the `results`/`metrics` panels in the Tests view are **stubs** → filled in by **M15** (ADR-051, native charts). Postgres → M13-service (M11). `strict structured-outputs` → a separate mini-milestone M-STRUCTURED-OUT.

---

## 1. Why

M13 delivered persistent, indexable state (5 store-gateway domains), but:
- **`scenarios`/`tests`/`results`** and the read-back path for **`chats`** (`GetChat`/`ListChats`) — RPC+schema+tests exist, but **there are no production callers or HTTP surface**: the UI has nothing to call beyond `runs`;
- the **UI** (`docs/index.html`) has no **library/history** — only the last run (`bLastRunId`/`chLastRunId`);
- **WS `/v1/stream`** carries only recorder-ingest (client→server) + takeover/return control frames — **there are no server→client AG-UI events**;
- **takeover** today is only initiated by a human/orchestrator — there is no auto-escalation on a run of failures.

M14 closes this: an HTTP surface for the domains, a live AG-UI timeline on top of the R3 WS, a Settings|Tests split, entity scenario→test promotion, auto-HITL. User requirement: **functionally not less than Open WebUI + CopilotKit** (parity core in M14; breadth across milestones).

## 2. AG-UI event schema + WS transport (frozen here)

**Envelope**: `{"type":<event>, "run_id":<id>, "seq":<int>, "ts":<iso8601>, "data":{…}}`

| type | data | emitter |
|---|---|---|
| `run.started` | `{mode,target,planner}` | brain (perceive) |
| `state.transition` | `{from,to}` | brain (perceive, verify) |
| `step.progress` | `{n,total,desc}` | brain (act) |
| `tool.call` | `{name,args_summary}` | brain (act) |
| `heal` | `{step,strategy(L1–L6),ok}` | brain (heal) |
| `hitl_needed` | `{reason,count}` | brain (checkpoint auto-arm) |
| `verdict` | `{verdict,exit_code,healed,failed}` | control-API/brain (report) |
| `run.finished` | `{exit_code,state}` | control-API — **emitted** (the finish goroutine injects an `@@AGUI` line after the brain's stdout, before `finish()`; `seq` omitted — a separate un-ordered space; failed-spawn → `exit_code:-1` (disambiguated by `state`: signal-kill=`done`, spawn-fail=`failed`); typed for WS, a raw line inside a `log` event for SSE) |
| `log` | `{line}` | passthrough of raw stdout |

**Transport (an R3 add-on, NOT the one-shot ADR-041 shim)**: on top of the existing WS `GET /v1/stream`. The client connects with `?run_id=<id>` (charset `validRunID` — the same charset guard as `?session=`; `run_id` is used as an `s.runs` map key / JSON field and never flows into a path, so no `filepath.Base` is needed) → the server **subscribes** the socket to that run's `runStream` → pushes envelopes as `wsOpText` frames. Client→server takeover/return unchanged — **duplex on one socket**.

> **A slice, not full scope**: M14 pulls forward from M9-LIVE only the **`run_id` subscription** (one tab ↔ one run). **Full cross-run ownership authorization** (a socket may only address its own runs) remains **M9-LIVE** — today any authed client can address any run_id (comment in `ws.go`), acceptable for single-user localhost.

**Event source (in-band, reusing the existing capture path)**: the brain prints lines prefixed `@@AGUI <json>` to stdout at graph nodes; the control-API consumer recognizes the prefix → forwards `data` as a typed event; other lines → a `log` event. This reuses the whole `lineWriter`→`runStream` path (main.go:138) — **no new transport is needed**, and `step/tool/heal/hitl_needed` flow uniformly. The control-API additionally injects the events it knows on its own (`run.started`/`run.finished`/`verdict`). *Fallback if in-band proves fragile:* a separate NDJSON side-channel from the brain (like recorder-ingest, inverted).

## 3. HTTP surface for the domains + scenario persistence (control-API)

New token-gated + CORS routes through the **existing fail-open store-gateway client** (`cmd/control-api/store.go`):
- `GET /v1/scenarios[/{id}]` → `ListScenarios`/`GetScenario`
- `GET /v1/tests[/{id}]` → `ListTests`/`GetTest`
- `POST /v1/tests/promote` `{scenario_id,name,schedule?}` → `PromoteTest` (freezes `plan_hash`)
- `GET /v1/chats[/{id}]` → `ListChats`/`GetChat`
- `DELETE /v1/{scenarios|tests|chats}/{id}` → new `Delete*` RPCs (under the single-writer `s.mu`) — library/conversation management

**Scenario persistence (wire `SaveScenario`)**: on the finish goroutine (`main.go:397`, next to `UpsertRun`), if `artifactDir` contains `scenario.json` → read it → `SaveScenario` (**`plan_hash` is taken from the artifact**, `brain/__main__.py:46` — not recomputed). This wires the `scenarios` domain to a real caller.

**Fallback**: as in M13 — gateway unreachable → reads fall back to empty/in-memory, persistence is silently skipped, control-API does NOT crash (fail-open).

## 4. brain: AG-UI emission + full auto-HITL

- **`brain/state.py`**: RunState += `consecutive_heal_failures:int`, `failed_steps:int`, `agui_seq:int` (default 0).
- **`brain/agui.py`** (new, pure/offline): `emit(type, run_id, seq, **data)` → `print("@@AGUI "+json.dumps(...))` with strict escaping.
- **`brain/graph.py`**: emission at the nodes (perceive→`run.started`/`state.transition`; act→`tool.call`/`step.progress`; verify→`state.transition`; heal→`heal`; report→`verdict`).
- **auto-HITL**: increment `consecutive_heal_failures` in the heal node (L1–L6 miss) + `failed_steps` in the verify node (`_verify_ok=False`); reset consecutive on any success. In `route_checkpoint` (`graph.py:357`): `if consecutive_heal_failures >= SENTINEL_AUTO_HITL_THRESHOLD (env, 0=off): arm _takeover_armed` + `emit hitl_needed{reason,count}`. **Reuses the existing `_takeover_armed` latch** (state.py:55 / graph.py:362) — this is a new *reason* to arm takeover, not a new pause machine. The signal reaches the UI over the AG-UI channel (§2).

The counters also double as **substrate for M15 metrics** (peak `consecutive_heal_failures`, an `auto_hitl_triggered` flag → the `metrics` domain).

## 5. Frontend: vanilla AG-UI co-pilot (parity core) + Settings|Tests

The single sovereign UI is `docs/index.html` (air-gapped, zero-dep, `file://`-safe, bilingual via `data-lang`+`bi(ru,en)`). `frontend/` (CopilotKit) is frozen as a non-maintained reference.

- **Tab mechanism** Settings|Tests (small JS/CSS show/hide — today nav is anchor-only).
- **Settings** = re-parent `#connect`+`#build` + editing **model-per-role / planner / mode / budgets** (temperature=0 **read-only** + a "determinism" label).
- **Tests**:
  - **Library**: list of scenarios/tests · **promote** · pass/fail history · delete/rename · search;
  - **Run history**: list of runs + a ▶/🔁/📌 launcher (reuse `from_run`);
  - **Chats mgmt**: list of conversations · rename · delete · search;
  - **Live AG-UI timeline**: typed events · state chips · a **`hitl_needed` banner + a "take control" button** (sends a takeover frame) · co-pilot launches runs/promote;
  - **Rich chat render**: minimal hand-rolled markdown+code (no CDN);
  - the `results`/`metrics` panels = **stubs** ("M15").

### Parity matrix (≥ OpenWebUI + CopilotKit)
| Capability | Our equivalent | Milestone |
|---|---|---|
| Generative UI (tool-call rendering) | AG-UI timeline | **M14** |
| Agent state (useCoAgent) | state chips | **M14** |
| Human-in-the-loop | takeover/return + auto-HITL | **M14** |
| Frontend actions | co-pilot launches runs/promote | **M14** |
| Streaming chat + markdown/code | #chat + SSE + hand-rolled render | **M14** |
| Conversation mgmt (list/rename/delete/search) | chats domain + delete RPC | **M14** |
| Prompt library / presets | scenarios/tests library | **M14** |
| Model switch + params in UI | Settings (per-role/budgets) | **M14** |
| Model management / model pulling | — | → M-AUTOPILOT-LOCAL |
| Multi-user / RBAC | bearer token (single) | → M9.7 |
| RAG / doc upload / multimodal input | N/A for this domain (a live-app pursuit) | — |

## 6. ADR-055 — why our own vanilla co-pilot, not CopilotKit

CopilotKit (npm/React + Node runtime) can only ever be a **dev convenience**: it requires a build toolchain + registry → which contradicts air-gapped sovereignty (ADR-049/053, "download a release → run offline"). Vanilla `docs/*` **must** carry the whole feature set independently. That makes maintaining CopilotKit pure tax (version drift + dual-UI parity). ADR-055: **in-house vanilla AG-UI co-pilot = the sovereign single UI; CopilotKit is removed from the delivery path; `frontend/` is frozen**. Bonus: **removes GAP-SEC-002** (the npm supply chain disappears along with CopilotKit). We define the AG-UI protocol (event schema, §2) ourselves — we just consume it in vanilla rather than through a kit.

## 7. Deferred
- **M15**: wiring the `results`/`metrics` domains + native charts into the Tests panels (stubs today).
- **M9-LIVE**: live e2e AG-UI (a real browser) · full `run_id`↔session ownership authorization · a live check of auto-HITL.
- **replay/baseline AG-UI + auto-HITL**: today AG-UI emission + auto-HITL are wired only for the **graph modes** (explore/goal/describe/chat via `build_graph`); the `run_replay` path (where real L1-L6 healing with a confidence gate happens) → **follow-up** (validated live in M9-LIVE). The Live timeline degrades to a `log` view for a replay/baseline run (not rich chips).
- **M-STRUCTURED-OUT** (right after M14): strict `tool_use`/`json_schema` instead of `find('{')` parsing.
- **M-INSTALL / M-AUTOPILOT-LOCAL** (after the epic): self-installer · hw-probe→sizing→ollama-deploy + UI model management.
- **M13-service** (M11): Postgres/migrations/TCP.

## 8. Acceptance criteria
- [ ] AG-UI event schema frozen (this contract) + **ADR-055** in ARCHITECTURE §3/§6.
- [ ] WS `/v1/stream` server→client channel: mutex-guarded writer goroutine, `?run_id=` subscription, `@@AGUI` consumer; recorder backward compatibility; httptest (`-race`).
- [ ] HTTP surface for `scenarios`/`tests`/`chats` (+promote, +delete) via the fail-open store client; scenario persistence on finish (`SaveScenario`); httptest.
- [ ] brain: AG-UI emission at the nodes + full auto-HITL (counters + auto-arm `_takeover_armed` + `hitl_needed`); offline test (`test_m14_agui_offline.py`); threshold=0 → byte-identical.
- [ ] vanilla co-pilot: Settings|Tests + library/promote/history/chats-mgmt + live AG-UI timeline + `hitl_needed` banner; bilingual; `frontend/` frozen (ADR-055).
- [ ] Gates green (go build/vet/race/gofmt · pytest offline+m14_agui · bilingual · gitleaks); **adversarial-verify (sonnet)**.
- [ ] Docs sync: contract(+en) · ADR-055 · COPILOT(+en) · FILEMAP · GAPS (new auto-HITL gap-id) · BACKLOG — bilingual.
- [ ] **Live e2e — outside M14** (M9-LIVE): real browser, ownership auth, live auto-HITL pause.

> **Anti-hallucination:** M14 implements the frozen ADR-052; **ADR-055** was introduced only because of a deliberate deviation (dropping CopilotKit) — not a "new feature" but a refinement of the delivery path. The `results`/`metrics` panels are marked as stubs so downstream (M15) doesn't mistake them for finished work. The `?run_id=` subscription is NOT full ownership authorization (that is M9-LIVE).
