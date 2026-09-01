# Sentinel — quickstart

🌐 EN · [Русский](QUICKSTART.md)

From zero to your first autonomous explore run in ≤ 10 minutes. For QA/devops with Docker — **no** Go/Python/Node
build toolchain, no need to read the full docs.

## 1. Requirements

- **Docker ≥ 24** (+ `docker compose` v2);
- **git** (to get the source — the image is built locally until a GHCR release is published).

## 2. Get Sentinel

```bash
git clone https://github.com/AlexGromer/sentinel.git
cd sentinel
```

> The Docker path builds the `sentinel:local` image from source (`docker-compose.yml` + `Dockerfile`), so a repo
> checkout is required. After the first signed release, a ready-made image lands in GHCR (then the clone becomes optional).

## 3. First run (Docker, no toolchain)

```bash
docker compose build                        # first time — build sentinel:local
docker compose run --rm sentinel run --target "https://example.com" --planner heuristic
```

With no API key / local model, the run uses the deterministic heuristic planner (offline, self-contained).

**Configure with no hand-written YAML** — the setup-WebUI wizard:

```bash
docker compose up                           # → http://localhost:8088/setup/ (the whole stack, no flags)
# then: docker compose run --rm sentinel run --run-config /config/run.yaml
```

## 4. Interpret the result

Artifacts land in `runs/<id>/`: `plan.json` (the frozen plan), `transcript`, `heal-report.json`, `trace.zip`.
**Exit code** (structured, `docs/STATE_MACHINE.md`):

| Code | Meaning |
|---|---|
| 0 | pass — the plan ran, no regressions |
| 1 | step-fail — a step did not execute |
| 2 | golden regression — DOM drift, healing/diff required |
| 3 | integrity — a `plan_hash`/golden-HMAC mismatch **or** budget exhaustion |
| 4 | THE TOOL ITSELF failed — NOT a finding about your application (ADR-087) |
| 5 | the tool failed but what it found was saved (ADR-131) |
| -1 | could not be spawned, or killed by a signal ⚠ synthetic; no process returns it |

## 5. Optional: the native `agentctl` CLI

For command-line control (not a full run — a run still goes through the Docker image). The installer verifies the
**checksum** (hard fail on mismatch) + the **Cosign signature** (if `cosign` is installed) and places the binary in a
user directory (**no root/admin**). Check with `agentctl version`.

- **Linux / macOS:** `curl -fsSL https://raw.githubusercontent.com/AlexGromer/sentinel/main/install.sh | sh` → `~/.local/bin`
- **Windows (PowerShell):** `iwr -useb https://raw.githubusercontent.com/AlexGromer/sentinel/main/install.ps1 | iex` → `%LOCALAPPDATA%\Programs\sentinel`
- **macOS (Homebrew):** `brew tap AlexGromer/sentinel https://github.com/AlexGromer/sentinel && brew install sentinel` *(works after the first signed release)*

## 6. Install with no internet access (air-gapped)

Download the offline bundle → transfer it → `docker load` the images → `docker compose -f docker-compose.offline.yml`.
Details: `docs/DISTRIBUTION.md` §6.

## 7. Next

- The full testing guide and run model: `docs/TESTING.md`.
- The local-model and runtime catalog: `docs/LOCAL_MODELS.md`.
- The co-pilot UI — a vertical rail that follows the session (ADR-066). The view list is deliberately not duplicated here: the hub itself (`docs/index.html`) defines it and `scripts/hub-views.mjs` derives it. The fastest way to bring it up (ADR-064,
  single-service, no CORS wiring): `CONTROL_API_SERVE_UI=1 CONTROL_API_CORS_ORIGINS= docker compose up control-api`,
  then open the one-time link `http://127.0.0.1:8090/?bootstrap=<nonce>` printed in the startup log (valid for 5
  minutes, fills in the address and token itself). The three UI deployment modes and the access token — `docs/DISTRIBUTION.md` §2.
