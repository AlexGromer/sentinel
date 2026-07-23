# Threat Model — Sentinel

> 🌐 [Русский](THREAT_MODEL.md) · **English**

> **Version**: 1.2 | **Date**: 2026-07-12 | **Authors**: appsec-engineer (auto), @AlexGromer
> **Methodology**: STRIDE-lite | **Scope**: whitebox, static analysis of source code

---

## 1. Introduction and Scope

**Sentinel** is an autonomous black-box UI tester. It runs as a Go CLI (`agentctl`), spawns a Python process (`brain`) that controls a Playwright server (`pw-executor` / TypeScript) over JSON-RPC/MCP-stdio, which in turn drives a headless Chromium instance pointed at the application under test (AUT).

**What this document covers:**
- The full chain of trust: `host-env → agentctl → brain → pw-executor → Chromium → AUT` and side channels `brain → LLM endpoint` and `agentctl → store-gateway → SQLite`.
- Threats to the confidentiality, integrity, and availability of the system and the data it processes.
- The existing codebase (`main`) only. The planned active security scanning module for the AUT (XSS/CSRF/IDOR) is out of scope.

**What is NOT covered:**
- The infrastructure layer (cluster networking, etcd encryption at rest, IAM — domain of infrastructure/devsecops).
- Dynamic testing / pentesting of the AUT.
- Vulnerability disclosure policies — see [`SECURITY.md`](../SECURITY.md).

---

## 2. Protected Assets

| Asset | Where stored | Confidentiality | Integrity | Availability |
|---|---|---|---|---|
| **LLM API keys** (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `LLM_API_KEY_*`) | Host env, Helm `extraEnv` | **Critical** | High | Medium |
| **DB checkpoint DSN** (`CHECKPOINT_DSN`) | Host env, Helm `checkpointDsn` | **Critical** | High | Medium |
| **AUT credentials** (typed password OR `storageState` file with session tokens) | Env variable `STORAGE_STATE` (path to file) | **Critical** | High | Medium |
| **plan.json / golden baseline** | `runs/<id>/plan.json`, `state/locators.db` → `golden_snapshots` | Medium | **Critical** (plan_hash is verified) | Medium |
| **Run artifacts** (`trace.zip`, `heal-report.json`, `transcript`, `scenario.json`) | `runs/<id>/` on FS / PVC | Medium (UI screenshots, DOM data) | Medium | Low |
| **SQLite locator DB** (`state/locators.db`) | FS / PVC | Low | Medium (affects heal quality) | Medium |
| **LLM endpoint trust** (Anthropic cloud / OpenAI-compat / Ollama/vLLM) | External network / localhost | Medium (AUT page content in prompts) | Medium | Medium |

---

## 3. Trust Boundary (ASCII diagram)

```
┌─────────────────────────────────────────────────────────────────────────┐
│  HOST ENVIRONMENT                                                       │
│  ENV: ANTHROPIC_API_KEY, OPENAI_API_KEY, CHECKPOINT_DSN, ...          │
│                               │ os.Environ() — full inherit (❶)       │
└───────────────────────────────┼─────────────────────────────────────────┘
                                ▼
               ┌─────────────────────────────┐
               │  agentctl  (Go CLI)         │  ← cmd/agentctl/main.go
               │  flag parsing, runID, mkArtifactDir
               └────────┬──────────┬─────────┘
                        │          │ gRPC over Unix socket (❷)
                        │          ▼
                        │  ┌──────────────────────────┐
                        │  │  store-gateway  (Go)     │  ← state/sentinel-store-<id>.sock
                        │  │  PersistenceService gRPC │    state/locators.db (SQLite)
                        │  └──────────────────────────┘
                        │ subprocess + append(os.Environ(),...) (❶)
                        ▼
      ┌─────────────────────────────────────────────────────────┐
      │  brain  (Python, LangGraph StateGraph)                  │
      │  planner.py · healing.py · llm.py · store.py · otel.py │
      │  prompt_hash only in spans, never prompt content        │
      │                │ stdio JSON-RPC / MCP-stdio (❸)        │
      │                ▼                                        │
      │  ┌──────────────────────────────────────┐              │
      │  │  pw-executor  (Node.js / TypeScript)  │              │
      │  │  Playwright API, newContext            │              │
      │  │  PW_NO_TRACE=1 on auth runs            │              │
      │  │  no ignoreHTTPSErrors (by design)      │              │
      │  │                │ Playwright API (❹)    │              │
      │  │                ▼                       │              │
      │  │         Chromium  (headless)            │              │
      │  │                │ HTTP/S (❺)             │              │
      │  │                ▼                       │              │
      │  │          AUT  (app under test)          │              │
      │  │          TLS cert errors: unclassified  │              │
      │  └──────────────────────────────────────┘              │
      │                                                         │
      │  LLM calls per role (❻)                                │
      │  ┌───────────────────────────────────────────────────┐ │
      │  │ AnthropicBackend    → api.anthropic.com (HTTPS)   │ │
      │  │ OpenAICompatBackend → OpenAI / OpenRouter / cloud │ │
      │  │                    → localhost Ollama / vLLM      │ │
      │  │ SamplingBackend     → MCP host (M7)               │ │
      │  └───────────────────────────────────────────────────┘ │
      └─────────────────────────────────────────────────────────┘

Artifacts → runs/<id>/ : plan.json, transcript, heal-report.json,
                          scenario.json, reconcile-report, trace.zip (❼)
```

Boundary points ❶–❼ correspond to rows in the table below.

> **New surfaces (M9.6/M9.8/M9-LIVE-prep), not shown in the diagram above (optional/dev-only/future):** ❽ **CDP-attach** to the user's browser (M9.6, opt-in `PW_CDP_ENDPOINT`), ❾ **browser extension** (M9.8, implemented — `extension/`, dev-only), and ❿ **live-run artifact export** (`scripts/collect-live-run.sh`, M9-LIVE-prep) — see §4.8 / §4.9 / §4.10.
>
> **Planned in-tool surfaces (ADR-046):** (a) **replay/baseline control-API endpoint** (M9.9/R1) — re-opens a boundary-❶-class spawn surface → mitigation: `from_run:<run_id>` + the artifact whitelist+traversal-guard only (never an arbitrary path). **[R1a ✅ shipped in `cmd/control-api`: `resolveFromRun` (guards `/`,`\`,`..` + `{plan.json\|scenario.json}` whitelist) + httptests for traversal/missing-plan.]** (b) **multi-turn conversation-state** (M9.10/R2) — a new asset: confidentiality of accumulated AUT context + DoS via unbounded state → mitigation: per-session cap + 0700 isolation. (c) **AG-UI npm front** (`frontend/`, ADR-044) — npm supply-chain (compounds GAP-SEC-002) + a browser token → mitigation: dev-only/not-air-gapped, token server-side in the Runtime.
>
> **Rich-UI/Persistence/Metrics epic surfaces (M13-15, ADR-049..053):** (d) **persistence DB with user content** (scenarios/chats/results — accumulated AUT context / possible PII) → confidentiality + at-rest + access-control: reuse `0700`/per-run token/SO_PEERCRED (SQLite, standalone), Postgres → standard authn + a secret via `secretKeyRef` (ADR-035); DoS via unbounded state → **cap+summary shipped (M13 w5, GAP-M9-20 ✅: `_capped_history`/`_rolling_summary`)**; retention → M13-service. (e) **always-on control-plane bind** (service profile) → reuse ADR-032 (localhost-default + bearer + CORS allowlist); a public bind = opt-in+warn; the service mode adds an authN/RBAC consideration on the CRUD endpoints. (f) **self-contained metrics** (ADR-051) — metrics in our own DB + native render ⇒ **reduces** the surface vs a Grafana embed (no external render / iframe trust). (g) **rich AG-UI over WS** (M14) → reuse the ADR-043 WS token (`Sec-WebSocket-Protocol`) + npm supply-chain (GAP-SEC-002, a not-air-gapped dev build). (h) **recorder session-resume `/v1/stream?session=`** (M13 R3-hardening, ✅ shipped) — user input into a write-path construction → mitigated by `filepath.Base`+the `validRunID` charset (2 CodeQL `go/path-injection` alerts dismissed as false-positive, the sanitizer kept as defense-in-depth); Origin fail-closed on a public bind. (i) **live-run artifact export** (`scripts/collect-live-run.sh`, M9-LIVE-prep, `docs/M9_LIVE_PLAN.md` §C) — a new egress boundary; the staging copy is redacted by default + `checkpoint.db`/`storage_state*.json` are unconditionally excluded; `trace.zip` is opt-in and shipped unredacted → see §4.10 (GAP-SEC-003/GAP-SEC-004/GAP-OPS-006).

---

## 4. STRIDE-lite: Threat Table

> **Notation**: Prob(ability) H/M/L without existing controls; Impact H/M/L on assets.
> GAP-IDs correspond to entries in BACKLOG/GAPS.

### 4.1 Boundary ❶ — host-env → agentctl → brain (full env inherit)

| Threat | Boundary | STRIDE | Prob / Impact | Existing control | Residual risk | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Leakage of all host secrets to child processes.** Before M11.3, `agentctl::spawnBrain` called `cmd.Env = append(os.Environ(), …)` without an allowlist (historical citation; that spot now holds `filteredEnv()`, cmd/agentctl/main.go:172–219). All host variables (SSH keys, cloud credentials, tokens unrelated to Sentinel) are inherited by the Python brain, Node.js pw-executor, and their subprocesses, and may also surface in stderr on error. | host-env → brain subprocess | **I** (Information Disclosure) | Prob: H / Impact: H | **MITIGATED (M11.3/ADR-035):** env-allowlist default-on (`filteredEnv`; opt-out `SENTINEL_ENV_ALLOWLIST=0`) | **GAP-SEC-001 CLOSED (Helm-half)**; residual — dynamic Vault/CSI | M11.3 ✅ |
| **Plaintext secrets in Helm values → Kubernetes.** `cronjob.yaml:39–46` uses `value: {{ .Values.checkpointDsn }}` and `{{range .Values.extraEnv}} value: {{ $v }}` without `secretKeyRef`. `CHECKPOINT_DSN` and `extraEnv` are stored as plain strings in `values-prod.yaml`, land in etcd in plaintext, and are visible via `kubectl describe pod`. | Helm chart → K8s etcd | **I** (Information Disclosure) | Prob: H / Impact: H | **MITIGATED (M11.3/ADR-035):** `secretKeyRef` plumbing (chart `secrets.*`) | **GAP-SEC-001 CLOSED (Helm-half)** | M11.3 ✅ |

### 4.2 Boundary ❷ — agentctl → store-gateway (Unix gRPC socket)

| Threat | Boundary | STRIDE | Prob / Impact | Existing control | Residual risk | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Unauthorized access to the Unix socket.** Any local process running under the same UID can invoke store-gateway gRPC methods — writing or deleting the golden baseline or locator cache without authentication. | local FS / Unix socket | **E** (Elevation of Privilege) | Prob: L / Impact: M | **MITIGATED (#23):** per-run shared secret in gRPC metadata (agentctl mints the token → gateway+brain; `TokenAuthInterceptor`, constant-time) — calls without a valid token are rejected (`codes.Unauthenticated`). Defense-in-depth: `0600` socket + SO_PEERCRED (rejects a foreign UID). Socket in `state/` (not `/tmp`); gRPC exposes only `PersistenceService`. | A same-UID process can read the token from `/proc/<brain>/environ` during the short-lived run window (the classic Unix same-UID boundary). No mTLS. SO_PEERCRED is Linux-only: on non-Linux platforms `PeerCredListener` is a no-op (`internal/store/peercred_other.go`), so the defense rests on the token + socket permissions alone. | **#23 MITIGATED** |
| **Golden baseline tamper via direct SQL.** If permissions on `state/locators.db` are insufficiently restrictive, a local attacker can overwrite `golden_snapshots` records and trigger a false regression result. | FS → SQLite | **T** (Tampering) | Prob: L / Impact: M | **MITIGATED (#24):** `golden_snapshots` rows carry an HMAC-SHA256 (`mac` column) keyed by `state/golden.key` (0600, outside the DB); pre-#24 rows are MAC'd once at upgrade (trust-on-first-use), then every read **requires** a valid MAC — a missing/invalid one (strip, inject, DB swap) → controlled **exit 3** (hard-abort). `plan_hash` is still verified; `locators.db` perms → `0600` (best-effort). | Tamper / edit / MAC-strip while `golden.key` persists is detected. Residual: a same-UID attacker who can read or delete the 0600 key can re-MAC a forgery or reset TOFU by deleting the key. | **#24 MITIGATED** |

### 4.3 Boundary ❸ — brain → pw-executor (stdio JSON-RPC / MCP-stdio)

| Threat | Boundary | STRIDE | Prob / Impact | Existing control | Residual risk | Owner / Milestone |
|---|---|---|---|---|---|---|
| **RPC method or parameter substitution.** Brain passes `method`/`params` over stdio. A compromised brain can invoke any `dispatchInner` method, including `browser.fill` with arbitrary data into the AUT. | brain stdio → pw-executor | **T** (Tampering) | Prob: L / Impact: M | `dispatch` routes only documented `TOOL_METHODS` (switch-case in `dispatchInner`); unknown methods → error. Both processes share one container and one security context. | No RPC frame signing. The boundary is protected only by process isolation. | dev / not prioritized |

### 4.4 Boundary ❹/❺ — pw-executor → Chromium → AUT

| Threat | Boundary | STRIDE | Prob / Impact | Existing control | Residual risk | Owner / Milestone |
|---|---|---|---|---|---|---|
| **AUT TLS cert error is not classified.** `browser.newContext` (`server.ts:100`) does not set `ignoreHTTPSErrors`. When a cert is expired or self-signed, Chromium returns a generic navigation error without indicating the cert as the cause. | pw-executor → AUT HTTPS | **D** (Denial of Service / diagnostic) | Prob: M / Impact: M | Explicit architectural decision: do not ignore cert errors (security best practice). `browser.navigate` returns `{ status: null }` on navigation failure. | **GAP-OPS-002 OPEN**: the operator sees `NavigationError` rather than `NET::ERR_CERT_DATE_INVALID`. No actionable cert diagnostic in heal-report. | M9.4 |
| **AUT DOM-based adversarial content in LLM prompts.** The AUT can place specially crafted element names or text nodes in the DOM that flow into the planner/heal prompt via `ariaSnapshot → candidates`, potentially influencing LLM behaviour. | AUT DOM → brain LLM prompt | **T** (Tampering) | Prob: M / Impact: M | **Partially mitigated**: `LLMPlanner` / `GoalPlanner` use index-pick grounding (ADR-022/027): the LLM selects an INDEX within the `candidates[]` array built by the deterministic `plan` node — the LLM cannot generate an arbitrary selector. `DescribePlanner` produces a `hypothesized_target` by role/name/text followed by reconcile-matching against real elements. | Adversarial content may influence index selection but cannot escape the set of discovered elements. The heal prompt (`healing.py:122`) passes `interactives[][:3000]` containing DOM names — element names enter the LLM without sanitisation. | dev / M10 (prompt sanitization) |
| **Fingerprinting / rate-limiting of the headless Chromium UA.** The AUT may detect the Playwright User-Agent and serve a simplified DOM or deny access. | Chromium → AUT | **I** / **D** | Prob: M / Impact: L | No specific controls. UA can be configured via `extraEnv`, which is out of scope for this document. | False test results — a quality threat to Sentinel, not a security threat. | ops / documented |
| **PII leakage from AUT UI into artifacts.** `trace.zip` contains DOM snapshots and screenshots; if the AUT displays personal data, it is persisted under `runs/`. | AUT DOM → runs/trace.zip | **I** (Information Disclosure) | Prob: H / Impact: M | **MITIGATED (#26):** `runs/` and `runs/<id>/` are created `0700` (owner-only) — other local users can't read `trace.zip`; retention in `agentctl` keeps `trace.zip` only for the newest `SENTINEL_TRACE_KEEP` runs (default 10) + a `SENTINEL_TRACE_TTL_HOURS` TTL. **Auth runs (GAP-RISK-010):** `PW_NO_TRACE=1` — tracing not started (`server.ts:108`), passwords not in the trace; prod uses `storageState`. brain logs only `prompt_hash`. | Within the retention window `trace.zip` holds DOM+screenshots (determined by the AUT); encryption-at-rest / PII redaction is optional in #26 (not implemented, on the AUT owner). Same-UID access is not restricted. | **#26 MITIGATED** (perms+retention) |

### 4.5 Boundary ❻ — brain → LLM endpoint (cloud / local)

| Threat | Boundary | STRIDE | Prob / Impact | Existing control | Residual risk | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Leakage of AUT page content to a cloud LLM provider.** The planner prompt contains `current_url`, element names, and intent; the heal prompt contains `interactives[]` (DOM elements, up to 3,000 chars). With a cloud backend, all of this is transmitted to the Anthropic API / OpenAI / OpenRouter. | brain → cloud LLM HTTPS | **I** (Information Disclosure) | Prob: H (with cloud backend) / Impact: M | Tracing: `prompt_hash()` (`otel.py:14`) — SHA-256 first 16 hex of the prompt, never the content. Span attributes store only token counts. Prompts are not logged to brain stderr. `LLM_BASE_URL` allows switching to a local Ollama/vLLM for data residency. | With a cloud backend, AUT page structure (URLs, element names) is sent to the provider. No DLP filtering of prompts. Data residency is guaranteed only with a local endpoint. | ops / documented (backend choice) |
| **LLM response compromise (malicious backend / MITM).** `OpenAICompatBackend` makes HTTPS calls to `base_url`. A compromised or MITM-intercepted endpoint can return a forged response. | brain → openai-compat endpoint | **T** (Tampering) / **S** (Spoofing) | Prob: L / Impact: M | TLS (HTTPS to external endpoints). Index-pick grounding limits impact: a malicious index will cause a click on the wrong element, but not RCE. An out-of-bounds index → brain degrades to `done` (`planner.py:97`). | No certificate pinning for cloud endpoints. | dev / post-M10 |
| **LLM token budget exhaustion.** An AUT with deep navigation or adversarial DOM can drive high token consumption and financial loss. | brain → LLM billing | **D** (Denial of Service / cost) | Prob: M / Impact: M | **Mitigated** (ADR-021, `budget.py`): `PLAN_TOKEN_LIMIT` (default 50,000), `HEAL_TOKEN_LIMIT` (default 20,000), `TOTAL_TOKEN_LIMIT` (default 0 = off). On budget exceeded → fallback to heuristic/L1–L6, run continues. | Financial loss if limits are disabled or the AUT is very large. | ops / documented |
| **SSRF via the UI-configurable `base_url` (ADR-063).** Token-gated `POST /v1/runs` (and `PUT /v1/config`) accept `llm.base_url`, which the control-API materializes into the run env → the brain makes an HTTP request to it. A compromised/malicious authenticated caller could point a run at an internal address (cloud-metadata `169.254.169.254`, internal services). | UI → control-API → brain → endpoint | **I** (SSRF) / **E** | Prob: L (token-gated) / Impact: M | **Mitigated (ADR-063):** `validateLLMBase` (shared with `probeLLM`) — absolute http(s) only, rejects `user:pass@` (else credentials would be sent outbound), blocks literal link-local (`169.254.0.0/16`, `fe80::/10`). RFC1918/loopback (homelab `ollama`/`vllm`/`llama.cpp`) are allowed. Secrets never travel in the run body or persisted config (`configguard` rejects `api_key`-shaped keys; `noauth` is defaulted for local). Precedence process env > per-run > persisted — the operator env is never overridden. | Validates the literal IP, not DNS-rebinding (as with `probeLLM`); the hostname is not resolved. | dev / post-M10 |

### 4.6 Supply chain (cross-cutting)

| Threat | Boundary | STRIDE | Prob / Impact | Existing control | Residual risk | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Python dependencies without a lockfile.** `brain/pyproject.toml` declares dependencies (`langgraph`, `anthropic`, `openai`, `mcp`, `pyyaml`, `opentelemetry-*`) without a `uv.lock` or hash-pinned requirements. `pip install` in CI without `--require-hashes` is vulnerable to dependency confusion and typosquatting on PyPI. | CI/CD → PyPI | **T** (Tampering) / **E** (Elevation) | Prob: M / Impact: H | Go modules are protected by `go.sum` (content hash verification). Playwright 1.61.1 is pinned in TS. **§1 (this cycle):** gitleaks/govulncheck/pip-audit/npm audit added to CI (pip-audit advisory + freeze artifact `requirements.lock`); committed lockfile/SBOM/cosign remain for M11.1. | **GAP-SEC-002 PARTIALLY OPEN**: SCA/SBOM/lockfile in progress for CI, but Python lockfile is not yet committed to the repo. | M11.1 |
| **No SBOM and no container image signing.** The production image has no attached SBOM and no cosign signature — composition cannot be verified at runtime. | Registry → K8s | **T** (Tampering) | Prob: L / Impact: H | None | **GAP-SEC-002 OPEN**: no SBOM generation in CI pipeline. | M11.1 |

### 4.7 Artifacts ❼ — `runs/` (integrity and audit)

| Threat | Boundary | STRIDE | Prob / Impact | Existing control | Residual risk | Owner / Milestone |
|---|---|---|---|---|---|---|
| **plan.json tampering before replay.** If an attacker modifies `plan.json` on disk between authoring and replay, brain will execute the altered steps. | FS → brain replay | **T** (Tampering) | Prob: L / Impact: M | `plan_hash` is verified before replay; mismatch → exit code 3. In K8s the plan is mounted from a ConfigMap. `--ci` disallows `--force-replay`. | `plan_hash` is a hash of `plan.json` itself, not an HMAC with a key: if the file is replaced, the hash is replaced along with it. Protects against accidental corruption but not deliberate substitution. | dev / low priority |
| **No audit trail for the run initiator.** Brain logs contain `prompt_hash` (not content) and step outcomes, but there is no record of who initiated the run, with which plan, in which environment. | brain → runs/transcript | **R** (Repudiation) | Prob: M / Impact: L | `run_id` is present in all artifacts; the `healing_audit` table in SQLite stores the full heal history. | No signed audit log. `run_id` is random hex, not linked to user identity in K8s (CronJob is not bound to a human identity). | ops / post-M10 |

### 4.8 Boundary ❽ — CDP-attach to the user's browser (M9.6)

> A new surface (M9.6, ADR-037). Active ONLY with `PW_CDP_ENDPOINT` — Sentinel connects to the user's **existing** Chrome (`--remote-debugging-port`) and drives their live session (their cookies/login), not its own headless instance.

| Threat | Boundary | STRIDE | Prob / Impact | Existing control | Residual risk | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Recording the user's live session into trace.zip.** In CDP mode the env-gated tracing + traceparent route apply to the user's adopted context → their DOM/screenshots/requests may land in `runs/<id>/trace.zip`. | user browser → runs/ | **I** | Prob: M / Impact: M | `PW_NO_TRACE=1` disables tracing; disclosed in `M9.6_CONTRACT` + code comments; CDP mode is opt-in. **#26:** `runs/` `0700` + `trace.zip` retention also apply to CDP traces. | Tracing is on by default (unless `PW_NO_TRACE`); the user may not expect a recording, but the artifact is owner-only and subject to retention. | M9.8 / docs (#26 perms+retention done) |
| **Access to someone else's session via an unprotected CDP port.** A CDP endpoint without authentication = any local process can drive the browser; Sentinel reuses the user's login. | local → CDP `:9222` | **E/I** | Prob: L / Impact: H | The user brings up the CDP port deliberately (opt-in); localhost-only. | The DevTools protocol has no authN — exposing the port = full browser control. **Do not expose the port externally.** | user / docs |

### 4.9 Boundary ❾ — browser extension (M9.8, IMPLEMENTED — `extension/`, #42-47 + R3 brain-side, ADR-054)

> **Implemented** (`extension/`, dev-only like `frontend/`) + **brain-side takeover/return** (R3, ADR-054). The project's largest surface expansion: an MV3 extension living in the user's browser.

| Threat | Boundary | STRIDE | Prob / Impact | Control | Residual risk | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Reading all of the user's pages.** A content-script recorder sees DOM/input on an in-scope page; recorded events go to the brain. | all pages → brain | **I** | Prob: H / Impact: H | Minimal permissions (`activeTab`/`storage`/`scripting`); **host access is optional, requested on the record-start gesture** (per-origin, not `<all_urls>`); the recorder is injected only on explicit start; local transport. **Mandatory redaction**: `type=password`, `autocomplete` cc/one-time-code/password, `data-sentinel-secret`, secret-ish names (token-anchored) → records a `secretRef`, never the value (selectors.ts, jsdom tests). | An extension inherently sees everything in the active tab; trust = code review. | **#42/#44 DONE** |
| **`chrome.debugger` = full CDP.** Takeover via the debugger API gives full page control. | extension → page | **E** | Prob: M / Impact: H | `debugger` is an **optional permission requested lazily on the takeover gesture** (not at install); Chrome's visible banner; auto-detach on return; reconcile of stale attachments after SW eviction. **Caveat:** if DevTools is open on the tab, a second debugger can't attach — the attach fails and the error surfaces in the panel (no half-state). | Broad powers, albeit with a banner. | **#47 DONE / ADR-039** |
| **extension↔brain transport.** The streaming channel is an injection/interception point. **Implemented (M9.8-prep, ADR-043):** hand-rolled WS `GET /v1/stream` (client→server recorder ingest). | extension → control-API | **S/T** | Prob: M / Impact: M | Reuse ADR-032: localhost-bind + bearer + **Origin allowlist (CSWSH defense)**. Token in `Sec-WebSocket-Protocol` (`bearer.<token>`, constant-time) — a browser WS can't send `Authorization`; the server echoes back **only** the non-secret subprotocol `sentinel.recorder.v1`. The client **refuses to send the token over plaintext `ws://` to a non-loopback host** (requires `wss`). native-messaging is the stdio alternative (no port, not implemented). | Token in the browser; WS port on localhost. | **M9.8-prep / GAP-M9-14 DONE** |
| **Takeover/return — mutating another run.** **Implemented (R3, ADR-054):** an authenticated `/v1/stream` client sends `{"type":"control","action":"takeover\|return","run_id":X}` → the control-API forwards `Takeover`/`Return` to the orchestrator for ANY `run_id`; there is NO `run_id`↔WS-session/`s.runs` binding. | extension → control-API → orchestrator → brain | **S/E** | Prob: M / Impact: M | Same gate as the transport (localhost-bind + bearer + Origin allowlist); `run_id` is format-guarded (`validRunID`: charset + ≤64) against map-key injection; `abort > takeover` (a hard stop beats a pause); control frames have their own per-session cap. | **Cross-run authorization gap:** a token holder (e.g. a compromised extension / malicious tab) can pause/resume an unrelated run. The ownership binding (`run_id`↔session) is deferred to the per-run socket discovery in **M9-LIVE**. | **R3 (ADR-054) DONE; ownership → M9-LIVE** |

> **Follow-up (review #42-47, defense-in-depth, server-side `cmd/control-api/ws.go`):** (1) the `/v1/stream` Origin check is active only when `corsAllow` is non-empty — with an empty allowlist the sole gate is the bearer (fine on a localhost bind, but it should explicitly allow only `chrome-extension://` + loopback). (2) Reconnect mints a **new** `record-<session>` per connection (`newRunID`), so a drop mid-recording fragments it across two `events.ndjson` — the extension now **surfaces** this in status, but there's no server-side session-resume yet (an R3 candidate). The bearer is fail-closed, so these are defense-in-depth, not holes.

### 4.10 Boundary ❿ — live-run artifact export (`scripts/collect-live-run.sh`, M9-LIVE-prep)

> A new egress boundary (M9-LIVE-prep, `docs/M9_LIVE_PLAN.md` §C): `scripts/collect-live-run.sh` bundles `runs/<id>/` into `live-<id>.tar.gz` for transfer to an analysis machine (USB/scp, never git). Redaction is on by default and applies to a staging copy — `runs/` is never modified.

| Threat | Boundary | STRIDE | Prob / Impact | Existing control | Residual risk | Owner / Milestone |
|---|---|---|---|---|---|---|
| **LLM authoring cannot emit `secretRef` → plaintext credentials in plan.json/scenario.json.** The authoring schemas (`brain/planner.py` `_SCHEMA_STEPS`/`_SCHEMA_DRAFT`) carry only `value`, with no `secretRef` — a goal like "log in as user/password" materialises as a literal password in the artifacts (and in `trace.zip`, if included). | brain authoring → runs/<id>/{plan,scenario}.json → export | **I** (Information Disclosure) | Prob: H / Impact: H | **MITIGATED at the export boundary:** `collect-live-run.sh` blanks `value`/`text` on `fill\|type\|select\|press` steps without a `secretRef` (structural layer, staging copy) + a textual sweep for auth headers/token shapes (Bearer, sk-/ghp_/AKIA/JWT); the `collect-live-run-smoke` CI job proves it with a canary. | The root cause is not fixed: `runs/<id>/plan.json` itself stays plaintext on disk — mitigation is only at the export boundary, not at authoring time. Known ceiling: a shapeless, keyword-less secret in a non-secret-named free-text field (e.g. `reason`) survives the textual sweep. | **GAP-SEC-003** — M9-LIVE / M10 |
| **`STORAGE_STATE_SAVE` is not guarded against writing inside `runs/<id>`.** The path comes from the caller (`brain/runconfig.py` → `pw-executor/src/server.ts`), with no code-level "not into the artifact dir" barrier. A Playwright storage state is auth cookies + localStorage (session-hijack material). | brain/runconfig.py → pw-executor write path → export | **I** (Information Disclosure) | Prob: L / Impact: H | **MITIGATED at the export boundary:** the collector unconditionally excludes `*state*.json` (even with `--with-trace`) and warns loudly when one is present. | No write-time barrier exists yet — a naive `tar runs/<id>` bypassing the collector would still ship the file. | **GAP-SEC-004** — M10 |
| **No on-disk "run finished" marker.** Neither `agentctl` nor the brain writes a completion signal (`report.json`/`report.html`/`metrics.prom` are produced by a separate `report` subcommand) → "a run that died at step 3" and "a run still in flight" are indistinguishable on disk. | agentctl/brain → runs/<id>/ → collector / M15 dashboard | **D** (diagnostic ambiguity) | Prob: M / Impact: L | Deliberate: the collector warns about missing artifacts but never fails (failing would be a false positive on a legitimately mid-flight run). | An operator/dashboard cannot tell a crash from in-flight without manual inspection. | **GAP-OPS-006** — post-M9-LIVE |

---

### 4.11 Boundary ⓫ — control-API as the only service: serving the UI + handing out the token (ADR-064)

> A new boundary (ADR-064): in **mode 3** control-API serves the browser UI from its own port (`CONTROL_API_SERVE_UI=1`) and provisions its own bearer token. Modes 1 (headless) and 2 (`webui` :8088 + API :8090) are unchanged — with `CONTROL_API_SERVE_UI`/`CONTROL_API_UI_DIR` empty, nothing from this layer is registered. Same-origin requests are not CORS requests, so mode 3 can run with an emptied allowlist (`CONTROL_API_CORS_ORIGINS=`) — a strictly **smaller** surface than mode 2.

| Threat | Boundary | STRIDE | Lik / Impact | Existing mitigation | Residual risk | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Handing the token to whoever reaches the port.** If the page received its token by injection into the served HTML (or from an unconditional endpoint), "port reachability" would equal "token possession" and the ADR-032 bearer gate on mutations would stop meaning anything. | client → control-API (`GET /`, `GET /v1/ui-token`) | **S/E** | Lik: M / Impact: H | **Mitigated (ADR-064):** the token is handed over ONLY in exchange for a single-use nonce minted at startup and printed exclusively to the process's stderr (the operator's terminal). `subtle.ConstantTimeCompare`; burned on success, on TTL expiry (`CONTROL_API_UI_BOOTSTRAP_TTL`, default 5 min) and after 5 wrong guesses; `Cache-Control: no-store`; the `sameOriginRequest` guard (`Sec-Fetch-Site` + `Origin` host match) rejects a cross-site page; with `s.token == ""` the endpoint is not registered at all. The page keeps the token in tab memory and strips the nonce from the URL (`history.replaceState`) — neither `localStorage` nor history. | Anyone who reads the process's stdout/stderr (a shared journal, `docker compose logs`, a CI log) within the TTL can redeem the nonce before the operator does. A deliberate trade: the operator's terminal is already the delivery channel in modes 1-2. | ADR-064 / M10 |
| **The token on disk.** `state/control-api.token` is a long-lived secret that survives restarts, on the `./state` directory shared with the container. | control-API → filesystem | **I** | Lik: L / Impact: M | The write is atomic (temp+rename) and `0600` from creation (`os.CreateTemp`); the directory is created `0700`; `state/` is in `.gitignore`, so the file cannot reach a commit. An existing but unusable/unreadable file is NEVER overwritten (it may be operator data behind `CONTROL_API_TOKEN_FILE`) — the process falls back to a throwaway in-memory token and warns. `CONTROL_API_AUTOTOKEN=0` restores the fully tokenless read-only instance. | On Windows Go's `0600` maps onto ACL semantics rather than POSIX bits — treat the file as user-scoped, do not rely on the bits. No at-rest encryption (the same level as `state/golden.key`). | ADR-064 / M10 |
| **Serving files out of `docs/`.** `docs/` holds gitignored INTERNAL-ONLY material (`*.internal.md`, `COMPETITIVE_ANALYSIS.raw.internal.json`) next to the public pages. A naive FileServer — especially the dev `CONTROL_API_UI_DIR` source, which points at the real tree — would serve them; a wildcard `go:embed` would bake them into a maintainer-built binary while CI stayed entirely green. | client → control-API → embed.FS / disk | **I** | Lik: M / Impact: M | An explicit allowlist in `docs/embed.go` (`index.html`, `prices.json`, `backend-presets.json`, `setup/`, `chat/`, `calculators/`) **plus** the runtime `uiPathAllowed` filter over both sources, plus a blanket refusal of directory listings (a listing is the only way to disclose sibling names). Gates: `TestEmbeddedUIHasNoInternalDocs` and `TestUIDiskSourceFiltersInternalDocs`. Prose `*.md` is not served at all — the UI links it to GitHub. | The allowlist is maintained by hand: a new public asset must be added explicitly (otherwise 404 — a safe failure). | ADR-064 / done |

---

## 5. GAP Tracking Summary Table

| GAP ID | Status | STRIDE | Severity | Short description | Owner / Milestone |
|---|---|---|---|---|---|
| **GAP-RISK-010** | **MITIGATED** | I | — | Leak-in-trace: tracing disabled (`PW_NO_TRACE`) on auth runs; secrets referenced by env-var NAME via secretRef; brain redacts logs; fail-closed on active tracing; prod uses storageState. | — |
| **GAP-SEC-001** | **CLOSED — Helm half + #25 (M11.3/ADR-035)** | I | HIGH | env-allowlist **default-on** (opt-out `SENTINEL_ENV_ALLOWLIST=0`) + Helm `secretKeyRef` + `sentinel.envAllow`. **#25 CLOSED:** `NODE_`/`GIT_` are no longer prefixes — `NODE_OPTIONS`/`NODE_EXTRA_CA_CERTS`/`GIT_SSL_CAINFO`/`GIT_SSL_CAPATH` are exact-allowlisted (`TestFilteredEnvPrefixNarrowing`). **Remainder:** only dynamic Vault/CSI-driver secrets. | done |
| **#23 store-gateway authN** | **MITIGATED** | E | MEDIUM | per-run token authN in gRPC metadata (`TokenAuthInterceptor`) + SO_PEERCRED + 0600 socket; unit test `TestTokenAuthInterceptor`. | done; #23 → 0xCoDSnet |
| **#24 golden integrity** | **MITIGATED** | T | MEDIUM | HMAC on `golden_snapshots` (key `state/golden.key`, outside the DB); tamper → exit 3; tests `TestGoldenIntegrityTamper` + `test_golden_mac_tamper_detected_exit3`. | done; #24 → 0xCoDSnet |
| **#26 trace.zip PII** | **MITIGATED** | I | MEDIUM | `runs/` + `runs/<id>/` → `0700` (owner-only); `trace.zip` retention (`SENTINEL_TRACE_KEEP`=10 / `SENTINEL_TRACE_TTL_HOURS`); tests `TestMkArtifactDirPerms`/`TestSweepTraces*`. Encryption/redaction optional, not implemented. | done; #26 → 0xCoDSnet |
| **GAP-SEC-002** | **PARTIALLY OPEN** | T, E | HIGH | Python no lockfile, no SBOM, no image signing. | M11.1 |
| **GAP-OPS-002** | **MITIGATED** | D | MEDIUM | `PW_IGNORE_HTTPS_ERRORS` opt-in + cert classification (`ERR_CERT*`) in `browser.navigate` (this cycle); strict by default. Richer diagnostic in heal-report — M9.4. | M9.4 |
| **GAP-SEC-003** | **MITIGATED (export boundary)** | I | MEDIUM | `scripts/collect-live-run.sh` blanks `value`/`text` on `fill\|type\|select\|press` steps without a `secretRef` (structural blanking + textual sweep on the staging copy); CI canary `collect-live-run-smoke`. Root cause (authoring schema has no `secretRef`) remains open. | M9-LIVE / M10 |
| **GAP-SEC-004** | **MITIGATED (export boundary)** | I | MEDIUM | The collector unconditionally excludes `*state*.json` (even with `--with-trace`) + warns loudly. No code-level write-path guard on `STORAGE_STATE_SAVE` yet. | M10 |
| **GAP-OPS-006** | **OPEN** | D | LOW | No on-disk run-finished marker; the collector/future M15 dashboard can't distinguish a crash from an in-flight run (the collector deliberately warns rather than fails on missing artifacts). | post-M9-LIVE |

---

## 6. Recommended Controls (Roadmap)

The following controls are **not yet implemented** in the current codebase. Listed as planned/milestone items.

1. ~~**GAP-SEC-001 — env allowlist**~~ — **DONE (M11.3 / ADR-035):** `filteredEnv()` flipped to default-on (opt-out `SENTINEL_ENV_ALLOWLIST=0`) + a curated list. **#25 CLOSED:** `NODE_`/`GIT_` removed as prefixes — `NODE_OPTIONS`/`NODE_EXTRA_CA_CERTS`/`GIT_SSL_CAINFO`/`GIT_SSL_CAPATH` are exact-allowlisted (`TestFilteredEnvPrefixNarrowing`, cmd/agentctl/main.go:190–191). **Remainder:** only dynamic Vault/CSI-driver secrets — open.
2. ~~**GAP-SEC-001 — Helm secretKeyRef**~~ — **DONE (M11.3):** `secrets.*` → `valueFrom.secretKeyRef` when `secrets.enabled` (plaintext fallback in dev); the `sentinel.envAllow` helper; `deploy/flux/`.
3. **GAP-SEC-002 — Python lockfile**: add `uv lock` to CI, commit `uv.lock` to the repo, use `uv sync --frozen` or pip with `--require-hashes` in the Dockerfile.
4. **GAP-SEC-002 — SCA + SBOM + image signing**: add a Trivy/Grype SCA scan to the CI pipeline; `syft` for SBOM generation; `cosign` for image signing.
5. ~~**GAP-OPS-002 — cert diagnostic**~~ — **DONE:** cert classification (`ERR_CERT*`/`ERR_SSL*`) in `browser.navigate` + opt-in `PW_IGNORE_HTTPS_ERRORS` (strict by default).
6. **Prompt sanitization**: strip control characters and limit the length of element names/intent before including them in LLM prompts (`healing.py:_llm_reground`, `planner.py:propose`).
7. ~~**`runs/` access control**~~ — **DONE (#26):** `runs/` and `runs/<id>/` → `0700` (agentctl + brain); `trace.zip` retention in `agentctl` (`SENTINEL_TRACE_KEEP` / `SENTINEL_TRACE_TTL_HOURS`), documented in `docs/OUTPUTS.md`. Optional (not implemented): encryption-at-rest / PII redaction. Also relevant to CDP mode ❽.
8. ~~**store-gateway integrity** (boundary ❷)~~ — **DONE (#23/#24):** per-run token authN in gRPC metadata (`TokenAuthInterceptor`) + SO_PEERCRED + `0600` socket; HMAC integrity for `golden_snapshots` (key `state/golden.key` outside the DB) verified at replay (tamper → exit 3).
9. **extension (M9.8, ❾, implemented `extension/`):** minimal permissions + lazy host/`debugger` (requested on the gesture), mandatory secret redaction in the recorder, debugger-attach only on the takeover gesture with a visible banner, local transport (control-API token; refuses plaintext `ws://` to a non-loopback host) — see `M9.8_CONTRACT` + ADR-038/039.
10. **GAP-SEC-003 — `secretRef` in the authoring schema**: add `secretRef` to `brain/planner.py` `_SCHEMA_STEPS`/`_SCHEMA_DRAFT` + a prompt rule against inlining credentials, so LLM-authored login steps stop materialising as a literal password in `plan.json`/`scenario.json`.
11. **GAP-SEC-004 — code guard on `STORAGE_STATE_SAVE`**: reject a save path inside the artifact dir in code (mirroring the `isUnder` guard in `cmd/agentctl`), not relying solely on the collector's exclusion.
12. **GAP-OPS-006 — run-finished marker**: `agentctl` writes a `status.json` (exit code) on exit so a crashed run is distinguishable from an in-flight one.

---

## 7. References

- Vulnerability disclosure policy: [`SECURITY.md`](../SECURITY.md)
- ADR-019 (provider-agnostic LLM backends): [`docs/M6_CONTRACT.md`](M6_CONTRACT.md)
- ADR-022/027 (index-pick grounding, GoalPlanner): [`docs/M9.2_CONTRACT.md`](M9.2_CONTRACT.md)
- ADR-015 (store-gateway, single SQLite writer): [`docs/M2b_CONTRACT.md`](M2b_CONTRACT.md)
- ADR-026 / GAP-RISK-010 (storageState, PW_NO_TRACE): [`docs/M9.1_CONTRACT.md`](M9.1_CONTRACT.md)
- ADR-021 (token budgets): [`docs/M8_CONTRACT.md`](M8_CONTRACT.md)
- M9-LIVE-prep (live-run artifact export, redaction, GAP-SEC-003/004, GAP-OPS-006): [`docs/M9_LIVE_PLAN.md`](M9_LIVE_PLAN.md) §C · `scripts/collect-live-run.sh`
