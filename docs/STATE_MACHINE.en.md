# State Machine — Sentinel

> 🌐 [Русский](STATE_MACHINE.md) (основная версия) · **English**

Derived from the design synthesis 2026-06-23; canonical summary in ../ARCHITECTURE.md (see §7).

> **Note on models:** the model names in this document (Opus 4.8 / Sonnet 4.6) are **per-role defaults**; planner/heal are provider-agnostic since M6 (ADR-019) — any backend via `LLM_BACKEND*` (Anthropic or OpenAI-compatible). `HeuristicPlanner` stays the deterministic anchor.

---

## 1. Framework

The Sentinel cognitive loop is implemented as a **LangGraph `StateGraph`** (Python).
All in-flight state is persisted between node invocations by a **`SqliteSaver` checkpointer**
that writes to a *separate* SQLite file from the Go `store-gateway` main database.
This separation is what makes the "single-writer" guarantee over the main DB actually hold.

| Concern | Detail |
|---|---|
| Framework | LangGraph `StateGraph` (Python, `langgraph` package) |
| Checkpoint store | `langgraph.checkpoint.sqlite.SqliteSaver` |
| Checkpoint DB path | `<artifact_dir>/checkpoint.db` — one SQLite file per run (`brain/__main__.py:135`), never the store-gateway file |
| Thread identity key | `thread_id = run_id` |
| Production DB (`CHECKPOINT_DSN`) | Synchronous `PostgresSaver` (package `langgraph.checkpoint.postgres`) replaces `SqliteSaver` when `CHECKPOINT_DSN` is set — same interface, schema unchanged (`_checkpointer`, `brain/__main__.py:26-38`) |
| Browser execution layer | **`pw-executor`** — our own TypeScript server implementing MCP/JSON-RPC 2.0 over stdio (built, not bought; replaces any off-the-shelf browser MCP server) |

> **Two independent execution paths.** This `StateGraph` drives `explore` and `chat` mode
> (including `goal`/`describe` via the `scenario` node, ADR-028/ADR-048) — there is NO branching
> on `run_mode` inside the graph (`graph.py:463-475`: edges are either unconditional or routed by
> 4 router functions, none of which reads `run_mode`). `replay` and `baseline` mode never go
> through this graph at all — they run a separate loop, `run_replay()` (`brain/replay.py`),
> dispatched from `_run_replay()` (`brain/__main__.py:514`). The real locator-healing engine
> (`HealingEngine.heal`, cache + strategies + LLM + visual mode) runs **only** in that loop —
> see §3.11.

---

## 2. Shared State Object — `RunState` (TypedDict)

`RunState` is the single shared object threaded through every node (`brain/state.py:10-63`,
`TypedDict total=False`, 33 fields). All fields, except the internal `_`-channels, are checkpointed
at each `checkpoint` node invocation.

> The table below lists ONLY the fields actually declared in `RunState`. An earlier version of this
> document described fields from an unbuilt design (`session_id`, `aut_version`, a detailed
> `PageModel` with `a11y_tree`/`landmarks`/`forms`/`completeness_ratio`/hashes, `episodic_buffer`,
> `healing_context`/`heal_attempts`, `token_usage`/`token_budget`/`budget_warning_emitted`,
> `human_gate_*`, `run_dir`/`artifacts`/`step_failures`) — none of these exist in `brain/state.py`
> or anywhere else in the tree (verified by `grep`); they have been removed from the table below.

### 2.1 Identity and Configuration

| Field | Description |
|---|---|
| `run_id` | Run identifier; doubles as the LangGraph checkpointer's `thread_id` |
| `run_mode` | `str`; observed values are `"explore"` (`_run_explore`) and `"chat"` (`_run_chat`) — both run through the SAME graph. No router function in the graph reads `run_mode` |
| `target_url` | Root URL of the application under test |
| `base_origin` | Target origin — filters `nav_frontier` to same-origin |
| `coverage_target` | Fraction of discovered buttons that must be clicked before convergence. Default `0.85` |
| `max_steps` | Hard cap on exploration steps |
| `artifact_dir` | Per-run artifact directory (`plan.json`, `checkpoint.db`, LLM transcript) |
| `goal` | NL goal for `GoalPlanner` (goal mode); `""` in pure explore |
| `describe` | NL flow description for `DescribePlanner` (describe mode); `""` otherwise |

### 2.2 Conversation and Site Map (ADR-028 / ADR-048)

| Field | Description |
|---|---|
| `messages` | Conversation accumulator (`Annotated[list, add_messages]`) — LangGraph's reducer appends turns across calls. Empty for one-shot explore/goal/describe runs |
| `site_map` | Site-wide element map `page_path -> [element]`, accumulated by `ground` over the whole explore walk |
| `phase` | `"explore"` \| `"scenario"` |
| `scenario_steps` | Grounded steps appended by the `scenario` node into `exploration_plan` |
| `scenario_unmatched` | Draft steps/refs that could not be grounded to a real element |

### 2.3 Perception

| Field | Description |
|---|---|
| `current_url` | URL currently loaded in the browser |
| `page_model` | Dict assembled by `perceive`/`ground`: `{url, title, aria, nodeCount}` plus `buttons` (added by `ground`). **Does not** contain `a11y_tree`/`landmarks`/`forms`/`completeness_ratio` or any hashes — the code never computes those subfields (`graph.py:156-202`) |

### 2.4 Exploration Accounting / Convergence

| Field | Description |
|---|---|
| `exploration_plan` | Ordered list of planned/executed steps |
| `plan_hash` | SHA-256 of canonical JSON of the steps — computed in the `report` node, **not** in `plan` |
| `current_step` | Number of the last executed step |
| `interactive_seen` | `semantic_id`s of all discovered buttons |
| `interactive_exercised` | `semantic_id`s of buttons that have been clicked |
| `interactive_failed` | **ADR-070** — `semantic_id` → how many times acting on the element RAISED. It exists because `act` marked an element only on SUCCESS: a control that would not act stayed a candidate forever and the planner proposed it every step (a live log showed one click 34 times until `max_steps`). At the `_EXPLORE_FAIL_LIMIT` threshold (env `SENTINEL_EXPLORE_FAIL_LIMIT`, default 2) the element leaves the candidate set; the same budget covers navigate candidates |
| `visited_paths` | Page paths visited |
| `nav_frontier` | Unvisited same-origin links |
| `coverage_achieved` | `len(exercised) / max(1, len(seen))` |
| `exploration_complete` | Set by the `plan` node when `current_step >= max_steps`, or no candidates remain, or (`coverage_achieved >= coverage_target` AND `nav_frontier` is empty) — or the planner itself proposed `done` |
| `executed_actions` | Flat log of executed actions `{step_id, type, ok}` |
| `errors` | List of `act` error strings |

### 2.5 Operator Takeover (ADR-054)

| Field | Description |
|---|---|
| `takeover_returns` | Operator return payloads — one entry per takeover→return cycle. Observability only: not part of `plan.json`/`scenario.json`, does not affect `plan_hash` |

### 2.6 Auto-HITL (ADR-055)

| Field | Description |
|---|---|
| `consecutive_heal_failures` | Counts consecutive misses of the `heal` node (in the explore graph `heal` is a stub, see §3.6, so EVERY entry is a miss); reset to 0 on a successful `verify`. Reaching `SENTINEL_AUTO_HITL_THRESHOLD` auto-arms `_takeover_armed` in the `checkpoint` node |
| `failed_steps` | Running total of `verify` failures (observability / M15-metrics substrate) |

### 2.7 Internal Channels

| Field | Description |
|---|---|
| `_pending` | The action planned but not yet executed (bridge from `plan` to `act`) |
| `_last_ok` | Result of the last `act` (bool) |
| `_verify_ok` | Result of `verify` — see §3.5 |
| `_takeover_armed` | Armed by `checkpoint` when an operator takeover was requested or the auto-HITL threshold fired; routes to the `takeover` node |

### 2.8 Token Budget — NOT a `RunState` field

Token accounting is a **process-global** `BudgetTracker` (`brain/budget.py`), not a `RunState`
field: the planner and `HealingEngine` read `budget.tracker()` directly. Limits come from env:
`PLAN_TOKEN_LIMIT` (default 50000), `HEAL_TOKEN_LIMIT` (default 20000), `TOTAL_TOKEN_LIMIT`
(default 0 = off). On reaching a limit, `exceeded(role)` simply returns `True` — the caller (the
planner / `HealingEngine._llm_reground`) degrades silently, with no dedicated AG-UI or "warning"
log event. No `BUDGET_WARNING` event exists in the code (`grep` across the tree — 0 matches).

---

## 3. Nodes

The graph has **10 named nodes** (`brain/graph.py:454-459`: `perceive`, `ground`, `plan`, `act`,
`verify`, `heal`, `checkpoint`, `takeover`, `scenario`, `report`) plus the two implicit LangGraph
built-in nodes (`START`, `END`). There is NO branching on `run_mode` inside the graph — every edge
is either unconditional or resolved by one of 4 router functions (`route_entry`, `route_plan`,
`route_verify`, `route_checkpoint`), none of which reads `run_mode`. The same graph serves explore,
`chat` (multi-turn, ADR-048), and goal/describe (when a `scenario_head` is wired, ADR-028).
The `checkpoint` node, when `_takeover_armed` is set, routes to the dedicated `takeover` node
(unconditional `interrupt()` → pause, operator takes over → `Command(resume)`; precedence
**abort > takeover**). The framework wires `START` → first node (via `route_entry`) and `END` as
the graph terminal automatically.

`replay`/`baseline` mode does not use this graph at all — it runs as a separate loop, see §3.11.

### Node summary

| # | Node | LLM | Notes |
|---|---|---|---|
| 1 | `perceive` | No | Snapshots the page (`browser.currentUrl` + `browser.snapshot`) |
| 2 | `ground` | No | Catalogues interactive elements, grows `nav_frontier`/`site_map`, computes coverage |
| 3 | `plan` | Conditional | Depends on `planner`: `HeuristicPlanner` is deterministic; an LLM planner — yes |
| 4 | `act` | No | Executes `_pending` via `pw-executor` |
| 5 | `verify` | No | One-line pass-through `ok = bool(_last_ok)` — NOT a classifier |
| 6 | `heal` | No | **Stub** in the explore graph — log + counter; real heal only in `run_replay()` (§3.11) |
| 7 | `checkpoint` | No | Polls the orchestrator (abort/takeover) + checks the auto-HITL threshold |
| 8 | `takeover` | No | `interrupt()` — pause for an operator takeover |
| 9 | `scenario` | Conditional | Only if a `scenario_head` is wired (goal/describe); also the warm-chat resume entry point |
| 10 | `report` | No | Terminal node; freezes `plan.json` + `plan_hash` |

### 3.1 `perceive`

**LLM: None.**

- Calls `pw-executor`: `browser.currentUrl` + `browser.snapshot`, assembles
  `page_model = {url, title, aria, nodeCount}`.
- On the very first pass (`page_model` still empty) emits AG-UI `run.started`; always emits
  `state.transition(to="perceive")`.
- **Does not compute** `completeness_ratio`, `a11y_hash`, `screenshot_hash`, `dom_subtree_hash`,
  and does not decide whether to call `screenshot()` — none of those mechanisms exist in the code
  (`grep completeness_ratio` finds only design-document mentions, no implementation).
- **Does not manage** starting/stopping the Playwright trace — the trace is stopped exactly once,
  after the graph finishes, in `brain/__main__.py` (`browser.traceStop`), not per `perceive` call.

### 3.2 `ground`

**LLM: None.**

- Catalogues interactive elements (`_elements_from_interactives`, `graph.py:54-102`): role, name,
  `testid`, a primary locator plus an ordered `alternatives` list (`testid`/`role_name`/`label`/
  `text_role`, priors 0.95/0.90/0.88/0.80).
- Coverage is computed over buttons only (`role == "button"`); links feed `nav_frontier`.
- Grows `site_map[path]` with the FULL element set (buttons + links + form fields) — the map the
  `scenario` node consumes (ADR-028).
- Recomputes `interactive_seen`, `nav_frontier` (same-origin only, not yet visited),
  `visited_paths`, `coverage_achieved = exercised / max(1, seen)`.
- **Does not validate** against a golden baseline (`a11y_hash`/`screenshot_hash`) — that check does
  not exist in the explore graph; golden-diff is implemented only in `run_replay()` (§3.11).
- Unconditional edge `ground → plan` (`graph.py:464`) — no branching on `run_mode`.

### 3.3 `plan`

**LLM: conditional** — depends on the `planner` that was wired in (`HeuristicPlanner` is
deterministic; an LLM-backed planner, or `GoalPlanner`/`DescribePlanner` for phase 1, are selected
in `_run_explore`, `brain/__main__.py:102-108`).

- Assembles candidates: unexercised buttons (`click`) plus the whole `nav_frontier` (`navigate`).
- Concludes exploration (`exploration_complete=True`) when `current_step >= max_steps`, or no
  candidates remain, or (`coverage_achieved >= coverage_target` AND `nav_frontier` is empty) — or
  when the planner itself returned `decision.get("done")`.
- Otherwise asks `planner.propose(...)` for the next action, appends it to `exploration_plan`, and
  places it in `_pending` (bridge to `act`).
- Calls `rc.report(...)` (RunControl); if the orchestrator returned `ABORT`, converges immediately
  (`exploration_complete=True`) without waiting for `checkpoint`.
- **Does NOT freeze** `plan.json` and **does NOT compute** `plan_hash` — that is the `report`
  node's job (§3.10), not `plan`'s.
- Writes an LLM-transcript record (`tx_write`) on every step, regardless of outcome.

### 3.4 `act`

**LLM: None.**

- Executes `_pending` via `pw-executor` (`navigate`/`click`/`fill`/`type`/`select`/`press`/
  `assert`); for `fill`/`type`/`select`/`assert` it reuses the `replay.py:_act`/`_expect_params`
  dispatcher — a single source of truth shared between explore and replay.
- On success: marks the button `exercised` (only for `click`), appends to `executed_actions`,
  sets `_last_ok=True`.
- On an exception: appends a string to `errors`, sets `_last_ok=False`.
- Emits AG-UI `tool.call` and `step.progress`. Unconditional edge `act → verify`.

### 3.5 `verify`

**LLM: None.**

- A one-line pass-through: `ok = bool(state.get("_last_ok", True))`.
- **Does NOT classify** the outcome into `PASS`/`LOCATOR_STALE`/`ELEMENT_GONE`/`TIMING`/
  `UNEXPECTED_ERROR` — those categories do not exist in the code (`grep` across the tree — 0
  matches); there is no re-snapshot of the page and no LLM call for soft assertions.
- On `ok=True`: resets `consecutive_heal_failures=0` — the ONLY reset point for the auto-HITL
  counter (ADR-055).
- On `ok=False`: increments `failed_steps`.
- Route (`route_verify`, `graph.py:428-429`): `"checkpoint" if ok else "heal"` — exactly 2 outcomes.

### 3.6 `heal`

**LLM: None — STUB in the explore graph.**

> Node docstring in the code: *"STUB in the explore graph (explore discovers, it does not heal).
> See brain/replay.py."*

- Logs and increments `consecutive_heal_failures` — the node physically cannot repair a locator:
  there is no cache, no strategy rotation, no LLM re-grounding, no visual mode here.
- Every entry into this node is a miss by definition; used as the auto-HITL signal (§2.6).
- The real healing engine (`HealingEngine.heal`: cache + strategies + LLM + visual set-of-marks)
  runs **only** in the separate `run_replay()` loop — see §3.11.
- Unconditional edge `heal → checkpoint`.

### 3.7 `checkpoint`

**LLM: None.**

- `rc.poll(run_id, "checkpoint")` — polls the Go orchestrator (RunControl gRPC, ADR-054):
  - `ABORT` → `exploration_complete=True`, clears `_takeover_armed` (converges immediately;
    **abort takes precedence over takeover**).
  - `TAKEOVER` → arms `_takeover_armed=True` (the actual pause happens in the next node,
    `takeover`, not here — the decision is latched into state so `interrupt()` replays
    deterministically on resume).
  - otherwise: if `consecutive_heal_failures >= SENTINEL_AUTO_HITL_THRESHOLD` (env, default `0` =
    off) — also arms `_takeover_armed=True` and emits AG-UI `hitl_needed`.
- There is **no** `store-gateway` write here, **no** `PersistenceService` call, **no** reset of
  `heal_attempts`/`healing_context` (neither field exists) — the actual LangGraph checkpoint is
  flushed by the framework itself (the compiled graph with `checkpointer=saver`), not by this
  node's code.
- Route (`route_checkpoint`, `graph.py:431-438`) — exactly 3 targets: `exploration_complete →
  scenario`; else `_takeover_armed → takeover`; else `current_step >= max_steps → scenario`,
  else `→ perceive`.

### 3.8 `takeover`

**LLM: None.**

- The only node calling `interrupt({"reason": "operator_takeover", "run_id": ...})` —
  unconditionally (the decision was already latched into `_takeover_armed` by `checkpoint`, so a
  repeat entry on resume is safe and idempotent).
- `app.invoke()` returns control with `__interrupt__`; the live browser is handed to the operator
  (CDP, M9-LIVE). The orchestrator sends `Command(resume=...)` on Return — the run continues from
  the same point (`_resume_through_takeovers`, `brain/__main__.py:61-85`).
- On resume: clears `_takeover_armed`, appends the return payload to `takeover_returns`.
- Unconditional edge `takeover → checkpoint` (re-polls before continuing, in case the Return has
  not yet propagated to the orchestrator).

### 3.9 `scenario`

**LLM: conditional** — only if a `scenario_head` was wired into the graph (`GoalPlanner` for goal
mode, `DescribePlanner` for describe mode); a no-op (`{}`) in pure explore.

- Phase 2 (ADR-028): authors a grounded scenario over the **complete** `site_map`, not just the
  buttons explicitly clicked — `goal` mode builds `refs` and calls `ground_scenario`; `describe`
  mode produces an LLM draft and deterministically reconciles it (`reconcile`) against the real map.
- **M9.10 (ADR-048): also the RESUME entry point for a warm multi-turn conversation.**
  `route_entry` (`graph.py:440-445`) routes `START` straight here, bypassing `perceive`, when the
  state already carries both `site_map` and `messages` (a warm thread) — it re-authors over the
  persisted map using prior turns as refine context (`_capped_history`, capped by
  `SENTINEL_REFINE_HISTORY_KEEP`, default 6 turns).
- Appends grounded steps to `exploration_plan`, records `scenario_unmatched`, appends an assistant
  summary turn to `messages` for the next turn.
- Unconditional edge `scenario → report`.

### 3.10 `report`

**LLM: None. Terminal node.**

- Computes `plan_hash` (canonical SHA-256 over `exploration_plan`) and writes `plan.json` to
  `artifact_dir` — this is the **only** place the plan is actually frozen.
- Adds `tokens` (a `budget.tracker().summary()` snapshot, §2.8) and `models` to `plan.json`.
- Emits AG-UI `verdict` — a best-effort verdict from this node's OWN view of the run (whether
  `errors` is non-empty), **not** the true process exit code: the real exit code is computed in
  `brain/__main__.py` after `app.invoke()` returns, outside the graph.
- **Does not call** `WriteRunResult`, does not assemble a unified `RunResult` (healing audit,
  golden-diff warnings, coverage map, cost breakdown, human-gate list), and does not emit a `DONE`
  event — none of those constructs exist in the graph's code.
- Unconditional edge `report → END`.

### 3.11 `run_replay()` — the standalone replay/baseline loop (bypasses the graph)

`run_mode in {"replay", "baseline"}` does NOT go through the `StateGraph` at all: `_run_replay()`
(`brain/__main__.py:514`) calls `run_replay()` (`brain/replay.py:101`) directly — a plain Python
loop over the frozen steps in `plan.json`, with no LangGraph, no checkpointer, and no
`perceive`/`ground`/`plan` nodes.

- **Integrity check (ADR-006):** recomputes `plan_hash` over the steps; on mismatch (and without
  `FORCE_REPLAY=1`) — a hard abort, `exit_code=3`, nothing executes.
- **Per step:** `navigate`/`assert`/`press` execute directly; for `click`/`fill`/`type`/`select`,
  a `browser.probe` on the primary locator runs first — at `count==1` the action executes right
  away, otherwise the **real** `HealingEngine.heal(ctx)` is called (`brain/healing.py:56-112`):
  1. Cache lookup (`store.lookup`) on `(page_path, semantic_id, dom_hash)`; a miss triggers
     `store.evict_stale`.
  2. Strategy rotation over the recorded `alternatives`: the first locator that resolves to
     exactly one element (`brain/healing.py:26-27`, `PRIORS`):

     | Strategy | Source | Prior |
     |---|---|---|
     | `testid` | generated by `ground()` from `data-testid`/`data-cy`/`data-pw` | 0.95 |
     | `role_name` | generated by `ground()` — ARIA role + accessible name | 0.90 |
     | `label` | generated by `ground()` from `aria-label` (not for buttons) | 0.88 |
     | `text_role` | generated by `ground()` from visible text | 0.80 |
     | `css` | ONLY from LLM re-grounding (step 3 below) | 0.65, then ×0.90 overconfidence discount |
     | `xpath` | declared in `PRIORS`; generated by `record_bridge.py` (recorded extension scenarios), not `ground()` | 0.45 |
     | `visual` | ONLY from visual set-of-marks (step 4) | 0.80 (in the FLAGGED band by design) |

  3. If rotation found nothing — LLM re-grounding (`_llm_reground`, a structured JSON reply with a
     CSS selector), only if an LLM backend is wired (`use_llm=True`, typically `HEAL_LLM=1`) and
     the `heal` budget is not exhausted (`budget.tracker().exceeded("heal")`).
  4. If that found nothing either — visual set-of-marks (`_visual_reground`), only if
     `use_visual=True` **and** the backend supports vision. There is no `completeness_ratio` gate
     — that field does not exist anywhere in the code.
  5. The candidate is re-probed against the live DOM; if it does not resolve to exactly one
     element, confidence is zeroed.
  6. Gate: `confidence >= 0.85` → `auto_healed` (locator persisted as `active`); `0.60–0.84` →
     `flagged` (applied optimistically, persisted with a review flag); `< 0.60` → `needs_review`,
     the locator is NOT persisted, the step fails.
  7. Every attempt writes a row to the `healing_audit` SQLite table (append-only,
     `brain/store.py:145-152`) — no `UPDATE`/`DELETE` ever.
- **Flake quarantine:** `store.record_step(plan_id, step_key, passed, aut_version)` keeps a
  sliding window of the last 5 outcomes PER AUT SHA (reset on a SHA change); ≥3 failures out of 5
  triggers quarantine (`quarantined=True`, excluded from `exit 1`); 3 consecutive passes clear the
  quarantine (`brain/store.py:179-196`). Golden-diff regressions (`exit 2`) are NOT suppressed by
  quarantine.
- **No retry loop with an attempt cap** for a single step (no `heal_attempts` field at all) — each
  step gets exactly one `heal.heal(ctx)` call.
- **AG-UI + auto-HITL (M14 tail 2, ADR-055):** emits `run.started`/`step.progress`/`heal`/
  `verdict`; counts `consecutive_heal_failures` (same semantics and `SENTINEL_AUTO_HITL_THRESHOLD`
  threshold as the graph, `brain/replay.py:143-153`) and emits `hitl_needed` at the threshold — but
  there is no real pause (interrupt/resume) in the replay loop: a live auto-takeover mid-replay is
  M9-LIVE work.

---

## 4. Edges

### 4.1 Edge Table

Source of truth: `brain/graph.py:454-475` (`b.add_edge`/`b.add_conditional_edges`). Every edge
below is valid for the ONE graph used by explore/chat/goal/describe — there is no `run_mode`
branching.

| From | To | Condition / Trigger |
|---|---|---|
| `START` | `perceive` | `route_entry`: cold start — `site_map` and/or `messages` empty |
| `START` | `scenario` | `route_entry`: warm resume — both `site_map` and `messages` populated (ADR-048) |
| `perceive` | `ground` | Always |
| `ground` | `plan` | Always |
| `plan` | `act` | `route_plan`: `not exploration_complete` — next action queued in `_pending` |
| `plan` | `scenario` | `route_plan`: `exploration_complete` |
| `act` | `verify` | Always |
| `verify` | `checkpoint` | `route_verify`: `_verify_ok == True` |
| `verify` | `heal` | `route_verify`: `_verify_ok == False` |
| `heal` | `checkpoint` | Always (stub node in the explore graph, §3.6) |
| `checkpoint` | `scenario` | `route_checkpoint`: `exploration_complete` OR `current_step >= max_steps` |
| `checkpoint` | `takeover` | `route_checkpoint`: `_takeover_armed` (and NOT `exploration_complete`) |
| `checkpoint` | `perceive` | `route_checkpoint`: otherwise — normal cycle continuation |
| `takeover` | `checkpoint` | Always (re-polls the orchestrator before continuing) |
| `scenario` | `report` | Always |
| `report` | `END` | Always (terminal) |

### 4.2 Router Functions — Verbatim

Exactly 4 functions emit conditional edges; none reads `run_mode` (`graph.py:425-445`):

```python
def route_plan(state):
    return "scenario" if state.get("exploration_complete") else "act"

def route_verify(state):
    return "checkpoint" if state.get("_verify_ok", True) else "heal"

def route_checkpoint(state):
    if state.get("exploration_complete"):
        return "scenario"
    if state.get("_takeover_armed"):
        return "takeover"
    return "scenario" if state.get("current_step", 0) >= state.get("max_steps", 40) else "perceive"

def route_entry(state):
    return "scenario" if (state.get("site_map") and state.get("messages")) else "perceive"
```

---

## 5. ASCII Flow Diagram

```
                                  ┌─────────┐
                          ┌────── │  START  │ ──────┐
                          │       └─────────┘       │
                (site_map+messages: warm resume)  (else: cold start)
                          │                          │
                          ▼                          ▼
                    ┌───────────┐              ┌───────────┐
        ┌─────────► │  scenario │ ◄──────┐     │  perceive │◄────────────┐
        │           └─────┬─────┘        │     └─────┬─────┘             │
        │                 │ always       │           │ always            │
        │                 ▼              │           ▼                   │
        │           ┌───────────┐        │     ┌───────────┐             │
        │           │  report   │        │     │   ground  │             │
        │           └─────┬─────┘        │     └─────┬─────┘             │
        │                 │ always       │           │ always            │
        │                 ▼              │           ▼                   │
        │              ┌─────┐           │     ┌───────────┐             │
        │              │ END │           │     │   plan    │             │
        │              └─────┘           │     └─────┬─────┘             │
        │                                │           │                   │
        │                    exploration_complete   not exploration_complete
        │                                │           │                   │
        └────────────────────────────────┘           ▼                   │
        ▲                                       ┌───────────┐            │
        │                                       │    act    │            │
        │                                       └─────┬─────┘            │
        │                                             │ always           │
        │                                             ▼                  │
        │                                       ┌───────────┐            │
        │                                       │  verify   │            │
        │                                       └─────┬─────┘            │
        │                                _verify_ok │  │ NOT _verify_ok  │
        │                                    ┌───────┘  └───────┐        │
        │                                    ▼                  ▼        │
        │                              ┌───────────┐      ┌───────────┐  │
        │                    ┌────────►│ checkpoint│      │   heal    │  │
        │                    │         └─────┬─────┘      │ (STUB)    │  │
        │                    │               │            └─────┬─────┘  │
        │            exploration_complete OR │ always (heal→checkpoint)  │
        │            current_step>=max_steps │◄───────────────────┘      │
        │                    │               │                           │
        │                    │      _takeover_armed                      │
        │                    │               │                           │
        │                    ▼               ▼                           │
        └────────────(scenario, above)  ┌───────────┐                    │
                                         │ takeover  │                    │
                                         │(interrupt)│                    │
                                         └─────┬─────┘                    │
                                               │ always                   │
                                               └──────────► checkpoint    │
                                        (else, from checkpoint) ──────────┘
                                            perceive (normal cycle)
```

> Simplified rendering of the real edges in `graph.py:454-475` (see the precise table in §4.1).
> There are no `LOCATOR_STALE`/`ELEMENT_GONE`/`TIMING`/`human gate` branches — none of those exist
> in the code.
> `checkpoint → perceive` is the primary back-edge driving the explore loop.

---

## 6. LLM Usage Per Node — Quick Reference

| Node | LLM Called | When |
|---|---|---|
| `perceive` | No | — |
| `ground` | No | — |
| `plan` | Conditional | Depends on `planner`: `HeuristicPlanner` is deterministic; an LLM planner/`GoalPlanner`/`DescribePlanner` — yes |
| `act` | No | — |
| `verify` | No | One-line pass-through, no LLM call (§3.5) |
| `heal` | No | Stub in the explore graph (§3.6); real LLM heal only in `run_replay()` (§3.11) |
| `checkpoint` | No | — |
| `takeover` | No | — |
| `scenario` | Conditional | Only if a `scenario_head` is wired (goal/describe) |
| `report` | No | — |

**Token budget** (§2.8) — the process-global `brain/budget.py:BudgetTracker`, NOT a `RunState` field:

| Env variable | Default |
|---|---|
| `PLAN_TOKEN_LIMIT` | 50,000 tokens / run |
| `HEAL_TOKEN_LIMIT` | 20,000 tokens / run |
| `TOTAL_TOKEN_LIMIT` | `0` (off) |

On reaching a limit, `exceeded(role)` returns `True`, and the caller (the planner /
`HealingEngine._llm_reground`) degrades silently (the planner falls back to the heuristic; heal
falls back to deterministic strategy rotation only), with no dedicated event —
**`BUDGET_WARNING` does not exist in the code** (`grep` across the tree — 0 matches).

---

## 7. pw-executor — Build Note

All references above to `pw-executor` refer to our **own TypeScript Playwright execution server**
that we build and maintain. It implements the MCP/JSON-RPC 2.0 stdio transport interface and
exposes browser primitives (navigation, `accessibility_snapshot`, `click`/`type`, trace control,
and `screenshot`) to the Python brain over a stdio pipe. The brain spawns it as a child process
and owns its lifecycle; SIGTERM cascades on brain exit.

Any API surface details flagged as **VERIFY** must be confirmed against the actual
`pw-executor` implementation before deployment.
