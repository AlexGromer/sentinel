# Design Record — Sentinel

> 🌐 **Русский** (основная версия) · [English](DESIGN_RECORD.en.md)

Этот документ архивирует **полную историю происхождения** проектного воркфлоу от 2026-06-23 для Sentinel (автономный self-healing агент для UI-тестирования на Playwright; polyglot Go / Python / TypeScript на основе LangGraph). В воркфлоу участвовали **4 независимых архитектора** (каждый сквозь уникальный lens) → **3 состязательных судьи** → **1 ведущий синтезатор**.

Каноническая, скорректированная с учётом ограничений архитектура находится в [`../ARCHITECTURE.md`](../ARCHITECTURE.md). Настоящий документ сохраняет исходные предложения и вердикты, из которых она сложилась.

> **NOTE: the synthesis's ADR-001 recommended _BUYING_ the official `@playwright/mcp` server. This was later REVERSED to _BUILD_ our own `pw-executor` by a hard build-only user constraint — see ARCHITECTURE.md §0. The proposals/verdicts below are preserved verbatim as historical record and still reference "buy".**

---

## Proposals

Четыре архитектора, четыре lens. Каждый представил полноценное структурированное предложение; все поля приведены ниже.

### `clean-arch` — Hexagonal Polyglot Agent — Versioned Ports Across Three Bounded Contexts

*Lens: Hexagonal / ports-and-adapters maintainability*

**Философия.** Три языка — это не склеенные модули; это независимые ограниченные контексты (bounded contexts), разделённые версионированными типизированными портами. Hexagonal-граница И ЕСТЬ архитектура: Python/LangGraph-мозг не зависит ни от какого Playwright API и ни от какой схемы базы данных; TypeScript-исполнитель ничего не знает об LLM-рассуждениях или жизненном цикле запуска; Go-плоскость управления владеет всей персистентностью, политикой стоимости и надзором за процессами, не импортируя никаких ML-библиотек. Каждая внешняя зависимость — LLM-модель, движок браузера, фреймворк рассуждений, бэкенд хранилища — скрыта за заменяемым адаптером, что позволяет менять, подменять заглушками или обновлять каждую из них изолированно. Ключевая ставка: авансовая оплата определения контрактов на трёх явных портовых границах устраняет «налог на полное переписывание при замене X», который губит polyglot-системы на второй год.

**Компоненты (18):**

| Компонент | Язык | Ответственность |
|-----------|------|----------------|
| orch-cli | Go | CLI-точка входа: разбирает YAML-конфиг, валидирует обязательные поля, определяет режим запуска (explore / replay / heal-audit), сигнализирует о завершении. Единый статический бинарник для CI-дистрибуции — без runtime-зависимостей. |
| run-orchestrator | Go | Запускает и контролирует Python-мозг как подпроцесс (через exec.Cmd); следит за работоспособностью процесса; управляет переходами run-стейт-машины (PENDING → RUNNING → HEALING → COMPLETE / FAILED); перезапускает Python-мозг при сбое (максимум 3 раза, с экспоненциальной задержкой); корректно передаёт сигнал завершения всем дочерним процессам. |
| grpc-server | Go | Мультиплексированный gRPC-сервер, предоставляющий пять сервисов через единый Unix domain socket (dev) или TCP (K8s): ConfigService, PlanService, EventService, PersistenceService, BudgetService. Все .proto-определения являются авторитетным контрактом; сгенерированные заглушки используются как Go-сервером, так и Python-клиентом. |
| persistence-gateway | Go | Единственный владелец всего долгосрочного состояния: SQLite (CI на одном хосте) или Postgres (кластер). Схема: sitemaps, healed_locators, exploration_plans, run_history, page_baselines, llm_traces. Миграции встроены через go:embed SQL-файлы. Python и TS никогда не обращаются к БД напрямую — весь доступ через PersistenceService gRPC. |
| report-service | Go | Потребляет поток EventService в реальном времени; накапливает результаты сценариев, healing-события, расходы токенов и ссылки на Playwright-трейсы; формирует HTML + JSON + JUnit XML-отчёты по завершении запуска. Записывает артефакты в настраиваемый выходной каталог. |
| cost-tracker | Go | Реализует BudgetService gRPC. Отслеживает потребление токенов per-run (входные + выходные токены, уровень модели); принудительно применяет настраиваемую жёсткую квоту; возвращает BUDGET_EXCEEDED в Python перед любым LLM-вызовом, который превысил бы квоту; генерирует событие cost-per-run. Таблица цен токенов задаётся через конфиг — без жёстко закодированных значений. |
| langgraph-agent | Python | LangGraph StateGraph: определяет все узлы, условные рёбра и общий RunContext TypedDict. Управляет циклом explore → plan → act → verify → heal → checkpoint. Использует LangGraph AsyncSqliteSaver (dev) или AsyncPostgresSaver (prod) в качестве checkpointer, с thread_id = run_id для восстановления после аварии в середине запуска. |
| llm-adapter | Python | Порт: LLMPort. Оборачивает Anthropic SDK; обрабатывает потоковую передачу, повторные попытки с экспоненциальной задержкой и подсчёт токенов. Перед каждым вызовом проверяет Go BudgetService.ConsumeTokens — блокирует, если квота была бы превышена. Модель задаётся через конфиг (Opus 4.8 для планирования exploration, Sonnet 4.6 для healing). Заменяема: реализуй LLMPort для использования любого другого провайдера. |
| page-perception | Python | Собирает LLM-контекст из MCP browser_snapshot-ответа: форматирует accessibility tree в виде отступленного текста role/name/state, встраивает set-of-marks-скриншот, вычисляет dom_hash (SHA-256 по сериализованным структурным полям a11y tree, исключая транзитные атрибуты). Обрезает дерево для соблюдения token budget; логирует соотношение усечения. |
| self-healer | Python | Формирует healing-промпты (неудачный locator + текущее a11y tree + set-of-marks-изображение); разбирает JSON-ответ LLM в HealedLocator; оценивает confidence; применяет логику confidence gate (auto / flagged / escalate); вызывает Go PersistenceService.RecordHealedLocator. Stateless — всё изменяемое состояние живёт в RunContext. |
| plan-manager | Python | Сериализует exploration_plan в JSON и вызывает Go PlanService.FreezePlan при переходе explore→replay. При запуске в режиме replay: получает замороженный план, запрашивает PersistenceService.GetHealedLocators по URL страницы, предварительно патчит локаторы плана известными рабочими healing-версиями до начала выполнения. Обрабатывает несовпадение версии плана (изменился target_url), помечая его для повторного explore. |
| playwright-executor | TypeScript | MCP-сервер через stdio: реализует JSON-RPC 2.0 сервер, предоставляющий 8 инструментов (browser_navigate, browser_snapshot, browser_action, browser_assert, browser_codegen_start, browser_codegen_stop, browser_trace_start, browser_trace_stop). Управляет жизненным циклом Playwright Browser и BrowserContext; один контекст на сценарий; headless по умолчанию; headed настраивается. |
| snapshot-provider | TypeScript | Реализует инструмент browser_snapshot: вызывает page.accessibility.snapshot() для структурированного a11y tree; создаёт set-of-marks-оверлей, запрашивая все интерактивные элементы, рисуя пронумерованные bounding boxes на canvas-слое, возвращая PNG-скриншот + mark_map [{id, role, name, bbox, ariaLabel}]. Откатывается к DOM-снимку для элементов, отсутствующих в a11y tree. |
| locator-resolver | TypeScript | Реализует иерархию стратегий локаторов: пробует каждую стратегию по порядку (ARIA role+name → data-testid/data-cy → getByText exact → scoped CSS → XPath), возвращает первое живое совпадение с меткой его стратегии. Вызывается инструментами browser_action и browser_assert. При полной неудаче сообщает обо всех 5 опробованных селекторах в payload ошибки, чтобы self-healer имел полный контекст. |
| trace-controller | TypeScript | Реализует инструменты browser_trace_start / browser_trace_stop. Оборачивает Playwright context.tracing.start() / stop(). Сохраняет .zip-трейс на каждый сценарий в настраиваемый каталог артефактов. Возвращает абсолютный путь к файлу в ответе stop, чтобы Go report-service мог ссылаться на него в HTML-отчёте. |
| codegen-exporter | TypeScript | Реализует инструменты browser_codegen_start / browser_codegen_stop. Записывает последовательность действий из replay-запуска (locator + тип действия + значение), генерирует идиоматичный TypeScript-тест в виде .spec.ts-файла с обёртками test() / expect(). Результат импортируется в существующий Playwright-проект без модификаций. |
| contracts/proto | shared | Авторитетные .proto-файлы для всех 5 gRPC-сервисов (Config, Plan, Event, Persistence, Budget). Go генерирует серверные заглушки; Python генерирует клиентские заглушки. Версионированы с major.minor в имени пакета. Ломающие изменения требуют мажорного инкремента и параллельного периода миграции. Расположены в корне репозитория contracts/proto/. |
| contracts/mcp-schema | shared | JSON Schema-определения для всех 8 MCP-инструментов, предоставляемых playwright-executor. Python MCP-клиент валидирует схемы инструментов при подключении подпроцесса; несоответствия вызывают ошибку при запуске с понятным сообщением. Версионированы в ответе MCP server_info. Расположены в корне репозитория contracts/mcp/. |

**Границы (polyglot-контракты).**

ГРАНИЦА 1: Go Control Plane ↔ Python Brain (gRPC + protobuf)

Протокол: gRPC через Unix domain socket (single-host: CI, dev) с автоматическим TCP-откатом (K8s multi-pod). Строка подключения инжектируется Go-оркестратором как переменная окружения AGENT_GRPC_ADDR при запуске Python-подпроцесса. Python brain является единственным gRPC-клиентом; Go — единственным сервером. Пять сервисов в одном мультиплексированном соединении:
  - ConfigService: GetRunConfig(run_id) → RunConfig. Вызывается один раз при старте Python.
  - PlanService: FreezePlan(plan_json, target_url, dom_hash) → plan_id. GetFrozenPlan(target_url) → plan_json. Вызывается узлом plan-manager.
  - EventService: Emit(RunEvent) → ack. StreamEvents(run_id) → stream (для report-service). Python генерирует событие после каждого перехода узла.
  - PersistenceService: RecordHealedLocator, GetHealedLocators(page_url), UpsertSitemap, GetBaseline, PutBaseline. Всё долгосрочное состояние пересекает эту границу.
  - BudgetService: ConsumeTokens(run_id, input_tokens, output_tokens, model) → {allowed: bool, remaining: int}. Python вызывает перед каждым LLM-вызовом.

Обоснование выбора gRPC вместо REST: protobuf обеспечивает соблюдение контракта на этапе компиляции — сгенерированные заглушки для обоих языков (Go-сервер и Python-клиент) из одного .proto-источника. Никакого дрейфа схем в runtime. Стриминг на EventService обеспечивает отрисовку отчётов в реальном времени без поллинга. Отклонённая альтернатива: REST/JSON — нет принудительного контроля схем на этапе компиляции; два независимых варианта реализации одного и того же контракта неизбежно разойдутся через несколько недель.

Версионирование: имена пакетов proto несут мажорную версию (v1, v2). Go и Python фиксируют одну версию proto через каталог contracts/proto/ в корне репозитория. Ломающее изменение proto — это координированное кросс-языковое развёртывание.

ГРАНИЦА 2: Python Brain ↔ TypeScript Executor (MCP JSON-RPC 2.0 over stdio)

Протокол: MCP (Model Context Protocol) — JSON-RPC 2.0 через stdin/stdout-каналы подпроцесса. Python brain запускает playwright-executor как дочерний процесс через subprocess.Popen и выступает MCP-клиентом. TS-исполнитель реализует MCP-сервер. Это тот же протокол, что использует Claude Code для всех своих tool-серверов — хорошо понятный, без управления портами, жизненный цикл подпроцесса тривиально принадлежит родителю.

Вызовы Python всегда sequential-await (один ожидающий запрос одновременно на одно выполняющееся вхождение узла LangGraph) — проблемы backpressure нет, поскольку выполнение узлов LangGraph по своей природе последовательно. Таймаут: 30 секунд на вызов инструмента (настраивается). При таймауте или выходе подпроцесса: Python генерирует событие EXECUTOR_CRASH в Go EventService, пытается перезапустить подпроцесс (максимум 3 раза, с задержкой), завершает запуск ошибкой, если все попытки исчерпаны.

Восемь MCP-инструментов, предоставляемых TS-исполнителем:
  browser_navigate(url, wait_until?) → {ok, current_url, title}
  browser_snapshot(set_of_marks?) → {accessibility_tree, screenshot_b64, dom_hash, current_url, mark_map?}
  browser_action(action_type, locator_spec, value?) → {ok, error_type?, attempted_locators?}
  browser_assert(assertion_type, locator_spec, expected?) → {ok, actual?, error_type?}
  browser_codegen_start(output_path) → {ok}
  browser_codegen_stop() → {output_path, line_count}
  browser_trace_start(name) → {ok}
  browser_trace_stop() → {artifact_path}

Обоснование выбора MCP вместо HTTP-JSON-RPC local server: нет выделения и конфликтов портов в CI, нет поллинга для проверки работоспособности, жизненный цикл процесса — простой Popen, протокол инспектируем и уже стандартизирован для интеграции LLM-инструментов. Отклонённая альтернатива: HTTP/REST local server — требует управления портами (конфликты портов в CI реальны в контейнерных окружениях), независимого цикла проверки работоспособности, больше точек отказа для того, что по сути является внутрипроцессным вызовом функции.

ИЗОЛЯЦИЯ ОТКАЗОВ:
  Сбой TS-исполнителя → Python обнаруживает код выхода подпроцесса, перезапускает, возобновляет с последнего checkpoint.
  Сбой Python brain → Go run-orchestrator обнаруживает выход процесса, помечает запуск как FAILED, сохраняет последний LangGraph checkpoint. Запуск можно возобновить: повторно запустить Python с тем же run_id; LangGraph загрузит checkpoint и продолжит с последнего сохранённого узла.
  Сбой Go orchestrator → незавершённый запуск восстановим: при перезапуске Go запрашивает в БД checkpointer запуски в состоянии RUNNING и возобновляет их.
  Одновременный сбой всех трёх процессов → запуск помечается как FAILED_UNRECOVERABLE в БД; последний checkpoint доступен для ручной инспекции.

**Агентный цикл.**

ОБЩИЙ ОБЪЕКТ СОСТОЯНИЯ (Python TypedDict — RunContext):

  run_id: str
  run_mode: Literal["explore", "replay", "heal_audit"]
  target_url: str
  config: RunConfig                          # from ConfigService, immutable after load
  sitemap: dict                              # {url: {flows: [...], links: [...], dom_hash: str}}
  exploration_plan: list[Scenario] | None    # None until frozen by plan node
  exploration_depth: int                     # hops explored from root
  exploration_complete: bool                 # set by LLM response in explore node
  current_scenario_idx: int
  current_step_idx: int
  current_action: BrowserAction | None
  accessibility_tree: dict | None            # refreshed each perceive node call
  screenshot_b64: str | None
  dom_hash: str | None                       # SHA-256 of structural a11y fields only
  current_url: str | None
  active_locator: LocatorSpec | None         # current target element spec
  locator_candidates: list[LocatorSpec]      # ranked alternatives from healing
  healing_mode: bool
  healing_attempts: int                      # resets per scenario
  last_healing_confidence: float
  last_verify_result: VerifyResult | None
  action_history: list[ActionRecord]         # episodic; trimmed to last 20 for LLM context window
  scenarios_complete: list[str]              # scenario IDs finished this run
  scenarios_blocked: list[str]              # scenario IDs blocked by escalated healing
  tokens_consumed: int
  token_budget: int
  cost_usd: float
  messages: Annotated[list[BaseMessage], add_messages]   # LangGraph message accumulator

УЗЛЫ (8):

  perceive: Вызывает MCP browser_snapshot(set_of_marks=healing_mode). Записывает accessibility_tree, screenshot_b64, dom_hash, current_url в состояние. Если dom_hash изменился с последнего шага, добавляет наблюдение в messages. LLM не вызывается.

  route: Чистый условный диспетчер — без побочных эффектов, без LLM-вызовов. Читает run_mode + healing_mode + exploration_complete + current_scenario_idx. Возвращает имя следующего узла. Это «регулировщик» графа: концентрация логики маршрутизации здесь оставляет все остальные узлы единоцелевыми.

  explore: LLM-узел (Opus 4.8). Системный промпт задаёт тестовую персону. Пользовательский промпт: текущее a11y tree + карта сайта к текущему моменту + action_history + «Определи следующий непроверенный интерактивный flow и единственное следующее действие. Если все доступные flow исследованы, установи exploration_complete=true.» Структурированный вывод: {next_action: BrowserAction, flow_discovered: Flow, exploration_complete: bool, reasoning: str}. Обновляет sitemap, current_action, exploration_complete в состоянии. Проверяет BudgetService перед вызовом.

  plan: Сериализует exploration_plan в JSON. Вызывает Go PlanService.FreezePlan. Устанавливает run_mode="replay". Вызывает plan-manager для предварительного патчинга плана известными healing-локаторами из PersistenceService. Записывает frozen_plan_id в состояние. Узел без LLM.

  act: Вызывает MCP browser_action({action_type, locator_spec: active_locator, value}) с таймаутом 30 секунд. При успехе: добавляет в action_history, сбрасывает healing_mode. При ELEMENT_NOT_FOUND / ELEMENT_NOT_VISIBLE / ELEMENT_STALE: устанавливает healing_mode=true, инкрементирует healing_attempts, фиксирует контекст неудачного локатора в состоянии. LLM не вызывается.

  verify: Вызывает MCP browser_assert({assertion_type, locator_spec, expected}) для ожидаемого результата текущего шага. При LOCATOR_NOT_FOUND: устанавливает healing_mode=true. При несовпадении утверждения (элемент найден, но значение неверное): фиксирует настоящий сбой теста в last_verify_result — это НЕ исцеляется, это РЕАЛЬНЫЙ дефект. При успехе: сбрасывает healing_mode, продвигает курсор шага. LLM не вызывается.

  heal: LLM-узел (Sonnet 4.6 — быстрее и дешевле Opus для этой ограниченной задачи). Формирует healing-промпт из неудачного локатора + текущего a11y tree + set-of-marks-скриншота + mark_map. Разбирает структурированный JSON-ответ {healed_locator, confidence, reasoning, mark_id}. Применяет confidence gate (см. selfHealing). При confidence >= 0.60: обновляет active_locator в состоянии, вызывает PersistenceService.RecordHealedLocator. При confidence < 0.60 или healing_attempts >= 3: помечает сценарий как BLOCKED, вызывает EventService.Emit(HEALING_ESCALATION).

  checkpoint: Вызывает LangGraph checkpointer.aput() — сохраняет полный снимок RunContext. Вызывает EventService.Emit(CHECKPOINT, {run_id, scenario_idx, step_idx, dom_hash, tokens_consumed}). Узел без LLM.

  emit_event: Вызывает EventService.Emit с результатом сценария (PASS / FAIL / BLOCKED), полным журналом шагов, дельтой стоимости токенов, healing-событиями для этого сценария. Продвигает курсор сценария или переходит к END. Узел без LLM.

РЁБРА (условные):

  START → perceive
  perceive → route
  route → explore        when: run_mode="explore" AND NOT healing_mode AND NOT exploration_complete
  route → plan           when: run_mode="explore" AND exploration_complete
  route → act            when: run_mode="replay" or "heal_audit" AND NOT healing_mode
  route → heal           when: healing_mode=True
  explore → perceive     (loop: explore calls perceive after each action to observe result)
  plan → checkpoint → route
  act → verify
  verify → checkpoint    (success path: save progress)
  verify → heal          (LOCATOR_NOT_FOUND path)
  verify → emit_event    (assertion failure: genuine test failure, do not heal)
  checkpoint → route     (continue to next step or next scenario)
  heal → act             (confidence >= 0.60 AND healing_attempts < 3: retry with healed locator)
  heal → emit_event      (confidence < 0.60 OR healing_attempts >= 3: escalate)
  emit_event → route     (next scenario exists)
  emit_event → END       (all scenarios processed)

CHECKPOINTER: LangGraph AsyncSqliteSaver (dev/CI single-host) или AsyncPostgresSaver (K8s prod). Строка подключения из Go ConfigService. thread_id = run_id. Checkpoint при: каждом успешном сценарии, каждом heal-событии, каждой заморозке плана. Обеспечивает: восстановление после сбоя без повторного прогона завершённых сценариев; паузу/возобновление с участием человека в режиме heal_audit.

**Self-healing.**

ШАГ 1 — ОБНАРУЖЕНИЕ СБОЯ (TS locator-resolver, синхронно, до MCP-ответа):
  При вызове browser_action или browser_assert locator-resolver перебирает все 5 стратегий в порядке иерархии до сообщения об ошибке. MCP-ответ с ошибкой включает: error_type (ELEMENT_NOT_FOUND | ELEMENT_NOT_VISIBLE | ELEMENT_STALE), attempted_locators [{strategy, selector, tried_at_ms}] (все 5 попыток), action_type, page_url, screenshot_b64 в момент сбоя. Это означает, что healing начинается с полным диагностическим контекстом — без дополнительного round-trip к TS для выяснения того, что было опробовано.

ШАГ 2 — ОБНОВЛЕНИЕ ВОСПРИЯТИЯ (узел perceive, целевое повторное вхождение):
  Узел heal инициирует свежий вызов browser_snapshot(set_of_marks=true). TS snapshot-provider: (a) вызывает page.accessibility.snapshot() для полного структурированного a11y tree; (b) запрашивает все интерактивные элементы через page.$$('button,input,select,a,[role=button],[tabindex]'), рисует пронумерованные bounding boxes как canvas-оверлей, делает скриншот, возвращает mark_map [{id, role, name, bbox, ariaLabel}]. Это даёт LLM как структурированные семантические данные, так и пространственный/визуальный контекст. Обоснование: a11y tree в одиночку упускает canvas-виджеты, кастомные веб-компоненты с плохой ARIA и элементы в cross-origin iframe'ах (задокументированное ограничение — помечается в отчёте запуска). Комбинированный канал — наиболее сильный доступный сигнал заземления без инжектирования тестовых ID в тестируемое приложение (AUT).

ШАГ 3 — ПЕРЕОРИЕНТАЦИЯ (LLM-вызов, Sonnet 4.6, режим структурированного вывода):
  Healing-промпт (из self-healer.py):
  "HEALING REQUEST [run_id={X}, scenario={Y}, step={Z}]
   ORIGINAL ACTION: {action_type} on element — role={role}, accessible_name={name}, selector={original_selector}
   FAILED STRATEGIES: {attempted_locators as table}
   DOM HASH CHANGE: {original_dom_hash} → {current_dom_hash}
   CURRENT ACCESSIBILITY TREE: {formatted_tree}
   SET-OF-MARKS SCREENSHOT: [image]  MARK MAP: {mark_map}
   TASK: The element was restructured or renamed. Identify what we were targeting.
   RESPOND WITH JSON ONLY:
   {healed_locator: {strategy: 'aria|testid|text|css|xpath', value: str}, confidence: float, reasoning: str, mark_id: int|null}"
  Проверка token budget через BudgetService перед вызовом. Если бюджет исчерпан: healing пропускается, генерируется BUDGET_BLOCKED.

ШАГ 4 — ИЕРАРХИЯ СТРАТЕГИЙ ЛОКАТОРОВ (порядок предпочтения, от наиболее к наименее стабильному):
  1. ARIA role + accessible name: page.getByRole('button', {name: 'Submit Order'}) — выживает при визуальных рефакторингах; наиболее стабилен при изменениях CSS/макета
  2. data-testid / data-cy / data-pw attribute: page.getByTestId('checkout-submit') — нулевая хрупкость, когда команда разработчиков использует тест-атрибуты; запросить PersistenceService.GetPageAttributes, чтобы узнать, использует ли AUT соглашение тест-атрибутов
  3. Точный видимый текст: page.getByText('Place Order', {exact: true}) — стабилен для уникальных строк-меток; хрупок при i18n
  4. Scoped CSS — семантический контейнер + тип элемента: page.locator('form[aria-label="Checkout"] button[type="submit"]') — лучше голого CSS, поскольку семантический контейнер стабилен, даже если внутренняя структура смещается
  5. XPath — структурный путь: page.locator('//form[@aria-label="Checkout"]//button[last()]') — крайнее средство; хрупкий; используется только когда LLM не возвращает альтернативу с более высокой confidence
  После того как Python записывает healed_locator в состояние, locator-resolver в TS валидирует, что кандидат живёт, прежде чем вернуть результат в Python. Кандидат, успешно прошедший валидацию, но затем отказавший в act, рассматривается как новый цикл healing (healing_attempts снова инкрементируется).

ШАГ 5 — CONFIDENCE GATE И ВЕТВЛЕНИЕ:
  confidence >= 0.85: AUTO_HEAL — обновить active_locator в состоянии, сбросить healing_attempts до 0 (мы доверяем этому), немедленно повторить узел act. Генерировать событие HEALING_AUTO в EventService.
  0.60 <= confidence < 0.85: FLAGGED_HEAL — обновить active_locator, повторить act, генерировать событие HEALING_FLAGGED. Сценарий помечается как HEALED_UNVERIFIED в отчёте запуска. Отображается в разделе «Healing Audit» для ревью человеком после запуска.
  confidence < 0.60 ИЛИ healing_attempts >= 3: ESCALATE — НЕ обновлять локатор, НЕ делать повторную попытку.
    Режим CI: добавить сценарий в список scenarios_blocked, перейти к следующему сценарию, пометить запуск как PARTIAL_HEAL (не полный FAIL — другие сценарии продолжаются).
    Режим Service: вызвать EventService.Emit(HEALING_ESCALATION, {scenario, step, context}); Go report-service инициирует настроенный вебхук (Slack / PagerDuty / custom); запуск приостанавливается на этом сценарии до тех пор, пока оператор не возобновит или не пропустит его через API.
  healing_attempts сбрасывается до 0 при каждом новом сценарии (сброс состояния в узле emit_event).

ШАГ 6 — ПЕРСИСТЕНТНОСТЬ И РАСПРОСТРАНЕНИЕ (амортизация стоимости healing):
  При любом heal с confidence >= 0.60: Go PersistenceService.RecordHealedLocator сохраняет {run_id, scenario_id, page_url, original_locator, healed_locator, confidence, reasoning, dom_hash_before, dom_hash_after, model_used, strategy_used, timestamp}.
  При запуске следующего replay: plan-manager вызывает PersistenceService.GetHealedLocators(target_url) и обходит шаги замороженного плана. Для каждого шага, чьи page_url + original_locator совпадают с healing-записью И чей текущий dom_hash совпадает с dom_hash_after: предварительно патчит шаг плана healing-локатором. Healing амортизируется — LLM оплачивается один раз, результат переиспользуется во всех последующих запусках до следующего изменения DOM (несовпадение dom_hash инициирует новый цикл healing).
  Устаревшие healing-локаторы (dom_hash_after больше не совпадает с текущей страницей) автоматически исключаются из списка предварительного патчинга и помечаются в отчёте запуска как STALE_HEALED_LOCATOR — это инициирует новый цикл heal, а не молчаливое использование устаревших данных.
  Аудит: записи HEALING_FLAGGED отображаются в HTML-отчёте с: разницей между оригинальным и healing-локатором, confidence, дословными рассуждениями LLM, выдержкой из a11y tree до/после. Режим запуска heal_audit воспроизводит ТОЛЬКО ПОМЕЧЕННЫЕ сценарии в headed-браузере для ручной проверки человеком перед слиянием healing-локаторов в канонический план.

**Детерминизм.**

БАЗОВЫЙ ПАТТЕРН: Explore-Once / Replay-Many. Фаза исследования (exploration), управляемая LLM, выполняется в выделенном периодическом задании (ночном или по требованию), а НЕ при каждом CI-триггере. Она производит артефакт замороженного плана, сохранённый в Go PersistenceService. CI-гейт выполняет только детерминированную фазу replay, которая потребляет замороженный план. Это фундаментальное разделение: недетерминизм изолирован в задании exploration; CI-гейт является чистым воспроизводителем.

СХЕМА ЗАМОРОЖЕННОГО ПЛАНА: план, сохранённый PlanService.FreezePlan, является версионированным JSON-документом: {plan_id, target_url, created_at, schema_version, scenarios: [{scenario_id, name, steps: [{step_id, action_type, locator_spec: {strategy, value}, value?, assertion_type?, expected?, page_url, dom_hash_at_record}]}]}. dom_hash в момент записи — это структурный отпечаток страницы при захвате шага. При replay plan-manager вычисляет текущий dom_hash и генерирует DOM_DRIFT_WARNING, если он отличается — это ранний сигнал о том, что план может нуждаться в повторном healing перед запуском.

ДЕТЕРМИНИРОВАННЫЙ ПОРЯДОК СЦЕНАРИЕВ: для окружений, где exploration должен перезапускаться (preview-развёртывания с эфемерными URL), порядок сценариев внутри фазы explore определяется из SHA-256 хэша целевого URL + настраиваемой seed-строки. Это не делает LLM-выборы детерминированными, но делает порядок выполнения сценариев детерминированным при повторных запусках одного и того же explore-задания, предотвращая ложные связанные с порядком flake.

GOLDEN STATE BASELINES: после каждого успешного replay-запуска Go report-service записывает baseline-запись: {page_url, dom_hash, a11y_tree_snapshot, timestamp, run_id}. При СЛЕДУЮЩЕМ replay-запуске узел perceive получает baseline для каждой страницы и вычисляет структурный дифф. Дифф, превышающий настраиваемый порог (например, > 10% изменённых узлов), инициирует BASELINE_DRIFT_ALERT — не автоматический сбой, но сигнал о том, что план может быть устаревшим.

HEALING AUDIT TRAIL КАК CI-АРТЕФАКТ: каждое событие HEALING_AUTO и HEALING_FLAGGED записывается в JSONL-артефакт (healing-audit.jsonl) рядом с отчётом запуска. CI загружает его как артефакт сборки. Запуск со слишком большим количеством AUTO heal (настраиваемый порог, например, > 5 за запуск) инициирует CI-предупреждение: высокий объём healing сигнализирует о DOM-churn, требующем свежего exploration.

КАРАНТИН FLAKE: сценарий, отказывающий с non-healing ошибкой (несовпадение утверждения, а не ошибка локатора) в 2 из последних 3 replay-запусков, автоматически добавляется в список карантина в PersistenceService. Сценарии в карантине пропускаются в последующих запусках и помечаются для ручного разбора человеком. Они никогда не удаляются молчаливо из плана — они остаются видимыми в отчёте как QUARANTINED. Статус карантина сбрасывается только явным действием оператора или успешным запуском после повторной заморозки плана.

LLM-FREE REPLAY: в режиме replay с полностью healing-пропатченным планом узлы act и verify делают ноль LLM-вызовов (чистое выполнение Playwright против явных локаторов). Единственный LLM-вызов в режиме replay — это healing-цикл, который инициируется только при живом сбое локатора. Регрессионный запуск на стабильном AUT поэтому полностью детерминирован и имеет нулевую стоимость LLM-токенов.

ЗАКРЕПЛЕНИЕ ВЕРСИИ ПЛАНА В CI: конфиг CI указывает опциональный plan_id для закрепления. Если plan_id задан, запуск использует именно этот замороженный план независимо от того, существует ли более новый. Это аварийный выход для заморозки известного рабочего плана на ветке релиза, предотвращающий молчаливое изменение CI-гейта upstream explore-заданием.

**Память.**

КРАТКОСРОЧНАЯ (эпизодическая, в пределах одного запуска):
  LangGraph RunContext TypedDict И ЕСТЬ краткосрочная память. Она живёт в процессе Python brain и сериализуется LangGraph checkpointer после каждого значимого перехода узла. Ключевые эпизодические поля: action_history (список ActionRecord, обрезанный до последних 20 записей для LLM context window), messages (Annotated-аккумулятор BaseMessage для треда LLM-разговора), текущий строящийся sitemap, счётчик healing_attempts на сценарий. Обрезка action_history до 20 предотвращает неограниченный рост контекста при длинных exploration-запусках; полная история доступна через поток EventService в Go для аудита.

CHECKPOINTER (персистентность в середине запуска, граница восстановления):
  LangGraph AsyncSqliteSaver в single-host-развёртываниях (нулевая инфраструктурная зависимость, встроен в Python-процесс, файл по настраиваемому пути). LangGraph AsyncPostgresSaver в K8s-развёртываниях. Строка подключения предоставляется Go ConfigService. thread_id = run_id. Это НЕ то же самое, что долгосрочная память — это состояние для восстановления после сбоя. При перезапуске Go orchestrator с запуском в состоянии RUNNING он повторно запускает Python brain с тем же run_id; LangGraph загружает последний checkpoint и продолжает с последнего сохранённого узла. Данные checkpoint эфемерны: удаляются после того, как запуск достигает COMPLETE или FAILED_UNRECOVERABLE.

ДОЛГОСРОЧНАЯ (кросс-сессионная, принадлежит исключительно Go persistence-gateway):
  Хранилище: SQLite-файл (dev / CI single-host, встроен в путь Go-бинарника через go:embed миграции), Postgres (K8s кластер). Выбор хранилища задаётся конфигом; persistence-gateway обрабатывает оба варианта через один и тот же gRPC-интерфейс. Таблицы схемы:

  sitemaps: {target_url, dom_hash, pages_json, flows_json, discovered_at, run_id}. Позволяет plan-manager проверять, был ли target_url недавно исследован, и пропускать повторный exploration, если dom_hash актуален.

  healed_locators: {id, page_url, original_locator_json, healed_locator_json, strategy, confidence, reasoning, dom_hash_before, dom_hash_after, model_used, run_id, created_at}. Первичный ключ поиска: (page_url, original_selector). Управляет предварительным патчингом плана при запуске replay. Индекс по dom_hash_after для быстрого обнаружения устаревших записей.

  exploration_plans: {plan_id, target_url, schema_version, plan_json, created_at, run_id, status (active|archived)}. Один активный план на target_url; предыдущие планы архивируются, не удаляются.

  page_baselines: {id, page_url, dom_hash, a11y_tree_json, created_at, run_id}. Одна запись на страницу на запуск; используется для обнаружения структурного дрейфа при следующем запуске.

  run_history: {run_id, target_url, run_mode, status, start_time, end_time, scenarios_total, scenarios_pass, scenarios_fail, scenarios_blocked, tokens_consumed, cost_usd, plan_id}. Сводка для отчётности по стоимости и отслеживания flake.

  llm_traces: {id, run_id, scenario_id, node_name, model, prompt_tokens, completion_tokens, latency_ms, created_at}. Содержимое промпта/ответа НЕ хранится по умолчанию (стоимость + приватность); полное содержимое хранится только если TRACE_FULL_LLM=true в конфиге.

  flake_quarantine: {scenario_id, plan_id, failure_count, last_failure_at, quarantined_at, reason, status (quarantined|cleared)}.

ОБОСНОВАНИЕ МОНОПОЛЬНОГО ВЛАДЕНИЯ Go БД: прямой доступ Python к БД связал бы схему Python со схемой Go и создал проблему координации миграций при нескольких авторах записи. Прямой доступ TS к БД был бы ещё хуже. Слой gRPC-сервисов является принудительной границей: Go владеет схемой, миграциями, пулингом соединений и оптимизацией запросов. Python и TS потребляют типизированные сервисные методы. Замена Python (например, на Go-агент) или TS (например, на Puppeteer): ноль работы по миграции БД.

**Наблюдаемость.**

РАСПРЕДЕЛЁННАЯ ТРАССИРОВКА (OpenTelemetry, все три уровня):
  Go: otelgrpc-интерсепторы на всех gRPC-сервисах; контекст трейса распространяется в gRPC metadata. Spans для: переходов состояний жизненного цикла запуска, каждого gRPC-вызова, рендеринга отчётов.
  Python: opentelemetry-sdk; кастомный инструментор оборачивает каждое выполнение узла LangGraph как span (атрибуты: node_name, run_id, scenario_id, step_idx). LLM-вызовы являются дочерними spans с количеством токенов и задержкой. Контекст трейса распространяется в Go gRPC-вызовах через metadata.
  TypeScript: opentelemetry-api в playwright-executor; каждый MCP tool call — это span с атрибутами action_type, locator_strategy, success/failure, dom_hash.
  Экспортёр: OTLP в локальный коллектор (Jaeger или Grafana Tempo в homelab; настраиваемый endpoint). Все три процесса экспортируют в один и тот же коллектор; trace ID коррелируются через run_id, распространяемый в gRPC metadata и как поле метаданных MCP-вызова.

LLM DECISION TRACING (Go persistence-gateway, таблица llm_traces):
  Каждый LLM-вызов записывает: run_id, node_name, model, prompt_tokens, completion_tokens, latency_ms, created_at. Полное содержимое промпта+ответа хранится только если установлена переменная окружения TRACE_FULL_LLM (по умолчанию отключено для контроля стоимости и приватности). При включении содержимое хранится сжатым (zstd) в отдельном blob-столбце. CLI-команда (orch-cli trace replay --run-id=X) реконструирует полную последовательность решений из llm_traces для post-mortem-отладки.

PLAYWRIGHT TRACES (на сценарий, trace-controller):
  Playwright-трейс (сеть, DOM-снимки, скриншоты, временная шкала действий) захватывается на каждый сценарий через trace-controller. Хранится как .zip-файлы в настраиваемом каталоге артефактов. HTML-отчёт запуска ссылается на трейс-файл каждого сценария. Трейсы просматриваются в Playwright Trace Viewer (локально или hosted). Пути к трейс-файлам записываются в EventService по завершении сценария; report-service встраивает их в вывод.

ВОСПРОИЗВОДИМЫЕ ТРАНСКРИПТЫ ЗАПУСКОВ:
  Go report-service записывает JSONL-транскрипт на каждый запуск: одна строка на каждый вызов EventService.Emit, упорядоченные по времени. Поля: {event_type, run_id, scenario_id, step_idx, node_name, timestamp, payload}. Это полный audit trail каждого решения, действия, верификации и healing-события. Отдельная команда orch-cli replay-transcript может воспроизвести последовательность LLM-решений (без повторного выполнения браузерных действий) для отладки. Транскрипты хранятся в каталоге артефактов рядом с трейсами.

TOKEN BUDGET И СТОИМОСТЬ:
  BudgetService принудительно применяет ограничение токенов per-run (настраивается; рекомендуемые значения по умолчанию: explore-запуск=200K токенов, replay-запуск=20K токенов, поскольку LLM нужен только для healing). Перед каждым LLM-вызовом Python вызывает BudgetService.ConsumeTokens; при отказе генерирует BUDGET_EXCEEDED, пропускает LLM-вызов, помечает сценарий как BUDGET_BLOCKED (не FAIL). Go cost-tracker вычисляет cost_usd = sum(input_tokens * input_price + output_tokens * output_price) по уровню модели. Таблица цен задаётся конфиг-файлом — никогда не хардкодится. Отчёт запуска показывает: стоимость на запуск, стоимость на сценарий, разбивку стоимости по типам узлов (explore vs heal). Данные трендов в таблице run_history позволяют формировать еженедельные отчёты по стоимости через orch-cli cost --since=7d.

ЭНДПОИНТ МЕТРИК (Go, Prometheus):
  Go grpc-server предоставляет /metrics на отдельном порту: run_count (по статусу), healing_events_total (по бакету confidence), token_budget_utilization, scenario_duration_seconds (гистограмма), cost_per_run_usd. Шаблон Grafana-дашборда поставляется в репозитории. Homelab ArgoCD развёртывает полный стек с предварительно настроенным конфигом Prometheus scrape.

**Выходные артефакты.**

1. ОТЧЁТ ЗАПУСКА (HTML + JSON + JUnit XML): HTML-отчёт включает: сводку запуска (счётчики pass/fail/blocked/quarantined, общая стоимость, длительность), разворачиваемый раздел на сценарий (шаги, утверждения, healing-события с LLM-рассуждениями, ссылка на Playwright-трейс), раздел healing audit (все FLAGGED heals для ревью человеком), предупреждения о baseline drift, добавления в карантин flake. JSON-отчёт — машиночитаемый для интеграции с CI. JUnit XML для визуализации результатов тестов в GitHub Actions / GitLab CI. Все три генерируются Go report-service из потока EventService.

2. PLAYWRIGHT TEST CODE (.spec.ts): codegen-exporter генерирует TypeScript-тест Playwright на каждый сценарий во время replay-запусков (codegen_start / codegen_stop оборачивает каждый сценарий). Вывод использует идиоматичные обёртки test() / expect(), именованные по scenario_id и имени flow. Файл импортируется напрямую в существующий Playwright-проект без модификаций. Это основной артефакт передачи в существующий воркфлоу инженера по автоматизации QA: агент генерирует начальный скелет теста; инженеры его сопровождают.

3. PLAYWRIGHT TRACES (.zip на сценарий): бинарные архивы трейсов, содержащие полный сетевой лог, DOM-снимки, скриншоты и временную шкалу действий. Просматриваются в Playwright Trace Viewer. Ссылки по имени файла в HTML-отчёте. Хранятся per-run в каталоге артефактов; политика хранения настраивается (по умолчанию: хранить последние 10 запусков).

4. HEALING AUDIT JSONL (healing-audit.jsonl): упорядоченный лог каждого healing-события в запуске: {event_type, scenario_id, step_idx, original_locator, healed_locator, confidence, reasoning, dom_hash_before, dom_hash_after, timestamp}. CI загружает как артефакт сборки. Обеспечивает асинхронное ревью человеком без повторного запуска агента.

5. ЗАМОРОЖЕННЫЙ ПЛАН ИССЛЕДОВАНИЯ (JSON, хранится в Go persistence-gateway + экспортируется): канонический план тестирования, создаваемый exploration — артефакт, делающий CI детерминированным. Также экспортируется как файловый артефакт для контроля версий рядом с кодовой базой.

6. REGRESSION BASELINES (снимки a11y tree, хранятся в Go persistence-gateway): структурные снимки на страницу, сделанные после каждого успешного replay-запуска. Входные данные для обнаружения DOM drift в следующем запуске. Не артефакт для человека, но первоклассный системный артефакт.

7. SARIF REPORT (опционально, Go report-service): вывод в формате Static Analysis Results Interchange Format для интеграции с GitHub Code Scanning или аналогами. Сопоставляет настоящие сбои тестов (несовпадения утверждений, не ошибки healing) с SARIF-результатами. По умолчанию отключён; включается флагом --sarif.

8. СВОДКА СТОИМОСТИ (JSON + stdout): добавляется к отчёту запуска и выводится на stdout по завершении: {run_id, total_cost_usd, tokens_by_model, cost_by_node_type, runs_this_week_cost_usd}. Предназначена для захвата CI и публикации как PR-комментарий через orch-cli cost-comment.

**Путь MVP.**

ФАЗА 1 — TypeScript MCP Server в изоляции (недели 1-2):
  Построить playwright-executor как автономный MCP-сервер с 4 инструментами: browser_navigate, browser_snapshot, browser_action, browser_assert. Протестировать с любым MCP-клиентом (Claude Code, curl-MCP). Убедиться, что page.accessibility.snapshot() возвращает достаточный сигнал на реальном SPA-таргете. Проверить генерацию set-of-marks-оверлея. Реализовать locator-resolver с полной 5-стратегийной иерархией. Установить JSON Schema contracts/mcp-schema. Эта фаза доказывает работоспособность канала восприятия до того, как всей архитектуре будет дан зелёный свет. Результат: команда npx playwright-executor, обслуживающая MCP через stdio.

ФАЗА 2 — Скелет Go control plane (недели 2-3):
  Бинарник orch-cli с разбором конфига (YAML). run-orchestrator, запускающий подпроцесс (изначально заглушку Python-мозга) и контролирующий его. grpc-server с заглушками ConfigService и EventService. persistence-gateway с SQLite и миграциями схемы для run_history. report-service, генерирующий базовый JSON-вывод из событий. Интеграционный тест: Go → запуск TS-исполнителя → отправка navigate + snapshot → получение события → запись отчёта. Устанавливает инфраструктуру надзора за процессами и gRPC. Результат: orch-cli run --config=run.yaml производит JSON-отчёт из жёстко закодированной последовательности действий.

ФАЗА 3 — Базовый цикл Python LangGraph (недели 3-5):
  LangGraph StateGraph с 4 узлами: perceive → act → verify → emit_event (без explore или heal пока). Python подключается к TS через MCP client subprocess. Python подключается к Go через gRPC (ConfigService + EventService). Жёстко закодированный тестовый сценарий (без LLM) для сквозной валидации всей сантехники. Затем добавить LLM-driven узел act: LLM выбирает действие из a11y tree. Добавить BudgetService gRPC и проверку BudgetService.ConsumeTokens перед LLM-вызовом. Результат: трёхпроцессный запуск, навигирующий на target_url, выполняющий выбранные LLM действия и производящий отчёт запуска.

ФАЗА 4 — Explore → Plan freeze → Replay (недели 5-7):
  Добавить узел explore (LLM-driven построение sitemap). Добавить узел plan с PlanService gRPC (FreezePlan / GetFrozenPlan). Добавить plan-manager для предварительного патчинга healing-локаторами. Реализовать режим запуска replay (без LLM в act/verify, кроме healing). Реализовать вычисление dom_hash и DOM_DRIFT_WARNING. Подтвердить базовое утверждение об CI-детерминизме: запустить explore один раз, запустить replay 10 раз, убедиться в идентичном выполнении сценариев. Результат: CI-воспроизводимые replay-запуски против staging-окружения.

ФАЗА 5 — Self-healing (недели 7-9):
  Добавить узел heal в LangGraph. Полная иерархия стратегий локаторов в locator-resolver. Реализация confidence gate. PersistenceService.RecordHealedLocator + GetHealedLocators. Предварительный патчинг плана с healing-локаторами. Логика карантина flake. Событие HEALING_ESCALATION + заглушка вебхука. Режим запуска heal_audit. Результат: агент, автоматически восстанавливающий сломанные локаторы и сохраняющий исправления для будущих запусков.

ФАЗА 6 — Полная наблюдаемость и артефактный конвейер (недели 9-11):
  OpenTelemetry на всех трёх уровнях (OTLP export). trace-controller в TS (Playwright traces). codegen-exporter, производящий .spec.ts-вывод. Полный HTML-отчёт с разделом healing audit. SARIF-вывод. cost-tracker с таблицей цен. orch-cli cost --since=7d. Эндпоинт Prometheus /metrics. Шаблон Grafana-дашборда. JUnit XML для интеграции с CI. Артефакт healing-audit JSONL. Результат: артефакты запусков production-уровня, пригодные для потребления инженерной командой.

ФАЗА 7 — Развёртывание K8s / ArgoCD (недели 11-12):
  Helm chart с тремя Deployments (Go orchestrator + HTTP API, Python brain pool, TS executor pool). Go persistence-gateway с Postgres-бэкендом (внешний). AsyncPostgresSaver для LangGraph. ArgoCD Application manifest. Горизонтальный pod autoscaler для Python brain (масштабирование по глубине очереди запусков). Конфиг per-namespace для целевых URL dev/staging/prod. Результат: стек, развёртываемый в homelab через ArgoCD GitOps.

**Ключевые риски:**

- Пробелы в покрытии accessibility tree: shadow DOM, canvas-элементы, кастомные веб-компоненты и cross-origin iframe'ы часто производят неполные или отсутствующие записи в page.accessibility.snapshot(). Агент будет иметь частичное восприятие современных SPA. Митигация: документировать известные слепые пятна per-AUT в отчёте запуска; откатываться к DOM-снимку + CSS-селектору для элементов, отсутствующих в a11y tree; рассмотреть Playwright's aria snapshots (проверить доступность в текущей версии Playwright) как альтернативный API.
- Утечка exploration-недетерминизма в план: LLM-фаза exploration может производить существенно различающиеся замороженные планы при повторных запусках (разный порядок обнаружения flow, разные имена сценариев), делая сравнение plan_id ненадёжным для диффинга. Митигация: диффинг плана является структурным (сравниваются action_type + page_url шага, а не равенство plan_id); exploration — это отдельное задание, запускаемое редко; повторная заморозка плана — это явное действие оператора, а не автоматическое.
- Взрыв стоимости токенов на крупных SPA: SPA с 50 страницами и богатыми интерактивными flow может потреблять 300K+ токенов в одном exploration-запуске, если не применяется ограничение глубины. По ценам Opus 4.8 это нетривиально. Митигация: жёсткое ограничение exploration_depth в конфиге (по умолчанию 3 перехода от корня); per-page token budget; BudgetService BUDGET_EXCEEDED останавливает exploration досрочно с частичным планом вместо сбоя запуска; инкрементальный exploration (только страницы, отсутствующие в существующем sitemap).
- Стоимость координации версионирования proto: Python brain быстро развивается (новые LangGraph-узлы, новые gRPC-вызовы к новым методам PersistenceService); каждое добавление требует изменения proto + перекомпиляции Go + координированного развёртывания. В быстром цикле итерации это создаёт трение. Митигация: проектировать все proto-сообщения с опциональными полями и oneof; поддерживать обратную совместимость хотя бы на одну мажорную версию; proto — явная цена hexagonal-границы — принять трение, автоматизировать генерацию заглушек в CI.
- Инфраструктурная зависимость LangGraph checkpointer: AsyncPostgresSaver требует живого экземпляра Postgres; в K8s это означает, что соединение с Postgres должно быть работоспособным до того, как Python brain сможет сделать checkpoint. Сбой Postgres в середине запуска означает, что запуск не может сделать checkpoint, но может продолжать выполнение. Митигация: сделать checkpointing неблокирующим (ошибки checkpoint генерируют событие WARNING, но не останавливают выполнение); SQLite checkpointer всегда доступен как откат на single-host.
- Отсутствие flow control в MCP stdio-протоколе: если Python выдаёт вызовы browser_action в жёстком цикле (что подграфы LangGraph с параллелизмом могут делать), stdio-канал не имеет backpressure. Митигация: текущий дизайн по своей природе последователен per LangGraph-узел — один ожидающий MCP-вызов одновременно; если параллелизм добавляется позже (параллельное выполнение сценариев), каждая параллельная ветвь должна использовать отдельный подпроцесс playwright-executor с собственным stdio-каналом.
- Хрупкость DOM hash как якоря baseline и предварительного патча локаторов: вычисление SHA-256 по сериализованному a11y tree означает, что любое изменение любого элемента (даже не связанного с тестируемым flow) инвалидирует healing-локаторы для всей страницы. Митигация: вычислять dom_hash только по поддереву, укоренённому в целевом контейнерном элементе сценария (полученном из метаданных сценария), а не по всей странице; сделать область хэша настраиваемой; документировать, что нестабильные виджеты (аналитика, реклама, баннеры A/B-тестов) должны быть исключены через CSS ignore-list в конфиге.

**ADR:**

- ADR-001: MCP JSON-RPC 2.0 over stdio для границы Python-TypeScript. Принято: MCP stdio — жизненный цикл подпроцесса тривиально принадлежит Python Popen, нет управления портами, инструменты валидируются по схеме через JSON Schema при подключении, соответствует устоявшемуся паттерну инструментирования экосистемы Claude, инспектируем любым JSON-RPC-отладчиком. Отклонено: HTTP JSON-RPC local server — требует выделения портов (конфликты портов в CI в контейнерных окружениях реальны), независимого цикла поллинга для проверки работоспособности, отдельного механизма синхронизации процессов; больше точек отказа для того, что по сути является внутрипроцессным вызовом функции.
- ADR-002: gRPC + protobuf для границы Go-Python. Принято: gRPC — контракт принудительно применяется на этапе компиляции через сгенерированные заглушки для обоих языков из общего .proto-источника; двоично-эффективный; нативный стриминг на EventService обеспечивает отрисовку отчётов в реальном времени без поллинга; интерсепторы обеспечивают чистую точку инжекции OpenTelemetry. Отклонено: REST/JSON — нет принудительного контроля схем на этапе компиляции между двумя независимо реализованными эндпоинтами; дрейф между Go-хендлером и Python-клиентом — это вопрос «когда», а не «если»; нет нативного стриминга без сложности SSE.
- ADR-003: Go как spine control plane (не Python). Принято: Go — единый статический бинарник для CI-дистрибуции с нулевыми runtime-зависимостями; надзор за процессами на основе горутин чисто обрабатывает жизненный цикл Python brain + TS executor; быстрый старт (< 50 мс vs Python cold start); нет GIL; нативная Prometheus-инструментация. Отклонено: Python как оркестратор — смешение логики рассуждений агента и надзора за процессами в одном Python-процессе устраняет hexagonal-границу между мозгом и control plane; накладные расходы Python на запуск и сложность управления пакетами делают его плохим выбором для CI-бинарника.
- ADR-004: LangGraph StateGraph как backbone агента (не кастомный async-цикл). Принято: LangGraph — встроенный checkpointing (AsyncSqliteSaver/AsyncPostgresSaver) устраняет одну из основных проблем реализации; условные рёбра декларативно выражают ветвление heal/retry/escalate; пауза/возобновление с участием человека является первоклассной функцией; объект состояния типизирован и инспектируем; стриминг нативный. Отклонено: кастомный async Python-цикл — нужно самостоятельно реализовывать checkpointing, восстановление после сбоев, human-gate, стриминг и сериализацию состояния; усложняется с каждым новым узлом и ребром; становится обязательством, когда LangGraph добавляет новые функции (параллельные ветви, подграфы), которые кастомный цикл не может принять.
- ADR-005: Accessibility tree как основной канал восприятия, дополненный set-of-marks-скриншотом. Принято: a11y tree (page.accessibility.snapshot()) + set-of-marks-скриншот — a11y tree даёт структурированные семантические данные (role, name, state), оптимальные для генерации локаторов; set-of-marks-оверлей даёт пространственную/визуальную привязку для элементов, о которых LLM нужно рассуждать позиционно; комбинированный канал — наиболее сильный из доступных без инжектирования тестовой инструментации в AUT. Отклонённая основная альтернатива A: полный DOM-снимок — слишком большой для LLM context window на реальном SPA (100K+ токенов HTML); слишком зашумленный (стили, скрипты, метаданные); генерация локаторов из сырого DOM производит хрупкие CSS-селекторы. Отклонённая основная альтернатива B: только скриншот — теряет структурированные семантические данные, необходимые для детерминированной генерации локаторов; вынуждает к попиксельному рассуждению, которое деградирует на динамическом контенте и дорогостоящее per-вызов.
- ADR-006: Explore-once / replay-many для CI-детерминизма. Принято: отдельная недетерминированная фаза exploration (периодическое задание) производит замороженный план; детерминированная фаза replay (каждый CI-триггер) потребляет замороженный план; LLM вызывается в CI только для healing, а не exploration. Отклонено: LLM-seeded exploration при каждом CI-запуске — LLM не предоставляют стабильный публичный API сидирования; незначительные обновления версии модели (которые происходят без предупреждения) меняют вывод даже с фиксированным seed; этот подход не может предоставить гарантию воспроизводимости, требуемую CI-гейтом. Отклонено: запись и воспроизведение HTTP-трафика браузера — хрупко при любом изменении бэкенда; поверхностно тестирует поведение UI без рассуждений агента; не решает проблему self-healing.
- ADR-007: Go persistence-gateway как эксклюзивный владелец БД. Принято: всё долгосрочное состояние (healing-локаторы, sitemaps, планы, baselines, история запусков) доступно исключительно через Go gRPC PersistenceService; Python и TS никогда не держат соединения с БД напрямую. Отклонено: прямой доступ Python к БД (например, SQLAlchemy) — два языковых рантайма, совместно владеющих одной схемой, создают координацию миграции: Go запускает миграцию схемы при старте, Python не должен запускать параллельные миграции; управление пулом соединений через два рантайма чревато ошибками; нарушает hexagonal-принцип единственного владельца порта персистентности.
- ADR-008: Sonnet 4.6 для healing / Opus 4.8 для exploration, модель per-тип узла. Принято: дифференцированный выбор модели по узлу — Opus 4.8 (более высокое рассуждение) для узла explore, где качество открытого планирования определяет покрытие; Sonnet 4.6 (быстрее + дешевле) для узла heal, где задача ограничена (сопоставить один элемент из структурированного дерева) и задержка важна (healing находится в критическом пути выполнения теста). Отклонено: однородный Opus 4.8 для всех узлов — healing потенциально запускается десятки раз за запуск; задержка Opus в цикле heal→act→verify сделала бы запуски неприемлемо медленными и дорогими; ограниченная природа healing не требует полных возможностей рассуждения Opus.

---


### `agentic-core` — CognitivePilot — Perception-First Autonomous UI Test Agent

*Lens: Cognitive / agentic-loop first*

**Философия.** Когнитивный цикл — это и есть продукт: каждое архитектурное решение — выбор языка, протокол взаимодействия, схема хранения данных — существует для того, чтобы обслуживать цикл perception→plan→act→verify→heal на LangGraph, работающий на Python. Агент рассматривает дерево доступности как свой основной орган чувств, поскольку семантическая ARIA-структура переживает смену DOM значительно лучше, чем CSS-селекторы; когда же дерево отказывает (приложения на canvas, shadow DOM, кастомные элементы), визуальная привязка set-of-marks даёт LLM пространственный якорь без откатов к хрупким XPath. Go — детерминированный позвоночник, который делает недетерминированный мозг LLM надёжным в CI: он отвечает за заморозку плана, персистентность локаторов, соблюдение бюджета и human gate. TypeScript — это ловкие руки: Playwright, открытый как нативный MCP-сервер инструментов, позволяет LLM вызывать примитивы браузера ровно так же, как любой другой инструмент, — без малейшего импедансного рассогласования между циклом агентного рассуждения и слоем выполнения в браузере.

**Компоненты (15):**

| Component | Language | Responsibility |
|-----------|----------|----------------|
| agent-ctl | Go | CLI-точка входа: команды run start/pause/stop/inspect/export; управление конфигурацией YAML + env; выводит структурированный статус запуска и ссылки на отчёты; единственный бинарник, распространяемый на CI-раннеры |
| orchestrator | Go | FSM жизненного цикла запуска (PENDING→RUNNING→HEALING→PAUSED→DONE/FAILED); gRPC-сервер, обращённый к мозгу на Python; watchdog-таймер, обнаруживающий прерывание потока от мозга; очередь планирования; маршрутизирует HumanGateEvent и оповещения о бюджете в event-router |
| persistence-gateway | Go | gRPC-сервис поверх SQLite (одиночный узел) или PostgreSQL (несколько раннеров); владеет хранилищем исцелённых локаторов, графом смежности карты сайта, хранилищем замороженных планов, историей запусков, кэшем page-объектов; один writer сериализует все межраннерные записи |
| report-service | Go | REST API сбора артефактов; генерация HTML- и JSON-отчётов запуска; раздача статических файлов трейсов Playwright по /ui/traces/{run_id}; эндпоинт Prometheus /metrics; архив JSONL транскриптов LLM |
| event-router | Go | Внутренний pub/sub (каналы в процессе + опциональная отправка на webhook); маршрутизирует HumanGateEvent в Slack/webhook; генерирует оповещения budget-consumed; открывает /api/healing/{id} для разрешения human в режиме долгоживущего сервиса |
| brain | Python | Стейт-машина LangGraph: 8 узлов (perceive, ground, plan, act, verify, heal, checkpoint, report) с условной маршрутизацией рёбер; checkpointer LangGraph на SQLite/PostgreSQL для возобновляемых запусков; владеет AgentState TypedDict; точка входа, запускаемая Go orchestrator через gRPC StartRun |
| perception | Python | Парсер и нормализатор дерева доступности; оценщик полноты (отношение интерактивных ARIA-ролей к общему числу узлов); координатор set-of-marks (делегирует захват скриншота в TS MCP, добавляет логику нумерованных оверлеев в Python); нормализатор DOM-снимка; определяет, какую модальность восприятия использовать за цикл |
| planner | Python | Шаблоны промптов LLM (Opus 4.8 для explore/plan, Sonnet 4.6 для verify/heal); применение схемы структурированного вывода для TestAction и Verdict; сериализатор/десериализатор плана для freeze/thaw; guard бюджета, блокирующий вызовы LLM при нехватке оставшихся токенов ниже порога |
| locator-grinder | Python | Движок ротации стратегий из 6 уровней (data-testid → aria-label → role+name → text+role → CSS → XPath); разрешение локатора через вызовы MCP resolve_locator; оценщик уверенности с базовыми оценками на стратегию; агрегатор предложений по исцелению по всем трём попыткам re-grounding |
| memory-client | Python | gRPC-клиент к Go persistence-gateway; получает known_locators и sitemap_fragment для текущего url_pattern на входе в perceive; сбрасывает HealedLocatorRecord и SitemapEdge на checkpoint; управляет LRU-кэшем в процессе с TTL 10 минут для записей локаторов |
| playwright-mcp-server | TypeScript | MCP-сервер (транспорт stdio), открывающий 12 инструментов Playwright: navigate_to, get_accessibility_tree, take_screenshot, click_element, fill_input, get_dom_snapshot, resolve_locator, start_trace, stop_trace, scroll_to, wait_for_navigation, get_network_log; полный API браузера как каталог инструментов MCP |
| browser-controller | TypeScript | Жизненный цикл Browser/BrowserContext/Page в Playwright; изоляция контекста на каждый запуск; инъекция состояния аутентификации (cookie jar / storage state по ссылке из Go vault); конфигурация viewport, локали и сетевых условий; надзор за процессом браузера |
| snapshot-service | TypeScript | Захват дерева доступности через Playwright snapshot API; сериализация DOM с разглаживанием shadow DOM; захват скриншота в PNG; возвращает структурированный JSON в Python через ответы инструментов MCP; измеряет задержку захвата для наблюдаемости |
| set-of-marks-renderer | TypeScript | Перечисляет все интерактивные элементы на текущей странице; рендерит нумерованные прямоугольные метки как canvas-оверлей на скриншоте; возвращает аннотированное изображение (base64) + карту элементов (index → {role, name, bounding_box}) в Python для визуальной привязки в heal-узле |
| trace-emitter | TypeScript | Управление жизненным циклом трейса Playwright (start/stop/archive); упаковывает ZIP трейса на сегмент запуска; HTTP POST с артефактом в Go report-service с повторными попытками + экспоненциальной выдержкой; прикрепляет OTEL-спан со ссылкой на ID артефакта трейса |

**Границы (полиглотные контракты).**

Go ↔ Python — gRPC (proto3), двунаправленный стриминг:

GoOrchestrator → PythonBrain RPC calls: StartRun(RunConfig), PauseRun(run_id), StopRun(run_id), InjectHumanDecision(healing_id, approved_locator_or_skip). PythonBrain → GoOrchestrator streamed RunEvents: {type: STEP_COMPLETE | HEAL_NEEDED | HUMAN_GATE | BUDGET_WARNING | DONE | ERROR, payload_json}. PythonBrain → GoPersistenceGateway RPC calls: GetLocators(url_pattern) → LocatorList, SaveHealedLocator(HealedLocatorRecord) → void, GetFrozenPlan(plan_hash) → PlanJSON, SaveFrozenPlan(PlanJSON) → plan_hash, GetSitemap(url_pattern) → SitemapNode, UpdateSitemap(SitemapEdge) → void, GetRunHistory(run_id) → RunSummary. Обоснование: gRPC обеспечивает типизированные контракты; Go-стабы, сгенерированные из .proto, являются единственным источником истины для контракта Go↔Python; стриминг естественен для непрерывного потока run-событий. Изоляция отказов: watchdog Go ожидает событие STEP_COMPLETE или keepalive каждые 60 секунд; тишина в потоке → помечает запуск как FAILED, сохраняет ссылку на последний чекпоинт для возобновления через StartRun(resume_from=checkpoint_id).

Python ↔ TypeScript — MCP (Model Context Protocol, субстрат JSON-RPC 2.0) через stdio:

TS playwright-mcp-server реализует спецификацию MCP-сервера; мозг на Python связывает все 12 инструментов через интеграцию MCP-клиента в LangGraph (доступна через langchain-mcp или прямой MCP-клиент — уточнить актуальное название пакета). Транспорт: stdio для развёртывания в одном поде (подпроцесс Python или sidecar-контейнер, разделяющий именованный пайп); переключается на HTTP+SSE для распределённого режима с несколькими раннерами простой сменой конфигурации транспорта MCP-клиента. Обоснование выбора MCP вместо gRPC для этой границы: MCP является нативным протоколом для вызова инструментов LLM — LangGraph может связывать инструменты MCP напрямую как инструменты агента без адаптерного слоя; TS-слой семантически IS является сервером инструментов; написание бесповоротного gRPC-сервиса потребовало бы трансляционного слоя (gRPC proto → вызовы Playwright), который MCP устраняет; схемы ввода/вывода инструментов автоматически валидируются с обеих сторон. Изоляция отказов: таймаут на инструмент (navigate_to: 30s, get_accessibility_tree: 10s, click_element: 15s) перехватывается в узлах act/perceive Python; ошибка инструмента → маршрут в heal-узел; повторный разрыв соединения MCP → эскалирует BRAIN_ERROR RunEvent в Go orchestrator через gRPC.

TypeScript → Go — REST/HTTP:

POST /api/artifacts/trace (multipart ZIP), POST /api/artifacts/screenshot (JSON + base64 ref), GET /health (вызывается Go browser-supervisor). Обоснование: отправка артефактов нечастая, однонаправленная и включает крупные бинарные блоки — REST проще и более отлаживаем, чем gRPC-стриминг для загрузки blob'ов; TS trace-emitter буферизует локально и повторяет попытки с экспоненциальной выдержкой; эндпоинт Go report-service идемпотентен по run_id + artifact_type.

**Цикл агента.**

Явная стейт-машина LangGraph. 8 узлов, типизированные условные рёбра, checkpointer на SQLite (переключается на PostgreSQL для нескольких раннеров).

─── SHARED STATE: AgentState (TypedDict) ───

run_id: str
session_id: str
mode: Literal["explore", "replay", "heal"]
config: RunConfig  # base_url, token_budget_per_model, auth_config, tags, plan_hash_override

# Perception
current_snapshot: PageSnapshot  # a11y_tree_json, screenshot_b64, marks_overlay, url, ts, completeness_score
snapshot_history: Deque[PageSnapshot]  # ring buffer max=10; evict+summarise via LLM when full

# Planning
plan_hash: str  # SHA-256 of frozen plan; empty string in explore mode
pending_actions: Deque[TestAction]  # {action_type, target_semantic_id, target_description, locator_hint, expected_outcome, reasoning}
completed_tests: List[TestResult]  # {test_id, verdict, confidence, healed, artifact_refs}

# Episodic memory (session)
action_history: List[ActionRecord]  # last 20 full records; older replaced by LLM-generated episode summaries
page_visit_log: List[UrlVisit]  # {url_pattern, visit_count, last_ts}

# Long-term memory refs (fetched, not stored inline — LRU cache in memory-client)
known_locators: Dict[str, LocatorRecord]  # semantic_id → {strategy, value, confidence, healed_at}
sitemap_fragment: Optional[SitemapNode]  # outbound edges from current url_pattern

# Healing
healing_context: Optional[HealingContext]  # {semantic_id, failure_type, attempts_log}
healing_attempts: int  # reset to 0 on each new action

# Budget
token_usage: Dict[str, TokenCount]  # model_id → {prompt_tokens, completion_tokens, cost_usd}
token_budget: Dict[str, int]  # model_id → max_tokens_per_run (hard cap)

# Artifacts
artifact_refs: List[ArtifactRef]  # {type: trace|screenshot|report, url}

# Control
stop_signal: bool
human_gate_pending: bool

─── NODES ───

perceive — Точка входа для каждого цикла. Вызывает инструменты MCP в порядке приоритета:
(1) get_accessibility_tree() → разбирает в нормализованное дерево AccessibilityNode → вычисляет completeness_score = (interactive_role_count / total_node_count). Если completeness_score >= 0.30: основная модальность — дерево a11y. (2) Если completeness_score < 0.30 (canvas-приложения, кастомные элементы): вызывает take_screenshot() → set-of-marks-renderer накладывает нумерованные метки → сохраняет как SetOfMarks {annotated_image_b64, index_map}. (3) get_dom_snapshot() вызывается только при входе в heal-узел — сохраняется в состояние, но НЕ передаётся LLM (стоимость токенов; используется только структурно). Обновляет current_snapshot, добавляет в кольцевой буфер snapshot_history. При переполнении буфера: старейшая запись суммируется Sonnet 4.6 в краткое резюме эпизода длиной 200 токенов и добавляется в action_history; слот освобождается.

ground — Выполняется перед act, когда pending_actions не пуст. Извлекает требуемый локатор из pending_actions[0]: {strategy, value, semantic_id}. Проверяет state.known_locators[semantic_id] из кэша memory-client. Вызывает MCP resolve_locator(strategy, value) для проверки актуальной валидности в текущем DOM. Если валиден → генерирует GROUNDED, маршрут в act. Если недействителен → заполняет healing_context значениями {semantic_id, failure_type: GROUNDING_FAILED, last_locator}, увеличивает healing_attempts, маршрут в heal.

plan — LLM-узел (Opus 4.8 в режиме explore; пропускается в режиме replay). Входные данные для LLM: current_snapshot.a11y_tree_json (или marks_overlay.annotated_image_b64 при низкой полноте), суммированный action_history (последние 5 полных + резюме эпизодов), sitemap_fragment (подсвеченные неисследованные рёбра), remaining_budget_pct. Роль в промпте: автономный QA-инженер, исследующий base_url; вывод: структурированный TestAction {action_type: CLICK|FILL|NAVIGATE|ASSERT_TEXT|ASSERT_VISIBLE, target_semantic_id, target_description, locator_hint, expected_outcome, reasoning} ИЛИ sentinel DONE. Структурированный вывод обеспечивается через схему ответа. Temperature=0 в CI/replay; 0.3 в explore. Добавляет TestAction в pending_actions; обновляет token_usage. В режиме REPLAY: загружает следующий TestAction из замороженного плана JSON вместо вызова LLM — узел plan является чистой выборкой данных, нулевая стоимость LLM.

act — Извлекает pending_actions[0]. Отображает TestAction.action_type → последовательность вызовов инструментов MCP: CLICK→click_element(resolved_locator); FILL→fill_input(locator, value) затем опционально click_element(submit_locator); NAVIGATE→navigate_to(url); ASSERT_TEXT / ASSERT_VISIBLE → встроенная проверка по текущему дереву a11y в current_snapshot (вызов браузера не нужен, если снимок свежее 2 секунд). Ожидает MCP ActionResult {success: bool, error_type: Optional[str], artifact_refs}. При успехе: добавляет ActionRecord в action_history, обновляет artifact_refs, маршрут в verify. При отказе: заполняет healing_context {semantic_id, failure_type: error_type, attempted_locator}, увеличивает healing_attempts, маршрут в heal.

verify — LLM-узел (Sonnet 4.6). Вход: TestAction.expected_outcome + поля ActionResult + целевой фрагмент дерева доступности (поддерево с корнем в задействованном элементе, не полное дерево — ограничение 2K токенов). LLM выводит Verdict {result: PASS|FAIL|AMBIGUOUS, confidence: float 0-1, reasoning: str}. Записывает TestResult в completed_tests. PASS → checkpoint. FAIL, когда reasoning указывает на неверный элемент (путаница локаторов) → маршрут в heal с failure_type: ASSERTION_MISMATCH. FAIL, когда reasoning указывает на логику приложения → записывается как настоящий FAIL, маршрут в checkpoint. AMBIGUOUS → записывается с предупреждением, маршрут в checkpoint (не блокирует запуск).

heal — Многостратегийный узел. Полный алгоритм см. в разделе selfHealing. Выводит HealedLocator {strategy, value, confidence} или HealingFailure. При успехе (confidence >= 0.60): обновляет state.known_locators[semantic_id], отправляет HealedLocatorRecord в Go persistence-gateway через gRPC memory-client, маршрут в ground для повторной попытки. При confidence 0.60–0.84: сохраняет с review_required=true, генерирует HEAL_NEEDED RunEvent в Go orchestrator, продолжает запуск. При отказе или confidence < 0.60: генерирует HUMAN_GATE RunEvent в Go orchestrator с полным HealingContext; устанавливает human_gate_pending=true; в неинтерактивном CI: записывает SKIPPED_HEALING_FAILURE, маршрут в checkpoint. Если healing_attempts >= 3 для того же semantic_id: генерирует QuarantineEvent, помечает QUARANTINED в persistence, маршрут в checkpoint безусловно.

checkpoint — Сериализует полный AgentState в бэкенд checkpointer LangGraph. Тег: {run_id, step_id, plan_hash, token_usage_snapshot, ts}. Сбрасывает healing_attempts=0, healing_context=None. Проверяет терминальные условия: stop_signal=true ИЛИ все модели на пределе бюджета ИЛИ (pending_actions пуст И LLM вернул DONE). Нетерминальный → perceive. Терминальный → report.

report — Терминальный узел. Агрегирует completed_tests, diff healed_locators, token_usage, artifact_refs. HTTP POST /api/runs/{run_id}/complete в Go report-service с полным JSON RunSummary. Генерирует экспортированные TypeScript page-object стабы из стабильных known_locators (confidence >= 0.85, не помечены как quarantined). Генерирует финальное gRPC RunEvent{DONE, summary} в Go orchestrator.

─── EDGES ───

START → perceive
perceive → ground  (when pending_actions non-empty)
perceive → plan    (when pending_actions empty)
ground  → act      (GROUNDED)
ground  → heal     (GROUNDING_FAILED)
plan    → act      (action appended to pending_actions)
plan    → report   (DONE signal OR budget_exhausted)
act     → verify   (ACT_SUCCESS)
act     → heal     (ACT_FAILED)
verify  → checkpoint (always — verdict recorded)
heal    → ground   (confidence >= 0.60, healing_attempts < 3, retry)
heal    → checkpoint (confidence 0.60–0.84, flagged-persist path OR human_gate skip path)
heal    → checkpoint (quarantine: healing_attempts >= 3)
checkpoint → perceive (non-terminal)
checkpoint → report   (terminal conditions met)

**Самовосстановление.**

Пошаговый алгоритм, выполняемый внутри heal-узла, управляемый HealingContext {semantic_id, failure_type, attempted_locator, element_description}.

ШАГ 1 — ВОСПРИЯТИЕ ОТКАЗА (перепривязка модели агента к странице):
Не переиспользовать current_snapshot — DOM изменился с момента неудачного действия. Немедленно вызвать подпрограмму perceive: get_accessibility_tree() → пересчитать completeness_score. Если completeness_score < 0.30: также вызвать take_screenshot() + set-of-marks-renderer. Загрузить историю исцелений из state.known_locators[semantic_id] (опробованные ранее стратегии, предыдущие оценки уверенности, флаг quarantine). Если элемент уже QUARANTINED: пропустить все попытки, записать SKIPPED_QUARANTINE, немедленно вернуться в checkpoint.

ШАГ 2 — ПОПЫТКА RE-GROUNDING 1: Ротация стратегий на свежем снимке:
Пройти шестиуровневую иерархию локаторов для целевого semantic_id, вызывая MCP resolve_locator(strategy, value) для каждого. Остановиться на первом живом совпадении.
Tier 1: data-testid attribute match → confidence_base = 1.00
Tier 2: aria-label exact match → confidence_base = 0.95
Tier 3: ARIA role + accessible name combination → confidence_base = 0.90
Tier 4: visible text content + role → confidence_base = 0.80
Tier 5: CSS structural selector (nth-of-type, class+tag) → confidence_base = 0.60
Tier 6: XPath → confidence_base = 0.45
Итоговая уверенность = confidence_base × 0.95 (дисконт за изменённый контекст DOM). Если совпадение найдено на Tier >= 5: генерирует метрику strategy_degradation — сигнализирует о структурной нестабильности DOM. Записывает attempt1_confidence = итоговая уверенность или 0, если совпадений нет.

ШАГ 3 — ПОПЫТКА RE-GROUNDING 2: Рассуждение LLM по дереву доступности:
Запускается только при отсутствии совпадений в попытке 1. Передаёт свежий a11y_tree_json в Sonnet 4.6 с структурированным промптом: описание semantic_id, element_description из HealingContext, полный JSON дерева доступности (урезанный до 8K токенов при необходимости — сначала внутренний фрагмент вблизи предполагаемого элемента). LLM выводит структурированный HealingProposal {locator_strategy, locator_value, confidence_0_to_1, reasoning}. Применяется эмпирический дисконт: attempt2_confidence = LLM_confidence × 0.90 (коррекция на систематическую самоуверенность LLM). Если attempt2_confidence >= 0.55: вызывает MCP resolve_locator(proposal.strategy, proposal.value) для проверки, что предложение действительно присутствует в живом DOM перед принятием — если не найдено, устанавливает attempt2_confidence = 0.

ШАГ 4 — ПОПЫТКА RE-GROUNDING 3: Визуальная привязка по set-of-marks:
Запускается только при attempt2_confidence < 0.55. Требует completeness_score < 0.50 ИЛИ failure_type == ELEMENT_NOT_FOUND при пустой ротации стратегий. Вызывает take_screenshot() (или переиспользует из шага 1, если свежее 5 секунд) + set-of-marks-renderer → аннотированное изображение с нумерованными метками интерактивных элементов + index_map. Передаёт аннотированное изображение в Sonnet 4.6 vision с промптом: "Previously this agent interacted with [element_description: role, label, visual context]. Which numbered mark in this screenshot most likely corresponds to that element? Output: mark_number, confidence_0_to_1, reasoning." LLM определяет mark_number → находит index_map[mark_number] → извлекает локатор из узла дерева доступности по этому индексу. attempt3_confidence = llm_visual_confidence × 0.85.

ШАГ 5 — АГРЕГАЦИЯ УВЕРЕННОСТИ:
final_confidence = max(attempt1_confidence, attempt2_confidence, attempt3_confidence).
winning_strategy = стратегия из попытки, давшей final_confidence.
Порог персистентности:
  >= 0.85 → авто-сохранить в healed-locator store через gRPC memory-client; продолжить запуск без паузы; увеличить счётчик healed_auto в метриках
  0.60–0.84 → сохранить с флагом review_required=true; сгенерировать HEAL_NEEDED RunEvent в Go orchestrator; сгенерировать метрику healing_degraded; продолжить запуск (неблокирующий режим)
  < 0.60 → НЕ сохранять (загрязнит хранилище локаторов плохими данными); сгенерировать HUMAN_GATE RunEvent с {run_id, semantic_id, all_attempts_log, best_proposal_if_any, snapshots_refs}; установить human_gate_pending=true; в неинтерактивном CI: записать SKIPPED_HEALING_FAILURE; маршрут в checkpoint

ШАГ 6 — ПЕРСИСТЕНТНОСТЬ И АУДИТОРСКИЙ СЛЕД:
Записать HealedLocatorRecord в Go persistence-gateway: {id, url_pattern, semantic_id, element_role, element_label, old_locator_strategy, old_locator_value, new_locator_strategy, new_locator_value, confidence, healed_at, run_id, review_required, reasoning_transcript_ref}. Ссылка reasoning_transcript_ref указывает на запись JSONL в архиве транскриптов запуска (не хранится встроено — слишком большой объём). Сгенерировать OTEL-спан со всеми полями HealedLocatorRecord в качестве атрибутов спана. Обновить state.known_locators[semantic_id] в процессе.

ШАГ 7 — ПОРОГ КАРАНТИНА:
После сохранения (или пропуска): увеличить healing_attempts. Если healing_attempts >= 3 для одного и того же semantic_id в рамках данного запуска: сгенерировать QuarantineEvent в Go event-router с {semantic_id, run_id, attempts_log}. Пометить элемент как QUARANTINED в persistence. Все последующие тесты в этом запуске, ссылающиеся на данный semantic_id, немедленно пропускаются со статусом SKIPPED_QUARANTINE — никаких дополнительных вызовов LLM для этого элемента. Go report-service включает quarantine_count в сводку отчёта о запуске. CI pipeline завершается с ошибкой только при quarantine_count > настраиваемого порога (по умолчанию: 5).

**Детерминизм.**

Основной контракт: исследовать однажды, воспроизводить детерминированно, исцелять с полным аудиторским следом.

ЗАМОРОЗКА ПЛАНА:
Первый запуск выполняется в режиме "explore". После завершения узла report полная последовательность объектов TestAction (с разрешёнными локаторами, схемами утверждений, ожидаемыми результатами) сериализуется в JSON. SHA-256(plan_json) = plan_hash. Хранится в таблице frozen_plans Go persistence-gateway, ключ: (base_url, config_hash, plan_hash). Замороженный план также записывается как версионированный JSON-артефакт в artifact store Go report-service — пригоден для коммита в систему контроля версий рядом с приложением. Последующие CI-запуски: Go orchestrator читает plan_hash из конфига или artifact store → передаёт в RunConfig → узел plan Python полностью обходит LLM, загружает следующий TestAction из замороженной деки. LLM вызывается только в узлах verify и heal.

ДЕТЕРМИНИРОВАННОЕ ИССЛЕДОВАНИЕ:
В режиме explore порядок исследования задаётся: (1) BFS-обходом карты сайта от base_url (детерминированный обход графа), (2) вызовами LLM плана при Temperature=0 с фиксированным тегом версии системного промпта. Сид исследования = SHA-256(base_url + nav_structure_fingerprint), включённый в RunConfig и залогированный в историю запусков. Одинаковый сид + одинаковая структура приложения → одинаковый путь исследования. Отпечаток nav_structure_fingerprint выводится из первого снимка дерева доступности (стабилен между запусками при неизменном приложении).

СНИМКИ ЗОЛОТОГО СОСТОЯНИЯ:
После первого стабильного исследовательского запуска JSON дерева доступности, захваченный на каждом checkpoint, записывается в persistence как golden_state {run_id, step_id, url_pattern, a11y_tree_json, ts}. Во всех последующих replay-запусках узел perceive вычисляет структурный diff текущего a11y_tree относительно golden_state для соответствующего step_id (расстояние редактирования дерева по узлам ARIA role+name). Diff ниже порога → продолжить. Diff выше порога → сгенерировать метрику GOLDEN_STATE_DIVERGENCE и пометить в отчёте о запуске; если оценка расхождения > настраиваемого лимита → немедленно запустить проверку устаревания плана (не ждать N сбоев исцеления).

ОБНАРУЖЕНИЕ УСТАРЕВАНИЯ ПЛАНА:
В режиме replay: если healing_attempts >= 2 на >= 3 разных semantic_id в рамках одного запуска → сгенерировать событие PLAN_STALE. Go orchestrator планирует один свежий цикл исследования (mode="explore") для регенерации замороженного плана. Новый plan_hash заменяет старый в persistence. Старый план архивируется с отметкой valid_until. Порог устаревания настраивается на base_url (по умолчанию: 3 элемента, по 2 попытки каждый).

КАРАНТИН НЕСТАБИЛЬНЫХ ТЕСТОВ:
Тест, идентифицируемый по (action_type, semantic_id), который проваливается >= 3 последовательных запуска подряд, и при этом исцеление каждый раз успешно разрешает локатор (локатор успешно исцелён, но verify всё равно возвращает FAIL или AMBIGUOUS) → классифицируется как FLAKY. Статус FLAKY хранится в persistence. Отчёт CI разделяет FLAKY_COUNT и реальный FAIL_COUNT. Нестабильные тесты не блокируют pipeline по умолчанию (настраивается); логируются в очередь проверки исцелений для инспекции человеком.

ПРИНУДИТЕЛЬНЫЙ БЮДЖЕТ ТОКЕНОВ:
Жёсткий лимит на модель на запуск определён в RunConfig.token_budget {model_id: max_tokens}. AgentState.token_usage отслеживает потреблённые токены. Guard бюджета срабатывает в двух местах: (1) узел plan — если оставшийся бюджет Opus < 3K токенов, прервать новое исследование, установить stop_signal=true; (2) узел heal — если оставшийся бюджет Sonnet < 2K токенов, пропустить попытки исцеления 2 и 3 (вызовы LLM), использовать только ротацию стратегий (попытка 1). Это ограничивает стоимость LLM за запуск до известного потолка. Процент потреблённого бюджета указывается в отчёте о запуске; превышение 80% вызывает оповещение Prometheus.

НЕИЗМЕНЯЕМОСТЬ ТРАНСКРИПТОВ LLM:
Каждый вызов LLM добавляет запись в append-only JSONL-транскрипт, ключ — run_id: {entry_id, ts, node, model, prompt_hash, response_hash, prompt_tokens, completion_tokens, cost_usd, full_prompt, full_response}. Транскрипт хранится Go report-service, удерживается согласно политике (по умолчанию 30 дней). OTEL-спаны ссылаются на transcript_entry_id (не на содержимое). Для воспроизведения любого запуска без новых API-вызовов: загрузить транскрипт + замороженный план → выполнить детерминированно. Это также запись для аудита затрат.

**Память.**

КРАТКОСРОЧНАЯ — эпизодическая, в рамках сессии:

Хранилище: AgentState TypedDict LangGraph (Python-объект в процессе), поддерживаемый checkpointer LangGraph, записывающим в SQLite с ключом (run_id, thread_id). Запись checkpoint происходит при каждом вызове узла checkpoint (каждая итерация цикла), обеспечивая возобновляемость после краша или OOM. Go orchestrator сохраняет последний checkpoint_ref, возвращённый мозгом при каждом событии STEP_COMPLETE; при перезапуске передаёт checkpoint_ref в StartRun → LangGraph загружает из SQLite.

Содержимое и ограниченные размеры: current_snapshot (актуальное состояние страницы, единственный объект); кольцевой буфер snapshot_history (максимум 10 объектов PageSnapshot; при заполнении старейший вытесняется после суммирования LLM в краткое резюме эпизода длиной 200 токенов, добавляемое в action_history — это предотвращает раздувание контекстного окна при длительных исследовательских запусках); action_history (последние 20 полных объектов ActionRecord + резюме эпизодов для более старых; резюме эпизодов — по 200 токенов каждое, т.е. 50 старых эпизодов = максимум 10K токенов используемого пространства); page_visit_log (неограниченный, но компактный: url_pattern + visit_count + ts); token_usage и artifact_refs (небольшие, неограниченные в рамках запуска).

Жизненный цикл: создаётся при gRPC-вызове StartRun; непрерывно записывается в checkpoint; читается для возобновления через thread_id; архивируется (только чтение) после завершения узла report.

ДОЛГОСРОЧНАЯ — усвоенная, межсессионная:

Всё долгосрочное хранилище принадлежит Go persistence-gateway. Python получает доступ через gRPC посредством модуля memory-client. Python никогда не пишет в SQLite напрямую — предотвращает конкуренцию параллельных записей.

(1) Хранилище исцелённых локаторов (таблица SQLite: healed_locators): столбцы {id, url_pattern, semantic_id, element_role, element_label, locator_strategy, locator_value, confidence, healed_at, run_id, review_required, quarantined}. Индекс по (url_pattern, semantic_id). memory-client получает все локаторы для текущего url_pattern одним gRPC-вызовом GetLocators на входе в perceive; кэширует в процессе с LRU и TTL 10 минут. При карантине: устанавливает quarantined=true, последующие запуски немедленно пропускают исцеление для данного semantic_id.

(2) Карта сайта / Граф знаний (таблица смежности SQLite: sitemap_edges): {from_url_pattern, action_label, to_url_pattern, confidence, last_seen_run_id}. BFS-обходим от base_url. Узел plan получает sitemap_fragment для текущего url_pattern для приоритизации неисследованных исходящих рёбер. Узел checkpoint записывает новые рёбра, обнаруженные в текущем цикле, через gRPC UpdateSitemap. Сохраняется между запусками — агент знает, какие страницы он исследовал, а какие нет.

(3) Хранилище замороженных планов (таблица SQLite: frozen_plans): {plan_hash, base_url, config_hash, plan_json, created_at, valid_until, explore_run_id}. Запрашивается Go orchestrator при запуске; передаёт plan_hash в RunConfig в мозг Python. Версионированность: старые планы сохраняются с valid_until для регрессионного анализа.

(4) Кэш page-объектов (таблица SQLite: page_objects): {url_pattern, last_run_id, object_json, содержащий скелет TypeScript-класса}. Записывается узлом report через Go persistence. Экспортируется как .ts-файлы по запросу через report-service для повторного использования в ручных тестах.

(5) Архив транскриптов LLM (append-only JSONL-файлы на run_id): управляется Go report-service. Ссылки из OTEL-спанов через transcript_entry_id. Удерживается согласно политике (по умолчанию 30 дней). Используется для аудита стоимости, детерминированного воспроизведения и инспекции человеком решений об исцелении.

(6) Бэкенд LangGraph Checkpointer: SQLite (одиночный узел, по умолчанию); PostgreSQL (многораннерный K8s — каждый раннер имеет свой thread_id, все разделяют базу данных checkpointer). Переключение — одно изменение конфига в конструкторе checkpointer LangGraph — без изменений кода.

(7) Векторное хранилище (Phase 2, опционально): локальный экземпляр ChromaDB (уточнить актуальный API и доступность модели эмбеддингов). Хранит эмбеддинги деревьев доступности, обеспечивая семантический поиск по сходству по страницам: "найти элемент, семантически похожий на кнопку Submit, которую мы видели на /checkout, но на /checkout/review". Используется heal-узлом, попытка 2, для аналогии элементов между страницами. Отложено до Phase 2, поскольку добавляет операционную сложность (ещё один процесс, задержка модели эмбеддингов) без критической важности для основного цикла исцеления.

**Наблюдаемость.**

РАСПРЕДЕЛЁННАЯ ТРАССИРОВКА — OpenTelemetry SDK на всех трёх языках, экспорт OTLP в Jaeger (одиночный узел) или Grafana Tempo (production K8s):

Go (otel-go): спаны для переходов состояний FSM жизненного цикла запуска (с атрибутами prev_state + next_state), длительности gRPC-вызовов к мозгу Python, длительности запросов persistence-gateway (таблица + операция), длительности генерации отчётов, генерации HumanGateEvent (с healing_id + semantic_id).

Python (otel-python): спаны для каждого выполнения узла LangGraph (node_name, duration_ms, snapshot_completeness_score); спаны для каждого вызова LLM (model_id, prompt_hash — SHA-256 промпта, НЕ содержимое для исключения секретов из трейсов, completion_hash, prompt_tokens, completion_tokens, cost_usd_estimate, decision_type: PLAN|VERIFY|HEAL, confidence_output); спаны для каждого вызова инструмента MCP (tool_name, duration_ms, success, error_type).

TypeScript (otel-node): спаны для каждого вызова инструмента MCP (tool_name, duration_ms); тайминги операций Playwright (navigate: url + time_to_interactive; click: locator + success; snapshot: node_count + completeness_score); тайминги событий браузера через CDP (First Contentful Paint, Largest Contentful Paint там, где доступно).

ТРАССИРОВКА И ВОСПРОИЗВЕДЕНИЕ РЕШЕНИЙ LLM:
Каждый вызов LLM: OTEL-спан ссылается на transcript_entry_id. Полный промпт + полный ответ записываются в append-only JSONL-транскрипт (файл с ключом run_id в хранилище Go report-service). transcript_entry_id — это индекс строки. Человек может восстановить точное рассуждение LLM для любого решения в любом запуске без доступа к API. Для воспроизведения: загрузить транскрипт → повторно выполнить решения без новых API-вызовов. Транскрипты неизменяемы после записи (append-only файловый дескриптор).

БЮДЖЕТ ТОКЕНОВ И СТОИМОСТИ:
AgentState.token_usage накапливает данные на модель на запуск. Go report-service открывает Prometheus gauge'и: llm_tokens_total{model, call_type: plan|verify|heal} и llm_cost_usd_total{model, run_id}. Вычисляется и указывается процент потреблённого бюджета; правило PrometheusAlertManager срабатывает при 80% лимита на запуск. Еженедельный тренд стоимости отображается в Grafana (стандартный стек Prometheus/Grafana, уже имеющийся в домашней лаборатории).

ТРЕЙСЫ PLAYWRIGHT:
TS trace-emitter оборачивает каждый запуск в сегмент трейса Playwright (start_trace на StartRun, stop_trace на каждой границе checkpoint). ZIP трейсов отправляется в Go report-service через HTTP POST. Report-service раздаёт Playwright trace viewer (статический HTML, поставляемый вместе с Playwright) по /ui/traces/{run_id}. Полное видео действий, сетевая шкала времени, журналы консоли и скриншоты захватываются в трейс — без дополнительной инструментации.

МЕТРИКИ PROMETHEUS (предоставляются Go report-service /metrics):
runs_total{status: success|fail|heal_fail|budget_exhausted, base_url_hash}
healing_attempts_total{attempt_type: strategy_rotation|llm_tree|visual_marks, outcome: success|fail}
healing_confidence_histogram{attempt_type} — распределение по корзинам выявляет качество калибровки и эффективность стратегий
llm_tokens_total{model, call_type}
llm_cost_usd_total{model} (счётчик, накопленный)
test_execution_latency_seconds histogram (на action_type)
flake_quarantine_count{base_url_hash} (gauge)
plan_staleness_detections_total (счётчик)
snapshot_completeness_histogram — распределение оценок полноты дерева доступности по всем вызовам perceive; низкая медиана сигнализирует о canvas-ориентированном приложении, требующем визуальной привязки

ОПОВЕЩЕНИЯ (правила Alertmanager):
healing_rate (healed / total_actions) > 0.20 в рамках одного запуска → предупреждение DOM_INSTABILITY
budget_consumed_pct > 0.80 → BUDGET_WARNING (генерируется также через gRPC в Go event-router)
quarantine_count > 5 в одном запуске → QUARANTINE_THRESHOLD (блокирует CI pipeline по умолчанию)
plan_staleness_detections_total rate > 2 в день → PLAN_STALENESS_ALERT (приложение меняется быстрее, чем цикл исследования)

**Вывод.**

(1) Отчёт о запуске (JSON + HTML): машиночитаемый JSON, потребляемый CI pipeline (код выхода 0/1 на основе настраиваемых порогов); HTML раздаётся по /ui/runs/{run_id} через Go report-service. Содержимое: результаты тестов на каждое действие (PASS/FAIL/SKIP/FLAKY/QUARANTINED), сводка исцелений (healed_count, auto_healed vs review_required, гистограмма распределения уверенности, diff исцелённых локаторов — старая vs новая стратегия на элемент), разбивка токенов и стоимости на модель на call_type, ссылки на Playwright trace viewer, сводка расхождения золотого состояния.

(2) Экспортированный код тестов Playwright (TypeScript .ts-файлы): стабильные локаторы из healed-locator store (confidence >= 0.85, не в карантине) + завершённые последовательности тестов, скомпилированные в формат Playwright test runner (describe/test/expect блоки). Выдаются в настроенный выходной каталог по завершении каждого запуска. Напрямую используются агентом qa-automation-engineer в качестве артефактов передачи. Не выдаются в режиме replay, если явно не запрошено через RunConfig.export_tests=true.

(3) Архивы трейсов Playwright (.zip): по одному на сегмент запуска (сегментация на каждой границе checkpoint). Просматриваются в Playwright Trace Viewer, поставляемом в комплекте с report-service. Удерживаются согласно политике. Ссылки на них в отчёте о запуске с прямыми ссылками.

(4) Регрессионные базовые линии (JSON-снимки дерева доступности): захватываются на каждом шаге checkpoint при первом стабильном исследовательском запуске. Хранятся как набор golden_state в persistence. Версионированы по (base_url, explore_run_id). Используются в последующих запусках для структурного diff. Экспортируемы как артефакт golden_states.tar.gz для контроля версий рядом с приложением.

(5) Аудиторский след исцелений (JSONL на запуск): каждый HealedLocatorRecord с полным контекстом: старый локатор, новый локатор, использованная стратегия, уверенность, reasoning_transcript_ref, требуется ли инспекция человека. Используется для проверки соответствия, отладки после инцидентов и ручного одобрения исцелений с review_required в очереди проверки исцелений по /ui/healing.

(6) Артефакт замороженного плана (JSON): сериализованная последовательность TestAction для воспроизведения. Версионирован по plan_hash. Выдаётся в artifact store и опционально по настроенному пути файла для контроля версий. CI pipelines могут закрепляться на конкретном plan_hash, гарантируя идентичное тестовое покрытие между запусками до намеренной регенерации плана.

**Путь к MVP.**

Phase 1 (Недели 1–2) — Позвоночник и провода: CLI agent-ctl на Go + заглушка orchestrator (gRPC-сервер, FSM жизненного цикла запуска с состояниями PENDING/RUNNING/DONE, watchdog). TypeScript playwright-mcp-server с 5 основными инструментами MCP: navigate_to, get_accessibility_tree, click_element, fill_input, take_screenshot. Скелет LangGraph мозга Python с 3 узлами (perceive, act, checkpoint) и БЕЗ LLM — жёстко закодированная последовательность TestAction для тестового приложения. Проверить сквозную работу: Go запускает Python через gRPC StartRun; Python вызывает TS через MCP stdio; TS управляет реальным браузером Playwright; артефакты возвращаются в Go. Критерий приёмки: контракт gRPC стабилен, вызовы инструментов MCP совершают полный оборот менее чем за 50ms на localhost, браузер Playwright открывается и выполняет навигацию.

Phase 2 (Недели 3–4) — Полный конвейер восприятия: Добавить оставшиеся инструменты MCP: get_dom_snapshot, resolve_locator, start_trace, stop_trace, scroll_to, wait_for_navigation, get_network_log. Построить snapshot-service (DOM-снимок с разглаживанием shadow DOM, оценка полноты). Построить set-of-marks-renderer (перечисление интерактивных элементов + canvas-оверлей). Модуль восприятия Python (парсер дерева a11y, нормализатор, порог полноты с триггером отката при < 0.30). Проверить: узел perceive корректно классифицирует три тестовых приложения — стандартное React SPA (высокая полнота), canvas-ориентированный дашборд (низкая полнота запускает marks) и приложение на кастомных элементах (средняя полнота). Порог полноты настраивается эмпирически.

Phase 3 (Недели 5–6) — Цикл plan и verify с LLM: Интеграция LLM (Sonnet 4.6 для экономии во время разработки; Opus 4.8 настраиваемо). Узел plan со схемой структурированного вывода TestAction. Узел verify со схемой вывода Verdict. Полный цикл perceive→plan→act→verify→checkpoint→perceive против тестового приложения. Go persistence-gateway с SQLite (таблицы healed_locators, sitemap_edges, run_history). Python memory-client с gRPC-стабами. Проверить: агент автономно навигирует по 5-страничному CRUD-приложению, записывает карту сайта, фиксирует результаты тестов, карта сайта сохраняется между двумя запусками.

Phase 4 (Недели 7–8) — Самовосстановление: locator-grinder со всеми тремя попытками re-grounding: ротация стратегий (попытка 1), рассуждение LLM по дереву доступности (попытка 2), визуальная привязка set-of-marks (попытка 3). Оценщик уверенности и порог персистентности. HumanGateEvent → Go event-router → webhook POST. Проверить: инъецировать 10 известных поломок локаторов в тестовое приложение (переименовать data-testid атрибуты, реструктурировать DOM, изменить текст элементов). Замерить: попытка 1 разрешает >= 6, попытка 2 разрешает >= 3 из оставшихся, попытка 3 разрешает >= 1 из оставшихся. Проверка калибровки уверенности: авто-принятые исцеления корректны >= 90% времени.

Phase 5 (Недели 9–10) — Детерминизм CI: Заморозка плана (сериализация после исследовательского запуска, вычисление plan_hash, хранение в persistence). Режим replay (обход узла plan, загрузка замороженной деки TestAction). Снимки золотого состояния, захватываемые при первом стабильном запуске. Структурный diff при последующих запусках. Обнаружение устаревания плана (порог N=3 сбоев исцеления). Карантин нестабильных тестов (3 последовательных провала). Принудительный бюджет токенов в узлах plan и heal. Проверить: то же тестовое приложение, тот же RunConfig → идентичный замороженный план → идентичные результаты тестов в 5 последовательных CI-запусках (GitLab CI pipeline). Ограничение бюджета корректно срабатывает на настроенном лимите 5K токенов.

Phase 6 (Недели 11–12) — Наблюдаемость и отчётность: Полная инструментация OpenTelemetry на всех трёх языках (OTLP → Jaeger). Генерация HTML-отчётов Go report-service и раздача трейсов Playwright. Эндпоинт Prometheus /metrics + дашборд Grafana (runs_total, healing_attempts_total, healing_confidence_histogram, llm_cost_usd_total, snapshot_completeness_histogram). JSONL транскриптов LLM + отслеживание стоимости на запуск. Генерация экспортированного кода тестов TypeScript (page-object стабы из стабильных локаторов). Проверить: полный запуск полностью наблюдаем в Jaeger со спанами решений LLM; экспортированные .ts-файлы чисто импортируются в проект Playwright test runner; стоимость запуска видна в Grafana.

Phase 7 (Неделя 13+) — Производственное закаление: Поддержка нескольких раннеров (PostgreSQL checkpointer + PostgreSQL persistence-gateway). Манифесты развёртывания K8s + ArgoCD Application CRD для домашней лаборатории. Инъекция состояния аутентификации (cookie/storage state из ссылки на секрет Vault в RunConfig). Векторное хранилище для семантического сходства элементов между страницами (память Phase 2). Нагрузочное тестирование на лимиты бюджета токенов CI. Правила Alertmanager развёрнуты. UI очереди проверки исцелений по /ui/healing. Ограничение скорости при загрузке артефактов в Go report-service.

**Ключевые риски:**

- Задержка транспорта MCP stdio: get_accessibility_tree вызывается каждый цикл perceive (потенциально каждые 2–5 секунд при быстром исследовательском запуске). Обороты stdio на localhost обычно 1–5ms, но сериализация снимка Playwright может занимать 50–200ms для сложных SPA. Если суммарная задержка превышает допустимое время цикла, меры по снижению риска: бенчмарк в Phase 1; при неприемлемости переключить транспорт MCP на HTTP+SSE (добавляет накладные расходы сокета, но разделяет сериализацию); альтернативно — объединить perceive+act в составные вызовы инструментов MCP, возвращающие и новый снимок, и результат действия за один оборот.
- Полнота дерева доступности на современных SPA: shadow DOM, слоты, компоненты на canvas (библиотеки графиков, rich text редакторы, дизайн-инструменты) и кастомные элементы с нестандартными паттернами взаимодействия могут давать completeness_score близкий к 0, переводя каждый цикл perceive в режим set-of-marks. Set-of-marks увеличивает стоимость токенов LLM в 3–5 раз за цикл (токены изображения vs токены текста). Меры по снижению риска: порог полноты настраивается на base_url; для заведомо canvas-ориентированных приложений явно задать marks-first режим в RunConfig; отслеживать метрику snapshot_completeness_histogram в Grafana для раннего обнаружения деградации.
- Хрупкость замороженного плана при быстро меняющихся UI: если тестируемое приложение деплоится еженедельно со структурными изменениями DOM, расхождение golden state будет срабатывать часто и запускать регенерацию плана. Несколько исследовательских запусков в неделю умножают стоимость LLM. Меры по снижению риска: настраиваемый порог расхождения на url_pattern (одни страницы стабильны, другие изменчивы); частичная инвалидация плана (повторно исследовать только те url_pattern, которые показали расхождение, а не весь sitemap); открыть закрепление plan_hash в RunConfig для команд, желающих закрепиться на конкретном релизе.
- Неверная калибровка уверенности LLM в heal-узле: Sonnet 4.6 может сообщать уверенность 0.88 для предложения исцеления, которое на самом деле неверно. Эмпирический дисконт 0.90 является заглушкой. При неверной калибровке авто-принятые исцеления (confidence >= 0.85) загрязнят healed-locator store плохими локаторами, вызвав тихие сбои тестов. Меры по снижению риска: в Phase 4 измерить фактическую точность исцеления vs заявленную уверенность на размеченном датасете поломок (инъецировать 50 известных поломок, измерить калибровку). Скорректировать коэффициент дисконта или снизить порог авто-принятия до >= 0.90 на основе эмпирических результатов. Долгосрочно: добавить пост-шаговую верификацию исцеления (выполнить действие ещё раз с исцелённым локатором перед сохранением) в качестве ворот подтверждения с высокой уверенностью.
- Узкое место human gate в CI pipeline: если большой процент попыток исцеления даёт confidence < 0.60, CI-запуски накапливают много результатов SKIPPED_HEALING_FAILURE. Команда, получившая CI-запуск с 30% SKIPPED, потеряет доверие к агенту. Меры по снижению риска: неблокирующий режим пропуска является режимом по умолчанию в CI (human gate носит информационный характер, не блокирует pipeline); порог quarantine_count (по умолчанию 5) — единственное жёсткое ограничение; очередь проверки исцелений по /ui/healing позволяет людям разбирать проблемы асинхронно без блокировки запусков. Оповещать команду при skip_rate > 10% — это сигнализирует о фундаментальном изменении приложения и необходимости нового исследовательского запуска.
- Зрелость интеграции LangGraph MCP-клиента: первоклассная поддержка инструментов MCP в LangGraph появилась относительно недавно (уточнить актуальный статус пакета langchain-mcp и стабильность транспорта stdio на дату реализации). Могут существовать шероховатости в распространении ошибок, строгости валидации схем инструментов или управлении подпроцессами. Меры по снижению риска: изолировать MCP-клиент в модулях memory-client + brain; написать тонкий адаптер, оборачивающий MCP-клиент, чтобы его можно было заменить на кастомный JSON-RPC 2.0-клиент (< 300 строк) в случае нестабильности официального пакета. Протокол JSON-RPC 2.0 достаточно прост, что делает резервную реализацию низкорискованной.
- Конкуренция записей SQLite при нескольких раннерах в K8s: при runner_count > 5 одновременные вызовы Python-мозгов к Go persistence-gateway создают давление на очередь записей. Go сериализует все записи (единственная горутина с очередью каналов), но при высоком параллелизме это становится узким местом пропускной способности. Меры по снижению риска: профилирование при runner_count=5 в Phase 7; если глубина очереди растёт > 100ms p99 задержки → мигрировать бэкенд persistence-gateway на PostgreSQL (пул соединений, параллельные записи); миграция — одно изменение конфига в gateway, изменения схемы не требуются.

**ADR:**

- ADR-001: MCP через stdio как протокол Python-to-TypeScript. Принято. Отклонённые альтернативы: (a) gRPC-сервис, оборачивающий Playwright — требует написания .proto-схемы, зеркалирующей API-поверхность Playwright, трансляционного слоя без архитектурной выгоды; (b) JSON-RPC через HTTP — добавляет сетевой сокет для вызова внутри одного пода, больше сложности, чем stdio, без выгоды в данном масштабе; (c) Python-библиотека Playwright непосредственно в процессе мозга — устраняет TypeScript как браузерный слой, нарушает полиглотное ограничение и ликвидирует нативную интеграцию трейсов Playwright в Node.js. Обоснование: MCP — нативный протокол для вызова инструментов LLM; LangGraph связывает инструменты MCP без адаптерного слоя; TS-слой семантически IS является сервером инструментов; рассогласование протоколов между агентным рассуждением и выполнением в браузере исчезает.
- ADR-002: Явная стейт-машина LangGraph как основа агента. Принято. Отклонённые альтернативы: (a) бесповоротный asyncio event loop — нет встроенного checkpointing, нет визуализации графа, отдельные узлы сложнее юнит-тестировать изолированно, возобновляемость требует кастомной реализации; (b) многоагентные фреймворки CrewAI или AutoGen — меньше контроля над циклом perception→heal, непрозрачное внутреннее состояние затрудняет отладку недетерминированного поведения исцеления, checkpointing не является первоклассной концепцией. Обоснование: checkpointer LangGraph даёт возобновляемость бесплатно; граф узлов/рёбер инспектируем и визуализируем; условная маршрутизация рёбер напрямую соответствует логике ветвления heal→ground vs heal→checkpoint.
- ADR-003: Дерево доступности как основная модальность восприятия. Принято. Отклонённые альтернативы: (a) только DOM (HTML-снимок в LLM) — нет семантической структуры, токено-затратно, ломается на shadow DOM, LLM должен выводить намерение из сырых HTML-тегов; (b) только скриншот (пиксельное зрение) — высокая стоимость токенов, нет структурной привязки для разрешения локаторов, LLM не может надёжно получать стабильные локаторы из пиксельных координат; (c) прямой CDP (Chrome DevTools Protocol) — мощный, но низкоуровневый, требует значительной работы по разбору и специфичен для браузера конкретного вендора. Обоснование: ARIA-роли и доступные имена спроектированы переживать DOM-мутации; они отражают семантическое намерение, а не реализацию; Playwright accessibility snapshot API возвращает структурированное JSON-дерево, укладывающееся в контекст LLM без предобработки.
- ADR-004: Set-of-marks как визуальная привязка-fallback в heal попытке 3. Принято. Отклонённые альтернативы: (a) клик по координатам (скриншот → LLM сообщает пиксельные координаты) — хрупко к изменениям viewport, device pixel ratio и частичному рендерингу страницы; (b) структурный XPath-fallback DOM — возврат к хрупким структурным селекторам, та же проблема, что у исходного неудачного локатора; (c) обрезка скриншота элемента (обрезать bounding box ожидаемого элемента) — требует знания bounding box, которого у нас нет при отсутствии элемента. Обоснование: нумерованные оверлеи на интерактивных элементах дают LLM пространственный контекст (в каком регионе страницы) плюс структурный контекст (индекс элемента отображается обратно в запись дерева доступности) без необходимости точности координат.
- ADR-005: Go persistence-gateway как единственный writer всего SQLite-хранилища. Принято. Отклонённые альтернативы: (a) каждый Python-раннер пишет в SQLite напрямую — WAL-режим помогает, но при > 5 параллельных писателях задержка очереди записей деградирует и появляются ошибки блокировок; (b) PostgreSQL с первого дня — излишняя инженерия для MVP на одном узле; добавляет операционную зависимость до того, как система доказана; (c) Redis для хранилища локаторов — неподходящая модель данных (Redis — key-value, не реляционная), теряется возможность запросов по url_pattern + semantic_id эффективно, добавляет ещё один сервер. Обоснование: единственный Go-процесс сериализует все записи через внутреннюю очередь каналов; переключение на PostgreSQL — изменение бэкенда в одном Go-файле без миграции схемы.
- ADR-006: Explore-once-then-replay для детерминизма CI. Принято. Отклонённые альтернативы: (a) регенерировать план из LLM при каждом CI-запуске — дорого (каждый запуск платит полную стоимость исследования Opus 4.8), недетерминированно (Temperature=0 помогает, но контекст промпта варьируется) и медленно; (b) запись/воспроизведение на сетевом уровне (HAR-файлы) — хрупко к любому изменению URL бэкенда или ответа, не тестирует семантику UI; (c) генерация тестов на основе свойств (выводить тесты из API-спецификации) — теряется исследовательское покрытие, являющееся отличительной возможностью агента; (d) только сравнение скриншотов (пиксельный diff) — высокий процент ложных срабатываний на динамическом контенте (временные метки, аватары, реклама). Обоснование: замороженный план даёт детерминизм; исцеление добавляет устойчивость к дрейфу UI без повторного исследования; структурный diff относительно золотых снимков дерева доступности рано обнаруживает намеренные изменения приложения.
- ADR-007: Opus 4.8 для explore/plan, Sonnet 4.6 для verify/heal. Принято. Отклонённые альтернативы: (a) Sonnet 4.6 для всех вызовов LLM — качество плана измеримо ниже при сложных многошаговых исследовательских решениях (проверить в Phase 3 с A/B-сравнением покрытия sitemap на тестовом приложении); (b) Opus 4.8 для всех вызовов LLM — стоимость в 5–8 раз выше за запуск при незначительном приросте качества verify (бинарный PASS/FAIL с явными свидетельствами) и heal попытка 2 (структурированное рассуждение по дереву, вполне в пределах возможностей Sonnet); (c) локальная модель (класса Llama, через Ollama) — бюджета GPU домашней лаборатории недостаточно для надёжного рассуждения уровня Opus; рассуждение по дереву доступности требует сильного следования инструкциям и соответствия структурированному выводу. Обоснование: исследовательские решения имеют наибольшую отдачу от качества модели; verify и heal — более узкие, лучше ограниченные задачи, где качество Sonnet приемлемо при значительно меньшей стоимости. Пересмотреть разбивку, если Sonnet 4.x сблизится с Opus в области сложного планирования.

---


### `reliability-ci` — TrustFirst — Автономный UI-агент надёжности и CI-детерминизма

*Lens: Reliability / CI-determinism first*

**Философия.** Ключевая ставка: недетерминированный LLM-исследователь становится надёжным участником CI через жёсткое разделение на explore-mode (LLM-driven, под контролем человека, однократный) и replay-mode (план заморожен, без LLM на happy path, детерминированный). Замороженный план — это артефакт, который верифицирует CI, а не живые рассуждения LLM. Доверие строится на четырёх столпах: неизменяемый аудиторский след восстановления, который присваивает каждому исправлению локатора оценку уверенности и требует участия человека ниже порогового значения; жёсткие бюджеты на токены/стоимость, применяемые на уровне управляющего plane Go до того, как Python-мозг сможет их израсходовать; golden a11y-tree снапшоты, превращающие DOM-регрессии в структурный diff, а не интуитивное ощущение; и реестр карантина флакающих тестов, предотвращающий отравление CI-сигнала нестабильным DOM-элементом для всего набора тестов.

**Компоненты (13):**

| Component | Язык | Ответственность |
|-----------|------|-----------------|
| agentctl | Go | CLI-точка входа для всех взаимодействий человека и CI: run (режимы explore\|replay\|ci), gate (approve/skip/abort человеческих gate), report (отрисовка артефактов), baseline (обновление golden снапшотов), healing (audit/calibrate), plan (list/inspect/hash-verify). Испускает структурированные exit codes для CI (0=pass, 1=step-fail, 2=regression, 3=budget-exhausted). |
| orchestrator | Go | gRPC-сервер (двунаправленный стриминг). Управляет FSM жизненного цикла запуска (CREATED→RUNNING→PAUSED→COMPLETED\|FAILED\|ABORTED). Предоставляет RPC: RunControl, BudgetService, EventStream, GateService. Применение бюджета — жёсткий потолок: отклоняет вызовы deduct от Python при достижении лимита, вне зависимости от показаний локального счётчика Python. Испускает метрики Prometheus на /metrics. |
| store-gateway | Go | Единственный писатель в базу данных SQLite WAL. Всё кросс-сессионное состояние проходит через него: plan_store, locator_store, sitemap, golden_snapshots, healing_audit, flake_registry, run_index. Предоставляет gRPC StoreService (upsert/get/query). Статус единственного писателя — это контракт отсутствия конкуренции за WAL: ни Python, ни TS-процессы не обращаются к SQLite напрямую. |
| report-api | Go | HTTP API (порт 8080) для раздачи артефактов запуска: отчёты в форматах JSON и HTML, загрузка JSONL аудита восстановления, прокси трассировки Playwright, загрузка транскрипта LLM, JSON графа sitemap. Читает из SQLite через store-gateway. Также обслуживает EventStream в реальном времени как SSE для потокового просмотра логов CI. |
| brain | Python | Конечный автомат LangGraph. 8 узлов: perceive, ground, plan, act, observe, verify, heal, checkpoint. Подключается к оркестратору Go через gRPC (клиенты RunControl, BudgetService, StoreService). Запускает подпроцесс playwright-executor и управляет им через MCP stdio. Владеет всеми вызовами LLM (Claude Opus 4.8 для узла plan; Sonnet 4.6 для узла heal). Управляет checkpointer LangGraph (SqliteSaver для локальной среды/dev, AsyncPostgresSaver для prod) для поддержки pause/resume. |
| healing-engine | Python | Логика повторного определения локатора, вызываемая исключительно из узла heal LangGraph. Реализует иерархию из 3 попыток: (1) ротация стратегий по альтернативам locator_store, (2) рассуждения LLM над текстом a11y tree, (3) визуальное переопределение LLM по скриншоту с set-of-marks. Вычисляет оценку уверенности, применяет пороговое значение gate, испускает прото HealAttempt в store-gateway для сохранения в аудите. |
| plan-manager | Python | Заморозка/разморозка артефактов плана. При заморозке: сериализует упорядоченную последовательность действий с хешами a11y-снапшотов до и после, а также альтернативами локаторов; вычисляет plan_hash (sha256 канонического JSON); отправляет в store-gateway StoreService.UpsertPlan. При загрузке для replay: получает план по plan_id, верифицирует plan_hash против сохранённого значения перед первым шагом — прерывает выполнение при обнаружении вмешательства. |
| llm-client | Python | Тонкая обёртка Claude API с учётом бюджета. Перед каждым вызовом: gRPC BudgetService.CheckAndDeduct(tokens_estimate, cost_estimate) — при отказе выбрасывает BudgetExhaustedError, который перенаправляет brain в checkpoint, а затем в report. После вызова: фиксирует фактические токены/стоимость через BudgetService.RecordActual. Логирует каждый prompt+response в llm_transcript.jsonl (только дозапись, один файл на run_id). |
| playwright-executor | TypeScript | MCP-сервер, работающий как дочерний подпроцесс brain. Предоставляет 12 инструментов через stdio MCP: navigate, click, fill, hover, select, press_key, scroll, wait_for_selector, snapshot_a11y (возвращает полное ARIA-дерево в виде JSON), screenshot_annotated (внедряет JS-оверлей с set-of-marks, возвращает base64 PNG с целочисленными номерами меток), evaluate_js и close_context. Управляет жизненным циклом контекста браузера Playwright и записью трассировки на каждый run_id. |
| snapshot-service | TypeScript | Внутренний модуль playwright-executor. snapshot_a11y вызывает page.accessibility.snapshot({interestingOnly: false}) и сериализует в нормализованную JSON-структуру (role, name, description, children). screenshot_annotated внедряет оверлейный скрипт, назначающий последовательные целочисленные метки (ограничивающий прямоугольник + числовой ярлык) всем интерактивным элементам, делает снимок экрана, удаляет оверлей и возвращает аннотированное изображение. Также вычисляет dom_hash: sha256 от document.body.innerHTML с удалёнными динамическими атрибутами data-reactid/data-v-*. |
| trace-manager | TypeScript | Жизненный цикл трассировки Playwright: запускает запись (screenshots: true, snapshots: true, sources: true) при старте запуска, останавливает и экспортирует trace.zip в конце в /runs/{run_id}/playwright-trace.zip. Отправляет путь к файлу трассировки в Go report-api через HTTP POST /artifacts/{run_id}/trace. |
| proto | shared | Определения protobuf3 для gRPC-границы Go↔Python. Сервисы: RunControl (Start, Stop, Pause, Resume, GetStatus), BudgetService (CheckAndDeduct, RecordActual, GetBudget), StoreService (UpsertPlan, GetPlan, UpsertLocator, GetLocators, UpsertSitemap, AppendHealingAudit, GetFlakeRegistry, UpdateFlakeCount), GateService (ListPending, Resolve), EventStream (Subscribe — server-streaming RunEvent). Компилируется для Go и Python (buf.build или protoc). |
| mcp-schema | shared | Определения JSON Schema для 12 MCP-инструментов и 2 MCP-ресурсов, предоставляемых playwright-executor. Схема инструментов определяет формы входных/выходных данных, валидируемые на границе MCP stdio. Ресурсы: current_page (live a11y tree) и trace_status (состояние записи). Версия фиксирована, чтобы Python brain и TS executor оставались синхронизированными по сигнатурам инструментов при обновлениях Playwright. |

**Границы (полиглотные контракты).**

Go ↔ Python — gRPC proto3 двунаправленный стриминг. Оркестратор Go является gRPC-сервером; Python brain — клиентом. Обоснование выбора протокола: библиотека grpc в Go производственного качества; proto3 обеспечивает типизированную схему через языковую границу, что позволяет обнаруживать расхождения на этапе компиляции (сгенерированные protoc стабы на обеих сторонах); двунаправленный стриминг позволяет реализовать три вещи, которые REST не может обеспечить чисто: (1) Go передаёт события исчерпания бюджета в Python в ходе выполнения без опроса, (2) Python стримит живые события ActionRecord в Go для SSE в реальном времени через report-api, (3) разрешение human gate передаётся от Go в Python как server push (GateService.Subscribe). Транспорт: локальный Unix domain socket в одноузловом режиме (меньше накладных расходов системных вызовов, чем TCP loopback, без нестабильного привязывания портов в CI-контейнерах); настраивается на TCP для многоузлового режима. Отклонённая альтернатива: REST/HTTP — нет стриминга без сложности SSE, типобезопасность нагрузки требует отдельной валидации JSON Schema, нет чистой передачи отмены операций.

Python ↔ TypeScript — MCP через stdio (фреймирование JSON-RPC 2.0). Python brain запускает playwright-executor как дочерний подпроцесс через subprocess.Popen с каналами stdin/stdout. Протокол MCP уже имеет JSON-RPC-подобную семантику tools/resources/prompts, которая напрямую отображается на паттерн узла вызова инструментов LangGraph — brain вызывает инструменты MCP точно так же, как любой инструмент LangGraph, поэтому адаптерный слой не нужен. Stdio исключает выделение портов (критично в CI, где конфликты портов вызывают нестабильность), а жизненный цикл подпроцесса принадлежит Python-процессу (SIGTERM распространяется при краше brain). Heartbeat: brain отправляет MCP ping каждые 30 секунд; при отсутствии pong в течение 5 секунд выполняет один перезапуск подпроцесса перед тем, как пометить шаг как неуспешный. Отклонённая альтернатива: gRPC — TypeScript grpc-node добавляет сложность сборки с генерируемым кодом, а TypeScript-экосистема для gRPC значительно менее зрелая, чем Python; результирующий API был бы идентичен по форме тому, что MCP предоставляет нативно.

TypeScript → Go — HTTP REST (fire-and-forget передача артефактов). По завершении запуска trace-manager выполняет POST с путём к файлу trace.zip в Go report-api POST /artifacts/{run_id}/trace. Это единственное взаимодействие TS→Go, и оно происходит после запуска, поэтому задержка не имеет значения. Файлы трассировки Playwright записываются в общий смонтированный том (/runs/{run_id}/); Go читает их напрямую по пути. Протокол реального времени здесь не нужен. Отклонённая альтернатива: gRPC — добавление третьего gRPC-клиента (TS) для единственного уведомления об артефакте — непропорциональная сложность.

Контракт изоляции отказов: при краше playwright-executor brain перехватывает разрыв MCP (poll подпроцесса возвращает не-None), пытается один раз перезапустить, затем завершает текущий шаг с ошибкой и вызывает узел heal (который повторит действие после переподключения). При краше brain оркестратор Go обнаруживает завершение gRPC-стрима, сохраняет состояние запуска как FAILED, сохраняет checkpoint LangGraph на диске, чтобы запуск можно было возобновить с помощью agentctl run --resume {run_id}. При перезапуске оркестратора Go gRPC-клиент Python brain переподключается с экспоненциальной задержкой (начальная 100ms, максимум 30s, с джиттером). Данные не теряются, поскольку store-gateway является авторитетным писателем, а SQLite устойчива к сбоям.

**Цикл агента.**

SHARED STATE OBJECT (AgentState TypedDict):
  session_id: str
  run_id: str
  run_mode: Literal["explore", "replay", "ci"]
  # Page context
  target_url: str
  current_url: str
  current_a11y_tree: dict            # normalized ARIA JSON from snapshot-service
  current_dom_hash: str              # sha256 of stripped innerHTML
  current_screenshot_b64: str        # annotated with set-of-marks integers
  a11y_completeness_ratio: float     # ratio of interactive elements with ARIA roles; < 0.30 triggers visual-grounding mode
  # Plan
  frozen_plan: Plan | None           # None during explore; populated after freeze node
  plan_step_index: int
  action_history: list[ActionRecord] # each: step_id, action, locator_used, outcome, pre_dom_hash, post_dom_hash, tokens_used
  # Exploration queue
  discovered_pages: dict[str, PageNode]
  pending_pages: deque[str]
  # Healing
  last_failed_selector: str | None
  last_failed_description: str | None
  healing_attempts: list[HealAttempt]
  current_healing_confidence: float
  # Budget
  tokens_used: int
  tokens_budget: int
  cost_usd: float
  cost_budget_usd: float
  budget_warning_emitted: bool
  # Gate
  human_gate_pending: bool
  human_gate_reason: str | None
  human_gate_decision: Literal["approve","skip","abort"] | None
  human_gate_resolved_locator: str | None
  # Artifacts
  run_dir: str                       # /runs/{run_id}/
  trace_active: bool
  artifacts: list[ArtifactRef]

NODE GRAPH (8 nodes):

perceive — Точка входа для каждого цикла. Вызывает MCP snapshot_a11y + screenshot_annotated. Вычисляет dom_hash. Обновляет current_a11y_tree, current_screenshot_b64, a11y_completeness_ratio, current_url. В режиме replay: также загружает frozen_plan[plan_step_index] для подготовки следующего действия. Всегда переходит → ground.

ground — Страж повторного определения. Проверяет: есть ли current_url в discovered_pages? Изменился ли dom_hash с последнего посещения? Если новая страница: извлекает интерактивные элементы, добавляет в sitemap, добавляет дочерние ссылки в pending_pages. Если dom_hash не изменился по сравнению с post_dom_hash последнего шага: помечает как возможное действие без эффекта (испускает WARNING в EventStream). Переходит → plan.

plan — Узел принятия решений. В режиме EXPLORE: формирует LLM-запрос из текущего a11y tree + action_history (последние 10) + budget_remaining + охвата sitemap + pending_pages. Модель: Claude Opus 4.8. Вывод: next_action (ActionSpec: type, target_description, locator_hint, assertion) или сигнал "exploration_complete". Предварительная проверка бюджета через BudgetService.CheckAndDeduct перед вызовом; перенаправляет в checkpoint при исчерпании бюджета. В режиме REPLAY/CI: читает frozen_plan.steps[plan_step_index] напрямую — БЕЗ вызова LLM. Переходит → act (при наличии действия) или → checkpoint (при exploration_complete или исчерпании плана).

act — Выполняет выбранное действие через вызов MCP-инструмента. Отображает ActionSpec.type на одно из: click, fill, hover, press_key, scroll, navigate, select. Записывает pre_action_dom_hash из текущего состояния. При возврате ошибки MCP-инструментом (locator_not_found, timeout, element_not_interactable): устанавливает last_failed_selector + last_failed_description, переходит → heal. При успехе: переходит → observe.

observe — Снова вызывает MCP snapshot_a11y + screenshot_annotated (состояние после действия). Обновляет current_a11y_tree, current_dom_hash, current_screenshot_b64. Обнаруживает навигацию (изменение current_url). Добавляет ActionRecord в action_history с фактическим количеством токенов. Переходит → verify.

verify — Утверждает ожидаемое состояние после действия. В режиме EXPLORE: мягкое утверждение LLM ("привело ли действие к значимому изменению состояния?") — вызов Sonnet 4.6, структурированный вывод (passed: bool, notes: str). В режиме REPLAY/CI: структурный diff текущего a11y tree с golden_snapshot[plan_id][step_id]. Алгоритм diff: рекурсивный JSON-diff дерева с подсчётом добавленных/удалённых/изменённых узлов. Если diff_ratio < 0.05: успех → увеличить plan_step_index → переходит → perceive. Если diff_ratio 0.05–0.25: успех с REGRESSION_WARN (пометка в отчёте, продолжение). Если diff_ratio > 0.25 или отсутствует требуемый элемент: неудача → переходит → heal (обработка как ошибки локатора для целевого утверждения).

heal — Узел самовосстановления. Делегирует в модуль healing-engine (см. поле selfHealing для полного алгоритма). Устанавливает current_healing_confidence из результата engine. Если confidence >= 0.85: автоматически сохраняет исправленный локатор в store-gateway StoreService.UpsertLocator, обновляет действие в состоянии, перенаправляет → act (повтор с исправленным локатором, максимум 2 повтора). Если 0.60–0.84: выполняет действие с исправленным локатором, но устанавливает healing_flagged=True в ActionRecord для последующей проверки человеком; переходит → act. Если < 0.60: устанавливает human_gate_pending=True, human_gate_reason="low_confidence_heal", вызывает GateService.CreateGate оркестратора, переходит → checkpoint (асинхронная пауза).

checkpoint — Сохранение checkpoint LangGraph. Вызывает checkpointer.aput(config, state, metadata) — сохраняет полный AgentState. Также вызывает RunControl.Checkpoint() оркестратора для записи checkpoint_id в SQLite (для возобновления). При human_gate_pending: вызывает RunControl.Pause(gate_id) и входит в цикл опроса (GateService.WaitForResolution с интервалом опроса 30s). При разрешении: устанавливает human_gate_decision + human_gate_resolved_locator, перенаправляет → heal (при approve) или → plan (при skip) или вызывает AbortError (при abort). При отсутствии ожидающего gate и run_mode==explore, и pending_pages пусто, и план не заморожен: переходит → plan (ветка заморозки через сигнал exploration_complete). Иначе: переходит → perceive (следующая страница/шаг).

report — Терминальный узел. Останавливает трассировку Playwright через MCP close_context (trace-manager экспортирует trace.zip). Сериализует JSON замороженного плана, если ещё не сохранён. Формирует прото RunReport со статистикой прохождения/неудачи/исправления на уровне шагов, разбивкой стоимости, количеством нестабильных шагов, количеством ожидающих human_gate. Отправляет в store-gateway для SQLite + вызывает POST /runs/{run_id}/finalize report-api. Переходит → END.

EDGES (conditional routing via LangGraph add_conditional_edges):
perceive → ground (always)
ground → plan (always)
plan → act | checkpoint (budget_exhausted) | report (exploration_complete or replay_done)
act → observe (success) | heal (mcp_error)
observe → verify (always)
verify → perceive (pass, plan_step_index++) | heal (fail)
heal → act (confidence>=0.60, retries<2) | checkpoint (confidence<0.60 OR retries>=2)
checkpoint → perceive (normal resume) | heal (gate approved) | plan (gate skipped) | END (gate aborted)
report → END

**Самовосстановление.**

Шаг 1 — Захват отказа: когда узел act получает ошибку MCP (locator_not_found, timeout, element_not_interactable или detached frame), он записывает в AgentState: last_failed_selector (точная строка селектора, которая не сработала), last_failed_description (семантическое описание из шага плана, например, "primary submit button in the checkout form"), failed_action_type (click/fill/и т.д.) и pre_failure_dom_hash. Переходит в heal. Движок восстановления получает этот снимок как входной контракт.

Шаг 2 — Иерархия стратегий (без LLM, сначала быстрый путь): healing-engine пробует альтернативные локаторы в строгом ранжированном порядке перед вызовом любого LLM. locator_store в SQLite предварительно загружается в AgentState при старте сессии как dict с ключом (page_url, element_description). Стратегия L1: запрос locator_store по (current_url, last_failed_description) — если найден и status=active, проверить этот селектор через MCP-зонд wait_for_selector (таймаут 100ms). L2: извлечь ARIA role + accessible name из current_a11y_tree, соответствующие семантическому описанию (сходство строк >= 0.85) — сформировать Playwright role-selector (например, role=button[name="Submit Order"]). L3: поиск в a11y tree узлов, у которых accessible name содержит ключевые токены из last_failed_description. L4: селектор по текстовому содержимому видимых текстовых узлов. Каждая попытка L1-L4 запускает MCP-зонд wait_for_selector для валидации перед фиксацией. При первом успешном зонде → переходим к оценке уверенности (Шаг 4). При неудаче всех четырёх → переходим к повторному определению с помощью LLM (Шаг 3).

Шаг 3 — Повторное определение LLM (два прохода): Проход A — рассуждение по a11y tree (Sonnet 4.6, только текст, дешевле): структура запроса: (1) "You were attempting to {action_type} the element described as: {last_failed_description}. Here is the current ARIA accessibility tree: {serialized_a11y_tree}. Identify the element that best matches. Return: aria_path (dot-notation path through tree), playwright_selector (role or aria-* selector), confidence (0.0–1.0), reasoning (max 2 sentences)." Вывод парсится как структурированный JSON через tool_use. Если a11y_completeness_ratio < 0.30 (страница с обильным canvas, пользовательские веб-компоненты, изоляция iframe): переходим к Проходу B. Проход B — визуальное повторное определение (Sonnet 4.6 с vision): использует current_screenshot_b64 (аннотированный с set-of-marks). Запрос: "The circled/numbered marks in this screenshot indicate interactive elements. You were trying to {action_type} the element described as: {last_failed_description}. Which mark number corresponds to the target? Return: mark_number (int), css_selector_suggestion (string), confidence (0.0–1.0), reasoning." healing-engine сопоставляет mark_number с DOM-элементом через вызов evaluate JS (скрипт оверлея сохранял маппинги метка→элемент в window.__somarks). Извлекает data-testid, aria-label или запасной CSS-путь элемента. Берёт max(confidence_A, confidence_B) как итоговую оценку уверенности.

Шаг 4 — Gate уверенности: confidence >= 0.85: автоматическое восстановление. Сохранить новый селектор в locator_store (gRPC-вызов UpsertLocator со status=active, source=auto_heal). Обновить действие в AgentState исправленным селектором. Увеличить heal_count. Перейти в act (повтор). Confidence 0.60–0.84: попытка с пометкой. Использовать исправленный селектор, установить healing_flagged=True в ActionRecord (отображается жёлтым в HTML-отчёте). Не сохранять в locator_store до проверки человеком после запуска. Перейти в act (повтор). Confidence < 0.60 ИЛИ количество повторов >= 2: отказ продолжать. Установить human_gate_pending=True, human_gate_reason="healing_failed: confidence={score}, selector={best_candidate}". Это вызывает протокол human gate.

Шаг 5 — Протокол human gate (асинхронный, безопасный для CI): healing-engine испускает HealingGateEvent в GateService.CreateGate(run_id, step_id, context_blob) оркестратора. Оркестратор записывает gate в таблицу gate_queue SQLite и транслирует через EventStream. agentctl gate list показывает: run_id, step_id, failed_selector, best_candidate, confidence, screenshot_url (ссылку на предпросмотр в base64). Человек выполняет одно из: agentctl gate approve {run_id} --locator "button[data-testid=submit]" (предоставляет правильный селектор напрямую), agentctl gate skip {run_id} (шаг пропускается, помечается как needs_human_review в отчёте), agentctl gate abort {run_id} (выполнение прерывается, частичный отчёт сохраняется). Таймаут CI (настраиваемый, по умолчанию 30 мин): GateService автоматически разрешает как skip. Одобренные локаторы сохраняются в locator_store со status=human_verified — высший приоритет при следующем поиске L1.

Шаг 6 — Карантин нестабильных тестов (защита CI): step_id отслеживается в таблице flake_registry (plan_id, step_id, fail_count, last_5_results, quarantine_status). После каждого CI replay-запуска вызывается UpdateFlakeCount для каждого неуспешного шага. Если fail_count >= 3 в последних 5 запусках БЕЗ изменения SHA коммита AUT: шагу присваивается quarantine_status=quarantined. Карантинные шаги: всё ещё выполняются, но их неудача НЕ влияет на exit code CI. Отображаются в отчёте в отдельной секции "Flaky (quarantined)". Еженедельная проверка человеком через agentctl healing report --flaky. Снятие карантина: agentctl gate clear-flake {plan_id} {step_id} (решение человека) или автоснятие после 3 последовательных прохождений.

Шаг 7 — Схема сохранения: таблица locator_store: {id UUID, plan_id, page_url, original_selector TEXT, healed_selector TEXT, element_description TEXT, aria_path TEXT, confidence REAL, heal_count INT, last_healed_at TIMESTAMP, status TEXT CHECK(status IN ('active','flagged','human_verified','deprecated')), human_verified BOOL}. Таблица healing_audit (только дозапись, без UPDATE/DELETE): {id UUID, timestamp, run_id, step_id, original_selector, strategy_used (L1|L2|L3|L4|llm_a11y|llm_visual), healed_selector, confidence, outcome (success|flagged|human_gate|flake_quarantined), llm_tokens_used INT, duration_ms INT}. Обе таблицы принадлежат исключительно store-gateway (контракт единственного писателя).

**Детерминизм.**

Основной принцип: недетерминизм LLM допустим ровно один раз — в режиме explore, под контролем человека. CI никогда не вызывает режим explore. Вот полный механизм:

РЕЖИМ EXPLORE (недетерминированный, однократный для каждой функциональной области): Запускается против выделенной среды staging с известным SHA коммита AUT, зафиксированным при старте запуска. Полные рассуждения LLM при каждом решении в узле plan. Производит артефакт frozen_plan: {plan_id: UUID, plan_hash: sha256(canonical_json), created_at: ISO8601, target_url: str, aut_version: str (git SHA AUT), exploration_seed: int, steps: [упорядоченный список PlanStep]}. Каждый PlanStep: {step_id: str, action: ActionSpec, element_description: str, locator_primary: str, locator_alternatives: [ранжированные L1..L4], pre_action_a11y_hash: str, post_action_a11y_hash: str, assertion: AssertionSpec}. Golden a11y-снапшоты (полные JSON-деревья) хранятся в таблице SQLite golden_snapshots с ключом (plan_id, step_id). plan_hash — sha256 канонического JSON массива steps (ключи отсортированы, числа с плавающей точкой нормализованы до 6 десятичных знаков). Хранится в таблице plan_store и также в файле frozen_plan.json в /runs/{run_id}/.

ЦЕЛОСТНОСТЬ PLAN HASH: При старте replay plan-manager вызывает StoreService.GetPlan(plan_id) → сравнивает сохранённый plan_hash со свежевычисленным хешем полученных шагов. Несоответствие хеша → немедленное прерывание с exit code 3 и ошибкой "plan integrity violation: stored={stored_hash}, computed={computed_hash}". Планы неизменяемы после заморозки. Новый запуск explore производит новый plan_id. Нет механизма редактирования замороженного плана на месте — единственный путь обновления — новый запуск explore.

РЕЖИМ REPLAY (детерминированный, точка входа для CI): agentctl run --mode ci --plan-id {id} --aut-version $(git rev-parse HEAD). Узел plan читает frozen_plan.steps[plan_step_index] — БЕЗ вызова LLM. act выполняет основной локатор напрямую. verify сравнивает текущее a11y tree с golden снапшотом (структурный JSON diff). LLM вызывается ТОЛЬКО при двух условиях: (a) act завершается неудачей с locator_not_found (путь восстановления, Шаг 3 selfHealing) или (b) verify обнаруживает diff_ratio > 0.25 (путь регрессии, восстановление вызывается для расходящегося элемента). На happy path (все локаторы валидны, DOM совпадает с golden): ноль вызовов LLM, нулевая стоимость API, детерминированное время выполнения.

ОБНАРУЖЕНИЕ ДРЕЙФА ВЕРСИИ AUT: plan_store.aut_version сравнивается с флагом --aut-version при старте replay. Если SHA различаются: испускается "AUT version mismatch: plan built on {stored}, current {current}" как WARNING (не прерывание). Включает режим "tolerant CI", где агент работает с включённым восстановлением, но помечает все исправленные шаги для проверки человеком. Настраиваемая политика: --on-aut-mismatch=[warn|heal|abort].

EXPLORATION SEED: AgentState.exploration_seed (целое число, по умолчанию 0, настраивается через флаг --seed) управляет: порядком посещения страниц (BFS-обход использует seed для детерминированного перемешивания pending_pages с одинаковым приоритетом) и разрешением конфликтов при выборе действий в узле plan (когда LLM предлагает несколько равно-валидных следующих действий, seed определяет выбор). Один и тот же seed на той же кодовой базе производит структурно похожие (но не побитово идентичные) запуски explore, что достаточно для отладки регрессий explore без ограничения гибкости LLM.

ПРОТОКОЛ ОБНОВЛЕНИЯ GOLDEN SNAPSHOT: Базовые линии никогда не обновляются автоматически во время CI-запусков. Обновление требует явной команды: agentctl baseline update --plan-id {id} --aut-version {sha}. Это запускает replay, принимает все текущие a11y trees как новые golden-эталоны и производит новый plan_hash. Старый план остаётся в plan_store как исторический артефакт. Это делает сценарий "CI-тесты самостоятельно изменили собственные базовые линии" невозможным.

КАРАНТИН НЕСТАБИЛЬНЫХ ТЕСТОВ (защита CI-сигнала): После каждого CI-запуска результат каждого шага (pass/fail/healed) записывается в flake_registry через StoreService.UpdateFlakeCount. Определение нестабильности использует скользящее окно из последних 5 CI-запусков для того же plan_id. Шаги с fail_count >= 3 в окне получают quarantine_status=quarantined. Карантинные шаги: выполняются в обычном режиме, неудачи НЕ устанавливают exit code 1 или 2, отображаются в секции отчёта "Quarantined (flaky)". Exit codes CI: 0=все неквантированные шаги прошли, 1=неудачи шагов без регрессии golden diff, 2=обнаружена регрессия golden diff (diff_ratio > 0.25 на неквантированном шаге), 3=нарушение целостности плана или исчерпание бюджета.

ИНТЕГРАЦИЯ С CI-ПАЙПЛАЙНОМ: Запуск replay полностью автономен: единый бинарный файл (agentctl) вызывает оркестратор Go (в-процессе в режиме single-binary для простоты CI), который запускает подпроцесс Python brain (brain --run-id {id} --mode ci), который запускает подпроцесс playwright-executor. Внешние сервисы не требуются, кроме файла SQLite и URL AUT. Опционально: отправка отчёта запуска в report-api для централизованной истории.

**Память.**

КРАТКОСРОЧНАЯ (эпизодическая, в рамках сессии, принадлежит LangGraph):
LangGraph встроенный checkpointer управляет AgentState через все переходы узлов в рамках запуска. Бэкенд: SqliteSaver для локальной среды/dev (файл в /runs/{run_id}/langgraph.db, отдельно от основного SQLite store-gateway во избежание конкуренции при записи), AsyncPostgresSaver для production K3s-деплоя. Thread ID = run_id, что позволяет параллельным запускам иметь независимое состояние. Checkpoint сохраняется на каждой границе узла — именно это обеспечивает работу pause/resume и восстановления после сбоя. При краше brain: оркестратор Go видит завершение gRPC-стрима, помечает запуск как FAILED в run_index. Человек выполняет agentctl run --resume {run_id}: brain повторно подключается к checkpoint и воспроизводит с последней сохранённой границы узла. Эпизодический буфер: AgentState.action_history ограничен 30 последними ActionRecord. Более старые записи суммируются (генерируется LLM, хранится в AgentState.history_summary) для ограничения токенного контекста. Сводка регенерируется каждые 30 действий (настраивается). Внутрисессионный кэш валидности локаторов: dict[selector, ProbeResult] в AgentState, заполняемый в ходе ротации стратегий L1-L4 во избежание повторного зондирования заведомо нерабочих селекторов в рамках одной сессии.

ДОЛГОСРОЧНАЯ (межсессионная, принадлежит Go store-gateway, SQLite WAL):
plan_store: {plan_id, plan_hash, target_url, aut_version, exploration_seed, created_at, step_count, status(active|archived), file_path}. Замороженный план JSON хранится как файл, таблица содержит метаданные + хеш целостности.
locator_store: {id, plan_id, page_url, original_selector, healed_selector, element_description, aria_path, confidence, heal_count, last_healed_at, status, human_verified}. Предзагружается в AgentState.locator_cache при старте сессии (с фильтрацией по целевому домену).
sitemap: {url, page_type, first_seen, last_seen, visit_count, interactive_element_count, a11y_completeness_ratio, in_pending_queue}. Загружается в AgentState.discovered_pages при старте сессии. Обновляется узлом ground.
golden_snapshots: {plan_id, step_id, snapshot_type(a11y|screenshot), content_hash, file_path, created_at, superseded_by}. Содержимое хранится как файлы в /runs/baselines/{plan_id}/.
healing_audit: аудиторский журнал только для дозаписи (без UPDATE, без DELETE). Схема в selfHealing шаг 7.
flake_registry: {plan_id, step_id, fail_count, last_5_results JSON, quarantine_status, last_updated}.
run_index: {run_id, plan_id, mode, aut_version, status, started_at, completed_at, tokens_used, cost_usd, steps_pass, steps_fail, steps_healed, steps_quarantined}.
page_object_cache: {url_pattern (regex), generated_ts_code, code_hash, created_at, used_count}. Сгенерированные TypeScript page object классы кэшируются для повторного использования qa-automation-engineer.

Обоснование хранилища: SQLite WAL mode для всего долгосрочного состояния. WAL позволяет параллельным читателям (report-api, agentctl) работать при том, что store-gateway является единственным писателем — это и есть весь контракт конкуренции. Нулевые операционные накладные расходы для домашней лаборатории и одноузлового CI. Простое резервное копирование: единственная команда cp. Достаточная скорость записи: < 200 строк/секунду в пиковой нагрузке (один шаг раз в несколько секунд). Триггер масштабирования: при > 10 параллельных запусков или скорости записи > 1000 строк/сек → мигрировать store-gateway на PostgreSQL (схема идентична, замена драйвера в Go). PostgreSQL отклонён для v1: операционная нагрузка превышает пользу при текущем масштабе.

**Наблюдаемость.**

РАСПРЕДЕЛЁННАЯ ТРАССИРОВКА (OpenTelemetry):
Все три среды выполнения инструментированы OTel SDK. Go: перехватчики otelgrpc на оркестраторе (на стороне клиента и сервера), стандартный otel-go SDK. Python: opentelemetry-sdk с интеграцией LangChain/LangGraph OTel (проверить: поддержка OTel в LangGraph варьируется в зависимости от версии — использовать ручное создание span, если авто-инструментация неполная). TypeScript: @opentelemetry/sdk-node с инструментациями HTTP и gRPC. Распространение трассировки: контекст трассировки W3C внедряется в метаданные gRPC (Go↔Python) и заголовки вызовов MCP (Python→TS, так как MCP позволяет произвольные метаданные на вызов). Иерархия трассировок: Session span (верхний уровень, принадлежит оркестратору) → Run span → Page span → Step span → дочерние span: LLMCall, MCPToolCall, HealAttempt, GateEvent. Обязательные атрибуты span: session_id, run_id, plan_id, step_id, run_mode, node_name (узел LangGraph), model_name, token_input (int), token_output (int), cost_usd (float), selector (в span act/heal), confidence (в span heal), diff_ratio (в span verify). Экспортёр: OTLP/gRPC в Grafana Tempo (домашняя лаборатория) или Jaeger (локальная разработка). Семплирование: 100% в режиме explore, 100% в режиме CI replay (каждый запуск должен быть полностью наблюдаемым для обеспечения доверия).

ТРАНСКРИПТ LLM (воспроизводимый, неизменяемый):
Каждый вызов LLM логируется в /runs/{run_id}/llm_transcript.jsonl (только дозапись, один файл на run). Формат строки: {timestamp, run_id, step_id, node_name, model, prompt_hash(sha256 отрендеренного запроса), prompt_text, response_text, tokens_input, tokens_output, latency_ms, cost_usd, finish_reason}. Файл закрывается (fsync) по завершении запуска. Никогда не усекается и не перезаписывается. Назначение: (a) офлайн-отладка решений LLM без повторного запуска против AUT, (b) итерации по prompt engineering, (c) распределение стоимости по типу узла, (d) аудит соответствия "что решил агент и почему".

ТРАССИРОВКИ PLAYWRIGHT:
Запись трассировки Playwright (screenshots: true, snapshots: true, sources: true) начинается при инициализации playwright-executor. Одна трассировка на run_id. Экспортируется в /runs/{run_id}/playwright-trace.zip менеджером trace-manager в конце запуска. Просматривается с помощью npx playwright show-trace. Также доступна через report-api GET /runs/{run_id}/trace (проксирует zip-файл).

СОБЛЮДЕНИЕ БЮДЖЕТА ТОКЕНОВ/СТОИМОСТИ:
Бюджет на запуск настраивается в agentctl run: --token-budget 50000 --cost-budget-usd 2.00. Значения по умолчанию настраиваются в config.yaml (Go Viper). Go BudgetService поддерживает авторитетные счётчики в SQLite run_index (обновляются атомарно через store-gateway). Python llm-client вызывает BudgetService.CheckAndDeduct(tokens_estimate, cost_estimate) перед каждым вызовом LLM — если любой бюджет будет превышен, вызов возвращает BudgetExhaustedError, brain перенаправляет узел plan в report (частичная заморозка в режиме explore). Жёсткий потолок: BudgetService.RecordActual проверяет, что фактическое использование не превышает потолок+10% допуска; при превышении (LLM вернул больше токенов, чем ожидалось) — помечает перерасход в run_index для проверки. Порог предупреждения: при 80% любого бюджета BudgetService испускает событие BUDGET_WARNING в EventStream. 100%: событие BUDGET_EXHAUSTED, brain получает его через стриминг и переходит в checkpoint → report.

МЕТРИКИ PROMETHEUS (эндпоинт /metrics оркестратора Go):
agent_run_duration_seconds{mode, status} histogram
agent_steps_total{mode, status(pass|fail|healed|quarantined|skipped)} counter
agent_heal_attempts_total{strategy(L1|L2|L3|L4|llm_a11y|llm_visual), outcome(success|flagged|human_gate|failed)} counter
agent_llm_calls_total{model, node} counter
agent_tokens_used_total{model} counter
agent_cost_usd_total{model} counter
agent_budget_remaining_ratio{resource(tokens|cost)} gauge
agent_human_gates_pending gauge
agent_flaky_steps_total gauge
agent_a11y_completeness_ratio{url} histogram (sampled per page visit)

ОТЧЁТЫ О ЗАПУСКАХ:
JSON (report.json): машиночитаемый, полная детализация по шагам, события восстановления, разбивка стоимости, список нестабильных шагов, список ожидающих human_gate, plan_id, plan_hash, aut_version. HTML (report.html): таблица шагов с цветовой кодировкой статуса (зелёный/красный/жёлтый=исправлен/серый=карантин), встроенные скриншоты для неуспешных+исправленных шагов, диаграмма разбивки стоимости, тепловая карта покрытия (посещённые страницы против общего sitemap). Оба генерируются Go report-api при финализации запуска. JSON-отчёт подходит для загрузки CI-артефактов и последующей обработки (Slack-уведомления, аннотации Grafana).

**Результаты.**

1. ЗАМОРОЖЕННЫЙ ПЛАН (plan-{id}.json): Основной результат режима explore. Артефакт детерминированного воспроизведения. Содержит: plan_id, plan_hash (гарантия целостности), aut_version, exploration_seed, упорядоченный список PlanStep с альтернативами локаторов и хешами a11y-снапшотов до и после. Хранится в SQLite plan_store + файл. Это "определение теста", которое заменяет написанный человеком тестовый скрипт для исследовательского покрытия.

2. КОД PLAYWRIGHT-ТЕСТА ({run_id}/generated.spec.ts): Автоматически сгенерированный TypeScript-файл теста Playwright из замороженного плана. Каждый PlanStep становится тестовым действием с использованием рекомендованной иерархии локаторов Playwright (предпочтение role/text/label-селекторам перед CSS). Page objects генерируются для каждого паттерна page_url и кэшируются в page_object_cache. Цель: 80%+ сгенерированного кода пригодно для использования qa-automation-engineer без модификации. Генерируется в конце каждого запуска explore вместе с замороженным планом. Позволяет автономному агенту питать существующий рабочий процесс qa-automation-engineer.

3. ТРАССИРОВКА PLAYWRIGHT ({run_id}/playwright-trace.zip): Полная трассировка браузера (сеть, консоль, скриншоты, DOM-снапшоты) для каждого запуска. Просматривается в Playwright Trace Viewer. Критично для отладки неуспешных CI-запусков без повторного запуска.

4. ОТЧЁТ О ЗАПУСКЕ ({run_id}/report.json + report.html): Таблица прохождения/неудачи/исправления/карантина на уровне шагов. Сводка восстановления (попытки, использованные стратегии, оценки уверенности). Разбивка стоимости (токены и USD на узел LangGraph). Метрики покрытия (посещённые страницы, покрытые типы взаимодействий, % покрытия sitemap в этом запуске). Список нестабильных шагов со статусом карантина. Список ожидающих human_gate. Статус целостности плана (совпадение/несовпадение plan_hash).

5. GOLDEN СНАПШОТЫ (/runs/baselines/{plan_id}/{step_id}.a11y.json + .png): JSON a11y-дерева и аннотированный скриншот для каждого шага плана. Эталон для обнаружения CI-регрессий. Обновляется только явной командой agentctl baseline update. Версионируется по (plan_id, step_id) — старые базовые линии архивируются со ссылкой superseded_by.

6. АУДИТОРСКИЙ СЛЕД ВОССТАНОВЛЕНИЯ (/runs/{run_id}/healing-audit.jsonl + агрегированный в SQLite healing_audit): Неизменяемый журнал только для дозаписи каждой попытки восстановления. Запрашивается через agentctl healing report [--plan-id] [--date-range]. Позволяет анализировать тенденции: какие селекторы наиболее часто восстанавливаются (сигнал нестабильности DOM), какие страницы нуждаются в инструментации data-testid, дрейф калибровки уверенности.

7. ТРАНСКРИПТ LLM ({run_id}/llm-transcript.jsonl): Воспроизводимая запись каждого prompt+response LLM с количеством токенов и стоимостью. Неизменяем после завершения запуска. Используется для: офлайн-отладки, prompt engineering, распределения стоимости, аудита соответствия.

8. SITEMAP ПОКРЫТИЯ ({run_id}/sitemap.json + сохранённый в SQLite): Граф обнаруженных страниц с узлами (URL, page_type, interactive_element_count, a11y_completeness_ratio) и рёбрами (пути навигации). Инкрементально обновляется между запусками. Показывает, какие части AUT исследованы, а какие нет. Доступен как JSON или через эндпоинт report-api /sitemap (формат графа для рендеринга UI).

**Путь к MVP.**

ФАЗА 1 — Go Spine (Недели 1-2): Создать Go-модуль с CLI agentctl (cobra), gRPC-сервером оркестратора (заглушки RunControl + BudgetService, возвращающие OK), store-gateway со схемой SQLite WAL (plan_store, locator_store, sitemap, run_index — все таблицы созданы, логики нет), HTTP-скелетом report-api (200 OK на /health). Подключить определения proto3 (инструментарий buf.build). Проверка: Go компилируется, gRPC-клиент может подключиться, таблицы SQLite создаются корректно, agentctl run выводит "run started" и немедленно завершается.

ФАЗА 2 — TypeScript Hands (Недели 3-4): Создать Node.js-проект (TypeScript, ts-node или tsx для разработки). Реализовать MCP stdio-сервер playwright-executor с 6 начальными инструментами: navigate, click, fill, snapshot_a11y, screenshot_annotated (внедрение JS оверлея с set-of-marks), close_context. Запуск/остановка записи трассировки Playwright. Проверка: MCP-сервер отвечает через stdio на вручную созданные JSON-RPC вызовы; snapshot_a11y возвращает валидное ARIA-дерево с реальной страницы (использовать playwright.dev как тестовый target); screenshot_annotated показывает наложенные числовые метки.

ФАЗА 3 — Python Brain Базовый цикл (Недели 5-6): Создать Python-проект (uv, pyproject.toml). Определить AgentState TypedDict с полной схемой. Реализовать 5 узлов: perceive (MCP snapshot), ground (обновление sitemap), plan (вызов LLM, Opus 4.8, вывод одного действия), act (вызов MCP-инструмента), observe (повторный MCP-снапшот). Подключить граф LangGraph с checkpointer SqliteSaver. Подключить gRPC-клиент к оркестратору Go (предварительная проверка BudgetService перед вызовом LLM в узле plan). Запустить 3-шаговую explore-сессию против локального тестового приложения. Проверка: полный полиглотный стек взаимодействует (gRPC Go↔Python, MCP stdio Python↔TS), LLM производит валидное действие click, действие выполняется в браузере, checkpoint сохраняется в SQLite.

ФАЗА 4 — Verify + Heal + Human Gate (Недели 7-8): Добавить узел verify (структурный diff a11y, захват golden снапшота при первом проходе). Добавить узел heal с healing-engine: ротация стратегий L1-L4 + рассуждение LLM по a11y-дереву (Проход A). Добавить human_gate через gRPC GateService + асинхронную паузу checkpoint. Реализовать agentctl gate list/approve/skip/abort. Добавить сохранение HealAttempt в store-gateway (дозапись в healing_audit). Проверка: намеренно сломать селектор в тестовом приложении, наблюдать срабатывание узла heal, неудачу стратегии L1, успешное повторное определение LLM, прохождение проверки уверенности, сохранение локатора, успешное выполнение act с исправленным селектором. Тест human gate: сломать локатор с неразрешимым DOM, наблюдать confidence < 0.60, создание gate, разрешение через agentctl gate skip.

ФАЗА 5 — Заморозка плана + Режим Replay (Недели 9-10): Реализовать узел freeze (сериализация плана, вычисление plan_hash, отправка в store-gateway). Реализовать freeze/load/hash-verify в plan-manager. Реализовать режимы replay/CI в узле plan (читает frozen_plan[step_index] без LLM). Реализовать diff golden снапшота в узле verify. Реализовать agentctl run --mode ci --plan-id. Реализовать exit codes CI (0/1/2/3). Проверка: полный запуск explore производит замороженный план; немедленное CI replay того же плана завершается с exit code 0 и 0 вызовами LLM; ручное редактирование JSON плана, проверка прерывания replay при несоответствии хеша.

ФАЗА 6 — Полная наблюдаемость + Соблюдение стоимости (Недели 11-12): Инструментирование OTel во всех трёх средах выполнения (Go otelgrpc, Python SDK, TS @opentelemetry/sdk-node). Распространение трассировки через заголовки контекста W3C в метаданных gRPC и вызовах MCP. Жёсткое соблюдение бюджета в BudgetService (проверка потолка в RecordActual). Предупреждение о бюджете при 80% (событие EventStream + лог). Эндпоинт /metrics Prometheus в оркестраторе Go. Логирование транскрипта LLM (дозапись в JSONL при каждом вызове llm-client). Генерация отчёта о запуске (JSON + HTML) в report-api. agentctl report show {run_id}. Проверка: полный запуск explore+replay виден в Jaeger/Tempo с корректной иерархией parent-child span; стоимость отслеживается точно; исчерпание бюджета при --cost-budget-usd 0.01 завершается корректно с частичным отчётом.

ФАЗА 7 — Карантин нестабильных + Экспорт тестового кода (Недели 13-14): Реализовать таблицу flake_registry и UpdateFlakeCount в store-gateway. Реализовать логику карантина в расчёте exit code CI. Реализовать экспорт generated.spec.ts из замороженного плана (генератор page object, предпочтение role-selector). Реализовать page_object_cache в SQLite. Реализовать agentctl healing report --flaky. Реализовать визуальное повторное определение с set-of-marks (LLM Проход B в healing-engine, добавляет возможности vision). Полный интеграционный тест: 2-недельный симулированный CI-цикл на реальном веб-приложении (предлагается: локальный экземпляр Gitea). Проверка: намеренно нестабильный шаг попадает в карантин после 3 неудач, exit code CI остаётся 0, generated.spec.ts проходит самостоятельный запуск npx playwright test.

**Ключевые риски:**

- Нестабильность задержки API LLM делает время CI replay непредсказуемым при срабатывании восстановления: смягчение с помощью ограничения в 2 попытки heal в режиме CI, затем автопропуск, плюс таймаут на шаг (флаг --step-timeout, по умолчанию 60s), применяемый оркестратором через распространение gRPC deadline.
- Обновления версии Playwright изменяют сигнатуры MCP-инструментов или формат снапшота доступности, нарушая replay: смягчение путём фиксации версии Playwright в package-lock.json, владения обёрткой MCP-сервера, чтобы перевод TypeScript-в-MCP поглощал изменения API Playwright без затрагивания Python или Go, и хранения сигнатур инструментов в версионированном mcp-schema.
- Дерево доступности неполное или отсутствующее на приложениях с обильным canvas, пользовательских веб-компонентах и межсайтовых iframe: смягчение путём представления a11y_completeness_ratio как метрики первого класса; когда ratio < 0.30, автоматически пропускать Проход A (рассуждения LLM по a11y-дереву) и переходить непосредственно к Проходу B (визуальное повторное определение); помечать страницу в sitemap как low-a11y и рекомендовать команде AUT инструментирование data-testid или ARIA role.
- Усиление записи SQLite WAL при параллельных CI-запусках, совместно использующих один файл базы данных: store-gateway является единственным писателем (мьютекс Go + журнал WAL), но высокочастотные параллельные запуски могут выстраиваться в очередь за контрольными точками WAL; смягчение с помощью настройки прагмы WAL checkpoint (PRAGMA wal_autocheckpoint=1000), отдельной SQLite LangGraph с областью видимости запуска на каждый запуск (уже отдельно) и задокументированного триггера масштабирования: > 10 параллельных запусков → миграция store-gateway на PostgreSQL.
- Замороженный план незаметно расходится, когда AUT имеет только визуальные изменения (CSS, макет), не затрагивающие ARIA-дерево: golden снапшоты только a11y пройдут, пока визуальный UX нарушен; смягчение путём включения хеша скриншота в golden_snapshot наряду с хешем a11y и пометки расхождения хеша скриншота как VISUAL_WARN в отчёте (не неудача, но отображается для проверки человеком).
- Оценки уверенности LLM неправильно откалиброваны — либо слишком агрессивные (автовосстановление неправильных элементов), либо слишком консервативные (слишком много human gate в CI): смягчение с помощью анализа калибровки аудиторского следа восстановления (agentctl healing calibrate --plan-id вычисляет точность/полноту прошлых автовосстановлений против результатов human_verified), плюс настройка порога уверенности для каждого сайта в config.yaml, а не глобальные жёстко заданные значения.
- Исчерпание токенного бюджета в середине explore производит неполный замороженный план с частичным покрытием: смягчение с предварительной проверкой бюджета перед шагом (перенаправление в checkpoint при исчерпании), частичной заморозкой плана (сохранение прогресса до последнего завершённого шага) и agentctl run --resume {run_id} для продолжения исследования в новом запуске с оставшимся бюджетом.
- Надёжность подпроцесса MCP stdio Python↔TypeScript: SIGPIPE при больших полезных нагрузках (скриншоты с set-of-marks большие), зомби-процессы при краше brain, дедлок при синхронном вызове MCP с полным выходным буфером: смягчение с помощью фреймирования MCP по частям (спецификация MCP поддерживает стриминг), инструмента heartbeat (MCP ping каждые 30s, таймаут 5s → перезапуск) и надзора процесса оркестратора Go, наблюдающего за PID Python brain (перезапуск при краше, что каскадно распространяется на перезапуск подпроцесса TS).

**ADR:**

- ADR-001: MCP через stdio для границы Python-TypeScript. ОТКЛОНЕНО: gRPC — TypeScript grpc-node добавляет сложность сборки с генерируемым кодом (плагин protoc для TS, отдельные сгенерированные стабы), имеет значительно менее зрелую экосистему, чем Python grpc, и производит форму API, идентичную той, что MCP предоставляет нативно; MCP stdio исключает нестабильность выделения портов в CI-контейнерах и напрямую отображается на паттерн узла вызова инструментов LangGraph без адаптерного кода.
- ADR-002: Explore-once-then-freeze-plan-replay-deterministically для CI, вместо повторного выполнения LLM с seed/temperature=0. ОТКЛОНЕНО: повторное выполнение LLM с seed — поставщики API LLM (включая Anthropic) не гарантируют по контракту детерминизм вывода даже при temperature=0 с фиксированным seed; потоковая токенизация и батчинг вносят вариативность; замороженный план — единственная надёжная гарантия воспроизводимости и к тому же устраняет зависимость от API LLM на CI happy path (нулевая стоимость, нулевая вариативность задержки от LLM при прохождении запусков).
- ADR-003: SQLite WAL mode с Go store-gateway как единственным писателем для всего долгосрочного хранения. ОТКЛОНЕНО: PostgreSQL — добавляет stateful-сервис (контейнер, расписание резервного копирования, конфигурация WAL, пул соединений) без какой-либо пользы при одноузловом масштабе; паттерн SQLite WAL параллельное-чтение + единственный-писатель соответствует архитектуре Go store-gateway; триггер масштабирования на PostgreSQL явный и совместимый по схеме.
- ADR-004: Дерево доступности как основной режим восприятия страницы, скриншот с set-of-marks как запасной только при a11y_completeness_ratio < 0.30. ОТКЛОНЕНО: восприятие только по скриншоту — вызовы модели vision стоят примерно в 4-6 раз больше токенов, чем текст, вносят нестабильность вариативности рендеринга (сглаживание, субпиксельные различия шрифтов в разных средах) и производят более слабые выходные данные локаторов (CSS-пути из визуальных ограничивающих прямоугольников хрупкие); ARIA-дерево семантично, стабильно при визуальном изменении стиля и производит role/name-селекторы, напрямую используемые Playwright.
- ADR-005: gRPC proto3 двунаправленный стриминг для границы оркестратор-brain Go-Python. ОТКЛОНЕНО: REST/HTTP — события исчерпания бюджета и разрешение gate требуют server-push от Go в Python, что через REST требует SSE или опроса; protobuf-схема валидируется при компиляции, предотвращая незаметный дрейф типов между стабами Go и Python; распространение gRPC deadline реализует таймаут на шаг корректно без таймеров уровня приложения.
- ADR-006: Встроенный checkpointer LangGraph (SqliteSaver для локальной среды / AsyncPostgresSaver для prod) как эпизодическая память сессии. ОТКЛОНЕНО: пользовательское хранилище эпизодического состояния — checkpointer LangGraph предоставляет изолированное по потокам состояние, восстановление после сбоя (возобновление с любой границы узла) и ветвление (развилки исследования "что если") из коробки; переимплементация этой семантики — месяцы работы с высоким риском корректности без архитектурного преимущества.
- ADR-007: Асинхронный human gate через паузу checkpoint LangGraph и CLI agentctl gate resolve, вместо синхронной блокировки пайплайна. ОТКЛОНЕНО: синхронная встроенная блокировка (агент опрашивает до ответа человека) — CI-пайплайны имеют жёсткие таймауты задания (обычно 30-60 мин); синхронная блокировка вызывает таймаут → потеря состояния запуска; асинхронный checkpoint сохраняет полное состояние неограниченно долго, позволяет человеку действовать через несколько часов и поддерживает настраиваемый таймаут автопропуска для неконтролируемого CI (по умолчанию 30 мин в режиме CI, без ограничений в режиме explore).
- ADR-008: Claude Sonnet 4.6 для повторного определения при восстановлении (узел heal), Claude Opus 4.8 зарезервирован исключительно для начального планирования исследования (узел plan в режиме explore). ОТКЛОНЕНО: Opus для всех вызовов LLM — узел heal находится на горячем пути CI (вызывается при каждом отказе локатора) и должен возвращать результат менее чем за 10 секунд; Sonnet достаточен для ограниченной задачи рассуждения при повторном определении локатора (структурированный вывод по ARIA-дереву или числовым меткам — не задача высокой сложности рассуждения); одно это решение маршрутизации снижает стоимость LLM на запуск примерно на 70-80% с учётом частоты восстановления в типичных запусках.

---

### `pragmatic-evolution` — CognitivePilot — Pragmatic MVP → Scale Evolution

*Lens: Pragmatic MVP -> scale evolution*

**Философия.** Ключевая ставка: использовать всё, что Playwright и LangGraph уже предоставляют бесплатно — официальный Playwright MCP server, встроенный trace viewer, встроенный codegen API, LangGraph SQLite checkpointer, LangGraph ToolNode — так чтобы единственным оригинальным кодом была дифференцирующая логика (стратегия автономного исследования, self-healing hierarchy, детерминированный движок explore-once-then-replay). Минимальный ценный срез — это Go CLI, который запускает Python brain, общающийся с официальным Playwright MCP subprocess и производящий замороженный plan.json плюс trace ZIP; всё остальное (gRPC orchestrator, report service, OTel, visual fallback) накладывается поверх этого проверенного канала после того, как он стабилизируется. Каждое решение build-vs-buy принимается явно на каждом контрольном рубеже milestone gate; ничто не откладывается молча — если откладывается, то называется с явным условием-триггером. Болевые точки масштабирования (SQLite single-writer, CI на одной машине) принимаются как ограничения v1 с задокументированными триггерами миграции, а не решаются превентивно инфраструктурой, которая команде ещё не нужна.

**Компоненты (7):**

| Component | Language | Responsibility |
|-----------|----------|----------------|
| agent-ctl | Go | Точка входа CLI: подкоманды run (--explore \| --replay \| --heal), report, serve, locators. Разбирает конфигурационный YAML запуска, запускает orchestrator, читает потоковые строки лога через gRPC, завершает работу с кодом статуса запуска (0 = pass, 1 = fail, 2 = partial/needs-human). Единственный бинарный файл, видимый пользователю. Должен работать в CI (non-TTY) и интерактивном режиме. Собирать первым. |
| orchestrator | Go | Менеджер жизненного цикла запуска и gRPC server. Контролирует subprocess Python brain (запуск, health-ping, перезапуск при сбое, SIGTERM по таймауту). Предоставляет gRPC service RunControl (StartRun, StopRun, GetRunStatus со streaming tail лога, GetRunResult). Управляет state machine запуска на уровне процесса: PENDING -> RUNNING -> HEALING -> PARTIAL -> DONE \| FAILED. Маршрутизирует события NEEDS_HUMAN_REVIEW от brain в stdout/webhook. Не обращается к SQLite напрямую — делегирует все записи в persistence-gateway. |
| brain | Python | LangGraph state machine из 8 узлов: perceive, ground, plan, act, verify, heal, checkpoint, report. Владеет всеми LLM-вызовами (Opus 4.8 для планирования, Sonnet 4.6 для healing). Запускает официальный Playwright MCP server как subprocess и вызывает его инструменты через MCP stdio protocol посредством LangGraph ToolNode. Подключается к Go orchestrator как gRPC client (сообщает о событиях, получает locator cache, сохраняет результаты). Управляет переключением режимов explore-once-then-replay и вычислением plan_hash. Единственное место, где живёт LLM reasoning. |
| playwright-mcp | TypeScript | BUY: официальный сервер @playwright/mcp (поддерживается Microsoft). Запускается как дочерний процесс Python brain (stdio). Предоставляет инструменты браузера: navigate, click, fill, accessibility_snapshot, screenshot, locator_evaluate, trace_start, trace_stop, codegen_record, codegen_stop. Brain вызывает их как MCP tools через ToolNode. Этот компонент НЕ строится — он устанавливается как npm package и запускается как subprocess. Единственный оригинальный TS-код — тонкий artifact-pusher sidecar, который по окончании запуска отправляет POST с trace ZIP и codegen output на REST endpoint persistence-gateway. |
| persistence-gateway | Go | Шлюз SQLite с одним writer'ом. Все записи в БД в системе проходят через этот service — brain записывает locators и события через gRPC PersistenceService RPC; artifact-pusher отправляет бинарные артефакты через REST методом POST. Управляет схемными миграциями (golang-migrate). Таблицы: runs, healed_locators, page_models, healing_events, step_failures, checkpoints (переполнение LangGraph), run_transcripts. SQLite в WAL mode. Предоставляет read RPC, чтобы brain мог запрашивать locator cache и page model cache без прямого обращения к SQLite из Python. |
| report-service | Go | Сборка артефактов запуска и REST API для потребителей CI. По завершении запуска: читает запись run из SQLite, собирает run_report.json, рендерит HTML-отчёт (Go template, повторяет структуру Playwright HTML reporter), вызывает остановку codegen у artifact-pusher и собирает .spec.ts output, упаковывает healing_report.json, публикует в artifact directory. Предоставляет GET /runs/{id}/report, GET /runs/{id}/trace, GET /runs/{id}/spec, GET /runs/{id}/cost. Опционально: webhook по завершении запуска для интеграции с CI. ОТЛОЖЕНО в milestone 0–2; вводится на milestone 3. |
| proto | shared | Определения Protobuf для всех gRPC-контрактов (Go spine <-> Python brain). Три service: RunControl (orchestrator предоставляет, agent-ctl потребляет), PersistenceService (persistence-gateway предоставляет, brain потребляет), EventStream (orchestrator предоставляет streaming событий, report-service потребляет). Единый источник истины — stubs генерируются в CI для Go и Python. Нарушение proto ломает сборку. Хранится в директории /proto, версионируется вместе с репозиторием. |

**Границы (полиглотные контракты).**

Go <-> Python: gRPC proto3, три service. RunControl: StartRun(RunConfig) -> stream RunEvent; StopRun(RunId) -> Ack; GetRunResult(RunId) -> RunResult. PersistenceService: WriteLocator(HealedLocator) -> WriteAck; ReadLocators(PageUrl) -> stream LocatorRecord; WriteEvent(RunEvent) -> WriteAck; WriteRunResult(RunResult) -> WriteAck; ReadPageModel(UrlHash) -> PageModelRecord. EventStream: SubscribeEvents(filter) -> stream RunEvent (используется report-service). Обоснование: gRPC обеспечивает типобезопасные контракты между Go и Python, двунаправленный streaming для tail логов и канал, проверяемый в CI (несоответствие proto = ошибка сборки). Изоляция отказов: если subprocess Python brain падает, Go orchestrator обнаруживает разрыв gRPC (DeadlineExceeded на следующем health-ping в течение 5s), помечает запуск как FAILED и сохраняет частичное состояние через persistence-gateway. Падение brain не выводит из строя orchestrator или CLI.

Python <-> TypeScript: MCP через stdio. Python brain запускает официальный бинарный файл @playwright/mcp как subprocess (`node @playwright/mcp/cli`) и общается через JSON-RPC 2.0 поверх stdin/stdout. LangGraph ToolNode нативно понимает описания MCP tools — адаптационный слой не нужен. Доступные tools: navigate, accessibility_snapshot, screenshot, click, fill, locator_evaluate, trace_start/stop, codegen_record/stop. Обоснование: MCP stdio — нативный протокол инструментальных вызовов LLM; LangGraph ToolNode обрабатывает его без каких-либо пользовательских адаптеров; stdio избегает выделения портов, правил брандмауэра и service discovery. Отклонённая альтернатива: кастомный gRPC TS server — потребовал бы поддержки proto для операций браузера, которые Playwright MCP уже хорошо определяет. Изоляция отказов: монитор subprocess Python обнаруживает EOF stdout (падение MCP server), перезапускает MCP subprocess и переходит к последнему известному URL из RunState.page_model.url. Checkpoint LangGraph означает, что никакая работа не теряется.

TypeScript -> Go: REST HTTP. Тонкий artifact-pusher sidecar (единственный оригинальный TS-код) отправляет POST на endpoints persistence-gateway: POST /artifacts/trace (multipart, ZIP), POST /artifacts/codegen (JSON, содержимое .spec.ts), POST /artifacts/screenshot (PNG). Fire-and-forget с тремя повторными попытками и экспоненциальным backoff. Обоснование: однонаправленная отправка данных, streaming не нужен, максимально простой интерфейс. Циклическая зависимость gRPC (TS вызывает обратно в Go orchestrator) потребовала бы, чтобы TS был gRPC client, что добавляет сложность для того, что по сути является загрузкой файлов.

**Цикл агента.**

УЗЛЫ (8):

perceive: Вызывает MCP accessibility_snapshot() — возвращает структурированное дерево ARIA (roles, names, states, relationships). Если completeness ratio (named interactive elements / total interactive elements) < 0.30, также вызывает screenshot() для контекста set-of-marks fallback. Сохраняет raw a11y tree в RunState.page_model. Также вызывает MCP trace_start() при запуске и при возобновлении.

ground: Разбирает a11y tree в типизированную PageModel: {url, title, landmarks: list[Landmark], forms: list[Form], interactive_elements: list[Element], depth_estimate}. Вычисляет хеш идентичности страницы (URL + структура landmark). Проверяет persistence-gateway на наличие кешированной PageModel (ReadPageModel RPC) — при cache hit и run_mode==replay проверяет структурный дрейф против golden snapshot. Обновляет RunState.page_model.

plan: Входит ТОЛЬКО при run_mode==explore ИЛИ current_step==0 без замороженного плана. Вызывает Opus 4.8 с: target_url, page_model, хвост episodic_buffer (последние 10 событий), exploration_goal. LLM возвращает упорядоченный список PlannedAction {intent, selector_hint, action_type, expected_outcome, is_critical}. Вычисляет plan_hash = SHA256(json.dumps(exploration_plan, sort_keys=True)). Записывает plan.json в artifact dir. Проверяет token_budget.plan_tokens_used перед вызовом — жёстко прерывает, если превышен лимит. Полностью пропускается в режиме replay.

act: Извлекает следующий PlannedAction из exploration_plan[current_step]. В режиме explore: выполняет через MCP tool (click/fill/navigate/etc.) используя selector_hint. В режиме replay: использует замороженный selector из plan.json. Добавляет в RunState.executed_actions. Инкрементирует current_step.

verify: Вызывает логику perceive inline (a11y snapshot) для обнаружения состояния после действия. Проверяет: URL изменился как ожидалось? Появилось модальное окно с ошибкой? Ожидаемый элемент теперь виден? Исключение locator-not-found от узла act? Классифицирует результат: PASS, LOCATOR_STALE, ELEMENT_GONE, TIMING, UNEXPECTED_ERROR. При PASS и шаге milestone: вызывает узел checkpoint. При любом сбое: перенаправляет на узел heal. Когда все шаги исчерпаны или бюджет превышен: перенаправляет на узел report.

heal: Алгоритм self-healing (см. поле selfHealing). При успешном heal: обновляет exploration_plan[current_step].selector исправленным selector, возвращает к act для повторной попытки. При сбое heal после 3 попыток: помечает шаг как FAILED в RunState, отправляет событие NEEDS_HUMAN_REVIEW через gRPC WriteEvent, перенаправляет на report (или продолжает к следующему шагу, если is_critical==false).

checkpoint: Запускает сброс checkpoint LangGraph в SQLite (встроенный LangGraph SQLite checkpointer). Также вызывает PersistenceService.WriteLocator для всех вновь исправленных locators, ожидающих в RunState.healed_locators. Записывает page model в persistence-gateway, если не кеширован. Возвращает к perceive для следующего цикла.

report: Вызывается по завершении запуска (все шаги выполнены, бюджет превышен или критический шаг завершился сбоем). Вызывает MCP trace_stop() — запускает artifact-pusher sidecar для отправки POST с trace ZIP на persistence-gateway. Записывает RunResult в persistence-gateway через WriteRunResult RPC. Заполняет RunState.artifacts путями. Переходит в END.

РЁБРА:
perceive -> ground (безусловное)
ground -> plan (run_mode==explore AND current_step==0)
ground -> act (run_mode==replay OR plan exists AND current_step>0)
plan -> checkpoint (plan just frozen)
plan -> act (after checkpoint)
act -> verify (безусловное)
verify -> heal (LOCATOR_STALE | ELEMENT_GONE | TIMING)
verify -> checkpoint (PASS AND milestone_step)
verify -> act (PASS AND not milestone_step AND steps_remain)
verify -> report (all_steps_done OR budget_exceeded OR UNEXPECTED_ERROR)
heal -> act (heal succeeded)
heal -> report (heal failed AND is_critical==true)
heal -> act_next_step (heal failed AND is_critical==false)
checkpoint -> perceive (следующий цикл)
report -> END

ОБЩИЙ ОБЪЕКТ СОСТОЯНИЯ (RunState TypedDict):
run_id: str
target_url: str
run_mode: Literal["explore", "replay", "heal"]
exploration_plan: list[PlannedAction]  # {intent, selector, action_type, expected_outcome, is_critical, healed: bool}
plan_hash: str
current_step: int
page_model: PageModel  # {url, title, a11y_tree: dict, landmarks, forms, interactive_elements, completeness_ratio, golden_hash}
episodic_buffer: list[EpisodicEvent]  # bounded deque max 50, evicts oldest
executed_actions: list[ExecutedAction]  # {step, action_type, selector, outcome, duration_ms}
healed_locators: list[HealedLocator]  # pending flush to persistence-gateway
pending_human_review: list[HealCandidate]  # confidence 0.60-0.84, flagged
token_budget: TokenBudget  # {plan_used, plan_limit, heal_used, heal_limit} — both in tokens
confidence_scores: dict[str, float]  # keyed by step index
artifacts: RunArtifacts  # {trace_path, screenshot_paths, codegen_path, report_path}
last_error: Optional[str]
heal_attempts: int  # reset on new step
step_failures: dict[str, int]  # step_key -> consecutive_failure_count, for flake quarantine

**Self-healing.**

ШАГ 1 — ВОСПРИЯТИЕ (в узле verify):
Вызвать MCP accessibility_snapshot(). Вычислить completeness_ratio = len(named_interactive) / max(1, expected_interactive_count). Если completeness_ratio < 0.30 (разреженное дерево — тяжёлый canvas, кастомные web components, shadow DOM), также вызвать screenshot() и аннотировать set-of-marks: нарисовать пронумерованные ограничивающие рамки вокруг всех обнаруженных интерактивных областей с помощью page.evaluate() + canvas overlay (проверить эту возможность Playwright перед использованием). Сохранить оба представления в RunState.page_model.

ШАГ 2 — КЛАССИФИКАЦИЯ СБОЯ (в узле verify):
Перехватить исключение Playwright из узла act. Классифицировать:
- LOCATOR_STALE: элемент присутствует в a11y tree, но selector больше не совпадает (например, изменилось имя класса, перегенерирован ID).
- ELEMENT_GONE: элемент полностью отсутствует в a11y tree (функция удалена, условный рендеринг, A/B вариант).
- TIMING: элемент присутствует, но ещё не доступен для взаимодействия (detached, закрыт, анимируется) — повторить act с ожиданием 2s перед эскалацией в heal.
- UNEXPECTED_ERROR: ошибка навигации, сетевой сбой, исключение JS — пропустить healing, отправить событие, перенаправить на report.

ШАГ 3 — ИЕРАРХИЯ RE-GROUNDING (в узле heal, последовательно, максимум 3 попытки):
Попытка 1 (нулевые затраты LLM, детерминированная): Ротация стратегий. Для неудавшегося PlannedAction.intent перебирать selectors в порядке: (a) атрибут data-testid, совпадающий с ключевыми словами intent, (b) ARIA role + accessible name, (c) CSS по семантическому классу (не генерируемые хеш-классы), (d) совпадение по видимому текстовому содержимому, (e) XPath позиционный как крайний вариант. Вызвать MCP locator_evaluate(selector) для каждого кандидата. Первый кандидат, разрешающийся ровно в один элемент: использовать его. Назначить confidence: 0.95 если data-testid, 0.90 если ARIA role+name, 0.80 если text, 0.70 если XPath.

Попытка 2 (Sonnet 4.6, структурированные рассуждения): Если попытка 1 не удалась или вернула ноль совпадений. Составить prompt: "The action [intent] previously matched selector [old_selector]. The element is no longer found. Here is the current accessibility tree: [a11y_tree_json truncated to 4000 tokens]. Return JSON {new_selector: string, confidence: float, reasoning: string}. Prefer data-testid, then ARIA role+name, then visible text. Do not return XPath unless no alternative exists." Разобрать JSON-ответ. Вызвать MCP locator_evaluate(new_selector). Если разрешается ровно в один элемент: использовать. Confidence = min(llm_confidence, 0.90). Проверить heal token budget перед вызовом.

Попытка 3 (Sonnet 4.6 vision, set-of-marks): Только если completeness_ratio < 0.30 (необходим visual fallback) И попытка 2 не удалась. Отправить аннотированный screenshot (пронумерованные ограничивающие рамки) с prompt: "Which numbered element corresponds to the action: [intent]? Return JSON {box_number: int, confidence: float}." Извлечь box_number, получить координаты ограничивающей рамки из карты аннотаций, вывести координатный клик (MCP click(x, y) — проверить поддержку координатных кликов Playwright MCP). Confidence = llm_confidence * 0.85 (штраф за неточность).

ШАГ 4 — CONFIDENCE GATE:
>= 0.85: автоматически сохранить исправленный locator. Вызвать PersistenceService.WriteLocator({original, healed, method, confidence, page_url, element_label, run_id}). Обновить RunState.exploration_plan[current_step].selector. Обновить plan.json на диске. Установить healed=true на PlannedAction. Продолжить запуск.
0.60 - 0.84: записать в RunState.pending_human_review. Вызвать PersistenceService.WriteEvent({type: HEAL_CANDIDATE, ...}). Продолжить запуск с исправленным locator (оптимистично — большинство heal'ов с 0.70+ корректны). CI отобразит очередь проверки в отчёте о запуске.
< 0.60: вызвать PersistenceService.WriteEvent({type: NEEDS_HUMAN_REVIEW}). Orchestrator отправляет в stdout/webhook. Поведение настраивается для каждого запуска: CI mode = пропустить шаг и продолжить; интерактивный mode = приостановить и ожидать ввода человека (таймаут 5 мин, затем пропустить).

ШАГ 5 — ЖУРНАЛ АУДИТА:
Каждая попытка heal (успешная или нет) записывает строку в таблицу SQLite healing_events через PersistenceService: {run_id, step, step_key, original_selector, attempt_1_result, attempt_2_tokens, attempt_3_tokens, final_selector, final_method, confidence, outcome, timestamp}. Это основной криминалистический артефакт для понимания дрейфа DOM с течением времени. Аудит healing включается в артефакт healing_report.json.

**Детерминизм.**

ОСНОВНОЙ МЕХАНИЗМ — Explore-Once-Then-Replay:
Первый запуск: agent-ctl run --explore. Brain входит в режим explore, выполняет полный цикл LangGraph explore->plan->act->verify->checkpoint. По завершении запуска записывается plan.json: {plan_hash: SHA256(json.dumps(exploration_plan, sort_keys=True)), steps: [...PlannedAction с разрешёнными selectors...], golden_snapshots: {step_index: a11y_hash}, created_at, target_url, agent_version}. plan.json коммитится в репозиторий. CI всегда запускает: agent-ctl run --replay --plan plan.json. В режиме replay узел plan полностью пропускается — ground перенаправляет напрямую в act. Токены LLM на планирование не расходуются. В горячем пути нет недетерминированных решений LLM.

ДИСЦИПЛИНА ТЕМПЕРАТУРЫ LLM:
Все вызовы планирования: temperature=0. Это не гарантирует идентичных выводов при разных версиях модели, но устраняет дисперсию семплирования в рамках фиксированной модели. plan_hash вычисляется после создания плана и сохраняется в plan.json. При replay agent-ctl run --replay проверяет соответствие plan_hash файла. Если не совпадает (файл был вручную отредактирован или частично исправлен), запуск прерывается, если не передан флаг --force-replay.

САМООБНОВЛЕНИЕ ПЛАНА ПРИ HEALING:
Когда locator исправлен с confidence >= 0.85 во время replay, report-service обновляет plan.json на месте и пересчитывает plan_hash. CI можно настроить на автоматический коммит обновлённого plan.json (git commit -m "heal: update plan for step N") или на вывод его как PR артефакта для ревью человеком. Это сохраняет файл plan как единственный источник истины для стабильных locators.

КАРАНТИН НЕСТАБИЛЬНЫХ ТЕСТОВ:
Таблица step_failures в SQLite отслеживает: step_key (url + хеш intent), consecutive_failure_count, last_seen_at. После 3 последовательных сбоев replay на одном и том же шаге в разных run ID CI (не в одном запуске, не транзиентных): шаг помечается как quarantined. Шаги в карантине пропускаются при replay (записываются как SKIPPED_QUARANTINED в отчёте о запуске). Ненулевое количество карантинных шагов делает запуск с кодом выхода 2 (PARTIAL) вместо 0 (PASS), что CI может воспринять как предупреждение. Человек снимает карантин запуском agent-ctl locators clear-quarantine --step-key <key>.

ЗОЛОТЫЕ A11Y СНИМКИ:
Узел perceive сохраняет a11y_hash (SHA256 отсортированного JSON a11y tree) для milestone шагов. Хранится в plan.json golden_snapshots. При replay узел ground вычисляет текущий a11y_hash и сравнивает с gold. Структурное расхождение (появилось новое модальное окно, навигация удалена, форма добавлена) генерирует событие STRUCTURAL_CHANGE. Настраиваемое действие: warn-only (по умолчанию) или запуск частичного re-explore для затронутого поддерева. Это обнаруживает случай, когда страница изменилась так, что замороженный план был бы бессмысленным, даже если отдельные selectors всё ещё разрешаются.

ПОСЕВ ИССЛЕДОВАНИЯ:
В режиме explore узлу plan передаётся стабильный exploration_seed, полученный из SHA256(target_url + run_date_utc_day). Это не делает выводы LLM детерминированными (реальный рычаг — temperature=0), но даёт исследованию стабильный контекстный якорь и делает seed проверяемым. Что важнее: seed записывается в plan.json, так что если план расходится, исследование можно повторить с тем же seed.

**Память.**

КРАТКОСРОЧНАЯ (эпизодическая, в рамках сессии):
RunState.episodic_buffer — ограниченная deque на стороне Python, максимум 50 записей EpisodicEvent {node, action, outcome, a11y_delta, timestamp}. Хранится в состоянии LangGraph в памяти. Сбрасывается в SQLite при каждом узле checkpoint через встроенный LangGraph SQLite checkpointer (BUY: использовать langgraph.checkpoint.sqlite.SqliteSaver — одна строка настройки). Обеспечивает возобновление после сбоя: если subprocess Python brain падает в середине запуска, orchestrator перезапускает его и brain перезагружает последний checkpoint. Запуск продолжается с последнего checkpointed шага, а не с нуля.

ДОЛГОСРОЧНАЯ (сохраняется между запусками):
1. Locator Store — таблица SQLite healed_locators: {id, page_url_hash, element_label, original_selector, healed_selector, healing_method, confidence, times_validated, last_used_at, last_validated_at, created_at, status: active|deprecated|pending_review}. Перед попыткой 1 в узле heal brain вызывает PersistenceService.ReadLocators(page_url) — cache hit (совпадающий element_label + высокий confidence) означает нулевые затраты LLM на healing. Это основная оптимизация затрат для повторяющихся изменений DOM.

2. Page Model Cache — таблица SQLite page_models: {url_hash, a11y_tree_json, landmarks_json, form_count, interactive_count, golden_a11y_hash, last_updated_at}. Заполняется при первом запуске explore. Используется узлом ground для обнаружения структурного дрейфа при replay. Инвалидируется при обнаружении структурного изменения.

3. Page Object Cache — экспортированные .spec.ts файлы в репозитории (генерируются report-service из executed_actions + Playwright codegen output). Они читаемы людьми, контролируются версией и могут быть переиспользованы qa-automation-engineer без обращения к агенту. BUY: Playwright codegen API генерирует валидный TypeScript тестовый код из записанных действий — агент вызывает codegen_record() во время act и codegen_stop() в конце запуска.

4. Run History — таблица SQLite runs: {run_id, target_url, plan_hash, run_mode, status, token_cost_usd, plan_tokens, heal_tokens, duration_ms, step_count, heal_count, artifact_dir, created_at, completed_at}. Используется report-service для запросов трендов и дашбордов стоимости.

ХРАНИЛИЩЕ: единый файл SQLite agent.db, WAL mode, записывается исключительно persistence-gateway. Расположение: настраиваемое (по умолчанию: ./data/agent.db, CI: /tmp/agent-{run_id}.db для параллелизма, home-lab: /opt/agent_development/data/agent.db). Триггер миграции на PostgreSQL: > 50 параллельных запусков CI на разных машинах с общей БД или необходимость в multi-node agent workers. Не ожидается в v1.

**Наблюдаемость.**

РАСПРЕДЕЛЁННАЯ ТРАССИРОВКА (OpenTelemetry):
Каждый узел LangGraph — это OTel span. Python brain инструментируется через opentelemetry-sdk + opentelemetry-instrumentation-langchain (проверить доступность для LangGraph — может потребоваться ручная обёртка span). Атрибуты span: run_id, node_name, step_index, run_mode, model_used. Контекст трассировки распространяется в метаданные gRPC (Go orchestrator -> Python brain через заголовок W3C traceparent). Go компоненты инструментируются через go.opentelemetry.io/otel. Экспорт: OTLP gRPC в home-lab Grafana Alloy -> Grafana Tempo. Один trace на запуск, один span на вызов узла. Spans self-healing включают: heal_attempt_number, healing_method, confidence_score, tokens_consumed.

ТРАНСКРИПТ LLM ДЛЯ КАЖДОГО РЕШЕНИЯ:
Каждый вызов LLM (узел plan, попытка heal 2, попытка heal 3) добавляет JSONL-запись в run_transcripts/{run_id}.jsonl: {ts, run_id, node, model, prompt_tokens, completion_tokens, latency_ms, cost_usd_estimate, decision_summary (первые 200 символов вывода), temperature}. Записывается на диск brain'ом, отправляется методом POST в persistence-gateway в конце запуска. Позволяет: пост-запусковый аудит стоимости, отладку prompt'ов, воспроизведение того, что именно решил LLM и почему.

БЮДЖЕТ ТОКЕНОВ + ЖЁСТКИЕ ЛИМИТЫ:
RunState.token_budget отслеживает использование по каждой модели. Config YAML указывает: plan_token_limit (по умолчанию 50000 Opus токенов на запуск), heal_token_limit (по умолчанию 20000 Sonnet токенов на запуск). Перед каждым вызовом LLM brain проверяет: если budget.plan_used >= plan_limit, пропустить узел plan и отправить событие BUDGET_PLAN_EXCEEDED (запуск переключается в replay с текущим планом). Если budget.heal_used >= heal_limit, healing откатывается только на ротацию стратегий (попытка 1), а неразрешённые шаги помечаются для ревью человеком. Превышение бюджета не прерывает запуск — он деградирует корректно. Расчётная стоимость вычисляется как: tokens * model_price_per_token (жёстко задано в config YAML, обновлять вручную при изменении цен).

PLAYWRIGHT TRACES:
MCP trace_start() вызывается при запуске и после каждого восстановления после сбоя. MCP trace_stop() вызывается в конце запуска — создаёт .zip, содержащий network HAR, console logs, screenshots и все действия с таймингом. Отправляется в persistence-gateway sidecar'ом artifact-pusher. Просмотр: playwright show-trace <path>. Это основной артефакт отладки для сбоев CI — никакой специальной трассировочной инфраструктуры не нужно. Полностью BUY от Playwright.

ДАШБОРД СТОИМОСТИ:
Go report-service предоставляет GET /metrics (формат Prometheus): agent_run_total, agent_run_duration_seconds, agent_tokens_plan_total, agent_tokens_heal_total, agent_cost_usd_total, agent_heal_success_rate, agent_flake_quarantine_count. Собирается home-lab Prometheus, визуализируется в Grafana. Специальная метрическая инфраструктура не нужна — стандартная клиентская библиотека Prometheus для Go.

**Выходные данные.**

1. Отчёт о запуске (JSON + HTML): run_{id}_report.json — машиночитаемый: {run_id, status, step_count, pass_count, fail_count, skip_count, heal_count, token_cost_usd, duration_ms, plan_hash, artifact_paths}. run_{id}_report.html — читаемый человеком HTML (Go template), повторяет структуру Playwright HTML reporter, чтобы CI-системы, уже парсящие отчёты Playwright, работали без изменений.

2. Замороженный план исследования: plan.json — {plan_hash, target_url, steps, golden_snapshots, agent_version, created_at}. Основной артефакт CI — коммитится в репозиторий, управляет всеми replay запусками. Также является основным выходом первого запуска explore.

3. Playwright Trace: traces/run_{id}.zip — полный Playwright trace (сеть, консоль, screenshots, действия, тайминг). Просмотр: playwright show-trace traces/run_{id}.zip. Дополнительный инструментарий не требуется. Полностью BUY.

4. Экспортированный Playwright тестовый код: generated/{page_slug}.spec.ts — валидный TypeScript Playwright тестовый файл, по одному на каждый исследованный page flow, полученный из executed_actions + Playwright codegen output. Используется qa-automation-engineer как отправная точка для поддерживаемых тестовых наборов. BUY: Playwright codegen API генерирует сырой тестовый код; агент оформляет его в правильный spec-файл с блоками test() и утверждениями expect(), полученными из полей PlannedAction.expected_outcome.

5. Регрессионные A11y базовые линии: baselines/{url_hash}_a11y.snap — JSON снимки a11y tree для milestone шагов. Сравниваются при каждом replay запуске. Структурные изменения отражаются в отчёте о запуске.

6. Отчёт об аудите healing: heal_{id}_report.json — {run_id, total_heals, auto_persisted, pending_review, failed, by_method: {strategy_rotation: N, llm_a11y: N, visual: N}, locators: [...]каждый с confidence и outcome}. Потребляется CI для принятия решения — автоматически закоммитить обновлённый plan.json или открыть PR.

7. Отчёт о стоимости: встроен в run_report.json плюс строки в таблице SQLite runs для анализа трендов через Prometheus/Grafana.

**Путь к MVP.**

MILESTONE 0 — "Hello Browser" (Дни 1-3). Цель: проверить канал, а не интеллект.
- Go: agent-ctl с единственной подкомандой `run`. gRPC пока нет. Запускает Python brain как subprocess с env vars (TARGET_URL, RUN_ID, ARTIFACT_DIR). Ожидает код выхода.
- Python: граф LangGraph с одним узлом (только узел perceive). Запускает официальный subprocess сервера @playwright/mcp. Вызывает accessibility_snapshot(). Выводит результат в stdout. Завершает работу с кодом 0.
- TypeScript: npm install @playwright/mcp. Только это. Никакого оригинального кода.
- Выход: JSON дерева accessibility, выведенный в stdout. Playwright trace ZIP в artifact dir.
- СОБРАТЬ: Go CLI (50 строк), узел perceive Python (30 строк). BUY: всё Playwright. ОТЛОЖИТЬ: gRPC, SQLite, orchestrator, все остальные узлы.
- Контрольная точка: agent-ctl run --target https://example.com производит непустое a11y дерево и trace ZIP. Запускается в CI (headless Chromium).

MILESTONE 1 — "Autonomous Walk" (Дни 4-10). Цель: полный запуск explore, замороженный план, транскрипт.
- Python brain: все 8 узлов (perceive, ground, plan, act, verify, checkpoint, report — heal-заглушка). LangGraph SQLite checkpointer (BUY, встроенный). Вызывает Opus 4.8 (или Sonnet 4.6 как бюджетный прокси) в узле plan. Записывает plan.json.
- Go: agent-ctl передаёт env var RUN_MODE=explore. Читает plan.json после выхода brain.
- Выход: plan.json с plan_hash, JSONL транскрипт запуска, Playwright trace ZIP.
- СОБРАТЬ: 7 узлов LangGraph, парсер ground, проверка бюджета токенов. BUY: LangGraph checkpointer.
- ОТЛОЖИТЬ: gRPC (по-прежнему subprocess+env), persistence-gateway, report-service, healing.
- Контрольная точка: запуск agent-ctl run --explore на реальном многостраничном приложении производит plan.json с >= 5 шагами и валидным trace ZIP.

MILESTONE 2 — "Self-Repairing Walker" (Дни 11-20). Цель: цикл healing + locator store.
- Python brain: узел heal с иерархией из 3 попыток. Ротация стратегий (без LLM), LLM a11y reasoning (Sonnet). Visual set-of-marks отложен — сначала проверить необходимость.
- Go: persistence-gateway введён. gRPC PersistenceService stubs (WriteLocator, ReadLocators, WriteEvent). Схема SQLite v1 (runs, healed_locators, healing_events).
- Proto: первый proto файл. Stubs Go + Python генерируются в CI.
- Выход: healing_report.json. Обновлённый plan.json при auto-heal.
- СОБРАТЬ: узел heal, persistence-gateway, proto v1. ОТЛОЖИТЬ: visual fallback (попытка 3), orchestrator (управление subprocess по-прежнему в agent-ctl), report-service.
- Контрольная точка: запуск против приложения, где selector был намеренно сломан; агент исправляет его с confidence >= 0.85 и сохраняет в SQLite; второй запуск использует кешированный locator (ноль вызовов LLM для heal).

MILESTONE 3 — "CI-Ready Replay" (Дни 21-30). Цель: детерминированные запуски CI.
- Python brain: режим replay (флаг --replay, загрузка plan.json, пропуск узла plan, проверка plan_hash).
- Go: orchestrator выделен из agent-ctl (полноценный gRPC server, service RunControl, управление жизненным циклом subprocess). agent-ctl становится тонким gRPC client.
- SQLite: таблица step_failures (карантин нестабильных). Золотые a11y снимки в plan.json.
- CI: воркфлоу GitHub Actions — job 1: explore (если plan.json отсутствует или --force-explore); job 2: replay (матрица по тестовым целям).
- Выход: код выхода 2 (PARTIAL) для шагов в карантине; код выхода 1 для критических сбоев.
- СОБРАТЬ: gRPC server orchestrator, режим replay, логика карантина нестабильных, воркфлоу CI. ОТЛОЖИТЬ: report-service, OTel, экспорт codegen, visual fallback.
- Контрольная точка: plan.json закоммичен в репозиторий. Матрица CI запускает 3 параллельных replay запуска менее чем за 2 минуты каждый, все завершаются с кодом 0.

MILESTONE 4 — "Production-Observable" (Дни 31-45). Здесь выпускается v1.0.
- Go: report-service (run_report JSON+HTML, endpoint Prometheus /metrics). Переключить узел plan на Opus 4.8.
- Python: OTel инструментирование (один span на узел, OTLP экспорт). Попытка heal 3 (set-of-marks visual, только если PoC подтверждает точность).
- TypeScript: codegen_record() вызывается в узле act; codegen_stop() в конце запуска; .spec.ts отправляется в persistence-gateway.
- Выход: полный набор артефактов (report, trace, spec, baselines, healing report, cost). Дашборд Grafana из метрик Prometheus.
- СОБРАТЬ: report-service, OTel spans, интеграция codegen. BUY: Playwright codegen, структура Playwright HTML reporter, Grafana.
- Контрольная точка: один запуск на приложении из 10 страниц производит полный HTML отчёт, валидный .spec.ts, Playwright trace и метрику стоимости Prometheus — всё в рамках настраиваемого бюджета токенов.

ОТЛОЖИТЬ НА v1.5+:
- Visual grounding по set-of-marks (попытка 3) — собирать только если PoC (в M4) показывает > 70% точности на реальных приложениях.
- Миграция PostgreSQL — триггер: > 50 параллельных запусков с общей БД или необходимость в распределённых worker'ах.
- Параллельные browser contexts — триггер: продолжительность одного запуска > 10 мин на целевом приложении.
- Web дашборд для истории запусков — триггер: размер команды > 1 или необходимость в отчётности для заинтересованных сторон.
- LangGraph Cloud / remote checkpointing — триггер: необходимость запускать brain на отдельной инфраструктуре от orchestrator.
- Поддержка Windows — не является проблемой home-lab; откладывать бессрочно.

**Ключевые риски:**

- Нестабильность API surface официального сервера @playwright/mcp — имена инструментов и JSON-схема MCP tools нестабильны контрактно в v1.x. Митигация: зафиксировать точную версию npm в package.json; написать набор контрактных тестов, подтверждающих, что имена инструментов и входные схемы соответствуют ожиданиям; относиться к нарушению схемы инструмента как к ошибке сборки. Это риск с наибольшей вероятностью в v1.
- Перерасход токенов LLM в режиме explore — неограниченное исследование большого приложения (50+ страниц) может исчерпать бюджет Opus за один запуск. Митигация: жёсткое ограничение бюджета токенов перед каждым вызовом LLM в узле plan; по умолчанию 50k токенов Opus на запуск; настраивается для каждой цели в run config YAML; событие BUDGET_PLAN_EXCEEDED вызывает корректную деградацию до replay с частичным планом, а не аварийное завершение.
- Фрикция версионирования gRPC Go-Python — изменения proto требуют перегенерации stubs на обоих языках и их синхронизации. Без принуждения это молча ломается. Митигация: stubs proto генерируются в CI из единственного файла /proto; Go тест подтверждает, что хеш файла proto соответствует последним сгенерированным stubs; шаг CI Python делает то же самое. Несоответствие proto = ошибка сборки, а не неожиданность в runtime.
- Узкое место SQLite single-writer при параллельной матрице CI — если 10 jobs CI одновременно пишут в один agent.db, WAL mode помогает при чтении, но сериализация записи через persistence-gateway становится потолком пропускной способности. Митигация: для CI использовать отдельные SQLite файлы для каждого job (AGENT_DB_PATH=/tmp/agent-{run_id}.db); только долгосрочный home-lab сервис использует общую БД. Явно задокументировать триггер миграции на PostgreSQL: общая БД + > 50 параллельных записей в минуту.
- Рост checkpoint LangGraph — длинные runs explore с глубокими episodic buffers записывают большие блобы checkpoint в SQLite. Без обрезки agent.db растёт неограниченно. Митигация: episodic_buffer — ограниченная deque (максимум 50 событий, старейшие вытесняются); узел checkpoint удаляет checkpoints старее N запусков (настраивается, по умолчанию 10); добавить gc cron для удаления строк checkpoint завершённых запусков.
- Точность visual grounding по set-of-marks не подтверждена — это fallback попытки 3 и единственная возможность в дизайне, не валидированная на реальном приложении на момент написания. Митигация: откладывается на v1.5 и требует прохождения PoC в Milestone 4, измеряющего точность на 20 реальных сценариях со сломанными selectors, прежде чем принимается какая-либо производственная зависимость.

**ADR:**

- ADR-001: BUY официальный @playwright/mcp (npm-пакет, поддерживаемый Microsoft) vs BUILD кастомный TypeScript gRPC bridge. Отклонено: кастомный bridge. Официальный MCP server предоставляет все Playwright primitives (navigate, click, fill, accessibility_snapshot, screenshot, trace, codegen) как MCP tools, которые LangGraph ToolNode понимает нативно без какого-либо кода адаптации. Построение кастомного bridge означает владение схемой инструмента, JSON-RPC framing и отображением Playwright API — всё это официальный server предоставляет бесплатно. Принятый компромисс: зависимость от внешнего пакета, который может изменить API; митигировано фиксацией версии и контрактными тестами.
- ADR-002: MCP через stdio (subprocess JSON-RPC 2.0) vs gRPC для границы Python brain — TypeScript Playwright. Отклонено: gRPC. MCP stdio — нативный протокол инструментальных вызовов LLM; LangGraph ToolNode разрешает описания MCP tools в Python callable без слоя адаптера. gRPC потребовал бы: определения proto для каждой операции Playwright, поддержки TypeScript gRPC server, написания Python gRPC client и ручного соединения каждого вызова инструмента с форматом tool-call LangGraph. Stdio избегает выделения портов и service discovery в CI. Режим отказа stdio (EOF) проще обнаружить и восстановиться после него, чем после gRPC-соединения, которое может зависать.
- ADR-003: LangGraph явная state machine vs кастомный async agent loop (asyncio FSM в Python). Отклонено: кастомный loop. LangGraph предоставляет: SQLite checkpointing (возобновление после сбоя) в одну строку настройки, time-travel отладку (воспроизведение любого исторического состояния), встроенное прерывание/возобновление для human-in-loop gates и типизированную схему состояния, применяемую на границах узлов. Кастомный asyncio loop потребовал бы всего этого, построенного с нуля. Единственная цена: LangGraph — дополнительная зависимость, и его схема checkpoint непрозрачна. Приемлемый компромисс.
- ADR-004: Дерево доступности (ARIA snapshot) как основная модальность восприятия vs визуальные screenshots как основные. Отклонено: visual-primary. A11y tree: структурировано (roles, names, states, иерархия), только текст (нет затрат на vision model при каждом вызове perceive), детерминировано (одинаковый DOM = одинаковое дерево) и нативно запрашиваемо Playwright. Визуальное восприятие требует vision-capable модели при каждом вызове perceive (множитель стоимости ~10x на шаг). Visual fallback (set-of-marks) сохраняется как попытка 3 в healing для < 5% случаев, когда a11y tree разрежено (canvas, shadow DOM, кастомные элементы).
- ADR-005: Стратегия детерминизма explore-once-then-replay vs требование написанных человеком тестовых скриптов для CI. Отклонено: скрипты человека. Автономное исследование И ЕСТЬ основной дифференциатор по сравнению с существующим субагентом qa-automation-engineer, пишущим Playwright тесты. Требование человеческих скриптов как артефакта CI противоречит цели. Детерминизм достигается заморозкой плана исследования (plan.json, plan_hash) после первого автономного запуска explore, а не устранением автономии. Файл plan коммитится в репозиторий и становится воспроизводимым артефактом CI. Недетерминизм LLM изолирован в фазе explore, которая выполняется один раз.
- ADR-006: SQLite single-writer через Go persistence-gateway vs PostgreSQL для долгосрочного хранения. Отклонено: PostgreSQL для v1. SQLite в WAL mode с единственным writer'ом (persistence-gateway) — это zero-ops, нет сетевой зависимости и достаточно для ожидаемой нагрузки v1 (1-10 параллельных запусков, одна машина). PostgreSQL добавляет: отдельный процесс для управления, connection pooling, сетевую задержку при каждом вызове БД и операционную сложность в CI. Триггер миграции явен и задокументирован: > 50 параллельных запусков, пишущих в общую БД, или распределённые worker'ы на отдельных машинах. До срабатывания этого триггера сложность не оправдана.
- ADR-007: Sonnet 4.6 для self-healing re-grounding (попытки 2-3) vs Opus 4.8 для всех вызовов LLM. Отклонено: Opus для healing. Healing — ограниченная, структурированная задача: для известного element intent и текущего a11y tree найти наиболее подходящий selector. Это не требует глубоких рассуждений, которые Opus предоставляет для планирования исследования (который должен понять цель приложения, решить что тестировать и выстроить последовательность действий). Sonnet при ~5x меньшей стоимости на токен достаточен для структурированной задачи поиска selector. Разделение моделей: Opus для узла plan (творческий, высокие ставки, выполняется один раз за explore), Sonnet для узла heal (аналитический, ограниченный, может выполняться много раз за replay).

---


## Вердикты судей

Три состязательных судьи оценили все четыре предложения по шести измерениям. Обратите внимание: шкалы у судей различаются: `arch-soundness` оценивал каждое измерение по шкале 0–10 и выводит **TOTAL как сумму (максимум 60)**; `agentic-rigor` и `feasibility-cost` также оценивали каждое измерение по шкале 0–10, но выводят **TOTAL по шкале 0–10**. Все числа воспроизведены точно в соответствии с записью.

### `arch-soundness`

**Линза судьи.** Архитектурная состоятельность и обоснованность polyglot-границ. Я оценивал, зарабатывает ли каждый язык своё место или это «туризм», добавляющий операционную нагрузку; чисты ли межъязыковые контракты, версионированы ли они и имеют ли единственного владельца; реальна ли изоляция отказов между Go/Python/TS или лишь задекларирована; и решены ли принципиально сложные задачи (CI-детерминизм недетерминированного исследователя, состоятельность self-healing) или лишь декларативно обозначены. Я придавал большой вес вопросу «является ли эта граница действительно нагруженной» и снижал оценку за изобретение существующей инфраструктуры заново и за ложные утверждения об единственном владельце.

| Proposal | Fit | Polyglot boundary | Self-healing | CI determinism | Feasibility | Observability/cost | TOTAL |
|---|---|---|---|---|---|---|---|
| Hexagonal Polyglot Agent | 9 | 7 | 9 | 9 | 5 | 7 | 46 |
| CognitivePilot — Perception-First (agentic-core) | 8 | 6 | 7 | 6 | 5 | 7 | 39 |
| TrustFirst — Reliability/CI-Determinism | 9 | 7 | 9 | 10 | 5 | 8 | 48 |
| CognitivePilot — Pragmatic MVP→Scale | 8 | 9 | 7 | 8 | 9 | 8 | 49 |

**Обоснования (по предложениям):**

- **Hexagonal Polyglot Agent** (total 46): Наиболее формально строгий дизайн границ: версионированный proto-пакет, contracts/ как единственный источник истины в корне репозитория, один клиент/один сервер на каждую границу, явно перечисленные отклонённые альтернативы. Self-healing — сильнейший из четырёх: амортизация через dom_hash pre-patching (LLM платится один раз, результат переиспользуется до дрейфа DOM), автоматическое вытеснение устаревших локаторов, режим воспроизведения heal_audit, хеш в рамках сценария для борьбы с хрупкостью на уровне всей страницы. Детерминизм отличный (воспроизведение без LLM, аварийный выход через закрепление plan_id, golden baselines, карантин). Однако polyglot-границы тяжелее, чем оправдано: (1) система СТРОИТ собственный 8-инструментный Playwright MCP server, хотя официальный @playwright/mcp от Microsoft существует — самодельная поверхность обслуживания, привязанная к нестабильному Playwright API = языковой туризм; (2) центральное утверждение «Go — единственный владелец БД» является ЛОЖНЫМ, поскольку LangGraph checkpointer пишет в SQLite/Postgres напрямую из Python — два писателя, две схемы, что подрывает hexagonal-тезис; (3) синхронный вызов gRPC BudgetService.ConsumeTokens перед каждым вызовом LLM — это межпроцессный round trip и источник отказов для того, что логически является внутрипроцессным счётчиком. 17 компонентов / 5 gRPC-сервисов / OTel везде для homelab — избыточная позолота. Чисто на бумаге, дорого в эксплуатации.
- **CognitivePilot — Perception-First (agentic-core)** (total 39): Связная концепция agent-loop и хороший инстинкт приватности (prompt_hash вместо контента в spans, verify-before-accept через probe resolve_locator). Однако слабейший по моей линзе. Три wire-протокола (gRPC bidi + MCP stdio + REST). Снова самодельный 12-инструментный MCP server (тот же туризм, что в #1). Та же ложная имплицитная история о единственном владельце (LangGraph checkpointer совместно пишет в БД). Модель уверенности при healing опирается на выдуманные discount-константы (×0.95/×0.90/×0.85), самостоятельно признанные заглушками. Наиболее разрушительно для детерминизма: устаревание плана АВТОМАТИЧЕСКИ запускает новый цикл исследования, который заменяет замороженный plan_hash в персистентности — CI-шлюз молча мутирует сам себя, прямо противореча контракту explore-once. Зависит от зрелости langchain-mcp (признано, с резервным вариантом). Надёжно, но границы и самомутирующий план снижают оценку.
- **TrustFirst — Reliability/CI-Determinism** (total 48): Чемпион по детерминизму и наиболее интеллектуально честный в вопросах персистентности. Проверка целостности plan_hash с ПРЕРЫВАНИЕМ при несоответствии (exit 3); неизменяемые golden baselines, обновляемые ТОЛЬКО через явную команду agentctl baseline update («CI не может изменить собственные baselines» — лучшая идея детерминизма во всём наборе); структурированные коды завершения 0/1/2/3; обнаружение дрейфа версии AUT с настраиваемой политикой; screenshot-hash наряду с a11y-hash для обнаружения визуальных регрессий (CSS/вёрстка), которые упускает диффинг, слепой к a11y. Healing отличный: append-only healing_audit (без UPDATE/DELETE), CI-безопасный АСИНХРОННЫЙ human gate через pause checkpoint (выживает при таймаутах pipeline), команда calibrate. Принципиально честный момент: LangGraph checkpointer использует ОТДЕЛЬНЫЙ файл SQLite от store-gateway, так что контракт о единственном писателе здесь действительно соблюдается. Стоимость границ по-прежнему высока — три протокола, самодельный 12-инструментный MCP server (туризм), UDS gRPC и самая тяжёлая сборка (14 недель, большая CLI-поверхность). Детерминизм и надёжность — лучшие в классе; feasibility и решение строить собственный MCP снижают итоговую оценку.
- **CognitivePilot — Pragmatic MVP→Scale** (total 49): Лучший ответ по моей линзе: единственное предложение, отказывающееся от языкового туризма. Оно ПОКУПАЕТ официальный @playwright/mcp (единственный самодельный TS — тонкий sidecar для публикации артефактов), что является верным обоснованием TS-границы — Playwright нативен для Node, и официальный MCP server существует. Оно честно признаёт, что Go control plane не является нагруженным с первого дня: M0–M1 используют subprocess+env vars, gRPC/proto появляются лишь на M2–M3, когда появляется что контрактовать. Используются отдельные файлы SQLite на каждый запуск для CI parallelism, и явно заданы триггеры миграции на PostgreSQL вместо предварительного построения инфраструктуры. Каждое решение build-vs-buy и каждый перенос названы вместе с условием-триггером. Детерминизм надёжен (plan_hash, golden snapshots, карантин), но немного мягче, чем у #3: --force-replay и опциональный auto-commit самоизлечённого plan.json могут при неправильном использовании подрывать гарантию замороженного плана, а abort-on-hash-mismatch по умолчанию не активирован. Self-healing переиспользует общую иерархию, но откладывает недоказанный визуальный fallback set-of-marks за PoC-шлюз (честно, но с меньшей детализацией амортизации, чем у #1). Жёсткая зависимость от стабильности tool-schema @playwright/mcp — главный риск (признан, снижен version pin + contract tests). Наименьшая операционная нагрузка, наибольшие шансы быть выпущенным, наичистейшие обоснованные границы — немного опережает #3 по линзе polyglot/soundness.

**Лучшие элементы:**

- #3 (TrustFirst): неизменяемые golden baselines, обновляемые ТОЛЬКО через явную команду оператора — делает «CI молча изменил собственный baseline» структурно невозможным; применять совместно с явным закреплением plan_id из #1 для релизных веток.
- #3: проверка целостности plan_hash, ЖЁСТКО ПРЕРЫВАЮЩАЯ воспроизведение при несоответствии (exit code 3), так что вручную отредактированный или частично исправленный замороженный план никогда не выполнится незаметно.
- #3: структурированная семантика CI exit-code (0 — успех / 1 — ошибка шага / 2 — golden-diff регрессия / 3 — нарушение целостности или бюджета) — даёт pipeline реальную гранулярность сигнала.
- #3: screenshot-hash фиксируется наряду с a11y-hash для обнаружения чисто визуальных (CSS/вёрстка) регрессий, которых не видит чистый a11y-tree диффинг.
- #3: асинхронный human gate реализован как пауза checkpoint в LangGraph + CLI-разрешение, так что heal с низкой уверенностью не блокирует (и не даёт истечь таймауту) CI-задачу.
- #4 (Pragmatic): ПОКУПКА официального @playwright/mcp вместо строительства собственного TS MCP server — устраняет наибольшую стоимость поверхности обслуживания/туризма, общую для остальных трёх.
- #4: откладывать gRPC/proto до появления реального контракта (сначала subprocess+env), называя каждый перенос явным условием-триггером — дисциплинированная эволюция вместо спекулятивной инфраструктуры.
- #4: отдельные файлы SQLite на каждый запуск для CI parallelism с задокументированным триггером миграции на PostgreSQL вместо предварительного решения задач масштабирования.
- #1 (Hexagonal): dom_hash pre-patching исправленного локатора с автоматическим вытеснением устаревших — амортизирует стоимость LLM healing по запускам, пока DOM реально не изменится.
- #1: append-only healing-audit JSONL, публикуемый как CI-артефакт для асинхронной проверки человеком (независимо присутствует также в #3 и #4).
- #2 (Perception-First): verify-before-accept — проверка исправленного локатора через resolve_locator в live DOM перед его фиксацией/сохранением, что предотвращает загрязнение хранилища уверенно ошибочным селектором.

**Критические недостатки:**

- #1 и #2 утверждают «Go persistence-gateway — единственный владелец БД», однако LangGraph checkpointer пишет в SQLite/Postgres напрямую из Python — два писателя и две схемы в одном хранилище. Центральный тезис hexagonal/единственного владельца опровергается выбранным checkpointer-ом. (#3 и #4 избегают этого, используя отдельный файл checkpoint DB.)
- #1, #2, #3 все СТРОЯТ собственный 8–12-инструментный Playwright MCP server, хотя официальный @playwright/mcp от Microsoft существует. Это наиболее явная стоимость языкового туризма: самодельный Node-сервис, единственная задача которого — переэкспортировать Playwright, связывая нестабильный Playwright API с вручную написанным кодом на самой хрупкой границе.
- #2: устаревание плана АВТОМАТИЧЕСКИ запускает новый цикл исследования, перезаписывающий замороженный plan_hash — CI-шлюз мутирует сам себя недетерминированно, прямо нарушая гарантию explore-once-replay-many, на которой построено предложение.
- #1: синхронный вызов gRPC BudgetService.ConsumeTokens перед каждым единственным вызовом LLM превращает внутрипроцессный счётчик в межпроцессный round trip с собственным режимом отказа/задержки; контроль бюджета на уровне control-plane границы избыточно усложнён.
- Сквозное: Go control plane в #1–#3 является лишь частично нагруженным (надзор за процессами + счётчик бюджета + DB-прокси, дублирующий checkpointer). Бо́льшая его часть могла бы быть тонким Python-супервизором, так что часть операционной нагрузки третьего языка + gRPC не оправдана — #4 неявно признаёт это, откладывая весь уровень Go/gRPC.
- #3 — самый тяжёлый для сборки (14 недель, большая CLI-поверхность, три wire-протокола) для цели homelab/одна команда — реальный риск, что он никогда не дойдёт до важных частей.
- #4: жёсткая runtime-зависимость от стабильности tool-schema @playwright/mcp (признано), а опциональный auto-commit самоизлечённого plan.json может молча подрывать гарантию детерминизма замороженного плана, если включён в CI без проверки.

---

### `agentic-rigor`

**Линза судьи.** Self-healing и agentic rigor — скептически отношусь к магическим числам уверенности, непроверенному re-grounding и ручному размахиванию флагом `exploration_complete`. Я поощряю предложения, в которых: (a) каждый healing-кандидат проверяется против live DOM перед доверием к нему; (b) сигнал уверенности основан на чём-то измеримом (per-strategy priors, эмпирические discount-коэффициенты, калибровка по верифицированным человеком результатам), а не на самооценке LLM; (c) CI-детерминизм обеспечивается механически (целостность хеша, неизменяемые baselines), а не просто декларируется; (d) циклы ограничены явными caps. Я снижаю оценку за магические пороги без пути калибровки, за размытое владение компонентами и за зависимости от tool-поверхностей, которые могут не существовать.

| Proposal | Fit | Polyglot boundary | Self-healing | CI determinism | Feasibility | Observability/cost | TOTAL |
|---|---|---|---|---|---|---|---|
| Hexagonal Polyglot Agent | 8 | 9 | 6.5 | 8 | 5.5 | 7 | 7.3 |
| CognitivePilot — Perception-First | 8 | 7 | 8.5 | 7.5 | 6 | 7.5 | 7.4 |
| TrustFirst — Reliability/CI-Determinism | 9 | 8 | 8 | 9.5 | 5.5 | 7.5 | 7.9 |
| CognitivePilot — Pragmatic MVP→Scale | 7.5 | 8.5 | 6.5 | 8 | 9 | 8 | 7.8 |

**Обоснования (по предложениям):**

- **Hexagonal Polyglot Agent** (total 7.3): Механика re-grounding надёжна: locator-resolver повторно проверяет исправленного кандидата как LIVE перед возвратом и явно трактует «validates-then-fails-on-act» как новый healing-цикл (одно из немногих предложений, закрывающих этот пробел). Амортизация на основе dom_hash (переиспользовать исправленный локатор только пока dom_hash_after совпадает, автоматически вытеснять устаревшие) — наиболее чистая история персистентности. Однако оценка уверенности — это чистая самооценка LLM, парсимая из JSON, ограниченная магическими порогами 0.85/0.60 БЕЗ плана калибровки и без эмпирических discount-коэффициентов — доверие к healing предполагается, а не измеряется. Это центральный пробел в строгости. Границы — сильнейшие из четырёх (версионированные hexagonal ports, Go как единственный владелец БД, закрепление major-версии proto). Feasibility — слабейшая: 18 компонентов, 5 gRPC-сервисов, трение от proto-версионирования, которое сами авторы обозначают как постоянный налог.
- **CognitivePilot — Perception-First** (total 7.4): Наиболее строгий re-grounding из четырёх. final_confidence = максимум из трёх попыток, каждая с измеримым prior: базовые оценки по стратегии (testid 1.00 → xpath 0.45), эмпирический changed-DOM discount (x0.95), LLM-overconfidence discount (x0.90), visual discount (x0.85) — и критически важно: каждое LLM/visual-предложение re-probe-ируется через resolve_locator перед принятием (обнуляет уверенность, если не live). Единственное предложение, которое прямо называет проблему калибровки и планирует labeled-breakage калибровку в Phase 4 вместе с шагом post-heal verification. Слабые стороны: discount-факторы признаны заглушками; auto-triggered re-explore при устаревании плана может создать feedback loop стоимости на нестабильных приложениях; владение set-of-marks размыто — модуль восприятия Python «добавляет логику пронумерованного overlay», тогда как TS set-of-marks-renderer также рисует метки — неоднозначная граница, которая вызовет интеграционные трудности.
- **TrustFirst — Reliability/CI-Determinism** (total 7.9): Сильнейший CI-детерминизм с большим отрывом — и он механический, а не декларативный: проверка целостности plan_hash, ПРЕРЫВАЮЩАЯ воспроизведение (exit 3) при несоответствии; неизменяемые замороженные планы (новое исследование = новый plan_id, без правки на месте); golden baselines, обновляемые ТОЛЬКО через явную команду agentctl baseline update (делающей «тесты перезаписали собственный baseline» структурно невозможным); AUT-commit-SHA-gated flake quarantine (лучшая идея во всём наборе — отличает настоящую регрессию от флакования окружения, требуя отсутствия изменения SHA); чистый exit-code контракт (0/1/2/3). Self-healing надёжен: L1–L4 fast path без LLM с probe wait_for_selector перед фиксацией, неизменяемый append-only healing_audit, локаторы, верифицированные человеком, повышаются до приоритета L1, и команда agentctl healing calibrate, вычисляющая precision/recall прошлых auto-heals. Штраф: эта калибровка имеет cold-start-зависимость от накопленных human_verified результатов, которые могут не появиться на начальном этапе; а 14 недель / 12 компонентов — это тяжело.
- **CognitivePilot — Pragmatic MVP→Scale** (total 7.8): С большим отрывом наиболее осуществимое и наиболее интеллектуально честное в вопросах неопределённости: явные BUY-vs-BUILD шлюзы, milestone-триггеры переноса, названные явно, а не скрытые; визуальный set-of-marks grounding ОТЛОЖЕН за measured PoC с точностью >70% в M4. Per-job SQLite для CI parallelism — прагматичный выигрыш в детерминизме. Self-healing переиспользует ту же 3-попытную иерархию с locator-cache-hit = нулевая стоимость LLM, но уверенность — это LLM-самооценка плюс method-prior БЕЗ механизма калибровки за пределами audit log — слабее P2/P3. Два слабых места по feasibility вокруг healing/output path: опирается на то, что официальный @playwright/mcp экспортирует trace_start/stop, codegen_record/stop и coordinate clicks как MCP tools — авторы отмечают «проверить, поддерживает ли Playwright MCP coordinate clicks», что является именно этим риском: tool-поверхность официального сервера может не совпадать с заявлениями о visual-heal и codegen-export, а риск #1 в дизайне (нестабильность MCP schema) лежит прямо на healing visual fallback. Детерминизм сильный, но мягче P3: --force-replay может по умолчанию обходить проверку хеша.

**Лучшие элементы:**

- AUT-commit-SHA-gated flake quarantine из TrustFirst: шаг засчитывается в flaky/regression только если он проваливается N из 5 БЕЗ изменения SHA AUT — единственный механизм в наборе, который чисто разделяет настоящую регрессию (exit 2) от флакования окружения (карантин, без блокировки). Включать напрямую.
- ABORT по целостности plan_hash из TrustFirst при воспроизведении + baselines, обновляемые только через явную команду оператора. Делает «агент молча перезаписал собственный CI baseline» структурно невозможным — центральная гарантия доверия.
- Измеримая модель уверенности из Perception-First: per-strategy base priors (testid→xpath) в сочетании с эмпирическими discounts (changed-DOM, LLM-overconfidence, visual) и обязательной re-probe каждого предложенного локатора против live DOM перед принятием — уверенность становится обоснованной, а не самодекларируемой.
- Запланированная калибровка уверенности из Perception-First по labeled breakage set + шаг post-heal verification + метрика healing_confidence_histogram: единственный способ, при котором порог auto-accept 0.85 перестаёт быть магическим числом.
- dom_hash-scoped амортизация из Hexagonal: сохранять исправленный локатор с ключом dom_hash_after, автоматически вытеснять и пере-healить при дрейфе структурного хеша страницы — платить LLM один раз, безопасно переиспользовать, закрываться при дрейфе. Сочетать с собственным risk-mitigation этого предложения: хешировать целевое поддерево сценария (а не всю страницу), чтобы избежать инвалидации всей страницы из-за несвязанного рекламного баннера.
- Версионированные hexagonal ports из Hexagonal + Go как единственный владелец БД: наичистейшая polyglot-граница, позволяющая заменить Python или TS без какой-либо миграции схемы.
- BUY-first milestone gating из Pragmatic с явными defer-триггерами, per-job SQLite для CI parallelism и вынос непроверенного visual grounding за measured PoC вместо предположения, что это работает.
- Set-of-marks, в котором overlay хранит карту mark→DOM-element (window.__somarks в TrustFirst, index_map в Perception-First), так что heal извлекает реальный семантический локатор из отмеченного узла — значительно надёжнее, чем coordinate-based click из Pragmatic, который хрупок к viewport/DPR.

**Критические недостатки:**

- СКВОЗНОЕ: ни одно из четырёх на самом деле не доказывает, что исследование СХОДИТСЯ. Все четыре завершаются по флагу exploration_complete, заявленному LLM, плюс depth/budget cap. Это ограничение, а не сходимость — LLM может объявить «готово» преждевременно (пробел в покрытии) или никогда (спасаясь только budget cap). Ни одно предложение не определяет измеримую цель покрытия (например, долю интерактивных элементов sitemap, которые были задействованы) как реальное условие завершения. Это самое значительное общее рукомахание.
- P1 (Hexagonal): healing confidence — это чистая самооценка LLM по магическим порогам 0.85/0.60 с нулевым механизмом калибровки. Auto-accepted heals (>=0.85) будут молча записывать неправильные локаторы в персистентное хранилище в момент, когда модель переоценена, и нет feedback loop для обнаружения этого. Дизайн амортизации затем распространяет этот плохой локатор на все последующие запуски. Высокая серьёзность для системы, весь pitch которой — надёжный CI.
- P2 (Perception-First): устаревание плана auto-triggers новый цикл исследования LLM; для еженедельно деплоящегося приложения это создаёт неконтролируемый Opus cost feedback loop, а discount-факторы, ограничивающие auto-accept, явно являются заглушками. К тому же ответственность за set-of-marks размыто распределена между Python perception module и TS set-of-marks-renderer — граница, которая не выживет при встрече с реализацией.
- P3 (TrustFirst): история с калибровкой (agentctl healing calibrate, вычисляющий precision/recall vs human_verified) имеет cold-start-зависимость — поначалу нет human_verified результатов, поэтому порог auto-accept работает без калибровки именно тогда, когда локальное хранилище наполняется наиболее важными записями. Mitigation требует накопления объёма проверок человеком, который дизайн не предусматривает.
- P4 (Pragmatic): путь visual-heal (coordinate clicks) и вывод codegen/.spec.ts оба предполагают, что официальный @playwright/mcp экспортирует инструменты (codegen_record/stop, coordinate click, trace как MCP tools), которые могут не существовать в реальной поверхности этого сервера — сами авторы отмечают «проверить, поддерживает ли Playwright MCP coordinate clicks». Признанный риск #1 дизайна (нестабильность MCP schema) ложится прямо на два нагруженных функционала. Если эти инструменты отсутствуют, visual fallback и первичный deliverable всё равно потребуют bespoke TS, разрушая BUY-first тезис именно для самых сложных частей.

---

### `feasibility-cost`

**Линза судьи.** Feasibility, CI-детерминизм и стоимость. Я оцениваю осуществимость для небольшой команды (реальный LOC и поверхность компонентов, а не амбициозные диаграммы), реальную CI-воспроизводимость недетерминированного LLM-исследователя (действительно ли недетерминизм изолирован и ограничен ли healing в replay hot path?), реализм токенов/стоимости на запуск (избегает ли дизайн стоимости LLM на happy path и ограничивает ли фазу исследования?) и риск избыточной инженерии в v1 (строит ли то, что мог бы купить, и поставляет ли ценность прежде инфраструктуры?). Все измерения: выше = лучше; высокий observabilityCost = observability соразмерна/дёшева, а не позолочена.

| Proposal | Fit | Polyglot boundary | Self-healing | CI determinism | Feasibility | Observability/cost | TOTAL |
|---|---|---|---|---|---|---|---|
| Hexagonal Polyglot Agent — Versioned Ports Across Three Bounded Contexts | 9 | 9 | 8 | 8 | 4 | 5 | 6.5 |
| CognitivePilot — Perception-First Autonomous UI Test Agent | 8 | 7 | 8 | 7 | 5 | 5 | 6.3 |
| TrustFirst — Reliability/CI-Determinism Autonomous UI Agent | 9 | 8 | 8 | 10 | 5 | 6 | 7.6 |
| CognitivePilot — Pragmatic MVP → Scale Evolution | 8 | 8 | 7 | 8 | 9 | 9 | 8.3 |

**Обоснования (по предложениям):**

- **Hexagonal Polyglot Agent — Versioned Ports Across Three Bounded Contexts** (total 6.5): Архитектурно наиболее строгое: версионированные ports, единственный владелец БД, чистое explore-once/replay-many с закреплением версии плана и LLM-free replay (нулевая стоимость токенов на happy path — по-настоящему сильно). Исправление dom_hash-scoped-to-subtree в keyRisks — лучшая идея healing-стабильности в наборе. Однако по моей линзе это наиболее избыточно сконструированный v1: 17 компонентов, 5 мультиплексированных gRPC-сервисов, вручную созданный 8-инструментный MCP server, два версионированных каталога контрактов и OTel/Prometheus/transcript с первого дня. Небольшая команда платит полный налог определения контрактов до того, как даже достигнет healing (фаза 5 из 7, ~9 недель). Трение от proto-версионирования, которое оно признаёт, реально и повторяющееся. Пересобирает официальный Playwright MCP server без каких-либо выгод. Надёжный дизайн, неверный профиль стоимости/feasibility для v1.
- **CognitivePilot — Perception-First Autonomous UI Test Agent** (total 6.3): Шлюз completeness_ratio (<0.30) для выбора perception modality — лучшая идея контроля стоимости vision-токенов, а откладывание vector store — дисциплинированное решение. Healing продуманный (3 попытки, эмпирические discounts). Но две вещи по моей линзе вредят: (1) авто-регенерация плана при обнаружении устаревания непредсказуемо умножает стоимость Opus исследования и повторно вносит недетерминизм в pipeline, который должен быть стабильным — это частично разрушает гарантию explore-once; (2) discount-факторы уверенности (0.90/0.85) — непроверенные заглушки, замаскированные под принципиальный подход — риск калибровки признан, но шлюз auto-accept всё равно выходит в релиз. Также строит самодельный 12-инструментный MCP server (план 13 недель). Надёжный agentic core, посредственный реализм стоимости.
- **TrustFirst — Reliability/CI-Determinism Autonomous UI Agent** (total 7.6): Лучший в классе по единственной сложнейшей проблеме: превращению недетерминированного исследователя в надёжного CI-гражданина. Проверка целостности plan_hash с жёстким прерыванием, обновление immutable golden-baseline только через явную команду (никогда auto — устраняет сбой «тесты изменили собственный baseline»), обнаружение дрейфа AUT git-SHA, структурированные коды завершения 0/1/2/3, sliding-window flake quarantine, распространение gRPC deadline для per-step timeout и cap на 2 попытки heal, ограничивающий дисперсию LLM-задержки в hot path. L1–L4 fast path локатора без LLM и жёсткий бюджетный потолок на стороне Go (независимый от счётчика Python) — отличные средства контроля стоимости. Асинхронный human gate через checkpoint pause по-настоящему CI-безопасен. Снижен по feasibility: всё ещё пересобирает самодельный 12-инструментный MCP server и является 14-недельной сборкой. Если бы он купил официальный MCP server, был бы явным победителем.
- **CognitivePilot — Pragmatic MVP → Scale Evolution** (total 8.3): Уверенно побеждает по моей линзе. Единственное архитектурное озарение, которое упускают остальные трое: КУПИТЬ официальный @playwright/mcp server и использовать встроенный ToolNode + SqliteSaver LangGraph — устраняя единственный наибольший самодельный компонент (custom MCP server), который предложения 1/2/3 все напрасно пересобирают. Milestone gates с явно названными defer-триггерами (PostgreSQL, visual fallback, parallel contexts) напрямую атакуют риск избыточной инженерии. Observability и gRPC накладываются на проверенный wire, а не проектируются заранее. Жёсткие token caps с graceful degradation (не abort) — наиболее реалистичная история стоимости. Детерминизм надёжен (plan_hash, golden a11y, карантин), хотя и немного уступает TrustFirst. Два реальных недостатка не дают достичь совершенства: жёсткая зависимость от нестабильной официальной MCP tool schema (его собственный риск #1) и plan self-update + auto-commit при heal могут молча мутировать детерминированный артефакт. Наилучшая основа; привить к ней строгость детерминизма из TrustFirst.

**Лучшие элементы:**

- КУПИТЬ официальный Microsoft @playwright/mcp server + LangGraph ToolNode/SqliteSaver вместо ручной сборки 8–12-инструментного MCP server (Proposal 4) — единственный наибольший выигрыш в feasibility и стоимости; предложения 1/2/3 все пересобирают это без какой-либо пользы
- проверка целостности plan_hash с жёстким прерыванием при несоответствии + обновление immutable golden-baseline только через явную команду оператора, никогда auto (Proposal 3) — закрывает дыру «CI мутировал собственный baseline», подрывающую детерминизм
- L1–L4 fast path ротации стратегии локаторов без LLM перед любым LLM heal вызовом (Proposal 3) — большинство heals стоят ноль токенов; в сочетании с LLM-free replay happy path (Proposal 1) это делает стоимость на запуск почти нулевой на стабильных запусках
- структурированные CI exit codes (0/1/2/3), sliding-window flake quarantine, обнаружение дрейфа AUT git-SHA и распространение gRPC deadline для per-step timeout (Proposal 3) — ограничивают дисперсию LLM-задержки в replay hot path
- порог a11y completeness_ratio для определения, вызывается ли vision/set-of-marks вообще (Proposal 2) — откладывает дорогостоящие image-токены только для страниц, которые в них нуждаются
- dom_hash, вычисляемый по целевому поддереву сценария, а не по всей странице (Proposal 1 keyRisks) — предотвращает аннулирование каждого исправленного локатора из-за несвязанных изменений DOM
- Milestone gates с явно названными defer-триггерами и BUILD-vs-BUY, определяемым на каждом шлюзе (Proposal 4) — структурная защита против избыточной инженерии v1
- асинхронный human gate через LangGraph checkpoint pause с CI auto-skip timeout (Proposals 3/4) — человек в контуре без нарушения временных лимитов CI-задачи
- жёсткий бюджетный потолок на стороне Go, применяемый независимо от Python-счётчика токенов (Proposal 3) — надёжный контроль стоимости даже при ошибочном подсчёте мозгом

**Критические недостатки:**

- Proposals 1, 2 и 3 все вручную строят самодельный Playwright MCP server (8–12 инструментов), хотя официальный @playwright/mcp от Microsoft существует и является естественным LangGraph ToolNode-таргетом — большая напрасная сборка + постоянное бремя обслуживания при обновлениях Playwright. Proposal 1 — худший в целом (17 компонентов, 5 gRPC-сервисов, 2 каталога контрактов) и достигает self-healing лишь в фазе 5 из 12-недельного плана, так что основной дифференциатор проверяется последним.
- Авто-регенерация плана из Proposal 2 при обнаружении устаревания повторно вносит неограниченную стоимость Opus исследования и LLM-недетерминизм в pipeline, который должен быть заморожен — это частично разрушает собственную гарантию explore-once; его discount-константы уверенности (0.90/0.85) — непроверенные заглушки, выпускаемые за шлюзом auto-accept.
- Proposal 4 принимает жёсткую зависимость от официальной @playwright/mcp tool schema, которая не является контрактно стабильной в v1.x (это обозначено как собственный риск #1) — breaking изменение upstream останавливает весь стек; а plan self-update + auto-commit при heal могут молча мутировать детерминированный артефакт plan.json, подрывая гарантию детерминизма, если не ограничено очень строго.
- Общее для всех четырёх: точность visual grounding set-of-marks заявлена, но никогда не проверена на реальных сценариях со сломанными селекторами — только Proposal 4 выносит это за PoC; и healing, вызываемый в replay hot path, повторно вносит дисперсию LLM-задержки/стоимости, которую строго ограничивает только Proposal 3 (cap на 2 попытки + deadline + auto-skip). Proposals 1 и 2 оставляют время выполнения CI и стоимость на запуск уязвимыми для heal-storm blowup при нестабильном AUT.

---

## Маршрут решения при синтезе

Ведущий синтезатор вывел: **Sentinel — Autonomous Self-Healing Playwright Testing Agent (Go spine / Python LangGraph brain / official Playwright MCP hands)**.

Полные `chosenApproach` и `executiveSummary` из синтеза воспроизведены ниже. (Помните об отмеченном выше развороте ADR-001 BUY→BUILD — текст ниже предшествует этому изменению.)

### Выбранный подход

ОСНОВНОЕ: Proposal 4 (CognitivePilot — Pragmatic MVP→Scale, lens=pragmatic-evolution). Побеждает по измерениям feasibility (9) и polyglot-cleanliness (9) у всех трёх судей и является единственным предложением, отказывающимся от языкового туризма. Его скелет принят дословно: Go CLI → порождает Python brain → brain управляет ОФИЦИАЛЬНЫМ @playwright/mcp subprocess через MCP tool integration LangGraph; SQLite через single-writer Go gateway; milestone gates с явно названными defer-триггерами; per-job SQLite для CI parallelism; жёсткие token caps с graceful degradation.

ПРИВИТО ИЗ P3 (TrustFirst, чемпион по детерминизму, ciDeterminism 9–10 у всех судей): весь trust layer — (1) проверка целостности plan_hash, ЖЁСТКО ПРЕРЫВАЮЩАЯ воспроизведение при несоответствии (exit code 3); (2) immutable golden baselines, обновляемые ТОЛЬКО через явную команду `agentctl baseline update`, никогда auto (структурно предотвращает «CI перезаписал собственный baseline»); (3) AUT git-SHA-gated flake quarantine (шаг засчитывается в flaky/regression только если проваливается N из 5 БЕЗ изменения SHA AUT — единственный наичистейший способ разделить настоящую регрессию и флакование окружения); (4) структурированные CI exit codes 0/1/2/3; (5) screenshot-hash фиксируется наряду с a11y-hash для обнаружения чисто визуальных (CSS/вёрстка) регрессий, которые упускает a11y-blind диффинг; (6) асинхронный human gate как LangGraph checkpoint pause с CI auto-skip timeout; (7) append-only healing_audit (без UPDATE/DELETE); (8) жёсткий бюджетный потолок на стороне Go, применяемый независимо от Python-счётчика.

ПРИВИТО ИЗ P2 (Perception-First, наилучшая строгость self-healing, score 8.5): обоснованная модель уверенности — per-strategy base priors (testid 1.00 → xpath 0.45), эмпирические discounts (changed-DOM ×0.95, LLM-overconfidence ×0.90, visual ×0.85), ОБЯЗАТЕЛЬНЫЙ verify-before-accept (каждый LLM/visual-кандидат re-probe-ируется против live DOM, и его уверенность обнуляется, если не live), запланированная калибровка по labeled-breakage set, метрика healing_confidence_histogram и шлюз completeness_ratio (<0.30), определяющий, тратятся ли vision-токены вообще.

ПРИВИТО ИЗ P1 (Hexagonal, наилучшая амортизация): dom_hash-scoped pre-patching исправленного локатора с автоматическим вытеснением устаревших — платить LLM один раз, переиспользовать исправленный локатор до дрейфа структурного хеша, затем пере-healить. Применяется с собственным keyRisk-исправлением P1: хешировать целевое ПОДДЕРЕВО сценария, а не всю страницу, чтобы несвязанные реклама/баннер не аннулировали каждый локатор.

НОВОЕ (закрывает сквозное рукомахание, названное Судьёй 2): сходимость исследования — это ИЗМЕРИМАЯ цель покрытия, а не LLM-флаг.

ОТБРОШЕНО: (a) вручную построенные custom MCP servers (P1/P2/P3) — заменены официальным @playwright/mcp; (b) авто-re-explore при устаревании плана из P2, перезаписывающий замороженный plan_hash — re-explore ВСЕГДА является явным действием оператора, порождающим НОВЫЙ plan_id; (c) ложное «Go — единственный владелец БД» из P1/P2 — checkpointer получает собственный файл БД; (d) синхронный gRPC ConsumeTokens перед каждым LLM-вызовом из P1; (e) `--force-replay` по умолчанию из P4 и молчаливый auto-commit самоизлечённых планов — hash-abort по умолчанию, изменения исправленного плана публикуются как PR-артефакт для проверки; (f) позолота из P1 с 17 компонентами / 5 gRPC-сервисами / OTel с первого дня — отложено за milestone-триггеры.

### Исполнительное резюме

Sentinel — это автономное agentic-приложение с поддержкой headless-режима, которое самостоятельно исследует веб-интерфейс, решает, что тестировать, замораживает детерминированный план и самостоятельно исправляет сломанные локаторы при дрейфе DOM. Оно соблюдает polyglot lock: Go является control-plane spine (CLI, оркестратор, persistence gateway, report service), Python — LangGraph brain (perception → plan → act → verify → heal), а TypeScript — руки браузера через ОФИЦИАЛЬНЫЙ Microsoft @playwright/mcp server (купленный, а не построенный). Выбранный дизайн берёт Proposal 4 (Pragmatic MVP→Scale) в качестве структурной и feasibility-основы — его единственное важнейшее озарение, повторённое всеми тремя судьями: КУПИТЬ официальный Playwright MCP server + LangGraph ToolNode/checkpointer вместо ручной сборки 8–12-инструментного MCP server (наибольший напрасный самодельный компонент в P1/P2/P3). На этот скелет привиты: trust-and-determinism layer TrustFirst (P3) целиком (plan_hash hard-abort при воспроизведении, immutable golden baselines, обновляемые только явной командой оператора, AUT git-SHA-gated flake quarantine, структурированные exit codes 0/1/2/3, dual a11y-hash + screenshot-hash baselines, async human gate через LangGraph checkpoint pause); обоснованная модель уверенности Perception-First (P2) (per-strategy priors, эмпирические discounts и обязательный verify-before-accept live-DOM probe каждого исправленного локатора плюс запланированная калибровка по верифицированным человеком результатам); и dom_hash-scoped амортизация исправленных локаторов Hexagonal (P1) с auto-eviction, исправленная согласно собственным keyRisks для хеширования целевого поддерева сценария вместо всей страницы. Отброшены обозначенные судьями критические недостатки: самомутирующий план (авто-re-explore из P2, перезаписывающий замороженный plan_hash), ложное утверждение об единственном владельце БД (решено сохранением LangGraph checkpointer в ОТДЕЛЬНОМ файле БД), синхронный per-call gRPC budget round-trip (заменён внутрипроцессным счётчиком с Go-side hard-ceiling reconciliation), магические пороги уверенности, выпускаемые без пути калибровки, и избыточная инженерия v1 (gRPC/proto и полный Go-уровень отложены за milestone-триггеры). Также закрывается единственное сквозное рукомахание, общее для всех четырёх предложений: завершение исследования переопределяется от LLM-заявленного флага «exploration_complete» к измеримой цели покрытия (доля обнаруженных интерактивных элементов, которые были задействованы, + пустая навигационная граница), при этом budget cap служит страховкой, а не основным условием останова.

