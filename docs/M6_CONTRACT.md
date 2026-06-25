# M6 Contract — "Provider-Agnostic Brain" (frozen 2026-06-25)

> 🌐 **Русский** (основная версия) · [English](M6_CONTRACT.en.md)

Цель: снять привязку «мозга» к единственному провайдеру (Anthropic). Узлы **planner** (explore) и
**heal** (text re-grounding + set-of-marks vision) теперь вызывают LLM через провайдер-нейтральный
`LLMBackend`, поэтому Sentinel работает на Anthropic ИЛИ на любом OpenAI-совместимом endpoint
(ChatGPT, DeepSeek, Qwen DashScope-compat, Gemini OpenAI-compat, OpenRouter, Ollama, vLLM),
выбираемом **per-role** через env. Это реализует первое направление запроса пользователя (*consume* —
наш мозг ходит в любую модель); второе (*be-driven* хостами) — M7/ADR-020.

## Ключевой рычаг (замена с низким риском)
`brain/planner.py` и `brain/healing.py` уже имели единственную точку LLM-вызова. M6 вводит
`brain/llm.py` и заменяет только эти call-sites. `graph.py` / `replay.py` / `__main__.py` —
**неизменны**: контракт planner (`name="llm"`, строковый `model`, `propose → {..., tokens}`) сохранён.

## Интерфейс (`brain/llm.py`)
- `LLMResult{text, prompt_tokens, completion_tokens, model?}` — нормализованный ответ. `model`
  несёт реальную модель провайдера (для M7 sampling); для фиксированных backend = `backend.model`.
- `LLMBackend` (Protocol): `name` (провайдер), `model` (per-backend, immutable), `supports_vision`,
  `complete(prompt, *, max_tokens, temperature)`, `complete_vision(prompt, image_b64, *, …)`.
- `AnthropicBackend` — нативный (`messages.create`), `supports_vision=True`.
- `OpenAICompatBackend` — `chat.completions.create` через `base_url` + key; vision opt-in
  (`image_url` data-URI); для keyless-локальных (Ollama) key = `"noauth"`.
- `make_backend(role)` — фабрика. Возвращает **`None`** при отсутствии ключа/SDK ⇒ caller сохраняет
  offline-fallback (heuristic / L1–L6). **Никогда не бросает** (import/config-ошибка → `None`).

## Per-role env (precedence: `LLM_<KEY>_<ROLE>` > `LLM_<KEY>` > дефолт)
| Параметр | Global | Per-role override | Дефолт (как до M6) |
|----------|--------|-------------------|--------------------|
| провайдер | `LLM_BACKEND` | `LLM_BACKEND_PLANNER` / `_HEAL` | `anthropic` |
| модель | `LLM_MODEL` | `LLM_MODEL_PLANNER` / `_HEAL` | `claude-opus-4-8` / `claude-sonnet-4-6` |
| base_url | `LLM_BASE_URL` | `LLM_BASE_URL_PLANNER` / `_HEAL` | — |
| api_key | `LLM_API_KEY` | `LLM_API_KEY_PLANNER` / `_HEAL` | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` |
| vision | `LLM_VISION` | `LLM_VISION_HEAL` | provider default (anthropic=on) |

**При нуле выставленных env — поведение байт-в-байт как до M6** (Anthropic, Opus planner / Sonnet
heal, ключ `ANTHROPIC_API_KEY`, fallback на heuristic / L1–L6 без ключа). Сохраняет ADR-009.

## Vision-gating
Tier-7 set-of-marks heal требует vision-модель; gate = `use_visual AND backend.supports_vision`.
Text-only провайдер (DeepSeek-V3, Qwen-text) пропускает Tier-7 → деградация в детерминированный L1–L6.

## Детерминизм (ADR-019)
LLM-путь — **best-effort**: разные модели → разные планы → разный `plan_hash`; provenance модели
**не хранится**. `HeuristicPlanner` остаётся детерминированным якорем; golden baselines —
heuristic-only; replay LLM-free. `canonical_plan_hash` / `DETERMINISM.md` / формат baseline **не менялись**.

## Что изменилось
- Создан `brain/llm.py`. `planner.LLMPlanner` / `healing.HealingEngine`: `_client`/`_llm` → `_backend`.
- `otel.set_llm_tokens(sp, result)` — принимает нормализованный `LLMResult` (None-span tolerant).
- `pyproject.toml`: добавлен `openai` (import-guarded, optional в runtime).

## Гейт
- **Offline** (exec-gating: без сети/бинарей, `FakeBackend`): `test_b1_offline` (8) + `test_m5_offline`
  (4) зелёные; регресс `test_m3` / `test_m4` / `test_m4b` зелёный; default-path byte-for-byte.
- **Реальный smoke — user-run** (сеть в среде заблокирована). Anthropic (как раньше, дефолт):
  `ANTHROPIC_API_KEY=… PLANNER=llm …`. OpenAI-compat пример (роутер):
  `LLM_BACKEND=openai LLM_BASE_URL=https://openrouter.ai/api/v1 LLM_API_KEY=… LLM_MODEL=deepseek/deepseek-chat PLANNER=llm`.
  Vision-heal на другом провайдере: `LLM_BACKEND_HEAL=openai LLM_BASE_URL_HEAL=… LLM_MODEL_HEAL=… LLM_VISION_HEAL=1 HEAL_LLM=1 HEAL_VISUAL=1`.

## VERIFY при реальном smoke (provider-quirks, anti-hallucination)
- `temperature=0` отклоняется частью моделей (OpenAI o-series) → call-site падает в fallback (ОК для best-effort).
- `max_tokens` vs `max_completion_tokens` (o-series) — у части endpoints иное имя; покрыть env/ретраем при необходимости.
- `usage` может отсутствовать (Ollama / vLLM / streaming) → счётчики = 0 (tolerated; транскрипт пишет 0).
- JSON в \`\`\`fences\`\`\` — экстрактор `text[find('{') : rfind('}')+1]` выдерживает.

## ADR
ADR-019 (Accepted). Совместим с ADR-009 (per-role split моделей) и ADR-011 (pluggable planner).

## Вне scope
M7 MCP-server exposure + `SamplingBackend` (ADR-020). Provenance модели в `plan_hash`
(решение пользователя: детерминизм для LLM-пути не критичен).
