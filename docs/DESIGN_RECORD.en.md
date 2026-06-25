# Design Record — Sentinel

> 🌐 [Русский](DESIGN_RECORD.md) (основная версия) · **English**

This document archives the **full provenance** of the 2026-06-23 design workflow for Sentinel (an autonomous self-healing Playwright UI-testing agent; polyglot Go / Python / TypeScript on a LangGraph backbone). The workflow ran **4 independent architects** (each through a distinct lens) → **3 adversarial judges** → **1 lead synthesizer**.

The canonical, constraint-adjusted architecture lives in [`../ARCHITECTURE.md`](../ARCHITECTURE.md). This record preserves the raw proposals and verdicts that fed it.

> **NOTE: the synthesis's ADR-001 recommended _BUYING_ the official `@playwright/mcp` server. This was later REVERSED to _BUILD_ our own `pw-executor` by a hard build-only user constraint — see ARCHITECTURE.md §0. The proposals/verdicts below are preserved verbatim as historical record and still reference "buy".**

---

## Proposals

Four architects, four lenses. Each produced a complete structured proposal; all fields are rendered below.

### `clean-arch` — Hexagonal Polyglot Agent — Versioned Ports Across Three Bounded Contexts

*Lens: Hexagonal / ports-and-adapters maintainability*

**Philosophy.** The three languages are not glue-connected modules; they are independent bounded contexts separated by versioned, typed ports. The hexagonal boundary IS the architecture: the Python/LangGraph brain depends on no Playwright API and no database schema; the TypeScript executor knows nothing about LLM reasoning or run lifecycle; the Go control plane owns all persistence, cost policy, and process supervision without importing any ML library. Every external dependency — LLM model, browser engine, reasoning framework, storage backend — sits behind a replaceable adapter, so each can be swapped, mocked, or upgraded in isolation. The core bet: paying the contract-definition cost upfront at three explicit port boundaries eliminates the complete-rewrite-when-we-swap-X tax that kills polyglot systems in year two.

**Components (18):**

| Component | Language | Responsibility |
|-----------|----------|----------------|
| orch-cli | Go | CLI entrypoint: parses YAML config, validates required fields, resolves run mode (explore / replay / heal-audit), signals shutdown. Single static binary for CI distribution — no runtime deps. |
| run-orchestrator | Go | Spawns and supervises the Python brain subprocess (via exec.Cmd); monitors process health; transitions run state machine (PENDING → RUNNING → HEALING → COMPLETE / FAILED); restarts Python brain on crash (max 3, exponential backoff); forwards shutdown signal to all child processes cleanly. |
| grpc-server | Go | Multiplexed gRPC server exposing five services over a single Unix domain socket (dev) or TCP (K8s): ConfigService, PlanService, EventService, PersistenceService, BudgetService. All .proto definitions are the authoritative contract; generated stubs consumed by both Go server and Python client. |
| persistence-gateway | Go | Single owner of all long-term state: SQLite (single-host CI) or Postgres (cluster). Schema: sitemaps, healed_locators, exploration_plans, run_history, page_baselines, llm_traces. Migrations embedded via go:embed SQL files. Python and TS never touch the DB directly — all access via PersistenceService gRPC. |
| report-service | Go | Consumes the EventService stream in real time; accumulates scenario results, healing events, token costs, and Playwright trace references; renders HTML + JSON + JUnit XML reports on run completion. Writes artifacts to a configurable output directory. |
| cost-tracker | Go | Implements BudgetService gRPC. Tracks per-run token consumption (input + output tokens, model tier); enforces configurable hard cap; responds BUDGET_EXCEEDED to Python before any LLM call that would breach the cap; emits cost-per-run event. Token price table is config-driven — no hard-coded values. |
| langgraph-agent | Python | The LangGraph StateGraph: defines all nodes, conditional edges, and the shared RunContext TypedDict. Owns the explore → plan → act → verify → heal → checkpoint cycle. Uses LangGraph AsyncSqliteSaver (dev) or AsyncPostgresSaver (prod) as checkpointer, with thread_id = run_id for mid-run crash recovery. |
| llm-adapter | Python | Port: LLMPort. Wraps the Anthropic SDK; handles streaming, retries with exponential backoff, and token counting. Before each call checks Go BudgetService.ConsumeTokens — blocks if cap would be breached. Model is config-injected (Opus 4.8 for exploration planning, Sonnet 4.6 for healing). Swappable: implement LLMPort to use any other provider. |
| page-perception | Python | Assembles LLM context from MCP browser_snapshot response: formats accessibility tree as indented role/name/state text, embeds set-of-marks screenshot, computes dom_hash (SHA-256 of serialized a11y tree structural fields only, excluding transient attributes). Truncates tree to stay within token budget; logs truncation ratio. |
| self-healer | Python | Constructs healing prompts (failed locator + current a11y tree + set-of-marks image); parses LLM JSON response into HealedLocator; scores confidence; applies confidence gate logic (auto / flagged / escalate); calls Go PersistenceService.RecordHealedLocator. Stateless — all mutable state lives in RunContext. |
| plan-manager | Python | Serializes exploration_plan to JSON and calls Go PlanService.FreezePlan on explore→replay transition. On replay mode startup: fetches frozen plan, queries PersistenceService.GetHealedLocators per page URL, pre-patches plan locators with known-good healed versions before execution begins. Handles plan version mismatch (target URL changed) by flagging for re-explore. |
| playwright-executor | TypeScript | MCP server over stdio: implements JSON-RPC 2.0 server exposing 8 tools (browser_navigate, browser_snapshot, browser_action, browser_assert, browser_codegen_start, browser_codegen_stop, browser_trace_start, browser_trace_stop). Owns Playwright Browser and BrowserContext lifecycle; one context per scenario; headless by default; headed configurable. |
| snapshot-provider | TypeScript | Implements browser_snapshot tool: calls page.accessibility.snapshot() for structured a11y tree; generates set-of-marks overlay by querying all interactive elements, drawing numbered bounding boxes on a canvas layer, returning screenshot PNG + mark_map [{id, role, name, bbox, ariaLabel}]. Falls back to DOM snapshot for elements absent from a11y tree. |
| locator-resolver | TypeScript | Implements the locator strategy hierarchy: tries each strategy in order (ARIA role+name → data-testid/data-cy → getByText exact → scoped CSS → XPath), returns the first live match with its strategy label. Called by both browser_action and browser_assert tools. On total failure reports all 5 attempted selectors in the error payload so self-healer has full context. |
| trace-controller | TypeScript | Implements browser_trace_start / browser_trace_stop tools. Wraps Playwright context.tracing.start() / stop(). Saves .zip trace per scenario to a configurable artifacts directory. Returns absolute file path in stop response so Go report-service can reference it in HTML report. |
| codegen-exporter | TypeScript | Implements browser_codegen_start / browser_codegen_stop tools. Records the action sequence from a replay run (locator + action type + value), emits idiomatic Playwright TypeScript test code as a .spec.ts file using test() / expect() wrappers. Output is importable into an existing Playwright project without modification. |
| contracts/proto | shared | Authoritative .proto files for all 5 gRPC services (Config, Plan, Event, Persistence, Budget). Go generates server stubs; Python generates client stubs. Versioned with major.minor in package name. Breaking changes require a major bump and a parallel migration period. Lives at repo root contracts/proto/. |
| contracts/mcp-schema | shared | JSON Schema definitions for all 8 MCP tools exposed by playwright-executor. Python MCP client validates tool schemas on subprocess connect; mismatches cause startup failure with a clear error. Versioned in the MCP server_info response. Lives at repo root contracts/mcp/. |

**Boundaries (polyglot contracts).**

BOUNDARY 1: Go Control Plane ↔ Python Brain (gRPC + protobuf)

Protocol: gRPC over Unix domain socket (single-host: CI, dev) with automatic TCP fallback (K8s multi-pod). Connection string injected by Go orchestrator as AGENT_GRPC_ADDR env var when spawning Python subprocess. Python brain is the sole gRPC client; Go is the sole server. Five services in one multiplexed connection:
  - ConfigService: GetRunConfig(run_id) → RunConfig. Called once at Python startup.
  - PlanService: FreezePlan(plan_json, target_url, dom_hash) → plan_id. GetFrozenPlan(target_url) → plan_json. Called by plan-manager node.
  - EventService: Emit(RunEvent) → ack. StreamEvents(run_id) → stream (for report-service). Python emits after every node transition.
  - PersistenceService: RecordHealedLocator, GetHealedLocators(page_url), UpsertSitemap, GetBaseline, PutBaseline. All long-term state crosses this boundary.
  - BudgetService: ConsumeTokens(run_id, input_tokens, output_tokens, model) → {allowed: bool, remaining: int}. Python calls before every LLM invocation.

Rationale for gRPC over REST: protobuf enforces the contract at compile time — generated stubs for both Go (server) and Python (client) from the same .proto source. No runtime schema drift. Streaming on EventService enables real-time report rendering without polling. Alternative rejected: REST/JSON — no compile-time schema enforcement, two independent implementations of the same contract will drift within weeks.

Versioning: proto package names carry major version (v1, v2). Go and Python pin the same proto version via the contracts/proto/ directory at repo root. A breaking proto change is a cross-language coordinated deploy.

BOUNDARY 2: Python Brain ↔ TypeScript Executor (MCP JSON-RPC 2.0 over stdio)

Protocol: MCP (Model Context Protocol) — JSON-RPC 2.0 over the subprocess's stdin/stdout pipes. Python brain spawns playwright-executor as a child process via subprocess.Popen and acts as the MCP client. TS executor implements an MCP server. This is the same protocol Claude Code uses for all its tool servers — well-understood, zero port management, subprocess lifecycle trivially owned by parent.

Python calls are always sequential-await (one outstanding request at a time per LangGraph node execution) — no backpressure problem because LangGraph node execution is inherently sequential. Timeout: 30s per tool call (configurable). On timeout or subprocess exit: Python emits EXECUTOR_CRASH event to Go EventService, attempts subprocess restart (max 3, backoff), fails run if all retries exhausted.

Eight MCP tools exposed by TS executor:
  browser_navigate(url, wait_until?) → {ok, current_url, title}
  browser_snapshot(set_of_marks?) → {accessibility_tree, screenshot_b64, dom_hash, current_url, mark_map?}
  browser_action(action_type, locator_spec, value?) → {ok, error_type?, attempted_locators?}
  browser_assert(assertion_type, locator_spec, expected?) → {ok, actual?, error_type?}
  browser_codegen_start(output_path) → {ok}
  browser_codegen_stop() → {output_path, line_count}
  browser_trace_start(name) → {ok}
  browser_trace_stop() → {artifact_path}

Rationale for MCP over HTTP-JSON-RPC local server: no port allocation or conflict in CI, no health-check polling, process lifecycle is a simple Popen, protocol is inspectable and already standardized for LLM-tool integration. Alternative rejected: HTTP/REST local server — requires port management (CI port conflicts are real), independent health-check loop, more failure modes for what is logically an in-process function call.

FAILURE ISOLATION:
  TS executor crash → Python detects subprocess exit code, restarts, resumes from last checkpoint.
  Python brain crash → Go run-orchestrator detects process exit, marks run FAILED, persists last LangGraph checkpoint. Run is resumable: re-spawn Python with same run_id; LangGraph loads checkpoint and continues from last saved node.
  Go orchestrator crash → in-flight run is recoverable: on restart, Go queries checkpointer DB for runs in RUNNING state and resumes them.
  All three processes crash simultaneously → run is marked FAILED_UNRECOVERABLE in DB; last checkpoint available for manual inspection.

**Agent loop.**

SHARED STATE OBJECT (Python TypedDict — RunContext):

  run_id: str
  run_mode: Literal["explore", "replay", "heal_audit"]
  target_url: str
  config: RunConfig                          # from ConfigService, immutable after load
  sitemap: dict                              # {url: {flows: [...], links: [...], dom_hash: str}}
  exploration_plan: list[Scenario] | None    # None until frozen by plan node
  exploration_depth: int                     # hops explored from root
  exploration_complete: bool                 # set by LLM response in explore node
  current_scenario_idx: int
  current_step_idx: int
  current_action: BrowserAction | None
  accessibility_tree: dict | None            # refreshed each perceive node call
  screenshot_b64: str | None
  dom_hash: str | None                       # SHA-256 of structural a11y fields only
  current_url: str | None
  active_locator: LocatorSpec | None         # current target element spec
  locator_candidates: list[LocatorSpec]      # ranked alternatives from healing
  healing_mode: bool
  healing_attempts: int                      # resets per scenario
  last_healing_confidence: float
  last_verify_result: VerifyResult | None
  action_history: list[ActionRecord]         # episodic; trimmed to last 20 for LLM context window
  scenarios_complete: list[str]              # scenario IDs finished this run
  scenarios_blocked: list[str]              # scenario IDs blocked by escalated healing
  tokens_consumed: int
  token_budget: int
  cost_usd: float
  messages: Annotated[list[BaseMessage], add_messages]   # LangGraph message accumulator

NODES (8):

  perceive: Calls MCP browser_snapshot(set_of_marks=healing_mode). Writes accessibility_tree, screenshot_b64, dom_hash, current_url to state. If dom_hash changed since last step, appends observation to messages. No LLM call.

  route: Pure conditional dispatcher — zero side effects, no LLM call. Reads run_mode + healing_mode + exploration_complete + current_scenario_idx. Returns next node name. This is the graph's traffic cop: putting routing logic here keeps every other node single-purpose.

  explore: LLM node (Opus 4.8). System prompt establishes testing persona. User prompt: current a11y tree + sitemap so far + action_history + "Identify the next untested interactive flow and the single next action to take. If all reachable flows are explored, set exploration_complete=true." Structured output: {next_action: BrowserAction, flow_discovered: Flow, exploration_complete: bool, reasoning: str}. Updates sitemap, current_action, exploration_complete in state. Checks BudgetService before call.

  plan: Serializes exploration_plan to JSON. Calls Go PlanService.FreezePlan. Sets run_mode="replay". Calls plan-manager to pre-patch plan with any known healed locators from PersistenceService. Writes frozen_plan_id to state. Non-LLM node.

  act: Calls MCP browser_action({action_type, locator_spec: active_locator, value}) with 30s timeout. On success: appends to action_history, clears healing_mode. On ELEMENT_NOT_FOUND / ELEMENT_NOT_VISIBLE / ELEMENT_STALE: sets healing_mode=true, increments healing_attempts, captures failed locator context in state. No LLM call.

  verify: Calls MCP browser_assert({assertion_type, locator_spec, expected}) for the current step's expected outcome. On LOCATOR_NOT_FOUND: sets healing_mode=true. On assertion mismatch (element found but value wrong): records genuine test failure in last_verify_result — this is NOT healed, it IS a real defect. On success: clears healing_mode, advances step cursor. No LLM call.

  heal: LLM node (Sonnet 4.6 — faster and cheaper than Opus for this bounded task). Constructs healing prompt from failed locator + current a11y tree + set-of-marks screenshot + mark_map. Parses structured JSON response {healed_locator, confidence, reasoning, mark_id}. Applies confidence gate (see selfHealing). On confidence >= 0.60: updates active_locator in state, calls PersistenceService.RecordHealedLocator. On confidence < 0.60 or healing_attempts >= 3: sets scenario to BLOCKED, calls EventService.Emit(HEALING_ESCALATION).

  checkpoint: Calls LangGraph checkpointer.aput() — saves full RunContext snapshot. Calls EventService.Emit(CHECKPOINT, {run_id, scenario_idx, step_idx, dom_hash, tokens_consumed}). Non-LLM node.

  emit_event: Calls EventService.Emit with scenario result (PASS / FAIL / BLOCKED), full step log, token cost delta, healing events for this scenario. Advances scenario cursor or transitions to END. Non-LLM node.

EDGES (conditional):

  START → perceive
  perceive → route
  route → explore        when: run_mode="explore" AND NOT healing_mode AND NOT exploration_complete
  route → plan           when: run_mode="explore" AND exploration_complete
  route → act            when: run_mode="replay" or "heal_audit" AND NOT healing_mode
  route → heal           when: healing_mode=True
  explore → perceive     (loop: explore calls perceive after each action to observe result)
  plan → checkpoint → route
  act → verify
  verify → checkpoint    (success path: save progress)
  verify → heal          (LOCATOR_NOT_FOUND path)
  verify → emit_event    (assertion failure: genuine test failure, do not heal)
  checkpoint → route     (continue to next step or next scenario)
  heal → act             (confidence >= 0.60 AND healing_attempts < 3: retry with healed locator)
  heal → emit_event      (confidence < 0.60 OR healing_attempts >= 3: escalate)
  emit_event → route     (next scenario exists)
  emit_event → END       (all scenarios processed)

CHECKPOINTER: LangGraph AsyncSqliteSaver (dev/CI single-host) or AsyncPostgresSaver (K8s prod). Connection string from Go ConfigService. thread_id = run_id. Checkpoint on: every successful scenario, every heal event, every plan freeze. Enables: crash recovery without re-running completed scenarios; human-in-loop pause/resume in heal_audit mode.

**Self-healing.**

STEP 1 — FAULT DETECTION (TS locator-resolver, synchronous, before MCP response):
  When browser_action or browser_assert is called, locator-resolver tries all 5 strategies in hierarchy order before reporting failure. The MCP error response includes: error_type (ELEMENT_NOT_FOUND | ELEMENT_NOT_VISIBLE | ELEMENT_STALE), attempted_locators [{strategy, selector, tried_at_ms}] (all 5 attempted), action_type, page_url, screenshot_b64 at failure moment. This means healing starts with complete diagnostic context — no extra round-trip to TS to learn what was tried.

STEP 2 — PERCEPTION REFRESH (perceive node, targeted re-entry):
  Heal node triggers a fresh browser_snapshot(set_of_marks=true) call. TS snapshot-provider: (a) calls page.accessibility.snapshot() for the full structured a11y tree; (b) queries all interactive elements via page.$$('button,input,select,a,[role=button],[tabindex]'), draws numbered bounding boxes as a canvas overlay, screenshots, returns mark_map [{id, role, name, bbox, ariaLabel}]. This gives the LLM both structured semantic data AND spatial/visual context. Rationale: a11y tree alone misses canvas widgets, custom web components with poor ARIA, and elements in cross-origin iframes (documented limitation — flagged in run report). Combined channel is the strongest grounding signal available without injecting test IDs into the AUT.

STEP 3 — RE-GROUNDING (LLM call, Sonnet 4.6, structured output mode):
  Healing prompt (from self-healer.py):
  "HEALING REQUEST [run_id={X}, scenario={Y}, step={Z}]
   ORIGINAL ACTION: {action_type} on element — role={role}, accessible_name={name}, selector={original_selector}
   FAILED STRATEGIES: {attempted_locators as table}
   DOM HASH CHANGE: {original_dom_hash} → {current_dom_hash}
   CURRENT ACCESSIBILITY TREE: {formatted_tree}
   SET-OF-MARKS SCREENSHOT: [image]  MARK MAP: {mark_map}
   TASK: The element was restructured or renamed. Identify what we were targeting.
   RESPOND WITH JSON ONLY:
   {healed_locator: {strategy: 'aria|testid|text|css|xpath', value: str}, confidence: float, reasoning: str, mark_id: int|null}"
  Token budget check via BudgetService before call. If budget exceeded: skip healing, emit BUDGET_BLOCKED.

STEP 4 — LOCATOR STRATEGY HIERARCHY (preference order, from most to least stable):
  1. ARIA role + accessible name: page.getByRole('button', {name: 'Submit Order'}) — survives visual refactors; most stable under CSS/layout changes
  2. data-testid / data-cy / data-pw attribute: page.getByTestId('checkout-submit') — zero fragility when dev team uses test attributes; query PersistenceService.GetPageAttributes to know if AUT uses a test-attribute convention
  3. Exact visible text: page.getByText('Place Order', {exact: true}) — stable for unique label strings; fragile for i18n
  4. Scoped CSS — semantic container + element type: page.locator('form[aria-label="Checkout"] button[type="submit"]') — better than bare CSS because the semantic container is stable even if inner structure shifts
  5. XPath — structural path: page.locator('//form[@aria-label="Checkout"]//button[last()]') — last resort; fragile; only used when LLM returns no higher-confidence alternative
  After Python writes healed_locator to state, locator-resolver in TS validates the candidate is live before returning result to Python. A candidate that validates successfully but then fails on act is treated as a new healing cycle (healing_attempts incremented again).

STEP 5 — CONFIDENCE GATE AND BRANCHING:
  confidence >= 0.85: AUTO_HEAL — update active_locator in state, reset healing_attempts to 0 (we trust this), retry act node immediately. Emit HEALING_AUTO event to EventService.
  0.60 <= confidence < 0.85: FLAGGED_HEAL — update active_locator, retry act, emit HEALING_FLAGGED event. Scenario is marked HEALED_UNVERIFIED in run report. Appears in 'Healing Audit' section for human review post-run.
  confidence < 0.60 OR healing_attempts >= 3: ESCALATE — do NOT update locator, do NOT retry.
    CI mode: add scenario to scenarios_blocked list, skip to next scenario, mark run as PARTIAL_HEAL (not full FAIL — other scenarios continue).
    Service mode: call EventService.Emit(HEALING_ESCALATION, {scenario, step, context}); Go report-service triggers configured webhook (Slack / PagerDuty / custom); run pauses at this scenario until operator resumes or skips via API.
  healing_attempts resets to 0 on every new scenario (state reset in emit_event node).

STEP 6 — PERSISTENCE AND PROPAGATION (amortize healing cost):
  On any heal with confidence >= 0.60: Go PersistenceService.RecordHealedLocator stores {run_id, scenario_id, page_url, original_locator, healed_locator, confidence, reasoning, dom_hash_before, dom_hash_after, model_used, strategy_used, timestamp}.
  On next replay run startup: plan-manager calls PersistenceService.GetHealedLocators(target_url) and iterates frozen plan steps. For each step whose page_url + original_locator matches a healed record AND whose current dom_hash matches dom_hash_after: pre-patch the plan step with the healed locator. Healing is amortized — LLM paid once, result reused across all subsequent runs until DOM changes again (dom_hash mismatch triggers a new healing cycle).
  Stale healed locators (dom_hash_after no longer matches current page) are automatically evicted from pre-patch list and flagged in run report as STALE_HEALED_LOCATOR — prompts a new heal cycle rather than silently using outdated data.
  Audit: HEALING_FLAGGED entries appear in HTML report with: original vs healed locator diff, confidence, LLM reasoning verbatim, before/after a11y tree excerpt. heal_audit run mode replays ONLY FLAGGED scenarios in headed browser for human spot-check before merging healed locators into canonical plan.

**Determinism.**

CORE PATTERN: Explore-Once / Replay-Many. The LLM-driven exploration phase runs in a dedicated periodic job (nightly or on-demand), NOT on every CI trigger. It produces a frozen plan artifact stored in Go PersistenceService. The CI gate runs only the deterministic replay phase, which consumes the frozen plan. This is the fundamental separation: non-determinism is quarantined to the exploration job; the CI gate is a pure replayer.

FROZEN PLAN SCHEMA: The plan stored by PlanService.FreezePlan is a versioned JSON document: {plan_id, target_url, created_at, schema_version, scenarios: [{scenario_id, name, steps: [{step_id, action_type, locator_spec: {strategy, value}, value?, assertion_type?, expected?, page_url, dom_hash_at_record}]}]}. The dom_hash at record time is the structural fingerprint of the page when the step was captured. On replay, plan-manager computes the current dom_hash and emits DOM_DRIFT_WARNING if it differs — this is an early signal that the plan may need re-healing before running.

SEEDED SCENARIO ORDERING: For environments where exploration must re-run (preview deployments with ephemeral URLs), scenario ordering within the explore phase is derived from a SHA-256 hash of the target URL + a configurable seed string. This does not make LLM choices deterministic but makes scenario execution order deterministic across re-runs of the same explore job, preventing spurious ordering-related flakes.

GOLDEN STATE BASELINES: After every successful replay run, Go report-service writes a baseline entry: {page_url, dom_hash, a11y_tree_snapshot, timestamp, run_id}. On the NEXT replay run, perceive node fetches the baseline for each page and computes a structural diff. Diff beyond a configurable threshold (e.g., > 10% of nodes changed) triggers BASELINE_DRIFT_ALERT — not an automatic failure, but a signal that the plan may be stale.

HEALING AUDIT TRAIL AS CI ARTIFACT: Every HEALING_AUTO and HEALING_FLAGGED event is written to a JSONL artifact (healing-audit.jsonl) alongside the run report. CI uploads this as a build artifact. A run with too many AUTO heals (configurable threshold, e.g., > 5 per run) triggers a CI warning: high heal volume signals DOM churn requiring a fresh exploration.

FLAKE QUARANTINE: A scenario that fails with a non-healing error (assertion mismatch, not locator failure) in 2 of the last 3 replay runs is automatically added to the quarantine list in PersistenceService. Quarantined scenarios are skipped on subsequent runs and flagged for human triage. They are never silently removed from the plan — they remain visible in the report as QUARANTINED. Quarantine status is reset only by explicit operator action or a successful run after a plan re-freeze.

LLM-FREE REPLAY: In replay mode with a fully healed plan, the act and verify nodes make zero LLM calls (pure Playwright execution against explicit locators). The only LLM invocation in replay mode is a healing cycle, which is triggered only on live locator failure. A regression run on a stable AUT is therefore fully deterministic and has zero LLM token cost.

PLAN VERSION PINNING IN CI: CI config specifies an optional plan_id to pin. If plan_id is set, the run uses that exact frozen plan regardless of whether a newer one exists. This is the escape hatch for freezing a known-good plan across a release branch, preventing an upstream exploration job from silently changing the CI gate.

**Memory.**

SHORT-TERM (episodic, within a single run):
  The LangGraph RunContext TypedDict IS the short-term memory. It lives in-process in the Python brain and is serialized by the LangGraph checkpointer after each significant node transition. Key episodic fields: action_history (list of ActionRecord trimmed to last 20 entries for LLM context window), messages (Annotated accumulator of BaseMessage for the LLM conversation thread), current sitemap being built, healing attempts counter per scenario. Trimming action_history to 20 prevents unbounded context growth on long exploration runs; the full history is available via the EventService stream in Go for audit purposes.

CHECKPOINTER (mid-run persistence, recovery boundary):
  LangGraph AsyncSqliteSaver in single-host deployments (zero infra dependency, embedded in Python process, file at configured path). LangGraph AsyncPostgresSaver in K8s deployments. Connection string provided by Go ConfigService. thread_id = run_id. This is NOT the same as long-term memory — it is crash-recovery state. On Go orchestrator restart with a RUNNING run, it re-spawns the Python brain with the same run_id; LangGraph loads the last checkpoint and resumes from the last saved node. Checkpoint data is ephemeral: purged after run reaches COMPLETE or FAILED_UNRECOVERABLE.

LONG-TERM (cross-session, owned exclusively by Go persistence-gateway):
  Storage: SQLite file (dev / CI single-host, embedded in Go binary path via go:embed migrations), Postgres (K8s cluster). Storage choice is config-driven; persistence-gateway handles both with the same gRPC interface. Schema tables:

  sitemaps: {target_url, dom_hash, pages_json, flows_json, discovered_at, run_id}. Enables plan-manager to check if a target URL was explored recently and skip re-exploration if dom_hash is current.

  healed_locators: {id, page_url, original_locator_json, healed_locator_json, strategy, confidence, reasoning, dom_hash_before, dom_hash_after, model_used, run_id, created_at}. Primary lookup key: (page_url, original_selector). Drives plan pre-patching on replay startup. Index on dom_hash_after for fast stale detection.

  exploration_plans: {plan_id, target_url, schema_version, plan_json, created_at, run_id, status (active|archived)}. One active plan per target_url; previous plans archived, not deleted.

  page_baselines: {id, page_url, dom_hash, a11y_tree_json, created_at, run_id}. One per page per run; used for structural drift detection on next run.

  run_history: {run_id, target_url, run_mode, status, start_time, end_time, scenarios_total, scenarios_pass, scenarios_fail, scenarios_blocked, tokens_consumed, cost_usd, plan_id}. Summary for cost reporting and flake tracking.

  llm_traces: {id, run_id, scenario_id, node_name, model, prompt_tokens, completion_tokens, latency_ms, created_at}. NOT storing prompt/response content by default (cost + privacy); full content stored only if TRACE_FULL_LLM=true in config.

  flake_quarantine: {scenario_id, plan_id, failure_count, last_failure_at, quarantined_at, reason, status (quarantined|cleared)}.

RATIONALE FOR Go AS SOLE DB OWNER: Python direct DB access would couple the Python schema to the Go schema and create a multi-writer migration coordination problem. TS direct DB access would be even worse. The gRPC service layer is the enforced boundary: Go owns the schema, migrations, connection pooling, and query optimization. Python and TS consume typed service methods. Swap Python (e.g., to a Go agent) or TS (e.g., to Puppeteer): zero DB migration work.

**Observability.**

DISTRIBUTED TRACING (OpenTelemetry, all three layers):
  Go: otelgrpc interceptors on all gRPC services; trace context propagated in gRPC metadata. Spans for: run lifecycle state transitions, every gRPC service call, report rendering.
  Python: opentelemetry-sdk; custom instrumentor wraps every LangGraph node execution as a span (node_name, run_id, scenario_id, step_idx as attributes). LLM calls are child spans with token counts and latency. Trace context propagated to Go gRPC calls via metadata.
  TypeScript: opentelemetry-api in playwright-executor; each MCP tool call is a span with action_type, locator_strategy, success/failure, dom_hash.
  Exporter: OTLP to local collector (Jaeger or Grafana Tempo in homelab; configurable endpoint). All three processes export to the same collector; trace IDs are correlated via run_id propagated in gRPC metadata and as an MCP call metadata field.

LLM DECISION TRACING (Go persistence-gateway, llm_traces table):
  Every LLM call records: run_id, node_name, model, prompt_tokens, completion_tokens, latency_ms, created_at. Full prompt+response content stored only if TRACE_FULL_LLM env var set (disabled by default for cost and privacy). When enabled, content stored compressed (zstd) in a separate blob column. A CLI command (orch-cli trace replay --run-id=X) reconstructs the full decision sequence from llm_traces for post-mortem debugging.

PLAYWRIGHT TRACES (per-scenario, trace-controller):
  Playwright trace (network, DOM snapshots, screenshots, action timeline) captured per scenario via trace-controller. Stored as .zip files in configured artifacts directory. Run report HTML links each scenario to its trace file. Traces are viewable in Playwright Trace Viewer (local or hosted). Trace file paths written to EventService on scenario completion; report-service embeds them in output.

REPLAYABLE RUN TRANSCRIPTS:
  Go report-service writes a JSONL transcript per run: one line per EventService.Emit call, ordered by timestamp. Fields: {event_type, run_id, scenario_id, step_idx, node_name, timestamp, payload}. This is the full audit trail of every decision, action, verification, and healing event. A separate orch-cli replay-transcript command can replay the LLM decision sequence (without re-executing browser actions) for debugging. Transcripts are stored in the artifacts directory alongside traces.

TOKEN BUDGET AND COST:
  BudgetService enforces a per-run token cap (configurable, suggested defaults: explore run=200K tokens, replay run=20K tokens since LLM is only for healing). Before each LLM call, Python calls BudgetService.ConsumeTokens; if denied, emits BUDGET_EXCEEDED, skips LLM call, marks scenario as BUDGET_BLOCKED (not FAIL). Go cost-tracker computes cost_usd = sum(input_tokens * input_price + output_tokens * output_price) per model tier. Price table is config-file driven — never hard-coded. Run report shows: cost per run, cost per scenario, cost breakdown by node type (explore vs heal). Trend data in run_history table enables per-week cost reporting via orch-cli cost --since=7d.

METRICS ENDPOINT (Go, Prometheus):
  Go grpc-server exposes /metrics on a separate port: run_count (by status), healing_events_total (by confidence bucket), token_budget_utilization, scenario_duration_seconds (histogram), cost_per_run_usd. Grafana dashboard template shipped in repo. Homelab ArgoCD deploys the full stack with pre-wired Prometheus scrape config.

**Outputs.**

1. RUN REPORT (HTML + JSON + JUnit XML): HTML report includes: run summary (pass/fail/blocked/quarantined counts, total cost, duration), per-scenario expandable section (steps, assertions, healing events with LLM reasoning, Playwright trace link), healing audit section (all FLAGGED heals for human review), baseline drift warnings, flake quarantine additions. JSON report is machine-readable for CI integration. JUnit XML for GitHub Actions / GitLab CI test result visualization. All three generated by Go report-service from EventService stream.

2. PLAYWRIGHT TEST CODE (.spec.ts): codegen-exporter emits a Playwright TypeScript test file per scenario during replay runs (codegen_start / codegen_stop wraps each scenario). Output uses idiomatic test() / expect() wrappers, named via scenario_id and flow name. File is importable directly into an existing Playwright project without modification. This is the primary handoff artifact to the existing qa-automation-engineer workflow: the agent generates the initial test skeleton; engineers maintain it.

3. PLAYWRIGHT TRACES (.zip per scenario): Binary trace archives containing full network log, DOM snapshots, screenshots, and action timeline. Viewable in Playwright Trace Viewer. Referenced by filename in HTML report. Retained per-run in artifacts directory; retention policy configurable (default: keep last 10 runs).

4. HEALING AUDIT JSONL (healing-audit.jsonl): Ordered log of every healing event in the run: {event_type, scenario_id, step_idx, original_locator, healed_locator, confidence, reasoning, dom_hash_before, dom_hash_after, timestamp}. CI uploads as build artifact. Enables async human review without re-running the agent.

5. FROZEN EXPLORATION PLAN (JSON, stored in Go persistence-gateway + exported): The canonical test plan produced by exploration — the artifact that makes CI deterministic. Also exportable as a file artifact for version control alongside the codebase.

6. REGRESSION BASELINES (a11y tree snapshots, stored in Go persistence-gateway): Per-page structural snapshots taken after each successful replay run. Input to the next run's DOM drift detection. Not a human-facing artifact but a first-class system artifact.

7. SARIF REPORT (optional, Go report-service): Static Analysis Results Interchange Format output for integration with GitHub Code Scanning or similar. Maps genuine test failures (assertion mismatches, not healing failures) to SARIF results. Disabled by default; enabled via --sarif flag.

8. COST SUMMARY (JSON + stdout): Appended to run report and emitted to stdout on completion: {run_id, total_cost_usd, tokens_by_model, cost_by_node_type, runs_this_week_cost_usd}. Designed to be captured by CI and posted as a PR comment via orch-cli cost-comment.

**MVP path.**

PHASE 1 — TypeScript MCP Server in isolation (Week 1-2):
  Build playwright-executor as a standalone MCP server with 4 tools: browser_navigate, browser_snapshot, browser_action, browser_assert. Test it with any MCP client (Claude Code, curl-MCP). Validate that page.accessibility.snapshot() returns sufficient signal on a real SPA target. Validate set-of-marks overlay generation. Implement locator-resolver with full 5-strategy hierarchy. Establish contracts/mcp-schema JSON Schema. This phase proves the perception channel before committing to the full architecture. Deliverable: npx playwright-executor command that serves MCP over stdio.

PHASE 2 — Go control plane skeleton (Week 2-3):
  orch-cli binary with config parsing (YAML). run-orchestrator that spawns a subprocess (initially a mock Python brain script) and monitors it. grpc-server with ConfigService and EventService stubs. persistence-gateway with SQLite and schema migrations for run_history. report-service generating basic JSON output from events. Integration test: Go → spawn TS executor → issue navigate + snapshot → receive event → write report. Establishes the process supervision and gRPC infrastructure. Deliverable: orch-cli run --config=run.yaml produces a JSON report from a hardcoded action sequence.

PHASE 3 — Python LangGraph basic loop (Week 3-5):
  LangGraph StateGraph with 4 nodes: perceive → act → verify → emit_event (no explore or heal yet). Python connects to TS via MCP client subprocess. Python connects to Go via gRPC (ConfigService + EventService). Hardcoded test scenario (no LLM) to validate full plumbing end-to-end. Then add LLM-driven act node: LLM chooses action from a11y tree. Add BudgetService gRPC and BudgetService.ConsumeTokens check before LLM call. Deliverable: three-process run that navigates a target URL, performs LLM-chosen actions, and produces a run report.

PHASE 4 — Explore → Plan freeze → Replay (Week 5-7):
  Add explore node (LLM-driven sitemap building). Add plan node with PlanService gRPC (FreezePlan / GetFrozenPlan). Add plan-manager for healed-locator pre-patching. Implement replay run mode (no LLM in act/verify unless healing). Implement dom_hash computation and DOM_DRIFT_WARNING. Validate the core CI determinism claim: run explore once, run replay 10 times, confirm identical scenario execution. Deliverable: CI-reproducible replay runs against a staging environment.

PHASE 5 — Self-healing (Week 7-9):
  Add heal node to LangGraph. Full locator strategy hierarchy in locator-resolver. Confidence gate implementation. PersistenceService.RecordHealedLocator + GetHealedLocators. Plan pre-patching with healed locators. Flake quarantine logic. HEALING_ESCALATION event + webhook stub. heal_audit run mode. Deliverable: agent that automatically repairs broken locators and persists repairs for future runs.

PHASE 6 — Full observability and artifact pipeline (Week 9-11):
  OpenTelemetry across all three layers (OTLP export). trace-controller in TS (Playwright traces). codegen-exporter producing .spec.ts output. Full HTML report with healing audit section. SARIF output. cost-tracker with price table. orch-cli cost --since=7d. Prometheus /metrics endpoint. Grafana dashboard template. JUnit XML for CI integration. Healing-audit JSONL artifact. Deliverable: production-grade run artifacts suitable for engineering team consumption.

PHASE 7 — K8s / ArgoCD deployment (Week 11-12):
  Helm chart with three Deployments (Go orchestrator + HTTP API, Python brain pool, TS executor pool). Go persistence-gateway with Postgres backend (external). AsyncPostgresSaver for LangGraph. ArgoCD Application manifest. Horizontal pod autoscaler for Python brain (scale by run queue depth). Per-namespace config for dev/staging/prod target URLs. Deliverable: homelab-deployable stack with ArgoCD GitOps.

**Key risks:**

- Accessibility tree coverage gaps: shadow DOM, canvas elements, custom web components, and cross-origin iframes often produce incomplete or absent entries in page.accessibility.snapshot(). The agent will have partial perception of modern SPAs. Mitigation: document known-blind-spots per AUT in run report; fall back to DOM snapshot + CSS selector for elements absent from a11y tree; consider Playwright's aria snapshots (verify availability in current Playwright version) as an alternative API.
- Exploration non-determinism leaking into plan: the LLM exploration phase may produce materially different frozen plans across re-runs (different flow discovery order, different scenario naming), making plan_id comparisons unreliable for diffing. Mitigation: plan diffing is structural (compare step action_type + page_url, not plan_id equality); exploration is a separate job run infrequently; plan re-freeze is explicit operator action, not automatic.
- Token cost explosion on large SPAs: a 50-page SPA with rich interactive flows can consume 300K+ tokens in a single exploration run if depth cap is not enforced. At Opus 4.8 prices this is non-trivial. Mitigation: exploration_depth hard cap in config (default 3 hops from root); per-page token budget; BudgetService BUDGET_EXCEEDED halts exploration early with a partial plan rather than failing the run; incremental exploration (only pages not in existing sitemap).
- Proto versioning coordination cost: Python brain evolves fast (new LangGraph nodes, new gRPC calls to new PersistenceService methods); each addition requires proto change + Go recompile + coordinated deploy. In a fast iteration cycle this creates friction. Mitigation: design all proto messages with optional fields and oneof; maintain backwards compatibility for at least one major version; proto is the explicit price of the hexagonal boundary — accept the friction, automate stub generation in CI.
- LangGraph checkpointer infra dependency: AsyncPostgresSaver requires a live Postgres instance; in K8s this means the Postgres connection must be healthy before the Python brain can checkpoint. A Postgres outage mid-run means the run cannot checkpoint but can still execute. Mitigation: make checkpointing non-blocking (checkpoint failures emit a WARNING event but do not halt execution); SQLite checkpointer is always available as fallback on single-host.
- MCP stdio protocol has no flow control: if Python issues browser_action calls in a tight loop (which LangGraph nodes could do via subgraph parallelism), the stdio pipe has no backpressure. Mitigation: current design is inherently sequential per LangGraph node — one outstanding MCP call at a time; if parallelism is added later (parallel scenario execution), each parallel branch must use a separate playwright-executor subprocess with its own stdio channel.
- DOM hash fragility as baseline and locator pre-patch anchor: computing SHA-256 over the serialized a11y tree means any change to any element (even unrelated to the tested flow) invalidates healed locators for the whole page. Mitigation: compute dom_hash only over the subtree rooted at the scenario's target container element (derived from scenario metadata) rather than the full page; make hash scope configurable; document that volatile widgets (analytics, ads, A/B test banners) should be excluded via a CSS ignore-list in config.

**ADRs:**

- ADR-001: MCP JSON-RPC 2.0 over stdio for Python-to-TypeScript boundary. Accepted: MCP stdio — subprocess lifecycle trivially owned by Python Popen, zero port management, schema-validated tools via JSON Schema at connect time, matches established Claude ecosystem tooling pattern, inspectable with any JSON-RPC debugger. Rejected: HTTP JSON-RPC local server — requires port allocation (CI port conflicts are real in containerized environments), independent health-check polling loop, separate process synchronization mechanism; more failure modes for what is logically an in-process function call.
- ADR-002: gRPC + protobuf for Go-to-Python boundary. Accepted: gRPC — contract enforced at compile time via generated stubs for both languages from shared .proto source; binary-efficient; native streaming on EventService enables real-time report rendering without polling; interceptors provide clean OpenTelemetry injection point. Rejected: REST/JSON — no compile-time schema enforcement between two independently-implemented endpoints; drift between Go handler and Python client is a when-not-if problem; no native streaming without SSE complexity.
- ADR-003: Go as control-plane spine (not Python). Accepted: Go — single static binary for CI distribution with zero runtime dependencies; goroutine-based process supervisor handles Python brain + TS executor lifecycle cleanly; fast startup (< 50ms vs Python cold start); no GIL; native Prometheus instrumentation. Rejected: Python as orchestrator — mixing agent reasoning logic and process supervision in the same Python process eliminates the hexagonal boundary between brain and control plane; Python startup overhead and package management complexity make it a poor choice for a CI binary.
- ADR-004: LangGraph StateGraph as agent backbone (not bespoke async loop). Accepted: LangGraph — built-in checkpointing (AsyncSqliteSaver/AsyncPostgresSaver) eliminates a major implementation concern; conditional edges express heal/retry/escalate branching declaratively; human-in-loop pause/resume is first-class; state object is typed and inspectable; streaming is native. Rejected: bespoke async Python loop — must reimplement checkpointing, crash recovery, human-gate, streaming, and state serialization; grows in complexity with every new node and edge; becomes a liability when LangGraph adds new features (parallel branches, subgraphs) that the bespoke loop cannot adopt.
- ADR-005: Accessibility tree as primary perception channel, augmented by set-of-marks screenshot. Accepted: a11y tree (page.accessibility.snapshot()) + set-of-marks screenshot — a11y tree gives structured semantic data (role, name, state) optimal for locator generation; set-of-marks overlay gives spatial/visual grounding for elements the LLM needs to reason about positionally; combined channel is the strongest available without injecting test instrumentation into the AUT. Rejected primary alternative A: full DOM snapshot — too large for LLM context window on a real SPA (100K+ tokens of HTML); too noisy (styles, scripts, metadata); locator generation from raw DOM produces fragile CSS selectors. Rejected primary alternative B: screenshot-only — loses structured semantic data needed for deterministic locator generation; forces pixel-level reasoning which degrades on dynamic content and is expensive per call.
- ADR-006: Explore-once / replay-many for CI determinism. Accepted: separate non-deterministic exploration phase (periodic job) produces a frozen plan; deterministic replay phase (every CI trigger) consumes the frozen plan; LLM is only invoked in CI for healing, not exploration. Rejected: LLM-seeded exploration on every CI run — LLMs expose no stable public seeding API; minor model version bumps (which happen without notice) change output even with a fixed seed; this approach cannot provide the reproducibility guarantee the CI gate requires. Rejected: record-and-replay of browser HTTP traffic — brittle against any backend change; tests UI behavior superficially without agent reasoning; does not solve the self-healing problem.
- ADR-007: Go persistence-gateway as exclusive DB owner. Accepted: all long-term state (healed locators, sitemaps, plans, baselines, run history) accessed exclusively via Go gRPC PersistenceService; Python and TS never hold DB connections directly. Rejected: Python direct DB access (e.g., SQLAlchemy) — two language runtimes co-owning the same schema creates migration coordination: Go runs schema migration on startup, Python must not run concurrent migrations; connection pool management across two runtimes is error-prone; violates the hexagonal principle that the persistence port has one owner.
- ADR-008: Sonnet 4.6 for healing / Opus 4.8 for exploration, model per node type. Accepted: differentiated model selection by node — Opus 4.8 (higher reasoning) for explore node where open-ended planning quality drives coverage; Sonnet 4.6 (faster + cheaper) for heal node where the task is bounded (match one element from a structured tree) and latency matters (healing is in the critical path of test execution). Rejected: uniform Opus 4.8 for all nodes — healing runs potentially dozens of times per run; Opus latency in the heal→act→verify loop would make runs unacceptably slow and expensive; the bounded nature of healing does not require full Opus reasoning capability.

---

### `agentic-core` — CognitivePilot — Perception-First Autonomous UI Test Agent

*Lens: Cognitive / agentic-loop first*

**Philosophy.** The cognitive loop is the product: every architectural decision — language choice, wire protocol, storage schema — exists to serve the LangGraph perception→plan→act→verify→heal cycle running in Python. The agent treats the accessibility tree as its primary sense organ because semantic ARIA structure outlasts DOM churn far better than CSS selectors, and when the tree fails (canvas-heavy apps, shadow DOM, custom elements), set-of-marks visual grounding gives the LLM spatial anchoring without falling back to brittle XPath. Go is the deterministic spine that makes the non-deterministic LLM brain trustworthy in CI: it owns plan freezing, locator persistence, budget enforcement, and the human gate. TypeScript is the dexterous hands: Playwright exposed as a native MCP tool server so the LLM calls browser primitives exactly the way it calls any other tool, with zero impedance mismatch between the agent reasoning loop and the browser execution layer.

**Components (15):**

| Component | Language | Responsibility |
|-----------|----------|----------------|
| agent-ctl | Go | CLI entrypoint: run start/pause/stop/inspect/export commands; YAML + env config management; outputs structured run status and report links; single binary distributed to CI runners |
| orchestrator | Go | Run lifecycle FSM (PENDING→RUNNING→HEALING→PAUSED→DONE/FAILED); gRPC server facing Python brain; watchdog timer detecting brain stream termination; scheduling queue; routes HumanGateEvent and budget alerts to event-router |
| persistence-gateway | Go | gRPC service over SQLite (single-node) or PostgreSQL (multi-runner); owns healed-locator store, sitemap adjacency graph, frozen plan store, run history, page-object cache; single writer serialises all cross-runner writes |
| report-service | Go | Artifact collection REST API; HTML + JSON run report generation; Playwright trace static file serving at /ui/traces/{run_id}; Prometheus /metrics endpoint; LLM transcript JSONL archive |
| event-router | Go | Internal pub/sub (in-process channels + optional webhook egress); routes HumanGateEvent to Slack/webhook; emits budget-consumed alerts; exposes /api/healing/{id} for human resolution in long-running service mode |
| brain | Python | LangGraph state machine: 8 nodes (perceive, ground, plan, act, verify, heal, checkpoint, report) with conditional edge routing; LangGraph SQLite/PostgreSQL checkpointer for resumable runs; owns AgentState TypedDict; entry point spawned by Go orchestrator via gRPC StartRun |
| perception | Python | Accessibility tree parser + normaliser; completeness scorer (ratio of interactive ARIA roles to total nodes); set-of-marks coordinator (delegates screenshot capture to TS MCP, adds numbered overlay logic in Python); DOM snapshot normaliser; decides which perception modality to use per cycle |
| planner | Python | LLM prompt templates (Opus 4.8 for explore/plan, Sonnet 4.6 for verify/heal); structured output schema enforcement for TestAction and Verdict; plan serialiser/deserialiser for freeze/thaw; budget guard that blocks LLM calls when remaining tokens are below threshold |
| locator-grinder | Python | 6-tier strategy-rotation engine (data-testid → aria-label → role+name → text+role → CSS → XPath); locator resolution via MCP resolve_locator calls; confidence scorer with per-strategy base scores; healing proposal aggregator across all three re-grounding attempts |
| memory-client | Python | gRPC client to Go persistence-gateway; fetches known_locators and sitemap_fragment for current url_pattern on perceive entry; flushes HealedLocatorRecord and SitemapEdge on checkpoint; manages 10-minute LRU in-process cache for locator records |
| playwright-mcp-server | TypeScript | MCP server (stdio transport) exposing 12 Playwright tools: navigate_to, get_accessibility_tree, take_screenshot, click_element, fill_input, get_dom_snapshot, resolve_locator, start_trace, stop_trace, scroll_to, wait_for_navigation, get_network_log; the complete browser API surface as an MCP tool catalogue |
| browser-controller | TypeScript | Playwright Browser/BrowserContext/Page lifecycle; context isolation per test run; auth state injection (cookie jar / storage state from Go vault reference); viewport, locale, and network condition configuration; browser process supervision |
| snapshot-service | TypeScript | Accessibility tree capture via Playwright snapshot API; DOM serialisation with shadow DOM flattening; screenshot capture in PNG; returns structured JSON to Python via MCP tool responses; measures capture latency for observability |
| set-of-marks-renderer | TypeScript | Enumerates all interactive elements on current page; renders numbered rectangular marks as canvas overlay on screenshot; returns annotated image (base64) + element-index map (index → {role, name, bounding_box}) to Python for visual grounding in heal node |
| trace-emitter | TypeScript | Playwright trace lifecycle management (start/stop/archive); packages trace ZIP per run segment; HTTP POST artifact push to Go report-service with retry + exponential backoff; attaches OTEL span referencing trace artifact ID |

**Boundaries (polyglot contracts).**

Go ↔ Python — gRPC (proto3), bidirectional streaming:

GoOrchestrator → PythonBrain RPC calls: StartRun(RunConfig), PauseRun(run_id), StopRun(run_id), InjectHumanDecision(healing_id, approved_locator_or_skip). PythonBrain → GoOrchestrator streamed RunEvents: {type: STEP_COMPLETE | HEAL_NEEDED | HUMAN_GATE | BUDGET_WARNING | DONE | ERROR, payload_json}. PythonBrain → GoPersistenceGateway RPC calls: GetLocators(url_pattern) → LocatorList, SaveHealedLocator(HealedLocatorRecord) → void, GetFrozenPlan(plan_hash) → PlanJSON, SaveFrozenPlan(PlanJSON) → plan_hash, GetSitemap(url_pattern) → SitemapNode, UpdateSitemap(SitemapEdge) → void, GetRunHistory(run_id) → RunSummary. Rationale: gRPC gives typed contracts, Go-generated stubs from .proto are the single source of truth for the Go↔Python contract, streaming is natural for the continuous run-event flow. Failure isolation: Go watchdog expects a STEP_COMPLETE or keepalive event every 60s; stream silence → mark run FAILED, store last checkpoint ref for resume via StartRun(resume_from=checkpoint_id).

Python ↔ TypeScript — MCP (Model Context Protocol, JSON-RPC 2.0 substrate) over stdio:

TS playwright-mcp-server implements the MCP server spec; Python brain binds all 12 tools via the MCP client integration in LangGraph (available via langchain-mcp or direct MCP client — verify current package name). Transport: stdio for same-pod deployment (Python subprocess or sidecar container sharing a named pipe); switchable to HTTP+SSE for distributed multi-runner mode by changing MCP client transport config only. Rationale over gRPC for this boundary: MCP is the native protocol for LLM tool-calling — LangGraph can bind MCP tools directly as agent tools without an adapter layer; the TS layer IS semantically a tool server; writing a bespoke gRPC service would require a translation layer (gRPC proto → Playwright calls) that MCP eliminates; structured tool input/output schemas validate at both ends automatically. Failure isolation: per-tool timeout (navigate_to: 30s, get_accessibility_tree: 10s, click_element: 15s) caught in Python act/perceive nodes; tool error → route to heal node; repeated MCP connection drop → escalate BRAIN_ERROR RunEvent to Go orchestrator via gRPC.

TypeScript → Go — REST/HTTP:

POST /api/artifacts/trace (multipart ZIP), POST /api/artifacts/screenshot (JSON + base64 ref), GET /health (called by Go browser-supervisor). Rationale: artifact push is infrequent, one-way, involves large binary blobs — REST is simpler and more debuggable than gRPC streaming for blob upload; TS trace-emitter queues locally and retries with exponential backoff; Go report-service endpoint is idempotent on run_id + artifact_type.

**Agent loop.**

LangGraph explicit state machine. 8 nodes, typed conditional edges, SQLite-backed checkpointer (switchable to PostgreSQL for multi-runner).

─── SHARED STATE: AgentState (TypedDict) ───

run_id: str
session_id: str
mode: Literal["explore", "replay", "heal"]
config: RunConfig  # base_url, token_budget_per_model, auth_config, tags, plan_hash_override

# Perception
current_snapshot: PageSnapshot  # a11y_tree_json, screenshot_b64, marks_overlay, url, ts, completeness_score
snapshot_history: Deque[PageSnapshot]  # ring buffer max=10; evict+summarise via LLM when full

# Planning
plan_hash: str  # SHA-256 of frozen plan; empty string in explore mode
pending_actions: Deque[TestAction]  # {action_type, target_semantic_id, target_description, locator_hint, expected_outcome, reasoning}
completed_tests: List[TestResult]  # {test_id, verdict, confidence, healed, artifact_refs}

# Episodic memory (session)
action_history: List[ActionRecord]  # last 20 full records; older replaced by LLM-generated episode summaries
page_visit_log: List[UrlVisit]  # {url_pattern, visit_count, last_ts}

# Long-term memory refs (fetched, not stored inline — LRU cache in memory-client)
known_locators: Dict[str, LocatorRecord]  # semantic_id → {strategy, value, confidence, healed_at}
sitemap_fragment: Optional[SitemapNode]  # outbound edges from current url_pattern

# Healing
healing_context: Optional[HealingContext]  # {semantic_id, failure_type, attempts_log}
healing_attempts: int  # reset to 0 on each new action

# Budget
token_usage: Dict[str, TokenCount]  # model_id → {prompt_tokens, completion_tokens, cost_usd}
token_budget: Dict[str, int]  # model_id → max_tokens_per_run (hard cap)

# Artifacts
artifact_refs: List[ArtifactRef]  # {type: trace|screenshot|report, url}

# Control
stop_signal: bool
human_gate_pending: bool

─── NODES ───

perceive — Entry point for every cycle. Calls MCP tools in priority order:
(1) get_accessibility_tree() → parse into normalised AccessibilityNode tree → compute completeness_score = (interactive_role_count / total_node_count). If completeness_score >= 0.30: primary modality is a11y tree. (2) If completeness_score < 0.30 (canvas-heavy apps, custom elements): call take_screenshot() → set-of-marks-renderer overlays numbered marks → store as SetOfMarks {annotated_image_b64, index_map}. (3) get_dom_snapshot() called only when entering heal node — stored to state but NOT passed to LLM (token cost; used only structurally). Updates current_snapshot, appends to snapshot_history ring buffer. When ring buffer full: oldest entry summarised via Sonnet 4.6 into 200-token episode summary appended to action_history; slot freed.

ground — Runs before act when pending_actions is non-empty. Extracts required locator from pending_actions[0]: {strategy, value, semantic_id}. Checks state.known_locators[semantic_id] from memory-client cache. Calls MCP resolve_locator(strategy, value) to verify live validity against current DOM. If valid → emit GROUNDED, route to act. If invalid → populate healing_context with {semantic_id, failure_type: GROUNDING_FAILED, last_locator}, increment healing_attempts, route to heal.

plan — LLM node (Opus 4.8 in explore mode; bypassed in replay mode). Input to LLM: current_snapshot.a11y_tree_json (or marks_overlay.annotated_image_b64 if completeness low), summarised action_history (last 5 full + episode summaries), sitemap_fragment (unexplored edges highlighted), remaining_budget_pct. Prompt role: autonomous QA engineer exploring base_url; output: structured TestAction {action_type: CLICK|FILL|NAVIGATE|ASSERT_TEXT|ASSERT_VISIBLE, target_semantic_id, target_description, locator_hint, expected_outcome, reasoning} OR sentinel DONE. Structured output enforced via response schema. Temperature=0 in CI/replay; 0.3 in explore. Appends TestAction to pending_actions; updates token_usage. In REPLAY mode: loads next TestAction from frozen plan JSON instead of calling LLM — plan node is a pure data fetch, zero LLM cost.

act — Pops pending_actions[0]. Maps TestAction.action_type → MCP tool call sequence: CLICK→click_element(resolved_locator); FILL→fill_input(locator, value) then optionally click_element(submit_locator); NAVIGATE→navigate_to(url); ASSERT_TEXT / ASSERT_VISIBLE → inline check against current_snapshot a11y tree (no browser call needed if snapshot is fresh < 2s). Awaits MCP ActionResult {success: bool, error_type: Optional[str], artifact_refs}. On success: append ActionRecord to action_history, update artifact_refs, route to verify. On failure: populate healing_context {semantic_id, failure_type: error_type, attempted_locator}, increment healing_attempts, route to heal.

verify — LLM node (Sonnet 4.6). Input: TestAction.expected_outcome + ActionResult fields + targeted accessibility tree excerpt (subtree rooted at acted-upon element, not full tree — cap at 2K tokens). LLM outputs Verdict {result: PASS|FAIL|AMBIGUOUS, confidence: float 0-1, reasoning: str}. Records TestResult to completed_tests. PASS → checkpoint. FAIL where reasoning implicates wrong element (locator confusion) → route to heal with failure_type: ASSERTION_MISMATCH. FAIL where reasoning implicates app logic → record as genuine FAIL, route to checkpoint. AMBIGUOUS → record with warning, route to checkpoint (do not block run).

heal — Multi-strategy node. See selfHealing for full algorithm. Outputs HealedLocator {strategy, value, confidence} or HealingFailure. On success (confidence >= 0.60): update state.known_locators[semantic_id], push HealedLocatorRecord to Go persistence-gateway via memory-client gRPC, route to ground for retry. On confidence 0.60–0.84: persist with review_required=true, emit HEAL_NEEDED RunEvent to Go orchestrator, continue run. On failure or confidence < 0.60: emit HUMAN_GATE RunEvent to Go orchestrator with full HealingContext; set human_gate_pending=true; in non-interactive CI: record SKIPPED_HEALING_FAILURE, route to checkpoint. If healing_attempts >= 3 for same semantic_id: emit QuarantineEvent, mark QUARANTINED in persistence, route to checkpoint unconditionally.

checkpoint — Serialise full AgentState to LangGraph checkpointer backend. Tag: {run_id, step_id, plan_hash, token_usage_snapshot, ts}. Reset healing_attempts=0, healing_context=None. Evaluate terminal conditions: stop_signal=true OR all models at budget cap OR (pending_actions empty AND LLM returned DONE). Non-terminal → perceive. Terminal → report.

report — Terminal node. Aggregates completed_tests, healed_locators diff, token_usage, artifact_refs. HTTP POST /api/runs/{run_id}/complete to Go report-service with full RunSummary JSON. Generates exported TypeScript page-object stubs from stable known_locators (confidence >= 0.85, not quarantined). Emits final gRPC RunEvent{DONE, summary} to Go orchestrator.

─── EDGES ───

START → perceive
perceive → ground  (when pending_actions non-empty)
perceive → plan    (when pending_actions empty)
ground  → act      (GROUNDED)
ground  → heal     (GROUNDING_FAILED)
plan    → act      (action appended to pending_actions)
plan    → report   (DONE signal OR budget_exhausted)
act     → verify   (ACT_SUCCESS)
act     → heal     (ACT_FAILED)
verify  → checkpoint (always — verdict recorded)
heal    → ground   (confidence >= 0.60, healing_attempts < 3, retry)
heal    → checkpoint (confidence 0.60–0.84, flagged-persist path OR human_gate skip path)
heal    → checkpoint (quarantine: healing_attempts >= 3)
checkpoint → perceive (non-terminal)
checkpoint → report   (terminal conditions met)

**Self-healing.**

Step-by-step algorithm executed inside the heal node, driven by HealingContext {semantic_id, failure_type, attempted_locator, element_description}.

STEP 1 — FAILURE PERCEPTION (re-ground the agent's model of the page):
Do not reuse current_snapshot — the DOM has changed since the failed action. Call perceive sub-routine immediately: get_accessibility_tree() → recompute completeness_score. If completeness_score < 0.30: also call take_screenshot() + set-of-marks-renderer. Load healing history from state.known_locators[semantic_id] (prior strategies tried, prior confidence scores, quarantine flag). If element is already QUARANTINED: skip all attempts, record SKIPPED_QUARANTINE, return to checkpoint immediately.

STEP 2 — RE-GROUNDING ATTEMPT 1: Strategy rotation on fresh snapshot:
Walk the six-tier locator hierarchy for the target semantic_id, calling MCP resolve_locator(strategy, value) for each. Stop at first live match.
Tier 1: data-testid attribute match → confidence_base = 1.00
Tier 2: aria-label exact match → confidence_base = 0.95
Tier 3: ARIA role + accessible name combination → confidence_base = 0.90
Tier 4: visible text content + role → confidence_base = 0.80
Tier 5: CSS structural selector (nth-of-type, class+tag) → confidence_base = 0.60
Tier 6: XPath → confidence_base = 0.45
Result confidence = confidence_base × 0.95 (discount for changed DOM context). If match found at Tier >= 5: emit strategy_degradation metric — signals structural DOM instability. Record attempt1_confidence = result confidence or 0 if no match.

STEP 3 — RE-GROUNDING ATTEMPT 2: LLM accessibility tree reasoning:
Triggered only if attempt 1 finds no match. Pass fresh a11y_tree_json to Sonnet 4.6 with structured prompt: semantic_id description, element_description from HealingContext, full accessibility tree JSON (truncated to 8K tokens if necessary — inner subtree around likely element area first). LLM outputs structured HealingProposal {locator_strategy, locator_value, confidence_0_to_1, reasoning}. Apply empirical discount: attempt2_confidence = LLM_confidence × 0.90 (correction for systematic LLM overconfidence). If attempt2_confidence >= 0.55: call MCP resolve_locator(proposal.strategy, proposal.value) to verify proposal is actually present in live DOM before accepting — if not found, set attempt2_confidence = 0.

STEP 4 — RE-GROUNDING ATTEMPT 3: Set-of-Marks visual grounding:
Triggered only if attempt2_confidence < 0.55. Requires completeness_score < 0.50 OR failure_type == ELEMENT_NOT_FOUND with empty strategy rotation. Call take_screenshot() (or reuse from step 1 if < 5s old) + set-of-marks-renderer → annotated image with numbered interactive element marks + index_map. Pass annotated image to Sonnet 4.6 vision with prompt: "Previously this agent interacted with [element_description: role, label, visual context]. Which numbered mark in this screenshot most likely corresponds to that element? Output: mark_number, confidence_0_to_1, reasoning." LLM identifies mark_number → look up index_map[mark_number] → extract locator from accessibility tree node at that index. attempt3_confidence = llm_visual_confidence × 0.85.

STEP 5 — CONFIDENCE AGGREGATION:
final_confidence = max(attempt1_confidence, attempt2_confidence, attempt3_confidence).
winning_strategy = strategy from the attempt that produced final_confidence.
Persistence gate:
  >= 0.85 → auto-persist to healed-locator store via memory-client gRPC; continue run without pause; increment healed_auto counter in metrics
  0.60–0.84 → persist with review_required=true flag; emit HEAL_NEEDED RunEvent to Go orchestrator; emit healing_degraded metric; continue run (non-blocking)
  < 0.60 → do NOT persist (would pollute locator store with bad data); emit HUMAN_GATE RunEvent with {run_id, semantic_id, all_attempts_log, best_proposal_if_any, snapshots_refs}; set human_gate_pending=true; in CI non-interactive mode: record SKIPPED_HEALING_FAILURE; route to checkpoint

STEP 6 — PERSISTENCE AND AUDIT TRAIL:
Write HealedLocatorRecord to Go persistence-gateway: {id, url_pattern, semantic_id, element_role, element_label, old_locator_strategy, old_locator_value, new_locator_strategy, new_locator_value, confidence, healed_at, run_id, review_required, reasoning_transcript_ref}. Reasoning transcript ref points to the JSONL entry in the run transcript archive (not stored inline — too large). Emit OTEL span with all HealedLocatorRecord fields as span attributes. Update state.known_locators[semantic_id] in-process.

STEP 7 — QUARANTINE THRESHOLD:
After persisting (or skipping): increment healing_attempts. If healing_attempts >= 3 for the same semantic_id within this run: emit QuarantineEvent to Go event-router with {semantic_id, run_id, attempts_log}. Mark element QUARANTINED in persistence. All subsequent tests in this run referencing this semantic_id are immediately skipped with status SKIPPED_QUARANTINE — no further LLM calls for this element. Go report-service includes quarantine_count in run report summary. CI pipeline fails only if quarantine_count > configurable threshold (default: 5).

**Determinism.**

The core contract: explore once, replay deterministically, heal with a full audit trail.

PLAN FREEZING:
First run executes in "explore" mode. After the report node completes, the full sequence of TestAction objects (with resolved locators, assertion schemas, expected outcomes) is serialised to JSON. SHA-256(plan_json) = plan_hash. Stored in Go persistence-gateway frozen_plans table keyed by (base_url, config_hash, plan_hash). Frozen plan also written as a versioned JSON artifact to Go report-service artifact store — commitable to version control alongside the app. Subsequent CI runs: Go orchestrator reads plan_hash from config or artifact store → passes in RunConfig → Python plan node bypasses LLM entirely, loads next TestAction from frozen deque. LLM invoked only in verify and heal nodes.

SEEDED EXPLORATION:
In explore mode, exploration order is guided by: (1) sitemap BFS traversal from base_url (deterministic graph walk), (2) plan LLM calls at Temperature=0 with fixed system prompt version tag. Exploration seed = SHA-256(base_url + nav_structure_fingerprint) included in RunConfig and logged in run history. Same seed + same app structure → same exploration path. Nav structure fingerprint derived from first accessibility tree snapshot (stable across runs if app is unchanged).

GOLDEN STATE SNAPSHOTS:
After first stable explore run, the accessibility tree JSON captured at each checkpoint is written to persistence as golden_state {run_id, step_id, url_pattern, a11y_tree_json, ts}. In all subsequent replay runs, perceive node computes structural diff of current a11y_tree against golden_state for the matching step_id (tree edit distance on ARIA role+name nodes). Diff below threshold → continue. Diff above threshold → emit GOLDEN_STATE_DIVERGENCE metric and flag in run report; if divergence score > configurable limit → trigger plan staleness check immediately (do not wait for N healing failures).

PLAN STALENESS DETECTION:
In replay mode: if healing_attempts >= 2 on >= 3 different semantic_ids within one run → emit PLAN_STALE event. Go orchestrator schedules one fresh explore cycle (mode="explore") to regenerate frozen plan. New plan_hash replaces old in persistence. Old plan archived with a valid_until timestamp. Staleness threshold configurable per base_url (default: 3 elements, 2 attempts each).

FLAKE QUARANTINE:
A test identified by (action_type, semantic_id) that fails >= 3 consecutive runs and where healing resolves the locator each time (locator healed successfully but verify still returns FAIL or AMBIGUOUS) → classified FLAKY. FLAKY status stored in persistence. CI report separates FLAKY_COUNT from genuine FAIL_COUNT. Flaky tests do not block pipeline by default (configurable); logged to healing review queue for human inspection.

TOKEN BUDGET ENFORCEMENT:
Hard cap per model per run defined in RunConfig.token_budget {model_id: max_tokens}. AgentState.token_usage tracks consumed tokens. Budget guard fires in two places: (1) plan node — if remaining Opus budget < 3K tokens, abort new exploration, set stop_signal=true; (2) heal node — if remaining Sonnet budget < 2K tokens, skip heal attempts 2+3 (LLM calls), use strategy rotation only (attempt 1). This caps per-run LLM cost to a known ceiling. Budget consumed percentage reported in run report; exceeding 80% emits a Prometheus alert.

LLM TRANSCRIPT IMMUTABILITY:
Every LLM call appends to append-only JSONL transcript keyed by run_id: {entry_id, ts, node, model, prompt_hash, response_hash, prompt_tokens, completion_tokens, cost_usd, full_prompt, full_response}. Transcript stored by Go report-service, retained per policy (default 30 days). OTEL spans reference transcript_entry_id (not content). To replay any run without new API calls: load transcript + frozen plan → re-execute deterministically. This is also the cost audit record.

**Memory.**

SHORT-TERM — episodic, within session:

Storage: LangGraph AgentState TypedDict (in-process Python object) backed by LangGraph checkpointer writing to SQLite keyed by (run_id, thread_id). Checkpointing happens at every checkpoint node invocation (each loop iteration), giving resumability after crash or OOM kill. Go orchestrator stores the last checkpoint_ref returned by the brain on each STEP_COMPLETE event; on restart, passes checkpoint_ref to StartRun → LangGraph loads from SQLite.

Contents and bounded sizes: current_snapshot (freshest page state, single object); snapshot_history ring buffer (max 10 PageSnapshots; when full, oldest evicted after LLM summarisation into a 200-token episode summary appended to action_history — this prevents context window bloat on long exploration runs); action_history (last 20 full ActionRecord objects + episode summaries for older; episode summaries are 200 tokens each, so 50 old episodes = 10K tokens max headroom consumed); page_visit_log (unbounded but compact: url_pattern + visit_count + ts); token_usage and artifact_refs (small, unbounded within a run).

Lifecycle: created on StartRun gRPC call; checkpointed continuously; readable for resume via thread_id; archived (read-only) after report node completes.

LONG-TERM — learned, cross-session:

All long-term storage is owned by Go persistence-gateway. Python accesses via gRPC through the memory-client module. Python never writes to SQLite directly — prevents concurrent write contention.

(1) Healed Locator Store (SQLite table: healed_locators): columns {id, url_pattern, semantic_id, element_role, element_label, locator_strategy, locator_value, confidence, healed_at, run_id, review_required, quarantined}. Indexed on (url_pattern, semantic_id). memory-client fetches all locators for current url_pattern in a single GetLocators gRPC call on perceive entry; caches in-process with 10-minute TTL LRU. On quarantine: flips quarantined=true, subsequent runs skip healing for that semantic_id immediately.

(2) Sitemap / Knowledge Graph (SQLite adjacency table: sitemap_edges): {from_url_pattern, action_label, to_url_pattern, confidence, last_seen_run_id}. BFS-traversable from base_url. plan node fetches sitemap_fragment for current url_pattern to prioritise unexplored outgoing edges. checkpoint node writes new edges discovered in current cycle via UpdateSitemap gRPC. Persists across runs — the agent knows which pages it has and has not explored.

(3) Frozen Plan Store (SQLite table: frozen_plans): {plan_hash, base_url, config_hash, plan_json, created_at, valid_until, explore_run_id}. Go orchestrator queries on run start; passes plan_hash in RunConfig to Python brain. Versioned: old plans retained with valid_until for regression analysis.

(4) Page Object Cache (SQLite table: page_objects): {url_pattern, last_run_id, object_json containing TypeScript class skeleton}. Written by report node via Go persistence. Exported as .ts files on demand by report-service for hand-written test reuse.

(5) LLM Transcript Archive (append-only JSONL files per run_id): managed by Go report-service. Referenced from OTEL spans via transcript_entry_id. Retained per policy (default 30 days). Used for cost auditing, deterministic replay, and human review of healing decisions.

(6) LangGraph Checkpointer Backend: SQLite (single node, default); PostgreSQL (multi-runner K8s — each runner has its own thread_id, all share the checkpointer DB). Switching is one config change in LangGraph checkpointer constructor — no code change.

(7) Vector Store (Phase 2, optional): Local ChromaDB instance (verify current API and embedding model availability). Stores accessibility tree embeddings enabling semantic similarity search across pages: "find element semantically similar to the Submit button we saw on /checkout, but on /checkout/review". Used by heal node attempt 2 for cross-page element analogy. Deferred to Phase 2 because it adds operational complexity (another process, embedding model latency) without being critical to the core healing loop.

**Observability.**

DISTRIBUTED TRACING — OpenTelemetry SDK in all three languages, OTLP export to Jaeger (single-node) or Grafana Tempo (production K8s):

Go (otel-go): spans for run lifecycle FSM state transitions (with prev_state + next_state attributes), gRPC call durations to Python brain, persistence-gateway query durations (table + operation), report generation duration, HumanGateEvent emissions (with healing_id + semantic_id).

Python (otel-python): spans for each LangGraph node execution (node_name, duration_ms, snapshot_completeness_score); spans for each LLM call (model_id, prompt_hash — SHA-256 of prompt, NOT the content to avoid secrets in traces, completion_hash, prompt_tokens, completion_tokens, cost_usd_estimate, decision_type: PLAN|VERIFY|HEAL, confidence_output); spans for each MCP tool call (tool_name, duration_ms, success, error_type).

TypeScript (otel-node): spans for each MCP tool invocation (tool_name, duration_ms); Playwright operation timings (navigate: url + time_to_interactive; click: locator + success; snapshot: node_count + completeness_score); browser event timings via CDP (First Contentful Paint, Largest Contentful Paint where available).

LLM DECISION TRACING AND REPLAY:
Every LLM call: OTEL span references transcript_entry_id. Full prompt + full response written to append-only JSONL transcript (run_id keyed file on Go report-service storage). transcript_entry_id is the line index. Human can reconstruct exact LLM reasoning for any decision in any run without API access. To replay: load transcript → re-execute decisions without new API calls. Transcripts are immutable after write (append-only file descriptor).

TOKEN AND COST BUDGET:
AgentState.token_usage accumulates per model per run. Go report-service exposes Prometheus gauges: llm_tokens_total{model, call_type: plan|verify|heal} and llm_cost_usd_total{model, run_id}. Budget consumed percentage computed and reported; PrometheusAlertManager rule fires at 80% of per-run cap. Weekly cost trend dashboarded in Grafana (standard Prometheus/Grafana stack already in home lab).

PLAYWRIGHT TRACES:
TS trace-emitter wraps every run in a Playwright trace segment (start_trace on StartRun, stop_trace at each checkpoint boundary). Trace ZIPs posted to Go report-service via HTTP POST. Report-service serves Playwright trace viewer (static HTML bundled with Playwright) at /ui/traces/{run_id}. Full action video, network timeline, console logs, and screenshots captured in trace — zero additional instrumentation needed.

PROMETHEUS METRICS (served by Go report-service /metrics):
runs_total{status: success|fail|heal_fail|budget_exhausted, base_url_hash}
healing_attempts_total{attempt_type: strategy_rotation|llm_tree|visual_marks, outcome: success|fail}
healing_confidence_histogram{attempt_type} — bucket distribution reveals calibration quality and strategy effectiveness
llm_tokens_total{model, call_type}
llm_cost_usd_total{model} (counter, accumulated)
test_execution_latency_seconds histogram (per action_type)
flake_quarantine_count{base_url_hash} (gauge)
plan_staleness_detections_total (counter)
snapshot_completeness_histogram — distribution of accessibility tree completeness scores across all perceive calls; low median signals canvas-heavy app requiring visual grounding

ALERTING (Alertmanager rules):
healing_rate (healed / total_actions) > 0.20 within one run → DOM_INSTABILITY warning
budget_consumed_pct > 0.80 → BUDGET_WARNING (emitted also via gRPC to Go event-router)
quarantine_count > 5 in one run → QUARANTINE_THRESHOLD (blocks CI pipeline by default)
plan_staleness_detections_total rate > 2 per day → PLAN_STALENESS_ALERT (app is changing faster than explore cycle)

**Outputs.**

(1) Run Report (JSON + HTML): machine-readable JSON consumed by CI pipeline (exit code 0/1 based on configurable thresholds); HTML served at /ui/runs/{run_id} by Go report-service. Contents: test results per action (PASS/FAIL/SKIP/FLAKY/QUARANTINED), healing summary (healed_count, auto_healed vs review_required, confidence distribution histogram, healed locator diff — old vs new strategy per element), token and cost breakdown per model per call_type, Playwright trace viewer links, golden state divergence summary.

(2) Exported Playwright Test Code (TypeScript .ts files): stable locators from healed-locator store (confidence >= 0.85, not quarantined) + completed test sequences compiled into Playwright test runner format (describe/test/expect blocks). Emitted to configured output directory at end of each run. Directly usable by the existing qa-automation-engineer agent as handoff artifacts. Not emitted in replay mode unless explicitly requested via RunConfig.export_tests=true.

(3) Playwright Trace Archives (.zip): one per run segment (segmented at each checkpoint boundary). Viewable in Playwright Trace Viewer bundled with report-service. Retained per policy. Referenced in run report with deep links.

(4) Regression Baselines (accessibility tree JSON snapshots): captured at each checkpoint step on first stable explore run. Stored as golden_state set in persistence. Versioned by (base_url, explore_run_id). Used in subsequent runs for structural diff. Exportable as a golden_states.tar.gz artifact for version control alongside the app.

(5) Healing Audit Trail (JSONL per run): every HealedLocatorRecord with full context: old locator, new locator, strategy used, confidence, reasoning_transcript_ref, whether human review is required. Used for compliance review, post-incident debugging, and manual approval of review_required healings via the healing review queue at /ui/healing.

(6) Frozen Plan Artifact (JSON): the serialised TestAction sequence for replay. Versioned by plan_hash. Emitted to artifact store and optionally to a configured file path for version control. CI pipelines can pin to a specific plan_hash to guarantee identical test coverage across runs until the plan is intentionally regenerated.

**MVP path.**

Phase 1 (Weeks 1–2) — Spine and Wire: Go agent-ctl CLI + orchestrator stub (gRPC server, run lifecycle FSM with PENDING/RUNNING/DONE states, watchdog). TypeScript playwright-mcp-server with 5 core MCP tools: navigate_to, get_accessibility_tree, click_element, fill_input, take_screenshot. Python brain LangGraph skeleton with 3 nodes (perceive, act, checkpoint) and NO LLM — hardcoded TestAction sequence for a test application. Validate end-to-end: Go starts Python via gRPC StartRun; Python calls TS via MCP stdio; TS operates real Playwright browser; artifacts flow back to Go. Acceptance: gRPC contract stable, MCP tool calls round-trip under 50ms on localhost, Playwright browser opens and navigates.

Phase 2 (Weeks 3–4) — Full Perception Pipeline: Add remaining MCP tools: get_dom_snapshot, resolve_locator, start_trace, stop_trace, scroll_to, wait_for_navigation, get_network_log. Build snapshot-service (DOM snapshot with shadow DOM flattening, completeness scoring). Build set-of-marks-renderer (interactive element enumeration + canvas overlay). Python perception module (a11y tree parser, normaliser, completeness gate with < 0.30 fallback trigger). Validate: perceive node correctly classifies three test apps — a standard React SPA (high completeness), a canvas-heavy dashboard (low completeness triggers marks), and a custom-element app (medium completeness). Completeness threshold tuned empirically.

Phase 3 (Weeks 5–6) — Plan and Verify Loop with LLM: LLM integration (Sonnet 4.6 for cost during development; Opus 4.8 configurable). plan node with structured TestAction output schema. verify node with Verdict output schema. Full perceive→plan→act→verify→checkpoint→perceive loop running against a test application. Go persistence-gateway with SQLite (healed_locators, sitemap_edges, run_history tables). Python memory-client with gRPC stubs. Validate: agent autonomously navigates a 5-page CRUD test app, records sitemap, records test results, sitemap persists across two runs.

Phase 4 (Weeks 7–8) — Self-Healing: locator-grinder with all three re-grounding attempts: strategy rotation (attempt 1), LLM accessibility tree reasoning (attempt 2), set-of-marks visual grounding (attempt 3). Confidence scorer and persistence gate. HumanGateEvent → Go event-router → webhook POST. Validate: inject 10 known locator breakages into the test app (rename data-testid attributes, restructure DOM, change element text). Measure: attempt 1 resolves >= 6, attempt 2 resolves >= 3 of remaining, attempt 3 resolves >= 1 of remaining. Confidence calibration check: auto-accepted healings are correct >= 90% of the time.

Phase 5 (Weeks 9–10) — CI Determinism: Plan freezing (serialise after explore run, compute plan_hash, store in persistence). Replay mode (bypass plan node, load frozen TestAction deque). Golden state snapshots captured on first stable run. Structural diff on subsequent runs. Plan staleness detection (N=3 healing failures threshold). Flake quarantine (3 consecutive failures). Token budget enforcement in plan and heal nodes. Validate: same test app, same RunConfig → identical frozen plan → identical test outcomes across 5 consecutive CI runs (GitLab CI pipeline). Budget cap fires correctly on a configured 5K-token limit.

Phase 6 (Weeks 11–12) — Observability and Reporting: Full OpenTelemetry instrumentation in all three languages (OTLP → Jaeger). Go report-service HTML report generation and Playwright trace serving. Prometheus /metrics endpoint + Grafana dashboard (runs_total, healing_attempts_total, healing_confidence_histogram, llm_cost_usd_total, snapshot_completeness_histogram). LLM transcript JSONL + per-run cost tracking. Exported TypeScript test code generation (page-object stubs from stable locators). Validate: a full run is fully observable in Jaeger with LLM decision spans; exported .ts files import cleanly into a Playwright test runner project; run cost is visible in Grafana.

Phase 7 (Week 13+) — Production Hardening: Multi-runner support (PostgreSQL checkpointer + PostgreSQL persistence-gateway). K8s deployment manifests + ArgoCD Application CRD for home lab. Auth state injection (cookie/storage state from Vault secret reference in RunConfig). Vector store for cross-page element similarity (Phase 2 memory). Load testing against CI token budget caps. Alertmanager rules deployed. healing review queue UI at /ui/healing. Rate limiting on Go report-service artifact upload.

**Key risks:**

- MCP stdio transport latency: get_accessibility_tree is called every perceive cycle (potentially every 2-5 seconds on a fast-moving exploration run). stdio round-trips on localhost are typically 1-5ms, but Playwright snapshot serialisation can be 50-200ms for complex SPAs. If combined latency exceeds acceptable cycle time, mitigation: benchmark in Phase 1; if unacceptable, switch MCP transport to HTTP+SSE (adds socket overhead but decouples serialisation); alternatively batch perceive+act into compound MCP tool calls that return both the new snapshot and the action result in one round-trip.
- Accessibility tree completeness on modern SPAs: shadow DOM, slots, canvas-based components (charting libraries, rich text editors, design tools), and custom elements using non-standard interaction patterns may produce completeness_score near 0, making every perceive cycle fall into set-of-marks mode. Set-of-marks increases LLM token cost 3-5x per cycle (image tokens vs text tokens). Mitigation: completeness threshold is configurable per base_url; for known canvas-heavy apps set marks-first mode explicitly in RunConfig; monitor snapshot_completeness_histogram metric in Grafana to detect degradation early.
- Frozen plan brittleness on rapidly-changing UIs: if the app under test deploys weekly with structural DOM changes, golden state divergence will fire frequently and trigger plan regeneration. Multiple explore runs per week multiply LLM cost. Mitigation: configurable divergence threshold per url_pattern (some pages are stable, others are volatile); partial plan invalidation (re-explore only the url_patterns that showed divergence, not the entire sitemap); expose plan_hash pinning in RunConfig for teams that want to pin to a specific release.
- LLM confidence miscalibration in the heal node: Sonnet 4.6 may report 0.88 confidence on a healing proposal that is actually wrong. The 0.90 empirical discount is a placeholder. If miscalibrated, auto-accepted healings (confidence >= 0.85) will pollute the healed-locator store with bad locators, causing silent test failures. Mitigation: in Phase 4, measure actual healing accuracy vs reported confidence on a labelled breakage dataset (inject 50 known breaks, measure calibration). Adjust discount factor or lower the auto-accept threshold to >= 0.90 based on empirical results. Long-term: add a post-healing verification step (run the action once more with the healed locator before persisting) as a high-confidence confirmation gate.
- Human gate bottleneck in CI pipelines: if a large percentage of healing attempts produce confidence < 0.60, CI runs accumulate many SKIPPED_HEALING_FAILURE results. A team receiving a CI run with 30% SKIPPED will lose trust in the agent. Mitigation: non-blocking skip mode is the default in CI (human gate is informational only, not a pipeline blocker); quarantine_count threshold (default 5) is the only hard gate; healing review queue at /ui/healing lets humans triage asynchronously without blocking runs. Alert the team when skip_rate > 10% — this signals the app has changed fundamentally and needs a new explore run.
- LangGraph MCP client integration maturity: LangGraph's first-class MCP tool support is relatively recent (verify current langchain-mcp package status and stdio transport stability as of implementation date). Rough edges may exist around error propagation, tool schema validation strictness, or subprocess management. Mitigation: isolate MCP client in the memory-client + brain modules; write a thin adapter that wraps the MCP client so it can be swapped for a custom JSON-RPC 2.0 client (< 300 lines) if the official package proves unstable. The JSON-RPC 2.0 protocol is simple enough that a fallback implementation is low-risk.
- SQLite write contention in multi-runner K8s: when runner_count > 5, all Python brains calling Go persistence-gateway concurrently creates write queue pressure. Go serialises all writes (single goroutine with channel queue), but at high concurrency this becomes a throughput bottleneck. Mitigation: profile at runner_count=5 in Phase 7; if queue depth grows > 100ms p99 latency → migrate persistence-gateway backend to PostgreSQL (connection pool, concurrent writes); migration is one config change in the gateway, no schema change required.

**ADRs:**

- ADR-001: MCP over stdio as the Python-to-TypeScript protocol. Accepted. Rejected alternatives: (a) gRPC service wrapping Playwright — requires writing a .proto schema that mirrors Playwright's API surface, a translation layer with no architectural benefit; (b) JSON-RPC over HTTP — adds a network socket for a same-pod call, more complexity than stdio with no benefit at this scale; (c) Python Playwright library directly in the brain process — eliminates TypeScript as the browser layer, violates the polyglot constraint and removes Playwright's native Node.js trace integration. Rationale: MCP is the native protocol for LLM tool-calling; LangGraph binds MCP tools without an adapter layer; TS layer IS semantically a tool server; the protocol mismatch between agent reasoning and browser execution disappears.
- ADR-002: LangGraph explicit state machine for the agent backbone. Accepted. Rejected alternatives: (a) bespoke asyncio event loop — no built-in checkpointing, no graph visualisation, individual nodes are harder to unit test in isolation, resumability requires custom implementation; (b) CrewAI or AutoGen multi-agent frameworks — less control over the perception→heal cycle, opaque internal state makes debugging non-deterministic healing behaviour difficult, checkpointing is not first-class. Rationale: LangGraph's checkpointer gives resumability for free; the node/edge graph is inspectable and visualisable; conditional edge routing maps directly onto the heal→ground vs heal→checkpoint branching logic.
- ADR-003: Accessibility tree as primary perception modality. Accepted. Rejected alternatives: (a) DOM-only (HTML snapshot to LLM) — no semantic structure, token-expensive, breaks on shadow DOM, LLM must infer intent from raw HTML tags; (b) screenshot-only (pixel vision) — high token cost, no structural grounding for locator resolution, LLM cannot reliably produce stable locators from pixel coordinates; (c) CDP (Chrome DevTools Protocol) direct — powerful but raw, requires significant parsing work and is browser-vendor-specific. Rationale: ARIA roles and accessible names are designed to outlast DOM mutations; they reflect semantic intent rather than implementation; Playwright's accessibility snapshot API returns a structured JSON tree that fits in LLM context without preprocessing.
- ADR-004: Set-of-marks as the visual grounding fallback in heal attempt 3. Accepted. Rejected alternatives: (a) coordinate-based clicking (screenshot → LLM reports pixel coordinates) — fragile to viewport changes, device pixel ratio, and partial-page rendering; (b) DOM structural XPath fallback — reverts to brittle structural selectors, same problem as the original failed locator; (c) explicit element screenshot crop (crop the bounding box of the expected element from the screenshot) — requires knowing the bounding box, which we do not have when the element is missing. Rationale: numbered overlays on interactive elements give the LLM spatial context (which region of the page) plus structural context (element index maps back to accessibility tree entry) without requiring coordinate precision.
- ADR-005: Go persistence-gateway as the single writer to all SQLite storage. Accepted. Rejected alternatives: (a) each Python runner writes SQLite directly — WAL mode helps but at > 5 concurrent writers, write queue latency degrades and lock contention errors appear; (b) PostgreSQL from day one — over-engineering for single-node MVP; adds an operational dependency before the system is proven; (c) Redis for locator store — wrong data model (Redis is key-value, not relational), loses the ability to query by url_pattern + semantic_id efficiently, adds another server. Rationale: single Go process serialises all writes via an internal channel queue; swap to PostgreSQL is a backend change in one Go file with no schema migration required.
- ADR-006: Explore-once-then-replay for CI determinism. Accepted. Rejected alternatives: (a) regenerate the plan on every CI run from LLM — expensive (every run pays full Opus 4.8 explore cost), non-deterministic (Temperature=0 helps but prompt context varies), and slow; (b) network-level record-and-replay (HAR files) — brittle to any backend URL or response change, not testing the UI semantics; (c) property-based test generation (derive tests from API spec) — loses the exploratory coverage that is the agent's differentiating capability; (d) screenshot comparison only (pixel diff) — high false-positive rate on dynamic content (timestamps, avatars, ads). Rationale: frozen plan gives determinism; healing adds resilience to UI drift without re-exploration; structural diff against golden accessibility tree snapshots detects intentional app changes early.
- ADR-007: Opus 4.8 for explore/plan, Sonnet 4.6 for verify/heal. Accepted. Rejected alternatives: (a) Sonnet 4.6 for all LLM calls — plan quality is measurably lower on complex multi-step exploration decisions (validate in Phase 3 with A/B comparison on the test app sitemap coverage); (b) Opus 4.8 for all LLM calls — cost is 5-8x higher per run with marginal quality improvement on verify (binary PASS/FAIL with clear evidence) and heal attempt 2 (structured tree reasoning, well within Sonnet capability); (c) local model (Llama-class, via Ollama) — home lab GPU budget insufficient for reliable Opus-quality reasoning; accessibility tree reasoning requires strong instruction-following and structured output compliance. Rationale: explore decisions have the highest return on model quality; verify and heal are narrower, better-constrained tasks where Sonnet quality is acceptable at significantly lower cost. Revisit split if Sonnet 4.x releases close the gap on complex planning.

---

### `reliability-ci` — TrustFirst — Reliability/CI-Determinism Autonomous UI Agent

*Lens: Reliability / CI-determinism first*

**Philosophy.** The core bet: a non-deterministic LLM explorer becomes a trustworthy CI citizen through hard separation of explore-mode (LLM-driven, human-supervised, one-time) and replay-mode (plan-frozen, LLM-free on the happy path, deterministic). The frozen plan is the artifact CI verifies — not the LLM's live reasoning. Trust is built from four pillars: an immutable healing audit trail that gives every locator repair a confidence score and a human gate below threshold; hard token/cost budgets enforced at the Go control-plane before the Python brain can spend them; golden a11y-tree snapshots that make DOM regressions a structural diff rather than a vibe; and a flake-quarantine registry that prevents a single unstable DOM element from poisoning the CI signal for the entire suite.

**Components (13):**

| Component | Language | Responsibility |
|-----------|----------|----------------|
| agentctl | Go | CLI entrypoint for all human and CI interactions: run (explore\|replay\|ci modes), gate (approve/skip/abort human gates), report (render artifacts), baseline (update golden snapshots), healing (audit/calibrate), plan (list/inspect/hash-verify). Emits structured exit codes for CI (0=pass, 1=step-fail, 2=regression, 3=budget-exhausted). |
| orchestrator | Go | gRPC server (bidirectional streaming). Owns run lifecycle FSM (CREATED→RUNNING→PAUSED→COMPLETED\|FAILED\|ABORTED). Exposes RunControl, BudgetService, EventStream, GateService RPCs. Budget enforcement is a hard ceiling: rejects Python deduct calls at ceiling regardless of what Python's local counter says. Emits Prometheus metrics on /metrics. |
| store-gateway | Go | Single writer to SQLite WAL database. All cross-session state flows through here: plan_store, locator_store, sitemap, golden_snapshots, healing_audit, flake_registry, run_index. Exposes StoreService gRPC (upsert/get/query). Being the sole writer is the WAL contention contract — no Python or TS process touches SQLite directly. |
| report-api | Go | HTTP API (port 8080) serving run artifacts: JSON + HTML reports, healing audit JSONL download, Playwright trace proxy, LLM transcript download, sitemap graph JSON. Reads from SQLite via store-gateway. Also serves the real-time EventStream as SSE for CI log tailing. |
| brain | Python | LangGraph state machine. 8 nodes: perceive, ground, plan, act, observe, verify, heal, checkpoint. Connects to Go orchestrator via gRPC (RunControl, BudgetService, StoreService clients). Spawns playwright-executor subprocess and drives it via MCP stdio. Owns all LLM calls (Claude Opus 4.8 for plan node; Sonnet 4.6 for heal node). Manages LangGraph checkpointer (SqliteSaver local, AsyncPostgresSaver prod) for pause/resume. |
| healing-engine | Python | Locator re-grounding logic called exclusively from the heal LangGraph node. Implements 3-attempt hierarchy: (1) strategy rotation over locator_store alternatives, (2) LLM reasoning over a11y tree text, (3) LLM visual re-grounding over set-of-marks screenshot. Computes confidence score, applies gate threshold, emits HealAttempt proto to store-gateway for audit persistence. |
| plan-manager | Python | Freeze/unfreeze plan artifacts. On freeze: serializes ordered action sequence with pre/post a11y snapshot hashes and locator alternatives, computes plan_hash (sha256 of canonical JSON), pushes to store-gateway StoreService.UpsertPlan. On replay load: fetches plan by plan_id, verifies plan_hash against stored value before first step — aborts if tampered. |
| llm-client | Python | Thin Claude API wrapper with budget-aware gating. Before every call: gRPC BudgetService.CheckAndDeduct(tokens_estimate, cost_estimate) — if rejected, raises BudgetExhaustedError which routes brain to checkpoint then report. After call: records actual tokens/cost via BudgetService.RecordActual. Logs every prompt+response to llm_transcript.jsonl (append-only, one file per run_id). |
| playwright-executor | TypeScript | MCP server running as a child subprocess of brain. Exposes 12 tools over stdio MCP: navigate, click, fill, hover, select, press_key, scroll, wait_for_selector, snapshot_a11y (returns full ARIA tree as JSON), screenshot_annotated (injects set-of-marks JS overlay, returns base64 PNG with integer mark numbers), evaluate_js, and close_context. Manages Playwright browser context lifecycle and trace recording per run_id. |
| snapshot-service | TypeScript | Internal module of playwright-executor. snapshot_a11y calls page.accessibility.snapshot({interestingOnly: false}) and serializes to a normalized JSON structure (role, name, description, children). screenshot_annotated injects an overlay script that assigns sequential integer marks (bounding-box + number label) to all interactive elements, captures screenshot, strips overlay, returns annotated image. Also computes dom_hash: sha256 of document.body.innerHTML with data-reactid/data-v-* dynamic attrs stripped. |
| trace-manager | TypeScript | Playwright trace lifecycle: starts tracing (screenshots: true, snapshots: true, sources: true) at run start, stops and exports trace.zip at run end to /runs/{run_id}/playwright-trace.zip. Pushes trace file path to Go report-api via HTTP POST /artifacts/{run_id}/trace. |
| proto | shared | protobuf3 definitions for the Go↔Python gRPC boundary. Services: RunControl (Start, Stop, Pause, Resume, GetStatus), BudgetService (CheckAndDeduct, RecordActual, GetBudget), StoreService (UpsertPlan, GetPlan, UpsertLocator, GetLocators, UpsertSitemap, AppendHealingAudit, GetFlakeRegistry, UpdateFlakeCount), GateService (ListPending, Resolve), EventStream (Subscribe — server-streaming of RunEvent). Compiled to Go and Python (buf.build or protoc). |
| mcp-schema | shared | JSON Schema definitions for the 12 MCP tools and 2 MCP resources exposed by playwright-executor. Tools schema defines input/output shapes validated at the MCP stdio boundary. Resources: current_page (live a11y tree) and trace_status (recording state). Version-pinned so Python brain and TS executor stay in sync on tool signatures across Playwright upgrades. |

**Boundaries (polyglot contracts).**

Go ↔ Python — gRPC proto3 bidirectional streaming. The Go orchestrator is the gRPC server; Python brain is the client. Protocol choice rationale: Go's grpc library is production-grade; proto3 enforces a typed schema across the language boundary so drift is caught at compile time (protoc generated stubs on both sides); bidirectional streaming enables three things that REST cannot do cleanly — (1) Go pushes budget-exhaustion events to Python mid-run without polling, (2) Python streams live ActionRecord events to Go for real-time report-api SSE, (3) human gate resolution flows from Go to Python as a server push (GateService.Subscribe). Transport: localhost Unix domain socket in single-node mode (lower syscall overhead than TCP loopback, no port binding flakiness in CI containers); configurable to TCP for multi-node. Rejected alternative: REST/HTTP — no streaming without SSE complexity, payload type safety requires separate JSON schema validation, no clean cancel propagation.

Python ↔ TypeScript — MCP over stdio (JSON-RPC 2.0 framing). Python brain spawns playwright-executor as a child subprocess via Python's subprocess.Popen with stdin/stdout pipes. MCP protocol is already JSON-RPC-like with tools/resources/prompts semantics that map directly onto LangGraph's tool-calling node pattern — the brain calls MCP tools exactly as it calls any LangGraph tool, so no adapter layer is needed. Stdio avoids port allocation (critical in CI where port conflicts cause flakes), and subprocess lifecycle is owned by the Python process (SIGTERM propagates on brain crash). Heartbeat: brain sends MCP ping every 30 seconds; if no pong within 5 seconds, it triggers a single subprocess restart before marking the step failed. Rejected alternative: gRPC — TypeScript grpc-node adds generated-code build complexity and the TypeScript ecosystem for gRPC is materially less mature than Python's; the resulting API would be identical in shape to what MCP provides natively.

TypeScript → Go — HTTP REST (fire-and-forget artifact push). At run completion, trace-manager POSTs the trace.zip file path to Go report-api POST /artifacts/{run_id}/trace. This is the only TS→Go communication and it is post-run, so latency is irrelevant. Playwright trace files are written to a shared volume mount (/runs/{run_id}/); Go reads them directly by path. No real-time protocol needed here. Rejected alternative: gRPC — adding a third gRPC client (TS) for a single artifact-path notification is disproportionate complexity.

Failure isolation contract: if playwright-executor crashes, brain catches MCP disconnection (subprocess poll returns non-None), attempts one restart, then fails the current step and invokes the heal node (which will re-attempt the action after reconnect). If brain crashes, Go orchestrator detects gRPC stream termination, saves run state as FAILED, preserves the LangGraph checkpoint on disk so the run can be resumed with agentctl run --resume {run_id}. If Go orchestrator restarts, Python brain's gRPC client reconnects with exponential backoff (initial 100ms, max 30s, jitter). No state is lost because store-gateway is the authoritative writer and SQLite is durable.

**Agent loop.**

SHARED STATE OBJECT (AgentState TypedDict):
  session_id: str
  run_id: str
  run_mode: Literal["explore", "replay", "ci"]
  # Page context
  target_url: str
  current_url: str
  current_a11y_tree: dict            # normalized ARIA JSON from snapshot-service
  current_dom_hash: str              # sha256 of stripped innerHTML
  current_screenshot_b64: str        # annotated with set-of-marks integers
  a11y_completeness_ratio: float     # ratio of interactive elements with ARIA roles; < 0.30 triggers visual-grounding mode
  # Plan
  frozen_plan: Plan | None           # None during explore; populated after freeze node
  plan_step_index: int
  action_history: list[ActionRecord] # each: step_id, action, locator_used, outcome, pre_dom_hash, post_dom_hash, tokens_used
  # Exploration queue
  discovered_pages: dict[str, PageNode]
  pending_pages: deque[str]
  # Healing
  last_failed_selector: str | None
  last_failed_description: str | None
  healing_attempts: list[HealAttempt]
  current_healing_confidence: float
  # Budget
  tokens_used: int
  tokens_budget: int
  cost_usd: float
  cost_budget_usd: float
  budget_warning_emitted: bool
  # Gate
  human_gate_pending: bool
  human_gate_reason: str | None
  human_gate_decision: Literal["approve","skip","abort"] | None
  human_gate_resolved_locator: str | None
  # Artifacts
  run_dir: str                       # /runs/{run_id}/
  trace_active: bool
  artifacts: list[ArtifactRef]

NODE GRAPH (8 nodes):

perceive — Entry point for every cycle. Calls MCP snapshot_a11y + screenshot_annotated. Computes dom_hash. Updates current_a11y_tree, current_screenshot_b64, a11y_completeness_ratio, current_url. In replay mode: also loads frozen_plan[plan_step_index] to prime the next action. Always transitions to → ground.

ground — Re-grounding guard. Checks: is current_url in discovered_pages? Is dom_hash changed from last visit? If new page: extract interactive elements, add to sitemap, push child links to pending_pages. If dom_hash unchanged from last step's post_dom_hash: flag as possible no-op action (emit WARNING to EventStream). Transitions to → plan.

plan — Decision node. In EXPLORE mode: constructs LLM prompt from current a11y tree + action_history (last 10) + budget_remaining + sitemap coverage + pending_pages. Model: Claude Opus 4.8. Output: next_action (ActionSpec: type, target_description, locator_hint, assertion) or signal "exploration_complete". Budget pre-check via BudgetService.CheckAndDeduct before call; routes to checkpoint if budget exhausted. In REPLAY/CI mode: reads frozen_plan.steps[plan_step_index] directly — NO LLM call. Routes to → act (if action available) or → checkpoint (if exploration_complete or plan exhausted).

act — Executes the chosen action via MCP tool call. Maps ActionSpec.type to one of: click, fill, hover, press_key, scroll, navigate, select. Records pre_action_dom_hash from current state. If MCP tool returns error (locator_not_found, timeout, element_not_interactable): sets last_failed_selector + last_failed_description, routes to → heal. On success: routes to → observe.

observe — Calls MCP snapshot_a11y + screenshot_annotated again (post-action state). Updates current_a11y_tree, current_dom_hash, current_screenshot_b64. Detects navigation (current_url changed). Appends ActionRecord to action_history with actual token counts. Routes to → verify.

verify — Asserts expected post-action state. In EXPLORE mode: LLM soft-assertion ("did the action produce a meaningful state change?") — Sonnet 4.6 call, structured output (passed: bool, notes: str). In REPLAY/CI mode: structural diff of current a11y tree against golden_snapshot[plan_id][step_id]. Diff algorithm: recursive JSON tree diff counting added/removed/changed nodes. If diff_ratio < 0.05: pass → advance plan_step_index → routes to → perceive. If diff_ratio 0.05–0.25: pass with REGRESSION_WARN (flag in report, continue). If diff_ratio > 0.25 or required_element absent: fail → routes to → heal (treat as locator failure for the assertion target).

heal — Self-healing node. Delegates to healing-engine module (see selfHealing field for full algorithm). Sets current_healing_confidence from engine result. If confidence >= 0.85: auto-persist healed locator to store-gateway StoreService.UpsertLocator, update action in state, route back to → act (retry with healed locator, max 2 retries). If 0.60–0.84: attempt action with healed locator but set healing_flagged=True in ActionRecord for human post-run review; route to → act. If < 0.60: set human_gate_pending=True, human_gate_reason="low_confidence_heal", call orchestrator GateService.CreateGate, route to → checkpoint (async pause).

checkpoint — LangGraph checkpoint save. Calls checkpointer.aput(config, state, metadata) — persists full AgentState. Also calls orchestrator RunControl.Checkpoint() to record checkpoint_id in SQLite (for resume). If human_gate_pending: calls RunControl.Pause(gate_id) and enters a polling loop (GateService.WaitForResolution with 30s poll interval). On resolution: sets human_gate_decision + human_gate_resolved_locator, routes back to → heal (on approve) or → plan (on skip) or raises AbortError (on abort). If no gate pending and run_mode==explore and pending_pages empty and plan not frozen: routes to → plan (freeze branch via exploration_complete signal). Otherwise: routes to → perceive (next page/step).

report — Terminal node. Stops Playwright trace via MCP close_context (trace-manager exports trace.zip). Serializes frozen_plan JSON if not yet persisted. Builds RunReport proto with step-level pass/fail/healed stats, cost breakdown, flaky step count, human_gate pending count. Pushes to store-gateway for SQLite + calls report-api POST /runs/{run_id}/finalize. Routes to END.

EDGES (conditional routing via LangGraph add_conditional_edges):
perceive → ground (always)
ground → plan (always)
plan → act | checkpoint (budget_exhausted) | report (exploration_complete or replay_done)
act → observe (success) | heal (mcp_error)
observe → verify (always)
verify → perceive (pass, plan_step_index++) | heal (fail)
heal → act (confidence>=0.60, retries<2) | checkpoint (confidence<0.60 OR retries>=2)
checkpoint → perceive (normal resume) | heal (gate approved) | plan (gate skipped) | END (gate aborted)
report → END

**Self-healing.**

Step 1 — Failure Capture: When act node receives an MCP error (locator_not_found, timeout, element_not_interactable, or detached frame), it records in AgentState: last_failed_selector (the exact selector string that failed), last_failed_description (semantic description from plan step, e.g., "primary submit button in the checkout form"), failed_action_type (click/fill/etc.), and pre_failure_dom_hash. Routes to heal. Healing engine receives this snapshot as its input contract.

Step 2 — Strategy Hierarchy (no LLM, fast path first): healing-engine attempts locator alternatives in strict ranked order before invoking any LLM. The locator_store in SQLite is pre-loaded into AgentState at session start as a dict keyed by (page_url, element_description). Strategy L1: query locator_store for (current_url, last_failed_description) — if found and status=active, try that selector via a MCP wait_for_selector probe (100ms timeout). L2: extract ARIA role + accessible name from current_a11y_tree matching the semantic description (string similarity >= 0.85) — construct Playwright role-selector (e.g., role=button[name="Submit Order"]). L3: search a11y tree for nodes whose accessible name contains key tokens from last_failed_description. L4: text-content selector on visible text nodes. Each L1-L4 attempt fires a MCP wait_for_selector probe to validate before committing. First probe success → go to confidence scoring (Step 4). All four fail → proceed to LLM re-grounding (Step 3).

Step 3 — LLM Re-grounding (two-pass): Pass A — a11y tree reasoning (Sonnet 4.6, text-only, cheaper): Prompt structure: (1) "You were attempting to {action_type} the element described as: {last_failed_description}. Here is the current ARIA accessibility tree: {serialized_a11y_tree}. Identify the element that best matches. Return: aria_path (dot-notation path through tree), playwright_selector (role or aria-* selector), confidence (0.0–1.0), reasoning (max 2 sentences)." Output parsed as structured JSON via tool_use. If a11y_completeness_ratio < 0.30 (canvas-heavy page, custom web components, iframe isolation): skip to Pass B. Pass B — visual re-grounding (Sonnet 4.6 with vision): Use current_screenshot_b64 (set-of-marks annotated). Prompt: "The circled/numbered marks in this screenshot indicate interactive elements. You were trying to {action_type} the element described as: {last_failed_description}. Which mark number corresponds to the target? Return: mark_number (int), css_selector_suggestion (string), confidence (0.0–1.0), reasoning." healing-engine maps mark_number to DOM element via a JS evaluate call (the overlay script stored mark→element mappings in window.__somarks). Extracts the element's data-testid, aria-label, or fallback CSS path. Takes max(confidence_A, confidence_B) as final confidence score.

Step 4 — Confidence Gate: confidence >= 0.85: auto-heal. Commit new selector to locator_store (UpsertLocator gRPC call with status=active, source=auto_heal). Update AgentState action with healed selector. Increment heal_count. Route to act (retry). Confidence 0.60–0.84: attempt-with-flag. Use healed selector, set healing_flagged=True in ActionRecord (shows as yellow in HTML report). Do not persist to locator_store until human reviews post-run. Route to act (retry). Confidence < 0.60 OR retry count >= 2: refuse to proceed. Set human_gate_pending=True, human_gate_reason="healing_failed: confidence={score}, selector={best_candidate}". This invokes the human gate protocol.

Step 5 — Human Gate Protocol (async, CI-safe): healing-engine emits HealingGateEvent to orchestrator GateService.CreateGate(run_id, step_id, context_blob). Orchestrator writes gate to SQLite gate_queue table and broadcasts via EventStream. agentctl gate list shows: run_id, step_id, failed_selector, best_candidate, confidence, screenshot_url (base64 preview link). Human executes one of: agentctl gate approve {run_id} --locator "button[data-testid=submit]" (provides correct selector directly), agentctl gate skip {run_id} (step skipped, marked needs_human_review in report), agentctl gate abort {run_id} (run terminated, partial report saved). CI timeout (configurable, default 30 min): GateService auto-resolves as skip. Approved locators are persisted to locator_store with status=human_verified — highest priority in next L1 lookup.

Step 6 — Flake Quarantine (CI defense): step_id tracked in flake_registry table (plan_id, step_id, fail_count, last_5_results, quarantine_status). After every CI replay run, UpdateFlakeCount called for each failed step. If fail_count >= 3 in last 5 runs WITHOUT AUT commit SHA change: step marked quarantine_status=quarantined. Quarantined steps: still executed, but their failure does NOT affect CI exit code. Appear in report as a distinct "Flaky (quarantined)" section. Human reviews weekly via agentctl healing report --flaky. Quarantine lifted by: agentctl gate clear-flake {plan_id} {step_id} (human judgment) or auto-clear after 3 consecutive passes.

Step 7 — Persistence Schema: locator_store table: {id UUID, plan_id, page_url, original_selector TEXT, healed_selector TEXT, element_description TEXT, aria_path TEXT, confidence REAL, heal_count INT, last_healed_at TIMESTAMP, status TEXT CHECK(status IN ('active','flagged','human_verified','deprecated')), human_verified BOOL}. healing_audit table (append-only, no UPDATE/DELETE): {id UUID, timestamp, run_id, step_id, original_selector, strategy_used (L1|L2|L3|L4|llm_a11y|llm_visual), healed_selector, confidence, outcome (success|flagged|human_gate|flake_quarantined), llm_tokens_used INT, duration_ms INT}. Both tables owned exclusively by store-gateway (single writer contract).

**Determinism.**

Core principle: LLM non-determinism is acceptable exactly once, in explore mode, under human supervision. CI never invokes explore mode. Here is the full mechanism:

EXPLORE MODE (non-deterministic, one-time per feature area): Runs against a dedicated staging environment with a known AUT commit SHA recorded at run start. Full LLM reasoning on every plan node decision. Produces a frozen_plan artifact: {plan_id: UUID, plan_hash: sha256(canonical_json), created_at: ISO8601, target_url: str, aut_version: str (git SHA of AUT), exploration_seed: int, steps: [ordered list of PlanStep]}. Each PlanStep: {step_id: str, action: ActionSpec, element_description: str, locator_primary: str, locator_alternatives: [L1..L4 ranked], pre_action_a11y_hash: str, post_action_a11y_hash: str, assertion: AssertionSpec}. Golden a11y snapshots (full JSON trees) stored in SQLite golden_snapshots table keyed by (plan_id, step_id). plan_hash is the sha256 of the canonical JSON of the steps array (keys sorted, floats normalized to 6 decimal places). Stored in plan_store table and also in a frozen_plan.json file under /runs/{run_id}/.

PLAN HASH INTEGRITY: At replay start, plan-manager calls StoreService.GetPlan(plan_id) → compares stored plan_hash against freshly computed hash of the retrieved steps. Hash mismatch → immediate abort with exit code 3 and error "plan integrity violation: stored={stored_hash}, computed={computed_hash}". Plans are immutable after freeze. A new exploration run produces a new plan_id. There is no mechanism to edit a frozen plan in place — the only upgrade path is a new explore run.

REPLAY MODE (deterministic, CI entrypoint): agentctl run --mode ci --plan-id {id} --aut-version $(git rev-parse HEAD). Plan node reads frozen_plan.steps[plan_step_index] — NO LLM call. act executes the primary locator directly. verify diffs current a11y tree against golden snapshot (structural JSON diff). LLM is invoked ONLY on two conditions: (a) act fails with locator_not_found (healing path, Step 3 of selfHealing), or (b) verify detects diff_ratio > 0.25 (regression path, healing invoked on the diverged element). In the happy path (all locators valid, DOM matches golden): zero LLM calls, zero API cost, deterministic execution time.

AUT VERSION DRIFT DETECTION: plan_store.aut_version compared against --aut-version flag at replay start. If SHA differs: emit "AUT version mismatch: plan built on {stored}, current {current}" as WARNING (not abort). Enables a "tolerant CI" mode where the agent runs with healing enabled but flags all healed steps for human review. Configurable policy: --on-aut-mismatch=[warn|heal|abort].

EXPLORATION SEED: AgentState.exploration_seed (integer, default 0, configurable via --seed flag) controls: page visit order (BFS traversal uses seed to deterministically shuffle equal-priority pending_pages), and action selection tiebreaking in the plan node (when LLM proposes multiple equally-valid next actions, seed determines pick). Same seed on same codebase produces structurally similar (though not bit-identical) explore runs, which is sufficient for debugging explore regressions without removing LLM flexibility.

GOLDEN SNAPSHOT UPDATE PROTOCOL: Baselines are never auto-updated during CI runs. Update requires explicit: agentctl baseline update --plan-id {id} --aut-version {sha}. This runs a replay, accepts all current a11y trees as the new golden, and produces a new plan_hash. The old plan remains in plan_store as a historical record. This makes "the CI tests changed their own baselines" impossible.

FLAKE QUARANTINE (CI signal protection): After each CI run, every step's outcome (pass/fail/healed) is written to flake_registry via StoreService.UpdateFlakeCount. The flake determination uses a sliding window of the last 5 CI runs for the same plan_id. Steps with fail_count >= 3 in the window get quarantine_status=quarantined. Quarantined steps: executed normally, failures do NOT set exit code 1 or 2, appear in report section "Quarantined (flaky)". CI exit codes: 0=all non-quarantined steps passed, 1=step failures without golden diff regression, 2=golden diff regression detected (diff_ratio > 0.25 on a non-quarantined step), 3=plan integrity violation or budget exhausted.

CI PIPELINE INTEGRATION: The replay run is fully self-contained: single binary (agentctl) calls Go orchestrator (in-process in single-binary mode for CI simplicity), which spawns Python brain subprocess (brain --run-id {id} --mode ci), which spawns playwright-executor subprocess. No external services required beyond SQLite file and AUT URL. Optional: push run report to report-api for centralized history.

**Memory.**

SHORT-TERM (episodic, within session, LangGraph-owned):
LangGraph built-in checkpointer manages AgentState across all node transitions within a run. Backend: SqliteSaver for local/dev (file at /runs/{run_id}/langgraph.db, separate from main store-gateway SQLite to avoid write contention), AsyncPostgresSaver for production K3s deployment. Thread ID = run_id, enabling concurrent runs with independent state. Checkpoint saved at every node boundary — this is what makes pause/resume and crash recovery work. On brain crash: Go orchestrator sees gRPC stream terminated, marks run FAILED in run_index. Human executes agentctl run --resume {run_id}: brain re-attaches to checkpoint, replays from the last saved node boundary. Episodic buffer: AgentState.action_history capped at 30 most recent ActionRecords. Older records are summarized (LLM-generated, stored in AgentState.history_summary) to keep token context bounded. The summary is regenerated every 30 actions (configurable). In-session locator validity cache: dict[selector, ProbeResult] in AgentState, populated during L1-L4 strategy rotation to avoid re-probing known-broken selectors within the same session.

LONG-TERM (cross-session, Go store-gateway owned, SQLite WAL):
plan_store: {plan_id, plan_hash, target_url, aut_version, exploration_seed, created_at, step_count, status(active|archived), file_path}. Frozen plan JSON stored as file, table holds metadata + integrity hash.
locator_store: {id, plan_id, page_url, original_selector, healed_selector, element_description, aria_path, confidence, heal_count, last_healed_at, status, human_verified}. Pre-loaded into AgentState.locator_cache at session start (filtered by target domain).
sitemap: {url, page_type, first_seen, last_seen, visit_count, interactive_element_count, a11y_completeness_ratio, in_pending_queue}. Loaded into AgentState.discovered_pages at session start. Updated by ground node.
golden_snapshots: {plan_id, step_id, snapshot_type(a11y|screenshot), content_hash, file_path, created_at, superseded_by}. Content stored as files under /runs/baselines/{plan_id}/.
healing_audit: append-only audit log (no UPDATE, no DELETE). Schema in selfHealing step 7.
flake_registry: {plan_id, step_id, fail_count, last_5_results JSON, quarantine_status, last_updated}.
run_index: {run_id, plan_id, mode, aut_version, status, started_at, completed_at, tokens_used, cost_usd, steps_pass, steps_fail, steps_healed, steps_quarantined}.
page_object_cache: {url_pattern (regex), generated_ts_code, code_hash, created_at, used_count}. Generated TypeScript page object classes cached for reuse by qa-automation-engineer.

Storage rationale: SQLite WAL mode for all long-term state. WAL allows concurrent readers (report-api, agentctl) while store-gateway is the sole writer — this is the entire contention contract. Zero operational overhead for home lab and single-node CI. Easy backup: single cp command. Sufficient write rate: < 200 rows/second at peak (one step per few seconds). Scale trigger: if concurrent runs > 10 or write rate > 1000 rows/sec → migrate store-gateway to PostgreSQL (schema is identical, driver swap in Go). Rejected PostgreSQL for v1: operational burden outweighs benefit at current scale.

**Observability.**

DISTRIBUTED TRACING (OpenTelemetry):
All three runtimes instrumented with OTel SDKs. Go: otelgrpc interceptors on orchestrator (both client and server sides), standard otel-go SDK. Python: opentelemetry-sdk with LangChain/LangGraph OTel integration (verify: LangGraph OTel support varies by version — use manual span creation if auto-instrumentation is incomplete). TypeScript: @opentelemetry/sdk-node with HTTP and gRPC instrumentations. Trace propagation: W3C trace context injected into gRPC metadata (Go↔Python) and MCP call headers (Python→TS, as MCP allows arbitrary metadata per call). Trace hierarchy: Session span (top-level, owned by orchestrator) → Run span → Page span → Step span → child spans: LLMCall, MCPToolCall, HealAttempt, GateEvent. Mandatory span attributes: session_id, run_id, plan_id, step_id, run_mode, node_name (LangGraph node), model_name, token_input (int), token_output (int), cost_usd (float), selector (on act/heal spans), confidence (on heal spans), diff_ratio (on verify spans). Exporter: OTLP/gRPC to Grafana Tempo (home lab) or Jaeger (local dev). Sampling: 100% in explore mode, 100% in CI replay mode (every run must be fully observable for trust).

LLM TRANSCRIPT (replayable, immutable):
Every LLM call logged to /runs/{run_id}/llm_transcript.jsonl (append-only, one file per run). Line format: {timestamp, run_id, step_id, node_name, model, prompt_hash(sha256 of rendered prompt), prompt_text, response_text, tokens_input, tokens_output, latency_ms, cost_usd, finish_reason}. File closed (fsync) at run completion. Never truncated or overwritten. Purpose: (a) offline debugging of LLM decisions without re-running against AUT, (b) prompt engineering iteration, (c) cost attribution per node type, (d) compliance audit for "what did the agent decide and why."

PLAYWRIGHT TRACES:
Playwright trace recording (screenshots: true, snapshots: true, sources: true) started at playwright-executor init. One trace per run_id. Exported to /runs/{run_id}/playwright-trace.zip by trace-manager at run end. Viewable with npx playwright show-trace. Also available via report-api GET /runs/{run_id}/trace (proxies the zip file).

TOKEN / COST BUDGET ENFORCEMENT:
Per-run budget configured in agentctl run: --token-budget 50000 --cost-budget-usd 2.00. Defaults configurable in config.yaml (Go Viper). Go BudgetService maintains authoritative counters in SQLite run_index (updated atomically via store-gateway). Python llm-client calls BudgetService.CheckAndDeduct(tokens_estimate, cost_estimate) before every LLM call — if either budget would be exceeded, call returns BudgetExhaustedError, brain routes plan node to report (partial freeze if in explore mode). Hard ceiling: BudgetService.RecordActual validates that actual usage doesn't exceed ceiling+10% tolerance; if exceeded (LLM returned more tokens than estimated), it flags the overrun in run_index for review. Alert threshold: at 80% of either budget, BudgetService emits a BUDGET_WARNING event on EventStream. 100%: BUDGET_EXHAUSTED event, brain receives it via streaming and routes to checkpoint → report.

PROMETHEUS METRICS (Go orchestrator /metrics endpoint):
agent_run_duration_seconds{mode, status} histogram
agent_steps_total{mode, status(pass|fail|healed|quarantined|skipped)} counter
agent_heal_attempts_total{strategy(L1|L2|L3|L4|llm_a11y|llm_visual), outcome(success|flagged|human_gate|failed)} counter
agent_llm_calls_total{model, node} counter
agent_tokens_used_total{model} counter
agent_cost_usd_total{model} counter
agent_budget_remaining_ratio{resource(tokens|cost)} gauge
agent_human_gates_pending gauge
agent_flaky_steps_total gauge
agent_a11y_completeness_ratio{url} histogram (sampled per page visit)

RUN REPORTS:
JSON (report.json): machine-readable, full step-level detail, healing events, cost breakdown, flaky step list, human_gate_pending list, plan_id, plan_hash, aut_version. HTML (report.html): step table with color-coded status (green/red/yellow=healed/gray=quarantined), inline screenshots for failed+healed steps, cost breakdown chart, coverage heatmap (pages visited vs total sitemap). Both generated by Go report-api at run finalize. JSON report suitable for CI artifact upload and downstream tooling (Slack notifications, Grafana annotations).

**Outputs.**

1. FROZEN PLAN (plan-{id}.json): Primary deliverable of explore mode. Deterministic replay artifact. Contains: plan_id, plan_hash (integrity guarantee), aut_version, exploration_seed, ordered PlanStep list with locator alternatives and pre/post a11y snapshot hashes. Stored in SQLite plan_store + file. This is the "test definition" that replaces a human-written test script for exploratory coverage.

2. PLAYWRIGHT TEST CODE ({run_id}/generated.spec.ts): Auto-generated TypeScript Playwright test file from the frozen plan. Each PlanStep becomes a test action using Playwright's recommended locator hierarchy (prefer role/text/label selectors over CSS). Page objects generated per page_url pattern and cached in page_object_cache. Goal: 80%+ of generated code usable by qa-automation-engineer without modification. Emitted at end of every explore run alongside the frozen plan. Allows the autonomous agent to feed the existing qa-automation-engineer workflow.

3. PLAYWRIGHT TRACE ({run_id}/playwright-trace.zip): Full browser trace (network, console, screenshots, DOM snapshots) for every run. Viewable in Playwright Trace Viewer. Critical for debugging failed CI runs without re-running.

4. RUN REPORT ({run_id}/report.json + report.html): Step-level pass/fail/healed/quarantined table. Healing summary (attempts, strategies used, confidence scores). Cost breakdown (tokens and USD per LangGraph node). Coverage metrics (pages visited, interaction types covered, % of sitemap covered this run). Flaky steps list with quarantine status. Human-gate pending list. Plan integrity status (plan_hash match/mismatch).

5. GOLDEN SNAPSHOTS (/runs/baselines/{plan_id}/{step_id}.a11y.json + .png): a11y tree JSON and annotated screenshot per plan step. Reference for CI regression detection. Updated only via explicit agentctl baseline update command. Versioned by (plan_id, step_id) — old baselines archived with superseded_by reference.

6. HEALING AUDIT TRAIL (/runs/{run_id}/healing-audit.jsonl + aggregated in SQLite healing_audit): Immutable append-only log of every healing attempt. Queryable via agentctl healing report [--plan-id] [--date-range]. Enables trend analysis: which selectors heal most often (DOM instability signal), which pages need data-testid instrumentation, confidence calibration drift.

7. LLM TRANSCRIPT ({run_id}/llm-transcript.jsonl): Replayable record of every LLM prompt+response with token counts and cost. Immutable post-run. Used for: offline debugging, prompt engineering, cost attribution, compliance audit.

8. COVERAGE SITEMAP ({run_id}/sitemap.json + persisted in SQLite): Discovered page graph with nodes (URL, page_type, interactive_element_count, a11y_completeness_ratio) and edges (navigation paths). Updated incrementally across runs. Shows which parts of the AUT have been explored vs not. Viewable as JSON or via report-api /sitemap endpoint (graph format for UI rendering).

**MVP path.**

PHASE 1 — Go Spine (Weeks 1-2): Scaffold Go module with agentctl CLI (cobra), orchestrator gRPC server (RunControl + BudgetService stubs returning OK), store-gateway with SQLite WAL schema (plan_store, locator_store, sitemap, run_index — all tables created, no logic yet), report-api HTTP skeleton (200 OK on /health). Wire proto3 definitions (buf.build toolchain). Validate: Go compiles, gRPC client can connect, SQLite tables created cleanly, agentctl run prints "run started" and immediately completes.

PHASE 2 — TypeScript Hands (Weeks 3-4): Scaffold Node.js project (TypeScript, ts-node or tsx for dev). Implement playwright-executor MCP stdio server with 6 initial tools: navigate, click, fill, snapshot_a11y, screenshot_annotated (set-of-marks overlay JS injected), close_context. Playwright trace recording start/stop. Validate: MCP server responds over stdio to manually crafted JSON-RPC calls; snapshot_a11y returns valid ARIA tree from a real page (use playwright.dev as test target); screenshot_annotated shows numbered mark overlays.

PHASE 3 — Python Brain Basic Loop (Weeks 5-6): Scaffold Python project (uv, pyproject.toml). Define AgentState TypedDict with full schema. Implement 5 nodes: perceive (MCP snapshot), ground (sitemap update), plan (LLM call, Opus 4.8, single action output), act (MCP tool call), observe (MCP re-snapshot). Wire LangGraph graph with SqliteSaver checkpointer. Wire gRPC client to Go orchestrator (BudgetService pre-check before plan LLM call). Run a 3-step explore session against a local test app. Validate: full polyglot stack communicates (Go↔Python gRPC, Python↔TS MCP stdio), LLM produces a valid click action, action executes in browser, checkpoint saves to SQLite.

PHASE 4 — Verify + Heal + Human Gate (Weeks 7-8): Add verify node (a11y structural diff, golden snapshot capture on first pass). Add heal node with healing-engine: strategy L1-L4 rotation + LLM a11y-tree reasoning (Pass A). Add human_gate via GateService gRPC + checkpoint async pause. Implement agentctl gate list/approve/skip/abort. Add HealAttempt persistence to store-gateway (healing_audit append-only). Validate: intentionally break a selector in the test app, observe heal node fire, L1 strategy fail, LLM re-ground successfully, confidence check pass, locator persisted, act succeed with healed selector. Test human gate: break locator with unsolvable DOM, observe confidence < 0.60, gate created, agentctl gate skip resolves it.

PHASE 5 — Plan Freezing + Replay Mode (Weeks 9-10): Implement freeze node (serialize plan, compute plan_hash, push to store-gateway). Implement plan-manager freeze/load/hash-verify. Implement replay/CI mode in plan node (reads frozen_plan[step_index] without LLM). Implement golden snapshot diff in verify node. Implement agentctl run --mode ci --plan-id. Implement CI exit codes (0/1/2/3). Validate: complete explore run produces frozen plan; immediate CI replay of same plan succeeds with 0 exit code and 0 LLM calls; manually edit plan JSON, verify replay aborts with hash mismatch.

PHASE 6 — Full Observability + Cost Enforcement (Weeks 11-12): OTel instrumentation across all three runtimes (Go otelgrpc, Python SDK, TS @opentelemetry/sdk-node). Trace propagation via W3C context headers in gRPC metadata and MCP calls. Hard budget enforcement in BudgetService (ceiling check in RecordActual). Budget warning at 80% (EventStream event + log). Prometheus /metrics endpoint in Go orchestrator. LLM transcript logging (append to JSONL on every llm-client call). Run report generation (JSON + HTML) in report-api. agentctl report show {run_id}. Validate: full explore+replay run visible in Jaeger/Tempo with correct parent-child span hierarchy; cost tracked accurately; budget exhaustion at --cost-budget-usd 0.01 terminates gracefully with partial report.

PHASE 7 — Flake Quarantine + Test Code Export (Weeks 13-14): Implement flake_registry table and UpdateFlakeCount in store-gateway. Implement quarantine logic in CI exit code calculation. Implement generated.spec.ts export from frozen plan (page object generator, role-selector preference). Implement page_object_cache in SQLite. Implement agentctl healing report --flaky. Implement set-of-marks visual re-grounding (LLM Pass B in healing-engine, adds vision capability). Full integration test: 2-week simulated CI cycle on a real web app (suggest: a local Gitea instance). Validate: intentionally flaky step gets quarantined after 3 failures, CI exit code remains 0, generated.spec.ts passes standalone npx playwright test run.

**Key risks:**

- LLM API latency variance makes CI replay time unpredictable when healing is triggered: mitigate with 2-attempt heal cap in CI mode then auto-skip, plus per-step timeout (--step-timeout flag, default 60s) enforced by orchestrator via gRPC deadline propagation.
- Playwright version upgrades change MCP tool signatures or accessibility snapshot format, breaking replay: mitigate by version-pinning Playwright in package-lock.json, owning the MCP server wrapper so TypeScript-to-MCP translation absorbs Playwright API changes without touching Python or Go, and keeping tool signatures in versioned mcp-schema.
- Accessibility tree is incomplete or absent on canvas-heavy apps, custom web components, and cross-origin iframes: mitigate by surfacing a11y_completeness_ratio as a first-class metric; when ratio < 0.30, automatically skip Pass A (LLM a11y-tree reasoning) and go directly to Pass B (visual re-grounding); flag page in sitemap as low-a11y and recommend to AUT team that data-testid or ARIA role instrumentation is needed.
- SQLite WAL write amplification under parallel CI runs sharing the same database file: store-gateway is the single writer (Go mutex + WAL journal), but high-frequency concurrent runs may queue behind WAL checkpointing; mitigate with WAL checkpoint pragma tuned (PRAGMA wal_autocheckpoint=1000), separate run-scoped LangGraph SQLite per run (already separate), and a documented scale trigger: > 10 concurrent runs → migrate store-gateway to PostgreSQL.
- Frozen plan diverges silently when AUT has visual-only changes (CSS, layout) that do not affect ARIA tree: a11y-only golden snapshots will pass while the visual UX is broken; mitigate by including screenshot hash in golden_snapshot alongside a11y hash, and flagging screenshot-hash divergence as VISUAL_WARN in report (not a failure, but surfaced for human review).
- LLM confidence scores are miscalibrated — either too aggressive (auto-healing wrong elements) or too conservative (too many human gates in CI): mitigate with healing audit trail calibration analysis (agentctl healing calibrate --plan-id computes precision/recall of past auto-heals against human_verified outcomes), plus per-site confidence threshold configuration in config.yaml rather than global hardcoded values.
- Token budget exhaustion mid-explore produces an incomplete frozen plan with partial coverage: mitigate with pre-step budget pre-check (route to checkpoint on exhaustion), partial plan freeze (save progress up to last completed step), and agentctl run --resume {run_id} to continue exploration in a new run with remaining budget.
- Python↔TypeScript MCP stdio subprocess reliability: SIGPIPE on large payloads (set-of-marks screenshots are large), zombie processes on brain crash, deadlock on synchronous MCP call with full output buffer: mitigate with chunked MCP framing (MCP spec allows streaming), heartbeat tool (MCP ping every 30s, 5s timeout → restart), and Go orchestrator process supervision watching the Python brain PID (restart on crash, which cascades to TS subprocess restart).

**ADRs:**

- ADR-001: MCP over stdio for Python-to-TypeScript boundary. REJECTED: gRPC — TypeScript grpc-node adds generated-code build complexity (protoc plugin for TS, separate generated stubs), has a materially less mature ecosystem than Python grpc, and produces an API shape identical to what MCP provides natively; MCP stdio avoids port allocation flakiness in CI containers and maps directly to LangGraph's tool-calling node pattern with zero adapter code.
- ADR-002: Explore-once-then-freeze-plan-replay-deterministically for CI, over seeded/temperature-0 LLM re-execution. REJECTED: seeded LLM replay — LLM API providers (including Anthropic) do not contractually guarantee output determinism even at temperature=0 with a fixed seed; streaming tokenization and batching introduce variance; frozen plan is the only trustworthy reproducibility guarantee and additionally eliminates LLM API dependency on the CI happy path (zero cost, zero latency variance from LLM on passing runs).
- ADR-003: SQLite WAL mode with Go store-gateway as sole writer for all long-term persistence. REJECTED: PostgreSQL — adds a stateful service (container, backup schedule, WAL config, connection pooling) with zero benefit at single-node scale; SQLite WAL concurrent-read + single-writer pattern matches the Go store-gateway architecture; scale trigger to PostgreSQL is explicit and schema-compatible.
- ADR-004: Accessibility tree as primary page perception modality, set-of-marks visual screenshot as fallback only when a11y_completeness_ratio < 0.30. REJECTED: screenshot-only perception — vision model calls cost approximately 4-6x more tokens than text, introduce rendering-variance flakiness (antialiasing, font subpixel differences across environments), and produce weaker locator outputs (CSS paths from visual bounding boxes are fragile); ARIA tree is semantic, stable across visual re-skins, and produces role/name selectors directly usable by Playwright.
- ADR-005: gRPC proto3 bidirectional streaming for Go-to-Python orchestrator-brain boundary. REJECTED: REST/HTTP — budget exhaustion events and gate resolution require server-push from Go to Python, which over REST requires SSE or polling; protobuf schema validated at compile time prevents silent type drift across Go and Python stubs; gRPC deadline propagation implements per-step timeout cleanly without application-layer timers.
- ADR-006: LangGraph built-in checkpointer (SqliteSaver local / AsyncPostgresSaver prod) as episodic session memory. REJECTED: custom episodic state store — LangGraph's checkpointer provides thread-isolated state, crash recovery (resume from any node boundary), and branching (what-if exploration forks) out of the box; reimplementing these semantics is months of work with high correctness risk for no architectural benefit.
- ADR-007: Async human gate via LangGraph checkpoint pause and CLI agentctl gate resolve, over synchronous pipeline blocking. REJECTED: synchronous inline block (agent polls until human responds) — CI pipelines have hard job timeouts (30-60 min typical); synchronous block causes timeout → lost run state; async checkpoint preserves full state indefinitely, allows human to act hours later, and supports configurable auto-skip timeout for unattended CI (default 30 min in CI mode, unlimited in explore mode).
- ADR-008: Claude Sonnet 4.6 for healing re-grounding (heal node), Claude Opus 4.8 reserved exclusively for initial exploration planning (plan node in explore mode). REJECTED: Opus for all LLM calls — heal node is in the hot path of CI (called on every locator failure) and must return in < 10 seconds; Sonnet is sufficient for the constrained reasoning task of locator re-grounding (structured output over an ARIA tree or numbered marks is not a high-difficulty reasoning problem); this single routing decision reduces per-run LLM cost by an estimated 70-80% given healing frequency in typical runs.

---

### `pragmatic-evolution` — CognitivePilot — Pragmatic MVP → Scale Evolution

*Lens: Pragmatic MVP -> scale evolution*

**Philosophy.** The core bet: buy everything Playwright and LangGraph already give you for free — official Playwright MCP server, built-in trace viewer, built-in codegen API, LangGraph SQLite checkpointer, LangGraph ToolNode — so that the only bespoke code is the differentiating logic (autonomous exploration strategy, self-healing hierarchy, explore-once-then-replay determinism engine). The smallest valuable slice is a Go CLI that spawns a Python brain that talks to the official Playwright MCP subprocess and produces a frozen plan.json plus a trace ZIP; everything else (gRPC orchestrator, report service, OTel, visual fallback) is layered on top of that proven wire after the wire is stable. Every build-vs-buy call is made explicitly at each milestone gate; nothing is deferred silently — if it is deferred it is named and its trigger condition is stated. Scale pain points (SQLite single-writer, single-machine CI) are accepted as v1 constraints with documented migration triggers rather than pre-solved with infrastructure the team does not yet need.

**Components (7):**

| Component | Language | Responsibility |
|-----------|----------|----------------|
| agent-ctl | Go | CLI entry point: subcommands run (--explore \| --replay \| --heal), report, serve, locators. Parses run config YAML, spawns orchestrator, tails streamed log lines from gRPC, exits with run status code (0 = pass, 1 = fail, 2 = partial/needs-human). The only user-facing binary. Must work in CI (non-TTY) and interactive. Build this first. |
| orchestrator | Go | Run lifecycle manager and gRPC server. Supervises the Python brain subprocess (start, health-ping, restart-on-crash, SIGTERM on timeout). Exposes RunControl gRPC service (StartRun, StopRun, GetRunStatus with server-streaming log tail, GetRunResult). Owns the run state machine at the process level: PENDING -> RUNNING -> HEALING -> PARTIAL -> DONE \| FAILED. Routes NEEDS_HUMAN_REVIEW events from brain to stdout/webhook. Does NOT touch SQLite directly — delegates all writes to persistence-gateway. |
| brain | Python | LangGraph 8-node state machine: perceive, ground, plan, act, verify, heal, checkpoint, report. Owns all LLM calls (Opus 4.8 for planning, Sonnet 4.6 for healing). Spawns the official Playwright MCP server as a subprocess and calls its tools via MCP stdio protocol through LangGraph ToolNode. Connects to Go orchestrator as gRPC client (reports events, fetches locator cache, persists results). Owns explore-once-then-replay mode switching and plan_hash computation. This is the only place LLM reasoning lives. |
| playwright-mcp | TypeScript | BUY: the official @playwright/mcp server (Microsoft-maintained). Run as child process of the Python brain (stdio). Exposes browser tools: navigate, click, fill, accessibility_snapshot, screenshot, locator_evaluate, trace_start, trace_stop, codegen_record, codegen_stop. The brain calls these as MCP tools via ToolNode. This component is NOT built — it is installed as an npm package and launched as a subprocess. The only custom TS code is a thin artifact-pusher sidecar that POSTs trace ZIPs and codegen output to the persistence-gateway REST endpoint when the run ends. |
| persistence-gateway | Go | Single-writer SQLite gateway. All DB writes in the system funnel through this service — brain writes locators and events via gRPC PersistenceService RPCs; artifact-pusher POSTs binary artifacts via REST. Owns schema migrations (golang-migrate). Tables: runs, healed_locators, page_models, healing_events, step_failures, checkpoints (LangGraph overflow), run_transcripts. SQLite in WAL mode. Exposes read RPCs so brain can query locator cache and page model cache without hitting SQLite directly from Python. |
| report-service | Go | Run artifact assembly and REST API for CI consumers. On run completion: reads run record from SQLite, assembles run_report.json, renders HTML report (Go template, mirrors Playwright HTML reporter structure), invokes Playwright codegen stop via the artifact-pusher and collects the .spec.ts output, packages healing_report.json, publishes to artifact directory. Exposes GET /runs/{id}/report, GET /runs/{id}/trace, GET /runs/{id}/spec, GET /runs/{id}/cost. Optional: webhook on run completion for CI integration. DEFERRED in v1 milestone 0-2; introduced at milestone 3. |
| proto | shared | Protobuf definitions for all gRPC contracts (Go spine <-> Python brain). Three services: RunControl (orchestrator serves, agent-ctl consumes), PersistenceService (persistence-gateway serves, brain consumes), EventStream (orchestrator serves streaming events, report-service consumes). Single source of truth — stubs generated in CI for Go and Python. Breaking the proto breaks the build. Kept in /proto directory, versioned with the repo. |

**Boundaries (polyglot contracts).**

Go <-> Python: gRPC proto3, three services. RunControl: StartRun(RunConfig) -> stream RunEvent; StopRun(RunId) -> Ack; GetRunResult(RunId) -> RunResult. PersistenceService: WriteLocator(HealedLocator) -> WriteAck; ReadLocators(PageUrl) -> stream LocatorRecord; WriteEvent(RunEvent) -> WriteAck; WriteRunResult(RunResult) -> WriteAck; ReadPageModel(UrlHash) -> PageModelRecord. EventStream: SubscribeEvents(filter) -> stream RunEvent (used by report-service). Rationale: gRPC gives type-safe contracts across Go and Python, bidirectional streaming for log tailing, and a wire that is verifiable in CI (proto mismatch = build failure). Failure isolation: if the Python brain process crashes, the Go orchestrator detects the gRPC disconnect (DeadlineExceeded on next health-ping within 5s), marks the run FAILED, and persists partial state via persistence-gateway. The brain crash does not take down the orchestrator or the CLI.

Python <-> TypeScript: MCP over stdio. Python brain spawns the official @playwright/mcp binary as a subprocess (`node @playwright/mcp/cli`) and communicates via JSON-RPC 2.0 over stdin/stdout. LangGraph ToolNode natively understands MCP tool descriptions — no adaptation layer needed. Tools available: navigate, accessibility_snapshot, screenshot, click, fill, locator_evaluate, trace_start/stop, codegen_record/stop. Rationale: MCP stdio is the native LLM tool-call protocol; LangGraph ToolNode handles it without any custom adapter; stdio avoids port allocation, firewall rules, and service discovery. Rejected alternative: custom gRPC TS server — would require maintaining a proto for browser operations that Playwright MCP already defines well. Failure isolation: Python subprocess monitor detects stdout EOF (MCP server crash), restarts the MCP subprocess and re-navigates to the last known URL from RunState.page_model.url. The LangGraph checkpoint means no work is lost.

TypeScript -> Go: REST HTTP. The thin artifact-pusher sidecar (the only custom TS code) POSTs to persistence-gateway endpoints: POST /artifacts/trace (multipart, ZIP), POST /artifacts/codegen (JSON, .spec.ts content), POST /artifacts/screenshot (PNG). Fire-and-forget with three retries and exponential backoff. Rationale: unidirectional data push, no streaming needed, simplest possible interface. Circular gRPC dependency (TS calling back into Go orchestrator) would require TS to be a gRPC client, which adds complexity for what is essentially a file upload.

**Agent loop.**

NODES (8):

perceive: Calls MCP accessibility_snapshot() — returns structured ARIA tree (roles, names, states, relationships). If completeness ratio (named interactive elements / total interactive elements) < 0.30, also calls screenshot() for set-of-marks fallback context. Stores raw a11y tree in RunState.page_model. Also calls MCP trace_start() at run start and on resume.

ground: Parses a11y tree into typed PageModel: {url, title, landmarks: list[Landmark], forms: list[Form], interactive_elements: list[Element], depth_estimate}. Computes page identity hash (URL + landmark structure). Checks persistence-gateway for cached PageModel (ReadPageModel RPC) — if cache hit and run_mode==replay, validates structural drift vs golden snapshot. Updates RunState.page_model.

plan: ONLY entered when run_mode==explore OR current_step==0 with no frozen plan. Calls Opus 4.8 with: target_url, page_model, episodic_buffer tail (last 10 events), exploration_goal. LLM returns ordered list of PlannedAction {intent, selector_hint, action_type, expected_outcome, is_critical}. Computes plan_hash = SHA256(json.dumps(exploration_plan, sort_keys=True)). Writes plan.json to artifact dir. Checks token_budget.plan_tokens_used before call — hard-aborts if over limit. Skipped entirely in replay mode.

act: Pops next PlannedAction from exploration_plan[current_step]. In explore mode: executes via MCP tool (click/fill/navigate/etc.) using selector_hint. In replay mode: uses frozen selector from plan.json. Appends to RunState.executed_actions. Increments current_step.

verify: Calls perceive logic inline (a11y snapshot) to detect post-action state. Checks: URL changed as expected? Error modal appeared? Expected element now visible? Locator-not-found exception from act node? Classifies outcome: PASS, LOCATOR_STALE, ELEMENT_GONE, TIMING, UNEXPECTED_ERROR. On PASS and milestone step: calls checkpoint node. On any failure: routes to heal node. On all steps exhausted or budget exceeded: routes to report node.

heal: Self-healing algorithm (see selfHealing field). On successful heal: updates exploration_plan[current_step].selector with healed selector, routes back to act for retry. On heal failure after 3 attempts: marks step FAILED in RunState, emits NEEDS_HUMAN_REVIEW event via gRPC WriteEvent, routes to report (or continues to next step if is_critical==false).

checkpoint: Triggers LangGraph checkpoint flush to SQLite (built-in LangGraph SQLite checkpointer). Also calls PersistenceService.WriteLocator for any newly healed locators pending in RunState.healed_locators. Writes page model to persistence-gateway if not cached. Routes back to perceive for next cycle.

report: Called on run completion (all steps done, budget exceeded, or critical step failed). Calls MCP trace_stop() — triggers artifact-pusher to POST trace ZIP to persistence-gateway. Writes RunResult to persistence-gateway via WriteRunResult RPC. Populates RunState.artifacts with paths. Routes to END.

EDGES:
perceive -> ground (unconditional)
ground -> plan (run_mode==explore AND current_step==0)
ground -> act (run_mode==replay OR plan exists AND current_step>0)
plan -> checkpoint (plan just frozen)
plan -> act (after checkpoint)
act -> verify (unconditional)
verify -> heal (LOCATOR_STALE | ELEMENT_GONE | TIMING)
verify -> checkpoint (PASS AND milestone_step)
verify -> act (PASS AND not milestone_step AND steps_remain)
verify -> report (all_steps_done OR budget_exceeded OR UNEXPECTED_ERROR)
heal -> act (heal succeeded)
heal -> report (heal failed AND is_critical==true)
heal -> act_next_step (heal failed AND is_critical==false)
checkpoint -> perceive (next cycle)
report -> END

SHARED STATE OBJECT (RunState TypedDict):
run_id: str
target_url: str
run_mode: Literal["explore", "replay", "heal"]
exploration_plan: list[PlannedAction]  # {intent, selector, action_type, expected_outcome, is_critical, healed: bool}
plan_hash: str
current_step: int
page_model: PageModel  # {url, title, a11y_tree: dict, landmarks, forms, interactive_elements, completeness_ratio, golden_hash}
episodic_buffer: list[EpisodicEvent]  # bounded deque max 50, evicts oldest
executed_actions: list[ExecutedAction]  # {step, action_type, selector, outcome, duration_ms}
healed_locators: list[HealedLocator]  # pending flush to persistence-gateway
pending_human_review: list[HealCandidate]  # confidence 0.60-0.84, flagged
token_budget: TokenBudget  # {plan_used, plan_limit, heal_used, heal_limit} — both in tokens
confidence_scores: dict[str, float]  # keyed by step index
artifacts: RunArtifacts  # {trace_path, screenshot_paths, codegen_path, report_path}
last_error: Optional[str]
heal_attempts: int  # reset on new step
step_failures: dict[str, int]  # step_key -> consecutive_failure_count, for flake quarantine

**Self-healing.**

STEP 1 — PERCEPTION (in verify node):
Call MCP accessibility_snapshot(). Compute completeness_ratio = len(named_interactive) / max(1, expected_interactive_count). If completeness_ratio < 0.30 (sparse tree — heavy canvas, custom web components, shadow DOM), also call screenshot() and annotate with set-of-marks: draw numbered bounding boxes over all detected interactive regions using page.evaluate() + canvas overlay (verify this Playwright capability before relying on it). Store both representations in RunState.page_model.

STEP 2 — FAILURE CLASSIFICATION (in verify node):
Catch Playwright exception from act node. Classify:
- LOCATOR_STALE: element present in a11y tree but selector no longer matches (e.g., class name changed, ID regenerated).
- ELEMENT_GONE: element absent from a11y tree entirely (feature removed, conditional render, A/B variant).
- TIMING: element present but not yet interactable (detached, covered, animating) — retry act with 2s wait before escalating to heal.
- UNEXPECTED_ERROR: navigation error, network failure, JS exception — skip healing, emit event, route to report.

STEP 3 — RE-GROUNDING HIERARCHY (in heal node, sequential, 3 attempts max):
Attempt 1 (zero LLM cost, deterministic): Strategy rotation. For the failed PlannedAction.intent, try selectors in order: (a) data-testid attribute matching intent keywords, (b) ARIA role + accessible name, (c) CSS by semantic class (not generated hash classes), (d) visible text content match, (e) XPath positional as last resort. Call MCP locator_evaluate(selector) for each candidate. First candidate that resolves to exactly one element: use it. Confidence assigned: 0.95 if data-testid, 0.90 if ARIA role+name, 0.80 if text, 0.70 if XPath.

Attempt 2 (Sonnet 4.6, structured reasoning): If attempt 1 fails or returns zero matches. Build prompt: "The action [intent] previously matched selector [old_selector]. The element is no longer found. Here is the current accessibility tree: [a11y_tree_json truncated to 4000 tokens]. Return JSON {new_selector: string, confidence: float, reasoning: string}. Prefer data-testid, then ARIA role+name, then visible text. Do not return XPath unless no alternative exists." Parse JSON response. Call MCP locator_evaluate(new_selector). If resolves to exactly one element: use it. Confidence = min(llm_confidence, 0.90). Check heal token budget before call.

Attempt 3 (Sonnet 4.6 vision, set-of-marks): Only if completeness_ratio < 0.30 (visual fallback is needed) AND attempt 2 failed. Send annotated screenshot (numbered bounding boxes) with prompt: "Which numbered element corresponds to the action: [intent]? Return JSON {box_number: int, confidence: float}." Extract box_number, retrieve bounding box coordinates from the annotation map, derive a coordinate-based click (MCP click(x, y) — verify Playwright MCP supports coordinate clicks). Confidence = llm_confidence * 0.85 (penalized for imprecision).

STEP 4 — CONFIDENCE GATE:
>= 0.85: auto-persist healed locator. Call PersistenceService.WriteLocator({original, healed, method, confidence, page_url, element_label, run_id}). Update RunState.exploration_plan[current_step].selector. Update plan.json on disk. Set healed=true on PlannedAction. Continue run.
0.60 - 0.84: write to RunState.pending_human_review. Call PersistenceService.WriteEvent({type: HEAL_CANDIDATE, ...}). Continue run with healed locator (optimistic — most 0.70+ heals are correct). CI will surface the review queue in the run report.
< 0.60: call PersistenceService.WriteEvent({type: NEEDS_HUMAN_REVIEW}). Orchestrator emits to stdout/webhook. Behavior is configurable per run: CI mode = skip step and continue; interactive mode = pause and wait for human input (5 min timeout then skip).

STEP 5 — AUDIT TRAIL:
Every heal attempt (successful or not) writes a row to SQLite healing_events table via PersistenceService: {run_id, step, step_key, original_selector, attempt_1_result, attempt_2_tokens, attempt_3_tokens, final_selector, final_method, confidence, outcome, timestamp}. This is the primary forensic artifact for understanding DOM drift over time. Healing audit included in healing_report.json artifact.

**Determinism.**

CORE MECHANISM — Explore-Once-Then-Replay:
First run: agent-ctl run --explore. Brain enters explore mode, executes full LangGraph explore->plan->act->verify->checkpoint cycle. On run completion, plan.json is written: {plan_hash: SHA256(json.dumps(exploration_plan, sort_keys=True)), steps: [...PlannedAction with resolved selectors...], golden_snapshots: {step_index: a11y_hash}, created_at, target_url, agent_version}. plan.json is committed to the repo. CI always runs: agent-ctl run --replay --plan plan.json. In replay mode, the plan node is skipped entirely — ground routes directly to act. No LLM planning tokens consumed. No non-deterministic LLM decisions in the hot path.

LLM TEMPERATURE DISCIPLINE:
All planning calls: temperature=0. This does not guarantee identical outputs across model versions, but eliminates sampling variance within a fixed model. plan_hash is computed after the plan is produced and stored in plan.json. On replay, agent-ctl run --replay verifies plan_hash matches the file. If it does not (file was manually edited or partially healed), the run aborts unless --force-replay is passed.

PLAN SELF-UPDATE ON HEALING:
When a locator is healed with confidence >= 0.85 during replay, report-service updates plan.json in place and recomputes plan_hash. CI can be configured to auto-commit the updated plan.json (git commit -m "heal: update plan for step N") or to emit it as a PR artifact for human review. This keeps the plan file as the single source of truth for stable locators.

FLAKE QUARANTINE:
step_failures table in SQLite tracks: step_key (url + intent hash), consecutive_failure_count, last_seen_at. After 3 consecutive replay failures on the same step across different CI run IDs (not the same run, not transient): step is marked quarantined. Quarantined steps are skipped in replay (logged as SKIPPED_QUARANTINED in run report). A non-zero quarantine count makes the run exit with code 2 (PARTIAL) rather than 0 (PASS), which CI can treat as a warning. Human clears quarantine by running agent-ctl locators clear-quarantine --step-key <key>.

GOLDEN A11Y SNAPSHOTS:
perceive node saves a11y_hash (SHA256 of sorted a11y tree JSON) for milestone steps. Stored in plan.json golden_snapshots. On replay, ground node computes current a11y_hash and diffs vs golden. Structural divergence (new modal present, nav removed, form added) emits STRUCTURAL_CHANGE event. Configurable action: warn-only (default) or trigger partial re-explore for the affected subtree. This catches the case where the page changed in ways that would make the frozen plan nonsensical even if individual selectors still resolve.

SEEDED EXPLORATION:
In explore mode, the plan node is given a stable exploration_seed derived from SHA256(target_url + run_date_utc_day). This does not make LLM outputs deterministic (temperature=0 is the real lever), but it gives the exploration a stable context anchor and makes the seed auditable. More importantly: the seed is logged in plan.json so that if a plan diverges, the exploration can be re-run with the same seed.

**Memory.**

SHORT-TERM (episodic, within session):
RunState.episodic_buffer — Python-side bounded deque, max 50 EpisodicEvent records {node, action, outcome, a11y_delta, timestamp}. Held in LangGraph in-memory state. Flushed to SQLite at every checkpoint node via the LangGraph built-in SQLite checkpointer (BUY: use langgraph.checkpoint.sqlite.SqliteSaver — one line of setup). Enables resume-on-crash: if the Python brain subprocess crashes mid-run, the orchestrator restarts it and the brain reloads the latest checkpoint. The run resumes from the last checkpointed step, not from zero.

LONG-TERM (persisted across runs):
1. Locator Store — SQLite table healed_locators: {id, page_url_hash, element_label, original_selector, healed_selector, healing_method, confidence, times_validated, last_used_at, last_validated_at, created_at, status: active|deprecated|pending_review}. Before Attempt 1 in the heal node, the brain calls PersistenceService.ReadLocators(page_url) — a cache hit (matching element_label + high confidence) means zero LLM healing cost. This is the primary cost optimization for recurring DOM changes.

2. Page Model Cache — SQLite table page_models: {url_hash, a11y_tree_json, landmarks_json, form_count, interactive_count, golden_a11y_hash, last_updated_at}. Seeded by first explore run. Used by ground node to detect structural drift on replay. Invalidated when structural change is detected.

3. Page Object Cache — exported .spec.ts files in repo (generated by report-service from executed_actions + Playwright codegen output). These are human-readable, version-controlled, and reusable by qa-automation-engineer without touching the agent. BUY: Playwright's codegen API produces valid TypeScript test code from recorded actions — the agent calls codegen_record() at act time and codegen_stop() at run end.

4. Run History — SQLite table runs: {run_id, target_url, plan_hash, run_mode, status, token_cost_usd, plan_tokens, heal_tokens, duration_ms, step_count, heal_count, artifact_dir, created_at, completed_at}. Used by report-service for trend queries and cost dashboards.

STORAGE: Single SQLite file agent.db, WAL mode, written exclusively by persistence-gateway. Location: configurable (default: ./data/agent.db, CI: /tmp/agent-{run_id}.db for parallelism, home-lab: /opt/agent_development/data/agent.db). Migration trigger for PostgreSQL: > 50 concurrent CI runs on different machines sharing the same DB, or need for multi-node agent workers. Not expected in v1.

**Observability.**

DISTRIBUTED TRACING (OpenTelemetry):
Every LangGraph node is an OTel span. Python brain instruments via opentelemetry-sdk + opentelemetry-instrumentation-langchain (verify availability for LangGraph — may require manual span wrapping). Span attributes: run_id, node_name, step_index, run_mode, model_used. Trace context propagated into gRPC metadata (Go orchestrator -> Python brain via W3C traceparent header). Go components instrumented via go.opentelemetry.io/otel. Export: OTLP gRPC to home-lab Grafana Alloy -> Grafana Tempo. One trace per run, one span per node invocation. Self-healing spans include: heal_attempt_number, healing_method, confidence_score, tokens_consumed.

PER-DECISION LLM TRANSCRIPT:
Every LLM call (plan node, heal attempt 2, heal attempt 3) appends a JSONL record to run_transcripts/{run_id}.jsonl: {ts, run_id, node, model, prompt_tokens, completion_tokens, latency_ms, cost_usd_estimate, decision_summary (first 200 chars of output), temperature}. Written to disk by brain, POSTed to persistence-gateway at run end. Enables: post-run cost audit, prompt debugging, replay of exactly what the LLM decided and why.

TOKEN BUDGET + HARD CAPS:
RunState.token_budget tracks per-model usage. Config YAML specifies: plan_token_limit (default 50000 Opus tokens per run), heal_token_limit (default 20000 Sonnet tokens per run). Before every LLM call, brain checks: if budget.plan_used >= plan_limit, skip plan node and emit BUDGET_PLAN_EXCEEDED event (run switches to replay with current plan). If budget.heal_used >= heal_limit, healing falls back to strategy rotation only (attempt 1) and marks unresolved steps for human review. Budget exceeded does not abort the run — it degrades gracefully. Estimated cost computed as: tokens * model_price_per_token (hardcoded from config YAML, update manually when pricing changes).

PLAYWRIGHT TRACES:
MCP trace_start() called at run start and after each crash-resume. MCP trace_stop() called at run end — produces a .zip containing network HAR, console logs, screenshots, and all actions with timing. Pushed to persistence-gateway by artifact-pusher sidecar. Viewable with: playwright show-trace <path>. This is the primary debugging artifact for CI failures — no custom trace infrastructure needed. BUY entirely from Playwright.

COST DASHBOARD:
Go report-service exposes GET /metrics (Prometheus format): agent_run_total, agent_run_duration_seconds, agent_tokens_plan_total, agent_tokens_heal_total, agent_cost_usd_total, agent_heal_success_rate, agent_flake_quarantine_count. Scraped by home-lab Prometheus, visualized in Grafana. No custom metrics infrastructure needed — standard Prometheus client library in Go.

**Outputs.**

1. Run Report (JSON + HTML): run_{id}_report.json — machine-readable: {run_id, status, step_count, pass_count, fail_count, skip_count, heal_count, token_cost_usd, duration_ms, plan_hash, artifact_paths}. run_{id}_report.html — human-readable HTML (Go template), mirrors Playwright HTML reporter structure so CI systems that already parse Playwright reports work without changes.

2. Frozen Exploration Plan: plan.json — {plan_hash, target_url, steps, golden_snapshots, agent_version, created_at}. The primary CI artifact — committed to repo, drives all replay runs. This is also the primary output of the first explore run.

3. Playwright Trace: traces/run_{id}.zip — complete Playwright trace (network, console, screenshots, actions, timing). Viewable with: playwright show-trace traces/run_{id}.zip. No additional tooling needed. BUY entirely.

4. Exported Playwright Test Code: generated/{page_slug}.spec.ts — valid TypeScript Playwright test file, one per explored page flow, derived from executed_actions + Playwright codegen output. Usable by qa-automation-engineer as a starting point for maintained test suites. BUY: Playwright codegen API generates the raw test code; agent formats it into a proper spec file with test() blocks and expect() assertions derived from PlannedAction.expected_outcome fields.

5. Regression A11y Baselines: baselines/{url_hash}_a11y.snap — JSON a11y tree snapshots for milestone steps. Diffed on each replay run. Structural changes surfaced in run report.

6. Healing Audit Report: heal_{id}_report.json — {run_id, total_heals, auto_persisted, pending_review, failed, by_method: {strategy_rotation: N, llm_a11y: N, visual: N}, locators: [...]each with confidence and outcome}. Consumed by CI to decide whether to auto-commit updated plan.json or open a PR.

7. Cost Report: Embedded in run_report.json plus rows in SQLite runs table for trend analysis via Prometheus/Grafana.

**MVP path.**

MILESTONE 0 — "Hello Browser" (Days 1-3). Goal: prove the wire, not the intelligence.
- Go: agent-ctl with single `run` subcommand. No gRPC yet. Spawns Python brain as subprocess with env vars (TARGET_URL, RUN_ID, ARTIFACT_DIR). Waits for exit code.
- Python: single-node LangGraph graph (just perceive node). Spawns official @playwright/mcp server subprocess. Calls accessibility_snapshot(). Prints result to stdout. Exits 0.
- TypeScript: npm install @playwright/mcp. That is all. No custom code.
- Output: accessibility tree JSON printed to stdout. Playwright trace ZIP in artifact dir.
- BUILD: Go CLI (50 lines), Python perceive node (30 lines). BUY: everything Playwright. DEFER: gRPC, SQLite, orchestrator, all other nodes.
- Gate: agent-ctl run --target https://example.com produces a non-empty a11y tree and a trace ZIP. CI-runnable (headless Chromium).

MILESTONE 1 — "Autonomous Walk" (Days 4-10). Goal: full explore run, frozen plan, transcript.
- Python brain: all 8 nodes (perceive, ground, plan, act, verify, checkpoint, report — heal is stub). LangGraph SQLite checkpointer (BUY, built-in). Calls Opus 4.8 (or Sonnet 4.6 as budget proxy) in plan node. Writes plan.json.
- Go: agent-ctl passes RUN_MODE=explore env var. Reads plan.json after brain exits.
- Output: plan.json with plan_hash, run_transcript JSONL, Playwright trace ZIP.
- BUILD: 7 LangGraph nodes, ground parser, token budget check. BUY: LangGraph checkpointer.
- DEFER: gRPC (still subprocess+env), persistence-gateway, report-service, healing.
- Gate: running agent-ctl run --explore on a real multi-page app produces a plan.json with >= 5 steps and a valid trace ZIP.

MILESTONE 2 — "Self-Repairing Walker" (Days 11-20). Goal: healing loop + locator store.
- Python brain: heal node with 3-attempt hierarchy. Strategy rotation (no LLM), LLM a11y reasoning (Sonnet). Visual set-of-marks deferred — validate need first.
- Go: persistence-gateway introduced. gRPC PersistenceService stubs (WriteLocator, ReadLocators, WriteEvent). SQLite schema v1 (runs, healed_locators, healing_events).
- Proto: first proto file. Go + Python stubs generated in CI.
- Output: healing_report.json. Updated plan.json on auto-heal.
- BUILD: heal node, persistence-gateway, proto v1. DEFER: visual fallback (attempt 3), orchestrator (still subprocess management in agent-ctl), report-service.
- Gate: run against an app where a selector was manually broken; agent heals it with confidence >= 0.85 and persists to SQLite; second run uses cached locator (zero LLM heal calls).

MILESTONE 3 — "CI-Ready Replay" (Days 21-30). Goal: deterministic CI runs.
- Python brain: replay mode (--replay flag, load plan.json, skip plan node, verify plan_hash).
- Go: orchestrator extracted from agent-ctl (proper gRPC server, RunControl service, subprocess lifecycle management). agent-ctl becomes a thin gRPC client.
- SQLite: step_failures table (flake quarantine). Golden a11y snapshots in plan.json.
- CI: GitHub Actions workflow — job 1: explore (if plan.json absent or --force-explore); job 2: replay (matrix over test targets).
- Output: exit code 2 (PARTIAL) for quarantined steps; exit code 1 for critical failures.
- BUILD: orchestrator gRPC server, replay mode, flake quarantine logic, CI workflow. DEFER: report-service, OTel, codegen export, visual fallback.
- Gate: plan.json committed to repo. CI matrix runs 3 parallel replay runs in under 2 minutes each, all exit 0.

MILESTONE 4 — "Production-Observable" (Days 31-45). v1.0 cut here.
- Go: report-service (run_report JSON+HTML, Prometheus /metrics endpoint). Switch plan node to Opus 4.8.
- Python: OTel instrumentation (one span per node, OTLP export). heal attempt 3 (set-of-marks visual, only if PoC validates accuracy).
- TypeScript: codegen_record() called in act node; codegen_stop() at run end; .spec.ts pushed to persistence-gateway.
- Output: full artifact set (report, trace, spec, baselines, healing report, cost). Grafana dashboard from Prometheus metrics.
- BUILD: report-service, OTel spans, codegen integration. BUY: Playwright codegen, Playwright HTML reporter structure, Grafana.
- Gate: single run on a 10-page app produces a complete HTML report, a valid .spec.ts, a Playwright trace, and a Prometheus cost metric, all within a configurable token budget.

DEFER TO v1.5+:
- Set-of-marks visual grounding (attempt 3) — only build if PoC (in M4) shows > 70% accuracy on real apps.
- PostgreSQL migration — trigger: > 50 concurrent runs on shared DB or distributed worker need.
- Parallel browser contexts — trigger: single-run duration > 10 min on target app.
- Web dashboard for run history — trigger: team size > 1 or stakeholder reporting need.
- LangGraph Cloud / remote checkpointing — trigger: need to run brain on separate infra from orchestrator.
- Windows support — not a home-lab concern; defer indefinitely.

**Key risks:**

- Official @playwright/mcp server API surface instability — the tool names and JSON schema of MCP tools are not contractually stable in v1.x. Mitigation: pin exact npm version in package.json; write a contract test suite that asserts tool names and input schemas match expectations; treat tool schema breakage as a build failure. This is the highest-probability v1 risk.
- LLM token cost blowout in explore mode — an unconstrained exploration of a large app (50+ pages) could exhaust Opus budget in a single run. Mitigation: hard token budget cap enforced before every LLM call in plan node; default 50k Opus tokens per run; configurable per-target in run config YAML; BUDGET_PLAN_EXCEEDED event causes graceful degradation to replay with partial plan rather than abort.
- gRPC Go-Python versioning friction — proto changes require regenerating stubs in both languages and keeping them in sync. Without enforcement this silently breaks. Mitigation: proto stubs are generated in CI from the single /proto source file; a Go test asserts proto file hash matches last generated stubs; Python CI step does the same. Proto mismatch = build failure, not a runtime surprise.
- SQLite single-writer bottleneck under parallel CI matrix — if 10 CI jobs all write to the same agent.db simultaneously, WAL mode helps reads but write serialization through persistence-gateway becomes a throughput ceiling. Mitigation: for CI, use per-job SQLite files (AGENT_DB_PATH=/tmp/agent-{run_id}.db); only the home-lab long-running service uses a shared DB. Document the PostgreSQL migration trigger explicitly: shared DB + > 50 concurrent writes/minute.
- LangGraph checkpoint growth — long exploration runs with deep episodic buffers write large checkpoint blobs to SQLite. Without pruning, agent.db grows unboundedly. Mitigation: episodic_buffer is a bounded deque (max 50 events, oldest evicted); checkpoint node prunes checkpoints older than N runs (configurable, default 10); add a gc cron to delete checkpoint rows for completed runs.
- Set-of-marks visual grounding accuracy is unverified — this is the attempt-3 fallback and is the only capability in the design that has not been validated against a real app at time of writing. Mitigation: it is deferred to v1.5 and gated behind a PoC in Milestone 4 that measures accuracy on 20 real-world broken-selector scenarios before any production dependency is taken.

**ADRs:**

- ADR-001: BUY official @playwright/mcp (Microsoft-maintained npm package) vs BUILD custom TypeScript gRPC bridge. Rejected: custom bridge. The official MCP server exposes all Playwright primitives (navigate, click, fill, accessibility_snapshot, screenshot, trace, codegen) as MCP tools, which LangGraph ToolNode understands natively with zero adaptation code. Building a custom bridge means owning the tool schema, the JSON-RPC framing, and the Playwright API mapping — all of which the official server gives for free. Trade-off accepted: dependency on an external package that may change API; mitigated by version pinning and contract tests.
- ADR-002: MCP over stdio (JSON-RPC 2.0 subprocess) vs gRPC for Python brain to TypeScript Playwright boundary. Rejected: gRPC. MCP stdio is the native LLM tool-call protocol; LangGraph ToolNode resolves MCP tool descriptions to Python callables with no adapter layer. gRPC would require: defining a proto for every Playwright operation, maintaining a TypeScript gRPC server, writing a Python gRPC client, and manually bridging each tool call into LangGraph's tool-call format. Stdio avoids port allocation and service discovery in CI. Failure mode of stdio (EOF) is simpler to detect and recover from than a gRPC connection that may linger.
- ADR-003: LangGraph explicit state machine vs custom async agent loop (asyncio FSM in Python). Rejected: custom loop. LangGraph provides: SQLite checkpointing (resume-on-crash) in one line of setup, time-travel debugging (replay any historical state), built-in interrupt/resume for human-in-loop gates, and a typed state schema enforced at node boundaries. A custom asyncio loop would need all of this built from scratch. The only cost: LangGraph is an additional dependency and its checkpoint schema is opaque. Acceptable trade.
- ADR-004: Accessibility tree (ARIA snapshot) as primary perception modality vs visual screenshots as primary. Rejected: visual-primary. A11y tree is: structured (roles, names, states, hierarchy), text-only (no vision model cost per perceive call), deterministic (same DOM = same tree), and natively queryable by Playwright. Visual perception requires a vision-capable model on every perceive call (cost multiplier ~10x per step). Visual fallback (set-of-marks) is retained as attempt-3 in healing for the < 5% of cases where the a11y tree is sparse (canvas, shadow DOM, custom elements).
- ADR-005: Explore-once-then-replay determinism strategy vs require human-written test scripts for CI. Rejected: human scripts. The autonomous exploration IS the primary differentiator over the existing qa-automation-engineer subagent that writes Playwright tests. Requiring human scripts as the CI artifact defeats the purpose. Determinism is achieved by freezing the exploration plan (plan.json, plan_hash) after the first autonomous explore run, not by eliminating autonomy. The plan file is committed to the repo and becomes the reproducible CI artifact. LLM non-determinism is isolated to the explore phase, which runs once.
- ADR-006: SQLite single-writer via Go persistence-gateway vs PostgreSQL for long-term storage. Rejected: PostgreSQL for v1. SQLite in WAL mode with a single writer process (persistence-gateway) is zero-ops, has no network dependency, and is sufficient for the expected v1 load (1-10 concurrent runs, single machine). PostgreSQL adds: a separate process to manage, connection pooling, network latency on every DB call, and operational complexity in CI. Migration trigger is explicit and documented: > 50 concurrent runs writing to a shared DB, or distributed worker nodes on separate machines. Until that trigger is hit, the complexity is not justified.
- ADR-007: Sonnet 4.6 for self-healing re-grounding (attempts 2-3) vs Opus 4.8 for all LLM calls. Rejected: Opus for healing. Healing is a bounded, structured task: given a known element intent and a current a11y tree, find the best matching selector. This does not require the deep reasoning that Opus provides for exploration planning (which must understand the app's purpose, decide what to test, and sequence actions coherently). Sonnet at ~5x lower cost per token is sufficient for the structured selector-finding task. The model split is: Opus for plan node (creative, high-stakes, runs once per explore), Sonnet for heal node (analytical, constrained, may run many times per replay).

---

## Judge verdicts

Three adversarial judges scored all four proposals on six dimensions. Note the scales differ between judges: `arch-soundness` scored each dimension 0–10 and reports **TOTAL as the sum (out of 60)**; `agentic-rigor` and `feasibility-cost` scored 0–10 per dimension and report **TOTAL on a 0–10 scale**. All numbers are reproduced exactly as recorded.

### `arch-soundness`

**Judge lens.** Architecture soundness & polyglot boundary justification. I judged whether each language earns its place or is tourism that adds ops burden; whether the cross-language contracts are clean, versioned, and single-owner; whether failure isolation across Go/Python/TS is real or asserted; and whether the hard problems (CI determinism of a non-deterministic explorer, self-healing soundness) are solved or hand-waved. I weighted "is this boundary genuinely load-bearing" heavily and penalized reinventing existing infrastructure and false single-owner claims.

| Proposal | Fit | Polyglot boundary | Self-healing | CI determinism | Feasibility | Observability/cost | TOTAL |
|---|---|---|---|---|---|---|---|
| Hexagonal Polyglot Agent | 9 | 7 | 9 | 9 | 5 | 7 | 46 |
| CognitivePilot — Perception-First (agentic-core) | 8 | 6 | 7 | 6 | 5 | 7 | 39 |
| TrustFirst — Reliability/CI-Determinism | 9 | 7 | 9 | 10 | 5 | 8 | 48 |
| CognitivePilot — Pragmatic MVP→Scale | 8 | 9 | 7 | 8 | 9 | 8 | 49 |

**Rationales (per proposal):**

- **Hexagonal Polyglot Agent** (total 46): Most formally rigorous boundary design: versioned proto package, contracts/ as repo-root source of truth, single client/single server per boundary, explicit rejected-alternatives. Self-healing is the strongest of the four — dom_hash pre-patch amortization (LLM paid once, reused until DOM drift), automatic stale-locator eviction, heal_audit replay mode, scenario-scoped hash to fight whole-page fragility. Determinism is excellent (LLM-free replay, plan_id pinning escape hatch, golden baselines, quarantine). But the polyglot boundaries are heavier than justified: (1) it BUILDS a custom 8-tool Playwright MCP server when Microsoft's official @playwright/mcp exists — bespoke maintenance surface coupled to Playwright API churn = language tourism; (2) the central 'Go is sole DB owner' claim is FALSE because the LangGraph checkpointer writes SQLite/Postgres directly from Python — two writers, two schemas, undercutting the hexagonal thesis; (3) synchronous gRPC BudgetService.ConsumeTokens before every LLM call is a cross-process round trip and failure mode for what is logically an in-process counter. 17 components / 5 gRPC services / OTel-everywhere in a homelab is gold-plated. Clean on paper, expensive in ops.
- **CognitivePilot — Perception-First (agentic-core)** (total 39): Coherent agent-loop framing and good privacy instinct (prompt_hash not content in spans, verify-before-accept via resolve_locator probe). But weakest on my lens. Three wire protocols (gRPC bidi + MCP stdio + REST). Custom 12-tool MCP server again (same tourism as #1). Same false implicit single-owner story (LangGraph checkpointer co-writes the DB). The healing confidence model leans on invented discount constants (×0.95/×0.90/×0.85) self-admitted as placeholders. Most damaging for determinism: plan-staleness AUTO-triggers a fresh explore cycle that replaces the frozen plan_hash in persistence — the CI gate silently mutates itself, directly contradicting the explore-once contract. Depends on langchain-mcp maturity (acknowledged, with fallback). Solid but the boundaries and the self-mutating plan hurt it.
- **TrustFirst — Reliability/CI-Determinism** (total 48): The determinism champion and the most intellectually honest about persistence. plan_hash integrity check that ABORTS on mismatch (exit 3); immutable golden baselines updated ONLY via explicit agentctl baseline update ('CI cannot change its own baselines' — the single best determinism idea in the set); structured exit codes 0/1/2/3; AUT-version drift detection with configurable policy; screenshot-hash alongside a11y-hash to catch visual-only regressions that a11y-blind diffing misses. Healing is excellent: append-only healing_audit (no UPDATE/DELETE), CI-safe ASYNC human gate via checkpoint pause (survives pipeline timeouts), calibrate command. Crucially honest: LangGraph checkpointer uses a SEPARATE SQLite file from store-gateway, so the single-writer contract is actually true here. Boundary cost is still high — three protocols, a custom 12-tool MCP server (tourism), UDS gRPC, and the heaviest build (14 weeks, large CLI surface). Determinism and trust are best-in-class; feasibility and the build-custom-MCP decision drag it.
- **CognitivePilot — Pragmatic MVP→Scale** (total 49): Best answer to my actual lens: it is the only proposal that refuses language tourism. It BUYS the official @playwright/mcp (the only custom TS is a thin artifact-pusher sidecar), which is the correct justification for the TS boundary — Playwright is Node-native and an official MCP server exists. It is honest that the Go control plane is not load-bearing on day one: M0–M1 use subprocess+env vars, gRPC/proto arrive only at M2–M3 when there is something to contract. It uses separate per-run SQLite files for CI parallelism and states explicit PostgreSQL migration triggers instead of pre-building infra. Every build-vs-buy and deferral is named with a trigger condition. Determinism is solid (plan_hash, golden snapshots, quarantine) but slightly softer than #3: --force-replay and optional auto-commit of a self-healed plan.json can erode the frozen-plan guarantee if misused, and there is no abort-on-hash-mismatch by default. Self-healing reuses the shared hierarchy but defers the unproven set-of-marks visual fallback behind a PoC gate (honest, but less amortization detail than #1). Hard dependency on @playwright/mcp tool-schema stability is the top risk (acknowledged, mitigated with version pin + contract tests). Lowest ops burden, most likely to ship, cleanest justified boundaries — edges out #3 on the polyglot/soundness lens.

**Best elements:**

- #3 (TrustFirst): immutable golden baselines updated ONLY via an explicit operator command — makes 'CI silently changed its own baseline' structurally impossible; pair with #1's explicit plan_id pinning for release branches.
- #3: plan_hash integrity check that hard-ABORTS replay on mismatch (exit code 3), so a hand-edited or partially-healed frozen plan can never run unnoticed.
- #3: structured CI exit-code semantics (0 pass / 1 step-fail / 2 golden-diff regression / 3 integrity-or-budget) — gives the pipeline real signal granularity.
- #3: screenshot-hash captured alongside the a11y-hash to flag visual-only (CSS/layout) regressions that pure a11y-tree diffing is blind to.
- #3: async human gate implemented as a LangGraph checkpoint pause + CLI resolution, so a low-confidence heal does not block (and time out) the CI job.
- #4 (Pragmatic): BUY the official @playwright/mcp instead of building a custom TS MCP server — eliminates the biggest maintenance-surface/tourism cost shared by the other three.
- #4: defer gRPC/proto until there is a real contract (subprocess+env first), and name every deferral with an explicit trigger condition — disciplined evolution over speculative infra.
- #4: separate per-run SQLite files for CI parallelism with a documented PostgreSQL migration trigger, instead of pre-solving scale.
- #1 (Hexagonal): healed-locator dom_hash pre-patching with automatic stale eviction — amortizes LLM healing cost across runs until the DOM actually drifts.
- #1: append-only healing-audit JSONL emitted as a CI artifact for async human review (also independently present in #3 and #4).
- #2 (Perception-First): verify-before-accept — probe a healed locator via resolve_locator against the live DOM before committing/persisting it, preventing a confidently-wrong selector from polluting the store.

**Fatal flaws:**

- #1 and #2 both assert 'Go persistence-gateway is the sole DB owner,' but the LangGraph checkpointer writes SQLite/Postgres directly from Python — two writers and two schemas against the same storage. The central hexagonal/single-owner thesis is contradicted by the chosen checkpointer. (#3 and #4 avoid this by using a separate checkpoint DB file.)
- #1, #2, #3 all BUILD a bespoke 8–12 tool Playwright MCP server when Microsoft's official @playwright/mcp exists. This is the clearest language-tourism cost: a custom Node service whose only job is to re-expose Playwright, coupling Playwright API churn to hand-written code on the most fragile boundary.
- #2: plan-staleness AUTO-triggers a fresh explore cycle that overwrites the frozen plan_hash — the CI gate mutates itself non-deterministically, directly violating the explore-once-replay-many guarantee the proposal is built on.
- #1: a synchronous gRPC BudgetService.ConsumeTokens call before every single LLM invocation turns an in-process counter into a cross-process round trip with its own failure/latency mode; budget enforcement at the control-plane boundary is over-engineered.
- Cross-cutting: the Go control plane in #1–#3 is only partially load-bearing (process supervision + a budget counter + a DB proxy that duplicates the checkpointer). Much of it could be a thin Python supervisor, so a chunk of the third-language + gRPC ops burden is unjustified — #4 implicitly concedes this by deferring the entire Go/gRPC layer.
- #3 is the heaviest to build (14 weeks, large CLI surface, three wire protocols) for a homelab/single-team target — real risk it never reaches the parts that matter.
- #4: hard runtime dependency on @playwright/mcp tool-schema stability (acknowledged), and the optional auto-commit of a self-healed plan.json can silently erode the frozen-plan determinism guarantee if enabled in CI without review.

---

### `agentic-rigor`

**Judge lens.** Self-healing & agentic rigor — skeptical of magic confidence numbers, unvalidated re-grounding, and "exploration_complete" hand-waving. I reward proposals where (a) every healing candidate is probed against the live DOM before it is trusted, (b) the confidence signal is grounded in something measurable (per-strategy priors, empirical discounts, calibration against human-verified outcomes) rather than raw LLM self-report, (c) CI determinism is enforced mechanically (hash integrity, immutable baselines) not just asserted, and (d) loops are bounded with explicit caps. I penalize magic thresholds with no calibration path, ambiguous component ownership, and dependencies on tool surfaces that may not exist.

| Proposal | Fit | Polyglot boundary | Self-healing | CI determinism | Feasibility | Observability/cost | TOTAL |
|---|---|---|---|---|---|---|---|
| Hexagonal Polyglot Agent | 8 | 9 | 6.5 | 8 | 5.5 | 7 | 7.3 |
| CognitivePilot — Perception-First | 8 | 7 | 8.5 | 7.5 | 6 | 7.5 | 7.4 |
| TrustFirst — Reliability/CI-Determinism | 9 | 8 | 8 | 9.5 | 5.5 | 7.5 | 7.9 |
| CognitivePilot — Pragmatic MVP→Scale | 7.5 | 8.5 | 6.5 | 8 | 9 | 8 | 7.8 |

**Rationales (per proposal):**

- **Hexagonal Polyglot Agent** (total 7.3): Re-grounding mechanics are sound: locator-resolver re-validates the healed candidate as LIVE before returning, and explicitly treats 'validates-then-fails-on-act' as a fresh healing cycle (one of the few proposals that closes that gap). dom_hash-anchored amortization (reuse healed locator only while dom_hash_after still matches, auto-evict stale) is the cleanest persistence story. But the confidence score is pure LLM self-report parsed from JSON, gated by magic 0.85/0.60 thresholds with NO calibration plan and no empirical discount — healing trust is assumed, not measured. That is the central rigor gap. Boundaries are the strongest of the four (versioned hexagonal ports, Go as sole DB owner, proto major-version pinning). Feasibility is the weakest: 18 components, 5 gRPC services, proto-versioning friction the authors themselves flag as ongoing tax.
- **CognitivePilot — Perception-First** (total 7.4): Most rigorous re-grounding of the four. final_confidence = max over three attempts, each with a measurable prior: per-strategy base scores (testid 1.00 → xpath 0.45), an empirical changed-DOM discount (x0.95), an LLM-overconfidence discount (x0.90), a visual discount (x0.85) — and crucially every LLM/visual proposal is re-probed via resolve_locator before acceptance (zeroes confidence if not live). It is the only proposal that names the calibration problem head-on and schedules a labeled-breakage calibration in Phase 4 plus a post-heal verification step. Weaknesses: the discount factors are admitted placeholders; auto-triggered re-explore on plan staleness can create a cost feedback loop on volatile apps; and set-of-marks ownership is muddy — the Python perception module 'adds numbered overlay logic' while the TS set-of-marks-renderer also draws marks, an ambiguous boundary that will cause integration churn.
- **TrustFirst — Reliability/CI-Determinism** (total 7.9): Strongest CI determinism by a wide margin and it is mechanical, not aspirational: plan_hash integrity check that ABORTS replay (exit 3) on mismatch, immutable frozen plans (new explore = new plan_id, no in-place edit), golden baselines updatable ONLY via explicit agentctl baseline update (making 'tests rewrote their own baseline' structurally impossible), AUT-commit-SHA-gated flake quarantine (the single best idea in the set — distinguishes genuine regression from environmental flake by requiring no SHA change), and a clean exit-code contract (0/1/2/3). Self-healing is solid: L1-L4 no-LLM fast path with wait_for_selector probe before commit, immutable append-only healing_audit, human-verified locators promoted to top L1 priority, and an agentctl healing calibrate command computing precision/recall of past auto-heals. Penalty: that calibration has a cold-start dependency on accumulated human_verified outcomes that may never materialize early on; and 14 weeks / 12 components is heavy.
- **CognitivePilot — Pragmatic MVP→Scale** (total 7.8): By far the most feasible and the most intellectually honest about uncertainty: explicit BUY-vs-BUILD gates, milestone defer triggers stated rather than hidden, and visual set-of-marks grounding DEFERRED behind a measured >70% accuracy PoC in M4. Per-job SQLite for CI parallelism is a pragmatic determinism win. Self-healing reuses the same 3-attempt hierarchy with locator-cache-hit = zero LLM cost, but confidence is LLM-self-report plus method-prior with NO calibration mechanism beyond the audit log — weaker than P2/P3. Two feasibility flaws around the healing/output path: it leans on the official @playwright/mcp exposing trace_start/stop, codegen_record/stop, and coordinate clicks as MCP tools — the authors flag 'verify Playwright MCP supports coordinate clicks,' which is exactly the risk: the official server's tool surface may not match the visual-heal and codegen-export claims, and the design's #1 risk (MCP schema instability) sits directly on the healing visual fallback. Determinism is strong but softer than P3: --force-replay can bypass the hash check by default.

**Best elements:**

- TrustFirst's AUT-commit-SHA-gated flake quarantine: a step only counts toward flaky/regression if it fails N-of-5 WITHOUT an AUT SHA change — this is the only mechanism in the set that cleanly separates real regression (exit 2) from environmental flake (quarantine, non-blocking). Graft this directly.
- TrustFirst's plan_hash integrity ABORT on replay + baselines updatable only via explicit operator command. Makes 'the agent silently rewrote its own CI baseline' structurally impossible — the core trust guarantee.
- Perception-First's measurable confidence model: per-strategy base priors (testid→xpath) combined with empirical discounts (changed-DOM, LLM-overconfidence, visual) and a mandatory re-probe of every proposed locator against the live DOM before acceptance — confidence becomes grounded, not self-reported.
- Perception-First's scheduled confidence calibration against a labeled breakage set + post-heal verification step + a healing_confidence_histogram metric: the only way the 0.85 auto-accept threshold stops being a magic number.
- Hexagonal's dom_hash-scoped amortization: persist a healed locator keyed to dom_hash_after, auto-evict and re-heal when the page's structural hash drifts — pay the LLM once, reuse safely, fail closed on drift. Combine with the proposal's own risk-mitigation of scoping the hash to the scenario's target subtree (not the whole page) to avoid whole-page invalidation from an unrelated ad banner.
- Hexagonal's versioned hexagonal ports + Go as sole DB owner: cleanest polyglot boundary, lets Python or TS be swapped with zero schema migration.
- Pragmatic's BUY-first milestone gating with explicit defer triggers, per-job SQLite for CI parallelism, and gating unverified visual grounding behind a measured PoC instead of assuming it works.
- Set-of-marks where the overlay stores a mark→DOM-element map (window.__somarks in TrustFirst, index_map in Perception-First) so the heal extracts a real semantic locator from the marked node — far sounder than Pragmatic's coordinate-based click, which is fragile to viewport/DPR.

**Fatal flaws:**

- CROSS-CUTTING: none of the four actually proves exploration CONVERGES. All four terminate on an LLM-asserted exploration_complete flag plus a depth/budget cap. That is bounding, not convergence — the LLM can declare 'done' prematurely (coverage gap) or never (saved only by the budget cap). No proposal defines a measurable coverage target (e.g., fraction of sitemap interactive elements exercised) as the real termination condition. This is the single biggest shared hand-wave.
- P1 (Hexagonal): healing confidence is raw LLM self-report against magic 0.85/0.60 thresholds with zero calibration mechanism. Auto-accepted heals (>=0.85) will silently write wrong locators into the persistent store the moment the model is overconfident, and there is no feedback loop to detect it. The amortization design then propagates that bad locator across all future runs. High-severity for a system whose whole pitch is trustworthy CI.
- P2 (Perception-First): plan staleness auto-triggers a fresh LLM explore cycle; on a weekly-deploying app this creates an uncontrolled Opus cost feedback loop, and the discount factors gating auto-accept are explicitly placeholders. Plus the set-of-marks responsibility is split ambiguously across the Python perception module and the TS set-of-marks-renderer — a boundary that will not survive contact with implementation.
- P3 (TrustFirst): the calibration story (agentctl healing calibrate computing precision/recall vs human_verified) has a cold-start dependency — early on there are no human_verified outcomes, so the auto-accept threshold runs uncalibrated exactly when the locator store is being seeded with the most consequential records. Mitigation requires bootstrapping human review volume the design does not budget for.
- P4 (Pragmatic): the visual-heal path (coordinate clicks) and the codegen/.spec.ts output both assume the official @playwright/mcp exposes tools (codegen_record/stop, coordinate click, trace as MCP tools) that may not exist in that server's actual surface — the authors themselves flag 'verify Playwright MCP supports coordinate clicks.' The design's acknowledged #1 risk (MCP schema instability) lands squarely on two load-bearing features. If those tools are absent, the visual fallback and a primary deliverable need bespoke TS after all, collapsing the BUY-first thesis for exactly the hardest parts.

---

### `feasibility-cost`

**Judge lens.** Feasibility, CI-determinism & cost. I judge buildability by a small team (real LOC and component surface, not aspirational diagrams), genuine CI reproducibility of a non-deterministic LLM explorer (is non-determinism actually quarantined, and is healing in the replay hot path bounded?), token/cost realism per run (does the design avoid LLM cost on the happy path and cap the explore phase?), and over-engineering risk in v1 (does it build what it could buy, and ship value before it ships infrastructure?). All dimensions scored higher=better; observabilityCost high = observability is appropriately scoped/cheap, not gold-plated.

| Proposal | Fit | Polyglot boundary | Self-healing | CI determinism | Feasibility | Observability/cost | TOTAL |
|---|---|---|---|---|---|---|---|
| Hexagonal Polyglot Agent — Versioned Ports Across Three Bounded Contexts | 9 | 9 | 8 | 8 | 4 | 5 | 6.5 |
| CognitivePilot — Perception-First Autonomous UI Test Agent | 8 | 7 | 8 | 7 | 5 | 5 | 6.3 |
| TrustFirst — Reliability/CI-Determinism Autonomous UI Agent | 9 | 8 | 8 | 10 | 5 | 6 | 7.6 |
| CognitivePilot — Pragmatic MVP → Scale Evolution | 8 | 8 | 7 | 8 | 9 | 9 | 8.3 |

**Rationales (per proposal):**

- **Hexagonal Polyglot Agent — Versioned Ports Across Three Bounded Contexts** (total 6.5): Architecturally the most rigorous: versioned ports, single DB owner, clean explore-once/replay-many with plan version pinning and LLM-free replay (zero token cost on happy path — genuinely strong). The dom_hash-scoped-to-subtree fix in keyRisks is the best healing-stability idea in the set. But under my lens this is the most over-engineered v1: 17 components, 5 multiplexed gRPC services, a hand-built 8-tool MCP server, two versioned contract directories, and OTel/Prometheus/transcript from day one. A small team pays the full contract-definition tax before healing is even reached (phase 5 of 7, ~9 weeks in). The proto-versioning friction it admits is real and recurring. It rebuilds the official Playwright MCP server for no benefit. Sound design, wrong cost/feasibility profile for v1.
- **CognitivePilot — Perception-First Autonomous UI Test Agent** (total 6.3): The completeness_ratio (<0.30) gate to choose perception modality is the best cost-control idea for vision tokens, and deferring the vector store is disciplined. Healing is thoughtful (3 attempts, empirical discounts). But two things bite under my lens: (1) auto plan regeneration on staleness detection multiplies Opus explore cost unpredictably and reinjects non-determinism into the pipeline that is supposed to be stable — this partially defeats the explore-once guarantee; (2) the confidence discount factors (0.90/0.85) are unvalidated placeholders dressed as principled — calibration risk is acknowledged but the auto-accept gate ships anyway. Also builds a custom 12-tool MCP server (13-week plan). Solid agentic core, mediocre cost realism.
- **TrustFirst — Reliability/CI-Determinism Autonomous UI Agent** (total 7.6): Best-in-class on the single hardest problem: making a non-deterministic explorer a trustworthy CI citizen. plan_hash integrity check with hard abort, immutable golden-baseline update via explicit command (never auto — kills the 'tests changed their own baseline' failure), AUT git-SHA drift detection, structured exit codes 0/1/2/3, sliding-window flake quarantine, gRPC deadline propagation for per-step timeout, and a 2-attempt heal cap that bounds LLM latency variance in the hot path. The L1–L4 no-LLM locator fast path and Go-side hard budget ceiling (independent of Python's counter) are excellent cost controls. Async human gate via checkpoint pause is genuinely CI-safe. Knocked on feasibility: still rebuilds a custom 12-tool MCP server and is a 14-week build. If it bought the official MCP server it would be the clear winner.
- **CognitivePilot — Pragmatic MVP → Scale Evolution** (total 8.3): Wins decisively on my lens. The one architectural insight the other three miss: BUY the official @playwright/mcp server and use LangGraph's built-in ToolNode + SqliteSaver — eliminating the single largest bespoke component (the custom MCP server) that proposals 1/2/3 all wastefully rebuild. Milestone gates with explicitly named defer triggers (PostgreSQL, visual fallback, parallel contexts) directly attack over-engineering risk. Observability and gRPC are layered on a proven wire rather than designed up front. Hard token caps with graceful degradation (not abort) are the most realistic cost story. Determinism is solid (plan_hash, golden a11y, quarantine) though slightly behind TrustFirst. Two real flaws keep it short of perfect: hard dependency on the unstable official MCP tool schema (its own #1 risk), and plan self-update + auto-commit on heal can silently mutate the deterministic artifact. Best foundation; graft TrustFirst's determinism rigor onto it.

**Best elements:**

- BUY the official Microsoft @playwright/mcp server + LangGraph ToolNode/SqliteSaver instead of hand-building an 8–12 tool MCP server (Proposal 4) — the single biggest feasibility and cost win; proposals 1/2/3 all rebuild this for no benefit
- plan_hash integrity check with hard abort on mismatch + immutable golden-baseline update only via explicit operator command, never auto (Proposal 3) — closes the 'CI mutated its own baseline' hole that undermines determinism
- L1–L4 no-LLM locator strategy-rotation fast path before any LLM heal call (Proposal 3) — most heals cost zero tokens; combined with LLM-free replay happy path (Proposal 1) this makes per-run cost near-zero on stable runs
- Structured CI exit codes (0/1/2/3), sliding-window flake quarantine, AUT git-SHA drift detection, and gRPC deadline propagation for per-step timeout (Proposal 3) — bounds LLM latency variance in the replay hot path
- a11y completeness_ratio threshold to gate whether vision/set-of-marks is invoked at all (Proposal 2) — defers expensive image tokens to only the pages that need them
- dom_hash computed over the scenario's target subtree rather than the whole page (Proposal 1 keyRisks) — prevents unrelated DOM churn from invalidating every healed locator
- Milestone gates with explicitly named defer triggers and BUILD-vs-BUY called at each gate (Proposal 4) — structural defense against v1 over-engineering
- Async human gate via LangGraph checkpoint pause with CI auto-skip timeout (Proposals 3/4) — human-in-loop without violating CI job time limits
- Go-side hard budget ceiling enforced independently of the Python token counter (Proposal 3) — trustworthy cost cap even if the brain miscounts

**Fatal flaws:**

- Proposals 1, 2, and 3 all hand-build a custom Playwright MCP server (8–12 tools) when Microsoft's official @playwright/mcp exists and is the natural LangGraph ToolNode target — large wasted build + perpetual maintenance burden against Playwright upgrades. Proposal 1 is worst overall (17 components, 5 gRPC services, 2 contract dirs) and reaches self-healing only in phase 5 of a 12-week plan, so the core differentiator is validated last.
- Proposal 2's auto plan-regeneration on staleness detection reinjects unbounded Opus explore cost and LLM non-determinism into the pipeline that is supposed to be frozen — it partially defeats its own explore-once guarantee; its confidence discount constants (0.90/0.85) are unvalidated placeholders shipped behind an auto-accept gate.
- Proposal 4 takes a hard dependency on the official @playwright/mcp tool schema, which is not contractually stable in v1.x (it flags this as its own #1 risk) — a breaking upstream change stalls the whole stack; and its plan self-update + auto-commit on heal can silently mutate the deterministic plan.json artifact, eroding the determinism guarantee unless very tightly gated.
- Common to all four: set-of-marks visual grounding accuracy is asserted, never validated against real broken-selector scenarios — only Proposal 4 gates it behind a PoC; and healing invoked in the replay hot path reintroduces LLM latency/cost variance that only Proposal 3 rigorously bounds (2-attempt cap + deadline + auto-skip). Proposals 1 and 2 leave CI run time and per-run cost exposed to heal-storm blowups on a churning AUT.

---

## Synthesis decision trail

The lead synthesizer produced: **Sentinel — Autonomous Self-Healing Playwright Testing Agent (Go spine / Python LangGraph brain / official Playwright MCP hands)**.

Full `chosenApproach` and `executiveSummary` from the synthesis are reproduced below. (Remember the ADR-001 BUY→BUILD reversal noted at the top — the text below predates that pivot.)

### Chosen approach

PRIMARY: Proposal 4 (CognitivePilot — Pragmatic MVP→Scale, lens=pragmatic-evolution). It wins the feasibility (9) and polyglot-cleanliness (9) dimensions across all three judges and is the only proposal that refuses language tourism. Its skeleton is adopted verbatim: Go CLI → spawns Python brain → brain drives the OFFICIAL @playwright/mcp subprocess via LangGraph's MCP tool integration; SQLite via a single-writer Go gateway; milestone gates with explicitly named defer triggers; per-job SQLite for CI parallelism; hard token caps with graceful degradation.

GRAFTED FROM P3 (TrustFirst, determinism champion, ciDeterminism 9–10 across judges): the entire trust layer — (1) plan_hash integrity check that HARD-ABORTS replay on mismatch (exit code 3); (2) immutable golden baselines updated ONLY via explicit `agentctl baseline update`, never auto (structurally prevents "CI rewrote its own baseline"); (3) AUT git-SHA-gated flake quarantine (a step only counts toward flaky/regression if it fails N-of-5 WITHOUT an AUT SHA change — the single cleanest way to separate real regression from environmental flake); (4) structured CI exit codes 0/1/2/3; (5) screenshot-hash captured alongside a11y-hash to catch visual-only (CSS/layout) regressions a11y-blind diffing misses; (6) async human gate as a LangGraph checkpoint pause with CI auto-skip timeout; (7) append-only healing_audit (no UPDATE/DELETE); (8) Go-side hard budget ceiling enforced independently of the Python counter.

GRAFTED FROM P2 (Perception-First, best self-healing rigor, score 8.5): the grounded confidence model — per-strategy base priors (testid 1.00 → xpath 0.45), empirical discounts (changed-DOM ×0.95, LLM-overconfidence ×0.90, visual ×0.85), MANDATORY verify-before-accept (every LLM/visual candidate is re-probed against the live DOM and its confidence zeroed if not live), scheduled calibration on a labeled-breakage set, a healing_confidence_histogram metric, and the completeness_ratio (<0.30) gate that decides whether vision tokens are spent at all.

GRAFTED FROM P1 (Hexagonal, best amortization): dom_hash-scoped healed-locator pre-patching with automatic stale eviction — pay the LLM once, reuse the healed locator until the structural hash drifts, then re-heal. Applied with P1's own keyRisk fix: hash the scenario's target SUBTREE, not the whole page, so an unrelated ad/banner does not invalidate every locator.

NEW (closes the cross-cutting hand-wave Judge 2 named): exploration convergence is a MEASURABLE coverage target, not an LLM flag.

DISCARDED: (a) hand-built custom MCP servers (P1/P2/P3) — replaced by official @playwright/mcp; (b) P2's plan-staleness auto-re-explore that overwrites the frozen plan_hash — re-explore is ALWAYS an explicit operator action producing a NEW plan_id; (c) P1/P2's false "Go sole DB owner" — the checkpointer gets its own DB file; (d) P1's synchronous gRPC ConsumeTokens before every LLM call; (e) P4's default `--force-replay` and silent auto-commit of self-healed plans — hash-abort is the default, healed-plan changes are emitted as a PR artifact for review; (f) P1's 17-component / 5-gRPC-service / OTel-from-day-1 gold-plating — deferred behind milestone triggers.

### Executive summary

Sentinel is a standalone, headless-capable agentic application that autonomously explores a web UI, decides what to test, freezes a deterministic plan, and self-heals broken locators when the DOM drifts. It honours the polyglot lock: Go is the control-plane spine (CLI, orchestrator, persistence gateway, report service), Python is the LangGraph brain (perception → plan → act → verify → heal), and TypeScript is the browser hands via the OFFICIAL Microsoft @playwright/mcp server (bought, not built). The chosen design takes Proposal 4 (Pragmatic MVP→Scale) as the structural and feasibility backbone — its single most important insight, repeated by all three judges, is BUY the official Playwright MCP server + LangGraph ToolNode/checkpointer instead of hand-building an 8–12 tool MCP server (the largest wasted bespoke component in P1/P2/P3). Onto that skeleton it grafts: TrustFirst's (P3) determinism-and-trust layer wholesale (plan_hash hard-abort on replay, immutable golden baselines updatable only by explicit operator command, AUT git-SHA-gated flake quarantine, structured exit codes 0/1/2/3, dual a11y-hash + screenshot-hash baselines, async human gate via LangGraph checkpoint pause); Perception-First's (P2) grounded confidence model (per-strategy priors, empirical discounts, and a mandatory verify-before-accept live-DOM probe of every healed locator, plus scheduled calibration against human-verified outcomes); and Hexagonal's (P1) dom_hash-scoped healed-locator amortization with auto-eviction, fixed per its own keyRisks to hash the scenario's target subtree rather than the whole page. It discards the judge-flagged fatal flaws: the self-mutating plan (P2's auto-re-explore that overwrites the frozen plan_hash), the false single-DB-owner claim (resolved by keeping the LangGraph checkpointer in a SEPARATE DB file), the synchronous per-call gRPC budget round-trip (replaced by an in-process counter with a Go-side hard-ceiling reconciliation), magic confidence thresholds shipped without a calibration path, and v1 over-engineering (gRPC/proto and the full Go layer are deferred behind milestone triggers). It also closes the one cross-cutting hand-wave all four proposals share: exploration termination is redefined from an LLM-asserted "exploration_complete" flag to a measurable coverage target (fraction of discovered interactive elements exercised + empty navigation frontier), with the budget cap as a backstop, not the primary stop condition.

