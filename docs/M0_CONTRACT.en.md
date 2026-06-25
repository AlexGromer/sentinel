# M0 Contract — "Hello Browser" (frozen 2026-06-23)

> 🌐 [Русский](M0_CONTRACT.md) (основная версия) · **English**

Goal of M0: **prove the wire across all three languages and produce a `trace.zip`** — NOT intelligence.
Scope excludes: gRPC, store-gateway, LLM, healing, the full 9-node LangGraph (those are M1+).

```
agentctl run --target <URL> [--artifact-dir DIR] [--mode explore]   (Go)
  └─ spawns: python3 -m brain      (env contract, Boundary A)        (Python)
       └─ spawns: node pw-executor/dist/server.js  (stdio)           (TypeScript)
            ↑ newline-delimited JSON-RPC 2.0  (Boundary B, MCP-aligned)
```

## Boundary A — agentctl → brain (subprocess + env)
agentctl generates `RUN_ID`, creates the artifact dir, then spawns `python3 -m brain`
(cwd = repo root, `PYTHONPATH` = repo root) with env:

| Env var | Meaning |
|---------|---------|
| `TARGET_URL` | URL to perceive (required) |
| `RUN_ID` | random hex id (agentctl-generated) |
| `RUN_MODE` | `explore` (M0 only) |
| `ARTIFACT_DIR` | absolute output dir (agentctl creates it; default `./runs/<RUN_ID>`) |
| `PW_EXECUTOR_CMD` | command brain uses to launch pw-executor (e.g. `node <repo>/pw-executor/dist/server.js`) — Go owns this config |

brain streams stdout/stderr to agentctl (inherited). Exit 0 = success (trace.zip present & non-empty), else non-zero. agentctl propagates the exit code.

## Boundary B — brain → pw-executor (JSON-RPC 2.0 over stdio)
**Transport:** one JSON object per line. **CRITICAL:** pw-executor stdout carries ONLY JSON-RPC; all logs go to stderr.
- Request: `{"jsonrpc":"2.0","id":<n>,"method":"<m>","params":{...}}\n`
- Response: `{"jsonrpc":"2.0","id":<n>,"result":{...}}\n` or `{...,"error":{"code":<c>,"message":"<m>"}}\n`

| Method | Params | Result |
|--------|--------|--------|
| `initialize` | — | `{name, version, capabilities[]}` — also lazily launches Chromium + starts tracing |
| `browser.navigate` | `{url}` | `{url, title, status}` |
| `browser.snapshot` | — | `{ariaSnapshot: <string>, nodeCount}` (Playwright `ariaSnapshot()`) |
| `browser.traceStop` | `{path}` | `{path}` — stops tracing, writes `path` (the trace.zip) |
| `shutdown` | — | `{ok:true}` — closes browser, server exits 0 |

pw-executor session: lazily on first call → `chromium.launch({headless:true})` → `newContext()` → `context.tracing.start({screenshots:true, snapshots:true})` → `newPage()`.

## brain "perceive" flow (M0)
`initialize` → `browser.navigate(TARGET_URL)` → `browser.snapshot()` (print aria tree to stdout + write `ARTIFACT_DIR/snapshot.aria.yaml`) → `browser.traceStop(ARTIFACT_DIR/trace.zip)` → `shutdown`. Verify `trace.zip` exists & non-empty → exit 0/1.

## Acceptance gate (Given/When/Then)
- **GIVEN** a reachable target URL and a built pw-executor + brain + agentctl,
- **WHEN** `agentctl run --target https://example.com` is executed,
- **THEN** the accessibility tree is printed to stdout AND `runs/<RUN_ID>/trace.zip` exists with size > 0 AND exit code = 0.

## M1 deltas (NOT in M0)
- Replace hand-rolled JSON-RPC with the **MCP SDK** (`@modelcontextprotocol/sdk` server + Python MCP client) — GAP-VERIFY-002.
- Wrap `perceive` as one node in a real **LangGraph StateGraph** (then add the other 8 nodes).
- Introduce `RunState`, plan node (Opus 4.8), coverage convergence, `plan_hash`.
