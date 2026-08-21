# Degradation map — Sentinel

> 🌐 [Русский](DEGRADATION_MAP.md) (primary) · **English**

What happens when part of a deployment dies: **what falls off · how it looks to a person · what
keeps working · and whether it is visible** in `/readyz`, in the Health view and in the service
journal.

**Why this document exists.** Over the last few sessions a surface nobody exercises between
releases was found **four times**: the orchestrator, declared and never once launched · the `ghcr`
delivery form · the Windows cross-build · taking over a run with no orchestrator wired. Every one
of them came from EXECUTION, not from a gate. This map is the systematic answer to "what else is in
that state": it enumerates the components and says, for each, whether a person learns that it died.

⚠ **Every row below is a MEASUREMENT, not reasoning.** The component was broken on a live stand and
what it answered is what is written. Where there is no measurement, it says so.

---

## 1. The component set is DERIVED, not maintained

The rows of this map come from the union of three sources: the probes in
`cmd/control-api/readyz.go` · the `services:` of the three compose files · the directories under
`cmd/*`. The gate `tests/test_degradation_map_offline.py` requires **every** derived name to have a
row here or a declared alias, and holds a floor on the number of rows. The reason is principle 5
(`docs/DEVELOPMENT.md` §0): a hand-kept list does not show what is missing, because absence has no
representation.

The same thing under different names is folded explicitly: `store` = `store-gateway` ·
`vnc` = `browser-vnc` · `llm` = `ollama` = `litellm`.

---

## 2. The map

| Component | What falls off | How it looks to a person | What keeps working | `/readyz` | Journal |
|---|---|---|---|---|---|
| **store-gateway** (`store`) | storage of runs, goldens, configuration, the library | control-api **starts**, but shouts into the log: `WARNING — store-gateway "…" did not answer at start`. A run finishes and is recorded `exit=3 fault=tool` | runs proceed; the run list answers from control-api's memory | ✅ `not_ready`, `store: error` with the socket address and `no such file or directory` / `connection refused` | ✅ `service.store_unreachable` with the address |
| **browser service** (`browser`) | the live view, CDP-mode runs | the run fails: `connectOverCDP: connect ECONNREFUSED <address>` — with the address. **exit 4** | everything that needs no browser | ✅ `browser: error` (or `skipped` when no browser service is declared in this deployment) | — |
| **the browser dies MID-run** | the current run | `Target page, context or browser has been closed` plus the explicit caveat "This is NOT a finding about your app". **exit 4** | the next run opens its own | ✅ | — |
| **orchestrator** | the hard budget ceiling, run takeover, the map gate | takeover answers `control-error: no orchestrator wired (set CONTROL_API_ORCH_ADDR)`; runs proceed **unsupervised** | runs, and the soft budget ceiling inside the brain | ✅ `skipped`, naming what is lost: "no budget ceiling, no takeover, no map gate"; when declared-but-dead — `error` with the address | — |
| **the model** (`llm`) | the AI planner and AI healing | ⚠ **the run does NOT fail**: `plan.llm_error_heuristic: The AI could not plan … falling back to simple rules`, then simple rules, **exit 0**. The upstream text is preserved (`400 — invalid model name`) | the whole run, on the heuristic | ✅ `llm` — but `skipped` when the model address arrived only in the run request rather than in the configuration | the code is catalogued; ⚠ `plan.json` carries `degradations: null` — `[EXPLORE-DEGRADATION-NOT-IN-ARTIFACT]` |
| **a mode that REQUIRES a model** (`goal`/`describe`/`chat`) | the mode itself | **refused at the door, not silently degraded**: `fatal.llm_required_unreachable: This mode needs a model and there is none: set LLM_BACKEND/LLM_MODEL/LLM_BASE_URL, or an API key`. **exit 3** | modes that need no model | ✅ | — |
| **agentctl** (the run's executable) | ALL runs | ⚠ **the deployment stays silent**: `POST /v1/runs` is accepted with 202, the run fails with `fork/exec …: no such file or directory`, and the run record carries `state: failed` with `exit_code: 0` | the UI, the journal, the list — all "healthy" | ❌ **`status: ready`** — there is NO probe for its own executable → `[READYZ-BLIND-TO-AGENTCTL]` | ❌ only `service.api_call POST /v1/runs → 202` |
| **configuration** (`config`) | the deployment's saved settings | the Health view shows `FAILED` with the reason `no config stored; run the setup wizard` | everything else; a run starts with the parameters from its request | ✅ `config: error`/`skipped` with a reason | — |
| **browser-vnc** (`vnc`) | the "Screen" mode, the real cursor, taking the mouse | the Screen tab prints the reason and the command: "the screen is unavailable in this deployment: no VNC screen configured (CONTROL_API_VNC_SOCK is unset)" + `docker compose --profile vnc up -d browser-vnc` | every other live mode; the "Video" tab shows the headless browser | ✅ `vnc: skipped` with a reason | — |
| **webui** | serving the pages on 8088 | the page does not open; ⚠ **not measured** — I did not break it | control-api and all its routes; pages are also served by control-api itself when `CONTROL_API_SERVE_UI=1` | ❌ no probe | — |
| **pw-executor** (inside a run) | browser steps | a step error classified as `tool`, the run stops | control-api and the next run | ❌ no probe of its own: it lives inside the run's process, not as a service | writes `service.started`/`stopped` on behalf of the browser service |
| **control-api** | HTTP control, the UI, spawning runs | nothing answers | `agentctl` from a terminal works in full | — (it is the one that answers) | ✅ `service.started`/`service.stopped` |

### Not components — and that is written down, not skipped

`sentinel` and `demo` are one-shot CLI invocations in compose rather than services: between runs
they have nothing to die of. `ollama-models` is an initialiser that ran once. `airgap` is the name
of a network in `docker-compose.offline.yml`. They appear in the derived list of names and are
therefore named here: a name with no row in the map would otherwise read as an omission.

---

## 3. What running every "mode × observation" combination showed

Six modes (`explore`/`goal`/`describe`/`replay`/`baseline`/`chat`) × five observation axes
(`off`/`frames`/`stream`/`human`/`record`) — thirty combinations, all executed.

**The refusals that had to happen did, and named their reason:**

- `baseline × human` and `baseline × record` → **exit 3**, `fatal.observe_refused: observe=human
  cannot be combined with a golden capture: the cursor overlay and slowMo change the…` — the
  boundary "a decorated mode is not mixed with capturing a golden" holds;
- `goal`/`describe` with no model → **exit 3**, `fatal.llm_required_unreachable`;
- `chat` with no intent → **exit 3**, `fatal.chat_no_intent: Chat mode needs a goal or a flow
  description — neither was given`.

**A third boundary was found that the registry does not mention:** `replay --observe frames` on a
green run yields **zero** frames while announcing `frames: chosen` (in `explore` the same words
yield seven). In replay a frame is captured only AT A FAILURE (`brain/replay.py:506-509`). →
`[OBSERVE-FRAMES-MEANS-TWO-THINGS]`.

---

## 4. What this map does NOT cover — named, not left silent

- **A live target of 50+ steps has not been found.** Point 9 of the campaign is open work. The
  `testdata/site-spa` fixture (80 states, the crawl stalls at 40 steps, coverage 0.5067) does not
  replace it: `file://` has no network latency.
- **The death of `webui` was not measured** — the only row without a measurement, and it is marked.
- **Pressing every button was not completed:** of 85 controls 8 are reachable immediately, 53 sit in
  collapsed panels, and 3 were skipped as destructive (`Sign out` · `new conversation` · `Clear`).
  → `[SMOKE-COUNTS-BUTTONS-BUT-DOES-NOT-PRESS]`.
- **A full disk** was not reproduced in this wave: the measurement is recorded earlier (the failure
  arrives as `ENOSPC` while launching Chromium, i.e. it looks like a browser regression rather than
  a disk that ran out).
