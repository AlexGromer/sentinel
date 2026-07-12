# Sentinel — deploy + M9-LIVE testing on a Windows host

> 🌐 [Русский](WINDOWS_TESTING.md) (основная версия) · **English**

> **Type:** How-to · **Audience:** the live-test operator on a separate Windows machine
> **Related:** [M9_LIVE_PLAN.md](./M9_LIVE_PLAN.md) · [LOCAL_MODELS.md](./LOCAL_MODELS.md) · [QUICKSTART.md](./QUICKSTART.md) · [DISTRIBUTION.md](./DISTRIBUTION.md)

A practical guide: stand up the whole stack on a Windows host, run the live test (fixtures L1–L6 →
explore/replay with a local LLM), and collect the artifacts for analysis. Every command is verified
against `install.ps1`, `docker-compose.yml`, `M9_LIVE_PLAN.md §A/§B`.

---

## 0. Before you start

- **`install.ps1` is NOT for this test.** It installs only `agentctl.exe` from a GitHub Release (and needs
  a published `v*` tag, which does not exist yet). M9-LIVE needs the whole stack (agentctl + Python brain +
  Node/Playwright executor + optional Ollama) → build from source.
- **Two paths on Windows** (pick one):
  - **Path A — Docker Desktop (recommended):** the `sentinel:local` image bundles agentctl+brain+
    pw-executor+fixtures. Least manual fuss.
  - **Path B — WSL2 (Ubuntu):** a native Linux environment where the bash scripts (`collect-live-run.sh`)
    work as documented.

### 0.1 Model & VRAM (important — the amount drives the choice)

The env variable names come from `brain/llm.py make_backend` (**NOT** `SENTINEL_*_MODEL`). Verified VRAM
from [`LOCAL_MODELS.md §3.3`](./LOCAL_MODELS.md); a precise per-config estimate is in `docs/calculators/vram.html`.

| GPU VRAM | PLAN role (planner) | VISION role (heal) |
|---|---|---|
| **8 GB** (e.g. RTX 2060 **SUPER** = 8 GB) | **Qwen3-8B** Q5 (~6 GB) | Qwen2.5-VL-7B Q4 (~7 GB) |
| **12 GB** (RTX 2060 **12GB**/3060) | **Qwen3-14B** Q4_K_M (~9.5 GB) | Qwen2.5-VL-7B Q4/Q5 (~7 GB) |

> **Key point:** the planner (explore) and the vision-heal (replay) run in **different phases** — they need
> not be resident in VRAM at the same time. Ollama loads a model on demand; set `OLLAMA_MAX_LOADED_MODELS=1`
> so one is unloaded before the other loads (a ~few-second pause on the switch). So what matters is that
> **each fits individually**, not their sum. `qwen3:14b` (~9.5 GB) needs **12 GB**; on 8 GB use **qwen3:8b**
> for the planner, or the 14B partially offloads to CPU and the planner gets slow.

Pull (native Ollama for Windows — https://ollama.com/download/windows, which has proper GPU access):
```powershell
ollama pull qwen3:14b        # planner on 12 GB; on 8 GB → ollama pull qwen3:8b
ollama pull qwen2.5-vl:7b    # heal (vision)
```

---

## Path A — Docker Desktop (recommended)

**Prereqs:** Docker Desktop (WSL2 backend, the default) · Git for Windows (gives `git` + **Git Bash** for
artifact collection) · native Ollama for Windows (see §0.1).

```powershell
# 1. clone + build the image
git clone https://github.com/AlexGromer/sentinel.git ; cd sentinel
docker compose build            # sentinel:local from the Dockerfile (agentctl+brain+pw-executor+fixtures)

# 2. LLM-free smoke (prove the stack is alive; artifacts to .\runs via a volume)
docker compose run --rm -v ${PWD}\runs:/app/runs sentinel `
  run --target "file:///app/testdata/fixtures/l3.html" --planner heuristic --artifact-dir /app/runs/smoke
#   success = exit 0 + .\runs\smoke\plan.json. Fixtures (l1..l6-newtab, L4=3 files) are already in the image.
```

**Env for the local LLM** (else it silently degrades to the heuristic — the default backend is anthropic!).
Native Ollama on Windows → the container reaches it via `host.docker.internal`:
```powershell
$LLM = @(
  "-e","LLM_BACKEND=openai",
  "-e","LLM_BASE_URL=http://host.docker.internal:11434/v1",
  "-e","LLM_API_KEY=noauth",
  "-e","LLM_MODEL_PLANNER=qwen3:14b",     # or qwen3:8b on 8 GB
  "-e","LLM_MODEL_HEAL=qwen2.5-vl:7b"
  # "-e","LLM_STRUCTURED=1"   # opt-in strict JSON; if the endpoint rejects json_schema it silently falls back to heuristic
)
```

**Live explore + replay:**
```powershell
# explore/author (grounded LLM plan)
docker compose run --rm -v ${PWD}\runs:/app/runs $LLM sentinel `
  run --goal "fill the form with valid data and submit" `
      --target "file:///app/testdata/fixtures/l3.html" --artifact-dir /app/runs/live1

# replay + heal (locator drift → self-heal L1–L6 + confidence gate)
docker compose run --rm -v ${PWD}\runs:/app/runs $LLM sentinel `
  run --replay --plan /app/runs/live1/plan.json --artifact-dir /app/runs/replay1
```

**(optional) Co-pilot UI:**
```powershell
docker compose --profile control-api up -d control-api   # HTTP facade
docker compose --profile webui up -d webui               # static UI at http://localhost:8088
```

---

## Path B — WSL2 (Ubuntu) natively

```bash
# prereqs
sudo apt update && sudo apt install -y golang-1.26 nodejs npm python3 git
curl -LsSf https://astral.sh/uv/install.sh | sh          # uv (Python venv)

# build (from the repo root)
go build -o bin/agentctl ./cmd/agentctl && go build -o bin/store-gateway ./cmd/store-gateway \
  && go build -o bin/control-api ./cmd/control-api
cd pw-executor && npm i && npm run build && npx playwright install chromium && cd ..
cd brain && UV_PROJECT_ENVIRONMENT=../.venv uv sync --frozen && cd ..   # venv at repo-root .venv (where agentctl looks)

# env + run (native Ollama on the Windows host)
export LLM_BACKEND=openai
export LLM_BASE_URL=http://host.docker.internal:11434/v1
export LLM_API_KEY=noauth LLM_MODEL_PLANNER=qwen3:14b LLM_MODEL_HEAL=qwen2.5-vl:7b
bin/agentctl run --goal "…" --target "file://$PWD/testdata/fixtures/l3.html" --artifact-dir runs/live1
bin/agentctl run --replay --plan runs/live1/plan.json --artifact-dir runs/replay1
```

---

## M9-LIVE checklist (`M9_LIVE_PLAN.md §B/§D`)

| Check | How | Confirms |
|---|---|---|
| explore/author | `run --goal … --target file://…/l3.html` | grounded LLM plan, no hallucinated selector |
| replay + heal | explore → drift the DOM (site→site-v2) → `run --replay --plan …` | self-heal L1–L6 + confidence gate (RISK-002) |
| determinism | 2× golden in separate processes → compare bytes | RISK-009 byte-stability |
| budget-kill | low `TOTAL_TOKEN_LIMIT` → planner→heuristic degradation | M8 budget ceiling |
| co-pilot UI | control-api + `docs/index.html` → Tests→Live on a run_id | M14 AG-UI timeline, auto-HITL |
| MV3 extension (optional) | `extension/` → `npm i && npm run build` → load unpacked in Chrome → record→scenario | M9.8 recorder |

> **The main trap:** a run "succeeds" even with the LLM off. Check `runs\<id>\llm-transcript.jsonl` —
> the `planner` field must be `llm`, not `heuristic`.

---

## Collecting the artifacts

Each run = `runs\<id>\` (`plan.json`·`scenario.json`·`heal-report.json`·`report.json/html`·
`llm-transcript.jsonl`·`metrics.prom`·`trace.zip`). **Keep `runs\LIVE_NOTES.md`**: id · model · target ·
expected/got · exit code · what surprised you.

**Collect with secret redaction** (`collect-live-run.sh` — bash, run it in **Git Bash or WSL**):
```bash
scripts/collect-live-run.sh <run_id>                # → live-results/live-<id>.tar.gz (redaction ON)
scripts/collect-live-run.sh <run_id> --with-trace   # + trace.zip (UNREDACTED — dev stand only!)
```
Redaction by default blanks passwords in form steps + sweeps Authorization/Bearer/Cookie/token shapes;
`checkpoint.db` and `storage_state*.json` are never collected. **Transfer: USB/scp, NOT git.**
On the dev machine drop it in `live-results/` → "analyse the live runs" → RISK-002/003 calibration from real numbers.

---

## Windows gotchas

- **`host.docker.internal`** — how the container/WSL reaches native Ollama on the Windows host. If Ollama runs
  as a compose service (`--profile ollama`), the address from the sentinel container is `http://ollama:11434/v1`.
- **Volume paths**: PowerShell `${PWD}\runs:/app/runs`; CMD `%cd%\runs:/app/runs`.
- **GPU for Ollama**: native Ollama for Windows uses the GPU directly; Ollama-in-Docker on Windows has trickier
  GPU passthrough (prefer native).
- **`trace.zip`** carries live DOM + request bodies → not collected by default; `--with-trace` is for a
  disposable dev stand only.
- **CRLF**: bash scripts need LF → `git config --global core.autocrlf input` before cloning, or run them in WSL.
