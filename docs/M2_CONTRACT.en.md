# M2 Contract — "Self-Repairing Walker" (frozen 2026-06-23)

> 🌐 [Русский](M2_CONTRACT.md) (основная версия) · **English**

Goal: the **self-healing** core. When a frozen locator no longer resolves against a changed
DOM, the agent re-grounds it deterministically (and, optionally, via LLM), verifies the repair
against the live DOM, scores confidence, persists/amortizes the heal, and audits every attempt.

## Scope decision (ADR-012)
**In M2:** the healing engine + a **minimal replay** path to exercise it offline; element
*alternatives* captured at explore time; an interim brain-local locator/audit store.
**Deferred (own milestones):** Go `store-gateway` + gRPC + `proto` (M2b — until then the brain
owns a local SQLite store, a documented temporary deviation from ADR-007 single-writer); MCP-SDK
transport migration (GAP-VERIFY-002); the M3 trust layer (plan_hash hard-abort, golden baselines,
structured exit codes, flake quarantine) stays in M3 even though M2 introduces minimal replay.

## Locator model + alternatives (evolves M1 plan.json)
A locator is exactly one of: `{testid}`, `{role,name}`, `{label}`, `{text}`, `{css}`, `{xpath}`.
At explore time each interactive element captures an ordered **alternatives** list (the L1–L6
candidates it currently satisfies). A `PlannedAction` for a click becomes:
```
{ ..., locator: <primary>, alternatives: [ {strategy:'testid', value, prior:0.95}, ... ] }
```
Captured deterministically from the DOM → `plan_hash` stays reproducible. (Adds a field; M1 plans
without `alternatives` still replay, just with fewer heal options.)

## pw-executor — new tools (M2)
| Method | Params | Result |
|--------|--------|--------|
| `browser.interactives` | — | `{elements:[{role,name,testid,text,tag,css}]}` (DOM eval over buttons/links/inputs) |
| `browser.probe` | `{locator}` | `{count}` — resolve a candidate, count matches, NO action (for verify-before-accept + L1–L6 rotation) |
| `browser.click` (extended) | `{locator}` any of the 6 kinds | `{clicked,url}` |
(`browser.navigate/snapshot/links/currentUrl/traceStop` unchanged.)
Locator→Playwright mapping: testid→`getByTestId`, role+name→`getByRole`, label→`getByLabel`,
text→`getByText`, css→`locator(css)`, xpath→`locator('xpath=…')`. **VERIFY** `getByTestId` attribute
default (`data-testid`).

## Healing engine (`brain/healing.py`) — algorithm (maps docs/SELF_HEALING.md, M2 subset)
Input `HealContext = {semantic_id, page_path, intent, attempted_locator, alternatives, dom_subtree_hash}`.
1. **Classify** the failure: `STALE` (probe count 0 but element likely present) vs `GONE`. (TIMING/visual = M3+.)
2. **Refresh perception** (`browser.snapshot` + `browser.interactives`); recompute `dom_subtree_hash` (M2: page-scoped hash; subtree-scoping = M3).
3. **Cache lookup**: `healed_locators` keyed `(page_path, semantic_id, dom_subtree_hash)` → reuse if hash matches (amortization); evict if stale.
4. **L1–L6 rotation** (deterministic, **offline, no LLM**): build candidates from `alternatives` + refreshed `interactives`, probe each via `browser.probe`, take the first resolving to **exactly 1**, with strategy prior:

   | | strategy | prior |
   |---|---|---|
   | L1 | testid | 0.95 |
   | L2 | role + name | 0.90 |
   | L3 | aria-label | 0.88 |
   | L4 | text + role | 0.80 |
   | L5 | scoped css | 0.65 |
   | L6 | xpath | 0.45 |
5. **LLM re-grounding** (optional, Sonnet 4.6; `--heal-llm`): only if L1–L6 fail; falls back gracefully without a key. Discount ×0.90.
6. **Verify-before-accept**: `browser.probe` the chosen candidate against the LIVE DOM → must be exactly 1, else confidence 0.
7. **Confidence gate**: ≥0.85 auto-heal · 0.60–0.84 flagged (apply + mark review) · <0.60 → skip (non-interactive) / human gate.
8. **Post-heal verification**: re-execute the action with the healed locator; only then persist `status=active`.
9. **Audit** (append-only): one `healing_audit` row per attempt {run_id, step, semantic_id, strategy, original, healed, confidence, outcome, dom_hash}.
10. **Amortize**: persist `healed_locators` keyed by `dom_subtree_hash`; reused at step 3 next run.

## Interim store (`brain/store.py` → `state/locators.db`, SQLite)
Tables: `healed_locators(page_path, semantic_id, strategy, value, confidence, dom_subtree_hash, status, times_used, created_at)`, `healing_audit(...)` append-only. **Interim**: written directly by the brain; M2b moves all writes behind the Go `store-gateway` (gRPC), restoring ADR-007. State path is git-ignored.

## Minimal replay path
`agentctl run --replay --plan <plan.json> [--target <url>]` → loads frozen steps; for each:
navigate as-is; click via frozen `locator` → `browser.probe`; if count≠1 → **heal** (engine above) → retry with healed locator. Emits `heal-report.json` (per-step: ok | healed(strategy,confidence) | failed) + `healing_audit`. No M3 trust layer.

## Fixtures
`testdata/site-v2/` = copy of `testdata/site/` with **drift**: index `Get started` button renamed to **"Launch"** (keeps `data-testid="cta"`); page-c `Finish` renamed to **"Complete"** (keeps `data-testid="finish"`). So a plan frozen on `site/` has stale role+name locators that **heal via testid (L1)** on `site-v2/`.

## Acceptance gate (Given/When/Then)
- **GIVEN** a plan.json explored on `testdata/site/` (with `alternatives` incl. testid) and the drifted `testdata/site-v2/`,
- **WHEN** `agentctl run --replay --plan <plan.json> --target file://.../site-v2/index.html`,
- **THEN** ≥1 click locator that broke by name is **healed via testid (L1)** with confidence ≥0.85 and auto-applied; `heal-report.json` records the heal; `healing_audit` has the row; replay completes; the heal result is deterministic across runs; a second replay reuses the cached healed locator (0 fresh rotation).

## Out of scope (later)
Go store-gateway + gRPC + proto (M2b) · MCP-SDK transport (GAP-VERIFY-002) · golden baselines, plan_hash hard-abort, exit codes, flake quarantine (M3) · subtree-scoped dom_hash, set-of-marks visual heal (M3/M5).
