# Security Policy

## Reporting a vulnerability

Please report security issues **privately**, not via public GitHub issues.

- Use **GitHub Security Advisories** (repository → *Security* → *Report a vulnerability*), or
- email the maintainer (see the repository owner's profile).

Include: the affected component, a version or commit SHA, reproduction steps, and impact.
Components include the `agentctl` CLI (it spawns everything else, inherits the host env and owns the
destructive subcommands `purge-store` / `purge-service` / `sweep-downloaded`), `brain`,
`pw-executor`, `orchestrator`, `store-gateway`, `control-api`, and the dev-only MV3 browser
extension in `extension/` (dev-only means it ships in no image or package — it does **not** mean
reports about it are out of scope). This list is illustrative, not exhaustive: the authority on
which process owns what is [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md) — its §3 diagram plus the
§4 boundary table, which covers the surfaces added after the diagram was drawn. If you are unsure
which component owns the behaviour, just say what you observed and where. We aim to acknowledge
within a few business days.

## Scope

Sentinel is a UI-testing agent. Of particular interest:

- Secret handling (API keys, app-under-test credentials) — these must **never** reach traces,
  transcripts, or logs (only `prompt_HASH`, never prompt content; tokens counted, never values).
- The `pw-executor` browser boundary and any code that runs against an app-under-test.
- The `control-api` HTTP control-plane (bearer-token REST + SSE `/v1/runs/{id}/events` + hand-rolled WebSocket `/v1/stream` + OpenAI-compat shim, CORS-allowlisted, localhost-bind) — the largest attack surface since M9.3.
- The gRPC / MCP transport surfaces.

For the full trust-boundary analysis (STRIDE-lite over agentctl → brain → pw-executor → Chromium →
AUT-cert → LLM-endpoint → store-gateway → control-api, with assets, current mitigations, residual risk and the
owning milestone for each open item), see [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md).

> Note: **active security testing of an app-under-test** (XSS/CSRF/IDOR scanning) is a *separate*,
> authorization-gated module (planned, not in the functional core) — see `docs/M9_CONTRACT.md` §L.

## Supported versions

Pre-1.0 — only `main` is supported. Pin a commit SHA for reproducible runs.
