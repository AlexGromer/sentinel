# Дистрибуция и онбординг — EPIC Contract (ADR-030 / ADR-031)

🌐 [English version](DISTRIBUTION.en.md)

> **Статус**: контракт заморожен | **Дата**: 2026-06-27
> **ADR**: ADR-030 (стратегия дистрибуции) · ADR-031 (setup-WebUI)
> **Эпик**: M11.1–M11.6 — секвенированный; большинство не строится в этом цикле (M11.6 доставлен — issue #12)
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

### Режимы запуска UI и токен доступа (ADR-064)

Единственное, чем различаются три режима — кто отдаёт браузеру статику UI:

| Режим | Как запустить | Порты | CORS | Токен в UI |
|---|---|---|---|---|
| 1 — headless | `docker compose --profile control-api up control-api` | 8090 | не нужен | UI не запускается; клиент сам шлёт `Bearer` |
| 2 — split (прежний дефолт) | `docker compose --profile control-api --profile webui up` | 8088 + 8090 | нужен allowlist (`CONTROL_API_CORS_ORIGINS`) | оператор копирует токен вручную в Settings |
| 3 — single-service (рекомендуется) | `CONTROL_API_SERVE_UI=1 CONTROL_API_CORS_ORIGINS= docker compose --profile control-api up control-api` → открыть `http://localhost:8090/` | 8090 | не нужен — same-origin запросы не являются CORS-запросами, allowlist можно оставить пустым | одноразовая ссылка `?bootstrap=<nonce>`, печатается при старте |

**Жизненный цикл токена (все режимы).** Больше не нужно придумывать `CONTROL_API_TOKEN` перед первым запуском:
если переменная не задана, control-api сам генерирует 32 случайных байта (hex) и атомарно сохраняет их в
`state/control-api.token` (права 0600); при следующем запуске файл переиспользуется, поэтому токен, уже
вставленный в UI, переживает перезапуск.

Приоритет источника токена: `CONTROL_API_TOKEN` (env) → иначе `CONTROL_API_AUTOTOKEN=0` даёт осознанно
безтокенный read-only инстанс (любая мутация — 403, поведение до ADR-064) → иначе сохранённый файл → иначе
свежесгенерированный токен. Если файл существует, но недоступен для чтения или содержит непригодное значение,
он НИКОГДА не перезаписывается: процесс предупреждает и работает с одноразовым токеном в памяти.

`CONTROL_API_TOKEN_FILE` переопределяет расположение файла. `CONTROL_API_PRINT_TOKEN=0` отключает печать
значения при старте (в режимах 1-2 печать включена по умолчанию — терминал единственный канал у оператора).
Файл токена лежит в `state/` (в `.gitignore`); на Windows права 0600 транслируются в ACL-семантику, а не
POSIX-биты — считай файл user-scoped и не полагайся на биты доступа.

**Особенности режима 3.** `CONTROL_API_SERVE_UI=1` отдаёт UI из ассетов, встроенных в бинарь — чекаут не нужен,
достаточно релизного бинаря. `CONTROL_API_UI_DIR=<путь>` вместо этого отдаёт страницы с диска (для live-правки
страниц при разработке). При старте control-api печатает в stderr:

```
control-api: serving the UI (embedded) at http://127.0.0.1:8090/
control-api: open http://127.0.0.1:8090/?bootstrap=<nonce>  (one-time, valid 5m0s)
```

Переход по этой ссылке сам заполняет поля адреса control-API и bearer-токена на странице и убирает nonce из
URL. Токен остаётся в памяти вкладки — никогда не в `localStorage`.

Nonce одноразовый и истекает (по умолчанию 5 минут, `CONTROL_API_UI_BOOTSTRAP_TTL`, например `90s`;
неположительное значение отключает bootstrap целиком). Повторное использование, использование после истечения,
пять неверных попыток или обращение с чужого origin — всё это возвращает 403. Если ссылку упустил: прочитай
токен из `state/control-api.token` и вставь его в поле Settings на странице, либо перезапусти control-api ради
свежего nonce. Обращение к порту после старта само по себе токен не даёт — это осознанное решение, сохраняющее
защитный инвариант ADR-032.

Отдаваемые страницы: `/` (хаб), `/setup/` (мастер), `/chat/` (чат-консоль), `/calculators/*.html`, плюс
`prices.json` и `backend-presets.json`. Прозаические `.md`-документы не отдаются (они линкуются на GitHub). Режимы 1 и
2 побайтово не изменились, если `CONTROL_API_SERVE_UI`/`CONTROL_API_UI_DIR` не заданы.

### Переменные окружения

Env-блок задаётся в `docker-compose.yml` или передаётся через `.env` файл:

```yaml
# Cloud (Anthropic) — без ключа → offline heuristic + L1–L6 heal
ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}

# Локальная модель (активировать, убрав комментарии):
# LLM_BACKEND: openai
# LLM_BASE_URL: http://ollama:11434/v1
# LLM_MODEL: qwen2.5:7b           # из каталога docs/LOCAL_MODELS.md §3
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

**Статус:** реализовано — `.github/workflows/release.yml` в репо (M11.1); E2E-релиз (публикация/подпись) — на первом `v*`-теге мейнтейнера, `workflow_dispatch` = build/SBOM dry-run. Предпосылки: Foundation CI-гейты (DONE).

### Что поставляется

Пять Go-бинарников (`agentctl`, `control-api`, `store-gateway`, `orchestrator`, `report-service`) для шести платформ:

| Платформа | GOOS | GOARCH |
|---|---|---|
| Linux x86-64 | linux | amd64 |
| Linux ARM64 | linux | arm64 |
| macOS Apple Silicon | darwin | arm64 |
| macOS Intel | darwin | amd64 |
| Windows x86-64 | windows | amd64 |
| Windows ARM64 | windows | arm64 |

Итого: 30 бинарников (6 платформ × 5 бинарников) + Docker-образ (multi-arch: linux/amd64 + linux/arm64).

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

- [ ] GitHub Release содержит 30 бинарников (6 платформ × 5 бинарников) в `.tar.gz`
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

**Статус:** **DELIVERED** (M11.3, ADR-035 — закрывает Helm-половину GAP-SEC-001). Helm chart (`deploy/sentinel/`) существует с M5; реализация ниже отражает фактический код (она богаче исходных набросков этого §5 и заменяет их).

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

**1. env-allowlist в agentctl — теперь default-on** (`cmd/agentctl/main.go`, `filteredEnv()`)

`filteredEnv()` ведёт **exact-map** (PATH/HOME/… + ANTHROPIC_API_KEY/OPENAI_API_KEY/CHECKPOINT_DSN/STORAGE_STATE/ORCH_ADDR/… **+ M11.3-добавки** PROM_PUSHGATEWAY/HEAL_VISUAL/SSL_CERT_FILE/SSL_CERT_DIR/HTTP(S)\_PROXY/NO_PROXY/`NODE_OPTIONS`/`NODE_EXTRA_CA_CERTS`/`GIT_SSL_CAINFO`/`GIT_SSL_CAPATH`) **+ prefix-list** (`LLM_`/`OTEL_`/`PW_`/`PLAYWRIGHT_`/`SENTINEL_`) — `NODE_`/`GIT_` умышленно НЕ входят в prefix-list (широкий префикс раньше протекал `NODE_AUTH_TOKEN`/`GIT_ASKPASS`; конкретные легитимные имена — exact-allowed выше) **+** имена из `SENTINEL_ENV_ALLOW` (comma-sep — для secretKeyRef-переменных вроде `AUT_PASSWORD`).

M11.3 переворачивает флаг в **default-on**: фильтр активен всегда, кроме явного **opt-out** `SENTINEL_ENV_ALLOWLIST=0` (escape hatch для отладки/нестандартных локальных setup'ов). Функциональные run-переменные (RUN_ID/TARGET_URL/RUN_MODE/PLANNER/…) фильтр **не трогает** — они добавляются после `filteredEnv()` в `spawnBrain`, не наследуются из хоста. Unit-тест: `cmd/agentctl/main_test.go` (default-on исключает `AWS_SECRET_ACCESS_KEY`, пропускает curated + `SENTINEL_ENV_ALLOW`-extras; `=0` → полный passthrough).

**2. Secret plumbing в Helm chart**

Новый блок в `values.yaml` (default `enabled: false` — dev/offline-friendly):
```yaml
secrets:
  enabled: false
  llmApiKey:
    secretName: sentinel-secrets
    key: llm-api-key
    envName: ANTHROPIC_API_KEY      # переименовать под backend (OPENAI_API_KEY / LLM_API_KEY)
  checkpointDsn:
    enabled: false                  # true только при Postgres-checkpoint-store (M5-3)
    secretName: sentinel-secrets
    key: checkpoint-dsn
  extraSecretEnv: []                # доп. secretKeyRef-переменные (напр. AUT_PASSWORD)
```

**Связка с env-allowlist (критично):** т.к. фильтр теперь default-on, любое chart-имя вне curated-списка иначе бы **отрезалось**. Поэтому `cronjob.yaml` авто-эмитит `SENTINEL_ENV_ALLOW` (helper `sentinel.envAllow`) из ключей `extraEnv` + имён `extraSecretEnv` + кастомного `llmApiKey.envName`, и ставит `SENTINEL_ENV_ALLOWLIST=1`.

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
├── sync.yaml                 # Namespace + GitRepository + Flux Kustomization (bootstrap-вход)
├── helmrelease.yaml          # HelmRelease → chart deploy/sentinel
└── sentinel-secrets.yaml     # ExternalSecret / SealedSecret пример (шаблон, без секретов)
```

**apiVersions = Flux v2 GA** (не `v2beta2` из ранних набросков; verify кластер ≥ Flux 2.3): HelmRelease `helm.toolkit.fluxcd.io/v2`, GitRepository `source.toolkit.fluxcd.io/v1`, Flux Kustomization `kustomize.toolkit.fluxcd.io/v1`. Файл Flux-Kustomization назван **`sync.yaml`**, НЕ `kustomization.yaml` (иначе kustomize принял бы каталог за overlay).

**Порядок Secret:** Flux `HelmRelease.spec.dependsOn` ссылается только на другие HelmRelease/Kustomization (не на raw Secret), поэтому буквального «dependsOn Secret» в Flux нет. CronJob запускается по расписанию (не при install) → терпит позднее появление Secret; `sync.yaml` (`wait: true`) применяет Secret-источник вместе с релизом. Для строгого порядка — разнести на две Flux-Kustomization (secrets → app c `dependsOn`). ArgoCD ↔ Flux **взаимоисключающи**.

`helmrelease.yaml` (пример):
```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
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

ArgoCD Application (`deploy/argocd/sentinel-app.yaml`, существует с M5) — комментарий дополнен: `secrets.enabled` (через `values-prod.yaml`) включает secretKeyRef; сам `sentinel-secrets` Secret подаётся out-of-band (SealedSecret/ExternalSecret/sops, ArgoCD его контент не хранит); ArgoCD ↔ Flux взаимоисключающи.

### Критерии приёмки M11.3

- [x] env-allowlist **default-on**: unit-тест (`cmd/agentctl/main_test.go`) подтверждает, что неизвестные env-переменные (`AWS_SECRET_ACCESS_KEY`) не передаются brain; `SENTINEL_ENV_ALLOWLIST=0` → полный passthrough
- [x] `helm lint deploy/sentinel` проходит при `secrets.enabled: true` **и** `false`
- [x] `helm template … -f values-prod.yaml` — `ANTHROPIC_API_KEY` и `CHECKPOINT_DSN` только через `secretKeyRef`, ни одного секрета в plaintext `value:`; в dev — plaintext-fallback без `secretKeyRef`
- [x] `deploy/flux/*.yaml` parse-clean, apiVersions = Flux v2 GA, нет файла `kustomization.yaml`
- [x] Документация: `docs/DEVELOPMENT.md` (+en) описывает Secret plumbing; GAP-SEC-001 Helm-половина закрыта; ADR-035
- [ ] **live-verify (нет кластера/flux CLI):** `kubectl describe cronjob sentinel` не содержит ключей/DSN
- [ ] **live-verify:** Flux HelmRelease reconciles green на K3s

---

## §6 M11.4 — Air-gapped bundle

**Статус:** реализован — offline compose + verify/bundle-скрипты + CI `airgap`-job (ядро проверяется на каждом push/PR). Полный bundle E2E (реальный GHCR-образ + модель + подписи) собирается мейнтейнером на первом `v*`-теге, как M11.1. Зависит от: M11.1 (подписанный образ), M11.2 (WebUI статика).

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
| Ollama + выбранная модель | pull на связанной машине → tar тома `OLLAMA_MODELS` (либо `ollama create` из GGUF+Modelfile) | configurable из каталога LOCAL_MODELS §3 |
| Python wheels | pre-installed в образе (uv.lock) | нет PyPI в runtime |
| pw-executor dist | включён в образ (dist/ при build) | нет npm registry в runtime |
| `docker-compose.offline.yml` | отдельный файл | репозиторий |
| Документация (GitHub Pages) | static HTML из docs/ | HTML-копия (offline bundle) |
| Checksums + Cosign bundle | `.sha256` + `cosign.bundle` | M11.1 |

### Что реализовано (M11.4)

- `docker-compose.offline.yml` — сеть `internal: true` (ноль egress), `pull_policy: never`, offline-anchor без `build:`, `demo`=`network_mode: none`, том `ollama-models` с pinned `name:`; профиль `ollama` (docs браузятся отдельным `http.server`-контейнером — internal-сеть не публикует порты).
- `scripts/offline-verify.sh` — единый верификатор: `--local` (build→save/load→`--network none` demo+docs+negative-DNS — гейт CI) и `--bundle <dir>` (checksums + `cosign verify-blob --bundle` offline + подъём стека + `/v1/models`).
- `scripts/build-airgap-bundle.sh` — maintainer-ассемблер (на связанной машине): `gh release download`, **verify образа GHCR до `docker save`**, экспорт ollama-модели, самоподписанный `MANIFEST.sha256`.
- CI `airgap`-job + `tests/test_m11_4_offline.py`; фикс `.dockerignore` (`!docs/index.html`).

**Важно:** «ноль внешних вызовов» относится к ПОТРЕБЛЕНИЮ bundle. Сборка (`build-airgap-bundle.sh`) выполняется на связанной машине и тянет образы/модель/релиз — это нормально, как и то, что сам CI/релиз-пайплайн не air-gapped. `docker save`/`load` НЕ переносит cosign-подпись образа, поэтому образ верифицируется на связанной машине ДО сохранения, а целостность bundle держится на cosign-подписанном `MANIFEST.sha256`.

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

| # | Критерий | Статус |
|---|---|---|
| 2 | demo завершает explore offline (heuristic, LLM-free) | ✅ **проверено в CI** (`airgap`-job) |
| 6 | статическая копия docs доступна offline | ✅ **проверено в CI** (после фикса `.dockerignore`) |
| 1 | `compose up` без внешнего DNS | ◐ **механизм** — `internal:true` + negative-DNS-probe для sentinel/demo; полный стек — на теге |
| 5 | cosign bundle верифицируется без Rekor | ◐ **механизм** — live self-signed `--bundle` round-trip в CI; реальная release-identity — на теге |
| 3 | Ollama `/v1/models` отвечает offline | ☐ **открыто** — на теге (нужен реальный model-bundle) |
| 4 | checksums верифицируются offline (`sha256sum -c`) | ☐ **открыто** — логика само-тестируется; реальные checksums — на теге |

---

## §7 M11.5 — Zero-level onboarding

**Статус:** docs-first freeze (ADR-059). Расширен сверх исходного тонкого спека до **guided onboarding** —
управляемой state-machine. Реализуется 5 последовательными PR: docs (этот freeze) → installer → config-schema+пресеты
→ wizard → config-домен+`/readyz`. Зависит от: M11.1 (Release-ассеты) + M11.2 (setup-WebUI) + M11.4 (offline-путь).

### Целевой пользователь

QA или devops-инженер, у которого есть Docker, но нет Go/Python/Node build toolchain. Цель: от нуля до первого
успешного explore-прогона за ≤ 10 минут, **без ручного редактирования YAML** и без чтения полной документации.

### Видение: онбординг = управляемая state-machine (ADR-059)

Не плоская форма + «подложи YAML руками», а пошаговый мастер, который знает про режимы работы и сам собирает
корректную конфигурацию, персистит её и переиспользует при повторном запуске.

**1. `install.sh` / `install.ps1` — single-command installers** (POSIX `sh` для Linux/macOS; PowerShell-пир
для Windows)
```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/AlexGromer/sentinel/main/install.sh | sh
```
```powershell
# Windows (без admin) — ставит КЛИЕНТ, см. оговорку ниже
iwr -useb https://raw.githubusercontent.com/AlexGromer/sentinel/main/install.ps1 | iex
```

> ⚠ **Windows — клиентская платформа (решение 2026-08-02, ADR-110).** Прежняя формулировка «нативный
> Windows, без Docker/WSL» была неверна и обещала больше, чем есть. `install.ps1` ставит **только
> `agentctl.exe`**, и это работает нативно. Но прогон — это ещё Python 3.11+/uv (планировщик и починка),
> Node 24+ (исполнитель Playwright) и сами браузеры; установщик их не приносит и не собирается.
> Поддерживаемый путь на Windows: `agentctl` как клиент к control-API, поднятому в контейнере или на
> другой машине. Полный стек на самом Windows-хосте — Docker Desktop или WSL. Сам `install.ps1` это
> говорил всегда (его `.DESCRIPTION`); расходились с ним документы.

- `install.sh`: `uname -s`/`-m` → `{linux,darwin}`×`{amd64,arm64}`; `install.ps1`: нативный Windows,
  `{amd64,arm64}` (`$env:PROCESSOR_ARCHITECTURE`);
- резолвит последний GitHub Release, качает `sentinel-<tag>-<os>-<arch>.tar.gz` + `checksums.sha256` + `*.cosign.bundle`;
- **`sha256sum -c`** (ненулевой код при несовпадении) → **`cosign verify-blob`** с **pinned identity** (тот же
  regex/issuer, что `scripts/offline-verify.sh`; если `cosign` нет — громкое предупреждение, не жёсткий фейл);
- `install.sh` кладёт `agentctl` в `~/.local/bin` (default, **без root**) или `/usr/local/bin` (opt-in);
  `install.ps1` — в `%LOCALAPPDATA%\Programs\sentinel` (**без admin**); оба проверяют `$PATH`;
- post-install `agentctl --version` (sanity) + указатель на setup-WebUI и `docs/QUICKSTART.md`; опц. качает `docker-compose.yml`.

**Homebrew (macOS):** репозиторий — собственный tap; `Formula/sentinel.rb` генерируется на каждый `v*`-тег
(`scripts/gen-brew-formula.sh` в `release.yml`):
```bash
brew tap AlexGromer/sentinel https://github.com/AlexGromer/sentinel
brew install sentinel
```

**2. setup-WebUI → пошаговый wizard** (переписывает `docs/setup/index.html`, ADR-031→ADR-059)
- Шаги: **Runtime → Model&Auth → Run-params → Review** (переиспользует `.tabbar`/`.subtabbar`-паттерн `docs/index.html`).
- **Runtime-дропдаун пресетов** (см. таблицу ниже) → условные поля per-backend (base_url/model/api_key). NB: выбор рантайма ≠ RunConfig-`mode` (explore/goal/describe), который живёт в шаге Run-params.
- **Schema-driven**: форма рендерится из `GET /v1/config-schema` (расширенного LLM-backend-полями) — единый источник
  правды: `brain/runconfig.py` для RunConfig-полей, `brain/llm.py` `make_backend` для LLM-backend-полей (ADR-060), без хардкод-дрейфа.
- **Валидация**: обязательные поля (target), диапазоны бюджетов, подсветка ошибок, re-ask при проблеме.
- **Draft-persist**: черновик конфигурации + control-API URL в `localStorage` (токен — НИКОГДА не хранится); на
  relaunch — предзаполнение и ре-валидация (re-run state-machine). Двуязычие (`data-lang`), air-gapped (no-CDN).

**3. Пресеты рантаймов (open-core, config-only seam ADR-019).** Все — `LLM_BACKEND=openai` + разный `LLM_BASE_URL`/`LLM_MODEL`
(машиночитаемо → `docs/backend-presets.json`; источник — `docs/LOCAL_MODELS.md`):

| Пресет | `LLM_BACKEND` | `LLM_BASE_URL` (default) | Заметка |
|---|---|---|---|
| Cloud — Anthropic | `anthropic` | — (native) | `ANTHROPIC_API_KEY` |
| Cloud — OpenAI-совместимый (OpenAI/DeepSeek/OpenRouter) | `openai` | провайдерский `/v1` | реальный API-ключ |
| Ollama | `openai` | `http://ollama:11434/v1` | ключ игнорируется, SDK требует непустой (`noauth`) |
| vLLM | `openai` | `http://vllm:8000/v1` | GPU/throughput |
| llama.cpp / llamafile | `openai` | `http://host:8080/v1` | edge/минимум зависимостей |
| LM Studio | `openai` | `http://host:1234/v1` | dev-воркстейшн |
| LocalAI | `openai` | `http://localai:8080/v1` | мульти-бэкенд |
| LiteLLM (роутер) | `openai` | `http://litellm:4000/v1` | роутер многих провайдеров (ADR-045; образ в `deploy/litellm`) |
| HuggingFace TGI | `openai` | `http://host:<PORT>/v1` | порт задаёт оператор (нет дефолта) |

**4. Tiered config-персистенция** (профиль = топология, ADR-049):
- **standalone**: конфиг = файл (RunConfig YAML / `.env`), идемпотентно перечитывается (`brain/runconfig.py` — уже готов, не менять);
- **service**: новый `config`-домен store-gateway (по паттерну ADR-050) — control-API читает конфиг на старте / пишет из wizard.

**5. `/readyz`** (поверх существующего `/healthz`-liveness): проверяет реальные зависимости — доступность
store-gateway-сокета · LLM-эндпоинт (`/v1/models`) · наличие конфига → `503` пока не готов, `200` когда готов (k8s-shaped).

**6. `docs/QUICKSTART.md`** (≤ 2 страницы): prereqs (Docker ≥ 24) → install (`curl|sh`) → конфигурация (setup-WebUI) →
первый запуск → интерпретация `runs/<id>/plan.json` + exit-codes → offline-путь (M11.4) → полное руководство `docs/TESTING.md`.

### Граница open-core / enterprise (ADR-056)

Wizard + **все** пресеты рантаймов + file/DB-config + health-пробы = **open-core** (open-core обязан быть полезным,
не crippleware). Enterprise = managed/EMS-provisioning · license-issuing · multi-tenancy · SSO/RBAC/Vault · advanced-BI.

### Критерии приёмки M11.5 (honest, по PR)

- [x] **PR-1 (этот freeze):** ADR-059 + переписанный §7 + bilingual-parity. *(docs, проверяемо сейчас)*
- [x] **PR-2 (этот PR):** `install.sh` верифицирует checksum+cosign (ненулевой код при несовпадении), ставит без root в `~/.local/bin`, `agentctl --version` печатает версию; CI install-smoke в чистом контейнере (fake-release + tamper-negative). *(полный E2E = maintainer `v*`-tag, как M11.1)*
- [x] **PR-3 (этот PR):** `/v1/config-schema` покрывает LLM-backend-поверхность (`backends`/`roles`/`llm`-дескрипторы из `brain/llm.py`; `api_key`=secret-без-значения); `backend-presets.json` (9 пресетов) парсится и каждый `backend` ⊆ enum схемы (гейт `TestBackendPresetsParseAndMatchSchema`). *(env-истина — `brain/llm.py` `make_backend`; `runconfig.py` = RunConfig-ядро, LLM_* там нет)*
- [x] **PR-4 (этот PR):** wizard пошаговый (Runtime→Model&Auth→Run-params→Review), schema-driven (рендер из `/v1/config-schema` + встроенный снимок для offline и live-override, ADR-061), валидирует ввод (target/бюджеты/openai-правила `make_backend`), персистит черновик (секреты — никогда), двуязычный (`data-lang`), air-gapped (`node --check` в CI теперь на всех `docs/*.html`; `file://` — снимок вместо `fetch`). *(DOM-прогон автоматизирован — `scripts/wizard-dom-check.mjs`, 12 проверок в headless-Chromium в CI; синтаксис-гейт — на всех 6 страницах `docs/`; + 2 anti-drift-гейта)*
- [x] **PR-5 (этот PR):** `config`-домен в store-gateway (6-й домен `StoreService`, ADR-062); секреты **отвергаются** (`internal/configguard`, одно правило на гейтвей и control-API — 14 попыток обхода в тестах); control-API читает конфиг на старте и пишет из мастера (`PUT /v1/config`, token-gated); `/readyz` → `503` до готовности зависимостей, `200` когда готов (несконфигурированная зависимость = `skipped`, поэтому standalone остаётся ready). *(сквозной DOM-гейт: браузер → control-API → gRPC → SQLite → `/readyz`)*
- [x] Новый пользователь с Docker завершает первый explore за ≤ 10 минут по `docs/QUICKSTART.md`. **Измерено (2026-07-11):** `docker compose build` **208 с** + `docker compose run … --target https://example.com --planner heuristic` **21 с** = **~3 мин 49 с** end-to-end, exit 0, `plan.json`+`trace.zip` произведены. *(Оговорка: base-образ `playwright` был закэширован; полностью холодный пул добавляет ~2.44 ГБ загрузки → ~7 мин на канале ~100 Мбит/с — тоже под бюджетом, но зависит от сети.)*

---

## §7b Podman — измеренная совместимость (ADR-110)

Проверено 2026-08-02 на podman 5.8.3, штатным `docker compose` **против сокета podman**:

```bash
systemctl --user start podman.socket
export DOCKER_HOST="unix:///run/user/$(id -u)/podman/podman.sock"
SENTINEL_VERSION=v0.1.0-rc1 docker compose -f docker-compose.ghcr.yml --profile demo run --rm demo
```

| проверка | docker 28.5.2 | podman 5.8.3 |
|---|---|---|
| `config -q` на обоих стеках | ✔ | ✔ |
| анонимный pull образа из GHCR | ✔ | ✔ |
| `--profile demo` (explore до плана) | ✔ 8 шагов | ✔ 8 шагов |
| **`--profile browser` ЧЕРЕЗ compose**, сервис к сервису по имени `http://browser:9223` | ✔ (`172.22.0.2:9223`) | ✔ (`10.89.1.2:9223`) |

**`plan_hash` совпал побайтово во всех четырёх прогонах: `edc74498ac7c5db0`.** Расхождений на
проверенных путях **нет** — включая CDP-релей, где rootless-сеть podman отличается только диапазоном
адресов: подстановка резолвленного адреса в исполнителе делает разницу невидимой по построению.

**Сколько на самом деле реализаций Compose участвовало — одна.** Это стоит знать, прежде чем считать
эту таблицу более широкой, чем она есть: `docker compose`, `/usr/bin/docker-compose` (симлинк на тот
же плагин — легаси-v1 на Python здесь нет вовсе) и `podman compose` (печатает
`Executing external compose provider …/docker-compose`) — **это один и тот же бинарь Compose
v2.40.3**, отличается только движок под ним. Все три имени прогнаны, и это проверка движка, а не
трёх разных Compose.

⚠ Что **не** проверено и потому не обещается: отдельный проект `podman-compose` (питоновский, к
`podman compose` отношения не имеет и в системе отсутствовал), rootful-podman, podman на macOS через
машину, Compose v1. Профили `webui`/`control-api` публикуют порты на хост, и в rootless-режиме порты
ниже 1024 недоступны — наши (8088/8090) выше, поэтому это не мешает, но при переносе на 80/443
помешает.

---

## §8 M11.6 — Pages-хаб одной страницей (dark-neon, двуязычный, recommendation)

**Статус:** доставлен (issue #12, расширенный scope). Зависит от: LOCAL_MODELS §3/§5/§6 (источник формул).

Лендинг Pages переделан в **одну самодостаточную страницу `docs/index.html`** (полный HTML, весь CSS/JS
инлайн, без сети/CDN/шрифтов/сборки). Все интерактивы — **разделами на одной странице** (без переходов):
рекомендация · стоимость (§6) · VRAM (§5) · подбор модели (§3.3) · легенда · документация.

- **Тёмная неоновая тема** (красный акцент `#ff2d55` на `#0b0b10`, высокий контраст, без нагромождений).
- **Переключатель языка RU/EN** на всю страницу (по умолчанию RU, запоминается в `localStorage`; видимость
  через `data-lang` + CSS; JS-вывод отдаёт обе локали — тумблер мгновенно рефлоит без пере-рендера).
- **Recommendation-движок**: задача + железо + бюджет → внятный ответ (какая модель, режим/глубина
  explore/goal/replay, сколько прогонов влезает в бюджет, токены, время, стоимость) — по модели/железу/задаче.
- **Легенда + пояснения к каждому полю**; продвинутые входы спрятаны в `<details>` (чисто, без нагромождений).
- **Анти-галлюцинации**: цены и tok/s — редактируемые ориентиры с пометкой «verify, cutoff Jan-2026» и
  ссылками на провайдеров; имена/размеры моделей — из §3 с флагами ✅/⚠ (ничего не выдумано).
- **Air-gapped паритет**: одинаково на Pages, `file://` и в Docker-бандле `webui` (раньше `index.md` рендерился
  только через Jekyll). Старые `docs/calculators/*.html` остаются на диске как «продвинутые».
- Замена `docs/index.md` (Jekyll cayman) → статический `index.html` (**ADR-033**). Формулы §5/§6 — дословно;
  встроенные self-test'ы воспроизводят worked-examples (cost A–E; VRAM); `node --check` чистый.
- **M11.6b (доработка cost-explorer, ADR-034):** каталог популярных моделей (Claude/GPT/Grok/GLM/DeepSeek/Qwen
  + локальные) + **среднее $/1M** по умолчанию (in/out — в advanced) + **per-model токен-множитель** (reasoning
  think-токены) + fit/reasoning/vision-бэйджи; **air-gapped live-pricing**: встроенные сиды → `prices.json`
  (CI `prices-refresh.yml` через OpenRouter → PR) → кнопка «Обновить из OpenRouter» (сеть только по клику).
  Источник цен — LOCAL_MODELS §3.4.

### Критерии приёмки M11.6

- [ ] С лендинга новичок вводит размер сайта + бюджет и сразу видит сравнение моделей и рекомендацию — без переходов на другие страницы
- [ ] Математика зеркалит LOCAL_MODELS §5/§6 (cost-векторы A–E + VRAM-примеры воспроизводятся)
- [ ] Полностраничный тумблер RU/EN (по умолчанию RU, localStorage) переключает весь текст, включая JS-вывод
- [ ] Тёмная неоновая тема, легенда и пояснения к полям присутствуют
- [ ] Air-gapped: без сети/CDN; открывается на Pages, `file://` и в бандле `webui`
- [ ] Цены/tok/s редактируемы и помечены «verify (cutoff Jan-2026)»; имена моделей несут флаги §3
- [ ] `node --check` по извлечённому `<script>` чистый; gitleaks чистый

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
