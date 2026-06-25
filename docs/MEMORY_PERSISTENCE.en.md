# Sentinel — Memory Architecture and Persistence

> 🌐 [Русский](MEMORY_PERSISTENCE.md) (основная версия) · **English**

Derived from the design synthesis 2026-06-23; canonical summary in ../ARCHITECTURE.md.

> **Type:** Explanation
> **Audience:** backend engineers, operators, contributors
> **Last updated:** 2026-06-23
> **Related:** [DETERMINISM.md](./DETERMINISM.md), [../ARCHITECTURE.md](../ARCHITECTURE.md)

## Overview

Sentinel operates two distinct memory tiers with deliberately separated ownership:
**short-term episodic memory** persisted by the Python LangGraph brain into its own
checkpoint database, and **long-term cross-session memory** owned exclusively by
the Go `store-gateway` component. Python and TypeScript never hold a direct database
handle. All long-term writes flow through the `PersistenceService` gRPC interface.

This separation is the architectural fix to the contradiction present in earlier
proposals (P1/P2), which claimed single DB ownership while simultaneously allowing
the LangGraph checkpointer to write the same file.

---

## Short-Term Memory — Episodic (within a run)

### Mechanism

The LangGraph `RunState` object **is** the working memory for a run. It is a typed
`TypedDict` that accumulates page observations, planned actions, executed outcomes,
heal attempts, budget counters, and human-gate state throughout the run lifecycle.

`RunState` is checkpointed at every `checkpoint` node transition by a LangGraph
`SqliteSaver`, which writes to a **separate DB file** from the store-gateway's
main database.

### Database file locations

| Context | Checkpoint DB path | Main store-gateway DB path |
|---------|-------------------|---------------------------|
| CI (per-job) | `/tmp/agent-{run_id}-ckpt.db` | `/tmp/agent-{run_id}.db` |
| Long-running service | Distinct file configured in `YAML` | Shared service DB |
| K3s (M5+) | `AsyncPostgresSaver` schema | Postgres (same instance, separate schema) |

These are **never the same file**. The `SqliteSaver` and the Go store-gateway are
independent single-writers of their respective databases. This is what makes the
"Go store-gateway is sole writer of the main DB" claim structurally true.

### Episodic buffer and context bounding

`RunState` contains an `episodic_buffer` — a bounded `deque` with a maximum of
50 events. When the buffer is full, the oldest events are summarised by a Sonnet
call into a ~200-token episode summary before eviction. This bounds the context
window growth across long explore runs without losing the narrative continuity the
plan node needs.

### Crash-resume

If the Python brain crashes mid-run:
1. The orchestrator detects gRPC stream termination (within the 5-second health
   ping interval).
2. The run is marked `FAILED` and partial state is recorded via store-gateway.
3. The LangGraph checkpoint DB remains intact on disk.
4. `agentctl run --resume <run_id>` restarts the brain, which reloads
   `RunState` from the checkpoint and continues from the last node boundary —
   no work lost.

---

## Long-Term Memory — Cross-Session (store-gateway)

Long-term state is owned **exclusively** by the Go `store-gateway` component.

- **Single writer:** all writes are serialised through the `PersistenceService`
  gRPC interface. No Python or TypeScript component ever opens a direct DB
  connection.
- **Concurrent readers:** `report-service` and `agentctl` may read concurrently
  under SQLite WAL mode without blocking the writer.
- **Schema migrations:** managed by `golang-migrate` inside `store-gateway`.
  Migrations run at startup; the schema is Postgres-compatible.

### Tables

#### `healed_locators`

The primary amortisation cache. A healed locator is stored once after its first
successful heal and reused on subsequent runs until the target subtree's structural
hash drifts — at which point it is auto-evicted.

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Primary key |
| `page_url` | TEXT | URL of the page where healing occurred |
| `semantic_id` | TEXT | Stable semantic identifier of the element (e.g. `auth/sign-in-button`) |
| `element_label` | TEXT | Human-readable element description |
| `original_selector` | TEXT | The broken selector that triggered healing |
| `healed_selector` | TEXT | The replacement locator produced by the healing engine |
| `healing_method` | TEXT | Strategy used: `L1`..`L6`, `llm_a11y`, `llm_visual`, or `cache` |
| `confidence` | REAL | Grounded confidence score after discounts and live-DOM probe |
| `dom_subtree_hash` | TEXT | SHA-256 of the scenario's target subtree at heal time |
| `times_validated` | INTEGER | Number of times this locator has been successfully reused |
| `status` | TEXT | `active`, `flagged`, `human_verified`, `deprecated`, or `quarantined` |
| `human_verified` | BOOLEAN | True if an operator has manually approved this locator |
| `created_at` | TIMESTAMP | When the heal was first persisted |
| `last_used_at` | TIMESTAMP | When the locator was last successfully reused |

**Primary lookup key:** `(page_url, semantic_id)`

**Amortisation and auto-eviction:** on cache lookup, the stored `dom_subtree_hash`
is compared to the current subtree hash. Match → reuse with zero LLM cost.
Mismatch → mark record `deprecated`, proceed with fresh healing. This scopes
invalidation to the element's structural neighbourhood, not the whole page —
an unrelated ad, banner, or analytics widget cannot invalidate all cached locators.

---

#### `page_models`

Structural state of each page as last observed. Used for drift detection and as
context for the `ground` node during replay.

| Column | Type | Description |
|--------|------|-------------|
| `url_hash` | TEXT | SHA-256 of the page URL (primary key) |
| `a11y_tree_json` | TEXT | Full serialised accessibility tree (normalised) |
| `landmarks_json` | TEXT | Page landmark regions (header, nav, main, footer, etc.) |
| `form_count` | INTEGER | Number of `<form>` elements detected |
| `interactive_count` | INTEGER | Number of interactive elements in the a11y tree |
| `a11y_hash` | TEXT | SHA-256 of the normalised a11y tree (structural fingerprint) |
| `screenshot_hash` | TEXT | Perceptual hash of the page screenshot |
| `last_updated` | TIMESTAMP | When this record was last written |

Used by `ground` in replay to detect `STRUCTURAL_CHANGE` (a11y_hash divergence)
and `VISUAL_WARN` (screenshot_hash divergence) before executing any step.

---

#### `golden_snapshots`

The immutable regression baselines captured at each milestone step during an
explore run. Never auto-updated by a CI run.

| Column | Type | Description |
|--------|------|-------------|
| `plan_id` | UUID | Plan this snapshot belongs to |
| `step_id` | TEXT | Step identifier within the plan |
| `a11y_hash` | TEXT | SHA-256 of the normalised a11y tree after the step completes |
| `screenshot_hash` | TEXT | Perceptual hash of the post-action screenshot |
| `content_path` | TEXT | Filesystem path to the stored snapshot content |
| `created_at` | TIMESTAMP | When the baseline was recorded |
| `superseded_by` | UUID | `plan_id` of the replacement snapshot if a baseline update was performed; `NULL` otherwise |

**Mutation path:** `agentctl baseline update --plan-id <id> --aut-version <sha>` is
the **only** command that writes new rows. It archives the previous record by
setting `superseded_by` and writes a new `plan_hash`. CI runs have no code path
that touches this table as a writer.

---

#### `healing_audit`

An append-only forensic ledger of every heal attempt. No `UPDATE` or `DELETE`
statement is ever issued against this table. It is the source of truth for
`agentctl calibrate` and the `healing-audit.jsonl` CI artifact.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | UUID | Run in which the attempt occurred |
| `step` | INTEGER | Step index within the plan |
| `semantic_id` | TEXT | Target element's semantic identifier |
| `original_selector` | TEXT | The locator that failed |
| `strategy_used` | TEXT | `L1`..`L6`, `llm_a11y`, `llm_visual`, or `cache` |
| `healed_selector` | TEXT | Candidate locator produced by the strategy |
| `confidence` | REAL | Final score after all discounts and the verify-before-accept probe |
| `outcome` | TEXT | `persisted`, `flagged`, `human_gate`, `quarantined`, or `skipped` |
| `llm_tokens` | INTEGER | Tokens consumed if an LLM strategy was invoked; `0` otherwise |
| `duration_ms` | INTEGER | Total wall time of the heal cycle |
| `dom_hash_before` | TEXT | Subtree hash before the heal attempt |
| `dom_hash_after` | TEXT | Subtree hash after the heal attempt (may be identical) |
| `timestamp` | TIMESTAMP | Row append time |

This table is the input to `agentctl calibrate`, which computes precision and
recall of auto-healed locators against `human_verified` outcomes over a rolling
window, and recalibrates the auto-accept confidence threshold away from the cold-
start default of 0.90.

---

#### `step_failures`

Per-step failure tracking for the AUT-SHA-gated flake quarantine logic.

| Column | Type | Description |
|--------|------|-------------|
| `plan_id` | UUID | Plan the step belongs to |
| `step_key` | TEXT | Composite key identifying the step (e.g. `plan_id:step_id`) |
| `fail_count` | INTEGER | Consecutive failure count |
| `last_5_results` | TEXT | JSON array of the last 5 outcomes (`pass`, `fail`, `healed`, `quarantined`) |
| `last_seen_aut_sha` | TEXT | AUT git SHA recorded on the most recent failure |
| `quarantine_status` | TEXT | `none`, `quarantined`, or `cleared` |

**Quarantine logic:** a step is only quarantined after failing in N-of-5 recent
runs **without** an AUT SHA change between those failures. If the AUT SHA changes,
the `fail_count` and `last_seen_aut_sha` are reset. Quarantined steps are cleared
by `agentctl locators clear-quarantine` or by 3 consecutive passes.

---

#### `runs`

One row per run. The primary cost and trend data source for Grafana and
`agentctl report`.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | UUID | Unique run identifier |
| `target_url` | TEXT | AUT URL |
| `plan_hash` | TEXT | SHA-256 of plan steps at run start |
| `run_mode` | TEXT | `explore`, `replay`, or `ci` |
| `status` | TEXT | `PENDING`, `RUNNING`, `HEALING`, `PAUSED`, `PARTIAL`, `DONE`, `FAILED`, `ABORTED` |
| `aut_version` | TEXT | git SHA of the AUT at run start |
| `token_cost_usd` | REAL | Total LLM spend across all nodes |
| `plan_tokens` | INTEGER | Tokens consumed in the `plan` node (Opus 4.8) |
| `heal_tokens` | INTEGER | Tokens consumed in the `heal` node (Sonnet 4.6) |
| `duration_ms` | INTEGER | Total wall time of the run |
| `steps_pass` | INTEGER | Count of steps that passed |
| `steps_fail` | INTEGER | Count of steps that failed |
| `steps_healed` | INTEGER | Count of steps successfully auto-healed |
| `steps_quarantined` | INTEGER | Count of steps currently quarantined |
| `coverage_achieved` | REAL | Final `len(exercised) / max(1, len(seen))` ratio |
| `started` | TIMESTAMP | Run start time |
| `completed` | TIMESTAMP | Run completion time (`NULL` if in progress) |

---

#### `run_transcripts`

File references to per-run LLM transcript files. Does not store transcript
content inline to keep the DB size bounded.

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | UUID | Foreign key to `runs` |
| `transcript_path` | TEXT | Absolute filesystem path to the `llm-transcript.jsonl` file |
| `byte_size` | INTEGER | File size at the time of registration |
| `recorded_at` | TIMESTAMP | When the path was registered |

The `.jsonl` file itself contains one JSON object per LLM call:
`{ts, run_id, step_id, node, model, prompt_tokens, completion_tokens, latency_ms,
cost_usd, decision_summary, temperature}`. It is written with `fsync` at run end
and never overwritten.

---

#### `page_object_cache`

Generated Playwright test code keyed by URL pattern. Populated by `report-service`
when it generates `.spec.ts` exports from `RunState.executed_actions`.

| Column | Type | Description |
|--------|------|-------------|
| `url_pattern` | TEXT | Normalised URL pattern (primary key; query strings stripped) |
| `spec_path` | TEXT | Filesystem path to the generated `.spec.ts` file |
| `page_object_path` | TEXT | Filesystem path to the generated page-object class file |
| `plan_id` | UUID | Plan from which this code was generated |
| `generated_at` | TIMESTAMP | Generation timestamp |
| `invalidated_at` | TIMESTAMP | Set when the plan is superseded; `NULL` if current |

---

## Storage Rationale

### Why SQLite WAL for the main store

- **Zero operational burden:** no daemon, no network port, no cluster — a single
  file. Backup is `cp` or `sqlite3 .dump`. Appropriate for 1–10 concurrent
  single-host runs.
- **Single-writer model:** Go's exclusive write ownership via the `store-gateway`
  serialises all mutations. WAL mode allows unlimited concurrent readers
  (`report-service`, `agentctl`) without writer blocking.
- **Schema portability:** the schema is written to be Postgres-compatible. The
  migration when Postgres is introduced is a driver change in `store-gateway`
  — no schema rewrites.

### Why the checkpoint DB is separate

Keeping the LangGraph `SqliteSaver` in its own file makes the ownership contract
unambiguous: Go store-gateway is the sole writer of the main database, Python
brain is the sole writer of the checkpoint database, TypeScript `pw-executor`
writes nothing to either. Two independent single-writer guarantees, verified by
inspection rather than convention.

### Postgres and AsyncPostgresSaver

Postgres is **not pre-built**. It is introduced at M5 and only when one of these
explicit triggers is hit:

- More than 50 concurrent shared-DB writers, **or**
- Workers distributed across multiple hosts (K3s multi-node).

When triggered: `store-gateway` switches its `database/sql` driver from
`modernc.org/sqlite` to `lib/pq`; LangGraph's `SqliteSaver` is replaced by
`AsyncPostgresSaver` (one constructor change in the brain). The schema requires
no changes.

### Checkpoint GC

Checkpoints for completed runs accumulate over time. A maintenance goroutine in
`store-gateway` prunes checkpoint records for runs in a terminal state
(`DONE`, `FAILED`, `ABORTED`) that are older than N runs (default: keep the 10
most recent completed runs per `target_url`). The checkpoint DB files themselves
are deleted from disk after the corresponding run's retention window expires. This
bounds checkpoint DB growth without manual intervention.
