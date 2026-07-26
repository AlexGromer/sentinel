# State Machine — Sentinel

> 🌐 **Русский** (основная версия) · [English](STATE_MACHINE.en.md)

Составлено по результатам проектного синтеза 2026-06-23; итоговое описание — в ../ARCHITECTURE.md (см. §7).

> **Примечание о моделях:** имена моделей в этом документе (Opus 4.8 / Sonnet 4.6) — **дефолты per-role**; planner/heal провайдер-агностичны начиная с M6 (ADR-019) — любой backend через `LLM_BACKEND*` (Anthropic или OpenAI-совместимый). `HeuristicPlanner` остаётся детерминированным якорем.

---

## 1. Фреймворк

Когнитивный цикл Sentinel реализован как **LangGraph `StateGraph`** (Python).
Всё промежуточное состояние между вызовами узлов сохраняет **`SqliteSaver` checkpointer**,
записывающий данные в *отдельный* SQLite-файл, отличный от основной базы данных Go `store-gateway`.
Именно это разделение обеспечивает реальную работу гарантии «единственного писателя» для основной БД.

| Область | Детали |
|---|---|
| Фреймворк | LangGraph `StateGraph` (Python, пакет `langgraph`) |
| Хранилище checkpoint | `langgraph.checkpoint.sqlite.SqliteSaver` |
| Путь к БД checkpoint | `<artifact_dir>/checkpoint.db` — один файл SQLite на прогон (`brain/__main__.py:135`), никогда не файл `store-gateway` |
| Ключ идентификации потока | `thread_id = run_id` |
| Продакшн-БД (`CHECKPOINT_DSN`) | Синхронный `PostgresSaver` (пакет `langgraph.checkpoint.postgres`) заменяет `SqliteSaver`, когда задана переменная окружения `CHECKPOINT_DSN` — тот же интерфейс, схема не меняется (`_checkpointer`, `brain/__main__.py:26-38`) |
| Уровень выполнения в браузере | **`pw-executor`** — наш собственный TypeScript-сервер, реализующий MCP/JSON-RPC 2.0 через stdio (создан самостоятельно, не куплен; заменяет любой готовый MCP-сервер браузера) |

> **Два независимых пути выполнения.** Этот `StateGraph` управляет режимами `explore` и `chat`
> (включая `goal`/`describe` через узел `scenario`, ADR-028/ADR-048) — внутри графа НЕТ ветвления
> по `run_mode` (`graph.py:463-475`: рёбра безусловны либо ведут через 4 роутер-функции, ни одна
> не читает `run_mode`). Режимы `replay` и `baseline` вообще не проходят через этот граф — они
> выполняются отдельным циклом `run_replay()` (`brain/replay.py`), диспетчеризуемым из
> `_run_replay()` (`brain/__main__.py:514`). Реальный движок восстановления локаторов
> (`HealingEngine.heal`, кеш + стратегии + LLM + визуальный режим) работает **только** в этом
> цикле — см. §3.11.

---

## 2. Общий объект состояния — `RunState` (TypedDict)

`RunState` — единственный общий объект, передаваемый через каждый узел (`brain/state.py:10-63`,
`TypedDict total=False`, 33 поля). Все поля, кроме служебных `_`-каналов, сохраняются в
контрольную точку при каждом вызове узла `checkpoint`.

> Ниже перечислены ТОЛЬКО поля, реально объявленные в `RunState`. Более ранняя версия этого
> документа описывала поля недостроенного дизайна (`session_id`, `aut_version`, детализированный
> `PageModel` с `a11y_tree`/`landmarks`/`forms`/`completeness_ratio`/хешами, `episodic_buffer`,
> `healing_context`/`heal_attempts`, `token_usage`/`token_budget`/`budget_warning_emitted`,
> `human_gate_*`, `run_dir`/`artifacts`/`step_failures`) — их нет ни в `brain/state.py`, ни где-либо
> ещё в дереве (проверено `grep`); они удалены из таблицы ниже.

### 2.1 Идентификация и конфигурация

| Поле | Описание |
|---|---|
| `run_id` | Идентификатор запуска; одновременно `thread_id` LangGraph-чекпойнтера |
| `run_mode` | `str`; наблюдаемые значения — `"explore"` (`_run_explore`) и `"chat"` (`_run_chat`), оба идут через ОДИН и тот же граф. Ни одна роутер-функция графа не читает `run_mode` |
| `target_url` | Корневой URL тестируемого приложения |
| `base_origin` | Origin цели — фильтрует `nav_frontier` по same-origin |
| `coverage_target` | Доля обнаруженных кнопок, которую нужно покрыть кликом до схождения. По умолчанию `0.85` |
| `max_steps` | Жёсткий лимит шагов исследования |
| `artifact_dir` | Каталог артефактов прогона (`plan.json`, `checkpoint.db`, LLM-транскрипт) |
| `goal` | NL-цель для `GoalPlanner` (goal-режим); `""` в чистом explore |
| `describe` | NL-описание для `DescribePlanner` (describe-режим); `""` иначе |

### 2.2 Диалог и карта сайта (ADR-028 / ADR-048)

| Поле | Описание |
|---|---|
| `messages` | Аккумулятор реплик разговора (`Annotated[list, add_messages]`) — LangGraph-редьюсер добавляет реплики между ходами. Пусто для одноразовых explore/goal/describe-прогонов |
| `site_map` | Карта сайта `page_path -> [element]`, накапливается узлом `ground` за весь проход explore |
| `phase` | `"explore"` \| `"scenario"` |
| `scenario_steps` | Grounded-шаги, добавленные узлом `scenario` в `exploration_plan` |
| `scenario_unmatched` | Черновые шаги/ссылки, которые не удалось привязать к реальному элементу |

### 2.3 Восприятие

| Поле | Описание |
|---|---|
| `current_url` | URL, загруженный в браузере в текущий момент |
| `page_model` | Словарь, собираемый `perceive`/`ground`: `{url, title, aria, nodeCount}` + `buttons` (добавляется в `ground`). **Не** содержит `a11y_tree`/`landmarks`/`forms`/`completeness_ratio` и хеши — этих подполей код не вычисляет (`graph.py:156-202`) |

### 2.4 Учёт исследования / сходимость

| Поле | Описание |
|---|---|
| `exploration_plan` | Упорядоченный список запланированных/выполненных шагов |
| `plan_hash` | SHA-256 канонического JSON шагов — вычисляется в узле `report`, **не** в `plan` |
| `current_step` | Номер последнего выполненного шага |
| `interactive_seen` | `semantic_id` всех обнаруженных кнопок |
| `interactive_exercised` | `semantic_id` кнопок, по которым выполнен клик |
| `interactive_failed` | **ADR-070** — `semantic_id` → сколько раз действие над элементом ПОДНЯЛО исключение. Существует потому, что `act` помечал элемент только при УСПЕХЕ: неподатливый контрол оставался кандидатом навсегда, и планировщик предлагал его каждый шаг (в живых логах — один клик 34 раза до `max_steps`). На пороге `_EXPLORE_FAIL_LIMIT` (env `SENTINEL_EXPLORE_FAIL_LIMIT`, дефолт 2) элемент выпадает из кандидатов; тот же бюджет применён к навигационным кандидатам |
| `visited_paths` | Посещённые пути страниц |
| `nav_frontier` | Необойдённые same-origin ссылки |
| `coverage_achieved` | `len(exercised) / max(1, len(seen))` |
| `exploration_complete` | Устанавливается узлом `plan`, когда `current_step >= max_steps`, либо нет кандидатов, либо (`coverage_achieved >= coverage_target` И `nav_frontier` пуст) — либо планировщик сам предложил `done` |
| `executed_actions` | Плоский журнал выполненных действий `{step_id, type, ok}` |
| `errors` | Список строк ошибок `act` |

### 2.5 Operator takeover (ADR-054)

| Поле | Описание |
|---|---|
| `takeover_returns` | Payload'ы возврата оператора — по одной записи на цикл takeover→return. Только для наблюдаемости: не входит в `plan.json`/`scenario.json`, не влияет на `plan_hash` |

### 2.6 Auto-HITL (ADR-055)

| Поле | Описание |
|---|---|
| `consecutive_heal_failures` | Счётчик подряд идущих промахов узла `heal` (в explore-графе `heal` — заглушка, см. §3.6, поэтому КАЖДЫЙ вход в него — промах); сбрасывается в 0 при успешном `verify`. Достижение `SENTINEL_AUTO_HITL_THRESHOLD` авто-взводит `_takeover_armed` в узле `checkpoint` |
| `failed_steps` | Суммарный счётчик неудачных `verify` (наблюдаемость / субстрат M15-метрик) |

### 2.7 Служебные каналы

| Поле | Описание |
|---|---|
| `_pending` | Запланированное, но ещё не выполненное действие (мост `plan` → `act`) |
| `_last_ok` | Результат последнего `act` (bool) |
| `_verify_ok` | Результат `verify` — см. §3.5 |
| `_takeover_armed` | Взводится узлом `checkpoint`, когда взят захват управления оператором или сработал порог авто-HITL; маршрутизирует в узел `takeover` |

### 2.8 Токен-бюджет — НЕ поле `RunState`

Учёт токенов — это **процесс-глобальный** `BudgetTracker` (`brain/budget.py`), а не поле
`RunState`: планировщик и `HealingEngine` читают `budget.tracker()` напрямую. Лимиты берутся из
окружения: `PLAN_TOKEN_LIMIT` (по умолчанию 50000), `HEAL_TOKEN_LIMIT` (по умолчанию 20000),
`TOTAL_TOKEN_LIMIT` (по умолчанию 0 = выключен). При достижении лимита `exceeded(role)` просто
возвращает `True` — вызывающий код (планировщик / `HealingEngine._llm_reground`) тихо деградирует,
без выделенного AG-UI- или лог-события уровня "warning". Событие `BUDGET_WARNING` в коде
отсутствует (`grep` по всему дереву — 0 совпадений).

---

## 3. Узлы

Граф содержит **10 именованных узлов** (`brain/graph.py:454-459`: `perceive`, `ground`, `plan`,
`act`, `verify`, `heal`, `checkpoint`, `takeover`, `scenario`, `report`) и два неявных встроенных
узла LangGraph (`START`, `END`). Внутри графа НЕТ ветвления по `run_mode` — все рёбра либо
безусловны, либо решаются одной из 4 роутер-функций (`route_entry`, `route_plan`, `route_verify`,
`route_checkpoint`), ни одна из которых не читает `run_mode`. Этот же граф обслуживает и explore,
и `chat` (multi-turn, ADR-048), и goal/describe (когда передан `scenario_head`, ADR-028).
Узел `checkpoint` при взведённом `_takeover_armed` маршрутизирует в выделенный `takeover`-узел
(безусловный `interrupt()` → пауза, оператор забирает управление → `Command(resume)`; приоритет
**abort > takeover**). Фреймворк автоматически связывает `START` с первым узлом (через
`route_entry`) и назначает `END` терминальным узлом графа.

Режимы `replay`/`baseline` НЕ используют этот граф вообще — они выполняются отдельным циклом
`run_replay()`, см. §3.11.

### Сводка по узлам

| # | Узел | LLM | Примечания |
|---|---|---|---|
| 1 | `perceive` | Нет | Снимок страницы (`browser.currentUrl` + `browser.snapshot`) |
| 2 | `ground` | Нет | Каталогизирует интерактивные элементы, растит `nav_frontier`/`site_map`, считает покрытие |
| 3 | `plan` | Условно | Зависит от `planner` (`HeuristicPlanner` детерминирован; LLM-планировщик — да) |
| 4 | `act` | Нет | Выполняет `_pending` через `pw-executor` |
| 5 | `verify` | Нет | Однострочный pass-through `ok = bool(_last_ok)` — НЕ классификатор |
| 6 | `heal` | Нет | **Заглушка** в explore-графе — лог + счётчик; реальный heal только в `run_replay()` (§3.11) |
| 7 | `checkpoint` | Нет | Опрос оркестратора (abort/takeover) + авто-HITL порог |
| 8 | `takeover` | Нет | `interrupt()` — пауза для захвата управления оператором |
| 9 | `scenario` | Условно | Только если передан `scenario_head` (goal/describe); также точка возобновления тёплого чата |
| 10 | `report` | Нет | Терминальный узел; замораживает `plan.json` + `plan_hash` |

### 3.1 `perceive`

**LLM: нет.**

- Вызывает `pw-executor`: `browser.currentUrl` + `browser.snapshot`, собирает
  `page_model = {url, title, aria, nodeCount}`.
- На самом первом проходе (`page_model` ещё пуст) эмитит AG-UI `run.started`; всегда эмитит
  `state.transition(to="perceive")`.
- **Не вычисляет** `completeness_ratio`, `a11y_hash`, `screenshot_hash`, `dom_subtree_hash` и не
  принимает решения о вызове `screenshot()` — этих механизмов в коде нет (`grep completeness_ratio`
  находит только упоминания в документах дизайна, не в реализации).
- **Не управляет** стартом/остановкой Playwright-трассировки — трассировка останавливается один
  раз, уже после завершения графа, в `brain/__main__.py` (`browser.traceStop`), не построчно на
  каждый вход в `perceive`.

### 3.2 `ground`

**LLM: нет.**

- Каталогизирует интерактивные элементы (`_elements_from_interactives`, `graph.py:54-102`): роль,
  имя, `testid`, primary-локатор + упорядоченный список `alternatives` (`testid`/`role_name`/
  `label`/`text_role`, приоры 0.95/0.90/0.88/0.80).
- Покрытие считается только по кнопкам (`role == "button"`); ссылки идут в `nav_frontier`.
- Пополняет `site_map[path]` полным набором элементов (кнопки + ссылки + поля форм) — карта,
  которую использует узел `scenario` (ADR-028).
- Пересчитывает `interactive_seen`, `nav_frontier` (только same-origin, ещё не посещённые),
  `visited_paths`, `coverage_achieved = exercised / max(1, seen)`.
- **Не сверяется** с golden-baseline (`a11y_hash`/`screenshot_hash`) — этой проверки в explore-графе
  нет; golden-diff реализован только в `run_replay()` (§3.11).
- Безусловное ребро `ground → plan` (`graph.py:464`) — нет ветвления по `run_mode`.

### 3.3 `plan`

**LLM: условно** — зависит от переданного `planner` (`HeuristicPlanner` детерминирован;
LLM-backed планировщик, `GoalPlanner`/`DescribePlanner` для фазы 1 выбираются в `_run_explore`,
`brain/__main__.py:102-108`).

- Собирает кандидатов: непокрытые кнопки (`click`) + весь `nav_frontier` (`navigate`).
- Завершает исследование (`exploration_complete=True`), когда `current_step >= max_steps`, либо
  кандидатов не осталось, либо (`coverage_achieved >= coverage_target` И `nav_frontier` пуст) —
  либо когда сам планировщик вернул `decision.get("done")`.
- Иначе просит `planner.propose(...)` следующее действие, добавляет его в `exploration_plan` и
  кладёт в `_pending` (мост к `act`).
- Вызывает `rc.report(...)` (RunControl); если оркестратор вернул `ABORT` — сходится немедленно
  (`exploration_complete=True`), не дожидаясь `checkpoint`.
- **НЕ замораживает** `plan.json` и **не вычисляет** `plan_hash` — это делает узел `report`
  (§3.10), не `plan`.
- Пишет запись в LLM-транскрипт (`tx_write`) на каждом шаге, независимо от исхода.

### 3.4 `act`

**LLM: нет.**

- Выполняет `_pending` через `pw-executor` (`navigate`/`click`/`fill`/`type`/`select`/`press`/
  `assert`); для `fill`/`type`/`select`/`assert` переиспользует диспетчер `replay.py:_act`/
  `_expect_params` — единый источник истины для этих глаголов между explore и replay.
- При успехе: отмечает кнопку `exercised` (только для `click`), добавляет запись в
  `executed_actions`, ставит `_last_ok=True`.
- При исключении: добавляет строку в `errors`, ставит `_last_ok=False`.
- Эмитит AG-UI `tool.call` и `step.progress`. Безусловное ребро `act → verify`.

### 3.5 `verify`

**LLM: нет.**

- Однострочный pass-through: `ok = bool(state.get("_last_ok", True))`.
- **НЕ классифицирует** результат на `PASS`/`LOCATOR_STALE`/`ELEMENT_GONE`/`TIMING`/
  `UNEXPECTED_ERROR` — таких категорий в коде нет (`grep` по дереву — 0 совпадений); нет
  повторного снимка страницы и нет вызова LLM для «мягких» утверждений.
- При `ok=True`: сбрасывает `consecutive_heal_failures=0` — единственная точка сброса счётчика
  авто-HITL (ADR-055).
- При `ok=False`: увеличивает `failed_steps`.
- Маршрут (`route_verify`, `graph.py:428-429`): `"checkpoint" if ok else "heal"` — ровно 2 исхода.

### 3.6 `heal`

**LLM: нет — ЗАГЛУШКА в explore-графе.**

> Docstring узла в коде: *«STUB in the explore graph (explore discovers, it does not heal). See
> brain/replay.py.»*

- Логирует и увеличивает `consecutive_heal_failures` — узел физически не может починить
  локатор: здесь нет ни кеша, ни ротации стратегий, ни LLM-переgrounding, ни визуального режима.
- Каждый вход в этот узел — промах по определению; используется как сигнал авто-HITL (§2.6).
- Реальный движок восстановления (`HealingEngine.heal`, кеш + стратегии + LLM + visual
  set-of-marks) работает **только** в отдельном цикле `run_replay()` — см. §3.11.
- Безусловное ребро `heal → checkpoint`.

### 3.7 `checkpoint`

**LLM: нет.**

- `rc.poll(run_id, "checkpoint")` — опрос Go-оркестратора (RunControl gRPC, ADR-054):
  - `ABORT` → `exploration_complete=True`, снимает `_takeover_armed` (сходится немедленно;
    **abort приоритетнее takeover**).
  - `TAKEOVER` → взводит `_takeover_armed=True` (сама пауза случится в следующем узле `takeover`,
    не здесь — решение фиксируется в состоянии, чтобы `interrupt()` детерминированно
    воспроизводился при resume).
  - иначе: если `consecutive_heal_failures >= SENTINEL_AUTO_HITL_THRESHOLD` (env, по умолчанию
    `0` = выключено) — тоже взводит `_takeover_armed=True` и эмитит AG-UI `hitl_needed`.
- Здесь **нет** записи в `store-gateway`, **нет** вызовов `PersistenceService`, **нет** сброса
  `heal_attempts`/`healing_context` (этих полей не существует) — саму LangGraph-контрольную точку
  сбрасывает фреймворк (компилированный граф с `checkpointer=saver`), а не код этого узла.
- Маршрут (`route_checkpoint`, `graph.py:431-438`) — ровно 3 цели: `exploration_complete →
  scenario`; иначе `_takeover_armed → takeover`; иначе `current_step >= max_steps → scenario`,
  else `→ perceive`.

### 3.8 `takeover`

**LLM: нет.**

- Единственный узел, вызывающий `interrupt({"reason": "operator_takeover", "run_id": ...})` —
  безусловно (решение уже зафиксировано в `_takeover_armed` узлом `checkpoint`, поэтому повторный
  вход при resume безопасен и идемпотентен).
- `app.invoke()` возвращает управление с `__interrupt__`; живой браузер передаётся оператору
  (CDP, M9-LIVE). Оркестратор шлёт `Command(resume=...)` на Return — цикл продолжается с того же
  места (`_resume_through_takeovers`, `brain/__main__.py:61-85`).
- На resume: снимает `_takeover_armed`, добавляет payload возврата в `takeover_returns`.
- Безусловное ребро `takeover → checkpoint` (повторный опрос перед продолжением — на случай, если
  Return ещё не долетел до оркестратора).

### 3.9 `scenario`

**LLM: условно** — только если графу передан `scenario_head` (`GoalPlanner` для goal-режима,
`DescribePlanner` для describe-режима); no-op (`{}`) в чистом explore.

- Фаза 2 (ADR-028): авторизует grounded-сценарий поверх **полного** `site_map`, а не только
  явно покрытых кнопок — `goal`-режим строит `refs` и вызывает `ground_scenario`; `describe`-режим
  делает LLM-черновик и детерминированно сверяет его (`reconcile`) с реальной картой.
- **M9.10 (ADR-048): также точка ВОЗОБНОВЛЕНИЯ тёплого multi-turn диалога.** `route_entry`
  (`graph.py:440-445`) направляет `START` прямо сюда, минуя `perceive`, когда в состоянии уже есть
  и `site_map`, и `messages` (тёплый поток) — переавторизует поверх сохранённой карты с учётом
  предыдущих реплик как refine-контекста (`_capped_history`, ограничено
  `SENTINEL_REFINE_HISTORY_KEEP`, по умолчанию 6 ходов).
- Добавляет grounded-шаги в `exploration_plan`, записывает `scenario_unmatched`, добавляет
  реплику-резюме в `messages` для следующего хода.
- Безусловное ребро `scenario → report`.

### 3.10 `report`

**LLM: нет. Терминальный узел.**

- Вычисляет `plan_hash` (канонический SHA-256 по `exploration_plan`) и пишет `plan.json` в
  `artifact_dir` — это **единственное** место, где план реально замораживается.
- Добавляет в `plan.json` `tokens` (сводка `budget.tracker().summary()`, §2.8) и `models`.
- Эмитит AG-UI `verdict` — best-effort оценка ПО СВОЕМУ взгляду на прогон (есть ли `errors`),
  **не** истинный код завершения процесса: реальный exit code вычисляется в `brain/__main__.py`
  уже после возврата `app.invoke()`, вне графа.
- **Не вызывает** `WriteRunResult`, не собирает единый `RunResult` (аудит восстановления,
  golden-diff предупреждения, карту покрытия, разбивку стоимости, список «шлюза») и не эмитит
  событие `DONE` — этих конструкций в коде графа нет.
- Безусловное ребро `report → END`.

### 3.11 `run_replay()` — отдельный цикл replay/baseline (в обход графа)

`run_mode in {"replay", "baseline"}` НЕ проходит через `StateGraph` вообще: `_run_replay()`
(`brain/__main__.py:514`) напрямую вызывает `run_replay()` (`brain/replay.py:101`) — обычный
Python-цикл по замороженным шагам `plan.json`, без LangGraph, без чекпойнтера, без узлов
`perceive`/`ground`/`plan`.

- **Проверка целостности (ADR-006):** пересчитывает `plan_hash` по шагам; при несовпадении (и без
  `FORCE_REPLAY=1`) — жёсткий abort, `exit_code=3`, ничего не выполняется.
- **По каждому шагу:** `navigate`/`assert`/`press` выполняются напрямую; для `click`/`fill`/`type`/
  `select` сперва `browser.probe` на primary-локаторе — при `count==1` действие выполняется сразу,
  иначе вызывается **реальный** `HealingEngine.heal(ctx)` (`brain/healing.py:56-112`):
  1. Кеш (`store.lookup`) по `(page_path, semantic_id, dom_hash)`; промах — `store.evict_stale`.
  2. Ротация стратегий по записанным `alternatives`: первый локатор, разрешающийся ровно в 1
     элемент (`brain/healing.py:26-27`, `PRIORS`):

     | Стратегия | Источник | Prior |
     |---|---|---|
     | `testid` | генерируется `ground()` из `data-testid`/`data-cy`/`data-pw` | 0.95 |
     | `role_name` | генерируется `ground()` — ARIA role + accessible name | 0.90 |
     | `label` | генерируется `ground()` из `aria-label` (не для кнопок) | 0.88 |
     | `text_role` | генерируется `ground()` из видимого текста | 0.80 |
     | `css` | ТОЛЬКО из LLM-переgrounding (шаг 3 ниже) | 0.65, далее ×0.90 скидка на самоуверенность |
     | `xpath` | объявлена в `PRIORS`; генерируется `record_bridge.py` (записанные extension-сценарии), не `ground()` | 0.45 |
     | `visual` | ТОЛЬКО из visual set-of-marks (шаг 4) | 0.80 (в FLAGGED-диапазоне по дизайну) |

  3. Если ротация не дала результата — LLM-переgrounding (`_llm_reground`, структурированный
     JSON-ответ с CSS-селектором), только если задан LLM-backend (`use_llm=True`, обычно
     `HEAL_LLM=1`) и бюджет `heal` не исчерпан (`budget.tracker().exceeded("heal")`).
  4. Если и это не дало результата — визуальный set-of-marks (`_visual_reground`), только если
     `use_visual=True` **и** backend поддерживает vision. Нет проверки `completeness_ratio` —
     такого поля нигде в коде нет.
  5. Кандидат повторно пробируется живым DOM; если не разрешается ровно в 1 элемент — уверенность
     обнуляется.
  6. Порог: `confidence >= 0.85` → `auto_healed` (локатор сохраняется как `active`);
     `0.60–0.84` → `flagged` (применяется оптимистически, сохраняется с пометкой на ревью);
     `< 0.60` → `needs_review`, локатор НЕ сохраняется, шаг падает.
  7. Каждая попытка пишет строку в SQLite-таблицу `healing_audit` (append-only,
     `brain/store.py:145-152`) — никаких `UPDATE`/`DELETE`.
- **Карантин нестабильных шагов:** `store.record_step(plan_id, step_key, passed, aut_version)`
  ведёт скользящее окно последних 5 исходов НА AUT SHA (сбрасывается при смене SHA); ≥3 провалов
  из 5 → карантин (`quarantined=True`, не считается в `exit 1`); 3 подряд успеха снимают карантин
  (`brain/store.py:179-196`). Golden-diff регрессии (`exit 2`) карантин НЕ подавляет.
- **Нет ретрай-цикла с лимитом попыток** на один шаг (никакого поля `heal_attempts`) — на каждый
  шаг ровно один вызов `heal.heal(ctx)`.
- **AG-UI + авто-HITL (M14 tail 2, ADR-055):** эмитит `run.started`/`step.progress`/`heal`/
  `verdict`; считает `consecutive_heal_failures` (та же семантика и порог
  `SENTINEL_AUTO_HITL_THRESHOLD`, что и в графе, `brain/replay.py:143-153`) и эмитит `hitl_needed`
  при достижении порога — но реальной паузы (interrupt/resume) в replay-цикле нет: живой
  авто-takeover в середине replay — задача M9-LIVE.

---

## 4. Рёбра

### 4.1 Таблица рёбер

Источник истины: `brain/graph.py:454-475` (`b.add_edge`/`b.add_conditional_edges`). Все рёбра ниже
верны для ОДНОГО графа, используемого explore/chat/goal/describe — нет ветвления по `run_mode`.

| От | До | Условие / Триггер |
|---|---|---|
| `START` | `perceive` | `route_entry`: холодный старт — `site_map` и/или `messages` пусты |
| `START` | `scenario` | `route_entry`: тёплый resume — заполнены и `site_map`, и `messages` (ADR-048) |
| `perceive` | `ground` | Всегда |
| `ground` | `plan` | Всегда |
| `plan` | `act` | `route_plan`: `not exploration_complete` — следующее действие поставлено в `_pending` |
| `plan` | `scenario` | `route_plan`: `exploration_complete` |
| `act` | `verify` | Всегда |
| `verify` | `checkpoint` | `route_verify`: `_verify_ok == True` |
| `verify` | `heal` | `route_verify`: `_verify_ok == False` |
| `heal` | `checkpoint` | Всегда (узел-заглушка в explore-графе, §3.6) |
| `checkpoint` | `scenario` | `route_checkpoint`: `exploration_complete` ИЛИ `current_step >= max_steps` |
| `checkpoint` | `takeover` | `route_checkpoint`: `_takeover_armed` (и НЕ `exploration_complete`) |
| `checkpoint` | `perceive` | `route_checkpoint`: иначе — обычное продолжение цикла |
| `takeover` | `checkpoint` | Всегда (повторный опрос оркестратора перед продолжением) |
| `scenario` | `report` | Всегда |
| `report` | `END` | Всегда (терминальный) |

### 4.2 Роутер-функции — дословно

Ровно 4 функции генерируют условные рёбра; ни одна не читает `run_mode` (`graph.py:425-445`):

```python
def route_plan(state):
    return "scenario" if state.get("exploration_complete") else "act"

def route_verify(state):
    return "checkpoint" if state.get("_verify_ok", True) else "heal"

def route_checkpoint(state):
    if state.get("exploration_complete"):
        return "scenario"
    if state.get("_takeover_armed"):
        return "takeover"
    return "scenario" if state.get("current_step", 0) >= state.get("max_steps", 40) else "perceive"

def route_entry(state):
    return "scenario" if (state.get("site_map") and state.get("messages")) else "perceive"
```

---

## 5. ASCII-диаграмма потока

```
                                  ┌─────────┐
                          ┌────── │  START  │ ──────┐
                          │       └─────────┘       │
                (site_map+messages: warm resume)  (иначе: cold start)
                          │                          │
                          ▼                          ▼
                    ┌───────────┐              ┌───────────┐
        ┌─────────► │  scenario │ ◄──────┐     │  perceive │◄────────────┐
        │           └─────┬─────┘        │     └─────┬─────┘             │
        │                 │ always       │           │ always            │
        │                 ▼              │           ▼                   │
        │           ┌───────────┐        │     ┌───────────┐             │
        │           │  report   │        │     │   ground  │             │
        │           └─────┬─────┘        │     └─────┬─────┘             │
        │                 │ always       │           │ always            │
        │                 ▼              │           ▼                   │
        │              ┌─────┐           │     ┌───────────┐             │
        │              │ END │           │     │   plan    │             │
        │              └─────┘           │     └─────┬─────┘             │
        │                                │           │                   │
        │                    exploration_complete   not exploration_complete
        │                                │           │                   │
        └────────────────────────────────┘           ▼                   │
        ▲                                       ┌───────────┐            │
        │                                       │    act    │            │
        │                                       └─────┬─────┘            │
        │                                             │ always           │
        │                                             ▼                  │
        │                                       ┌───────────┐            │
        │                                       │  verify   │            │
        │                                       └─────┬─────┘            │
        │                                _verify_ok │  │ NOT _verify_ok  │
        │                                    ┌───────┘  └───────┐        │
        │                                    ▼                  ▼        │
        │                              ┌───────────┐      ┌───────────┐  │
        │                    ┌────────►│ checkpoint│      │   heal    │  │
        │                    │         └─────┬─────┘      │ (STUB)    │  │
        │                    │               │            └─────┬─────┘  │
        │            exploration_complete OR │ always (heal→checkpoint)  │
        │            current_step>=max_steps │◄───────────────────┘      │
        │                    │               │                           │
        │                    │      _takeover_armed                      │
        │                    │               │                           │
        │                    ▼               ▼                           │
        └────────────(scenario, выше)   ┌───────────┐                    │
                                         │ takeover  │                    │
                                         │(interrupt)│                    │
                                         └─────┬─────┘                    │
                                               │ always                   │
                                               └──────────► checkpoint    │
                                        (иначе, из checkpoint) ───────────┘
                                            perceive (нормальный цикл)
```

> Упрощённая схема реальных рёбер `graph.py:454-475` (см. точную таблицу в §4.1). Нет узлов
> `LOCATOR_STALE`/`ELEMENT_GONE`/`TIMING`/`human gate` — этих веток в коде нет.
> `checkpoint → perceive` — основное обратное ребро, управляющее циклом explore.

---

## 6. Использование LLM по узлам — краткий справочник

| Узел | LLM-вызов | Когда |
|---|---|---|
| `perceive` | Нет | — |
| `ground` | Нет | — |
| `plan` | Условно | Зависит от `planner`: `HeuristicPlanner` детерминирован; LLM-планировщик/`GoalPlanner`/`DescribePlanner` — да |
| `act` | Нет | — |
| `verify` | Нет | Однострочный pass-through, LLM не вызывается (§3.5) |
| `heal` | Нет | Заглушка в explore-графе (§3.6); реальный LLM-heal — только в `run_replay()` (§3.11) |
| `checkpoint` | Нет | — |
| `takeover` | Нет | — |
| `scenario` | Условно | Только если передан `scenario_head` (goal/describe) |
| `report` | Нет | — |

**Токен-бюджет** (§2.8) — процесс-глобальный `brain/budget.py:BudgetTracker`, НЕ поле `RunState`:

| Переменная окружения | По умолчанию |
|---|---|
| `PLAN_TOKEN_LIMIT` | 50 000 токенов / прогон |
| `HEAL_TOKEN_LIMIT` | 20 000 токенов / прогон |
| `TOTAL_TOKEN_LIMIT` | `0` (выключено) |

При достижении лимита `exceeded(role)` возвращает `True`, и вызывающий код (планировщик /
`HealingEngine._llm_reground`) тихо деградирует (планировщик падает на эвристику; heal — только
на детерминированную ротацию стратегий), без выделенного события — **`BUDGET_WARNING` в коде не
существует** (`grep` по всему дереву — 0 совпадений).

---

## 7. pw-executor — замечание о сборке

Все упоминания `pw-executor` выше относятся к нашему **собственному TypeScript-серверу выполнения Playwright**,
который мы создаём и поддерживаем. Он реализует транспортный интерфейс MCP/JSON-RPC 2.0 через stdio и
предоставляет примитивы браузера (навигация, `accessibility_snapshot`, `click`/`type`,
управление трассировкой и `screenshot`) Python-мозгу через stdio-канал. Мозг запускает его
как дочерний процесс и управляет его жизненным циклом; SIGTERM каскадирует при выходе мозга.

Любые детали API-поверхности, помеченные как **VERIFY**, необходимо подтвердить по реальной
реализации `pw-executor` перед развёртыванием.
