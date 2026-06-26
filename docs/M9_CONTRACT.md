# M9 Contract — "Conversational & Goal-Directed Testing" (PROPOSED — design freeze 2026-06-26)

> 🌐 **Русский** (основная версия) · [English](M9_CONTRACT.en.md)

Статус: **Proposed** (заморозка дизайна; roadmap-эпик с под-milestone'ами M9.1…M9.8). Источник —
дизайн-сессия 2026-06-26. Реализация — отдельными под-milestone'ами, каждый docs-first.

Цель: эволюция Sentinel из «coverage-explore + CLI» в инструмент, который (а) тестирует реальные
**многошаговые бизнес-процессы** включая формы и авторизацию; (б) даёт **авторинг тестов словами
(NL)**, граунденный в живую карту элементов; (в) работает в **MCP и не-MCP** режимах, с **локальными
или облачными** моделями; (г) остаётся **универсальным** (не только Deckhouse). Две ветки доставки:
**чат-UI сейчас**, **браузерное расширение позже**.

## A. Пробелы возможностей (с решениями сессии)
| # | Gap | Решение / направление |
|---|-----|----------------------|
| A1 | **`fill`/`type` в pw-executor отсутствуют** | Добавить tools `browser.fill`/`type`/`press`/`select` — БЕЗ них нет форм, поиска, **логина**. Блокер №1. |
| A2 | Explore **coverage-driven**, не бизнес-процесс | `GoalPlanner` (см. B) — NL-цель + карта элементов → шаги. |
| A3 | Нет NL-авторинга | Чат-UX (см. G) поверх M7 + не-MCP API. |
| A4 | Auth (Keycloak) | storageState-предусловие + **логин как тест-цель** (нужен A1). См. H. |
| A5 | **Вкладки приложения** (`role=tab`/`tabpanel` в одной странице) | Perception дополнить ролью `tab`; tab-switch = навигация-внутри-страницы. Почти работает. |
| A6 | **Браузерные вкладки/окна** (multi-page) | pw-executor → multi-context/page; план ссылается на вкладку. Реальный gap. |
| A7 | Backend-корреляция (микросервисы+Kafka) | Инъекция `traceparent` во все запросы браузера (см. I). |
| A8 | Универсальность не-только-DH | Pluggable adapters (см. J). |
| A9 | Режимы браузера (свой/пользователя/co-pilot) | own-headless → headed → CDP-attach → takeover (см. F). |

## B. Модель авторинга — explore-first (дефолт) vs describe-first
- **explore-first (дефолт, рекомендовано):** explore строит карту **реальных** элементов
  (`semantic_id`/role/name/testid) → LLM по карте **предлагает сценарии** ИЛИ принимает NL-описание,
  но **граундит в реальные элементы** → ты правишь/утверждаешь → заморозка `plan.json` + `.spec.ts`.
  Почему: LLM не галлюцинирует селекторы, которых нет.
- **describe-first (опц.):** NL-описание → LLM черновой план → explore **сверяет** с реальностью
  (reconcile) → правки. Для тех, кто знает флоу заранее.

## C. Режимы и переключатели
- **`--mode explore | goal | describe`** (явный) **+ авто-дефолт:** цель не задана → чистый
  `explore` (для простых страниц без БП); задана NL-цель → `goal`. **Не** «авто-определение сложности».
- **Pluggable `GoalPlanner`** встаёт в существующий шов `Planner` (ADR-011) рядом с Heuristic/LLM.

## D. Вкладки: in-app vs браузерные (явно различаем)
- **In-app tabs** (DOM tab-widgets) — интерактивные элементы; explore кликает, контент `tabpanel`
  меняется → re-perceive. Нужно: perception ловит `role=tab`, coverage учитывает табы. **A5.**
- **Browser tabs/windows** — pw-executor должен держать несколько `page`/`context`, план хранит
  идентификатор вкладки, executor роутит вызовы по вкладке. **A6 (новый код).**

## E. Модели: облако + локально
- **Локальные модели работают уже (M6):** `LLM_BACKEND=openai` + `LLM_BASE_URL=<ollama/vllm/lmstudio>`.
  В DH — Ollama/vLLM in-cluster → суверенно/air-gapped. Vision-heal: vision-модель (Qwen-VL/LLaVA),
  гейтится `supports_vision`. Per-role: можно локальную для heal, облачную для plan, и наоборот.

## F. Режимы выполнения браузера (эволюция)
1. **own-headless (сейчас):** pw-executor поднимает свой headless Chromium; пользователь не видит.
2. **headed (видимый):** `chromium.launch({headless:false})` — тривиальный тумблер; пользователь смотрит.
3. **CDP-attach к браузеру пользователя:** `chromium.connectOverCDP` к Chrome с `--remote-debugging-port`
   — Sentinel драйвит **существующий** браузер/сессию пользователя. Основа ветки-расширения.
4. **co-pilot takeover/return:** агент ведёт → отдаёт управление человеку → забирает. Human-in-the-loop
   авторинг/правка вживую. Вторая ветка (после чат-UI).

## G. Поверхности доступа — MCP И не-MCP (обе)
- **MCP-режим:** brain как MCP-сервер (M7, ADR-020) — любой MCP-хост (Open WebUI/Claude Desktop/…) рулит.
- **Не-MCP режим:** тонкий **HTTP/gRPC control-API** к brain (reuse/расширение RunControl) — для чат-UI,
  который не хочет MCP, и для CI/скриптов. **Чат НЕ только через MCP.**
- **Ветки доставки:** **(1) чат-UI сейчас** — OSS-фронт (Open WebUI / свой) в DH/Docker, говорит с brain
  через MCP **или** control-API; **(2) браузерное расширение позже** — record/describe вживую + CDP-takeover (F3/F4).

## H. Auth (решение пользователя: прикрутить + тестировать сам логин)
- **Предусловие:** Playwright `storageState` (логин один раз → reuse cookie/token); креды из **Vault**,
  никогда в traces (`GAP-ARCH-008`).
- **Тест-цель:** флоу логина (Keycloak: форма→submit→redirect→token) — бизнес-процесс для explore/replay.
  **Требует A1 (`fill`/`type`).**
- **Pluggable auth-adapter:** none | basic | OIDC/Keycloak | storageState-файл (для универсальности, J).

## I. Backend-корреляция (микросервисы + Kafka)
- Sentinel тестирует UI чёрным ящиком; backend напрямую не трогает. **НО** инъекция `traceparent`
  во ВСЕ исходящие запросы браузера (`context.setExtraHTTPHeaders`/`route`) → **каждое тест-действие
  маппится на сквозной backend-trace** (UI→frontend→сервис A→Kafka→сервис B→БД) в Tempo — если сервисы
  OTel-инструментированы (сторона пользователя). Это и есть ценность Tempo для микросервисного зоопарка.

## J. Универсальность (не только DH)
- **Ядро (Go/Python/TS) уже агностично:** target = URL; OTLP/Prometheus стандартны; build-only без локов.
- **Принцип:** универсальное ядро + **сменные adapters по краям:** auth-provider · deploy-target
  (CronJob/Docker/standalone CLI) · model-backend (cloud/local) · trace/metrics-backend (любой OTLP/Prom).
  DH-специфика изолируется в Helm values + Keycloak-адаптер. Ядро не трогаем.

## K. Уточнение: код + агент, НЕ md-агент
Sentinel = автономный агент, **реализованный в коде** (полиглот), а не Claude Code `.md`-сабагент
(это явно вне scope, ARCHITECTURE §1). md-доки — спецификация (docs-first), не реализация.

## L. Уточнения сессии-2 (2026-06-26)
- **Модели где угодно, не только в DH:** endpoint модели = `LLM_BASE_URL` + `LLM_API_KEY`. Облако
  (Anthropic/OpenAI/OpenRouter) или self-hosted в другом месте — просто endpoint. **Vision-модель — per-role**
  (`LLM_*_HEAL`): может быть **отдельный** endpoint+ключ (или тот же). В DH ничего разворачивать не обязательно.
- **Подключение к уже развёрнутому UI:** да — `target` это просто URL (in-cluster Service, Ingress, внешний,
  staging). Sentinel подключается к любому достижимому URL; переразворачивать ничего не надо.
- **goal-режим = сначала ПОЛНЫЙ explore, потом цель:** explore сам «понять, что нужен goal» не может (это
  семантика). Поэтому в goal-режиме сначала идёт **полный explore** (карта всех страниц/элементов + обнаружение
  проблем), и по этой карте `GoalPlanner` строит сценарий под цель → находятся И рабочие элементы, И проблемы.
  Чистый explore (без цели) — для простых страниц.
- **Метрики: и push, и pull.** Ephemeral CronJob **пушит** в Prometheus **Pushgateway** (мы отдаём). Долгоживущий
  `report-service` отдаёт `/metrics`, который Prometheus **скрейпит** (он забирает). Выбор по режиму деплоя.
- **Что/как разворачиваем:** (а) **DH/k8s** — Helm CronJob + ArgoCD (GitOps), Ollama/vLLM опц. in-cluster;
  (б) **Docker** — один контейнер (Dockerfile есть), `agentctl` в контейнере/compose; (в) **bare CLI** — бинари
  `agentctl`/`store-gateway`/`orchestrator` + venv + node, без k8s. Ядро одинаково; меняется только обёртка.
- **Валидация полей (формат/символы/границы) — В scope (M9.x):** негативное тестирование — ввести невалидное →
  **assert, что UI отверг** (ошибка/не дал submit). Нужны `fill`/`type` (A1) + слой ассертов + генератор невалидных
  вводов по типу/маске поля. Так ловим «валидация не работает».
- **Безопасность UI — ОТДЕЛЬНЫЙ модуль, не ядро.** Функциональное + валидационное — ядро; security
  (XSS/CSRF/IDOR/auth-bypass/sensitive-data-in-DOM) — **pluggable security-модуль (M10/расширение)**, потребляющий
  карту элементов + traces. Причины: другая дисциплина, **другой authorization-режим** (active security требует явной
  авторизации — по твоим же правилам), чтобы не раздувать ядро. Substrate (explore-карта) готов.
- **CI/коммиты:** Sentinel = CLI + exit-коды 0/1/2/3 → **любой CI** (Jenkins/GitLab/Drone/GH Actions) зовёт
  `agentctl run --replay --ci`. На коммит → CI-хук → build/deploy preview → прогон → гейт по exit. GitHub Actions
  уже есть; шаблоны Jenkinsfile/.gitlab-ci — M9.x.
- **Монорепо — правильно, не делить.** Полиглот-продукт (Go+Python+TS) с общими контрактами (proto/MCP) должен
  версионироваться **атомарно**; раздельные репо → version-skew по gRPC/MCP, тройной release-кошмар. Один источник
  proto, один CI, один релиз. Выделять компонент — только если станет самостоятельным продуктом; сейчас нет.

## Под-milestone'ы (предлагаемое секвенирование по ценности/риску)
- **M9.1** `fill`/`type`/`press`/`select` + auth (storageState + login-as-test) — *минимум для живого теста.*
- **M9.2** `GoalPlanner` (NL→plan, explore-first grounding) + `--mode` switch.
- **M9.3** Чат-UX: не-MCP HTTP/gRPC control-API + OSS-фронт в DH (плюс MCP-путь через M7).
- **M9.4** In-app tabs (perception) + browser multi-tab/context.
- **M9.5** Browser trace-header injection (backend-корреляция).
- **M9.6** headed + CDP-attach режимы (фундамент расширения).
- **M9.7** Pluggable adapters (auth/deploy/model) — универсальность.
- **M9.8 (ветка 2)** Браузерное расширение + co-pilot takeover/return.

## ADR'ы (этого контракта)
- **ADR-022** Goal-directed / NL-авторинг через explore-first grounding (новый `GoalPlanner`).
- **ADR-023** Двойной доступ к чату: MCP (M7) + не-MCP HTTP/gRPC control-API.
- **ADR-024** Режимы выполнения браузера: own-headless → headed → CDP-attach → co-pilot.
- **ADR-025** Универсальность через pluggable adapters (auth/deploy/model/backend).

## Вне scope (этот контракт = заморозка дизайна)
Имплементация (под-milestone'ы M9.x). Тестирование backend-API без UI (Sentinel — UI-агент).
Конкретный выбор OSS-чата (Open WebUI vs свой) — решается в M9.3.
