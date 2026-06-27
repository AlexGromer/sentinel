# Дистрибуция и онбординг — EPIC Contract (ADR-030 / ADR-031)

🌐 [English version](DISTRIBUTION.en.md)

> **Статус**: контракт заморожен | **Дата**: 2026-06-27
> **ADR**: ADR-030 (стратегия дистрибуции) · ADR-031 (setup-WebUI)
> **Эпик**: M11.1–M11.5 — секвенированный; большинство не строится в этом цикле
> **Авторы**: system-architect agent, @AlexGromer

---

## §1 Введение и область охвата

### Что доставлено в этом цикле (Foundation)

Foundation-цикл закрыл три предварительных условия, без которых публичный релиз не заслуживает доверия:

| Доставлено | Что закрывает |
|---|---|
| Security CI-гейты: gitleaks (hard) + govulncheck (hard) + pip-audit (advisory + freeze-артефакт) + npm audit (critical) + `go vet`/`go test` + offline-suite m3..m9_2b | GAP-SEC-002 (частично): SCA-сканирование в CI — предпосылка для доверия к бинарникам |
| `docker-compose.yml` one-command quickstart (sentinel + demo + ollama profiles) | Первый zero-external-dependency путь онбординга |
| GitHub Pages (docs/index.md + 3 калькулятора: VRAM · token-cost · model-selector) + `docs/LOCAL_MODELS.md` + `docs/TESTING.md` | Air-gapped документация; калькуляторы работают без сети |
| `docs/THREAT_MODEL.md` | Модель угроз как предпосылка для секурного релиза |

**Остальное секвенировано в M11.1–M11.5.** Каждый milestone не начинается без обновления этого контракта и соответствующего ADR.

### Обоснование секвенирования (ADR-030)

Релиз без hardening (SCA/SBOM/lockfile/подпись + threat-model) не заслуживает доверия. Поэтому:

```
Foundation hardening → Releases + подписи (M11.1)
                     → setup-WebUI MVP (M11.2, static-only)
                     → Helm Secret plumbing (M11.3, закрывает GAP-SEC-001)
                     → Air-gapped bundle (M11.4)
                     → Zero-level installer + QUICKSTART (M11.5)
```

Альтернатива «всё сразу одним релизом» отклонена: 4–5 milestone'ов across release-eng / containers / GitOps / frontend — высокий integration-risk при одновременной поставке.

---

## §2 docker-compose quickstart (DONE — этот цикл)

### Что уже работает

Файл `docker-compose.yml` в корне репозитория предоставляет three-service quickstart без установки Go/Python/Node:

```
docker compose build                                      # собрать образ один раз
docker compose run --rm sentinel --help                   # справка agentctl
docker compose run --rm sentinel run \
    --target "https://your-app.example.com"              # explore против реального AUT
docker compose --profile demo up                          # zero-dep демо (fixture file://)
docker compose --profile ollama up -d ollama             # локальная модель (OpenAI-compat)
docker compose --profile webui up                        # setup-WebUI + калькуляторы локально → localhost:8088/setup/
```

### Сервисы

| Сервис | Profile | Назначение |
|---|---|---|
| `sentinel` | (всегда) | Основная точка входа — `agentctl` CLI. По умолчанию печатает `--help`. Монтирует `./runs`, `./state`, `./config`. |
| `demo` | `demo` | Zero-external-dependency explore против `testdata/site/index.html` (fixture file://); heuristic planner (без LLM, без API-ключа). Результат: `./runs/demo/plan.json`. |
| `webui` | `webui` | Локальный air-gapped **setup-WebUI + калькуляторы** (забандлены в образ под `/app/docs`); `python -m http.server` на :8088. Открыть `http://localhost:8088/setup/`. ADR-031 фаза-1. |
| `ollama` | `ollama` | OpenAI-compatible endpoint `http://ollama:11434/v1`. Запустить: `docker compose --profile ollama up -d ollama`, затем `docker compose exec ollama ollama pull <model>`. |

### Переменные окружения

Env-блок задаётся в `docker-compose.yml` или передаётся через `.env` файл:

```yaml
# Cloud (Anthropic) — без ключа → offline heuristic + L1–L6 heal
ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}

# Локальная модель (активировать, убрав комментарии):
# LLM_BACKEND: openai
# LLM_BASE_URL: http://ollama:11434/v1
# LLM_MODEL: qwen2.5:7b           # из каталога docs/LOCAL_MODELS.md §5c
# LLM_API_KEY: noauth             # Ollama игнорирует ключ; SDK требует непустое значение
# LLM_VISION: 0                   # 1 только для vision-capable heal модели
```

Полная матрица env-переменных (per-role `_PLANNER`/`_HEAL` суффиксы, приоритет) — `docs/LOCAL_MODELS.md`.

### Тестовые fixtures

Профиль `demo` использует `testdata/site/index.html`. Для градуированных сценариев (форма, логин, shadow-DOM):

```bash
docker compose run --rm sentinel run \
    --target "file:///app/testdata/fixtures/l2.html" \
    --planner heuristic
```

Каталог fixtures: `testdata/fixtures/l1..l5.html` — см. `testdata/fixtures/README.md` для описания уровней L1–L5.

### Монтируемые тома

| Том | Хост-путь | Назначение |
|---|---|---|
| runs | `./runs` | plan.json, transcript, heal-report, scenario.json, trace.zip |
| state | `./state` | SQLite locator/golden/quarantine DB + store-gateway socket |
| config | `./config` | RunConfig YAML или plan.json (`--run-config /config/run.yaml`) |

### Полное руководство

`docs/TESTING.md` — подробные инструкции: offline gates, local-model setup, интерпретация артефактов, exit codes.

---

## §3 M11.1 — GitHub Releases: мульти-OS/arch бинарники + Docker + подписи

**Статус:** не начат. Предпосылки: Foundation CI-гейты (DONE).

### Что поставляется

Четыре Go-бинарника (`agentctl`, `store-gateway`, `orchestrator`, `report-service`) для пяти платформ:

| Платформа | GOOS | GOARCH |
|---|---|---|
| Linux x86-64 | linux | amd64 |
| Linux ARM64 | linux | arm64 |
| macOS Apple Silicon | darwin | arm64 |
| macOS Intel | darwin | amd64 |
| Windows x86-64 | windows | amd64 |

Итого: 20 бинарников + Docker-образ (multi-arch: linux/amd64 + linux/arm64).

### CI workflow: `release.yml`

Триггер: `push` к тегу `v*` (например, `v1.0.0`).

Шаги:
1. `go build -ldflags "-X main.Version=$TAG"` для каждой платформы (matrix).
2. Генерация `sentinel-$TAG-$OS-$ARCH.tar.gz` + `.sha256` per-artifact.
3. Единый `checksums.sha256` (SHA-256 для всех архивов) — верифицируется через `sha256sum -c checksums.sha256`.
4. **Cosign keyless signing** (Sigstore OIDC): `cosign sign-blob --bundle=...` для каждого архива. Верификация: `cosign verify-blob --bundle=... --certificate-identity-regexp=... artifact.tar.gz`.
5. **Docker buildx + GHCR**: `docker buildx build --platform linux/amd64,linux/arm64 --push -t ghcr.io/alexgromer/sentinel:$TAG .`
6. **SBOM**: `syft ghcr.io/alexgromer/sentinel:$TAG -o cyclonedx-json > sbom.cdx.json`; аттачится к Release как asset.
7. GitHub Release создаётся через `gh release create` с аттачами всех артефактов.

### Оставшиеся GAP-SEC-002 пункты, закрываемые M11.1

| Пункт | Действие |
|---|---|
| Нет committed lockfile | `uv lock` → `uv.lock` коммитится; `pip-audit --requirement uv.lock` в CI |
| Нет SBOM | `syft` генерирует CycloneDX JSON — аттачится к GitHub Release |
| Нет подписей релиза | Cosign keyless подпись каждого архива + Docker image |

### Критерии приёмки M11.1

- [ ] GitHub Release содержит 20 бинарников (5 платформ × 4 бинарника) в `.tar.gz`
- [ ] `checksums.sha256` присутствует и проходит `sha256sum -c checksums.sha256`
- [ ] Cosign bundle верифицируется: `cosign verify-blob --bundle=sentinel.bundle sentinel.tar.gz`
- [ ] Docker образ доступен на `ghcr.io/alexgromer/sentinel:<tag>` для linux/amd64 + linux/arm64
- [ ] SBOM (CycloneDX JSON) аттачен к Release
- [ ] `uv.lock` закоммичен; `pip-audit` проходит в CI на основе lockfile
- [ ] CI workflow `release.yml` триггерится на тег `v*` и проходит без ошибок

---

## §4 M11.2 — setup-WebUI: статический генератор конфигурации (ADR-031)

**Статус:** не начат. Зависит от: М11.1 (чтобы ссылаться на реальные релизы). Предпосылки: GitHub Pages (DONE).

### Решение (ADR-031): static-now / control-API-later

**Фаза 1 (M11.2):** Статический клиентский HTML-генератор конфигурации. Без бэкенда. Air-gapped. Тот же подход, что у трёх калькуляторов (docs/calculators/*.html).

**Фаза 2 (после M9.3):** Live-WebUI, backed by brain HTTP control-API (M9.3 — GAP-M9-03). До появления control-API фаза 2 не реализуется — live-WebUI без бэкенда означает запись секретов в localStorage (недопустимо).

### Что генерирует Phase-1 WebUI

Пользователь заполняет форму в браузере → WebUI генерирует:

1. **RunConfig YAML** (для `--run-config /config/run.yaml`):
   ```yaml
   mode: explore          # explore | replay | goal | describe
   target: https://...
   planner: heuristic     # heuristic | llm | goal
   goal: "Оформить заказ через корзину"
   auth:
     type: storageState
     path: /config/auth.json
   budgets:
     plan_tokens: 50000
     heal_tokens: 20000
   ```
2. **env-блок** для вставки в `docker-compose.yml` или передачи через `--env-file`:
   ```
   LLM_BACKEND=anthropic
   LLM_MODEL=claude-opus-4-8
   ANTHROPIC_API_KEY=<вставить>
   LLM_BACKEND_HEAL=openai
   LLM_BASE_URL_HEAL=http://ollama:11434/v1
   LLM_MODEL_HEAL=qwen2.5:7b
   ```

### Поля формы

| Поле | Тип | Значение по умолчанию |
|---|---|---|
| Target URL | text | — |
| Mode | select | explore |
| Planner | select | heuristic |
| Goal (если mode=goal/describe) | textarea | — |
| LLM backend (planner) | select | anthropic / openai-compat / none (offline) |
| Модель planner | text (с подсказками из LOCAL_MODELS каталога) | claude-opus-4-8 |
| LLM backend (heal) | select | same as planner |
| PLAN token budget | number | 50000 |
| HEAL token budget | number | 20000 |
| Auth type | select | none / storageState |

### Архитектурные ограничения WebUI (Phase 1)

- **Нет backend-вызовов.** Генерация происходит полностью в браузере (vanilla JS, zero deps).
- **Секреты не хранятся.** Поля API-ключей — placeholder с инструкцией «замените в env-файле».
- **Air-gapped.** Страница работает без подключения к сети (локальная копия из GitHub Pages).
- **Явная фазовая метка.** Функции Phase 2 (live run, hot-reload конфига) помечены баннером «Требует M9.3 control-API — не реализовано».

### Критерии приёмки M11.2

- [ ] Статическая страница `docs/setup.html` доступна на GitHub Pages
- [ ] Генерирует валидный RunConfig YAML (проходит `python -c "from brain.runconfig import load_run_config; ..."`)
- [ ] Генерирует корректный env-блок (все ключи из ADR-019 env-схемы)
- [ ] Нет внешних сетевых вызовов (проверяется DevTools → Network в offline-режиме)
- [ ] Phase-2 функции явно помечены (недоступны без M9.3)
- [ ] Ссылки на `docs/LOCAL_MODELS.md` и `docs/TESTING.md` присутствуют

---

## §5 M11.3 — Helm / Flux / Argo расширение (закрывает GAP-SEC-001)

**Статус:** не начат. Зависит от: M11.1 (release tag для chart appVersion). Helm chart (`deploy/sentinel/`) уже существует с M5.

### Проблема (GAP-SEC-001)

Текущий Helm chart инжектирует секреты как plaintext:

```yaml
# deploy/sentinel/templates/cronjob.yaml:34-46 — СЕЙЧАС (небезопасно)
env:
  - name: CHECKPOINT_DSN
    value: {{ .Values.checkpointDsn | quote }}          # plaintext DSN в CronJob spec
  {{- range $k, $v := .Values.extraEnv }}
  - name: {{ $k }}
    value: {{ $v | quote }}                              # plaintext API-ключи
  {{- end }}
```

Это означает: `kubectl describe cronjob sentinel` раскрывает API-ключи и DSN.

Дополнительно: `agentctl` передаёт `cmd.Env = append(os.Environ(), ...)` без allowlist — каждая переменная хоста (включая не связанные с Sentinel секреты) наследуется brain и его дочерними процессами.

### Что строит M11.3

**1. env-allowlist в agentctl** (`cmd/agentctl/main.go`)

```go
// Было: cmd.Env = append(os.Environ(), extraEnv...)
// Станет:
allowedPrefixes := []string{
    "LLM_", "ANTHROPIC_", "OPENAI_", "OTEL_",
    "CHECKPOINT_DSN", "STORAGE_STATE", "PW_", "MCP_",
    "ORCH_ADDR", "STORE_SOCKET", "ARTIFACT_DIR",
    "RUN_ID", "RUN_MODE", "TARGET_URL", "AUT_VERSION",
}
cmd.Env = filterEnv(os.Environ(), allowedPrefixes, extraEnv)
```

**2. Secret plumbing в Helm chart**

Новые значения в `values.yaml`:
```yaml
secrets:
  llmApiKey:
    secretName: sentinel-secrets
    key: llm-api-key
  checkpointDsn:
    secretName: sentinel-secrets
    key: checkpoint-dsn
  storageState:
    secretName: sentinel-secrets
    key: storage-state-path
```

В `cronjob.yaml` — `secretKeyRef` вместо plaintext:
```yaml
env:
  - name: ANTHROPIC_API_KEY
    valueFrom:
      secretKeyRef:
        name: {{ .Values.secrets.llmApiKey.secretName }}
        key: {{ .Values.secrets.llmApiKey.key }}
  - name: CHECKPOINT_DSN
    valueFrom:
      secretKeyRef:
        name: {{ .Values.secrets.checkpointDsn.secretName }}
        key: {{ .Values.secrets.checkpointDsn.key }}
```

Обратная совместимость: plaintext `value:` сохраняется как fallback (dev/offline режим через `secrets.enabled: false`).

**3. Flux HelmRelease / Kustomization**

Новый каталог `deploy/flux/`:
```
deploy/flux/
├── helmrelease.yaml          # HelmRelease referencing deploy/sentinel chart
├── kustomization.yaml        # Flux Kustomization
└── sentinel-secrets.yaml     # ExternalSecret / SealedSecret пример (шаблон)
```

`helmrelease.yaml` (пример):
```yaml
apiVersion: helm.toolkit.fluxcd.io/v2beta2
kind: HelmRelease
metadata:
  name: sentinel
  namespace: sentinel
spec:
  interval: 10m
  chart:
    spec:
      chart: ./deploy/sentinel
      sourceRef:
        kind: GitRepository
        name: sentinel
  values:
    target: "https://your-app.example.com"
    schedule: "0 2 * * *"
    secrets:
      enabled: true
      llmApiKey:
        secretName: sentinel-secrets
        key: llm-api-key
```

ArgoCD Application (уже существует с M5) — обновляется для поддержки нового `secrets` блока.

### Критерии приёмки M11.3

- [ ] `kubectl describe cronjob sentinel` не содержит API-ключей и DSN (они в Secret)
- [ ] env-allowlist в agentctl: unit-тест подтверждает, что неизвестные env-переменные не передаются brain
- [ ] `helm lint deploy/sentinel` проходит (с `secrets.enabled: true` и `secrets.enabled: false`)
- [ ] Flux HelmRelease reconciles green на тестовом кластере K3s
- [ ] `helm template deploy/sentinel -f deploy/sentinel/values-prod.yaml | grep "value:"` — ни одного секрета в plaintext
- [ ] Документация обновлена: `docs/DEVELOPMENT.md` описывает Secret plumbing

---

## §6 M11.4 — Air-gapped bundle

**Статус:** не начат. Зависит от: M11.1 (подписанный образ), M11.2 (WebUI статика).

### Цель

Полный пакет для установки Sentinel в сети без доступа к интернету:
- нет вызовов к Docker Hub, GHCR, npm registry, PyPI, GitHub
- включает все бинарники, образ, модель и документацию
- верифицируется offline после установки

### Состав bundle

| Компонент | Формат | Источник |
|---|---|---|
| Docker-образ | OCI tar (`docker save`) | `ghcr.io/alexgromer/sentinel:<tag>` (linux/amd64 + linux/arm64) |
| `agentctl` (нативный) | `.tar.gz` из M11.1 Release | GitHub Release |
| Ollama + выбранная модель | Ollama `ollama pull --model-dir` export | configurable из каталога LOCAL_MODELS §5c |
| Python wheels | pre-installed в образе (uv.lock) | нет PyPI в runtime |
| pw-executor dist | включён в образ (dist/ при build) | нет npm registry в runtime |
| `docker-compose.offline.yml` | отдельный файл | репозиторий |
| Документация (GitHub Pages) | static HTML из docs/ | HTML-копия (offline bundle) |
| Checksums + Cosign bundle | `.sha256` + `cosign.bundle` | M11.1 |

### `docker-compose.offline.yml`

```yaml
# Offline-вариант: все образы из локального архива, нет внешних pull
services:
  sentinel:
    image: sentinel:local          # загружен через docker load
    # ... (идентично docker-compose.yml)
  ollama:
    image: ollama:local-bundle     # загружен через docker load
    # нет pull policy: always
```

### Верификация offline

```bash
# Проверить checksum бинарников
sha256sum -c checksums.sha256

# Верифицировать подпись образа (Cosign offline через bundle)
cosign verify-blob --bundle=sentinel.bundle \
    --certificate-identity-regexp=".*" sentinel.tar.gz

# Запустить в изолированной сети
docker run --network none sentinel:local agentctl --help

# Проверить demo (heuristic, LLM-free) в offline
docker compose -f docker-compose.offline.yml --profile demo up
```

### Критерии приёмки M11.4

- [ ] `docker compose -f docker-compose.offline.yml up` не делает внешних DNS-запросов (проверяется tcpdump или network namespace isolation)
- [ ] `demo` profile завершает explore успешно в offline (heuristic planner, LLM-free)
- [ ] Ollama endpoint `http://ollama:11434/v1` отвечает на `/v1/models` без подключения к интернету
- [ ] Все checksums верифицируются offline (`sha256sum -c`)
- [ ] Cosign bundle верифицируется без обращения к Rekor (offline bundle mode)
- [ ] Документация (GitHub Pages static copy) доступна без сети

---

## §7 M11.5 — Zero-level onboarding

**Статус:** не начат. Зависит от: M11.1 + M11.2 + M11.4.

### Целевой пользователь

QA или devops-инженер, у которого есть Docker, но нет Go/Python/Node build toolchain. Цель: от нуля до первого успешного explore-прогона за ≤ 10 минут.

### Компоненты

**1. `install.sh` — single-command installer**

```bash
curl -fsSL https://raw.githubusercontent.com/alexgromer/sentinel/main/install.sh | sh
```

Что делает:
- определяет платформу (uname -m / os)
- скачивает нужный бинарник `agentctl` из последнего GitHub Release
- верифицирует checksum (`sha256sum -c`)
- верифицирует Cosign подпись (если cosign установлен; предупреждение если нет)
- кладёт бинарник в `~/.local/bin/agentctl` или `/usr/local/bin/agentctl`
- опционально скачивает `docker-compose.yml` в текущую директорию

**2. `docs/QUICKSTART.md` — step-by-step guide**

Структура (целевой объём: ≤ 2 страницы):
1. Предварительные требования (Docker ≥ 24)
2. Установка (`curl | sh`)
3. Генерация конфигурации (ссылка на setup-WebUI M11.2)
4. Первый запуск: `docker compose run --rm sentinel run --target <URL>`
5. Интерпретация результата: `runs/<id>/plan.json` + exit codes
6. Следующий шаг: `docs/TESTING.md` для полного руководства

**3. Интеграция с setup-WebUI (M11.2)**

QUICKSTART ссылается на setup-WebUI для генерации RunConfig YAML без ручного редактирования.

**4. Offline-путь (M11.4)**

QUICKSTART содержит раздел «Установка без доступа к интернету»: скачать bundle, `docker load`, `docker compose -f docker-compose.offline.yml`.

### Критерии приёмки M11.5

- [ ] Новый пользователь с Docker завершает первый explore за ≤ 10 минут, следуя `docs/QUICKSTART.md`
- [ ] `install.sh` верифицирует checksum перед установкой; завершается с ненулевым кодом при несовпадении
- [ ] Все шаги QUICKSTART.md воспроизводятся в чистом Docker окружении (проверяется в GitHub Actions)
- [ ] Offline-путь описан и верифицируется (зависит от M11.4)
- [ ] `install.sh` не требует root при установке в `~/.local/bin`

---

## §11 Модель интеграции

> **Этот раздел является нормативным.** Он определяет, что Sentinel делает и что он намеренно не делает при интеграции с инфраструктурой заказчика. Отклонение от этой модели требует нового ADR.

### Sentinel — black-box UI-тестер

Sentinel не имеет и не должен иметь прямого доступа к:
- базам данных (SQL, NoSQL, vector stores)
- очередям сообщений (Kafka, RabbitMQ, SQS)
- backend gRPC/REST API (кроме AUT через браузер)
- service mesh (Istio, Linkerd)
- логам и трассам других сервисов

**Это не ограничение, это гарантия.** Black-box контракт означает:
1. Sentinel тестирует то, что тестирует реальный пользователь — observable UI state в браузере.
2. Sentinel не требует backend credentials и не создаёт угрозу компрометации backend при утечке конфига.
3. Sentinel переносим между стеками — тестирует любое веб-приложение вне зависимости от backend-технологии.

### «Время ответа» в контексте Sentinel

Sentinel **уже измеряет** browser-side UI-action latency:

- Каждый Playwright-инструмент (`navigate`, `click`, `fill`, `expect`, ...) инструментирован OTel span'ом с точными временными метками (ADR-021/M8, `pw-executor/src/otel.ts`).
- Метрики экспортируются в Prometheus (Pushgateway или textfile collector).
- «Время ответа» = время от вызова инструмента до stable DOM / прохождения assert — то, что наблюдает реальный пользователь в браузере.

Это не «proxy-latency» и не «network RTT» — это сквозная user-observable latency UI-действия, включая frontend rendering, XHR, и DOM-мутации.

### Корреляция с backend: W3C traceparent (M9.5)

Для корреляции UI-теста с backend-трассами используется **инъекция W3C `traceparent` заголовка** во все HTTP-запросы браузера.

**Механизм:**

```
Sentinel OTel span (explore/replay step)
    │
    ├─ traceparent: 00-<trace-id>-<span-id>-01
    │
    └──► pw-executor устанавливает заголовок на browser context
              │
              ├─► AUT frontend (каждый XHR/fetch несёт traceparent)
              │        │
              │        └──► backend service (если OTel-инструментирован)
              │                  │
              │                  └──► Kafka / DB / downstream service
              │
              └──► Tempo / Jaeger / Zipkin заказчика:
                   единая трасса: UI-action → browser → service → Kafka → DB
```

**Требование к заказчику:** backend-сервисы должны быть OTel-инструментированы и пропагировать `traceparent` заголовок через свою инфраструктуру. Sentinel не добавляет инструментацию к чужому коду.

**Результат:** в Tempo/Jaeger заказчика появляется сквозная трасса, связывающая конкретный UI-шаг Sentinel с backend-обработкой. Это работает IFF заказчик уже использует OTel.

### Что НЕ будет построено (намеренно)

| Что | Почему нет |
|---|---|
| Прямой connector к DB / Kafka / gRPC backend | Нарушает black-box контракт; требует backend credentials; привязывает к конкретному стеку |
| «Response time» через backend polling | Уже решено через browser-side OTel spans — добавление backend-polling дублирует измерение и вводит coupling |
| Service mesh интеграция (Istio mTLS) | Out of scope; инфраструктурный домен; не связано с UI-тестированием |
| Log aggregation connector (Loki, ELK) | Sentinel не агрегирует логи; трейсинг через traceparent покрывает use case |
| Backend-специфическая instrumentation | Заказчик делает это сам; Sentinel — passive header propagator |

### Конфигурируемые точки интеграции

Единственные «швы» Sentinel для интеграции с инфраструктурой заказчика:

| Параметр | Env-переменная | Назначение |
|---|---|---|
| OTLP endpoint | `OTEL_EXPORTER_OTLP_ENDPOINT` | Куда Sentinel отправляет свои span'ы (Tempo/Jaeger заказчика) |
| Prometheus | `PROMETHEUS_PUSHGATEWAY_URL` / textfile | Метрики Sentinel (latency, heal-rate, token cost) |
| W3C traceparent injection | M9.5 (GAP-M9-06) | Инъекция span context в browser requests |

### Реафирмация scope M9.5

**M9.5 = traceparent injection в браузерные запросы. Только это.**

M9.5 **не расширяется** на:
- прямой опрос backend-сервисов
- парсинг backend-ответов
- активное взаимодействие с Kafka / DB
- агрегацию логов
- интеграцию с service mesh

Любой запрос на расширение M9.5 за пределы traceparent injection = новый GAP entry + новый ADR + отдельный milestone.

### Наглядная граница

```
┌─────────────────────────────────────────────────────────────┐
│                    Зона ответственности Sentinel              │
│                                                               │
│   agentctl → orchestrator → brain → pw-executor → Chromium   │
│                                          │                    │
│                              browser HTTP requests            │
│                              (с traceparent, M9.5)            │
│                                          │                    │
└──────────────────────────────────────────┼────────────────────┘
                                           │
                   ────────────────────────┼──────────────
                   Зона ответственности заказчика         │
                                           ▼
                              AUT frontend → backend service
                              → Kafka → DB → downstream...
                                           │
                                           ▼
                              Tempo/Jaeger/Zipkin заказчика
                              (сквозная трасса — если OTel-инструментирован)
```

Всё, что ниже пунктирной линии — инфраструктура заказчика. Sentinel пассивно пропагирует trace context через W3C заголовок; он не читает, не пишет и не опрашивает ничего за этой чертой.
