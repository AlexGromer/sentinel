# M6 Contract — "Provider-Agnostic Brain" (frozen 2026-06-25)

> 🌐 [Русский](M6_CONTRACT.md) (основная версия) · **English**

Goal: remove the brain's lock-in to a single provider (Anthropic). The **planner** (explore) and
**heal** (text re-grounding + set-of-marks vision) nodes now call the LLM through a provider-neutral
`LLMBackend`, so Sentinel runs on Anthropic OR any OpenAI-compatible endpoint (ChatGPT, DeepSeek,
Qwen DashScope-compat, Gemini OpenAI-compat, OpenRouter, Ollama, vLLM), selected **per role** via env.
This realizes the first direction of the user ask (*consume* — our brain runs on any model); the
second (*be-driven* by hosts) is M7/ADR-020.

## Key lever (low-risk swap)
`brain/planner.py` and `brain/healing.py` already had a single LLM call point each. M6 introduces
`brain/llm.py` and swaps only those call-sites. `graph.py` / `replay.py` / `__main__.py` are
**unchanged**: the planner contract (`name="llm"`, a string `model`, `propose → {…, tokens}`) is preserved.

## Interface (`brain/llm.py`)
- `LLMResult{text, prompt_tokens, completion_tokens, model?}` — normalized reply. `model` carries the
  provider's real model (for M7 sampling); for fixed backends it equals `backend.model`.
- `LLMBackend` (Protocol): `name` (provider), `model` (per-backend, immutable), `supports_vision`,
  `complete(prompt, *, max_tokens, temperature)`, `complete_vision(prompt, image_b64, *, …)`.
- `AnthropicBackend` — native (`messages.create`), `supports_vision=True`.
- `OpenAICompatBackend` — `chat.completions.create` via `base_url` + key; vision opt-in
  (`image_url` data-URI); for keyless local endpoints (Ollama) key = `"noauth"`.
- `make_backend(role)` — factory. Returns **`None`** when key/SDK is absent ⇒ the caller keeps the
  offline fallback (heuristic / L1–L6). **Never raises** (any import/config problem → `None`).

## Per-role env (precedence: `LLM_<KEY>_<ROLE>` > `LLM_<KEY>` > default)
| Concern | Global | Per-role override | Default (as before M6) |
|---------|--------|-------------------|------------------------|
| provider | `LLM_BACKEND` | `LLM_BACKEND_PLANNER` / `_HEAL` | `anthropic` |
| model | `LLM_MODEL` | `LLM_MODEL_PLANNER` / `_HEAL` | `claude-opus-4-8` / `claude-sonnet-4-6` |
| base_url | `LLM_BASE_URL` | `LLM_BASE_URL_PLANNER` / `_HEAL` | — |
| api_key | `LLM_API_KEY` | `LLM_API_KEY_PLANNER` / `_HEAL` | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` |
| vision | `LLM_VISION` | `LLM_VISION_HEAL` | provider default (anthropic=on) |

**With zero env set the behaviour is byte-for-byte as before M6** (Anthropic, Opus planner / Sonnet
heal, keyed off `ANTHROPIC_API_KEY`, falling back to heuristic / L1–L6 without a key). Preserves ADR-009.

## Vision gating
Tier-7 set-of-marks heal needs a vision model; the gate is `use_visual AND backend.supports_vision`.
A text-only provider (DeepSeek-V3, Qwen-text) skips Tier-7 → degrades to the deterministic L1–L6.

## Determinism (ADR-019)
The LLM path is **best-effort**: different models → different plans → different `plan_hash`; the
model provenance is **not stored**. `HeuristicPlanner` stays the deterministic anchor; golden
baselines stay heuristic-only; replay is LLM-free. `canonical_plan_hash` / `DETERMINISM.md` / the
baseline format are **unchanged**.

## What changed
- Added `brain/llm.py`. `planner.LLMPlanner` / `healing.HealingEngine`: `_client`/`_llm` → `_backend`.
- `otel.set_llm_tokens(sp, result)` — takes a normalized `LLMResult` (None-span tolerant).
- `pyproject.toml`: added `openai` (import-guarded, optional at runtime).

## Gate
- **Offline** (exec-gating: no network/binaries, `FakeBackend`): `test_b1_offline` (8) +
  `test_m5_offline` (4) green; regress `test_m3` / `test_m4` / `test_m4b` green; default path byte-for-byte.
- **Real-provider smoke is user-run** (the environment blocks network). Anthropic (as before, default):
  `ANTHROPIC_API_KEY=… PLANNER=llm …`. OpenAI-compat example (router):
  `LLM_BACKEND=openai LLM_BASE_URL=https://openrouter.ai/api/v1 LLM_API_KEY=… LLM_MODEL=deepseek/deepseek-chat PLANNER=llm`.
  Vision-heal on a different provider: `LLM_BACKEND_HEAL=openai LLM_BASE_URL_HEAL=… LLM_MODEL_HEAL=… LLM_VISION_HEAL=1 HEAL_LLM=1 HEAL_VISUAL=1`.

## VERIFY during real smoke (provider quirks, anti-hallucination)
- `temperature=0` is rejected by some models (OpenAI o-series) → the call-site falls back (OK for best-effort).
- `max_tokens` vs `max_completion_tokens` (o-series) — some endpoints rename it; cover via env/retry if needed.
- `usage` may be absent (Ollama / vLLM / streaming) → counts = 0 (tolerated; the transcript records 0).
- JSON inside \`\`\`fences\`\`\` — the extractor `text[find('{') : rfind('}')+1]` survives.

## ADR
ADR-019 (Accepted). Compatible with ADR-009 (per-role model split) and ADR-011 (pluggable planner).

## Out of scope
M7 MCP-server exposure + `SamplingBackend` (ADR-020). Model provenance in `plan_hash` (user decision:
determinism is not critical for the LLM path).
