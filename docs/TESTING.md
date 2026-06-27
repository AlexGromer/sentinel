# Sentinel — Руководство по тестированию

> 🌐 **Русский** (основная версия) · English — _будет добавлен_ (перевод — следующий цикл)

Handoff-grade гайд: любой разработчик должен дойти от чистого клона до зелёного CI без
устных объяснений. Читайте вместе с [`DEVELOPMENT.md`](DEVELOPMENT.md) (сборка компонентов,
предпосылки, рецепты расширения).

---

## 1. Offline-гейты (без сети, без браузера, без LLM-ключа)

Все команды этого раздела работают на чистой машине и в CI без токенов или сети.
Запускайте их в корне репозитория (`/opt/agent_development`).

### 1.1 Go: vet + unit tests

```bash
go vet ./...
go test ./...
```

`go test ./...` — unit-тесты control-plane (agentctl, store-gateway). Ожидаемый результат:
все пакеты выводят `ok` или `--- PASS`; ненулевой exit code = блокер.

### 1.2 Python: offline-сьют (весь регресс M3–M9)

Запустите все offline-тесты одной командой:

```bash
for t in m3 m4 m4b m5 b1 m7 m8 m9 m9_2 m9_2b; do
    .venv/bin/python tests/test_${t}_offline.py
done
```

Или по одному — для быстрой изоляции:

```bash
.venv/bin/python tests/test_m3_offline.py   # параллельный replay, exit-коды, детерминизм
.venv/bin/python tests/test_m4_offline.py   # run_report, .spec.ts, Prometheus-метрики
.venv/bin/python tests/test_m4b_offline.py  # OTel no-op без ENDPOINT, Pushgateway-guard
.venv/bin/python tests/test_m5_offline.py   # set-of-marks overlay, visual-heal порог
.venv/bin/python tests/test_b1_offline.py   # provider-neutral llm.py: AnthropicBackend/OpenAI-compat/offline-fallback
.venv/bin/python tests/test_m7_offline.py   # MCP-сервер brain, SamplingBackend
.venv/bin/python tests/test_m8_offline.py   # W3C-трейс brain→pw-executor→store-gateway, BudgetTracker
.venv/bin/python tests/test_m9_offline.py   # secretRef не утекает, exit-коды, plan_hash стабилен
.venv/bin/python tests/test_m9_2_offline.py # GoalPlanner grounding, OOB→done, RunConfig precedence
.venv/bin/python tests/test_m9_2b_offline.py # двухфазный goal/describe, reconcile, богатый RunConfig
```

Каждый файл выводит `PASS <test_name>` на каждый тест и `ALL PASS (N)` в конце.
Ненулевой exit code или строка `FAIL` в stdout = блокер.

> **Предпосылки:** `uv venv && uv pip install langgraph langgraph-checkpoint-sqlite anthropic openai`
> (или `uv sync`, если `pyproject.toml` полный). Подробнее — `DEVELOPMENT.md §1–2`.

### 1.3 TypeScript: проверка типов pw-executor

```bash
cd pw-executor
npx tsc --noEmit
cd ..
```

Чистый выход (код 0, нет строк `error TS`) — обязателен перед коммитом, так как pw-executor
не имеет отдельных unit-тестов: типы — его основной статический контракт.

### 1.4 Secrets scan (gitleaks)

```bash
gitleaks detect --source . --verbose
```

Запускать перед каждым коммитом. Ненулевой exit code = СТОП, не коммитить.
Конфигурация разрешений: `.gitignore` — `runs/`, `state/`, `.env*`, `bin/`, `dist/`.

### 1.5 SCA — анализ зависимостей на уязвимости

Три сканера, по одному на стек:

```bash
# Go — официальный сканер (использует данные OSV)
govulncheck ./...

# Python — pip-audit против OSV и PyPI Advisory Database
pip-audit

# Node (pw-executor)
cd pw-executor
npm audit
cd ..
```

`govulncheck` ставится один раз: `go install golang.org/x/vuln/cmd/govulncheck@latest`.
`pip-audit` ставится в venv: `uv pip install pip-audit` или `pip install pip-audit`.

В CI: ненулевой exit code любого из трёх сканеров требует ревью (CRITICAL/HIGH — блокер;
MODERATE/LOW — решение по контексту).

---

## 2. Local-model setup (serving-agnostic)

Sentinel поддерживает **любой OpenAI-совместимый endpoint** наравне с Anthropic (ADR-019).
Модель выбирается **только через env** — кода менять не нужно.

### 2.1 ENV-профиль: схема переменных

| Переменная | Глобальная | Per-role override | Дефолт (нет env) |
|---|---|---|---|
| `LLM_BACKEND` | да | `LLM_BACKEND_PLANNER` / `LLM_BACKEND_HEAL` | `anthropic` |
| `LLM_MODEL` | да | `LLM_MODEL_PLANNER` / `LLM_MODEL_HEAL` | `claude-opus-4-8` (planner) / `claude-sonnet-4-6` (heal) |
| `LLM_BASE_URL` | да | `LLM_BASE_URL_PLANNER` / `LLM_BASE_URL_HEAL` | — (Anthropic SDK default) |
| `LLM_API_KEY` | да | `LLM_API_KEY_PLANNER` / `LLM_API_KEY_HEAL` | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` |
| `LLM_VISION` | да | `LLM_VISION_HEAL` | provider-default (Anthropic = включён) |

**Precedence:** `LLM_<KEY>_<ROLE>` > `LLM_<KEY>` > встроенный дефолт.
Роли: `PLANNER` (фаза explore/scenario), `HEAL` (heal + vision Tier-7).

Отсутствие ключа и `LLM_BACKEND=anthropic` → offline-fallback: heuristic planner + детерминированный L1–L6 heal. **Sentinel никогда не бросает исключение из-за отсутствия ключа** — он деградирует gracefully.

### 2.2 Пример: Ollama (локальная модель)

```bash
# 1. Запустить Ollama (если ещё не запущен)
ollama serve &

# 2. Скачать модель (один раз)
ollama pull qwen2.5:7b          # пример; каталог моделей — docs/LOCAL_MODELS.md

# 3. Запустить explore с planner на Ollama (heal остаётся на дефолте/offline)
LLM_BACKEND_PLANNER=openai \
LLM_BASE_URL_PLANNER=http://localhost:11434/v1 \
LLM_MODEL_PLANNER=qwen2.5:7b \
LLM_API_KEY_PLANNER=noauth \
  ./bin/agentctl run \
    --target "file://$PWD/testdata/site/index.html" \
    --planner llm

# 4. Обе роли на Ollama (включая vision-heal, если модель поддерживает)
LLM_BACKEND=openai \
LLM_BASE_URL=http://localhost:11434/v1 \
LLM_MODEL_PLANNER=qwen2.5:7b \
LLM_MODEL_HEAL=llava:13b \
LLM_API_KEY=noauth \
LLM_VISION_HEAL=1 \
  ./bin/agentctl run --replay \
    --plan runs/<id>/plan.json \
    --target "file://$PWD/testdata/site-v2/index.html" \
    --heal-llm
```

> `LLM_API_KEY=noauth` — Ollama игнорирует ключ, но Anthropic/OpenAI SDK требует непустую строку.

### 2.3 Пример: OpenRouter / любой облачный прокси

```bash
LLM_BACKEND_PLANNER=openai \
LLM_BASE_URL_PLANNER=https://openrouter.ai/api/v1 \
LLM_API_KEY_PLANNER=sk-or-... \
LLM_MODEL_PLANNER=deepseek/deepseek-chat \
  ./bin/agentctl run --target "https://staging.example.com" --planner llm
```

Heal-роль при этом остаётся на Anthropic (`ANTHROPIC_API_KEY`) или переходит в offline L1–L6
если ключ не задан.

> Каталог протестированных моделей по ролям (контекст, throughput, vision-совместимость):
> `docs/LOCAL_MODELS.md` — файл будет добавлен в рамках цикла документации после M9.

---

## 3. Live run (интеграционный прогон с браузером)

Live-прогоны требуют собранных компонентов (`DEVELOPMENT.md §2`). Используйте
`file://`-фикстуры из `testdata/` для воспроизводимой среды без внешней сети.

### 3.1 M9.1 — login-as-test с secretRef (PW_NO_TRACE=1)

Сценарий: план содержит шаг `fill` с `secretRef` (имя env-переменной с паролем).
Tracing **должен быть выключен** — иначе секрет попадёт в `trace.zip` (архитектурный fail-closed, `brain/__main__.py`).

```bash
# Подготовить: собрать бинари и браузер (DEVELOPMENT.md §2)
./bin/agentctl run \
  --replay \
  --plan path/to/login-plan.json \
  --target "https://staging.example.com/login" \
  --heal-llm \
  PW_NO_TRACE=1 \
  LOGIN_USERNAME=alice \
  LOGIN_PASSWORD=s3cret
```

Или через экспортированные переменные (предпочтительно в CI):

```bash
export PW_NO_TRACE=1
export LOGIN_USERNAME=alice
export LOGIN_PASSWORD=s3cret

./bin/agentctl run \
  --replay \
  --plan path/to/login-plan.json \
  --target "https://staging.example.com/login"
```

После успешного прогона `storageState` сохраняется автоматически если задан `STORAGE_STATE_SAVE`:

```bash
export STORAGE_STATE_SAVE=state/auth.json
./bin/agentctl run --replay --plan login-plan.json ...
# Следующие прогоны: --target <protected-page> с STORAGE_STATE=state/auth.json
```

### 3.2 M9.2 — goal-режим и describe-режим

**goal-режим**: Sentinel делает полный детерминированный explore (фаза 1), строит карту сайта,
затем один LLM-вызов авторит сценарий по всей карте (фаза 2).

```bash
# Против file:// фикстуры (воспроизводимо, без сети)
GOAL="log in and click Pay" \
  ./bin/agentctl run \
    --target "file://$PWD/testdata/site/index.html" \
    --planner goal          # или: --planner heuristic + GOAL env (make_planner авто-выбирает)

# Против staging
GOAL="submit the contact form" \
ANTHROPIC_API_KEY=sk-ant-... \
  ./bin/agentctl run --target "https://staging.example.com"
```

**describe-режим**: сначала LLM-черновик флоу словами, затем детерминированный reconcile с картой.

```bash
DESCRIBE="fill the username field with 'alice', then click the Pay button" \
  ./bin/agentctl run \
    --target "file://$PWD/testdata/site/index.html"
```

**RunConfig YAML** (декларативный, M9.2b):

```bash
./bin/agentctl run \
  --target "https://staging.example.com" \
  --run-config /config/run.yaml
```

Пример `run.yaml`:

```yaml
auth:
  storage_state: state/auth.json  # пропускает логин, если файл есть
  pw_no_trace: true               # обязателен при наличии secretRef
  login_plan: runs/login/plan.json

scenarios:
  - name: checkout
    goal: "add item to cart and complete checkout"
  - name: search
    describe: "type 'laptop' in the search field and press Enter"
```

Выбрать конкретный сценарий: `./bin/agentctl run --target ... --run-config run.yaml --scenario search`.

### 3.3 Чтение артефактов

Все артефакты пишутся в `runs/<run_id>/` (переопределяется `--artifact-dir`).

#### `plan.json` — замороженный план explore

```json
{
  "plan_id": "550e8400-...",
  "plan_hash": "sha256:abcdef...",   // SHA-256 от canonical JSON шагов; изменение → exit 3
  "target_url": "file:///...",
  "coverage_achieved": 0.92,
  "steps": [
    {"step_id": 1, "action_type": "navigate", "semantic_id": "...", "intent": "...", "target": "..."},
    {"step_id": 2, "action_type": "click",    "semantic_id": "...", "locator": {...}}
  ]
}
```

Коммитится в репозиторий приложения. Любая ручная правка `plan.json` без `agentctl baseline update`
приводит к **exit 3** на следующем replay.

#### `scenario.json` — воспроизводимый бизнес-процесс (goal/describe)

```json
{
  "plan_id": "<run_id>-scenario",
  "plan_hash": "sha256:...",
  "run_mode": "scenario",
  "mode": "goal",           // или "describe"
  "unmatched": 1,
  "steps": [
    {"step_id": 1, "action_type": "navigate", "target": "file:///s/login.html", "phase": "scenario"},
    {"step_id": 2, "action_type": "fill",     "locator": {"role": "textbox", "name": "Username"}, "value": "alice"},
    {"step_id": 3, "action_type": "navigate", "target": "file:///s/billing.html", "phase": "scenario"},
    {"step_id": 4, "action_type": "click",    "locator": {"role": "button", "name": "Pay"}}
  ]
}
```

`scenario.json` реплеится детерминированно без LLM (шаги несут полные `locator`+`alternatives`).

#### `reconcile-report.json` — отчёт describe-режима

```json
{
  "target_url": "file:///...",
  "grounded": 3,
  "unmatched": [
    {"ref": "NOPE", "reason": "ref not in site map"}
  ]
}
```

Создаётся только в describe-режиме. `unmatched` непусто → exit 1 (CI фиксирует «описанный флоу не существует в UI»).

#### `heal-report.json` — итог replay с healing

```json
{
  "exit_code": 0,
  "steps": [
    {"step_id": 2, "type": "click", "outcome": "healed",
     "heal": {"strategy": "role_name", "confidence": 0.92},
     "regression": null}
  ],
  "healed": 1,
  "failed": 0,
  "regressions": []
}
```

В baseline-режиме файл называется `baseline-report.json`.

### 3.4 Коды выхода

| Код | Условие |
|-----|---------|
| **0** | Все шаги прошли (или исцелены); `scenario.json` записан (goal/describe) |
| **1** | Минимум один шаг упал без исцеления; или describe вернул любой unmatched; или ноль привязанных шагов |
| **2** | Golden regression — a11y-хеш или screenshot-хеш расходится с baseline |
| **3** | Нарушение целостности плана (`plan_hash` не совпал); взаимоисключающие флаги (`GOAL` + `DESCRIBE`); битый RunConfig; неизвестный `--scenario`; `secretRef` при `PW_NO_TRACE != '1'` |

В CI: exit 0 или 1 — нормальные исходы (1 = «тест нашёл проблему»); exit 2 = UI-регрессия; exit 3 = конфигурационная ошибка, требует ручного вмешательства.

---

## 4. Zero-level path — docker compose demo (без сборки, без ключей)

Минимальный способ убедиться в работоспособности системы: одна команда, нет API-ключей,
нет внешней сети. Использует bundled `file://`-фикстуру и heuristic planner.

```bash
# Собрать образ (один раз; ~2–3 минуты)
docker compose build

# Запустить demo: explore testdata/site/index.html -> runs/demo/plan.json
docker compose --profile demo up
```

После завершения артефакты доступны на хосте в `./runs/demo/`:

```
runs/demo/
├── plan.json             # замороженный план
├── llm-transcript.jsonl  # пустой (LLM не вызывался)
├── trace.zip             # Playwright trace
└── checkpoint.db         # LangGraph checkpointer
```

Просмотр трассировки:

```bash
npx playwright show-trace runs/demo/trace.zip
```

**Прямой запуск конкретной команды:**

```bash
# произвольный target
docker compose run --rm sentinel run \
  --target "file:///app/testdata/site/index.html" \
  --planner heuristic

# replay с frozen plan
docker compose run --rm sentinel run \
  --replay \
  --plan /app/runs/demo/plan.json \
  --target "file:///app/testdata/site-v2/index.html"

# goal-режим с Ollama (сначала поднять сервис)
docker compose --profile ollama up -d ollama
docker compose exec ollama ollama pull qwen2.5:7b
docker compose run --rm \
  -e LLM_BACKEND=openai \
  -e LLM_BASE_URL=http://ollama:11434/v1 \
  -e LLM_MODEL_PLANNER=qwen2.5:7b \
  -e LLM_API_KEY=noauth \
  -e GOAL="click the first link" \
  sentinel run --target "file:///app/testdata/site/index.html"
```

> **Volumes:** `./runs` и `./state` монтируются в контейнер — артефакты персистентны между
> запусками. `./config` монтируется как `/config` — кладите RunConfig YAML туда.

---

## Справочник: что запускать в каком контексте

| Контекст | Минимальный набор команд |
|---|---|
| Перед любым коммитом | `go vet ./...` + `cd pw-executor && npx tsc --noEmit` + `gitleaks detect` |
| PR / feature branch | Все offline-тесты (§1.1–1.5) |
| Релизный кандидат | Offline-тесты + live demo via docker compose + live run vs. staging (§3) |
| Новая LLM-модель | `test_b1_offline.py` + smoke-прогон goal-режима vs. `file://` (§3.2) |
| Изменение pw-executor | `npx tsc --noEmit` + `test_m9_offline.py` |
| Изменение healing | `test_m3_offline.py` + `test_m8_offline.py` + live replay vs. site-v2 (§3.1) |
| Изменение RunConfig | `test_m9_2_offline.py` + `test_m9_2b_offline.py` |

Подробнее о предпосылках сборки, структуре компонентов и рецептах расширения:
[`DEVELOPMENT.md`](DEVELOPMENT.md).
