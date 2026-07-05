# Contract M15 — metrics/results-in-UI (implements ADR-051)

> Status: **ADR-051 (Accepted-design) → M15 implementation**. One branch `feat/m15-metrics-ui` on top of main. Token-cost ($) → M15.1.

## 1. Why

M13 shipped the `results` + `metrics` domains (proto + `internal/store/domains.go` + DDL + round-trip
tests) but wired **no caller**. M14 left the SPA `results`/`metrics` subpanels as labeled M15-stubs.
ADR-051: run metrics → our store domains → **native charts** in the vanilla SPA (Grafana-embed rejected,
build-only; Prometheus/Grafana stay an optional export, `brain/report.py`, untouched). M15 = **wire**
(extract → ingest → HTTP) + **render** (inline-SVG), removing the stubs.

## 2. Metrics & their sources (assembled at the control-API finish, NO brain change)

| Metric | Source |
|---|---|
| verdict (enum `pass\|problem\|regression\|integrity`) | `verdictEnum(rec.ExitCode)` (0/1/2/3) |
| steps · healed · failed · regressions | `heal-report.json` (replay/baseline) |
| coverage | `plan.json` `coverage_achieved` (authoring; replay has none — per-mode) |
| duration_ms | `FinishedAt − StartedAt` (RFC3339, second precision) |
| **token-cost ($)** | **→ M15.1** (needs brain token-total emission + a price table; $ validated live = RISK-003) |

`persistResult(rec)` in the finish goroutine (`cmd/control-api/main.go`, beside `persistScenario`):
fail-open (a malformed/missing artifact never aborts the run/goroutine) → `saveResult` (ResultRecord) +
`ingestMetrics` (trend points). Each point carries `labels_json={mode,target}`. **Edge cases:** runs that failed to
spawn (`state=failed`, agentctl couldn't start → `ExitCode` stays 0) are **NOT** recorded as results
(no outcome; the runs domain logs them) — otherwise `verdictEnum(0)="pass"` would inflate the pass-rate.
The SPA coverage chart excludes runs without coverage (replay/baseline would render as "0%").

## 3. HTTP surface (`cmd/control-api`)

- `GET /v1/results` (`limit`/`offset` paging) → `{"results":[…],"total":N}`
- `GET /v1/results/{id}` → ResultRecord | 404
- `GET /v1/trends?metric=&window=` → `{"metric":…,"points":[…]}` (last N points, chronological)

All token-gated (`authed`), fail-open (empty list without a store, never a 503). Client —
`cmd/control-api/store.go` (`saveResult`/`getResult`/`listResults`/`ingestMetrics`/`trends`, best-effort).

## 4. SPA — native inline-SVG (`docs/index.html`, ADR-055 vanilla)

The `results` + `metrics` subpanels (Tests tab): `tLoadResults`/`tLoadMetrics` (fetch → `esc()`/`bi()`
render), lazy-loaded in `tSubTab`. Charts = inline-SVG string-building (`svgBars` — coverage per run,
colour = verdict; `svgSpark` — trend sparklines). Colours go through `style="fill:var(--x)"` — CSS custom
properties do **NOT** resolve in the `fill`/`stroke` presentation attribute, only in a CSS property.
CSP-safe: no canvas, no external requests. Bilingual (RU/EN).

## 5. Metric names (store `metrics.name`)

`pass` (1/0) · `coverage` · `healed` · `failed` · `regressions` · `steps` · `duration_ms`. The Prometheus
`sentinel_*` names (`brain/report.py`) are a separate optional export, NOT this UI feed.

## 6. Open-core / commercial seam (applies ADR-056)

M15 is entirely **base/Apache** (data + basic metrics/trends + charts = open-core, useful for one team,
not crippleware). Only **enterprise-BI** (org rollups · cost-chargeback · ML-flake · long-retention ·
BI-export · multi-tenant) is reserved for commercial (`sentinel-enterprise`, later). Mechanism: a
commercial module is a **pure consumer** of the `metrics`/`results` domains over the token-authed
store-gateway (`QueryMetrics`/`Trends`/`ListResults`); `metrics.labels_json` (currently `{mode,target}`)
is the seam for org/project/team tags and rollups — **zero base schema change**, no core fork.
Generalizes ADR-045 (adapters/SPI) → ADR-056 (module-registry).

## 7. Deferred

- token-cost ($) → **M15.1** (brain token-total emission + price table; validated live = RISK-003).
- Sub-second duration (RFC3339 is second-precision → duration is a multiple of 1000 ms).
- `QueryMetrics` time-window in the UI (the RPC exists; the UI uses `Trends`).

## 8. Acceptance criteria

1. `persistResult` on finish writes a ResultRecord (verdict + heal/fail + coverage + duration) + metrics, fail-open.
2. `/v1/results`, `/v1/results/{id}`, `/v1/trends` token-gated + fail-open + JSON shape.
3. SPA renders native SVG (bars + sparklines), stubs gone, bilingual, CSP-safe, lazy-loaded.
4. Gates: go build/vet/test (+`results_test.go`) · node --check · 18/18 offline unaffected · bilingual · gitleaks.
5. token-cost → M15.1 documented; `brain` untouched; data/metrics entirely in base/Apache.
