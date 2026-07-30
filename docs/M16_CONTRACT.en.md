# M16 contract — "everything through the UI": three equal surfaces, and chat at the centre

Proposes **ADR-107…110**. All four are **Proposed**; they enter the `ARCHITECTURE.md §3` table on
acceptance.

The directives that set this contract (Alex, 2026-07-29):

> "EVERYTHING must be possible through the UI. Only the very first launch is from the CLI, without
> flags. Anything doable from the CLI must be doable from the UI."

> "The whole stack, and every part of it, must be configurable whichever way the user picks: entirely
> by API · entirely by UI · entirely by files/env. Not smeared across all of them."

---

## 1. Why

The product states a principle and had never been measured against it. Below is the 2026-07-29
measurement — 49 user capabilities, each row carrying `file:line`; the full table is
`docs/M16_MATRIX.en.md`.

| surface | capability absent | of 49 |
|---|---|---|
| **CLI** (`agentctl`) | 20 | 41 % |
| **HTTP** (control-api) | 18 | 37 % |
| **UI** (hub) | 23 + 5 partial | 47 % / 57 % |
| **present on all three** | **5** | **10 %** |

The gap is **not** that the UI lags the CLI. All three surfaces are holed, each somewhere else:

- `agentctl` is a run-and-artifact tool. Everything about the **store and the config** (listing and
  deleting scenarios/tests/chats, promoting a test, results, trends, logs, reading and writing the
  config, the schema, health) exists on HTTP alone: every one of the 15 "UI-only" rows has `cli = none`.
- HTTP carries no **git channel, no `.spec.ts` export, no operational commands** (quarantine,
  calibration, trace redaction, `purge-store`, `sweep-downloaded`), and has no fields at all for
  **budgets, `auth`, `scenarios`, CI flags** (`cmd/control-api/main.go:1061-1062`).
- The UI cannot do 23 things, does 5 partially, and one of its buttons is dead.

Worst by group: **versioning is 4-for-4 absent from the UI** (`list`/`show`/`diff`/`rollback` exist in
both the CLI and HTTP; the interface has nothing); **import/export is 5-of-6 absent from both UI and
HTTP**; **operations 8-of-9 the same**.

---

## 2. ADR-107 — configuration: one model, three complete projections

### 2.1 The invariant

There is **one** configuration model. CLI, HTTP and UI are its **projections**, not independent
surfaces. A capability available on one projection must be available on all three. The single
exception is bootstrap (§2.5).

The source of truth is the schema `GET /v1/config-schema` already serves
(`cmd/control-api/main.go:306`, unauthenticated). It carries 8 top-level blocks today: `fields`,
`settings`, `llm`, `modes`, `planner`, `roles`, `backends`, `note`; `settings` holds 16 knobs grouped
into `gates`, `healing`, `hitl`, `retention`, each bilingual and each naming its environment variable.

**That is already the foundation.** The work is not to design a surface but to complete the schema and
generate three projections from it.

### 2.2 What the schema lacks

| subject | where it lives today | where it moves |
|---|---|---|
| `auth{storage_state, storage_state_save, login_plan, pw_no_trace}` | `run.yaml` only (`brain/runconfig.py:34-35`) | schema → all three projections |
| `scenarios[{name, goal\|describe}]` | `run.yaml` only (`runconfig.py:36`) | schema → all three projections |
| env allowlist (`SENTINEL_ENV_ALLOWLIST`, `SENTINEL_ENV_ALLOW`) | environment only (`cmd/agentctl/main.go:339,359-363`) | schema, marked security-sensitive |
| `--ci`, `--force-replay`, `--aut-version` | CLI flags only (`main.go:487-489`) | schema + `runRequest` |
| an arbitrary `plan.json` path for replay/baseline | `--plan` only (`main.go:484-485`) | plan upload over HTTP |
| CDP attach, brain interpreter selection | environment only | schema |

### 2.3 What HTTP lacks

`runRequest` (`cmd/control-api/main.go:1061-1062`) carries `Target, Mode, Goal, Describe, Planner,
CoverageTarget, MaxSteps, FromRun, ConversationID, LLM` — and nothing else.

Hence a defect that **cannot be fixed by editing the UI**: the New-run form shows budget and `auth`
fields (`docs/index.html:748-768`), but the Run button's handler (`docs/index.html:2287-2298`) never
reads them. Instead `renderBuild()` (`docs/index.html:1783-1835`) writes them into a `run.yaml` text
block, self-documented as "Pass via: `--run-config <file>`". The interface assembles a file and sends
the person back to the console.

⇒ The `⬇ run.yaml` and `⬇ sentinel.env` buttons are **not a feature and not an export — they are a
patch over a hole in the API contract**. Once `runRequest` is complete they stay, but change meaning
and name: "export this run's configuration" for reproducing it in CI, rather than the mandatory route
to a budget.

### 2.4 What the CLI lacks

Subcommands covering the product's second half:

- `agentctl config schema|get|set`;
- `agentctl scenarios list|show|delete`, `agentctl tests list|show|promote|rename|delete`;
- `agentctl chats list|show|delete`;
- `agentctl results list|show`, `agentctl trends`;
- `agentctl logs --run <id> [filters]`;
- `agentctl health` (the `/healthz` + `/readyz` equivalent).

All are thin clients over the existing routes — never a second implementation.

### 2.5 The one exception — bootstrap

Exactly one thing sits outside the principle: **the first launch of control-api** (listen address,
token source, CORS origins, path to `agentctl`). That is `first-launch-exempt` in the matrix. Today
control-api **accepts no command-line flags at all** and is configured purely by environment
(`CONTROL_API_ADDR`, `cmd/control-api/main.go:1937`). That stays.

### 2.6 Gates

Two, and both are stated as a **property rather than a list** — the `measure-the-capability-not-a-copy`
rule:

1. **Projection completeness.** Every schema key must have a projection in the CLI, in HTTP and in the
   UI. Checked by walking the schema, not by listing known keys — otherwise a new key falls outside
   the check by construction.
2. **Snapshot drift gate — fixed.** `TestSetupWizardSchemaSnapshotMatchesHandler`
   (`cmd/control-api/setup_wizard_test.go:89`) compared the top-level key set and then descended into
   `modes`, `planner`, `backends`, `roles`, `fields` and `llm`. `settings` was in the key set and so
   looked covered, while its contents were never compared. The fix walks both trees.

   It had already let a defect through: the wizard's offline snapshot carried 14 settings against the
   live schema's 16, missing `run_keep` and `run_ttl_hours` — so air-gapped operators could not bound
   run retention at all.

---

## 3. ADR-108 — information architecture: chat at the centre

### 3.1 The three-pane screen — inside the chat tab only

The layout applies **exclusively inside a given chat's tab**. Not in settings, not in the authoring
console, not in tools, not in the library, not in results.

```
┌──────────────┬────────────────────────────┐
│              │  LIVE AREA      [toggle]   │
│ conversation │  mode 1 / 2 / 3            │
│              │                            │
├──────────────┼────────────────────────────┤
│ chat's goal  │  RUN PROGRESS              │
│ (immutable)  │  steps · events            │
│              │  [business] [tool]         │
└──────────────┴────────────────────────────┘
```

### 3.2 The live area and its mode toggle

Three modes, switched by a **toggle with no page reload**:

| mode | what it shows | state |
|---|---|---|
| **1. Browser view** | one frame per action | available today: `page.screenshot()` is deterministic — fixed viewport, DSR=1 (`pw-executor/src/server.ts:404,888,956`) |
| **2. Actions** | the Playwright action list with a DOM snapshot on selection, as in Trace Viewer | needs per-step DOM snapshots streamed rather than frames |
| **3. Video** | a continuous stream from the browser | **not implemented anywhere**: `Page.startScreencast` does not appear in the code; a new subsystem (capture, codec, delivery, storage) |
| *4. Network trace* | requests, as in the browser's Network tab | **desirable, not required** (Alex: "ideally… but never mind") |

Transport is the existing WS `/v1/stream` (ADR-043/054).

**Terminological honesty.** `trace.zip` is Playwright's own format, finalised **when the context
closes** (`pw-executor/src/server.ts:439`); it cannot be streamed by construction. It **stays as it
is** — downloaded and viewed after a run. Live modes 1-3 are a different subject, and calling them
"live trace" in the documentation is forbidden to avoid repeating that conflation.

**Redaction limit.** Pixels are not redactable (ADR-098, `server.ts:432-433`). Modes 1-3 obey the same
switch as trace screenshots (`SENTINEL_TRACE_SCREENSHOTS`) and the no-secrets mode (§3.8).

### 3.3 Run progress, and a visible log split

The lower-right pane shows a run's steps and events with a **visible** business-logic ↔ tool split.

The mechanism is **already built and works end to end**: the `audiences` layer in `brain/events.json`
(`business` = {`application`, `testing`}, `tool` = {`tool`}) → served through `GET /v1/events-catalog`
→ the hub builds `<optgroup>`s (`docs/index.html:3405-3428`), and the filter language accepts an
audience name as the union of its sources (`:3264-3265`). Verified by a live request to control-api.

**What was never built is the presentation.** Today the split exists as a *filter value* in a "Source"
dropdown inside a separate "Logs" tab: to see it you must think to open the tab, expand the list, and
pick "business logic". Nothing on screen says there are two kinds of log. The live timeline has **no
source filter at all**.

⇒ Requirement: the split becomes a **presentation** — two explicit tracks, or a toggle in plain sight —
and it exists where a person actually watches a run.

### 3.4 The goal belongs to the conversation

- The goal is pinned to the **whole conversation**, not to a run.
- **It cannot be changed.** A new goal means a new chat.
- Schema: `chats.last_goal` (a mutable "most recent", `internal/store/server.go:60-63`) becomes an
  immutable `goal`, set once when the conversation is created; an attempt to change it is rejected.

### 3.5 Two entrances, one centre

1. **Through the authoring console.** The console stays. On completion it **hands off into the chat**,
   where the conversation continues and corrections happen.
2. **Straight in the chat.** A new conversation: the person gives a link and describes the task in
   words → every mechanism starts, with no detour through the console.

Working from anywhere means the same chain.

A note on naming: today the tab is declared `data-view="chat"` while its internal name is
`data-subpanel="author"` (`docs/index.html:889`), and its content is an authoring form (mode, target,
"describe the flow") with run buttons. So Run/Re-run/Baseline sit there **appropriately** — the name is
what is wrong. After M16 the two are separated physically: the authoring console and the chat are
different tabs.

### 3.6 No conversation without a goal

If a person simply talks without setting a goal, the model answers by asking for one. Free
conversation without a goal is not a supported product behaviour.

### 3.7 The map gate: report, then ask

After the explore stage builds the map, the tool **analyses the map itself**, reports what it found,
and **asks permission** to go further. No subsequent step runs before an answer.

The seam exists: `SENTINEL_AUTO_HITL_THRESHOLD` already raises `hitl_needed` as a signal, and
takeover/return are implemented (ADR-054, `ws.go:199`). What is new is an **unconditional stop after
explore** (not one driven by a failure counter) and a **substantive report about the map** rather than
a bare "a human is needed".

### 3.8 Artifacts and modes

- Artifacts are **downloaded straight from the chat** and **viewed through a canvas** — a viewing pane
  inside the conversation, as other models' chats offer. The serving side already exists:
  `GET /v1/runs/{id}/artifact` (whitelisted, `cmd/control-api/main.go:1276`).
- **Every mode is a toggle in settings**, no-secrets included. Not an environment variable, not a
  flag: a switch on screen, while keeping full parity with API and files (§2.1).

### 3.9 The exception

Neither the layout nor the live modes apply when a person works **from the browser extension** or has
**handed over their own browser** — there the picture's source is theirs, not ours.

### 3.10 What is new here, and what is completion

**A new subsystem:** a real multi-turn conversation with an LLM. It does not exist as a class today —
`POST /v1/chat/completions` is one turn → one run (ADR-041), and multi-turn `conversation_id` refines a
scenario without holding a conversation (recorded at `BACKLOG.md:82`). This is not an improvement to
the chat; it is the chat's arrival.

**Completion:** the live area (transport exists), run progress (events exist), the log split (mechanism
exists, presentation does not), artifacts (serving exists), the map gate (seam exists).

---

## 4. ADR-109 — local identity in the core; ADR-056 revised

### 4.1 Alex's decision

> "On authorization — things like OIDC go commercial, and for open source we do local authorization."

| subject | licence |
|---|---|
| local accounts, login, each person's own chats/runs/scenarios | **OSS (Apache)** |
| OIDC/SSO/RBAC, multi-tenancy, corporate directories | **COMM** |

### 4.2 This is a data-model change, not middleware

There is no subject **anywhere** today. Verified against the schema (`internal/store/server.go:27-77`):
eight tables (`runs`, `scenarios`, `tests`, `chats`, `results`, `metrics`, `config` + legacy) and **not
one owner column**. `config` is a global key-value for the whole instance. Authentication is a single
shared bearer compared in constant time (`cmd/control-api/main.go:288-292`), fail-closed when unset.
Every occurrence of the word `role` in the code is an ARIA role, a chat message role, or a per-role
model override.

⇒ A subject is required on `runs`, `scenarios`, `tests`, `chats`, `results`, and `config` must split
into global and per-user. Without that, "each person has their own chats" is inexpressible.

### 4.3 Revising ADR-056

ADR-056 currently places **multi-user wholesale** in the commercial reserve ("enterprise-auth
(SSO/RBAC/multi-user)", `ARCHITECTURE.md:187`). The decision in §4.1 contradicts that and **requires
amending ADR-056** rather than citing it. The grounds come from ADR-056's own principle: "open-core =
a COMPLETE useful tool for one team, NOT crippleware" — and a team that cannot separate its members'
data is crippleware.

---

## 5. ADR-110 — delivery

### 5.1 The measurement

The release pipeline is **complete and has never fired**:

| fact | evidence |
|---|---|
| 25 binaries (5 binaries × 5 platforms), a multi-arch image on GHCR, cosign keyless, a CycloneDX SBOM, a GitHub Release with every asset | `.github/workflows/release.yml:1-6,21,100-166` |
| the trigger is a `v*` tag only | `release.yml:13-15` |
| **zero** tags, locally and on the remote | `git tag --list`, `git ls-remote --tags origin` |
| `install.sh` resolves `latest` through the GitHub API | `install.sh:43` — it meets an empty list |
| `Formula/sentinel.rb` is a deliberate `v0.0.0` placeholder with a zero sha256 | `Formula/sentinel.rb:3-4,8,13-18` |
| `docker-compose.yml` only ever builds from source | `docker-compose.yml:17-19` |

⇒ **All three installation paths are dead today, for one shared reason.** This is not construction —
it is **one signed tag**. `workflow_dispatch` runs a dry run (build + SBOM, no push, no signing), so
the pipeline can be proved before the tag.

### 5.2 Scope

1. `workflow_dispatch` dry run → confirm the pipeline is green.
2. The first signed tag `v0.1.0` → a GitHub Release, the GHCR image, a working `install.sh` and brew.
3. `docker-compose.yml` — a variant pulling `image:` from GHCR instead of building.
4. **podman** — check `docker-compose.yml` under `podman-compose` and name the divergences honestly.
5. **deb/apt** — absent today; searched `nfpm`, `goreleaser`, `fpm`, `debian/`, `packaging/` and found
   only `Formula/sentinel.rb`.
6. **An honest banner on GitHub Pages**: a static shopfront must say it is one, and must disable rather
   than merely display controls with no backend behind them.

---

## 6. Defects found by the measurement, to be fixed

| # | defect | evidence |
|---|---|---|
| 1 | **The "👁 Watch" button is dead.** It calls `tSubTab('live')`, which no longer exists — removed by ADR-066. The `ReferenceError` also swallows the `agConnect(id)` that follows it on the same line | `docs/index.html:2860`; the removal comment is at `:2719`; no definition anywhere in the file |
| 2 | **The wizard's schema snapshot drifted** — `run_keep` and `run_ttl_hours` are missing | `docs/setup/index.html` FALLBACK_SCHEMA against `GET /v1/config-schema` |
| 3 | **The drift gate never entered `settings`** — the cause of defect 2 | `cmd/control-api/setup_wizard_test.go:102,115`; `"settings"` appears 0 times in the file |
| 4 | **The log split is built but has no presentation** — a filter in a separate tab instead of a visible division; absent entirely from the live view | §3.3 |
| 5 | **Fragile navigation.** `setView` is declared nested; the call sites at `:1231` and `:1589` resolve only through the global created at `:3890`. They work today but would die silently if that assignment moved. The explicit form was already in use at `:1661` | hygiene, not a bug |

---

## 7. Deferred and open

- **Network trace** (mode 4, §3.2) — desirable; outside M16's required scope.
- **Video (mode 3)** — a new subsystem; needs its own decision on storage and pixel redaction.
- **The backlog register.** `mcp__backlog-mcp__*` is **unusable against this file**: the parser requires
  a line to end with a date in the form `(P2) @agent — YYYY-MM-DD`, and knows only `[ ]`, `[x]`, `[X]`
  (`~/.claude/mcp-servers/backlog-mcp/parser.go:26-36`). Our entries are narrative and three-state
  (`[~]`, 9 of them), so the MCP sees **11 tasks out of 126** and considers Active empty. Until the
  parser is fixed the register is edited by hand. That work lives in the configuration repo, not here.

---

## 8. Acceptance criteria

1. **Projection completeness.** Every schema key has a projection in the CLI, HTTP and the UI, checked
   by walking the schema rather than listing keys. `docs/M16_MATRIX.en.md` is recomputed: zero rows
   verdicted `ui-missing`, `ui-partial`, `ui-only` or `dead-in-ui`, apart from `first-launch-exempt`.
2. **The drift gate** compares the schema's top-level key set and recurses through every block; adding
   a new block without updating the wizard snapshot reddens the test.
3. **Chat.** A multi-turn conversation with the model happens; the goal belongs to the conversation and
   does not change; an attempt to change it is refused by a gate; both entrances (console → chat, a
   link in the chat) reach the same chain; a conversation with no goal answers by asking for one.
4. **The three-pane screen** is present in the chat tab and absent from every other view; the toggle
   switches the live area's modes **without a page reload**.
5. **The log split is visible on screen** during a run, not only as a filter value.
6. **The map gate**: after explore the run stops, reports its reading of the map, and waits for
   permission; without an answer the next step does not run.
7. **Artifacts** download from the chat and open in the canvas.
8. **Identity**: two local users see different sets of chats, runs and scenarios; `config` is split
   into global and per-user.
9. **Delivery**: from a clean machine with no Go/Node/Python, one of the paths (script, brew, image)
   installs and brings up the UI; on GitHub Pages the shopfront is honestly declared.
10. **Defects §6 (1-4)** are closed, each behind a behavioural gate proved by mutation.

---

## 9. Where the measurement came from

The matrix — `docs/M16_MATRIX.en.md`, 49 capabilities, taken on 2026-07-29 by inventorying the CLI (by
invoking `agentctl` and reading the dispatcher at `cmd/agentctl/main.go:905-933`), HTTP
(`cmd/control-api/main.go:1855-1903`), the UI (`docs/index.html`) and the configuration schema (a live
request to `GET /v1/config-schema`). Both dangerous claim classes were run against refutation: "absent
from the UI" (22 rows, none refuted) and "present in the UI" (one correction — retention turned out to
be partial).
