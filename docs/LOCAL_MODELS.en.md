# Local and Cloud Models — Selection Methodology

> 🌐 [Русский](LOCAL_MODELS.md) · **English**

> **ADR-029** · **Date**: 2026-06-27 · **Status**: methodology (platform-agnostic)
> **Calculators**: [VRAM](https://alexgromer.github.io/sentinel/calculators/vram.html) · [token-cost](https://alexgromer.github.io/sentinel/calculators/token-cost.html) · [model-selector](https://alexgromer.github.io/sentinel/calculators/model-selector.html) (this document is the authoritative source for their formulas)

---

## Contents
1. [Introduction: models are configuration](#1-introduction-models-are-configuration)
2. [Env profile: how to point Sentinel at any backend](#2-env-profile)
3. [Model catalog by role (PLAN / VISION)](#3-model-catalog-by-role)
4. [Runtime / endpoint catalog (6 categories)](#4-runtime--endpoint-catalog)
5. [VRAM-sizing methodology](#5-vram-sizing-methodology)
6. [Token-cost-per-phase methodology](#6-token-cost-per-phase-methodology)
7. [Anti-hallucination and cutoff](#7-anti-hallucination-and-cutoff)

---

## 1. Introduction: models are configuration

Sentinel is **already** provider-agnostic (M6, ADR-019): three LLM roles — **PLAN** (scenario research/authoring),
**HEAL** (re-grounding a broken locator), and **VISION** (set-of-marks over a screenshot) —
are invoked via `brain/llm.py::make_backend(role)`, which constructs an `AnthropicBackend` (native) **or**
an `OpenAICompatBackend` (any OpenAI-compatible endpoint). The selection is **per-role via env**, **without new
code and without a new "profile" knob** (ADR-029): provider profiles are *documented*, not hard-coded.

**Key properties of Sentinel's workload** (explaining why small local models are suitable):

| Property | Value (verified against code) | Source |
|---|---|---|
| Output type | structured JSON (index-pick / scenario), **not** long-form generation | `brain/planner.py` |
| Output size | PLAN propose ≤ **200** tok · scenario ≤ **800** tok · HEAL-text ≤ **200** · HEAL-vision ≤ **100** | `planner.py:116,177,228,282` · `healing.py:131,176` |
| Input context | ≤ **8000** chars (PLAN-menu) / ≤ **3000** chars (HEAL) ≈ ≤ 2000 / 750 tok | `planner.py:222` · `healing.py:126` |
| Vision input | one PNG (≈1280×720) + tiny marks menu | `healing.py:168` |
| Temperature | **0** (deterministic selection) | `planner.py` / `healing.py` |
| Replay (hot path) | **LLM-free**, 0 tokens | `brain/replay.py` |

Therefore: PLAN needs **reliable instruction-following / structured-JSON**, HEAL needs **re-grounding**
of a broken locator, VISION needs a **VLM that can read numbered marks** on a screenshot. This is
achievable by models from 3–4B and above — see §3 and the VRAM calculator (§5).

> **In-code defaults remain `claude-*`** (`_DEFAULT_MODEL = {"planner": "claude-opus-4-8",
> "heal": "claude-sonnet-4-6"}`, `llm.py:233`). Offline runs use `FakeBackend` (determinism/CI);
> a real local model is **opt-in** via the env profile below. RTX 2060 12 GB is **one example**
> among the 8/12/16/24 GB tiers, not the basis of the methodology.

---

## 2. Env profile

All variables are read by `make_backend` (`llm.py:241–279`). **Priority: role-specific
`LLM_<KEY>_<ROLE>` > global `LLM_<KEY>`.** Roles: `PLANNER`, `HEAL`. Keys: `BACKEND`, `MODEL`,
`BASE_URL`, `API_KEY`, `VISION`, `STRUCTURED`.

| Env | Purpose | Note |
|---|---|---|
| `LLM_BACKEND` | `anthropic` (native) \| `openai` (OpenAI-compat) \| `sampling` (MCP-host, M7) | default `anthropic` |
| `LLM_MODEL` | model id at the provider | for `openai` — **required** |
| `LLM_BASE_URL` | URL of OpenAI-compat endpoint (`…/v1`) | local: Ollama/vLLM/llama.cpp |
| `LLM_API_KEY` | key; for local — any non-empty string (`noauth`) | fallback to `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` |
| `LLM_VISION` | `1` → enable vision on OpenAI-compat HEAL backend | text-only model skips vision |
| `LLM_STRUCTURED` | `1` → strict structured-output (Anthropic tool_use / OpenAI json_schema, ADR-057) on the OpenAI-compat backend | default OFF; otherwise falls back to `complete`+`extract_json` |
| suffix `_PLANNER` / `_HEAL` | overrides the global key for that role | e.g. `LLM_MODEL_PLANNER` |

**Degradation is safe:** when a key/SDK is absent `make_backend` → `None` ⇒ PLAN falls back to
the deterministic `HeuristicPlanner`, HEAL falls back to L1–L6. The run does not crash.

**Example (local Ollama, different models per role):**
```bash
export LLM_BACKEND=openai
export LLM_BASE_URL=http://localhost:11434/v1
export LLM_API_KEY=noauth
export LLM_MODEL_PLANNER=qwen3:14b        # reasoning / structured JSON
export LLM_MODEL_HEAL=qwen2.5-vl:7b       # VLM for re-grounding + set-of-marks
export LLM_VISION=1
```
Ready-made env blocks for each runtime are in §4.

---

## 3. Model catalog by role

> **Verification legend:** ✅ `verified` — name/size confirmed from a primary source during
> research (2026-06-27); ⚠️ `verify-before-use` — partially/not confirmed (post-cutoff —
> check before production use). All benchmarks are linked; figures not confirmed from a primary
> source have been excluded or flagged.

### 3.1 PLAN role — local open-weight LLMs (structured-JSON / reasoning)

| Model | Parameters | Quant | ~VRAM | Context | Verify | Key strengths (brief) |
|---|---|---|---|---|---|---|
| **Phi-4-mini-instruct** | 3.8B | Q5_K_M | ~3 GB | 128K | ⚠️ | Lowest VRAM threshold; SFT+DPO+RLHF for structured output; ARC-C 83.7%, GSM8K 88.6% (model card) |
| **Qwen3-4B** | 4B | Q5_K_M | ~3.5 GB | 32K (131K YaRN) | ⚠️ | thinking/non-thinking in one checkpoint; Apache-2.0; fits any 6 GB GPU |
| **Qwen3-8B** | 8.2B | Q5_K_M | ~6 GB | 32K (131K YaRN) | ⚠️ | GQA (8 KV-heads) → light KV-cache; recommended choice for 8 GB tier |
| **Qwen3-14B** | 14.8B | Q4_K_M | ~9.5 GB | 32K (131K YaRN) | ✅ | 40 layers, 40Q/8KV (GQA); fits in 12 GB; quality gain over 8B; Apache-2.0 |
| **DeepSeek-R1-Distill-Qwen-14B** | 14B | Q4_K_M | ~9.5 GB | 128K | ✅ | CoT reasoning from R1; AIME-2024 69.7%, MATH-500 93.9%. ⚠️ think-tokens consume `max_tokens`; "no system-prompt" — verify compatibility |
| **Qwen3-30B-A3B** (MoE) | 30B / 3B active | Q4_K_M | ~18 GB | 32K (131K YaRN) | ⚠️ | MoE: speed ~3B at quality >14B; all experts in VRAM; for 24 GB |
| **Gemma-3-27B-IT** | 27B | Q4_0 QAT | ~18 GB | 128K in / 8K out | ✅ | Official Google QAT-GGUF (17.2 GB); function-calling + structured output; multimodal (also suitable for VISION) |
| **Mistral-Small-3.2-24B** | 24B | Q4_K_M | ~15 GB | 128K | ⚠️ | IFEval 84.78%, HumanEval+ 92.9% (model card); multimodal. ⚠️ release date `2506`=June 2025 per naming convention, not 2026 |
| **Qwen3-32B** | 32.8B | Q4_K_M | ~21 GB | 32K (131K YaRN) | ⚠️ | Best dense structured-JSON quality on a single 24 GB GPU; for complex `build_scenario` |
| **Llama-3.3-70B-Instruct** | 70B | Q3_K_M | ~33 GB | 128K | ✅ | IFEval 92.1, BFCL-v2 77.3 (model card — highest IFEval in this list). Requires 32 GB+ / 2×GPU |

*Benchmark / sources (primary):* [Phi-4-mini](https://huggingface.co/microsoft/Phi-4-mini-instruct) ·
[Qwen3-14B](https://huggingface.co/Qwen/Qwen3-14B) · [Qwen3 tech report](https://arxiv.org/html/2505.09388v1) ·
[DeepSeek-R1-Distill-14B](https://huggingface.co/deepseek-ai/DeepSeek-R1-Distill-Qwen-14B) ·
[Gemma-3-27B QAT](https://huggingface.co/google/gemma-3-27b-it-qat-q4_0-gguf) ·
[Mistral-Small-3.2](https://huggingface.co/mistralai/Mistral-Small-3.2-24B-Instruct-2506) ·
[Qwen3-32B](https://huggingface.co/Qwen/Qwen3-32B) ·
[Llama-3.3-70B](https://huggingface.co/meta-llama/Llama-3.3-70B-Instruct).

> **IFEval warning:** for the Qwen3 family, IFEval figures circulating in secondary
> sources are **absent** from the tech report (arXiv:2505.09388) — verify against
> [Open LLM Leaderboard](https://huggingface.co/spaces/open-llm-leaderboard/open_llm_leaderboard) before locking them into deployment docs.

### 3.2 VISION role — local VLMs (set-of-marks grounding on screenshots)

| Model | Parameters | Quant | ~VRAM | Context | Verify | Key strengths (brief) |
|---|---|---|---|---|---|---|
| **Qwen2.5-VL-7B-Instruct** | 7.6B* | Q4_K_M | ~7 GB | 32K | ✅ | ScreenSpot 84.7, OCRBench 864, DocVQA 95.7 (model card); dynamic-res tiles for dense marks; Ollama-native |
| **Phi-4-reasoning-vision-15B** | 15B | Q4_K_M | ~11 GB* | 16K | ✅ | ScreenSpot-V2 **88.2** (highest here), OCRBench 76.0 (model card, released 2026-03). Larger/slower — acceptable for heal-only |
| **Qwen3-VL-8B-Instruct** | 8B | Q4_K_M | ~12 GB* | 256K | ⚠️ | released 2025-10-15; 256K context; MMBench figures not confirmed from primary source |
| **InternVL3-8B** | 8B | Q4_K_M | ~7 GB | ~8K | ⚠️ | OCR+GUI; ScreenSpot-V2 81.4% — **not confirmed** (tables in paper are images) |
| **MiniCPM-V-4.5** | 8B | Q4 | ~7 GB | ~8K | ⚠️ | Qwen3-8B + SigLIP2; OpenCompass avg 77.0; OCRBench figure not extracted |
| **MiniCPM-V-4.6** | 1.3B | Q4 | ~3 GB | ⚠️ not confirmed | ⚠️ | released 2026-05-11; 262K context **not confirmed** — do not use this figure until verified |
| **SmolVLM2-2.2B** | 2.2B | Q4 | ~2 GB* | ⚠️ not confirmed | ⚠️ | OCRBench 72.9 (arXiv); ⚠️ 2 GB VRAM contradicts model card (5.2 GB video) |
| **Pixtral-12B** | 12B (+~0.4B vision) | Q4_K_M | ~9 GB | 128K | ⚠️ | ⚠️ CORRECTED: ChartQA **81.8** (not 83.7), MMMU **52.5** (not 62.5) per model card |

*`7.6B`/VRAM marked with asterisk — estimated/not from official spec; see notes in [`#7`](#7-anti-hallucination-and-cutoff).*

*Sources:* [Qwen2.5-VL-7B](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct) ·
[Phi-4-reasoning-vision-15B](https://huggingface.co/microsoft/Phi-4-reasoning-vision-15B) ·
[Qwen3-VL-8B](https://ollama.com/library/qwen3-vl:8b) · [InternVL3-8B](https://arxiv.org/abs/2504.10479) ·
[MiniCPM-V](https://github.com/OpenBMB/MiniCPM-V) · [SmolVLM2](https://huggingface.co/HuggingFaceTB/SmolVLM2-2.2B-Instruct) ·
[Pixtral-12B](https://huggingface.co/mistralai/Pixtral-12B-2409). Grounding leaderboard: [gui-agent](https://gui-agent.github.io/grounding-leaderboard/).

### 3.3 Selection by VRAM tier (quick reference)

| GPU VRAM | PLAN (dense) | VISION |
|---|---|---|
| **8 GB** | Qwen3-8B Q5_K_M / Phi-4-mini | Qwen2.5-VL-7B Q4 / InternVL3-8B |
| **12 GB** (RTX 2060/3060) | Qwen3-14B Q4_K_M | Phi-4-vision-15B Q4 / Qwen2.5-VL-7B Q5 |
| **16 GB** | Qwen3-14B Q6 / Mistral-Small-24B Q4 | any 7–15B VLM |
| **24 GB** | Qwen3-32B Q4_K_M / Gemma-3-27B QAT / Qwen3-30B-A3B | 15B VLM Q5/Q6 |
| **32 GB+ / 2×GPU** | Llama-3.3-70B Q3/Q4 | — |

---

### 3.4 Cloud models — illustrative pricing (⚠ verify; cloud figures re-verified 2026-07-04)

Price source for the cost-explorer (`docs/index.html`). **Editable, illustrative** — NOT a claim of current
cost (cloud pricing drifts). Claude is from the claude-api skill (2026-06-04); others were researched
2026-06-28 from provider pages. Refreshed by CI `prices-refresh.yml` (weekly, via OpenRouter → PR) + the
"Refresh from OpenRouter" button on the page. **Fit** for Sentinel's structured-JSON role is **opinion**, not a benchmark.

| Model | $/1M in | $/1M out | Context | reasoning | vision | fit | Source |
|---|---|---|---|---|---|---|---|
| Claude Opus 4.8 | 5 | 25 | 1M | ✓ | ✓ | high | [anthropic](https://www.anthropic.com/pricing) (skill 06-04) |
| Claude Sonnet 5 | 2 | 10 | 1M | ✓ | ✓ | high | anthropic (verified 07-04; intro 2/10 → 3/15 from 09-01) |
| Claude Haiku 4.5 | 1 | 5 | 200K | — | ✓ | high | anthropic (skill 06-04) |
| GPT-5.5 (flagship) | 5 | 30 | 1M | ✓ | ✓ | high | [openai](https://developers.openai.com/api/docs/pricing) (verified 07-04) |
| GPT-5.4 | 2.5 | 15 | 1M | — | ✓ | high | openai (verified 07-04) |
| GPT-5.4-mini | 0.75 | 4.5 | 400K | — | ✓ | high | openai ⚠ |
| OpenAI o3 | 2 | 8 | 200K | ✓ | — | med | openai ⚠ (vision unconfirmed) |
| xAI Grok 4.3 | 1.25 | 2.5 | 1M | — | ✓ | med | [x.ai](https://docs.x.ai/developers/models) ⚠ |
| Zhipu GLM-5.2 | 1.4 | 4.4 | — | ✓ | — | med | [z.ai](https://docs.z.ai/guides/overview/pricing) (verified 07-04; MIT claim unverified) |
| Zhipu GLM-5 | 1.0 | 3.2 | — | — | — | med | z.ai (verified 07-04) |
| Zhipu GLM-4.7 | 0.6 | 2.2 | — | — | — | med | z.ai ⚠ |
| DeepSeek-V4-flash | 0.14 | 0.28 | 1M | ✓ | — | high | [deepseek](https://api-docs.deepseek.com/quick_start/pricing) ⚠ |
| DeepSeek-V4-pro | 0.435 | 0.87 | 1M | ✓ | — | high | deepseek ⚠ (a secondary source disputes this — verify) |
| Qwen-plus | 0.4 | 1.2 | 1M | — | — | med | [alibaba](https://www.alibabacloud.com/help/en/model-studio/model-pricing) ⚠ (tiered pricing) |

> Cost per run = `tokens × token-multiplier × blended$/1M`, where `blended = in·0.8 + out·0.2` (§6.3). Local =
> free (your own hardware), §3.1–3.3. Reasoning models (o3 / DeepSeek-V4-pro) emit extra think-tokens →
> their token-multiplier defaults > 1 (flash/lightweight variants, e.g. DeepSeek-V4-flash, stay 1.0; all editable).

---

## 4. Runtime / endpoint catalog

Exact Sentinel env blocks. All listed runtimes expose an **OpenAI-compatible** `/v1`
(except native Anthropic). Liveness check: `curl <BASE_URL>/models`.

### 4.1 Windows-local
- **Ollama** (`https://ollama.com`) — single-file installer; CUDA/Vulkan auto-detected.
  ```bash
  LLM_BACKEND=openai
  LLM_BASE_URL=http://localhost:11434/v1
  LLM_MODEL=qwen3:14b
  LLM_API_KEY=noauth
  # VISION: ollama pull qwen2.5-vl:7b
  LLM_MODEL_HEAL=qwen2.5-vl:7b
  LLM_VISION=1
  ```
- **LM Studio** (`https://lmstudio.ai`) — GUI; Developer→Start Server (port 1234); `LLM_BASE_URL=http://localhost:1234/v1`, `LLM_MODEL=<slug-from-Developer-tab>`, `LLM_API_KEY=lm-studio`.
- **llamafile** (`https://github.com/Mozilla-Ocho/llamafile`) — zero-install single-exe (llama.cpp); port 8080; `LLM_BASE_URL=http://127.0.0.1:8080/v1`.

### 4.2 Linux-local
- **Ollama** — `curl -fsSL https://ollama.com/install.sh | sh`; systemd service; env as above.
- **vLLM** (`https://docs.vllm.ai`) — production GPU serving (PagedAttention). `vllm serve <hf-repo-id> --port 8000`; `LLM_BASE_URL=http://localhost:8000/v1`, `LLM_MODEL=meta-llama/Llama-3.3-70B-Instruct`.
- **llama.cpp server** (`https://github.com/ggml-org/llama.cpp`) — `llama-server -m model.gguf --port 8080 --alias <name>`; Q4 works on CPU-only as well.
- **HuggingFace TGI** — Docker; OpenAI Messages API with TGI ≥ 1.4.0; `LLM_MODEL=<hf-repo-id>`.
- **LocalAI** (`https://localai.io`) — multi-backend; Docker `localai/localai:latest-cpu`; model = YAML config name.

### 4.3 Cloud provider
- **OpenAI** — `LLM_BASE_URL` not needed (defaults to `api.openai.com/v1`); role split: `LLM_MODEL_PLANNER=o3`, `LLM_MODEL_HEAL=gpt-4o-mini`, `LLM_VISION=1`.
- **Anthropic (native)** — `LLM_BACKEND=anthropic`, `LLM_MODEL=claude-opus-4-8` (= in-code default); for OpenAI-compat access, route through OpenRouter/LiteLLM.
- **DeepSeek** — `LLM_BASE_URL=https://api.deepseek.com`; models `deepseek-v4-pro`/`deepseek-v4-flash` (verify at api-docs.deepseek.com; `deepseek-chat`/`-reasoner` are being retired 2026-07-24).
- **Together AI** — `https://api.together.ai/v1`; slugs `provider/model` (⚠️ Llama-4 slugs not confirmed — check `api.together.ai/models`).
- **Fireworks AI** — `https://api.fireworks.ai/inference/v1`; slugs `accounts/fireworks/models/<name>`.

### 4.4 Cloud router
- **OpenRouter** (`https://openrouter.ai`) — single `/api/v1` to 300+ models; slugs `provider/model` (`anthropic/claude-opus-4` confirmed). Role split through a single gateway:
  ```bash
  LLM_BACKEND=openai
  LLM_BASE_URL=https://openrouter.ai/api/v1
  LLM_API_KEY=sk-or-...
  LLM_MODEL_PLANNER=anthropic/claude-opus-4
  LLM_MODEL_HEAL=openai/gpt-4o-mini
  ```

### 4.5 Local router / proxy
- **LiteLLM Proxy** (`https://docs.litellm.ai`) — `pip install 'litellm[proxy]'`; `litellm --config config.yaml` (port 4000); aliases in `config.yaml` → different upstreams per role. Image `docker.litellm.ai/berriai/litellm:latest`.
- **LocalAI (as router)** — same image; one instance serves multiple YAML-defined models, can proxy to cloud.

### 4.6 Workflow (n8n)
- **n8n** (`https://n8n.io`) — not a runtime, but an orchestrator. Mode-B (n8n as proxy for Sentinel): Webhook (`llm-proxy/v1/chat/completions`) → HTTP Request (real backend) → Respond to Webhook (return OpenAI JSON verbatim). `LLM_BASE_URL=https://<n8n-host>/webhook/llm-proxy`. Requires careful preservation of the OpenAI response schema.

---

## 5. VRAM-sizing methodology

> Authoritative formula source for the calculator [vram.html](https://alexgromer.github.io/sentinel/calculators/vram.html).

```
VRAM_total ≈ weights_GB + kv_cache_GB + overhead_GB

  weights_GB  = params_B × bytes_per_param(quant)
  kv_cache_GB = 2 × n_layers × (n_kv_heads × head_dim) × ctx × bytes_per_elt / 1e9
  overhead_GB ≈ 1.0–2.0 GB (CUDA/ROCm context + allocator + activation buffers)

Fit check:
  available_for_model = GPU_VRAM_GB − overhead_GB
  max_params_B(quant) = (available_for_model − kv_cache_GB) / bytes_per_param(quant)
```
All estimates are **approximate (±10–15%)**: embedding/output-head tensors are sometimes stored
at full precision even in a quantized file. Verify with `ollama show` / `llama.cpp --info`
before purchasing hardware.

### 5.1 Bits-per-weight / bytes-per-param (GGUF, approximate)

| Quant | bits/weight | bytes/param | Note |
|---|---|---|---|
| FP16 | 16 | 2.0 | exact, no quantization |
| Q8_0 | 8.5 | 1.0625 | block=32 int8 + 1×FP16 scale |
| Q6_K | 6.56 | 0.8203 | k-quant, minimal quality loss |
| Q5_K_M | 5.7 | 0.7129 | good quality/size balance |
| Q4_K_M | 4.81 | 0.6016 | **recommended 4-bit** (~1% loss vs FP16) |
| Q4_0 | 4.5 | 0.5625 | legacy block-quant |
| Q3_K_M | 3.41 | 0.4258 | noticeable loss; last resort when VRAM is scarce |

### 5.2 KV-cache

`KV_bytes = 2 × n_layers × (n_kv_heads × head_dim) × ctx × batch × bytes_per_elt`; `bytes_per_elt`
= 2 (FP16), 1 (int8 `--kv-cache-type q8_0`), 0.5 (int4). **GQA** reduces `n_kv_heads` (Llama-3 8B/70B:
`n_kv_heads=8, head_dim=128` → kv_dim=1024). For Sentinel's workload (input ≤2000 + output ≤800 tok)
KV-cache is a **minor term (<100 MB at batch=1)**; it matters only when hosting an LLM shared across
long-context workloads.

### 5.3 Worked examples (overhead=1.5 GB; KV negligible at batch=1)

**12 GB GPU (RTX 2060/3060 12 GB):**

| Model | Quant | weights | total | Fits? |
|---|---|---|---|---|
| 7B | Q8_0 | 7.44 | 8.94 | ✅ comfortable (~3 GB headroom for KV) |
| 13B | Q4_K_M | 7.82 | 9.32 | ✅ |
| 13B | Q6_K | 10.66 | 12.16 | ⚠️ tight |
| 32B | Q4_K_M | 19.25 | 20.75 | ❌ |

**24 GB GPU (RTX 4090/3090):**

| Model | Quant | weights | total | Fits? |
|---|---|---|---|---|
| 13B | Q8_0 | 13.81 | 15.31 | ✅ |
| 32B | Q4_K_M | 19.25 | 20.75 | ✅ (~3.3 GB for KV) |
| 70B | Q4_K_M | 42.11 | 43.61 | ❌ requires 2×GPU |
| 70B | Q3_K_M | 29.81 | 31.31 | ❌ requires ≥32 GB |

(8/16 GB tiers — in the calculator.) On consumer cards the display driver reserves ~100–300 MB of
VRAM — subtract this from effective capacity.

---

## 6. Token-cost-per-phase methodology

> Authoritative formula source for the calculator [token-cost.html](https://alexgromer.github.io/sentinel/calculators/token-cost.html).
> Variables: `P`=pages, `S`=steps/page, `R`=fraction of broken locators (0–1), `V`=vision (0|1).

### 6.1 Per-call bounds (verified against code)

| Phase | Input (tok) | Output (tok) | Per call |
|---|---|---|---|
| explore `propose` | ≤ 8000/4 = 2000 | ≤ 200 | ≤ 2200 |
| goal `build_scenario` | ≤ 2000 | ≤ 800 | ≤ 2800 (once) |
| describe `draft` | description/4 | ≤ 800 | once |
| heal-text | ≤ 3000/4 = 750 | ≤ 200 | ≤ 950 |
| heal-vision | ≈ image(≈1100) + menu(≈50) | ≤ 100 | ≈ 1250 |

(`~4 chars/token`. Vision tokens for PNG **depend on provider**: OpenAI tile ≈1105; Anthropic ≈1200–1400; local LLaVA 256–576 — verify.)

### 6.2 Call counts and aggregate

```
explore : P×S calls to propose()   (one per step, if --planner llm|goal; 0 with heuristic)
goal    : 1 call to build_scenario()    describe: 1 call to draft()
heal-text   : P×S×R   (after deterministic L1–L6 failure)
heal-vision : P×S×R×V (only if heal-text also failed AND LLM_VISION=1)
replay  : 0 calls, 0 tokens (deterministic hot path)

plan_tokens_explore  = min(P×S×2200, PLAN_BUDGET=50000)   # excess → heuristic
plan_tokens_goal     = min(2800, 50000)
heal_tokens          = min(P×S×R×950 + P×S×R×V×1250, HEAL_BUDGET=20000)  # excess → L1–L6
cost_usd   = total_tokens × price_per_token           local_time = total_tokens / throughput_tps
```

### 6.3 Worked examples (PLAN_BUDGET=50k, HEAL_BUDGET=20k)

| # | Scenario | plan | heal | total |
|---|---|---|---|---|
| A | small site, explore, no-vision (P=1,S=5,R=.10,V=0) | 11 000 | 475 | **11 475** |
| B | medium, explore (P=3,S=10,R=.20,V=0) | 50 000 (cap) | 5 700 | **55 700** |
| C | medium, **goal**-mode (P=3,S=10,R=.20,V=0) | 2 800 | 5 700 | **8 500** (−87% vs B) |
| D | large, explore, vision (P=5,S=15,R=.30,V=1) | 50 000 (cap) | 20 000 (cap) | **70 000** (both budgets saturated → graceful degrade) |
| E | **replay** (any) | 0 | 0 | **0** (LLM-free) |

Example A in dollars (~$3/1M input + $15/1M output, ~80/20 split): ≈ **$0.06**; locally @10 tok/s: ≈ 19 min.
Lesson C: goal-mode delivers ~87% token savings on planning vs explore under the same load.

---

## 7. Anti-hallucination and cutoff

- **Assistant knowledge cutoff — January 2026.** Models and facts marked ⚠️ `verify-before-use`
  are post-cutoff and/or were not confirmed from a primary source during research (2026-06-27).
  **Verify before production use.**
- Benchmark figures are included **only** when confirmed from a primary source (HuggingFace model
  card / arXiv / official leaderboard). Several secondary figures were **excluded or corrected**
  during fact-checking (e.g. Pixtral-12B ChartQA 81.8 not 83.7, MMMU 52.5 not 62.5; IFEval for
  Qwen3 — absent from the tech report).
- The math in §5/§6 is derived directly from the code (`max_tokens`, budgets, caps — see §1
  citations) and standard GGUF bpw; it is authoritative, unlike model nomenclature.
- The calculators implement the §5/§6 formulas **verbatim** (vanilla JS, no network) — any
  discrepancy between a calculator and this document is considered a documentation bug.
