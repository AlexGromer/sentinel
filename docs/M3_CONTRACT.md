# M3 Contract — "CI-Ready Replay / Trust Layer" (frozen 2026-06-23)

Goal: make replay **trustworthy in CI**. A non-deterministic agent must be *structurally* unable
to run a tampered plan or silently rewrite its own baseline. Implements ADR-006 (+ ADR-013).

## Scope decision
**In M3 (deterministic, offline-testable):** `plan_hash` hard-abort; structured exit codes 0/1/2/3;
dual **golden baselines** (a11y-hash + screenshot-hash) with operator-only update + golden-diff
regression detection; **AUT-SHA-gated flake quarantine**; **GitHub Actions** CI.
**Deferred:** Go gRPC orchestrator + store-gateway (M2b); subtree-scoped dom_hash + set-of-marks (later).

## Structured exit codes (brain returns; agentctl propagates)
| Code | Meaning |
|------|---------|
| 0 | all non-quarantined steps passed, no golden regression |
| 1 | a step failed (locator unhealable / action error) on a non-quarantined step |
| 2 | golden-diff regression (a11y-hash or screenshot-hash differs from golden) on a non-quarantined page |
| 3 | **plan integrity** (plan_hash mismatch) or budget — hard-abort, highest priority, nothing executed |

## plan_hash HARD-ABORT (ADR-006)
At replay start: recompute `canonical_plan_hash(plan["steps"])`, compare to `plan["plan_hash"]`.
Mismatch → immediate abort, **exit 3**, log stored-vs-computed. A hand-edited / partially-healed
plan can never run silently. Default is hard-abort; `--force-replay` bypasses with a loud warning
and is **disallowed under `--ci`**.

## Golden baselines — dual hash, page-keyed (ADR-006)
- **page key** = basename of the normalized URL (`index.html`, `page-a.html`, …) so a plan explored on
  `site/` compares against `site-v2/`.
- **golden record** `{page_key, a11y_hash, screenshot_hash, created_at}` — IMMUTABLE except via the
  one explicit mutation path: `agentctl baseline update`.
- `agentctl baseline update --plan <p> [--target <url>]` → replays the plan, captures the current
  a11y-hash (`sha256(ariaSnapshot)`) + screenshot-hash per visited page, writes goldens (archiving
  the prior). **The CI replay never writes goldens** → "tests can't rewrite their own baseline".
- On replay (non-baseline): on first arrival at each page, compute current hashes; compare to the
  golden for that page_key; mismatch → regression. Captured in `heal-report.json`.
- **M3 refinement (implemented):** goldens are captured **once per page at first landing** — baseline
  and replay symmetric, so a later click can't shift the golden. **a11y-hash drives exit 2**
  (deterministic); **screenshot-hash regression is advisory** (reported, not exit-gating) until
  cross-process screenshot determinism is hardened — GAP-RISK-009.

## Heal + golden-diff coexist (ADR-013)
A healed step **still executes** (replay continues) AND the golden-diff **still runs** on the page.
So a name-drift that heals via testid *also* flags an a11y golden regression → exit 2. Healing =
test robustness; golden-diff = change detection. Both are reported per step.

## AUT-SHA-gated flake quarantine
- `--aut-version <sha>` (e.g. the app-under-test `git rev-parse HEAD`), recorded per run.
- store `step_failures(plan_id, step_key, last5 json, last_aut_sha, quarantined)`.
- a failure counts toward "flaky" ONLY if `aut_version` is unchanged vs the prior run (separates real
  regression from environmental flake). Quarantine when a step fails **≥3 of the last 5** runs without
  an AUT-SHA change.
- quarantined steps still execute but do NOT contribute to exit 1/2; cleared by 3 consecutive passes
  or `agentctl locators clear-quarantine`.

## pw-executor — new tool (M3)
`browser.screenshotHash` → `{ hash }`: `sha256(await page.screenshot())`, hashed in TS (no bytes over stdio).

## agentctl — new surface (M3)
- `agentctl baseline update --plan <p> [--target <url>]` — the only golden mutation path.
- `run --replay --plan <p> [--target <url>] --aut-version <sha> [--ci]` — `--ci` forbids `--force-replay`.
- `agentctl locators clear-quarantine` (M3 minimal: clears step_failures).
- store gains `golden_snapshots` + `step_failures` tables (interim, brain-local; → store-gateway @ M2b).

## GitHub Actions (`.github/workflows/ci.yml`)
`build` (Go + TS `npm ci`/build + `uv` deps + `playwright install`) → `replay` matrix (per-job SQLite);
the `explore` job is manual/`workflow_dispatch`. Asserts exit codes. Documented, not runtime-tested locally.

## Acceptance gate (Given/When/Then)
1. **GIVEN** a plan explored on `site/`, **WHEN** `agentctl baseline update --plan <p>`, **THEN** goldens exist for index/page-a/b/c.
2. **WHEN** `run --replay --plan <p> --target site/index.html --ci` (unchanged), **THEN** exit **0** (no regression, no heal).
3. **WHEN** `run --replay --plan <p> --target site-v2/index.html --ci` (drifted), **THEN** locators heal AND golden-diff flags a11y regression on index/page-c → exit **2**; `heal-report.json` shows both heal + regression.
4. **WHEN** replaying a hand-edited plan (one byte changed in a step), **THEN** exit **3** (hard-abort), nothing executed.
5. Deterministic: identical inputs → identical exit code + plan_hash verdict.

## Out of scope (later)
Go gRPC orchestrator/store-gateway (M2b) · MCP-SDK transport (GAP-VERIFY-002) · subtree-scoped dom_hash, set-of-marks visual heal, real OTel/Prometheus (M4/M5).
