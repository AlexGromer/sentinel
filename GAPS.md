# GAPS — Sentinel

> 🌐 **Русский** (основная версия) · [English](GAPS.en.md)

Отслеживание открытых вопросов, пунктов для верификации и известных рисков. Формат: `GAP-[CAT]-[NUM]`.
Категории: ARCH (архитектура), VERIFY (требует проверки фактов), RISK, AGENT (отсутствующий инструментарий), DECISION (ожидание решения пользователя).

> Источник: синтез workflow дизайна (2026-06-23) + согласование ограничений BUILD-ONLY.

---

## Пробелы в решениях / ограничениях

| ID | Приоритет | Пробел | Статус |
|----|-----------|--------|--------|
| GAP-DECISION-001 | P1 | **Интерпретация BUILD-ONLY.** OSS-библиотеки (Playwright, LangGraph, Anthropic SDK, MCP SDK) = разрешены (это «написание»); готовые серверные продукты/SaaS = не разрешены. | **RESOLVED 2026-06-23** (подтверждено пользователем). GAP-ARCH-002 разблокирован → закрыт. |
| GAP-ARCH-001 | P1 | **`pw-executor` теперь на критическом пути.** Создание и поддержка собственного TypeScript Playwright execution server — самый большой отдельный компонент и наибольший риск постоянного обслуживания (отслеживает изменения API библиотеки Playwright). Снижение риска: тонкий слой инструментов поверх стабильного API `Locator`/`accessibility` Playwright; контрактные тесты, проверяющие имена инструментов и схемы; фиксация версии Playwright. | OPEN |
| GAP-ARCH-002 | P2 | Если GAP-DECISION-001 разрешится в «никакого OSS тоже», придётся переоценить весь стек (LangGraph → собственный цикл, Playwright → raw CDP). Аннулирует ADR-002/004/005. | BLOCKED on GAP-DECISION-001 |

## VERIFY (проверка фактов — anti-hallucination; не предполагать)

| ID | Приоритет | Пункт | Устранить к |
|----|-----------|-------|-------------|
| GAP-VERIFY-001 | P1 | Возможности базового **Playwright library API** для зафиксированной версии: поверхность `accessibility` snapshot, `tracing.start/stop`, движки локаторов (role/text/label/testid), `screenshot`. Мы ОПРЕДЕЛЯЕМ собственную поверхность инструментов в `pw-executor`, но она опирается на эти примитивы. | M0 |
| GAP-VERIFY-002 | P1 | Пакет **Python↔MCP binding** и его зрелость (например, `langchain-mcp-adapters`): стабильность транспорта stdio, строгость валидации схем инструментов, распространение ошибок subprocess. Оба конца теперь наши (снижает риск). Сохраняем тонкий заменяемый адаптер, чтобы кастомный JSON-RPC клиент на ~300 строк оставался запасным вариантом с низким риском. | M1 |
| GAP-VERIFY-003 | P2 | API checkpointer **LangGraph `SqliteSaver` / `AsyncPostgresSaver`** + использование отдельного DB-файла; семантика `interrupt`/pause для human gate. | M1 |
| GAP-VERIFY-004 | P3 | Формат вызовов structured-output / tool-use **Anthropic SDK** для узлов plan + heal (ID моделей `claude-opus-4-8`, `claude-sonnet-4-6`). | M1 |
| GAP-VERIFY-005 | P2 | **Реальный smoke провайдер-агностичного backend (M6/ADR-019).** Сеть в среде заблокирована → offline покрыт `FakeBackend`; реальное поведение OpenAI-compat (минимум один роутер: OpenRouter/DeepSeek/Qwen/Gemini-compat) — **user-run**: `temperature=0` принимается?; `max_tokens` vs `max_completion_tokens` (o-series); отсутствие `usage` (Ollama/vLLM); vision `image_url` data-URI. Инструкция в `docs/M6_CONTRACT.md`. | M6 (user-run) |
| GAP-VERIFY-006 | P2 | **Поддержка MCP `sampling/createMessage` по хостам** для M7 `SamplingBackend` (ADR-020): M7 реализован, offline-verified; серверный `create_message` API подтверждён на установленном `mcp`. Остаётся **user-run** — реальный хост (Claude Desktop — да; **OpenCode/Kilocode — подтвердить capability перед боевым использованием**). Нет sampling → backend недоступен → fallback на heuristic/L1–L6. | M7 (user-run) |

## Открытые вопросы дизайна (из синтеза)

| ID | Приоритет | Вопрос |
|----|-----------|--------|
| GAP-ARCH-003 | P1 | Точная **метрика покрытия** для ADR-010: «интерактивный элемент задействован» считается по `semantic_id` / по `(page,role,name)` / по отдельному flow? Не должна поощрять тривиальные клики и не должна штрафовать небольшие приложения. |
| GAP-ARCH-004 | P1 | Как надёжно определить целевое **SUBTREE** сценария для скоупинга `dom_subtree_hash` (ближайший landmark/role container?) без избыточного или недостаточного охвата. Проверить на реальном DOM drift. |
| GAP-ARCH-005 | P2 | **Объём начальной калибровки:** сколько верифицированных человеком исходов нужно до снижения порога auto-accept с 0.90 по умолчанию, и за какой период? |
| GAP-ARCH-006 | P2 | Стоит ли **мягкая верификация на Sonnet** в режиме explore своих затрат, или достаточно детерминированной проверки состояния после действия (изменение URL, наличие ожидаемого элемента)? Измерить на M3. |
| GAP-ARCH-007 | P2 | **Охват браузеров:** для MVP предполагается только Chromium; подтвердить краткосрочную потребность в Firefox/WebKit (влияет на переносимость golden-baseline — хэши различаются для каждого движка). |
| GAP-ARCH-008 | P1 | **Обработка auth/секретов** для AUT (внедрение storage-state/cookie): где хранятся учётные данные (home-lab Vault?) и как они указываются в `RunConfig` без попадания в traces/транскрипты? |

## M9 — пробелы возможностей (дизайн-сессия 2026-06-26 → docs/M9_CONTRACT.md)

| ID | Приоритет | Пробел | Цель |
|----|-----------|--------|------|
| GAP-M9-01 | P1 | `fill`/`type`/`press`/`select` в pw-executor отсутствуют → нет форм/поиска/**логина** | M9.1 (блокер №1) |
| GAP-M9-02 | P1 | `GoalPlanner` (NL→plan, explore-first grounding) + `--mode explore\|goal\|describe` + авто-дефолт | M9.2 |
| GAP-M9-03 | P2 | Не-MCP HTTP/gRPC control-API + OSS чат-UI в DH (плюс MCP-путь через M7) | M9.3 |
| GAP-M9-04 | P2 | Auth-adapter (storageState + Keycloak/OIDC), креды из Vault; **логин как тест-цель** | M9.1/M9.7 |
| GAP-M9-05 | P2 | In-app tabs (perception `role=tab`/`tabpanel`) + browser multi-tab/context (multi-page в pw-executor) | M9.4 |
| GAP-M9-06 | P2 | Инъекция `traceparent` во все запросы браузера → корреляция UI-теста с backend/Kafka-trace в Tempo | M9.5 |
| GAP-M9-07 | P2 | Режимы браузера: headed + CDP-attach к браузеру пользователя (`connectOverCDP`) + co-pilot takeover/return | M9.6 / ветка-2 |
| GAP-M9-08 | P3 | Pluggable adapters (auth/deploy/model/backend) — универсальность не-только-DH | M9.7 |
| GAP-M9-09 | P2 | RunConfig-файл (YAML) + config-surfaces: режим/goal/auth/бюджеты через флаги · env · файл · интерактивно (чат). Сейчас только флаги+env, per-run | M9.2/M9.3 |
| GAP-M9-10 | P2 | Validation / негативное тестирование: генератор невалидных вводов по типу/маске поля + assert-слой («UI отверг ввод») | M9.1 |
| GAP-M9-11 | P3 | **Security-модуль (M10, отдельный):** XSS/CSRF/IDOR/auth-bypass/sensitive-data-in-DOM поверх explore-карты; **authorization-gated** | M10 |
| GAP-M9-12 | P3 | CI-шаблоны: Jenkinsfile + `.gitlab-ci.yml` (Sentinel = CLI + exit-коды → любой CI на коммит) | M9.3 |

## Риски (полный список; итог в ARCHITECTURE §8)

| ID | Приоритет | Риск | Снижение риска |
|----|-----------|------|----------------|
| GAP-RISK-001 | P1 | Нагрузка по обслуживанию `pw-executor` (build-only) — см. GAP-ARCH-001 | тонкий слой + контрактные тесты + зафиксированная версия |
| GAP-RISK-002 | P1 | Cold start модели уверенности (нет верифицированных человеком данных при первоначальном заполнении хранилища наиболее важными записями) | порог 0.90 до N размеченных записей; verify-before-accept + post-heal verify = model-independent gate; бюджет ручной проверки на M2/M3 для начальной калибровки |
| GAP-RISK-003 | P2 | Взрывной рост стоимости токенов на больших SPA (50+ страниц, ценообразование Opus) | convergence покрытия (ADR-010), ограничение глубины, бюджет на страницу, плавная деградация до частично замороженного плана, жёсткий потолок Go, инкрементальное исследование (пропуск страниц с неизменённым a11y-hash) |
| GAP-RISK-004 | P2 | Дисперсия задержки/стоимости heal-storm в детерминированном replay hot path при активно изменяющемся AUT | жёсткий лимит в 2 попытки + дедлайн на шаг + автопропуск; амортизация `dom_subtree_hash` (повторяющееся изменение = 0 запросов к LLM после первого heal); карантин ограничивает радиус поражения |
| GAP-RISK-005 | P2 | Слепые зоны a11y-tree (shadow DOM, canvas, кастомные web-компоненты, cross-origin iframes) | `completeness_ratio` — метрика первого класса (Grafana histogram) запускает visual fallback + отображается в отчёте; рекомендуем команде AUT добавить data-testid/ARIA там, где хронически низкое значение |
| GAP-RISK-006 | P2 | Хрупкость `dom_hash` — хэш всей страницы аннулирует все локаторы при несвязанном изменении (реклама/A-B/аналитика) | хэшировать целевое SUBTREE, а не страницу; настраиваемый скоуп; CSS-список игнорирования для волатильных виджетов |
| GAP-RISK-007 | P2 | Конкуренция за запись в SQLite при параллельном CI / multi-runner K3s | per-job SQLite для CI (без общего writer); единственный Go-writer + WAL для сервиса; задокументированный триггер перехода на Postgres (>50 параллельных shared-DB writers / distributed workers) |
| GAP-RISK-008 | P3 | Сложности версионирования Proto/gRPC по мере развития brain | стабы, генерируемые CI из одного `.proto`; проверка proto-hash (несовпадение = ошибка сборки); необязательные поля + обратная совместимость 1 major; граница вводится только на M2 |
| GAP-RISK-009 | P2 | Screenshot-hash нестабилен побайтно при отдельных запусках браузера (baseline run vs replay run) → нестабильный visual golden-diff. Снижение риска на M3: захват один раз на страницу при первом посещении (без фокуса/каретки), и сделать **visual regression advisory** (a11y-hash управляет exit 2). Полное исправление отложено: фиксированный viewport + набор шрифтов + `animations:'disabled'`/`caret:'hide'`, или захват goldens в одном процессе. **PARTIAL (M8):** determinism-опции реализованы в pw-executor (`animations:'disabled'` + `caret:'hide'` + `scale:'css'` + фикс-viewport 1280×720/DSR=1), tsc-verified; visual остаётся **advisory** до подтверждения байт-стабильности (golden дважды в разных процессах = equal). Flip в authoritative — follow-up | PARTIAL |
| GAP-OBS-001 | P3 | Отложенные пункты M4b: Go `report-service` HTTP `/metrics` (убран — batch CronJob использует Pushgateway/textfile, ADR-018); TS (`pw-executor`) + Go (`store-gateway`) OTel spans с W3C context propagation; жёсткий потолок бюджета на стороне Go (требует долгоживущего Go orchestrator + отчётности brain→Go о токенах; путь эвристики по умолчанию не использует LLM). **RESOLVED (M8/ADR-021):** Go `report-service` (HTTP) + TS/Go OTel spans + W3C propagation (executor `_meta` + store.py gRPC interceptor + otelgrpc StatsHandler + per-node spans) + Go `orchestrator` budget-ceiling — реализованы и **compile/test-verified** (Python 36 offline + `go build`/`vet`/`test` + `tsc` — всё clean). Остаётся observe: live OTLP-trace + реальный budget-kill end-to-end | RESOLVED |
