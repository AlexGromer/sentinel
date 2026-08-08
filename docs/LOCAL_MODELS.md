# Локальные и облачные модели — методология выбора

> 🌐 **Русский** (основная версия) · [English](LOCAL_MODELS.en.md)

> **ADR-029** · **Дата**: 2026-06-27 · **Статус**: методика (платформо-агностична)
> **Калькуляторы**: [VRAM](https://alexgromer.github.io/sentinel/calculators/vram.html) · [token-cost](https://alexgromer.github.io/sentinel/calculators/token-cost.html) · [model-selector](https://alexgromer.github.io/sentinel/calculators/model-selector.html) (этот документ — авторитетный источник их формул)

---

## Содержание
1. [Введение: модели — это конфигурация](#1-введение-модели--это-конфигурация)
2. [Env-профиль: как направить Sentinel на любой бэкенд](#2-env-профиль)
3. [Каталог моделей по ролям (PLAN / VISION)](#3-каталог-моделей-по-ролям)
4. [Каталог runtime / endpoint (6 категорий)](#4-каталог-runtime--endpoint)
5. [Методология VRAM-sizing](#5-методология-vram-sizing)
6. [Методология token-cost-per-phase](#6-методология-token-cost-per-phase)
7. [Anti-hallucination и cutoff](#7-anti-hallucination-и-cutoff)
8. [MODEL-002 — измерение сходимости между моделями](#8-model-002--измерение-сходимости-между-моделями)

---

## 1. Введение: модели — это конфигурация

Sentinel **уже** провайдер-агностичен (M6, ADR-019): три LLM-роли — **PLAN** (исследование/авторинг
сценариев), **HEAL** (повторная привязка сломанного локатора) и **VISION** (set-of-marks по скриншоту) —
вызываются через `brain/llm.py::make_backend(role)`, который строит `AnthropicBackend` (нативно) **или**
`OpenAICompatBackend` (любой OpenAI-совместимый endpoint). Выбор — **per-role через env**, **без нового
кода и без нового «profile»-knob** (ADR-029): провайдер-профили *документируются*, а не кодируются.

**Ключевые свойства нагрузки Sentinel** (определяют, почему годятся небольшие локальные модели):

| Свойство | Значение (проверено по коду) | Источник |
|---|---|---|
| Тип вывода | структурированный JSON (index-pick / scenario), **не** длинная генерация | `brain/planner.py` |
| Размер вывода | PLAN propose ≤ **200** tok · scenario ≤ **800** tok · HEAL-text ≤ **200** · HEAL-vision ≤ **100** | `planner.py:116,177,228,282` · `healing.py:131,176` |
| Контекст входа | ≤ **8000** симв. (PLAN-меню) / ≤ **3000** симв. (HEAL) ≈ ≤ 2000 / 750 tok | `planner.py:222` · `healing.py:126` |
| Vision-вход | один PNG (≈1280×720) + крошечное меню марок | `healing.py:168` |
| Temperature | **0** (детерминированный выбор) | `planner.py` / `healing.py` |
| Replay (hot path) | **LLM-free**, 0 токенов | `brain/replay.py` |

Значит: PLAN хочет **надёжного instruction-following / structured-JSON**, HEAL — **повторной привязки**
сломанного локатора, VISION — **VLM, читающего нумерованные марки** на скриншоте. Это посильно моделям
от 3–4B и выше — см. §3 и калькулятор VRAM (§5).

> **In-code дефолты остаются `claude-*`** (`_DEFAULT_MODEL = {"planner": "claude-opus-4-8",
> "heal": "claude-sonnet-4-6"}`, `llm.py:233`). Offline-прогоны используют `FakeBackend` (детерминизм/CI);
> реальная локальная модель — **opt-in** через env-профиль ниже. RTX 2060 12 ГБ — это **один пример**
> среди тиров 8/12/16/24 ГБ, а не основа методики.

---

## 2. Env-профиль

Все переменные читаются `make_backend` (`llm.py:241–279`). **Приоритет: role-specific
`LLM_<KEY>_<ROLE>` > глобальный `LLM_<KEY>`.** Роли: `PLANNER`, `HEAL`. Ключи: `BACKEND`, `MODEL`,
`BASE_URL`, `API_KEY`, `VISION`, `STRUCTURED`.

| Env | Назначение | Примечание |
|---|---|---|
| `LLM_BACKEND` | `anthropic` (нативно) \| `openai` (OpenAI-compat) \| `sampling` (MCP-host, M7) | дефолт `anthropic` |
| `LLM_MODEL` | id модели у провайдера | для `openai` — **обязателен** |
| `LLM_BASE_URL` | URL OpenAI-compat endpoint (`…/v1`) | local: Ollama/vLLM/llama.cpp |
| `LLM_API_KEY` | ключ; для local — любая непустая строка (`noauth`) | fallback к `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` |
| `LLM_VISION` | `1` → включить vision на OpenAI-compat HEAL-бэкенде | text-only модель vision пропускает |
| `LLM_STRUCTURED` | `1` → строгий structured-output (Anthropic tool_use / OpenAI json_schema, ADR-057) на OpenAI-compat бэкенде | дефолт OFF; иначе `complete`+`extract_json`-fallback |
| суффикс `_PLANNER` / `_HEAL` | переопределяет глобальный ключ для роли | напр. `LLM_MODEL_PLANNER` |

**Деградация безопасна:** при отсутствии ключа/SDK `make_backend` → `None` ⇒ PLAN падает на
детерминированный `HeuristicPlanner`, HEAL — на L1–L6. Прогон не падает.

**Пример (локальный Ollama, разные модели на роли):**
```bash
export LLM_BACKEND=openai
export LLM_BASE_URL=http://localhost:11434/v1
export LLM_API_KEY=noauth
export LLM_MODEL_PLANNER=qwen3:14b        # рассуждения/структурированный JSON
export LLM_MODEL_HEAL=qwen2.5vl:7b       # VLM для re-grounding + set-of-marks
export LLM_VISION=1
```
Готовые env-блоки на каждый runtime — в §4.

---

## 3. Каталог моделей по ролям

> **Легенда verification:** ✅ `verified` — имя/размер подтверждены первичным источником в ходе
> исследования (2026-06-27); ⚠️ `verify-before-use` — частично/не подтверждено (post-cutoff —
> проверьте перед боевым использованием). Все бенчмарки — по ссылкам; цифры, не подтверждённые
> первичным источником, исключены или помечены.

### 3.1 PLAN-роль — локальные open-weight LLM (structured-JSON / reasoning)

| Модель | Параметры | Quant | ~VRAM | Контекст | Verify | Сильные стороны (кратко) |
|---|---|---|---|---|---|---|
| **Phi-4-mini-instruct** | 3.8B | Q5_K_M | ~3 ГБ | 128K | ⚠️ | Самый низкий VRAM-порог; SFT+DPO+RLHF под structured output; ARC-C 83.7%, GSM8K 88.6% (model card) |
| **Qwen3-4B** | 4B | Q5_K_M | ~3.5 ГБ | 32K (131K YaRN) | ⚠️ | thinking/non-thinking в одном чекпойнте; Apache-2.0; влезает в любой 6 ГБ GPU |
| **Qwen3-8B** | 8.2B | Q5_K_M | ~6 ГБ | 32K (131K YaRN) | ⚠️ | GQA (8 KV-голов) → лёгкий KV-cache; рекомендуемый выбор для 8 ГБ тира |
| **Qwen3-14B** | 14.8B | Q4_K_M | ~9.5 ГБ | 32K (131K YaRN) | ✅ | 40 слоёв, 40Q/8KV (GQA); влезает в 12 ГБ; рост качества над 8B; Apache-2.0 |
| **DeepSeek-R1-Distill-Qwen-14B** | 14B | Q4_K_M | ~9.5 ГБ | 128K | ✅ | CoT-рассуждения из R1; AIME-2024 69.7%, MATH-500 93.9%. ⚠️ think-токены едят `max_tokens`; «без system-prompt» — проверьте совместимость |
| **Qwen3-30B-A3B** (MoE) | 30B / 3B акт. | Q4_K_M | ~18 ГБ | 32K (131K YaRN) | ⚠️ | MoE: скорость ~3B при качестве >14B; все эксперты в VRAM; для 24 ГБ |
| **Gemma-3-27B-IT** | 27B | Q4_0 QAT | ~18 ГБ | 128K in / 8K out | ✅ | Официальный Google QAT-GGUF (17.2 ГБ); function-calling + structured output; мультимодальна (годна и в VISION) |
| **Mistral-Small-3.2-24B** | 24B | Q4_K_M | ~15 ГБ | 128K | ⚠️ | IFEval 84.78%, HumanEval+ 92.9% (model card); мультимодальна. ⚠️ дата релиза `2506`=июнь 2025 по naming, не 2026 |
| **Qwen3-32B** | 32.8B | Q4_K_M | ~21 ГБ | 32K (131K YaRN) | ⚠️ | Лучшее dense structured-JSON качество на одном 24 ГБ GPU; для сложного `build_scenario` |
| **Llama-3.3-70B-Instruct** | 70B | Q3_K_M | ~33 ГБ | 128K | ✅ | IFEval 92.1, BFCL-v2 77.3 (model card — наивысший IFEval списка). Нужен 32 ГБ+ / 2×GPU |

*Бенчмарк/источники (первичные):* [Phi-4-mini](https://huggingface.co/microsoft/Phi-4-mini-instruct) ·
[Qwen3-14B](https://huggingface.co/Qwen/Qwen3-14B) · [Qwen3 tech report](https://arxiv.org/html/2505.09388v1) ·
[DeepSeek-R1-Distill-14B](https://huggingface.co/deepseek-ai/DeepSeek-R1-Distill-Qwen-14B) ·
[Gemma-3-27B QAT](https://huggingface.co/google/gemma-3-27b-it-qat-q4_0-gguf) ·
[Mistral-Small-3.2](https://huggingface.co/mistralai/Mistral-Small-3.2-24B-Instruct-2506) ·
[Qwen3-32B](https://huggingface.co/Qwen/Qwen3-32B) ·
[Llama-3.3-70B](https://huggingface.co/meta-llama/Llama-3.3-70B-Instruct).

> **IFEval-предупреждение:** для семейства Qwen3 цифры IFEval, циркулирующие во вторичных
> источниках, **отсутствуют** в tech report (arXiv:2505.09388) — проверяйте по
> [Open LLM Leaderboard](https://huggingface.co/spaces/open-llm-leaderboard/open_llm_leaderboard) до фиксации в деплой-доках.

### 3.2 VISION-роль — локальные VLM (set-of-marks grounding по скриншоту)

| Модель | Параметры | Quant | ~VRAM | Контекст | Verify | Сильные стороны (кратко) |
|---|---|---|---|---|---|---|
| **Qwen2.5-VL-7B-Instruct** | 7.6B* | Q4_K_M | ~7 ГБ | 32K | ✅ | ScreenSpot 84.7, OCRBench 864, DocVQA 95.7 (model card); dynamic-res тайлы под плотные марки; Ollama-native |
| **Phi-4-reasoning-vision-15B** | 15B | Q4_K_M | ~11 ГБ* | 16K | ✅ | ScreenSpot-V2 **88.2** (наивысший здесь), OCRBench 76.0 (model card, релиз 2026-03). Крупнее/медленнее — ок для heal-only |
| **Qwen3-VL-8B-Instruct** | 8B | Q4_K_M | ~12 ГБ* | 256K | ⚠️ | релиз 2025-10-15; 256K контекст; MMBench-цифры не подтверждены первичным источником |
| **InternVL3-8B** | 8B | Q4_K_M | ~7 ГБ | ~8K | ⚠️ | OCR+GUI; ScreenSpot-V2 81.4% — **не подтверждён** (таблицы в paper — картинки) |
| **MiniCPM-V-4.5** | 8B | Q4 | ~7 ГБ | ~8K | ⚠️ | Qwen3-8B + SigLIP2; OpenCompass avg 77.0; OCRBench-цифра не извлечена |
| **MiniCPM-V-4.6** | 1.3B | Q4 | ~3 ГБ | ⚠️ не подтв. | ⚠️ | релиз 2026-05-11; 262K-контекст **не подтверждён** — не использовать цифру до проверки |
| **SmolVLM2-2.2B** | 2.2B | Q4 | ~2 ГБ* | ⚠️ не подтв. | ⚠️ | OCRBench 72.9 (arXiv); ⚠️ VRAM 2 ГБ противоречит model card (5.2 ГБ video) |
| **Pixtral-12B** | 12B (+~0.4B vision) | Q4_K_M | ~9 ГБ | 128K | ⚠️ | ⚠️ ИСПРАВЛЕНО: ChartQA **81.8** (не 83.7), MMMU **52.5** (не 62.5) по model card |

*`7.6B`/VRAM со звёздочкой — оценка/не из официального спека; см. примечания в [`#7`](#7-anti-hallucination-и-cutoff).*

*Источники:* [Qwen2.5-VL-7B](https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct) ·
[Phi-4-reasoning-vision-15B](https://huggingface.co/microsoft/Phi-4-reasoning-vision-15B) ·
[Qwen3-VL-8B](https://ollama.com/library/qwen3-vl:8b) · [InternVL3-8B](https://arxiv.org/abs/2504.10479) ·
[MiniCPM-V](https://github.com/OpenBMB/MiniCPM-V) · [SmolVLM2](https://huggingface.co/HuggingFaceTB/SmolVLM2-2.2B-Instruct) ·
[Pixtral-12B](https://huggingface.co/mistralai/Pixtral-12B-2409). Лидерборд grounding: [gui-agent](https://gui-agent.github.io/grounding-leaderboard/).

### 3.3 Подбор по тиру VRAM (быстрый ориентир)

| GPU VRAM | PLAN (dense) | VISION |
|---|---|---|
| **8 ГБ** | Qwen3-8B Q5_K_M / Phi-4-mini | Qwen2.5-VL-7B Q4 / InternVL3-8B |
| **12 ГБ** (RTX 2060/3060) | Qwen3-14B Q4_K_M | Phi-4-vision-15B Q4 / Qwen2.5-VL-7B Q5 |
| **16 ГБ** | Qwen3-14B Q6 / Mistral-Small-24B Q4 | любая 7–15B VLM |
| **24 ГБ** | Qwen3-32B Q4_K_M / Gemma-3-27B QAT / Qwen3-30B-A3B | 15B VLM Q5/Q6 |
| **32 ГБ+ / 2×GPU** | Llama-3.3-70B Q3/Q4 | — |

---

### 3.4 Облачные модели — ориентировочные цены (⚠ verify; cloud-цены re-verified 2026-07-04)

Источник цен для cost-explorer (`docs/index.html`). **Редактируемые, ориентировочные** — не утверждение об
актуальной стоимости (облачные цены дрейфуют). Claude — из claude-api skill (2026-06-04); остальные —
исследование 2026-06-28, **cloud-цены re-verified 2026-07-04** (WebFetch первичных pricing-страниц). Обновляются: CI `prices-refresh.yml` (еженедельно, через
OpenRouter → PR) + кнопка «Обновить из OpenRouter» на странице. **Пригодность (fit)** для structured-JSON
Sentinel — **мнение**, не бенчмарк.

| Модель | $/1M вход | $/1M выход | Контекст | reasoning | vision | fit | Источник |
|---|---|---|---|---|---|---|---|
| Claude Opus 4.8 | 5 | 25 | 1M | ✓ | ✓ | high | [anthropic](https://www.anthropic.com/pricing) (skill 06-04) |
| Claude Sonnet 5 | 2 | 10 | 1M | ✓ | ✓ | high | anthropic (verified 07-04; intro 2/10 → 3/15 с 09-01) |
| Claude Haiku 4.5 | 1 | 5 | 200K | — | ✓ | high | anthropic (skill 06-04) |
| GPT-5.5 (флагман) | 5 | 30 | 1M | ✓ | ✓ | high | [openai](https://developers.openai.com/api/docs/pricing) (verified 07-04) |
| GPT-5.4 | 2.5 | 15 | 1M | — | ✓ | high | openai (verified 07-04) |
| GPT-5.4-mini | 0.75 | 4.5 | 400K | — | ✓ | high | openai ⚠ |
| OpenAI o3 | 2 | 8 | 200K | ✓ | — | med | openai ⚠ (vision не подтверждён) |
| xAI Grok 4.3 | 1.25 | 2.5 | 1M | — | ✓ | med | [x.ai](https://docs.x.ai/developers/models) ⚠ |
| Zhipu GLM-5.2 | 1.4 | 4.4 | — | ✓ | — | med | [z.ai](https://docs.z.ai/guides/overview/pricing) (verified 07-04; MIT-claim не проверен) |
| Zhipu GLM-5 | 1.0 | 3.2 | — | — | — | med | z.ai (verified 07-04) |
| Zhipu GLM-4.7 | 0.6 | 2.2 | — | — | — | med | z.ai ⚠ |
| DeepSeek-V4-flash | 0.14 | 0.28 | 1M | ✓ | — | high | [deepseek](https://api-docs.deepseek.com/quick_start/pricing) ⚠ |
| DeepSeek-V4-pro | 0.435 | 0.87 | 1M | ✓ | — | high | deepseek ⚠ (вторичный источник спорит — проверьте) |
| Qwen-plus | 0.4 | 1.2 | 1M | — | — | med | [alibaba](https://www.alibabacloud.com/help/en/model-studio/model-pricing) ⚠ (тарифы по тирам) |

> Стоимость прогона = `токены × токен-множитель × среднее$/1M`, где `среднее = вход·0.8 + выход·0.2` (§6.3).
> Локально = бесплатно (своё железо), §3.1–3.3. Reasoning-модели (o3 / DeepSeek-V4-pro) шлют лишние
> think-токены → их токен-множитель по умолчанию > 1 (flash/лёгкие варианты, напр. DeepSeek-V4-flash — 1.0; всё редактируемо).

---

## 4. Каталог runtime / endpoint

Точные env-блоки `Sentinel`. Все перечисленные runtime экспонируют **OpenAI-совместимый** `/v1`
(кроме нативного Anthropic). Проверка живости: `curl <BASE_URL>/models`.

### 4.1 Windows-local
- **Ollama** (`https://ollama.com`) — однофайловый установщик; CUDA/Vulkan авто.
  ```bash
  LLM_BACKEND=openai
  LLM_BASE_URL=http://localhost:11434/v1
  LLM_MODEL=qwen3:14b
  LLM_API_KEY=noauth
  # VISION: ollama pull qwen2.5vl:7b
  LLM_MODEL_HEAL=qwen2.5vl:7b
  LLM_VISION=1
  ```
- **LM Studio** (`https://lmstudio.ai`) — GUI; Developer→Start Server (порт 1234); `LLM_BASE_URL=http://localhost:1234/v1`, `LLM_MODEL=<slug-из-Developer-tab>`, `LLM_API_KEY=lm-studio`.
- **llamafile** (`https://github.com/Mozilla-Ocho/llamafile`) — zero-install single-exe (llama.cpp); порт 8080; `LLM_BASE_URL=http://127.0.0.1:8080/v1`.

### 4.2 Linux-local
- **Ollama** — `curl -fsSL https://ollama.com/install.sh | sh`; systemd-сервис; env как выше.
- **vLLM** (`https://docs.vllm.ai`) — продакшн GPU-сервинг (PagedAttention). `vllm serve <hf-repo-id> --port 8000`; `LLM_BASE_URL=http://localhost:8000/v1`, `LLM_MODEL=meta-llama/Llama-3.3-70B-Instruct`.
- **llama.cpp server** (`https://github.com/ggml-org/llama.cpp`) — `llama-server -m model.gguf --port 8080 --alias <name>`; Q4 работает и на CPU-only.
- **HuggingFace TGI** — Docker; OpenAI Messages API с TGI ≥ 1.4.0; `LLM_MODEL=<hf-repo-id>`.
- **LocalAI** (`https://localai.io`) — мульти-бэкенд; Docker `localai/localai:latest-cpu`; модель = имя YAML-конфига.

### 4.3 Cloud-provider
- **OpenAI** — `LLM_BASE_URL` не нужен (дефолт `api.openai.com/v1`); сплит `LLM_MODEL_PLANNER=o3`, `LLM_MODEL_HEAL=gpt-4o-mini`, `LLM_VISION=1`.
- **Anthropic (нативно)** — `LLM_BACKEND=anthropic`, `LLM_MODEL=claude-opus-4-8` (= in-code дефолт); для OpenAI-compat доступа маршрутизируйте через OpenRouter/LiteLLM.
- **DeepSeek** — `LLM_BASE_URL=https://api.deepseek.com`; модели `deepseek-v4-pro`/`deepseek-v4-flash` (проверьте на api-docs.deepseek.com; `deepseek-chat`/`-reasoner` выводятся из эксплуатации 2026-07-24).
- **Together AI** — `https://api.together.ai/v1`; слаги `provider/model` (⚠️ Llama-4-слаги не подтверждены — сверьтесь с `api.together.ai/models`).
- **Fireworks AI** — `https://api.fireworks.ai/inference/v1`; слаги `accounts/fireworks/models/<name>`.

### 4.4 Cloud-router
- **OpenRouter** (`https://openrouter.ai`) — единый `/api/v1` к 300+ моделям; слаги `provider/model` (`anthropic/claude-opus-4` подтверждён). Сплит ролей через один шлюз:
  ```bash
  LLM_BACKEND=openai
  LLM_BASE_URL=https://openrouter.ai/api/v1
  LLM_API_KEY=sk-or-...
  LLM_MODEL_PLANNER=anthropic/claude-opus-4
  LLM_MODEL_HEAL=openai/gpt-4o-mini
  ```

### 4.5 Local-router / proxy
- **LiteLLM Proxy** (`https://docs.litellm.ai`) — `pip install 'litellm[proxy]'`; `litellm --config config.yaml` (порт 4000); алиасы из `config.yaml` → разные апстримы на роли. Образ `docker.litellm.ai/berriai/litellm:latest`.
- **LocalAI (как роутер)** — тот же образ; один инстанс отдаёт несколько YAML-моделей, может проксировать к облаку.

### 4.6 Workflow (n8n)
- **n8n** (`https://n8n.io`) — не runtime, а оркестратор. Режим-B (n8n как прокси для Sentinel): Webhook (`llm-proxy/v1/chat/completions`) → HTTP Request (реальный бэкенд) → Respond to Webhook (вернуть JSON OpenAI verbatim). `LLM_BASE_URL=https://<n8n-host>/webhook/llm-proxy`. Требует аккуратного сохранения схемы ответа OpenAI.

---

## 5. Методология VRAM-sizing

> Авторитетная копия формулы для калькулятора [vram.html](https://alexgromer.github.io/sentinel/calculators/vram.html).

```
VRAM_total ≈ weights_GB + kv_cache_GB + overhead_GB

  weights_GB  = params_B × bytes_per_param(quant)
  kv_cache_GB = 2 × n_layers × (n_kv_heads × head_dim) × ctx × bytes_per_elt / 1e9
  overhead_GB ≈ 1.0–2.0 ГБ (CUDA/ROCm контекст + аллокатор + буферы активаций)

Проверка влезаемости:
  доступно_под_модель = GPU_VRAM_GB − overhead_GB
  max_params_B(quant) = (доступно_под_модель − kv_cache_GB) / bytes_per_param(quant)
```
Все оценки **приблизительны (±10–15%)**: embedding/output-head тензоры иногда хранятся в полной
точности даже в квантованном файле. Сверяйте `ollama show` / `llama.cpp --info` перед покупкой железа.

### 5.1 Bits-per-weight / bytes-per-param (GGUF, приближённо)

| Quant | bits/weight | bytes/param | Примечание |
|---|---|---|---|
| FP16 | 16 | 2.0 | точно, без квантизации |
| Q8_0 | 8.5 | 1.0625 | блок=32 int8 + 1×FP16 scale |
| Q6_K | 6.56 | 0.8203 | k-quant, минимальная потеря качества |
| Q5_K_M | 5.7 | 0.7129 | хороший баланс качество/размер |
| Q4_K_M | 4.81 | 0.6016 | **рекомендуемый 4-bit** (~1% потери vs FP16) |
| Q4_0 | 4.5 | 0.5625 | legacy блок-quant |
| Q3_K_M | 3.41 | 0.4258 | заметная потеря; крайний случай при дефиците VRAM |

### 5.2 KV-cache

`KV_bytes = 2 × n_layers × (n_kv_heads × head_dim) × ctx × batch × bytes_per_elt`; `bytes_per_elt`
= 2 (FP16), 1 (int8 `--kv-cache-type q8_0`), 0.5 (int4). **GQA** снижает `n_kv_heads` (Llama-3 8B/70B:
`n_kv_heads=8, head_dim=128` → kv_dim=1024). Для нагрузки Sentinel (вход ≤2000 + выход ≤800 tok)
KV-cache — **малый член (<100 МБ при batch=1)**; значим только при общем хостинге LLM с длинным контекстом.

### 5.3 Worked examples (overhead=1.5 ГБ; KV пренебрежимо при batch=1)

**12 ГБ GPU (RTX 2060/3060 12 ГБ):**

| Модель | Quant | weights | total | Влезает? |
|---|---|---|---|---|
| 7B | Q8_0 | 7.44 | 8.94 | ✅ комфортно (~3 ГБ запас под KV) |
| 13B | Q4_K_M | 7.82 | 9.32 | ✅ |
| 13B | Q6_K | 10.66 | 12.16 | ⚠️ впритык |
| 32B | Q4_K_M | 19.25 | 20.75 | ❌ |

**24 ГБ GPU (RTX 4090/3090):**

| Модель | Quant | weights | total | Влезает? |
|---|---|---|---|---|
| 13B | Q8_0 | 13.81 | 15.31 | ✅ |
| 32B | Q4_K_M | 19.25 | 20.75 | ✅ (~3.3 ГБ под KV) |
| 70B | Q4_K_M | 42.11 | 43.61 | ❌ нужен 2×GPU |
| 70B | Q3_K_M | 29.81 | 31.31 | ❌ нужен ≥32 ГБ |

(8/16 ГБ тиры — в калькуляторе.) На consumer-картах драйвер дисплея резервирует ~100–300 МБ VRAM —
вычтите из эффективной ёмкости.

---

## 6. Методология token-cost-per-phase

> Авторитетная копия для калькулятора [token-cost.html](https://alexgromer.github.io/sentinel/calculators/token-cost.html).
> Переменные: `P`=страниц, `S`=шагов/страницу, `R`=доля сломанных локаторов (0–1), `V`=vision (0|1).

### 6.1 Границы на вызов (проверены по коду)

| Фаза | Вход (tok) | Выход (tok) | На вызов |
|---|---|---|---|
| explore `propose` | ≤ 8000/4 = 2000 | ≤ 200 | ≤ 2200 |
| goal `build_scenario` | ≤ 2000 | ≤ 800 | ≤ 2800 (один раз) |
| describe `draft` | описание/4 | ≤ 800 | один раз |
| heal-text | ≤ 3000/4 = 750 | ≤ 200 | ≤ 950 |
| heal-vision | ≈ image(≈1100) + меню(≈50) | ≤ 100 | ≈ 1250 |

(`~4 симв./токен`. Vision-токены PNG **зависят от провайдера**: OpenAI tile ≈1105; Anthropic ≈1200–1400; local LLaVA 256–576 — проверяйте.)

### 6.2 Количество вызовов и агрегат

```
explore : P×S вызовов propose()   (по одному на шаг, если --planner llm|goal; 0 при heuristic)
goal    : 1 вызов build_scenario()    describe: 1 вызов draft()
heal-text   : P×S×R   (после провала детерминированных L1–L6)
heal-vision : P×S×R×V (только если heal-text тоже не справился И LLM_VISION=1)
replay  : 0 вызовов, 0 токенов (детерминированный hot path)

plan_tokens_explore  = min(P×S×2200, PLAN_BUDGET=50000)   # превышение → heuristic
plan_tokens_goal     = min(2800, 50000)
heal_tokens          = min(P×S×R×950 + P×S×R×V×1250, HEAL_BUDGET=20000)  # превышение → L1–L6
cost_usd   = total_tokens × price_per_token           local_time = total_tokens / throughput_tps
```

### 6.3 Worked examples (PLAN_BUDGET=50k, HEAL_BUDGET=20k)

| # | Сценарий | plan | heal | total |
|---|---|---|---|---|
| A | малый сайт, explore, no-vision (P=1,S=5,R=.10,V=0) | 11 000 | 475 | **11 475** |
| B | средний, explore (P=3,S=10,R=.20,V=0) | 50 000 (cap) | 5 700 | **55 700** |
| C | средний, **goal**-режим (P=3,S=10,R=.20,V=0) | 2 800 | 5 700 | **8 500** (−87% vs B) |
| D | большой, explore, vision (P=5,S=15,R=.30,V=1) | 50 000 (cap) | 20 000 (cap) | **70 000** (оба бюджета сатурированы → graceful degrade) |
| E | **replay** (любой) | 0 | 0 | **0** (LLM-free) |

Пример A в деньгах (~$3/1M вход + $15/1M выход, ~80/20): ≈ **$0.06**; локально @10 tok/s: ≈ 19 мин.
Урок C: goal-режим даёт ~87% экономии токенов планирования vs explore при той же нагрузке.

---

## 7. Anti-hallucination и cutoff

- **Knowledge cutoff ассистента — январь 2026.** Модели и факты, помеченные ⚠️ `verify-before-use`,
  относятся к post-cutoff и/или не подтверждены первичным источником в ходе исследования (2026-06-27).
  **Проверяйте перед боевым использованием.**
- Цифры бенчмарков включены **только** при подтверждении из первичного источника (HuggingFace model
  card / arXiv / официальный лидерборд). Несколько вторичных цифр были **исключены или исправлены**
  в ходе fact-check (напр. Pixtral-12B ChartQA 81.8 не 83.7, MMMU 52.5 не 62.5; IFEval Qwen3 —
  отсутствует в tech report).
- Математика (§5/§6) выведена напрямую из кода (`max_tokens`, бюджеты, caps — см. цитаты §1) и
  стандартных GGUF bpw; она авторитетна, в отличие от номенклатуры моделей.
- Калькуляторы реализуют формулы §5/§6 **дословно** (vanilla JS, без сети) — расхождение между
  калькулятором и этим документом считается багом документа.

---

## 8. MODEL-002 — измерение сходимости между моделями

> ⚠️ **Известное расхождение с §1/§6.** Таблица §1 и разбор §6.1 цитируют потолки вывода PLAN
> `propose` ≤200 / scenario ≤800 ток. со ссылкой на код (`planner.py:116,177,228,282`). Эти строки
> устарели: `brain/planner.py` (комментарий на `:37-44`) фиксирует, что старые 200/800 заменены на
> `_PICK_TOKENS=1024` (per-action pick) и `_SCENARIO_TOKENS=3072` (scenario/draft), потому что именно
> на потолке 800 `qwen3:14b` замерянно останавливался на `finish_reason=length` («думает», не успевая
> дать ответ) на 4 из 6 фикстур M9-LIVE. §1/§6 не переписаны этой правкой (пересчёт всей стоимостной
> математики — отдельная задача) — здесь только зафиксирован факт расхождения, чтобы не считать
> устаревшую цифру текущей.

Вопрос «эта модель вообще годится для PLAN-роли» отвечает не бенчмарк, а **прямой замер на реальном
explore**: сходятся ли повторные прогоны ОДНОЙ модели друг с другом, сходятся ли РАЗНЫЕ модели друг с
другом, и какая доля вызовов упирается в потолок вывода. Оснастка — `scripts/model_convergence.py`
(MODEL-002; измерительный инструмент, не меняет поведение продукта кроме двух точечных зондов в
`brain/llm.py`, см. FILEMAP.md).

### 8.1 Что и как измеряется

Один `explore` (без `--goal`, планировщик `PLANNER=llm` → `LLMPlanner`, `brain/planner.py`) на ОДНОЙ
фикстуре из `testdata/fixtures/*.html`, N моделей × M повторных прогонов каждая. На каждый прогон:

- **`plan_hash`** (`brain/state.py::canonical_plan_hash`) — детерминированный SHA-256 по ВСЕМУ
  упорядоченному списку шагов; чувствителен к порядку (`step_id` сидит в каждой записи).
- **`grounded_step_set`** — то же множество выборов планировщика, но БЕЗ порядка и без шага 1
  (детерминированная начальная навигация, которую пишет `_run_explore` ДО первого вызова
  `planner.propose()`, — она одинакова у всех моделей/прогонов по построению).
- **доля `finish_reason=length` по ПОПЫТКАМ**, не по финальному решению шага —
  `brain/llm.py::complete_structured` суммирует токены и повторяет попытку с удвоенным потолком,
  сохраняя `finish_reason` только ПОСЛЕДНЕЙ попытки; успешный повтор стирает след усечения первой.
  Опциональный per-attempt лог (`SENTINEL_LLM_ATTEMPT_LOG`, `_record_attempt`) — единственное место,
  где это видно.

Сравнение хеша И множества вместе разводит два разных явления: хеш разошёлся при совпадении
множества — модель нестабильна по ПОРЯДКУ выбора; разошлось само множество — модель выбирает разные
элементы, а не просто в другом порядке.

### 8.2 Запуск

```bash
# список моделей и фикстур ВЫВОДИТСЯ, не пишется сюда: модели — из GET {ollama}/api/tags,
# фикстуры — из testdata/fixtures/*.html
scripts/model_convergence.py --dry-run                       # резолвит и печатает матрицу, не тратит вызовы
scripts/model_convergence.py --models qwen3:8b,qwen3:14b --runs 3 --max-steps 6
```

Изоляция потолка (§1 «Env-профиль»): каждой паре (модель, прогон) — свой `SENTINEL_LLM_BUDGET_FILE`
и свой `SENTINEL_LLM_ATTEMPT_LOG` (`build_run_env`/`plan_matrix`), иначе выученный потолок одной
модели протекает в следующую (или в следующий прогон ТОЙ ЖЕ модели) и глушит сигнал усечения именно
там, где он важнее всего.

Полный список флагов и их дефолты — `scripts/model_convergence.py --help`; офлайн-проверка логики
(без сети, без браузера, без живой модели — `FakeBackend`/инъецированный `fetch`) —
`tests/test_model_convergence_offline.py`.

### 8.3 Что это НЕ решает

Какие поля ОБЯЗАНЫ совпадать между моделями (пороги для CI-гейта, допустимый набор `finish_reason`,
что считать «моделью, годной для PLAN») — отдельная, не начатая задача **MODEL-001**. Без цифр этого
раздела она была бы гаданием; с ними — измеримым решением.
