# Threat Model — Sentinel

> 🌐 [Русский](THREAT_MODEL.md) · **English**

> **Version**: 1.0 | **Date**: 2026-06-27 | **Authors**: appsec-engineer (auto), @AlexGromer
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

> **New surfaces (M9.6/M9.8), not shown in the diagram above (optional/future):** ❽ **CDP-attach** to the user's browser (M9.6, opt-in `PW_CDP_ENDPOINT`) and ❾ **browser extension** (M9.8, planned) — see §4.8 / §4.9.

---

## 4. STRIDE-lite: Threat Table

> **Notation**: Prob(ability) H/M/L without existing controls; Impact H/M/L on assets.
> GAP-IDs correspond to entries in BACKLOG/GAPS.

### 4.1 Boundary ❶ — host-env → agentctl → brain (full env inherit)

| Threat | Boundary | STRIDE | Prob / Impact | Existing control | Residual risk | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Leakage of all host secrets to child processes.** `agentctl::spawnBrain` calls `cmd.Env = append(os.Environ(), …)` without an allowlist (`main.go:68`). All host variables (SSH keys, cloud credentials, tokens unrelated to Sentinel) are inherited by the Python brain, Node.js pw-executor, and their subprocesses, and may also surface in stderr on error. | host-env → brain subprocess | **I** (Information Disclosure) | Prob: H / Impact: H | None | **GAP-SEC-001 OPEN**: no env allowlist | M11.3 (env allowlist) |
| **Plaintext secrets in Helm values → Kubernetes.** `cronjob.yaml:39–46` uses `value: {{ .Values.checkpointDsn }}` and `{{range .Values.extraEnv}} value: {{ $v }}` without `secretKeyRef`. `CHECKPOINT_DSN` and `extraEnv` are stored as plain strings in `values-prod.yaml`, land in etcd in plaintext, and are visible via `kubectl describe pod`. | Helm chart → K8s etcd | **I** (Information Disclosure) | Prob: H / Impact: H | None | **GAP-SEC-001 OPEN**: no `secretKeyRef` plumbing | M11.3 (Helm secretKeyRef) |

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
| **PII leakage from AUT UI into artifacts.** `trace.zip` contains DOM snapshots and screenshots; if the AUT displays personal data, it is persisted under `runs/`. | AUT DOM → runs/trace.zip | **I** (Information Disclosure) | Prob: H / Impact: M | **Auth runs MITIGATED (GAP-RISK-010)**: `PW_NO_TRACE=1` on auth runs — tracing is not started (`server.ts:108`); typed passwords do not appear in the trace. Prod runs use `storageState` (password is never typed). brain logs only `prompt_hash`, never page content. | Regular explore/replay runs record `trace.zip` with DOM and screenshots. Content is determined by the AUT. No encryption at rest for `runs/`. | ops / classified by AUT owner |

### 4.5 Boundary ❻ — brain → LLM endpoint (cloud / local)

| Threat | Boundary | STRIDE | Prob / Impact | Existing control | Residual risk | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Leakage of AUT page content to a cloud LLM provider.** The planner prompt contains `current_url`, element names, and intent; the heal prompt contains `interactives[]` (DOM elements, up to 3,000 chars). With a cloud backend, all of this is transmitted to the Anthropic API / OpenAI / OpenRouter. | brain → cloud LLM HTTPS | **I** (Information Disclosure) | Prob: H (with cloud backend) / Impact: M | Tracing: `prompt_hash()` (`otel.py:14`) — SHA-256 first 16 hex of the prompt, never the content. Span attributes store only token counts. Prompts are not logged to brain stderr. `LLM_BASE_URL` allows switching to a local Ollama/vLLM for data residency. | With a cloud backend, AUT page structure (URLs, element names) is sent to the provider. No DLP filtering of prompts. Data residency is guaranteed only with a local endpoint. | ops / documented (backend choice) |
| **LLM response compromise (malicious backend / MITM).** `OpenAICompatBackend` makes HTTPS calls to `base_url`. A compromised or MITM-intercepted endpoint can return a forged response. | brain → openai-compat endpoint | **T** (Tampering) / **S** (Spoofing) | Prob: L / Impact: M | TLS (HTTPS to external endpoints). Index-pick grounding limits impact: a malicious index will cause a click on the wrong element, but not RCE. An out-of-bounds index → brain degrades to `done` (`planner.py:97`). | No certificate pinning for cloud endpoints. | dev / post-M10 |
| **LLM token budget exhaustion.** An AUT with deep navigation or adversarial DOM can drive high token consumption and financial loss. | brain → LLM billing | **D** (Denial of Service / cost) | Prob: M / Impact: M | **Mitigated** (ADR-021, `budget.py`): `PLAN_TOKEN_LIMIT` (default 50,000), `HEAL_TOKEN_LIMIT` (default 20,000), `TOTAL_TOKEN_LIMIT` (default 0 = off). On budget exceeded → fallback to heuristic/L1–L6, run continues. | Financial loss if limits are disabled or the AUT is very large. | ops / documented |

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
| **Recording the user's live session into trace.zip.** In CDP mode the env-gated tracing + traceparent route apply to the user's adopted context → their DOM/screenshots/requests may land in `runs/<id>/trace.zip`. | user browser → runs/ | **I** | Prob: M / Impact: M | `PW_NO_TRACE=1` disables tracing; disclosed in `M9.6_CONTRACT` + code comments; CDP mode is opt-in. | Tracing is on by default (unless `PW_NO_TRACE`); the user may not expect a recording. See issue #26. | M9.8 / docs |
| **Access to someone else's session via an unprotected CDP port.** A CDP endpoint without authentication = any local process can drive the browser; Sentinel reuses the user's login. | local → CDP `:9222` | **E/I** | Prob: L / Impact: H | The user brings up the CDP port deliberately (opt-in); localhost-only. | The DevTools protocol has no authN — exposing the port = full browser control. **Do not expose the port externally.** | user / docs |

### 4.9 Boundary ❾ — browser extension (M9.8, PLANNED)

> **Not yet implemented** — modeled ahead of time (design-first, `M9.8_CONTRACT`). The project's largest surface expansion: an MV3 extension living in the user's browser.

| Threat | Boundary | STRIDE | Prob / Impact | Planned control | Residual risk | Owner / Milestone |
|---|---|---|---|---|---|---|
| **Reading all of the user's pages.** A content-script recorder sees DOM/input on EVERY in-scope page (`host_permissions`); recorded events go to the brain. | all pages → brain | **I** | Prob: H / Impact: H | Minimal `host_permissions` (`activeTab` / on request); recorder only on explicit start; local transport (control-API localhost+token / native-messaging). | An extension inherently sees everything in the active tab; trusting the extension = trusting its code + review. | M9.8 |
| **`chrome.debugger` = full CDP.** Takeover via the debugger API gives full page control and bypasses web restrictions. | extension → page | **E** | Prob: M / Impact: H | Debugger-attach only on an explicit takeover signal; Chrome's visible indicator; auto-detach on return. | Broad powers, albeit with a banner. | M9.8 / ADR-039 |
| **extension↔brain transport.** The streaming channel (WS on the control-API / native-messaging) is an injection/interception point. | extension → control-API | **S/T** | Prob: M / Impact: M | Reuse ADR-032: localhost-bind + bearer-token + CORS-allowlist; native-messaging is stdio (no network port). | Token in the browser; WS port on localhost. | M9.8 / GAP-M9-14 |

---

## 5. GAP Tracking Summary Table

| GAP ID | Status | STRIDE | Severity | Short description | Owner / Milestone |
|---|---|---|---|---|---|
| **GAP-RISK-010** | **MITIGATED** | I | — | Leak-in-trace: tracing disabled (`PW_NO_TRACE`) on auth runs; secrets referenced by env-var NAME via secretRef; brain redacts logs; fail-closed on active tracing; prod uses storageState. | — |
| **GAP-SEC-001** | **CLOSED — Helm half (M11.3/ADR-035)** | I | HIGH | env-allowlist **default-on** (opt-out `SENTINEL_ENV_ALLOWLIST=0`) + Helm `secretKeyRef` + `sentinel.envAllow`. **Remainder:** `NODE_`/`GIT_` prefixes → `NODE_AUTH_TOKEN`/`GIT_ASKPASS` (issue #25). | done; #25 → 0xCoDSnet |
| **#23 store-gateway authN** | **MITIGATED** | E | MEDIUM | per-run token authN in gRPC metadata (`TokenAuthInterceptor`) + SO_PEERCRED + 0600 socket; unit test `TestTokenAuthInterceptor`. | done; #23 → 0xCoDSnet |
| **#24 golden integrity** | **MITIGATED** | T | MEDIUM | HMAC on `golden_snapshots` (key `state/golden.key`, outside the DB); tamper → exit 3; tests `TestGoldenIntegrityTamper` + `test_golden_mac_tamper_detected_exit3`. | done; #24 → 0xCoDSnet |
| **#26 trace.zip PII** | OPEN | I | MEDIUM | explore/replay write AUT DOM+screenshots to `runs/` with no encryption-at-rest/retention (boundaries ❹/❼/❽). | issue #26 → 0xCoDSnet |
| **GAP-SEC-002** | **PARTIALLY OPEN** | T, E | HIGH | Python no lockfile, no SBOM, no image signing. | M11.1 |
| **GAP-OPS-002** | **MITIGATED** | D | MEDIUM | `PW_IGNORE_HTTPS_ERRORS` opt-in + cert classification (`ERR_CERT*`) in `browser.navigate` (this cycle); strict by default. Richer diagnostic in heal-report — M9.4. | M9.4 |

---

## 6. Recommended Controls (Roadmap)

The following controls are **not yet implemented** in the current codebase. Listed as planned/milestone items.

1. ~~**GAP-SEC-001 — env allowlist**~~ — **DONE (M11.3 / ADR-035):** `filteredEnv()` flipped to default-on (opt-out `SENTINEL_ENV_ALLOWLIST=0`) + a curated list. **Remainder:** `NODE_`/`GIT_` prefixes pass `NODE_AUTH_TOKEN`/`GIT_ASKPASS` → issue **#25**.
2. ~~**GAP-SEC-001 — Helm secretKeyRef**~~ — **DONE (M11.3):** `secrets.*` → `valueFrom.secretKeyRef` when `secrets.enabled` (plaintext fallback in dev); the `sentinel.envAllow` helper; `deploy/flux/`.
3. **GAP-SEC-002 — Python lockfile**: add `uv lock` to CI, commit `uv.lock` to the repo, use `uv sync --frozen` or pip with `--require-hashes` in the Dockerfile.
4. **GAP-SEC-002 — SCA + SBOM + image signing**: add a Trivy/Grype SCA scan to the CI pipeline; `syft` for SBOM generation; `cosign` for image signing.
5. ~~**GAP-OPS-002 — cert diagnostic**~~ — **DONE:** cert classification (`ERR_CERT*`/`ERR_SSL*`) in `browser.navigate` + opt-in `PW_IGNORE_HTTPS_ERRORS` (strict by default).
6. **Prompt sanitization**: strip control characters and limit the length of element names/intent before including them in LLM prompts (`healing.py:_llm_reground`, `planner.py:propose`).
7. **`runs/` access control**: restrict read permissions on the `runs/` directory to the Sentinel process UID; document the retention policy for `trace.zip` (→ issue **#26**; also relevant to CDP mode ❽).
8. ~~**store-gateway integrity** (boundary ❷)~~ — **DONE (#23/#24):** per-run token authN in gRPC metadata (`TokenAuthInterceptor`) + SO_PEERCRED + `0600` socket; HMAC integrity for `golden_snapshots` (key `state/golden.key` outside the DB) verified at replay (tamper → exit 3).
9. **extension (M9.8, ❾):** on implementation — minimal `host_permissions`, debugger-attach only on a takeover signal, local transport (control-API token / native-messaging) — see `M9.8_CONTRACT` + ADR-038/039.

---

## 7. References

- Vulnerability disclosure policy: [`SECURITY.md`](../SECURITY.md)
- ADR-019 (provider-agnostic LLM backends): [`docs/M6_CONTRACT.md`](M6_CONTRACT.md)
- ADR-022/027 (index-pick grounding, GoalPlanner): [`docs/M9.2_CONTRACT.md`](M9.2_CONTRACT.md)
- ADR-015 (store-gateway, single SQLite writer): [`docs/M2b_CONTRACT.md`](M2b_CONTRACT.md)
- ADR-026 / GAP-RISK-010 (storageState, PW_NO_TRACE): [`docs/M9.1_CONTRACT.md`](M9.1_CONTRACT.md)
- ADR-021 (token budgets): [`docs/M8_CONTRACT.md`](M8_CONTRACT.md)
