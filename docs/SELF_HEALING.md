# Self-Healing — Sentinel

> Derived from the design synthesis 2026-06-23; canonical summary in ../ARCHITECTURE.md.

**Scope:** This document describes the complete self-healing pipeline executed inside the `heal` node
via the `healing-engine` Python module. The entry contract is a `HealingContext` struct:
`{semantic_id, failure_type, attempted_locator, element_description}`. The pipeline is bounded,
calibrated, and verify-before-trust.

**Browser interface note (BUILD_ONLY_DELTA):** All MCP tool calls referenced below
(`accessibility_snapshot`, locator resolution/probe, screenshot) are issued to **`pw-executor`** —
our own TypeScript Playwright execution server that we BUILD, implementing an MCP/JSON-RPC-2.0
stdio interface. The MCP-over-stdio transport is identical to any other MCP server; `pw-executor`
is a bespoke implementation, not an off-the-shelf product.

---

## Step 1 — Failure Classification

Catch the MCP error returned from the `act` node and classify it into one of four categories before
any healing attempt begins:

| Class | Meaning | Action |
|---|---|---|
| `LOCATOR_STALE` | Element present in a11y tree but selector no longer matches | Proceed through the healing pipeline |
| `ELEMENT_GONE` | Element absent from tree — removed, conditional, or A/B variant | Proceed through the healing pipeline |
| `TIMING` | Element present but not interactable at call time | Retry `act` once with a short wait **before** escalating to healing |
| `UNEXPECTED_ERROR` | Navigation / network / JS error | Do NOT heal — emit event, route directly to `report` |

Only `LOCATOR_STALE` and `ELEMENT_GONE` enter the full healing pipeline. `TIMING` gets one
cheap retry first. `UNEXPECTED_ERROR` is never a healing candidate.

---

## Step 2 — Perception Refresh

Do not reuse the stale snapshot from the previous cycle.

1. Call `pw-executor` → `accessibility_snapshot()` fresh.
2. Recompute `completeness_ratio` = named interactive elements / total interactive elements.
3. If `completeness_ratio < 0.30` (canvas, shadow DOM, custom elements, or cross-origin iframe),
   **also** capture a screenshot via `pw-executor` → `screenshot()` for the gated visual attempt
   in Step 6.
4. Recompute the **subtree-scoped** `dom_hash` for the scenario's target container only (not the
   whole page — see Risk note in ../ARCHITECTURE.md §Risks).

The fresh snapshot prevents healing against DOM state that may have already changed again between
the failure and the heal cycle.

---

## Step 3 — Cache Lookup (zero LLM)

Query `store-gateway` → `ReadLocators(page_url)` for `(semantic_id)` where
`status IN {human_verified, active}`.

**Cache hit:** if a record exists **and** its stored `dom_subtree_hash` matches the current subtree
hash → **reuse immediately**. This is the amortization payoff: the LLM is paid once; the healed
locator is reused across all replay runs until structural drift is detected.

**Cache miss / hash mismatch:** evict the stale record (mark `deprecated`) and continue to
Step 4. This auto-eviction prevents the silent propagation of an outdated locator.

No LLM tokens are consumed in this step.

---

## Step 4 — Strategy Rotation L1–L6 (zero LLM, deterministic)

For the failed intent, build candidate locators and **probe** each against the live DOM via
`pw-executor` MCP locator resolution. Take the **first candidate that resolves to exactly one
element**.

| # | Strategy | Selector form | Base prior |
|---|---|---|---|
| L1 | `data-testid` / `data-cy` / `data-pw` attribute | `[data-testid="…"]` | **0.95** |
| L2 | ARIA role + accessible name | `role=button[name="Submit"]` | **0.90** |
| L3 | `aria-label` exact match | `[aria-label="…"]` | **0.88** |
| L4 | Visible text content + role | `text=Sign in >> role=link` | **0.80** |
| L5 | Scoped CSS — semantic container + element type | `.form-login input[type=email]` | **0.65** |
| L6 | XPath positional | `//table/tbody/tr[2]/td[1]` | **0.45** |

A successful match at **L5 or L6** emits a `strategy_degradation` metric — a signal that the AUT
has unstable DOM structure and warrants attention from the development team.

If any level L1–L6 yields a unique match, the confidence value from the table above is carried
forward to Step 7 (Verify-Before-Accept). No LLM tokens are consumed.

---

## Step 5 — LLM Re-Grounding (Sonnet 4.6, structured output)

Invoked **only if Steps 3–4 both fail** to produce a unique live match.

**Budget pre-check:** verify remaining Sonnet token budget before calling the model. If budget
is exhausted, skip directly to Step 8 confidence gate at confidence = 0.

**Prompt inputs:**
- Original intent and `element_description`
- `attempted_locator` (the one that failed)
- The failed-strategy table from Step 4 (which levels were tried and why they missed)
- Current a11y tree, truncated to budget, target subtree first

**Model output** (structured JSON):
```json
{
  "strategy": "aria_role_name | css | xpath | …",
  "value": "<the candidate selector string>",
  "confidence": 0.00,
  "reasoning": "…"
}
```

**Discount applied:** `final_confidence = model_confidence × 0.90`
(LLM-overconfidence discount — models systematically over-report selector certainty).

The discounted confidence is passed to Step 7.

---

## Step 6 — Visual Set-of-Marks (Sonnet 4.6 vision) — GATED

This step is only reached and only executed when **all three gates pass**:

1. `completeness_ratio < 0.30` (a11y tree is sparse — canvas, shadow DOM, custom components)
2. Step 5 (LLM re-grounding) failed to produce a valid candidate
3. The M5 PoC has been validated with **> 70% accuracy** on at least 20 real broken-selector
   scenarios (gate is off by default until M5 delivers the measurement)

**Mechanism:**
- Numbered overlay marks are rendered on the screenshot captured in Step 2.
- A `mark → DOM-element` map is built (mark numbers to semantic nodes in the a11y tree).
- Sonnet 4.6 vision receives the annotated screenshot and returns a `mark_number`.
- We extract a **real semantic locator** from the mapped DOM node — **not** a coordinate click.
  Coordinate clicks are fragile to viewport size, device-pixel-ratio, and scroll position.

**Discount applied:** `final_confidence = model_confidence × 0.85`
(visual modality discount — pixel rendering adds variance beyond text reasoning).

---

## Step 7 — Verify-Before-Accept (live DOM re-probe)

**Every candidate produced by Steps 5 or 6** is re-probed against the **live DOM** via
`pw-executor` locator resolution before any confidence value is trusted.

- If the candidate **does not resolve to exactly one element**: `confidence = 0` (zeroed, not
  discounted — it is simply wrong).
- If the candidate resolves to exactly one element: the discounted confidence from Step 5 or 6
  is confirmed.

```
final_confidence = max(confidence values that passed the live-probe check)
```

Candidates from Step 4 (L1–L6) are already probed live during the rotation — they do not require
a second probe here. Only LLM and visual candidates (Steps 5–6) go through this gate.

This step closes the gap between "model says this locator works" and "locator actually works right
now."

---

## Step 8 — Confidence Gate (calibrated, not magic)

The `final_confidence` computed in Step 7 is evaluated against three tiers. The thresholds are
not hard-coded constants — they are recalibrated by `agentctl calibrate` against past
`human_verified` outcomes.

| Confidence band | Decision | Behaviour |
|---|---|---|
| **≥ 0.85** | **AUTO-HEAL** | Run one post-heal verification: re-execute the action with the healed locator. On success, persist `HealedLocator(status=active)` keyed to `(page_url, semantic_id, dom_subtree_hash)`, update `RunState` and the in-memory plan, continue. On failure, demote to HUMAN GATE. |
| **0.60 – 0.84** | **FLAGGED** | Apply optimistically; set `healing_flagged=true`; persist with `review_required=true`. Surfaces in the run report's healing-audit section. Does **not** block execution. |
| **< 0.60** | **HUMAN GATE** | Do not persist the locator. Emit `NEEDS_HUMAN_REVIEW`. CI mode: skip the step and record `SKIPPED_HEALING_FAILURE`, then continue. Interactive mode: async `checkpoint` pause until `agentctl gate approve/skip/abort` resolves (auto-skip after configurable timeout, default 30 min). |

**Calibration note — cold-start (0.90 default):** The 0.85 auto-accept threshold is only valid
once enough labeled outcomes exist. Until a sufficient number of `human_verified` records have
accumulated (calibration bootstrap), the default auto-accept threshold is raised to **0.90** to
reduce the risk of seeding the locator store with confidently-wrong healed selectors. The
threshold lowers toward 0.85 as `agentctl calibrate` produces a reliable precision/recall signal.

---

## Step 9 — Audit (append-only)

Every healing attempt — regardless of outcome — writes one row to `healing_audit`. The table is
**append-only**: no `UPDATE` or `DELETE` is ever issued against it. This makes the audit a
forensic-grade record and the ground truth for calibration.

**Row schema:**

| Column | Type | Description |
|---|---|---|
| `run_id` | uuid | The run that triggered this heal attempt |
| `step` | int | Step index within the plan |
| `semantic_id` | str | The element's semantic identifier |
| `original_selector` | str | The selector that failed |
| `strategy_used` | enum | `L1`–`L6` \| `llm_a11y` \| `llm_visual` \| `cache` |
| `healed_selector` | str | The candidate selector (may be `null` on total failure) |
| `confidence` | float | Final confidence after discounts and live-probe |
| `outcome` | enum | `auto_healed` \| `flagged` \| `human_gate` \| `failed` |
| `llm_tokens` | int | Tokens consumed by Steps 5–6 (0 for cache/L1–L6 paths) |
| `duration_ms` | int | Wall-clock time for the full heal cycle |
| `dom_hash_before` | str | Subtree hash at the time of failure |
| `dom_hash_after` | str | Subtree hash after fresh perception (Step 2) |
| `timestamp` | datetime | UTC timestamp of the attempt |

The `healing_audit` table is emitted as a `healing-audit.jsonl` CI artifact and as OpenTelemetry
span attributes (`selector` and `confidence` fields only — **never** prompt content, to avoid
leaking any AUT credentials that may appear in the a11y tree).

---

## Step 10 — Bounded Retry and Quarantine

### Attempt cap

`heal_attempts` is hard-capped per step:

- **3 attempts** in explore mode (allows broader strategy search during plan authoring)
- **2 attempts** in replay / CI hot path (bounds latency variance; replay correctness matters more
  than thoroughness)

On reaching the cap without an accepted locator, the step is escalated rather than retried further.

### AUT-SHA-gated flake quarantine

A step is placed in quarantine only when it satisfies the gating condition:

> The step fails **N-of-5** recent runs **without** an AUT `git-SHA` change between those runs.

This separates two distinct failure modes:
- **Real regression:** locator failure correlates with an AUT code change → surfaces as exit 1/2,
  not quarantined, must be addressed.
- **Environmental flake:** repeated failure with no AUT change → quarantined, non-blocking.

**Quarantine behaviour:**
- Quarantined steps still execute in every run.
- They do **not** contribute to exit code 1 or 2.
- They are visible in the run report under a dedicated quarantine section.
- **Cleared by:** `agentctl locators clear-quarantine <step>` or 3 consecutive passes on the same
  AUT SHA.

This rule is sourced from P3 (TrustFirst) as the cleanest mechanism to separate genuine regression
signal from environmental noise without suppressing real failures.
