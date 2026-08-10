# Sentinel — Подключаемые адаптеры (SPI · LiteLLM-роутер · MCP-Inspector)

> 🌐 **Русский** (основная версия) · [English](ADAPTERS.en.md)

> **ADR-045** (инструменты) · **ADR-123** (SPI) · **Дата**: 2026-06-28, дополнено 2026-08-10 ·
> **Статус**: методика + реализованный SPI

Зонтичный документ для **подключаемых адаптеров** Sentinel (тема M9.7 / GAP-M9-08). Две разные вещи под
одной обложкой, и их полезно не путать:

- **§1 — SPI (ADR-123):** место, куда встаёт **ЧУЖОЙ КОД**. Реестр `brain/adapters.py`, три вида
  (`model` · `auth` · `deploy`), обнаружение через `SENTINEL_ADAPTERS`. Полный контракт —
  [`M9.7_CONTRACT.md`](M9.7_CONTRACT.md).
- **§2–§3 — внешние инструменты (ADR-045):** **LiteLLM** (опциональный model-router) и
  **MCP-Inspector** (отладка M7-сервера). Кода не требуют вовсе: садятся на существующие швы конфигом.

## 1. SPI — швы, через которые расширяется сам продукт

| Вид | Что подменяет | Точка входа продукта |
|-----|---------------|----------------------|
| **`model`** | провайдер модели (`ModelAdapter.make(spec) -> LLMBackend`) | `brain/llm.py::make_backend(role)` — `anthropic` и `openai` зарегистрированы тут же как обычные адаптеры |
| **`auth`** | декларативный блок `auth:` → окружение (`EnvAdapter.env(spec)`) | `brain/runconfig.py::_apply_auth`; референс `storage_state` = M9.1 логин-как-тест (ADR-026) |
| **`deploy`** | декларативный блок `deploy:` → окружение | `brain/runconfig.py::_apply_deploy`; референс `local` = `STORE_ADDR` · `OTEL_EXPORTER_OTLP_ENDPOINT` · `CHECKPOINT_DSN` |

```bash
SENTINEL_ADAPTERS=mycorp_sentinel.adapters LLM_BACKEND=bedrock ./bin/agentctl run --target …
```

```yaml
# run.yaml — `adapter:` необязателен; без него берётся тот, что поставлялся
auth:   {adapter: storage_state, storage_state: /run/state.json, pw_no_trace: true}
deploy: {store_addr: gateway:50051, otel_endpoint: http://otel:4317}
```

**Правила, которые стоит знать до написания своего адаптера** (обоснование — в контракте):
`EnvAdapter.env()` **чистая** и в окружение не пишет (предпочтение «явный флаг > файл» остаётся в
`runconfig.py`, а не у адаптера) · `ModelAdapter` **не читает `LLM_*`** сам, всё разрешённое приходит
в `ModelSpec` · неизвестное имя адаптера — **конфигурационная ошибка (exit 3)**, а не молчаливый
откат · неимпортируемый модуль из `SENTINEL_ADAPTERS` **бросает** на пути RunConfig и **деградирует
с объявлением** на пути модели.

> 🔒 **Граница лицензии (ADR-056 §2 строка 42):** SPI и референсные адаптеры — открытый каркас
> (Apache-2.0, необратимо). Корпоративная авторизация (Keycloak/OIDC/Vault/SSO/RBAC) цепляется к
> этому SPI **снаружи** и в это дерево не коммитится — `[M-COMMERCIAL-auth]`. Правило **проверяется**
> гейтом `tests/test_adapter_spi_offline.py`, а не только записано.

### Швы, адаптерами НЕ являющиеся

| Шов | Где | Как подключиться |
|-----|-----|------------------|
| **OpenAI-совместимый endpoint** | `brain/llm.py` `OpenAICompatBackend` (ADR-019, M6) | env `LLM_BACKEND=openai` + `LLM_BASE_URL=<endpoint>` — конфигурация встроенного адаптера, кода не нужно (сюда садится LiteLLM, §2) |
| **MCP-хост** | `brain/server.py` M7-сервер (ADR-020) | хост драйвит `explore`/`heal`/`replay`/`report` и поставляет модель через `sampling/createMessage`; `sampling` разрешается **до** реестра — это свойство запуска процесса, а не настроенный провайдер |

Всё перечисленное **опционально**: с пустым env Sentinel работает как раньше (Anthropic по умолчанию;
без хоста — эвристика/L1–L6).

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
  `SamplingBackend` (`brain/llm.py`, класс `SamplingBackend` — якорь по имени, не по номеру строки). Нет sampling → backend недоступен → fallback на эвристику/L1–L6.

CLI-режим (`--cli`) удобен для скриптовой проверки tools/resources/prompts; **поддержку sampling сверяйте с
версией Inspector** (sampling — интерактивный, обычно через UI). Offline-аналог этих проверок без живого
хоста — [`tests/test_m7_offline.py`](../tests/test_m7_offline.py) (`FakeSamplingSession` + tools-list smoke).

> **Реальный sampling-прогон по хостам — user-run** (Claude Desktop поддерживает; OpenCode/Kilocode —
> подтвердить перед боевым использованием), GAP-VERIFY-006.

## См. также
[`M9.7_CONTRACT.md`](M9.7_CONTRACT.md) (**контракт SPI**: реестр, три вида, референсные адаптеры,
граница лицензии, гейт) · [`LOCAL_MODELS.md`](LOCAL_MODELS.md) (каталог моделей/runtime + калькуляторы) ·
[`M6_CONTRACT.md`](M6_CONTRACT.md) (provider-agnostic brain) · [`M7_CONTRACT.md`](M7_CONTRACT.md)
(MCP-exposure) · [`M9.1_CONTRACT.md`](M9.1_CONTRACT.md) §4 (storageState-жизненный цикл) ·
[`DEVELOPMENT.md`](DEVELOPMENT.md).
