# M4 Contract — "Production-Observable" (frozen 2026-06-24)

> 🌐 **Русский** (основная версия) · [English](M4_CONTRACT.en.md)

Цель: сделать запуск **потребляемым людьми и машинами** — экспортировать переиспользуемые Playwright-тесты, генерировать
читаемый отчёт + метрики Prometheus и выводить данные калибровки healing. Value-first + offline-тестируемый.

## Scope decision (ADR-014)
**В M4 (чистые генераторы, offline-тестируемые):** экспорт `.spec.ts` из плана; HTML + JSON отчёт о запуске;
текстовые метрики Prometheus включая `healing_confidence_histogram`; `agentctl calibrate`.
**Отложено:** Go `report-service` (ARCHITECTURE §2) и OTel→Tempo / Prometheus HTTP `/metrics`
endpoint и потолок бюджета на стороне Go — они требуют слоя Go-сервисов (M2b) или внешней инфраструктуры.
Генераторы M4 работают в **brain (Python)** сейчас, читая артефакты запуска + промежуточное хранилище; Go
report-service — их конечное место, как только M2b консолидирует персистентность.

## Новые подкоманды agentctl (каждая — RUN_MODE в brain; браузер не нужен)
| Команда | RUN_MODE | Читает | Записывает |
|---------|----------|--------|------------|
| `agentctl export-spec --plan <p> [-o <file>]` | export-spec | plan.json | `<run>/exported.spec.ts` (или `-o`) |
| `agentctl report --run <dir>` | report | `<dir>/heal-report.json` (+ plan.json) | `<dir>/report.json`, `report.html`, `metrics.prom` |
| `agentctl calibrate` | calibrate | `state/locators.db` `healing_audit` | `state/calibration.json` (+ stdout) |

## Экспорт .spec.ts (brain/exporter.py)
Чистая функция `plan -> str` (нет браузера, нет зависимости от MCP-codegen — ADR выполнен). Генерирует идиоматический
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
Маппинг локатор → Playwright (совпадает с `buildLocator` pw-executor): testid→`getByTestId`, role+name→
`getByRole(role,{name})`, label→`getByLabel`, text→`getByText`, css→`locator`, xpath→`locator('xpath=…')`;
navigate→`page.goto`. Строки экранированы. **Детерминированный**: тот же план → побитово идентичный spec.

## HTML + JSON отчёт (brain/report.py)
Из `heal-report.json`: самодостаточный HTML (без внешних ресурсов) — заголовок (run_id, mode, target,
**exit code** с цветом), таблица для каждого шага (шаг / тип / результат / стратегия heal+confidence /
регрессия / quarantined) и сводные счётчики. `report.json` = те же данные, машиночитаемые.

## Текстовые метрики Prometheus (`metrics.prom`)
Формат textfile-collector для node_exporter (нет HTTP-сервера). Метрики:
`sentinel_run_steps`, `sentinel_run_exit_code`, `sentinel_heal_total{strategy}`,
`sentinel_regression_total{kind}` (a11y / visual), `sentinel_quarantined_total`,
`sentinel_healing_confidence_bucket{strategy,le}` (гистограммные бакеты 0.60/0.85/1.0).

## agentctl calibrate (brain/calibrate.py)
Читает `healing_audit`. M4 (без верифицированных человеком меток пока): сообщает счётчики результатов по стратегии
(auto_healed / flagged / needs_review / failed / cache_hit), гистограмму confidence и активный
порог (0.85; cold-start 0.90). Записывает `calibration.json`. Полная точность/полнота vs верифицированных человеком
результатов будет подключена, когда приземлится human gate (в будущем). Основа цикла калибровки ADR-008.

## Acceptance gate (Given/When/Then)
1. **GIVEN** plan.json, **WHEN** `agentctl export-spec --plan <p>`, **THEN** синтаксически корректный
   `.spec.ts` существует, содержащий `page.goto` + локаторы клика; **детерминированный** (re-export = идентичные байты);
   `npx tsc --noEmit` (с типами @playwright/test) не сообщает ошибок.
2. **GIVEN** `heal-report.json` из запуска с дрейфом, **WHEN** `agentctl report --run <dir>`, **THEN**
   `report.html` (валидный, показывает heal'ы + строки a11y-регрессии), `report.json` и корректный `metrics.prom` существуют.
3. **GIVEN** предыдущие heal'ы в хранилище, **WHEN** `agentctl calibrate`, **THEN** `calibration.json` со
   счётчиками результатов по стратегиям + гистограммой confidence.
4. Offline unit-тесты покрывают exporter / report / metrics / calibrate с фикстурами (без браузера).

## Вне scope (позже)
Go report-service (после M2b) · OTel collector/Tempo + Prometheus HTTP endpoint · потолок токенного бюджета на стороне Go
(требует Go orchestrator, M2b) · учёт токенов live-транскрипта LLM помимо того, что уже записывает explore.
