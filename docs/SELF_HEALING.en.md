# Self-Healing — Sentinel

> 🌐 [Русский](SELF_HEALING.md) (основная версия) · **English**

> Derived from the design synthesis 2026-06-23; canonical summary in ../ARCHITECTURE.md.

**Scope:** This document describes the complete self-healing pipeline (`HealingEngine.heal`,
`brain/healing.py`). It runs **not** in the graph `heal` node (that is a stub in the explore graph)
but in the standalone `run_replay()` loop (`brain/replay.py`), which calls `heal.heal(ctx)` on a live
locator failure. The `ctx` input is a plain dict (`brain/replay.py:207-210`):
`{step, semantic_id, page_path, intent, attempted_locator, alternatives, dom_hash, interactives}`.
The pipeline is bounded, calibrated, and verify-before-trust.

**Browser interface note (BUILD_ONLY_DELTA):** All MCP tool calls referenced below
(`accessibility_snapshot`, locator resolution/probe, screenshot) are issued to **`pw-executor`** —
our own TypeScript Playwright execution server that we BUILD, implementing an MCP/JSON-RPC-2.0
stdio interface. The MCP-over-stdio transport is identical to any other MCP server; `pw-executor`
is a bespoke implementation, not an off-the-shelf product.

---

## Step 1 — Heal Trigger

There is **no** four-class failure classifier (`LOCATOR_STALE`/`ELEMENT_GONE`/`TIMING`/`UNEXPECTED_ERROR`)
in the code. The real trigger is a single check in `run_replay()` (`brain/replay.py:194-198`): for a
locator-bearing step, `browser.probe` is run on the frozen primary locator, and if it does **not**
resolve to exactly 1 element (`count != 1`), `heal.heal(ctx)` is invoked. Non-locator steps
(navigation, etc.) are not healing candidates.

---

## Step 2 — Perception Refresh

Do not reuse the stale snapshot from the previous cycle.

1. Call `pw-executor` → `accessibility_snapshot()` fresh.
2. Take the page's current interactive elements (`current_elements`) to feed the reground.
3. When needed, capture a screenshot via `pw-executor` → `screenshot()` for the gated visual attempt
   in Step 6.
4. Recompute `dom_hash = _a11y_hash(ariaSnapshot)[:16]` — a hash of the **whole page** a11y snapshot
   (`brain/replay.py:210`; the same value is the cache key `dom_subtree_hash`). There is no
   `completeness_ratio` in the code.

The fresh snapshot prevents healing against DOM state that may have already changed again between
the failure and the heal cycle.

---

## Step 3 — Cache Lookup (zero LLM)

Query `store-gateway` → `Lookup(LocatorKey{page_path, semantic_id, dom_subtree_hash})` where
`status = active`.

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

## Step 5 — LLM Re-Grounding (default heal model Sonnet 4.6, structured output)

> **M6 (ADR-019):** "Sonnet 4.6" here and below is the **default heal model**, routed through
> the `LLMBackend` (`brain/llm.py`). Any OpenAI-compatible provider is wired via the `LLM_*_HEAL`
> variables (`LLM_BACKEND_HEAL`, `LLM_BASE_URL_HEAL`, `LLM_MODEL_HEAL`, …). The LLM path is
> best-effort; with no key, heal falls back to the deterministic L1–L6 rotation.

Invoked **only if Steps 3–4 both fail** to produce a unique live match.

**Budget pre-check:** verify remaining heal-model token budget (Sonnet by default) before calling the model. If budget
is exhausted, skip directly to Step 8 confidence gate at confidence = 0.

**Prompt inputs** (`brain/healing.py::_llm_reground` — exactly three fields):
- `step intent` — the step's intent
- `element the step used to use` — the failed `attempted_locator`
- `elements on the page now` — the page's current interactive elements, **each with its index**

(the prompt sends exactly these three; there is no failed-strategy table and no `element_description`)

**Model output** (structured JSON, schema `_SCHEMA_PICK` — an INDEX into the element list, with no
self-reported `confidence` / `reasoning` / `strategy`):
```json
{"index": 2}
```
or, if no element could plausibly serve the step's purpose:
```json
{"none": true}
```

Since **ADR-082** the model chooses among elements the executor actually reported instead of
authoring a CSS selector — the grounding rule the planner has always followed (ADR-022/027,
`candidates[idx]`). An out-of-range index is discarded, so no reply can produce a locator for an
element nobody observed.

Reach is unchanged in practice. An explore-authored plan cannot contain an element outside the
executor's perception, because the site map is built from that same `browser.interactives`. A
RECORDED plan can (`record_bridge.py` infers the strategy from whatever key the recorder observed,
including `css`/`xpath`) — but the old tier could not repair such an element either: it scored 0.585
against FLAG 0.60 and landed in `needs_review`, so the step was never applied. What narrowed is
unreachable reach, not reachable reach. The chosen element's own descriptor becomes the locator (`testid`, else
`role`+`name`) and is what Step 7's identity check compares against the frozen locator.

The prompt asks which element now serves the SAME PURPOSE, not which one "matches the intent".
That wording is load-bearing and a live run proved it: asked the latter, both qwen3:14b and
qwen2.5vl:7b answered `{"none": true}` for every rename — correct in a literal reading, since no
element carries that name any more, which is exactly why the step failed. Proposing the candidate is
the tier's job; deciding whether it is really the same element is the identity check's.

**Discount applied:** the model does not report its own confidence — the prior of the locator the
pick produced is discounted by a FIXED factor instead:
`final_confidence = PRIORS[testid|role_name] × 0.90` (0.855 or 0.81).

**Practical consequence:** both values clear FLAGGED (0.60) and are capped below AUTO by ADR-080, so
a text re-ground is applied OPTIMISTICALLY and reported. Before ADR-082 the tier scored
`PRIORS["css"] × 0.90 = 0.585`, below FLAGGED, and therefore healed nothing ever — while still
suppressing the visual tier below, which `heal` reaches only `if not chosen`.

The discounted confidence is passed to Step 7.

---

## Step 6 — Visual Set-of-Marks (default heal vision model Sonnet 4.6) — GATED

This step is only reached and only executed when **all three gates pass**:

1. `completeness_ratio < 0.30` (a11y tree is sparse — canvas, shadow DOM, custom components)
2. Step 5 (LLM re-grounding) failed to produce a valid candidate
3. The M5 PoC has been validated with **> 70% accuracy** on at least 20 real broken-selector
   scenarios (gate is off by default until M5 delivers the measurement)

> **M6 vision-gating (ADR-019, see M6_CONTRACT.md §Vision-gating):** this step (Tier-7 set-of-marks)
> additionally requires a **vision-capable backend** — the effective gate is `use_visual AND backend.supports_vision`.
> A text-only provider (e.g. DeepSeek-V3) **skips Tier-7** and degrades to the deterministic
> L1–L6 rotation.

**Mechanism:**
- Numbered overlay marks are rendered on the screenshot captured in Step 2.
- A `mark → DOM-element` map is built (mark numbers to semantic nodes in the a11y tree).
- The default heal vision model (Sonnet 4.6) receives the annotated screenshot and returns JSON
  `{"mark": <int>}` (the chosen mark number) or `{"none": true}` — with no self-reported
  confidence.
- We extract a **real semantic locator** from the mapped DOM node — **not** a coordinate click.
  Coordinate clicks are fragile to viewport size, device-pixel-ratio, and scroll position.

**Confidence:** the model does not report a confidence value — a FIXED value is used instead, the
`visual` strategy's base prior: `final_confidence = PRIORS["visual"] = 0.80` (no discount applied).

**Practical consequence:** 0.80 satisfies the FLAGGED threshold (0.60) but never reaches the
AUTO-HEAL threshold (0.85), so visual re-grounding — when the candidate passes Step 7 — **always**
lands in `flagged`. It can never fully auto-heal.

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

### Identity (ADR-082) — the second half of the same step

A cardinality probe establishes that ONE element matched. It does not establish that it is the SAME
element, and until ADR-082 nothing did: a re-ground could bind a different control and the run
reported an ordinary heal.

For a **re-ground** (a strategy the plan did not freeze — see Step 8) the live element's descriptor
is compared with what the frozen locator already carries:

| Outcome | Meaning | Effect |
|---|---|---|
| `verified` | live `role` and normalised `name` equal the frozen ones | applied; no degradation reported |
| `contradicted` | they differ — the control was renamed or is a different one | **applied anyway**, and reported as a degradation (`heal.identity_contradicted`) |
| `unverifiable` | the plan froze no name (a testid-only primary), or the tier reported nothing | applied; reported as `heal.identity_unverifiable` |

It is a **predicate, not a score**: there is no threshold, which is deliberate — `PRIORS` are
unmeasured by this file's own admission (GAP-RISK-002), and a similarity threshold would be a second
uncalibrated number beside the first.

The comparison is **stricter than the probe that produced the candidate**: `buildLocator` passes
`{name}` to `getByRole` without `exact`, so Playwright matches case-insensitively and by substring —
`"Pay"` is satisfied by `"Pay now"`. Equality is not.

A **re-bind** carries no identity annotation at all: the plan vouches for a frozen key, and
inventing a doubt there would be as dishonest as hiding one on a re-ground.

**What this does not close:** two controls genuinely indistinguishable by role and name (two
"Add to cart" buttons in a list) pass the check even when the wrong one is bound. That is the
"same *instance*" question, and only surroundings could answer it — but the case is now countable
(`probe_count >= 2` reaches the heal context), which is the labelled signal a future mechanism would
need.

---

## Step 8 — Confidence Gate (calibrated, not magic)

The `final_confidence` computed in Step 7 is evaluated against three tiers. The thresholds are
FIXED module constants (`AUTO, FLAG = 0.85, 0.60` in `brain/healing.py`), used directly in the
gate (`conf >= AUTO`, `conf >= FLAG`); they are not recalibrated at runtime.

| Confidence band | Decision | Behaviour |
|---|---|---|
| **≥ 0.85** | **AUTO-HEAL** | Run one post-heal verification: re-execute the action with the healed locator. On success, persist `HealedLocator(status=active)` keyed to `(page_url, semantic_id, dom_subtree_hash)`, update `RunState` and the in-memory plan, continue. On post-heal failure the outcome is `needs_review`/`failed` (row below). |
| **0.60 – 0.84** | **FLAGGED** | Apply optimistically; set `healing_flagged=true`; persist with `review_required=true`. Surfaces in the run report's healing-audit section. Does **not** block execution. |
| **< 0.60** | **`needs_review` / `failed`** | The locator is **not** persisted. The outcome `needs_review` (a candidate was found but below threshold) or `failed` (no candidate) is recorded in the `healing_audit` table and surfaced in the heal-report. There is **no** per-heal `agentctl gate` CLI (it does not exist in the code); live human takeover is the separate `takeover`-node mechanism (`interrupt`/`Command(resume)`, ADR-054), validated at M9-LIVE. |

**Calibration reporting — `cold_start` (0.90 default):** `brain/calibrate.py::calibrate()` takes a
`cold_start=0.90` parameter, but this value is **reporting-only**: it is written into
`state/calibration.json` (the `RUN_MODE=calibrate` offline path, `_run_calibrate` in
`brain/__main__.py`) as a reference figure alongside the `confidence` histogram. `healing.py`
imports nothing from `calibrate.py`; the live gate always uses the FIXED 0.85/0.60 thresholds
above — there is no "threshold lowers toward 0.85" feedback loop in the code.

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
| `strategy_used` | enum | `testid` \| `role_name` \| `label` \| `text_role` \| `css` \| `xpath` \| `visual` \| `none` (see `PRIORS`, `healing.py:26-27`; `none` on total failure at Steps 4–6) |
| `healed_selector` | str | The candidate selector (may be `null` on total failure) |
| `confidence` | float | Final confidence after discounts and live-probe |
| `outcome` | enum | `cache_hit` \| `auto_healed` \| `flagged` \| `needs_review` \| `failed` |
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

### Single call — no attempt cap

As-built: there is no `heal_attempts` cap. The heal cycle has no retry counter — each step calls
`HealingEngine.heal()` at most once per action attempt:

- **Explore:** the `heal` node in the LangGraph graph (`brain/graph.py`) is a **stub**: it performs
  no real re-grounding at all, only increments the `consecutive_heal_failures` counter for the
  auto-HITL signal (M14, ADR-055) and hands control to `checkpoint`. No real healing happens in
  explore mode.
- **Replay / CI:** `brain/replay.py` calls `heal.heal(ctx)` **exactly once** on a failed step (no
  retry loop). The result is applied if `outcome` is `auto_healed`, `flagged`, or `cache_hit`;
  otherwise the step is marked `failed`.

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
