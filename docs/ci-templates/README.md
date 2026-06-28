# Sentinel — CI-шаблоны

> 🌐 **Русский** (основная версия) · [English](README.en.md)

> Готовые шаблоны для **вашего** CI. Это **не** наши `.github/workflows/` — это примеры, которые
> вы копируете в свой репозиторий, чтобы гонять Sentinel UI-тесты на каждый коммит.

## Что это

Sentinel — это CLI со **структурными кодами выхода** (`0/1/2/3`), поэтому он встаёт в любой CI как
обычный шаг сборки: запустить прогон → код выхода → вердикт. В папке два шаблона:

- [`Jenkinsfile`](Jenkinsfile) — декларативный pipeline (Jenkins).
- [`.gitlab-ci.yml`](.gitlab-ci.yml) — GitLab CI (Docker-in-Docker).

Оба делают одно и то же: собирают образ, **реплеят замороженный `plan.json`** против живого приложения
и мапят код выхода на вердикт сборки.

## Предпосылки

- Docker (для GitLab — Docker-in-Docker).
- Чекаут репозитория Sentinel рядом (или опубликованный образ — после M11.1).
- Закоммиченный замороженный план `config/plan.json` (см. [«Как заморозить план»](#как-заморозить-план)).
- Опционально `ANTHROPIC_API_KEY` (секрет CI) — включает LLM-self-healing; без ключа работает
  детерминированный heuristic + L1–L6 heal (полностью офлайн).

Команда прогона (одинаковая для обоих шаблонов):

```sh
docker compose run --rm sentinel run --target "$TARGET" --replay --plan /config/plan.json --ci \
  --aut-version "$COMMIT_SHA"
```

`--ci` запрещает обход plan_hash hard-abort: подменённый/устаревший план падает fail-closed (exit 3).
`/config/plan.json` берётся из примонтированной папки `./config` (см. `docker-compose.yml`).

## Коды выхода → вердикт CI

| exit | Значение | Вердикт CI |
|------|----------|------------|
| `0` | pass — UI ведёт себя как в замороженном плане | ✅ success |
| `1` | step-fail — упал assert / описанный поток исчез из UI | ❌ failure («тест нашёл проблему») |
| `2` | golden/visual-регрессия — дрейф a11y-baseline | ❌ failure (UI регрессировал) |
| `3` | plan-integrity / ошибка конфигурации (несовпадение plan_hash, битый конфиг) | ⚠ warning/unstable — **нужен человек** (ре-baseline или фикс конфига) |

Источник правды: `cmd/agentctl/main.go:10` · `docs/M3_CONTRACT.md` · `docs/TESTING.md`.

## Jenkins

Скопируйте [`Jenkinsfile`](Jenkinsfile) в корень репозитория. Задайте `TARGET` (и при желании
`ANTHROPIC_API_KEY` через Jenkins credentials). Маппинг: `0`→PASS, `1`/`2`→`error` (FAILURE),
`3`→`currentBuild.result = 'UNSTABLE'`. Артефакты `runs/**` архивируются в `post { always }`.

## GitLab CI

Скопируйте [`.gitlab-ci.yml`](.gitlab-ci.yml). Задайте `TARGET` в `variables` и `ANTHROPIC_API_KEY`
как masked-переменную. Job завершается **собственным** кодом Sentinel; `allow_failure.exit_codes: [3]`
делает exit 3 «жёлтым» предупреждением, а `1`/`2` — красным провалом. Артефакты — `runs/`.

## Как заморозить план

1. Авторинг сценария: через [чат-фронт](../chat/) (опиши тест словами) **или**
   `agentctl run --target <URL> --describe "…"` / `--goal "…"` → получите `runs/<id>/scenario.json`
   (+ `plan.json`).
2. Зафиксируйте golden-baseline: `agentctl baseline update --plan runs/<id>/plan.json` — это
   **единственный** путь мутации golden.
3. Закоммитьте `plan.json` в `config/plan.json`. Дальше CI реплеит его на каждый коммит.

## Офлайн / без ключа

Без `ANTHROPIC_API_KEY` Sentinel не падает: планировщик — детерминированный heuristic, heal — L1–L6
(без LLM-re-grounding). Это медленнее лечит сложные дрейфы, но прогон полностью офлайн и воспроизводим —
удобно для air-gapped CI. Для `file://`-фикстур сеть не нужна вовсе.
