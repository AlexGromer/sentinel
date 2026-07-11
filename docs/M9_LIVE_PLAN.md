# M9-LIVE — план живого прогона (локальная LLM) + feedback-протокол

> Рабочий док для offline-теста. Артефакт-дашборд (визуальная версия): claude.ai/code/artifact/23f4bcfd.
> **Статус:** актуализирован в M9-LIVE-подготовке (env-схема и команды сверены с кодом; `scripts/collect-live-run.sh` реализован).
> Док одноязычный намеренно — он в `SINGLE_LANGUAGE`-списке `scripts/check_bilingual.py`, `.en`-зеркало не требуется.

Цель: доказать, что offline-верифицированный конвейер (14+ вех) работает **вживую**.

**Топология — две фазы.** Фаза 1: фикстуры `testdata/fixtures/` на dev-машине (`/opt/agent_development`) — реальных секретов нет, артефакты `runs/<id>/` читаются напрямую, канал переноса не нужен. Фаза 2: реальное приложение (в т.ч. с логином) на **отдельной** test-машине — там артефакты несут DOM/креды реального стенда, и наружу они уезжают только через `scripts/collect-live-run.sh` (§C).

---

## A. Подготовка
1. **Локальная модель:** `docker compose --profile ollama up -d ollama` → `docker compose exec ollama ollama pull <модель>`.
   Подбор — `docs/LOCAL_MODELS.md §3` + VRAM-калькулятор (Pages §5). Authoring (structured-JSON) — dense ≥14B (Qwen3-14B/32B), если хватает VRAM; heal — 7–8B.
   Env — **строго имена из `brain/llm.py` `make_backend`** (`LLM_<KEY>` с необязательным per-role суффиксом `_PLANNER`/`_HEAL`); те же имена эмитит визард `docs/setup/index.html` и пресет `ollama` в `docs/backend-presets.json`:
   ```bash
   export LLM_BACKEND=openai                       # БЕЗ этого backend по умолчанию = anthropic, и LLM_BASE_URL просто игнорируется
   export LLM_BASE_URL=http://localhost:11434/v1   # нативный agentctl на хосте; ИЗ compose-сети → http://ollama:11434/v1
   export LLM_API_KEY=noauth                       # Ollama ключ игнорирует, но OpenAI-SDK требует непустую строку
   export LLM_MODEL_PLANNER=qwen3:14b
   export LLM_MODEL_HEAL=qwen2.5-vl:7b
   # export LLM_VISION=1      # только если heal-модель действительно vision-capable
   # export LLM_STRUCTURED=1  # opt-in strict-JSON; если эндпоинт не умеет json_schema — планер молча
   #                          # деградирует в heuristic, heal → детерминированные L1–L6. Проверь по логу.
   ```
   ⚠ Молчаливая деградация — главная ловушка этой вехи: прогон «успешен» и с выключенной LLM. Смотри в `llm-transcript.jsonl`: `planner` должен быть `llm`, а не `heuristic`.
2. **Билды:**
   ```bash
   go build -o bin/agentctl ./cmd/agentctl && go build -o bin/store-gateway ./cmd/store-gateway \
     && go build -o bin/control-api ./cmd/control-api        # control-api нужен для co-pilot UI (§B)
   cd pw-executor && npm i && npm run build && npx playwright install chromium && cd ..
   cd brain && uv sync --frozen --no-dev && cd ..            # локед-сет из pyproject+uv.lock (ручной список зависимостей неполон)
   ```
3. **Мишени:** начни с фикстур (`file://`, без сети): `l1.html` · `l2.html` · `l3.html` · `l4.html`→`l4-dashboard.html`→`l4-billing.html` (L4 = 3-страничный флоу) · `l5.html` · `l6-newtab.html`. Потом — реальное приложение (опц. Keycloak-логин для auth-теста) — это уже фаза 2, на отдельной машине.

## B. Что прогнать / что наблюдать
| Проверка | Команда/действие | Подтверждает |
|---|---|---|
| explore/author | `bin/agentctl run --goal "…" --target file://$PWD/testdata/fixtures/l3.html` | M9.1/M9.2 — real-LLM grounded-план, не галлюцинирует селектор |
| replay + heal | прогнать → дрейфануть DOM (site→site-v2) → `run --replay --plan runs/<id>/plan.json` | M2/M3 — self-heal L1–L6 вживую + confidence-gate (**RISK-002!**) |
| determinism | 2× golden в отдельных процессах → сравнить байты | RISK-009 — byte-stability скриншотов |
| budget-kill | низкий бюджет → degradation planner→heuristic | M8 — real budget-ceiling |
| co-pilot UI | поднять control-api → открыть `docs/index.html` → Tests→Live на run_id | M14 — AG-UI-timeline chips, hitl-баннер, promote scenario→test |
| auto-HITL | `SENTINEL_AUTO_HITL_THRESHOLD=2` + спровоцировать heal-неудачи | M14 — авто-эскалация (graph-modes **и** replay: сигнал `hitl_needed`, #87) |
| multi-turn/takeover | chat с `conversation_id`; takeover/return (live-F4 нужен #58) | R2/R3 |

## C. Feedback-протокол — как передать данные мне
**Фаза 1 (эта машина).** Ничего переносить не нужно: **не чисти `runs/`** и веди `runs/LIVE_NOTES.md` — по каждому прогону: id · модель · мишень · ожидал/получил · exit-код · что сломалось/удивило. Дальше — «разбери live-прогоны», я читаю `runs/<id>/` напрямую.

**Фаза 2 (отдельная test-машина).** Прямого доступа к ней у меня нет — канал только один: **бандл, который ты переносишь**.
1. Каждый прогон = `runs/<id>/`: `plan.json` · `scenario.json` · `heal-report.json` · `report.json/html` · `llm-transcript.jsonl` · `metrics.prom` · `trace.zip` · `reconcile-report.json`.
2. **Сбор:** `scripts/collect-live-run.sh <run_id>` → `live-results/live-<id>.tar.gz`.
   Редакция включена **по умолчанию** и применяется к staging-копии (`runs/` не меняется): обнуляются значения `fill|type|select|press`-шагов без `secretRef` (LLM-authoring `secretRef` эмитить **не умеет** — пароль из цели-«залогинься» ложится в `plan.json` плейнтекстом), вычищаются `Authorization`/`Bearer`/`Cookie`-заголовки, секретные k/v и строки вида `sk-…`/JWT. Хеши, id и счётчики (`plan_hash`, golden-sha256, `step_id`, токены) **не трогаются** — на них держится анализ.
   **Никогда не собираются:** `checkpoint.db` (opaque-msgpack с сырым RunState) и `storage_state*.json` (auth-cookies + localStorage). Незнакомые файлы не уезжают (fail-safe), но перечисляются в warn.
   `trace.zip` — **исключён по умолчанию**, включается флагом `--with-trace` и едет **нередактированным**: у Playwright нет mask-API, trace несёт живой DOM (`input.value`) и тела запросов. Только для одноразового dev-стенда.
3. **Перенос: USB/scp.** **Не через git** — в `.gitignore` голые `*.zip`/`*.tar.gz` молча проглатывают бандл (`git add` = no-op), а промах редакции, попавший в историю, чинится только rewrite'ом; gitleaks внутрь gzip **не смотрит** и страховкой не является. Клади на dev-машину в `/opt/agent_development/live-results/`.
4. Бандл — **аналитический, не replay-safe**: тело отредактировано, `plan_hash` в нём больше не сходится с содержимым (это написано и в `README.txt` внутри бандла).

> Минимальный вариант без скрипта (только если он почему-то не запускается): скопируй `runs/<id>/*.json` + `LIVE_NOTES.md`, **без** `trace.zip`, `checkpoint.db` и `storage_state*.json`, и пробеги глазами на предмет введённых в формы значений.

## D. Exit-критерии M9-LIVE
- [ ] Real-LLM explore/author проходит на L1–L6 (grounded, exit 0, `planner: llm` в транскрипте — не heuristic).
- [ ] Live heal чинит дрейф с корректным confidence-gate (не ложный auto-heal).
- [ ] Golden byte-stable дважды (RISK-009 flip).
- [ ] Budget-kill срабатывает с graceful degradation.
- [ ] Co-pilot UI: live AG-UI-timeline + promote + auto-HITL наблюдаемы.
- [ ] Собраны реальные числа для RISK-002 (confidence) + RISK-003 (cost/латентность).
