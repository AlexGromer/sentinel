# Development Guide — Sentinel

> 🌐 [Русский](DEVELOPMENT.md) (основная версия) · **English**

Handoff-grade guide so any developer can build, run, and **extend** Sentinel.
Read this with [`../ARCHITECTURE.md`](../ARCHITECTURE.md) (the canonical design + ADRs) and the
milestone contracts in this folder (`M0_CONTRACT.md`, `M1_CONTRACT.md`, …).

## 0. Working principles (non-negotiable)
1. **Docs-first.** Freeze a spec/contract (`docs/M*_CONTRACT.md`) before writing code for a milestone.
2. **Everything documented.** Every module and public function has a docstring; wire formats live in a contract doc.
3. **Build, don't buy** (ADR-001). Use OSS *libraries* (Playwright, LangGraph, Anthropic SDK); never adopt a turnkey product/server — we write the components.
4. **Determinism & trust** (ADR-006/010). Explore-once → replay-many; convergence is a measurable coverage target, not an LLM "done" flag.

## 1. Prerequisites
| Tool | Version used | Notes |
|------|--------------|-------|
| Go | 1.26.x | control-plane |
| Node | 24.x + npm 11.x | pw-executor |
| Python | 3.12+ | brain (LangGraph) |
| uv | 0.10.x | Python env/dep manager |
| Playwright browser | chromium-headless-shell (matches pinned playwright) | one-time download |

## 2. Per-component build
```bash
# TypeScript — pw-executor (our Playwright server)
cd pw-executor
npm install
npm run build                              # tsc → dist/server.js
npx playwright install chromium-headless-shell   # one-time; matches pinned playwright version
cd ..

# Go — control-plane (if /tmp is full: `go env -w GOTMPDIR=/opt/go/tmp` first — Go build scratch)
go build -o bin/agentctl ./cmd/agentctl
go build -o bin/store-gateway ./cmd/store-gateway   # M2b-1: gRPC persistence; agentctl auto-spawns it

# Python — brain (LangGraph)
uv venv                                    # creates .venv
uv pip install langgraph langgraph-checkpoint-sqlite anthropic openai
#   openai is optional — only needed for OpenAI-compatible providers (M6); import-guarded
```
`agentctl` auto-uses `./.venv/bin/python` to run the brain (override with `BRAIN_PYTHON`).

## 3. Run
```bash
# M0 — single perceive, prints a11y tree + trace.zip
./bin/agentctl run --target "file://$PWD/testdata/m0.html"

# M1 — autonomous walk over a multi-page fixture → plan.json
./bin/agentctl run --target "file://$PWD/testdata/site/index.html" --planner heuristic
#   flags: --planner heuristic|llm   --coverage-target 0.85   --max-steps 40

# M2 — replay a frozen plan against a drifted DOM, self-healing broken locators
./bin/agentctl run --replay --plan runs/<id>/plan.json --target "file://$PWD/testdata/site-v2/index.html"
#   flags: --heal-llm   (Sonnet fallback when L1-L6 miss; needs ANTHROPIC_API_KEY)
```
Artifacts land in `runs/<run_id>/` (`plan.json`, `llm-transcript.jsonl`, `trace.zip`, `checkpoint.db`; replay adds `heal-report.json`) — `runs/` git-ignored. Healed locators + audit persist in `state/locators.db` (interim local store, M2 → store-gateway at M2b; git-ignored).

> **Permission note (this environment):** running freshly-built binaries and outbound network are gated.
> Build steps (`npm`, `go build`, `uv pip`) run fine; execute `agentctl` yourself (e.g. via the `!` prefix)
> and prefer local `file://` fixtures over external targets.

### LLM backend (M6, provider-agnostic)
By default (zero env) it is Anthropic, as before: `claude-opus-4-8` (planner) / `claude-sonnet-4-6`
(heal), keyed off `ANTHROPIC_API_KEY`; without a key the planner falls back to the heuristic and heal
to L1–L6. To run the **planner** and/or **heal** on any OpenAI-compatible endpoint (ChatGPT, DeepSeek,
Qwen, Gemini-compat, OpenRouter, Ollama, vLLM), set env **per role**. Precedence:
`LLM_<KEY>_<ROLE>` > `LLM_<KEY>` > default (roles: `PLANNER`, `HEAL`).

| Key | Global | Per-role override | Default (as before M6) |
|-----|--------|-------------------|------------------------|
| `LLM_BACKEND` | yes | `LLM_BACKEND_PLANNER` / `_HEAL` | `anthropic` |
| `LLM_MODEL` | yes | `LLM_MODEL_PLANNER` / `_HEAL` | `claude-opus-4-8` / `claude-sonnet-4-6` |
| `LLM_BASE_URL` | yes | `LLM_BASE_URL_PLANNER` / `_HEAL` | — |
| `LLM_API_KEY` | yes | `LLM_API_KEY_PLANNER` / `_HEAL` | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` |
| `LLM_VISION` | yes | `LLM_VISION_HEAL` | provider default (anthropic=on) |

```bash
# planner on OpenRouter/DeepSeek, heal stays on the Anthropic default
LLM_BACKEND_PLANNER=openai LLM_BASE_URL_PLANNER=https://openrouter.ai/api/v1 \
  LLM_API_KEY_PLANNER=… LLM_MODEL_PLANNER=deepseek/deepseek-chat \
  ./bin/agentctl run --target "file://$PWD/testdata/site/index.html" --planner llm

# vision-heal on a different provider (set-of-marks Tier-7 needs a vision model)
LLM_BACKEND_HEAL=openai LLM_BASE_URL_HEAL=… LLM_MODEL_HEAL=… LLM_VISION_HEAL=1 \
  ./bin/agentctl run --replay --plan runs/<id>/plan.json --target "file://…" --heal-llm
```
`make_backend(role)` (`brain/llm.py`) builds the backend from env or returns `None` ⇒ the offline
fallback (heuristic / L1–L6) is kept; it **never raises**. A text-only provider skips Tier-7 (no
vision) → deterministic L1–L6. Source of truth: `brain/llm.py` + `docs/M6_CONTRACT.md`.

## 4. Milestone gates (acceptance)
- **M0** (`M0_CONTRACT.md`): a11y tree printed + `runs/<id>/trace.zip` size>0 + exit 0.
- **M1** (`M1_CONTRACT.md`): `plan.json` with **≥5 steps**, `coverage_achieved` recorded, `plan_hash` present, `trace.zip` present; a second identical run yields the **same `plan_hash`** (determinism — heuristic planner).

```bash
# determinism check (M1)
A=$(./bin/agentctl run --target "file://$PWD/testdata/site/index.html" >/dev/null; jq -r .plan_hash runs/*/plan.json | tail -1)
# run again, compare plan_hash — must match
```

- **M2** (`M2_CONTRACT.md`): a broken replay locator heals with confidence **≥ 0.85**, a `HealedLocator` `status=active` row is persisted, exit 0; a second run consumes **zero LLM tokens** for that semantic_id (cache hit).
- **M2b** (`M2b_CONTRACT.md`): with `store-gateway` (Go gRPC over UDS) explore/baseline/replay/calibrate are identical; `grep -r sqlite3 brain/` is empty; pw-executor `tools/list` returns **7 tools**; the M0–M3 live gates pass over the MCP transport (live is user-run).
- **M3** (`M3_CONTRACT.md`): 3 parallel `--ci` replays **< 2 min** each, exit 0; a replay against a tampered `plan_hash` **exits 3** in < 5 s (both hashes to stderr).
- **M4** (`M4_CONTRACT.md`): `run_report.html` is non-empty and renders; the exported `.spec.ts` passes `tsc --noEmit`; `trace.zip` opens; `agent_cost_usd_total` **> 0** in `/metrics`.
- **M4b** (`M4b_CONTRACT.md`): without `OTEL_EXPORTER_OTLP_ENDPOINT` / `PROM_PUSHGATEWAY` — spans are no-ops, no push, offline tests green; with endpoint+gateway (user-run) traces land in Tempo, metrics in the Pushgateway.
- **M5** (`M5_CONTRACT.md`): set-of-marks visual heal — **≥ 15/20** scenarios match the human-verified locator (≥ 75% > 70% gate), otherwise the feature is deferred and the overlay is removed from the binary.
- **M6** (`M6_CONTRACT.md`): offline `test_b1_offline` (8) + `test_m5_offline` (4) green, the `test_m3` / `test_m4` / `test_m4b` regression green, **default path byte-for-byte**; the real-provider smoke is user-run.
- **M7** (`M7_CONTRACT.md`): brain MCP server — `tools/list` returns `explore`/`heal`/`replay`/`report`; offline `test_m7` (5) green + `SamplingBackend` via a fake sampling session; a live MCP host is user-run.
- **M8** (`M8_CONTRACT.md`): W3C trace brain→pw-executor→store-gateway (gated, no-op without OTLP) + `BudgetTracker` flips `exceeded()` at the limit with degradation; offline `test_m8` (9) green + `go build`/`vet`/`test` + `tsc`; live OTLP / the real budget-kill are user-run.
- **M9.1** (`M9.1_CONTRACT.md`): pw-executor `fill`/`type`/`press`/`select`/`expect`/`saveStorageState` (`tsc --noEmit` clean); offline `test_m9` (19) green — the secret never leaks into artifacts, `plan_hash` is stable, assert exit composition; gitleaks clean; the live UI run (forms/login) is on "go".

```bash
# offline suite (no network/binaries): the full M3..M9 regression
for t in m3 m4 m4b m5 b1 m7 m8 m9; do .venv/bin/python tests/test_${t}_offline.py; done
```

## 5. Wire contracts (where the boundaries are defined)
| Boundary | Doc |
|----------|-----|
| agentctl ↔ brain (subprocess + env) | `M0_CONTRACT.md` §Boundary A |
| brain ↔ pw-executor (JSON-RPC 2.0 / stdio) | `M0_CONTRACT.md` §Boundary B + `M1_CONTRACT.md` (new tools) |
| LangGraph nodes / RunState | `STATE_MACHINE.md`, `M1_CONTRACT.md` |
| (M2) Go ↔ Python gRPC, MCP-SDK transport | `ARCHITECTURE.md` §2, GAP-VERIFY-002 |

## 6. Extension recipes
### Add a pw-executor browser tool (TypeScript)
1. Add a `case 'browser.<x>':` in `pw-executor/src/server.ts` `handle()` (call `await ensureBrowser()` first; return a JSON-safe object; **logs to stderr only**).
2. Add the method name to the `initialize` `capabilities` array.
3. Document it in the relevant `M*_CONTRACT.md` tool table.
4. `npm run build`; call it from the brain via `ex.call("browser.<x>", ...)`.

### Add a planner (Python)
1. Implement the `Planner` protocol in `brain/planner.py`: `propose(state, candidates) -> {action, done, reason, tokens}` with a `name` and `model` attribute.
2. Wire selection in `brain/__main__.py` (the `--planner` switch / `PLANNER` env).
3. Keep heuristic deterministic; LLM planners must fall back to heuristic on error/no-key (graceful degradation, ADR-011) and log token usage to the transcript.

### Add / change a LangGraph node (Python)
1. Declare any new state field as a channel in `RunState` (`brain/state.py`) — **undeclared keys are dropped between nodes**.
2. Add the node function and register it in `brain/graph.py` `build_graph()` (`add_node`), then wire edges (`add_edge` / `add_conditional_edges`).
3. Mind cycles: raise the `recursion_limit` in the `invoke` config if you add supersteps per loop.
4. Update `STATE_MACHINE.md` and the milestone contract.

### Add a heal strategy (Python)
1. Add the strategy key + its prior to `PRIORS` in `brain/healing.py`.
2. Emit a matching `alternatives` entry at explore time in `brain/graph.py` `_buttons_from_interactives`, and ensure `pw-executor` `buildLocator` can build+probe that locator kind.
3. `HealingEngine.heal` rotates alternatives in recorded order; verify-before-accept re-probes every candidate live. Document in `docs/SELF_HEALING.md` + `docs/M2_CONTRACT.md`.

### Start a new milestone
Write `docs/M<N>_CONTRACT.md` first (scope, contracts, acceptance gate Given/When/Then), add an ADR to `ARCHITECTURE.md` if it’s an architectural decision, add tasks to `BACKLOG.md`, *then* implement.

## 7. Coding standards
- Docstrings on every module + public function; comments explain *why*, not *what*.
- Conventional commits (`feat(m1): …`); end messages with the `Co-Authored-By` trailer.
- `gitleaks detect` before commit; never commit `.claude/`, secrets, `runs/`, `node_modules/`, `dist/`, `bin/`.
- Track unknowns in `GAPS.md` (`GAP-[CAT]-[NUM]`); tasks in `BACKLOG.md` via the backlog MCP.
