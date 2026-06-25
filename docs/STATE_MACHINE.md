# State Machine — Sentinel

> 🌐 **Русский** (основная версия) · [English](STATE_MACHINE.en.md)

Составлено по результатам проектного синтеза 2026-06-23; итоговое описание — в ../ARCHITECTURE.md (см. §7).

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
| Путь к БД checkpoint (CI) | `/tmp/agent-{run_id}-ckpt.db` — один файл на задание, без конкуренции за запись |
| Путь к БД checkpoint (сервис) | Отдельный файл или схема `AsyncPostgresSaver`, никогда не store-gateway файл |
| Ключ идентификации потока | `thread_id = run_id` |
| Замена в продакшне (K3s, M5) | `AsyncPostgresSaver` заменяет `SqliteSaver` — один аргумент конструктора, схема не меняется |
| Уровень выполнения в браузере | **`pw-executor`** — наш собственный TypeScript-сервер, реализующий MCP/JSON-RPC 2.0 через stdio (создан самостоятельно, не куплен; заменяет любой готовый MCP-сервер браузера) |

---

## 2. Общий объект состояния — `RunState` (TypedDict)

`RunState` — единственный общий объект, передаваемый через каждый узел.
Все поля сохраняются в контрольную точку при каждом вызове узла `checkpoint`.

### 2.1 Идентификация и режим

| Поле | Тип | Описание |
|---|---|---|
| `session_id` | `str` | Уникальный идентификатор сессии |
| `run_id` | `str` | Уникальный идентификатор запуска; одновременно служит LangGraph `thread_id` |
| `run_mode` | `Literal["explore", "replay", "ci"]` | Определяет, какие узлы активны, а какие пропускаются |
| `target_url` | `str` | Корневой URL тестируемого приложения |
| `aut_version` | `str` | Git SHA тестируемого приложения, записывается при старте запуска |
| `current_url` | `str` | URL, загруженный в браузере в текущий момент |

### 2.2 Восприятие

| Поле | Тип | Описание |
|---|---|---|
| `page_model` | `PageModel` | Разобранное представление страницы: `{url, title, a11y_tree: dict, landmarks, forms, interactive_elements, completeness_ratio, a11y_hash, screenshot_hash, dom_subtree_hash}` |

### 2.3 План

| Поле | Тип | Описание |
|---|---|---|
| `exploration_plan` | `list[PlannedAction]` | Упорядоченная последовательность запланированных шагов. Каждый `PlannedAction`: `{step_id, intent, semantic_id, action_type, locator, locator_alternatives[L1..L6], value?, expected_outcome, assertion, is_critical, is_milestone, healed: bool}` |
| `plan_hash` | `str` | SHA-256 канонического JSON всех шагов (ключи отсортированы, числа с плавающей точкой нормализованы до 6 знаков). Жёсткий аварийный останов при несоответствии в режиме replay/ci |
| `current_step` | `int` | Индекс в `exploration_plan` |

### 2.4 Покрытие / Сходимость

> Эти поля заменяют флаг `exploration_complete`, который ранее устанавливался только LLM.
> LLM может *предложить* завершение, но не может его принудить; решение принимает метрика.

| Поле | Тип | Описание |
|---|---|---|
| `coverage_target` | `float` | Доля обнаруженных интерактивных элементов, с которыми необходимо провести взаимодействие перед завершением исследования. По умолчанию: `0.85` |
| `interactive_seen` | `set[str]` | Семантические идентификаторы всех обнаруженных интерактивных элементов |
| `interactive_exercised` | `set[str]` | Семантические идентификаторы интерактивных элементов, с которыми выполнено взаимодействие |
| `nav_frontier` | `deque[str]` | Неисследованные URL и ссылки, оставшиеся в очереди |
| `coverage_achieved` | `float` | Вычисляется как `len(exercised) / max(1, len(seen))` |
| `exploration_complete` | `bool` | `True` только когда `coverage_achieved >= coverage_target` И `nav_frontier` пуст |

### 2.5 Эпизодическая память

| Поле | Тип | Описание |
|---|---|---|
| `episodic_buffer` | `deque[EpisodicEvent]` | Ограниченный кольцевой буфер, максимум 50 записей. При заполнении старейшие события суммируются LLM (Sonnet) в краткие сводки (~200 токенов) для ограничения роста контекста |
| `executed_actions` | `list[ExecutedAction]` | Полная история действий: `{step, action_type, locator, outcome, duration_ms, pre_hash, post_hash, healing_flagged}` |

### 2.6 Восстановление

| Поле | Тип | Описание |
|---|---|---|
| `healing_context` | `Optional[HealingContext]` | Активный контекст восстановления: `{semantic_id, failure_type, attempted_locator, element_description}`. `None`, когда восстановление не выполняется |
| `heal_attempts` | `int` | Счётчик попыток восстановления на шаг. Сбрасывается при каждом `checkpoint`. Жёсткий лимит: 3 в режиме explore, 2 в критическом пути replay |
| `pending_human_review` | `list[HealCandidate]` | Кандидаты на восстановление, ожидающие решения человека на «шлюзе» |
| `healed_locators` | `list[HealedLocator]` | Восстановленные локаторы, ожидающие сброса в `store-gateway` на следующем `checkpoint` |

### 2.7 Токен-бюджет

| Поле | Тип | Описание |
|---|---|---|
| `token_usage` | `dict[str, TokenCount]` | Использование по идентификатору модели: `model_id → {prompt, completion, cost_usd}`. Счётчик в процессе; оркестратор Go независимо применяет жёсткий потолок |
| `token_budget` | `dict[str, int]` | Лимиты бюджета по идентификатору модели (из конфигурации). По умолчанию: 50k токенов/запуск для Opus 4.8 (plan), 20k токенов/запуск для Sonnet 4.6 (heal) |
| `budget_warning_emitted` | `bool` | Устанавливается при расходовании 80% любого бюджета; предотвращает дублирование событий `BUDGET_WARNING` |

### 2.8 Шлюз оператора / Управление

| Поле | Тип | Описание |
|---|---|---|
| `human_gate_pending` | `bool` | `True`, когда запуск приостановлен в `checkpoint`, ожидая решения оператора |
| `human_gate_reason` | `Optional[str]` | Понятное человеку объяснение причины активации «шлюза» |
| `human_gate_decision` | `Optional[Literal["approve", "skip", "abort"]]` | Решение, устанавливаемое командой `agentctl gate approve|skip|abort` |
| `human_gate_resolved_locator` | `Optional[str]` | Локатор, предоставленный оператором при решении `"approve"` |
| `stop_signal` | `bool` | Флаг останова, введённый извне (например, таймаут CI или оператор `agentctl run --stop`) |

### 2.9 Артефакты

| Поле | Тип | Описание |
|---|---|---|
| `run_dir` | `str` | Путь в файловой системе к каталогу артефактов данного запуска |
| `artifacts` | `RunArtifacts` | `{trace_path, screenshot_paths, spec_path, report_path}` — пути к сгенерированным файлам |
| `step_failures` | `dict[str, int]` | `step_key → consecutive_failure_count`. Входные данные для логики карантина нестабильных тестов с учётом AUT SHA |

---

## 3. Узлы

Граф содержит **8 именованных узлов** и два неявных встроенных узла LangGraph (`START`, `END`).
Фреймворк автоматически связывает `START` с первым узлом и назначает `END` терминальным узлом графа.

### Сводка по узлам

| # | Узел | LLM | Модель | Примечания |
|---|---|---|---|---|
| 1 | `perceive` | Нет | — | Вход каждого цикла; вызывает `pw-executor` для получения a11y-снимка |
| 2 | `ground` | Нет | — | Разбирает `PageModel`; в режиме replay проверяет соответствие эталонным базовым линиям |
| 3 | `plan` | Да | Opus 4.8 | **Только в режиме explore** — полностью пропускается в replay/ci |
| 4 | `act` | Нет | — | Выполняет текущий шаг через `pw-executor` |
| 5 | `verify` | Условно | Sonnet 4.6 | Sonnet только для мягкой проверки утверждений в режиме explore; детерминирован в replay |
| 6 | `heal` | Условно | Sonnet 4.6 | Sonnet для повторного связывания с a11y-деревом (L5+); визуальный шлюзовый режим тоже Sonnet |
| 7 | `checkpoint` | Нет | — | Сбрасывает LangGraph checkpoint + записывает в `store-gateway`; обрабатывает паузу на «шлюзе» оператора |
| 8 | `report` | Нет | — | Терминальный узел; собирает и отправляет `RunResult` |

### 3.1 `perceive`

**LLM: нет.**

Точка входа каждого цикла агента.

- Вызывает `pw-executor` (через MCP/JSON-RPC 2.0 по stdio) для `accessibility_snapshot()`.
- Вычисляет `completeness_ratio` (именованные интерактивные элементы / все интерактивные элементы).
- Если `completeness_ratio < 0.30` (canvas, shadow DOM, кастомные элементы, cross-origin iframes),
  дополнительно вызывает `screenshot()` у `pw-executor` для формирования контекста set-of-marks
  при визуальном восстановлении — этот вызов защищён шлюзом и не выполняется на каждом цикле.
- Вычисляет `a11y_hash`, `screenshot_hash` и `dom_subtree_hash` (с областью видимости поддерева,
  а не всей страницы, чтобы изменения в несвязанных рекламных баннерах не инвалидировали
  все кешированные локаторы).
- Запускает или возобновляет Playwright-трассировку через `pw-executor` при старте запуска.

### 3.2 `ground`

**LLM: нет.**

- Разбирает сырое a11y-дерево в типизированную структуру `PageModel`.
- Обновляет `interactive_seen` и `nav_frontier` на основе вновь обнаруженных элементов и ссылок.
- Вычисляет `coverage_achieved = len(interactive_exercised) / max(1, len(interactive_seen))`.
- Устанавливает `exploration_complete = True` только когда `coverage_achieved >= coverage_target`
  И `nav_frontier` пуст.
- **В режиме replay/ci:** проверяет `a11y_hash` и `screenshot_hash` по неизменяемой эталонной
  базовой линии для каждого этапного шага. Генерирует `STRUCTURAL_CHANGE` при дрейфе a11y
  или `VISUAL_WARN` при расхождении screenshot-хеша, не считая шаг провальным.

### 3.3 `plan`

**LLM: Opus 4.8, temperature = 0. Только режим explore.**

- Полностью пропускается в режимах `replay` и `ci` — `ground` направляет управление прямо в `act`.
- Входной контекст: `page_model`, хвост эпизодических событий из `episodic_buffer`, `nav_frontier`,
  оставшийся токен-бюджет и `coverage_achieved`.
- Вывод: одна или несколько записей `PlannedAction` для добавления в `exploration_plan`, либо
  *предложение* `exploration_complete = True`.
- LLM может предложить завершение, но флаг устанавливается только при независимом
  подтверждении метрикой покрытия (авторитетная проверка принадлежит `ground`).
- Выполняет внутрипроцессную предварительную проверку бюджета перед вызовом LLM;
  при недостатке бюджета деградирует мягко (частичная заморозка плана), а не аварийно завершается.
- По завершении исследования: замораживает `plan.json` и вычисляет `plan_hash`.

### 3.4 `act`

**LLM: нет.**

- Извлекает `exploration_plan[current_step]`.
- **Режим explore:** выполняет действие через `pw-executor`, используя `locator_hint` из плана.
- **Режим replay/ci:** выполняет *замороженный* локатор из зафиксированного `plan.json`.
  LLM не вызывается; нулевые токен-расходы на успешном пути.
- Добавляет скелетную запись `ExecutedAction` в `executed_actions`.
- При любой ошибке `pw-executor` (селектор не найден, элемент не взаимодействуем и т.п.):
  направляет в `verify`, который классифицирует сбой перед передачей в `heal`.

### 3.5 `verify`

**LLM: условно — Sonnet 4.6 только для мягкой проверки утверждений в режиме explore.**

- Повторно снимает снимок страницы inline (вызывает `pw-executor accessibility_snapshot()` после действия).
- Классифицирует результат в одну из категорий:
  - `PASS`
  - `LOCATOR_STALE` — элемент присутствует в a11y-дереве, но селектор более не разрешается
  - `ELEMENT_GONE` — элемент отсутствует в дереве (удалён, условный или вариант A/B)
  - `TIMING` — элемент присутствует, но ещё не доступен для взаимодействия; выполнить одну
    повторную попытку `act` с коротким ожиданием *перед* эскалацией в `heal`
  - `UNEXPECTED_ERROR` — ошибка навигации/сети/JS; не восстанавливается, направляется прямо в `report`
- **В режиме replay:** выполняет структурный a11y-diff по эталонной базовой линии
  (проверка `diff_ratio`) и сравнение screenshot-хешей для этапных шагов.
- **В режиме explore:** использует Sonnet для оценки мягких утверждений, когда шаг содержит
  нетривиальные `expected_outcome`/`assertion`.
- Подлинное несоответствие утверждения (элемент найден, но наблюдаемое значение неверно) — это
  настоящий `FAIL`, который НЕ восстанавливается — направляется в `report`.

### 3.6 `heal`

**LLM: условно — Sonnet 4.6 (повторное связывание с a11y-деревом, визуальный set-of-marks).**

Делегирует всю логику восстановления модулю `healing-engine`.
Работает с `HealingContext {semantic_id, failure_type, attempted_locator, element_description}`.

Восстановление выполняется ограниченным и упорядоченным образом:

1. **Поиск в кеше** (нулевой LLM): запрос к `store-gateway` для поиска `healed_locator`,
   соответствующего `(page_url, semantic_id)` с действующим `dom_subtree_hash`. При попадании:
   немедленное переиспользование. При промахе: выгрузить (пометить `deprecated`) и продолжить.
2. **Ротация стратегий L1–L6** (нулевой LLM): проверка кандидатов по порядку через `pw-executor`;
   выбрать первого, разрешающего в ровно один элемент.

   | Уровень | Стратегия | Prior |
   |---|---|---|
   | L1 | `data-testid` / `data-cy` / `data-pw` | 0.95 |
   | L2 | ARIA role + доступное имя | 0.90 |
   | L3 | точное совпадение `aria-label` | 0.88 |
   | L4 | Видимый текст + role | 0.80 |
   | L5 | Ограниченный CSS (семантический контейнер + тип элемента) | 0.65 |
   | L6 | Позиционный XPath | 0.45 |

   Совпадение на L5/L6 генерирует метрику `strategy_degradation` (сигнал нестабильности DOM).

3. **Повторное связывание с a11y через LLM** (Sonnet 4.6, структурированный вывод): только если
   кеш + L1–L6 — все не подошли. Применяет скидку на самоуверенность LLM `× 0.90`.
4. **Визуальный set-of-marks** (Sonnet 4.6 vision): только при `completeness_ratio < 0.30`
   И неудаче шага 3, И при том что M5 PoC подтвердил точность `> 70%` на 20 реальных сценариях
   с нерабочими селекторами. Возвращает `mark_number`, отображённый в реальный семантический
   локатор, а не в координатный клик. Применяет визуальную скидку `× 0.85`.
5. **Проверка перед принятием**: каждый LLM/визуальный кандидат повторно проверяется в живом DOM
   через `pw-executor`. Если он не разрешается в ровно один элемент, уверенность обнуляется.
6. **Порог уверенности**:
   - `≥ 0.85` → автоматическое восстановление: выполнить действие с восстановленным локатором;
     при успехе сохранить `HealedLocator(status=active)`, привязанный к `(page_url, semantic_id, dom_subtree_hash)`.
   - `0.60–0.84` → помечено: применить оптимистически, установить `healing_flagged = True`,
     сохранить с `review_required = True`; отображается в отчёте запуска.
   - `< 0.60` → «шлюз» оператора: не сохранять; генерировать `NEEDS_HUMAN_REVIEW`; в CI
     автоматически пропустить шаг; в интерактивном режиме приостановить на `checkpoint`
     до разрешения через `agentctl gate`.
7. **Аудит** (только добавление): каждая попытка записывает строку в `healing_audit` —
   никаких `UPDATE`/`DELETE`. Записывается как CI-артефакт `healing-audit.jsonl` и как
   атрибуты OTel span (только selector + confidence; содержимое промптов никогда не включается).
8. **Ограниченные повторы + карантин**: `heal_attempts` жёстко ограничен до 3 (explore) / 2 (replay).
   При достижении лимита: карантин нестабильных тестов с учётом AUT SHA — шаг помещается
   в карантин только при N неудачах из 5 последних запусков *без* изменения AUT git-SHA.

> **Замечание по холодному старту:** порог автоматического принятия по умолчанию равен **0.90**
> (не 0.85), пока не накопится достаточно верифицированных человеком результатов для вычисления
> precision/recall командой `agentctl calibrate` и безопасного снижения порога.

### 3.7 `checkpoint`

**LLM: нет.**

- Сбрасывает LangGraph checkpoint в отдельную БД checkpoint
  (`SqliteSaver` / `AsyncPostgresSaver`).
- Сбрасывает ожидающие `healed_locators` и обновлённый `page_model` в `store-gateway`
  через gRPC `PersistenceService`.
- Записывает событие `checkpoint_id` в Go `orchestrator`.
- Сбрасывает `heal_attempts` и очищает `healing_context`.
- **Если `human_gate_pending = True`:** вызывает `RunControl.Checkpoint` / `Pause` на оркестраторе
  и приостанавливает поток LangGraph до получения решения через gRPC от `agentctl gate approve|skip|abort`.
  В режиме CI «шлюз» автоматически пропускается по истечении настроенного таймаута (по умолчанию 30 мин).

### 3.8 `report`

**LLM: нет. Терминальный узел.**

- Останавливает Playwright-трассировку `pw-executor`; передаёт `trace_path` оркестратору через gRPC.
- Сериализует `plan.json` (с `plan_hash` + двойными эталонными базовыми линиями),
  если ещё не заморожен.
- Формирует `RunResult`:
  - Результаты по шагам: `PASS` / `FAIL` / `SKIP` / `HEALED` / `QUARANTINED`
  - Раздел аудита восстановления (оригинальный и восстановленный локатор, уверенность,
    обоснование, список отмеченных для проверки)
  - Предупреждения о дрейфе golden-diff и screenshot-хешей
  - Карта покрытия (обработанные vs обнаруженные элементы)
  - Разбивка стоимости по узлам и моделям
  - Список ожидающих решения на «шлюзе» оператора
  - Статус целостности плана
- Вызывает `WriteRunResult` на `store-gateway`.
- Генерирует событие `DONE` для оркестратора (код завершения передаётся в `agentctl`).

---

## 4. Рёбра

### 4.1 Таблица рёбер

| От | До | Условие / Триггер |
|---|---|---|
| `START` | `perceive` | Всегда (вход в граф) |
| `perceive` | `ground` | Всегда |
| `ground` | `plan` | `run_mode == "explore"` AND `not exploration_complete` |
| `ground` | `act` | `run_mode in {"replay", "ci"}` OR (`explore` AND plan exists AND `current_step > 0`) |
| `ground` | `report` | `run_mode == "explore"` AND `exploration_complete` |
| `plan` | `checkpoint` | План только что заморожен (исследование завершено на уровне планирования) |
| `plan` | `act` | Следующее действие поставлено в очередь; исследование продолжается |
| `plan` | `report` | `exploration_complete` подтверждён ИЛИ бюджет исчерпан |
| `act` | `verify` | Всегда |
| `verify` | `heal` | Результат — `LOCATOR_STALE` ИЛИ `ELEMENT_GONE` ИЛИ `TIMING` |
| `verify` | `checkpoint` | Результат — `PASS` И `step.is_milestone == True` |
| `verify` | `act` | Результат — `PASS` И `not is_milestone` И шаги остаются |
| `verify` | `report` | Все шаги выполнены ИЛИ результат `UNEXPECTED_ERROR` ИЛИ подлинный `FAIL` на критическом шаге |
| `heal` | `act` | `confidence >= 0.60` И `heal_attempts < cap` — повтор с восстановленным локатором |
| `heal` | `checkpoint` | `confidence < 0.60` — активирован «шлюз» оператора |
| `heal` | `checkpoint` | `heal_attempts >= cap` — карантин и сброс |
| `heal` | `act` *(следующий шаг)* | Восстановление не удалось И `step.is_critical == False` — пропустить шаг, продолжить |
| `checkpoint` | `heal` | «Шлюз» оператора разрешён с `decision == "approve"` и локатором |
| `checkpoint` | `act` | «Шлюз» оператора разрешён с `decision == "skip"` — продолжить со следующего шага |
| `checkpoint` | `END` | «Шлюз» оператора разрешён с `decision == "abort"` |
| `checkpoint` | `perceive` | Нормальное продолжение цикла (нет шлюза, нет терминального условия) |
| `checkpoint` | `report` | Терминальное условие: достигнуто покрытие ИЛИ бюджет исчерпан ИЛИ `stop_signal` |
| `report` | `END` | Всегда (терминальный) |

### 4.2 Логика условных рёбер — сводка

Три узла генерируют условные рёбра на основе полей `RunState`:

**`ground`** (маршрутизатор режимов):
```
if run_mode == "explore" and exploration_complete  →  report
if run_mode == "explore" and not exploration_complete  →  plan
if run_mode in {"replay", "ci"}  →  act
if run_mode == "explore" and plan_exists and current_step > 0  →  act
```

**`verify`** (классификатор результатов):
```
if outcome in {LOCATOR_STALE, ELEMENT_GONE, TIMING}  →  heal
if outcome == PASS and is_milestone  →  checkpoint
if outcome == PASS and not is_milestone and steps_remain  →  act
else (done | error | critical fail)  →  report
```

**`heal`** (порог уверенности + лимит попыток):
```
if confidence >= 0.60 and heal_attempts < cap  →  act          # retry
if confidence < 0.60  →  checkpoint                            # human gate
if heal_attempts >= cap  →  checkpoint                         # quarantine
if heal_failed and not is_critical  →  act (next step)         # skip
```

**`checkpoint`** (обработчик «шлюза» + контроллер цикла):
```
if human_gate_pending and decision == "approve"  →  heal
if human_gate_pending and decision == "skip"  →  act
if human_gate_pending and decision == "abort"  →  END
if terminal (coverage | budget | stop_signal)  →  report
else  →  perceive
```

---

## 5. ASCII-диаграмма потока

```
                        ┌─────────┐
                        │  START  │
                        └────┬────┘
                             │
                             ▼
                        ┌─────────┐
              ┌──────── │ perceive│ ◄──────────────────────────────┐
              │         └────┬────┘                                │
              │              │ always                              │
              │              ▼                                     │
              │         ┌─────────┐                               │
              │         │  ground │                               │
              │         └────┬────┘                               │
              │              │                                     │
              │    ┌─────────┼──────────────┐                     │
              │    │         │              │                     │
              │ (explore     │           (explore                 │
              │ +complete)   │(replay/ci  +plan+                  │
              │    │         │ OR explore  step>0)                │
              │    │         │ +!complete) │                      │
              │    ▼         ▼             │                      │
              │ ┌────────┐ ┌──────┐       │                      │
              │ │ report │ │ plan │       │                      │
              │ └───┬────┘ └──┬───┘       │                      │
              │     │        │\           │                      │
              │     │   frozen │\next     │                      │
              │     │    plan  │ action   │                      │
              │     │        ▼  \         │                      │
              │     │  ┌──────────┐       │                      │
              │     │  │checkpoint│◄──────┘◄──────────┐          │
              │     │  └─────┬────┘                   │          │
              │     │        │ normal cycle            │          │
              │     │        └────────────────────────►┘          │
              │     │                                             │
              │     │       ┌──────────────────────────────────── ┤
              │     │       │                                     │
              │     │       ▼                                     │
              │     │    ┌─────┐                                  │
              │     │    │ act │                                  │
              │     │    └──┬──┘                                  │
              │     │       │ always                              │
              │     │       ▼                                     │
              │     │    ┌────────┐                               │
              │     │    │ verify │                               │
              │     │    └───┬────┘                               │
              │     │        │                                     │
              │     │  ┌─────┼──────────────────┐                │
              │     │  │     │                  │                │
              │     │(stale/ │(PASS+         (PASS+             │
              │     │ gone/  │ milestone)    !milestone          │
              │     │ timing)│               +steps)            │
              │     │  │     ▼               │                   │
              │     │  │  ┌──────────┐       └──► act ──────────►┘
              │     │  │  │checkpoint│◄───────────────┐
              │     │  │  └─────┬────┘                │
              │     │  │        │                      │
              │     │  │  (human gate decision)        │
              │     │  │    approve │  skip  │  abort  │
              │     │  │           │        │    │     │
              │     │  │           ▼        ▼    ▼     │
              │     │  │        ┌──────┐   act  END    │
              │     │  │        │ heal │               │
              │     │  ▼        └──┬───┘               │
              │     │ ┌──────┐     │                   │
              │     │ │ heal │     │ confidence≥0.60   │
              │     │ └──┬───┘     │ AND attempts<cap  │
              │     │    │         └──────────────────►┘(act retry)
              │     │    │
              │     │    ├── confidence<0.60 ──────────► checkpoint (human gate)
              │     │    ├── attempts>=cap  ──────────► checkpoint (quarantine)
              │     │    └── failed+!critical ─────────► act (next step)
              │     │
              │     └──────────────────────────────────► END
              │                                          ▲
              └──────────────────────────────────────────┘
                  (explore+complete OR budget OR stop)
```

> Упрощено для наглядности — точные условные правила см. в Разделе 4.
> `checkpoint → perceive` (нормальный цикл) — основное обратное ребро, управляющее циклом.

---

## 6. Использование LLM по узлам — краткий справочник

| Узел | LLM-вызов | Модель | Когда |
|---|---|---|---|
| `perceive` | Нет | — | — |
| `ground` | Нет | — | — |
| `plan` | Да | Opus 4.8 (temperature 0) | Только в режиме explore; пропускается в replay/ci |
| `act` | Нет | — | — |
| `verify` | Условно | Sonnet 4.6 | Только в режиме explore, для оценки мягких утверждений |
| `heal` | Условно | Sonnet 4.6 | Только после того, как кеш + ротация L1–L6 не дали результата; визуальный режим тоже Sonnet |
| `checkpoint` | Нет | — | — |
| `report` | Нет | — | — |

**Значения бюджета по умолчанию** (настраиваемые; применяются внутри процесса с жёстким потолком на стороне Go):

| Ключ бюджета | Модель | По умолчанию |
|---|---|---|
| `plan_token_limit` | Opus 4.8 | 50 000 токенов / запуск |
| `heal_token_limit` | Sonnet 4.6 | 20 000 токенов / запуск |

При превышении 80% любого бюджета генерируется `BUDGET_WARNING`; при исчерпании бюджета
система деградирует мягко (узел plan прекращает выдавать новые шаги исследования;
узел heal откатывается только к ротации L1–L6), а не аварийно завершает запуск.

---

## 7. pw-executor — замечание о сборке

Все упоминания `pw-executor` выше относятся к нашему **собственному TypeScript-серверу выполнения Playwright**,
который мы создаём и поддерживаем. Он реализует транспортный интерфейс MCP/JSON-RPC 2.0 через stdio и
предоставляет примитивы браузера (навигация, `accessibility_snapshot`, `click`/`type`,
управление трассировкой и `screenshot`) Python-мозгу через stdio-канал. Мозг запускает его
как дочерний процесс и управляет его жизненным циклом; SIGTERM каскадирует при выходе мозга.

Любые детали API-поверхности, помеченные как **VERIFY**, необходимо подтвердить по реальной
реализации `pw-executor` перед развёртыванием.
