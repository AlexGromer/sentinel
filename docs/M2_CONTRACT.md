# M2 Contract — "Self-Repairing Walker" (frozen 2026-06-23)

> 🌐 **Русский** (основная версия) · [English](M2_CONTRACT.en.md)

Цель: **самовосстанавливающееся** ядро. Когда замороженный локатор больше не разрешается на изменённом
DOM, агент детерминированно переустанавливает его (и, опционально, через LLM), проверяет исправление
на живом DOM, оценивает уверенность, сохраняет/амортизирует heal и аудитирует каждую попытку.

## Scope decision (ADR-012)
**В M2:** движок healing + **минимальный путь replay** для его offline-упражнения; элементные
*alternatives*, захваченные во время explore; промежуточное brain-локальное хранилище локаторов/аудита.
**Отложено (отдельные milestones):** Go `store-gateway` + gRPC + `proto` (M2b — до тех пор brain
владеет локальным SQLite-хранилищем, задокументированное временное отклонение от ADR-007 single-writer); миграция
транспорта MCP-SDK (GAP-VERIFY-002); уровень доверия M3 (plan_hash hard-abort, golden baselines,
структурированные exit codes, flake quarantine) остаётся в M3, даже несмотря на то что M2 вводит минимальный replay.

## Модель локаторов + alternatives (развивает plan.json из M1)
Локатор — ровно одно из: `{testid}`, `{role,name}`, `{label}`, `{text}`, `{css}`, `{xpath}`.
При explore каждый интерактивный элемент захватывает упорядоченный список **alternatives** (кандидаты L1–L6,
которым он в данный момент удовлетворяет). `PlannedAction` для клика становится:
```
{ ..., locator: <primary>, alternatives: [ {strategy:'testid', value, prior:0.95}, ... ] }
```
Захватывается детерминированно из DOM → `plan_hash` остаётся воспроизводимым. (Добавляет поле; планы M1
без `alternatives` всё ещё воспроизводятся, просто с меньшим числом вариантов heal.)

## pw-executor — новые инструменты (M2)
| Method | Params | Result |
|--------|--------|--------|
| `browser.interactives` | — | `{elements:[{role,name,testid,text,tag,css}]}` (DOM eval над buttons/links/inputs) |
| `browser.probe` | `{locator}` | `{count}` — разрешает кандидата, подсчитывает совпадения, БЕЗ действия (для verify-before-accept + ротации L1–L6) |
| `browser.click` (расширенный) | `{locator}` любого из 6 видов | `{clicked,url}` |
(`browser.navigate/snapshot/links/currentUrl/traceStop` без изменений.)
Маппинг локатор→Playwright: testid→`getByTestId`, role+name→`getByRole`, label→`getByLabel`,
text→`getByText`, css→`locator(css)`, xpath→`locator('xpath=…')`. **VERIFY** атрибут `getByTestId` по умолчанию (`data-testid`).

## Движок healing (`brain/healing.py`) — алгоритм (соответствует docs/SELF_HEALING.md, подмножество M2)
Входные данные `HealContext = {semantic_id, page_path, intent, attempted_locator, alternatives, dom_subtree_hash}`.
1. **Classify** сбой: `STALE` (счётчик probe равен 0, но элемент, вероятно, присутствует) vs `GONE`. (TIMING/visual = M3+.)
2. **Обновить восприятие** (`browser.snapshot` + `browser.interactives`); пересчитать `dom_subtree_hash` (M2: хэш на уровне страницы; subtree-scoping = M3).
3. **Cache lookup**: `healed_locators` с ключом `(page_path, semantic_id, dom_subtree_hash)` → повторно использовать при совпадении хэша (амортизация); вытеснять при устаревании.
4. **Ротация L1–L6** (детерминированная, **offline, без LLM**): строит кандидатов из `alternatives` + обновлённых `interactives`, зондирует каждого через `browser.probe`, берёт первого с разрешением ровно **1**, с приоритетом стратегии:

   | | strategy | prior |
   |---|---|---|
   | L1 | testid | 0.95 |
   | L2 | role + name | 0.90 |
   | L3 | aria-label | 0.88 |
   | L4 | text + role | 0.80 |
   | L5 | scoped css | 0.65 |
   | L6 | xpath | 0.45 |
5. **LLM re-grounding** (опционально, Sonnet 4.6; `--heal-llm`): только если L1–L6 не дали результата; gracefully деградирует без ключа. Скидка ×0.90.
6. **Verify-before-accept**: `browser.probe` выбранного кандидата на ЖИВОМ DOM → должно быть ровно 1, иначе confidence 0.
7. **Confidence gate**: ≥0.85 auto-heal · 0.60–0.84 помечается (применяется + отмечается для проверки) · <0.60 → пропуск (неинтерактивное) / human gate.
8. **Post-heal verification**: повторное выполнение действия с healedным локатором; только после этого сохраняется `status=active`.
9. **Аудит** (append-only): одна строка `healing_audit` на попытку {run_id, step, semantic_id, strategy, original, healed, confidence, outcome, dom_hash}.
10. **Амортизация**: сохраняет `healed_locators` с ключом `dom_subtree_hash`; повторно используется на шаге 3 при следующем запуске.

## Промежуточное хранилище (`brain/store.py` → `state/locators.db`, SQLite)
Таблицы: `healed_locators(page_path, semantic_id, strategy, value, confidence, dom_subtree_hash, status, times_used, created_at)`, `healing_audit(...)` append-only. **Временно**: записывается напрямую brain; M2b перемещает все записи за Go `store-gateway` (gRPC), восстанавливая ADR-007. Путь к состоянию включён в git-ignore.

## Минимальный путь replay
`agentctl run --replay --plan <plan.json> [--target <url>]` → загружает замороженные шаги; для каждого:
переходит как обычно; кликает через замороженный `locator` → `browser.probe`; если count≠1 → **heal** (движок выше) → повтор с healedным локатором. Генерирует `heal-report.json` (для каждого шага: ok | healed(strategy,confidence) | failed) + `healing_audit`. Нет уровня доверия M3.

## Фикстуры
`testdata/site-v2/` = копия `testdata/site/` с **дрейфом**: кнопка `Get started` на index переименована в **"Launch"** (сохраняет `data-testid="cta"`); `Finish` на page-c переименована в **"Complete"** (сохраняет `data-testid="finish"`). Таким образом, план, замороженный на `site/`, имеет устаревшие локаторы role+name, которые **исцеляются через testid (L1)** на `site-v2/`.

## Acceptance gate (Given/When/Then)
- **GIVEN** plan.json, исследованный на `testdata/site/` (с `alternatives` вкл. testid) и дрейфовавший `testdata/site-v2/`,
- **WHEN** `agentctl run --replay --plan <plan.json> --target file://.../site-v2/index.html`,
- **THEN** ≥1 локатор клика, сломавшийся по name, **исцелён через testid (L1)** с confidence ≥0.85 и применён автоматически; `heal-report.json` фиксирует heal; `healing_audit` содержит строку; replay завершается; результат heal детерминирован между запусками; второй replay повторно использует закэшированный healedный локатор (0 свежих ротаций).

## Вне scope (позже)
Go store-gateway + gRPC + proto (M2b) · MCP-SDK transport (GAP-VERIFY-002) · golden baselines, plan_hash hard-abort, exit codes, flake quarantine (M3) · subtree-scoped dom_hash, set-of-marks visual heal (M3/M5).
