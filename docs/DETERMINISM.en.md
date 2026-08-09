# Sentinel — Determinism, CI Contract, and Plan Integrity

> 🌐 [Русский](DETERMINISM.md) (основная версия) · **English**

Derived from the design synthesis 2026-06-23; canonical summary in ../ARCHITECTURE.md.

> **Type:** Explanation
> **Audience:** CI engineers, QA leads, operators
> **Last updated:** 2026-06-28
> **Related:** [MEMORY_PERSISTENCE.md](./MEMORY_PERSISTENCE.md), [../ARCHITECTURE.md](../ARCHITECTURE.md)

## Overview

Sentinel separates the non-deterministic, human-supervised **explore** phase from the
fully deterministic, LLM-free **replay** phase. This document defines every contract,
rule, and policy that makes that separation trustworthy: plan freezing, hash-abort,
golden baselines, the no-self-mutation rule, AUT version drift policy, seeded
exploration, structured exit codes, and CI parallelism strategy.

---

## Core Contract: Explore-Once / Replay-Many

CI **never** runs explore. Explore is a one-time (or operator-triggered) event.
Replay is the CI workhorse.

```
explore run (non-deterministic, human-supervised, one-time)
    │
    └─→ plan.json frozen, committed to app repo
            │
            └─→ replay run × N  (deterministic, LLM-free on the happy path)
                replay run × N
                replay run × N  ← CI executes these, never explore
```

The frozen `plan.json` is the only trustworthy reproducibility guarantee. LLM
providers do not contractually guarantee bit-identical output even at
`temperature=0` with a fixed seed (streaming tokenisation, model bumps). The
explore-once contract accepts that non-determinism and quarantines it to a single
human-reviewed event.

> **M6 (ADR-019):** LLM planning is **best-effort**. Switching the provider or model
> entirely (Anthropic ↔ any OpenAI-compatible, per-role via `LLM_BACKEND*`) is one more
> source of non-determinism: a different model → a different plan → a different `plan_hash`;
> and **model provenance is not stored**. The deterministic anchor remains `HeuristicPlanner`,
> and golden baselines stay **heuristic-only** (LLM-free). The model of that non-determinism
> is itself unchanged.

---

## Plan Freezing and the `plan.json` Schema

At the end of a successful explore run, the brain serialises the ordered
`PlannedAction` sequence — including resolved locators and their alternative healing
strategies — into `plan.json`. This file is committed to the application repository and
becomes the authoritative test definition. Golden hashes (`a11y_hash`/`screenshot_hash`)
are **not** part of `plan.json` — they live separately, in the SQL `golden_snapshots` table,
and are only written by the explicit `agentctl baseline update` command (see "Immutable
Golden Baselines" below).

### Schema

Real top-level keys (`brain/graph.py:401-416`):

```json
{
  "plan_id":               "<UUIDv4>",
  "plan_hash":              "<SHA-256 of canonical JSON over steps[]>",
  "target_url":             "https://app.local",
  "run_mode":               "explore",
  "coverage_target":        0.85,
  "coverage_achieved":      0.91,
  "interactive_seen":       42,
  "interactive_exercised":  38,
  "steps": [
    {
      "step_id":       1,
      "intent":        "click button 'Sign in'",
      "semantic_id":   "auth/sign-in-button",
      "action_type":   "click",
      "target":        null,
      "locator":       {"role": "button", "name": "Sign in"},
      "alternatives": [
        {"strategy": "testid",    "locator": {"testid": "sign-in-btn"}, "prior": 0.95},
        {"strategy": "role_name", "locator": {"role": "button", "name": "Sign in"}, "prior": 0.90},
        {"strategy": "label",     "locator": {"label": "Sign in"}, "prior": 0.88}
      ],
      "is_milestone":  false
    }
  ],
  "tokens": { "...": "budget.tracker().summary()" },
  "models": { "plan": "<planner model name>" }
}
```

Every `steps[]` object has **exactly** these 8 keys (`brain/graph.py:237-241`): `step_id`,
`intent`, `semantic_id`, `action_type`, `target`, `locator`, `alternatives`, `is_milestone`.
`alternatives` is a flat list of healing strategies `{strategy, locator, prior}`
(`brain/graph.py:90-98`), not an `L1..L6` map. The fields `aut_version`, `exploration_seed`,
`value`, `expected_outcome`, `assertion`, `is_critical`, `healed` do not exist in the real
schema. `golden_snapshots` is not embedded in `plan.json` either — see the note above the
heading.

**Hash canonicalisation:** `plan_hash` is SHA-256 of the compact JSON serialisation of the **entire**
`steps[]` array (`json.dumps(sort_keys=True, separators=(",",":"), ensure_ascii=False)`): object keys
are sorted lexicographically (key order is irrelevant), **no field is excluded**, and numbers are
serialised as-is — with **no** additional rounding. It is computed only in the Python brain, so the
float representation is deterministic within the interpreter. Computed at freeze time and re-computed
at replay start; any field change → mismatch → exit 3.

---

## Plan-Hash Hard-Abort

At the start of every replay or CI run, before any browser action, the brain
re-computes the hash of the loaded `steps[]` array and compares it to the stored
`plan_hash`.

**Match:** proceed normally.

**Mismatch:** immediate abort — exit code **3** — with both the stored hash and
the computed hash written to stderr and the run log. A hand-edited, partially
healed, or accidentally merged plan can never run silently in replay mode.

```
agentctl run --replay --plan plan.json --target https://app.local --ci --aut-version $(git rev-parse HEAD)

[sentinel] Loading plan.json — plan_id=3f7a...
[sentinel] Stored plan_hash:   sha256:aef9c2...
[sentinel] Computed plan_hash: sha256:be01d7...
[sentinel] HASH MISMATCH — HARD ABORT
[sentinel] exit 3
```

**Bypass (interactive only):** `--force-replay` exists as an escape hatch for
debugging. Using it emits a loud warning to stderr and stdout, records the
override in the run transcript, and is **disallowed in CI mode** (the orchestrator
rejects it with exit 3).

---

## LLM-Free Replay Happy Path

Replay and CI mode do **not** run the LangGraph explore graph at all — they run a
separate standalone loop, `run_replay()` (`brain/replay.py`, dispatched from
`brain/__main__.py`). There is no `plan` node and no LLM planning; for each frozen
step the loop resolves the locator (cache / frozen `locator`), acts, and verifies,
invoking `HealingEngine.heal()` only on a live locator failure.

```
run_replay(): for each frozen step → resolve locator → act → verify
                                    └── HealingEngine.heal (only on live locator failure)
```

Consequences:
- **Zero planning tokens** consumed per CI run.
- **Deterministic timing**: no LLM inference latency on the critical path.
- **The only LLM call in replay** is a healing cycle, triggered solely when a live
  locator probe fails. Healing is hard-capped at **2 attempts per step** plus a
  per-step gRPC deadline and auto-skip, so a heal-storm on a churning AUT cannot
  blow up CI runtime or cost.

---

## Immutable Golden Baselines

**As-built:** these two hashes are **not** recorded by a plain explore run. They are only
written by the explicit `agentctl baseline update` command (`RUN_MODE=baseline` →
`brain/replay.py:253-266`, where `save_golden()` fires only `if baseline:`) — and not "per
milestone step," but once per page, at first landing. Plain replay/CI **reads and diffs**
against these baselines, but never writes them.

| Hash | What it covers | Regression type caught |
|------|---------------|----------------------|
| `a11y_hash` | SHA-256 of the normalised accessibility tree at first landing on a page | Structural DOM change — new/removed elements, role/label drift |
| `screenshot_hash` | Perceptual hash of the screenshot at first landing on a page | Visual-only regression — CSS layout, colour, hidden elements |

> **Hash stability (M9.6 / ADR-037):** `screenshot_hash` is byte-stable **in headless Chromium only** — the mode in which golden replay is validated. **headed** and **CDP-attach** (and non-Chromium engines, deferred in GAP-OPS-001) are observation modes: a different render path / the user's viewport make bytes non-reproducible, so golden replay is not run there.

Golden baselines are **never auto-updated by a CI run**. The only mutation path is
an explicit operator command:

```bash
agentctl baseline update --plan plan.json [--target <URL>]
```

This command:
1. Runs a full replay against the live AUT.
2. On first landing on each page, overwrites that page's row in the SQL
   `golden_snapshots` table (`INSERT OR REPLACE`) with the current `a11y_hash`/`screenshot_hash`.

**As-built:** `plan_hash` is not recomputed and `plan.json` is not touched — there is no
versioning or archiving of the old record via a `superseded_by` reference in the code; the
"new" golden simply replaces the old SQL row. This design still makes "the tests rewrote
their own baseline" structurally impossible: writing `golden_snapshots` is only reachable
through this separate, explicitly-invoked operator path, never from inside a plain CI replay.

Dual hashing catches visual-only (CSS/layout) regressions that pure a11y diffing
is blind to. By default a visual regression is **advisory** (`VISUAL_WARN`, no effect
on the exit code; cross-process screenshot byte-stability is not yet proven —
GAP-RISK-009). A deployment that has proven byte-stability can gate exit 2 on a visual
diff (like a11y) with **`SENTINEL_VISUAL_AUTHORITATIVE=1`** (ADR-042); gating on by
default awaits the real-browser byte-stability proof (M9-LIVE).

> **Golden integrity (HMAC, #24):** `golden_snapshots` rows are signed with HMAC-SHA256
> (key `state/golden.key`, kept out of the DB). The `created_at` field is **excluded** from
> the signature so Go/Python float-formatting divergence can't break verification. A tampered
> row → mismatch on read → exit 3 (a forged baseline is never trusted).

---

## No Self-Mutating Plan Rule

**As-built:** there is no separate `PLAN_STALE` event or "≥ 2 heal attempts on ≥ 3
`semantic_id`s" threshold in the code (grep across `brain/` — zero matches). What actually
exists is two related mechanisms: (1) a consecutive-step-failure counter
(`consecutive_heal_failures`, `brain/graph.py` and `brain/replay.py`) that, once it hits
`SENTINEL_AUTO_HITL_THRESHOLD`, emits the AG-UI `hitl_needed` event — the signal for the
co-pilot's "take control" banner (M14) — and (2) the AUT-SHA flake quarantine (see "AUT
Version Drift Policy" below). Neither auto-triggers a fresh explore run or overwrites
`plan_hash`: `brain/replay.py` never opens `plan.json` for writing at all; the only writer
is the `report()` node at the end of an explore run.

Re-explore is always an **explicit operator action**:

```bash
agentctl run --explore --target https://app.local --aut-version $(git rev-parse HEAD)
```

This produces a **new** `plan_id` — the old plan remains intact and archivable.

**As-built:** when auto-heal finds a new locator during replay, the change stays only in the
cache (`healed_locators`/`healing_audit`, SQL) — there is no mechanism in the code that
emits it as a "PR artifact" (a proposed `plan.json` diff); grep across `brain/` and `cmd/`
finds zero matches. Either way it is never written to `plan.json` automatically:
`brain/replay.py` never opens `plan.json` for writing. If an engineer wants to promote a
found locator into the plan's `locator`/`alternatives`, that requires manually editing
`plan.json` and re-freezing it — there is no dedicated tooling for this today.

---

## AUT Version Drift Policy

Every run accepts an `--aut-version` flag (typically `$(git rev-parse HEAD)`).

**As-built:** this SHA is **not** compared against any value in `plan.json` — `plan.json`
does not store an `aut_version` at all (see the schema above) — and there is no
`--on-aut-mismatch=warn|heal|abort` policy or `PLAN_STALE` event in the code (grep across
`brain/` and `cmd/` — zero matches). The only role of `--aut-version` is to be the
quarantine key for flaky steps (`brain/store.py`, the `step_failures` table;
`record_step()` is called from `brain/replay.py:232`): a step is quarantined once **≥ 3
of the last 5 attempts** on that AUT SHA have failed (`brain/store.py:188`). Changing
`--aut-version` between runs resets the history (`last5`) and clears the quarantine,
separating real regression from environmental flake.

---

## Seeded Exploration

**As-built:** there is no separate `exploration_seed` field in the code — no
`SHA-256(target_url + nav_structure_fingerprint)` computation, no such key in `plan.json`,
and none in `llm-transcript.jsonl` (grep across `brain/` — zero matches).

What actually exists: all planning and healing LLM calls use `temperature=0`
(`brain/planner.py`, `brain/healing.py`). This reduces, but does not eliminate, provider
non-determinism (streaming tokenisation, model bumps — see the explore-once contract
above). The auditable trail is `llm-transcript.jsonl` — one record per planner decision,
with fields `step`, `planner`, `model`, `decision`, `reason`, `prompt_tokens`,
`completion_tokens` (`brain/__main__.py:113-115`, `brain/graph.py:225-245`) — but neither
`seed` nor `temperature` is stored in those records.

This is the correct trade-off: the frozen `plan.json` absorbs the non-determinism
after the fact, and the transcript gives auditability of decisions — not a bit-identical
reproducibility guarantee the provider cannot give (see ADR-006 in `../ARCHITECTURE.md`).

---

## Structured Exit Codes

| Code | Meaning | Typical cause |
|------|---------|--------------|
| **0** | All non-quarantined steps passed | Clean CI run |
| **1** | One or more step failures, no golden-diff regression | Functional test failure; no baseline impact |
| **2** | Golden-diff regression on a non-quarantined step | `a11y_hash` divergence on a milestone step (**always** gates); `screenshot_hash` divergence only when `SENTINEL_VISUAL_AUTHORITATIVE=1` (advisory by default) |
| **3** | Plan-integrity violation **or** budget exhausted | Hash mismatch on load, explicit budget cap hit, or `--force-replay` used in CI mode |

Quarantined steps are **excluded** from exit-code computation: a quarantined step
that fails does not push the run from exit 0 to exit 1. Quarantine exists precisely
to prevent known-flaky steps from blocking the CI signal.

Alertmanager integration: `heal_rate > 0.20/run` → `DOM_INSTABILITY`;
`budget > 80%` → `BUDGET_WARNING`; `quarantine_count > 5` → blocks CI pipeline.

---

## CI Parallelism and Database Strategy

### Per-job SQLite (CI)

Every CI job writes to an **isolated, per-run SQLite file**:

```
AGENT_DB_PATH=/tmp/agent-{run_id}.db       # main store-gateway DB
AGENT_CKPT_PATH=/tmp/agent-{run_id}-ckpt.db  # LangGraph checkpoint DB (separate)
```

Concurrent CI jobs never contend on a shared writer. Files are ephemeral and
discarded after the job uploads its artifacts.

### Shared SQLite (home-lab service)

The long-lived service on K3s uses a single shared SQLite (WAL mode) under the Go
store-gateway's exclusive write ownership. Concurrent reads from `control-api`
and `agentctl` are safe under WAL.

### Postgres Migration Trigger

Postgres for the checkpointer (the synchronous `PostgresSaver` via `CHECKPOINT_DSN`; the store-gateway DB via `STORE_DSN` is still refused → M13-service) is introduced **only** when either of these
explicit triggers is hit:

- More than 50 concurrent shared-DB writers, **or**
- Distributed workers spanning multiple hosts

The schema is Postgres-compatible by design; the migration is a driver swap in
`store-gateway` with no schema changes. This is deferred to M5 — not pre-built.

---

## End-to-end Session Walkthrough

*Derived from `.result.final.dataFlowNarrative` — two sessions: one explore, one CI replay.*

### Session 1 — Explore

An engineer runs:

```bash
agentctl run --explore --target https://app.local --aut-version $(git rev-parse HEAD)
```

**Startup.** `agentctl` (Go) loads the YAML config and, at M2+, calls
`orchestrator.StartRun` over gRPC. The orchestrator spawns the Python brain
subprocess with environment variables: `RUN_ID`, `RUN_MODE=explore`,
`ARTIFACT_DIR`, `AGENT_DB_PATH`.

**Brain initialisation.** The brain initialises a LangGraph `StateGraph` with a
`SqliteSaver` checkpointer pointed at a **separate** checkpoint DB file
(`{ARTIFACT_DIR}/ckpt.db`). It spawns the `pw-executor` TS MCP server (built by
us) as a child process over stdio and binds its tools via the LangGraph MCP
adapter.

**perceive.** `START → perceive`: the brain calls `pw-executor`'s
`accessibility_snapshot()` tool. The `perception` module parses the result into a
`PageModel`, computes `completeness_ratio` (say 0.62 — a11y-primary path), and
derives `a11y_hash`, `screenshot_hash`, and `dom_subtree_hash` for the scenario's
target container. Playwright tracing is started via `pw-executor`.

**ground.** `ground` updates `interactive_seen` and `nav_frontier`, computes
`coverage_achieved = 0.0`. Since `coverage < target` and mode is explore,
`ground → plan`.

**plan.** The default planner — Opus 4.8 (`temperature=0`) — reads the `PageModel`, the episodic tail,
the nav frontier, and the remaining budget. It returns the next `PlannedAction`
(e.g., click "Sign in"). The in-process token counter increments. The orchestrator
reconciles the Go-side hard budget ceiling on the next `RunEvent`.

**act → verify.** `plan → act` executes the click via `pw-executor`.
`act → verify` re-snapshots; the step passes and is a milestone, so
`verify → checkpoint`. The `checkpoint` node flushes the LangGraph checkpoint and
the new `page_model` to the store-gateway over gRPC.

**Mid-run heal.** Later, a locator probe fails with `LOCATOR_STALE`.
`verify → heal` invokes the healing engine:

1. Cache lookup — miss (no prior heal for this `semantic_id` / `dom_subtree_hash`).
2. L1–L6 rotation — L2 ARIA role + name match found; verify-before-accept probe
   confirms the candidate resolves to exactly one live element.
3. `confidence = 0.90 ≥ 0.85` → auto-heal path: post-heal verification re-runs the
   action successfully.
4. `HealedLocator` persisted to store-gateway (keyed to `dom_subtree_hash`);
   `healing_audit` row appended (append-only).

The loop continues: each planner decision (Opus by default) expands coverage and the nav frontier
shrinks.

**Convergence.** When `coverage_achieved ≥ 0.85` AND `nav_frontier` is empty,
`ground` sets `exploration_complete = True` and routes to `report`.

**Freeze and emit.** The brain freezes `plan.json` (the `report()` node, computes
`plan_hash` over the ordered `steps[]` array) — golden baselines are **not** written at
this step, that is a separate explicit step (see "Immutable Golden Baselines" above). The
brain stops the `pw-executor` trace and relays `trace_path` to Go via gRPC. HTML/JSON
artifacts and an exported `.spec.ts` are generated separately from `RunState.executed_actions`.

**Engineer review.** The engineer reviews flagged heals in the report, then:

```bash
git add plan.json
git commit -m "feat(sentinel): add explore plan for https://app.local"
```

---

### Session 2 — CI Replay

CI runs:

```bash
agentctl run --replay --plan plan.json --target https://app.local --ci \
  --aut-version $(git rev-parse HEAD)
```

with `AGENT_DB_PATH=/tmp/agent-{run_id}.db` (per-job isolation).

**Hash integrity check.** The brain loads `plan.json` and **immediately**
re-computes `plan_hash`. The hashes match — proceed.

**Golden baseline validation.** On first landing on a page, replay computes `a11y_hash`
and `screenshot_hash` and checks them against the immutable golden baselines stored in
the SQL `golden_snapshots` table (not in `plan.json`) — written there by an explicit
`agentctl baseline update` run, not by this CI replay. No drift detected — proceed.

**LLM-free execution.** The run goes through `run_replay()` (not the graph): each step
takes its frozen locator and executes the action. Zero planning tokens consumed. Most
steps pass deterministically.

**Amortised cache heal.** One step's `data-testid` was renamed by a developer in a
recent commit. `verify → heal`, cache lookup finds the `HealedLocator` written
during the explore session — its `dom_subtree_hash` still matches the current
subtree. The cached healed locator is reused **instantly, zero LLM**
(amortisation: LLM cost paid once at explore time, reused until structural drift).

**Low-confidence step.** Another element is genuinely gone. L1–L6 rotation and one
heal-model attempt (Sonnet by default; hard 2-cap + per-step deadline) yield `confidence = 0.55`.
In CI mode, `confidence < 0.60` → `SKIPPED_HEALING_FAILURE` recorded; the run
continues without blocking.

**Flake quarantine.** A third step fails for the third consecutive time, all
failures occurring without an AUT SHA change between them. The step is quarantined
(non-blocking); its failure does not affect the exit code.

**Exit and artifacts.** The run exits **0** (no golden regression, no critical
unquarantined failure). The brain writes `heal-report.json`, from which
`brain/report.py::generate()` produces `report.json` + `report.html` + `metrics.prom`;
`trace.zip` is already on disk from `pw-executor`. The amortised-reuse locator change
stays only in the cache (`healed_locators`/`healing_audit`) — `plan.json` is not
rewritten by this step (see "No Self-Mutating Plan Rule" above).
