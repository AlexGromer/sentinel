# M3 Contract — "CI-Ready Replay / Trust Layer" (frozen 2026-06-23)

> 🌐 **Русский** (основная версия) · [English](M3_CONTRACT.en.md)

Цель: сделать replay **надёжным в CI**. Недетерминированный агент структурно должен быть неспособен
запустить подделанный план или молча переписать собственный baseline. Реализует ADR-006 (+ ADR-013).

## Scope decision
**В M3 (детерминированный, offline-тестируемый):** `plan_hash` hard-abort; структурированные exit codes 0/1/2/3;
два **golden baselines** (a11y-hash + screenshot-hash) с обновлением только оператором + golden-diff
обнаружение регрессии; **AUT-SHA-gated flake quarantine**; **GitHub Actions** CI.
**Отложено:** Go gRPC orchestrator + store-gateway (M2b); subtree-scoped dom_hash + set-of-marks (позже).

## Структурированные exit codes (возвращает brain; propagates agentctl)
| Код | Значение |
|------|---------|
| 0 | все неизолированные шаги прошли, нет golden-регрессии |
| 1 | шаг завершился неудачей (локатор неизлечим / ошибка действия) на неизолированном шаге |
| 2 | golden-diff регрессия (a11y-hash или screenshot-hash отличается от golden) на неизолированной странице |
| 3 | **plan integrity** (несовпадение plan_hash) или бюджет — hard-abort, наивысший приоритет, ничего не выполнено |

## plan_hash HARD-ABORT (ADR-006)
В начале replay: пересчитать `canonical_plan_hash(plan["steps"])`, сравнить с `plan["plan_hash"]`.
Несовпадение → немедленный abort, **exit 3**, логирование сохранённого vs вычисленного. Вручную отредактированный / частично healedный
план никогда не может выполниться молча. По умолчанию hard-abort; `--force-replay` обходит с громким предупреждением
и **запрещён при `--ci`**.

## Golden baselines — двойной хэш, с ключом по странице (ADR-006)
- **page key** = basename нормализованного URL (`index.html`, `page-a.html`, …), поэтому план, исследованный на
  `site/`, сравнивается с `site-v2/`.
- **golden record** `{page_key, a11y_hash, screenshot_hash, created_at}` — НЕИЗМЕНЯЕМЫЙ, кроме как через
  единственный явный путь мутации: `agentctl baseline update`.
- `agentctl baseline update --plan <p> [--target <url>]` → воспроизводит план, захватывает текущий
  a11y-hash (`sha256(ariaSnapshot)`) + screenshot-hash для каждой посещённой страницы, записывает goldens (архивируя
  предыдущие). **CI-replay никогда не записывает goldens** → «тесты не могут переписывать собственный baseline».
- При replay (не baseline): при первом посещении каждой страницы вычисляются текущие хэши; сравниваются с
  golden для данного page_key; несовпадение → регрессия. Фиксируется в `heal-report.json`.
- **Уточнение M3 (реализовано):** goldens захватываются **один раз на странице при первом приземлении** — baseline
  и replay симметричны, поэтому последующий клик не может сместить golden. **a11y-hash управляет exit 2**
  (детерминированный); **регрессия screenshot-hash является информационной** (сообщается, не влияет на exit) до
  укрепления детерминизма скриншотов в кросс-процессном режиме — GAP-RISK-009.

## Heal + golden-diff сосуществуют (ADR-013)
Healedный шаг **всё равно выполняется** (replay продолжается) И golden-diff **всё равно запускается** на странице.
Таким образом, дрейф имён, healedный через testid, *также* сигнализирует об a11y golden-регрессии → exit 2. Healing =
устойчивость тестов; golden-diff = обнаружение изменений. Оба отражаются для каждого шага.

## AUT-SHA-gated flake quarantine
- `--aut-version <sha>` (например, `git rev-parse HEAD` тестируемого приложения), записывается для каждого запуска.
- store `step_failures(plan_id, step_key, last5 json, last_aut_sha, quarantined)`.
- сбой считается «нестабильным» ТОЛЬКО если `aut_version` не изменился по сравнению с предыдущим запуском (отделяет реальную
  регрессию от нестабильности окружения). Изоляция, когда шаг завершается неудачей **≥3 из последних 5** запусков без
  изменения AUT-SHA.
- изолированные шаги всё равно выполняются, но НЕ учитываются в exit 1/2; снимается после 3 последовательных прохождений
  или `agentctl locators clear-quarantine`.

## pw-executor — новый инструмент (M3)
`browser.screenshotHash` → `{ hash }`: `sha256(await page.screenshot())`, хэшируется в TS (нет байтов через stdio).

## agentctl — новая поверхность (M3)
- `agentctl baseline update --plan <p> [--target <url>]` — единственный путь мутации golden.
- `run --replay --plan <p> [--target <url>] --aut-version <sha> [--ci]` — `--ci` запрещает `--force-replay`.
- `agentctl locators clear-quarantine` (минимальный M3: очищает step_failures).
- store получает таблицы `golden_snapshots` + `step_failures` (временно, brain-локальные; → store-gateway @ M2b).

## GitHub Actions (`.github/workflows/ci.yml`)
`build` (Go + TS `npm ci`/build + зависимости `uv` + `playwright install` + `go vet`/`go test ./...` + offline-suite m3..m9_2b) → матрица `replay` (SQLite для каждого задания);
параллельный job `security` (gitleaks/govulncheck/pip-audit/npm audit); задание `explore` ручное/`workflow_dispatch`. Проверяет exit codes. С #4 эти шаги реально исполняются в CI.

## Acceptance gate (Given/When/Then)
1. **GIVEN** план, исследованный на `site/`, **WHEN** `agentctl baseline update --plan <p>`, **THEN** goldens существуют для index/page-a/b/c.
2. **WHEN** `run --replay --plan <p> --target site/index.html --ci` (без изменений), **THEN** exit **0** (нет регрессии, нет heal).
3. **WHEN** `run --replay --plan <p> --target site-v2/index.html --ci` (с дрейфом), **THEN** локаторы healятся И golden-diff сигнализирует об a11y-регрессии на index/page-c → exit **2**; `heal-report.json` показывает и heal, и регрессию.
4. **WHEN** воспроизводится вручную отредактированный план (один байт изменён в шаге), **THEN** exit **3** (hard-abort), ничего не выполнено.
5. Детерминированность: одинаковые входные данные → одинаковый exit code + вердикт plan_hash.

## Вне scope (позже)
Go gRPC orchestrator/store-gateway (M2b) · MCP-SDK transport (GAP-VERIFY-002) · subtree-scoped dom_hash, set-of-marks visual heal, реальные OTel/Prometheus (M4/M5).
