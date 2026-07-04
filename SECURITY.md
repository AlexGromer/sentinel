# Security Policy

## Reporting a vulnerability

Please report security issues **privately**, not via public GitHub issues.

- Use **GitHub Security Advisories** (repository → *Security* → *Report a vulnerability*), or
- email the maintainer (see the repository owner's profile).

Include: affected component (`brain` / `pw-executor` / `orchestrator` / `store-gateway` / `control-api`), version or
commit SHA, reproduction steps, and impact. We aim to acknowledge within a few business days.

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
