# Contract M12 — unified config+chat console + OpenAI-compat shim (variant i)

> 🌐 [Русский](M12_CONTRACT.md) · **English**

> **Status**: Phase-1 (shim) — ✅ **DELIVERED**; Phase-2 (unified page) — ✅ **DELIVERED** · **Date**: 2026-06-28
> introduces **ADR-041** (OpenAI-compat shim) · builds on ADR-032 (control-API security) + ADR-040 (SSE machinery)

---

## Goal

One GitHub Pages page where **chat and run-control are "one model"**: the user describes a test in words →
Sentinel authors a scenario and starts a run → progress streams in → the resulting `scenario.json` downloads;
alongside it, a detailed configurator (RunConfig YAML/env + calculators). The foundation is **variant (i): an
OpenAI-compatible shim** on the control-API, so ANY OpenAI client (Open WebUI, DeepSeek/Mistral clients, SDKs,
our page) drives Sentinel "as a model".

**Decisions (user-confirmed):**
- **(i) shim now** (foundation protocol); **(iii) AG-UI/CopilotKit later** (rich co-pilot frontend, M9.8+ phase).
- **Chat v1 = one-shot**: brain runs one pass (one message = one run → `scenario.json`). Multi-turn = a separate
  brain-extension milestone.
- This milestone is **before** M9.8-impl. The M9.8 extension transport = **WS** (native-messaging = documented alt).

## Phase-1 — OpenAI-compat shim (✅ DELIVERED, ADR-041)

`cmd/control-api`: a new **`POST /v1/chat/completions`** (stdlib, no new deps; token-gated via `s.authed`, CORS
via `s.cors`). The `handleCreateRun` body is extracted into a reusable **`spawnRun(req) *run`** (build args +
goroutine + `runStream`), called by both `POST /v1/runs` and the shim.

**Chat-turn → run mapping:**
- **Mode**: from `model` (`sentinel` → describe · `sentinel-goal` → goal · `sentinel-explore` → explore) OR a
  leading `goal:`/`explore:`/`describe:` prefix (the prefix wins). Default describe.
- **Target**: the most recent (last) `http(s)://`/`file://` URL across all messages (a `target: <url>` line is supported).
- **Instruction**: the last user message (minus `target:` lines).

**Response (OpenAI wire):**
- `stream:true` → SSE `chat.completion.chunk` frames: `delta.role` → per-line `delta.content` (run log from
  `runStream`) → a final `delta.content` with the verdict → `finish_reason:"stop"` → `data: [DONE]`.
- `stream:false` → a single `chat.completion`: `message.content` = log + verdict (by exit code: 0 pass / 1 found
  a problem / 2 visual-golden / 3 config-error) + the `scenario.json` content.
- No target/instruction → a friendly chat-shaped guidance reply (200), not an HTTP error.

**Security**: the same bearer token + CORS allowlist + localhost bind (ADR-032). Only the known `agentctl` is
spawned, the target is validated. Gates: `go build/vet/test -race` + `gofmt` + 5 httptest (`parseChatInstruction`
unit · 403 without token · non-stream · stream · no-target) + a live curl smoke (stream + non-stream + 403).

## Phase-2 — unified `docs/index.html` (✅ DELIVERED)

**Delivered** (`docs/index.html` 905→1469; calculators untouched): 3 sections added to the neon hub — **#connect** (control-API URL+token, memory-only), **#build** (RunConfig builder: YAML/env/cmd + download + ▶Run), **#chat** (describe/goal/explore → SSE-via-fetch → verdict + download `scenario.json`); a shared SSE/poll driver; bilingual (`data-lang`/`setLang`/`sentinel_lang`); air-gapped; `setup`/`chat` kept as standalone advanced deep-links; an OpenAI-shim (`/v1/chat/completions`) note in the chat section; `node --check` clean.

Evolve the neon hub (`docs/index.html`): add two control-API-driven sections — **(a) RunConfig builder** (port
`docs/setup` `render()` → YAML/env/cmd + download) and **(b) chat panel** (port `docs/chat` `streamEvents()`
SSE-via-fetch + transcript + artifact download). A shared connection panel (URL + bearer, memory-only) feeds both
the builder's live-run and the chat. Bilingual (`data-lang`/`setLang`/`sentinel_lang`), one neon palette,
air-gapped. `docs/setup`+`docs/chat` stay as standalone "advanced" deep-links. The hub calculators stay as-is.

## Roadmap after (user order)
M12 → close tails (M9-LIVE; remaining GAPs; GAP-RISK-009) → adopt LiteLLM (optional router) + MCP Inspector →
**M9.8-impl** (extension + WS) + AG-UI/CopilotKit (rich co-pilot) → Langfuse/DSPy after user tests.

## Deferred
- Full conversational chat (multi-turn) — needs a brain change (conversation state).
- Passing budget flags through `POST /v1/runs` (agentctl takes budgets via env/`--run-config`, not run flags) —
  the builder keeps budgets in the YAML/env; M12 leaves agentctl untouched.
