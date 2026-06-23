# Sentinel — Determinism, CI Contract, and Plan Integrity

Derived from the design synthesis 2026-06-23; canonical summary in ../ARCHITECTURE.md.

> **Type:** Explanation
> **Audience:** CI engineers, QA leads, operators
> **Last updated:** 2026-06-23
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

---

## Plan Freezing and the `plan.json` Schema

At the end of a successful explore run, the brain serialises the ordered
`PlannedAction` sequence — including resolved locators, L1–L6 alternatives,
expected outcomes, and golden snapshot hashes — into `plan.json`. This file is
committed to the application repository and becomes the authoritative test
definition.

### Schema

```json
{
  "plan_id":          "<UUIDv4>",
  "plan_hash":        "<SHA-256 of canonical JSON>",
  "target_url":       "https://app.local",
  "aut_version":      "<git SHA of the app under test at explore time>",
  "exploration_seed": "<SHA-256(target_url + nav_structure_fingerprint)>",
  "coverage_achieved": 0.91,
  "steps": [
    {
      "step_id":             "step-001",
      "intent":              "Sign in as a standard user",
      "semantic_id":         "auth/sign-in-button",
      "action_type":         "click",
      "locator":             "[data-testid='sign-in-btn']",
      "locator_alternatives": {
        "L1": "[data-testid='sign-in-btn']",
        "L2": "role=button[name='Sign in']",
        "L3": "[aria-label='Sign in']",
        "L4": "text='Sign in' >> role=button",
        "L5": ".auth-form button[type='submit']",
        "L6": "//form[@class='auth-form']//button"
      },
      "value":              null,
      "expected_outcome":   "URL changes to /dashboard",
      "assertion":          "url_contains:/dashboard",
      "is_critical":        true,
      "is_milestone":       true,
      "healed":             false
    }
  ],
  "golden_snapshots": {
    "step-001": {
      "a11y_hash":       "<SHA-256 of normalised a11y tree post-action>",
      "screenshot_hash": "<perceptual hash of post-action screenshot>"
    }
  }
}
```

**Hash canonicalisation:** `plan_hash` is SHA-256 of the JSON serialisation of
`steps[]` with all object keys sorted lexicographically and floats normalised to
6 decimal places. This is computed in the brain at freeze time and re-computed at
replay start for integrity verification.

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
agentctl run --ci --plan-id <id> --aut-version $(git rev-parse HEAD)

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

In replay and CI mode, the `plan` node is **skipped entirely**. The `ground` node
routes directly to `act`, which executes the frozen locator without any LLM
involvement.

```
perceive → ground → act → verify → checkpoint → (next step)
                                  └── heal (only on live locator failure)
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

Every milestone step records two hashes at explore time:

| Hash | What it covers | Regression type caught |
|------|---------------|----------------------|
| `a11y_hash` | SHA-256 of the normalised accessibility tree after the step | Structural DOM change — new/removed elements, role/label drift |
| `screenshot_hash` | Perceptual hash of the post-action screenshot | Visual-only regression — CSS layout, colour, hidden elements |

Golden baselines are **never auto-updated by a CI run**. The only mutation path is
an explicit operator command:

```bash
agentctl baseline update --plan-id <id> --aut-version $(git rev-parse HEAD)
```

This command:
1. Runs a full replay against the live AUT.
2. Accepts all current snapshots as the new golden state.
3. Writes a **new** `plan_hash`, archiving the old one with a `superseded_by`
   reference in `golden_snapshots`.

This design makes "the tests rewrote their own baseline" structurally impossible.
Dual hashing catches visual-only (CSS/layout) regressions that pure a11y diffing
is blind to, surfaced as a `VISUAL_WARN` event rather than a hard failure
(configurable).

---

## No Self-Mutating Plan Rule

If the replay detects plan staleness — defined as ≥ 2 heal attempts on ≥ 3
distinct `semantic_id`s in one run — it emits a `PLAN_STALE` event and a
recommendation in the run report. It **does not** auto-trigger a fresh explore run
or overwrite `plan_hash`.

Re-explore is always an **explicit operator action**:

```bash
agentctl run --explore --target https://app.local --aut-version $(git rev-parse HEAD)
```

This produces a **new** `plan_id` — the old plan remains intact and archivable.

When an auto-heal updates a frozen locator during replay, the change is emitted as
a **PR artifact** (a proposed `plan.json` diff) for human review. It is never
silently auto-committed to the plan file. Engineers review, approve, and commit
the diff manually.

---

## AUT Version Drift Policy

Every run accepts an `--aut-version` flag (typically `$(git rev-parse HEAD)`).
Sentinel compares this to the `aut_version` stored in `plan.json`.

| Relationship | Default behaviour | Override |
|---|---|---|
| Match | Proceed normally | — |
| Mismatch | `--on-aut-mismatch=warn` (default): log warning, enable healing, mark healed steps as flagged | `--on-aut-mismatch=heal` or `--on-aut-mismatch=abort` |

The stored `aut_version` is also the key for the AUT-SHA-gated flake quarantine: a
step is only quarantined after failing N-of-5 recent runs **without** an AUT SHA
change. SHA changes reset the failure counter, separating real regression from
environmental flake.

---

## Seeded Exploration

The explore run is seeded but not guaranteed bit-identical. The brain records:

```
exploration_seed = SHA-256(target_url + nav_structure_fingerprint)
```

All planning LLM calls use `temperature=0`. The seed and temperature are logged in
`plan.json` and the LLM transcript, making the exploration context fully auditable
and re-runnable with the same anchor — even though LLM provider non-determinism
means output is not bit-identical across model versions or provider-side changes.

This is the correct trade-off: the frozen `plan.json` absorbs the non-determinism
after the fact. The seed provides auditability, not a reproducibility guarantee
the provider cannot give (see ADR-006 in `../ARCHITECTURE.md`).

---

## Structured Exit Codes

| Code | Meaning | Typical cause |
|------|---------|--------------|
| **0** | All non-quarantined steps passed | Clean CI run |
| **1** | One or more step failures, no golden-diff regression | Functional test failure; no baseline impact |
| **2** | Golden-diff regression on a non-quarantined step | `diff_ratio` above threshold **or** `screenshot_hash` divergence on a milestone step |
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
store-gateway's exclusive write ownership. Concurrent reads from `report-service`
and `agentctl` are safe under WAL.

### Postgres Migration Trigger

Postgres with `AsyncPostgresSaver` is introduced **only** when either of these
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

**plan.** Opus 4.8 (`temperature=0`) reads the `PageModel`, the episodic tail,
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

The loop continues: each Opus decision expands coverage and the nav frontier
shrinks.

**Convergence.** When `coverage_achieved ≥ 0.85` AND `nav_frontier` is empty,
`ground` sets `exploration_complete = True` and routes to `report`.

**Freeze and emit.** The brain freezes `plan.json` (computes `plan_hash`, writes
dual golden baselines per milestone step), stops the `pw-executor` trace, and
relays `trace_path` to Go via gRPC. `report-service` emits HTML/JSON artifacts
and an exported `.spec.ts` generated from `RunState.executed_actions`.

**Engineer review.** The engineer reviews flagged heals in the report, then:

```bash
git add plan.json
git commit -m "feat(sentinel): add explore plan for https://app.local"
```

---

### Session 2 — CI Replay

CI runs:

```bash
agentctl run --ci \
  --plan-id 3f7a... \
  --aut-version $(git rev-parse HEAD)
```

with `AGENT_DB_PATH=/tmp/agent-{run_id}.db` (per-job isolation).

**Hash integrity check.** The brain loads `plan.json` and **immediately**
re-computes `plan_hash`. The hashes match — proceed.

**Golden baseline validation.** The `ground` node validates each milestone step's
`a11y_hash` and `screenshot_hash` against the immutable golden baselines stored in
`golden_snapshots`. No drift detected — proceed.

**LLM-free execution.** The `plan` node is **skipped**. `ground → act` uses the
frozen locator for each step. Zero planning tokens consumed. Most steps pass
deterministically.

**Amortised cache heal.** One step's `data-testid` was renamed by a developer in a
recent commit. `verify → heal`, cache lookup finds the `HealedLocator` written
during the explore session — its `dom_subtree_hash` still matches the current
subtree. The cached healed locator is reused **instantly, zero LLM**
(amortisation: LLM cost paid once at explore time, reused until structural drift).

**Low-confidence step.** Another element is genuinely gone. L1–L6 rotation and one
Sonnet attempt (hard 2-cap + per-step deadline) yield `confidence = 0.55`.
In CI mode, `confidence < 0.60` → `SKIPPED_HEALING_FAILURE` recorded; the run
continues without blocking.

**Flake quarantine.** A third step fails for the third consecutive time, all
failures occurring without an AUT SHA change between them. The step is quarantined
(non-blocking); its failure does not affect the exit code.

**Exit and artifacts.** The run exits **0** (no golden regression, no critical
unquarantined failure). `report-service` publishes JSON + HTML + `trace.zip`. The
proposed `plan.json` diff for the amortised-reuse locator change is emitted as a
PR artifact — for human review, never auto-committed.
