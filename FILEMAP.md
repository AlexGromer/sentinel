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
| ARCHITECTURE.md | Canonical architecture + ADRs | context, components, boundaries, 46 ADRs, §0 BUILD-ONLY, change log |
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
| docs/M9.4_CONTRACT.md | M9.4 + M9.5 (**Delivered offline**, Wave A) | in-app tab perception (`[role=tab]`, A5) + browser multi-page (`browser.tabs`/`switchTab`, `context.on('page')`, A6) + `traceparent` injection into browser requests (`context.route`, gated on OTLP, §I) |
| docs/M9.3_CONTRACT.md | M9.3 (**Contract**, Wave B; ADR-023/032) | non-MCP HTTP control-API (Go `cmd/control-api`: localhost-bind + bearer-token + CORS → Pages can drive a local instance) + setup-WebUI download (Pages, done) + live-mode (phase-2) + CI templates |
| docs/LOCAL_MODELS.md | Local-model methodology (ADR-029) | platform-agnostic: VRAM-sizing + token-cost-per-phase math + verified model/runtime catalog + benchmark links; authoritative formula source for the Pages calculators |
| docs/THREAT_MODEL.md | Security model (→ SECURITY.md) | STRIDE-lite over the trust boundaries; assets, current/planned mitigations, residual risk, owner-milestone |
| docs/TESTING.md | Testing + onboarding guide | offline gates + local-model setup + live run (M9.1/M9.2 interpret artifacts/exit-codes) + zero-level docker-compose path |
| docs/DISTRIBUTION.md | Distribution & onboarding epic (ADR-030/031) | Release/compose/Helm-Flux/setup-WebUI/air-gapped milestones + integration model (black-box + W3C traceparent M9.5, NO backend connector) |
| docs/index.html · docs/prices.json · docs/_config.yml · docs/calculators/*.html | GitHub Pages hub (M11.6/M11.6b, ADR-033/034) | self-contained single-page hub `index.html` (dark-neon, bilingual RU/EN, air-gapped — Pages/file:///webui): recommendation engine + cost §6 (model catalog Claude/GPT/Grok/GLM/DeepSeek/Qwen + local; blended $/1M + per-model token multiplier; fit/reasoning/vision) + VRAM §5 + model-selector §3.3 + legend; pricing = embedded seeds + `prices.json` (CI-refreshed via `.github/workflows/prices-refresh.yml` + on-page OpenRouter button); mirrors LOCAL_MODELS §3.4/§5/§6. 3 standalone calculators kept as "advanced". **M12 ph2 (ADR-041):** + **#connect/#build/#chat** sections — unified config+chat console driving control-API + the `/v1/chat/completions` shim (905→1469); shared SSE/poll; bilingual/air-gapped; `docs/setup`+`docs/chat` kept as advanced deep-links |
| docs/setup/index.html | setup-WebUI (ADR-031; M9.3 live) | static config generator (RunConfig YAML + env + command) + **download** buttons (full Pages generation) + **live mode** (control-API URL+token → /healthz → ▶Run POST /v1/runs → poll, M9.3 p2); vanilla JS, air-gapped; on Pages + Docker `webui` :8088 |
| docs/chat/index.html | chat-authoring console (M9.3-tail, ADR-040) | vanilla/air-gapped/**bilingual** front over control-API: describe→author (`POST /v1/runs`)→**SSE** stream (`/events` via fetch-reader — EventSource can't send the bearer token)→show/download `scenario.json`/report (`/artifact`); poll fallback; exit-code verdict; Docker `webui` :8088/chat/ |
| docs/ci-templates/{Jenkinsfile,.gitlab-ci.yml,README.md+.en.md} | CI templates (M9.3-tail, GAP-M9-12) | user-facing replay-gate templates: `agentctl run --replay --plan … --ci`, exit-code→verdict (0 pass / 1-2 fail / 3 unstable); Jenkins `UNSTABLE` + GitLab `allow_failure.exit_codes:[3]`; **NOT** our `.github/workflows/` |
| docs/STATE_MACHINE / SELF_HEALING / DETERMINISM / MEMORY_PERSISTENCE / OBSERVABILITY / OUTPUTS .md | mechanics deep-dives | reference |
| docs/ROADMAP.md, DESIGN_RECORD.md | delivery plan / design provenance | M0–M5 gates / 4 proposals + 3 verdicts |

## Quick Reference — source
| Path | Lang | Purpose |
|------|------|---------|
| cmd/agentctl/main.go | Go | CLI subcommands: run / baseline update / locators clear-quarantine / export-spec / report / calibrate; spawns store-gateway (`runWithStore`); exit 0/1/2/3; **M11.3** env-allowlist `filteredEnv()` **default-on** (opt-out `SENTINEL_ENV_ALLOWLIST=0`, extras via `SENTINEL_ENV_ALLOW`) |
| cmd/store-gateway/main.go | Go | M2b-1: gRPC PersistenceService over a Unix socket (agentctl-spawned) |
| cmd/orchestrator/main.go | Go | M8 run supervisor (ADR-021): gRPC RunControl + spawns brain + budget reconcile + SIGTERM hard-ceiling; grpc+stdlib only, compile-verified |
| cmd/report-service/main.go | Go | M8 HTTP report-service (ADR-021): /report/<id> HTML+JSON, /metrics (stdlib only), long-lived service mode; compile-verified |
| cmd/control-api/{main,main_test}.go | Go | **M9.3** non-MCP HTTP control-plane (ADR-032): /healthz · /v1/config-schema · POST /v1/runs (spawns agentctl) · /v1/runs/{id}; **M9.3-tail (ADR-040)** SSE `/v1/runs/{id}/events` (token-gated; `runStream` ring-buffer + fan-out, `lineWriter` capture) + `/v1/runs/{id}/artifact` (token-gated whitelist + traversal-guard); 127.0.0.1-bind + bearer-token + CORS-allowlist (Pages→local); stdlib only; **M12 (ADR-041)** OpenAI-compat `POST /v1/chat/completions` shim (`spawnRun` refactor; 1 chat turn→1 run; stream→`chat.completion.chunk`, non-stream→verdict+`scenario.json`); **M9.8-prep (ADR-043)** WS `GET /v1/stream` (recorder ingest → see `ws.go`); 13+6 httptest (race-clean) |
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
| pw-executor/src/server.ts | TS | OUR Playwright server: navigate/snapshot/click/links/currentUrl/probe/interactives/screenshotHash/setOfMarks/traceStop + **M9.1** fill/type/press/select/expect/saveStorageState; storageState load (`STORAGE_STATE`) + tracing gate (`PW_NO_TRACE`) + secret `secretRef` redaction (ADR-026); M8 per-tool spans via otel.ts; screenshot determinism (GAP-RISK-009); **OPS-002** opt-in `PW_IGNORE_HTTPS_ERRORS` + cert-error classify; **M9.4** tab perception (`[role=tab]`) + multi-page (`browser.tabs`/`switchTab`, `context.on('page')`); **M9.5** `traceparent` route-injection (§I); **M9.6** launch modes via `resolveLaunchPlan` (headless/headed `PW_HEADLESS=0`/CDP-attach `PW_CDP_ENDPOINT`→`connectOverCDP`, reuses user context, `attachedOverCDP` teardown-guard) |
| pw-executor/src/otel.ts | TS | M8 gated OTel tracer (NodeSDK + OTLP-grpc) + spanForTool (extracts W3C `_meta`); no-op without OTEL endpoint (ADR-021); **M9.5** `currentTraceparent()` (W3C for browser-request injection) |
| tests/test_*_offline.py (m3/m4/m4b/m5/b1/m7/m8/m9/m9_2/m9_2b) | Python | offline suites: trust/heal, M4 generators, OTel, visual-heal, LLM backend, MCP sampling/server, budget+W3C+interceptor, **m9** fill/type/select/assert + secret-non-leak + determinism + heal-reuse, **m9_2** GoalPlanner grounding/routing/RunConfig, **m9_2b** site-map + two-phase scenario grounding/cross-page-navigate + describe reconcile + rich RunConfig (fake executor/backend/session) |
| .github/workflows/ci.yml | CI | build (+`go vet`/`go test` + offline suite m3..m9_2b) → **security** (gitleaks/govulncheck/pip-audit/npm audit) → replay matrix → explore (manual) |
| .github/workflows/pages.yml | CI | GitHub Pages deploy (actions/deploy-pages) from docs/ on push to main |
| docker-compose.yml | Container | one-command quickstart: `sentinel` + `demo` (zero-dep fixture) + `ollama` (local model) + `litellm` (opt. model-router :4000, ADR-045) + `webui` (setup-WebUI/calculators :8088) + `control-api` (HTTP control-plane :8090) profiles |
| Dockerfile | Container | multi-stage runtime image (Go bins + TS pw-executor + Playwright + Python brain); pip deps mirror pyproject (incl. `openai`+`pyyaml`) |
| testdata/m0.html · site/*.html · site-v2/*.html | fixtures | M0 page · M1 clean · M2/M3 drifted |
| testdata/fixtures/l1..l6.html + README.md | fixtures | graded difficulty (file://): L1 trivial · L2 login · L3 validation · L4 multi-page · L5 tabs+shadow-DOM · L6 new-tab/multi-page |
| CONTRIBUTING.md · SECURITY.md · CODE_OF_CONDUCT.md · .github/{PULL_REQUEST_TEMPLATE,ISSUE_TEMPLATE/*,CODEOWNERS} | Community | repo hygiene: contribution guide (Conventional Commits, test gates, bilingual rule), security policy (+threat-model link), CoC, PR + issue templates, code owners |
| LICENSE · NOTICE | Legal | Apache-2.0 license text + NOTICE (Copyright 2026 AlexGromer) |

| docs/index.html | Web page | — |
| docs/prices.json | Configuration | — |
| scripts/refresh-prices.mjs | Project file | — |
| .github/workflows/prices-refresh.yml | Configuration | — |
| cmd/agentctl/main_test.go | Go | **M11.3** `filteredEnv()` unit tests (t.Setenv): default-on drops `AWS_SECRET_ACCESS_KEY` + passes curated/`SENTINEL_ENV_ALLOW` extras; `SENTINEL_ENV_ALLOWLIST=0` → full passthrough |
| deploy/sentinel/ (chart) | Helm | M5 chart: CronJob replay (ADR-017) + configmap/pvc/sa; **M11.3** `secrets.{enabled,llmApiKey,checkpointDsn,extraSecretEnv}` → `secretKeyRef` (plaintext fallback) + `sentinel.envAllow` helper → `SENTINEL_ENV_ALLOW`; values-prod enables secrets |
| deploy/flux/{sync,helmrelease,sentinel-secrets}.yaml | YAML | **M11.3** (ADR-035) Flux GitOps (v2 GA): `sync.yaml` Namespace+GitRepository+Kustomization (`wait`); `helmrelease.yaml` → chart; `sentinel-secrets.yaml` ExternalSecret/SealedSecret template (no secrets). ArgoCD↔Flux mutually exclusive |
| deploy/argocd/sentinel-app.yaml | YAML | M5 ArgoCD Application (ADR-017); **M11.3** comment: secrets out-of-band + Flux mutual-exclusivity |
| deploy/flux/sentinel-secrets.yaml | Configuration | — |
| pw-executor/src/launch.ts | TS | **M9.6** (ADR-037) pure `resolveLaunchPlan(env)→{kind,headless,cdpEndpoint}` — precedence CDP>headed>headless; consumed by `ensureBrowser` |
| pw-executor/src/launch.test.ts | TS | **M9.6** `node --test` for `resolveLaunchPlan` (5 cases, offline, no browser) |
| docs/M9.6_CONTRACT.md / .en.md | Docs | **M9.6** (Wave D) browser-modes contract: headed + CDP-attach env-config, Chromium-only (ADR-036), headless-only determinism (ADR-037), deferred live-verify |
| scripts/check_bilingual.py | Python | bilingual docs-parity CI gate — every primary `.md` must have a paired `.en.md` (+ SINGLE_LANGUAGE allowlist); run by the `bilingual` job in ci.yml |
| docs/M9.8_CONTRACT.md / .en.md | Docs | **M9.8** (design-first, ADR-038/039) browser-extension contract: MV3 recorder + control-API WS transport (native-messaging alt) + record→scenario reuse (M9.2b) + co-pilot takeover/return; threat-model ❾; implementation deferred (blockers GAP-M9-03/13/14/15) |
| docs/M12_CONTRACT.md / .en.md | Docs | **M12** (ADR-041) unified config+chat console + OpenAI-compat shim (variant i): ph1 shim `POST /v1/chat/completions` (DELIVERED — 1 chat turn→1 run, Open WebUI/DeepSeek/Mistral compatible) + ph2 unified `docs/index.html` (design). (i) now / (iii) AG-UI later; chat v1 one-shot |
| pw-executor/src/determinism.ts | TS | **GAP-RISK-009 / ADR-042** screenshot determinism anchors (single source of truth): `DETERMINISM_VIEWPORT` 1280×720 + `DETERMINISM_DEVICE_SCALE_FACTOR`=1 + `SCREENSHOT_DETERMINISM_OPTS` (`animations:'disabled'`/`caret:'hide'`/`scale:'css'`); consumed by `server.ts` |
| pw-executor/src/determinism.test.ts | TS | **GAP-RISK-009** `node --test` locking the determinism anchors (regression guard, offline, no browser) |
| tests/test_determinism_offline.py | Python | **GAP-RISK-009 / ADR-042** offline test for the opt-in visual-authoritative flip (`SENTINEL_VISUAL_AUTHORITATIVE`): advisory default → exit 0 vs authoritative → exit 2; FakeEx, no browser. In CI offline loop |
| cmd/control-api/ws.go | Go | **M9.8-prep / ADR-043** hand-rolled RFC6455 WebSocket `GET /v1/stream` (client→server recorder ingest, closes GAP-M9-14): `Hijacker` upgrade + `wsAccept`/frame codec; token via `Sec-WebSocket-Protocol` (`bearer.<token>`, echoes only `sentinel.recorder.v1`); NDJSON events → `runs/record-<session>/events.ndjson`; ping/pong + idle/cap; reuse `s.authed`/Origin-allowlist (ADR-032). stdlib only |
| cmd/control-api/ws_test.go | Go | **M9.8-prep** httptest for `/v1/stream` (race-clean): RFC6455 handshake/accept, token-via-subprotocol 403, bad-handshake 400, Origin reject, full 101 + masked-frame ingest |
| frontend/ | TS (Next.js) | **M9.8-prep / ADR-044** AG-UI/CopilotKit rich co-pilot scaffold (`package.json` + `app/page.tsx` CopilotChat + `app/api/copilotkit/route.ts` Runtime→`createOpenAI({baseURL})`→shim + README). **DEV-only: not air-gapped, not in CI** (in `check_bilingual.py` SKIP_DIRS; node_modules gitignored). Versions verified 2026-06-28 |
| deploy/litellm/config.yaml | YAML | **ADR-045** example LiteLLM proxy config: `model_list` routing to DeepSeek/Mistral/Anthropic/Ollama; provider keys via `os.environ/<VAR>` (no literals); mounted by the compose `litellm` profile |
| docs/ADAPTERS.md / .en.md | Docs | **ADR-045** umbrella for pluggable adapters (M9.7/GAP-M9-08): §LiteLLM optional router (behind `LLM_BASE_URL`, compose `litellm` profile) + §MCP-Inspector M7-debug recipe (stdio → `tools/list`+sampling, GAP-VERIFY-006) |
| docs/COPILOT.md / .en.md | Docs | **ADR-046** co-pilot single-source: end-goal · layers · §F evolution · honest feature-inventory (DONE/scaffold/design/not-built) · agreements (in-tool-first; vanilla=primary/AG-UI=dev) · wave-roadmap [me R1/R2/R3]/[@0xCoDSnet #42-47/#36-38] |
## Directory Structure
```
agent_development/
├── README.md ARCHITECTURE.md GAPS.md BACKLOG.md FILEMAP.md  Dockerfile docker-compose.yml
├── docs/ (+calculators/ +_config.yml +index.md)  memory/  testdata/ (+fixtures/)  tests/  .github/workflows/ (ci.yml pages.yml)
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
- Last updated: 2026-06-28
- Phase: **M0–M8 + M2b + M4b done — gates green; M9.1 (ADR-026) + M9.2a (ADR-027) + M9.2b (ADR-028) delivered offline; Foundation cycle (ADR-029/030/031) delivered: security CI gates + docker-compose quickstart + GitHub Pages + calculators + LOCAL_MODELS/THREAT_MODEL/TESTING/DISTRIBUTION docs + L1–L5 fixtures.** M6 provider-agnostic backend (ADR-019); M7 MCP-server exposure (ADR-020); M8 distributed tracing + budget ceiling + Go orchestrator/report-service (ADR-021); **M9.1 form/login/validation primitives** (pw-executor fill/type/press/select/expect/saveStorageState + storageState auth + secrets-via-`secretRef` + `PW_NO_TRACE` gate) — all compile/test-verified (Python offline suite m3..m9 + go build/vet/test + tsc). Remaining: end-to-end observe (live OTLP trace, real budget-kill, browser byte-stability → RISK-009 flip) + M6 real-provider smoke (needs API key) + **M9.1 live UI run** (forms/Keycloak login, on "go").
