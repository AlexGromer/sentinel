# Sentinel — Testing Guide

> 🌐 [Русский](TESTING.md) · **English**

Handoff-grade guide: any developer should be able to go from a clean clone to a green CI run
without any verbal explanation. Read alongside [`DEVELOPMENT.md`](DEVELOPMENT.md) (component
builds, prerequisites, extension recipes).

---

## 1. Offline gates (no network, no browser, no LLM key)

All commands in this section work on a clean machine and in CI without tokens or network access.
Run them from the repository root (`/opt/agent_development`).

### 1.1 Go: vet + unit tests

```bash
go vet ./...
go test ./...
```

`go test ./...` — unit tests for ALL Go components (agentctl, control-api, orchestrator,
store-gateway, internal/*; `report-service` was removed 2026-08-09, ADR-119). Expected result:
every package prints `ok` or `--- PASS`; a non-zero exit code is a blocker.

### 1.2 Python: offline suite (full regression M3–M9)

Run all offline tests with a single command:

```bash
for f in tests/test_*_offline.py; do
    .venv/bin/python "$f"
done
```

Or individually — for fast isolation:

```bash
.venv/bin/python tests/test_m3_offline.py   # parallel replay, exit codes, determinism
.venv/bin/python tests/test_m4_offline.py   # run_report, .spec.ts, Prometheus metrics
.venv/bin/python tests/test_m4b_offline.py  # OTel no-op without ENDPOINT, Pushgateway-guard
.venv/bin/python tests/test_m5_offline.py   # set-of-marks overlay, visual-heal threshold
.venv/bin/python tests/test_b1_offline.py   # provider-neutral llm.py: AnthropicBackend/OpenAI-compat/offline-fallback
.venv/bin/python tests/test_m7_offline.py   # MCP-server brain, SamplingBackend
.venv/bin/python tests/test_m8_offline.py   # W3C trace brain→pw-executor→store-gateway, BudgetTracker
.venv/bin/python tests/test_m9_offline.py   # secretRef does not leak, exit codes, plan_hash is stable
.venv/bin/python tests/test_m9_2_offline.py # GoalPlanner grounding, OOB→done, RunConfig precedence
.venv/bin/python tests/test_m9_2b_offline.py # two-phase goal/describe, reconcile, rich RunConfig
```

The list above is a representative sample (one example per M-stage), not an exhaustive list.
The full list of offline suites is not maintained by hand (principle 5, `docs/DEVELOPMENT.en.md`
§0) — the `for` loop in §1.2 discovers them by glob `tests/test_*_offline.py` and runs them with
the same pattern (`.venv/bin/python tests/test_<module>_offline.py`). The count is deliberately
NOT written down here — it goes stale faster than it gets corrected; ask the tree instead with
`ls tests/test_*_offline.py | wc -l`. CI requires at least 25 discovered (ADR-087,
`.github/workflows/ci.yml`).

Each file prints `PASS <test_name>` for every test and `ALL PASS (N)` at the end.
A non-zero exit code or the string `FAIL` in stdout is a blocker.

> **Prerequisites:** `uv venv && uv pip install langgraph langgraph-checkpoint-sqlite anthropic openai`
> (or `uv sync` if `pyproject.toml` is complete). See `DEVELOPMENT.md §1–2` for details.

### 1.3 TypeScript: build + unit tests for pw-executor

```bash
cd pw-executor
npm ci             # on a fresh clone; otherwise skippable
npm test           # tsc + node:test — exactly what CI runs
npx tsc --noEmit   # types only, for a fast pass without a build
cd ..
```

The full gate is `npm test` (`pw-executor/package.json`: `tsc && node --test dist/*.test.js`): one
`tsc` pass compiles the package, then `node --test` runs the compiled `dist/*.test.js`; the
`postbuild`/`posttest` hooks strip them again, so no consumer of `pw-executor/dist` ships test code.
`npm test` is what CI runs — the "Build + unit-test pw-executor" step of the `build` job, which first
checks a floor on the number of `src/*.test.ts` it finds (`.github/workflows/ci.yml`, QA-PWEXEC-CI).
The suite count is not written here — ask the tree: `ls pw-executor/src/*.test.ts | wc -l`.
`npx tsc --noEmit` is a fast type check, NOT the full gate: it executes nothing. The acceptance row
for this gate lives in `docs/PR_ACCEPTANCE.en.md` §1.

### 1.4 Secrets scan (gitleaks)

```bash
gitleaks detect --source . --verbose
```

Run before every commit. A non-zero exit code = STOP, do not commit.
Allow-list configuration: `.gitignore` — `runs/`, `state/`, `.env*`, `bin/`, `dist/`.

### 1.5 SCA — dependency vulnerability analysis

Three scanners, one per stack:

```bash
# Go — official scanner (uses OSV data)
govulncheck ./...

# Python — pip-audit against OSV and PyPI Advisory Database
pip-audit

# Node (pw-executor)
cd pw-executor
npm audit
cd ..
```

`govulncheck` is installed once: `go install golang.org/x/vuln/cmd/govulncheck@latest` (locally;
in CI the version is pinned — `@v1.1.4` — for scanner reproducibility).
`pip-audit` is installed in the venv: `uv pip install pip-audit` or `pip install pip-audit`.

In CI (#8): only **deterministic** gates run on push/PR — `gitleaks` (job `security`,
HARD) + `pip-audit` (advisory). Scanners against **live advisory DBs** (`govulncheck` + `npm audit`)
are moved to a separate scheduled workflow `.github/workflows/security-nightly.yml` (daily +
`workflow_dispatch`): a red nightly run can reflect a fresh upstream CVE rather than a PR regression.
A non-zero exit code from any scanner requires review (CRITICAL/HIGH — blocker; MODERATE/LOW — context-dependent).

---

## 2. Local-model setup (serving-agnostic)

Sentinel supports **any OpenAI-compatible endpoint** on equal footing with Anthropic (ADR-019).
The model is selected **exclusively via env** — no code changes needed.

### 2.1 ENV profile: variable schema

| Variable | Global | Per-role override | Default (no env) |
|---|---|---|---|
| `LLM_BACKEND` | yes | `LLM_BACKEND_PLANNER` / `LLM_BACKEND_HEAL` | `anthropic` |
| `LLM_MODEL` | yes | `LLM_MODEL_PLANNER` / `LLM_MODEL_HEAL` | `claude-opus-4-8` (planner) / `claude-sonnet-4-6` (heal) |
| `LLM_BASE_URL` | yes | `LLM_BASE_URL_PLANNER` / `LLM_BASE_URL_HEAL` | — (Anthropic SDK default) |
| `LLM_API_KEY` | yes | `LLM_API_KEY_PLANNER` / `LLM_API_KEY_HEAL` | `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` |
| `LLM_VISION` | yes | `LLM_VISION_HEAL` | provider-default (Anthropic = enabled) |
| `LLM_STRUCTURED` | yes | `LLM_STRUCTURED_PLANNER` / `LLM_STRUCTURED_HEAL` | provider-default (Anthropic = enabled; OpenAI-compat = disabled, opt-in, ADR-057) |

**Precedence:** `LLM_<KEY>_<ROLE>` > `LLM_<KEY>` > built-in default.
Roles: `PLANNER` (explore/scenario phase), `HEAL` (heal + vision Tier-7).

No key present and `LLM_BACKEND=anthropic` → offline-fallback: heuristic planner + deterministic L1–L6 heal. **Sentinel never throws an exception due to a missing key** — it degrades gracefully.

### 2.2 Example: Ollama (local model)

```bash
# 1. Start Ollama (if not already running)
ollama serve &

# 2. Pull a model (one-time)
ollama pull qwen2.5:7b          # example; model catalog — docs/LOCAL_MODELS.md

# 3. Run explore with planner on Ollama (heal stays on default/offline)
LLM_BACKEND_PLANNER=openai \
LLM_BASE_URL_PLANNER=http://localhost:11434/v1 \
LLM_MODEL_PLANNER=qwen2.5:7b \
LLM_API_KEY_PLANNER=noauth \
  ./bin/agentctl run \
    --target "file://$PWD/testdata/site/index.html" \
    --planner llm

# 4. Both roles on Ollama (including vision-heal if the model supports it)
LLM_BACKEND=openai \
LLM_BASE_URL=http://localhost:11434/v1 \
LLM_MODEL_PLANNER=qwen2.5:7b \
LLM_MODEL_HEAL=llava:13b \
LLM_API_KEY=noauth \
LLM_VISION_HEAL=1 \
  ./bin/agentctl run --replay \
    --plan runs/<id>/plan.json \
    --target "file://$PWD/testdata/site-v2/index.html" \
    --heal-llm
```

> `LLM_API_KEY=noauth` — Ollama ignores the key, but the Anthropic/OpenAI SDK requires a non-empty string.

### 2.3 Example: OpenRouter / any cloud proxy

```bash
LLM_BACKEND_PLANNER=openai \
LLM_BASE_URL_PLANNER=https://openrouter.ai/api/v1 \
LLM_API_KEY_PLANNER=sk-or-... \
LLM_MODEL_PLANNER=deepseek/deepseek-chat \
  ./bin/agentctl run --target "https://staging.example.com" --planner llm
```

The heal role in this case remains on Anthropic (`ANTHROPIC_API_KEY`) or falls back to offline L1–L6
if no key is set.

> Catalog of tested models by role (context, throughput, vision compatibility):
> [`docs/LOCAL_MODELS.md`](LOCAL_MODELS.en.md).

---

## 3. Live run (integration run with browser)

Live runs require built components (`DEVELOPMENT.md §2`). Use `file://` fixtures from `testdata/`
for a reproducible environment with no external network.

### 3.1 M9.1 — login-as-test with secretRef (PW_NO_TRACE=1)

Scenario: the plan contains a `fill` step with a `secretRef` (the name of an env variable holding the password).
Tracing **must be disabled** — otherwise the secret will end up in `trace.zip` (architectural fail-closed, `brain/__main__.py`).

```bash
# Prerequisite: build binaries and browser (DEVELOPMENT.md §2)
./bin/agentctl run \
  --replay \
  --plan path/to/login-plan.json \
  --target "https://staging.example.com/login" \
  --heal-llm \
  PW_NO_TRACE=1 \
  LOGIN_USERNAME=alice \
  LOGIN_PASSWORD=s3cret
```

Or via exported variables (preferred in CI):

```bash
export PW_NO_TRACE=1
export LOGIN_USERNAME=alice
export LOGIN_PASSWORD=s3cret

./bin/agentctl run \
  --replay \
  --plan path/to/login-plan.json \
  --target "https://staging.example.com/login"
```

After a successful run, `storageState` is saved automatically if `STORAGE_STATE_SAVE` is set:

```bash
export STORAGE_STATE_SAVE=state/auth.json
./bin/agentctl run --replay --plan login-plan.json ...
# Subsequent runs: --target <protected-page> with STORAGE_STATE=state/auth.json
```

### 3.2 M9.2 — goal mode and describe mode

**goal mode**: Sentinel performs a full deterministic explore (phase 1), builds a site map,
then a single LLM call authors a scenario from the full map (phase 2).

```bash
# Against a file:// fixture (reproducible, no network)
GOAL="log in and click Pay" \
  ./bin/agentctl run \
    --target "file://$PWD/testdata/site/index.html" \
    --planner goal          # or: --planner heuristic + GOAL env (make_planner auto-selects)

# Against staging
GOAL="submit the contact form" \
ANTHROPIC_API_KEY=sk-ant-... \
  ./bin/agentctl run --target "https://staging.example.com"
```

**describe mode**: an LLM first drafts the flow in prose, then a deterministic reconcile runs against the site map.

```bash
DESCRIBE="fill the username field with 'alice', then click the Pay button" \
  ./bin/agentctl run \
    --target "file://$PWD/testdata/site/index.html"
```

**RunConfig YAML** (declarative, M9.2b):

```bash
./bin/agentctl run \
  --target "https://staging.example.com" \
  --run-config /config/run.yaml
```

Example `run.yaml`:

```yaml
auth:
  storage_state: state/auth.json  # skips login if the file exists
  pw_no_trace: true               # required when secretRef is present
  login_plan: runs/login/plan.json

scenarios:
  - name: checkout
    goal: "add item to cart and complete checkout"
  - name: search
    describe: "type 'laptop' in the search field and press Enter"
```

To select a specific scenario: `./bin/agentctl run --target ... --run-config run.yaml --scenario search`.

### 3.3 Reading artifacts

All artifacts are written to `runs/<run_id>/` (overridden by `--artifact-dir`).

#### `plan.json` — frozen explore plan

```json
{
  "plan_id": "550e8400-...",
  "plan_hash": "sha256:abcdef...",   // SHA-256 of the canonical JSON steps; any change → exit 3
  "target_url": "file:///...",
  "coverage_achieved": 0.92,
  "steps": [
    {"step_id": 1, "action_type": "navigate", "semantic_id": "...", "intent": "...", "target": "..."},
    {"step_id": 2, "action_type": "click",    "semantic_id": "...", "locator": {...}}
  ]
}
```

Committed to the application repository. Any manual edit of `plan.json` without `agentctl baseline update`
results in **exit 3** on the next replay.

#### `scenario.json` — reproducible business flow (goal/describe)

```json
{
  "plan_id": "<run_id>-scenario",
  "plan_hash": "sha256:...",
  "run_mode": "scenario",
  "mode": "goal",           // or "describe"
  "unmatched": 1,
  "steps": [
    {"step_id": 1, "action_type": "navigate", "target": "file:///s/login.html", "phase": "scenario"},
    {"step_id": 2, "action_type": "fill",     "locator": {"role": "textbox", "name": "Username"}, "value": "alice"},
    {"step_id": 3, "action_type": "navigate", "target": "file:///s/billing.html", "phase": "scenario"},
    {"step_id": 4, "action_type": "click",    "locator": {"role": "button", "name": "Pay"}}
  ]
}
```

`scenario.json` replays deterministically without LLM (steps carry full `locator`+`alternatives`).

#### `reconcile-report.json` — describe-mode report

```json
{
  "target_url": "file:///...",
  "grounded": 3,
  "unmatched": [
    {"ref": "NOPE", "reason": "ref not in site map"}
  ]
}
```

Generated only in describe mode. Non-empty `unmatched` → exit 1 (CI records "described flow does not exist in the UI").

#### `heal-report.json` — replay-with-healing result

```json
{
  "exit_code": 0,
  "steps": [
    {"step_id": 2, "type": "click", "outcome": "healed",
     "heal": {"strategy": "role_name", "confidence": 0.92},
     "regression": null}
  ],
  "healed": 1,
  "failed": 0,
  "regressions": []
}
```

In baseline mode the file is named `baseline-report.json`.

### 3.4 Exit codes

| Code | Condition |
|-----|---------|
| **0** | All steps passed (or healed); `scenario.json` written (goal/describe) |
| **1** | At least one step failed without healing; or describe returned any unmatched; or zero grounded steps |
| **2** | Golden regression — a11y hash or screenshot hash diverges from baseline |
| **3** | Plan integrity violation (`plan_hash` mismatch); mutually exclusive flags (`GOAL` + `DESCRIBE`); malformed RunConfig; unknown `--scenario`; `secretRef` present with `PW_NO_TRACE != '1'` |
| **4** | THE TOOL ITSELF failed: our own exception, or the crawl saw nothing at all (and not because there was nothing to see), or it failed to write its own artefact. This is NOT a finding about the application |
| **5** | The tool itself failed, but what it found was SAVED: the plan, the map and the proposed scenario are in the artefact, and they say at which step it stopped |
| **-1** | The process could not be spawned, or was killed by a signal. ⚠ A synthetic code: no real process exits with it — control-api assigns it by pairing `state` with a missing code |

In CI: exit 0 or 1 are normal outcomes (1 = "test found a problem"); exit 2 = UI regression; exit 3 = configuration error, requires manual intervention; exits 4 and 5 are **our** breakage, not a finding about your application (the templates make them `unstable` rather than a failure).

> The list is derived from `brain/events.json` → `exit_codes` and checked by the gate
> `tests/test_exit_code_surfaces_offline.py` (ADR-141). A row for a code the catalogue does not
> declare, and a missing row for one it does, are both red.

---

## 4. Zero-level path — docker compose demo (no build, no keys)

The minimal way to verify the system is working: one command, no API keys, no external network.
Uses the bundled `file://` fixture and the heuristic planner.

```bash
# Build the image (once; ~2–3 minutes)
docker compose build

# Run the demo: explore testdata/site/index.html -> runs/demo/plan.json
docker compose --profile demo up
```

After completion, artifacts are available on the host in `./runs/demo/`:

```
runs/demo/
├── plan.json             # frozen plan
├── llm-transcript.jsonl  # empty (LLM was not called)
├── trace.zip             # Playwright trace
└── checkpoint.db         # LangGraph checkpointer
```

View the trace:

```bash
npx playwright show-trace runs/demo/trace.zip
```

**Running a specific command directly:**

```bash
# arbitrary target
docker compose run --rm sentinel run \
  --target "file:///app/testdata/site/index.html" \
  --planner heuristic

# replay with frozen plan
docker compose run --rm sentinel run \
  --replay \
  --plan /app/runs/demo/plan.json \
  --target "file:///app/testdata/site-v2/index.html"

# goal mode with Ollama (start the service first)
docker compose --profile ollama up -d ollama
docker compose exec ollama ollama pull qwen2.5:7b
docker compose run --rm \
  -e LLM_BACKEND=openai \
  -e LLM_BASE_URL=http://ollama:11434/v1 \
  -e LLM_MODEL_PLANNER=qwen2.5:7b \
  -e LLM_API_KEY=noauth \
  -e GOAL="click the first link" \
  sentinel run --target "file:///app/testdata/site/index.html"
```

> **Volumes:** `./runs` and `./state` are mounted into the container — artifacts persist across
> runs. `./config` is mounted as `/config` — place RunConfig YAML files there.

---

## Reference: what to run in which context

| Context | Minimum set of commands |
|---|---|
| Before any commit | `go vet ./...` + `go test ./...` + `cd pw-executor && npm test` + `gitleaks detect` |
| PR / feature branch that adds a component or feature | Full offline suite (§1.1–1.2) + the DOM gates (setup-wizard, hub Logs-view, static-showcase) + `ui-smoke` screenshots + docker in EVERY delivery shape + a live run of the changed component + mutation testing — principle 7 (`docs/DEVELOPMENT.en.md` §0) requires this on EVERY such PR; the full binding checklist is [`docs/PR_ACCEPTANCE.en.md`](PR_ACCEPTANCE.en.md) |
| Release candidate (tag) | Everything above, plus the release-artifact gates: `install-smoke` / `deb-smoke` / `install-ps1-smoke` / `airgap` (`.github/workflows/ci.yml`, `release.yml`) |
| New LLM model | `test_b1_offline.py` + smoke run of goal mode vs. `file://` (§3.2) |
| pw-executor change | `cd pw-executor && npm ci && npm test` (compiles + runs `src/*.test.ts`, as CI does) + `test_m9_offline.py` |
| Healing change | `test_m3_offline.py` + `test_m8_offline.py` + live replay vs. site-v2 (§3.1) |
| RunConfig change | `test_m9_2_offline.py` + `test_m9_2b_offline.py` |

For details on build prerequisites, component structure, and extension recipes:
[`DEVELOPMENT.md`](DEVELOPMENT.md).
