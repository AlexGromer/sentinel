# BACKLOG

> Roadmap source of truth: `docs/M9_CONTRACT.md` (§A gaps + sub-milestones) · `GAPS.md` (GAP-M9-01..12) · `ARCHITECTURE.md` §3 ADRs / §6 change log.

## Active

- [ ] [M9.3] Chat-UX: non-MCP HTTP/gRPC control-API + OSS chat front in DH (plus the MCP path via M7) + CI templates (Jenkinsfile / .gitlab-ci.yml). (P2) @web-developer — GAP-M9-03/12
- [ ] [M9.6] Browser modes: headed + CDP-attach to the user's browser (`connectOverCDP`). (P2) @desktop-developer — GAP-M9-07
- [ ] [M9.7] Pluggable adapters (auth/deploy/model/backend) — universality beyond DH. (P3) @system-architect — GAP-M9-08
- [ ] [M9.8] (branch 2) Browser extension + co-pilot takeover/return. (P3) @desktop-developer — GAP-M9-07
- [ ] [M10] Security module (separate, **authorization-gated**): XSS/CSRF/IDOR/auth-bypass/sensitive-data-in-DOM over the explore map. (P3) @appsec-engineer — GAP-M9-11
- [ ] [M11.1] Release pipeline — GitHub Releases (multi-OS/arch binaries + Docker publish + checksums + Cosign/GPG signing + SBOM) + committed dependency lockfile. Closes GAP-SEC-002 remainder. (P2) @ci-cd-engineer — ADR-030
- [ ] [M11.2] setup-WebUI — static client-side config generator MVP (vanilla, air-gapped → RunConfig YAML/env) then control-API-backed via M9.3. (P2) @web-developer — ADR-031
- [ ] [M11.3] Helm/Flux expansion — env-allowlist (agentctl) + Secret/secretKeyRef plumbing (closes GAP-SEC-001) + Flux HelmRelease/Kustomization + expanded values. (P2) @k8s-engineer — ADR-030 / GAP-SEC-001
- [ ] [M11.4] Air-gapped bundle — offline image bundle + local-model preload + vendored deps + no-network verify. (P3) @deployer — ADR-030 / GAP-SEC-002
- [ ] [M11.5] Zero-level onboarding — installer + quickstart + setup-WebUI + docs path for a non-DevOps user. (P3) @tech-writer — ADR-030
- [ ] [M11.6] Pages cost-explorer UX — beginner-friendly cost estimate **embedded on the landing** (no extra hops): tokens + $/run across models/plans + budget input ("does it fit / how many runs"); built on LOCAL_MODELS §6, editable pricing (cutoff-noted), static/air-gapped. (P2) @web-developer — issue #12
- [ ] [M9-LIVE] Live verification — GAP-VERIFY-005/006 (provider + MCP-host smoke) + live M9.1 login-as-test + M9.2 goal/describe run + GAP-RISK-009 flip (byte-stable goldens) + GAP-ARCH-003..006 live-measure. (P2) @sre-engineer — needs «go» + API key

## Completed Archive

- [x] [M9.4+M9.5] (Wave A, offline) M9.4 in-app tab perception (`[role=tab]` in interactives/setOfMarks, A5) + browser multi-page (`browser.tabs`/`browser.switchTab` + `context.on('page')`, A6); M9.5 `traceparent` injection into all browser requests (`context.route`, gated on OTLP, §I). pw-executor tsc clean; fixture l6-newtab.html; `docs/M9.4_CONTRACT.md`. Live multi-tab + backend-correlation pending «go». Stacked PR (on #3). (P2) @web-developer — GAP-M9-05/06 ✓ 2026-06-27

- [x] [M9.2b] two-phase goal (§L) + describe-first (§B) + rich RunConfig (ADR-028). Site map generalized beyond buttons (input/select/link); `brain/scenario.py` (`ground_scenario`/`reconcile` — bind to real elements only, cross-page navigate synth, conservative matcher); `GoalPlanner.build_scenario` (one-shot) + `DescribePlanner`; graph `scenario` node; `scenario.json`/`reconcile-report.json`; agentctl `--describe`/`--scenario`; declarative `auth:`/`scenarios:` RunConfig. Terminology «грауденный»→`grounding`. Offline-verified (test_m9_2b 20 + regress m3..m9_2b 95 green + go build/vet + tsc + gitleaks); 5-dim adversarial review fixes (cross-role-bind, unknown-scenario→exit3, verb-whitelist, pure-explore-invariant test). Live goal/describe run pending «go». (P2) @ml-engineer — GAP-M9-02/09 ✓ 2026-06-27
- [x] [M9.2a] `GoalPlanner` (NL→plan, explore-first grounding — grounded index-pick, never fabricates a selector) + `make_planner` `--goal` auto-default + `brain/runconfig.py` (minimal RunConfig YAML; precedence flag>file>default via agentctl `SENTINEL_EXPLICIT`/`fs.Visit`; numeric validation; mode/planner alias) + agentctl `--goal`/`--run-config`; ADR-027. Offline-verified (test_m9_2 20 + regress m3..m9_2 green + go build/vet + tsc + gitleaks); 4-dim adversarial review fixes. Live goal-run pending «go». (P1) @ml-engineer — GAP-M9-02/09 ✓ 2026-06-26
- [x] [M9.1] pw-executor `fill`/`type`/`press`/`select` + `expect`/`saveStorageState` (both transports) + storageState auth (login-as-test, `secretRef`, `PW_NO_TRACE` gate) + assert/negative layer + `brain/validation.py` (sketch); ADR-026. Offline-verified (test_m9 19 + regress m3..m9 green + tsc + go build + gitleaks); 4-dim adversarial review fixes. Live UI run pending «go». (P1) @web-developer — GAP-M9-01/04/10 ✓ 2026-06-26
- [x] M8 Distributed Observability + Budget Ceiling (ADR-021): W3C tracing across Go/Python/TS + Python BudgetTracker + Go orchestrator (RunControl + SIGTERM hard-ceiling) + Go report-service (HTTP). Compile/test-verified (Py 36 + go build/vet/test + tsc). ✓ 2026-06-26
- [x] M7 MCP-Server Exposure (ADR-020): brain/server.py FastMCP (explore/heal/replay/report) + SamplingBackend (host-supplied model). Offline-verified (test_m7). ✓ 2026-06-26
- [x] M6 Provider-Agnostic Brain (ADR-019): LLMBackend (AnthropicBackend + OpenAICompatBackend) + per-role make_backend; local models via LLM_BASE_URL. Offline-verified (test_b1). ✓ 2026-06-25
- [x] [W2] M2b-2: migrate brain<->pw-executor transport to the MCP SDK (ADR-016, GAP-VERIFY-002). pw-executor -> @modelcontextprotocol/sdk McpServer (StdioServerTransport) exposing the same 8 tools; brain -> MCP stdio client wrapped behind Executor.call so graph/healing/replay are unchanged; retain hand-rolled JSON-RPC as a documented fallback. VERIFY MCP SDK + python mcp pkg API before coding. (P2) @api-developer — 2026-06-23 ✓ 2026-06-24
- [x] [W4] M4b: full observability — Go report-service (HTML/JSON + Prometheus HTTP /metrics endpoint), OpenTelemetry spans across all 3 layers (prompt_HASH not content) -> Tempo, and the Go-side hard budget ceiling. Deferred from M4 (ADR-014); needs the Go service layer (M2b). (P3) @observability-engineer — 2026-06-23 ✓ 2026-06-23
- [x] [W5] M5-3: Postgres checkpointer opt-in. brain/__main__._run_explore: if CHECKPOINT_DSN set, use langgraph PostgresSaver.from_conn_string(dsn) (sync, near drop-in for SqliteSaver) + .setup() once, else SqliteSaver. Add langgraph-checkpoint-postgres dep. For K3s multi-runner. VERIFY package/API + needs a Postgres to test. (P3) @database-engineer — 2026-06-23 ✓ 2026-06-23
- [x] [W5] M5 Visual Heal PoC + K3s/ArgoCD: set-of-marks visual heal built into pw-executor, BUILT ONLY IF PoC measures >70% accuracy on 20 real broken-selector scenarios; Postgres + AsyncPostgresSaver IF concurrency trigger hit; Helm chart + ArgoCD Application for home-lab GitOps; per-namespace dev/staging/prod config. (P2) @k8s-engineer — 2026-06-22 ✓ 2026-06-23
- [x] [W2] M2b: extract Go store-gateway (sole SQLite writer, WAL) + gRPC + proto v1 (PersistenceService) to replace the interim brain-local store (ADR-012) and restore ADR-007 single-writer. (P1) @api-developer — 2026-06-22 ✓ 2026-06-23
- [x] [W4] M4 Production-Observable v1.0: report-service (JSON+HTML, Prometheus /metrics); OTel spans across all 3 layers to Tempo; .spec.ts export from RunState; agentctl calibrate + healing_confidence_histogram. (P2) @observability-engineer — 2026-06-22 ✓ 2026-06-23
- [x] [W3] M3 CI-Ready Replay: replay/ci mode (LLM-free happy path); plan_hash HARD-ABORT (exit 3); dual a11y+screenshot golden baselines; AUT-SHA-gated flake quarantine; structured exit codes 0/1/2/3; per-job SQLite; GitHub Actions (explore conditional + replay matrix). (P1) @ci-cd-engineer — 2026-06-22 ✓ 2026-06-22
- [x] [W2] M2 Self-Repairing Walker: heal node (cache + L1-L6 rotation + Sonnet a11y re-grounding + verify-before-accept + confidence gate + post-heal verify + append-only healing_audit + dom_subtree_hash amortization); gRPC + proto v1 + Go store-gateway. (P1) @ml-engineer — 2026-06-22 ✓ 2026-06-22
- [x] [W1] M1 Autonomous Walk: all 9 LangGraph nodes (heal stubbed); SqliteSaver checkpointer in a SEPARATE DB file; Opus 4.8 plan node (T=0); coverage-based convergence; emit plan.json + plan_hash + llm-transcript.jsonl + trace. (P1) @ml-engineer — 2026-06-22 ✓ 2026-06-22
- [x] [W0] M0 Hello Browser: agentctl (Go) spawns Python brain via subprocess+env; single perceive node; minimal pw-executor (navigate + accessibility_snapshot + trace). (P1) @general-purpose — 2026-06-22 ✓ 2026-06-22
- [x] GAP-DECISION-001 (BLOCKER): confirm BUILD-ONLY interpretation — OSS libraries allowed as "writing". (P1) @system-architect — 2026-06-22 ✓ 2026-06-22

## Deferred

_No deferred tasks._
