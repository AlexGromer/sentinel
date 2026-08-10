# Sentinel — Outputs and Artifacts

> 🌐 [Русский](OUTPUTS.md) (основная версия) · **English**

Derived from the design synthesis 2026-06-23; canonical summary in ../ARCHITECTURE.md.

---

## Overview

Each Sentinel run emits a deterministic set of artifacts; the exact set depends on the run
mode (explore vs. replay/baseline) — see the "As-built" notes on each entry below. All
artifacts land in `ARTIFACT_DIR/runs/{run_id}/` unless noted. The JSON artifacts are
machine-readable for CI tooling; the HTML artifacts are human-facing.

> Two catalog entries (the `sitemap.json` coverage map and the optional SARIF export) are
> marked below as **as-built: not implemented** — kept in the catalog as known gaps rather
> than removed, so the request history isn't lost.

---

## Artifact Catalog

### 1. Frozen Exploration Plan (`plan.json`)

The primary output of an `--explore` run and the primary input for every subsequent
`--replay` or `--ci` run.

Schema (real top-level keys, `brain/graph.py:401-416`): `{plan_id (UUID), plan_hash (SHA-256 of
canonical JSON over steps[] with sorted keys; numbers serialised as-is, no rounding, no field
excluded), target_url, run_mode, coverage_target, coverage_achieved, interactive_seen (int),
interactive_exercised (int), steps[], tokens (from `budget.tracker().summary()`), models
({"plan": <planner model name>})}`. Every `steps[]` object has exactly 8 keys: `step_id`,
`intent`, `semantic_id`, `action_type`, `target`, `locator`, `alternatives` (a flat list of
`{strategy, locator, prior}`, not an `L1..L6` map), `is_milestone`.

**As-built:** `aut_version` and `exploration_seed` are **not** part of `plan.json` — no such
fields exist in the code. `golden_snapshots` is not embedded in `plan.json` either — the
baselines live separately, in the SQL `golden_snapshots` table (see §5), and are only written
by the explicit `agentctl baseline update` command.

`plan.json` is committed to the application repository and drives all replay runs. It
is the machine-readable equivalent of a hand-authored exploratory test script, but
discovered autonomously and verifiable by hash. Any mutation to `plan.json` outside of
an explicit `agentctl baseline update` causes a hard-abort (exit code 3) on the next
replay.

---

### 2. Run Report (`report.json` + `report.html` + `metrics.prom`)

The primary CI-consumption artifact, emitted at the end of every run.

**As-built:** the names `run_report.json`/`run_report.html` do not exist in the code. In
reality, `brain/report.py::generate()` (`brain/report.py:88-97`) reads `heal-report.json`
(the **replay** run's artifact; a baseline run writes `baseline-report.json`) and writes three files: `report.json`, `report.html`,
and `metrics.prom` (Prometheus textfile format, node_exporter textfile-collector).

**JSON** is machine-readable: it drives the process exit code and is parseable by any CI
system without custom tooling.

**HTML** is a self-contained page with inline CSS (`brain/report.py::_html()`); it does not
mirror any third-party Playwright HTML reporter format.

What is actually present in `report.json`/`report.html` (per the `heal-report.json` fields,
see `brain/replay.py`): per-step status (`ok` / `healed` / `failed`) with a `quarantined`
flag; a per-step heal-audit record (`heal: {strategy, confidence, outcome}`); a
`regressions` list (golden-diff on a11y/screenshot); the summary `healed`/`failed`/`exit_code`;
and the `tokens`/`models` blocks (see §8). There is no per-LangGraph-node cost breakdown, no
separate coverage map, and no pending-human-gate-decisions list in this schema.

---

### 3. Exported Playwright Test Code (`.spec.ts`)

A TypeScript Playwright test file generated from `RunState.executed_actions`.
**Implementation:** `brain/exporter.py` (Python, deterministic; ADR-014). The target architecture
"a Go `report-service`" named in that same ADR-014 never arrived: `cmd/report-service` was removed
2026-08-09, having never run a single day (ADR-119, QA-REPORT-SERVICE) — the Python generator
remains the sole, permanent implementation, not an interim one.

The generated code uses idiomatic Playwright patterns (`test()` / `expect()`),
role/text/label-preferred locators (matching the healing strategy hierarchy), and
per-URL-pattern page objects.

This artifact is **deliberately independent of any MCP codegen tool**: it is generated
from recorded action data (`RunState.executed_actions`), not by asking `pw-executor` to emit code.
This eliminates a fragile MCP dependency flagged by architecture judges.

The `.spec.ts` is the handoff artifact to the existing `qa-automation-engineer`
workflow: Sentinel generates the skeleton; engineers own and maintain it thereafter.

---

### 4. Playwright Trace (`trace.zip`)

A browser execution trace: network activity, console output, DOM snapshots, screenshots, and the
complete action timeline. **It is NOT kept for every run** (ADR-084): a green run has nothing to
dissect, and the file holds the tested application's live DOM and request bodies, so `trace.zip`
survives only when `exit_code != 0` (`brain/__main__.py::_keep_trace`). Levers:
`SENTINEL_TRACE_ALWAYS=1` restores the old behaviour, `PW_NO_TRACE=1` beats both — then no trace is
recorded at all.

Produced by `pw-executor` and written to the shared artifact directory; served through control-api's
artifact whitelist (`GET /v1/runs/{id}/artifact?name=trace.zip`, ADR-099) or read directly from the
run directory — there is no separate HTTP service for this anymore (`report-service` removed,
ADR-119). Viewable locally with:

```bash
playwright show-trace trace.zip
```

This is the primary artifact for debugging CI failures. No custom trace infrastructure
is required; the Playwright trace format is the source of truth.

**Access & retention (#26, THREAT_MODEL ❹).** `runs/` and each `runs/<id>/` are created `0700`
(owner-only): `trace.zip` may hold AUT DOM/screenshots (PII), so other local users can't read it.
Retention: on every run `agentctl` keeps `trace.zip` only for the newest `SENTINEL_TRACE_KEEP` runs
(default 10; a value `<0` disables count-pruning; a value `0` keeps zero newest = deletes **all**
`trace.zip` on every run) and deletes any `trace.zip` older than
`SENTINEL_TRACE_TTL_HOURS` (default `0` = TTL off). **Only** `trace.zip` is removed — `plan.json` and
reports stay for the audit trail. Applied only to the default `runs/`, never a user `--artifact-dir`.

**Contents (ADR-098).** A kept trace is a redacted trace, or there is no trace at all. The hook sits
where the trace is KEPT (`_stop_trace`), not in report generation: the report is built later, and a
replay started directly (`python -m brain`) never reaches it — a redaction that depends on how the
run was launched is the worst property a protective mechanism can have. Redacted are the values of
input verbs (unconditionally, whatever the field is called), driver narratives (by `callId`, never by
text), `__playwright_value_` in DOM snapshots, and named secrets across every non-image entry.
**Fail closed:** a trace that could not be redacted is DELETED — that is not a degraded artifact, it
is a leak. The emergency `SENTINEL_TRACE_RAW=1` announces itself EVERY time. To redo it by hand on an
archive already on disk: `agentctl redact-trace --trace <trace.zip>`. ⚠ The window between Playwright
writing the archive and the redaction is stated, not closed: there is no hook between "the bytes hit
the disk" and "we can read them", so the raw file lives for the length of one subprocess.

**Pixels are never cleaned** — OCR plus masking is unreliable and expensive, and a redactor that
half-works on screenshots is worse than one that says it does not try. The lever:
`SENTINEL_TRACE_SCREENSHOTS=0` (`pw-executor/src/server.ts`, at `tracing.start`) — frames are not
recorded at all.

**The sweep does not stay silent.** Every retention deletion leaves a `trace-removed.json` in the
swept run's own directory (with the reason — `count` or `ttl` — and the `keep`/`ttl_hours` in force);
the file is served through the artifact whitelist, so the hub shows "a trace existed and is gone"
instead of indistinguishable emptiness. If the deletion failed, the marker is NOT written: it must
mean "the trace is gone", not "we tried".

**Viewing is not downloading.** The hub reads artifacts on every run open, so "delete once served"
would erase a run just for being looked at. The distinguishing primitive is `?download=1`, which the
hub sets ONLY on a download-button click (the run's artifact panel and the ADR-108d artifact canvas)
and never on a prefetch for rendering: a genuine download writes a `downloaded.json` marker and
nothing else. Deleting the marked runs is a separate explicit command, `agentctl sweep-downloaded
--yes` (ADR-103), never automatic; `--dry-run` shows without acting.

---

### 5. Regression Golden Baselines

Per-page accessibility hash (`a11y_hash`) and screenshot hash (`screenshot_hash`), captured
at first landing on that page.

**As-built:** stored only in the SQL `golden_snapshots` table (`brain/store.py:68-70`, local
SQLite) — **not** embedded in `plan.json` (the real `plan.json` schema has no such key, see
§1). They are written **only** by a baseline run (`agentctl baseline update`,
`RUN_MODE=baseline` → `brain/replay.py:253-266`, `save_golden()` fires only `if baseline:`) —
a plain explore run never writes them. They are immutable between baseline runs: no CI
replay or automated heal can update them. The only mutation path is the explicit operator
command:

```bash
agentctl baseline update --plan <plan.json> [--target <URL>]
```

This command runs a replay and, on first landing on each page, overwrites that page's row
in `golden_snapshots` (`INSERT OR REPLACE`) with the current hashes. **As-built:** `plan_hash`
is not recomputed, `plan.json` is not touched, and there is no versioning/archiving of the
previous record (`superseded_by`) in the code — the "new" golden simply replaces the old SQL
row. This structure still makes "CI rewrote its own baseline" architecturally impossible:
writing `golden_snapshots` is only reachable through this separate, explicitly-invoked
operator path.

The dual-hash design (a11y + screenshot) catches visual-only regressions (CSS / layout
changes) that a11y-blind diffing cannot detect, surfacing them as `VISUAL_WARN` events (gates
`exit 2` only when `SENTINEL_VISUAL_AUTHORITATIVE=1`, see `DETERMINISM.md`). No separate
tarball-export mechanism for the baselines was found in the code.

---

### 6. Healing Audit (`healing_audit` table, SQLite)

**As-built:** there is no separate `healing-audit.jsonl` file in the code, and no
`agentctl healing report` command exists (grep across `brain/` and `cmd/` — zero matches).
Every heal attempt during a run is appended (`INSERT` only, never `UPDATE`/`DELETE`) to the
SQL `healing_audit` table (`brain/store.py:64`, `LocalStore.audit()`, `brain/store.py:145-152`).

**Real row schema** (`brain/store.py:58-67`): `{run_id, step, semantic_id, page_path,
strategy, original, healed, confidence, outcome, dom_hash, ts}`.

The accumulated history is queryable via `agentctl calibrate` (`brain/store.py:154-155`,
`LocalStore.audit_rows()` reads `strategy, outcome, confidence` from `healing_audit`) —
computing precision/recall of past auto-heals; the forensic trail for understanding locator
drift history stays in that same table (SQL, not a separate JSONL artifact).

---

### 7. LLM Transcript (`llm-transcript.jsonl`)

A per-explore-run JSONL file recording every planner decision
(`brain/__main__.py:113-115`, `tx_write()`; populated from `brain/graph.py:225-245,390-392`).

**As-built — real record schema:** exactly `{step, planner, model, decision, reason,
prompt_tokens, completion_tokens}`. The fields `ts`, `run_id`, `node`, `latency_ms`,
`cost_usd`, `temperature` do not exist in the record.

Written line-by-line (`tx.write(...)`, `tx.flush()`) as the planner is called; `decision`
is either `"done"` (when explore finishes) or the `intent` text of the chosen action.
Prompt content is not stored in the file; the prompt hash (`prompt_HASH`, `brain/otel.py`)
only appears in OTel span attributes, not in this JSONL — so secrets embedded in page state
never reach stored artifacts.

Enables offline debugging of planner decisions and a line-by-line cross-check against
`plan.json`.

---

### 8. Token Summary (the `tokens` field — not a separate file)

**As-built:** there is no separate `cost_report.json` in the code (grep across `brain/` —
zero matches). The per-run token summary is the `tokens` field embedded in already-existing
artifacts: `plan.json` (`brain/graph.py:412-413`,
`plan_obj["tokens"] = budget.tracker().summary()`, for explore) and
`heal-report.json`/`report.json` (`brain/replay.py:302-303`, the same
`budget.tracker().summary()`, for replay/baseline).

**`tokens` schema** (`brain/budget.py:52-58`): `{prompt, completion, total,
plan: {prompt, completion}, heal: {prompt, completion}}`. A `models` field is written
alongside it (`{"plan": <planner model>}` in `plan.json`, `{"heal": <heal-backend model>}`
in `report.json`).

Pricing (`cost_usd`) is not computed in these artifacts — per the comment in
`brain/budget.py`, these token counters are meant for downstream pricing on the Go
control-API side, not in the brain itself. No stdout cost table, `cost_by_node`, or
`runs_this_week_cost` field was found in the code.

---

### 9. Coverage Sitemap — as-built: not written as a file

**As-built:** `sitemap.json` is not created anywhere in the code (grep across `brain/` —
zero matches). Explore does build an in-memory site map (`site_map` in `RunState`,
accumulated in the `ground()` node in `brain/graph.py`, consumed by the `scenario_head`
node, ADR-028) — but it is never serialised to disk as a separate `sitemap.json`. `plan.json`
only stores integer counters, `interactive_seen` and `interactive_exercised` (see §1), not a
per-page graph.

Today the only source for reconstructing the explored surface of the AUT is `plan.json`
itself (`steps[]`, from which the navigation route can be reconstructed) or `trace.zip`;
there is no separate coverage-map artifact.

---

### 10. SARIF Report — as-built: no `--sarif` flag in the code

**As-built:** `cmd/agentctl/main.go` has no `--sarif` flag on any subcommand — grep across `cmd/`
and `brain/` returns zero matches, and the full flag list for `run` does not include it. A SARIF
export for GitHub Code Scanning is a proposed but unimplemented capability; today `report.json` (§2)
can serve as the source for an external conversion script if one is needed.

⚠ The subcommand list is deliberately gone: a hand-kept list of names is itself a defect (principle
5, `docs/DEVELOPMENT.md` §0) — what is surplus in it is visible, what is missing is not. The
seven-name list that stood here was seven subcommands behind by August (the storage/leak arc and
HEALTH), three of them destructive. The authoritative list is printed by the product: `agentctl` with
no arguments calls `usage()` (`cmd/agentctl/main.go`), whose completeness rests on a gate rather than
on care — `TestUsageListsEverySubcommand` (`cmd/agentctl/main_test.go`) reads the `case "…"` labels
out of `switch os.Args[1]` and fails on any dispatched subcommand missing from usage(). Thin clients
over control-api routes (ADR-107) are appended by `apiUsage` in the same function. The capability
catalog with access paths is `docs/capabilities.json`.

---

### 11. Run logs (`runs/<id>/logs/`) — ADR-065/067/068

Three files split by **audience** rather than by severity (`cmd/control-api/logsink.go`):

| file | what it is | where it is read |
|---|---|---|
| `logs/events.jsonl` | AG-UI frames — the run's **narrative** ("step 2 of 40", "click Sign in", the healing strategy and outcome) | the Live view |
| `logs/run.jsonl` | structured **diagnostics**: level · category · module · step · source | the Logs view |
| `logs/run.log` | the raw stdout+stderr stream, 1:1 | the file, `grep` |

Russian text does **not travel on the wire**: the server sends a `code`, the page takes the phrase from
`/v1/events-catalog`, and recovers placeholder values by matching the English template against the
rendered string. Collapsing repeats (`×N`) and nesting stack frames happen on the READ side
(`handleRunLogs`); writes are immediate.

**As-built — important limits:**
- **`logs/*` are NOT in the artifact whitelist** (`cmd/control-api/main.go`, `artifactWhitelist`). They
  cannot be fetched through `GET /v1/runs/{id}/artifacts/{name}` — only through
  `GET /v1/runs/{id}/logs` (token-gated, with level/source/step filters and the ADR-068 expression
  parser).
- **The application channel is capped at 500 records**; on truncation `app.log_capped` is printed, so a
  truncated capture cannot be mistaken for a complete one.
- **`app.*` events never reach the verdict** — a run reports `exit 0` while the application throws
  exceptions (`GAP-PROD-001`, analysed in [`REGRESSION_MAP.en.md`](REGRESSION_MAP.en.md) §6).
- **Write-side redaction EXISTS** (`GAP-SEC-005` closed by ADR-081, see
  [`THREAT_MODEL.en.md`](THREAT_MODEL.en.md) §4.12): `logSink.write` is the single choke point all
  three files descend from, and it passes EVERY line through `redact.Line`
  (`cmd/control-api/logsink.go`) — `Bearer` form, JWTs and named secrets via `configguard.Secretish`,
  the vocabulary shared with the config guard. There is deliberately no entropy or "long hex/base64"
  rule: it would eat `run_id`/`plan_hash`/`dom_hash`/goldens, the audit trail itself. An unnamed,
  shapeless secret survives redaction — a stated ceiling, not an oversight.
- **`logs/` retention exists but defaults to OFF** (`SENTINEL_LOG_KEEP`/`SENTINEL_LOG_TTL_HOURS`,
  both `0` = off; `cmd/agentctl/main.go::sweepLogs`, exposed as `/v1/config` fields in the
  `retention` group), unlike `trace.zip`, whose sweep runs on its own. The logs ARE the diagnosis,
  and silently deleting someone's evidence is a worse failure than a pile of files; redaction removes
  the credentials, retention is disk hygiene the operator opts into.

### 12. The store marker on list endpoints — ADR-069

Five list endpoints (`scenarios`/`tests`/`chats`/`results`/`trends`) carry `store: false` plus a
`store_reason` naming the remedy (in the compose stack `docker compose up` starts one; elsewhere run
`store-gateway` and set `CONTROL_API_STORE_ADDR`) beside the data. A `501`
would be wrong here: an empty list is **valid** with a live store. Before ADR-069 an empty `200` meant
both "nothing saved yet" and "this deployment saves nothing at all" — and "the library will not load"
was a correct reading of a silent interface.
