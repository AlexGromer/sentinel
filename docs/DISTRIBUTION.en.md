# Distribution and Onboarding — EPIC Contract (ADR-030 / ADR-031)

> 🌐 [Русский](DISTRIBUTION.md) · **English**

> **Status**: contract frozen | **Date**: 2026-06-27
> **ADR**: ADR-030 (distribution strategy) · ADR-031 (setup-WebUI)
> **Epic**: M11.1–M11.6 — sequenced; most items are not being built in this cycle (M11.6 delivered — issue #12)
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
docker compose --profile webui up                        # setup-WebUI + calculators locally → localhost:8088/setup/
```

### Services

| Service | Profile | Purpose |
|---|---|---|
| `sentinel` | (always) | Main entry point — `agentctl` CLI. Prints `--help` by default. Mounts `./runs`, `./state`, `./config`. |
| `demo` | `demo` | Zero-external-dependency explore against `testdata/site/index.html` (fixture file://); heuristic planner (no LLM, no API key). Output: `./runs/demo/plan.json`. |
| `webui` | `webui` | Local air-gapped **setup-WebUI + calculators** (bundled into the image under `/app/docs`); `python -m http.server` on :8088. Open `http://localhost:8088/setup/`. ADR-031 phase-1. |
| `ollama` | `ollama` | OpenAI-compatible endpoint `http://ollama:11434/v1`. Start with: `docker compose --profile ollama up -d ollama`, then `docker compose exec ollama ollama pull <model>`. |

### UI deployment modes and the access token (ADR-064)

The only difference between the three modes is who serves the browser UI:

| Mode | How to start | Ports | CORS | Token in the UI |
|---|---|---|---|---|
| 1 — headless | `docker compose --profile control-api up control-api` | 8090 | not needed | no UI; the client sends `Bearer` itself |
| 2 — split (the previous default) | `docker compose --profile control-api --profile webui up` | 8088 + 8090 | an allowlist is required (`CONTROL_API_CORS_ORIGINS`) | the operator copy-pastes it into Settings |
| 3 — single-service (recommended) | `CONTROL_API_SERVE_UI=1 CONTROL_API_CORS_ORIGINS= docker compose --profile control-api up control-api` → open `http://localhost:8090/` | 8090 | none — same-origin requests are not CORS requests, so the allowlist can be left empty | the one-time `?bootstrap=<nonce>` link printed at startup |

**Token lifecycle (all modes).** You no longer have to invent `CONTROL_API_TOKEN` before the first start: if the
variable is unset, control-api generates 32 random bytes (hex) itself and atomically persists them to
`state/control-api.token` (mode 0600); it reuses that file on the next start, so a token already pasted into the
UI survives a restart.

Token source precedence: `CONTROL_API_TOKEN` (env) → else `CONTROL_API_AUTOTOKEN=0` gives a deliberately
tokenless read-only instance (every mutation is 403, the pre-ADR-064 behaviour) → else the persisted file → else
a freshly generated token. If the file exists but is unreadable or holds content control-api cannot use, it is
NEVER overwritten: the process warns and runs with a throwaway in-memory token instead.

`CONTROL_API_TOKEN_FILE` overrides the file location. `CONTROL_API_PRINT_TOKEN=0` stops the value being printed
at startup (in modes 1-2 it is printed by default, because the terminal is the only channel the operator has).
The token file lives under `state/` (gitignored); on Windows the 0600 mode maps onto ACL semantics rather than
POSIX bits — treat the file as user-scoped and do not rely on the permission bits.

**Mode 3 specifics.** `CONTROL_API_SERVE_UI=1` serves the UI from assets embedded in the binary — no checkout
needed, a release binary is enough. `CONTROL_API_UI_DIR=<path>` serves the pages from disk instead (for
live-editing the pages during development). At startup control-api prints, on stderr:

```
control-api: serving the UI (embedded) at http://127.0.0.1:8090/
control-api: open http://127.0.0.1:8090/?bootstrap=<nonce>  (one-time, valid 5m0s)
```

Opening that link fills the page's control-API URL and bearer-token fields automatically and strips the nonce
from the URL. The token stays in tab memory — never `localStorage`.

The nonce is single-use and expires (default 5 minutes, `CONTROL_API_UI_BOOTSTRAP_TTL`, e.g. `90s`; a
non-positive value disables the bootstrap entirely). Replaying it, using it after expiry, five wrong guesses, or
calling from a cross-origin page all return 403. If you miss the window: read the token out of
`state/control-api.token` and paste it into the page's Settings field, or restart control-api for a fresh nonce.
Reaching the port after startup does NOT get you a token by itself — that is deliberate and preserves the
ADR-032 security invariant.

Pages served: `/` (hub), `/setup/` (wizard), `/chat/` (chat console), `/calculators/*.html`, plus `prices.json`
and `backend-presets.json`. Prose `.md` docs are not served (they are linked to GitHub). Modes 1 and 2 are
byte-for-byte unchanged when `CONTROL_API_SERVE_UI`/`CONTROL_API_UI_DIR` are unset.

### Environment variables

The env block is defined in `docker-compose.yml` or passed via a `.env` file:

```yaml
# Cloud (Anthropic) — no key → offline heuristic + L1–L6 heal
ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY:-}

# Local model (activate by uncommenting):
# LLM_BACKEND: openai
# LLM_BASE_URL: http://ollama:11434/v1
# LLM_MODEL: qwen2.5:7b           # from the docs/LOCAL_MODELS.md §3 catalog
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

**Status:** implemented — `.github/workflows/release.yml` landed (M11.1); the E2E signed release (publish/sign) happens on the first maintainer `v*` tag, `workflow_dispatch` is a build/SBOM dry-run. Prerequisites: Foundation CI gates (DONE).

### What is delivered

Five Go binaries (`agentctl`, `control-api`, `store-gateway`, `orchestrator`, `report-service`) for six platforms:

| Platform | GOOS | GOARCH |
|---|---|---|
| Linux x86-64 | linux | amd64 |
| Linux ARM64 | linux | arm64 |
| macOS Apple Silicon | darwin | arm64 |
| macOS Intel | darwin | amd64 |
| Windows x86-64 | windows | amd64 |
| Windows ARM64 | windows | arm64 |

Total: 30 binaries (6 platforms × 5 binaries) + Docker image (multi-arch: linux/amd64 + linux/arm64).

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

- [ ] GitHub Release contains 30 binaries (6 platforms × 5 binaries) in `.tar.gz`
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

**Status:** **DELIVERED** (M11.3, ADR-035 — closes the Helm half of GAP-SEC-001). Helm chart (`deploy/sentinel/`) exists from M5; the implementation below reflects the actual code (richer than this §5's original sketches, which it supersedes).

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

**1. env-allowlist in agentctl — now default-on** (`cmd/agentctl/main.go`, `filteredEnv()`)

`filteredEnv()` keeps an **exact-map** (PATH/HOME/… + ANTHROPIC_API_KEY/OPENAI_API_KEY/CHECKPOINT_DSN/STORAGE_STATE/ORCH_ADDR/… **+ M11.3 additions** PROM_PUSHGATEWAY/HEAL_VISUAL/SSL_CERT_FILE/SSL_CERT_DIR/HTTP(S)\_PROXY/NO_PROXY/`NODE_OPTIONS`/`NODE_EXTRA_CA_CERTS`/`GIT_SSL_CAINFO`/`GIT_SSL_CAPATH`) **+ a prefix-list** (`LLM_`/`OTEL_`/`PW_`/`PLAYWRIGHT_`/`SENTINEL_`) — `NODE_`/`GIT_` are deliberately NOT in the prefix-list (the broad family used to leak `NODE_AUTH_TOKEN`/`GIT_ASKPASS`; the specific legitimate names are exact-allowed above) **+** names from `SENTINEL_ENV_ALLOW` (comma-sep — for secretKeyRef vars like `AUT_PASSWORD`).

M11.3 flips the flag to **default-on**: the filter is always active unless explicitly opted out via `SENTINEL_ENV_ALLOWLIST=0` (an escape hatch for debugging / unusual local setups). Functional run vars (RUN_ID/TARGET_URL/RUN_MODE/PLANNER/…) are **untouched** by the filter — they are appended after `filteredEnv()` in `spawnBrain`, never inherited from the host. Unit test: `cmd/agentctl/main_test.go` (default-on drops `AWS_SECRET_ACCESS_KEY`, passes curated + `SENTINEL_ENV_ALLOW` extras; `=0` → full passthrough).

**2. Secret plumbing in the Helm chart**

New block in `values.yaml` (default `enabled: false` — dev/offline-friendly):
```yaml
secrets:
  enabled: false
  llmApiKey:
    secretName: sentinel-secrets
    key: llm-api-key
    envName: ANTHROPIC_API_KEY      # rename per backend (OPENAI_API_KEY / LLM_API_KEY)
  checkpointDsn:
    enabled: false                  # true only with a Postgres checkpoint store (M5-3)
    secretName: sentinel-secrets
    key: checkpoint-dsn
  extraSecretEnv: []                # extra secretKeyRef vars (e.g. AUT_PASSWORD)
```

**env-allowlist coupling (critical):** since the filter is now default-on, any chart name outside the curated list would otherwise be **stripped**. So `cronjob.yaml` auto-emits `SENTINEL_ENV_ALLOW` (the `sentinel.envAllow` helper) from `extraEnv` keys + `extraSecretEnv` names + a custom `llmApiKey.envName`, and sets `SENTINEL_ENV_ALLOWLIST=1`.

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
├── sync.yaml                 # Namespace + GitRepository + Flux Kustomization (bootstrap entry)
├── helmrelease.yaml          # HelmRelease → chart deploy/sentinel
└── sentinel-secrets.yaml     # ExternalSecret / SealedSecret example (template, no secrets)
```

**apiVersions = Flux v2 GA** (not the early-sketch `v2beta2`; verify the cluster runs Flux ≥ 2.3): HelmRelease `helm.toolkit.fluxcd.io/v2`, GitRepository `source.toolkit.fluxcd.io/v1`, Flux Kustomization `kustomize.toolkit.fluxcd.io/v1`. The Flux Kustomization file is named **`sync.yaml`**, NOT `kustomization.yaml` (else kustomize would treat the dir as an overlay).

**Secret ordering:** Flux `HelmRelease.spec.dependsOn` references only other HelmReleases/Kustomizations (not a raw Secret), so there is no literal "dependsOn Secret" in Flux. The CronJob runs on a schedule (not at install), so it tolerates the Secret arriving later; `sync.yaml` (`wait: true`) applies the Secret source alongside the release. For strict ordering, split into two Flux Kustomizations (secrets → app with `dependsOn`). ArgoCD ↔ Flux are **mutually exclusive**.

`helmrelease.yaml` (example):
```yaml
apiVersion: helm.toolkit.fluxcd.io/v2
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

ArgoCD Application (`deploy/argocd/sentinel-app.yaml`, exists from M5) — comment expanded: `secrets.enabled` (via `values-prod.yaml`) turns on secretKeyRef; the `sentinel-secrets` Secret itself is supplied out-of-band (SealedSecret/ExternalSecret/sops — ArgoCD does not store its content); ArgoCD ↔ Flux are mutually exclusive.

### Acceptance criteria M11.3

- [x] env-allowlist **default-on**: unit test (`cmd/agentctl/main_test.go`) confirms unknown env vars (`AWS_SECRET_ACCESS_KEY`) are not passed to brain; `SENTINEL_ENV_ALLOWLIST=0` → full passthrough
- [x] `helm lint deploy/sentinel` passes with `secrets.enabled: true` **and** `false`
- [x] `helm template … -f values-prod.yaml` — `ANTHROPIC_API_KEY` and `CHECKPOINT_DSN` only via `secretKeyRef`, no secret in plaintext `value:`; dev path keeps the plaintext fallback without `secretKeyRef`
- [x] `deploy/flux/*.yaml` parse-clean, apiVersions = Flux v2 GA, no `kustomization.yaml` file
- [x] Documentation: `docs/DEVELOPMENT.md` (+en) describes Secret plumbing; GAP-SEC-001 Helm half closed; ADR-035
- [ ] **live-verify (no cluster/flux CLI):** `kubectl describe cronjob sentinel` contains no keys/DSN
- [ ] **live-verify:** Flux HelmRelease reconciles green on K3s

---

## §6 M11.4 — Air-gapped bundle

**Status:** implemented — offline compose + verify/bundle scripts + CI `airgap` job (the core is verified on every push/PR). The full bundle E2E (real GHCR image + model + signatures) is assembled by the maintainer on the first `v*` tag, same as M11.1. Depends on: M11.1 (signed image), M11.2 (WebUI static assets).

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
| Ollama + selected model | pull on a connected machine → tar the `OLLAMA_MODELS` volume (or `ollama create` from a GGUF+Modelfile) | configurable from the LOCAL_MODELS §3 catalog |
| Python wheels | pre-installed in image (uv.lock) | no PyPI at runtime |
| pw-executor dist | included in image (dist/ at build) | no npm registry at runtime |
| `docker-compose.offline.yml` | separate file | repository |
| Documentation (GitHub Pages) | static HTML from docs/ | HTML copy (offline bundle) |
| Checksums + Cosign bundle | `.sha256` + `cosign.bundle` | M11.1 |

### What was implemented (M11.4)

- `docker-compose.offline.yml` — `internal: true` network (zero egress), `pull_policy: never`, an offline anchor with no `build:`, `demo`=`network_mode: none`, an `ollama-models` volume with a pinned `name:`; the `ollama` profile (docs are browsed via a separate `http.server` container — an internal network can't publish ports).
- `scripts/offline-verify.sh` — a single verifier: `--local` (build→save/load→`--network none` demo+docs+negative-DNS — the CI gate) and `--bundle <dir>` (checksums + `cosign verify-blob --bundle` offline + stack up + `/v1/models`).
- `scripts/build-airgap-bundle.sh` — maintainer assembler (run on a connected machine): `gh release download`, **verify the GHCR image before `docker save`**, export the ollama model, self-signed `MANIFEST.sha256`.
- CI `airgap` job + `tests/test_m11_4_offline.py`; fixed `.dockerignore` (`!docs/index.html`).

**Important:** "zero external calls" refers to bundle CONSUMPTION. Building the bundle (`build-airgap-bundle.sh`) runs on a connected machine and pulls images/model/release — that's expected, same as the CI/release pipeline itself not being air-gapped. `docker save`/`load` does NOT carry the image's cosign signature, so the image is verified on the connected machine BEFORE saving, and bundle integrity rests on the cosign-signed `MANIFEST.sha256`.

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

| # | Criterion | Status |
|---|---|---|
| 2 | demo completes explore offline (heuristic, LLM-free) | ✅ **verified in CI** (`airgap` job) |
| 6 | static docs copy is available offline | ✅ **verified in CI** (after the `.dockerignore` fix) |
| 1 | `compose up` with no external DNS | ◐ **mechanism** — `internal:true` + a negative-DNS probe for sentinel/demo; the full stack — at the tag |
| 5 | cosign bundle verifies without Rekor | ◐ **mechanism** — a live self-signed `--bundle` round-trip in CI; the real release identity — at the tag |
| 3 | Ollama `/v1/models` responds offline | ☐ **open** — at the tag (requires a real model bundle) |
| 4 | checksums verify offline (`sha256sum -c`) | ☐ **open** — the logic self-tests; real checksums — at the tag |

---

## §7 M11.5 — Zero-level onboarding

**Status:** docs-first freeze (ADR-059). Expanded beyond the original thin spec into **guided onboarding** — a
guided state machine. Delivered across 5 sequential PRs: docs (this freeze) → installer → config-schema+presets
→ wizard → config-domain+`/readyz`. Depends on: M11.1 (release assets) + M11.2 (setup-WebUI) + M11.4 (the offline path).

### Target user

A QA or DevOps engineer who has Docker but no Go/Python/Node build toolchain. Goal: from zero to a first
successful explore run in ≤ 10 minutes, **with no manual YAML editing** and no need to read the full documentation.

### Vision: onboarding as a guided state machine (ADR-059)

Not a flat form plus "drop in a YAML file by hand", but a stepped wizard that understands the runtime modes,
assembles a correct configuration itself, persists it, and reuses it on the next launch.

**1. `install.sh` / `install.ps1` — single-command installers** (POSIX `sh` for Linux/macOS; a
PowerShell peer for Windows)
```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/AlexGromer/sentinel/main/install.sh | sh
```
```powershell
# Windows (no admin) — installs the CLIENT; see the note below
iwr -useb https://raw.githubusercontent.com/AlexGromer/sentinel/main/install.ps1 | iex
```

> ⚠ **Windows is a CLIENT platform (decided 2026-08-02, ADR-110).** The previous wording — "native
> Windows, no Docker/WSL needed" — was wrong and promised more than exists. `install.ps1` installs
> **`agentctl.exe` only**, and that part is genuinely native. A run, however, also needs Python
> 3.11+ with uv (the planning/healing brain), Node 24+ (the Playwright executor) and the browsers
> themselves; the installer neither ships nor intends to ship those. The supported Windows path is
> `agentctl` as a client of a control-API running in a container or on another host. For the full
> stack on the Windows host itself, use Docker Desktop or WSL. `install.ps1` said this all along in
> its own `.DESCRIPTION`; the documents were what disagreed with it.

- `install.sh`: `uname -s`/`-m` → `{linux,darwin}`×`{amd64,arm64}`; `install.ps1`: native Windows,
  `{amd64,arm64}` (`$env:PROCESSOR_ARCHITECTURE`);
- resolves the latest GitHub Release, downloads `sentinel-<tag>-<os>-<arch>.tar.gz` + `checksums.sha256` + `*.cosign.bundle`;
- **`sha256sum -c`** (non-zero exit on mismatch) → **`cosign verify-blob`** with a **pinned identity** (the same
  regex/issuer as `scripts/offline-verify.sh`; if `cosign` is missing — a loud warning, not a hard failure);
- `install.sh` installs `agentctl` into `~/.local/bin` (default, **no root**) or `/usr/local/bin` (opt-in);
  `install.ps1` installs into `%LOCALAPPDATA%\Programs\sentinel` (**no admin**); both check `$PATH`;
- post-install `agentctl --version` (sanity check) + a pointer to setup-WebUI and `docs/QUICKSTART.md`; optionally downloads `docker-compose.yml`.

**Homebrew (macOS):** the repository is its own tap; `Formula/sentinel.rb` is generated on every `v*`
tag (`scripts/gen-brew-formula.sh` in `release.yml`):
```bash
brew tap AlexGromer/sentinel https://github.com/AlexGromer/sentinel
brew install sentinel
```

**2. setup-WebUI → a stepped wizard** (rewrites `docs/setup/index.html`, ADR-031→ADR-059)
- Steps: **Runtime → Model&Auth → Run params → Review** (reuses the `.tabbar`/`.subtabbar` pattern from `docs/index.html`).
- **A runtime dropdown of presets** (see the table below) → conditional per-backend fields (base_url/model/api_key). NB: the runtime choice ≠ the RunConfig `mode` (explore/goal/describe), which lives in the Run-params step.
- **Schema-driven**: the form renders from `GET /v1/config-schema` (extended with LLM-backend fields) — the single
  source of truth: `brain/runconfig.py` for RunConfig fields, `brain/llm.py` `make_backend` for the LLM-backend fields (ADR-060), with no hardcoded drift.
- **Validation**: required fields (target), budget ranges, error highlighting, re-ask on a problem.
- **Draft persistence**: a configuration draft + the control-API URL in `localStorage` (the token is NEVER stored); on
  relaunch — prefill and re-validation (the re-run state machine). Bilingual (`data-lang`), air-gapped (no CDN).

**3. Runtime presets (open-core, the config-only seam of ADR-019).** All of them are `LLM_BACKEND=openai` + a different
`LLM_BASE_URL`/`LLM_MODEL` (machine-readable → `docs/backend-presets.json`; source of truth — `docs/LOCAL_MODELS.md`):

| Preset | `LLM_BACKEND` | `LLM_BASE_URL` (default) | Note |
|---|---|---|---|
| Cloud — Anthropic | `anthropic` | — (native) | `ANTHROPIC_API_KEY` |
| Cloud — OpenAI-compatible (OpenAI/DeepSeek/OpenRouter) | `openai` | the provider's `/v1` | a real API key |
| Ollama | `openai` | `http://ollama:11434/v1` | the key is ignored, the SDK requires a non-empty value (`noauth`) |
| vLLM | `openai` | `http://vllm:8000/v1` | GPU/throughput |
| llama.cpp / llamafile | `openai` | `http://host:8080/v1` | edge/minimal dependencies |
| LM Studio | `openai` | `http://host:1234/v1` | dev workstation |
| LocalAI | `openai` | `http://localai:8080/v1` | multi-backend |
| LiteLLM (router) | `openai` | `http://litellm:4000/v1` | multi-provider router (ADR-045; image in `deploy/litellm`) |
| HuggingFace TGI | `openai` | `http://host:<PORT>/v1` | the operator sets the port (no default) |

**4. Tiered config persistence** (profile = topology, ADR-049):
- **standalone**: the config is a file (RunConfig YAML / `.env`), read back idempotently (`brain/runconfig.py` — already in place, unchanged);
- **service**: a new `config` domain in the store-gateway (following the ADR-050 pattern) — control-API reads the config at startup / writes it from the wizard.

**5. `/readyz`** (on top of the existing `/healthz` liveness probe): checks real dependencies — the store-gateway
socket reachable · the LLM endpoint (`/v1/models`) · a config present → `503` while not ready, `200` once ready (k8s-shaped).

**6. `docs/QUICKSTART.md`** (≤ 2 pages): prerequisites (Docker ≥ 24) → install (`curl|sh`) → configuration (setup-WebUI) →
the first run → interpreting `runs/<id>/plan.json` + exit codes → the offline path (M11.4) → the full guide, `docs/TESTING.md`.

### Open-core / enterprise boundary (ADR-056)

The wizard + **all** runtime presets + file/DB config + health probes = **open-core** (open-core must be useful,
not crippleware). Enterprise = managed/EMS provisioning · license issuing · multi-tenancy · SSO/RBAC/Vault · advanced BI.

### Acceptance criteria M11.5 (honest, per PR)

- [x] **PR-1 (this freeze):** ADR-059 + the rewritten §7 + bilingual parity. *(docs, verifiable now)*
- [x] **PR-2 (this PR):** `install.sh` verifies checksum+cosign (non-zero exit on mismatch), installs without root into `~/.local/bin`, `agentctl --version` prints the version; CI install-smoke in a clean container (fake release + tamper negative). *(full E2E = maintainer `v*` tag, as with M11.1)*
- [x] **PR-3 (this PR):** `/v1/config-schema` covers the LLM-backend surface (`backends`/`roles`/`llm` descriptors from `brain/llm.py`; `api_key`=secret-with-no-value); `backend-presets.json` (9 presets) parses and every `backend` ⊆ the schema enum (gate `TestBackendPresetsParseAndMatchSchema`). *(env source of truth = `brain/llm.py` `make_backend`; `runconfig.py` = RunConfig core, no LLM_* there)*
- [x] **PR-4 (this PR):** the wizard is stepped (Runtime→Model&Auth→Run-params→Review), schema-driven (renders from `/v1/config-schema` plus an embedded snapshot for offline and a live override, ADR-061), validates input (target / budgets / the `make_backend` openai rules), persists a draft (never secrets), is bilingual (`data-lang`), air-gapped (`node --check` in CI now covers every `docs/*.html`; on `file://` the snapshot replaces the `fetch`). *(the DOM run is automated — `scripts/wizard-dom-check.mjs`, 12 checks in headless Chromium in CI; the syntax gate covers all 6 `docs/` pages; + 2 anti-drift gates)*
- [x] **PR-5 (this PR):** the `config` domain lands in the store-gateway (a 6th `StoreService` domain, ADR-062); secrets are **refused** (`internal/configguard`, one rule shared by the gateway and the control-API — 14 bypass attempts in tests); the control-API reads the config at start and the wizard writes it (`PUT /v1/config`, token-gated); `/readyz` → `503` until dependencies are ready, `200` once ready (an unconfigured dependency is `skipped`, so standalone stays ready). *(end-to-end DOM gate: browser → control-API → gRPC → SQLite → `/readyz`)*
- [x] A new user with Docker completes the first explore in ≤ 10 minutes following `docs/QUICKSTART.md`. **Measured (2026-07-11):** `docker compose build` **208 s** + `docker compose run … --target https://example.com --planner heuristic` **21 s** = **~3 min 49 s** end-to-end, exit 0, `plan.json`+`trace.zip` produced. *(Caveat: the `playwright` base image was cached; a fully cold pull adds ~2.44 GB of download → ~7 min on a ~100 Mbps link — still under budget, but network-dependent.)*

---

## §8 M11.6 — Single-page Pages hub (dark-neon, bilingual, recommendation)

**Status:** delivered (issue #12, expanded scope). Depends on: LOCAL_MODELS §3/§5/§6 (formula source).

The Pages landing is rebuilt as **one self-contained `docs/index.html`** (full HTML, all CSS/JS inline, no
network/CDN/fonts/build). All interactives are **sections on a single page** (no page hops): recommendation ·
cost (§6) · VRAM (§5) · model selector (§3.3) · legend · documentation.

- **Dark neon theme** (red accent `#ff2d55` on `#0b0b10`, high contrast, no clutter).
- **Full-page RU/EN toggle** (default RU, persisted in `localStorage`; visibility via `data-lang` + CSS;
  JS-rendered output emits both locales — toggling reflows instantly with no re-render).
- **Recommendation engine**: task + hardware + budget → a clear answer (which model, mode/depth
  explore/goal/replay, how many runs fit the budget, tokens, time, cost) — by model/hardware/task.
- **Legend + per-field explanations**; advanced inputs collapsed in `<details>` (clean, no clutter).
- **Anti-hallucination**: prices and tok/s are editable illustrative defaults marked "verify, cutoff Jan-2026"
  with provider links; model names/sizes come from §3 with ✅/⚠ flags (nothing invented).
- **Air-gapped parity**: identical on Pages, `file://` and the `webui` Docker bundle (previously `index.md`
  rendered only via Jekyll). The old `docs/calculators/*.html` stay on disk as "advanced".
- Replaces `docs/index.md` (Jekyll cayman) → static `index.html` (**ADR-033**). §5/§6 formulas verbatim;
  embedded self-tests reproduce the worked examples (cost A–E; VRAM); `node --check` clean.
- **M11.6b (cost-explorer follow-up, ADR-034):** popular-model catalog (Claude/GPT/Grok/GLM/DeepSeek/Qwen
  + local) + **blended $/1M** by default (in/out under advanced) + **per-model token multiplier** (reasoning
  think-tokens) + fit/reasoning/vision badges; **air-gapped live pricing**: embedded seeds → `prices.json`
  (CI `prices-refresh.yml` via OpenRouter → PR) → "Refresh from OpenRouter" button (network on click only).
  Price source LOCAL_MODELS §3.4.

### Acceptance criteria M11.6

- [ ] From the landing a non-expert enters site size + budget and immediately sees the model comparison and a recommendation — with no hops to other pages
- [ ] Math mirrors LOCAL_MODELS §5/§6 (cost vectors A–E + VRAM examples reproduce)
- [ ] Full-page RU/EN toggle (default RU, localStorage) switches all text, including JS-rendered output
- [ ] Dark neon theme, legend and per-field explanations are present
- [ ] Air-gapped: no network/CDN; opens on Pages, `file://` and the `webui` bundle
- [ ] Prices/tok/s are editable and marked "verify (cutoff Jan-2026)"; model names carry §3 flags
- [ ] `node --check` on the extracted `<script>` is clean; gitleaks is clean

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
