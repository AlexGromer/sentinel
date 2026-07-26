# Sentinel deployment guide for Windows

> 🌐 [Русский](WINDOWS_TESTING.md) (основная версия) · **English**

> **Type:** How-to · **Audience:** the live-test operator on a Windows host
> **Related:** [M9_LIVE_PLAN.md](./M9_LIVE_PLAN.md) · [LOCAL_MODELS.md](./LOCAL_MODELS.md) · [QUICKSTART.md](./QUICKSTART.md) · [DISTRIBUTION.md](./DISTRIBUTION.md)

Stand up the Sentinel stack, run the M9-LIVE checks against a local LLM, and collect the artifacts. Both phases (the built-in fixtures and the real sites) are run through the web UI - this is the primary demo path; the same runs from the command line are in a separate section. Every command matches `install.ps1`, `docker-compose.yml`, and `M9_LIVE_PLAN.md`. The stack is built from source: `install.ps1` installs only `agentctl.exe` from a release with a `v*` tag and is not suitable for this test.

## Host requirements

| Component | Value |
|---|---|
| OS | Windows 10 or 11 |
| GPU | NVIDIA, 8-12 GB VRAM (the model choice depends on the amount, see "Choosing models") |
| Docker | Docker Desktop, WSL2 backend |
| Git | Git for Windows (provides `git` and Git Bash) |
| LLM runtime | native Ollama for Windows (direct GPU access) |

## How the test works: two phases and two models

Sentinel runs in two phases, and each uses its own model. The models solve different tasks at different times, so they are not resident in VRAM at the same time: Ollama loads them one at a time when `OLLAMA_MAX_LOADED_MODELS=1`. That is why there are two models.

| Phase | When it runs | What the model does | Role | Model |
|---|---|---|---|---|
| explore, author | First pass: the tool studies the page and builds a plan | Reads the page tree (DOM and accessibility), picks a real element by index, forms the plan steps | planner, text | `qwen3:14b` |
| replay, heal | Replay of a ready plan | If a saved locator broke because of a layout change, finds the element again, including by screenshot (set-of-marks) | heal, vision | `qwen2.5vl:7b` |

Consequences:
- the planner never fabricates a selector: it picks an index among the elements actually found on the page (grounding). This guards against locator hallucination.
- heal is invoked only when the deterministic strategies L1-L6 could not re-bind the broken locator. The vision heal path is currently disabled, so the planner does the main work; the vision model is needed for the future visual heal.
- the replay phase normally runs without an LLM (0 tokens): the planner already decided everything during explore, and replay reproduces the frozen plan. The LLM kicks in only for heal.

## Choosing models

The Sentinel workload does not need a large model: the output is short structured JSON (planner propose at most 200 tokens, scenario at most 800, heal at most 200), the input is at most 2000 tokens, temperature 0. The goal is the minimum viable model, not the largest that fits into VRAM.

### Baseline models

| Role | Model (12 GB) | Model (8 GB) | Note |
|---|---|---|---|
| Planner (explore, author) | `qwen3:14b` Q4_K_M, about 9.5 GB | `qwen3:8b` Q5, about 6 GB | Non-reasoning mode. On 8 GB the 14B partially offloads to CPU and is slow. |
| Heal (replay, vision) | `qwen2.5vl:7b` Q4, about 7 GB | `qwen2.5vl:7b` Q4 | Supported by Ollama, ScreenSpot 84.7. The vision heal path is currently disabled. |

A precise per-config estimate is in the calculator `docs/calculators/vram.html`, the method is in `LOCAL_MODELS.md §5`.

### Finding the minimum model on M9-LIVE

1. Run the baseline set, capture the RISK-002 metrics (confidence and the unmatched rate on local models).
2. Repeat the same test with planner `qwen3:8b` (parameter `LLM_MODEL_PLANNER=qwen3:8b`). If grounding and JSON validity hold, `qwen3:8b` becomes the recommended minimum.
3. Use models at the VRAM ceiling (14B in Q5 or Q6, vision 15B, 32B on a larger GPU) only if the data shows that 14B does not cope. That is a decision by data, not up front.

Do not use DeepSeek-R1-Distill-14B for the planner: a reasoning model adds internal reasoning tokens that exhaust the planner output limit (at most 200 tokens) and cause degradation.

## Install Ollama and pull the models

Install the native Ollama for Windows from https://ollama.com/download/windows

Set the limit on simultaneously loaded models. Without it the planner and heal together exceed the VRAM, part of a model offloads to RAM, and speed drops:

1. Open "Edit the system environment variables", add the variable `OLLAMA_MAX_LOADED_MODELS` with value `1`.
2. Restart Ollama: the tray icon, "Quit Ollama", then start it again. A PowerShell session variable does not affect an already-running background service, you need a system variable and a restart.

Pull the models:
```powershell
ollama pull qwen3:14b
ollama pull qwen2.5vl:7b
ollama pull qwen3:8b
ollama list
```

## Build Sentinel (Docker Desktop)

```powershell
git config --global core.autocrlf input
git clone https://github.com/AlexGromer/sentinel.git
cd sentinel
docker compose build
```

LLM-free stack check:
```powershell
docker compose run --rm sentinel run --target "file:///app/testdata/fixtures/l3.html" --planner heuristic --artifact-dir /app/runs/smoke
```
Expected result: exit code 0 and the file `.\runs\smoke\plan.json`. Conclusion: the stack is built and works without an LLM. Fixtures l1-l6 are already in the image.

## Working directory

Run every `docker compose` command from the root of the sentinel repository (where you landed with `cd sentinel`). Compose mounts three directories relative to the root: `./runs` (run artifacts, `.\runs` on the host), `./state` (the conversation database, locators, golden snapshots, the store-gateway socket), `./config` (RunConfig YAML). So artifacts appear in `.\runs\<id>`, and the chat database is saved in `.\state\conversations.db` and survives a container restart. If you open a new PowerShell window, cd back into the repo root before the command.

## Local LLM connection parameters

Without `LLM_BACKEND=openai` the default backend is anthropic, the `LLM_BASE_URL` address is ignored, and the run silently switches to the deterministic HeuristicPlanner with exit code 0. This is the main trap of the milestone: the run counts as successful while the LLM is off. These variables are given to the control-api service (for runs from the UI) and/or to the sentinel container (for runs from the command line).

| Parameter | Why it is needed | Where to get the value | Options | How to set |
|---|---|---|---|---|
| `LLM_BACKEND` | Choice of the LLM client | The `make_backend` function in `brain/llm.py` | anthropic; openai; sampling | `openai` for Ollama |
| `LLM_BASE_URL` | Address of the OpenAI-compatible endpoint | Ollama port 11434 | from the container `http://host.docker.internal:11434/v1`; native `http://localhost:11434/v1` | a value with the `/v1` suffix |
| `LLM_API_KEY` | Access key | Ollama does not check the key | any non-empty string | `noauth` |
| `LLM_MODEL_PLANNER` | Model for the planner role | The `ollama list` output | `qwen3:14b`; `qwen3:8b` | the exact tag name |
| `LLM_MODEL_HEAL` | Model for the heal role | The `ollama list` output | `qwen2.5vl:7b` | the exact tag name |
| `LLM_VISION` | Vision for the heal role | A vision model is required | `1` to enable; empty to disable | `1` for `qwen2.5vl` |
| `LLM_STRUCTURED` | Strict structured output (ADR-057) | Optional | `1` to enable; empty to disable | enable after checking the endpoint supports `json_schema` |
| `OLLAMA_MAX_LOADED_MODELS` | Limit of models in memory | A Windows system variable | `1` | value `1`, then restart Ollama |

`LLM_MODEL_PLANNER` and `LLM_MODEL_HEAL` override the general `LLM_MODEL` for a specific role. The names come from `brain/llm.py`, the form `SENTINEL_..._MODEL` is not supported.

## Running via the web UI

This is the primary path: runs are configured and started from the browser, the steps are visible on a live timeline. Both phases below are run this way. The recommended mode on a Windows host (ADR-064) is single-service: one port, zero CORS wiring, zero copy-paste of the token. Only one service is needed, `control-api` - in this mode it serves both the HTTP API and the UI pages, listening on `127.0.0.1:8090`.

Start the service in single-service mode. The access token is generated automatically on first start - there is no need to set it by hand; the LLM connection is still set in the UI itself (ADR-063):
```powershell
$env:CONTROL_API_SERVE_UI = "1"
$env:CONTROL_API_CORS_ORIGINS = ""
docker compose --profile control-api up -d control-api    # 127.0.0.1:8090
docker compose logs -f control-api                        # find the line "open http://127.0.0.1:8090/?bootstrap=<nonce>"
```

About the `$env:CONTROL_API_CORS_ORIGINS = ""` line. In PowerShell, assigning an empty string to an environment variable does not set it empty - it removes the variable, so compose substitutes its own default allowlist instead. That used to break the live timeline: the `/v1/stream` socket got a 403 and the browser reported close 1006. In single-service mode the allowlist no longer affects the socket at all - one origin serves both the page and the API, and such a handshake passes on a host match rather than on the list. You can keep the line: it is harmless under either outcome.

On first start control-api generates the access token itself (32 random bytes, saved to `state/control-api.token` and reused on restart). Open the `http://127.0.0.1:8090/?bootstrap=<nonce>` link from the log - it is one-time, valid for 5 minutes, and fills in the control-api address and the token in Settings itself, stripping the nonce from the URL. If the log window is closed or the link has expired - read the token from `state/control-api.token` and paste it into Settings by hand, or restart control-api for a fresh link.

Open the hub `http://localhost:8090/`. It is the co-pilot (ADR-055, ADR-066): a vertical rail on the left, one view on screen at a time. **Chat** — describe a check in words; **New run** — the launch form (what used to be called the "configurator" and sat under Settings); **Live** — the live timeline; **Library** — scenarios, tests, runs, conversations; **Results** — verdicts and metrics; **Logs** — what happened, in plain language and filterable; **Tools** — the calculators and reference; **Settings** — the connection (address and token, the single place in the product), the LLM and the logging levels. The rail foot shows a connection dot and the version. Any view has a direct link: `#v=chat`, `#v=logs`, and so on.

1. The bootstrap link has already filled in the control-api address and the token in Settings - there is no need to enter them by hand.
2. In the #build section set the LLM connection: backend `openai`, base_url `http://host.docker.internal:11434/v1`, planner model `qwen3:14b`, heal model `qwen2.5vl:7b`, vision as needed. These fields travel with the run and control-api materializes them into the env (ADR-063) - you do not set `LLM_*` in the control-api environment separately. A local Ollama needs no key (control-api defaults `noauth`).
3. For each run set target, goal (a natural-language goal) and mode (usually `goal`), click Run and watch the live timeline; the verdict and artifacts appear there, the run lands in Tests history with an id like `control-<...>`.

LLM source precedence: **control-api process env > per-run from the UI > persisted config**. So to pin a model on the control-api side, set `LLM_*` in its environment - the UI will not override it. The wizard `http://localhost:8090/setup/` collects the same fields and its "Save to server" button writes them to the persisted config (needs the `store` profile, see "Database and chat mode"). Chat is at `http://localhost:8090/chat/`.

## How to read run artifacts

A run from the UI creates the directory `runs\control-<id>\` (from the command line - `runs\<id>\` by your `--artifact-dir`). In the UI the outcome is on the timeline and in Tests history; the same data is in the files:

| File | What it holds | What to check |
|---|---|---|
| `plan.json` | The frozen step plan and `plan_hash` | Steps reference real elements; on replay `plan_hash` must not change |
| `scenario.json` | The scenario for goal and describe modes | Step-to-element binding, the unmatched field |
| `heal-report.json` | The locator recovery report | Strategy (L1-L6), confidence, healed and failed |
| `llm-transcript.jsonl` | The LLM call log | The `planner` field equals `llm`, not `heuristic` |
| `report.json`, `report.html` | The run summary and the exit code | The `exit_code` field |
| `metrics.prom` | Prometheus metrics | `sentinel_run_exit_code` |
| `trace.zip` | Playwright trace (live DOM and request bodies) | For local analysis only, not shipped out |

Exit codes (from `brain/replay.py` and `brain/__main__.py`):

| Code | Meaning | Interpretation |
|---|---|---|
| 0 | pass | The run passed successfully |
| 1 | step failure | A step failed (non-quarantined) |
| 2 | golden regression | Divergence from the golden baseline (non-quarantined) |
| 3 | plan integrity | `plan_hash` mismatch or a bad invocation, hard abort |

## Phase 1: run against the built-in fixtures l1-l6

The fixtures are self-contained HTML pages reachable by the container at a `file://` address (they are in the image). Run each through the web UI: in Settings paste the target and the goal, pick the `goal` mode, click Run and watch the timeline. Go in ascending order of complexity. For each below: what to set in the UI, the expected result, and the conclusion. After each run check in its record that the LLM was used (`planner` = `llm`).

### L1: element discovery
What it checks: discovery of buttons, clicks, and anchor links (4 buttons, one disabled, anchor links).
- Target: `file:///app/testdata/fixtures/l1.html`
- Goal: `click the primary button, then follow the anchor link to a section`
- Mode: `goal`

Expected result: on the timeline the steps point at real buttons and links, the disabled button is not chosen; verdict 0. Conclusion: the planner sees page elements and does not fabricate a selector.

### L2: login
What it checks: fill and login, the correct and wrong credential branches. Demo creds `demo` / `demo`.
- Target: `file:///app/testdata/fixtures/l2.html`
- Goal: `log in with username demo and password demo and confirm the logged-in panel appears`
- Mode: `goal`

Expected result: on the timeline `#panel-logged-in` appears, there is no `#alert-error`; verdict 0. Conclusion: field fill and the login button click work, the post-login state is recognized.

### L3: form validation
What it checks: negative checks, per-field errors (email format, number 18-120, required, at most 80 characters with a counter).
- Target: `file:///app/testdata/fixtures/l3.html`
- Goal: `fill the form with valid data and submit, then check the error for a wrong email and a number outside 18-120`
- Mode: `goal`

Expected result: with valid data the form is accepted, with invalid data `#err-*` appears next to the fields; verdict 0. Conclusion: the tool distinguishes valid and invalid states and reads per-field errors.

### L4: multi-page flow
What it checks: an end-to-end scenario over 3 pages with a sessionStorage handoff and a confirmation modal. Demo creds `admin` / `secret`.
- Target: `file:///app/testdata/fixtures/l4.html`
- Goal: `log in with username admin and password secret, go to the dashboard, open billing and confirm the upgrade in the modal`
- Mode: `goal`

Expected result: on the timeline the plan goes through `l4.html`, then `l4-dashboard.html`, then `l4-billing.html`, the modal confirmation is clicked; verdict 0. Conclusion: the tool drives a multi-step business scenario with navigation between pages.

### L5: tabs and shadow DOM
What it checks: ARIA tabs, async content injection (after 600 ms), and elements in the shadow DOM (RISK-005).
- Target: `file:///app/testdata/fixtures/l5.html`
- Goal: `switch to the tab with dynamic content, wait for the elements to load, then open the color picker`
- Mode: `goal`

Expected result: `#dynamic-slot` waits for the replacement with real content, the shadow-DOM element is found via a pierce locator; verdict 0. Conclusion: the tool works with async content and the shadow DOM, not only with static markup.

### L6: multiple browser tabs
What it checks: tracking new browser tabs (`target=_blank` and `window.open`) plus in-page ARIA tabs.
- Target: `file:///app/testdata/fixtures/l6-newtab.html`
- Goal: `open the link in a new tab and switch to it, then switch the in-page tabs`
- Mode: `goal`

Expected result: the new tabs are reflected in `browser.tabs`, switching between them is done; verdict 0. Conclusion: the tool sees and switches browser tabs and in-page tabs.

### Self-heal check on fixtures
1. Run the L2 run through the UI (as above) - a plan appears in Tests history.
2. Replay the same plan against a changed version of the page: in Tests pick the run and click Re-run (that is replay), or set the target to a changed copy of the fixture (with a renamed id or a different element order).

Expected result: if a locator broke, the run record (`heal-report.json`) shows the L1-L6 strategy and confidence; on a successful recovery the verdict is 0. Conclusion: the deterministic heal fixes a broken locator, the confidence gate fires (data for RISK-002).

## Phase 2: run against real public sites

Point Sentinel only at public sandboxes built for automation, or at apps you own. Never point it at third-party production sites: even a read-only DOM traversal of a site you do not own risks a terms-of-service violation and unauthorized access. All the sites below are public practice sandboxes (Sauce Labs and others). Before the first run make sure the site is reachable. Enter only the published test credentials, never real personal, payment, or account data. Each site is run the same way, through the UI: the target is the site's `https` address, Chromium is already in the image.

### Site 1: simple login (Practice Test Automation)
Address and credentials: https://practicetestautomation.com/practice-test-login/ , `student` / `Password123` (published on the page).
- Target: `https://practicetestautomation.com/practice-test-login/`
- Goal: `log in with username student and password Password123, click Submit and confirm the successful login message`
- Mode: `goal`

Expected result: a redirect to `/logged-in-successfully/`, the heading "Congratulations student. You successfully logged in!" and a "Log out" button; verdict 0. Conclusion: a basic login on a real site works. Negative fixtures on the same form: a wrong username gives "Your username is invalid!", a wrong password gives "Your password is invalid!". Note: the site has an anti-bot wall for simple HTTP clients; Sentinel drives a real Chromium browser and passes normally.

Self-heal test: the page is static, so use a controlled break. Ground the Submit button by its id `#submit`, then in replay substitute a stale or renamed id. Heal must re-bind by the visible text "Submit" or the button role and still submit the form.

### Site 2: multi-page checkout (SauceDemo)
Address and credentials: https://www.saucedemo.com/ , `standard_user` / `secret_sauce`.
- Target: `https://www.saucedemo.com/`
- Goal: `log in as standard_user with password secret_sauce, add the Sauce Labs Backpack to the cart, open the cart, check out with any first name, last name and zip, click Finish and confirm the order completes`
- Mode: `goal`

Expected result: on the timeline six sequential screens (login, inventory, cart, checkout-step-one, checkout-step-two, checkout-complete), the final page `/checkout-complete.html` with the heading "THANK YOU FOR YOUR ORDER" and a "Back Home" button; verdict 0. Conclusion: the tool drives a full multi-page e-commerce scenario. Additional accounts (same password): `locked_out_user` for a negative login test; `problem_user`, `performance_glitch_user`, `error_user`, `visual_user` for resilience under UI bugs and slowness.

Self-heal test: ground the Backpack "Add to cart" button by a positional selector (an index in the product grid), then change "Sort by" to Name Z-A or Price high-low. The reorder moves the product to a different index and breaks the positional locator, while the stable `data-test` attribute (`add-to-cart-sauce-labs-backpack`) and the visible product name stay. A good heal re-binds by `data-test` or the product name, not by position.

### Site 3: dynamic widgets and self-heal stress (The Internet)
Address and credentials: https://the-internet.herokuapp.com/ , the login form on `/login`: `tomsmith` / `SuperSecretPassword!`. The widget pages need no credentials.
- Target: `https://the-internet.herokuapp.com/`
- Goal: `on /login log in as tomsmith with password SuperSecretPassword! and confirm the success banner, then on /checkboxes check the first checkbox, on /dropdown select Option 2, on /add_remove_elements add an element and delete it, on /dynamic_loading/1 click Start and wait for the text Hello World`
- Mode: `goal`

Expected result: the login leads to `/secure` with the text "You logged into a secure area!"; the first checkbox is checked; Option 2 is selected in the dropdown; after deletion the added element is gone; on `/dynamic_loading/1` "Hello World!" appears after the async delay; verdict 0. Conclusion: the tool works with dynamic widgets and async content. Note: the free Heroku dyno can sleep, the first request can be slow - this is latency, not an outage.

Self-heal test: the page `/challenging_dom` regenerates ids and classes on every load, so a saved id or class locator breaks on the next run while the visible text and role stay. This is a clean check of text-or-role recovery. Verify empirically with a reload-diff before using it as a benchmark.

### Site 4 (optional): registration forms (Automation Exercise)
Address: https://automationexercise.com . There are no fixed credentials: for each run register a new unique email (with a time suffix), otherwise you get "email already exists".
- Target: `https://automationexercise.com`
- Goal: `register a new account with a unique email, fill the details form with dummy values, add any product to the cart, place the order, pay with dummy card data and confirm the order confirmation`
- Mode: `goal`

Expected result: the final page shows "Congratulations! Your order has been confirmed!"; verdict 0. Conclusion: the tool goes through registration and three forms (registration, account details, payment). Limitations (why optional): no fixed credentials and the catalog changes over time, so checks must not rely on a specific product name or price. Payment accepts only dummy card digits.

## Database and chat mode

A multi-turn dialog (chat) keeps context between turns in a database, so the second and later turns work on top of the already-built site map.

The default store is SQLite in the `./state` directory (mounted by compose): conversations in `state/conversations.db`, locators and golden snapshots there too. Persistence comes from the `./state` mount, so the dialogs survive a container restart. Postgres (optional, for several runners or K3s): set `CHECKPOINT_DSN=postgresql://user:pass@host:5432/sentinel` on the service and the conversation checkpointer switches from SQLite to Postgres (`langgraph PostgresSaver`). The store-gateway for the runs, scenarios, tests, results, metrics domains is still SQLite; Postgres for it is a separate M13 service, ahead.

Chat via the web UI: open `http://localhost:8090/chat/` (in split mode, where the `webui` service serves the static pages, `http://localhost:8088/chat/`). The console generates a `conversation_id` itself, the thread accumulates between turns, the "New conversation" button starts a new one. Under the hood control-api starts `agentctl run --mode chat --conversation-id` via `POST /v1/runs` with the `conversation_id` field. Hold the dialog over several turns on one goal (for example: "log in as demo/demo", then "now click logout") - the second turn works on top of the site map from the first.

Persistence check from the command line (two turns with one `conversation-id`):
```powershell
$LLM = @("-e","LLM_BACKEND=openai","-e","LLM_BASE_URL=http://host.docker.internal:11434/v1","-e","LLM_API_KEY=noauth","-e","LLM_MODEL_PLANNER=qwen3:14b","-e","LLM_MODEL_HEAL=qwen2.5vl:7b")
docker compose run --rm $LLM sentinel run --mode chat --conversation-id demo-conv-1 --goal "log in as demo with password demo" --target "file:///app/testdata/fixtures/l2.html" --artifact-dir /app/runs/chat1
docker compose run --rm $LLM sentinel run --mode chat --conversation-id demo-conv-1 --goal "now click the logout button" --target "file:///app/testdata/fixtures/l2.html" --artifact-dir /app/runs/chat2
```
Check: turn one logs the line `chat: COLD conversation=demo-conv-1`, turn two logs `chat: RESUME conversation=demo-conv-1`. The file `state\conversations.db` is not cleared at the end of a turn. Conclusion: the dialog is held with context kept in the database. Saved conversations are also available via `GET /v1/chats` on control-api (a token is required), and any OpenAI-compatible client can drive Sentinel "as a model" via `POST /v1/chat/completions` (one chat turn = one run).

## Verifying the LLM was used

Open the file `.\runs\<id>\llm-transcript.jsonl` (for a run from the UI - `.\runs\control-<id>\llm-transcript.jsonl`), find the `planner` field: the value must be `llm`, not `heuristic`.
```powershell
Select-String -Path .\runs\*\llm-transcript.jsonl -Pattern '"planner"' | Select-Object -First 3
```
On local models 14B and 7B the FLAG and unmatched rate is higher than on cloud Opus and Sonnet: the thresholds AUTO 0.85 and FLAG 0.60 were tuned on cloud models. This is expected data for calibrating RISK-002 and RISK-003, not an error. For each run record in the file `runs\LIVE_NOTES.md`: the id, the model, the target, the expected and actual result, the exit code, the noticed deviations.

## Collecting and transferring artifacts

`<run_id>` is the name of the directory under `runs`: for a run from the UI it is `control-<...>` (visible in Tests history), for a run from the command line it is the name from `--artifact-dir`. List the ready runs: `dir runs` in PowerShell or `ls runs` in Git Bash.

Collect EACH run you want analysed (run in Git Bash or WSL):
```bash
for id in $(ls runs | grep -vx LIVE_NOTES.md); do
  scripts/collect-live-run.sh "$id"
done
scripts/collect-live-run.sh <run_id> --with-trace   # optional: + trace.zip, not redacted, disposable stand only
```
Each call creates `live-results/live-<id>.tar.gz`. Redaction is on by default and applies to a staging copy, the `runs/` directory is not changed: values of fill, type, select, press steps without `secretRef` are blanked, the Authorization, Bearer, Cookie headers and strings like `sk-` and JWT are removed. Hashes, ids, and counters (`plan_hash`, golden-sha256) are preserved. The files `checkpoint.db` and `storage_state*.json` are never collected. The conversation database `state/conversations.db` lives outside `runs/<id>/` and does not enter the bundle; for chat analysis the `runs/chat1` and `runs/chat2` artifacts are enough.

What to send: all the `live-results/live-*.tar.gz` files plus your log `runs/LIVE_NOTES.md` (the collect script does not put it in the bundle, copy it separately). Transfer: USB or scp, not via git (`.gitignore` silently swallows `*.tar.gz`, gitleaks does not look inside gzip). Drop everything on the dev host in the directory `/opt/agent_development/live-results/`, then say "analyse the live runs".

⚠ Do not send the control-api logs: in modes 1-2 they print the `CONTROL_API_TOKEN` value, and in mode 3 the one-time bootstrap link. Do not hand over `state/control-api.token` either - the collector does not pick it up (it lives outside `runs/<id>/`), and that is deliberate.

## Network access

For running against the fixtures and the real sites the host address is not needed: everything runs locally. In single-service mode (recommended above) the UI and the API live on the same port `127.0.0.1:8090` and open from the same host over `localhost`; port 8088 exists only in split mode, where a separate `webui` service serves the static pages. The host address on the local network (find it with `ipconfig`) is needed to reach the services from another machine:

- Open the UI and the API from another host: control-api listens on `127.0.0.1:8090` by default. To expose it, set `CONTROL_API_ADDR=0.0.0.0:8090`, publish the port and add a Windows firewall rule - then `http://<host-address>:8090` serves both the pages and the API **with no CORS configuration at all**: they are one origin. Open the bootstrap link from the log against that same host address (the log prints `127.0.0.1`; substitute it by hand). ⚠ A public bind exposes run spawning to the network; the bearer token is the only gate (ADR-032). Trusted home network only.
- Reach Ollama from another host: set the system variable `OLLAMA_HOST=0.0.0.0`, restart Ollama, then set `LLM_BASE_URL=http://<host-address>:11434/v1`. This opens Ollama on the local network without authentication, use it only on a trusted network.

## Windows specifics

> **The easiest path is WSL2.** Every bash script in the repository (`scripts/offline-verify.sh`, `scripts/collect-live-run.sh`, `scripts/build-airgap-bundle.sh`) assumes a POSIX shell; Git Bash runs them, with the caveat below. The "Alternative: build in WSL2" section at the end of this document is the recommended route, not the fallback.

- **Git Bash rewrites paths in Docker arguments.** MSYS treats an argument starting with a slash as a Windows path and expands it: `docker compose run --entrypoint /bin/sh …` becomes `exec: "C:/Program Files/Git/usr/bin/sh"` and fails. Two equally good cures:
  ```bash
  MSYS_NO_PATHCONV=1 docker compose run --rm --entrypoint /bin/sh sentinel
  docker compose run --rm --entrypoint //bin/sh sentinel     # a doubled slash is left alone
  ```
  This applies to ANY argument with a leading slash — `--entrypoint`, `-v`, `--artifact-dir /app/runs/...`. PowerShell, CMD and WSL are unaffected.

- `host.docker.internal` - the address by which the container reaches the native Ollama on the Windows host. If Ollama runs as a compose service (the ollama profile), the address from the container is `http://ollama:11434/v1`.
- Volume paths: in PowerShell `${PWD}\runs:/app/runs`, in CMD `%cd%\runs:/app/runs`.
- GPU: the native Ollama for Windows uses the GPU directly; GPU passthrough into Ollama inside Docker on Windows is harder, so the native one is used.
- Line endings: bash scripts need LF, so before cloning set `git config --global core.autocrlf input`, or work in WSL.

## Running from the command line (alternative)

The same runs can be started without the UI. The `$LLM` set is from "Local LLM connection parameters":
```powershell
$LLM = @(
  "-e","LLM_BACKEND=openai",
  "-e","LLM_BASE_URL=http://host.docker.internal:11434/v1",
  "-e","LLM_API_KEY=noauth",
  "-e","LLM_MODEL_PLANNER=qwen3:14b",
  "-e","LLM_MODEL_HEAL=qwen2.5vl:7b",
  "-e","LLM_VISION=1"
)
docker compose run --rm $LLM sentinel run --goal "<goal>" --target "<target>" --artifact-dir /app/runs/l1
docker compose run --rm $LLM sentinel run --replay --plan /app/runs/l1/plan.json --artifact-dir /app/runs/l1-replay
```
Substitute the target and goal from Phase 1 and Phase 2. Here you set `--artifact-dir` yourself, so `<run_id>` equals that name (in the UI the id is generated as `control-<...>`).

## Alternative: build in WSL2 (Ubuntu)

```bash
sudo apt update && sudo apt install -y golang-1.26 nodejs npm python3 git
curl -LsSf https://astral.sh/uv/install.sh | sh
go build -o bin/agentctl ./cmd/agentctl && go build -o bin/store-gateway ./cmd/store-gateway && go build -o bin/control-api ./cmd/control-api
cd pw-executor && npm i && npm run build && npx playwright install chromium && cd ..
cd brain && UV_PROJECT_ENVIRONMENT=../.venv uv sync --frozen && cd ..
export LLM_BACKEND=openai LLM_BASE_URL=http://localhost:11434/v1 LLM_API_KEY=noauth LLM_MODEL_PLANNER=qwen3:14b LLM_MODEL_HEAL=qwen2.5vl:7b
bin/agentctl run --goal "log in with username demo and password demo" --target "file://$PWD/testdata/fixtures/l2.html" --artifact-dir runs/l2
```
The Python virtual environment must be at the repo root (`UV_PROJECT_ENVIRONMENT=../.venv`), otherwise agentctl uses the system python3 and exits with an error.

## M9-LIVE acceptance criteria

- [ ] Phase 1: explore and author via the web UI pass on l1-l6 (grounded, verdict 0, the `planner` field equals `llm`).
- [ ] Phase 2: login, checkout, and the widget scenario pass via the web UI on at least three real sites.
- [ ] a run is configured and observed in the UI: connection to control-api, the live timeline, the run in Tests history.
- [ ] heal fixes the divergence with a correct confidence gate, without a false auto-heal.
- [ ] chat in `/chat/`: the second turn yields a RESUME of the same conversation-id, the conversation is saved in `state\conversations.db`.
- [ ] the golden is byte-stable twice (RISK-009).
- [ ] the budget limit fires with correct degradation.
- [ ] real values for RISK-002 (confidence) and RISK-003 (cost and latency) are collected.
