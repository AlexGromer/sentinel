"""One rule for turning an address into a gRPC target.

Two conventions are alive in this codebase and both are legitimate: Go processes hand the brain a BARE
unix socket path (`agentctl` startGateway, `cmd/orchestrator` ORCH_ADDR), while configuration written
by a person carries a full target (`unix:/abs/path`, `host:port`). gRPC accepts the second and treats
the first as a DNS name — so a bare path does not fail loudly, it resolves to nothing.

That failure mode is the reason this module exists rather than a second copy of a two-line fix. Both
of the places that dial gRPC from Python swallow their errors on purpose — the store projection is
best-effort, and the run-control client must never break a run over telemetry — so a bare path became
a silent no-op in both. It cost the `chats` projection (ADR-050, never written by any deployment) and
the entire brain↔orchestrator channel: budget reconciliation, the abort signal and operator
takeover/return (ADR-021/ADR-054) all degraded to "continue" without a word, because ORCH_ADDR is a
bare path and always has been.
"""


def target(addr: str) -> str:
    """Normalise an address into a gRPC target. Idempotent: applying it twice cannot change the result.

    A leading "/" is our bare-socket-path convention and becomes `unix:<path>`. Anything else already
    names a scheme or a host:port and is handed through untouched — prefixing THAT produced
    `unix:unix:/path`, a target that never connects.
    """
    a = (addr or "").strip()
    if not a:
        return a
    if a.startswith("/"):
        return "unix:" + a
    return a
