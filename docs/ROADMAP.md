# Sentinel — MVP Roadmap (M0–M5)

Derived from the design synthesis 2026-06-23; canonical summary in ../ARCHITECTURE.md.

---

## Critical Path: `pw-executor` (GAP-ARCH-001)

The critical path across all six milestones is now **`pw-executor`** — our own TypeScript
Playwright execution server that implements the MCP/JSON-RPC-2.0 stdio interface. Every
milestone that spawns a browser subprocess depends on an incrementally delivered
`pw-executor`. This server is not an off-the-shelf product; it is built and version-pinned
by the Sentinel team. The initial surface (M0) is minimal: `navigate`,
`accessibility_snapshot`, and `trace`. The visual set-of-marks overlay capability is added
in M5, gated by the PoC accuracy threshold. No milestone may be considered complete until
the `pw-executor` surface required by that milestone is implemented and covered by a
contract test asserting tool names and input schemas. **GAP-ARCH-001** tracks this
dependency; treat any regression in `pw-executor` as a blocker for the affected milestone.

---

## M0 — Hello Browser (Days 1–3)

**Languages:** Go, Python, TypeScript

**Deliverable:**

`agentctl run` spawns the Python brain via subprocess and environment variables — no gRPC
yet. The brain consists of a single `perceive` node. On startup the brain spawns
`pw-executor`, our TypeScript Playwright execution server implementing MCP/JSON-RPC-2.0
over stdio, with the minimal surface: `navigate`, `accessibility_snapshot`, and `trace`.
The brain calls `accessibility_snapshot()`, prints the a11y tree to stdout, and drops a
`trace.zip` into `ARTIFACT_DIR`. Goal: prove the end-to-end wire across all three runtime
layers (Go → Python → TypeScript over stdio). No LLM call, no state machine, no
persistence. Intelligence comes later.

**Acceptance Criterion:**

> **Given** `pw-executor` is built and running (npm build succeeds; MCP handshake completes over stdio)
>
> **When** `agentctl run --explore --target <URL>` is invoked against any live web page
>
> **Then** the a11y tree JSON is printed to stdout with at least one interactive element
> listed, **and** a `trace.zip` file is present in `ARTIFACT_DIR` and is openable by
> `playwright show-trace` without error — both within 30 seconds of invocation.

---

## M1 — Autonomous Walk (Days 4–10)

**Languages:** Python

**Deliverable:**

All 9 LangGraph nodes are implemented (`perceive`, `ground`, `plan`, `act`, `verify`,
`heal` — stubbed, `checkpoint`, `report`, plus `START`/`END`). The LangGraph
`SqliteSaver` checkpointer writes to a **separate** DB file from the store-gateway DB
(this is what makes the single-writer claim true). The `plan` node uses Opus 4.8 at
`temperature=0`. Exploration terminates on a **measurable coverage target**
(`coverage_target` + `nav_frontier` emptiness) — not on an LLM-asserted flag. The run
produces `plan.json` (with `plan_hash`), `llm-transcript.jsonl`, and `trace.zip`.

**Acceptance Criterion:**

> **Given** a real multi-page web application is accessible at `TARGET_URL` with at least
> 3 distinct pages reachable from the landing page
>
> **When** `agentctl run --explore` completes (or terminates due to budget)
>
> **Then** `plan.json` exists in `ARTIFACT_DIR`, contains >= 5 distinct `PlannedAction`
> entries with non-empty `locator` fields, **and** `coverage_achieved` is recorded as a
> float in `[0.0, 1.0]` in the plan file — all verifiable by `jq '.coverage_achieved,
> (.steps | length)' plan.json`.

---

## M2 — Self-Repairing Walker (Days 11–20)

**Languages:** Go, Python

**Deliverable:**

The `heal` node is fully implemented via `healing-engine`: cache lookup → L1–L6
deterministic strategy rotation → Sonnet 4.6 a11y re-grounding (structured output) →
`verify-before-accept` live-DOM probe → confidence gate (>= 0.85 auto-heal, 0.60–0.84
flagged, < 0.60 human gate) → post-heal verification (re-run action with healed locator
before persisting) → append-only `healing_audit` write → `dom_subtree_hash` amortization
with automatic stale eviction.

The gRPC boundary is introduced at this milestone: `proto v1` (`PersistenceService`) is
defined and stubs are generated for Go and Python in CI. The Go `store-gateway` is
implemented (SQLite WAL: `runs`, `healed_locators`, `healing_audit` tables).

**Acceptance Criterion:**

> **Given** a plan.json produced by M1 has one selector manually changed to an invalid
> value, **and** the brain's locator cache for that selector is empty
>
> **When** `agentctl run --replay` is executed once (first run), then executed again with
> the same broken selector and the same AUT (second run)
>
> **Then** the first run heals the broken selector with a recorded confidence >= 0.85,
> persists a `HealedLocator` row with `status=active` in the store-gateway DB, and exits
> 0; **and** the second run's `healing_audit` log shows zero LLM tokens consumed for
> that semantic_id (cache hit, amortized reuse verified by `jq '.llm_tokens' healing-audit.jsonl`).

---

## M3 — CI-Ready Replay (Days 21–30)

**Languages:** Go, Python

**Deliverable:**

`replay` and `ci` run modes are implemented: the `plan` node is skipped entirely, and
`ground` routes directly to `act` using frozen locators — zero LLM on the happy path.
**`plan_hash` hard-abort** (exit code 3) is enforced at replay start. Dual `a11y_hash` +
`screenshot_hash` golden baselines are validated per milestone step. AUT-SHA-gated flake
quarantine is implemented (`step_failures` table; a step counts toward flake only if it
fails N-of-5 without an AUT git-SHA change). Structured exit codes `0/1/2/3` are emitted.
The orchestrator is extracted as a proper gRPC server (`RunControl`, subprocess
supervision, per-step deadline enforcement). Per-job SQLite is used for CI
(`AGENT_DB_PATH=/tmp/agent-{run_id}.db`). A GitHub Actions workflow is shipped
(conditional explore job + parallel replay matrix).

**Acceptance Criterion:**

> **Given** a valid `plan.json` is committed to the repository (hash verified), **and** a
> second copy of that file has one step's `locator` field manually altered
>
> **When** three parallel CI replay jobs run against the committed `plan.json` (`--ci`
> mode), **and** one additional replay runs against the hand-edited copy
>
> **Then** all three parallel replays complete in under 2 minutes each (wall clock) and
> exit 0; **and** the replay against the hand-edited file exits 3 within 5 seconds of
> plan load, with the stored and computed hashes both logged to stderr — measurable by CI
> job timing and exit code assertions in the GitHub Actions workflow.

---

## M4 — Production-Observable v1.0 (Days 31–45)

**Languages:** Go, Python

**Deliverable:**

`report-service` is implemented (Go): emits `run_report.json` + `run_report.html`
(mirroring Playwright HTML reporter structure), exposes Prometheus `/metrics`, and
generates the exported `.spec.ts` from `RunState.executed_actions` via a Go template
— with no dependency on a pw-executor codegen tool.

OTel spans are added across all three runtime layers: every LangGraph node, every
pw-executor MCP call, and every Go gRPC call. `prompt_HASH` is attached to LLM spans;
prompt content is never stored. Export: OTLP → Grafana Alloy → Tempo.

`agentctl calibrate` is implemented, running `healing_confidence_histogram` precision/recall
computation against `human_verified` outcomes. Go-side hard budget ceiling reconciliation
is activated. The `plan` node switches to Opus 4.8 in full (not gated or stubbed).

**Acceptance Criterion:**

> **Given** a 10-page AUT (>= 10 distinct URLs reachable) is explored or replayed to
> completion
>
> **When** the run finishes within the configured token budget
>
> **Then** `run_report.html` is present, non-empty, and renders without errors in a
> browser; the exported `.spec.ts` passes `tsc --noEmit` without type errors; `trace.zip`
> is viewable via `playwright show-trace` without error; **and** `agent_cost_usd_total`
> appears in the `/metrics` scrape output with a value greater than zero — all four
> conditions verified in a single CI job.

---

## M5 — Visual Heal PoC + K3s/ArgoCD (Days 46–60+)

**Languages:** Go, Python, TypeScript (PoC-gated)

**Deliverable:**

The set-of-marks visual heal path (healing strategy attempt 3) is **built into
`pw-executor`** and activated only if the PoC achieves > 70% accuracy on 20 real
broken-selector scenarios. The overlay capability is added to `pw-executor` as an
additional MCP tool on the same stdio channel, providing numbered mark overlays mapped to
DOM elements; the LLM returns a mark number, and `healing-engine` extracts a real semantic
locator from the mapped node — no coordinate clicks. If the PoC threshold is not met, the
feature remains deferred. There is no "if the official server lacks it" escape hatch:
`pw-executor` is our server and we build whatever surface we need.

Postgres + `AsyncPostgresSaver` are introduced **only** if the documented concurrency
trigger is reached (> 50 concurrent shared-DB writers or distributed workers); otherwise
SQLite WAL continues. A Helm chart and ArgoCD Application manifest are shipped for
home-lab GitOps deployment, with per-namespace config for `dev` / `staging` / `prod`
targets.

**Acceptance Criterion:**

> **Given** a labeled benchmark of exactly 20 real broken-selector scenarios is prepared,
> each with a human-verified correct locator as ground truth
>
> **When** `pw-executor`'s set-of-marks overlay tool is exercised by `healing-engine` on
> all 20 scenarios (no L1–L6 or LLM a11y fallback — visual path only), with
> `verify-before-accept` applied to every candidate
>
> **Then** at least 15 of the 20 scenarios produce a healed locator that matches the
> human-verified selector (>= 75% accuracy exceeds the 70% gate) — measured by automated
> comparison and logged to `healing-audit.jsonl`; if fewer than 15 scenarios pass, the
> set-of-marks feature is recorded as deferred in `ARCHITECTURE.md` and the `pw-executor`
> overlay tool is removed from the shipped binary until a subsequent PoC cycle.
