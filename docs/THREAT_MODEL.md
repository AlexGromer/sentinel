# Threat Model — Sentinel

> 🌐 **Русский** (основная версия) · [English](THREAT_MODEL.en.md)

> **Версия**: 1.0 | **Дата**: 2026-06-27 | **Авторы**: appsec-engineer (auto), @AlexGromer
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

---

## 4. STRIDE-lite: таблица угроз

> **Обозначения**: Вер(оятность) H/M/L без существующих мер; Влия(ние) H/M/L на активы.
> GAP-ID соответствуют записям в BACKLOG/GAPS.

### 4.1 Граница ❶ — host-env → agentctl → brain (full env inherit)

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Утечка всех host secrets в дочерние процессы.** `agentctl::spawnBrain` вызывает `cmd.Env = append(os.Environ(), …)` без allowlist (`main.go:68`). Все переменные хоста (SSH-ключи, облачные credentials, не относящиеся к Sentinel токены) наследуются Python brain, Node.js pw-executor и их подпроцессами, а также могут попасть в stderr при ошибке. | host-env → brain subprocess | **I** (Information Disclosure) | Вер: H / Влияние: H | Нет | **GAP-SEC-001 OPEN**: нет env allowlist | M11.3 (env allowlist) |
| **Plaintext secrets в Helm values → Kubernetes.** `cronjob.yaml:39–46` использует `value: {{ .Values.checkpointDsn }}` и `{{range .Values.extraEnv}} value: {{ $v }}` без `secretKeyRef`. CHECKPOINT_DSN и extraEnv хранятся как строки в `values-prod.yaml`, попадают в etcd в открытом виде и видны через `kubectl describe pod`. | Helm chart → K8s etcd | **I** (Information Disclosure) | Вер: H / Влияние: H | Нет | **GAP-SEC-001 OPEN**: нет `secretKeyRef` plumbing | M11.3 (Helm secretKeyRef) |

### 4.2 Граница ❷ — agentctl → store-gateway (Unix gRPC socket)

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Несанкционированный доступ к Unix-сокету.** Любой локальный процесс с правами того же UID может вызвать gRPC-методы store-gateway: записать/удалить golden baseline или locator cache без аутентификации. | local FS / Unix socket | **E** (Elevation of Privilege) | Вер: L / Влияние: M | Сокет создаётся в `state/` под repo root (не в `/tmp`); защита — Unix FS permissions (наследует umask). gRPC-сервер экспонирует только `PersistenceService`. | Нет mTLS, нет authN между brain и gateway. Эксплуатируется только при уже скомпрометированном хосте. | dev / post-M10 |
| **Tamper golden baseline через прямой SQL.** Если права на `state/locators.db` недостаточно рестриктивны, злоумышленник с локальным доступом может подменить записи `golden_snapshots` и вызвать ложный regression-результат. | FS → SQLite | **T** (Tampering) | Вер: L / Влияние: M | `plan_hash` проверяется перед replay; несоответствие → exit code 3. `agentctl baseline update` — единственный задокументированный mutation path (`main.go:9`). | `golden_snapshots` не имеют MAC/signing. При замене SQLite-файла целиком `plan_hash` не защищает (он хранится в plan.json, а не в БД). | dev / post-M10 |

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
| **Утечка PII из AUT UI в артефакты.** `trace.zip` содержит DOM snapshots и screenshots; если AUT отображает персональные данные, они сохраняются в `runs/`. | AUT DOM → runs/trace.zip | **I** (Information Disclosure) | Вер: H / Влияние: M | **Auth runs MITIGATED (GAP-RISK-010)**: `PW_NO_TRACE=1` на auth runs — tracing не запускается (`server.ts:108`); typed passwords не попадают в trace. Prod runs используют `storageState` (пароль не вводится). brain логирует только `prompt_hash`, никогда — page content. | Обычные explore/replay runs записывают `trace.zip` с DOM и screenshots. Содержимое определяется AUT. Нет encryption at rest для `runs/`. | ops / classified by AUT owner |

### 4.5 Граница ❻ — brain → LLM endpoint (cloud / local)

| Угроза | Граница | STRIDE | Вер / Влияние | Существующая мера | Остаточный риск | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Утечка AUT page content в cloud LLM provider.** Planner prompt содержит `current_url`, element names, intent; heal prompt — `interactives[]` (DOM-элементы, до 3 000 chars). При cloud backend всё это передаётся Anthropic API / OpenAI / OpenRouter. | brain → cloud LLM HTTPS | **I** (Information Disclosure) | Вер: H (при cloud backend) / Влияние: M | Трейсинг: `prompt_hash()` (`otel.py:14`) — SHA-256 первые 16 hex от prompt, никогда содержимое. Span attributes хранят только token counts. Промпты не логируются в brain stderr. `LLM_BASE_URL` позволяет переключиться на local Ollama/vLLM для data residency. | При cloud backend AUT page structure (URLs, element names) передаётся провайдеру. Нет DLP-фильтрации промптов. Data residency гарантируется только при local endpoint. | ops / documented (backend choice) |
| **Компрометация LLM-ответа (malicious backend / MITM).** `OpenAICompatBackend` делает HTTPS к `base_url`. Скомпрометированный или MITM-перехваченный endpoint может вернуть подделанный ответ. | brain → openai-compat endpoint | **T** (Tampering) / **S** (Spoofing) | Вер: L / Влияние: M | TLS (HTTPS к внешним endpoint). Index-pick grounding ограничивает impact: malicious index вызовет click не на тот элемент, но не RCE. OOB index → brain деградирует к `done` (`planner.py:97`). | Нет certificate pinning для cloud endpoints. | dev / post-M10 |
| **Исчерпание LLM токен-бюджета.** AUT с глубокой навигацией или adversarial DOM может привести к высокому token consumption и финансовым потерям. | brain → LLM billing | **D** (Denial of Service / cost) | Вер: M / Влияние: M | **Митигировано** (ADR-021, `budget.py`): `PLAN_TOKEN_LIMIT` (default 50 000), `HEAL_TOKEN_LIMIT` (default 20 000), `TOTAL_TOKEN_LIMIT` (default 0 = off). При превышении → fallback на heuristic/L1–L6, run продолжается. | Финансовые потери при отключённых лимитах или очень большом AUT. | ops / documented |

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

---

## 5. Сводная таблица GAP-трекинга

| GAP ID | Статус | STRIDE | Severity | Краткое описание | Owner / Milestone |
|---|---|---|---|---|---|
| **GAP-RISK-010** | **MITIGATED** | I | — | Утечка-в-трейс: трейсинг отключён (`PW_NO_TRACE`) на auth-прогонах; секреты по env-var NAME через secretRef; brain redacts logs; fail-closed при активном трейсинге; prod использует storageState. | — |
| **GAP-SEC-001** | **OPEN** | I | HIGH | Full env inherit (`main.go:68`) + Helm plaintext secrets (`cronjob.yaml:39–46`). | M11.3 (env allowlist) |
| **GAP-SEC-002** | **PARTIALLY OPEN** | T, E | HIGH | Python no lockfile, no SBOM, no image signing. | M11.1 |
| **GAP-OPS-002** | **OPEN** | D | MEDIUM | AUT cert error не классифицируется — нет actionable diagnostic в heal-report. | M9.4 |

---

## 6. Рекомендованные меры (Roadmap)

Следующие меры **не реализованы** в текущей кодовой базе. Указаны как planned/milestone.

1. **GAP-SEC-001 — env allowlist**: в `agentctl/main.go::spawnBrain` заменить `os.Environ()` на explicit allowlist переменных, нужных brain (`TARGET_URL`, `RUN_MODE`, `LLM_*`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CHECKPOINT_DSN`, `STORE_ADDR`, `PYTHONPATH`, `PATH`, …). Всё прочее — отрезать.
2. **GAP-SEC-001 — Helm secretKeyRef**: переписать env-блок `cronjob.yaml` для чувствительных переменных через `valueFrom.secretKeyRef`. Удалить `checkpointDsn` и секреты из `values-prod.yaml`, вынести в отдельный K8s Secret.
3. **GAP-SEC-002 — Python lockfile**: добавить `uv lock` в CI, зафиксировать `uv.lock` в repo, в Dockerfile использовать `uv sync --frozen` или pip с `--require-hashes`.
4. **GAP-SEC-002 — SCA + SBOM + image signing**: добавить Trivy/Grype SCA scan в CI pipeline; `syft` для генерации SBOM; `cosign` для подписи образа.
5. **GAP-OPS-002 — cert diagnostic**: в `browser.navigate` / `dispatchInner` обработать net-error class и вернуть классифицированную ошибку (`cert_expired`, `cert_invalid`) в `heal-report.json`.
6. **Prompt sanitization**: strip управляющих символов и ограничение длины element names/intent перед включением в LLM-промпты (`healing.py:_llm_reground`, `planner.py:propose`).
7. **`runs/` access control**: ограничить права чтения директории `runs/` до UID Sentinel-процесса; задокументировать retention policy для `trace.zip`.

---

## 7. Ссылки

- Политика раскрытия уязвимостей: [`SECURITY.md`](../SECURITY.md)
- ADR-019 (провайдер-агностичные LLM backends): [`docs/M6_CONTRACT.md`](M6_CONTRACT.md)
- ADR-022/027 (index-pick grounding, GoalPlanner): [`docs/M9.2_CONTRACT.md`](M9.2_CONTRACT.md)
- ADR-015 (store-gateway, single SQLite writer): [`docs/M2b_CONTRACT.md`](M2b_CONTRACT.md)
- ADR-026 / GAP-RISK-010 (storageState, PW_NO_TRACE): [`docs/M9.1_CONTRACT.md`](M9.1_CONTRACT.md)
- ADR-021 (token budgets): [`docs/M8_CONTRACT.md`](M8_CONTRACT.md)
