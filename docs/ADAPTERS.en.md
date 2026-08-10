# Sentinel — Pluggable adapters (SPI · LiteLLM router · MCP-Inspector)

> 🌐 [Русский](ADAPTERS.md) (authoritative) · **English**

> **ADR-045** (tooling) · **ADR-123** (SPI) · **Date**: 2026-06-28, extended 2026-08-10 ·
> **Status**: methodology + an implemented SPI

The umbrella doc for Sentinel's **pluggable adapters** (the M9.7 / GAP-M9-08 theme). Two different things
under one cover, and it helps not to conflate them:

- **§1 — the SPI (ADR-123):** the place where **THIRD-PARTY CODE** stands. The `brain/adapters.py`
  registry, three kinds (`model` · `auth` · `deploy`), discovery through `SENTINEL_ADAPTERS`. The full
  contract is [`M9.7_CONTRACT.en.md`](M9.7_CONTRACT.en.md).
- **§2–§3 — external tools (ADR-045):** **LiteLLM** (optional model-router) and **MCP-Inspector**
  (debugging the M7 server). They need no code at all: they sit on existing seams by configuration.

## 1. The SPI — the seams the product itself is extended through

| Kind | What it replaces | Product entry point |
|------|------------------|---------------------|
| **`model`** | the model provider (`ModelAdapter.make(spec) -> LLMBackend`) | `brain/llm.py::make_backend(role)` — `anthropic` and `openai` are registered right there as ordinary adapters |
| **`auth`** | the declarative `auth:` block → environment (`EnvAdapter.env(spec)`) | `brain/runconfig.py::_apply_auth`; reference `storage_state` = the M9.1 login-as-test flow (ADR-026) |
| **`deploy`** | the declarative `deploy:` block → environment | `brain/runconfig.py::_apply_deploy`; reference `local` = `STORE_ADDR` · `OTEL_EXPORTER_OTLP_ENDPOINT` · `CHECKPOINT_DSN` |

```bash
SENTINEL_ADAPTERS=mycorp_sentinel.adapters LLM_BACKEND=bedrock ./bin/agentctl run --target …
```

```yaml
# run.yaml — `adapter:` is optional; without it the one that shipped is used
auth:   {adapter: storage_state, storage_state: /run/state.json, pw_no_trace: true}
deploy: {store_addr: gateway:50051, otel_endpoint: http://otel:4317}
```

**Rules worth knowing before writing your own adapter** (the reasoning is in the contract):
`EnvAdapter.env()` is **pure** and does not write to the environment (the "explicit flag > file"
precedence stays in `runconfig.py`, not in the adapter) · a `ModelAdapter` **does not read `LLM_*`**
itself, everything resolved arrives in `ModelSpec` · an unknown adapter name is a **config error
(exit 3)**, not a silent fallback · an unimportable `SENTINEL_ADAPTERS` module **raises** on the
RunConfig path and **degrades with an announcement** on the model path.

> 🔒 **Licence boundary (ADR-056 §2 row 42):** the SPI and its reference adapters are the open-core
> framework (Apache-2.0, irreversibly). Enterprise auth (Keycloak/OIDC/Vault/SSO/RBAC) attaches to this
> SPI **from outside** and is never committed to this tree — `[M-COMMERCIAL-auth]`. The rule is
> **checked** by `tests/test_adapter_spi_offline.py`, not merely written down.

### Seams that are NOT adapters

| Seam | Where | How to plug in |
|------|-------|----------------|
| **OpenAI-compatible endpoint** | `brain/llm.py` `OpenAICompatBackend` (ADR-019, M6) | env `LLM_BACKEND=openai` + `LLM_BASE_URL=<endpoint>` — configuration of a built-in adapter, no code needed (this is where LiteLLM sits, §2) |
| **MCP host** | `brain/server.py` M7 server (ADR-020) | a host drives `explore`/`heal`/`replay`/`report` and supplies the model via `sampling/createMessage`; `sampling` resolves **before** the registry — it is a property of how the process runs, not a configured provider |

All of the above is **optional**: with empty env Sentinel behaves as before (Anthropic by default; no
host → heuristic/L1–L6).

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
  (`brain/llm.py`, class `SamplingBackend` — anchored by name, not by line number). No sampling → the backend is unavailable → fallback to heuristic/L1–L6.

CLI mode (`--cli`) is handy for scripted checks of tools/resources/prompts; **verify sampling support against your
Inspector version** (sampling is interactive, usually via the UI). The offline analogue without a live host is
[`tests/test_m7_offline.py`](../tests/test_m7_offline.py) (`FakeSamplingSession` + a tools-list smoke).

> **A real sampling run across hosts is user-run** (Claude Desktop supports it; OpenCode/Kilocode — confirm
> before production use), GAP-VERIFY-006.

## See also
[`M9.7_CONTRACT.en.md`](M9.7_CONTRACT.en.md) (**the SPI contract**: registry, three kinds, reference
adapters, licence boundary, gate) · [`LOCAL_MODELS.en.md`](LOCAL_MODELS.en.md) (model/runtime catalog +
calculators) · [`M6_CONTRACT.en.md`](M6_CONTRACT.en.md) (provider-agnostic brain) ·
[`M7_CONTRACT.en.md`](M7_CONTRACT.en.md) (MCP exposure) · [`M9.1_CONTRACT.en.md`](M9.1_CONTRACT.en.md) §4
(storageState lifecycle) · [`DEVELOPMENT.en.md`](DEVELOPMENT.en.md).
