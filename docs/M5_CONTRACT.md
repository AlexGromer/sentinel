# M5 Contract — "Deploy + Visual Heal" (frozen 2026-06-24)

> 🌐 **Русский** (основная версия) · [English](M5_CONTRACT.en.md)

Цель: развернуть Sentinel в домашнюю K3s/ArgoCD как GitOps и создать заготовку **set-of-marks visual
heal** (Tier-7) за измеряемым PoC-гейтом. Разбит на три части; M5-1 — часть, ориентированная на ценность и authorable offline.

## Разбивка scope
- **M5-1 — Развёртывание (GitOps).** Многостадийный Dockerfile (Go + Python + Node + Playwright browser);
  Helm chart, упаковывающий агента как **CronJob/Job** (запланированный replay) + значения для каждого namespace
  (dev/staging/prod); манифест **ArgoCD Application**. Authorable как YAML/Dockerfile сейчас; пользователь
  деплоит в свой K3s (не тестируется здесь — нет кластера).
- **M5-2 — Visual heal scaffold (Tier-7).** `browser.setOfMarks` pw-executor (пронумерованный оверлей над
  интерактивными элементами → marks[]); HealingEngine Tier-7 (vision: Sonnet выбирает метку → реальный локатор),
  **заблокирован** за `--heal-visual` + `--heal-llm` И PoC-гейтом точности (**поставлять только если ≥70%** на 20
  реальных сценариях со сломанными селекторами — ADR-005). Требует vision LLM (ключ/сеть) → PoC запускается пользователем.
- **M5-3 — Опция Postgres checkpointer.** Заменить LangGraph `SqliteSaver` → `AsyncPostgresSaver` при
  установленном `CHECKPOINT_DSN` (для K3s multi-runner). Только конфигурация.

## Развёртывание M5-1 (authorable offline)
- `Dockerfile`: stage1 `golang:1.26` собирает `agentctl` + `store-gateway`; stage2 `node:24` собирает
  `pw-executor` (`npm ci && npm run build`); stage3 runtime `mcr.microsoft.com/playwright` (или python
  base + `playwright install`) с venv (`uv pip install …`), копируя Go-бинари + dist +
  brain. Entrypoint `agentctl`.
- Helm chart `deploy/sentinel/`: `Chart.yaml`, `values.yaml` (image, schedule, target URL, источник плана,
  `--ci`, `--aut-version`, resources), шаблоны: `cronjob.yaml` (запланированный `agentctl run
  --replay --ci`), `configmap.yaml` (plan.json / config), `serviceaccount.yaml`. Для каждого namespace:
  `values-dev.yaml` / `values-staging.yaml` / `values-prod.yaml`.
- `deploy/argocd/sentinel-app.yaml`: ArgoCD `Application` → chart (auto-sync, домашний репозиторий).
- **VERIFY при деплое:** базовый образ для браузера Playwright; агент в кластере достигает своего target;
  persistent volume для `state/` (Ceph PVC), если store-gateway работает как sidecar.

## M5-2 visual heal (scaffold + gate)
- `browser.setOfMarks` → `{marks: [{mark:int, role, name, css, bbox}], screenshot_path}` — накладывает
  пронумерованный блок на каждый интерактивный элемент (DOM-eval bbox + data-URL/скриншот), возвращает карту mark→element.
- HealingEngine **Tier-7** (после сбоя L1–L6 + LLM-a11y И `completeness_ratio < 0.30`): Sonnet vision
  получает скриншот + метки, возвращает номер `mark`; мы извлекаем **реальный** локатор этого элемента
  (НЕ клик по координатам). Скидка ×0.85 (ADR-005). За `--heal-visual` (по умолчанию выключен).
- **PoC gate** (`agentctl heal-poc --scenarios <dir>`): запускает Tier-7 на ≥20 размеченных сценариях со сломанными
  селекторами, сообщает точность/полноту; **Tier-7 поставляется включённым только при ≥70%** (иначе остаётся scaffolded/выключен).
- ANTI-HALLUCINATION: не предполагать конкретную форму vision API — повторно использовать существующий путь Sonnet в
  `healing.py._llm_reground`; расширить его блоком с изображением; VERIFY API Anthropic image-block.

## M5-3 Postgres checkpointer
`brain/__main__.py`: если `CHECKPOINT_DSN` установлен → `AsyncPostgresSaver.from_conn_string(dsn)`, иначе
SqliteSaver (текущий). Замена одного конструктора; VERIFY пакет `langgraph-checkpoint-postgres` + API.

## Acceptance gate (Given/When/Then)
- **M5-1:** `helm template deploy/sentinel` рендерит валидные манифесты (offline); `docker build` создаёт
  образ, в котором `agentctl run --replay --ci` работает (пользователь запускает на кластере / локально); ArgoCD app линтуется.
- **M5-2:** `browser.setOfMarks` возвращает карту marks[]; Tier-7 подключён + выключен по умолчанию; harness `heal-poc`
  запускается и выводит показатель точности (пользователь предоставляет ключ + сценарии).
- **M5-3:** при установленном `CHECKPOINT_DSN` explore-запуск делает checkpoint в Postgres (запускается пользователем); без установки → SQLite (без изменений).

## Вне scope
Production observability (M4b) · multi-tenant SaaS · кросс-браузерная матрица · авто-слияние healedных планов.
