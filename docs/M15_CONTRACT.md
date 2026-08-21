# Контракт M15 — metrics/results-in-UI (реализует ADR-051)

> 🌐 **Русский** (основная версия) · [English](M15_CONTRACT.en.md)

> Статус: **ADR-051 (Accepted-design) → реализация M15**. Одна ветка `feat/m15-metrics-ui` поверх main. Token-cost ($) → M15.1.

## 1. Зачем

M13 поставил домены `results` + `metrics` (proto + `internal/store/domains.go` + DDL + round-trip тесты),
но НЕ подключил ни одного вызывающего. M14 оставил SPA-подпанели `results`/`metrics` помеченными
M15-заглушками. ADR-051: метрики прогона → наши store-домены → **native-графики** в vanilla-SPA
(Grafana-embed отклонён по build-only; Prometheus/Grafana — опц. экспорт, `brain/report.py`, не тронут).
M15 = **подключить** (extract → ingest → HTTP) + **отрисовать** (inline-SVG), убрав заглушки.

## 2. Метрики и их источники (собираются на control-API-finish, БЕЗ правок brain)

| Метрика | Источник |
|---|---|
| verdict (enum `pass\|problem\|regression\|integrity`) | `verdictEnum(rec.ExitCode)` (0/1/2/3) |
| steps · healed · failed · regressions | `heal-report.json` (replay/baseline) |
| coverage | `plan.json` `coverage_achieved` (authoring; у replay нет — per-mode) |
| duration_ms | `FinishedAt − StartedAt` (RFC3339, секундная точность) |
| **token-cost ($)** | **→ M15.1** (нужна brain-эмиссия токен-тоталов + price-таблица; валидация live = GAP-RISK-003) |

`persistResult(rec)` в finish-горутине (`cmd/control-api/main.go`, рядом с `persistScenario`): fail-open
(битый/отсутствующий артефакт не рушит прогон/горутину) → `saveResult` (ResultRecord) + `ingestMetrics`
(точки трендов). Каждая точка несёт `labels_json={mode,target}`. **Краевые случаи:** прогоны, не
запустившиеся (`state=failed`, agentctl не спавнится → `ExitCode` остаётся 0), **НЕ** пишутся как
результат (нет исхода; фиксируются в runs-домене) — иначе `verdictEnum(0)="pass"` завысил бы pass-rate.
Coverage-график в SPA исключает прогоны без coverage (replay/baseline рисовались бы как «0%»).

## 3. HTTP-поверхность (`cmd/control-api`)

- `GET /v1/results` (пагинация `limit`/`offset`) → `{"results":[…],"total":N}`
- `GET /v1/results/{id}` → ResultRecord | 404
- `GET /v1/trends?metric=&window=` → `{"metric":…,"points":[…]}` (последние N точек, хронологически)

Все token-gated (`authed`), fail-open (пустой список без store, не 503). Клиент —
`cmd/control-api/store.go` (`saveResult`/`getResult`/`listResults`/`ingestMetrics`/`trends`, best-effort).

## 4. SPA — native inline-SVG (`docs/index.html`, ADR-055 vanilla)

Подпанели `results` + `metrics` (вкладка Tests): `tLoadResults`/`tLoadMetrics` (fetch → `esc()`/`bi()`
render), lazy-load в `tSubTab`. Графики — inline-SVG string-building (`svgBars` — покрытие по прогонам,
цвет = verdict; `svgSpark` — тренд-спарклайны). Цвета через `style="fill:var(--x)"` — CSS-переменные
**НЕ** резолвятся в presentation-атрибуте `fill`/`stroke`, только в CSS-свойстве. CSP-safe: без canvas,
без внешних запросов. Bilingual (RU/EN).

## 5. Метрики-имена (store `metrics.name`)

`pass` (1/0) · `coverage` · `healed` · `failed` · `regressions` · `steps` · `duration_ms`. Prometheus
`sentinel_*` (`brain/report.py`) — отдельный опц. экспорт, НЕ этот UI-фид.

## 6. Open-core / commercial seam (применяет ADR-056)

M15 целиком **base/Apache** (данные + базовые метрики/тренды + графики = open-core, полезно одной
команде, не crippleware). Только **enterprise-BI** (org-rollups · cost-chargeback · ML-flake ·
long-retention · BI-export · multi-tenant) резервируется под commercial (`sentinel-enterprise`, позже).
Механизм: коммерческий модуль — **чистый потребитель** доменов `metrics`/`results` через token-authed
store-gateway (`QueryMetrics`/`Trends`/`ListResults`); `metrics.labels_json` (сейчас `{mode,target}`) =
seam для org/project/team-тегов и rollup'ов — **zero base schema change**, без форка core. Обобщает
ADR-045 (adapters/SPI) → ADR-056 (module-registry).

## 7. Отложено

- token-cost ($) — **✅ доставлено в M15.1**: brain эмитит per-run токен-тоталы (`budget.summary()`) в `plan.json`/`heal-report.json`; control-API ингестит `tokens_total/prompt/completion`+`cost_usd` (best-effort: локальные модели → $0, счётчики точны); SPA — тренд-спарклайны Tokens+Cost. **Разблокирует ЗАМЕР по `GAP-RISK-003`** — сам риск ОТКРЫТ. ⚠ Здесь стояло «Снимает GAP-RISK-003»; поправлено 2026-08-21 (`[DOCS-REGISTERS]`): M15.1 дал ЭМИССИЮ токен-чисел, а замер стоимости на 50+-страничном SPA не сделан — мишени такого размера в дереве нет (`[PROD-FIXTURE-SPA]`). Статус — в `GAPS.md`.
- Sub-second duration (сейчас RFC3339 секундная точность → duration кратна 1000 мс).
- `QueryMetrics` окно-по-времени в UI (RPC есть; UI использует `Trends`).

## 8. Критерии приёмки

1. `persistResult` на finish пишет ResultRecord (verdict + heal/fail + coverage + duration) + метрики, fail-open.
2. `/v1/results`, `/v1/results/{id}`, `/v1/trends` token-gated + fail-open + JSON-форма.
3. SPA рисует native-SVG (bars + sparklines), заглушки убраны, bilingual, CSP-safe, lazy-load.
4. Гейты: go build/vet/test (+`results_test.go`) · node --check · 18/18 offline не тронуты · bilingual · gitleaks.
5. token-cost → M15.1 задокументирован; `brain` не тронут; данные/метрики целиком в base/Apache.
