# Sentinel — деплой + M9-LIVE тест на Windows-хосте

> 🌐 **Русский** (основная версия) · [English](WINDOWS_TESTING.en.md)

> **Тип:** How-to · **Аудитория:** оператор live-теста на отдельной Windows-машине
> **Связанные:** [M9_LIVE_PLAN.md](./M9_LIVE_PLAN.md) · [LOCAL_MODELS.md](./LOCAL_MODELS.md) · [QUICKSTART.md](./QUICKSTART.md) · [DISTRIBUTION.md](./DISTRIBUTION.md)

Практический гайд: развернуть весь стек на Windows-хосте, прогнать живой тест (фикстуры L1–L6 →
explore/replay с локальной LLM) и собрать артефакты для разбора. Все команды сверены с `install.ps1`,
`docker-compose.yml`, `M9_LIVE_PLAN.md §A/§B`.

---

## 0. Перед стартом

- **`install.ps1` НЕ для этого теста.** Он ставит только `agentctl.exe` из GitHub-релиза (и требует
  опубликованный `v*`-тег, которого пока нет). Для M9-LIVE нужен весь стек (agentctl + Python brain +
  Node/Playwright executor + опц. Ollama) → собираем из исходников.
- **Два пути на Windows** (выбери один):
  - **Путь A — Docker Desktop (рекомендуется):** образ `sentinel:local` бандлит agentctl+brain+
    pw-executor+фикстуры. Минимум ручной возни.
  - **Путь B — WSL2 (Ubuntu):** нативная Linux-среда, где bash-скрипты (`collect-live-run.sh`) работают
    как задокументировано.

### 0.1 Модель и VRAM (важно — от объёма зависит выбор)

Схема имён env — из `brain/llm.py make_backend` (**НЕ** `SENTINEL_*_MODEL`). Verified-VRAM из
[`LOCAL_MODELS.md §3.3`](./LOCAL_MODELS.md); точный расчёт под конфиг — калькулятор `docs/calculators/vram.html`.

| GPU VRAM | PLAN-роль (planner) | VISION-роль (heal) |
|---|---|---|
| **8 ГБ** (напр. RTX 2060 **SUPER** = 8 ГБ) | **Qwen3-8B** Q5 (~6 ГБ) | Qwen2.5-VL-7B Q4 (~7 ГБ) |
| **12 ГБ** (RTX 2060 **12GB**/3060) | **Qwen3-14B** Q4_K_M (~9.5 ГБ) | Qwen2.5-VL-7B Q4/Q5 (~7 ГБ) |

> **Ключевое:** planner (explore) и vision-heal (replay) работают в **разные фазы** — им не нужно
> лежать в VRAM одновременно. Ollama грузит модель по требованию; поставь `OLLAMA_MAX_LOADED_MODELS=1`,
> чтобы одна выгружалась перед загрузкой другой (пауза ~секунды на переключении). Поэтому важно, чтобы
> влезала **каждая по отдельности**, а не сумма. `qwen3:14b` (~9.5 ГБ) требует **12 ГБ**; на 8 ГБ бери
> **qwen3:8b** для planner, иначе 14B частично выгрузится в CPU и planner будет медленным.

Скачать (нативный Ollama для Windows — https://ollama.com/download/windows, у него нормальный GPU-доступ):
```powershell
ollama pull qwen3:14b        # planner на 12 ГБ; на 8 ГБ → ollama pull qwen3:8b
ollama pull qwen2.5-vl:7b    # heal (vision)
```

---

## Путь A — Docker Desktop (рекомендуется)

**Пререквизиты:** Docker Desktop (WSL2-backend, дефолт) · Git for Windows (даёт `git` + **Git Bash** для
сбора артефактов) · нативный Ollama для Windows (см. §0.1).

```powershell
# 1. клон + сборка образа
git clone https://github.com/AlexGromer/sentinel.git ; cd sentinel
docker compose build            # sentinel:local из Dockerfile (agentctl+brain+pw-executor+фикстуры)

# 2. дым БЕЗ LLM (проверить, что стек жив; артефакты в .\runs через volume)
docker compose run --rm -v ${PWD}\runs:/app/runs sentinel `
  run --target "file:///app/testdata/fixtures/l3.html" --planner heuristic --artifact-dir /app/runs/smoke
#   успех = exit 0 + .\runs\smoke\plan.json. Фикстуры (l1..l6-newtab, L4=3 файла) уже в образе.
```

**Env для локальной LLM** (иначе молчаливая деградация в heuristic — backend по умолчанию anthropic!).
Нативный Ollama на Windows → контейнер видит его по `host.docker.internal`:
```powershell
$LLM = @(
  "-e","LLM_BACKEND=openai",
  "-e","LLM_BASE_URL=http://host.docker.internal:11434/v1",
  "-e","LLM_API_KEY=noauth",
  "-e","LLM_MODEL_PLANNER=qwen3:14b",     # или qwen3:8b на 8 ГБ
  "-e","LLM_MODEL_HEAL=qwen2.5-vl:7b"
  # "-e","LLM_STRUCTURED=1"   # opt-in strict-JSON; при отказе эндпоинта от json_schema молча уйдёт в heuristic
)
```

**Живой explore + replay:**
```powershell
# explore/author (LLM-план grounded)
docker compose run --rm -v ${PWD}\runs:/app/runs $LLM sentinel `
  run --goal "заполни форму валидными данными и отправь" `
      --target "file:///app/testdata/fixtures/l3.html" --artifact-dir /app/runs/live1

# replay + heal (дрейф локатора → self-heal L1–L6 + confidence-gate)
docker compose run --rm -v ${PWD}\runs:/app/runs $LLM sentinel `
  run --replay --plan /app/runs/live1/plan.json --artifact-dir /app/runs/replay1
```

**(опц.) Co-pilot UI:**
```powershell
docker compose --profile control-api up -d control-api   # HTTP-фасад
docker compose --profile webui up -d webui               # статический UI на http://localhost:8088
```

---

## Путь B — WSL2 (Ubuntu) нативно

```bash
# пререквизиты
sudo apt update && sudo apt install -y golang-1.26 nodejs npm python3 git
curl -LsSf https://astral.sh/uv/install.sh | sh          # uv (Python-venv)

# сборка (из корня репо)
go build -o bin/agentctl ./cmd/agentctl && go build -o bin/store-gateway ./cmd/store-gateway \
  && go build -o bin/control-api ./cmd/control-api
cd pw-executor && npm i && npm run build && npx playwright install chromium && cd ..
cd brain && UV_PROJECT_ENVIRONMENT=../.venv uv sync --frozen && cd ..   # venv в repo-root .venv (где ищет agentctl)

# env + прогон (Ollama нативный на Windows-хосте)
export LLM_BACKEND=openai
export LLM_BASE_URL=http://host.docker.internal:11434/v1
export LLM_API_KEY=noauth LLM_MODEL_PLANNER=qwen3:14b LLM_MODEL_HEAL=qwen2.5-vl:7b
bin/agentctl run --goal "…" --target "file://$PWD/testdata/fixtures/l3.html" --artifact-dir runs/live1
bin/agentctl run --replay --plan runs/live1/plan.json --artifact-dir runs/replay1
```

---

## Чек-лист M9-LIVE (`M9_LIVE_PLAN.md §B/§D`)

| Проверка | Как | Подтверждает |
|---|---|---|
| explore/author | `run --goal … --target file://…/l3.html` | LLM-план grounded, не галлюцинирует селектор |
| replay + heal | explore → дрейфануть DOM (site→site-v2) → `run --replay --plan …` | self-heal L1–L6 + confidence-gate (RISK-002) |
| determinism | 2× golden в разных процессах → сравнить байты | RISK-009 byte-stability |
| budget-kill | низкий `TOTAL_TOKEN_LIMIT` → деградация planner→heuristic | M8 budget-ceiling |
| co-pilot UI | control-api + `docs/index.html` → Tests→Live на run_id | M14 AG-UI-timeline, auto-HITL |
| MV3-расширение (опц.) | `extension/` → `npm i && npm run build` → load unpacked в Chrome → запись→scenario | M9.8 recorder |

> **Главная ловушка:** прогон «успешен» и с выключенной LLM. Проверяй в `runs\<id>\llm-transcript.jsonl`
> поле `planner` = `llm` (не `heuristic`).

---

## Сбор артефактов для разбора

Каждый прогон = `runs\<id>\` (`plan.json`·`scenario.json`·`heal-report.json`·`report.json/html`·
`llm-transcript.jsonl`·`metrics.prom`·`trace.zip`). **Веди `runs\LIVE_NOTES.md`**: id · модель · мишень ·
ожидал/получил · exit-код · что удивило.

**Сбор с редакцией секретов** (`collect-live-run.sh` — bash, запускай в **Git Bash или WSL**):
```bash
scripts/collect-live-run.sh <run_id>                # → live-results/live-<id>.tar.gz (редакция ВКЛ)
scripts/collect-live-run.sh <run_id> --with-trace   # + trace.zip (НЕредактированный — только dev-стенд!)
```
Редакция по умолчанию обнуляет пароли в form-шагах + вычищает Authorization/Bearer/Cookie/token-шейпы;
`checkpoint.db` и `storage_state*.json` не собираются никогда. **Перенос: USB/scp, НЕ git.**
На dev-машине положи в `live-results/` → «разбери live-прогоны» → калибровка RISK-002/003 реальными числами.

---

## Windows-гочи

- **`host.docker.internal`** — так контейнер/WSL достаёт нативный Ollama на Windows-хосте. Если Ollama как
  compose-сервис (`--profile ollama`), адрес из sentinel-контейнера = `http://ollama:11434/v1`.
- **Volume-пути**: PowerShell `${PWD}\runs:/app/runs`; CMD — `%cd%\runs:/app/runs`.
- **GPU для Ollama**: нативный Ollama для Windows использует GPU напрямую; Ollama-в-Docker на Windows —
  GPU-проброс сложнее (бери нативный).
- **`trace.zip`** несёт живой DOM + тела запросов → по умолчанию НЕ собирается; `--with-trace` только для
  одноразового dev-стенда.
- **CRLF**: bash-скрипты требуют LF → `git config --global core.autocrlf input` перед клоном, либо гоняй в WSL.
