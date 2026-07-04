# M9-LIVE — план живого прогона (локальная LLM) + feedback-протокол

> Рабочий док для offline-теста. Артефакт-дашборд (визуальная версия): claude.ai/code/artifact/23f4bcfd.
> **Статус:** черновик (закоммитить + `.en`-зеркало в следующем PR — M-STRUCTURED-OUT W1).

Цель: доказать, что offline-верифицированный конвейер (14+ вех) работает **вживую**. Тестируешь на своём ПК с локальной моделью; я на той же машине (`/opt/agent_development`) читаю артефакты после.

---

## A. Подготовка
1. **Локальная модель:** `docker compose --profile ollama up -d ollama` → `ollama pull <модель>`. Подбор — `docs/LOCAL_MODELS.md §3` + VRAM-калькулятор (Pages §5). Authoring (structured-JSON) — dense ≥14B (Qwen3-14B/32B) если хватает VRAM; heal — 7–8B. Экспорт: `export LLM_BASE_URL=http://localhost:11434/v1` + модели per-role `SENTINEL_PLANNER_MODEL`/`SENTINEL_HEAL_MODEL`.
2. **Билды:** `go build -o bin/agentctl ./cmd/agentctl && go build -o bin/store-gateway ./cmd/store-gateway` · `cd pw-executor && npm i && npm run build && npx playwright install chromium-headless-shell && cd ..` · `uv venv && uv pip install langgraph langgraph-checkpoint-sqlite anthropic openai grpcio grpcio-tools pyyaml`.
3. **Мишени:** начни с `testdata/fixtures/l1..l6.html` (file://, без сети): L1 trivial → L6 new-tab/multi-page. Потом — реальное приложение (опц. Keycloak-логин для auth-теста).

## B. Что прогнать / что наблюдать
| Проверка | Команда/действие | Подтверждает |
|---|---|---|
| explore/author | `bin/agentctl run --goal "…" --target file://$PWD/testdata/fixtures/l3-validation.html` | M9.1/M9.2 — real-LLM grounded-план, не галлюцинирует селектор |
| replay + heal | прогнать → дрейфануть DOM (site→site-v2) → `run --replay --plan runs/<id>/plan.json` | M2/M3 — self-heal L1–L6 вживую + confidence-gate (**RISK-002!**) |
| determinism | 2× golden в отдельных процессах → сравнить байты | RISK-009 — byte-stability скриншотов |
| budget-kill | низкий бюджет → degradation planner→heuristic | M8 — real budget-ceiling |
| co-pilot UI | поднять control-api → открыть `docs/index.html` → Tests→Live на run_id | M14 — AG-UI-timeline chips, hitl-баннер, promote scenario→test |
| auto-HITL | `SENTINEL_AUTO_HITL_THRESHOLD=2` + спровоцировать heal-неудачи | M14 — авто-эскалация (graph-modes) |
| multi-turn/takeover | chat с `conversation_id`; takeover/return (live-F4 нужен #58) | R2/R3 |

## C. Feedback-протокол — как передать данные мне (инструмент на ОТДЕЛЬНОЙ машине)
Инструмент разворачивается на **другой (test) машине** → напрямую `runs/` я НЕ прочту. Канал = **бандл артефактов, который ты переносишь** на машину, где я работаю (или в git-repo).
1. **Не чисти `runs/`.** Каждый прогон = `runs/<id>/`: `plan.json` · `scenario.json` · `heal-report.json` · `report.json/html` · `llm-transcript.jsonl` · `metrics.prom` · `trace.zip` · `reconcile-report.json`.
2. **Заметки:** `runs/LIVE_NOTES.md` — по каждому прогону: id · модель · мишень · ожидал/получил · exit-код · что сломалось/удивило · скрин если визуальное.
3. **Секреты/PII (важнее на отдельной машине!):** `trace.zip` может нести DOM/скриншоты реального приложения; `llm-transcript.jsonl` — промпты. Фикстуры безопасны. Реальное приложение → dev-стенд, либо `PW_NO_TRACE=1`, либо исключи `trace.zip` из бандла. Секреты в traces митигированы (`PW_NO_TRACE`, prompt_hash), но пробегись глазами перед переносом.
4. **Сбор+перенос:** `scripts/collect-live-run.sh <run_id>` (сделаю в M9-LIVE-подготовке) → `live-results/live-<id>.tar.gz` = JSON-артефакты + metrics + NOTES; **`trace.zip` по умолчанию ИСКЛЮЧ�ён** (PII), добавляется флагом `--with-trace`. Перенеси tarball туда, где я читаю: (а) git-commit в ветку `live-results/` (если test-машина видит remote), либо (б) USB/scp на dev-машину в `/opt/agent_development/live-results/`. В след. сессии — «разбери live-прогоны» → findings + калибровка RISK-002/003 реальными числами.

> Прямого доступа к test-инструменту у меня нет (air-gapped, отдельная машина — так и правильно). Единственный канал = **перенесённый бандл**. Поэтому `collect-live-run.sh` (+ redaction по умолчанию) — не опция, а часть M9-LIVE-подготовки. Минимальный вариант без скрипта: скопируй `runs/<id>/*.json` + `LIVE_NOTES.md` (без `trace.zip`) и вставь/перенеси мне.

## D. Exit-критерии M9-LIVE
- [ ] Real-LLM explore/author проходит на L1–L6 (grounded, exit 0).
- [ ] Live heal чинит дрейф с корректным confidence-gate (не ложный auto-heal).
- [ ] Golden byte-stable дважды (RISK-009 flip).
- [ ] Budget-kill срабатывает с graceful degradation.
- [ ] Co-pilot UI: live AG-UI-timeline + promote + auto-HITL наблюдаемы.
- [ ] Собраны реальные числа для RISK-002 (confidence) + RISK-003 (cost/латентность).
