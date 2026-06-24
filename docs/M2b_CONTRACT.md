# M2b Contract — "Service Layer" (frozen 2026-06-24)

Goal: pay down the ADR-012 interim deviation — introduce the **Go `store-gateway` (gRPC)** as the
sole SQLite writer (restores ADR-007) and migrate the brain↔pw-executor transport to the **MCP SDK**
(closes GAP-VERIFY-002). Pure infrastructure: **no new user-facing value**, and the **least
offline-testable** milestone (needs live Go+Python+Node processes). Split into two independent halves.

## Scope split (do M2b-1 first)
- **M2b-1 — Go store-gateway + gRPC + proto.** Replace `brain/store.py`'s SQLite with a Go service
  owning the DB; brain talks to it over gRPC. Restores single-writer (ADR-007).
- **M2b-2 — MCP-SDK transport.** Replace the hand-rolled newline JSON-RPC (brain↔pw-executor) with
  the MCP SDK; `pw-executor` becomes an MCP server, brain an MCP client. Closes GAP-VERIFY-002,
  realizes ADR-002's "LangGraph binds MCP tools natively".

## Key lever (low-risk swaps)
`brain/store.py` and `brain/executor.py` are clean interfaces. M2b swaps their **implementations**
only — `healing.py` / `replay.py` / `calibrate.py` / `graph.py` keep calling the same
`store.<method>` / `ex.call(method, **params)` and are **unchanged**. This bounds the blast radius.

## M2b-1 — proto / store-gateway / gRPC
**proto (`proto/persistence.proto`, protobuf3)** — `PersistenceService` mirrors today's `Store` 1:1:
`Lookup`, `EvictStale`, `SaveLocator`, `BumpUsed`, `AppendAudit`, `SaveGolden`, `GetGolden`,
`RecordStep`(→quarantined bool), `IsQuarantined`, `ClearQuarantine`, `AuditRows`(for calibrate).
Stubs generated for Go + Python in CI; `.proto`-hash asserted against checked-in stubs (drift = build fail).

**Go store-gateway (`internal/store/`)** — owns the SQLite (WAL) + schema migrations + single-writer;
implements `PersistenceService`. Lifecycle: **agentctl spawns it as a child** over a Unix-domain socket
(local/CI), passing the address to brain via `STORE_ADDR`. TCP option for K3s later.

**Brain (`brain/store.py`)** — reimplemented as a thin gRPC client preserving the EXACT current method
signatures (drop-in). `calibrate.py`'s direct `store.db` access is replaced by an `AuditRows` RPC.

**Toolchain (VERIFY at impl):** `protoc`/`buf`; Go `google.golang.org/grpc` + `google.golang.org/protobuf`;
Python `grpcio` + `grpcio-tools`; a pure-Go SQLite driver (`modernc.org/sqlite`, cgo-free) preferred.

**M2b-1 gate:** with store-gateway running, explore + baseline + replay + calibrate behave identically
(same exit codes / heal / golden) ; `grep -r sqlite3 brain/` returns nothing (brain holds no DB handle);
Go unit tests for the gateway; the Python offline suite runs against an in-proc fake store implementing
the same interface (so trust-layer/heal tests stay browser+grpc-free).

## M2b-2 — MCP-SDK transport
**pw-executor** — rewrite `server.ts` on `@modelcontextprotocol/sdk` (`McpServer` + `StdioServerTransport`),
registering the 7 tools (navigate, snapshot, click, probe, interactives, screenshotHash, traceStop) with
input schemas; same behaviors, stdout reserved for the protocol.
**Brain (`brain/executor.py`)** — replace the hand-rolled client with an MCP stdio client (`mcp`), wrapped
behind the existing `Executor.call(method, **params)` so `graph`/`healing`/`replay` are unchanged. Keep a
feature-flag fallback to the JSON-RPC client (GAP-VERIFY-002 risk mitigation).
**Toolchain (VERIFY):** `@modelcontextprotocol/sdk` (npm), `mcp` (pypi) — confirm versions + the
`McpServer.registerTool` / `ClientSession.call_tool` API before coding (anti-hallucination).
**M2b-2 gate:** `tools/list` returns the 7 tools; M0–M3 live gates still pass over MCP transport.

## Testability (honest)
Cross-process gRPC + MCP-stdio can't be fully exercised offline here → the **live gates are handed to
the user** (as for M0–M3). Offline coverage is preserved via: in-proc fake `PersistenceService` for the
Python suite, Go unit tests for the gateway, and MCP tool-schema contract tests.

## ADRs
- **ADR-015 (M2b-1):** store-gateway = Go gRPC service spawned by agentctl over UDS; `brain/store.py`
  becomes a thin gRPC client preserving its method interface (drop-in). Restores ADR-007.
- **ADR-016 (M2b-2):** pw-executor migrates to the MCP SDK; brain wraps an MCP client behind the existing
  `ex.call` interface; JSON-RPC retained as a documented fallback.

## Out of scope
M4b observability (Go report-service, OTel→Tempo, Prometheus HTTP) · M5 (visual heal PoC, K3s/ArgoCD).
