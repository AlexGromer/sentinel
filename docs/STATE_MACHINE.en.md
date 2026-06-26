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
| Checkpoint DB path (CI) | `/tmp/agent-{run_id}-ckpt.db` — one file per job, no contention |
| Checkpoint DB path (service) | Distinct file or `AsyncPostgresSaver` schema, never the store-gateway file |
| Thread identity key | `thread_id = run_id` |
| Production swap (K3s, M5) | `AsyncPostgresSaver` replaces `SqliteSaver` — one constructor change, schema unchanged |
| Browser execution layer | **`pw-executor`** — our own TypeScript server implementing MCP/JSON-RPC 2.0 over stdio (built, not bought; replaces any off-the-shelf browser MCP server) |

---

## 2. Shared State Object — `RunState` (TypedDict)

`RunState` is the single shared object threaded through every node.
All fields are checkpointed at each `checkpoint` node invocation.

### 2.1 Identity and Mode

| Field | Type | Description |
|---|---|---|
| `session_id` | `str` | Unique session identifier |
| `run_id` | `str` | Unique run identifier; doubles as LangGraph `thread_id` |
| `run_mode` | `Literal["explore", "replay", "ci"]` | Controls which nodes are active and which are skipped |
| `target_url` | `str` | Root URL of the application under test |
| `aut_version` | `str` | Git SHA of the application under test, recorded at run start |
| `current_url` | `str` | URL currently loaded in the browser |

### 2.2 Perception

| Field | Type | Description |
|---|---|---|
| `page_model` | `PageModel` | Parsed page representation: `{url, title, a11y_tree: dict, landmarks, forms, interactive_elements, completeness_ratio, a11y_hash, screenshot_hash, dom_subtree_hash}` |

### 2.3 Plan

| Field | Type | Description |
|---|---|---|
| `exploration_plan` | `list[PlannedAction]` | Ordered sequence of planned steps. Each `PlannedAction`: `{step_id, intent, semantic_id, action_type, locator, locator_alternatives[L1..L6], value?, expected_outcome, assertion, is_critical, is_milestone, healed: bool}` |
| `plan_hash` | `str` | SHA-256 of canonical JSON of all steps (sorted keys, floats normalized to 6 dp). Hard-abort on mismatch in replay/ci mode |
| `current_step` | `int` | Index into `exploration_plan` |

### 2.4 Coverage / Convergence

> These fields replace the LLM-only `exploration_complete` flag from earlier proposals.
> The LLM may *propose* done but cannot force it; the metric decides.

| Field | Type | Description |
|---|---|---|
| `coverage_target` | `float` | Fraction of discovered interactive elements that must be exercised before exploration ends. Default: `0.85` |
| `interactive_seen` | `set[str]` | Semantic IDs of all interactive elements discovered |
| `interactive_exercised` | `set[str]` | Semantic IDs of interactive elements acted upon |
| `nav_frontier` | `deque[str]` | Unexplored URLs and links remaining |
| `coverage_achieved` | `float` | Computed as `len(exercised) / max(1, len(seen))` |
| `exploration_complete` | `bool` | `True` only when `coverage_achieved >= coverage_target` AND `nav_frontier` is empty |

### 2.5 Episodic Memory

| Field | Type | Description |
|---|---|---|
| `episodic_buffer` | `deque[EpisodicEvent]` | Bounded circular buffer, max 50 entries. When full, oldest events are LLM-summarised (Sonnet by default) into ~200-token episode summaries to bound context growth |
| `executed_actions` | `list[ExecutedAction]` | Full action history: `{step, action_type, locator, outcome, duration_ms, pre_hash, post_hash, healing_flagged}` |

### 2.6 Healing

| Field | Type | Description |
|---|---|---|
| `healing_context` | `Optional[HealingContext]` | Active heal context: `{semantic_id, failure_type, attempted_locator, element_description}`. `None` when no heal is in progress |
| `heal_attempts` | `int` | Per-step heal counter. Reset at each `checkpoint`. Hard cap: 3 in explore mode, 2 in replay hot path |
| `pending_human_review` | `list[HealCandidate]` | Heal candidates awaiting human gate decision |
| `healed_locators` | `list[HealedLocator]` | Healed locators pending flush to `store-gateway` at next `checkpoint` |

### 2.7 Token Budget

| Field | Type | Description |
|---|---|---|
| `token_usage` | `dict[str, TokenCount]` | Per-model-id usage: `model_id → {prompt, completion, cost_usd}`. In-process counter; Go orchestrator independently enforces a hard ceiling |
| `token_budget` | `dict[str, int]` | Per-model-id budget limits (from config). Defaults: 50k tokens/run for Opus 4.8 (plan, default), 20k tokens/run for Sonnet 4.6 (heal, default) |
| `budget_warning_emitted` | `bool` | Set when 80% of any budget is consumed; prevents duplicate `BUDGET_WARNING` events |

### 2.8 Human Gate / Control

| Field | Type | Description |
|---|---|---|
| `human_gate_pending` | `bool` | `True` when the run is paused at a `checkpoint` awaiting operator decision |
| `human_gate_reason` | `Optional[str]` | Human-readable explanation of why the gate was raised |
| `human_gate_decision` | `Optional[Literal["approve", "skip", "abort"]]` | Resolution set by `agentctl gate approve|skip|abort` |
| `human_gate_resolved_locator` | `Optional[str]` | Operator-supplied locator when decision is `"approve"` |
| `stop_signal` | `bool` | Externally injected stop flag (e.g., CI timeout or operator `agentctl run --stop`) |

### 2.9 Artifacts

| Field | Type | Description |
|---|---|---|
| `run_dir` | `str` | Filesystem path to the per-run artifact directory |
| `artifacts` | `RunArtifacts` | `{trace_path, screenshot_paths, spec_path, report_path}` — paths to emitted files |
| `step_failures` | `dict[str, int]` | `step_key → consecutive_failure_count`. Input to AUT-SHA-gated flake quarantine logic |

---

## 3. Nodes

There are **8 named nodes** plus the two implicit LangGraph built-in nodes (`START`, `END`).
The framework wires `START` → first node and `END` as the graph terminal automatically.

### Node summary

| # | Node | LLM | Model | Notes |
|---|---|---|---|---|
| 1 | `perceive` | No | — | Entry of every cycle; calls `pw-executor` for a11y snapshot |
| 2 | `ground` | No | — | Parses `PageModel`; validates against golden baselines in replay |
| 3 | `plan` | Yes | Opus 4.8 (default) | **Explore mode only** — skipped entirely in replay/ci |
| 4 | `act` | No | — | Executes the current step via `pw-executor` |
| 5 | `verify` | Conditional | Sonnet 4.6 (default) | Sonnet only for explore-mode soft assertion; deterministic in replay |
| 6 | `heal` | Conditional | Sonnet 4.6 (default) | Sonnet for a11y re-grounding (L5+); gated visual also Sonnet |
| 7 | `checkpoint` | No | — | Flushes LangGraph checkpoint + `store-gateway` writes; handles human gate pause |
| 8 | `report` | No | — | Terminal node; assembles and emits `RunResult` |

### 3.1 `perceive`

**LLM: None.**

Entry point of every agent cycle.

- Calls `pw-executor` (via MCP/JSON-RPC 2.0 over stdio) for `accessibility_snapshot()`.
- Computes `completeness_ratio` (named interactive elements / total interactive elements).
- If `completeness_ratio < 0.30` (canvas, shadow DOM, custom elements, cross-origin iframes),
  also calls `screenshot()` on `pw-executor` to produce a set-of-marks context for the visual
  healing fallback — this expenditure is gated and does not occur on every cycle.
- Computes `a11y_hash`, `screenshot_hash`, and `dom_subtree_hash` (subtree-scoped, not
  whole-page, to prevent unrelated ad/banner changes from invalidating all cached locators).
- Starts or resumes the `pw-executor` Playwright trace at run start.

### 3.2 `ground`

**LLM: None.**

- Parses the raw a11y tree into the typed `PageModel`.
- Updates `interactive_seen` and `nav_frontier` from newly discovered elements and links.
- Computes `coverage_achieved = len(interactive_exercised) / max(1, len(interactive_seen))`.
- Sets `exploration_complete = True` only when `coverage_achieved >= coverage_target`
  AND `nav_frontier` is empty.
- **In replay/ci mode:** validates `a11y_hash` and `screenshot_hash` against the immutable
  golden baseline for each milestone step. Emits `STRUCTURAL_CHANGE` on a11y drift or
  `VISUAL_WARN` on screenshot-hash divergence without failing the step outright.

### 3.3 `plan`

**LLM: Opus 4.8 (default), temperature = 0. Explore mode only.**

- Skipped entirely in `replay` and `ci` modes — `ground` routes straight to `act`.
- Input context: `page_model`, episodic tail from `episodic_buffer`, `nav_frontier`,
  remaining token budget, and `coverage_achieved`.
- Output: one or more `PlannedAction` entries to append to `exploration_plan`, or a
  *proposed* `exploration_complete = True`.
- The LLM may propose done but the flag is only set if the coverage metric independently
  confirms it (`ground` owns the authoritative check).
- Applies an in-process budget pre-check before the LLM call; degrades gracefully (partial
  plan freeze) rather than aborting.
- On completion of exploration: freezes `plan.json` and computes `plan_hash`.

### 3.4 `act`

**LLM: None.**

- Pops `exploration_plan[current_step]`.
- **Explore mode:** executes the action via `pw-executor` using the `locator_hint` from
  the plan.
- **Replay/ci mode:** executes the *frozen* locator from the committed `plan.json`.
  No LLM invoked; zero token cost on the happy path.
- Appends a skeleton `ExecutedAction` record to `executed_actions`.
- On any `pw-executor` error (selector not found, element not interactable, etc.): routes
  to `verify`, which classifies the failure before escalating to `heal`.

### 3.5 `verify`

**LLM: Conditional — Sonnet 4.6 (default) only for explore-mode soft assertion.**

- Re-snapshots the page inline (calls `pw-executor accessibility_snapshot()` after the action).
- Classifies the outcome into one of:
  - `PASS`
  - `LOCATOR_STALE` — element present in the a11y tree but selector no longer resolves
  - `ELEMENT_GONE` — element absent from the tree (removed, conditional, or A/B variant)
  - `TIMING` — element present but not yet interactable; retry `act` once with a short wait
    *before* escalating to `heal`
  - `UNEXPECTED_ERROR` — navigation/network/JS error; not healed, routed directly to `report`
- **In replay mode:** performs a structural a11y diff against the golden baseline
  (`diff_ratio` check) and a screenshot-hash comparison for milestone steps.
- **In explore mode:** uses Sonnet for soft assertion evaluation when the step has a
  non-trivial `expected_outcome`/`assertion`.
- A genuine assertion mismatch (element found, observed value wrong) is a real `FAIL`
  and is NOT healed — it routes to `report`.

### 3.6 `heal`

**LLM: Conditional — Sonnet 4.6 (default) (a11y re-grounding, visual set-of-marks).**

Delegates all healing logic to the `healing-engine` module.
Operates on `HealingContext {semantic_id, failure_type, attempted_locator, element_description}`.

Healing proceeds in bounded, ordered steps:

1. **Cache lookup** (zero LLM): query `store-gateway` for a `healed_locator` matching
   `(page_url, semantic_id)` with a still-valid `dom_subtree_hash`. On hit: reuse immediately.
   On miss: evict (mark `deprecated`) and proceed.
2. **Strategy rotation L1–L6** (zero LLM): probe candidates in order via `pw-executor`;
   take the first resolving to exactly one element.

   | Level | Strategy | Prior |
   |---|---|---|
   | L1 | `data-testid` / `data-cy` / `data-pw` | 0.95 |
   | L2 | ARIA role + accessible name | 0.90 |
   | L3 | `aria-label` exact match | 0.88 |
   | L4 | Visible text + role | 0.80 |
   | L5 | Scoped CSS (semantic container + element type) | 0.65 |
   | L6 | XPath positional | 0.45 |

   A match at L5/L6 emits a `strategy_degradation` metric (DOM instability signal).

3. **LLM a11y re-grounding** (Sonnet 4.6 by default, structured output): only if cache + L1–L6 all fail.
   Applies LLM-overconfidence discount `× 0.90`.
4. **Visual set-of-marks** (Sonnet 4.6 vision by default): only if `completeness_ratio < 0.30`
   AND step 3 failed AND the M5 PoC validated `> 70%` accuracy on 20 real broken-selector
   scenarios. Returns a `mark_number` mapped to a real semantic locator, not a coordinate click.
   Applies visual discount `× 0.85`.
5. **Verify-before-accept**: every LLM/visual candidate is re-probed against the live DOM
   via `pw-executor`. If it does not resolve to exactly one element, confidence is zeroed.
6. **Confidence gate**:
   - `≥ 0.85` → auto-heal: run the action once with the healed locator; on success persist
     `HealedLocator(status=active)` keyed to `(page_url, semantic_id, dom_subtree_hash)`.
   - `0.60–0.84` → flagged: apply optimistically, set `healing_flagged = True`, persist with
     `review_required = True`; surfaces in the run report.
   - `< 0.60` → human gate: do not persist; emit `NEEDS_HUMAN_REVIEW`; in CI auto-skip
     the step; in interactive mode pause at `checkpoint` until `agentctl gate` resolves it.
7. **Audit** (append-only): every attempt writes a `healing_audit` row — no `UPDATE`/`DELETE`
   ever. Written as `healing-audit.jsonl` CI artifact and as OTel span attributes
   (selector + confidence only; never prompt content).
8. **Bounded retry + quarantine**: `heal_attempts` hard-capped at 3 (explore) / 2 (replay).
   On cap: AUT-SHA-gated flake quarantine — a step is quarantined only when it fails in
   N-of-5 recent runs *without* an AUT git-SHA change.

> **Cold-start note:** the auto-accept threshold defaults to **0.90** (not 0.85) until
> enough human-verified outcomes accumulate for `agentctl calibrate` to compute
> precision/recall and lower it safely.

### 3.7 `checkpoint`

**LLM: None.**

- Flushes the LangGraph checkpoint to the separate checkpoint DB
  (`SqliteSaver` / `AsyncPostgresSaver`).
- Flushes `pending healed_locators` and the updated `page_model` to `store-gateway`
  via `PersistenceService` gRPC.
- Records a `checkpoint_id` event with the Go `orchestrator`.
- Resets `heal_attempts` and clears `healing_context`.
- **If `human_gate_pending = True`:** calls `RunControl.Checkpoint` / `Pause` on the
  orchestrator and suspends the LangGraph thread until `agentctl gate approve|skip|abort`
  delivers a resolution via gRPC. In CI mode the gate auto-skips after the configured
  timeout (default 30 min).

### 3.8 `report`

**LLM: None. Terminal node.**

- Stops the `pw-executor` Playwright trace; relays `trace_path` to the orchestrator via gRPC.
- Serialises `plan.json` (with `plan_hash` + dual golden baselines) if not yet frozen.
- Builds the `RunResult`:
  - Per-step outcomes: `PASS` / `FAIL` / `SKIP` / `HEALED` / `QUARANTINED`
  - Healing-audit section (original vs healed locator, confidence, reasoning,
    flagged-for-review list)
  - Golden-diff and screenshot-hash drift warnings
  - Coverage map (exercised vs discovered elements)
  - Cost breakdown by node and model
  - Human-gate pending list
  - Plan integrity status
- Calls `WriteRunResult` on `store-gateway`.
- Emits `DONE` event to the orchestrator (exit code propagated to `agentctl`).

---

## 4. Edges

### 4.1 Edge Table

| From | To | Condition / Trigger |
|---|---|---|
| `START` | `perceive` | Always (graph entry) |
| `perceive` | `ground` | Always |
| `ground` | `plan` | `run_mode == "explore"` AND `not exploration_complete` |
| `ground` | `act` | `run_mode in {"replay", "ci"}` OR (`explore` AND plan exists AND `current_step > 0`) |
| `ground` | `report` | `run_mode == "explore"` AND `exploration_complete` |
| `plan` | `checkpoint` | Plan just frozen (exploration complete at planning layer) |
| `plan` | `act` | Next action queued; exploration continuing |
| `plan` | `report` | `exploration_complete` confirmed OR budget exhausted |
| `act` | `verify` | Always |
| `verify` | `heal` | Outcome is `LOCATOR_STALE` OR `ELEMENT_GONE` OR `TIMING` |
| `verify` | `checkpoint` | Outcome is `PASS` AND `step.is_milestone == True` |
| `verify` | `act` | Outcome is `PASS` AND `not is_milestone` AND steps remain |
| `verify` | `report` | All steps done OR outcome is `UNEXPECTED_ERROR` OR genuine `FAIL` on a critical step |
| `heal` | `act` | `confidence >= 0.60` AND `heal_attempts < cap` — retry with healed locator |
| `heal` | `checkpoint` | `confidence < 0.60` — human gate raised |
| `heal` | `checkpoint` | `heal_attempts >= cap` — quarantine and flush |
| `heal` | `act` *(next step)* | Heal failed AND `step.is_critical == False` — skip step, continue |
| `checkpoint` | `heal` | Human gate resolved with `decision == "approve"` and a locator |
| `checkpoint` | `act` | Human gate resolved with `decision == "skip"` — resume at next step |
| `checkpoint` | `END` | Human gate resolved with `decision == "abort"` |
| `checkpoint` | `perceive` | Normal cycle continuation (no gate, no terminal condition) |
| `checkpoint` | `report` | Terminal condition met: coverage achieved OR budget exhausted OR `stop_signal` |
| `report` | `END` | Always (terminal) |

### 4.2 Conditional Edge Logic — Summary

Three nodes emit conditional edges driven by `RunState` fields:

**`ground`** (mode router):
```
if run_mode == "explore" and exploration_complete  →  report
if run_mode == "explore" and not exploration_complete  →  plan
if run_mode in {"replay", "ci"}  →  act
if run_mode == "explore" and plan_exists and current_step > 0  →  act
```

**`verify`** (outcome classifier):
```
if outcome in {LOCATOR_STALE, ELEMENT_GONE, TIMING}  →  heal
if outcome == PASS and is_milestone  →  checkpoint
if outcome == PASS and not is_milestone and steps_remain  →  act
else (done | error | critical fail)  →  report
```

**`heal`** (confidence gate + attempt cap):
```
if confidence >= 0.60 and heal_attempts < cap  →  act          # retry
if confidence < 0.60  →  checkpoint                            # human gate
if heal_attempts >= cap  →  checkpoint                         # quarantine
if heal_failed and not is_critical  →  act (next step)         # skip
```

**`checkpoint`** (gate resolver + cycle controller):
```
if human_gate_pending and decision == "approve"  →  heal
if human_gate_pending and decision == "skip"  →  act
if human_gate_pending and decision == "abort"  →  END
if terminal (coverage | budget | stop_signal)  →  report
else  →  perceive
```

---

## 5. ASCII Flow Diagram

```
                        ┌─────────┐
                        │  START  │
                        └────┬────┘
                             │
                             ▼
                        ┌─────────┐
              ┌──────── │ perceive│ ◄──────────────────────────────┐
              │         └────┬────┘                                │
              │              │ always                              │
              │              ▼                                     │
              │         ┌─────────┐                               │
              │         │  ground │                               │
              │         └────┬────┘                               │
              │              │                                     │
              │    ┌─────────┼──────────────┐                     │
              │    │         │              │                     │
              │ (explore     │           (explore                 │
              │ +complete)   │(replay/ci  +plan+                  │
              │    │         │ OR explore  step>0)                │
              │    │         │ +!complete) │                      │
              │    ▼         ▼             │                      │
              │ ┌────────┐ ┌──────┐       │                      │
              │ │ report │ │ plan │       │                      │
              │ └───┬────┘ └──┬───┘       │                      │
              │     │        │\           │                      │
              │     │   frozen │\next     │                      │
              │     │    plan  │ action   │                      │
              │     │        ▼  \         │                      │
              │     │  ┌──────────┐       │                      │
              │     │  │checkpoint│◄──────┘◄──────────┐          │
              │     │  └─────┬────┘                   │          │
              │     │        │ normal cycle            │          │
              │     │        └────────────────────────►┘          │
              │     │                                             │
              │     │       ┌──────────────────────────────────── ┤
              │     │       │                                     │
              │     │       ▼                                     │
              │     │    ┌─────┐                                  │
              │     │    │ act │                                  │
              │     │    └──┬──┘                                  │
              │     │       │ always                              │
              │     │       ▼                                     │
              │     │    ┌────────┐                               │
              │     │    │ verify │                               │
              │     │    └───┬────┘                               │
              │     │        │                                     │
              │     │  ┌─────┼──────────────────┐                │
              │     │  │     │                  │                │
              │     │(stale/ │(PASS+         (PASS+             │
              │     │ gone/  │ milestone)    !milestone          │
              │     │ timing)│               +steps)            │
              │     │  │     ▼               │                   │
              │     │  │  ┌──────────┐       └──► act ──────────►┘
              │     │  │  │checkpoint│◄───────────────┐
              │     │  │  └─────┬────┘                │
              │     │  │        │                      │
              │     │  │  (human gate decision)        │
              │     │  │    approve │  skip  │  abort  │
              │     │  │           │        │    │     │
              │     │  │           ▼        ▼    ▼     │
              │     │  │        ┌──────┐   act  END    │
              │     │  │        │ heal │               │
              │     │  ▼        └──┬───┘               │
              │     │ ┌──────┐     │                   │
              │     │ │ heal │     │ confidence≥0.60   │
              │     │ └──┬───┘     │ AND attempts<cap  │
              │     │    │         └──────────────────►┘(act retry)
              │     │    │
              │     │    ├── confidence<0.60 ──────────► checkpoint (human gate)
              │     │    ├── attempts>=cap  ──────────► checkpoint (quarantine)
              │     │    └── failed+!critical ─────────► act (next step)
              │     │
              │     └──────────────────────────────────► END
              │                                          ▲
              └──────────────────────────────────────────┘
                  (explore+complete OR budget OR stop)
```

> Simplified for readability — see Section 4 for the precise conditional rules.
> `checkpoint → perceive` (normal cycle) is the primary back-edge driving the loop.

---

## 6. LLM Usage Per Node — Quick Reference

| Node | LLM Called | Model | When |
|---|---|---|---|
| `perceive` | No | — | — |
| `ground` | No | — | — |
| `plan` | Yes | Opus 4.8 (default, temperature 0) | Explore mode only; skipped in replay/ci |
| `act` | No | — | — |
| `verify` | Conditional | Sonnet 4.6 (default) | Explore mode only, for soft assertion evaluation |
| `heal` | Conditional | Sonnet 4.6 (default) | Only after cache + L1–L6 rotation fail; vision also Sonnet |
| `checkpoint` | No | — | — |
| `report` | No | — | — |

**Budget defaults** (configurable; enforced in-process with Go-side hard ceiling):

| Budget key | Model | Default |
|---|---|---|
| `plan_token_limit` | Opus 4.8 (default) | 50,000 tokens / run |
| `heal_token_limit` | Sonnet 4.6 (default) | 20,000 tokens / run |

Exceeding 80% of any budget emits `BUDGET_WARNING`; exhaustion degrades gracefully
(plan node stops issuing new exploration steps; heal node falls back to L1–L6 rotation only)
rather than aborting the run.

---

## 7. pw-executor — Build Note

All references above to `pw-executor` refer to our **own TypeScript Playwright execution server**
that we build and maintain. It implements the MCP/JSON-RPC 2.0 stdio transport interface and
exposes browser primitives (navigation, `accessibility_snapshot`, `click`/`type`, trace control,
and `screenshot`) to the Python brain over a stdio pipe. The brain spawns it as a child process
and owns its lifecycle; SIGTERM cascades on brain exit.

Any API surface details flagged as **VERIFY** must be confirmed against the actual
`pw-executor` implementation before deployment.
