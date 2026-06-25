# M1 Contract — "Autonomous Walk" (frozen 2026-06-23)

> 🌐 **Русский** (основная версия) · [English](M1_CONTRACT.en.md)

Цель: превратить линейный `perceive` из M0 в реальный **LangGraph StateGraph**, который автономно
исследует многостраничный сайт, сходится к **измеримой цели по покрытию** и фиксирует
детерминированный `plan.json` (+ `plan_hash`). Heal остаётся заглушкой (M2). Транспорт остаётся
JSON-RPC из M0 (миграция на MCP-SDK отложена до M2, GAP-VERIFY-002).

## Planner (ADR-011 — pluggable)
`Planner.propose(state) -> {action: PlannedAction|None, done: bool, reason: str}`
- **HeuristicPlanner** (по умолчанию, offline, детерминированный, $0): на текущей странице выбирает первый
  *неиспользованный* интерактивный элемент (в порядке чтения, предпочтение button/link); иначе переходит к следующему
  URL с тем же источником из `nav_frontier`; иначе `done`. Нет LLM, нет сети.
- **LLMPlanner** (Opus 4.8, T=0; `--planner llm`): prompt = краткое описание page_model + seen/exercised +
  frontier + оставшийся бюджет → JSON следующего действия. Требует `ANTHROPIC_API_KEY`; при отсутствии ключа или
  ошибке → **переключается на HeuristicPlanner** (graceful degradation). Записывает токены в транскрипт.
- Гейт сходимости (ADR-010) обеспечивается графом, НЕ планировщиком: `exploration_complete` устанавливается
  в True только когда `coverage_achieved >= coverage_target` И `nav_frontier` пуст. `max_steps` — предохранитель.

## RunState (подмножество M1; TypedDict)
```
run_id, run_mode='explore', target_url, base_origin, current_url
page_model: {url, title, aria, interactive: [{semantic_id, role, name, kind}], link_count}
exploration_plan: [PlannedAction]; plan_hash; current_step
coverage_target=0.85; interactive_seen:set; interactive_exercised:set
nav_frontier:list[str]; coverage_achieved:float; exploration_complete:bool
executed_actions:list; episodic:list; token_usage:dict
max_steps=40 (backstop); artifact_dir; errors:list
```
`PlannedAction = {step_id, intent, semantic_id, action_type:'navigate'|'click', target, locator:{role,name}|{css}, is_milestone}`
`semantic_id = sha1(f"{url_path}|{role}|{name}")[:12]` (стабильный между запусками для одного и того же DOM).

## Узлы (9) и рёбра
`perceive → ground → plan → act → verify → (heal*) → checkpoint → report`  (* heal = заглушка @ M1)
- **perceive**: `browser.snapshot` + `browser.currentUrl` → page_model; запускает трассировку в начале запуска.
- **ground**: разбор интерактивных элементов → назначение semantic_ids → обновление `interactive_seen`;
  `browser.links` → добавление непросмотренных URL с тем же источником в `nav_frontier`; пересчёт `coverage_achieved`.
- **plan**: `Planner.propose`; добавление PlannedAction ИЛИ установка `exploration_complete` (с гейтом). Запись в транскрипт.
- **act**: выполнение через pw-executor (`browser.navigate` | `browser.click`); запись executed_action;
  пометка semantic_id как использованного; `current_step++`.
- **verify**: повторный снимок; классификация PASS / changed. Heal в M1 — заглушка → ошибка логируется, выполнение продолжается.
- **heal**: ЗАГЛУШКА — логирует "heal deferred to M2"; направляет в checkpoint.
- **checkpoint**: LangGraph `SqliteSaver` checkpoint в **отдельную** БД `runs/<id>/checkpoint.db`.
- **report**: фиксация `plan.json` + вычисление `plan_hash`; остановка трассировки; вывод сводки.

Условные рёбра: `ground→report` если explore_complete; `plan→report` при done/`max_steps`; иначе цикл
`checkpoint→perceive`. `verify→heal` при ошибке (заглушка), иначе `verify→checkpoint`.

## pw-executor — новые инструменты (M1)
| Method | Params | Result |
|--------|--------|--------|
| `browser.click` | `{locator:{role,name}|{css}}` | `{clicked, url}` (через `getByRole`/css) |
| `browser.links` | — | `{links:[{href,text}]}` (якоря; brain фильтрует по same-origin) |
| `browser.currentUrl` | — | `{url, title}` |
(плюс из M0: `initialize`, `browser.navigate`, `browser.snapshot`, `browser.traceStop`, `shutdown`)

## Артефакты
- `plan.json`: `{plan_id, plan_hash, target_url, run_mode, coverage_target, coverage_achieved,
  interactive_seen, interactive_exercised, steps:[PlannedAction]}`.
- **`plan_hash`** = `sha256(canonical_json(steps))` — отсортированные ключи, разделители `(",",":")`, числа с плавающей запятой→6 знаков;
  **ИСКЛЮЧАЕТ** волатильные поля (`plan_id`, временные метки), поэтому повторные запуски над одним и тем же DOM побитово идентичны.
- `llm-transcript.jsonl`: одна строка на каждое решение планировщика `{step, planner, model|null, prompt_tokens|null,
  completion_tokens|null, decision, reason}`.
- `trace.zip` (как в M0).

## Env / spawn
- `brain/pyproject.toml`: зависимости `langgraph`, `langgraph-checkpoint-sqlite`, `anthropic`. Управляется **uv** (`.venv`).
- agentctl запускает Python из venv: переменная окружения `BRAIN_PYTHON` (по умолчанию `<repo>/.venv/bin/python`, fallback `python3`).
- Новые флаги agentctl: `--planner heuristic|llm` (по умолчанию heuristic), `--coverage-target` (0.85), `--max-steps` (40).

## Acceptance gate (Given/When/Then)
- **GIVEN** многостраничный фикстурный сайт (≥3 страницы, ≥5 интерактивных элементов, внутренние ссылки) в `testdata/site/`,
- **WHEN** `agentctl run --explore --target file://.../site/index.html --planner heuristic`,
- **THEN** `runs/<id>/plan.json` существует с **≥5 шагами**, записанным `coverage_achieved` (>0), наличием `plan_hash`,
  наличием `trace.zip`, exit 0; **И** второй идентичный запуск даёт **тот же `plan_hash`** (детерминизм).

## Вне scope (M2+)
real heal, MCP-SDK transport, gRPC + store-gateway, golden baselines, replay mode.
