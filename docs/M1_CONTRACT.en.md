# M1 Contract — "Autonomous Walk" (frozen 2026-06-23)

> 🌐 [Русский](M1_CONTRACT.md) (основная версия) · **English**

Goal: turn M0's linear `perceive` into a real **LangGraph StateGraph** that autonomously
explores a multi-page site, converges on a **measurable coverage target**, and freezes a
deterministic `plan.json` (+ `plan_hash`). Heal stays a stub (M2). Transport stays the M0
JSON-RPC (MCP-SDK migration deferred to M2, GAP-VERIFY-002).

## Planner (ADR-011 — pluggable)
`Planner.propose(state) -> {action: PlannedAction|None, done: bool, reason: str}`
- **HeuristicPlanner** (default, offline, deterministic, $0): on the current page, pick the first
  *unexercised* interactive element (reading order, prefer button/link); else navigate to the next
  same-origin URL in `nav_frontier`; else `done`. No LLM, no network.
- **LLMPlanner** (Opus 4.8, T=0; `--planner llm`): prompt = page_model summary + seen/exercised +
  frontier + remaining budget → next-action JSON. Requires `ANTHROPIC_API_KEY`; on missing key or
  error → **falls back to HeuristicPlanner** (graceful degradation). Logs tokens to transcript.
- Convergence gate (ADR-010) is enforced by the graph, NOT the planner: `exploration_complete` is set
  True only when `coverage_achieved >= coverage_target` AND `nav_frontier` empty. `max_steps` is a backstop.

## RunState (M1 subset; TypedDict)
```
run_id, run_mode='explore', target_url, base_origin, current_url
page_model: {url, title, aria, interactive: [{semantic_id, role, name, kind}], link_count}
exploration_plan: [PlannedAction]; plan_hash; current_step
coverage_target=0.85; interactive_seen:set; interactive_exercised:set
nav_frontier:list[str]; coverage_achieved:float; exploration_complete:bool
executed_actions:list; episodic:list; token_usage:dict
max_steps=40 (backstop); artifact_dir; errors:list
```
`PlannedAction = {step_id, intent, semantic_id, action_type:'navigate'|'click', target, locator:{role,name}|{css}, is_milestone}`
`semantic_id = sha1(f"{url_path}|{role}|{name}")[:12]` (stable across runs of the same DOM).

## Nodes (9) and edges
`perceive → ground → plan → act → verify → (heal*) → checkpoint → report`  (* heal = stub @ M1)
- **perceive**: `browser.snapshot` + `browser.currentUrl` → page_model; start trace at run start.
- **ground**: parse interactive elements → assign semantic_ids → update `interactive_seen`;
  `browser.links` → push same-origin unseen URLs to `nav_frontier`; recompute `coverage_achieved`.
- **plan**: `Planner.propose`; append PlannedAction OR set `exploration_complete` (gated). Log to transcript.
- **act**: execute via pw-executor (`browser.navigate` | `browser.click`); record executed_action;
  mark semantic_id exercised; `current_step++`.
- **verify**: re-snapshot; classify PASS / changed. M1 heal is a stub → failure logs + continues.
- **heal**: STUB — logs "heal deferred to M2"; routes to checkpoint.
- **checkpoint**: LangGraph `SqliteSaver` checkpoint to a **SEPARATE** db `runs/<id>/checkpoint.db`.
- **report**: freeze `plan.json` + compute `plan_hash`; stop trace; print summary.

Conditional edges: `ground→report` if explore_complete; `plan→report` if done/`max_steps`; else loop
`checkpoint→perceive`. `verify→heal` on failure (stub), else `verify→checkpoint`.

## pw-executor — new tools (M1)
| Method | Params | Result |
|--------|--------|--------|
| `browser.click` | `{locator:{role,name}|{css}}` | `{clicked, url}` (via `getByRole`/css) |
| `browser.links` | — | `{links:[{href,text}]}` (anchors; brain filters same-origin) |
| `browser.currentUrl` | — | `{url, title}` |
(plus M0: `initialize`, `browser.navigate`, `browser.snapshot`, `browser.traceStop`, `shutdown`)

## Artifacts
- `plan.json`: `{plan_id, plan_hash, target_url, run_mode, coverage_target, coverage_achieved,
  interactive_seen, interactive_exercised, steps:[PlannedAction]}`.
- **`plan_hash`** = `sha256(canonical_json(steps))` — sorted keys, `(",",":")` separators, floats→6dp;
  **EXCLUDES** volatile fields (`plan_id`, timestamps) so re-runs over the same DOM are bit-identical.
- `llm-transcript.jsonl`: one line per plan decision `{step, planner, model|null, prompt_tokens|null,
  completion_tokens|null, decision, reason}`.
- `trace.zip` (as M0).

## Env / spawn
- `brain/pyproject.toml`: deps `langgraph`, `langgraph-checkpoint-sqlite`, `anthropic`. Managed by **uv** (`.venv`).
- agentctl spawns the venv python: `BRAIN_PYTHON` env (default `<repo>/.venv/bin/python`, fallback `python3`).
- agentctl new flags: `--planner heuristic|llm` (default heuristic), `--coverage-target` (0.85), `--max-steps` (40).

## Acceptance gate (Given/When/Then)
- **GIVEN** a multi-page fixture site (≥3 pages, ≥5 interactive elements, internal links) at `testdata/site/`,
- **WHEN** `agentctl run --explore --target file://.../site/index.html --planner heuristic`,
- **THEN** `runs/<id>/plan.json` exists with **≥5 steps**, `coverage_achieved` recorded (>0), `plan_hash` present,
  `trace.zip` present, exit 0; **AND** a second identical run produces the **same `plan_hash`** (determinism).

## Out of scope (M2+)
real heal, MCP-SDK transport, gRPC + store-gateway, golden baselines, replay mode.
