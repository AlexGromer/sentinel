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
| **Unauthorized access to the Unix socket.** Any local process running under the same UID can invoke store-gateway gRPC methods — writing or deleting the golden baseline or locator cache without authentication. | local FS / Unix socket | **E** (Elevation of Privilege) | Prob: L / Impact: M | Socket is created in `state/` under repo root (not in `/tmp`); protection comes from Unix FS permissions (inherits umask). The gRPC server exposes only `PersistenceService`. | No mTLS, no authN between brain and gateway. Exploitable only on an already-compromised host. | dev / post-M10 |
| **Golden baseline tamper via direct SQL.** If permissions on `state/locators.db` are insufficiently restrictive, a local attacker can overwrite `golden_snapshots` records and trigger a false regression result. | FS → SQLite | **T** (Tampering) | Prob: L / Impact: M | `plan_hash` is verified before replay; mismatch → exit code 3. `agentctl baseline update` is the only documented mutation path (`main.go:9`). | `golden_snapshots` have no MAC/signing. If the entire SQLite file is replaced, `plan_hash` offers no protection (it is stored in plan.json, not in the DB). | dev / post-M10 |

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

---

## 5. GAP Tracking Summary Table

| GAP ID | Status | STRIDE | Severity | Short description | Owner / Milestone |
|---|---|---|---|---|---|
| **GAP-RISK-010** | **MITIGATED** | I | — | Leak-in-trace: tracing disabled (`PW_NO_TRACE`) on auth runs; secrets referenced by env-var NAME via secretRef; brain redacts logs; fail-closed on active tracing; prod uses storageState. | — |
| **GAP-SEC-001** | **PARTIAL** | I | HIGH | Full env inherit (`main.go:68`) — **opt-in allowlist added** (`SENTINEL_ENV_ALLOWLIST=1`, default OFF); Helm plaintext secrets (`cronjob.yaml:39–46`) — remaining. | M11.3 (default-on + secretKeyRef) |
| **GAP-SEC-002** | **PARTIALLY OPEN** | T, E | HIGH | Python no lockfile, no SBOM, no image signing. | M11.1 |
| **GAP-OPS-002** | **MITIGATED** | D | MEDIUM | `PW_IGNORE_HTTPS_ERRORS` opt-in + cert classification (`ERR_CERT*`) in `browser.navigate` (this cycle); strict by default. Richer diagnostic in heal-report — M9.4. | M9.4 |

---

## 6. Recommended Controls (Roadmap)

The following controls are **not yet implemented** in the current codebase. Listed as planned/milestone items.

1. **GAP-SEC-001 — env allowlist**: in `agentctl/main.go::spawnBrain` replace `os.Environ()` with an explicit allowlist of variables needed by brain (`TARGET_URL`, `RUN_MODE`, `LLM_*`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CHECKPOINT_DSN`, `STORE_ADDR`, `PYTHONPATH`, `PATH`, …). All others — strip.
2. **GAP-SEC-001 — Helm secretKeyRef**: rewrite the env block in `cronjob.yaml` for sensitive variables using `valueFrom.secretKeyRef`. Remove `checkpointDsn` and secrets from `values-prod.yaml`, extract them into a separate K8s Secret.
3. **GAP-SEC-002 — Python lockfile**: add `uv lock` to CI, commit `uv.lock` to the repo, use `uv sync --frozen` or pip with `--require-hashes` in the Dockerfile.
4. **GAP-SEC-002 — SCA + SBOM + image signing**: add a Trivy/Grype SCA scan to the CI pipeline; `syft` for SBOM generation; `cosign` for image signing.
5. **GAP-OPS-002 — cert diagnostic**: in `browser.navigate` / `dispatchInner`, handle the net-error class and return a classified error (`cert_expired`, `cert_invalid`) in `heal-report.json`.
6. **Prompt sanitization**: strip control characters and limit the length of element names/intent before including them in LLM prompts (`healing.py:_llm_reground`, `planner.py:propose`).
7. **`runs/` access control**: restrict read permissions on the `runs/` directory to the Sentinel process UID; document the retention policy for `trace.zip`.

---

## 7. References

- Vulnerability disclosure policy: [`SECURITY.md`](../SECURITY.md)
- ADR-019 (provider-agnostic LLM backends): [`docs/M6_CONTRACT.md`](M6_CONTRACT.md)
- ADR-022/027 (index-pick grounding, GoalPlanner): [`docs/M9.2_CONTRACT.md`](M9.2_CONTRACT.md)
- ADR-015 (store-gateway, single SQLite writer): [`docs/M2b_CONTRACT.md`](M2b_CONTRACT.md)
- ADR-026 / GAP-RISK-010 (storageState, PW_NO_TRACE): [`docs/M9.1_CONTRACT.md`](M9.1_CONTRACT.md)
- ADR-021 (token budgets): [`docs/M8_CONTRACT.md`](M8_CONTRACT.md)
