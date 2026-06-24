# M4 Contract — "Production-Observable" (frozen 2026-06-24)

Goal: make a run **consumable by humans and machines** — export reusable Playwright tests, emit a
readable report + Prometheus metrics, and surface healing calibration data. Value-first + offline-testable.

## Scope decision (ADR-014)
**In M4 (pure generators, offline-testable):** `.spec.ts` export from a plan; HTML + JSON run report;
Prometheus textfile metrics incl. `healing_confidence_histogram`; `agentctl calibrate`.
**Deferred:** the Go `report-service` (ARCHITECTURE §2) and OTel→Tempo / Prometheus HTTP `/metrics`
endpoint and the Go-side budget ceiling — they need the Go service layer (M2b) or external infra.
M4 generators run in the **brain (Python)** now, reading run artifacts + the interim store; the Go
report-service is their eventual home once M2b consolidates persistence.

## New agentctl subcommands (each a brain RUN_MODE; no browser needed)
| Command | RUN_MODE | Reads | Writes |
|---------|----------|-------|--------|
| `agentctl export-spec --plan <p> [-o <file>]` | export-spec | plan.json | `<run>/exported.spec.ts` (or `-o`) |
| `agentctl report --run <dir>` | report | `<dir>/heal-report.json` (+ plan.json) | `<dir>/report.json`, `report.html`, `metrics.prom` |
| `agentctl calibrate` | calibrate | `state/locators.db` `healing_audit` | `state/calibration.json` (+ stdout) |

## .spec.ts export (brain/exporter.py)
Pure function `plan -> str` (no browser, no MCP-codegen dependency — ADR satisfied). Emits idiomatic
`@playwright/test`:
```ts
import { test, expect } from '@playwright/test';
test('sentinel: <plan_id>', async ({ page }) => {
  await page.goto('<target_url>');
  await page.getByRole('button', { name: 'Get started' }).click();   // step 2
  await page.goto('<next url>');                                      // step 3
  // ...
});
```
Locator → Playwright mapping (matches pw-executor `buildLocator`): testid→`getByTestId`, role+name→
`getByRole(role,{name})`, label→`getByLabel`, text→`getByText`, css→`locator`, xpath→`locator('xpath=…')`;
navigate→`page.goto`. Strings escaped. **Deterministic**: same plan → byte-identical spec.

## HTML + JSON report (brain/report.py)
From `heal-report.json`: a self-contained HTML (no external assets) — header (run_id, mode, target,
**exit code** with color), a per-step table (step / type / outcome / heal strategy+confidence /
regression / quarantined), and summary counts. `report.json` = the same data, machine-readable.

## Prometheus textfile metrics (`metrics.prom`)
node_exporter textfile-collector format (no HTTP server). Metrics:
`sentinel_run_steps`, `sentinel_run_exit_code`, `sentinel_heal_total{strategy}`,
`sentinel_regression_total{kind}` (a11y / visual), `sentinel_quarantined_total`,
`sentinel_healing_confidence_bucket{strategy,le}` (histogram buckets 0.60/0.85/1.0).

## agentctl calibrate (brain/calibrate.py)
Reads `healing_audit`. M4 (no human-verified labels yet): reports outcome counts by strategy
(auto_healed / flagged / needs_review / failed / cache_hit), the confidence histogram, and the active
threshold (0.85; cold-start 0.90). Writes `calibration.json`. Full precision/recall vs human-verified
outcomes is wired when the human gate lands (future). Foundation for ADR-008's calibration loop.

## Acceptance gate (Given/When/Then)
1. **GIVEN** a plan.json, **WHEN** `agentctl export-spec --plan <p>`, **THEN** a syntactically valid
   `.spec.ts` exists containing `page.goto` + the click locators; **deterministic** (re-export = identical bytes);
   `npx tsc --noEmit` (with @playwright/test types) reports no errors.
2. **GIVEN** a drift run's `heal-report.json`, **WHEN** `agentctl report --run <dir>`, **THEN**
   `report.html` (valid, shows heals + a11y regression rows), `report.json`, and a valid `metrics.prom` exist.
3. **GIVEN** prior heals in the store, **WHEN** `agentctl calibrate`, **THEN** `calibration.json` with
   per-strategy outcome counts + confidence histogram.
4. Offline unit tests cover exporter / report / metrics / calibrate with fixtures (no browser).

## Out of scope (later)
Go report-service (post-M2b) · OTel collector/Tempo + Prometheus HTTP endpoint · Go-side token budget
ceiling (needs Go orchestrator, M2b) · live LLM transcript token accounting beyond what explore already writes.
