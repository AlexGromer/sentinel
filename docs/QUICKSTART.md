# Sentinel — быстрый старт

🌐 [English](QUICKSTART.en.md) · RU

От нуля до первого автономного explore-прогона за ≤ 10 минут. Для QA/devops с Docker — **без** Go/Python/Node
build-toolchain и без чтения полной документации.

## 1. Требования

- **Docker ≥ 24** (+ `docker compose` v2);
- **git** (получить исходники — образ строится локально, пока не опубликован GHCR-релиз).

## 2. Получить Sentinel

```bash
git clone https://github.com/AlexGromer/sentinel.git
cd sentinel
```

> Docker-путь строит образ `sentinel:local` из исходников (`docker-compose.yml` + `Dockerfile`), поэтому нужен
> checkout репозитория. После первого подписанного релиза появится готовый образ в GHCR (тогда clone станет опциональным).

## 3. Первый запуск (Docker, без toolchain)

```bash
docker compose build                        # первый раз — собрать sentinel:local
docker compose run --rm sentinel run --target "https://example.com" --planner heuristic
```

Без API-ключа / локальной модели прогон идёт на детерминированном эвристическом планировщике (offline, self-contained).

**Конфигурация без ручного YAML** — мастер setup-WebUI:

```bash
docker compose up                           # → http://localhost:8088/setup/ (весь стек, без флагов)
# затем: docker compose run --rm sentinel run --run-config /config/run.yaml
```

## 4. Интерпретация результата

Артефакты — в `runs/<id>/`: `plan.json` (замороженный план), `transcript`, `heal-report.json`, `trace.zip`.
**Exit-code** (структурный, `docs/STATE_MACHINE.md`):

| Код | Значение |
|---|---|
| 0 | pass — план исполнен, регрессий нет |
| 1 | step-fail — шаг не выполнился |
| 2 | golden-регрессия — DOM-дрейф, потребовалось лечение/диф |
| 3 | integrity — несовпадение `plan_hash`/golden-HMAC **или** исчерпание бюджета |

## 5. Опционально: нативный CLI `agentctl`

Для управления из командной строки (без полного прогона — прогон всё равно через Docker-образ). Установщик
проверяет **checksum** (жёсткий отказ при несовпадении) + **Cosign-подпись** (если `cosign` установлен) и кладёт
бинарь в пользовательский каталог (**без root/admin**). Проверка: `agentctl version`.

- **Linux / macOS:** `curl -fsSL https://raw.githubusercontent.com/AlexGromer/sentinel/main/install.sh | sh` → `~/.local/bin`
- **Windows (PowerShell):** `iwr -useb https://raw.githubusercontent.com/AlexGromer/sentinel/main/install.ps1 | iex` → `%LOCALAPPDATA%\Programs\sentinel`
- **macOS (Homebrew):** `brew tap AlexGromer/sentinel https://github.com/AlexGromer/sentinel && brew install sentinel` *(работает после первого подписанного релиза)*

## 6. Установка без доступа к интернету (air-gapped)

Скачать offline-bundle → перенести → `docker load` образов → `docker compose -f docker-compose.offline.yml`.
Детали: `docs/DISTRIBUTION.md` §6.

## 7. Дальше

- Полное руководство по тестированию и модели прогонов: `docs/TESTING.md`.
- Каталог локальных моделей и рантаймов: `docs/LOCAL_MODELS.md`.
- Co-pilot UI — вертикальный рельс по ходу сессии (ADR-066). Перечень видов здесь намеренно не дублируется: его задаёт сам хаб (`docs/index.html`), а выводит `scripts/hub-views.mjs`. Самый быстрый способ поднять (ADR-064,
  single-service, без CORS-настройки): `CONTROL_API_SERVE_UI=1 CONTROL_API_CORS_ORIGINS= docker compose up control-api`,
  затем открыть одноразовую ссылку `http://127.0.0.1:8090/?bootstrap=<nonce>` из лога запуска (действует 5 минут,
  сама подставляет адрес и токен). Три режима запуска UI и токен доступа — `docs/DISTRIBUTION.md` §2.
