# FILEMAP — agent_development (Sentinel)

<!-- Check this before Glob/Grep. Update on file create/delete/major refactor. -->

## Documentation language
Docs are **bilingual**: every `*.md` is the **Russian primary (authoritative)** version and
carries a paired `*.en.md` English copy (e.g. `README.md` ↔ `README.en.md`). Each file links to
its counterpart via a `🌐` banner on line 3. Edit the `.md` first, then mirror into `.en.md`.
(`FILEMAP.md` and `BACKLOG.md` are working files — kept single-language.)

## Quick Reference — docs
| Path | Purpose | Key contents |
|------|---------|--------------|
| README.md | Project overview + quickstart | what/why, status, architecture, build/run |
| ARCHITECTURE.md | Canonical architecture + ADRs | context, components, boundaries, 28 ADRs, §0 BUILD-ONLY, change log |
| GAPS.md | Open questions / VERIFY / risks | GAP-[CAT]-[NUM] tracking |
| BACKLOG.md | Task tracking | M0–M8 done; Active = M9.1..M9.8 + M10 |
| docs/DEVELOPMENT.md | Contributor guide | setup, build/run, milestone gates, extension recipes |
| docs/M0..M5_CONTRACT.md, M2b/M6/M7/M8_CONTRACT.md | Frozen milestone contracts | per-milestone scope/wire/gate |
| docs/M7_CONTRACT.md | M7 (Delivered, ADR-020) | MCP-server exposure + SamplingBackend |
| docs/M8_CONTRACT.md | M8 (done, ADR-021) | distributed tracing + budget ceiling + Go orchestrator/report-service |
| docs/M9_CONTRACT.md | M9 (**Proposed** design freeze, ADR-022..025) | conversational & goal-directed testing: fill/type+auth, GoalPlanner/NL, chat-UI (MCP+non-MCP), tabs, backend correlation, browser modes, pluggable adapters |
| docs/M9.1_CONTRACT.md | M9.1 (**Delivered offline**, ADR-026) | form/login/validation primitives: pw-executor fill/type/press/select/expect/saveStorageState (both transports), storageState auth + login-as-test, secrets via `secretRef` + `PW_NO_TRACE` tracing gate, assert/negative semantics, new step kinds |
| docs/M9.2_CONTRACT.md | M9.2a (**Delivered offline**, ADR-027) | GoalPlanner (NL→plan, explore-first grounding): goal-directed grounded planner in the Planner seam (index-only, never fabricates), `--goal` auto-default + `make_planner`, minimal RunConfig YAML; describe-first/two-phase/auth deferred to M9.2b |
| docs/M9.2b_CONTRACT.md | M9.2b (**Delivered offline**, ADR-028) | two-phase goal (§L) + describe-first (§B) + rich RunConfig: full heuristic explore→site map (generalized to input/select/link)→one-shot grounded scenario (`build_scenario`/`reconcile`, cross-page navigate synth); plan.json+scenario.json+reconcile-report.json; declarative auth/scenarios + `--scenario`/`--describe` |
| docs/STATE_MACHINE / SELF_HEALING / DETERMINISM / MEMORY_PERSISTENCE / OBSERVABILITY / OUTPUTS .md | mechanics deep-dives | reference |
| docs/ROADMAP.md, DESIGN_RECORD.md | delivery plan / design provenance | M0–M5 gates / 4 proposals + 3 verdicts |

## Quick Reference — source
| Path | Lang | Purpose |
|------|------|---------|
| cmd/agentctl/main.go | Go | CLI subcommands: run / baseline update / locators clear-quarantine / export-spec / report / calibrate; spawns store-gateway (`runWithStore`); exit 0/1/2/3 |
| cmd/store-gateway/main.go | Go | M2b-1: gRPC PersistenceService over a Unix socket (agentctl-spawned) |
| cmd/orchestrator/main.go | Go | M8 run supervisor (ADR-021): gRPC RunControl + spawns brain + budget reconcile + SIGTERM hard-ceiling; grpc+stdlib only, compile-verified |
| cmd/report-service/main.go | Go | M8 HTTP report-service (ADR-021): /report/<id> HTML+JSON, /metrics (stdlib only), long-lived service mode; compile-verified |
| internal/orchestrator/pb/ | Go | generated gRPC stubs (from proto/runcontrol.proto) |
| internal/store/server.go | Go | SQLite-backed PersistenceService (sole writer, ADR-007/015); WAL checkpoint on close |
| internal/store/server_test.go | Go | gateway unit tests (golden/locator/quarantine round-trips) |
| internal/store/pb/ | Go | generated gRPC stubs (from proto/persistence.proto) |
| proto/persistence.proto | proto3 | PersistenceService contract (mirrors store.py 1:1) |
| proto/runcontrol.proto | proto3 | M8 RunControl contract (StartRun/ReportEvent→Control/Abort); brain↔orchestrator token-reconcile (ADR-021) |
| go.mod, go.sum | Go | module + deps (grpc, protobuf, modernc.org/sqlite, opentelemetry-go + otelgrpc) |
| brain/__main__.py | Python | entrypoint; dispatch explore/replay/baseline/clear-quarantine/export-spec/report/calibrate; `make_store` |
| brain/graph.py | Python | LangGraph StateGraph; explore captures L1–L6 alternatives; **M9.2b** `_elements_from_interactives` (button+input/select/link) + `site_map` accumulation + `scenario` node (one-shot phase-2 head, ADR-028) |
| brain/planner.py | Python | HeuristicPlanner (default) + LLMPlanner (provider-agnostic, ADR-011/019) + **M9.2a** GoalPlanner (goal-directed, grounded index-pick, ADR-027) + `make_planner(env)` factory (`--goal` auto-default); **M9.2b** `GoalPlanner.build_scenario` (one-shot) + `DescribePlanner.draft` (ADR-028) |
| brain/scenario.py | Python | **M9.2b** (ADR-028) authoring substrate: `flatten_site_map` + `ground_scenario`(LLM refs→steps) + `reconcile`(draft→steps); binds to real site-map elements, synthesizes cross-page navigates, shapes to the replay step schema; pure/offline |
| brain/runconfig.py | Python | **M9.2a** minimal RunConfig YAML (ADR-027) + **M9.2b** rich (ADR-028): `load_run_config` + `apply_run_config` (mode/goal/planner/budgets + declarative `auth:`/`scenarios:` + `--scenario` selector; precedence flag>file>default); pyyaml |
| brain/llm.py | Python | LLMBackend: AnthropicBackend + OpenAICompatBackend + SamplingBackend + make_backend(role); provider-agnostic planner+heal (ADR-019, M6) + MCP sampling (ADR-020, M7) |
| brain/server.py | Python | M7 brain MCP server (FastMCP): tools explore/heal/replay/report; SamplingBackend via host sampling; sync graph in worker-thread (ADR-020) |
| brain/budget.py | Python | M8 BudgetTracker — per-role token accumulator + `exceeded()` guard; graceful degradation planner→heuristic / heal→L1–L6 (ADR-021) |
| brain/runcontrol.py | Python | M8 RunControl client — reports token deltas to the Go orchestrator + honours abort; no-op when ORCH_ADDR unset (ADR-021) |
| brain/healing.py | Python | HealingEngine (cache→L1–L6→verify→gate→audit) — store-agnostic |
| brain/replay.py | Python | replay + M3 trust layer (plan_hash, golden-diff, quarantine, exit codes) — store-agnostic |
| brain/store.py | Python | LocalStore (SQLite, tests/fallback) + GrpcStore (gRPC client, prod) + `make_store` (ADR-015) |
| brain/exporter.py / report.py / calibrate.py | Python | M4 generators (.spec.ts / HTML+JSON+Prom / heal histogram) |
| brain/state.py, brain/executor.py | Python | RunState + hashing helpers; pw-executor JSON-RPC client |
| brain/validation.py | Python | **M9.1** negative-input generator (sketch, ADR-026): `invalid_inputs_for(field)` by type + `fill`+`assert` step-pair helper; pure, no I/O (full engine M9.2) |
| brain/pb/ | Python | generated gRPC stubs (PersistenceService + RunControl) |
| brain/pyproject.toml | Python | deps: langgraph, langgraph-checkpoint-sqlite, anthropic, openai, grpcio, grpcio-tools, pyyaml (M9.2a RunConfig) |
| pw-executor/src/server.ts | TS | OUR Playwright server: navigate/snapshot/click/links/currentUrl/probe/interactives/screenshotHash/setOfMarks/traceStop + **M9.1** fill/type/press/select/expect/saveStorageState; storageState load (`STORAGE_STATE`) + tracing gate (`PW_NO_TRACE`) + secret `secretRef` redaction (ADR-026); M8 per-tool spans via otel.ts; screenshot determinism (GAP-RISK-009) |
| pw-executor/src/otel.ts | TS | M8 gated OTel tracer (NodeSDK + OTLP-grpc) + spanForTool (extracts W3C `_meta`); no-op without OTEL endpoint (ADR-021) |
| tests/test_*_offline.py (m3/m4/m4b/m5/b1/m7/m8/m9/m9_2/m9_2b) | Python | offline suites: trust/heal, M4 generators, OTel, visual-heal, LLM backend, MCP sampling/server, budget+W3C+interceptor, **m9** fill/type/select/assert + secret-non-leak + determinism + heal-reuse, **m9_2** GoalPlanner grounding/routing/RunConfig, **m9_2b** site-map + two-phase scenario grounding/cross-page-navigate + describe reconcile + rich RunConfig (fake executor/backend/session) |
| .github/workflows/ci.yml | CI | build → replay matrix |
| testdata/m0.html · site/*.html · site-v2/*.html | fixtures | M0 page · M1 clean · M2/M3 drifted |
| CONTRIBUTING.md · SECURITY.md · CODE_OF_CONDUCT.md · .github/{PULL_REQUEST_TEMPLATE,ISSUE_TEMPLATE/*,CODEOWNERS} | Community | repo hygiene: contribution guide (Conventional Commits, test gates, bilingual rule), security policy, CoC, PR + issue templates, code owners |
| LICENSE · NOTICE | Legal | Apache-2.0 license text + NOTICE (Copyright 2026 AlexGromer) |

## Directory Structure
```
agent_development/
├── README.md ARCHITECTURE.md GAPS.md BACKLOG.md FILEMAP.md
├── docs/        memory/        testdata/       tests/        .github/workflows/
├── cmd/agentctl/   cmd/store-gateway/        # Go binaries
├── internal/store/  internal/store/pb/       # Go store-gateway + gRPC stubs
├── proto/                                    # protobuf3 contract
├── brain/  brain/pb/                         # Python brain + gRPC stubs
├── pw-executor/                              # TS Playwright server (node_modules/ dist/ ignored)
└── runs/ state/ bin/ .venv/ .claude/         # all git-ignored
```

## Module Dependency Map
```
agentctl ──spawn──▶ store-gateway (Go, gRPC/UDS) ◀──gRPC── brain.store.GrpcStore   [M2b-1]
agentctl ──spawn+env──▶ brain (.venv) ──JSON-RPC/stdio──▶ pw-executor ──▶ Chromium
explore:  brain.graph (LangGraph) → SqliteSaver → runs/<id>/checkpoint.db
replay:   brain.replay (trust) → brain.healing → store (GrpcStore | LocalStore fallback)
M4:       brain.exporter / report / calibrate (pure generators)
[M2b-2] brain↔pw-executor: dual transport — JSON-RPC default + MCP SDK opt-in (env MCP_TRANSPORT), ADR-016 [done]
[M6]    planner + heal LLM calls → brain.llm.LLMBackend (Anthropic | OpenAI-compat), provider-agnostic, ADR-019
```

## Build / run
- gateway-aware: `go build -o bin/agentctl ./cmd/agentctl && go build -o bin/store-gateway ./cmd/store-gateway` (if /tmp full: `go env -w GOTMPDIR=/opt/go/tmp`)
- TS: `cd pw-executor && npm install && npm run build` (`npx playwright install chromium-headless-shell`)
- Py: `uv venv && uv pip install langgraph langgraph-checkpoint-sqlite anthropic openai grpcio grpcio-tools`
- gRPC stubs (regen): `.venv/bin/python -m grpc_tools.protoc -I proto --python_out=brain/pb --grpc_python_out=brain/pb proto/persistence.proto proto/runcontrol.proto` — then patch the `_pb2_grpc.py` top-level import to `from . import` (package-relative); (+ go plugins for internal/store/pb, internal/orchestrator/pb)
- tests: `go test ./internal/store/ && for t in m3 m4 m4b m5 b1 m7 m8 m9 m9_2 m9_2b; do .venv/bin/python tests/test_${t}_offline.py; done`
- full contributor guide: docs/DEVELOPMENT.md

## Metadata
- Last updated: 2026-06-26
- Phase: **M0–M8 + M2b + M4b done — gates green; M9.1 (ADR-026) + M9.2a (ADR-027) + M9.2b (ADR-028) delivered offline.** M6 provider-agnostic backend (ADR-019); M7 MCP-server exposure (ADR-020); M8 distributed tracing + budget ceiling + Go orchestrator/report-service (ADR-021); **M9.1 form/login/validation primitives** (pw-executor fill/type/press/select/expect/saveStorageState + storageState auth + secrets-via-`secretRef` + `PW_NO_TRACE` gate) — all compile/test-verified (Python offline suite m3..m9 + go build/vet/test + tsc). Remaining: end-to-end observe (live OTLP trace, real budget-kill, browser byte-stability → RISK-009 flip) + M6 real-provider smoke (needs API key) + **M9.1 live UI run** (forms/Keycloak login, on "go").
