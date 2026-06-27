# Distribution and Onboarding — EPIC Contract (ADR-030 / ADR-031)

> 🌐 [Русский](DISTRIBUTION.md) · **English**

> **Status**: contract frozen | **Date**: 2026-06-27
> **ADR**: ADR-030 (distribution strategy) · ADR-031 (setup-WebUI)
> **Epic**: M11.1–M11.5 — sequenced; most items are not being built in this cycle
> **Authors**: system-architect agent, @AlexGromer

---

## §1 Introduction and Scope

### What was delivered in this cycle (Foundation)

The Foundation cycle closed three preconditions without which a public release cannot be trusted:

| Delivered | What it closes |
|---|---|
| Security CI gates: gitleaks (hard) + govulncheck (hard) + pip-audit (advisory + freeze artifact) + npm audit (critical) + `go vet`/`go test` + offline-suite m3..m9_2b | GAP-SEC-002 (partial): SCA scanning in CI — prerequisite for trusting binaries |
| `docker-compose.yml` one-command quickstart (sentinel + demo + ollama profiles) | First zero-external-dependency onboarding path |
| GitHub Pages (docs/index.md + 3 calculators: VRAM · token-cost · model-selector) + `docs/LOCAL_MODELS.md` + `docs/TESTING.md` | Air-gapped documentation; calculators work without network access |
| `docs/THREAT_MODEL.md` | Threat model as a prerequisite for a secure release |

**Everything else is sequenced into M11.1–M11.5.** Each milestone does not begin without updating this contract and the corresponding ADR.

### Sequencing rationale (ADR-030)

A release without hardening (SCA/SBOM/lockfile/signatures + threat-model) cannot be trusted. Therefore:

```
Foundation hardening → Releases + signatures (M11.1)
                     → setup-WebUI MVP (M11.2, static-only)
                     → Helm Secret plumbing (M11.3, closes GAP-SEC-001)
                     → Air-gapped bundle (M11.4)
                     → Zero-level installer + QUICKSTART (M11.5)
```

The "all at once in a single release" alternative was rejected: 4–5 milestones spanning release-eng / containers / GitOps / frontend carry high integration risk when delivered simultaneously.

---

## §2 docker-compose quickstart (DONE — this cycle)

### What already works

The `docker-compose.yml` file in the repository root provides a three-service quickstart with no Go/Python/Node installation required:

```
docker compose build                                      # build the image once
docker compose run --rm sentinel --help                   # agentctl help
docker compose run --rm sentinel run \
    --target "https://your-app.example.com"              # explore against a real AUT
docker compose --profile demo up                          # zero-dep demo (fixture file://)
docker compose --profile ollama up -d ollama             # local model (OpenAI-compat)
```

### Services

| Service | Profile | Purpose |
|---|---|---|
| `sentinel` | (always) | Main entry point — `agentctl` CLI. Prints `--help` by default. Mounts `./runs`, `./state`, `./config`. |
| `demo` | `demo` | Zero-external-dependency explore against `testdata/site/index.html` (fixture file://); heuristic planner (no LLM, no API key). Output: `./runs/demo/plan.json`. |
| `ollama` | `ollama` | OpenAI-compatible endpoint `http://ollama:11434/v1`. Start with: `docker compose --profile ollama up -d ollama`, then `docker compose exec ollama ollama pull <model>`. |

### Environment variables

The env block is defined in `docker-compose.yml` or passed via a `.env` file:

```yaml
# Cloud (Anthropic) — no key → offline heuristic + L1–L6 heal
ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}

# Local model (activate by uncommenting):
# LLM_BACKEND: openai
# LLM_BASE_URL: http://ollama:11434/v1
# LLM_MODEL: qwen2.5:7b           # from the docs/LOCAL_MODELS.md §5c catalog
# LLM_API_KEY: noauth             # Ollama ignores the key; SDK requires a non-empty value
# LLM_VISION: 0                   # 1 only for a vision-capable heal model
```

The full env-variable matrix (per-role `_PLANNER`/`_HEAL` suffixes, priority) — see `docs/LOCAL_MODELS.md`.

### Test fixtures

The `demo` profile uses `testdata/site/index.html`. For graduated scenarios (form, login, shadow-DOM):

```bash
docker compose run --rm sentinel run \
    --target "file:///app/testdata/fixtures/l2.html" \
    --planner heuristic
```

Fixtures directory: `testdata/fixtures/l1..l5.html` — see `testdata/fixtures/README.md` for L1–L5 level descriptions.

### Mounted volumes

| Volume | Host path | Purpose |
|---|---|---|
| runs | `./runs` | plan.json, transcript, heal-report, scenario.json, trace.zip |
| state | `./state` | SQLite locator/golden/quarantine DB + store-gateway socket |
| config | `./config` | RunConfig YAML or plan.json (`--run-config /config/run.yaml`) |

### Full guide

`docs/TESTING.md` — detailed instructions: offline gates, local-model setup, artifact interpretation, exit codes.

---

## §3 M11.1 — GitHub Releases: multi-OS/arch binaries + Docker + signatures

**Status:** not started. Prerequisites: Foundation CI gates (DONE).

### What is delivered

Four Go binaries (`agentctl`, `store-gateway`, `orchestrator`, `report-service`) for five platforms:

| Platform | GOOS | GOARCH |
|---|---|---|
| Linux x86-64 | linux | amd64 |
| Linux ARM64 | linux | arm64 |
| macOS Apple Silicon | darwin | arm64 |
| macOS Intel | darwin | amd64 |
| Windows x86-64 | windows | amd64 |

Total: 20 binaries + Docker image (multi-arch: linux/amd64 + linux/arm64).

### CI workflow: `release.yml`

Trigger: `push` to a `v*` tag (e.g., `v1.0.0`).

Steps:
1. `go build -ldflags "-X main.Version=$TAG"` for each platform (matrix).
2. Generate `sentinel-$TAG-$OS-$ARCH.tar.gz` + `.sha256` per artifact.
3. Single `checksums.sha256` (SHA-256 for all archives) — verified via `sha256sum -c checksums.sha256`.
4. **Cosign keyless signing** (Sigstore OIDC): `cosign sign-blob --bundle=...` for each archive. Verification: `cosign verify-blob --bundle=... --certificate-identity-regexp=... artifact.tar.gz`.
5. **Docker buildx + GHCR**: `docker buildx build --platform linux/amd64,linux/arm64 --push -t ghcr.io/alexgromer/sentinel:$TAG .`
6. **SBOM**: `syft ghcr.io/alexgromer/sentinel:$TAG -o cyclonedx-json > sbom.cdx.json`; attached to the Release as an asset.
7. GitHub Release is created via `gh release create` with all artifacts attached.

### Remaining GAP-SEC-002 items closed by M11.1

| Item | Action |
|---|---|
| No committed lockfile | `uv lock` → `uv.lock` committed; `pip-audit --requirement uv.lock` in CI |
| No SBOM | `syft` generates CycloneDX JSON — attached to GitHub Release |
| No release signatures | Cosign keyless signature for each archive + Docker image |

### Acceptance criteria M11.1

- [ ] GitHub Release contains 20 binaries (5 platforms × 4 binaries) in `.tar.gz`
- [ ] `checksums.sha256` is present and passes `sha256sum -c checksums.sha256`
- [ ] Cosign bundle verifies: `cosign verify-blob --bundle=sentinel.bundle sentinel.tar.gz`
- [ ] Docker image is available at `ghcr.io/alexgromer/sentinel:<tag>` for linux/amd64 + linux/arm64
- [ ] SBOM (CycloneDX JSON) attached to Release
- [ ] `uv.lock` committed; `pip-audit` passes in CI based on lockfile
- [ ] CI workflow `release.yml` triggers on `v*` tag and passes without errors

---

## §4 M11.2 — setup-WebUI: static configuration generator (ADR-031)

**Status:** not started. Depends on: M11.1 (to reference real releases). Prerequisites: GitHub Pages (DONE).

### Decision (ADR-031): static-now / control-API-later

**Phase 1 (M11.2):** Static client-side HTML configuration generator. No backend. Air-gapped. The same approach as the three calculators (docs/calculators/*.html).

**Phase 2 (after M9.3):** Live-WebUI, backed by the brain HTTP control-API (M9.3 — GAP-M9-03). Phase 2 is not implemented until the control-API exists — a live-WebUI without a backend would mean writing secrets to localStorage (unacceptable).

### What Phase-1 WebUI generates

The user fills in a form in the browser → WebUI generates:

1. **RunConfig YAML** (for `--run-config /config/run.yaml`):
   ```yaml
   mode: explore          # explore | replay | goal | describe
   target: https://...
   planner: heuristic     # heuristic | llm | goal
   goal: "Complete checkout via cart"
   auth:
     type: storageState
     path: /config/auth.json
   budgets:
     plan_tokens: 50000
     heal_tokens: 20000
   ```
2. **env block** for insertion into `docker-compose.yml` or passing via `--env-file`:
   ```
   LLM_BACKEND=anthropic
   LLM_MODEL=claude-opus-4-8
   ANTHROPIC_API_KEY=<insert>
   LLM_BACKEND_HEAL=openai
   LLM_BASE_URL_HEAL=http://ollama:11434/v1
   LLM_MODEL_HEAL=qwen2.5:7b
   ```

### Form fields

| Field | Type | Default |
|---|---|---|
| Target URL | text | — |
| Mode | select | explore |
| Planner | select | heuristic |
| Goal (if mode=goal/describe) | textarea | — |
| LLM backend (planner) | select | anthropic / openai-compat / none (offline) |
| Planner model | text (with hints from LOCAL_MODELS catalog) | claude-opus-4-8 |
| LLM backend (heal) | select | same as planner |
| PLAN token budget | number | 50000 |
| HEAL token budget | number | 20000 |
| Auth type | select | none / storageState |

### WebUI architectural constraints (Phase 1)

- **No backend calls.** Generation happens entirely in the browser (vanilla JS, zero deps).
- **No secrets stored.** API key fields are placeholders with an instruction to "replace in the env file".
- **Air-gapped.** The page works without a network connection (local copy from GitHub Pages).
- **Explicit phase label.** Phase 2 features (live run, hot-reload config) are marked with the banner "Requires M9.3 control-API — not implemented".

### Acceptance criteria M11.2

- [ ] Static page `docs/setup.html` available on GitHub Pages
- [ ] Generates valid RunConfig YAML (passes `python -c "from brain.runconfig import load_run_config; ..."`)
- [ ] Generates correct env block (all keys from ADR-019 env schema)
- [ ] No external network calls (verified via DevTools → Network in offline mode)
- [ ] Phase-2 features explicitly marked (unavailable without M9.3)
- [ ] Links to `docs/LOCAL_MODELS.md` and `docs/TESTING.md` are present

---

## §5 M11.3 — Helm / Flux / Argo extension (closes GAP-SEC-001)

**Status:** not started. Depends on: M11.1 (release tag for chart appVersion). Helm chart (`deploy/sentinel/`) already exists from M5.

### Problem (GAP-SEC-001)

The current Helm chart injects secrets as plaintext:

```yaml
# deploy/sentinel/templates/cronjob.yaml:34-46 — CURRENT (insecure)
env:
  - name: CHECKPOINT_DSN
    value: {{ .Values.checkpointDsn | quote }}          # plaintext DSN in CronJob spec
  {{- range $k, $v := .Values.extraEnv }}
  - name: {{ $k }}
    value: {{ $v | quote }}                              # plaintext API keys
  {{- end }}
```

This means: `kubectl describe cronjob sentinel` exposes API keys and DSN.

Additionally: `agentctl` passes `cmd.Env = append(os.Environ(), ...)` without an allowlist — every host variable (including Sentinel-unrelated secrets) is inherited by brain and its child processes.

### What M11.3 builds

**1. env-allowlist in agentctl** (`cmd/agentctl/main.go`)

```go
// Was: cmd.Env = append(os.Environ(), extraEnv...)
// Becomes:
allowedPrefixes := []string{
    "LLM_", "ANTHROPIC_", "OPENAI_", "OTEL_",
    "CHECKPOINT_DSN", "STORAGE_STATE", "PW_", "MCP_",
    "ORCH_ADDR", "STORE_SOCKET", "ARTIFACT_DIR",
    "RUN_ID", "RUN_MODE", "TARGET_URL", "AUT_VERSION",
}
cmd.Env = filterEnv(os.Environ(), allowedPrefixes, extraEnv)
```

**2. Secret plumbing in the Helm chart**

New values in `values.yaml`:
```yaml
secrets:
  llmApiKey:
    secretName: sentinel-secrets
    key: llm-api-key
  checkpointDsn:
    secretName: sentinel-secrets
    key: checkpoint-dsn
  storageState:
    secretName: sentinel-secrets
    key: storage-state-path
```

In `cronjob.yaml` — `secretKeyRef` instead of plaintext:
```yaml
env:
  - name: ANTHROPIC_API_KEY
    valueFrom:
      secretKeyRef:
        name: {{ .Values.secrets.llmApiKey.secretName }}
        key: {{ .Values.secrets.llmApiKey.key }}
  - name: CHECKPOINT_DSN
    valueFrom:
      secretKeyRef:
        name: {{ .Values.secrets.checkpointDsn.secretName }}
        key: {{ .Values.secrets.checkpointDsn.key }}
```

Backward compatibility: plaintext `value:` is retained as a fallback (dev/offline mode via `secrets.enabled: false`).

**3. Flux HelmRelease / Kustomization**

New directory `deploy/flux/`:
```
deploy/flux/
├── helmrelease.yaml          # HelmRelease referencing deploy/sentinel chart
├── kustomization.yaml        # Flux Kustomization
└── sentinel-secrets.yaml     # ExternalSecret / SealedSecret example (template)
```

`helmrelease.yaml` (example):
```yaml
apiVersion: helm.toolkit.fluxcd.io/v2beta2
kind: HelmRelease
metadata:
  name: sentinel
  namespace: sentinel
spec:
  interval: 10m
  chart:
    spec:
      chart: ./deploy/sentinel
      sourceRef:
        kind: GitRepository
        name: sentinel
  values:
    target: "https://your-app.example.com"
    schedule: "0 2 * * *"
    secrets:
      enabled: true
      llmApiKey:
        secretName: sentinel-secrets
        key: llm-api-key
```

ArgoCD Application (already exists from M5) — updated to support the new `secrets` block.

### Acceptance criteria M11.3

- [ ] `kubectl describe cronjob sentinel` contains no API keys or DSN (they are in a Secret)
- [ ] env-allowlist in agentctl: unit test confirms that unknown env variables are not passed to brain
- [ ] `helm lint deploy/sentinel` passes (with `secrets.enabled: true` and `secrets.enabled: false`)
- [ ] Flux HelmRelease reconciles green on a test K3s cluster
- [ ] `helm template deploy/sentinel -f deploy/sentinel/values-prod.yaml | grep "value:"` — no secrets in plaintext
- [ ] Documentation updated: `docs/DEVELOPMENT.md` describes Secret plumbing

---

## §6 M11.4 — Air-gapped bundle

**Status:** not started. Depends on: M11.1 (signed image), M11.2 (WebUI static assets).

### Goal

A complete package for installing Sentinel in a network without internet access:
- no calls to Docker Hub, GHCR, npm registry, PyPI, or GitHub
- includes all binaries, the image, the model, and documentation
- verifiable offline after installation

### Bundle contents

| Component | Format | Source |
|---|---|---|
| Docker image | OCI tar (`docker save`) | `ghcr.io/alexgromer/sentinel:<tag>` (linux/amd64 + linux/arm64) |
| `agentctl` (native) | `.tar.gz` from M11.1 Release | GitHub Release |
| Ollama + selected model | Ollama `ollama pull --model-dir` export | configurable from LOCAL_MODELS §5c catalog |
| Python wheels | pre-installed in image (uv.lock) | no PyPI at runtime |
| pw-executor dist | included in image (dist/ at build) | no npm registry at runtime |
| `docker-compose.offline.yml` | separate file | repository |
| Documentation (GitHub Pages) | static HTML from docs/ | HTML copy (offline bundle) |
| Checksums + Cosign bundle | `.sha256` + `cosign.bundle` | M11.1 |

### `docker-compose.offline.yml`

```yaml
# Offline variant: all images from local archive, no external pulls
services:
  sentinel:
    image: sentinel:local          # loaded via docker load
    # ... (identical to docker-compose.yml)
  ollama:
    image: ollama:local-bundle     # loaded via docker load
    # no pull policy: always
```

### Offline verification

```bash
# Verify binary checksums
sha256sum -c checksums.sha256

# Verify image signature (Cosign offline via bundle)
cosign verify-blob --bundle=sentinel.bundle \
    --certificate-identity-regexp=".*" sentinel.tar.gz

# Run in an isolated network
docker run --network none sentinel:local agentctl --help

# Run demo (heuristic, LLM-free) offline
docker compose -f docker-compose.offline.yml --profile demo up
```

### Acceptance criteria M11.4

- [ ] `docker compose -f docker-compose.offline.yml up` makes no external DNS requests (verified via tcpdump or network namespace isolation)
- [ ] `demo` profile completes explore successfully offline (heuristic planner, LLM-free)
- [ ] Ollama endpoint `http://ollama:11434/v1` responds to `/v1/models` without internet connectivity
- [ ] All checksums verify offline (`sha256sum -c`)
- [ ] Cosign bundle verifies without contacting Rekor (offline bundle mode)
- [ ] Documentation (GitHub Pages static copy) is accessible without network

---

## §7 M11.5 — Zero-level onboarding

**Status:** not started. Depends on: M11.1 + M11.2 + M11.4.

### Target user

A QA or DevOps engineer who has Docker but no Go/Python/Node build toolchain. Goal: from zero to a first successful explore run in ≤ 10 minutes.

### Components

**1. `install.sh` — single-command installer**

```bash
curl -fsSL https://raw.githubusercontent.com/alexgromer/sentinel/main/install.sh | sh
```

What it does:
- detects platform (uname -m / os)
- downloads the appropriate `agentctl` binary from the latest GitHub Release
- verifies checksum (`sha256sum -c`)
- verifies Cosign signature (if cosign is installed; warns if not)
- places the binary in `~/.local/bin/agentctl` or `/usr/local/bin/agentctl`
- optionally downloads `docker-compose.yml` to the current directory

**2. `docs/QUICKSTART.md` — step-by-step guide**

Structure (target length: ≤ 2 pages):
1. Prerequisites (Docker ≥ 24)
2. Installation (`curl | sh`)
3. Configuration generation (link to setup-WebUI M11.2)
4. First run: `docker compose run --rm sentinel run --target <URL>`
5. Interpreting results: `runs/<id>/plan.json` + exit codes
6. Next step: `docs/TESTING.md` for the full guide

**3. Integration with setup-WebUI (M11.2)**

QUICKSTART links to setup-WebUI for generating RunConfig YAML without manual editing.

**4. Offline path (M11.4)**

QUICKSTART includes an "Installation without internet access" section: download bundle, `docker load`, `docker compose -f docker-compose.offline.yml`.

### Acceptance criteria M11.5

- [ ] A new user with Docker completes the first explore in ≤ 10 minutes following `docs/QUICKSTART.md`
- [ ] `install.sh` verifies the checksum before installation; exits with a non-zero code on mismatch
- [ ] All QUICKSTART.md steps are reproducible in a clean Docker environment (verified in GitHub Actions)
- [ ] Offline path is documented and verified (depends on M11.4)
- [ ] `install.sh` does not require root when installing to `~/.local/bin`

---

## §11 Integration model

> **This section is normative.** It defines what Sentinel does and what it intentionally does not do when integrating with customer infrastructure. Deviating from this model requires a new ADR.

### Sentinel — black-box UI tester

Sentinel does not have and must not have direct access to:
- databases (SQL, NoSQL, vector stores)
- message queues (Kafka, RabbitMQ, SQS)
- backend gRPC/REST APIs (other than the AUT via the browser)
- service mesh (Istio, Linkerd)
- logs and traces from other services

**This is not a limitation — it is a guarantee.** The black-box contract means:
1. Sentinel tests what a real user tests — observable UI state in the browser.
2. Sentinel requires no backend credentials and does not create a backend compromise risk if the config leaks.
3. Sentinel is portable across stacks — it tests any web application regardless of the backend technology.

### "Response time" in the Sentinel context

Sentinel **already measures** browser-side UI-action latency:

- Every Playwright tool (`navigate`, `click`, `fill`, `expect`, ...) is instrumented with an OTel span carrying precise timestamps (ADR-021/M8, `pw-executor/src/otel.ts`).
- Metrics are exported to Prometheus (Pushgateway or textfile collector).
- "Response time" = time from tool invocation to stable DOM / passing assert — what a real user observes in the browser.

This is not "proxy-latency" or "network RTT" — it is the end-to-end user-observable latency of a UI action, including frontend rendering, XHR, and DOM mutations.

### Backend correlation: W3C traceparent (M9.5)

For correlating a UI test with backend traces, **W3C `traceparent` header injection** is used across all browser HTTP requests.

**Mechanism:**

```
Sentinel OTel span (explore/replay step)
    │
    ├─ traceparent: 00-<trace-id>-<span-id>-01
    │
    └──► pw-executor sets the header on the browser context
              │
              ├─► AUT frontend (every XHR/fetch carries traceparent)
              │        │
              │        └──► backend service (if OTel-instrumented)
              │                  │
              │                  └──► Kafka / DB / downstream service
              │
              └──► customer's Tempo / Jaeger / Zipkin:
                   single trace: UI-action → browser → service → Kafka → DB
```

**Customer requirement:** backend services must be OTel-instrumented and propagate the `traceparent` header through their infrastructure. Sentinel does not add instrumentation to external code.

**Result:** in the customer's Tempo/Jaeger a full-stack trace appears, linking a specific Sentinel UI step to backend processing. This works IFF the customer already uses OTel.

### What will NOT be built (intentionally)

| What | Why not |
|---|---|
| Direct connector to DB / Kafka / gRPC backend | Violates the black-box contract; requires backend credentials; ties Sentinel to a specific stack |
| "Response time" via backend polling | Already solved via browser-side OTel spans — adding backend polling duplicates the measurement and introduces coupling |
| Service mesh integration (Istio mTLS) | Out of scope; infrastructure domain; unrelated to UI testing |
| Log aggregation connector (Loki, ELK) | Sentinel does not aggregate logs; tracing via traceparent covers the use case |
| Backend-specific instrumentation | Customer handles this; Sentinel is a passive header propagator |

### Configurable integration points

The only "seams" Sentinel exposes for integration with customer infrastructure:

| Parameter | Env variable | Purpose |
|---|---|---|
| OTLP endpoint | `OTEL_EXPORTER_OTLP_ENDPOINT` | Where Sentinel sends its spans (customer's Tempo/Jaeger) |
| Prometheus | `PROMETHEUS_PUSHGATEWAY_URL` / textfile | Sentinel metrics (latency, heal-rate, token cost) |
| W3C traceparent injection | M9.5 (GAP-M9-06) | Injecting span context into browser requests |

### M9.5 scope reaffirmation

**M9.5 = traceparent injection into browser requests. That is all.**

M9.5 **does not expand** to:
- directly polling backend services
- parsing backend responses
- actively interacting with Kafka / DB
- aggregating logs
- integrating with service mesh

Any request to expand M9.5 beyond traceparent injection = a new GAP entry + a new ADR + a separate milestone.

### Boundary diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    Sentinel responsibility zone               │
│                                                               │
│   agentctl → orchestrator → brain → pw-executor → Chromium   │
│                                          │                    │
│                              browser HTTP requests            │
│                              (with traceparent, M9.5)         │
│                                          │                    │
└──────────────────────────────────────────┼────────────────────┘
                                           │
                   ────────────────────────┼──────────────
                   Customer responsibility zone            │
                                           ▼
                              AUT frontend → backend service
                              → Kafka → DB → downstream...
                                           │
                                           ▼
                              Customer's Tempo/Jaeger/Zipkin
                              (full-stack trace — if OTel-instrumented)
```

Everything below the dashed line is customer infrastructure. Sentinel passively propagates trace context via the W3C header; it does not read, write, or poll anything beyond that boundary.
