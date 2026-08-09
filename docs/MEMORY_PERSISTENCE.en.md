# Sentinel — Memory Architecture and Persistence

> 🌐 [Русский](MEMORY_PERSISTENCE.md) (основная версия) · **English**

Derived from the design synthesis 2026-06-23; canonical summary in ../ARCHITECTURE.md.

> **Type:** Explanation
> **Audience:** backend engineers, operators, contributors
> **Last updated:** 2026-07-12
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
`TypedDict` that accumulates page observations, planned actions, executed outcomes, and a
consecutive-heal-failure counter (`consecutive_heal_failures`) throughout the run lifecycle.
The token budget is the process-global `BudgetTracker` (`brain/budget.py`), not a `RunState`
field; there are no human-gate fields in `RunState`.

`RunState` is checkpointed at every `checkpoint` node transition by a LangGraph
checkpointer — a synchronous `SqliteSaver` by default, or a synchronous `PostgresSaver`
if the `CHECKPOINT_DSN` environment variable is set (`brain/__main__.py:_checkpointer`) —
which writes to a **separate file/DB** from the store-gateway's main database.

### Database file locations

| Context | Checkpoint DB path |
|---------|-------------------|
| Explore / replay run | `<artifact_dir>/checkpoint.db` — unique per `run_id` (`brain/__main__.py:135`) |
| Multi-turn chat (turn-N refine) | `state/conversations.db` — a shared, NON-ephemeral DB, keyed by `thread_id=conversation_id`; overridable via `SENTINEL_CONVERSATIONS_DB`; the thread is NOT deleted at turn end (`brain/__main__.py:_conversations_store_path`) |
| `CHECKPOINT_DSN` set (any mode) | Postgres via a synchronous `PostgresSaver`, requiring a one-time `saver.setup()` — replaces the SQLite file path above |

This is **never the same file** as the store-gateway's main DB (default
`state/locators.db` — the `-db` flag on `cmd/store-gateway`; the same path is a constant
in `brain/__main__.py:_STORE_PATH`). The LangGraph checkpointer and the Go store-gateway
are independent single-writers of their respective databases. This is what makes the
"Go store-gateway is sole writer of the main DB" claim structurally true.

### Refine-history bounding (GAP-M9-20)

`RunState` does **not** contain a separate `episodic_buffer` — no such field exists in the
state. The real history-bounding mechanism (`brain/graph.py:105-141`) operates on the
multi-turn conversation's user turns (`_user_turns`): `_capped_history()` leaves the last
`SENTINEL_REFINE_HISTORY_KEEP` turns (6 by default) unchanged, and collapses all older
turns into a single summary line via `_rolling_summary()` — pure string formatting
(`"{N} turn(s); started: {opening turn text}"`), **with no LLM call**. This bounds the
refine prompt's growth across long multi-turn conversations without losing the turn count
or the content of the very first turn.

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
- **Concurrent readers:** `control-api` and `agentctl` may read concurrently
  under SQLite WAL mode without blocking the writer.
- **Schema migrations (reality as of M13):** today — idempotent `CREATE TABLE
  IF NOT EXISTS` (+ one-off `ALTER`s) in `store-gateway` at startup, for the
  heal/trust tables and the **6 M13 domains** (runs·scenarios/tests·chats·results·
  metrics + `config`, ADR-050/062). The SQL is written portably (`ON CONFLICT DO UPDATE`, no
  SQLite-only syntax in the M13 domains). `golang-migrate` + a Postgres backend
  (behind `STORE_DSN`) are **aspirational, deferred to M13-service** (M11/ADR-053);
  `STORE_DSN` is currently recognized and refused (fail-loud), not silently SQLite.

### Tables

#### `healed_locators`

The primary amortisation cache. A healed locator is stored after a successful heal and
reused on subsequent runs until the target subtree's structural hash drifts — at which
point it is auto-evicted. Real schema (`internal/store/server.go:28-32`):

| Column | Type | Description |
|--------|------|-------------|
| `page_path` | TEXT | Path of the page where healing occurred |
| `semantic_id` | TEXT | Stable semantic identifier of the element |
| `strategy` | TEXT | Healing strategy used |
| `value` | TEXT | The healed locator's value |
| `confidence` | REAL | Grounded confidence score after discounts and live-DOM probe |
| `dom_subtree_hash` | TEXT | SHA-256 of the target subtree at heal time |
| `status` | TEXT | `active` by default; `deprecated` after auto-eviction (`EvictStale`) |
| `times_used` | INTEGER | Reuse counter for this locator (default 0, incremented via `BumpUsed`) |
| `created_at` | REAL | Unix time (seconds, float) the record was first persisted |

**Primary key:** `(page_path, semantic_id, dom_subtree_hash)` — not just
`(page_path, semantic_id)`: distinct `dom_subtree_hash` values for the same element
coexist as separate rows.

**Amortisation and auto-eviction:** on lookup (`Lookup`), the row is looked up by
`page_path` + `semantic_id` + the current `dom_subtree_hash` and `status='active'`.
Match → reuse with zero LLM cost. Mismatch → `EvictStale` marks the element's prior rows
`deprecated`, and fresh healing proceeds. This scopes invalidation to the element's
structural neighbourhood, not the whole page — an unrelated ad, banner, or analytics
widget cannot invalidate all cached locators.

---

> **Note:** there is no separate long-term `page_models` table in the store-gateway —
> neither in the legacy heal/trust schema nor in the 6 M13 domains
> (`internal/store/server.go`). `page_model` is a `RunState` field (`brain/state.py`) — part
> of the run's short-term checkpoint memory, not a row in long-term storage.

---

#### `golden_snapshots`

The immutable regression baselines, keyed by page/step (`page_key`). Never auto-updated
by a CI run. Real schema (`internal/store/server.go:37-39`):

| Column | Type | Description |
|--------|------|-------------|
| `page_key` | TEXT | Page/step key this snapshot belongs to (primary key) |
| `a11y_hash` | TEXT | SHA-256 of the normalised a11y tree after the step completes |
| `screenshot_hash` | TEXT | Perceptual hash of the post-action screenshot |
| `created_at` | REAL | Unix time (seconds, float) the row was written |
| `mac` | TEXT | HMAC-SHA256 (#24) of `page_key`+`a11y_hash`+`screenshot_hash`, keyed by `state/golden.key`; protects against tampering with the row outside `SaveGolden` |

**Mutation path:** `agentctl baseline update --plan <plan.json> [--target <URL>]` is
the **only** command that updates baselines. The write is an `INSERT OR REPLACE` keyed
on `page_key` (it overwrites the previous row entirely; the table keeps no separate
archive of prior versions and has no `superseded_by` field). CI runs have no code path
that touches this table as a writer.

**Integrity check:** every read (`GetGolden`) recomputes and compares `mac`; a missing
or invalid MAC is rejected as `codes.DataLoss` ("golden integrity: missing or invalid
MAC … tampered or DB swapped"), which the brain maps to a controlled exit code rather
than silently trusting a swapped-in row.

---

#### `healing_audit`

An append-only forensic ledger of every heal attempt (`INSERT` only, no `UPDATE`/`DELETE`
is ever issued against this table). Real schema (`internal/store/server.go:33-36`) — no
`mac` column (unlike `golden_snapshots`, this table is not HMAC-protected):

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | TEXT | Run in which the attempt occurred |
| `step` | INTEGER | Step index within the plan |
| `semantic_id` | TEXT | Target element's semantic identifier |
| `page_path` | TEXT | Path of the page where the attempt occurred |
| `strategy` | TEXT | Healing strategy used |
| `original` | TEXT | The locator that failed |
| `healed` | TEXT | Candidate locator produced by the strategy |
| `confidence` | REAL | Final score after all discounts |
| `outcome` | TEXT | The attempt's outcome (the exact value set is defined by the brain's calling code, not the DB) |
| `dom_hash` | TEXT | Subtree hash at the time of the heal attempt |
| `ts` | REAL | Unix time (seconds, float) the row was appended |

This table is the data source for `agentctl calibrate` and a CI artifact that computes
precision/recall of auto-healed locators over a rolling window.

---

#### `step_failures`

Per-step failure tracking for the AUT-SHA-gated flake quarantine logic. Real schema
(`internal/store/server.go:40-43`):

| Column | Type | Description |
|--------|------|-------------|
| `plan_id` | TEXT | Plan the step belongs to |
| `step_key` | TEXT | Composite key identifying the step (e.g. `plan_id:step_id`) |
| `last5` | TEXT | JSON array of the last up-to-5 binary outcomes: `1` = pass, `0` = fail |
| `last_aut_sha` | TEXT | AUT git SHA recorded on the most recent run |
| `quarantined` | INTEGER | `1` if the step is quarantined, else `0` — derived from `last5` on every `RecordStep`; there is no separate `fail_count` counter in the schema |

**Primary key:** `(plan_id, step_key)`

**Quarantine logic** (`RecordStep`, `internal/store/server.go:334-390`): `last5` is
reset when the AUT SHA changes. A step is quarantined once `last5` accumulates **>=3**
`0` (fail) entries out of the last up-to-5. It is auto-cleared on **3 consecutive** `1`
(pass) entries at the end of `last5` — or manually via `agentctl locators
clear-quarantine`.

---

#### `runs`

One row per run (M13, ADR-050) — survives a `control-api`/`agentctl` restart, unlike the
prior in-memory run map. Real schema (`internal/store/server.go:48-51`); the RPC
`RunRecord` (`proto/store.proto:15-28`) carries the same 11 columns plus a read-only
`found` (not stored in the DB — a "no such run" signal on `GetRun`):

| Column | Type | Description |
|--------|------|-------------|
| `run_id` | TEXT | Unique run identifier (primary key) |
| `conversation_id` | TEXT | The runs<->chats join (M13) — previously lived only in argv and was lost on restart |
| `mode` | TEXT | `explore` \| `goal` \| `describe` \| `replay` \| `baseline` \| `chat` |
| `target` | TEXT | AUT URL |
| `planner` | TEXT | Planner used |
| `state` | TEXT | `running` \| `done` \| `failed` |
| `exit_code` | INTEGER | The brain process's exit code |
| `artifact_dir` | TEXT | Path to the run's artifact directory |
| `error` | TEXT | Error text if the run failed |
| `started_at` | TEXT | Run start time (RFC3339) |
| `finished_at` | TEXT | Run completion time (RFC3339; empty while running) |

Detailed step/coverage results and token metrics live not in `runs` but in the
neighbouring M13 domains `results` and `metrics` (`proto/store.proto`), not documented
in depth here.

---

> **Note:** the `run_transcripts` and `page_object_cache` tables do not exist in the
> store-gateway (neither in the legacy heal/trust schema nor in the 6 M13 domains). The
> LLM transcript (`llm-transcript.jsonl`) is written to disk in the run's artifact
> directory (`brain/__main__.py:_run_explore`) with no separate index table; there is no
> code in the repository that generates a `page_object_cache`.

---

## Storage Rationale

### Why SQLite WAL for the main store

- **Zero operational burden:** no daemon, no network port, no cluster — a single
  file. Backup is `cp` or `sqlite3 .dump`. Appropriate for 1–10 concurrent
  single-host runs.
- **Single-writer model:** Go's exclusive write ownership via the `store-gateway`
  serialises all mutations. WAL mode allows unlimited concurrent readers
  (`control-api`, `agentctl`) without writer blocking.
- **Schema portability:** the schema is written to be Postgres-compatible. The
  migration when Postgres is introduced is a driver change in `store-gateway`
  — no schema rewrites.

### Why the checkpoint DB is separate

Keeping the LangGraph checkpointer (`SqliteSaver`/`PostgresSaver`) in its own file/DB
makes the ownership contract unambiguous: Go store-gateway is the sole writer of the
main database, Python brain is the sole writer of the checkpoint database, TypeScript
`pw-executor` writes nothing to either. Two independent single-writer guarantees,
verified by inspection rather than convention.

### Postgres: checkpoint DB vs. the store-gateway's main DB

For the **brain's checkpoint DB**, switching to Postgres is already implemented and is
operator-controlled via the `CHECKPOINT_DSN` environment variable
(`brain/__main__.py:_checkpointer`, M5-3): when set, the brain uses a synchronous
LangGraph `PostgresSaver` instead of `SqliteSaver`, with a one-time `saver.setup()` call
on connect. This is **not** `AsyncPostgresSaver` — the brain's loop is synchronous.

For the **store-gateway's main DB**, Postgres is not implemented: `STORE_DSN` is
recognized and **explicitly refused** with a startup error
(`internal/store/server.go:96-99`, "the Postgres backend is deferred to M13-service")
rather than silently falling back to SQLite. Switching the driver from
`modernc.org/sqlite` to Postgres is M13-service work (M11/ADR-053); the schema is
already written portably for that transition.

### Checkpoint GC

There is **no** automatic checkpoint-DB cleanup in the store-gateway: the Go
store-gateway (`internal/store/server.go`) never reads or deletes `checkpoint.db` or
`state/conversations.db` — those files belong exclusively to the Python brain's
checkpointer (LangGraph `SqliteSaver`/`PostgresSaver`), not to the store-gateway. The
per-run `<artifact_dir>/checkpoint.db` is naturally bounded by the lifetime of the run's
artifact directory; `state/conversations.db` is the shared, non-ephemeral DB (see above)
and is not pruned automatically today. Managing its growth (archiving/rotating old
conversation threads) is an operational concern not implemented in the store-gateway's
code.
