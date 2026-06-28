# Sentinel — Pluggable adapters (LiteLLM router · MCP-Inspector)

> 🌐 [Русский](ADAPTERS.md) (authoritative) · **English**

> **ADR-045** · **Date**: 2026-06-28 · **Status**: methodology (optional tooling, no hard dependency)

The umbrella doc for Sentinel's **pluggable adapters** (the M9.7 / GAP-M9-08 theme): tools that sit on the
engine's existing seams **without changing code**. It covers the two picked from the roadmap: **LiteLLM**
(optional model-router) and **MCP-Inspector** (debugging the M7 MCP server).

## 1. Pluggable seams

| Seam | Where | How to plug in |
|------|-------|----------------|
| **Model / backend** | `brain/llm.py` `OpenAICompatBackend` (ADR-019, M6) | env `LLM_BACKEND=openai` + `LLM_BASE_URL=<OpenAI-compat endpoint>` — any OpenAI-compatible provider/proxy |
| **MCP host** | `brain/server.py` M7 server (ADR-020) | a host drives `explore`/`heal`/`replay`/`report` and supplies the model via `sampling/createMessage` |

Both are **optional**: with empty env Sentinel behaves as before (Anthropic by default; no host → heuristic/L1–L6).
The adapters below don't touch the core — they are config + external tools.

## 2. LiteLLM — optional model-router

**LiteLLM** is a self-hosted OpenAI-compatible gateway in front of 100+ providers (DeepSeek/Mistral/Anthropic/Ollama/…).
Why: one routing point, fallback across providers, budget/rate-limit, logging. Sentinel **does not depend** on
LiteLLM — the brain already speaks OpenAI-compat (M6/ADR-019), so the router sits behind `LLM_BASE_URL`. ADR-019
recorded this explicitly: LiteLLM is an **option, not a hard dep in the hot path**.

### Bring it up (compose profile `litellm`, mirroring `ollama`)

```bash
# Provider keys live in the environment (the config references them via os.environ/, never literals).
export DEEPSEEK_API_KEY=…  MISTRAL_API_KEY=…  ANTHROPIC_API_KEY=…  LITELLM_MASTER_KEY=sk-…
docker compose --profile litellm up -d litellm        # OpenAI-compat at http://litellm:4000/v1
```

Image `docker.litellm.ai/berriai/litellm:latest`, port `4000`, config `deploy/litellm/config.yaml`
(`model_list` → providers; keys via `os.environ/<VAR>`). Edit `config.yaml` for your own models.

### Point Sentinel at the router

```bash
LLM_BACKEND=openai \
LLM_BASE_URL=http://litellm:4000/v1 \
LLM_MODEL=deepseek-chat \
LLM_API_KEY=$LITELLM_MASTER_KEY \
  ./bin/agentctl run --target https://app.example
```

Per-role: `LLM_BASE_URL_PLANNER` / `LLM_MODEL_HEAL` etc. (precedence `LLM_<KEY>_<ROLE>` > `LLM_<KEY>`, see
[`LOCAL_MODELS.md`](LOCAL_MODELS.md) §2 "env profile" and §4 "runtime catalog"). Conceptually LiteLLM is just
another OpenAI-compat endpoint, like the `ollama` profile, only a routing proxy.

> **Security:** provider keys are read from env (`os.environ/<VAR>`) — **never a literal** in `config.yaml`
> (it would be committed and caught by gitleaks). This is an OpenAI-compat proxy with no vision guarantee — for
> visual-heal (M5) you need a backend with `supports_vision` (see [`M6_CONTRACT.md`](M6_CONTRACT.md)).
> **A real router smoke is user-run** (network/keys; the dev env blocks network to providers), see GAP-VERIFY-005.

## 3. MCP-Inspector — debugging the M7 server

[MCP Inspector](https://github.com/modelcontextprotocol/inspector) (`@modelcontextprotocol/inspector`) is the
official visual tool for testing MCP servers: it connects over **stdio**, shows `tools/list` with schemas, gives
a call form, and (in UI mode) acts as the **host for `sampling/createMessage`**. That makes it a concrete host
to verify M7 (`brain/server.py`) → partially closes **GAP-VERIFY-006**.

### Run it against the brain MCP server (stdio)

```bash
# First build pw-executor (the brain launches it as a child process).
( cd pw-executor && npm ci && npm run build )

# UI mode: the Inspector spawns the brain, passes env via -e, '--' separates the server command.
npx @modelcontextprotocol/inspector \
  -e RUN_MODE=mcp-server \
  -e PW_EXECUTOR_CMD="node $PWD/pw-executor/dist/server.js" \
  -e ARTIFACT_DIR="$PWD/runs/inspect" \
  -e PYTHONPATH="$PWD" \
  -- .venv/bin/python -m brain
```

What to check:
- **`tools/list`** shows 4 tools: `explore` · `heal` · `replay` · `report` (see [`M7_CONTRACT.md`](M7_CONTRACT.md)).
- Call `replay` (deterministic, **no LLM**) on an existing `plan.json` — needs no sampling.
- Call `explore`/`heal` — they request the model from the host via `sampling/createMessage`; **in UI mode the
  Inspector prompts you to answer the sampling request** (you act as the model) → this exercises `SamplingBackend`
  (`brain/llm.py:150-175`). No sampling → the backend is unavailable → fallback to heuristic/L1–L6.

CLI mode (`--cli`) is handy for scripted checks of tools/resources/prompts; **verify sampling support against your
Inspector version** (sampling is interactive, usually via the UI). The offline analogue without a live host is
[`tests/test_m7_offline.py`](../tests/test_m7_offline.py) (`FakeSamplingSession` + a tools-list smoke).

> **A real sampling run across hosts is user-run** (Claude Desktop supports it; OpenCode/Kilocode — confirm
> before production use), GAP-VERIFY-006.

## See also
[`LOCAL_MODELS.md`](LOCAL_MODELS.md) (model/runtime catalog + calculators) · [`M6_CONTRACT.md`](M6_CONTRACT.md)
(provider-agnostic brain) · [`M7_CONTRACT.md`](M7_CONTRACT.md) (MCP exposure) · [`DEVELOPMENT.md`](DEVELOPMENT.md).
