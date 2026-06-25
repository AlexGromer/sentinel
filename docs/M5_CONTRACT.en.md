# M5 Contract — "Deploy + Visual Heal" (frozen 2026-06-24)

> 🌐 [Русский](M5_CONTRACT.md) (основная версия) · **English**

Goal: ship Sentinel to the home-lab K3s/ArgoCD as GitOps, and scaffold the **set-of-marks visual
heal** (Tier-7) behind a measured PoC gate. Split into three; M5-1 is the value-first, offline-authorable part.

## Scope split
- **M5-1 — Deployment (GitOps).** Multi-stage Dockerfile (Go + Python + Node + Playwright browser);
  Helm chart packaging the agent as a **CronJob/Job** (scheduled replay) + per-namespace values
  (dev/staging/prod); an **ArgoCD Application** manifest. Authorable as YAML/Dockerfile now; user
  deploys to their K3s (not testable here — no cluster).
- **M5-2 — Visual heal scaffold (Tier-7).** pw-executor `browser.setOfMarks` (numbered overlay over
  interactive elements → marks[]); HealingEngine Tier-7 (vision: Sonnet picks a mark → real locator),
  **gated** behind `--heal-visual` + `--heal-llm` AND a PoC accuracy gate (**ship only if ≥70%** on 20
  real broken-selector scenarios — ADR-005). Needs a vision LLM (key/network) → PoC run by the user.
- **M5-3 — Postgres checkpointer option.** Swap LangGraph `SqliteSaver` → `AsyncPostgresSaver` when
  `CHECKPOINT_DSN` is set (for K3s multi-runner). Config-only.

## M5-1 deployment (authorable offline)
- `Dockerfile`: stage1 `golang:1.26` builds `agentctl` + `store-gateway`; stage2 `node:24` builds
  `pw-executor` (`npm ci && npm run build`); stage3 runtime `mcr.microsoft.com/playwright` (or python
  base + `playwright install`) with the venv (`uv pip install …`), copying the Go binaries + dist +
  brain. Entrypoint `agentctl`.
- `deploy/sentinel/` Helm chart: `Chart.yaml`, `values.yaml` (image, schedule, target URL, plan
  source, `--ci`, `--aut-version`, resources), templates: `cronjob.yaml` (scheduled `agentctl run
  --replay --ci`), `configmap.yaml` (plan.json / config), `serviceaccount.yaml`. Per-namespace:
  `values-dev.yaml` / `values-staging.yaml` / `values-prod.yaml`.
- `deploy/argocd/sentinel-app.yaml`: ArgoCD `Application` → the chart (auto-sync, the home-lab repo).
- **VERIFY at deploy:** image base for Playwright browser; the agent in-cluster reaches its target;
  persistent volume for `state/` (Ceph PVC) if the store-gateway runs as a sidecar.

## M5-2 visual heal (scaffold + gate)
- `browser.setOfMarks` → `{marks: [{mark:int, role, name, css, bbox}], screenshot_path}` — overlays a
  numbered box per interactive element (DOM-eval bbox + a data-URL/screenshot), returns the mark→element map.
- HealingEngine **Tier-7** (after L1–L6 + LLM-a11y fail AND `completeness_ratio < 0.30`): Sonnet vision
  is given the screenshot + marks, returns a `mark` number; we extract that element's **real** locator
  (NOT a coordinate click). Discount ×0.85 (ADR-005). Behind `--heal-visual` (off by default).
- **PoC gate** (`agentctl heal-poc --scenarios <dir>`): runs Tier-7 over ≥20 labeled broken-selector
  scenarios, reports precision/recall; **Tier-7 ships enabled only if ≥70%** (else stays scaffolded/off).
- ANTI-HALLUCINATION: do not assume a specific vision API shape — reuse the existing Sonnet path in
  `healing.py._llm_reground`; extend it with image content; VERIFY the Anthropic image-block API.

## M5-3 Postgres checkpointer
`brain/__main__.py`: if `CHECKPOINT_DSN` set → `AsyncPostgresSaver.from_conn_string(dsn)` else the
SqliteSaver (current). One-constructor swap; VERIFY `langgraph-checkpoint-postgres` package + API.

## Acceptance gate (Given/When/Then)
- **M5-1:** `helm template deploy/sentinel` renders valid manifests (offline); `docker build` produces an
  image whose `agentctl run --replay --ci` works (user runs on cluster / locally); ArgoCD app lints.
- **M5-2:** `browser.setOfMarks` returns a marks[] map; Tier-7 is wired + gated off; `heal-poc` harness
  runs and prints an accuracy figure (user supplies a key + scenarios).
- **M5-3:** with `CHECKPOINT_DSN` set the explore run checkpoints to Postgres (user-run); unset → SQLite (unchanged).

## Out of scope
Production observability (M4b) · multi-tenant SaaS · cross-browser matrix · auto-merge of healed plans.
