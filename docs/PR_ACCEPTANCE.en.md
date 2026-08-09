# PR acceptance checklist — Sentinel

> 🌐 [Русский](PR_ACCEPTANCE.md) (primary) · **English**

What has to have happened before a PR merges. Half of it is executed by CI and only **described**
here; the other half cannot be automated by construction, and every one of its rows carries a
recorded reason — not as an excuse, but because otherwise a skipped check has nowhere to announce
itself.

Normative reference: `docs/DEVELOPMENT.md` §0, principle 7 — a PR that adds a component or a
capability must extend the observation of it. Until this file existed, that principle pointed at a
"check matrix" that was not in the repository: the only occurrence of the phrase was the principle
itself.

**Why not "matrix".** The word is taken four times over: `docs/M16_MATRIX.md` (the CLI↔UI gap
measurement), the CI's own `strategy.matrix`, the release platform matrix in
`docs/DISTRIBUTION.md`, and the parity matrix in `docs/M14_CONTRACT.md`. Someone told to "cover it
in the matrix" would open a document about ui-missing verdicts. "Acceptance" already carries this
meaning — `docs/DEVELOPMENT.md` §4, "Milestone gates (acceptance)" — and nothing else. The name
`PR_GATES.md` was rejected: "gate" in this repository means an automatic check, and half of this
list is manual by construction, so the name would promise an automation that does not exist.

---

## 1. The machine half — CI runs this

The rows below are **checked against `.github/workflows/ci.yml`** by
`tests/test_pr_acceptance_offline.py`: each must name a job that exists and a step that exists, and
the set of jobs named here must equal the set of jobs in that file **in both directions**. A
documented check no workflow runs, and a job this document is silent about, are both red.

None of it needs doing by hand: this is what happens on its own, and it is written down so that the
manual half below reads as the **remainder** rather than as the whole list.

<!-- pr-acceptance:machine -->
| Check | Job | Step in `.github/workflows/ci.yml` |
|---|---|---|
| pw-executor: TypeScript build + unit tests (`npm test` gates the compile too) | `build` | `Build + unit-test pw-executor (TypeScript -> dist/server.js; node:test gates the compile)` |
| Go: `go vet ./...` + `go test ./...` across the whole tree | `build` | `Vet + unit test (Go)` |
| Inline JS syntax of every `docs/` page (`node --check`, with a floor on the page count) | `build` | `SPA syntax check (inline JS of every docs page — node --check floor gate; M15 + M11.5 PR-4)` |
| Setup-wizard DOM gate, live headless Chromium (floor 15) | `build` | `Setup-wizard DOM gate (headless Chromium; M11.5)` |
| Hub DOM gate, live headless Chromium (floor 45) | `build` | `Hub Logs-view DOM gate (headless Chromium; ADR-065)` |
| Static-showcase DOM gate — the hub with no control-API behind it (floor 5) | `build` | `Static-showcase DOM gate (the hub with NO control-API behind it; ADR-110)` |
| Python offline suite: **every** `tests/test_*_offline.py`, discovered by glob, floor 25 | `build` | `Python offline suite (every tests/test_*_offline.py, discovered — FakeBackend/FakeExecutor, no network)` |
| End-to-end UI smoke against a real deployment | `build` | `End-to-end UI smoke against a real deployment (screenshots; ADR-110/111)` |
| Screenshots uploaded as an artifact (`always()` — a failure is exactly when they are worth having) | `build` | `Upload UI smoke screenshots` |
| Deterministic per-fixture replay with a golden diff, asserting the exit code | `replay` | `Explore + freeze goldens + replay (assert exit code)` |
| Explore over `testdata/site` producing `plan.json` | `explore` | `Explore testdata/site -> plan.json` |
| Secrets: gitleaks over the **whole history**, hard fail | `security` | `gitleaks (secrets scan — HARD fail)` |
| Python dependency advisories against the committed lock (advisory) | `security` | `pip-audit (Python deps — advisory; audits the committed lock)` |
| CycloneDX SBOM from the frozen lock | `security` | `SBOM (CycloneDX, from the frozen lock — #38)` |
| Bilingual parity: every primary `docs/*.md` has its `.en.md` sibling | `bilingual` | `Check bilingual docs parity` |
| Build the `sentinel:local` image | `airgap` | `Build sentinel:local (cached, amd64, loaded — not pushed)` |
| The image knows its own version | `airgap` | `The image knows its own version` |
| Offline runtime verification: `save`/`load` → `--network none`, no external calls | `airgap` | `Offline runtime verification (save/load -> --network none, no external calls)` |
| Cosign: sign and **offline** `verify-blob` round-trip | `airgap` | `Cosign sign + OFFLINE verify-blob round-trip` |
| `shellcheck` over every shell script | `install-smoke` | `shellcheck all shell scripts` |
| `install.sh` against a fake release + the tampered-checksum negative | `install-smoke` | `install.sh e2e vs a local fake release (+ tampered-checksum negative)` |
| Build the four control-plane binaries for the package | `deb-smoke` | `Build the four control-plane binaries` |
| The `.deb`: build, **install**, inspect, remove | `deb-smoke` | `Build, install, inspect and remove the package` |
| `collect-live-run.sh`: default redaction, exclusions, opt-in `--with-trace` | `collect-live-run-smoke` | `collect-live-run.sh — default redaction + exclusions + --with-trace opt-in` |
| `install.ps1` against a fake release + the tampered-checksum negative | `install-ps1-smoke` | `install.ps1 e2e vs a local fake release (+ tampered-checksum negative)` |
| `golangci-lint` (advisory) | `lint` | `golangci-lint (advisory)` |
| `ruff` over brain and tests (advisory) | `lint` | `ruff (advisory — brain + tests)` |
<!-- /pr-acceptance:machine -->

---

## 2. The manual half — the PR author does this

Four rows. Each carries a **recorded reason** for not being in CI: the shape is copied from
`componentsWithoutProbe` (`cmd/control-api/readyz.go`), where the absence of a declaration is itself
declared a failure — otherwise a skip has nowhere to record its "why", and the gate would have to
accept silence.

The reason automating any of them would be **worse** than a tick box: "I looked" is false and
refutable by the next person who opens the same file, whereas a CI step asserting "the PNG exists
and is non-empty" is unrefutable in principle — it is green over precisely the defect it was added
for. The measurement that bought this: across two sessions the automatic gates found **0 of 9** and
**0 of 5** defects that screenshots and a live run found, and both times every gate was green.

<!-- pr-acceptance:manual -->
| Check | How it is done | Why it is not in CI |
|---|---|---|
| **Look at the `ui-smoke` screenshots** — not "the step is green", but open the frames and see what is on them | Run the smoke (§3) and **open the PNGs**; name the panel and what is visible on it in the PR body | The only automatable form is "the file exists and is non-empty" — a gate that is green over the very defect the step is added for. Measured on PR-B: the screenshots found six event codes rendered in English to a Russian reader, and a `/readyz` 503 painting a healthy service red; the smoke asserted neither, and could not have, because nobody foresaw them |
| **A live run against a real model** — not FakeBackend | `LLM_BACKEND=openai LLM_MODEL=qwen3:8b LLM_BASE_URL=<endpoint>/v1 LLM_STRUCTURED=1`, run to a verdict; name the model and the outcome in the PR body | The endpoint lives on the maintainer's LAN and is unreachable from a GitHub runner. Both available automations are worse than a tick box: requiring it hard means red for a reason outside the PR while `main` is protected and demands green (which trains people to merge over red); skipping when unreachable means the step reports success when the model was never asked. HEALTH-003 already measured `HTTP 000` from that endpoint |
| **Docker in all three delivery forms** — `docker-compose.yml`, `.ghcr.yml`, `.offline.yml` | Bring the stack up with each file (§3) and check the affected surface inside the container | A fresh runner does not reproduce what this step catches: root-owned volumes against `user: 1000:1000`, rotation on **live** containers, a journal surviving `docker compose down`. Defects of this class exist because of accumulated host filesystem state; on a clean machine they cannot happen, and the job would be green where there is nothing to measure. CI today has only a `docker build` of one image and two `docker run`s (job `airgap`) — not one of the three compose files |
| **Mutations** — every new check must be able to fail | Make an edit that breaks the assertion, confirm the test goes red, revert. A surviving mutation is either covered or recorded as equivalent beside the test; name the line and the outcome in the PR body | A threshold on mutation score loses exactly what mutations give. Measured: 85 mutations, 18 survivors, **every one** a defect in a test or a fixture and never in product code; the PR-C find (deleting `journal(...)` from `cdp-service.ts` leaves every gate green) came from a **human** picking that line on a suspicion about an uncovered call site. A number instead of a judgement would be satisfied by the same suite that missed the defect |
<!-- /pr-acceptance:manual -->

Not every PR adds a component. A one-document edit need not bring up three stacks — but a skipped
row is declared in the PR body rather than assumed: `docs/DEVELOPMENT.md` §0 requires a **recorded
reason**, not silence.

---

## 3. Running this locally

No name lists here — commands with a glob only. The reason is `docs/DEVELOPMENT.md` §0, principle 5:
a hand-kept list cannot show what is missing, because absence has no representation to look at. This
very list had already gone stale in four places at once — 7 names in `CONTRIBUTING.md`, 20 in
`docs/DEVELOPMENT.md` and `docs/TESTING.md`, 20 in `FILEMAP.md` — while `tests/` held dozens.

```bash
# Go — the whole tree, as CI does
go build ./... && go vet ./... && go test ./...
go test -race ./cmd/control-api/          # where shared state changes

# Python — by discovery, not by list
for f in tests/test_*_offline.py; do PYTHONPATH="$PWD" .venv/bin/python "$f"; done

# pw-executor
cd pw-executor && npm test && cd ..

# The three DOM gates
node scripts/wizard-dom-check.mjs
node scripts/hub-dom-check.mjs
node scripts/pages-static-check.mjs

# UI smoke with screenshots — the exact recipe is in ci.yml, step "End-to-end UI smoke"
#   ⚠ store-gateway on a SHORT socket path (a unix address is capped near 108 bytes),
#     CONTROL_API_SERVE_UI=1 and CONTROL_API_UI_DIR=$PWD/docs — otherwise `/` answers 404
node scripts/ui-smoke.mjs --base http://127.0.0.1:8090 --token <token> --out "$PWD/ui-smoke"

# Docker, three delivery forms
docker compose -f docker-compose.yml up -d
docker compose -f docker-compose.ghcr.yml up -d
docker compose -f docker-compose.offline.yml --profile demo run --rm demo
```

⚠ **Park `runs/` before concluding anything about a DOM gate** (`mv runs/*/ runs/.park/`): past run
directories, some of them root-owned by containers, break both `go test ./...` and the gate's count.

---

## 4. Recorded exemptions

A literal list of suite names (`for t in m3 m4 …`) is forbidden by the gate in `CONTRIBUTING.md`,
`.github/PULL_REQUEST_TEMPLATE.md`, `docs/TESTING.md`, `docs/DEVELOPMENT.md`, `FILEMAP.md` and in
this file — that is, everywhere it reads as an **instruction to execute today**.

`docs/M*_CONTRACT.md` are deliberately outside the gate. A milestone contract records what was run
to accept **that** milestone; rewriting its list would retroactively alter the record of an
acceptance. The exemption is recorded here rather than implied by a list inside the gate's code.
