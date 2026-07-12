# Sentinel — Подключаемые адаптеры (LiteLLM-роутер · MCP-Inspector)

> 🌐 **Русский** (основная версия) · [English](ADAPTERS.en.md)

> **ADR-045** · **Дата**: 2026-06-28 · **Статус**: методика (опциональный tooling, без хард-зависимостей)

Зонтичный документ для **подключаемых адаптеров** Sentinel (тема M9.7 / GAP-M9-08): инструменты, которые
встают на уже существующие швы движка **без изменения кода**. Покрывает два, выбранных в roadmap:
**LiteLLM** (опциональный model-router) и **MCP-Inspector** (отладка M7-MCP-сервера).

## 1. Швы для подключения

| Шов | Где | Как подключиться |
|-----|-----|------------------|
| **Модель / бэкенд** | `brain/llm.py` `OpenAICompatBackend` (ADR-019, M6) | env `LLM_BACKEND=openai` + `LLM_BASE_URL=<OpenAI-compat endpoint>` — любой OpenAI-совместимый провайдер/прокси |
| **MCP-хост** | `brain/server.py` M7-сервер (ADR-020) | хост драйвит `explore`/`heal`/`replay`/`report` и поставляет модель через `sampling/createMessage` |

Оба — **опциональны**: с пустым env Sentinel работает как раньше (Anthropic по умолчанию; без хоста —
эвристика/L1–L6). Адаптеры ниже не меняют ядро — это config + внешние инструменты.

## 2. LiteLLM — опциональный model-router

**LiteLLM** — self-hosted OpenAI-совместимый шлюз над 100+ провайдерами (DeepSeek/Mistral/Anthropic/Ollama/…).
Зачем: единая точка маршрутизации, fallback между провайдерами, budget/rate-limit, логирование. Sentinel
**не зависит** от LiteLLM — brain уже говорит OpenAI-compat (M6/ADR-019), поэтому роутер встаёт за
`LLM_BASE_URL`. ADR-019 явно зафиксировал: LiteLLM — **опция, не хард-деп в hot-path**.

### Поднять (compose-профиль `litellm`, зеркало `ollama`)

```bash
# Ключи провайдеров — в окружении (config ссылается на них через os.environ/, без литералов).
export DEEPSEEK_API_KEY=…  MISTRAL_API_KEY=…  ANTHROPIC_API_KEY=…  LITELLM_MASTER_KEY=sk-…
docker compose --profile litellm up -d litellm        # OpenAI-compat на http://litellm:4000/v1
```

Образ `docker.litellm.ai/berriai/litellm:latest`, порт `4000`, конфиг — `deploy/litellm/config.yaml`
(`model_list` → провайдеры; ключи через `os.environ/<VAR>`). Правьте `config.yaml` под свои модели.

### Направить Sentinel на роутер

```bash
LLM_BACKEND=openai \
LLM_BASE_URL=http://litellm:4000/v1 \
LLM_MODEL=deepseek-chat \
LLM_API_KEY=$LITELLM_MASTER_KEY \
  ./bin/agentctl run --target https://app.example
```

Per-role: `LLM_BASE_URL_PLANNER` / `LLM_MODEL_HEAL` и т.д. (precedence `LLM_<KEY>_<ROLE>` > `LLM_<KEY>`,
см. [`LOCAL_MODELS.md`](LOCAL_MODELS.md) §2 «env-профиль» и §4 «runtime-каталог»). Концептуально LiteLLM —
ещё один OpenAI-compat endpoint, как `ollama`-профиль, только маршрутизирующий прокси.

> **Безопасность:** ключи провайдеров читаются из env (`os.environ/<VAR>`) — **никогда не литералом** в
> `config.yaml` (иначе попадёт в коммит и зацепит gitleaks). Это OpenAI-compat прокси без vision-гарантий —
> для visual-heal (M5) нужен бэкенд с `supports_vision` (см. [`M6_CONTRACT.md`](M6_CONTRACT.md)).
> **Реальный smoke роутера — user-run** (сеть/ключи; в среде разработки сеть к провайдерам заблокирована),
> см. GAP-VERIFY-005.

## 3. MCP-Inspector — отладка M7-сервера

[MCP Inspector](https://github.com/modelcontextprotocol/inspector) (`@modelcontextprotocol/inspector`) —
официальный визуальный инструмент тестирования MCP-серверов: подключается к серверу по **stdio**, показывает
`tools/list` со схемами, даёт форму вызова, и (в UI-режиме) выступает **хостом для `sampling/createMessage`**.
Это конкретный хост для проверки M7 (`brain/server.py`) → частично закрывает **GAP-VERIFY-006**.

### Запуск против brain-MCP-сервера (stdio)

```bash
# Сначала собрать pw-executor (brain запускает его как дочерний процесс).
( cd pw-executor && npm ci && npm run build )

# UI-режим: Inspector спавнит brain, прокидывает env через -e, '--' отделяет команду сервера.
npx @modelcontextprotocol/inspector \
  -e RUN_MODE=mcp-server \
  -e PW_EXECUTOR_CMD="node $PWD/pw-executor/dist/server.js" \
  -e ARTIFACT_DIR="$PWD/runs/inspect" \
  -e PYTHONPATH="$PWD" \
  -- .venv/bin/python -m brain
```

Что проверять:
- **`tools/list`** показывает 4 инструмента: `explore` · `heal` · `replay` · `report` (см. [`M7_CONTRACT.md`](M7_CONTRACT.md)).
- Вызвать `replay` (детерминированный, **без LLM**) на готовом `plan.json` — не требует sampling.
- Вызвать `explore`/`heal` — они запрашивают модель у хоста через `sampling/createMessage`; **в UI-режиме
  Inspector предложит ответить на sampling-запрос** (вы выступаете моделью) → так проверяется
  `SamplingBackend` (`brain/llm.py:203-229`). Нет sampling → backend недоступен → fallback на эвристику/L1–L6.

CLI-режим (`--cli`) удобен для скриптовой проверки tools/resources/prompts; **поддержку sampling сверяйте с
версией Inspector** (sampling — интерактивный, обычно через UI). Offline-аналог этих проверок без живого
хоста — [`tests/test_m7_offline.py`](../tests/test_m7_offline.py) (`FakeSamplingSession` + tools-list smoke).

> **Реальный sampling-прогон по хостам — user-run** (Claude Desktop поддерживает; OpenCode/Kilocode —
> подтвердить перед боевым использованием), GAP-VERIFY-006.

## См. также
[`LOCAL_MODELS.md`](LOCAL_MODELS.md) (каталог моделей/runtime + калькуляторы) · [`M6_CONTRACT.md`](M6_CONTRACT.md)
(provider-agnostic brain) · [`M7_CONTRACT.md`](M7_CONTRACT.md) (MCP-exposure) · [`DEVELOPMENT.md`](DEVELOPMENT.md).
