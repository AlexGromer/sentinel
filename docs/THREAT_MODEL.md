# Threat Model — Sentinel

> 🌐 **Русский** (основная версия) · [English](THREAT_MODEL.en.md)

> **Версия**: 1.2 | **Дата**: 2026-07-12 | **Авторы**: appsec-engineer (auto), @AlexGromer
> **Методология**: STRIDE-lite | **Scope**: whitebox, static analysis по исходному коду

---

## 1. Введение и scope

**Sentinel** — автономный black-box UI-тестер. Он запускается как Go CLI (`agentctl`), порождает Python-процесс (`brain`), который управляет Playwright-сервером (`pw-executor` / TypeScript) через JSON-RPC/MCP-stdio, а тот — headless Chromium, нацеленным на тестируемое приложение (AUT).

**Что рассматривается в этом документе:**
- Полная цепочка доверия: `host-env → agentctl → brain → pw-executor → Chromium → AUT` и боковые каналы `brain → LLM endpoint` и `agentctl → store-gateway → SQLite`.
- Угрозы конфиденциальности, целостности и доступности системы и данных, которые она обрабатывает.
- Только существующая кодовая база (`main`). Запланированный модуль активного security-сканирования AUT (XSS/CSRF/IDOR) — вне scope.

**Что НЕ рассматривается:**
- Инфраструктурный уровень (сеть кластера, etcd encryption at rest, IAM — домен infrastructure/devsecops).
- Динамическое тестирование / пентест AUT.
- Политики раскрытия уязвимостей — см. [`SECURITY.md`](../SECURITY.md).

---

## 2. Защищаемые активы

| Актив | Где хранится | Конфиденциальность | Целостность | Доступность |
|---|---|---|---|---|
| **API-ключи LLM** (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `LLM_API_KEY_*`) | Host env, Helm `extraEnv` | **Критическая** | Высокая | Средняя |
| **DSN checkpoint БД** (`CHECKPOINT_DSN`) | Host env, Helm `checkpointDsn` | **Критическая** | Высокая | Средняя |
| **AUT credentials** (typed password ИЛИ `storageState` файл с session tokens) | Env-переменная `STORAGE_STATE` (путь к файлу) | **Критическая** | Высокая | Средняя |
| **plan.json / golden baseline** | `runs/<id>/plan.json`, `state/locators.db` → `golden_snapshots` | Средняя | **Критическая** (plan_hash проверяется) | Средняя |
| **Артефакты run** (`trace.zip`, `heal-report.json`, `transcript`, `scenario.json`) | `runs/<id>/` на FS / PVC | Средняя (UI screenshots, DOM data) | Средняя | Низкая |
| **SQLite locator DB** (`state/locators.db`) | FS / PVC | Низкая | Средняя (влияет на качество heal) | Средняя |
| **LLM endpoint trust** (Anthropic cloud / OpenAI-compat / Ollama/vLLM) | Внешняя сеть / localhost | Средняя (AUT page content в промптах) | Средняя | Средняя |
| **Локальные учётные записи** (ADR-109): имя, хеш пароля, живые сессии | `state/` через store-gateway; сессии — в памяти control-api | **Критическая** (компрометация даёт доступ ко всем строкам владельца) | Высокая | Средняя |
| **Агрегатные метрики прогонов** (ADR-119, `GET /metrics`) | `runs/<id>/metrics.prom`, склейка в control-api | Средняя (числа чужой работы: сколько прогонов, что падало) | Низкая | Низкая |

---

## 3. Граница доверия (ASCII-диаграмма)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  HOST ENVIRONMENT                                                       │
│  ENV: ANTHROPIC_API_KEY, OPENAI_API_KEY, CHECKPOINT_DSN, ...          │
│                               │ os.Environ() — full inherit (❶)       │
└───────────────────────────────┼─────────────────────────────────────────┘
                                ▼
               ┌─────────────────────────────┐
               │  agentctl  (Go CLI)         │  ← cmd/agentctl/main.go
               │  flag parsing, runID, mkArtifactDir
               └────────┬──────────┬─────────┘
                        │          │ gRPC over Unix socket (❷)
                        │          ▼
                        │  ┌──────────────────────────┐
                        │  │  store-gateway  (Go)     │  ← state/sentinel-store-<id>.sock
                        │  │  PersistenceService gRPC │    state/locators.db (SQLite)
                        │  └──────────────────────────┘
                        │ subprocess + append(os.Environ(),...) (❶)
                        ▼
      ┌─────────────────────────────────────────────────────────┐
      │  brain  (Python, LangGraph StateGraph)                  │
      │  planner.py · healing.py · llm.py · store.py · otel.py │
      │  prompt_hash only in spans, never prompt content        │
      │                │ stdio JSON-RPC / MCP-stdio (❸)        │
      │                ▼                                        │
      │  ┌──────────────────────────────────────┐              │
      │  │  pw-executor  (Node.js / TypeScript)  │              │
      │  │  Playwright API, newContext            │              │
      │  │  PW_NO_TRACE=1 on auth runs            │              │
      │  │  no ignoreHTTPSErrors (by design)      │              │
      │  │                │ Playwright API (❹)    │              │
      │  │                ▼                       │              │
      │  │         Chromium  (headless)            │              │
      │  │                │ HTTP/S (❺)             │              │
      │  │                ▼                       │              │
      │  │          AUT  (app under test)          │              │
      │  │          TLS cert errors: unclassified  │              │
      │  └──────────────────────────────────────┘              │
      │                                                         │
      │  LLM calls per role (❻)                                │
      │  ┌───────────────────────────────────────────────────┐ │
      │  │ AnthropicBackend    → api.anthropic.com (HTTPS)   │ │
      │  │ OpenAICompatBackend → OpenAI / OpenRouter / cloud │ │
      │  │                    → localhost Ollama / vLLM      │ │
      │  │ SamplingBackend     → MCP host (M7)               │ │
      │  └───────────────────────────────────────────────────┘ │
      └─────────────────────────────────────────────────────────┘

Артефакты → runs/<id>/ : plan.json, transcript, heal-report.json,
                          scenario.json, reconcile-report, trace.zip (❼)
```

Граничные точки ❶–❼ соответствуют строкам таблицы ниже.

> **Новые поверхности (M9.6/M9.8/M9-LIVE-prep), не показанные на диаграмме выше (опциональны/dev-only/будущее):** ❽ **CDP-attach** к браузеру пользователя (M9.6, opt-in `PW_CDP_ENDPOINT`), ❾ **браузерное расширение** (M9.8, реализовано — `extension/`, dev-only) и ❿ **экспорт live-run артефактов** (`scripts/collect-live-run.sh`, M9-LIVE-prep) — см. §4.8 / §4.9 / §4.10.
>
> **Планируемые in-tool-поверхности (ADR-046):** (a) **replay/baseline control-API-endpoint** (M9.9/R1) — re-открывает spawn-поверхность ❶-класса → мера: только `from_run:<run_id>` + artifact-whitelist+traversal-guard (не произвольный путь). **[R1a ✅ реализовано в `cmd/control-api`: `resolveFromRun` (guard `/`,`\`,`..` + `{plan.json\|scenario.json}`-whitelist) + httptest на traversal/missing-plan.]** (b) **multi-turn conversation-state** (M9.10/R2) — новый ассет: конфиденциальность накопленного AUT-контекста + DoS unbounded-state → мера: per-session cap + 0700-изоляция. (c) **AG-UI npm-фронт** (`frontend/`, ADR-044) — npm supply-chain (усиливает GAP-SEC-002) + browser-токен → мера: dev-only/не-air-gapped, токен server-side в Runtime.
>
> **Поверхности эпика Rich-UI/Persistence/Metrics (M13-15, ADR-049..053):** (d) **persistence-БД с user-content** (scenarios/chats/results — накопленный AUT-контекст/возможный PII) → конфиденциальность + at-rest + access-control: reuse `0700`/per-run-token/SO_PEERCRED (SQLite, standalone), Postgres → стандартный authn + секрет через `secretKeyRef` (ADR-035); DoS unbounded-state → **cap+summary реализованы (M13 w5, GAP-M9-20 ✅: `_capped_history`/`_rolling_summary`)**; retention → M13-service; **ИНВЕНТАРЬ ЭТОГО КОНТЕНТА СОСТАВЛЕН (ADR-100, `docs/DB_FOREIGN_TEXT.md`)** — каждая колонка обеих схем классифицирована, и главный вывод меняет форму митигации: почти весь чужой текст здесь ПРИСУЩИЙ (локатор `{"role":"button","name":"Confirm payment"}` — это и есть текст страницы), поэтому редакция на записи к нему неприменима, а применимы `agentctl purge-store` (только явный вызов, две равнозаконные политики — с `--vacuum` и без, выбирает разворачивающий), режим `0600` и честная запись о содержимом. (e) **always-on control-plane bind** (service-профиль) → reuse ADR-032 (localhost-default + bearer + CORS-allowlist); публичный bind = opt-in+warn; service-режим добавляет рассмотрение authN/RBAC на CRUD-эндпоинты. (f) **self-contained metrics** (ADR-051) — метрики в нашей БД + native-рендер ⇒ **снижает** поверхность vs Grafana-embed (нет внешнего рендера/iframe-доверия). (g) **rich AG-UI поверх WS** (M14) → reuse ADR-043 WS-token (`Sec-WebSocket-Protocol`) + npm supply-chain (GAP-SEC-002, не-air-gapped dev-сборка). (h) **recorder session-resume `/v1/stream?session=`** (M13 R3-hardening, ✅ реализовано) — user-input в конструкцию пути записи → митигировано `filepath.Base`+charset `validRunID` (2 CodeQL `go/path-injection` разобраны как false-positive, sanitizer оставлен defense-in-depth); Origin fail-closed на публичном bind. (i) **экспорт live-run артефактов** (`scripts/collect-live-run.sh`, M9-LIVE-prep, `docs/M9_LIVE_PLAN.md` §C) — новая egress-граница; редакция staging-копии по умолчанию + безусловное исключение `checkpoint.db`/`storage_state*.json`; `trace.zip` — opt-in и неотредактирован → см. §4.10 (GAP-SEC-003/GAP-SEC-004/GAP-OPS-006).

---

## 4. STRIDE-lite: таблица угроз

> **Обозначения**: Вер(оятность) H/M/L без существующих мер; Влия(ние) H/M/L на активы.
> GAP-ID соответствуют записям в BACKLOG/GAPS.

### 4.1 Граница ❶ — host-env → agentctl → brain (full env inherit)

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Утечка всех host secrets в дочерние процессы.** До M11.3 `agentctl::spawnBrain` вызывал `cmd.Env = append(os.Environ(), …)` без allowlist (историческая цитата; теперь на этом месте `filteredEnv()` — `cmd/agentctl/main.go`). Все переменные хоста (SSH-ключи, облачные credentials, не относящиеся к Sentinel токены) наследуются Python brain, Node.js pw-executor и их подпроцессами, а также могут попасть в stderr при ошибке. | host-env → brain subprocess | **I** (Information Disclosure) | Вер: H / Влияние: H | **MITIGATED (M11.3/ADR-035):** env-allowlist default-on (`filteredEnv`; opt-out `SENTINEL_ENV_ALLOWLIST=0`) | **GAP-SEC-001 PARTIALLY OPEN** (Helm-половина закрыта); остаток — динамические Vault/CSI | M11.3 ✅ |
| **Plaintext secrets в Helm values → Kubernetes.** `cronjob.yaml:39–46` использует `value: {{ .Values.checkpointDsn }}` и `{{range .Values.extraEnv}} value: {{ $v }}` без `secretKeyRef`. CHECKPOINT_DSN и extraEnv хранятся как строки в `values-prod.yaml`, попадают в etcd в открытом виде и видны через `kubectl describe pod`. | Helm chart → K8s etcd | **I** (Information Disclosure) | Вер: H / Влияние: H | **MITIGATED (M11.3/ADR-035):** `secretKeyRef` plumbing (chart `secrets.*`) | **GAP-SEC-001 PARTIALLY OPEN** (Helm-половина закрыта) | M11.3 ✅ |

### 4.2 Граница ❷ — agentctl → store-gateway (Unix gRPC socket)

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Несанкционированный доступ к Unix-сокету.** Любой локальный процесс с правами того же UID может вызвать gRPC-методы store-gateway: записать/удалить golden baseline или locator cache без аутентификации. | local FS / Unix socket | **E** (Elevation of Privilege) | Вер: L / Влияние: M | **MITIGATED (#23):** per-run shared-secret в gRPC-metadata (agentctl минтит токен → gateway+brain; `TokenAuthInterceptor`, constant-time) — вызовы без валидного токена отклоняются (`codes.Unauthenticated`). Defense-in-depth: сокет `0600` + SO_PEERCRED (отказ чужому UID). Сокет в `state/` (не `/tmp`); gRPC экспонирует только `PersistenceService`. | Same-UID процесс может прочитать токен из `/proc/<brain>/environ` за окно короткоживущего прогона (классический Unix same-UID порог). Нет mTLS. SO_PEERCRED — Linux-only: на не-Linux платформах `PeerCredListener` — no-op (`internal/store/peercred_other.go`), защита держится только на токене + правах сокета. | **#23 MITIGATED** |
| **Tamper golden baseline через прямой SQL.** Если права на `state/locators.db` недостаточно рестриктивны, злоумышленник с локальным доступом может подменить записи `golden_snapshots` и вызвать ложный regression-результат. | FS → SQLite | **T** (Tampering) | Вер: L / Влияние: M | **MITIGATED (#24):** записи `golden_snapshots` несут HMAC-SHA256 (колонка `mac`) ключом `state/golden.key` (0600, вне БД); pre-#24 строки MAC'аются один раз при апгрейде (trust-on-first-use), далее каждое чтение **требует** валидный MAC — отсутствующий/неверный (strip, inject, подмена БД) → контролируемый **exit 3** (hard-abort). `plan_hash` сохраняется; права `locators.db` → `0600` (best-effort). | Подмена/правка/strip MAC при сохранённом `golden.key` — обнаруживается. Остаток: same-UID злоумышленник, способный читать или удалить 0600-ключ, может пере-MAC'нуть подмену либо сбросить TOFU удалением ключа. | **#24 MITIGATED** |

### 4.3 Граница ❸ — brain → pw-executor (stdio JSON-RPC / MCP-stdio)

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Подмена RPC-метода или параметров.** Brain передаёт `method`/`params` через stdio. Скомпрометированный brain может вызвать любой `dispatchInner` метод, включая `browser.fill` с произвольными данными в AUT. | brain stdio → pw-executor | **T** (Tampering) | Вер: L / Влияние: M | `dispatch` маршрутизирует только задокументированные `TOOL_METHODS` (switch-case в `dispatchInner`); неизвестные методы → ошибка. Оба процесса — один container, одна security context. | Нет подписи RPC-кадров. Граница защищена только процессной изоляцией. | dev / not prioritized |

### 4.4 Граница ❹/❺ — pw-executor → Chromium → AUT

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **AUT TLS cert error классифицируется, диагностика в heal-report — нет.** При истёкшем или самоподписанном cert Chromium возвращает generic navigation error без указания на причину cert. | pw-executor → AUT HTTPS | **D** (Denial of Service / diagnostic) | Вер: M / Влияние: M | **MITIGATED:** строгость сохранена по умолчанию — `browser.newContext` НЕ ставит `ignoreHTTPSErrors` (`server.ts:464`), обход только явным opt-in `PW_IGNORE_HTTPS_ERRORS=1`. `browser.navigate` ловит отказ `page.goto` и классифицирует его по `ERR_CERT*`/`ERR_SSL*`/`SSL_ERROR*` (`server.ts:612`), возвращая actionable-сообщение с кодом и предупреждением «never for prod auth» (`server.ts:616`). | Диагностика живёт в сообщении о навигации; в `heal-report` cert-разбора нет (`brain/healing.py` — ни одного упоминания cert) — M9.4. В CDP-режиме контекст усыновлён, наши override'ы к нему не применяются (`server.ts:439`), поэтому строгость там задаёт браузер пользователя. | **GAP-OPS-002 MITIGATED**; heal-report diagnostic → M9.4 |
| **AUT DOM-based adversarial content в LLM-промптах.** AUT может разместить в DOM специально сформированные названия элементов или текстовые узлы, которые попадут в planner/heal prompt через `ariaSnapshot → candidates`. Это может повлиять на поведение LLM. | AUT DOM → brain LLM prompt | **T** (Tampering) | Вер: M / Влияние: M | **Частично митигировано**: `LLMPlanner` / `GoalPlanner` используют index-pick grounding (ADR-022/027): LLM выбирает ИНДЕКС в массиве `candidates[]`, сформированных детерминированной `plan`-нодой — LLM не может сгенерировать произвольный selector. `DescribePlanner` выводит `hypothesized_target` по role/name/text с последующим reconcile-матчингом по реальным элементам. | Adversarial content может повлиять на выбор индекса, но не позволяет выйти за пределы discovered элементов. Heal prompt передаёт перечень `interactives` с DOM-именами. ⚠ **Строка «без санитизации» устарела: санитизация есть с M10** — `brain/sanitize.py::safe_json` чистит каждое строковое поле (управляющие и форматирующие символы, BiDi, схлопывание пробелов) и режет его до 300 символов с явной меткой «…». С ADR-136 перечень вдобавок укладывается ЦЕЛЫМИ записями под объявленный бюджет, а остаток называется и в промпте, и в журнале — прежний срез `[:3000]` рвал JSON посреди дескриптора. | dev / M10 (prompt sanitization) |
| **Fingerprinting / rate limiting headless Chromium UA.** AUT может распознать Playwright User-Agent и выдавать упрощённый DOM или отказывать в доступе. | Chromium → AUT | **I** / **D** | Вер: M / Влияние: L | Нет специфических мер. UA настраивается через `extraEnv` вне scope данного документа. | Ложные результаты тестирования — не security угроза Sentinel, но quality угроза. | ops / documented |
| **Утечка PII из AUT UI в артефакты.** `trace.zip` содержит DOM snapshots и screenshots; если AUT отображает персональные данные, они сохраняются в `runs/`. | AUT DOM → runs/trace.zip | **I** (Information Disclosure) | Вер: H / Влияние: M | **MITIGATED (#26):** `runs/` и `runs/<id>/` создаются `0700` (только владелец) — другие локальные пользователи не читают `trace.zip`; retention в `agentctl` оставляет `trace.zip` лишь у `SENTINEL_TRACE_KEEP` свежайших прогонов (по умолч. 10) + TTL `SENTINEL_TRACE_TTL_HOURS`. **Auth runs (GAP-RISK-010):** `PW_NO_TRACE=1` — tracing не запускается (`pw-executor/src/server.ts`, гард вокруг `context.tracing.start`), passwords не в trace; prod — `storageState`. brain логирует только `prompt_hash`. | В пределах окна retention `trace.zip` содержит DOM+screenshots (определяется AUT); encryption-at-rest / PII-redaction — опц. в #26 (не реализовано, на стороне AUT-owner). Same-UID доступ не ограничивается. | **#26 MITIGATED** (perms+retention) |

### 4.5 Граница ❻ — brain → LLM endpoint (cloud / local)

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Утечка AUT page content в cloud LLM provider.** Planner prompt содержит `current_url`, element names, intent; heal prompt — `interactives[]` (DOM-элементы, до 3 000 chars). При cloud backend всё это передаётся Anthropic API / OpenAI / OpenRouter. | brain → cloud LLM HTTPS | **I** (Information Disclosure) | Вер: H (при cloud backend) / Влияние: M | Трейсинг: `prompt_hash()` (`otel.py:14`) — SHA-256 первые 16 hex от prompt, никогда содержимое. Span attributes хранят только token counts. Промпты не логируются в brain stderr. `LLM_BASE_URL` позволяет переключиться на local Ollama/vLLM для data residency. | При cloud backend AUT page structure (URLs, element names) передаётся провайдеру. Нет DLP-фильтрации промптов. Data residency гарантируется только при local endpoint. | ops / documented (backend choice) |
| **Компрометация LLM-ответа (malicious backend / MITM).** `OpenAICompatBackend` делает HTTPS к `base_url`. Скомпрометированный или MITM-перехваченный endpoint может вернуть подделанный ответ. | brain → openai-compat endpoint | **T** (Tampering) / **S** (Spoofing) | Вер: L / Влияние: M | TLS (HTTPS к внешним endpoint). Index-pick grounding ограничивает impact: malicious index вызовет click не на тот элемент, но не RCE. OOB index → brain деградирует к `done` (`planner.py:97`). | Нет certificate pinning для cloud endpoints. | dev / post-M10 |
| **Исчерпание LLM токен-бюджета.** AUT с глубокой навигацией или adversarial DOM может привести к высокому token consumption и финансовым потерям. | brain → LLM billing | **D** (Denial of Service / cost) | Вер: M / Влияние: M | **Митигировано** (ADR-021, `budget.py`): `PLAN_TOKEN_LIMIT` (default 50 000), `HEAL_TOKEN_LIMIT` (default 20 000), `TOTAL_TOKEN_LIMIT` (default 0 = off). При превышении → fallback на heuristic/L1–L6, run продолжается. | Финансовые потери при отключённых лимитах или очень большом AUT. | ops / documented |
| **SSRF через настраиваемый в UI `base_url` (ADR-063).** Token-gated `POST /v1/runs` (и `PUT /v1/config`) принимают `llm.base_url`, который control-API материализует в env прогона → brain делает к нему HTTP-запрос. Скомпрометированный/злонамеренный аутентифицированный клиент мог бы направить прогон на внутренний адрес (cloud-metadata `169.254.169.254`, внутренние сервисы). | UI → control-API → brain → endpoint | **I** (SSRF) / **E** | Вер: L (token-gated) / Влияние: M | **Митигировано (ADR-063):** `validateLLMBase` (общий с `probeLLM`) — только абсолютный http(s), отказ на `user:pass@` (иначе креды ушли бы наружу), блок литерального link-local (`169.254.0.0/16`, `fe80::/10`). RFC1918/loopback (homelab `ollama`/`vllm`/`llama.cpp`) разрешены. Секреты через тело прогона/persisted-конфиг не проходят (`configguard` отвергает `api_key`-подобные ключи; для local подставляется `noauth`). Приоритет process env > per-run > persisted — операторский env не переопределяется. | Валидируется литеральный IP, не DNS-rebinding (как и у `probeLLM`); хостнейм не резолвится. | dev / post-M10 |

### 4.6 Supply chain (cross-cutting)

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Python dependencies без lockfile — закрыто.** `brain/pyproject.toml` объявляет зависимости (`langgraph`, `anthropic`, `openai`, `mcp`, `pyyaml`, `opentelemetry-*`); до M11.1 сборка шла без `uv.lock`, что открывало `pip install` в CI dependency confusion и typosquatting на PyPI. | CI/CD → PyPI | **T** (Tampering) / **E** (Elevation) | Вер: M / Влияние: H | **MITIGATED (M11.1):** `brain/uv.lock` закоммичен в repo; Dockerfile ставит зависимости `uv sync --frozen --no-dev` (Dockerfile:61); `pip-audit` идёт по тому же frozen-экспорту в CI (advisory). Go-модули защищены `go.sum` (content hash verification). Playwright 1.61.1 pinned в TS. | Lockfile закрывает dependency confusion; `pip-audit` — advisory, не hard-fail на найденных CVE. | M11.1 ✅ |
| **SBOM и подпись container image — закрыто; SCA-скан образа — нет.** Production образ ранее не имел прикреплённого SBOM и cosign-подписи — состав было нельзя верифицировать в runtime. | Registry → K8s | **T** (Tampering) | Вер: L / Влияние: H | **MITIGATED (M11.1):** CI генерит CycloneDX SBOM из закреплённого лока (`sbom.cdx.json`, артефакт с `if-no-files-found: error`); релиз подписан **cosign keyless** (Sigstore OIDC, без долгоживущего ключа — образ и архивы, `release.yml`); офлайновый round-trip `sign-blob`/`verify-blob` — гейт `airgap`-джобы. Конвейер отработал живьём на `v0.1.0-rc1`/`v0.1.0` (ADR-110). | **Остаток:** SCA-скан образа (Trivy/Grype) не заведён (`grep -rn "trivy\|grype" .github/ scripts/` → 0 совпадений). | M11.1 ✅ (SCA образа — открыто) |

### 4.7 Артефакты ❼ — `runs/` (целостность и audit)

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **plan.json tampering перед replay.** Если злоумышленник модифицирует `plan.json` на диске между authoring и replay, brain выполнит изменённые шаги. | FS → brain replay | **T** (Tampering) | Вер: L / Влияние: M | `plan_hash` верифицируется перед replay; несоответствие → exit code 3. В K8s план монтируется из ConfigMap. `--ci` запрещает `--force-replay`. | `plan_hash` — хэш самого `plan.json`, не HMAC с ключом: при замене файла хэш обновляется вместе с ним. Защита от случайного повреждения, но не от умышленной подмены. | dev / low priority |
| **Отсутствие audit trail для инициатора run.** Brain logs содержат `prompt_hash` (не content) и step outcomes, но нет записи кто инициировал run, с каким plan, в каком окружении. | brain → runs/transcript | **R** (Repudiation) | Вер: M / Влияние: L | `run_id` присутствует во всех артефактах; `healing_audit` таблица в SQLite хранит полную историю heal. | Нет подписанного audit log. `run_id` — random hex, не связан с user identity в K8s (CronJob не привязан к human identity). | ops / post-M10 |

### 4.8 Граница ❽ — CDP-attach к браузеру пользователя (M9.6)

> Новая поверхность (M9.6, ADR-037). Активна ТОЛЬКО при `PW_CDP_ENDPOINT` — Sentinel подключается к **существующему** Chrome пользователя (`--remote-debugging-port`) и драйвит его живую сессию (его cookies/логин), а не свой headless-инстанс.
>
> ⚠ **Уточнено ADR-128 (2026-08-18), и уточнение НЕ равно сужению.** Прогон больше не ведёт вкладку пользователя: он открывает СВОЮ страницу в его контексте, поэтому чужая вкладка перестала быть предметом наших действий, скриншотов и сбора консоли (ADR-067) и недостижима через `browser.switchTab`. Но `tracing` и `route` в Playwright — свойства **КОНТЕКСТА**, а контекст остаётся усыновлённым, и это ЗАМЕРЕНО, а не предположено: при работающем прогоне запрос чужой вкладки прошёл через нашу route-инъекцию, а её URL и заголовок, выставленный уже ПОСЛЕ старта трейса, оказались в `trace.network` и `trace.trace` нашего `trace.zip`. То есть строка ниже остаётся в силе целиком; изменилось только то, ЧТО именно туда попадает — сетевые и страничные события чужих вкладок, а не наша работа в них. Рычаг прежний: `PW_NO_TRACE=1`. Сужение области инъекции до страниц прогона заведено отдельной задачей `[SEC-CDP-CONTEXT-SCOPE]`.

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Запись живой сессии пользователя в trace.zip.** В CDP-режиме env-gated tracing + traceparent-route применяются к adopted-контексту пользователя → его DOM/скриншоты/запросы могут попасть в `runs/<id>/trace.zip`. | user browser → runs/ | **I** | Вер: M / Влияние: M | `PW_NO_TRACE=1` отключает tracing; раскрыто в `M9.6_CONTRACT` + комментариях кода; CDP-режим — opt-in. **#26:** `runs/` `0700` + retention `trace.zip` применяются и к CDP-трейсам. | Tracing включён по умолчанию (если не `PW_NO_TRACE`); пользователь может не ожидать записи, но артефакт owner-only и подлежит retention. | M9.8 / docs (#26 perms+retention done) |
| **Доступ к чужой сессии через незащищённый CDP-порт.** CDP-endpoint без аутентификации = любой локальный процесс может драйвить браузер; Sentinel переиспользует логин пользователя. | local → CDP `:9222` | **E/I** | Вер: L / Влияние: H | CDP-порт поднимает сам пользователь осознанно (opt-in); localhost-only. | DevTools-протокол не имеет authN — экспозиция порта = полный контроль браузера. **Не экспонировать порт вовне.** | user / docs |

### 4.9 Граница ❾ — браузерное расширение (M9.8, РЕАЛИЗОВАНО — `extension/`, #42-47 + R3 brain-side, ADR-054)

> **Реализовано** (`extension/`, dev-only как `frontend/`) + **brain-side takeover/return** (R3, ADR-054). Крупнейшее расширение surface проекта: MV3-расширение, живущее в браузере пользователя.

| Угроза | Граница | STRIDE | Вер / Влияние | Мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Чтение всех страниц пользователя.** Content-script рекордер видит DOM/ввод на странице в scope; записанные события уходят на brain. | all pages → brain | **I** | Вер: H / Влияние: H | Минимальные permissions (`activeTab`/`storage`/`scripting`); **host-доступ — optional, по запросу на старте записи** (per-origin, не `<all_urls>`); рекордер инжектится только при явном старте; локальный транспорт. **Обязательная redaction**: `type=password`, `autocomplete` cc/one-time-code/password, `data-sentinel-secret`, secret-имена (token-anchored) → пишется `secretRef`, не значение (selectors.ts, jsdom-тесты). | Расширение по природе видит всё в активной вкладке; доверие = ревью кода. | **#42/#44 DONE** |
| **`chrome.debugger` = полный CDP.** Takeover через debugger-API даёт полный контроль страницы. | extension → page | **E** | Вер: M / Влияние: H | `debugger` — **optional permission, запрашивается лениво на takeover-жесте** (не при install); видимый баннер Chrome; auto-detach при return; reconcile повисших attach после SW-эвикции. **Оговорка:** если на вкладке открыт DevTools, второй debugger не приаттачится — attach падает, ошибка в панель (а не half-state). | Полномочия широкие, хоть и с баннером. | **#47 DONE / ADR-039** |
| **Транспорт extension↔brain.** Стриминговый канал — точка инъекции/перехвата. **Реализован (M9.8-prep, ADR-043):** hand-rolled WS `GET /v1/stream` (client→server recorder ingest). | extension → control-API | **S/T** | Вер: M / Влияние: M | Reuse ADR-032: localhost-bind + bearer + **Origin-allowlist (CSWSH-защита)**. Токен в `Sec-WebSocket-Protocol` (`bearer.<token>`, constant-time) — браузерный WS не шлёт `Authorization`; сервер эхает **только** не-секретный сабпротокол `sentinel.recorder.v1` (токен не светится в ответе). Клиент **отказывается слать токен по plaintext `ws://` на non-loopback** (требует `wss`). native-messaging — stdio-альтернатива (без порта, не реализована). | Токен в браузере; WS-порт на localhost. | **M9.8-prep / GAP-M9-14 DONE** |
| **Takeover/return — мутация чужого прогона.** **Реализован (R3, ADR-054):** аутентифицированный `/v1/stream`-клиент шлёт `{"type":"control","action":"takeover\|return","run_id":X}` → control-API форвардит в orchestrator `Takeover`/`Return` для ЛЮБОГО `run_id`; привязки `run_id`↔WS-сессия/`s.runs` НЕТ. | extension → control-API → orchestrator → brain | **S/E** | Вер: M / Влияние: M | Тот же гейт, что транспорт (localhost-bind + bearer + Origin-allowlist); `run_id` — format-guard (`validRunID`: charset + ≤64) против инъекции ключа-мапы; `abort > takeover` (hard-stop приоритетнее паузы); control-фреймы имеют собственный per-session cap. | **Cross-run authorization gap:** держатель токена (напр. скомпрометированное расширение / вредоносная вкладка) может паузить/резюмить чужой прогон. Ownership-binding (`run_id`↔сессия) откладывается до per-run сокет-discovery на **M9-LIVE**. | **R3 (ADR-054) DONE; ownership → M9-LIVE** |

> **Follow-up (ревью #42-47, defense-in-depth, server-side `cmd/control-api/ws.go`):** (1) Origin-проверка `/v1/stream` активна только при непустом `corsAllow` — при пустом allowlist единственный гейт = bearer (оправдано localhost-bind, но стоит явно разрешать только `chrome-extension://` + loopback). (2) Reconnect минтит **новый** `record-<session>` на каждое подключение (`newRunID`), так что обрыв во время записи фрагментирует её на два `events.ndjson` — расширение теперь это **показывает** в статусе, но серверного session-resume пока нет (кандидат в R3). bearer fail-closed, так что это глубина-защиты, не дыра.

### 4.10 Граница ❿ — экспорт live-run артефактов (`scripts/collect-live-run.sh`, M9-LIVE-prep)

> Новая egress-граница (M9-LIVE-prep, `docs/M9_LIVE_PLAN.md` §C): `scripts/collect-live-run.sh` пакует `runs/<id>/` в `live-<id>.tar.gz` для переноса на машину анализа (USB/scp, не git). Редакция включена по умолчанию и применяется к staging-копии — `runs/` не модифицируется.

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **LLM-authoring мог только вписать пароль литералом — теперь несёт `secretRef`.** До ADR-102 authoring-схемы (`brain/planner.py` `_SCHEMA_STEPS`/`_SCHEMA_DRAFT`) содержали только `value`, без `secretRef` — цель вида «залогинься под user/password» материализовалась литеральным паролем в артефактах (и в `trace.zip`, если он включён). | brain authoring → runs/<id>/{plan,scenario}.json → export | **I** (Information Disclosure) | Вер: M / Влияние: H | **MITIGATED (ADR-102):** `secretRef` добавлен в обе authoring-схемы (`_SCHEMA_STEPS`/`_SCHEMA_DRAFT`, `brain/planner.py:80,91`) и в промпты (`:271,330`); fill-only по существующему сквозному контракту (executor резолвит `secretRef` только для `browser.fill`), приоритет безопасен — при секрете сохраняется ссылка, литерал выбрасывается (`brain/scenario.py:57-60`); `secretRef` на не-fill verbs ОТВЕРГАЕТСЯ в `unmatched`, а не теряется молча (`brain/scenario.py:106-107,155-157`). Плюс прежний слой на границе экспорта: `collect-live-run.sh` обнуляет `value`/`text` у `fill\|type\|select\|press`-шагов без `secretRef` (структурный слой, staging-копия) + текстовый sweep auth-заголовков/token-шейпов (Bearer, sk-/ghp_/AKIA/JWT); CI-канарейка `collect-live-run-smoke`. | Схема даёт безопасный путь, но не принуждает: цель, в тексте которой буквально стоит пароль, всё ещё может привести к литералу (промпт-правило мутацией не покрыть — FakeBackend его игнорирует). Экспортный слой остаётся вторым рубежом: shapeless keyword-less секрет в свободнотекстовом non-secret-поле (напр. `reason`) переживает текстовый sweep. | **GAP-SEC-003 MITIGATED** — ADR-102 |
| **`STORAGE_STATE_SAVE` не защищён от записи внутрь `runs/<id>`.** Путь приходит от вызывающего (`brain/runconfig.py` → `pw-executor/src/server.ts`), код-барьера «не в артефакт-каталог» нет. Playwright storage-state = auth cookies + localStorage (session-hijack материал). | brain/runconfig.py → pw-executor write path → export | **I** (Information Disclosure) | Вер: L / Влияние: H | **MITIGATED на границе экспорта:** коллектор безусловно исключает `*state*.json` (даже с `--with-trace`) и громко предупреждает о находке. | На уровне записи барьера всё ещё нет — наивный `tar runs/<id>` в обход коллектора всё равно унёс бы файл. | **GAP-SEC-004** — M10 |
| **Нет on-disk маркера «ран завершён».** Ни `agentctl`, ни brain не пишут признак завершения (`report.json`/`report.html`/`metrics.prom` создаёт отдельный subcommand `report`) → «ран, упавший на 3-м шаге» и «ран в полёте» неотличимы на диске. | agentctl/brain → runs/<id>/ → collector / M15-дашборд | **D** (диагностическая неоднозначность) | Вер: M / Влияние: L | Осознанное решение: коллектор warn'ит об отсутствующих артефактах, но не fail'ит (fail был бы ложноположительным на mid-flight-ране). | Оператор/дашборд не может отличить crash от in-flight без ручной проверки. | **GAP-OPS-006** — post-M9-LIVE |

---

### 4.11 Граница ⓫ — control-API как единственный сервис: отдача UI + выдача токена (ADR-064)

> Новая граница (ADR-064): в **режиме 3** control-API отдаёт браузерный UI со своего же порта (`CONTROL_API_SERVE_UI=1`) и сам заводит себе bearer-токен. Режимы 1 (headless) и 2 (`webui` :8088 + API :8090) не меняются — при пустых `CONTROL_API_SERVE_UI`/`CONTROL_API_UI_DIR` из этого слоя не регистрируется ничего. Same-origin запросы не являются CORS-запросами, поэтому в режиме 3 allowlist можно опустошить (`CONTROL_API_CORS_ORIGINS=`) — это строго **меньшая** поверхность, чем в режиме 2.

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Выдача токена всякому, кто дошёл до порта.** Если бы страница получала токен инъекцией в отдаваемый HTML (или из безусловного эндпоинта), «достижимость порта» стала бы равна «владению токеном», и bearer-гейт ADR-032 на мутациях перестал бы что-либо значить. | клиент → control-API (`GET /`, `GET /v1/ui-token`) | **S/E** | Вер: M / Влияние: H | **Митигировано (ADR-064):** токен выдаётся ТОЛЬКО в обмен на одноразовый нонс, который минтится на старте и печатается исключительно в stderr процесса (терминал оператора). `subtle.ConstantTimeCompare`; сжигание при успехе, истечении TTL (`CONTROL_API_UI_BOOTSTRAP_TTL`, дефолт 5 мин) и после 5 промахов; `Cache-Control: no-store`; гейт `sameOriginRequest` (`Sec-Fetch-Site` + сверка хоста `Origin`) отсекает кросс-сайтовую страницу; при `s.token == ""` эндпоинт не регистрируется вовсе. Страница держит токен в памяти вкладки и вычищает нонс из URL (`history.replaceState`) — ни `localStorage`, ни история. | Кто читает stdout/stderr процесса (общий журнал, `docker compose logs`, CI-лог) в течение TTL — может обменять нонс раньше оператора. Осознанный размен: терминал оператора и так является каналом доставки в режимах 1-2. | ADR-064 / M10 |
| **Токен на диске.** `state/control-api.token` — долгоживущий секрет, переживающий рестарт, на общей с контейнером `./state`-примонтированной директории. | control-API → файловая система | **I** | Вер: L / Влияние: M | Запись атомарная (temp+rename) и с режимом `0600` от момента создания (`os.CreateTemp`), каталог создаётся `0700`; `state/` в `.gitignore`, поэтому в коммит файл не попадает. Существующий, но непригодный/нечитаемый файл НИКОГДА не перезаписывается (может быть данными оператора за `CONTROL_API_TOKEN_FILE`) — процесс уходит на throwaway-токен в памяти и предупреждает. `CONTROL_API_AUTOTOKEN=0` полностью возвращает бестокенный read-only-инстанс. | На Windows Go-режим `0600` ложится на ACL-семантику, а не на POSIX-биты — считать файл user-scoped, на биты не опираться. Шифрование at-rest не делается (тот же уровень, что у `state/golden.key`). | ADR-064 / M10 |
| **Отдача файлов из `docs/`.** В `docs/` рядом с публичными страницами лежит gitignored INTERNAL-ONLY-материал (`*.internal.md`, `COMPETITIVE_ANALYSIS.raw.internal.json`). Наивный FileServer (особенно в dev-режиме `CONTROL_API_UI_DIR`, который смотрит в реальное дерево) отдал бы их наружу; wildcard `go:embed` вшил бы их в бинарь, собранный на машине мейнтейнера, при полностью зелёном CI. | клиент → control-API → embed.FS / диск | **I** | Вер: M / Влияние: M | Явный allowlist в `docs/embed.go` — сами директивы `go:embed` являются единственным авторитетным перечнем и здесь НЕ копируются (рукописная копия рядом с оригиналом расходится молча — принцип 5, `docs/DEVELOPMENT.md` §0) — **плюс** рантайм-фильтр `uiPathAllowed` (`cmd/control-api/ui.go`) поверх обоих источников, плюс полный отказ от листингов каталогов (листинг — единственный способ раскрыть имена соседей). Гейты: `TestEmbeddedUIHasNoInternalDocs` падает, если во вшитое дерево попал путь с `internal` либо `*.md`/`*.docx` (то есть ловит расширение шаблона до `all:.`/`*`); `TestUIDiskSourceFiltersInternalDocs` падает, если дисковый dev-источник (`CONTROL_API_UI_DIR`) отдал что-то мимо фильтра. Прозаические `*.md` не отдаются вовсе — UI линкует их на GitHub. | Allowlist ведётся вручную и в ДВУХ местах: директивы `go:embed` и `uiPathAllowed`. Их равенство не проверяет ни один тест — ассет, добавленный только в одно из них, даёт 404 (отказ безопасный), а добавленный в оба не роняет ничего, поэтому расширение поверхности проходит при зелёном CI, если файл не выглядит internal/`*.md`. | ADR-064 / done |

---

### 4.12 Граница ⓬ — систематический сбор вывода тестируемого приложения (ADR-067) и убийство группы процессов (ADR-069)

> Две границы, появившиеся после ADR-064 и до этой ревизии в модели не описанные. Первая — новая по существу: с ADR-067 продукт **систематически собирает вывод чужого приложения** (консоль, URL упавших запросов, текст диалогов) и кладёт его на диск. Прежде такого канала не существовало: мы писали только собственную диагностику. Вторая — не новая поверхность, а **инвариант**, который держится кодом и обязан держаться при рефакторинге.

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Секреты и ПДн тестируемого приложения попадают в наши логи.** Семь кодов `app.*` (каталог `APP_MESSAGES`, подписки `attachAppCapture` — `pw-executor/src/server.ts`) переносят в `runs/<id>/logs/run.jsonl` то, что приложение само напечатало: `console.log` с токеном сессии, URL с query-параметром `?token=`, текст диалога с ПДн, тело 4xx/5xx-ответа в сообщении об ошибке. Мы этого не выбираем — что печатает приложение, решает не Sentinel. Далее файл читается по `GET /v1/runs/{id}/logs` и попадает в экспорт `collect-live-run.sh`. | AUT → Chromium → pw-executor → logsink → диск / HTTP / экспорт | **I** | Вер: **H** (плохо ведущие себя приложения печатают в консоль секреты постоянно) / Влияние: M | **Редакция на стороне ЗАПИСИ (ADR-081):** единственная точка врезки `logSink.write` — все три файла (`run.log`, `run.jsonl`, `events.jsonl`), редактируется КАЖДАЯ строка, а не только `app.*`, словарь имён общий с конфиг-гардом (`configguard.Secretish`), плюс JWT. Реализовано сканером, а не регексом (совпадение регекса поглощало вложенную пару `console: token=SECRET`). Каталог `app.*`-кодов фиксирован — произвольные строки не эмитятся, только семь известных форм. Приложенческий канал имеет **предел 500 записей** (`app.log_capped` сообщает об усечении, поэтому обрезанный сбор нельзя принять за полный). `runs/` и `runs/<id>/` — `0700` (owner-only, #26). Чтение по HTTP **token-gated** (ADR-032). Экспорт `collect-live-run.sh` **редактирует по умолчанию** (§4.10). | Матчатся только ИМЕНОВАННЫЕ секреты и JWT. Энтропийной эвристики и правила «длинный hex/base64» нет намеренно: они съели бы `run_id`/`plan_hash`/`dom_hash`/голдены — сам аудит-след. Значит бесформенный безымянный секрет редакцию переживает. Retention `SENTINEL_LOG_KEEP`/`SENTINEL_LOG_TTL_HOURS` существует, но ВЫКЛЮЧЕН по умолчанию — осознанно: секреты убирает редакция, тихое удаление чужих улик было бы худшим отказом. | **GAP-SEC-005** / ADR-081 · done |
| **Отмена прогона убивает не то дерево процессов.** `POST /v1/runs/{id}/cancel` (ADR-069) шлёт SIGTERM, а затем SIGKILL **группе процессов**, а не одному процессу — иначе после смерти `agentctl` остаются сироты `brain` и `pw-executor` с живым Chromium (измерено: 2 сироты из 3 без группы, 0 с группой). Если инвариант «группа создаётся на спавне и принадлежит ровно этому прогону» будет нарушен рефакторингом, сигнал уйдёт по группе вызывающего — то есть по всему, что оператор запустил в той же сессии оболочки. | control-API → группа процессов прогона | **D** | Вер: L / Влияние: **H** | Группа создаётся **на спавне** (`procgroup_unix.go` — `Setpgid`, `procgroup_windows.go` — job object), то есть прогон никогда не наследует чужую группу. Сигнал шлётся по отрицательному PGID, равному PID лидера, полученному от нас же. Грация 3 с между TERM и KILL — исполнитель успевает закрыть трассу. Отмена завершившегося прогона отвечает 200 «уже остановлен», а не сигналит в переиспользованный PGID. | Инвариант держится **соглашением, а не тестом на границе**: `cancel_test.go` проверяет поведение отмены, но не проверяет, что PGID лидера ≠ PGID control-API. Мутационно измерено на живом прогоне (8 chromium → 0), но регрессия при рефакторинге спавна не будет поймана автоматически. | **новый GAP-OPS-007** / M10 |

---

### 4.13 Граница ⓭ — сервисный журнал как СВИДЕТЕЛЬСТВО (HEALTH-005 · ADR-116)

> Граница, появившаяся вместе с журналом. До HEALTH-005 сервисный план не логировался нигде, и это само было дырой: ADR-109 ввёл локальную идентичность, а идентичность без следа операций — слабость. Теперь след есть, и у него есть собственные границы, которые важно назвать, потому что аудит, чьи пределы не объявлены, читают как гарантию.

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Журнал уничтожают, и это не оставляет следа.** `state/logs/service.jsonl` — обычный файл. Тот, у кого есть доступ к диску под тем же uid, может его переписать или удалить, и никакой записи об этом не появится. | оператор/злоумышленник → ФС → журнал | **R** (repudiation) | Вер: L / Влияние: **H** | Штатный путь удаления — `agentctl purge-service`, и он **пишет о себе** (`service.log_purged`, `warn`, со счётчиками): «удаляли ли что-нибудь?» перестаёт отвечаться молчанием. Автоматической зачистки нет нигде — та же поза, что у ADR-098/100. Перезапись держит `flock`, поэтому параллельная запись не теряется, а не «обычно не теряется». | **Внешнего удаления это не ловит.** Ни подписи цепочки записей, ни выноса на другой носитель, ни append-only-режима ФС. 0640 — пол, а не потолок: кто дошёл до диска, дошёл и до журнала. Для регулируемого покупателя нужен внешний приёмник (syslog/OTLP), которого нет. | **новый GAP-SEC-006** / после HEALTH-006 |
| **Журнал показывает аккаунту чужое.** Одна запись на всё развёртывание, читается по HTTP и из CLI. Наивная выдача опубликовала бы топологию и расписание операций владельцу слабейшего credential. | аккаунт → `GET /v1/service-log` | **I** | Вер: M / Влияние: M | Скоуп **по записи** (ADR-109): машина и админ видят всё; обычный аккаунт — только записи, где владелец он; событие БЕЗ владельца (старт сервиса, глобальный конфиг, отказ стора) — только админу. Правило инвертирует «бесхозное видно всем» намеренно и с обоснованием в коде. Предикат применяется ДО счётчика `matched`, поэтому счётчики не разглашают объём скрытого. Частичный вид называет себя частичным. | Скоуп опирается на поле `Owner` записи. Событие, записанное БЕЗ владельца там, где он известен, становится админским молча — **именно так и было** с успешным входом, пока живая проверка не показала, что аккаунт не видит записи о собственном входе. Гейт теперь идёт через продукт целиком, но класс остаётся: новый эмиттер легко забудет субъект. | HEALTH-005 (закрыто в PR-B) / класс — открыт |
| **Запись сервиса браузера теряется при чистке.** Он пишет из Node, где `flock` недоступен без нативного модуля. | browser (Node) → журнал ↔ purge (Go) | **R** | Вер: L / Влияние: L | Перезапись при чистке **дочитывает хвост**, дописанный с момента снимка, поэтому окно сведено к одному сисколлу вместо всей длительности перезаписи. Go-писатели берут блокировку и не теряют ничего. | Окно не ноль, и оно объявлено здесь и в `docs/OBSERVABILITY.md` §8, а не оставлено на обнаружение. | заявленная граница, не GAP |
| **Чужие сервисы в журнал не попадают вовсе.** `ollama`, `litellm`, `webui` пишут своим форматом в журнал докера. | чужие образы → docker | **—** | — | Им докеровская ротация (`logging:` с `max-size`/`max-file`, PR-C) и `docker compose logs`. | **Осознанный отказ, а не пробел:** разобрать чужой вывод в структуру, которой у него нет, — придумать данные. Кто расследует инцидент с участием этих сервисов, обязан знать, что смотреть надо в двух местах. | заявленная граница, не GAP |

---

### 4.14 Граница ⓮ — плоскость идентичности control-API (ADR-109)

Появилась после того, как §4.11 была написана, и до этого аудита не была описана ни строкой —
модель говорила о control-API как о сервисе с ОДНИМ машинным токеном, а сервис успел обзавестись
учётными записями, сессиями и правом владения строками.

| Угроза (STRIDE) | Вектор | Текущая защита | Остаточный риск |
|---|---|---|---|
| **S** — подбор пароля | `POST /v1/login` объявлен `accessOpen`, то есть подбор идёт БЕЗ токена | Ответ одинаков для неверного имени и неверного пароля (перечисление аккаунтов невозможно); хеш — не быстрый | ⚠ Ограничения частоты НЕТ. Открытая ручка, принимающая пароль, — это ровно то место, где его и подбирают |
| **E** — чужие строки | Аккаунт запрашивает строку другого владельца | `guard` резолвит владельца по `{id}` ДО обработчика (ADR-109 вторая половина); список и агрегаты скоупятся В обработчике, потому что `{id}` у них нет | Строка БЕЗ владельца видна всем — осознанное правило для строк, созданных до появления аккаунтов |
| **I** — утечка через агрегат | `GET /metrics` суммирует прогоны развёртывания | `accessAuthed` + фильтр владельца в обработчике; скоуп отказывает ЗАКРЫТО (недоступный стор ⇒ прогон не атрибутирован ⇒ не показан) | Машинный токен видит всё — это и есть скрейп оператора, и он должен быть так настроен |
| **R** — отрицание действия | Кто создал или удалил аккаунт | Сервисный журнал (§4.13) пишет каждый вызов из ОДНОГО места | Журнал не подписан: см. §4.13 |

### 4.15 Граница ⓯ — чужой текст, попадающий НЕ через каталог событий

§4.12 объявляет границей «систематический сбор вывода тестируемого приложения» и перечисляет коды
`app.*`. Замер показал, что это описывает **один** канал из двух, и второй уже приводил к утечке.

`trace.network` хранит заголовки **парами** `{"name": …, "value": …}` — имя заголовка является
ЗНАЧЕНИЕМ члена, а не ключом. Правило редактирования, написанное «по ключу», такой заголовок не
видит вовсе: `X-Api-Key` и `Cookie` уезжали в артефакт как есть. Хуже, отказ был **невидим**:
`Authorization: Bearer …` вычищался другим правилом — по форме значения, — и создавал впечатление,
что канал закрыт.

| Угроза (STRIDE) | Вектор | Текущая защита | Остаточный риск |
|---|---|---|---|
| **I** — секрет в артефакте | Заголовки запросов AUT внутри `trace.zip` | Исправлено в обходе, а не в словаре: редактор ходит по ПАРАМ и решает по имени соседа (#215). Проверено на настоящем архиве: 0 канареек | Секрет в СОБСТВЕННОМ исходнике страницы приложения — граница, а не долг: энтропийная эвристика отвергнута, потому что `run_id`/`plan_hash` имеют ту же форму |

**Урок, который эта граница фиксирует, а не только случай:** канал чужого текста надо описывать по
СТРУКТУРЕ хранения, а не по имени подсистемы. Правило «секретное — по ключу» и хранилище
«пары {name,value}» несовместимы молча.

### 4.16 Граница ⓰ — живой вид: кадры браузера, проксируемые под учётными данными (ADR-111)

> Поверхность появилась с ADR-111 и в модели не была описана ни строкой. Браузер стал сервисом и
> отдаёт СВОЙ screencast (`pw-executor/src/cdp-service.ts`), а control-API делает то, ради чего он
> там стоит, — ставит перед ним учётные данные: `GET /v1/live/{status,frame.jpg,mjpeg}`,
> `accessAuthed`. Поверхность существует только когда задан `CONTROL_API_CDP_LIVE`
> (`cmd/control-api/live.go`, `liveBase()`); пустое значение — штатное одноконтейнерное
> развёртывание, в котором живого вида нет вовсе.

| Угроза (STRIDE) | Вектор | Текущая защита | Остаточный риск |
|---|---|---|---|
| **I** — картинка чужой работы | Кадр показывает то, что открыто в браузере, включая залогиненное тестируемое приложение | Маршрут И ЕСТЬ учётные данные (`accessAuthed`); `CONTROL_API_CDP_LIVE` пуст по умолчанию | `run_id` выбирает, ЧЬЮ страницу показать, но владельца прогона не проверяет никто: аутентифицированный запрос получает кадр любого названного прогона. Скоуп по владельцу здесь не обещан — картинка принадлежит браузерному СЕРВИСУ, общему по конструкции (причина записана рядом с маршрутами в `cmd/control-api/access.go`) |
| **E** — обход единственного гейта | Живой порт браузерного сервиса собственных учётных данных НЕ имеет — ровно как его CDP-порт | Порт живёт во внутренней сети и на хост не публикуется (шапка `cmd/control-api/live.go`) | Публикация этого порта наружу снимает защиту ЦЕЛИКОМ: control-API — единственный гейт, и мимо него кадры отдаются кому угодно без проверки |

### 4.17 Граница ⓱ — экран как сервис: RFB на unix-сокете (LIVE-VNC, ADR-127)

> Поверхность появляется вместе с профилем `vnc` и при выключенном профиле НЕ СУЩЕСТВУЕТ: не
> поднимается `browser-vnc`, не создаётся `state/vnc.sock`, `CONTROL_API_VNC_SOCK` пуст, а проба
> `/readyz` отвечает `skipped`. Отличие от границы ⓰ не в масштабе: там наружу шли КАДРЫ под
> учётными данными control-API, здесь наружу идёт РАБОЧИЙ СТОЛ, и он **принимает ввод**.
>
> ⚠ **Переделано 2026-08-18.** Первая редакция описывала RFB по TCP с паролем; CodeQL справедливо
> назвал это слабой криптографией (VNC-аутентификация = DES по первым восьми байтам над
> нешифрованным каналом). Решение владельца продукта: слабых шифров не поставляем. Замер дал замену
> строго сильнее — `-unixsock … -rfbport 0`: TCP-слушателя нет вовсе, security type `None`, полная
> сессия работает, а доступ решают права файла (замерено: чужой uid получает `EACCES`).

| Угроза (STRIDE) | Вектор | Текущая защита | Остаточный риск |
|---|---|---|---|
| **S/E — чужая мышь в живом сеансе** | Кто дошёл до сокета, получает клавиатуру и мышь в браузере, уже залогиненном в тестируемое приложение под cookies пользователя. Это не «посмотреть»: это takeover, и в журнале ТЕСТИРУЕМОГО приложения это будут действия пользователя, а не робота. | Сокет создаётся с `umask 077` и ЯВНО сужается `chmod 600` до того, как entrypoint передаёт управление браузерному сервису; ядро отказывает чужому uid до первого байта RFB. TCP-порта нет вовсе (`-rfbport 0`) — это утверждает гейт, а не комментарий. Единственный путь из браузера — `GET /v1/live/screen` за bearer-токеном. ⚠ Реле САМО проверяет режим сокета и ОТКАЗЫВАЕТСЯ отдавать экран, если права шире 0600: утверждение «права и есть аутентификация» проверяется в рантайме, а не только записано. | Кто владеет тем же uid на хосте (или может выполнить команду в контейнере), доходит до сокета — но такой доступ уже равносилен доступу к браузеру. Стенд не является многопользовательским хостом; это то же допущение, что у `runs/` и `state/`. |
| **I — содержимое экрана** | По сокету идут пиксели рабочего стола и нажатия клавиш. | Данные не покидают хост: AF_UNIX не маршрутизируется, шифровать нечего. ⚠ Ровно это и было главной претензией к прежней схеме: там канал шёл по TCP открытым текстом, и «порт не опубликован» не спасало — замерено 2026-08-17, прежний RFB-порт отвечал на bridge-IP контейнера ПРЯМО С ХОСТА. | Внутри контейнера содержимое доступно любому процессу того же uid — как и сам X-дисплей. |
| **T — X-сервер как вторая дверь** | Xvfb мог бы слушать TCP, и тогда к тому же дисплею вёл бы второй путь. | `-nolisten tcp`: у X-сервера нет сетевого сокета вовсе; x11vnc ходит к нему через `/tmp/.X11-unix/X99` внутри контейнера. Утверждается гейтом. | — |
| **S — подмена сервера** | Реле подключается к пути в общем томе; кто может писать в `state/`, мог бы подставить свой сокет. | `state/` монтируется только в наши контейнеры и принадлежит оператору (`user: ${UID}:${GID}`), права каталога 0700 у создающего кода. | Тот, кто может писать в `state/`, уже может подменить БД стора и файл токена control-API — это не новая граница, а та же (§4.11). |
| **E — слабый шифр как соблазн** | Вернуть `-passwdfile`/`-rfbauth` «для глубины защиты» означало бы вернуть DES. | Гейт `tests/test_vnc_profile_offline.py` отвергает оба флага в любом поставляемом файле, а реле ОТКАЗЫВАЕТСЯ говорить с сервером, который не предлагает `None`, и называет причину. | — |

---

## 5. Сводная таблица GAP-трекинга

> **Словарь колонки «Статус».** До 2026-08-10 его здесь не было вовсе — блок «Обозначения» в §4
> определяет только оси вероятности и влияния, — и ярлык нечем было ни проверить, ни отличить один
> от другого. Из-за этого `GAP-SEC-001` нёс `CLOSED`, а тело той же ячейки тут же его отзывало
> («остаток … открыто»); при чтении по колонке — а это единственное назначение сводной таблицы —
> P1/HIGH читался как закрытый. Допустимые значения:
>
> | Ярлык | Что означает |
> |---|---|
> | `MITIGATED` | Мера внедрена и работает; остатка, требующего работы, нет |
> | `MITIGATED (<квалификатор>)` | Закрыт названный рубеж, первопричина лежит ЗА границей и остаётся |
> | `PARTIALLY OPEN` | Часть объёма сделана, часть прямо заявлена невыполненной |
> | `OPEN` | Не сделано |
>
> ⚠ `CLOSED` в этой колонке **не употребляется**: «закрыто с остатком» — противоречие, а не
> квалификатор. Гэп, у которого остаток назван, — это `PARTIALLY OPEN`.

| GAP ID | Статус | STRIDE | Severity | Краткое описание | Owner / Milestone |
|---|---|---|---|---|---|
| **GAP-RISK-010** | **PARTIALLY OPEN — остаток назван (правка 2026-08-21, `[DOCS-REGISTERS]`)** | I | — | Утечка-в-трейс: трейсинг отключён (`PW_NO_TRACE`) на auth-прогонах; секреты по env-var NAME через secretRef; brain redacts logs; fail-closed при активном трейсинге; prod использует storageState; содержимое `trace.zip` очищается перед сохранением, неочищаемый трейс удаляется (ADR-098). ⚠ Здесь стояло `MITIGATED` — по правилу этого же документа («гэп, у которого остаток назван, — это `PARTIALLY OPEN`») это неверно: ОТКРЫТО — пиксели `resources/*.jpeg` (решено не чистить, рычаг `SENTINEL_TRACE_SCREENSHOTS=0`), окно между записью архива Playwright'ом и очисткой, политика хранения и уже записанный в БД чужой текст. Статус реестра — `GAPS.md`. | — |
| **GAP-SEC-001** | **PARTIALLY OPEN — Helm-половина и #25 закрыты (M11.3/ADR-035)** | I | HIGH | env-allowlist **default-on** (opt-out `SENTINEL_ENV_ALLOWLIST=0`) + Helm `secretKeyRef` + `sentinel.envAllow`. **#25 CLOSED:** `NODE_`/`GIT_` больше не префиксы — `NODE_OPTIONS`/`NODE_EXTRA_CA_CERTS`/`GIT_SSL_CAINFO`/`GIT_SSL_CAPATH` exact-allowlisted (`TestFilteredEnvPrefixNarrowing`). **Остаток:** только динамические секреты Vault/CSI-driver. | done |
| **#23 store-gateway authN** | **MITIGATED** | E | MEDIUM | per-run token authN в gRPC-metadata (`TokenAuthInterceptor`) + SO_PEERCRED + сокет 0600; unit-тест `TestTokenAuthInterceptor`. | done; #23 → 0xCoDSnet |
| **#24 golden integrity** | **MITIGATED** | T | MEDIUM | HMAC `golden_snapshots` (ключ `state/golden.key`, вне БД); tamper → exit 3; тесты `TestGoldenIntegrityTamper` + `test_golden_mac_tamper_detected_exit3`. | done; #24 → 0xCoDSnet |
| **#26 trace.zip PII** | **MITIGATED** | I | MEDIUM | `runs/` + `runs/<id>/` → `0700` (owner-only); retention `trace.zip` (`SENTINEL_TRACE_KEEP`=10 / `SENTINEL_TRACE_TTL_HOURS`); тесты `TestMkArtifactDirPerms`/`TestSweepTraces*`. Encryption/redaction — опц., не реализовано. | done; #26 → 0xCoDSnet |
| **GAP-SEC-002** | **MITIGATED — остаток: SCA образа** | T, E | HIGH | Lockfile закреплён в repo (`brain/uv.lock`), Dockerfile ставит `uv sync --frozen --no-dev` (Dockerfile:61), `pip-audit` идёт по тому же frozen-экспорту (advisory, ci.yml:471-476), CycloneDX SBOM генерится в CI из закреплённого лока (`sbom.cdx.json`, артефакт с `if-no-files-found: error`), релиз подписан **cosign keyless** (Sigstore OIDC, без долгоживущего ключа; release.yml), а `airgap` держит офлайновый round-trip `sign-blob`/`verify-blob` как гейт механизма (ci.yml:575-584). Конвейер отработал живьём на `v0.1.0-rc1`/`v0.1.0` (ADR-110). **Остаток:** SCA-скан образа (Trivy/Grype) не заведён. | done (лок/SBOM/подпись) · SCA образа — открыто |
| **GAP-OPS-002** | **MITIGATED** | D | MEDIUM | `PW_IGNORE_HTTPS_ERRORS` opt-in + cert-классификация (`ERR_CERT*`) в `browser.navigate` (этот цикл); строго по умолчанию. Расширенный diagnostic в heal-report — M9.4. | M9.4 |
| **GAP-SEC-003** | **MITIGATED (ADR-102 + граница экспорта)** | I | LOW | Первопричина закрыта: `secretRef` в обеих authoring-схемах (`brain/planner.py:80,91`) и в промптах (`:271,330`), fill-only по сквозному контракту, на не-fill ОТВЕРГАЕТСЯ в `unmatched`. Плюс прежний слой: `collect-live-run.sh` обнуляет `value`/`text` у `fill\|type\|select\|press`-шагов без `secretRef` + текстовый sweep. **Остаток:** схема не принуждает (промпт мутацией не покрыть — FakeBackend его игнорирует). | ADR-102 |
| **GAP-SEC-004** | **MITIGATED (граница экспорта)** | I | MEDIUM | Коллектор безусловно исключает `*state*.json` (даже с `--with-trace`) + громкий warn. Код-уровневого барьера на запись `STORAGE_STATE_SAVE` внутрь `runs/<id>` пока нет. | M10 |
| **GAP-SEC-005** | **MITIGATED (ADR-081)** | I | MEDIUM | Вывод тестируемого приложения (семь кодов `app.*`, ADR-067) писался в `runs/<id>/logs/` как пришёл. Закрыто редакцией **на стороне записи**: одна точка врезки `logSink.write`, каждая строка всех трёх файлов, словарь имён общий с `configguard.Secretish`. Энтропийной эвристики намеренно нет — она съела бы `run_id`/`plan_hash`/`dom_hash`/голдены. Retention `logs/` (`SENTINEL_LOG_KEEP`/`SENTINEL_LOG_TTL_HOURS`) выключен по умолчанию, осознанно. **Остаток:** безымянный бесформенный секрет редакцию переживает. | ADR-081 / done |
| **GAP-SEC-006** | **OPEN** | R | MEDIUM | Сервисный журнал — обычный файл: внешнее удаление или переписывание следа не оставляет. Штатная чистка о себе пишет (`service.log_purged`), но ни цепочки хешей, ни выноса на другой носитель, ни append-only нет. Для регулируемого покупателя нужен внешний приёмник (syslog/OTLP). | после HEALTH-006 |
| **GAP-OPS-006** | **OPEN** | D | LOW | Нет on-disk маркера завершения рана; коллектор/будущий M15-дашборд не отличают crash от in-flight рана (коллектор осознанно warn'ит, а не fail'ит на отсутствующих артефактах). | post-M9-LIVE |
| **GAP-OPS-007** | **OPEN** | D | LOW | Инвариант «группа процессов создаётся на спавне и принадлежит ровно этому прогону» держится соглашением, а не тестом на границе: `cancel_test.go` проверяет поведение отмены, но не проверяет, что PGID лидера ≠ PGID control-API. Измерено мутацией на живом прогоне (8 chromium → 0), но регрессия при рефакторинге спавна автоматически не поймается. | M10 |

---

## 6. Рекомендованные меры (Roadmap)

Следующие меры **не реализованы** в текущей кодовой базе. Указаны как planned/milestone.

1. ~~**GAP-SEC-001 — env allowlist**~~ — **DONE (M11.3 / ADR-035):** `filteredEnv()` переведён в default-on (opt-out `SENTINEL_ENV_ALLOWLIST=0`) + curated-список. **#25 CLOSED:** `NODE_`/`GIT_` убраны как префиксы — `NODE_OPTIONS`/`NODE_EXTRA_CA_CERTS`/`GIT_SSL_CAINFO`/`GIT_SSL_CAPATH` exact-allowlisted (`TestFilteredEnvPrefixNarrowing`; список — в теле `filteredEnv`, `cmd/agentctl/main.go`). **Остаток:** только динамические секреты Vault/CSI-driver — открыто.
2. ~~**GAP-SEC-001 — Helm secretKeyRef**~~ — **DONE (M11.3):** `secrets.*` → `valueFrom.secretKeyRef` при `secrets.enabled` (plaintext-fallback в dev); helper `sentinel.envAllow`; `deploy/flux/`.
3. ~~**GAP-SEC-002 — Python lockfile**~~ — **DONE (M11.1):** `uv lock` в CI; `uv.lock` закоммичен (`brain/uv.lock`); Dockerfile использует `uv sync --frozen --no-dev`; `pip-audit` идёт по frozen-экспорту.
4. **GAP-SEC-002 — SCA scan образа**: SBOM (`syft`/CycloneDX) и подпись (`cosign` keyless) уже сделаны в CI/release (M11.1) — остаётся добавить Trivy/Grype SCA-скан container image в CI pipeline (`grep -rn "trivy\|grype" .github/ scripts/` → 0 совпадений).
5. ~~**GAP-OPS-002 — cert diagnostic**~~ — **DONE:** cert-классификация (`ERR_CERT*`/`ERR_SSL*`) в `browser.navigate` + opt-in `PW_IGNORE_HTTPS_ERRORS` (строго по умолчанию).
6. **Prompt sanitization**: strip управляющих символов и ограничение длины element names/intent перед включением в LLM-промпты (`healing.py:_llm_reground`, `planner.py:propose`).
7. ~~**`runs/` access control**~~ — **DONE (#26):** `runs/` и `runs/<id>/` → `0700` (agentctl + brain); retention `trace.zip` в `agentctl` (`SENTINEL_TRACE_KEEP` / `SENTINEL_TRACE_TTL_HOURS`), задокументирована в `docs/OUTPUTS.md`. Опц. (не реализовано): encryption-at-rest / PII-redaction. Актуально и для CDP-режима ❽.
8. ~~**store-gateway integrity** (граница ❷)~~ — **DONE (#23/#24):** per-run token authN в gRPC-metadata (`TokenAuthInterceptor`) + SO_PEERCRED + сокет `0600`; HMAC-целостность `golden_snapshots` (ключ `state/golden.key` вне БД) с верификацией при replay (tamper → exit 3).
9. **расширение (M9.8, ❾, реализовано `extension/`):** минимальные permissions + lazy host/`debugger` (по запросу на жесте), обязательная redaction секретов в рекордере, debugger-attach только по takeover-жесте с видимым баннером, локальный транспорт (control-API token; отказ от plaintext `ws://` на non-loopback) — см. `M9.8_CONTRACT` + ADR-038/039.
10. ~~**GAP-SEC-003 — `secretRef` в authoring-схеме**~~ — **DONE (2026-07-28, ADR-102):** `secretRef` в `_SCHEMA_STEPS`/`_SCHEMA_DRAFT` + промпт-правило; fill-only, на не-fill отвергается в `unmatched`. Остаток: схема даёт безопасный путь, но не принуждает.
11. **GAP-SEC-004 — код-барьер на `STORAGE_STATE_SAVE`**: отклонять путь сохранения внутри артефакт-каталога на уровне кода (по образцу `isUnder`-гарда в `cmd/agentctl`), не полагаясь только на исключение в коллекторе.
12. **GAP-OPS-006 — маркер завершения рана**: `agentctl` пишет `status.json` (exit-код) на выходе, чтобы отличить упавший ран от in-flight.

---

## 7. Ссылки

- Политика раскрытия уязвимостей: [`SECURITY.md`](../SECURITY.md)
- ADR-019 (провайдер-агностичные LLM backends): [`docs/M6_CONTRACT.md`](M6_CONTRACT.md)
- ADR-022/027 (index-pick grounding, GoalPlanner): [`docs/M9.2_CONTRACT.md`](M9.2_CONTRACT.md)
- ADR-015 (store-gateway, single SQLite writer): [`docs/M2b_CONTRACT.md`](M2b_CONTRACT.md)
- ADR-026 / GAP-RISK-010 (storageState, PW_NO_TRACE): [`docs/M9.1_CONTRACT.md`](M9.1_CONTRACT.md)
- ADR-021 (token budgets): [`docs/M8_CONTRACT.md`](M8_CONTRACT.md)
- M9-LIVE-prep (экспорт live-run артефактов, редакция, GAP-SEC-003/004, GAP-OPS-006): [`docs/M9_LIVE_PLAN.md`](M9_LIVE_PLAN.md) §C · `scripts/collect-live-run.sh`
