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
| **Утечка всех host secrets в дочерние процессы.** До M11.3 `agentctl::spawnBrain` вызывал `cmd.Env = append(os.Environ(), …)` без allowlist (историческая цитата; теперь на этом месте `filteredEnv()`, cmd/agentctl/main.go:172–219). Все переменные хоста (SSH-ключи, облачные credentials, не относящиеся к Sentinel токены) наследуются Python brain, Node.js pw-executor и их подпроцессами, а также могут попасть в stderr при ошибке. | host-env → brain subprocess | **I** (Information Disclosure) | Вер: H / Влияние: H | **MITIGATED (M11.3/ADR-035):** env-allowlist default-on (`filteredEnv`; opt-out `SENTINEL_ENV_ALLOWLIST=0`) | **GAP-SEC-001 CLOSED (Helm-half)**; остаток — динамические Vault/CSI | M11.3 ✅ |
| **Plaintext secrets в Helm values → Kubernetes.** `cronjob.yaml:39–46` использует `value: {{ .Values.checkpointDsn }}` и `{{range .Values.extraEnv}} value: {{ $v }}` без `secretKeyRef`. CHECKPOINT_DSN и extraEnv хранятся как строки в `values-prod.yaml`, попадают в etcd в открытом виде и видны через `kubectl describe pod`. | Helm chart → K8s etcd | **I** (Information Disclosure) | Вер: H / Влияние: H | **MITIGATED (M11.3/ADR-035):** `secretKeyRef` plumbing (chart `secrets.*`) | **GAP-SEC-001 CLOSED (Helm-half)** | M11.3 ✅ |

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
| **AUT TLS cert error не классифицируется.** `browser.newContext` (`server.ts:100`) не устанавливает `ignoreHTTPSErrors`. При истёкшем или самоподписанном cert Chromium возвращает generic navigation error без указания на причину cert. | pw-executor → AUT HTTPS | **D** (Denial of Service / diagnostic) | Вер: M / Влияние: M | Явное архитектурное решение: не игнорировать cert errors (лучшая практика безопасности). `browser.navigate` возвращает `{ status: null }` при navigation failure. | **GAP-OPS-002 OPEN**: оператор видит `NavigationError`, а не `NET::ERR_CERT_DATE_INVALID`. Нет actionable cert diagnostic в heal-report. | M9.4 |
| **AUT DOM-based adversarial content в LLM-промптах.** AUT может разместить в DOM специально сформированные названия элементов или текстовые узлы, которые попадут в planner/heal prompt через `ariaSnapshot → candidates`. Это может повлиять на поведение LLM. | AUT DOM → brain LLM prompt | **T** (Tampering) | Вер: M / Влияние: M | **Частично митигировано**: `LLMPlanner` / `GoalPlanner` используют index-pick grounding (ADR-022/027): LLM выбирает ИНДЕКС в массиве `candidates[]`, сформированных детерминированной `plan`-нодой — LLM не может сгенерировать произвольный selector. `DescribePlanner` выводит `hypothesized_target` по role/name/text с последующим reconcile-матчингом по реальным элементам. | Adversarial content может повлиять на выбор индекса, но не позволяет выйти за пределы discovered элементов. Heal prompt (`healing.py:122`) передаёт `interactives[][:3000]` с DOM-именами — имена элементов попадают в LLM без санитизации. | dev / M10 (prompt sanitization) |
| **Fingerprinting / rate limiting headless Chromium UA.** AUT может распознать Playwright User-Agent и выдавать упрощённый DOM или отказывать в доступе. | Chromium → AUT | **I** / **D** | Вер: M / Влияние: L | Нет специфических мер. UA настраивается через `extraEnv` вне scope данного документа. | Ложные результаты тестирования — не security угроза Sentinel, но quality угроза. | ops / documented |
| **Утечка PII из AUT UI в артефакты.** `trace.zip` содержит DOM snapshots и screenshots; если AUT отображает персональные данные, они сохраняются в `runs/`. | AUT DOM → runs/trace.zip | **I** (Information Disclosure) | Вер: H / Влияние: M | **MITIGATED (#26):** `runs/` и `runs/<id>/` создаются `0700` (только владелец) — другие локальные пользователи не читают `trace.zip`; retention в `agentctl` оставляет `trace.zip` лишь у `SENTINEL_TRACE_KEEP` свежайших прогонов (по умолч. 10) + TTL `SENTINEL_TRACE_TTL_HOURS`. **Auth runs (GAP-RISK-010):** `PW_NO_TRACE=1` — tracing не запускается (`server.ts:108`), passwords не в trace; prod — `storageState`. brain логирует только `prompt_hash`. | В пределах окна retention `trace.zip` содержит DOM+screenshots (определяется AUT); encryption-at-rest / PII-redaction — опц. в #26 (не реализовано, на стороне AUT-owner). Same-UID доступ не ограничивается. | **#26 MITIGATED** (perms+retention) |

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
| **Python dependencies без lockfile.** `brain/pyproject.toml` объявляет зависимости (`langgraph`, `anthropic`, `openai`, `mcp`, `pyyaml`, `opentelemetry-*`) без `uv.lock` или requirements с hash-pinning. `pip install` в CI без `--require-hashes` уязвим к dependency confusion и typosquatting на PyPI. | CI/CD → PyPI | **T** (Tampering) / **E** (Elevation) | Вер: M / Влияние: H | Go-модули защищены `go.sum` (content hash verification). Playwright 1.61.1 pinned в TS. **§1 (этот цикл):** gitleaks/govulncheck/pip-audit/npm audit добавлены в CI (pip-audit advisory + freeze-артефакт `requirements.lock`); committed lockfile/SBOM/cosign остаются для M11.1. | **GAP-SEC-002 PARTIALLY OPEN**: SCA/SBOM/lockfile в работе для CI, но Python lockfile не зафиксирован в repo на данный момент. | M11.1 |
| **Нет SBOM и подписи container image.** Production образ не имеет прикреплённого SBOM и cosign-подписи — нельзя верифицировать состав в runtime. | Registry → K8s | **T** (Tampering) | Вер: L / Влияние: H | Нет | **GAP-SEC-002 OPEN**: нет SBOM generation в CI pipeline. | M11.1 |

### 4.7 Артефакты ❼ — `runs/` (целостность и audit)

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **plan.json tampering перед replay.** Если злоумышленник модифицирует `plan.json` на диске между authoring и replay, brain выполнит изменённые шаги. | FS → brain replay | **T** (Tampering) | Вер: L / Влияние: M | `plan_hash` верифицируется перед replay; несоответствие → exit code 3. В K8s план монтируется из ConfigMap. `--ci` запрещает `--force-replay`. | `plan_hash` — хэш самого `plan.json`, не HMAC с ключом: при замене файла хэш обновляется вместе с ним. Защита от случайного повреждения, но не от умышленной подмены. | dev / low priority |
| **Отсутствие audit trail для инициатора run.** Brain logs содержат `prompt_hash` (не content) и step outcomes, но нет записи кто инициировал run, с каким plan, в каком окружении. | brain → runs/transcript | **R** (Repudiation) | Вер: M / Влияние: L | `run_id` присутствует во всех артефактах; `healing_audit` таблица в SQLite хранит полную историю heal. | Нет подписанного audit log. `run_id` — random hex, не связан с user identity в K8s (CronJob не привязан к human identity). | ops / post-M10 |

### 4.8 Граница ❽ — CDP-attach к браузеру пользователя (M9.6)

> Новая поверхность (M9.6, ADR-037). Активна ТОЛЬКО при `PW_CDP_ENDPOINT` — Sentinel подключается к **существующему** Chrome пользователя (`--remote-debugging-port`) и драйвит его живую сессию (его cookies/логин), а не свой headless-инстанс.

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
| **LLM-authoring не умеет `secretRef` → плейнтекст-креды в plan.json/scenario.json.** Authoring-схемы (`brain/planner.py` `_SCHEMA_STEPS`/`_SCHEMA_DRAFT`) содержат только `value`, без `secretRef` — цель вида «залогинься под user/password» материализуется литеральным паролем в артефактах (и в `trace.zip`, если он включён). | brain authoring → runs/<id>/{plan,scenario}.json → export | **I** (Information Disclosure) | Вер: H / Влияние: H | **MITIGATED на границе экспорта:** `collect-live-run.sh` обнуляет `value`/`text` у `fill\|type\|select\|press`-шагов без `secretRef` (структурный слой, staging-копия) + текстовый sweep auth-заголовков/token-шейпов (Bearer, sk-/ghp_/AKIA/JWT); CI-канарейка `collect-live-run-smoke` проверяет это. | Первопричина не устранена: сам `runs/<id>/plan.json` на диске остаётся плейнтекстом — митигация только на границе экспорта, не на authoring-время. Известный потолок: shapeless keyword-less секрет в свободнотекстовом non-secret-поле (напр. `reason`) переживает текстовый sweep. | **GAP-SEC-003** — M9-LIVE / M10 |
| **`STORAGE_STATE_SAVE` не защищён от записи внутрь `runs/<id>`.** Путь приходит от вызывающего (`brain/runconfig.py` → `pw-executor/src/server.ts`), код-барьера «не в артефакт-каталог» нет. Playwright storage-state = auth cookies + localStorage (session-hijack материал). | brain/runconfig.py → pw-executor write path → export | **I** (Information Disclosure) | Вер: L / Влияние: H | **MITIGATED на границе экспорта:** коллектор безусловно исключает `*state*.json` (даже с `--with-trace`) и громко предупреждает о находке. | На уровне записи барьера всё ещё нет — наивный `tar runs/<id>` в обход коллектора всё равно унёс бы файл. | **GAP-SEC-004** — M10 |
| **Нет on-disk маркера «ран завершён».** Ни `agentctl`, ни brain не пишут признак завершения (`report.json`/`report.html`/`metrics.prom` создаёт отдельный subcommand `report`) → «ран, упавший на 3-м шаге» и «ран в полёте» неотличимы на диске. | agentctl/brain → runs/<id>/ → collector / M15-дашборд | **D** (диагностическая неоднозначность) | Вер: M / Влияние: L | Осознанное решение: коллектор warn'ит об отсутствующих артефактах, но не fail'ит (fail был бы ложноположительным на mid-flight-ране). | Оператор/дашборд не может отличить crash от in-flight без ручной проверки. | **GAP-OPS-006** — post-M9-LIVE |

---

### 4.11 Граница ⓫ — control-API как единственный сервис: отдача UI + выдача токена (ADR-064)

> Новая граница (ADR-064): в **режиме 3** control-API отдаёт браузерный UI со своего же порта (`CONTROL_API_SERVE_UI=1`) и сам заводит себе bearer-токен. Режимы 1 (headless) и 2 (`webui` :8088 + API :8090) не меняются — при пустых `CONTROL_API_SERVE_UI`/`CONTROL_API_UI_DIR` из этого слоя не регистрируется ничего. Same-origin запросы не являются CORS-запросами, поэтому в режиме 3 allowlist можно опустошить (`CONTROL_API_CORS_ORIGINS=`) — это строго **меньшая** поверхность, чем в режиме 2.

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Выдача токена всякому, кто дошёл до порта.** Если бы страница получала токен инъекцией в отдаваемый HTML (или из безусловного эндпоинта), «достижимость порта» стала бы равна «владению токеном», и bearer-гейт ADR-032 на мутациях перестал бы что-либо значить. | клиент → control-API (`GET /`, `GET /v1/ui-token`) | **S/E** | Вер: M / Влияние: H | **Митигировано (ADR-064):** токен выдаётся ТОЛЬКО в обмен на одноразовый нонс, который минтится на старте и печатается исключительно в stderr процесса (терминал оператора). `subtle.ConstantTimeCompare`; сжигание при успехе, истечении TTL (`CONTROL_API_UI_BOOTSTRAP_TTL`, дефолт 5 мин) и после 5 промахов; `Cache-Control: no-store`; гейт `sameOriginRequest` (`Sec-Fetch-Site` + сверка хоста `Origin`) отсекает кросс-сайтовую страницу; при `s.token == ""` эндпоинт не регистрируется вовсе. Страница держит токен в памяти вкладки и вычищает нонс из URL (`history.replaceState`) — ни `localStorage`, ни история. | Кто читает stdout/stderr процесса (общий журнал, `docker compose logs`, CI-лог) в течение TTL — может обменять нонс раньше оператора. Осознанный размен: терминал оператора и так является каналом доставки в режимах 1-2. | ADR-064 / M10 |
| **Токен на диске.** `state/control-api.token` — долгоживущий секрет, переживающий рестарт, на общей с контейнером `./state`-примонтированной директории. | control-API → файловая система | **I** | Вер: L / Влияние: M | Запись атомарная (temp+rename) и с режимом `0600` от момента создания (`os.CreateTemp`), каталог создаётся `0700`; `state/` в `.gitignore`, поэтому в коммит файл не попадает. Существующий, но непригодный/нечитаемый файл НИКОГДА не перезаписывается (может быть данными оператора за `CONTROL_API_TOKEN_FILE`) — процесс уходит на throwaway-токен в памяти и предупреждает. `CONTROL_API_AUTOTOKEN=0` полностью возвращает бестокенный read-only-инстанс. | На Windows Go-режим `0600` ложится на ACL-семантику, а не на POSIX-биты — считать файл user-scoped, на биты не опираться. Шифрование at-rest не делается (тот же уровень, что у `state/golden.key`). | ADR-064 / M10 |
| **Отдача файлов из `docs/`.** В `docs/` рядом с публичными страницами лежит gitignored INTERNAL-ONLY-материал (`*.internal.md`, `COMPETITIVE_ANALYSIS.raw.internal.json`). Наивный FileServer (особенно в dev-режиме `CONTROL_API_UI_DIR`, который смотрит в реальное дерево) отдал бы их наружу; wildcard `go:embed` вшил бы их в бинарь, собранный на машине мейнтейнера, при полностью зелёном CI. | клиент → control-API → embed.FS / диск | **I** | Вер: M / Влияние: M | Явный allowlist в `docs/embed.go` (`index.html`, `prices.json`, `backend-presets.json`, `setup/`, `chat/`, `calculators/`) **плюс** рантайм-фильтр `uiPathAllowed` поверх обоих источников, плюс полный отказ от листингов каталогов (листинг — единственный способ раскрыть имена соседей). Гейты: `TestEmbeddedUIHasNoInternalDocs` и `TestUIDiskSourceFiltersInternalDocs`. Прозаические `*.md` не отдаются вовсе — UI линкует их на GitHub. | Allowlist ведётся вручную: новый публичный ассет надо добавить в него явно (иначе 404 — отказ безопасный). | ADR-064 / done |

---

### 4.12 Граница ⓬ — систематический сбор вывода тестируемого приложения (ADR-067) и убийство группы процессов (ADR-069)

> Две границы, появившиеся после ADR-064 и до этой ревизии в модели не описанные. Первая — новая по существу: с ADR-067 продукт **систематически собирает вывод чужого приложения** (консоль, URL упавших запросов, текст диалогов) и кладёт его на диск. Прежде такого канала не существовало: мы писали только собственную диагностику. Вторая — не новая поверхность, а **инвариант**, который держится кодом и обязан держаться при рефакторинге.

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Секреты и ПДн тестируемого приложения попадают в наши логи.** Семь кодов `app.*` (`pw-executor/src/server.ts:79-96`) переносят в `runs/<id>/logs/run.jsonl` то, что приложение само напечатало: `console.log` с токеном сессии, URL с query-параметром `?token=`, текст диалога с ПДн, тело 4xx/5xx-ответа в сообщении об ошибке. Мы этого не выбираем — что печатает приложение, решает не Sentinel. Далее файл читается по `GET /v1/runs/{id}/logs` и попадает в экспорт `collect-live-run.sh`. | AUT → Chromium → pw-executor → logsink → диск / HTTP / экспорт | **I** | Вер: **H** (плохо ведущие себя приложения печатают в консоль секреты постоянно) / Влияние: M | **Частично.** Каталог фиксирован — произвольные строки не эмитятся, только семь известных форм. Приложенческий канал имеет **предел 500 записей** (`app.log_capped` сообщает об усечении, поэтому обрезанный сбор нельзя принять за полный). `runs/` и `runs/<id>/` — `0700` (owner-only, #26). Чтение по HTTP **token-gated** (ADR-032). Экспорт `collect-live-run.sh` **редактирует по умолчанию** (§4.10). | **Редактирования на стороне записи нет.** `logsink.go` кладёт сообщение как пришло; фильтра по форме секрета (`Bearer `, `eyJ`, `?token=`, длинные hex/base64) на входе нет — в отличие от `brain`, который свои логи редактирует. Значит `run.jsonl` может содержать секрет приложения в открытом виде, и защищает только `0700` + токен на чтении. Для регулируемого покупателя это **недостаточно**: политика хранения не задана, TTL для `logs/` не определён (в отличие от `trace.zip`, где есть `SENTINEL_TRACE_KEEP`/`SENTINEL_TRACE_TTL_HOURS`). | **новый GAP-SEC-005** / M10 |
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

## 5. Сводная таблица GAP-трекинга

| GAP ID | Статус | STRIDE | Severity | Краткое описание | Owner / Milestone |
|---|---|---|---|---|---|
| **GAP-RISK-010** | **MITIGATED** | I | — | Утечка-в-трейс: трейсинг отключён (`PW_NO_TRACE`) на auth-прогонах; секреты по env-var NAME через secretRef; brain redacts logs; fail-closed при активном трейсинге; prod использует storageState. | — |
| **GAP-SEC-001** | **CLOSED — Helm-половина + #25 (M11.3/ADR-035)** | I | HIGH | env-allowlist **default-on** (opt-out `SENTINEL_ENV_ALLOWLIST=0`) + Helm `secretKeyRef` + `sentinel.envAllow`. **#25 CLOSED:** `NODE_`/`GIT_` больше не префиксы — `NODE_OPTIONS`/`NODE_EXTRA_CA_CERTS`/`GIT_SSL_CAINFO`/`GIT_SSL_CAPATH` exact-allowlisted (`TestFilteredEnvPrefixNarrowing`). **Остаток:** только динамические секреты Vault/CSI-driver. | done |
| **#23 store-gateway authN** | **MITIGATED** | E | MEDIUM | per-run token authN в gRPC-metadata (`TokenAuthInterceptor`) + SO_PEERCRED + сокет 0600; unit-тест `TestTokenAuthInterceptor`. | done; #23 → 0xCoDSnet |
| **#24 golden integrity** | **MITIGATED** | T | MEDIUM | HMAC `golden_snapshots` (ключ `state/golden.key`, вне БД); tamper → exit 3; тесты `TestGoldenIntegrityTamper` + `test_golden_mac_tamper_detected_exit3`. | done; #24 → 0xCoDSnet |
| **#26 trace.zip PII** | **MITIGATED** | I | MEDIUM | `runs/` + `runs/<id>/` → `0700` (owner-only); retention `trace.zip` (`SENTINEL_TRACE_KEEP`=10 / `SENTINEL_TRACE_TTL_HOURS`); тесты `TestMkArtifactDirPerms`/`TestSweepTraces*`. Encryption/redaction — опц., не реализовано. | done; #26 → 0xCoDSnet |
| **GAP-SEC-002** | **PARTIALLY OPEN** | T, E | HIGH | Python no lockfile, no SBOM, no image signing. | M11.1 |
| **GAP-OPS-002** | **MITIGATED** | D | MEDIUM | `PW_IGNORE_HTTPS_ERRORS` opt-in + cert-классификация (`ERR_CERT*`) в `browser.navigate` (этот цикл); строго по умолчанию. Расширенный diagnostic в heal-report — M9.4. | M9.4 |
| **GAP-SEC-003** | **MITIGATED (граница экспорта)** | I | MEDIUM | `scripts/collect-live-run.sh` обнуляет `value`/`text` у `fill\|type\|select\|press`-шагов без `secretRef` (структурная блокировка + текстовый sweep на staging-копии); CI-канарейка `collect-live-run-smoke`. Первопричина (authoring-схема без `secretRef`) остаётся открытой. | M9-LIVE / M10 |
| **GAP-SEC-004** | **MITIGATED (граница экспорта)** | I | MEDIUM | Коллектор безусловно исключает `*state*.json` (даже с `--with-trace`) + громкий warn. Код-уровневого барьера на запись `STORAGE_STATE_SAVE` внутрь `runs/<id>` пока нет. | M10 |
| **GAP-SEC-006** | **OPEN** | R | MEDIUM | Сервисный журнал — обычный файл: внешнее удаление или переписывание следа не оставляет. Штатная чистка о себе пишет (`service.log_purged`), но ни цепочки хешей, ни выноса на другой носитель, ни append-only нет. Для регулируемого покупателя нужен внешний приёмник (syslog/OTLP). | после HEALTH-006 |
| **GAP-OPS-006** | **OPEN** | D | LOW | Нет on-disk маркера завершения рана; коллектор/будущий M15-дашборд не отличают crash от in-flight рана (коллектор осознанно warn'ит, а не fail'ит на отсутствующих артефактах). | post-M9-LIVE |

---

## 6. Рекомендованные меры (Roadmap)

Следующие меры **не реализованы** в текущей кодовой базе. Указаны как planned/milestone.

1. ~~**GAP-SEC-001 — env allowlist**~~ — **DONE (M11.3 / ADR-035):** `filteredEnv()` переведён в default-on (opt-out `SENTINEL_ENV_ALLOWLIST=0`) + curated-список. **#25 CLOSED:** `NODE_`/`GIT_` убраны как префиксы — `NODE_OPTIONS`/`NODE_EXTRA_CA_CERTS`/`GIT_SSL_CAINFO`/`GIT_SSL_CAPATH` exact-allowlisted (`TestFilteredEnvPrefixNarrowing`, cmd/agentctl/main.go:190–191). **Остаток:** только динамические секреты Vault/CSI-driver — открыто.
2. ~~**GAP-SEC-001 — Helm secretKeyRef**~~ — **DONE (M11.3):** `secrets.*` → `valueFrom.secretKeyRef` при `secrets.enabled` (plaintext-fallback в dev); helper `sentinel.envAllow`; `deploy/flux/`.
3. **GAP-SEC-002 — Python lockfile**: добавить `uv lock` в CI, зафиксировать `uv.lock` в repo, в Dockerfile использовать `uv sync --frozen` или pip с `--require-hashes`.
4. **GAP-SEC-002 — SCA + SBOM + image signing**: добавить Trivy/Grype SCA scan в CI pipeline; `syft` для генерации SBOM; `cosign` для подписи образа.
5. ~~**GAP-OPS-002 — cert diagnostic**~~ — **DONE:** cert-классификация (`ERR_CERT*`/`ERR_SSL*`) в `browser.navigate` + opt-in `PW_IGNORE_HTTPS_ERRORS` (строго по умолчанию).
6. **Prompt sanitization**: strip управляющих символов и ограничение длины element names/intent перед включением в LLM-промпты (`healing.py:_llm_reground`, `planner.py:propose`).
7. ~~**`runs/` access control**~~ — **DONE (#26):** `runs/` и `runs/<id>/` → `0700` (agentctl + brain); retention `trace.zip` в `agentctl` (`SENTINEL_TRACE_KEEP` / `SENTINEL_TRACE_TTL_HOURS`), задокументирована в `docs/OUTPUTS.md`. Опц. (не реализовано): encryption-at-rest / PII-redaction. Актуально и для CDP-режима ❽.
8. ~~**store-gateway integrity** (граница ❷)~~ — **DONE (#23/#24):** per-run token authN в gRPC-metadata (`TokenAuthInterceptor`) + SO_PEERCRED + сокет `0600`; HMAC-целостность `golden_snapshots` (ключ `state/golden.key` вне БД) с верификацией при replay (tamper → exit 3).
9. **расширение (M9.8, ❾, реализовано `extension/`):** минимальные permissions + lazy host/`debugger` (по запросу на жесте), обязательная redaction секретов в рекордере, debugger-attach только по takeover-жесте с видимым баннером, локальный транспорт (control-API token; отказ от plaintext `ws://` на non-loopback) — см. `M9.8_CONTRACT` + ADR-038/039.
10. **GAP-SEC-003 — `secretRef` в authoring-схеме**: добавить `secretRef` в `brain/planner.py` `_SCHEMA_STEPS`/`_SCHEMA_DRAFT` + промпт-правило против инлайна credentials, чтобы LLM-авторинг логина перестал материализовывать пароль литералом в `plan.json`/`scenario.json`.
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
