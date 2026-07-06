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
docker compose --profile webui up           # → http://localhost:8088/setup/
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

Для управления из командной строки (без полного прогона — прогон всё равно через Docker-образ):

```bash
curl -fsSL https://raw.githubusercontent.com/AlexGromer/sentinel/main/install.sh | sh
```

Установщик проверяет **checksum** (жёсткий отказ при несовпадении) + **Cosign-подпись** (если `cosign` установлен) и
кладёт `agentctl` в `~/.local/bin` (**без root**). Проверка: `agentctl version`.

## 6. Установка без доступа к интернету (air-gapped)

Скачать offline-bundle → перенести → `docker load` образов → `docker compose -f docker-compose.offline.yml`.
Детали: `docs/DISTRIBUTION.md` §6.

## 7. Дальше

- Полное руководство по тестированию и модели прогонов: `docs/TESTING.md`.
- Каталог локальных моделей и рантаймов: `docs/LOCAL_MODELS.md`.
- Co-pilot UI (Settings\|Tests, live-таймлайн): `docs/index.html`.
