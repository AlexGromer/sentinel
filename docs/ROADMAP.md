# Sentinel — MVP Roadmap (M0–M9.1)

> 🌐 **Русский** (основная версия) · [English](ROADMAP.en.md)

Производный документ синтеза дизайна от 2026-06-23; канонический итог в ../ARCHITECTURE.md.

> **Статус доставки (на 2026-06-26):** M0–M8 (+ M2b/M4b) — ✅ доставлено; **M9** — дизайн заморожен
> (Proposed, ADR-022..025); **M9.1** (формы/логин/валидация, ADR-026) — ✅ доставлено offline. Детальные
> секции M0–M7 ниже — историческая запись плана; авторитетный текущий статус — `../ARCHITECTURE.md` §6 +
> `../BACKLOG.md`.

---

## Критический путь: `pw-executor` (GAP-ARCH-001)

Критический путь через все основные milestone (M0–M7, плюс под-вехи M2b/M4b) теперь — **`pw-executor`**, наш собственный TypeScript
Playwright execution server, реализующий MCP/JSON-RPC-2.0 stdio интерфейс. Каждый
milestone, порождающий browser subprocess, зависит от инкрементально поставляемого
`pw-executor`. Этот сервер — не готовый продукт «из коробки»; он собирается и фиксируется
по версии командой Sentinel. Начальная поверхность (M0) минимальна: `navigate`,
`accessibility_snapshot` и `trace`. Возможность visual set-of-marks overlay добавляется
в M5, ограниченная порогом точности PoC. Ни один milestone не может считаться завершённым,
пока поверхность `pw-executor`, требуемая этим milestone, не реализована и не покрыта
контрактным тестом, проверяющим имена инструментов и входные схемы. **GAP-ARCH-001**
отслеживает эту зависимость; любая регрессия в `pw-executor` является блокером для
затронутого milestone.

---

## M0 — Hello Browser (Дни 1–3)

**Языки:** Go, Python, TypeScript

**Поставляемый результат:**

`agentctl run` запускает Python brain через subprocess и переменные окружения — без gRPC
пока. Brain состоит из единственного узла `perceive`. При запуске brain порождает
`pw-executor`, наш TypeScript Playwright execution server, реализующий MCP/JSON-RPC-2.0
через stdio, с минимальной поверхностью: `navigate`, `accessibility_snapshot` и `trace`.
Brain вызывает `accessibility_snapshot()`, выводит a11y tree в stdout и помещает
`trace.zip` в `ARTIFACT_DIR`. Цель: доказать сквозную связь через все три уровня
выполнения (Go → Python → TypeScript через stdio). Никаких обращений к LLM, никакой
state machine, никакой персистентности. Интеллект — позже.

**Критерий приёмки:**

> **Дано** `pw-executor` собран и запущен (npm build выполнен успешно; MCP handshake завершён через stdio)
>
> **Когда** `agentctl run --explore --target <URL>` вызван против любой живой веб-страницы
>
> **Тогда** JSON a11y tree выведен в stdout с как минимум одним перечисленным интерактивным элементом,
> **и** файл `trace.zip` присутствует в `ARTIFACT_DIR` и открывается командой
> `playwright show-trace` без ошибок — всё в течение 30 секунд с момента вызова.

---

## M1 — Autonomous Walk (Дни 4–10)

**Языки:** Python

**Поставляемый результат:**

Все 9 узлов LangGraph реализованы (`perceive`, `ground`, `plan`, `act`, `verify`,
`heal` — заглушка, `checkpoint`, `report`, а также `START`/`END`). LangGraph
`SqliteSaver` checkpointer записывает в **отдельный** DB-файл от базы данных store-gateway
(именно это делает утверждение о single-writer верным). Узел `plan` использует Opus 4.8
при `temperature=0`. Исследование завершается по **измеримой цели покрытия**
(`coverage_target` + пустота `nav_frontier`) — не по флагу, утверждённому LLM. Запуск
производит `plan.json` (с `plan_hash`), `llm-transcript.jsonl` и `trace.zip`.

**Критерий приёмки:**

> **Дано** реальное многостраничное веб-приложение доступно по `TARGET_URL`, и с landing page
> достижимы как минимум 3 различные страницы
>
> **Когда** `agentctl run --explore` завершился (или прерван по бюджету)
>
> **Тогда** `plan.json` существует в `ARTIFACT_DIR`, содержит >= 5 различных записей `PlannedAction`
> с непустыми полями `locator`, **и** `coverage_achieved` записан как float в `[0.0, 1.0]`
> в файле плана — всё проверяемо командой `jq '.coverage_achieved,
> (.steps | length)' plan.json`.

---

## M2 — Self-Repairing Walker (Дни 11–20)

**Языки:** Go, Python

**Поставляемый результат:**

Узел `heal` полностью реализован через `healing-engine`: поиск в кэше → детерминированная
ротация стратегий L1–L6 → a11y re-grounding на Sonnet 4.6 (structured output) →
live-DOM probe `verify-before-accept` → ворота уверенности (>= 0.85 auto-heal, 0.60–0.84
отмечен, < 0.60 human gate) → post-heal верификация (повторный запуск действия с
исправленным локатором перед сохранением) → запись `healing_audit` только для добавления →
амортизация `dom_subtree_hash` с автоматическим вытеснением устаревших записей.

На этом milestone вводится граница gRPC: определяется `proto v1` (`PersistenceService`) и
стабы генерируются для Go и Python в CI. Реализуется Go `store-gateway` (SQLite WAL:
таблицы `runs`, `healed_locators`, `healing_audit`).

**Критерий приёмки:**

> **Дано** в plan.json, полученном на M1, один selector вручную изменён на недопустимое
> значение, **и** кэш локаторов brain для этого selector пуст
>
> **Когда** `agentctl run --replay` выполнен однажды (первый запуск), затем выполнен снова
> с тем же сломанным selector и тем же AUT (второй запуск)
>
> **Тогда** первый запуск восстанавливает сломанный selector с зафиксированной уверенностью
> >= 0.85, сохраняет строку `HealedLocator` с `status=active` в базе данных store-gateway
> и завершается с кодом 0; **и** журнал `healing_audit` второго запуска показывает ноль
> потреблённых токенов LLM для этого semantic_id (попадание в кэш, амортизированное
> повторное использование, проверяемое командой `jq '.llm_tokens' healing-audit.jsonl`).

---

## M2b — Service Layer (Go store-gateway + MCP-SDK transport)

**Языки:** Go, Python, TypeScript

**Поставляемый результат:**

Чистая инфраструктура без новой пользовательской ценности (ADR-015/016): погашен долг временного
отклонения ADR-012. **M2b-1** — Go `store-gateway` (gRPC, proto `PersistenceService`) становится
единственным SQLite-писателем (восстанавливает ADR-007); `brain/store.py` переписан тонким
gRPC-клиентом с теми же сигнатурами; `agentctl` запускает gateway как дочерний процесс через
Unix-domain socket (`STORE_ADDR`). **M2b-2** — транспорт brain↔pw-executor мигрирован на MCP SDK
(`pw-executor` = MCP-сервер, brain = MCP-клиент за тем же `ex.call`); закрывает GAP-VERIFY-002,
JSON-RPC сохранён как fallback под feature-flag.

**Критерий приёмки:**

> **Дано** `store-gateway` собран и запущен `agentctl` через UDS, а `pw-executor` переписан на MCP SDK
>
> **Когда** explore + baseline + replay + calibrate выполнены через сервисный слой, и запрошен `tools/list`
>
> **Тогда** все четыре дают идентичное поведение (те же exit codes / heal / golden), `grep -r sqlite3
> brain/` пуст (brain не держит дескриптор БД), `tools/list` возвращает 7 инструментов, **и** live-гейты
> M0–M3 по-прежнему проходят через MCP-транспорт — Go unit-тесты + Python offline-набор с in-proc fake
> store зелёные (live-часть — user-run).

---

## M3 — CI-Ready Replay (Дни 21–30)

**Языки:** Go, Python

**Поставляемый результат:**

Реализованы режимы запуска `replay` и `ci`: узел `plan` полностью пропускается, а `ground`
направляется непосредственно в `act`, используя замороженные локаторы — ноль обращений к
LLM на happy path. **Жёсткое прерывание по `plan_hash`** (exit code 3) применяется при
старте replay. Двойные golden baselines `a11y_hash` + `screenshot_hash` валидируются на
каждом шаге milestone. Реализован AUT-SHA-gated карантин нестабильных тестов (таблица
`step_failures`; шаг засчитывается как нестабильный только при N неудач из 5 без изменения
AUT git-SHA). Генерируются структурированные exit codes `0/1/2/3`. Orchestrator выделен в
отдельный gRPC-сервер (`RunControl`, надзор за subprocess, принудительное соблюдение
дедлайна на шаг). Для CI используется per-job SQLite
(`AGENT_DB_PATH=/tmp/agent-{run_id}.db`). Поставляется workflow GitHub Actions (условное
задание explore + матрица параллельных replay).

**Критерий приёмки:**

> **Дано** валидный `plan.json` зафиксирован в репозитории (hash верифицирован), **и** вторая
> копия этого файла имеет вручную изменённое поле `locator` одного из шагов
>
> **Когда** три параллельных задания CI replay запускаются против зафиксированного `plan.json`
> (режим `--ci`), **и** одно дополнительное replay выполняется против вручную
> отредактированной копии
>
> **Тогда** все три параллельных replay завершаются менее чем за 2 минуты каждое (wall clock)
> и выходят с кодом 0; **и** replay против вручную отредактированного файла завершается с
> кодом 3 в течение 5 секунд после загрузки плана, причём оба хэша — сохранённый и
> вычисленный — записаны в stderr — измеряется по времени CI заданий и проверкам exit code
> в workflow GitHub Actions.

---

## M4 — Production-Observable v1.0 (Дни 31–45)

**Языки:** Go, Python

**Поставляемый результат:**

Реализован `report-service` (Go): генерирует `run_report.json` + `run_report.html`
(зеркалируя структуру Playwright HTML reporter), открывает Prometheus `/metrics` и создаёт
экспортируемый `.spec.ts` из `RunState.executed_actions` через Go template — без зависимости
от инструмента codegen pw-executor.

OTel spans добавлены во все три уровня выполнения: каждый узел LangGraph, каждый
MCP-вызов pw-executor и каждый Go gRPC-вызов. `prompt_HASH` прикрепляется к LLM span;
содержимое prompt никогда не хранится. Экспорт: OTLP → Grafana Alloy → Tempo.

Реализована команда `agentctl calibrate`, выполняющая вычисление precision/recall
`healing_confidence_histogram` против исходов `human_verified`. Активирована согласованность
жёсткого потолка бюджета на стороне Go. Узел `plan` полностью переключается на Opus 4.8
(без ворот и заглушек).

**Критерий приёмки:**

> **Дано** AUT из 10 страниц (>= 10 различных доступных URL) исследован или воспроизведён
> до завершения
>
> **Когда** запуск завершился в пределах настроенного бюджета токенов
>
> **Тогда** `run_report.html` присутствует, непустой и отображается без ошибок в браузере;
> экспортируемый `.spec.ts` проходит `tsc --noEmit` без ошибок типов; `trace.zip`
> просматривается через `playwright show-trace` без ошибок; **и** `agent_cost_usd_total`
> присутствует в выводе scrape `/metrics` со значением больше нуля — все четыре условия
> верифицированы в одном CI задании.

---

## M4b — Observability (brain OTel + Prometheus Pushgateway)

**Языки:** Python

**Поставляемый результат:**

Распределённая трассировка + push-метрики поверх слоя Go-сервисов (ADR-018). OTel в brain
(`brain/otel.py`): span запуска `sentinel.run` + LLM-span'ы `plan.llm` / `heal.llm`, несущие
**prompt_HASH, никогда не содержимое prompt**; экспорт в OTLP → Tempo только если установлен
`OTEL_EXPORTER_OTLP_ENDPOINT`, иначе no-op (нулевые накладные расходы). Prometheus Pushgateway
(`PROM_PUSHGATEWAY`) для пакетных метрик `sentinel_*`, так как агент — это CronJob; текстовый
`metrics.prom` по-прежнему поставляется. Go report-service HTTP, TS/Go span'ы и Go-потолок бюджета
отложены (GAP-OBS-001).

**Критерий приёмки:**

> **Дано** `OTEL_EXPORTER_OTLP_ENDPOINT` / `PROM_PUSHGATEWAY` не установлены (дефолт)
>
> **Когда** запуск завершён
>
> **Тогда** span'ы — no-op, push нет, offline-тесты зелёные и новых режимов сбоя нет; **и** когда
> пользователь устанавливает endpoint + gateway с живым collector, трейсы запуска появляются в Tempo,
> а метрики `sentinel_*` — в Pushgateway.

---

## M5 — Visual Heal PoC + K3s/ArgoCD (Дни 46–60+)

**Языки:** Go, Python, TypeScript (PoC-gated)

**Поставляемый результат:**

Путь visual heal set-of-marks (попытка стратегии healing 3) **встроен в `pw-executor`**
и активируется только если PoC достигает > 70% точности на 20 реальных сценариях со
сломанными selector. Возможность overlay добавлена в `pw-executor` как дополнительный
MCP-инструмент на том же stdio-канале, предоставляя пронумерованные mark overlays,
сопоставленные с DOM-элементами; LLM возвращает номер метки, а `healing-engine` извлекает
реальный semantic locator из сопоставленного узла — без координатных кликов. Если порог
PoC не достигнут, функция остаётся отложенной. Нет запасного выхода «если официальный
сервер этого не умеет»: `pw-executor` — наш сервер, и мы строим любую необходимую нам
поверхность.

Postgres + `AsyncPostgresSaver` вводятся **только** при достижении задокументированного
порога конкурентности (> 50 параллельных shared-DB writers или distributed workers); в
противном случае продолжается использование SQLite WAL. Для home-lab GitOps развёртывания
поставляются Helm chart и манифест ArgoCD Application с конфигурацией per-namespace для
целей `dev` / `staging` / `prod`.

**Критерий приёмки:**

> **Дано** подготовлен размеченный бенчмарк ровно из 20 реальных сценариев со сломанными
> selector, каждый с верифицированным человеком корректным локатором в качестве эталона
>
> **Когда** инструмент set-of-marks overlay `pw-executor` применяется `healing-engine` ко всем
> 20 сценариям (без L1–L6 или LLM a11y fallback — только visual path), с применением
> `verify-before-accept` к каждому кандидату
>
> **Тогда** как минимум 15 из 20 сценариев дают исправленный локатор, соответствующий
> верифицированному человеком selector (>= 75% точности превышает порог 70%) — измеряется
> автоматизированным сравнением и записывается в `healing-audit.jsonl`; если менее 15
> сценариев проходят, функция set-of-marks записывается как отложенная в `ARCHITECTURE.md`,
> а инструмент overlay `pw-executor` удаляется из поставляемого бинарного файла до
> следующего цикла PoC.

---

## M6 — Provider-Agnostic LLM Backend (ADR-019)

**Языки:** Python

**Поставляемый результат:**

Снята привязка «мозга» к единственному провайдеру (Anthropic). Узлы **planner** (explore) и **heal**
(text re-grounding + set-of-marks vision) вызывают LLM через провайдер-нейтральный `LLMBackend`
(`brain/llm.py`), поэтому Sentinel работает на Anthropic ИЛИ на любом OpenAI-совместимом endpoint
(ChatGPT, DeepSeek, Qwen, Gemini-compat, OpenRouter, Ollama, vLLM), выбираемом **per-role** через env
(precedence `LLM_<KEY>_<ROLE>` > `LLM_<KEY>` > дефолт). Реализованы `AnthropicBackend` +
`OpenAICompatBackend`; `make_backend(role)` возвращает `None` без ключа/SDK ⇒ offline-fallback
(heuristic / L1–L6) и **никогда не бросает**. При нуле выставленных env — поведение байт-в-байт как
до M6 (Anthropic, Opus planner / Sonnet heal). LLM-путь best-effort: provenance модели не хранится,
replay LLM-free, golden baselines heuristic-only.

**Критерий приёмки:**

> **Дано** окружение exec-gating (без сети/бинарей), `FakeBackend` вместо живого провайдера
>
> **Когда** запущен offline-набор
>
> **Тогда** `test_b1_offline` (8) + `test_m5_offline` (4) зелёные, регресс `test_m3` / `test_m4` /
> `test_m4b` зелёный, **и** default-path (ноль env) воспроизводится байт-в-байт; реальный smoke
> провайдера (Anthropic-дефолт и OpenAI-compat роутер) — user-run, сеть в среде заблокирована.

---

## M7 — MCP-Server Exposure (Proposed, ADR-020)

**Языки:** Python

**Статус:** ✅ **Доставлено (ADR-020)** — brain MCP-сервер (FastMCP) + `SamplingBackend`; offline-verified
(`test_m7`). Живой MCP-host — user-run (GAP-VERIFY-006).

**Поставляемый результат (план):**

Второе направление запроса пользователя: Sentinel **вызывается из** агентов-хостов (OpenCode,
Kilocode, Claude Desktop), которые сами поставляют модель. «Мозг» экспонируется как **MCP-сервер**
(отдельный от pw-executor) с инструментами `explore` / `heal` / `replay` / `report`; host драйвит его
и поставляет модель через MCP `sampling/createMessage`. Реализуется как ещё одна реализация
`LLMBackend` — `SamplingBackend` (`supports_vision=False` ⇒ heal деградирует в L1–L6; токены 0), без
изменений planner/healing. Поддержка sampling неравномерна по хостам (Claude Desktop — да;
OpenCode / Kilocode — VERIFY до кодирования).

**Критерий приёмки (когда реализуем):**

> **Дано** запущен MCP-сервер Sentinel
>
> **Когда** запрошен `tools/list`, и реальный MCP-host драйвит explore через sampling
>
> **Тогда** `tools/list` возвращает `explore`/`heal`/`replay`/`report`, host поставляет модель через
> sampling, а артефакты идентичны CLI-режиму; offline — contract-тест схем инструментов +
> `SamplingBackend` через fake sampling-session (паттерн `FakeBackend`).

---

## M8 — Distributed Observability + Budget Ceiling (Доставлено, ADR-021)

**Языки:** Go + Python + TS. W3C-трейс-контекст через все три слоя (brain→pw-executor→store-gateway,
gated OTLP); Python `BudgetTracker` (graceful degradation → heuristic / L1–L6) + долгоживущий Go
`orchestrator` (gRPC `RunControl`, token-reconcile, SIGTERM hard-ceiling) + Go `report-service` (HTTP).
Compile/test-verified (Python 36 offline + `go build`/`vet`/`test` + `tsc`). Live OTLP + реальный
budget-kill — user-run.

## M9 — Conversational & Goal-Directed Testing (Proposed, ADR-022..025)

Дизайн заморожен — roadmap-эпик с под-вехами M9.1…M9.8. Эволюция из «coverage-explore + CLI» в
тестирование реальных бизнес-процессов (формы/логин), NL-авторинг (explore-first grounding), доступ
**MCP И не-MCP**, универсальность через pluggable adapters. См. `../docs/M9_CONTRACT.md`.

### M9.1 — Form/Login/Validation primitives (Доставлено offline, ADR-026)

**Языки:** TS + Python. pw-executor `fill`/`type`/`press`/`select`/`expect`/`saveStorageState` (оба
транспорта); storageState-auth (login-as-test) + секреты через `secretRef` (никогда не сериализуются) +
`PW_NO_TRACE` tracing-gate; assert/негативный слой + `brain/validation.py` (набросок). Offline-verified
(`test_m9` 19 + регресс m3..m9 + `tsc` + `go build` + gitleaks); 4-мерный adversarial review. Живой UI — по «go».
**Следующее: M9.2** (`GoalPlanner` NL→plan + `--mode explore|goal|describe` + RunConfig YAML).
